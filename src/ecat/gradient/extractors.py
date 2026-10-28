"""Work-list extraction from EntropyReport via plugin-owned extractors.

Each plugin implements `extract_work_items(score, details)` on its class.
This module provides the public entry point that iterates plugins by score
(worst first) and returns items sorted by severity.
"""

from __future__ import annotations

import re
from pathlib import Path

from ecat.entropy.base import EntropyPlugin, EntropyReport
from ecat.gradient.schema import WorkItem

_SEVERITY_ORDER: dict[str, int] = {"high": 0, "medium": 1, "low": 2}

# Match a leading file path in an agentic issue string, e.g.
# "entry/src/main/ets/pages/Foo.ets: ..." or "pages/Foo.ets line 42: ..."
_ISSUE_FILE_RE = re.compile(r"^([\w./\\-]+\.ets)\b")


def _file_from_issue(issue: str) -> Path:
    m = _ISSUE_FILE_RE.match(issue.strip())
    return Path(m.group(1)) if m else Path(".")


def _agentic_verdict_items(
    plugin: EntropyPlugin,
    details: dict[str, object],
    score: float,
) -> list[WorkItem]:
    """Convert agentic_verdict.issues into WorkItems if the plugin has no
    hard-check-derived items (i.e. its own extract_work_items returned nothing).
    Always called for agentic plugins so the Generator receives actionable tasks
    even when the hard-check score is zero.
    """
    if plugin.judge_mode != "agentic":
        return []
    verdict = (details.get("agentic_verdict") or {})
    if not isinstance(verdict, dict):
        return []
    issues: list[str] = verdict.get("issues") or []
    if not issues:
        return []
    sev = "high" if score >= 0.3 else "medium" if score >= 0.1 else "low"
    default_skill = getattr(plugin, "default_fix_skill", "") or ""
    suggested = [default_skill] if default_skill else []
    items: list[WorkItem] = []
    # Group issues by file so the Generator can dispatch one subagent per file.
    by_file: dict[Path, list[str]] = {}
    for iss in issues:
        fp = _file_from_issue(str(iss))
        by_file.setdefault(fp, []).append(str(iss))
    for fp, file_issues in by_file.items():
        items.append(WorkItem(
            file=fp,
            issues=file_issues,
            suggested_skills=list(suggested),
            severity=sev,
        ))
    return items


def extract_work_list(
    report: EntropyReport,
    plugins: list[EntropyPlugin],
    zero_weight_dims: set[str] | None = None,
) -> list[WorkItem]:
    """Extract WorkItems from all plugins, sorted by severity.

    `zero_weight_dims` (dims disabled via weight 0) are skipped — the breakdown
    carries every plugin's raw hard score regardless of weight, so without this
    a disabled dimension that still found issues would leak work items.
    """
    zero_weight_dims = zero_weight_dims or set()
    plugin_map = {p.name: p for p in plugins}
    items: list[WorkItem] = []
    for name, score in sorted(report.breakdown.items(), key=lambda x: -x[1]):
        if score <= 0.0 or name in zero_weight_dims:
            continue
        plugin = plugin_map.get(name)
        if plugin is None:
            continue
        details = report.details.get(name, {}) or {}
        hard_items = plugin.extract_work_items(score, details)
        items.extend(hard_items)
        # Agentic verdict issues are always surfaced as WorkItems so the
        # Generator has actionable tasks even when hard-check items are absent.
        items.extend(_agentic_verdict_items(plugin, details, score))
    items.sort(key=lambda w: _SEVERITY_ORDER.get(w.severity, 9))
    return items
