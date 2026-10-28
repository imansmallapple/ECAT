"""DevecoCodeRunner — wraps the `deveco` CLI subprocess.

`deveco` (npm `@deveco/deveco-code`) is a fork of `opencode` — its config schema
points at ``opencode.ai/config.json`` (verified via ``deveco debug config``), its
``run --format json`` event stream is structurally identical (``step_start`` /
``text`` / ``step_finish`` / ``error``, with usage in ``part.tokens``), and its
429 errors surface as ``{"type":"error",...,"statusCode":429,"isRetryable":true}``
on both the JSON stream and stderr. So this runner reuses OpencodeRunner's
event parsing, usage accumulation, and rate-limit soft-abort wholesale, and only
overrides what differs: the command name, the auto-approve flag, and the
config-dir env var.

Verified against a live install:
  * ``deveco run --model zhipu/glm-5.3-flash --format json --auto --dir <proj>``
    returns a real ``text`` event with content and non-zero tokens.
  * ``DEVECO_CONFIG_DIR=<dir>`` redirects config loading to *dir* — deveco reads
    ``<dir>/deveco.jsonc`` and discovers ``<dir>/agents/*.md`` there (verified
    via ``debug config`` picking up a marker ``username`` and ``debug agent``
    finding a staged agent). This is deveco's equivalent of ``OPENCODE_CONFIG_DIR``.
  * The project ``.deveco/`` dir is *also* read for config, but a per-invocation
    temp config dir is how we isolate agents/skills per call (mirroring
    OpencodeRunner).
  * 6 concurrent invocations all succeed regardless — deveco's global WAL DB
    tolerates concurrency, but the temp config dir still keeps each call's
    staged agents/skills from colliding.

So this runner is a near-copy of OpencodeRunner with three deltas:
  * command ``deveco`` and ``--auto`` instead of ``opencode``/``--dangerously-skip-permissions``,
  * ``DEVECO_CONFIG_DIR`` instead of ``OPENCODE_CONFIG_DIR``,
  * config filename ``deveco.jsonc`` instead of ``opencode.jsonc``.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import uuid
from pathlib import Path

from ecat.runner.base import RunResult
from ecat.runner.opencode import (
    OpencodeRunner,
    _canonical_key_env,
    _link_skills,
    _stage_agents,
)

log = logging.getLogger(__name__)

# per-invocation 配置目录名（deveco 经 DEVECO_CONFIG_DIR 读取；与 opencode 的
# OPENCODE_CONFIG_DIR 同构）。文件名用 deveco.jsonc（deveco 读这个；改叫
# opencode.jsonc 会被忽略）。
_CONFIG_NAME = "deveco.jsonc"


class DevecoCodeRunner(OpencodeRunner):
    """Runs the `deveco` CLI (an opencode fork) as an isolated subprocess.

    Inherits OpencodeRunner's event parsing / usage accumulation / 429 soft-abort
    unchanged (the two CLIs emit the same stream schema). Overrides only:
      * the command name and auto-approve flag in ``_build_cmd``,
      * the config-dir env var (``DEVECO_CONFIG_DIR``) and config file name in
        ``run()`` — otherwise the per-invocation temp dir, ``_stage_agents`` and
        ``_link_skills`` are identical to OpencodeRunner.
    """

    # deveco 的 JSON 事件流与 opencode 同构（step_start/text/step_finish/error，
    # 用量在 part.tokens）—— 这些解析、429 检测、会话处理全部继承。
    emits_stream_json = True
    # 429 软中止日志里的 CLI 名：继承自 OpencodeRunner 的"opencode"字样会误报，
    # 这里覆写成 deveco。
    _cli_name = "deveco"

    def _build_cmd(self, model: str) -> list[str]:
        """deveco 的命令：换名字 + `--auto` 代替 --dangerously-skip-permissions。

        其余（--format json / --print-logs / --continue / extra_args）与 opencode 一致。
        """
        full_model = model if "/" in model else f"{self.provider}/{model}"
        cmd = [
            "deveco",
            "run",
            "--model",
            full_model,
            "--auto",  # deveco 的自动授权 flag（opencode 是
            # --dangerously-skip-permissions）
            "--format",
            "json",  # 逐行 JSON 事件，供 base.py tee 到 stream.jsonl
            # deveco 默认把内部日志写到 ~/.local/share/deveco/log/，是全局共享且自动
            # 清理的。镜像到 stderr 后由 base.py 落进本轮 run 目录，每轮独立（同 opencode）。
            "--print-logs",
        ]
        if self.resume_session and self._ran_once:
            cmd.append("--continue")
        cmd += self.extra_args
        # prompt 走 stdin（base.py），避免 Windows cmd 命令行长度上限。deveco 与
        # opencode 一样在 stdin 被 pipe 且无 positional message 时读 stdin。
        return cmd

    def _build_cwd_args(self, cwd: Path | None) -> list[str]:
        """deveco 的 `--dir` 是项目根，不是访问边界——语义同 opencode。

        安卓源码 / spec 靠 prompt 里的绝对路径给到，无需映射 add_dirs（有那道权限门
        的只有 Claude Code）。取 cwd 与 add_dirs 公共祖先会退化到整个 workspace，
        徒增索引/监视开销，所以直接指向 harmony 项目根。
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
        """Run deveco in a per-invocation temp config dir (DEVECO_CONFIG_DIR).

        与 OpencodeRunner 同构：每次调用一个用完即弃的 tmp 目录，经 DEVECO_CONFIG_DIR
        指向它，deveco 从那里读 deveco.jsonc、发现 agents/ 和 skills/。把角色源里的
        agents 用 _stage_agents 改写（Claude Code frontmatter → object-tools）后写进
        tmp/agents/，skills 用 _link_skills 直接暴露到 tmp/skills/。这样源文件不被污染
        （agent 的 parent-model 注入只发生在 tmp），并发调用各自独立。
        """
        # 默认每次调用一个用完即弃的目录，避免并发调用共用同一个 SQLite 库（同 opencode）。
        # 开了 resume_session 则必须用固定目录 —— 会话存在配置目录下，换目录 `--continue`
        # 就找不回上一轮。只有串行角色（generator）能开，判别器的 agentic judge 会并发。
        if self.resume_session:
            tmp_dir = self.config_dir.resolve() / "tmp" / "session"
        else:
            tmp_dir = self.config_dir.resolve() / "tmp" / uuid.uuid4().hex
        tmp_dir.mkdir(parents=True, exist_ok=True)
        try:
            # 写 deveco.jsonc：plugin 指向角色配置目录（skills 由 _link_skills 暴露，
            # plugin 数组与 skill 发现无关，这里保留与 opencode 一致），provider 注册为
            # OpenAI-compatible 自定义 provider（base_url 设置时）。
            jsonc: dict[str, object] = {
                "$schema": "https://opencode.ai/config.json",
                "plugin": [str(self.config_dir.resolve())],
                # `--auto` 不覆盖全部权限：external_directory 的默认规则仍是 ask，子代理
                # 一读 --dir 之外的鸿蒙 SDK 就弹询问，而 ECAT 非交互、stdin 已关 —— 进程
                # 从此永久挂起。这里显式声明 allow，与 --auto 意图一致，一次覆盖同类权限。
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
            (tmp_dir / _CONFIG_NAME).write_text(json.dumps(jsonc), encoding="utf-8")

            # Stage subagent files into tmp_dir/agents/ with frontmatter rewritten
            # to deveco's schema（= opencode 的 object-tools）。源文件保留 Claude Code
            # 格式，ClaudeCodeRunner 读它们不变；deveco 读 tmp 里的改写副本。
            full_model = model if "/" in model else f"{self.provider}/{model}"
            staged = _stage_agents(
                self.config_dir / "agents",
                tmp_dir / "agents",
                parent_model=full_model,
            )
            if staged:
                log.debug("Staged %d agent file(s) for deveco in %s", staged, tmp_dir)
            skills = _link_skills(self.config_dir / "skills", tmp_dir / "skills")
            log.debug("Exposed %d skill(s) for deveco in %s", skills, tmp_dir)

            # Start from the caller-supplied env (or full os.environ) and override
            # DEVECO_CONFIG_DIR so each invocation has its own config dir / SQLite.
            invocation_env: dict[str, str] = dict(env) if env is not None else dict(os.environ)
            invocation_env["DEVECO_CONFIG_DIR"] = str(tmp_dir)

            # 当 base_url override 时 provider SDK 从 canonical env 读 key（OPENAI_API_KEY
            # 等）。ECAT 把 key 存在 api_key_env（如 GLM_API_KEY），copy 到 canonical。
            if self.base_url and self.api_key_env:
                key_val = invocation_env.get(self.api_key_env, "")
                if key_val:
                    canonical = _canonical_key_env(self.provider) or "OPENAI_API_KEY"
                    invocation_env[canonical] = key_val

            # 429 限流软中止（opencode 的那套，deveco 的 429 结构一致）。
            self.on_stderr_line = self._on_stderr_line
            result = super().run(prompt=prompt, model=model, env=invocation_env, cwd=cwd, role=role)

            self._session_failed = not result.success
            return result
        finally:
            # resume 模式下这是跨调用共用的会话目录，删了 `--continue` 就没得续。
            # 失败(超时/崩溃/quota)时却要删 —— 那份会话目录已经坏了，留着等于让下一轮
            # `--continue` 续上一个坏会话。只在成功且 resume 时保留。
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
