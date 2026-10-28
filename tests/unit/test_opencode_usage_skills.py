"""opencode 的 token 用量提取与 skills 暴露 —— 两者都不得影响 Claude Code 路径。"""
from __future__ import annotations

import json
from pathlib import Path

from ecat.runner.base import AgentRunner
from ecat.runner.claude_code import ClaudeCodeRunner
from ecat.runner.opencode import OpencodeRunner, _link_skills


def _step(inp: int, out: int, reasoning: int = 0, read: int = 0, write: int = 0) -> str:
    return json.dumps({
        "type": "step_finish",
        "part": {"type": "step-finish", "tokens": {
            "total": inp + out, "input": inp, "output": out,
            "reasoning": reasoning, "cache": {"read": read, "write": write}}},
    })


def _runner(tmp_path: Path) -> OpencodeRunner:
    return OpencodeRunner(config_dir=tmp_path / "cfg")


# ── token 用量 ──────────────────────────────────────────────────────────────
def test_usage_sums_every_step(tmp_path: Path) -> None:
    """每个 step 都是一次独立计费的 API 调用 —— 求和，不能取最后一条。"""
    stream = "\n".join([_step(100, 10), _step(250, 20), _step(400, 30)])
    u = _runner(tmp_path)._usage(stream)
    assert u["input_tokens"] == 750
    assert u["output_tokens"] == 60


def test_usage_counts_reasoning_as_output(tmp_path: Path) -> None:
    """reasoning 按输出 token 计价。"""
    u = _runner(tmp_path)._usage(_step(100, 10, reasoning=249))
    assert u["output_tokens"] == 259


def test_usage_picks_up_cache(tmp_path: Path) -> None:
    u = _runner(tmp_path)._usage(_step(100, 10, read=2560, write=64))
    assert u["cache_read_input_tokens"] == 2560
    assert u["cache_creation_input_tokens"] == 64


def test_usage_survives_junk_lines(tmp_path: Path) -> None:
    stream = "\n".join(["not json", "", "{bad", _step(5, 1), '{"type":"text"}'])
    assert _runner(tmp_path)._usage(stream)["input_tokens"] == 5


def test_usage_real_opencode_event(tmp_path: Path) -> None:
    """真实事件原文（取自 runs_20260820_2112 的 generator.stream.jsonl）。"""
    line = ('{"_ecat_time":"2026-08-20T21:13:48+08:00","_ecat_elapsed_s":9.69,'
            '"type":"step_finish","timestamp":1787231628975,"sessionID":"ses_x",'
            '"part":{"type":"step-finish","tokens":{"total":15189,"input":12265,'
            '"output":115,"reasoning":249,"cache":{"write":0,"read":2560}}}}')
    u = _runner(tmp_path)._usage(line)
    assert u == {"input_tokens": 12265, "output_tokens": 364,
                 "cache_read_input_tokens": 2560, "cache_creation_input_tokens": 0}


# ── Claude Code 不受影响 ────────────────────────────────────────────────────
def test_claude_code_usage_untouched(tmp_path: Path) -> None:
    """base 的默认实现仍走 Claude Code 的 schema；opencode 的字段对它无效。"""
    cc = ClaudeCodeRunner(config_dir=tmp_path / "cfg")
    assert cc._usage(_step(999, 999)) == {}          # opencode 事件 → 认不出，全 0
    assert AgentRunner._usage is not OpencodeRunner._usage


def test_claude_code_has_no_skill_staging(tmp_path: Path) -> None:
    """skills 暴露只发生在 opencode 侧。"""
    assert not hasattr(ClaudeCodeRunner, "_link_skills")


# ── skills 暴露 ─────────────────────────────────────────────────────────────
def _make_skills(root: Path, n: int) -> Path:
    src = root / "skills"
    for i in range(n):
        d = src / f"skill-{i}"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(f"---\nname: skill-{i}\ndescription: d\n---\n",
                                    encoding="utf-8")
    return src


def test_link_skills_exposes_all(tmp_path: Path) -> None:
    src = _make_skills(tmp_path, 3)
    assert _link_skills(src, tmp_path / "tmp" / "skills") == 3


def test_link_skills_does_not_copy_when_linkable(tmp_path: Path) -> None:
    """链接优先 —— 42 个 skill 不该每次调用都复制一遍。"""
    src = _make_skills(tmp_path, 2)
    dst = tmp_path / "tmp" / "skills"
    dst.parent.mkdir(parents=True)
    _link_skills(src, dst)
    # 改源文件，链接侧应立刻可见（复制则不会）
    (src / "skill-0" / "SKILL.md").write_text("changed", encoding="utf-8")
    assert (dst / "skill-0" / "SKILL.md").read_text(encoding="utf-8") == "changed"


def test_link_skills_missing_dir_is_zero(tmp_path: Path) -> None:
    assert _link_skills(tmp_path / "nope", tmp_path / "dst") == 0


def test_cleanup_never_deletes_real_skills(tmp_path: Path) -> None:
    """核心安全性质：清理临时目录不得动到真实 skills。"""
    import contextlib
    import shutil as _sh
    src = _make_skills(tmp_path, 2)
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    dst = tmp / "skills"
    _link_skills(src, dst)
    link = dst
    if link.is_symlink():
        link.unlink()
    elif link.is_dir():
        with contextlib.suppress(OSError):
            link.rmdir()
    _sh.rmtree(tmp, ignore_errors=True)
    assert (src / "skill-0" / "SKILL.md").is_file()
    assert (src / "skill-1" / "SKILL.md").is_file()
