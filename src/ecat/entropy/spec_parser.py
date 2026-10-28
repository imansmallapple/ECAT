"""Shared parsers for spec markdown files used by multiple entropy plugins.

Parses ui-manifest.md, feature-index.md, placeholder-registry.md tables,
and falls back to L1-domain.md, L2-migration.md, source-mapping.md,
and ui-graph JSON when the primary spec files are absent.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ── ui-manifest.md ──────────────────────────────────────────────────────────


@dataclass
class PageEntry:
    page_id: str
    name: str
    priority: str  # P0 / P1 / P2
    status: str  # pending / converted / verified / skipped
    confidence: str  # high / medium / low
    target_file: str  # expected .ets path relative to entry/src/main/ets/
    extras: dict[str, Any] = field(default_factory=dict)


def parse_ui_manifest(spec_dir: Path | None) -> list[PageEntry]:
    """Parse spec/baseline/ui-manifest.md page table; fall back to source-mapping /
    ui page files. Returns whichever covers MORE pages (tie → the manifest table,
    which carries richer priority/confidence/status than the fallback defaults), so
    a small summary table can't mask a fuller ui/ page set."""
    if spec_dir is None:
        return []
    header_pages: list[PageEntry] = []
    manifest = spec_dir / "baseline" / "ui-manifest.md"
    if manifest.exists():
        header_pages = _parse_page_table(manifest.read_text(encoding="utf-8", errors="ignore"))
    fallback_pages = parse_source_mapping(spec_dir)
    if header_pages and len(header_pages) >= len(fallback_pages):
        return header_pages
    return fallback_pages


# Header keyword sets for locating columns in a page-list table. Column ORDER
# varies across spec formats (序号|Android|ArkTS产出|优先级|conf|状态,
# ID|Source Activity|ArkTS target|Priority|Status|..., 7-col Wordpress, etc.),
# so columns are mapped by header name rather than by fixed position.
_HDR_ARKTS = ("arkts", "产出", "输出", "target")
_HDR_PRIORITY = ("优先级", "priority", "prio")
_HDR_PAGEID = ("序号", "编号", "id", "#")
_HDR_CONF = ("confidence", "conf", "置信")
_HDR_STATUS = ("状态", "status")


def _split_cols(line: str) -> list[str]:
    cols = [c.strip().strip("`").strip() for c in line.split("|")]
    return [c for c in cols if c]


def _hdr_index(headers: list[str], keys: tuple[str, ...]) -> int | None:
    for i, h in enumerate(headers):
        hl = h.lower()
        if any(k in hl for k in keys):
            return i
    return None


def _norm_page_id(raw: str, seq: int) -> str:
    raw = (raw or "").strip()
    m = re.match(r"(page_\d+)", raw, re.IGNORECASE)
    if m:
        return m.group(1).lower()
    if re.fullmatch(r"\d+", raw):
        return f"page_{int(raw):04d}"
    if re.fullmatch(r"[Pp]\d+", raw):
        return raw.upper()
    return f"page_{seq:04d}"


def _parse_page_table(text: str) -> list[PageEntry]:
    """Locate the page-list table by its header — it must have BOTH an ArkTS-output
    column and a priority column (this rules out naming-convention / stats tables
    that share an "ArkTS" header) — then map columns by header name. Falls back to
    the legacy col[0]-regex scan when no qualifying header table is found, so older
    spec layouts keep working."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if not line.strip().startswith("|"):
            continue
        headers = _split_cols(line)
        ai = _hdr_index(headers, _HDR_ARKTS)
        pi = _hdr_index(headers, _HDR_PRIORITY)
        if ai is None or pi is None:
            continue
        idi = _hdr_index(headers, _HDR_PAGEID)
        ci = _hdr_index(headers, _HDR_CONF)
        si = _hdr_index(headers, _HDR_STATUS)
        pages: list[PageEntry] = []
        seq = 0
        for row in lines[i + 1:]:
            rs = row.strip()
            if not rs.startswith("|"):
                break  # table ended
            if _TABLE_SEP.match(rs):
                continue
            cols = _split_cols(rs)
            if len(cols) <= max(ai, pi):
                continue
            ets_rel = re.sub(r"^entry/src/main/ets/", "", cols[ai].strip())
            stem = (Path(ets_rel).stem if ets_rel.endswith(".ets") else ets_rel).strip()
            if not stem:
                continue
            seq += 1
            raw_id = cols[idi] if (idi is not None and idi < len(cols)) else ""
            pages.append(PageEntry(
                page_id=_norm_page_id(raw_id, seq),
                name=stem,
                priority=_norm_priority(cols[pi]),
                status=(cols[si].lower() if (si is not None and si < len(cols)) else "pending"),
                confidence=(cols[ci].lower() if (ci is not None and ci < len(cols)) else "medium"),
                target_file=ets_rel if ets_rel.endswith(".ets") else f"{stem}.ets",
            ))
        if pages:
            return pages
    return _parse_page_table_legacy(text)


def _parse_page_table_legacy(text: str) -> list[PageEntry]:
    rows = _extract_table_rows(text)
    pages: list[PageEntry] = []
    for row in rows:
        cols = [c.strip() for c in row.split("|")]
        cols = [c for c in cols if c]
        if len(cols) < 5:
            continue
        page_id = cols[0]
        if not re.match(r"page_\d+|P\d+", page_id, re.IGNORECASE):
            continue
        pages.append(PageEntry(
            page_id=page_id,
            name=cols[1] if len(cols) > 1 else "",
            priority=_norm_priority(cols[2] if len(cols) > 2 else "P2"),
            status=cols[3].lower() if len(cols) > 3 else "pending",
            confidence=cols[4].lower() if len(cols) > 4 else "medium",
            target_file=cols[5] if len(cols) > 5 else "",
        ))
    return pages


def _norm_priority(raw: str) -> str:
    raw = raw.strip().upper()
    if raw in ("P0", "P1", "P2"):
        return raw
    return "P2"


# ── feature-index.md ────────────────────────────────────────────────────────


@dataclass
class FeatureEntry:
    feature_id: str  # F-xxx
    name: str
    version: str  # V1 / V2
    output_files: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    extras: dict[str, str] = field(default_factory=dict)


def parse_feature_index(spec_dir: Path | None) -> list[FeatureEntry]:
    """Parse spec/baseline/feature-index.md, fallback to L2 SS6 then L1 D4."""
    if spec_dir is None:
        return []
    index = spec_dir / "baseline" / "feature-index.md"
    if index.exists():
        text = index.read_text(encoding="utf-8", errors="ignore")
        features = _parse_feature_table(text)
        if features:
            return features
    features = parse_l2_feature_phasing(spec_dir)
    if features:
        return features
    return parse_l1_features(spec_dir)


def _norm_version(raw: str) -> str:
    """Normalise a feature-table column-3 value to V1/V2.

    Old layout puts the version (V1/V2) there; new layout puts the priority
    (P0/P1/P2). Adapt by value, not by header text. Reuses the P0/P1→V1,
    P2→V2 convention from parse_l1_features. Unknown values default to V1.
    """
    val = raw.strip().upper()
    if val in ("V1", "V2"):
        return val
    if val in ("P0", "P1", "P2"):
        return "V2" if val == "P2" else "V1"
    return "V1"


def _parse_feature_table(text: str) -> list[FeatureEntry]:
    rows = _extract_table_rows(text)
    features: list[FeatureEntry] = []
    for row in rows:
        cols = [c.strip() for c in row.split("|")]
        cols = [c for c in cols if c]
        if len(cols) < 3:
            continue
        fid = cols[0]
        # Accept both "F-001" and "F001" spellings
        if not re.match(r"F-?\d+", fid, re.IGNORECASE):
            continue
        # Only treat a column as output_files if values contain .ets paths.
        # Other formats use col[3] for dependencies or page counts.
        output_files: list[str] = []
        for col_val in cols[3:]:
            candidates = [f.strip() for f in col_val.split(",") if f.strip()]
            ets = [c for c in candidates if c.endswith(".ets")]
            if ets:
                output_files = ets
                break
        if not output_files:
            # Specs that list involved pages by name ("MainPage, MediaPage")
            # instead of .ets paths → synthesise pages/<Name>.ets.
            for col_val in cols[3:]:
                names = re.findall(r"\b([A-Z][A-Za-z0-9]*Page)\b", col_val)
                if names:
                    output_files = [f"pages/{n}.ets" for n in dict.fromkeys(names)]
                    break
        features.append(FeatureEntry(
            feature_id=fid,
            name=cols[1] if len(cols) > 1 else "",
            version=_norm_version(cols[2]) if len(cols) > 2 else "V1",
            output_files=output_files,
        ))
    return features


# ── placeholder-registry.md ─────────────────────────────────────────────────


@dataclass
class PlaceholderEntry:
    ph_id: str  # PH-xxx
    file: str
    ph_type: str  # navigation / data / ui / ...
    target: str
    status: str  # pending / resolved / deferred
    note: str = ""


def parse_placeholder_registry(spec_dir: Path | None) -> list[PlaceholderEntry]:
    """Parse spec/placeholder-registry.md table."""
    if spec_dir is None:
        return []
    for candidate in (
        spec_dir / "placeholder-registry.md",
        spec_dir / "baseline" / "placeholder-registry.md",
    ):
        if candidate.exists():
            text = candidate.read_text(encoding="utf-8", errors="ignore")
            return _parse_placeholder_table(text)
    return []


def _parse_placeholder_table(text: str) -> list[PlaceholderEntry]:
    rows = _extract_table_rows(text)
    entries: list[PlaceholderEntry] = []
    for row in rows:
        cols = [c.strip() for c in row.split("|")]
        cols = [c for c in cols if c]
        if len(cols) < 4:
            continue
        ph_id = cols[0]
        # New layout: P-S1-001 etc. (P-, optional segment, digits)
        if re.match(r"P-[A-Za-z0-9]{2,}-\d+", ph_id):
            # Columns: P-ID | location | trigger | status | kind | resolve_by
            # NOTE: new schema reuses PlaceholderEntry fields with shifted meaning:
            # target carries trigger_condition, ph_type carries kind. Min cols=4 (status@3);
            # legacy needs >=5 (status@4) and is guarded separately below.
            entries.append(PlaceholderEntry(
                ph_id=ph_id,
                file=cols[1] if len(cols) > 1 else "",
                ph_type=cols[4] if len(cols) > 4 else "",   # "kind"
                target=cols[2] if len(cols) > 2 else "",    # "trigger_condition"
                status=cols[3].lower() if len(cols) > 3 else "pending",
                note=cols[5] if len(cols) > 5 else "",
            ))
            continue
        # Legacy layout: PH-001 | file | type | target | status | note
        if re.match(r"PH-\d+", ph_id, re.IGNORECASE):
            if len(cols) < 5:
                continue
            entries.append(PlaceholderEntry(
                ph_id=ph_id,
                file=cols[1] if len(cols) > 1 else "",
                ph_type=cols[2] if len(cols) > 2 else "",
                target=cols[3] if len(cols) > 3 else "",
                status=cols[4].lower() if len(cols) > 4 else "pending",
                note=cols[5] if len(cols) > 5 else "",
            ))
    return entries


# ── page spec reader ────────────────────────────────────────────────────────


def read_page_spec_components(spec_dir: Path | None, page_id: str) -> list[str]:
    """Extract component names from spec/baseline/ui/page_NNNN*.md.

    Accepts both bare `page_0001.md` and named `page_0001_MainActivity.md` (the
    layout a2h-spec actually emits)."""
    if spec_dir is None:
        return []
    num = re.sub(r"\D", "", page_id).zfill(4)
    ui_dir = spec_dir / "baseline" / "ui"
    if not ui_dir.exists():
        return []
    page_spec = ui_dir / f"page_{num}.md"
    if not page_spec.exists():
        matches = sorted(ui_dir.glob(f"page_{num}_*.md"))
        if not matches:
            return []
        page_spec = matches[0]
    text = page_spec.read_text(encoding="utf-8", errors="ignore")
    components = re.findall(r"(?:^|\s)((?:Text|Image|Button|Column|Row|List|Grid|Tabs|"
                            r"Stack|Flex|Scroll|Navigation|NavDestination|TabContent|"
                            r"Swiper|Search|TextInput|Toggle|Slider|Progress|Badge|"
                            r"Divider|Blank|LoadingProgress|Refresh|WaterFlow|"
                            r"RelativeContainer|GridRow|GridCol)\b)", text)
    return list(set(components))


# ── shared helpers ──────────────────────────────────────────────────────────


_TABLE_SEP = re.compile(r"^\s*\|[\s:|-]+\|\s*$")


def _extract_table_rows(text: str) -> list[str]:
    """Extract data rows from markdown tables (skip header + separator)."""
    lines = text.splitlines()
    rows: list[str] = []
    in_table = False
    header_seen = False
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("|"):
            if in_table:
                in_table = False
                header_seen = False
            continue
        in_table = True
        if _TABLE_SEP.match(stripped):
            header_seen = True
            continue
        if not header_seen:
            continue
        rows.append(stripped)
    return rows


# ── L1 domain feature list ────────────────────────────────────────────────


def parse_l1_features(spec_dir: Path | None) -> list[FeatureEntry]:
    """Parse L1-domain.md D4 功能清单 table."""
    if spec_dir is None:
        return []
    l1 = spec_dir / "baseline" / "L1-domain.md"
    if not l1.exists():
        return []
    text = l1.read_text(encoding="utf-8", errors="ignore")
    section = _extract_section(text, "D4")
    if not section:
        return []
    rows = _extract_table_rows(section)
    features: list[FeatureEntry] = []
    for row in rows:
        cols = [c.strip() for c in row.split("|")]
        cols = [c for c in cols if c]
        if len(cols) < 4:
            continue
        fid = cols[0]
        if not re.match(r"F-\d+", fid, re.IGNORECASE):
            continue
        priority = _norm_priority(cols[2]) if len(cols) > 2 else "P2"
        version = "V2" if priority == "P2" else "V1"
        features.append(FeatureEntry(
            feature_id=fid,
            name=cols[1] if len(cols) > 1 else "",
            version=version,
            extras={"module": cols[3]} if len(cols) > 3 else {},
        ))
    return features


# ── L2 migration feature phasing ──────────────────────────────────────────


def parse_l2_feature_phasing(spec_dir: Path | None) -> list[FeatureEntry]:
    """Parse L2-migration.md SS6 功能分期 table."""
    if spec_dir is None:
        return []
    l2 = spec_dir / "baseline" / "L2-migration.md"
    if not l2.exists():
        return []
    text = l2.read_text(encoding="utf-8", errors="ignore")
    section = _extract_section(text, "SS6")
    if not section:
        return []
    rows = _extract_table_rows(section)
    features: list[FeatureEntry] = []
    for row in rows:
        cols = [c.strip() for c in row.split("|")]
        cols = [c for c in cols if c]
        if len(cols) < 4:
            continue
        fid = cols[0]
        if not re.match(r"F-\d+", fid, re.IGNORECASE):
            continue
        version = cols[3].upper() if len(cols) > 3 else "V1"
        if version not in ("V1", "V2", "SKIP"):
            version = "V1"
        features.append(FeatureEntry(
            feature_id=fid,
            name=cols[1] if len(cols) > 1 else "",
            version=version,
            extras={"migration_type": cols[4]} if len(cols) > 4 else {},
        ))
    return features


# ── source-mapping.md page mapping ────────────────────────────────────────


def parse_source_mapping(spec_dir: Path | None) -> list[PageEntry]:
    """Parse page-level Android→ArkTS mappings.

    Supports two source-mapping.md table formats:
      Gallery:   col[0]=ArkTS page name, col[3+] contains Android .kt class
      Opencalc:  col[0]=page_NNN_Name (page_ID), col[1]=FQCN, col[2]=HarmonyOS .ets filename
    Fallback: spec/baseline/ui/page_XXXX_*.md files (single-page-file style).
    """
    if spec_dir is None:
        return []
    sm = spec_dir / "baseline" / "source-mapping.md"
    if sm.exists():
        text = sm.read_text(encoding="utf-8", errors="ignore")
        section = _extract_section(text, "页面级映射")
        if not section:
            # Opencalc-style heading
            section = _extract_section(text, "Page ID")
        if not section:
            section = text
        rows = _extract_table_rows(section)
        pages: list[PageEntry] = []
        seen_ids: set[str] = set()
        for idx, row in enumerate(rows, start=1):
            cols = [c.strip().strip("`").strip() for c in row.split("|")]
            cols = [c for c in cols if c]
            if len(cols) < 2:
                continue
            col0 = cols[0]

            if re.match(r"page_\d+_", col0):
                # Opencalc format: col[0]=page_ID, col[1]=FQCN, col[2]=HarmonyOS .ets
                if len(cols) < 3:
                    continue
                harmonyos_page = cols[2]
                if not harmonyos_page.endswith(".ets"):
                    continue
                pid_m = re.match(r"page_(\d+)_", col0)
                page_id = f"page_{pid_m.group(1)}" if pid_m else f"page_{idx:03d}"
                if page_id in seen_ids:
                    continue
                seen_ids.add(page_id)
                fqcn = cols[1]
                android_name = fqcn.split(".")[-1]
                ets_stem = harmonyos_page[:-4]  # strip ".ets"
                pages.append(PageEntry(
                    page_id=page_id,
                    name=ets_stem,
                    priority="P0",
                    status="pending",
                    confidence="medium",
                    target_file=f"pages/{harmonyos_page}",
                    extras={"android_class": f"{android_name}.kt"},
                ))
            else:
                # Gallery format: col[0]=ArkTS page name, search rest for .kt/.java
                arkts_name = col0
                if not arkts_name or arkts_name.lower().startswith("arkts"):
                    continue
                android_class = ""
                for c in cols[1:]:
                    if c.endswith(".kt") or c.endswith(".java"):
                        android_class = c
                        break
                pages.append(PageEntry(
                    page_id=f"page_{idx:03d}",
                    name=arkts_name,
                    priority="P0",
                    status="pending",
                    confidence="medium",
                    target_file=f"pages/{arkts_name}.ets",
                    extras={"android_class": android_class} if android_class else {},
                ))
        return pages

    # Fallback: parse individual ui/page_XXXX_*.md files
    return _parse_ui_page_files(spec_dir)


# Patterns for extracting fields from ui/page_*.md files.
# ArkTS output handles both plain and bold markdown:
#   plain:  "ArkTS 输出: `entry/.../XxxPage.ets`"
#   bold:   "**ArkTS 输出**: `entry/.../XxxPage.ets`"
#   WordPress: "输出文件: `entry/.../XxxPage.ets`"
_RE_ARKTS_OUTPUT = re.compile(
    r"(?:ArkTS\s*输出|输出文件)[*`\s]*[:：]\s*[*`\s]*(entry/[^\s`）\)（(]+\.ets)",
    re.IGNORECASE,
)
# Android source path: "主源码: `path/to/XxxActivity.java`"  (WordPress)
_RE_ANDROID_SRC = re.compile(
    r"主源码[*`\s]*[:：]\s*[*`\s]*([^\s`\)）]+\.(?:kt|java))",
    re.IGNORECASE,
)


def _parse_ui_page_files(spec_dir: Path) -> list[PageEntry]:
    """Parse spec/baseline/ui/page_XXXX_*.md as source-mapping fallback."""
    ui_dir = spec_dir / "baseline" / "ui"
    if not ui_dir.exists():
        return []

    pages: list[PageEntry] = []
    for md_file in sorted(ui_dir.glob("page_*.md")):
        # Extract Android class name from filename: page_0002_HomeActivity.md → HomeActivity
        stem_parts = md_file.stem.split("_", 2)  # ["page", "0002", "HomeActivity"]
        page_id = "_".join(stem_parts[:2])        # "page_0002"
        android_name = stem_parts[2] if len(stem_parts) > 2 else ""
        # Infer android_class basename (Activity name → filename, try .kt then .java)
        android_class = f"{android_name}.kt" if android_name else ""

        try:
            text = md_file.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue

        # Extract ArkTS output path
        m = _RE_ARKTS_OUTPUT.search(text)
        if not m:
            continue
        ets_full = m.group(1).strip()  # e.g. "entry/src/main/ets/pages/HomePage.ets"
        # Normalize to relative path from ets root: "pages/HomePage.ets"
        ets_rel = re.sub(r"^entry/src/main/ets/", "", ets_full)
        arkts_name = Path(ets_rel).stem  # "HomePage"

        # Override android_class from 主源码 if available (gives full path)
        m2 = _RE_ANDROID_SRC.search(text)
        if m2:
            android_class = m2.group(1).strip()

        pages.append(PageEntry(
            page_id=page_id,
            name=arkts_name,
            priority="P0",
            status="pending",
            confidence="medium",
            target_file=ets_rel,
            extras={"android_class": android_class} if android_class else {},
        ))
    return pages


# ── ui-graph JSON parsers ─────────────────────────────────────────────────


def parse_navigation_graph(spec_dir: Path | None) -> dict[str, Any]:
    """Parse ui-graph/Navigation_Edge.json."""
    if spec_dir is None:
        return {"edges": [], "parent_child_nav": {}}
    nav_file = spec_dir / "baseline" / "ui-graph" / "Navigation_Edge.json"
    if not nav_file.exists():
        return {"edges": [], "parent_child_nav": {}}
    try:
        data = json.loads(nav_file.read_text(encoding="utf-8", errors="ignore"))
    except (json.JSONDecodeError, OSError):
        return {"edges": [], "parent_child_nav": {}}
    # Two exporter shapes exist: a dict wrapper ({"version":.., "edges":[..]})
    # and a bare top-level array of edges. Tolerate both — a list IS the edges.
    if isinstance(data, list):
        return {"edges": data, "parent_child_nav": {}}
    if not isinstance(data, dict):
        return {"edges": [], "parent_child_nav": {}}
    return {
        "edges": data.get("edges", []),
        "parent_child_nav": data.get("parent_child_nav", {}),
    }


def parse_screen_graph(spec_dir: Path | None) -> list[dict[str, Any]]:
    """Parse ui-graph/Screen.json, return screen list."""
    if spec_dir is None:
        return []
    screen_file = spec_dir / "baseline" / "ui-graph" / "Screen.json"
    if not screen_file.exists():
        return []
    try:
        data = json.loads(screen_file.read_text(encoding="utf-8", errors="ignore"))
    except (json.JSONDecodeError, OSError):
        return []
    # Two exporter shapes: a dict wrapper ({"version":.., "screens":[..]}) and a
    # bare top-level array of screens. Tolerate both — a list IS the screens.
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    return data.get("screens", [])  # type: ignore[no-any-return]


# ── L2 SS10 acceptance matrix ─────────────────────────────────────────────


def parse_acceptance_matrix(spec_dir: Path | None) -> list[dict[str, str]]:
    """Parse L2-migration.md SS10 验收矩阵 V1 table."""
    if spec_dir is None:
        return []
    l2 = spec_dir / "baseline" / "L2-migration.md"
    if not l2.exists():
        return []
    text = l2.read_text(encoding="utf-8", errors="ignore")
    section = _extract_section(text, "SS10")
    if not section:
        return []
    rows = _extract_table_rows(section)
    entries: list[dict[str, str]] = []
    for row in rows:
        cols = [c.strip() for c in row.split("|")]
        cols = [c for c in cols if c]
        if len(cols) < 3:
            continue
        feature_name = cols[0]
        fid_match = re.match(r"(F-\d+)", feature_name)
        entries.append({
            "feature_id": fid_match.group(1) if fid_match else "",
            "feature_name": feature_name,
            "steps": cols[1] if len(cols) > 1 else "",
            "pass_criteria": cols[2] if len(cols) > 2 else "",
            "boundary": cols[3] if len(cols) > 3 else "",
        })
    return entries


# ── section extraction helper ─────────────────────────────────────────────


def _extract_section(text: str, heading_key: str) -> str:
    """Extract text from a ## heading containing heading_key to the next ## heading."""
    pattern = rf"^##\s+.*{re.escape(heading_key)}.*$"
    match = re.search(pattern, text, re.MULTILINE | re.IGNORECASE)
    if not match:
        return ""
    start = match.start()
    next_heading = re.search(r"^##\s+", text[match.end():], re.MULTILINE)
    end = match.end() + next_heading.start() if next_heading else len(text)
    return text[start:end]
