"""Unit tests for deterministic work_list extraction from EntropyReport."""

from pathlib import Path

from ecat.entropy.base import EntropyReport
from ecat.entropy.plugins.a2h_verify.check_app_identity import AppIdentityPlugin
from ecat.entropy.plugins.a2h_verify.check_compile import CompilePlugin
from ecat.entropy.plugins.a2h_verify.check_entry_nav import EntryNavPlugin
from ecat.entropy.plugins.a2h_verify.check_static_lint import StaticLintPlugin
from ecat.entropy.plugins.a2h_verify.check_style import StylePlugin
from ecat.gradient.builder import _build_sorted_details, _format_work_list_summary
from ecat.gradient.extractors import extract_work_list


def _make_report(
    breakdown: dict[str, float],
    details: dict[str, dict],
) -> EntropyReport:
    total_H = sum(breakdown.values()) / max(len(breakdown), 1)
    return EntropyReport(iteration=1, total_H=total_H, breakdown=breakdown, details=details)


# ── compile ──────────────────────────────────────────────────────────────────


def test_extract_compile_errors():
    report = _make_report(
        breakdown={"compile": 0.8},
        details={"compile": {
            "errors": ["ERROR: entry/src/main/ets/pages/Index.ets:10 TS2304", "BUILD FAILED"],
            "error_count": 2,
            "compiled": False,
        }},
    )
    items = extract_work_list(report, [CompilePlugin()])
    assert len(items) == 1
    assert items[0].severity == "high"
    assert "harmonyos-build-fix" in items[0].suggested_skills
    assert len(items[0].issues) == 2


# ── entry_nav ────────────────────────────────────────────────────────────────


def test_extract_entry_nav_fails():
    report = _make_report(
        breakdown={"entry_nav": 0.5},
        details={"entry_nav": {
            "entry_file": "entry/src/main/ets/pages/Index.ets",
            "fails": ["Entry page contains Hello World text", "P0 page Detail not registered"],
            "warns": ["Tab 0 missing icon"],
            "icons": {"missing_resources": ["ic_home"]},
        }},
    )
    items = extract_work_list(report, [EntryNavPlugin()])
    high = [i for i in items if i.severity == "high"]
    medium = [i for i in items if i.severity == "medium"]
    # Now: 2 fails → 2 high, 1 missing icon → 1 high, 1 warn → 1 medium
    assert len(high) == 3
    assert len(medium) == 1


# ── empty report ─────────────────────────────────────────────────────────────


def test_extract_empty_report():
    report = _make_report(breakdown={}, details={})
    items = extract_work_list(report, [])
    assert items == []


# ── zero-score skipped ───────────────────────────────────────────────────────


def test_extract_zero_score_skipped():
    report = _make_report(
        breakdown={"compile": 0.0, "style": 0.0},
        details={
            "compile": {"errors": ["should not appear"], "error_count": 1},
            "style": {"violations": [{"file": "x.ets", "line": 1, "rule": "test"}]},
        },
    )
    items = extract_work_list(report, [CompilePlugin(), StylePlugin()])
    assert items == []


def test_extract_zero_weight_dim_skipped():
    """A disabled (weight 0) dimension with a non-zero score must not leak work
    items — breakdown carries its raw hard score regardless of weight."""
    report = _make_report(
        breakdown={"compile": 0.8},
        details={"compile": {"errors": ["E1: missing import"], "error_count": 1}},
    )
    # Without the filter the compile error would produce a work item.
    assert extract_work_list(report, [CompilePlugin()]) != []
    # With compile disabled via weight 0, it is filtered out.
    items = extract_work_list(report, [CompilePlugin()], zero_weight_dims={"compile"})
    assert items == []


# ── severity ordering ────────────────────────────────────────────────────────


def test_work_list_severity_ordering():
    report = _make_report(
        breakdown={"style": 0.3, "entry_nav": 0.5, "static_lint": 0.4},
        details={
            "style": {"violations": [{"file": "a.ets", "line": 1, "rule": "var usage"}]},
            "entry_nav": {
                "entry_file": None,
                "fails": ["Hello World"],
                "warns": ["Tab missing"],
                "icons": {},
            },
            "static_lint": {
                "errors": [{"file": "b.ets", "line": 5, "pattern": "Android click", "severity": "ERROR"}],
                "warnings": [{"file": "c.ets", "line": 10, "pattern": "deprecated import", "severity": "WARN"}],
            },
        },
    )
    plugins = [EntryNavPlugin(), StaticLintPlugin(), StylePlugin()]
    items = extract_work_list(report, plugins)
    severities = [i.severity for i in items]
    # high items come first, then medium, then low
    assert severities == sorted(severities, key=lambda s: {"high": 0, "medium": 1, "low": 2}[s])
    assert "high" in severities
    assert "low" in severities


# ── sorted details truncation ────────────────────────────────────────────────


def test_sorted_details_truncation():
    big_details = {f"dim_{i}": {"data": "x" * 500} for i in range(20)}
    breakdown = {f"dim_{i}": (20 - i) / 20.0 for i in range(20)}
    report = _make_report(breakdown=breakdown, details=big_details)
    result = _build_sorted_details(report, max_chars=3000)
    assert "truncated" in result
    # Worst dimension (dim_0, score=1.0) should be present
    assert "dim_0" in result


# ── work_list summary formatting ──────────────────────────────────────────────


def test_format_work_list_summary_empty():
    assert _format_work_list_summary([], max_items=5) == "(no issues extracted)"


def test_format_work_list_summary_truncated():
    from ecat.gradient.schema import WorkItem
    items = [
        WorkItem(file=Path(f"page{i}.ets"), issues=[f"issue {i}"], suggested_skills=[], severity="high")
        for i in range(10)
    ]
    result = _format_work_list_summary(items, max_items=3)
    assert "page0.ets" in result
    assert "7 more items" in result


# ── app_identity extraction ──────────────────────────────────────────────────


def test_extract_app_identity():
    report = _make_report(
        breakdown={"app_identity": 0.3},
        details={"app_identity": {
            "fails": ["bundleName is com.example.myapp"],
            "warns": ["icon foreground.png < 1KB"],
        }},
    )
    items = extract_work_list(report, [AppIdentityPlugin()])
    high = [i for i in items if i.severity == "high"]
    low = [i for i in items if i.severity == "low"]
    assert len(high) == 1
    assert len(low) == 1
    assert items[0].file == Path("AppScope/app.json5")
