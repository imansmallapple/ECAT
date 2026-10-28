"""Loop policies: regression detection and oscillation guard."""

from __future__ import annotations

from ecat.errors import Oscillation, Regression


class RegressionPolicy:
    """Evaluate whether a change in H is a regression."""

    def __init__(self, tolerance: float, revert: bool) -> None:
        self.tolerance = tolerance
        self.revert = revert
        self._consecutive_regressions = 0

    def assess(self, h_prev: float, h_curr: float, iteration: int) -> str:
        """Return state string: 'improvement' | 'noise' | 'regression_grace' | 'regression_reverted' | 'regression_accepted'."""
        delta = h_curr - h_prev
        if delta <= 0:
            self._consecutive_regressions = 0
            return "improvement"
        if delta <= self.tolerance:
            self._consecutive_regressions = 0
            return "noise"
        self._consecutive_regressions += 1
        if self.revert and self._consecutive_regressions >= 2:
            self._consecutive_regressions = 0
            raise Regression(iteration, delta)
        if self.revert:
            return "regression_grace"
        return "regression_accepted"


class OscillationPolicy:
    """Detect entropy oscillation over a sliding window."""

    def __init__(self, window: int, min_delta: float, enabled: bool = True) -> None:
        self.window = window
        self.min_delta = min_delta
        self.enabled = enabled
        self._history: list[float] = []

    def record(self, h: float) -> None:
        self._history.append(h)
        if len(self._history) > self.window:
            self._history.pop(0)

    def check(self) -> None:
        if not self.enabled or len(self._history) < self.window:
            return
        spread = max(self._history) - min(self._history)
        if spread < self.min_delta:
            raise Oscillation(
                self.window, self.min_delta,
                spread=spread, history=self._history,
            )
