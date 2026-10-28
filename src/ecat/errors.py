"""ECAT exception hierarchy."""

from __future__ import annotations


class EcatError(Exception):
    """Base class for all ECAT errors."""


# ── Transient / retryable ────────────────────────────────────────────────────


class RetryableTransient(EcatError):
    """Transient failure that should be retried (LLM 429/5xx, timeout, bad JSON)."""


class RateLimitError(RetryableTransient):
    """HTTP 429 from an LLM provider."""


class ServerError(RetryableTransient):
    """HTTP 5xx from an LLM provider."""


class TimeoutError(RetryableTransient):  # noqa: A001
    """Subprocess or API call timed out."""


class SchemaError(RetryableTransient):
    """Structured output did not conform to the expected schema."""


# ── Iteration-level ──────────────────────────────────────────────────────────


class IterationFailed(EcatError):
    """A single adversarial iteration failed after exhausting retries."""

    def __init__(self, iteration: int, reason: str) -> None:
        self.iteration = iteration
        self.reason = reason
        super().__init__(f"Iteration {iteration} failed: {reason}")


# ── Run-level ────────────────────────────────────────────────────────────────


class RunFailed(EcatError):
    """Unrecoverable run-level failure (bad config, missing CLI, missing API key)."""


class ConfigError(RunFailed):
    """Invalid or incomplete configuration."""


class WorkspaceError(RunFailed):
    """Workspace directory structure is corrupt or inaccessible."""


class AgentQuotaExhausted(RunFailed):
    """Agent CLI hit a non-recoverable quota wall (session limit / 1M-context billing).

    Both error families require user action (wait for session reset, or enable
    usage credits in claude.ai settings) and cannot be retried in-loop — every
    subsequent call burns time for nothing. The driver catches this to break
    the loop immediately instead of falling through to the empty-gradient path.
    """

    def __init__(self, role: str, reason: str) -> None:
        self.role = role
        self.reason = reason
        super().__init__(f"{role} quota exhausted: {reason}")


# ── Loop dynamics ────────────────────────────────────────────────────────────


class Regression(EcatError):
    """Entropy increased beyond regression_tolerance."""

    def __init__(self, iteration: int, delta: float) -> None:
        self.iteration = iteration
        self.delta = delta
        super().__init__(f"Regression at iteration {iteration}: ΔH = +{delta:.4f}")


class Oscillation(EcatError):
    """Entropy oscillating within a sliding window without meaningful progress."""

    def __init__(
        self, window: int, min_delta: float,
        spread: float | None = None, history: list[float] | None = None,
    ) -> None:
        self.window = window
        self.min_delta = min_delta
        self.spread = spread
        self.history = list(history) if history is not None else None
        detail = ""
        if spread is not None:
            detail = f", actual range={spread:.4f}"
        if history:
            detail += f", H series={[round(h, 4) for h in history]}"
        super().__init__(
            f"Oscillation detected over window={window} (threshold {min_delta}){detail}"
        )


# ── Skill evolution ──────────────────────────────────────────────────────────


class EvolutionPoison(EcatError):
    """Discriminator-written skill caused consecutive entropy increases."""

    def __init__(self, skill_path: str, consecutive: int) -> None:
        self.skill_path = skill_path
        self.consecutive = consecutive
        super().__init__(
            f"Evolution poison: {skill_path} caused {consecutive} consecutive regressions"
        )
