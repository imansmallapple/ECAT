"""page_audit —— Spec 驱动的页面状态审计（CHECK-5，硬+agentic 复合）。

读取 spec/baseline/ui-manifest.md 的页面清单，对每个页面主动验证文件
是否存在、是否真有内容；再按 P0/P1/P2 优先级分组统计。

具体检查项：
  1. ui-manifest.md 中每个页面对应的 .ets 文件是否存在（支持模糊名字匹配）
  2. .ets 文件是否带有 @Entry / @Component 装饰器、build() 方法、合理行数
  3. 按优先级（P0/P1/P2）分组统计 verified / partial / missing 数量
  4. P0 页面必须 100% verified 才能通过；任何 P0 缺失立刻成为高优先级 work_item
  5. Agentic judge 兜底：直接读取未 verified 页面的 .ets 文件评估实现深度

适用于任何 Android→HarmonyOS 迁移——取决于 spec/baseline/ui-manifest.md 是否提供页面清单。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from ecat.entropy.base import EntropyPlugin
from ecat.entropy.plugins.a2h_verify._signal_density import (
    combined_completeness,
    density_score,
)
from ecat.entropy.spec_parser import PageEntry, parse_source_mapping, parse_ui_manifest
from ecat.gradient.schema import WorkItem

log = logging.getLogger(__name__)

_MIN_LINES_FOR_CONVERTED = 8
_MIN_LINES_FOR_VERIFIED = 30
# Completeness thresholds blend line-count ratio + functional signal density
# (event handlers, $r() refs, ListItem count vs Android setupX, etc.).
# Density is weighted 0.6 and lines 0.4 because density tracks features more
# faithfully than physical size — ArkTS declarative UI is naturally compact.
# Thresholds are intentionally lenient: hard check only flags definite stubs;
# nuanced quality assessment is left to the agentic judge.
_COMPLETENESS_VERIFIED = 0.55  # blended ≥ 0.55 → verified
_COMPLETENESS_PARTIAL = 0.10   # blended ≥ 0.10 → converted else stub


class PageAuditPlugin(EntropyPlugin):
    """Audit page status against ui-manifest.md with priority-based scoring."""

    name = "page_audit"
    default_weight = 0.10
    judge_mode = "agentic"
    # Final score = soft (agentic judge) only. The hard @Entry/manifest rule check
    # is a coarse heuristic that false-positives on the single-@Entry + NavDestination
    # architecture (every NavDestination page reads as "missing @Entry"), leaving an
    # un-clearable 0.4-weighted floor even when the judge confirms no real issue. The
    # hard layer still runs — its details (unverified_p0, audit_results) feed the judge
    # as a baseline — but it no longer contributes to the blended score.
    hard_weight = 0.0
    judge_prompt = """\
Assess the real implementation quality of HarmonyOS pages.

Steps:
1. Check `details.unverified_p0`. For each unverified P0 page, Read the .ets file and
   assess: real business logic vs scaffolding, TODO/FIXME count and severity.
2. Spot-check P1 pages with status="stub" from `details.audit_results`.
3. Also read any page whose name contains Setting/Preference/Config/Theme/Appearance —
   even if the hard check marked it "verified". For these pages check specifically:
   - Each user-adjustable option (toggle, picker, color, theme selector) has interactive
     UI (not a hardcoded literal) AND a handler that persists the choice.
   - State is loaded from storage on entry, not hard-initialized to a constant.
   - Orphaned settings: `details.orphan_setting_candidates` are keys no behaviour page
     reads (pre-filtered). Confirm each; flag the genuinely no-effect ones, skip any you
     find a real consumer for. Empty list → none.
   Flag any option that is static or non-persistent as a separate issue.

Avoid false positives: a feature may use a different name/key/page/API than Android —
search the repo for the equivalent before flagging it missing; don't assume a 1:1 mapping.
Don't flag a default-value mismatch from the spec table alone (often auto-derived, wrong) —
only if the code default contradicts the source app's real default.

Score 0.0 (production-ready) to 1.0 (P0 pages empty/broken).
In `issues`: "path/to/Page.ets: <specific missing feature or stub observed>" """

    def extract_work_items(
        self, score: float, details: dict[str, object]
    ) -> list[WorkItem]:
        items: list[WorkItem] = []
        for r in details.get("audit_results", []):
            if isinstance(r, dict) and r.get("active_status") == "missing":
                name = str(r.get("name", r.get("page_id", "?")))
                reason = str(r.get("reason", ""))
                issue = f"Page {name} file missing"
                if reason:
                    issue += f" — {reason}"
                items.append(WorkItem(
                    file=Path("."),
                    issues=[issue],
                    suggested_skills=[],
                    severity="high",
                ))
        for uv in details.get("unverified_p0", []):
            if isinstance(uv, dict):
                name = uv.get("name", uv.get("page_id", "?"))
                status = uv.get("active_status", "?")
                reason = str(uv.get("reason", ""))
                matched = uv.get("matched_file", "")
                # Pass the actual ets file path (relative) so subagent dispatch
                # can route to it; fall back to "." when the page is missing.
                file_path = (
                    Path("entry") / "src" / "main" / "ets" / "pages" / matched
                    if matched else Path(".")
                )
                issue = f"P0 page {name} status={status}"
                if reason:
                    issue += f" — {reason}"
                items.append(WorkItem(
                    file=file_path,
                    issues=[issue],
                    suggested_skills=[],
                    severity="high",
                ))
        return items

    def evaluate(
        self,
        harmony_dir: Path,
        *,
        android_dir: Path | None = None,
        spec_dir: Path | None = None,
        config: dict[str, object] | None = None,
    ) -> tuple[float, dict[str, object]]:
        pages_dir = harmony_dir / "entry" / "src" / "main" / "ets" / "pages"

        if not pages_dir.exists():
            return 1.0, {
                "issues": ["entry/src/main/ets/pages/ directory missing"],
                "page_count": 0,
            }

        ets_files = list(pages_dir.rglob("*.ets"))
        ets_lookup = _build_ets_lookup(ets_files)
        # Fallback lookup over the WHOLE ets tree — consulted ONLY when the
        # pages/-only lookup misses, so a spec "page" implemented as a component
        # (components/, dialogs/, …) is found instead of falsely judged missing.
        # Pages already matched in pages/ keep their exact match (zero regression).
        ets_root = harmony_dir / "entry" / "src" / "main" / "ets"
        fallback_lookup = _build_ets_lookup(
            list(ets_root.rglob("*.ets")) if ets_root.exists() else []
        )

        # Build ArkTS-page-name → Android-source-file map via source-mapping.md.
        # source-mapping rows give a relative path like `activities/MainActivity.kt`
        # which is relative to the Android module's source root, not the repo
        # root. We index every .kt/.java file by basename and look up by class
        # filename to handle this without per-project hardcoding.
        arkts_to_android: dict[str, Path] = {}
        if spec_dir and android_dir:
            android_index = _build_android_basename_index(android_dir)
            for sp in parse_source_mapping(spec_dir):
                rel = sp.extras.get("android_class", "")
                if not rel:
                    continue
                basename = rel.split("/")[-1]
                if basename in android_index:
                    arkts_to_android[sp.name] = android_index[basename]

        # Check @Entry decorators (basic sanity)
        all_ets_text = {
            f.name: f.read_text(encoding="utf-8", errors="ignore")
            for f in ets_files
        }
        pages_without_entry = [
            name for name, content in all_ets_text.items()
            if "@Entry" not in content
        ]

        # If no spec_dir, fall back to basic checks
        pages = parse_ui_manifest(spec_dir) if spec_dir else []
        if not pages:
            issues: list[str] = []
            if not ets_files:
                issues.append("No .ets page files found in pages/")
            if pages_without_entry:
                issues.append(f"Pages missing @Entry: {pages_without_entry}")
            score = min(1.0, len(issues) / 2.0)
            return score, {
                "issues": issues,
                "page_count": len(ets_files),
                "pages": [f.name for f in ets_files],
                "spec_driven": False,
            }

        # Active verification: check each page's file existence + content quality
        audit_results: list[dict[str, object]] = []
        stats: dict[str, dict[str, int]] = {
            pri: {"total": 0, "verified": 0, "converted": 0, "missing": 0, "stub": 0}
            for pri in ("P0", "P1", "P2")
        }

        for page in pages:
            pri = page.priority if page.priority in stats else "P2"
            stats[pri]["total"] += 1

            ets_path = _find_ets_file(harmony_dir, page, ets_lookup)
            if ets_path is None:
                ets_path = _find_ets_file(harmony_dir, page, fallback_lookup)
            if ets_path is None:
                stats[pri]["missing"] += 1
                audit_results.append({
                    "page_id": page.page_id,
                    "name": page.name,
                    "priority": pri,
                    "active_status": "missing",
                    "reason": "no matching .ets file found",
                })
                continue

            content = ets_path.read_text(encoding="utf-8", errors="ignore")
            android_src = arkts_to_android.get(page.name) or arkts_to_android.get(ets_path.stem)
            active_status, reason = _assess_content(content, android_src, ets_path)
            stats[pri][active_status] += 1
            audit_results.append({
                "page_id": page.page_id,
                "name": page.name,
                "priority": pri,
                "active_status": active_status,
                "matched_file": ets_path.name,
                "reason": reason,
            })

        # Scoring based on active verification
        p0 = stats["P0"]
        p0_total = p0["total"]
        p0_good = p0["verified"] + p0["converted"]
        p0_score = 0.0 if p0_total == 0 else 1.0 - (p0_good / p0_total)

        p1 = stats["P1"]
        p1_total = p1["total"]
        p1_good = p1["verified"] + p1["converted"]
        p1_rate = (p1_good / p1_total) if p1_total > 0 else 1.0
        p1_penalty = max(0.0, 0.8 - p1_rate) if p1_total > 0 else 0.0

        score = min(1.0, p0_score * 0.8 + p1_penalty * 0.2)

        # Collect unverified P0 for gradient
        unverified_p0 = [
            r for r in audit_results
            if r["priority"] == "P0" and r["active_status"] not in ("verified",)
        ]

        return score, {
            "spec_driven": True,
            "audit_results": audit_results,
            "unverified_p0": unverified_p0,
            "pages_without_entry": pages_without_entry,
            # Deterministic pre-filter: setting keys read only in settings/seed
            # files (no behaviour-page consumer). The judge confirms each.
            "orphan_setting_candidates": _find_orphan_candidates(harmony_dir),
        }


_SL_KEY_RE = re.compile(r"@StorageLink\(\s*['\"]([A-Za-z_]\w*)['\"]")


def _is_settings_or_seed_file(name: str) -> bool:
    """Central settings page / seed (Ability) / constants file — where a setting
    key is only declared/toggled/seeded, not behaviourally consumed. Tight by
    design: the constants/app files match exactly (so a feature page like
    WidgetConfigPage.ets is NOT mistaken for the settings file via "Config")."""
    return name in ("Config.ets", "App.ets") or bool(
        re.search(r"Settings|Preference|Customization|Ability", name, re.IGNORECASE)
    )


def _find_orphan_candidates(harmony_dir: Path) -> list[str]:
    """@StorageLink-bound setting keys that NO behaviour page reads — candidate
    orphans (a toggle whose value has no effect). DETERMINISTIC pre-filter only;
    the agentic judge confirms each, so over-producing here never becomes a false
    positive.

    Cross-app generic: keys come from @StorageLink; a 'settings/seed' file is
    matched by name pattern (Setting/Preference/Config/Customization/Theme or
    *Ability), not a hard-coded list. Read detection covers @StorageLink,
    prefs.get and AppStorage.get<T>(...) generics, so a value consumed via any of
    these in a real page is never flagged.
    """
    ets_root = harmony_dir / "entry" / "src" / "main" / "ets"
    if not ets_root.exists():
        return []
    files: dict[str, str] = {}
    keys: set[str] = set()
    for f in ets_root.rglob("*.ets"):
        try:
            t = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        files[f.name] = t
        keys.update(_SL_KEY_RE.findall(t))
    candidates: list[str] = []
    for k in sorted(keys):
        ek = re.escape(k)
        read_re = re.compile(
            rf"@StorageLink\(\s*['\"]{ek}['\"]"
            rf"|AppStorage\.\w+(?:<[^>]*>)?\(\s*['\"]{ek}['\"]"
            rf"|prefs\.get\(\s*['\"]{ek}['\"]"
        )
        consumed = any(
            read_re.search(t)
            for name, t in files.items()
            if not _is_settings_or_seed_file(name)
        )
        if not consumed:
            candidates.append(k)
    return candidates


def _build_android_basename_index(android_dir: Path) -> dict[str, Path]:
    """basename (e.g. MainActivity.kt) → full Path; for source-mapping resolution."""
    index: dict[str, Path] = {}
    if not android_dir.exists():
        return index
    for ext in ("*.kt", "*.java"):
        for f in android_dir.rglob(ext):
            index.setdefault(f.name, f)
    return index


def _build_ets_lookup(ets_files: list[Path]) -> dict[str, Path]:
    """Build a normalized-name → Path lookup for fuzzy matching."""
    lookup: dict[str, Path] = {}
    for f in ets_files:
        key = f.stem.lower().replace("_", "").replace("-", "")
        lookup[key] = f
    return lookup


def _find_ets_file(
    harmony_dir: Path,
    page: PageEntry,
    ets_lookup: dict[str, Path],
) -> Path | None:
    """Find the .ets file for a page using exact path then fuzzy name matching."""
    if page.target_file:
        candidate = harmony_dir / "entry" / "src" / "main" / "ets" / page.target_file
        if candidate.exists():
            return candidate

    name_norm = page.name.lower().replace(" ", "").replace("_", "").replace("-", "")
    for key, path in ets_lookup.items():
        if name_norm in key or key in name_norm:
            return path
    return None


def _assess_content(
    content: str,
    android_src_path: Path | None = None,
    arkts_path: Path | None = None,
) -> tuple[str, str]:
    """Assess page content quality. Returns (status, reason).

    Status: verified / converted / stub / missing.

    When *android_src_path* and *arkts_path* are both provided, blends two
    signals to judge completeness:
      • line-count ratio   — physical size, weight 0.4
      • signal density     — functional idiom counts (onClick, $r(), ListItem,
                             …) vs Android equivalents (setOnClickListener,
                             R.string.*, setupXxx, …), weight 0.6
    Density is weighted higher because ArkTS declarative UI is naturally
    compact — a 25%-line port can still be a *complete* port if its event
    handler / resource / list-item densities match the Android original.
    """
    lines = [ln for ln in content.splitlines() if ln.strip()]
    line_count = len(lines)

    if line_count < 5:
        return "stub", f"only {line_count} non-empty lines"

    has_component = "@Component" in content or "@Entry" in content
    has_build = "build()" in content or "build ()" in content
    has_ui = bool(re.search(
        r"\b(Column|Row|List|Grid|Tabs|Stack|Navigation|Text|Image|Button)\s*[({]",
        content,
    ))
    todo_count = len(re.findall(r"\bTODO\b", content, re.IGNORECASE))

    if not has_component and not has_build:
        return "stub", "no @Component/@Entry or build() found"

    if line_count < _MIN_LINES_FOR_CONVERTED:
        return "stub", f"{line_count} lines, minimal scaffolding"

    # Blend line ratio + signal density when Android source is known.
    line_ratio: float | None = None
    density: float | None = None
    signal_breakdown: dict[str, dict[str, int | float]] = {}
    if android_src_path is not None and android_src_path.exists():
        try:
            android_text = android_src_path.read_text(encoding="utf-8", errors="ignore")
            android_lines = len(
                [ln for ln in android_text.splitlines() if ln.strip()]
            )
            if android_lines > 0:
                # 0.5 baseline accounts for ArkTS' typical compactness.
                line_ratio = min(1.0, line_count / max(1.0, android_lines * 0.5))
        except OSError:
            pass
        density, signal_breakdown = density_score(android_src_path, arkts_path)

    if line_ratio is not None or density is not None:
        completeness = combined_completeness(
            line_ratio,
            density if density is not None else 1.0,
        )
        # Build a one-line reason that exposes both signals so the discriminator
        # can spot which is the bottleneck (low lines vs low density).
        sig_summary = ""
        if signal_breakdown:
            worst = sorted(signal_breakdown.items(), key=lambda kv: kv[1]["ratio"])[:2]
            sig_summary = "; signals: " + ", ".join(
                f"{name} {int(v['arkts'])}/{int(v['android'])}"
                for name, v in worst
            )
        line_part = f"lines {line_count}" + (
            f"/{int(line_count / line_ratio / 0.5) if line_ratio and line_ratio > 0 else '?'} "
            f"({line_ratio:.0%})" if line_ratio is not None else ""
        )
        density_part = f"density {density:.0%}" if density is not None else ""

        if completeness < _COMPLETENESS_PARTIAL:
            return "stub", (
                f"completeness {completeness:.0%} ({line_part}, {density_part}{sig_summary}) "
                f"— missing most features"
            )
        if completeness < _COMPLETENESS_VERIFIED:
            return "converted", (
                f"completeness {completeness:.0%} ({line_part}, {density_part}{sig_summary})"
            )
        if has_ui and todo_count <= 2:
            return "verified", (
                f"completeness {completeness:.0%} ({line_part}, {density_part}), "
                f"UI present, {todo_count} TODOs"
            )
        return "converted", (
            f"completeness {completeness:.0%} but {todo_count} TODOs"
        )

    # Fallback: no Android source for comparison — use absolute thresholds.
    if has_ui and line_count >= _MIN_LINES_FOR_VERIFIED and todo_count <= 2:
        return "verified", f"{line_count} lines, UI components present, {todo_count} TODOs"

    return "converted", f"{line_count} lines, {todo_count} TODOs, UI={'yes' if has_ui else 'no'}"
