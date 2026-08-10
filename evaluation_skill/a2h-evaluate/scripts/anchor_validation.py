from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path


ANCHOR_RE = re.compile(r"^(?P<path>.+):(?P<start>[1-9]\d*)(?:-(?P<end>[1-9]\d*))?$")


@lru_cache(maxsize=None)
def _line_count(path: Path) -> int:
    with path.open("rb") as handle:
        return sum(1 for _ in handle)


def validate_anchor_field(
    value: str,
    repo: Path,
    field: str,
    row_number: int,
    *,
    allow_not_found: bool,
) -> list[str]:
    label = f"row {row_number} {field}"
    if not value:
        return [f"{label}: value is blank"]
    if value == "NOT_FOUND":
        return [] if allow_not_found else [f"{label}: NOT_FOUND is not allowed"]
    if "NOT_FOUND" in value:
        return [f"{label}: NOT_FOUND cannot be mixed with source anchors"]

    errors: list[str] = []
    anchors = [anchor.strip() for anchor in value.split(";")]
    if value != "; ".join(anchors):
        errors.append(f"{label}: multiple anchors must be separated by '; '")

    repo = repo.resolve()
    for anchor in anchors:
        match = ANCHOR_RE.fullmatch(anchor)
        if not match:
            errors.append(f"{label}: invalid anchor syntax {anchor!r}")
            continue
        relative = Path(match.group("path"))
        if relative.is_absolute():
            errors.append(f"{label}: absolute paths are not allowed: {relative}")
            continue
        if ".." in relative.parts:
            errors.append(f"{label}: parent traversal is not allowed: {relative}")
            continue
        resolved = (repo / relative).resolve()
        try:
            resolved.relative_to(repo)
        except ValueError:
            errors.append(f"{label}: anchor is outside the repository: {relative}")
            continue
        if not resolved.is_file():
            errors.append(f"{label}: file does not exist: {relative}")
            continue
        start = int(match.group("start"))
        end = int(match.group("end") or start)
        if end < start:
            errors.append(f"{label}: line range ends before it starts: {anchor}")
            continue
        line_count = _line_count(resolved)
        if end > line_count:
            errors.append(f"{label}: line {end} exceeds {relative} line count {line_count}")
    return errors
