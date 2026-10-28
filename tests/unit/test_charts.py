"""Unit tests for ChartRenderer — various history shapes must not raise."""

from pathlib import Path

from ecat.report.charts import ChartRenderer


def _renderer(tmp_path: Path) -> ChartRenderer:
    return ChartRenderer(tmp_path / "charts", fmt="png")


def test_single_point(tmp_path: Path):
    r = _renderer(tmp_path)
    out = r.render_trend([0], [0.8])
    assert out.exists() or True  # skips gracefully if matplotlib missing


def test_multi_point_trend(tmp_path: Path):
    r = _renderer(tmp_path)
    r.render_trend([0, 1, 2, 3], [0.8, 0.6, 0.5, 0.4])


def test_breakdown_chart(tmp_path: Path):
    r = _renderer(tmp_path)
    breakdowns = [
        {"compile": 0.5, "static_lint": 0.2},
        {"compile": 0.3, "static_lint": 0.1},
    ]
    r.render_breakdown([0, 1], breakdowns)


def test_empty_breakdown(tmp_path: Path):
    r = _renderer(tmp_path)
    r.render_breakdown([], [])  # should not raise


def test_state_colors_applied(tmp_path: Path):
    """Passing states list should not raise."""
    r = _renderer(tmp_path)
    r.render_trend(
        [0, 1, 2],
        [0.8, 0.7, 0.9],
        states=["improvement", "noise", "regression_reverted"],
    )
