"""Unit tests for ecat.errors hierarchy."""


from ecat.errors import (
    ConfigError,
    EvolutionPoison,
    IterationFailed,
    Oscillation,
    RateLimitError,
    Regression,
    RetryableTransient,
    RunFailed,
    SchemaError,
    ServerError,
    WorkspaceError,
)


def test_retryable_hierarchy():
    assert issubclass(RateLimitError, RetryableTransient)
    assert issubclass(ServerError, RetryableTransient)
    assert issubclass(SchemaError, RetryableTransient)


def test_run_failed_hierarchy():
    assert issubclass(ConfigError, RunFailed)
    assert issubclass(WorkspaceError, RunFailed)


def test_iteration_failed_message():
    exc = IterationFailed(3, "compile error")
    assert "3" in str(exc)
    assert "compile error" in str(exc)


def test_regression_message():
    exc = Regression(5, 0.03)
    assert "5" in str(exc)
    assert "0.0300" in str(exc)


def test_oscillation_message():
    exc = Oscillation(window=3, min_delta=0.01)
    assert "window=3" in str(exc)


def test_evolution_poison_message():
    exc = EvolutionPoison("some/skill/SKILL.md", 3)
    assert "3" in str(exc)
    assert "some/skill/SKILL.md" in str(exc)
