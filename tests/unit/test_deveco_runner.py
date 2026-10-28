"""DevecoCodeRunner 的参数构造与配置写入。

deveco 是 opencode 的 fork，事件流/schema 兼容，所以 runner 继承 OpencodeRunner，
这里只锁「与 opencode 不同的那几处」：命令名 `deveco`、自动授权 flag `--auto`、
以及配置写进项目 `.deveco/deveco.jsonc`（deveco 只读项目级配置，没有 per-invocation
config-dir env）。
"""

from __future__ import annotations

import json
from pathlib import Path

from ecat.runner.base import RunResult
from ecat.runner.deveco import DevecoCodeRunner
from ecat.runner.opencode import _RATE_LIMIT_STREAK_LIMIT


def _runner(tmp_path: Path, **kw) -> DevecoCodeRunner:
    return DevecoCodeRunner(config_dir=tmp_path / "cfg", **kw)


def test_build_cmd_uses_deveco_and_auto(tmp_path: Path) -> None:
    """命令是 `deveco run`，授权 flag 是 `--auto`（不是 --dangerously-skip-permissions）。"""
    cmd = _runner(tmp_path, provider="zhipu")._build_cmd("glm-5.3-flash")
    assert cmd[0] == "deveco"
    assert "run" in cmd
    assert "--model" in cmd
    assert cmd[cmd.index("--model") + 1] == "zhipu/glm-5.3-flash"
    assert "--auto" in cmd
    assert "--dangerously-skip-permissions" not in cmd
    assert "--format" in cmd and cmd[cmd.index("--format") + 1] == "json"


def test_build_cmd_keeps_qualified_model(tmp_path: Path) -> None:
    """provider/model 已限定时原样保留，不加前缀。"""
    cmd = _runner(tmp_path)._build_cmd("zhipu/glm-5.3-flash[1m]")
    assert cmd[cmd.index("--model") + 1] == "zhipu/glm-5.3-flash[1m]"


def test_build_cmd_resume_only_after_success(tmp_path: Path) -> None:
    """--continue 只在 resume_session 且已成功跑过时追加（同 opencode）。"""
    r = _runner(tmp_path, resume_session=True)
    assert "--continue" not in r._build_cmd("m")
    r._ran_once = True
    assert "--continue" in r._build_cmd("m")


def test_build_cwd_args_is_dir_only(tmp_path: Path) -> None:
    """--dir 只跟 cwd 走，语义同 opencode（--dir 不是访问边界）。"""
    cwd = tmp_path / "harmony"
    cwd.mkdir()
    assert _runner(tmp_path)._build_cwd_args(cwd) == ["--dir", str(cwd.resolve())]
    assert _runner(tmp_path)._build_cwd_args(None) == []


def test_run_writes_and_cleans_project_deveco_config(tmp_path: Path) -> None:
    """run() 在项目根写 `.deveco/deveco.jsonc`，跑完清理（finally）。

    用 mock 顶替父类 run()（避开 subprocess），使 deveco 的 run() 真正执行
    配置写入 → 调用父 run（mock 返回成功）→ finally 清理，从而验证整个闭环。
    """
    import os

    os.environ["GLM_API_KEY"] = "test-key-123"
    r = _runner(
        tmp_path,
        provider="zhipu",
        base_url="https://open.bigmodel.cn/api/coding/paas/v4",
        api_key_env="GLM_API_KEY",
    )
    cwd = tmp_path / "harmony"
    cwd.mkdir()

    calls: dict[str, object] = {}

    def fake_run(self, *a, **kw):
        calls["prompt"] = kw.get("prompt", a[0] if a else None)
        calls["model"] = kw.get("model")
        calls["env"] = kw.get("env")
        calls["cwd"] = kw.get("cwd")
        return RunResult(returncode=0, stdout="", stderr="", duration_s=0.1)

    # 顶替被继承的 OpencodeRunner.run（即 deveco run() 里 super().run(...) 的目标）
    import ecat.runner.opencode as op_mod

    orig = op_mod.OpencodeRunner.run
    op_mod.OpencodeRunner.run = fake_run
    try:
        result = r.run(prompt="hi", model="glm-5.3-flash", cwd=cwd, role="judge_1")
    finally:
        op_mod.OpencodeRunner.run = orig

    # 配置写入过（deveco jsonc 被创建且含 baseURL/apiKey）
    config_dir = cwd / ".deveco"
    assert calls["model"] == "glm-5.3-flash"
    assert calls["cwd"] == cwd or str(calls["cwd"]).endswith("harmony")
    # 清理：finally 应已删除配置（子进程返回后）。但 fake_run 不触发 Popen，
    # 配置是在调用 super().run 前写、finally 里删，所以此处应已删除。
    assert not (config_dir / "deveco.jsonc").exists(), "run() 结束后应清理 .deveco/deveco.jsonc"
    assert result.success


def test_run_writes_provider_key_into_config(tmp_path: Path) -> None:
    """run() 把 api_key_env 的值写进 tmp config-dir 的 deveco.jsonc（不落项目目录）。

    run() 用 per-invocation tmp config-dir，把 provider 配置写进那里的 deveco.jsonc，
    并设 DEVECO_CONFIG_DIR 指向它。fake_run 从 env 里的 DEVECO_CONFIG_DIR 读配置。
    """
    import os

    os.environ["GLM_API_KEY"] = "test-key-123"
    r = _runner(
        tmp_path,
        provider="zhipu",
        base_url="https://open.bigmodel.cn/api/coding/paas/v4",
        api_key_env="GLM_API_KEY",
    )
    cwd = tmp_path / "harmony"
    cwd.mkdir()

    captured: dict[str, object] = {}

    def fake_run(self, *a, **kw):
        cfg_dir = kw.get("env", {}).get("DEVECO_CONFIG_DIR")
        if cfg_dir:
            config_path = Path(cfg_dir) / "deveco.jsonc"
            if config_path.exists():
                captured["config"] = json.loads(config_path.read_text(encoding="utf-8"))
        return RunResult(returncode=0, stdout="", stderr="", duration_s=0.1)

    import ecat.runner.opencode as op_mod

    orig = op_mod.OpencodeRunner.run
    op_mod.OpencodeRunner.run = fake_run
    try:
        r.run(prompt="hi", model="glm-5.3-flash", cwd=cwd)
    finally:
        op_mod.OpencodeRunner.run = orig

    cfg = captured.get("config")
    assert cfg is not None, "run() 应把 provider 配置写进 tmp config-dir 的 deveco.jsonc"
    prov = cfg["provider"]["zhipu"]
    assert prov["npm"] == "@ai-sdk/openai-compatible"
    assert prov["options"]["baseURL"] == "https://open.bigmodel.cn/api/coding/paas/v4"
    assert prov["options"]["apiKey"] == "test-key-123"
    assert "glm-5.3-flash" in prov["models"]


def test_run_stages_agents_into_tmp_config_dir(tmp_path: Path) -> None:
    """run() 把角色的 agents/*.md stage 到 tmp config-dir 的 agents/（deveco 经
    DEVECO_CONFIG_DIR 发现）。_stage_agents 把 frontmatter 改写为 object-tools。

    重写为 tmp config-dir 而非项目 .deveco/（跟 opencode 的 per-invocation 隔离一致）。
    """
    import os

    cfg_dir = tmp_path / "cfg"
    agents_dir = cfg_dir / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "myjudge.md").write_text(
        "---\nname: myjudge\ndescription: test judge\ntools: [Read, Glob, Grep]\n"
        "---\nYou are a judge.\n",
        encoding="utf-8",
    )

    os.environ["GLM_API_KEY"] = "test-key-123"
    r = DevecoCodeRunner(
        config_dir=cfg_dir,
        provider="zhipu",
        base_url="https://open.bigmodel.cn/api/coding/paas/v4",
        api_key_env="GLM_API_KEY",
    )
    cwd = tmp_path / "harmony"
    cwd.mkdir()

    staged: dict[str, object] = {}
    seen_cfg_dir: dict[str, object] = {}

    def fake_run(self, *a, **kw):
        cfg_dir_path = kw.get("env", {}).get("DEVECO_CONFIG_DIR")
        if cfg_dir_path:
            seen_cfg_dir["path"] = cfg_dir_path
            staged_path = Path(cfg_dir_path) / "agents" / "myjudge.md"
            if staged_path.exists():
                staged["text"] = staged_path.read_text(encoding="utf-8")
        return RunResult(returncode=0, stdout="", stderr="", duration_s=0.1)

    import ecat.runner.opencode as op_mod

    orig = op_mod.OpencodeRunner.run
    op_mod.OpencodeRunner.run = fake_run
    try:
        r.run(prompt="hi", model="glm-5.3-flash", cwd=cwd)
    finally:
        op_mod.OpencodeRunner.run = orig

    # stage 发生时（fake_run 在执行阶段读到），agent 文件在 tmp config-dir 的 agents/ ，
    # 且被改成 object-tools 形式。
    assert "text" in staged, "run() 应把 agents/*.md stage 到 tmp config-dir 的 agents/"
    assert "tools:" in staged["text"] and "read: true" in staged["text"], (
        "deveco 的 agent 需 object 形式 tools（_stage_agents 改写后应有 read:true）"
    )
    # 清理也应发生：run() 的 finally 删除整个 tmp config-dir（含 staged agents）。
    cfg_dir_path = seen_cfg_dir.get("path")
    assert cfg_dir_path, "run() 应把 DEVECO_CONFIG_DIR 设进 env"
    assert not Path(cfg_dir_path).exists(), "run() 结束后应删除 tmp config-dir"


def test_cli_name_overridden_for_deveco_log(tmp_path: Path) -> None:
    """deveco 触发 429 软中止时日志应写 "deveco"，不写 "opencode"。

    回归护栏：DevecoCodeRunner 继承 OpencodeRunner 的 _on_stderr_line，其硬编码日志
    "opencode hit %d consecutive 429 responses"。改了 _cli_name 后 deveco 应覆写为
    "deveco"，否则日志误报（实测 runs_20260902_2102 报的是 opencode，实际跑的是 deveco）。
    """
    log_records: list[str] = []
    import logging

    # 捕获 429 软中止的 ERROR 日志

    class _Cap(logging.Handler):
        def emit(self, record):
            log_records.append(record.getMessage())

    logger = logging.getLogger("ecat.runner.opencode")
    handler = _Cap()
    logger.addHandler(handler)
    try:
        runner = DevecoCodeRunner(config_dir=tmp_path)
        # monkeypatch _kill_process_tree 避免真杀
        import ecat.runner.opencode as op_mod

        orig_kill = op_mod._kill_process_tree
        op_mod._kill_process_tree = lambda pid: None
        try:
            for _ in range(_RATE_LIMIT_STREAK_LIMIT):
                runner._on_stderr_line(
                    801,
                    'message="retryable error matched" service=session.retry '
                    "provider=zhipu category=api_429 attempt=1 message=速率限制",
                )
        finally:
            op_mod._kill_process_tree = orig_kill
    finally:
        logger.removeHandler(handler)

    assert any("deveco hit" in r for r in log_records), (
        f"deveco 的 429 日志应写 'deveco hit'，实际记录: {log_records}"
    )
    assert not any("opencode hit" in r for r in log_records), (
        f"deveco 的 429 日志不应含 'opencode hit'，实际记录: {log_records}"
    )
