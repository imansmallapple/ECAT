"""ClaudeCodeRunner — wraps the `claude` CLI subprocess."""

from __future__ import annotations

import functools
import json
import os
import subprocess
from pathlib import Path

from ecat.runner.base import AgentRunner


@functools.cache
def detect_subagent_tool(config_dir: str | None = None) -> str:
    """Name of Claude Code's subagent tool in the installed CLI: "Agent" or "Task".

    Claude Code has shipped this tool under both names depending on the
    version. Start `claude` against a closed local port: it prints its
    `system init` event (with the tool list) before any API request, so this
    costs no tokens. Falls back to "Agent" if the probe fails.
    """
    env = dict(os.environ)
    env.update({
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:9",
        "ANTHROPIC_API_KEY": "probe",
        "ANTHROPIC_AUTH_TOKEN": "probe",
    })
    if config_dir:
        env["CLAUDE_CONFIG_DIR"] = config_dir
    try:
        proc = subprocess.Popen(
            ["claude", "-p", ".", "--output-format", "stream-json", "--verbose", "--max-turns", "1"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", env=env,
        )
    except OSError:
        return "Agent"
    tools: list[str] = []
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("type") == "system" and event.get("subtype") == "init":
                tools = list(event.get("tools") or [])
                break
    finally:
        proc.kill()
        proc.wait(timeout=10)
    if "Agent" in tools:
        return "Agent"
    if "Task" in tools:
        return "Task"
    return "Agent"


class ClaudeCodeRunner(AgentRunner):
    """Runs the `claude` CLI (Claude Code) as an isolated subprocess."""

    # Claude Code's stream-json emits one JSON event per line including
    # tool_use / tool_result, so base.py can persist the raw stream to
    # transcripts/<role>.stream.jsonl while still extracting the final
    # assistant text for the human-readable <role>.log.
    emits_stream_json = True

    def __init__(
        self,
        *,
        config_dir: Path,
        timeout_s: int = 2700,
        extra_args: list[str] | None = None,
        add_dirs: list[Path] | None = None,
        transcript_dir: Path | None = None,
        resume_session: bool = False,
    ) -> None:
        super().__init__(
            config_dir=config_dir,
            timeout_s=timeout_s,
            extra_args=extra_args,
            add_dirs=add_dirs,
            transcript_dir=transcript_dir,
        )
        # Opt-in cross-call session reuse: when True, after the first run we
        # remember the session_id emitted in the stream and pass it as
        # `--resume <sid>` on subsequent runs. Saves cache_create cost across
        # refine iters at the price of accumulating context in the same
        # session. Caller is responsible for only enabling on roles where
        # continuity is desirable (Generator yes, Discriminator no).
        self.resume_session = resume_session
        self._last_session_id: str | None = None

    def _build_cmd(self, model: str) -> list[str]:
        # `--output-format stream-json` requires `--verbose` when used with
        # `--print` (otherwise claude exits 1 with a usage error).
        cmd = [
            "claude", "--model", model, "--print", "--dangerously-skip-permissions",
            "--output-format", "stream-json", "--verbose",
        ]

        if self.resume_session and self._last_session_id:
            cmd += ["--resume", self._last_session_id]

        # Inject agent config dir so Claude Code discovers its CLAUDE.md
        cmd += ["--add-dir", str(self.config_dir.resolve())]

        # Load skills from the agent's skills/ directory if it exists
        skills_dir = self.config_dir / "skills"
        if skills_dir.is_dir():
            cmd += ["--plugin-dir", str(skills_dir.resolve())]

        for d in self.add_dirs:
            cmd += ["--add-dir", str(d.resolve())]

        cmd += self.extra_args
        return cmd

    def _build_prompt_args(self, prompt: str) -> list[str]:
        # Return [] so base.py delivers the prompt via stdin instead of as a
        # CLI argument.  Avoids Windows `CreateProcess` 32 767-char limit when
        # the prompt embeds large artifacts (e.g. Spec review feeds the full
        # `spec/baseline/` contents back to the discriminator).  `claude
        # --print` accepts the prompt on stdin when no positional is supplied.
        return []

    def compact(
        self,
        model: str,
        env: dict[str, str] | None = None,
        cwd: Path | None = None,
    ) -> bool:
        """Send /compact to the current session to compress conversation history.

        No-op when resume_session is disabled or no session has been established.
        Returns True if the compact call succeeded.
        """
        if not self.resume_session or not self._last_session_id:
            return False
        result = self.run(
            prompt="/compact",
            model=model,
            env=env,
            cwd=cwd,
            role="generator-compact",
        )
        return result.success

    def _capture_session_id(self, raw_stdout: str, *, success: bool = True) -> None:
        """Pluck the session_id from the first `system init` event in the
        stream-json stdout and remember it for the next call (only when
        resume_session is enabled).
        Only on success: a failed run must not mark the session "ran once",
        or the next `--continue` would resume a broken session."""
        if not success or not self.resume_session or not raw_stdout:
            return
        import json
        for line in raw_stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") == "system" and ev.get("subtype") == "init":
                sid = ev.get("session_id")
                if isinstance(sid, str) and sid:
                    self._last_session_id = sid
                return
