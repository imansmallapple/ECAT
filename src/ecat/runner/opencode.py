"""OpencodeRunner — wraps the `opencode` CLI subprocess."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import shutil
import threading
import uuid
from pathlib import Path

from ecat.runner.base import AgentRunner, RunResult, _kill_process_tree

log = logging.getLogger(__name__)

# 429 限流识别：匹配 AI SDK 抛出的 AI_APICallError JSON 里的 statusCode:429，以及
# zhipu/DashScope 等兼容端点常见文案。不含 `modelCode` / 配置类错误 —— 那是重试才无效的
# 另一类问题，不该走"限流重试"这条路。
#
# 中文限流文案（zhipu 实测「您的账户已达到速率限制，请您控制请求频率」、DeepSeek 的
# 「请求过于频繁」等）也必须覆盖 —— 否则连续 429 在 stderr 里出现却不被识别，软中止
# 不触发，进程就卡在限流重试上直到外层 timeout_s（实测 runs_20260902_1638 iter1 卡死）。
# `category=api_429` / `api_429` 是 opencode/deveco 内部对限流的标记，同行也命中。
#
# 最后一条把整个软中止从"只认 429"扩展到"认任何被 runtime 判定为可重试的持续错误"：
# deveco/opencode 把 429/500/网络/超时等会重试的错误统一打上
# `retryable error matched ... category=api_XXX attempt=N` 标记。任何这类错误持续出现
# （网络断了、服务端持续 500、限流不退）都会像 429 一样无限重试、进程不退，卡到外层
# timeout_s。软中止认这个统一标记，一网打尽所有"反复重试打转"的场景；一次性错误靠
# 下面的成功标志（step_finish 等）在恢复时清空计数，不会误杀。
_RATE_LIMIT_PATTERNS = (
    re.compile(r'"statusCode"\s*[:=]\s*429', re.IGNORECASE),
    re.compile(r"statusCode['\"]?\s*[:=]\s*429", re.IGNORECASE),
    re.compile(r"\b429\b.*(rate|Too Many Requests|quota|limit)", re.IGNORECASE),
    re.compile(r"(rate limit|too many requests|quota exceeded)", re.IGNORECASE),
    # 中文限流文案（zhipu / DeepSeek / DashScope 兼容端点常返回中文）
    re.compile(r"速率限制|请求频率|访问频率|请求过于频繁|调用过于频繁|限制.*频率", re.IGNORECASE),
    # opencode/deveco 的 retry 标记（服务端把限流归一成这个 category）
    re.compile(r"category\s*=\s*api_429\b|api_429", re.IGNORECASE),
    re.compile(r"(?:rate|qps).{0,12}limited|Too\s*many\s*requests", re.IGNORECASE),
    # 任何被 runtime 判定为 retryable 的持续错误（429/500/网络/超时都打这个标记）
    re.compile(r"retryable error matched", re.IGNORECASE),
)

# 成功标志：429 之间只要出现成功响应/正常事件，就认为限流已恢复，清空连续计数。
# 用宽松匹配 —— 命中任意一条即清零，避免把正常进度误判成持续的限流。
#
# 注意：**不能**把 `llm runtime selected` 当成功标志 —— 它只是 runtime 在**又一次发起
# 重试调用**时打印的（报错后立刻重试，必然伴随这条），不代表本次调用成功。若误把它当
# 恢复标志，每次 retryable 后都会被它清零，连续计数永远到不了 _RATE_LIMIT_STREAK_LIMIT，
# 软中止就失效（实测 runs_20260902_1638 iter1：卡死区间每条 retryable 后紧跟
# `llm runtime selected`，max streak 只到 2）。真正代表"恢复"的只有成功产出：step_finish /
# message.updated / text 事件 —— 它们只在一次调用**真正成功并产出**时才出现。
_RATE_LIMIT_SUCCESS_PATTERNS = (
    re.compile(r"step_finish", re.IGNORECASE),
    re.compile(r"message\.updated", re.IGNORECASE),
    re.compile(r'"type":"text"', re.IGNORECASE),
)

# 连续多少次 429 判定为"限流重试打转"，触发主动终止。
# 10 次而非 5：短时抖动（一次网络错误后恢复）不该杀，得是真正的"反复重试打转"
# 才算卡死。成功标志会在恢复时清零，所以这里只是连续失败次数下限。
_RATE_LIMIT_STREAK_LIMIT = 10


# Claude Code accepts named colors only; opencode requires hex codes or semantic
# keywords. Map Claude Code's 8 supported colors to readable hex equivalents.
# Claude Code 的命名色 → opencode 要的 hex。**未收录的名字会原样透传，被 opencode
# 判为 "Configuration is invalid" 并让整个调用失败**（实测 visual-judge.md 的 magenta
# 就这样让判别器 4 秒退出、整轮降级到 fallback gradient）。所以下面收全常见名字，
# 且 _rewrite_agent_frontmatter 对认不出的名字**删掉该行**而不是透传。
_COLOR_TO_HEX = {
    "magenta": "#cc00cc",
    "white": "#ffffff",
    "black": "#000000",
    "gray": "#808080",
    "grey": "#808080",
    "red": "#cc0000",
    "blue": "#0066cc",
    "green": "#00aa00",
    "yellow": "#ccaa00",
    "purple": "#a020f0",
    "orange": "#ff6600",
    "pink": "#ff66aa",
    "cyan": "#00aaaa",
}


def _rewrite_agent_frontmatter(text: str, parent_model: str | None = None) -> str:
    """Rewrite Claude Code-style subagent frontmatter into opencode's schema.

    Claude Code uses comma-separated capitalized tools (`Read, Glob`) and named
    colors (`green`); opencode requires an object of lowercase tools
    (`{read: true, ...}`) and hex colors (`#RRGGBB`). The seed files are kept
    in Claude Code format; this function produces an opencode-compatible copy
    on the fly.

    When *parent_model* is given (qualified `provider/model`), any `model:` line
    in the frontmatter is replaced with it so subagents use the same provider
    as the parent session. Without this, an agent declaring `model: opus` would
    fail when the parent runtime only has `deepseek` configured.
    """
    if not text.startswith("---"):
        return text
    end = text.find("\n---", 3)
    if end == -1:
        return text

    fm_inner = text[3:end]  # content between the two --- markers
    rest = text[end:]  # starts with "\n---" + body

    new_lines: list[str] = []
    fm_lines = fm_inner.splitlines()
    idx = 0
    while idx < len(fm_lines):
        line = fm_lines[idx]
        idx += 1
        stripped = line.lstrip()
        if stripped.startswith("tools:") and ":" in stripped:
            value = stripped.split(":", 1)[1].strip()
            # 多行列表形式：`tools:` 独占一行，工具名在后续 `  - Read` 各行。
            # 这些续行必须一起吃掉再重写，否则原样透传给 opencode 仍是 list。
            if not value:
                block: list[str] = []
                while idx < len(fm_lines) and fm_lines[idx].lstrip().startswith("- "):
                    block.append(fm_lines[idx].lstrip()[2:].strip().strip('"').strip("'"))
                    idx += 1
                block = [b.lower() for b in block if b]
                if block:
                    new_lines.append("tools:")
                    new_lines.extend(f"  {b}: true" for b in block)
                    continue
                new_lines.append(line)
                continue
            # 行内形式：`tools: Read, Glob` 或 `tools: ["Read","Glob"]`。opencode 只认
            # object，**列表也必须翻译** —— 早先按"已是 object/list 就别动"放行，结果
            # compose-fact-analyzer.md 让 opencode 判整份配置非法、整个 generator 起不来
            # （和 magenta 同一类：一个文件废掉全部）。`{` 开头是原生格式，原样保留。
            names: list[str] = []
            if value.startswith("["):
                names = [
                    n.strip().strip('"').strip("'").lower() for n in value.strip("[]").split(",")
                ]
            elif value and not value.startswith("{"):
                names = [n.strip().lower() for n in value.split(",")]
            names = [n for n in names if n]
            if names:
                new_lines.append("tools:")
                for n in names:
                    new_lines.append(f"  {n}: true")
                continue
            new_lines.append(line)
        elif stripped.startswith("color:"):
            value = stripped.split(":", 1)[1].strip().strip('"').strip("'")
            hex_value = _COLOR_TO_HEX.get(value.lower())
            if hex_value:
                new_lines.append(f'color: "{hex_value}"')
            elif value.startswith("#"):
                new_lines.append(line)  # 已经是 hex，原样保留
            else:
                # 认不出的命名色：**丢掉这一行**。color 只影响 UI 着色，而透传一个
                # opencode 不认的值会让整个 agent 配置非法、调用直接失败。
                log.debug("opencode: 丢弃无法映射的 color=%r", value)
        elif stripped.startswith("model:") and parent_model:
            new_lines.append(f"model: {parent_model}")
        else:
            new_lines.append(line)

    return "---" + "\n".join(new_lines) + rest


def _canonical_key_env(provider: str) -> str | None:
    """Return the env var name that the AI SDK for *provider* reads its key from.

    opencode delegates to the AI SDK's per-provider client, which by convention
    reads its key from a fixed env var. When ECAT sets a custom base_url and
    stores the key under a non-canonical env name (e.g. GLM_API_KEY), we copy it
    into the canonical slot so the SDK actually authenticates.
    """
    return {
        "openai": "OPENAI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
        "deepseek": "DEEPSEEK_API_KEY",
        "google": "GOOGLE_GENERATIVE_AI_API_KEY",
        "groq": "GROQ_API_KEY",
        "mistral": "MISTRAL_API_KEY",
    }.get(provider.lower())


def _stage_agents(
    seed_agents_dir: Path,
    target_agents_dir: Path,
    parent_model: str | None = None,
) -> int:
    """Copy *.md from seed_agents_dir into target_agents_dir, rewriting frontmatter.

    Returns the number of files staged. Errors on individual files are logged
    and skipped — one bad agent should not break the whole opencode invocation.
    """
    if not seed_agents_dir.is_dir():
        return 0
    target_agents_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    for md in seed_agents_dir.glob("*.md"):
        try:
            text = md.read_text(encoding="utf-8")
            (target_agents_dir / md.name).write_text(
                _rewrite_agent_frontmatter(text, parent_model=parent_model),
                encoding="utf-8",
            )
            count += 1
        except OSError as exc:
            log.debug("Failed to stage agent %s: %s", md.name, exc)
    return count


def _link_skills(src_skills: Path, dst_skills: Path) -> int:
    """把角色的 skills/ 暴露到 OPENCODE_CONFIG_DIR 下。

    opencode 只扫固定位置找 skill（实测 <config_dir>/skills/、<project>/.opencode/skills/
    等六处），`plugin` 数组与 skill 发现**无关** —— 早先靠 plugin 指向配置目录的做法从未
    生效，opencode 只会报 "missing package.json" 然后跳过，42 个 skill 一个都没加载。
    真实配置目录被 OPENCODE_CONFIG_DIR 换成了临时目录，所以要在那边补一份。

    优先建链接（零拷贝，42 个 skill 也不花时间）；Windows 未开开发者模式时 symlink
    会失败，退回 junction，再退回复制。返回可见的 skill 数量。
    """
    if not src_skills.is_dir():
        return 0
    if not dst_skills.exists():
        try:
            dst_skills.symlink_to(src_skills, target_is_directory=True)
        except (OSError, NotImplementedError):
            try:
                import _winapi  # noqa: PLC0415 — 仅 Windows 回退路径需要

                _winapi.CreateJunction(str(src_skills), str(dst_skills))
            except (ImportError, OSError):
                try:
                    shutil.copytree(src_skills, dst_skills)
                except OSError as exc:
                    log.warning(
                        "opencode: skills 无法暴露到 %s（%s）—— 本次调用 skill 不可用",
                        dst_skills,
                        exc,
                    )
                    return 0
    return sum(1 for _ in dst_skills.glob("*/SKILL.md"))


class OpencodeRunner(AgentRunner):
    """Runs the `opencode` CLI as an isolated subprocess.

    Role-level isolation uses OPENCODE_CONFIG_DIR (set by Workspace.agent_env).
    Skills are discovered via opencode.jsonc in that dir (generated by ecat init).

    Invocation-level isolation: each run() call creates a throw-away temp dir
    under config_dir/tmp/<uuid>/ so concurrent invocations never share an
    opencode SQLite database.  The temp dir is removed when the subprocess exits.
    """

    # `opencode run --format json` 逐行输出事件（type/timestamp/sessionID/part），
    # 与 Claude Code 的 schema 不同 —— 终答提取由 _final_text 覆写。
    emits_stream_json = True
    # 429 软中止日志里用的 CLI 名。deveco（fork）会覆写为 "deveco"，否则日志会误报
    # "opencode hit 10 consecutive 429..." 而实际跑的是 deveco。
    _cli_name = "opencode"

    def __init__(
        self,
        *,
        config_dir: Path,
        provider: str = "anthropic",
        timeout_s: int = 2700,
        extra_args: list[str] | None = None,
        add_dirs: list[Path] | None = None,
        transcript_dir: Path | None = None,
        base_url: str | None = None,
        api_key_env: str | None = None,
        resume_session: bool = False,
    ) -> None:
        super().__init__(
            config_dir=config_dir,
            timeout_s=timeout_s,
            extra_args=extra_args,
            add_dirs=add_dirs,
            transcript_dir=transcript_dir,
        )
        self.provider = provider
        self.base_url = base_url
        self.api_key_env = api_key_env
        # 跨调用续会话（`--continue` 续最近一个，不用抓 session id）。开启后必须放弃
        # 每次调用新建临时配置目录的隔离 —— 会话存在配置目录里，换目录就找不回来。
        # 只对串行角色开（generator）；判别器的 agentic judge 会并发调用 run()。
        self.resume_session = resume_session
        self._ran_once = False
        self._session_failed = False
        # 429 限流主动软中止：opencode 在进程内部对 429 退避重试，进程不退，ECAT 只能
        # 靠实时 stderr 感知。连续命中 5 次（中间出现成功标志则清零）就杀掉进程树，让
        # 本轮生成器以普通失败结束、实验继续，而不是空等 timeout_s 耗尽。
        #
        # 计数按 pid 隔离而不是单一实例计数：agentic judge 并发调用同一个 runner 实例
        # （engine.TThreadPoolExecutor 同时跑 feature_checklist / crash_risk 等多个
        # judge），但每次 run() 都 Popen 一个*独立*进程。把多个进程的 429 混进一个计数
        # 会让 A 的限流把 B 的进程杀掉（实测复现：6 线程各触发 1 次，共享计数到 5，某线程
        # 读到别的线程的 _abort_reason）。429 是单个上游请求的行为，属于那次 run 的
        # 进程，所以按 pid 各自累计、只杀自己。
        self._rate_limit_streak_by_pid: dict[int, int] = {}
        self._rate_limit_lock = threading.Lock()
        self._abort_reason_by_pid: dict[int, str] = {}

    def _build_cmd(self, model: str) -> list[str]:
        # opencode requires "provider/model" format; accept pre-qualified strings too
        full_model = model if "/" in model else f"{self.provider}/{model}"
        cmd = [
            "opencode",
            "run",
            "--model",
            full_model,
            "--dangerously-skip-permissions",
            "--format",
            "json",  # 逐行 JSON 事件，供 base.py tee 到 stream.jsonl
            # opencode 默认把内部日志写到 ~/.local/share/opencode/log/，但那是**全局共享
            # 且会自动清理**的（实测只留最近 ~10 个），后续几次调用就把现场冲掉了。镜像到
            # stderr 后由 base.py 落进本轮 run 目录，每轮独立、不被覆盖。
            "--print-logs",
        ]
        if self.resume_session and self._ran_once:
            cmd.append("--continue")  # 续最近一个会话，省掉重复的 prompt cache
        cmd += self.extra_args
        # Prompt is passed via stdin (see base.py) to avoid Windows cmd-line length limit.
        # opencode run reads from stdin when stdin is piped and no positional message given.
        return cmd

    def _usage(self, stream_stdout: str) -> dict[str, int]:
        """opencode 把用量放在每个 step_finish 的 part.tokens 里，逐步累加。

        {"total":…, "input":…, "output":…, "reasoning":…, "cache":{"write":…,"read":…}}
        每步都是一次独立计费的 API 调用（input 含当次完整上下文），所以求和才是真实花费，
        不能取最后一条。reasoning 计入 output —— 它按输出 token 计价。
        """
        usage = dict.fromkeys(
            (
                "input_tokens",
                "output_tokens",
                "cache_read_input_tokens",
                "cache_creation_input_tokens",
            ),
            0,
        )
        for line in stream_stdout.splitlines():
            line = line.strip()
            if not line.startswith("{") or '"tokens"' not in line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            tok = ((ev.get("part") or {}).get("tokens")) or {}
            if not isinstance(tok, dict):
                continue
            cache = tok.get("cache") or {}
            usage["input_tokens"] += int(tok.get("input") or 0)
            usage["output_tokens"] += int(tok.get("output") or 0) + int(tok.get("reasoning") or 0)
            if isinstance(cache, dict):
                usage["cache_read_input_tokens"] += int(cache.get("read") or 0)
                usage["cache_creation_input_tokens"] += int(cache.get("write") or 0)
        return usage

    def _final_text(self, stream_stdout: str) -> str | None:
        """opencode 的事件是 {type,timestamp,sessionID,part}，终答在 type=="text" 的
        part.text 里（实测 step_start → text → step_finish）。取最后一条非空的。"""
        last: str | None = None
        for line in stream_stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") == "text":
                txt = ((ev.get("part") or {}).get("text") or "").strip()
                if txt:
                    last = txt
        return last

    def _capture_session_id(self, raw_stdout: str, *, success: bool = True) -> None:
        """base.run() 每轮调用一次 —— 借它记住"成功跑过一次"，下次才加 --continue。

        Only on ``success``: a failed run (quota, crash, timeout) must not mark the
        session as resumed, or ``_build_cmd`` would ``--continue`` a broken session
        on the next round instead of starting fresh.
        """
        if success:
            self._ran_once = True

    def _on_stderr_line(self, pid: int, line: str) -> None:
        """stderr 逐行钩子（base.run 经 _drain_stderr 回调）。统计连续 429 命中。

        在 daemon 线程执行 —— 只用 Lock，不做任何 IO/long work。
        - 命中限流模式：该 pid 的 streak += 1；达到 _RATE_LIMIT_STREAK_LIMIT 就杀
          **这个 pid** 的进程树（不影响到别的同行 judge 进程）。
        - 命中成功标志：该 pid 的 streak 清零（限流恢复，继续正常跑）。
        - 其他行：不改 streak（退避 sleep 期间的空档行不打断计数）。

        按 pid 计是因为多个 agentic judge 会在并发线程里共用同一个 runner 实例，
        但各自是独立 Popen 子进程。共享一个计数/触发标记会互相污染——A 的 429 凑满
        5 次后把 B 的进程杀掉。命中/清零只影响触发它的那个 pid。
        """
        if any(p.search(line) for p in _RATE_LIMIT_PATTERNS):
            with self._rate_limit_lock:
                streak = self._rate_limit_streak_by_pid.get(pid, 0) + 1
                self._rate_limit_streak_by_pid[pid] = streak
            if streak >= _RATE_LIMIT_STREAK_LIMIT:
                self._abort_reason_by_pid[pid] = (
                    f"rate-limited ({streak}x 429) after {streak} consecutive retries"
                )
                log.error(
                    "%s hit %d consecutive 429 responses — killing subprocess "
                    "(pid=%d) to avoid spinning on rate-limit retries",
                    self._cli_name,
                    streak,
                    pid,
                )
                _kill_process_tree(pid)
            return
        if any(p.search(line) for p in _RATE_LIMIT_SUCCESS_PATTERNS):
            with self._rate_limit_lock:
                self._rate_limit_streak_by_pid[pid] = 0

    def _build_cwd_args(self, cwd: Path | None) -> list[str]:
        """opencode 的 `--dir` 只是项目根，**不是访问边界**，所以 add_dirs 无需映射。

        Claude Code 的 `--add-dir` 是在放宽权限门；opencode 没有这道门（实测给了
        `--dir` 之外的绝对路径照样读得到），安卓源码 / spec 靠 prompt 里的绝对路径就够。
        skills 走 OPENCODE_CONFIG_DIR + plugin，memory 直接拼进 prompt，都与 --dir 无关。

        曾试过取 cwd 与 add_dirs 的公共祖先，结果 --dir 退到整个 workspace（几百个 run
        目录 + 多个代码仓），徒增 opencode 的索引/监视开销。
        """
        return ["--dir", str(cwd.resolve())] if cwd is not None else []

    def run(
        self,
        prompt: str,
        model: str,
        env: dict[str, str] | None = None,
        cwd: Path | None = None,
        role: str = "agent",
    ) -> RunResult:
        """Run opencode in a per-invocation temp config dir for SQLite isolation."""
        # 默认每次调用一个用完即弃的目录，避免并发调用共用同一个 SQLite 库。
        # 开了 resume_session 则必须用固定目录 —— 会话存在配置目录下，换目录 `--continue`
        # 就找不回上一轮。只有串行角色（generator）能开，判别器的 agentic judge 会并发。
        if self.resume_session:
            tmp_dir = self.config_dir.resolve() / "tmp" / "session"
        else:
            tmp_dir = self.config_dir.resolve() / "tmp" / uuid.uuid4().hex
        tmp_dir.mkdir(parents=True, exist_ok=True)
        try:
            # Build opencode.jsonc:
            #   - plugin: skills are loaded from the role config dir
            #   - provider: when base_url is set, register the named provider as
            #     an OpenAI-compatible custom provider (via @ai-sdk/openai-compatible)
            #     so opencode does not validate the model name against its built-in
            #     OpenAI registry. The model is also registered explicitly because
            #     opencode rejects unknown model IDs even on custom providers.
            jsonc: dict[str, object] = {
                "$schema": "https://opencode.ai/config.json",
                "plugin": [str(self.config_dir.resolve())],
                # `--dangerously-skip-permissions` **不覆盖全部权限**：external_directory
                # 的默认规则仍是 {"pattern":"*","action":"ask"}，子代理一读 --dir 之外的
                # 鸿蒙 SDK 就弹询问，而 ECAT 非交互、stdin 已关 —— 进程从此永久挂起
                # （实测 runs_20260820_2025 停在 "permission.asked publishing" 再无事件）。
                # 这里显式声明 allow，与那个 flag 的意图一致，且一次覆盖同类的
                # skill / task / lsp 等默认 ask 的权限。
                "permission": "allow",
            }
            if self.base_url:
                api_key_value = ""
                if self.api_key_env:
                    api_key_value = (env or os.environ).get(self.api_key_env, "")
                jsonc["provider"] = {
                    self.provider: {
                        "npm": "@ai-sdk/openai-compatible",
                        "name": self.provider,
                        "options": {
                            "baseURL": self.base_url,
                            **({"apiKey": api_key_value} if api_key_value else {}),
                        },
                        "models": {model: {"name": model}},
                    },
                }
            (tmp_dir / "opencode.jsonc").write_text(
                json.dumps(jsonc),
                encoding="utf-8",
            )
            # Stage subagent files into tmp_dir/agents/ with frontmatter rewritten
            # to opencode's schema (lowercase tools object + hex color). The
            # source files in self.config_dir/agents/ keep Claude Code format so
            # ClaudeCodeRunner reads them unchanged. Pass the parent's qualified
            # model so subagents inherit the same provider/model.
            full_model = model if "/" in model else f"{self.provider}/{model}"
            staged = _stage_agents(
                self.config_dir / "agents",
                tmp_dir / "agents",
                parent_model=full_model,
            )
            if staged:
                log.debug("Staged %d agent file(s) for opencode in %s", staged, tmp_dir)
            skills = _link_skills(self.config_dir / "skills", tmp_dir / "skills")
            log.debug("Exposed %d skill(s) for opencode in %s", skills, tmp_dir)
            # Start from the caller-supplied env (or full os.environ) and override
            # OPENCODE_CONFIG_DIR so each invocation has its own SQLite database.
            invocation_env: dict[str, str] = dict(env) if env is not None else dict(os.environ)
            invocation_env["OPENCODE_CONFIG_DIR"] = str(tmp_dir)
            # When a base_url override is set, opencode's provider SDK reads its
            # API key from the canonical env var of that provider (OPENAI_API_KEY
            # for `openai`, ANTHROPIC_API_KEY for `anthropic`, ...). ECAT stores
            # the key under api_key_env (e.g. GLM_API_KEY); copy it into the
            # canonical name so the SDK actually picks it up.
            if self.base_url and self.api_key_env:
                key_val = invocation_env.get(self.api_key_env, "")
                if key_val:
                    canonical = _canonical_key_env(self.provider)
                    if canonical:
                        invocation_env[canonical] = key_val
            # 挂上 429 限流的实时检测钩子：进程存活期间逐行看 stderr，连续 5 次 429
            # 就杀进程树，让本轮以失败结束而不是空等 timeout_s。默认不设此钩子的 runner
            # （claude_code 等）不受影响。
            self.on_stderr_line = self._on_stderr_line
            result = super().run(prompt=prompt, model=model, env=invocation_env, cwd=cwd, role=role)
            # 429 主动中止：进程树已被钩子杀掉，base.run 拿到非 0 returncode → success=False。
            # 具体条目与 pid 已在 _on_stderr_line 里 log.error 时带 pid 说明，这里不再读
            # 共享的 abort_reason —— 并发 judge 下那是别的 run 写的，读了只会串行。不 raise
            # AgentQuotaExhausted —— 那会终止整个实验循环；429 是可恢复的限流，应让本轮
            # 生成器失败、实验继续下一轮。
            # resume 模式下这是跨调用共用的会话目录，删了 `--continue` 就没得续。
            # 失败(超时/崩溃/quota)时却要删 —— 那份会话目录已经坏了，留着等于让下一轮
            # `--continue` 续上一个坏会话(基线里 _ran_once 在 returncode 检查前被置位，
            # 失败也会当成"跑过一次")。只在成功且 resume 时保留。
            self._session_failed = not result.success
            return result
        finally:
            # resume 模式下这是跨调用共用的会话目录，删了 `--continue` 就没得续
            if not self.resume_session or self._session_failed:
                # 先把 skills 链接拆掉再删目录。实测 rmtree 不会跟着 junction/symlink
                # 递归，但那是真实 skills 目录，赌不起 —— 显式拆链接不依赖实现细节。
                link = tmp_dir / "skills"
                if link.is_symlink() or (link.exists() and not link.is_dir()):
                    with contextlib.suppress(OSError):
                        link.unlink()
                elif link.is_dir():
                    with contextlib.suppress(OSError):
                        link.rmdir()  # junction / 空目录：成功；真实副本：失败留给 rmtree
                shutil.rmtree(tmp_dir, ignore_errors=True)
