"""Integration tests for LoopDriver using stubbed entropy + runner."""

from pathlib import Path

from ecat.entropy.base import EntropyPlugin
from ecat.entropy.engine import EntropyEngine
from ecat.entropy.registry import PluginRegistry
from ecat.gradient.builder import GradientBuilder
from ecat.loop.driver import LoopDriver
from ecat.loop.policies import OscillationPolicy, RegressionPolicy
from ecat.report.timeseries import EntropyTimeseries
from ecat.runner.base import AgentRunner, RunResult

# ── stubs ─────────────────────────────────────────────────────────────────────


class _DecreasingPlugin(EntropyPlugin):
    name = "stub"
    default_weight = 1.0

    def __init__(self) -> None:
        self._call = 0

    def evaluate(self, harmony_dir, **kwargs):  # type: ignore[override]
        self._call += 1
        return max(0.0, 1.0 - self._call * 0.2), {"call": self._call}


class _FakeRunner(AgentRunner):
    def _build_cmd(self, model: str) -> list[str]:
        return ["echo", "ok"]

    def run(self, prompt, model, env=None, cwd=None, role="agent"):  # type: ignore[override]
        return RunResult(returncode=0, stdout="ok", stderr="", duration_s=0.01)


def _make_driver(tmp_path: Path, max_iter: int = 5) -> LoopDriver:
    # Register stub plugin set
    plugin = _DecreasingPlugin()
    PluginRegistry.register("stub_set", [plugin])

    engine = EntropyEngine("stub_set", {"stub": 1.0})

    fake_runner = _FakeRunner(config_dir=tmp_path / ".ecat/agents/gen")

    gradient_gen = GradientBuilder(
        runner=fake_runner,
        model="model",
        plugins=engine.plugins,
        env=None,
        cwd=tmp_path,
    )

    timeseries = EntropyTimeseries(tmp_path / "runs" / "entropy_history.jsonl")

    harmony = tmp_path / "harmony"
    harmony.mkdir(parents=True)

    return LoopDriver(
        harmony_dir=harmony,
        android_dir=None,
        spec_dir=None,
        runs_dir=tmp_path / "runs",
        entropy_engine=engine,
        gradient_builder=gradient_gen,
        generator_runner=fake_runner,
        generator_model="model",
        generator_env=None,
        max_iterations=max_iter,
        entropy_threshold=0.05,
        regression_policy=RegressionPolicy(0.02, False),
        oscillation_policy=OscillationPolicy(3, 0.01, enabled=False),
        timeseries=timeseries,
    )


def test_loop_runs_and_converges(tmp_path: Path):
    driver = _make_driver(tmp_path, max_iter=6)
    reports = driver.run()
    # Should converge (H reaches ~0.0) before max_iter
    assert len(reports) >= 1
    assert reports[-1].total_H <= 0.05 + 0.01  # within epsilon


def test_loop_early_stop(tmp_path: Path):
    """If first H is already below threshold, loop should stop after iter 0."""
    PluginRegistry.register("zero_set", [_ZeroPlugin()])
    engine = EntropyEngine("zero_set", {"zero": 1.0})

    fake_runner = _FakeRunner(config_dir=tmp_path / ".ecat/agents/gen")
    gradient_gen = GradientBuilder(runner=fake_runner, model="m", plugins=engine.plugins, env=None, cwd=tmp_path)
    ts = EntropyTimeseries(tmp_path / "runs" / "entropy_history.jsonl")
    harmony = tmp_path / "harmony2"
    harmony.mkdir()

    driver = LoopDriver(
        harmony_dir=harmony,
        android_dir=None,
        spec_dir=None,
        runs_dir=tmp_path / "runs",
        entropy_engine=engine,
        gradient_builder=gradient_gen,
        generator_runner=fake_runner,
        generator_model="m",
        generator_env=None,
        max_iterations=10,
        entropy_threshold=0.5,
        regression_policy=RegressionPolicy(0.02, False),
        oscillation_policy=OscillationPolicy(3, 0.01, enabled=False),
        timeseries=ts,
        stable_iters_to_stop=1,
    )
    reports = driver.run()
    assert len(reports) == 1  # stopped immediately


class _ZeroPlugin(EntropyPlugin):
    name = "zero"
    default_weight = 1.0

    def evaluate(self, harmony_dir, **kwargs):  # type: ignore[override]
        return 0.0, {}
