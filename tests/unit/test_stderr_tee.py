"""stderr 的实时落盘。

原本 stderr 只在 _write_transcripts 里写进 <role>.log，而那是进程退出后才调用的 ——
超时被杀时正好什么都留不下，偏偏那才是最需要日志的时候。这里锁住"边跑边写"。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from ecat.runner.base import AgentRunner


class _Bare(AgentRunner):
    """只为拿到 _stream_and_capture；不构造真实 CLI 命令。"""

    def _build_cmd(self, model: str) -> list[str]:
        return []


def _runner(tmp_path: Path) -> _Bare:
    r = _Bare(config_dir=tmp_path / "cfg")
    r.transcript_dir = tmp_path / "run" / "0"
    return r


def _spawn(code: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.PIPE,
        text=True, encoding="utf-8",
    )


def test_stderr_is_teed_to_run_dir(tmp_path: Path) -> None:
    r = _runner(tmp_path)
    proc = _spawn(
        "import sys; sys.stderr.write('boom line\\n'); sys.stderr.flush()",
    )
    _, stderr, _, _ = r._stream_and_capture(
        proc, stdin_input=None, stream_path=None, timeout_s=30, role="generator",
    )
    path = r.transcript_dir / "generator.stderr.log"
    assert path.is_file(), "stderr 必须落到本轮 run 目录"
    assert "boom line" in path.read_text(encoding="utf-8")
    assert "boom line" in stderr          # 返回值仍保持原样，不受落盘影响


def test_stderr_survives_a_timeout_kill(tmp_path: Path) -> None:
    """核心性质：进程被 watchdog 杀掉，此前写出的 stderr 仍在盘上。"""
    r = _runner(tmp_path)
    proc = _spawn(
        "import sys,time; sys.stderr.write('before hang\\n'); sys.stderr.flush();"
        " time.sleep(300)",
    )
    _, _, _, timed_out = r._stream_and_capture(
        proc, stdin_input=None, stream_path=None, timeout_s=2, role="generator",
    )
    assert timed_out
    path = r.transcript_dir / "generator.stderr.log"
    assert path.is_file()
    assert "before hang" in path.read_text(encoding="utf-8")


def test_lines_carry_elapsed_seconds(tmp_path: Path) -> None:
    """带上相对秒数，才能和 stream.jsonl 的 _ecat_elapsed_s 对齐看。"""
    r = _runner(tmp_path)
    proc = _spawn("import sys; sys.stderr.write('x\\n'); sys.stderr.flush()")
    r._stream_and_capture(
        proc, stdin_input=None, stream_path=None, timeout_s=30, role="generator",
    )
    text = (r.transcript_dir / "generator.stderr.log").read_text(encoding="utf-8")
    assert text.lstrip().startswith("["), text
    assert "s] x" in text


def test_no_transcript_dir_is_not_fatal(tmp_path: Path) -> None:
    r = _Bare(config_dir=tmp_path / "cfg")
    r.transcript_dir = None
    proc = _spawn("import sys; sys.stderr.write('y\\n')")
    _, stderr, _, _ = r._stream_and_capture(
        proc, stdin_input=None, stream_path=None, timeout_s=30, role="generator",
    )
    assert "y" in stderr
