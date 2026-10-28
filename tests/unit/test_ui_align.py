"""Unit tests for the device-driven ui_align plugin + visual-judge dispatch."""

from pathlib import Path

from ecat.entropy.base import EntropyReport
from ecat.entropy.plugins.a2h_verify import check_ui_align as cua
from ecat.entropy.plugins.a2h_verify.check_ui_align import UiAlignPlugin
from ecat.gradient.builder import _build_judge_dispatch_section
from ecat.gradient.extractors import extract_work_list


def _report(breakdown, details):
    total_H = sum(breakdown.values()) / max(len(breakdown), 1)
    return EntropyReport(iteration=1, total_H=total_H, breakdown=breakdown, details=details)


# ── hard-check gating ─────────────────────────────────────────────────────────


def test_ui_align_no_device_skips(monkeypatch, tmp_path: Path):
    # adb/hdc unresolvable → clean skip contributing 0 to H, no WorkItems.
    monkeypatch.setattr(cua, "_resolve_adb_exe", lambda: None)
    monkeypatch.setattr(cua, "_resolve_hdc_exe", lambda _deveco: None)
    plugin = UiAlignPlugin()
    score, details = plugin.evaluate(tmp_path, android_dir=tmp_path, spec_dir=tmp_path)
    assert score == 0.0
    assert details.get("skipped") == "no_device"
    assert plugin.extract_work_items(score, details) == []


def test_ui_align_no_facttree_skips(monkeypatch, tmp_path: Path):
    # Devices present but no fact-tree (and bootstrap unavailable) → skip + insight.
    monkeypatch.setattr(cua, "_resolve_adb_exe", lambda: "adb")
    monkeypatch.setattr(cua, "_resolve_hdc_exe", lambda _deveco: "hdc")
    monkeypatch.setattr(cua, "_resolve_android_serial", lambda _exe: "emulator-5554")
    monkeypatch.setattr(cua, "_resolve_hmos_target", lambda _exe: "127.0.0.1:5555")
    monkeypatch.setattr(cua, "_locate_fact_tree", lambda *a, **k: None)
    score, details = UiAlignPlugin().evaluate(tmp_path, android_dir=tmp_path, spec_dir=tmp_path)
    assert score == 0.0
    assert details.get("skipped") == "no_facttree"
    assert "fact-tree" in details.get("insight", "").lower()


# ── verdict → WorkItem routing ────────────────────────────────────────────────


def test_ui_align_verdict_issues_become_workitems_no_skill():
    # Visual gaps yield WorkItems keyed on the .ets path, but with NO suggested
    # skill — UI fixing goes through visual-verify's visual-fixer flow, which
    # self-routes skills; recommending one ECAT skill here would mis-route.
    report = _report(
        {"ui_align": 0.5},
        {"ui_align": {
            "subset": [{"page": "AccountInfo", "ets_path": "pages/AccountInfo.ets"}],
            "agentic_verdict": {
                "score": 0.5,
                "issues": ["pages/AccountInfo.ets: HMOS missing top NavHeader back button"],
            },
        }},
    )
    items = extract_work_list(report, [UiAlignPlugin()])
    visual = [i for i in items if str(i.file).replace("\\", "/") == "pages/AccountInfo.ets"]
    assert visual, "visual gap issue should yield a WorkItem keyed on the .ets path"
    assert visual[0].suggested_skills == []


def test_ui_align_missing_ets_yields_high_workitem_no_skill():
    report = _report(
        {"ui_align": 0.3},
        {"ui_align": {"subset": [{"page": "Ghost", "ets_path": None}]}},
    )
    items = extract_work_list(report, [UiAlignPlugin()])
    high = [i for i in items if i.severity == "high" and "Ghost" in "; ".join(i.issues)]
    assert high
    assert high[0].suggested_skills == []


# ── dispatch renders the device-capable judge ─────────────────────────────────


def test_dispatch_section_uses_visual_judge_subagent():
    report = _report({"ui_align": 0.4}, {"ui_align": {"subset_pages": ["A", "B"]}})
    section = _build_judge_dispatch_section([UiAlignPlugin()], report, subagent_tool="Agent")
    assert 'subagent_type="visual-judge"' in section
    assert 'subagent_type="entropy-judge"' not in section
