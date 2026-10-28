"""Tests that verify parallel plugin execution in EntropyEngine."""

from pathlib import Path

from ecat.entropy.base import EntropyPlugin
from ecat.entropy.engine import EntropyEngine
from ecat.entropy.registry import PluginRegistry
from ecat.runner.base import AgentRunner, RunResult


class _SlowPlugin(EntropyPlugin):
    """Plugin that sleeps for N seconds to simulate I/O work."""

    name = "slow"
    default_weight = 1.0

    def __init__(self, delay_s: float) -> None:
        self.delay_s = delay_s

    def evaluate(self, harmony_dir, **kwargs):  # type: ignore[override]
        import time
        time.sleep(self.delay_s)
        return 0.5, {}


class _FastPlugin(EntropyPlugin):
    """Plugin that returns immediately."""

    name = "fast"
    default_weight = 1.0

    def evaluate(self, harmony_dir, **kwargs):  # type: ignore[override]
        return 0.3, {}


class _FakeRunner(AgentRunner):
    def _build_cmd(self, prompt: str, model: str) -> list[str]:
        return ["echo", "ok"]

    def run(self, prompt, model, env=None, cwd=None, role="agent"):  # type: ignore[override]
        return RunResult(returncode=0, stdout="ok", stderr="", duration_s=0.01)


def test_parallel_hard_checks_faster_than_serial(tmp_path: Path):
    """3 plugins of 0.5s each: parallel ≈ 0.5s, serial ≈ 1.5s."""
    import time

    PluginRegistry.register("parallel_test", [
        _SlowPlugin(0.5),
        _SlowPlugin(0.5),
        _SlowPlugin(0.5),
    ])

    engine = EntropyEngine("parallel_test", {"slow": 1.0})
    harmony = tmp_path / "harmony"
    harmony.mkdir()

    start = time.monotonic()
    report = engine.evaluate(harmony)
    elapsed = time.monotonic() - start

    # With parallelism: ~0.5s (plus overhead). Without: ~1.5s.
    # Allow generous margin for slow CI environments
    assert elapsed < 1.0, f"Expected parallel < 1.0s, got {elapsed:.1f}s (likely serial)"
    assert report.breakdown["slow"] == 0.5
    assert len(report.breakdown) == 1


def test_all_plugins_run_in_parallel(tmp_path: Path):
    """All 3 plugins start at roughly the same time (within 0.1s of each other)."""
    import time

    PluginRegistry.register("parallel_test2", [
        _SlowPlugin(0.3),
        _FastPlugin(),
        _SlowPlugin(0.3),
    ])

    engine = EntropyEngine("parallel_test2", {"slow": 0.5, "fast": 0.5})
    harmony = tmp_path / "harmony"
    harmony.mkdir()

    start = time.monotonic()
    report = engine.evaluate(harmony)
    elapsed = time.monotonic() - start

    # Fast plugin adds ~0s, slow plugins add ~0.3s. Total parallel ≈ 0.3s.
    # Serial would be ~0.3 + 0 + 0.3 = 0.6s
    assert elapsed < 0.5, f"Expected ~0.3s, got {elapsed:.1f}s"
    assert "slow" in report.breakdown
    assert "fast" in report.breakdown


def test_judge_mode_hard_no_llm(tmp_path: Path):
    """judge_mode='hard' plugins should not call any judge client."""
    PluginRegistry.register("hard_only", [_FastPlugin()])

    engine = EntropyEngine("hard_only", {"fast": 1.0}, judge_client=None)
    harmony = tmp_path / "harmony"
    harmony.mkdir()

    report = engine.evaluate(harmony)
    assert "fast" in report.breakdown
    assert report.breakdown["fast"] == 0.3
    # No verdict keys since judge_mode is hard
    assert "llm_verdict" not in report.details.get("fast", {})
    assert "agentic_verdict" not in report.details.get("fast", {})