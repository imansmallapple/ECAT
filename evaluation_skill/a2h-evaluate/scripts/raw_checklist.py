#!/usr/bin/env python3
from __future__ import annotations

import csv
import hashlib
import re
from pathlib import Path
from typing import Any


NORMALIZER_VERSION = "raw-checklist/2"


class RawChecklistError(ValueError):
    pass


ALIASES = {
    "feature_id": {"id", "feature_id", "featureid", "功能id", "功能编号"},
    "feature_name": {"feature_name", "featurename", "name", "功能名", "功能名称"},
    "feature_path": {"feature_path", "featurepath", "hierarchy_path", "hierarchypath", "path", "层级路径", "功能路径"},
    "source_hint": {
        "source_hint",
        "sourcehint",
        "source_location",
        "sourcelocation",
        "source_path",
        "sourcepath",
        "源码位置",
        "源代码位置",
    },
    "control_hint": {
        "control_hint",
        "controlhint",
        "control_identifier",
        "controlidentifier",
        "control_id",
        "controlid",
        "控件标识符",
        "控件id",
    },
    "interaction_type": {"interaction_type", "interactiontype", "交互类型"},
    "priority": {"priority", "优先级"},
    "depth": {"depth", "深度"},
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _header_key(value: Any) -> str:
    return re.sub(r"[\s_\-]+", "", _text(value).lower())


def _is_hierarchy_header(value: Any) -> bool:
    text = _text(value)
    key = _header_key(value)
    return bool(re.fullmatch(r"[一二三四五六七八九十百0-9]+级模块", text)) or bool(
        re.fullmatch(r"(?:level|module)[0-9]+", key)
        or re.fullmatch(r"level[0-9]+module", key)
    )


def _normalize_path(value: str) -> str:
    parts = [part.strip() for part in re.split(r"\s*>\s*|\s*/\s*", value) if part.strip()]
    return " > ".join(parts)


def _map_headers(headers: list[Any], source: str) -> tuple[dict[str, int], list[int]]:
    mapped: dict[str, int] = {}
    hierarchy: list[int] = []
    for index, header in enumerate(headers):
        if _is_hierarchy_header(header):
            hierarchy.append(index)
            continue
        key = _header_key(header)
        for role, aliases in ALIASES.items():
            if key not in {_header_key(alias) for alias in aliases}:
                continue
            if role in mapped:
                raise RawChecklistError(f"{source}: ambiguous header mapping for {role}")
            mapped[role] = index
            break
    if "feature_path" not in mapped and not hierarchy:
        raise RawChecklistError(f"{source}: feature path or hierarchy columns are required")
    return mapped, hierarchy


def _sheet_rows_xlsx(path: Path) -> list[tuple[str, list[Any], list[tuple[int, list[Any], bool]]]]:
    from openpyxl import load_workbook

    workbook = load_workbook(path, read_only=False, data_only=True)
    result = []
    for sheet in workbook.worksheets:
        if sheet.merged_cells.ranges:
            raise RawChecklistError(f"{path.name}/{sheet.title}: merged cells are not allowed")
        headers = [cell.value for cell in sheet[1]]
        rows: list[tuple[int, list[Any], bool]] = []
        for row_number in range(2, sheet.max_row + 1):
            values = [sheet.cell(row_number, column).value for column in range(1, sheet.max_column + 1)]
            if not any(_text(value) for value in values):
                continue
            rows.append((row_number, values, bool(sheet.row_dimensions[row_number].hidden)))
        if rows:
            result.append((sheet.title, headers, rows))
    return result


def _sheet_rows_delimited(path: Path) -> list[tuple[str, list[Any], list[tuple[int, list[Any], bool]]]]:
    delimiter = "\t" if path.suffix.lower() == ".tsv" else ","
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.reader(handle, delimiter=delimiter)
        rows = list(reader)
    if not rows:
        raise RawChecklistError(f"{path.name}: checklist is empty")
    body = [(index, row, False) for index, row in enumerate(rows[1:], start=2) if any(_text(value) for value in row)]
    return [(path.stem, rows[0], body)]


def normalize_raw_checklist(path: Path | str) -> dict[str, Any]:
    path = Path(path).resolve()
    if not path.is_file():
        raise RawChecklistError(f"raw checklist not found: {path}")
    suffix = path.suffix.lower()
    if suffix == ".xlsx":
        sheets = _sheet_rows_xlsx(path)
    elif suffix in {".csv", ".tsv"}:
        sheets = _sheet_rows_delimited(path)
    else:
        raise RawChecklistError(f"unsupported raw checklist format: {suffix}")
    if not sheets:
        raise RawChecklistError("raw checklist contains no feature rows")

    normalized: list[dict[str, Any]] = []
    sheet_names: list[str] = []
    for sheet_index, (sheet_name, headers, rows) in enumerate(sheets, start=1):
        sheet_names.append(sheet_name)
        mapped, hierarchy = _map_headers(headers, f"{path.name}/{sheet_name}")
        for row_number, values, hidden in rows:
            def value(role: str) -> str:
                index = mapped.get(role)
                return _text(values[index]) if index is not None and index < len(values) else ""

            hierarchy_values = [_text(values[index]) for index in hierarchy if index < len(values) and _text(values[index])]
            explicit_path = value("feature_path")
            feature_path = _normalize_path(explicit_path) if explicit_path else " > ".join(hierarchy_values)
            if not feature_path:
                raise RawChecklistError(f"{path.name}/{sheet_name}: empty feature_path at row {row_number}")
            feature_name = value("feature_name") or (hierarchy_values[-1] if hierarchy_values else feature_path.split(" > ")[-1])
            feature_id = value("feature_id") or f"S{sheet_index:02d}-R{row_number:06d}"
            normalized.append(
                {
                    "feature_id": feature_id,
                    "feature_name": feature_name,
                    "feature_path": feature_path,
                    "source_hint": value("source_hint"),
                    "control_hint": value("control_hint"),
                    "interaction_type": value("interaction_type"),
                    "priority": value("priority"),
                    "depth": value("depth"),
                    "raw_sheet": sheet_name,
                    "raw_row": row_number,
                    "_hidden": hidden,
                }
            )

    ids = [row["feature_id"] for row in normalized]
    duplicates = sorted({feature_id for feature_id in ids if ids.count(feature_id) > 1})
    if duplicates:
        raise RawChecklistError(f"duplicate feature_id values: {duplicates}")

    has_p0 = any(row["priority"] == "P0" for row in normalized)
    selected = [row for row in normalized if row["priority"] == "P0"] if has_p0 else normalized
    hidden_selected = [row["raw_row"] for row in selected if row.pop("_hidden")]
    for row in normalized:
        row.pop("_hidden", None)
    if hidden_selected:
        raise RawChecklistError(f"hidden feature row values selected for evaluation: {hidden_selected}")
    return {
        "normalizer_version": NORMALIZER_VERSION,
        "raw_path": str(path),
        "raw_sha256": sha256_file(path),
        "raw_row_count": len(normalized),
        "selected_row_count": len(selected),
        "selection_policy": "P0_only" if has_p0 else "all_rows_no_p0",
        "sheet_names": sheet_names,
        "rows": selected,
    }
