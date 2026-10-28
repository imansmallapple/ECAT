"""Abstract base class for agent runners."""

from __future__ import annotations

import contextlib
import json
import logging
import shutil
import subprocess
import sys
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)


def _stamp(line: str, t0: float) -> str:
    """给一行 stream-json 事件加上时间，供事后排查"哪一步慢了"。

    claude_code 的 stream-json 事件本身不带时间，落盘后只能靠文件 mtime 猜。加两个
    字段：`_ecat_time`（本地墙钟，秒级，和 run.log 对得上）、`_ecat_elapsed_s`
    （距本次 agent 调用开始的秒数，直接就是相对耗时）。`_ecat_` 前缀避免与
    Claude Code 自己的字段撞名。

    直接在 `{` 后插字符串而不是 json.loads/dumps 往返 —— 单行可达数 MB（工具结果），
    往返一次比写盘还贵。非 JSON 行和空对象原样返回（后者插进去会留下尾逗号）。
    """
    body = line.lstrip()
    if not body.startswith("{") or not body[1:].lstrip().startswith('"'):
        return line
    indent = line[: len(line) - len(body)]
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    return (f'{indent}{{"_ecat_time":"{now}",'
            f'"_ecat_elapsed_s":{time.monotonic() - t0:.2f},{body[1:]}')


def _resolve_cmd(cmd: list[str], env: dict[str, str] | None = None) -> list[str]:
    """On Windows, .CMD/.BAT files must be invoked via cmd.exe /c; .EXE files run directly.

    Uses *env*'s PATH for the lookup so that test shims injected via a modified
    PATH env dict are found correctly (CreateProcess uses the parent's PATH, not
    the child env, when searching for the module to launch).
    """
    if sys.platform != "win32" or not cmd:
        return cmd
    path = env.get("PATH") if env else None
    exe = shutil.which(cmd[0], path=path)
    if exe and exe.upper().endswith((".CMD", ".BAT")):
        return ["cmd", "/c"] + cmd
    return cmd


def _taskkill_tree(pid: int) -> None:
    """Windows kernel-side tree kill (`taskkill /T /F`). Catches descendants a
    psutil enumerate-then-kill snapshot missed — the OS walks parent-PID links
    itself. No-op off Windows; best-effort (never raises)."""
    if sys.platform != "win32":
        return
    with contextlib.suppress(Exception):
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
        )


def _kill_process_tree(pid: int) -> None:
    """Kill the process at *pid* and every descendant.

    On Windows, killing a cmd.exe wrapper does NOT propagate to its node.exe
    children, so the inner CLI (claude / opencode) keeps running and its
    stdout pipe stays open — making the reader block on a never-arriving EOF.
    psutil walks the tree and kills every descendant first. Because the CLI
    spawns subagents continuously, one enumerate-then-kill snapshot can miss a
    grandchild spawned just after the snapshot; we retry a few rounds and then
    fall back to `taskkill /T /F` (kernel-side walk) as a belt-and-suspenders.
    """
    try:
        import psutil
    except ImportError:
        log.warning("psutil not installed; killing subprocess tree via taskkill for pid=%d", pid)
        _taskkill_tree(pid)
        return

    for _ in range(3):
        try:
            parent = psutil.Process(pid)
        except psutil.NoSuchProcess:
            break
        # Kill children first so they don't get reparented to init/services.
        procs = parent.children(recursive=True) + [parent]
        for p in procs:
            with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                p.kill()
        # Give the OS a moment to reap; loop again to catch late-spawned kids.
        psutil.wait_procs(procs, timeout=3)

    # Belt-and-suspenders: kernel-side walk catches anything the snapshots above
    # missed (no-op if the tree is already gone / not on Windows).
    _taskkill_tree(pid)


@dataclass
class RunResult:
    """Outcome of a single agent run."""

    returncode: int
    stdout: str
    stderr: str
    duration_s: float
    transcript_path: Path | None = None
    # Full stream-json (all events) for stream-json runners — lets a consumer
    # recover the verdict from an earlier turn when the model appends prose after
    # its JSON. None for non-stream runners. Only read on a parse-failure fallback.
    raw_stream: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    cache_read: int = 0
    cache_write: int = 0

    @property
    def success(self) -> bool:
        return self.returncode == 0


class AgentRunner(ABC):
    """Abstract runner that wraps an agent CLI subprocess."""

    # Maximum number of concurrent subprocess invocations allowed for this runner.
    # Override in subclasses whose underlying CLI cannot safely handle concurrent
    # access to shared state (e.g. SQLite databases).
    # 取够大的值，让所有 agentic 插件一批跑完 —— 实际并发被插件数封顶
    # （engine.py: min(len(agentic_plugins), max_concurrent)），当前插件集 9 个带
    # judge_prompt，留出加插件的余量。两个 runtime 共用此值。
    max_concurrent: int = 20

    # When True, the CLI's stdout is line-delimited JSON (one event per line,
    # e.g. Claude Code's `--output-format stream-json`).  base.py tees the
    # raw stream to `<role>.stream.jsonl` and reconstructs the final
    # assistant text for the human-readable `<role>.log`.
    emits_stream_json: bool = False

    def __init__(
        self,
        *,
        config_dir: Path,
        timeout_s: int = 2700,
        extra_args: list[str] | None = None,
        add_dirs: list[Path] | None = None,
        transcript_dir: Path | None = None,
        on_stderr_line: Callable[[int, str], None] | None = None,
    ) -> None:
        self.config_dir = config_dir
        self.timeout_s = timeout_s
        self.extra_args: list[str] = extra_args or []
        self.add_dirs: list[Path] = add_dirs or []
        self.transcript_dir = transcript_dir
        # 逐行 stderr 钩子：进程还活着时，每读到一行 stderr 就回调一次。默认 None
        #（不设）意味着行为与旧版完全一致。只有需要"实时盯 stderr、主动终止进程树"
        # 的 runner 才设置它 —— 目前仅 OpencodeRunner 用于检测 429 限流重试。
        self.on_stderr_line = on_stderr_line

    @abstractmethod
    def _build_cmd(self, model: str) -> list[str]:
        """Return the base argv list for the subprocess (flags only, no prompt)."""

    def _final_text(self, stream_stdout: str) -> str | None:
        """从事件流里取终答。默认按 Claude Code 的 schema；其它 runtime 覆写本方法。"""
        return _extract_final_text(stream_stdout)

    def _usage(self, stream_stdout: str) -> dict[str, int]:
        """从事件流里取 token 用量。默认按 Claude Code 的 schema；其它 runtime 覆写。

        返回的键沿用 Claude Code 的命名（input_tokens / output_tokens /
        cache_read_input_tokens / cache_creation_input_tokens），run() 只认这几个。
        """
        return _extract_usage(stream_stdout)

    def _build_prompt_args(self, prompt: str) -> list[str]:
        """Return extra argv that encodes the prompt.

        Return [] (default) to deliver the prompt via stdin instead of as a
        CLI argument — this avoids the Windows cmd.exe ~8 191-char limit.
        Override in subclasses whose CLI requires the prompt as an argument
        (e.g. ClaudeCodeRunner passes ["--", prompt]).
        """
        return []

    def _build_cwd_args(self, cwd: Path | None) -> list[str]:
        """Extra CLI args derived from cwd. Override in subclasses that need a flag."""
        return []

    def run(
        self,
        prompt: str,
        model: str,
        env: dict[str, str] | None = None,
        cwd: Path | None = None,
        role: str = "agent",
    ) -> RunResult:
        """Run the agent subprocess and return a RunResult."""
        prompt_args = self._build_prompt_args(prompt)
        cmd = _resolve_cmd(
            self._build_cmd(model=model) + self._build_cwd_args(cwd) + prompt_args,
            env,
        )
        # Model is logged once at session start (Models: generator=... | discriminator=...)
        # and doesn't change across iterations — suppress the per-run repeat.
        log.debug("Running %s with model=%s", role, model)
        log.debug("cmd: %s", " ".join(cmd[:10]))
        start = time.monotonic()

        # Deliver prompt via stdin only when the CLI doesn't take it as an arg.
        stdin_input = prompt if not prompt_args else None

        # We use Popen + manual timeout so that on timeout we can recursively
        # kill the entire process tree. On Windows the cmd.exe wrapper does not
        # propagate TerminateProcess to its children (e.g. node.exe running
        # opencode), so subprocess.run's built-in timeout often hangs forever
        # in process.communicate() draining a never-closing stdout pipe.
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE if stdin_input is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            env=env,
            cwd=cwd,
        )
        # stream-json runners tee stdout to <role>.stream.jsonl line-by-line as
        # events arrive, so `ecat run` (and `tail -f`) can watch what the agent
        # is doing live instead of waiting for the whole turn to finish. Other
        # runners keep the simpler communicate() path.
        stream_live = self.emits_stream_json and self.transcript_dir is not None
        stream_path = (
            self.transcript_dir / f"{role}.stream.jsonl"
            if stream_live and self.transcript_dir is not None
            else None
        )

        if stream_live:
            stdout, stderr, returncode, timed_out = self._stream_and_capture(
                proc, stdin_input, stream_path, self.timeout_s, role
            )
            if timed_out:
                from ecat.errors import TimeoutError as EcatTimeout

                if self.transcript_dir:
                    with contextlib.suppress(OSError):
                        self._write_transcripts(
                            role=role,
                            stdout=stdout or "",
                            stderr=stderr or "",
                            timed_out_after=self.timeout_s,
                            stream_already_written=True,
                        )
                raise EcatTimeout(f"{role} timed out after {self.timeout_s}s")
        else:
            try:
                stdout, stderr = proc.communicate(input=stdin_input, timeout=self.timeout_s)
                returncode = proc.returncode
            except subprocess.TimeoutExpired as exc:
                from ecat.errors import TimeoutError as EcatTimeout

                log.warning(
                    "%s exceeded %ds — killing process tree (pid=%d)",
                    role, self.timeout_s, proc.pid,
                )
                _kill_process_tree(proc.pid)
                # Drain any buffered output now that the tree is gone; bound it so
                # we never re-hang here even if a pipe somehow stays open.
                try:
                    stdout, stderr = proc.communicate(timeout=10)
                except Exception:
                    stdout, stderr = "", ""
                # Persist whatever stdout/stderr we captured before raising —
                # otherwise the transcript-writing path below is skipped and we
                # lose all forensic data on timeouts (the very runs we most need
                # to debug).
                if self.transcript_dir:
                    with contextlib.suppress(OSError):
                        self._write_transcripts(
                            role=role,
                            stdout=stdout or "",
                            stderr=stderr or "",
                            timed_out_after=self.timeout_s,
                        )
                raise EcatTimeout(
                    f"{role} timed out after {self.timeout_s}s"
                ) from exc

        raw_stdout = stdout or ""
        final_stderr = stderr or ""
        # For stream-json runners, downstream consumers (GradientBuilder, judge
        # parsers, etc.) expect the human-readable final message text, not the
        # raw event stream.  Extract it here so RunResult.stdout has the same
        # semantics across runtimes.  The raw JSONL is still persisted to
        # `<role>.stream.jsonl` for forensic inspection.
        if self.emits_stream_json and raw_stdout.strip():
            extracted = self._final_text(raw_stdout)
            final_stdout = extracted if extracted is not None else raw_stdout
            # Give the subclass a chance to capture per-stream metadata
            # (e.g. session_id for opt-in resume).  No-op by default.
            # Only on success: a failed run must not mark the session "ran once",
            # or the next round would `--continue` a broken session.
            capture = getattr(self, "_capture_session_id", None)
            if callable(capture):
                with contextlib.suppress(Exception):
                    capture(raw_stdout, success=(returncode == 0))
        else:
            final_stdout = raw_stdout

        duration = time.monotonic() - start
        transcript_path: Path | None = None

        if self.transcript_dir:
            transcript_path = self._write_transcripts(
                role=role, stdout=raw_stdout, stderr=final_stderr,
                stream_already_written=stream_live,
            )

        tok_usage: dict[str, int] = {}
        if self.emits_stream_json and raw_stdout.strip():
            tok_usage = self._usage(raw_stdout)

        run_result = RunResult(
            returncode=returncode,
            stdout=final_stdout,
            stderr=final_stderr,
            duration_s=duration,
            transcript_path=transcript_path,
            raw_stream=raw_stdout if self.emits_stream_json else None,
            tokens_in=tok_usage.get("input_tokens", 0),
            tokens_out=tok_usage.get("output_tokens", 0),
            cache_read=tok_usage.get("cache_read_input_tokens", 0),
            cache_write=tok_usage.get("cache_creation_input_tokens", 0),
        )

        if not run_result.success:
            log.warning(
                "%s exited with code %d (%.1fs)", role, returncode, duration
            )
            if final_stderr.strip():
                log.warning("%s stderr: %s", role, final_stderr.strip()[:500])
            if final_stdout.strip():
                log.warning("%s stdout: %s", role, final_stdout.strip()[:500])
            quota_reason = _detect_quota_exhaustion(final_stdout, final_stderr)
            if quota_reason:
                from ecat.errors import AgentQuotaExhausted

                raise AgentQuotaExhausted(role, quota_reason)

        return run_result


    def compact(
        self,
        model: str,
        env: dict[str, str] | None = None,
        cwd: Path | None = None,
    ) -> bool:
        """Compress the agent's conversation history in-session. No-op by default."""
        return False

    def _stream_and_capture(
        self,
        proc: subprocess.Popen[str],
        stdin_input: str | None,
        stream_path: Path | None,
        timeout_s: int,
        role: str,
    ) -> tuple[str, str, int, bool]:
        """Read the subprocess's stdout line-by-line, teeing each line to
        *stream_path* (flushed immediately) so a watcher can follow the agent
        live, while draining stderr and feeding stdin on separate threads to
        avoid a full-pipe deadlock. A watchdog kills the whole process tree if
        the run outlives *timeout_s*.

        Returns (stdout, stderr, returncode, timed_out).
        """
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        timed_out = {"v": False}

        def _feed_stdin() -> None:
            if stdin_input is None or proc.stdin is None:
                return
            with contextlib.suppress(OSError, ValueError):
                proc.stdin.write(stdin_input)
                proc.stdin.close()

        def _drain_stderr() -> None:
            # 同样边读边落盘。stderr 原本只在 _write_transcripts 里写进 <role>.log，
            # 而那是进程**退出后**才调用的 —— 卡死时正好一个字都留不下（实测 runs_20260820_1521
            # 只有 live tee 的 stream.jsonl，没有 generator.log）。诊断卡死要的恰恰是这份。
            if proc.stderr is None:
                return
            fh = None
            t0 = time.monotonic()
            try:
                if self.transcript_dir is not None:
                    self.transcript_dir.mkdir(parents=True, exist_ok=True)
                    fh = (self.transcript_dir / f"{role}.stderr.log").open(
                        "a", encoding="utf-8",
                    )
                # readline (not `for line in`) so we don't sit on a read-ahead
                # buffer — matters if stderr is interleaved with progress.
                for line in iter(proc.stderr.readline, ""):
                    stderr_parts.append(line)
                    if fh is not None:
                        fh.write(f"[{time.monotonic() - t0:8.2f}s] {line}")
                        fh.flush()
                    # 逐行钩子：进程存活期间实时观察 stderr（如 429 限流）。回调在
                    # daemon 线程里执行，必须自行保证线程安全；默认 None 不触发。
                    if self.on_stderr_line is not None:
                        try:
                            self.on_stderr_line(proc.pid, line)
                        except Exception:  # 钩子异常不得中断 stderr 落盘
                            log.exception("on_stderr_line callback raised")
            except (OSError, ValueError):
                pass
            finally:
                if fh is not None:
                    with contextlib.suppress(OSError):
                        fh.close()

        def _read_stdout() -> None:
            # Tee stdout line-by-line to the stream file (flushed) so a watcher
            # can follow the agent live. Runs on its OWN daemon thread — never
            # the main thread — so that if the child is killed on timeout but a
            # surviving grandchild keeps the stdout pipe open (EOF never comes),
            # this readline can block forever WITHOUT wedging run(): the main
            # thread gates on proc.wait(), not on this loop reaching EOF.
            fh = None
            t0 = time.monotonic()
            try:
                if stream_path is not None:
                    stream_path.parent.mkdir(parents=True, exist_ok=True)
                    fh = stream_path.open("a", encoding="utf-8")
                if proc.stdout is not None:
                    # readline yields each line the instant its newline arrives,
                    # so the tee is genuinely live; `for line in proc.stdout`
                    # would buffer ahead and defer the writes.
                    for line in iter(proc.stdout.readline, ""):
                        # 时间只加在落盘的那份；stdout_parts 保持原样，verdict 解析
                        # （_extract_final_text 等）看到的事件与 CLI 输出逐字一致。
                        stdout_parts.append(line)
                        if fh is not None:
                            fh.write(_stamp(line, t0))
                            fh.flush()
            except (OSError, ValueError):
                pass
            finally:
                if fh is not None:
                    with contextlib.suppress(OSError):
                        fh.close()

        def _watchdog() -> None:
            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    return
                time.sleep(0.5)
            if proc.poll() is None:
                timed_out["v"] = True
                log.warning(
                    "%s exceeded %ds — killing process tree (pid=%d)",
                    role, timeout_s, proc.pid,
                )
                _kill_process_tree(proc.pid)
                # Nudge the reader out of a blocked readline in case a survivor
                # still holds the pipe (best-effort; reader.join is the real bound).
                if proc.stdout is not None:
                    with contextlib.suppress(Exception):
                        proc.stdout.close()

        reader = threading.Thread(target=_read_stdout, daemon=True)
        workers = [
            threading.Thread(target=_feed_stdin, daemon=True),
            threading.Thread(target=_drain_stderr, daemon=True),
            threading.Thread(target=_watchdog, daemon=True),
        ]
        reader.start()
        for t in workers:
            t.start()

        # Gate on the DIRECT child, not on stdout EOF. On normal exit the child
        # dies and this returns at once; on timeout the watchdog kills the tree,
        # which also makes this return — even if a grandchild keeps the pipe open
        # (that would block the reader thread forever, but it is a daemon).
        #
        # Bounded wait: if the watchdog fired but the direct child survived its
        # tree-kill (psutil AccessDenied on Windows can refuse p.kill() even when
        # taskkill also found nothing), proc.wait() would hang forever and take the
        # whole run() with it — precisely the timeout we tried to recover from.
        # Kill the direct child directly, then wait with a hard ceiling.
        if timed_out["v"]:
            with contextlib.suppress(Exception):
                proc.kill()
            try:
                proc.wait(timeout=10)
            except (subprocess.TimeoutExpired, subprocess.SubprocessError):
                log.warning("%s (pid=%d) survived timeout kill; abandoning it",
                            role, proc.pid)
        else:
            proc.wait()
        # Let the reader drain the buffered tail, but bounded — never re-hang here.
        reader.join(timeout=10)
        for t in workers:
            t.join(timeout=5)
        if stream_path is not None:
            with contextlib.suppress(OSError):
                _truncate_jsonl(stream_path, max_bytes=20 * 1024 * 1024)
        return "".join(stdout_parts), "".join(stderr_parts), proc.returncode, timed_out["v"]

    def _write_transcripts(
        self,
        *,
        role: str,
        stdout: str,
        stderr: str,
        timed_out_after: int | None = None,
        stream_already_written: bool = False,
    ) -> Path:
        """Persist stdout/stderr to transcripts/.  For stream-json runners,
        also tee the raw JSONL stream to `<role>.stream.jsonl` and use the
        reconstructed final assistant text in `<role>.log`.

        *stream_already_written* is set when `run()` already teed the stream
        live via `_stream_and_capture`, so we skip re-appending the whole
        stdout (which would duplicate the file) and only refresh the `.log`.
        """
        assert self.transcript_dir is not None
        self.transcript_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.transcript_dir / f"{role}.log"

        if self.emits_stream_json and stdout.strip():
            if not stream_already_written:
                stream_path = self.transcript_dir / f"{role}.stream.jsonl"
                with stream_path.open("a", encoding="utf-8") as fh:
                    fh.write(stdout if stdout.endswith("\n") else stdout + "\n")
                _truncate_jsonl(stream_path, max_bytes=20 * 1024 * 1024)
            human_stdout = self._final_text(stdout) or stdout
        else:
            human_stdout = stdout

        header = (
            f"=== STDOUT (TIMED OUT after {timed_out_after}s) ==="
            if timed_out_after is not None
            else "=== STDOUT ==="
        )
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(f"{header}\n{human_stdout}\n=== STDERR ===\n{stderr}\n")
        _truncate_log(log_path, max_bytes=10 * 1024 * 1024)
        return log_path


def _extract_final_text(stream_stdout: str) -> str | None:
    """Pull the final assistant text from a Claude Code stream-json stream.

    The last event is `{"type":"result","result":"<final text>",...}`.  Falls
    back to concatenating text blocks from the last `{"type":"assistant",...}`
    message if the result event is absent (interrupted run).
    """
    last_result: str | None = None
    last_assistant_text: list[str] = []
    for line in stream_stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        et = ev.get("type")
        if et == "result" and isinstance(ev.get("result"), str):
            last_result = ev["result"]
        elif et == "assistant":
            msg = ev.get("message") or {}
            content = msg.get("content") or []
            texts: list[str] = []
            if isinstance(content, list):
                for blk in content:
                    if isinstance(blk, dict) and blk.get("type") == "text":
                        txt = blk.get("text")
                        if isinstance(txt, str):
                            texts.append(txt)
            if texts:
                last_assistant_text = texts
    if last_result is not None:
        return last_result
    if last_assistant_text:
        return "\n".join(last_assistant_text)
    return None


def _truncate_jsonl(path: Path, max_bytes: int) -> None:
    """Like _truncate_log but drops the first (possibly partial) line so the
    remaining file is still valid JSONL.
    """
    size = path.stat().st_size
    if size <= max_bytes:
        return
    with path.open("rb") as fh:
        fh.seek(-max_bytes, 2)
        tail = fh.read()
    nl = tail.find(b"\n")
    if nl >= 0:
        tail = tail[nl + 1:]
    path.write_bytes(tail)


def _truncate_log(path: Path, max_bytes: int) -> None:
    """Keep only the last *max_bytes* of *path* to prevent runaway growth."""
    size = path.stat().st_size
    if size <= max_bytes:
        return
    with path.open("rb") as fh:
        fh.seek(-max_bytes, 2)
        tail = fh.read()
    path.write_bytes(tail)


# Fatal quota patterns. Both messages mean the next call will fail the same
# way until the user takes external action (wait for plan reset, or enable
# usage credits at claude.ai/settings/usage), so the driver should terminate
# the loop instead of falling back to an empty gradient.
_QUOTA_PATTERNS: tuple[tuple[str, str], ...] = (
    ("session limit", "Claude subscription session limit hit (resets later — see CLI output)"),
    ("Usage credits required", "1M-context billing wall — enable usage credits at claude.ai/settings/usage or switch to standard context"),
    ("Credit balance is too low", "credit balance too low — top up at the provider dashboard"),
)


def _detect_quota_exhaustion(stdout: str, stderr: str) -> str | None:
    """Return a human-readable reason if *stdout* or *stderr* matches a fatal
    quota pattern, else None. Case-insensitive substring match."""
    combined = (stdout + "\n" + stderr).lower()
    for needle, reason in _QUOTA_PATTERNS:
        if needle.lower() in combined:
            return reason
    return None


# modelUsage sub-keys (camelCase) → canonical usage keys (snake_case) used by
# RunResult / the run.log formatter.
_MODEL_USAGE_KEYS = {
    "inputTokens": "input_tokens",
    "outputTokens": "output_tokens",
    "cacheReadInputTokens": "cache_read_input_tokens",
    "cacheCreationInputTokens": "cache_creation_input_tokens",
}


def _usage_from_model_usage(model_usage: dict[str, object]) -> dict[str, int]:
    """Sum a result event's per-model `modelUsage` into canonical usage keys.

    `modelUsage` maps each model id to its *cumulative* token counts for the
    whole session (all turns), so summing across models yields the true
    consumption of the run.
    """
    totals: dict[str, int] = {}
    for per_model in model_usage.values():
        if not isinstance(per_model, dict):
            continue
        for cam, snake in _MODEL_USAGE_KEYS.items():
            v = per_model.get(cam)
            if isinstance(v, (int, float)):
                totals[snake] = totals.get(snake, 0) + int(v)
    return totals


def _extract_usage(stream_stdout: str) -> dict[str, int]:
    """Extract token usage from the last 'result' event in a Claude Code stream-json stream.

    Prefer the event's `modelUsage` block, which carries the *cumulative* token
    counts across every turn (and every model) of the session. The top-level
    `usage` field only reports the final assistant turn, so relying on it grossly
    undercounts multi-turn agentic runs (observed ~7-9x low on discriminator/
    generator sessions). Fall back to `usage` only when `modelUsage` is absent.
    """
    usage: dict[str, int] = {}
    for line in stream_stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "result":
            model_usage = ev.get("modelUsage")
            if isinstance(model_usage, dict) and model_usage:
                summed = _usage_from_model_usage(model_usage)
                if summed:
                    usage = summed
                    continue
            u = ev.get("usage") or {}
            if isinstance(u, dict):
                usage = {k: int(v) for k, v in u.items() if isinstance(v, (int, float))}
    return usage
