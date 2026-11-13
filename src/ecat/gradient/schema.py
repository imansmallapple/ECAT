"""Data contracts for text gradients."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast


@dataclass
class WorkItem:
    file: Path
    issues: list[str]
    suggested_skills: list[str]
    severity: Literal["high", "medium", "low"]
    lines: list[int] | None = None
    # Machine-checkable steps that prove this item was actually fixed on device,
    # e.g. [{"tap": "设置"}, {"expect_visible": ["主题", "深色"]}]. Emitted by the
    # discriminator; executed by the UI-flow gate on the *next* round.
    verify: list[dict[str, Any]] | None = None


@dataclass
class SkillProposal:
    """Phase 3+ only. Empty list in Phase 1."""

    action: Literal["create", "refine"]
    target_path: Path
    rationale: str
    body: str


@dataclass
class TextGradient:
    summary: str
    work_list: list[WorkItem] = field(default_factory=list)
    skill_evolution: list[SkillProposal] = field(default_factory=list)  # Phase 3+
    insights: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "summary": self.summary,
            "work_list": [
                {
                    "file": str(item.file),
                    "issues": item.issues,
                    "suggested_skills": item.suggested_skills,
                    "severity": item.severity,
                    "lines": item.lines,
                    "verify": item.verify,
                }
                for item in self.work_list
            ],
            "skill_evolution": [
                {
                    "action": p.action,
                    "target_path": str(p.target_path),
                    "rationale": p.rationale,
                    "body": p.body,
                }
                for p in self.skill_evolution
            ],
            "insights": self.insights,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> TextGradient:
        raw_wl = data.get("work_list", [])
        items: list[Any] = list(raw_wl) if isinstance(raw_wl, list) else []
        work_list = [
            WorkItem(
                file=Path(str(item["file"])),
                issues=[str(i) for i in item.get("issues", [])],
                suggested_skills=[str(s) for s in item.get("suggested_skills", [])],
                severity=cast(Literal["high", "medium", "low"], str(item.get("severity", "medium"))),
                lines=item.get("lines") if isinstance(item.get("lines"), list) else None,
            )
            for item in items
            if isinstance(item, dict)
        ]
        raw_se = data.get("skill_evolution", [])
        proposals: list[Any] = list(raw_se) if isinstance(raw_se, list) else []
        skill_evolution = [
            SkillProposal(
                action=cast(Literal["create", "refine"], str(p["action"])),
                target_path=Path(str(p["target_path"])),
                rationale=str(p["rationale"]),
                body=str(p["body"]),
            )
            for p in proposals
            if isinstance(p, dict)
        ]
        raw_insights = data.get("insights", [])
        insights = [str(i) for i in raw_insights] if isinstance(raw_insights, list) else []

        return cls(
            summary=str(data.get("summary", "")),
            work_list=work_list,
            skill_evolution=skill_evolution,
            insights=insights,
        )
