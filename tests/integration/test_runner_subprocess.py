"""Integration tests for AgentRunner's subprocess wrapper.

These spawn a real subprocess but drive it through `sys.executable -c <script>`
rather than a fake `claude` shim on PATH. The old shim approach was silently
shadowed by a real `claude` binary installed on the dev machine (resolved via
the parent-process PATH by `_resolve_cmd`) and could not be launched as a
`.bat` on Windows, so the tests hung until timeout on any real dev box. A
portable in-process runner subclass tests the exact same contract — subprocess
→ RunResult, failure code, transcript, live stream tee — deterministically and
cross-platform.
"""

import sys
import time
from pathlib import Path

import pytest

from ecat.runner.base import AgentRunner, RunResult


class _PyRunner(AgentRunner):
    """Runs a self-contained Python snippet as the 'agent' subprocess."""

    def __init__(self, *args, script: str, emits_stream_json: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        self._script = script
        # Instance-level override of the class attribute so a single subclass
        # can exercise both the streaming and communicate() paths.
        self.emits_stream_json = emits_stream_json

    def _build_cmd(self, model: str) -> list[str]:
        return [sys.executable, "-c", self._script]


def test_runner_returns_run_result(tmp_path: Path):
    """Runner wraps subprocess output in a RunResult with well-typed fields."""
    config_dir = tmp_path / ".ecat/agents/generator"
    config_dir.mkdir(parents=True)

    runner = _PyRunner(
        config_dir=config_dir,
        timeout_s=10,
        script="print('migration done')",
    )
    result = runner.run(prompt="do something", model="m", role="generator")

    assert isinstance(result, RunResult)
    assert result.returncode == 0
    assert "migration done" in result.stdout
    assert isinstance(result.stderr, str)
    assert result.duration_s >= 0
    assert result.success


def test_runner_failure(tmp_path: Path):
    """A non-zero exit is surfaced as success=False with the real returncode."""
    config_dir = tmp_path / ".ecat/agents/generator"
    config_dir.mkdir(parents=True)

    runner = _PyRunner(
        config_dir=config_dir,
        timeout_s=10,
        script="import sys; sys.stderr.write('boom'); sys.exit(1)",
    )
    result = runner.run(prompt="do something", model="m")

    assert not result.success
    assert result.returncode == 1


def test_runner_transcript_written(tmp_path: Path):
    """The human-readable <role>.log transcript is written to transcript_dir."""
    config_dir = tmp_path / ".ecat/agents/generator"
    config_dir.mkdir(parents=True)
    transcript_dir = tmp_path / "transcripts"

    runner = _PyRunner(
        config_dir=config_dir,
        timeout_s=10,
        transcript_dir=transcript_dir,
        script="print('transcript_content')",
    )
    result = runner.run(prompt="test", model="m", role="gen")

    assert result.transcript_path is not None
    assert result.transcript_path.exists()
    assert "transcript_content" in result.transcript_path.read_text(encoding="utf-8")


def test_runner_streams_live_to_jsonl(tmp_path: Path):
    """A stream-json runner tees each stdout line to <role>.stream.jsonl as it
    is produced — the live-refresh path. Assert the raw stream file captured
    every emitted event line exactly once (no double-write) and the final
    assistant text is reconstructed from the result event."""
    config_dir = tmp_path / ".ecat/agents/generator"
    config_dir.mkdir(parents=True)
    transcript_dir = tmp_path / "transcripts"

    # Emit three assistant events + one result event, like Claude Code stream-json.
    script = (
        "import json,sys\n"
        "for i in range(3):\n"
        "    print(json.dumps({'type':'assistant','i':i}))\n"
        "print(json.dumps({'type':'result','result':'done'}))\n"
        "sys.stdout.flush()\n"
    )
    runner = _PyRunner(
        config_dir=config_dir,
        timeout_s=10,
        transcript_dir=transcript_dir,
        emits_stream_json=True,
        script=script,
    )
    result = runner.run(prompt="test", model="m", role="disc")

    assert result.success
    stream_file = transcript_dir / "disc.stream.jsonl"
    assert stream_file.exists()
    lines = [ln for ln in stream_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
    # 3 assistant events + 1 result event, teed live and not duplicated.
    assert len(lines) == 4
    assert '"type": "result"' in lines[-1]
    # RunResult.stdout is the reconstructed final assistant text, not the raw stream.
    assert result.stdout == "done"


def test_runner_stream_timeout_does_not_hang(tmp_path: Path):
    """Regression: a stream-json child that emits an event then hangs forever
    (holding stdout open) must make run() raise a bounded TimeoutError — never
    wedge indefinitely. Before the fix the stdout reader ran on the MAIN thread,
    so a never-arriving EOF blocked run() past the watchdog's kill (observed:
    32h+ of 'Generator running…' after 'killing process tree'). Now the reader
    is a daemon thread and run() gates on proc.wait()."""
    from ecat.errors import TimeoutError as EcatTimeout

    config_dir = tmp_path / ".ecat/agents/generator"
    config_dir.mkdir(parents=True)
    transcript_dir = tmp_path / "transcripts"

    script = (
        "import json,sys,time\n"
        "print(json.dumps({'type':'assistant','i':0}))\n"
        "sys.stdout.flush()\n"
        "time.sleep(3600)\n"  # hang forever, stdout pipe stays open
    )
    runner = _PyRunner(
        config_dir=config_dir,
        timeout_s=2,
        transcript_dir=transcript_dir,
        emits_stream_json=True,
        script=script,
    )

    start = time.monotonic()
    with pytest.raises(EcatTimeout):
        runner.run(prompt="test", model="m", role="gen")
    elapsed = time.monotonic() - start
    # Watchdog kills at 2s; reader.join bounds the tail at ≤10s. If this ever
    # exceeds the bound the hang has regressed.
    assert elapsed < 60, f"run() did not return promptly on timeout ({elapsed:.0f}s)"

    # The event emitted before the hang was still teed live to the stream file.
    stream_file = transcript_dir / "gen.stream.jsonl"
    assert stream_file.exists()
    assert '"type": "assistant"' in stream_file.read_text(encoding="utf-8")
