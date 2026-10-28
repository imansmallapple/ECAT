"""Unit tests for ecat.config — Pydantic schema and YAML loading."""

from pathlib import Path

import pytest
import yaml

from ecat.config import load_config

# ── helpers ──────────────────────────────────────────────────────────────────


def _write_config(
    tmp_path: Path, android: Path, harmony: Path,
    *, version: str | None = None, loop: dict | None = None,
) -> Path:
    data = {
        "project": "test-app",
        "paths": {
            "android": str(android),
            "harmony": str(harmony),
            "spec": str(tmp_path / "spec"),
        },
        "generator": {
            "runtime": "claude_code",
            "model": "claude-sonnet-4-6",
            "api_key_env": "TEST_API_KEY",
            "config_dir": str(tmp_path / ".ecat/agents/generator"),
        },
        "discriminator": {
            "runtime": "claude_code",
            "model": "claude-sonnet-4-6",
            "api_key_env": "TEST_API_KEY",
            "config_dir": str(tmp_path / ".ecat/agents/discriminator"),
        },
        "entropy": {
            "judge_model": {
                "api_key_env": "TEST_API_KEY",
            }
        },
    }
    if version is not None:
        data["entropy"]["version"] = version
    if loop is not None:
        data["loop"] = loop
    cfg_path = tmp_path / "ecat.config.yaml"
    cfg_path.write_text(yaml.dump(data), encoding="utf-8")
    return cfg_path


# ── tests ─────────────────────────────────────────────────────────────────────


def test_load_valid_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    android = tmp_path / "android"
    android.mkdir()
    harmony = tmp_path / "harmony"
    harmony.mkdir()
    (tmp_path / "spec").mkdir()

    monkeypatch.setenv("TEST_API_KEY", "sk-test")
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/claude")

    cfg_path = _write_config(tmp_path, android, harmony)
    cfg = load_config(cfg_path)
    assert cfg.project == "test-app"
    assert Path(cfg.paths.android) == android


def test_missing_android_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    android = tmp_path / "android_missing"
    harmony = tmp_path / "harmony"
    harmony.mkdir()

    monkeypatch.setenv("TEST_API_KEY", "sk-test")
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/claude")

    cfg_path = _write_config(tmp_path, android, harmony)
    with pytest.raises(Exception, match="android"):
        load_config(cfg_path)


def test_missing_api_key_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    android = tmp_path / "android"
    android.mkdir()
    harmony = tmp_path / "harmony"
    harmony.mkdir()

    monkeypatch.delenv("TEST_API_KEY", raising=False)
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/claude")

    cfg_path = _write_config(tmp_path, android, harmony)
    with pytest.raises(Exception, match="TEST_API_KEY"):
        load_config(cfg_path)


# ── entropy.version 预设（flash / pro / custom）───────────────────────────────


def test_entropy_version_default_is_custom():
    """未写 version → custom：沿用用户配的 weights，行为不变（向后兼容）。"""
    from ecat.config import EntropyConfig

    c = EntropyConfig(weights={"compile": 0.4, "ui_align": 0.3, "whatever": 0.2})
    assert c.version == "custom"
    assert c.weights == {"compile": 0.4, "ui_align": 0.3, "whatever": 0.2}


def test_entropy_version_flash_keeps_static_plus_checklist():
    """flash = 纯静态确定性插件 + feature_checklist；agentic 与 device 维度全 0。"""
    from ecat.config import EntropyConfig

    c = EntropyConfig(version="flash")
    nz = {k for k, v in c.weights.items() if v}
    assert nz == {
        "compile", "static_lint", "placeholder", "app_identity",
        "style", "data_layer_parity", "permission_parity", "crash_risk",
        "feature_checklist",
    }
    # 关键排除
    for gone in ("entry_nav", "feature_check", "page_audit",
                 "source_parity", "hallucination", "ui_align",
                 "runtime_liveness"):
        assert c.weights.get(gone, 0) == 0


def test_entropy_version_pro_excludes_device_and_two_custom():
    """pro = 默认权重，排除 ui_align/runtime_liveness。"""
    from ecat.config import EntropyConfig

    c = EntropyConfig(version="pro")
    nz = {k for k, v in c.weights.items() if v}
    for gone in ("ui_align", "runtime_liveness"):
        assert gone not in nz
    # 其余默认有权重的插件都在
    for kept in ("compile", "entry_nav", "feature_check", "source_parity",
                 "hallucination", "feature_checklist"):
        assert kept in nz


def test_entropy_version_custom_ignores_preset():
    """custom 下 weights 原样保留，即使配了会被预设排除的维度。"""
    from ecat.config import EntropyConfig

    c = EntropyConfig(version="custom", weights={"ui_align": 0.5, "hallucination": 0.5})
    assert c.weights == {"ui_align": 0.5, "hallucination": 0.5}


def test_entropy_version_preset_overrides_user_weights():
    """flash/pro 是整体替换：用户手配的 weights 被预设覆盖，不残留混杂。"""
    from ecat.config import EntropyConfig

    c = EntropyConfig(version="flash", weights={"ui_align": 0.99, "entry_nav": 0.99})
    assert c.weights.get("ui_align", 0) == 0
    assert c.weights.get("entry_nav", 0) == 0


# ── entropy.version 跨段联动 loop（max_iterations / entropy_threshold）────────


def _cfg(tmp_path: Path, monkeypatch, **kw):
    android = tmp_path / "android"
    android.mkdir(exist_ok=True)
    harmony = tmp_path / "harmony"
    harmony.mkdir(exist_ok=True)
    (tmp_path / "spec").mkdir(exist_ok=True)
    monkeypatch.setenv("TEST_API_KEY", "sk-test")
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/claude")
    return load_config(_write_config(tmp_path, android, harmony, **kw))


def test_version_flash_sets_loop_preset(tmp_path: Path, monkeypatch):
    """flash → max_iterations=30, entropy_threshold=0.02。"""
    cfg = _cfg(tmp_path, monkeypatch, version="flash")
    assert cfg.loop.max_iterations == 30
    assert cfg.loop.entropy_threshold == 0.02


def test_version_pro_sets_loop_preset(tmp_path: Path, monkeypatch):
    """pro → max_iterations=100, entropy_threshold=0.01。"""
    cfg = _cfg(tmp_path, monkeypatch, version="pro")
    assert cfg.loop.max_iterations == 100
    assert cfg.loop.entropy_threshold == 0.01


def test_version_custom_keeps_configured_loop(tmp_path: Path, monkeypatch):
    """custom（默认）→ 保留 config 里写的 loop 参数，不被预设覆盖。"""
    cfg = _cfg(
        tmp_path, monkeypatch, version="custom",
        loop={"max_iterations": 7, "entropy_threshold": 0.5},
    )
    assert cfg.loop.max_iterations == 7
    assert cfg.loop.entropy_threshold == 0.5


def test_version_flash_overrides_configured_loop(tmp_path: Path, monkeypatch):
    """flash 是整体替换：即便 config 写了 loop，也被预设覆盖。"""
    cfg = _cfg(
        tmp_path, monkeypatch, version="flash",
        loop={"max_iterations": 999, "entropy_threshold": 0.9},
    )
    assert cfg.loop.max_iterations == 30
    assert cfg.loop.entropy_threshold == 0.02
