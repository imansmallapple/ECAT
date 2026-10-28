"""Integration tests for ecat.workspace — directory structure and init."""

from pathlib import Path

import pytest

from ecat.errors import WorkspaceError
from ecat.workspace import _RUNTIME_DIRS, _VERSIONED_DIRS, Workspace


def test_init_creates_structure(tmp_path: Path):
    ws = Workspace(tmp_path)
    ws.init()

    for rel in _VERSIONED_DIRS:
        assert (tmp_path / rel).is_dir(), f"Missing: {rel}"

    for rel in _RUNTIME_DIRS:
        assert (tmp_path / rel).is_dir(), f"Missing runtime dir: {rel}"


def test_init_creates_gitignore(tmp_path: Path):
    ws = Workspace(tmp_path)
    ws.init()
    gi = tmp_path / ".ecat" / ".gitignore"
    assert gi.exists()
    content = gi.read_text(encoding="utf-8")
    assert "runs_*/" in content
    assert "state.json" in content


def test_init_creates_env_file(tmp_path: Path):
    ws = Workspace(tmp_path)
    ws.init()
    env_file = tmp_path / ".env"
    assert env_file.exists()
    content = env_file.read_text(encoding="utf-8")
    assert "ANTHROPIC_API_KEY" in content


def test_init_creates_state_json(tmp_path: Path):
    ws = Workspace(tmp_path)
    ws.init()
    state = ws.read_state()
    assert state["iteration"] == 0
    assert state["status"] == "idle"


def test_force_wipes_memory_and_runs(tmp_path: Path):
    ws = Workspace(tmp_path)
    ws.init()

    # Put a file in memory
    mem = tmp_path / ".ecat/agents/generator/memory"
    (mem / "test.md").write_text("data", encoding="utf-8")

    # Create a timestamped runs directory
    runs = ws.create_runs_dir()
    (runs / "entropy.json").write_text("{}", encoding="utf-8")

    ws.init(force=True)
    assert not (mem / "test.md").exists()
    assert not runs.exists()


def test_validate_raises_if_missing(tmp_path: Path):
    ws = Workspace(tmp_path)
    with pytest.raises(WorkspaceError):
        ws.validate()


def test_validate_passes_after_init(tmp_path: Path):
    ws = Workspace(tmp_path)
    ws.init()
    ws.validate()  # should not raise


def test_write_and_read_state(tmp_path: Path):
    ws = Workspace(tmp_path)
    ws.init()
    ws.write_state({"iteration": 5, "last_H": 0.42})
    state = ws.read_state()
    assert state["iteration"] == 5
    assert state["last_H"] == pytest.approx(0.42)


def test_agent_env_sets_claude_config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(tmp_path)
    ws = Workspace(tmp_path)
    ws.init()
    env = ws.agent_env("generator")
    expected = str((tmp_path / ".ecat/agents/generator").resolve())
    assert env["CLAUDE_CONFIG_DIR"] == expected


def test_create_runs_dir_timestamped(tmp_path: Path):
    ws = Workspace(tmp_path)
    ws.init()
    runs = ws.create_runs_dir()
    assert runs.is_dir()
    assert runs.name.startswith("runs_")
    assert (runs / "charts").is_dir()
    state = ws.read_state()
    assert state["current_runs_dir"] == runs.name


def test_current_runs_dir_from_state(tmp_path: Path):
    ws = Workspace(tmp_path)
    ws.init()
    runs = ws.create_runs_dir()
    resolved = ws.current_runs_dir()
    assert resolved == runs


def test_current_runs_dir_fallback_glob(tmp_path: Path):
    ws = Workspace(tmp_path)
    ws.init()
    # Manually create a runs dir without updating state
    fake_runs = tmp_path / ".ecat" / "runs_20260101_0000"
    fake_runs.mkdir()
    resolved = ws.current_runs_dir()
    assert resolved == fake_runs


def test_current_runs_dir_raises_when_none(tmp_path: Path):
    ws = Workspace(tmp_path)
    ws.init()
    with pytest.raises(WorkspaceError):
        ws.current_runs_dir()
