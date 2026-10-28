"""detect_subagent_tool picks the subagent tool name the installed `claude` exposes."""

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from ecat.runner import claude_code

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="fake CLI is a POSIX shell script")


def _fake_claude(tmp_path: Path, tools: list[str]) -> None:
    init = json.dumps({"type": "system", "subtype": "init", "tools": tools})
    script = tmp_path / "claude"
    script.write_text(f"#!/bin/sh\necho '{init}'\nsleep 30\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)


@pytest.mark.parametrize(
    ("tools", "expected"),
    [
        (["Task", "Bash", "Read"], "Task"),
        (["Agent", "Bash", "Read"], "Agent"),
        (["Bash", "Read"], "Agent"),  # neither found → keep the historical default
    ],
)
def test_detects_tool_name(tmp_path: Path, monkeypatch, tools, expected):
    _fake_claude(tmp_path, tools)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    claude_code.detect_subagent_tool.cache_clear()
    assert claude_code.detect_subagent_tool(None) == expected
    claude_code.detect_subagent_tool.cache_clear()


def test_missing_cli_falls_back(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    claude_code.detect_subagent_tool.cache_clear()
    assert claude_code.detect_subagent_tool(None) == "Agent"
    claude_code.detect_subagent_tool.cache_clear()
