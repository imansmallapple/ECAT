"""Unit tests for gradient schema round-trip and fallback parsing."""

import json
from pathlib import Path

from ecat.gradient.schema import TextGradient, WorkItem


def _sample_gradient() -> TextGradient:
    return TextGradient(
        summary="Fix click handlers and placeholders.",
        work_list=[
            WorkItem(
                file=Path("entry/src/main/ets/pages/Index.ets"),
                issues=["Android-style setOnClickListener found"],
                suggested_skills=["arkts-event-binding"],
                severity="high",
                lines=[42],
            ),
            WorkItem(
                file=Path("entry/src/main/ets/pages/Detail.ets"),
                issues=["TODO placeholder"],
                suggested_skills=[],
                severity="low",
            ),
        ],
    )


def test_round_trip():
    original = _sample_gradient()
    data = original.to_dict()
    restored = TextGradient.from_dict(data)

    assert restored.summary == original.summary
    assert len(restored.work_list) == 2
    assert restored.work_list[0].severity == "high"
    assert restored.work_list[0].lines == [42]
    assert restored.work_list[1].lines is None


def test_json_round_trip():
    original = _sample_gradient()
    serialised = json.dumps(original.to_dict())
    data = json.loads(serialised)
    restored = TextGradient.from_dict(data)
    assert restored.summary == original.summary


def test_from_dict_missing_fields():
    """from_dict should tolerate missing optional fields."""
    data = {"summary": "minimal", "work_list": [], "skill_evolution": []}
    g = TextGradient.from_dict(data)
    assert g.summary == "minimal"
    assert g.work_list == []
    assert g.skill_evolution == []


def test_empty_gradient():
    g = TextGradient(summary="")
    d = g.to_dict()
    assert d["work_list"] == []
    assert d["skill_evolution"] == []
