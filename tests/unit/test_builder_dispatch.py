"""Tests for GradientBuilder's deferred-agentic-judge selection gate.

Regression coverage for the bug where a SKIPPED hard-check (e.g. ui_align with no
fact-tree) was still dispatched to the visual judge, which then scored stale images
from a previous run instead of contributing 0.
"""

from __future__ import annotations

import json

from ecat.entropy.base import EntropyReport
from ecat.gradient.builder import (
    _last_json_verdict_from_stream,
    _parse_discriminator_response,
    _select_deferred_agentic,
)


class _FakePlugin:
    def __init__(self, name: str, judge_mode: str = "agentic", judge_prompt: str = "score it"):
        self.name = name
        self.judge_mode = judge_mode
        self.judge_prompt = judge_prompt


def _report(breakdown, details):
    return EntropyReport(iteration=0, total_H=0.0, breakdown=breakdown, details=details)


def test_skipped_hardcheck_not_dispatched():
    """A dimension whose details say skipped must NOT be sent to the judge."""
    p = _FakePlugin("ui_align")
    report = _report({"ui_align": 0.0}, {"ui_align": {"skipped": "no_facttree"}})
    selected = _select_deferred_agentic([p], report, {"ui_align": 1.0})
    assert selected == []


def test_captured_hardcheck_is_dispatched():
    """A non-skipped dimension with a dispatch file IS sent to the judge."""
    p = _FakePlugin("ui_align")
    report = _report({"ui_align": 0.0}, {"ui_align": {"dispatch_file": "/run/0/ui_align/dispatch.json"}})
    selected = _select_deferred_agentic([p], report, {"ui_align": 1.0})
    assert [x.name for x in selected] == ["ui_align"]


def test_zero_weight_not_dispatched():
    p = _FakePlugin("ui_align")
    report = _report({"ui_align": 0.0}, {"ui_align": {"dispatch_file": "x"}})
    assert _select_deferred_agentic([p], report, {"ui_align": 0.0}) == []


def test_already_judged_not_redispatched():
    p = _FakePlugin("ui_align")
    report = _report({"ui_align": 0.5}, {"ui_align": {"agentic_verdict": {"score": 0.5}}})
    assert _select_deferred_agentic([p], report, {"ui_align": 1.0}) == []


def test_hard_mode_plugin_never_dispatched():
    p = _FakePlugin("compile", judge_mode="hard")
    report = _report({"compile": 0.2}, {"compile": {}})
    assert _select_deferred_agentic([p], report, {"compile": 1.0}) == []


def test_absent_from_breakdown_not_dispatched():
    p = _FakePlugin("ui_align")
    report = _report({}, {})
    assert _select_deferred_agentic([p], report, {"ui_align": 1.0}) == []


# ── stream JSON-verdict recovery ──────────────────────────────────────────────
# Some models (e.g. qwen) write the JSON verdict mid-turn then append a prose
# closing remark, leaving RunResult.stdout (the last message) without parseable
# JSON. The discriminator parser must recover the verdict from the earlier turn.


def _assistant_event(text: str) -> str:
    return json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}})


def _verdict_json(dim: str, score: float) -> str:
    return json.dumps({"summary": "s", "verdicts": {dim: {"score": score, "rationale": "r", "issues": ["x"]}},
                       "priority_overrides": [], "suggested_skills": [], "insights": []})


def test_stream_recovery_finds_verdict_after_trailing_prose():
    """JSON verdict in an earlier turn + a final prose turn → recovered from stream."""
    stream = "\n".join([
        _assistant_event("Working on it..."),
        _assistant_event("Here is the verdict:\n```json\n" + _verdict_json("ui_align", 0.4) + "\n```"),
        _assistant_event("The JSON above is authoritative and complete."),
    ])
    data = _last_json_verdict_from_stream(stream)
    assert data is not None and "ui_align" in data["verdicts"]

    report = _report({"ui_align": 0.0}, {"ui_align": {}})
    # RunResult.stdout is the trailing prose (no JSON) → primary parse fails,
    # recovery kicks in and the ui_align verdict survives.
    parsed = _parse_discriminator_response(
        "The JSON above is authoritative and complete.", report,
        stream=stream,
    )
    assert "ui_align" in parsed["verdicts"]


def test_stream_recovery_keeps_last_verdict_not_preliminary():
    """When two turns carry JSON, the LAST (authoritative) one wins."""
    stream = "\n".join([
        _assistant_event(_verdict_json("ui_align", 0.2)),   # preliminary
        _assistant_event(_verdict_json("ui_align", 0.9)),   # authoritative
    ])
    data = _last_json_verdict_from_stream(stream)
    assert data["verdicts"]["ui_align"]["score"] == 0.9


def test_stream_recovery_not_used_when_primary_parses():
    """Normal case: stdout already has the JSON → recovery must not override it."""
    report = _report({"ui_align": 0.0}, {"ui_align": {}})
    good = _verdict_json("ui_align", 0.3)
    # A different stream must be ignored because the primary parse succeeds.
    other_stream = _assistant_event(_verdict_json("ui_align", 0.99))
    parsed = _parse_discriminator_response(good, report, stream=other_stream)
    assert parsed["verdicts"]["ui_align"]["score"] == 0.3
