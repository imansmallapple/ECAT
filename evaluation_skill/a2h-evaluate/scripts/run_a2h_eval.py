#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from anchor_validation import validate_anchor_field
from node_align_cli import (
    NODE_SIMILARITY_THRESHOLD,
    resolve_alignment_runtime,
    resolve_zip,
    verify_zip_contents,
)
from raw_checklist import NORMALIZER_VERSION, RawChecklistError, normalize_raw_checklist


SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
SOURCE_PROMPT_PATH = SKILL_DIR / "references" / "source_reference.md"
TARGET_PROMPT_PATH = SKILL_DIR / "references" / "target_judge.md"
PROTOCOL = "a2h-evaluate/4"

REFERENCE_COLUMNS = [
    "feature_id",
    "feature_path",
    "priority",
    "raw_sheet",
    "raw_row",
    "android_anchor",
    "reference_behavior",
    "source_basis",
]
EVIDENCE_COLUMNS = ["feature_id", "status", "target_anchor", "rationale"]
STATUSES = {"FULL": 1.0, "PARTIAL": 0.5, "MISSING": 0.0}
SOURCE_BASES = {"EXACT", "BOUNDARY"}

SOURCE_STATE_FILE = "state.json"
REFERENCE_PARTIAL_FILE = "reference.partial.csv"
REFERENCE_FILE = "reference.csv"
TARGET_STATE_FILE = "state.json"
EVIDENCE_FILE = "evidence.csv"

HASH_EXCLUDED_PARTS = {
    ".cxx",
    ".ecat",
    ".git",
    ".gradle",
    ".hvigor",
    ".idea",
    "build",
    "dist",
    "hvigor",
    "node_modules",
    "oh_modules",
    "out",
    "target",
}


class RunError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_tree(root: Path) -> str:
    root = root.resolve()
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root)
        if any(part in HASH_EXCLUDED_PARTS for part in relative.parts):
            continue
        relative_bytes = relative.as_posix().encode("utf-8")
        if path.is_symlink():
            digest.update(b"L\0" + relative_bytes + b"\0" + os.readlink(path).encode("utf-8"))
        elif path.is_file():
            digest.update(b"F\0" + relative_bytes + b"\0")
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
    return digest.hexdigest()


def canonical_hash(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def skill_sha256() -> str:
    paths = [SKILL_DIR / "SKILL.md"]
    paths.extend(sorted((SKILL_DIR / "scripts").glob("*.py")))
    paths.extend(sorted((SKILL_DIR / "references").glob("*.md")))
    paths.append(SKILL_DIR / "assets" / "node_align_tool.checksum")
    digest = hashlib.sha256()
    for path in paths:
        if not path.is_file():
            raise RunError(f"skill component missing: {path}")
        relative = path.relative_to(SKILL_DIR).as_posix().encode("utf-8")
        digest.update(relative + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def bundled_zip_sha256() -> str:
    asset = SKILL_DIR / "assets" / "node_align_tool.zip"
    manifest = SKILL_DIR / "assets" / "node_align_tool.checksum"
    if not asset.is_file() or not manifest.is_file():
        raise RunError("bundled Node Align ZIP or checksum file is missing")
    expected = manifest.read_text(encoding="utf-8").split()[0].strip()
    if len(expected) != 64:
        raise RunError("invalid Node Align checksum file")
    actual = sha256_file(asset)
    if actual != expected:
        raise RunError(f"node_align_tool.zip SHA256 mismatch: expected {expected}, got {actual}")
    return actual


def read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RunError(f"cannot read {path}: {exc}") from exc


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    atomic_write_text(path, json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def atomic_write_compact_json(path: Path, value: dict[str, Any]) -> None:
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")


def atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    with source.open("rb") as reader, temporary.open("wb") as writer:
        shutil.copyfileobj(reader, writer)
    os.replace(temporary, destination)


def read_csv_exact(path: Path, columns: list[str], *, missing_ok: bool = False) -> list[dict[str, str]]:
    if not path.is_file():
        if missing_ok:
            return []
        raise RunError(f"required file does not exist: {path}")
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != columns:
            raise RunError(f"{path.name} columns must be exactly {columns}; got {reader.fieldnames}")
        return [{column: row.get(column) or "" for column in columns} for row in reader]


def atomic_write_csv(path: Path, columns: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows([{column: row.get(column, "") for column in columns} for row in rows])
    os.replace(temporary, path)


def source_reference_key(fingerprint: dict[str, Any]) -> str:
    if any("harmony" in key.lower() for key in fingerprint):
        raise RunError("source-reference fingerprint must not contain Harmony fields")
    return canonical_hash(fingerprint)


def _source_fingerprint(android: Path, normalized: dict[str, Any], model: str) -> dict[str, Any]:
    return {
        "android_sha256": sha256_tree(android),
        "raw_sha256": normalized["raw_sha256"],
        "normalizer_version": normalized["normalizer_version"],
        "selection_policy": normalized["selection_policy"],
        "selected_row_count": normalized["selected_row_count"],
        "model": model,
        "source_prompt_sha256": sha256_file(SOURCE_PROMPT_PATH),
        "protocol": PROTOCOL,
        "skill_sha256": skill_sha256(),
    }


def _target_fingerprint(harmony: Path, source_state: dict[str, Any], model: str) -> dict[str, Any]:
    return {
        "harmony_sha256": sha256_tree(harmony),
        "source_reference_sha256": source_state["reference_sha256"],
        "model": model,
        "target_prompt_sha256": sha256_file(TARGET_PROMPT_PATH),
        "protocol": PROTOCOL,
        "skill_sha256": skill_sha256(),
        "node_align_zip_sha256": bundled_zip_sha256(),
    }


def _validate_model(model: str) -> str:
    model = (model or "").strip()
    if not model or model == "NA":
        raise RunError("resolved evaluator model is required")
    locked = os.environ.get("A2H_EVALUATOR_MODEL_LOCK", "").strip()
    if locked and model != locked:
        raise RunError(f"evaluator model violates launcher lock: expected {locked}, got {model}")
    return model


def _current_model_matches(expected: str) -> None:
    current = _validate_model(os.environ.get("A2H_EVALUATOR_MODEL", ""))
    if current != expected:
        raise RunError(f"evaluator model changed: expected {expected}, got {current}")


def _verify_source_state(state: dict[str, Any], *, rehash_repositories: bool = True) -> None:
    _current_model_matches(state["evaluator_model"])
    android = Path(state["android_repo"])
    raw = Path(state["raw_checklist"])
    checks = {
        "source_prompt_sha256": sha256_file(SOURCE_PROMPT_PATH),
        "skill_sha256": skill_sha256(),
    }
    if rehash_repositories:
        checks = {
            "android_sha256": sha256_tree(android),
            "raw_sha256": sha256_file(raw),
            **checks,
        }
    for name, actual in checks.items():
        expected = state["fingerprint"][name]
        if actual != expected:
            raise RunError(f"{name} changed: expected {expected}, got {actual}")


def _verify_target_state(state: dict[str, Any], *, rehash_repositories: bool = True) -> None:
    _current_model_matches(state["evaluator_model"])
    checks = {
        "source_reference_sha256": sha256_file(Path(state["source_reference_path"])),
        "target_prompt_sha256": sha256_file(TARGET_PROMPT_PATH),
        "skill_sha256": skill_sha256(),
        "node_align_zip_sha256": bundled_zip_sha256(),
    }
    if rehash_repositories:
        checks = {
            "android_sha256": sha256_tree(Path(state["android_repo"])),
            "raw_sha256": sha256_file(Path(state["raw_checklist"])),
            "harmony_sha256": sha256_tree(Path(state["harmony_repo"])),
            **checks,
        }
    expected_values = {**state["source_fingerprint"], **state["target_fingerprint"]}
    for name, actual in checks.items():
        expected = expected_values[name]
        if actual != expected:
            raise RunError(f"{name} changed: expected {expected}, got {actual}")


def _reference_rows(source_state: dict[str, Any]) -> list[dict[str, str]]:
    path = Path(source_state["reference_path"] if source_state.get("complete") else source_state["partial_path"])
    return read_csv_exact(path, REFERENCE_COLUMNS, missing_ok=not source_state.get("complete"))


def _validate_named_numeric_values(feature_path: str, reference_behavior: str) -> None:
    path_text = unicodedata.normalize("NFKC", feature_path)
    behavior_text = unicodedata.normalize("NFKC", reference_behavior)
    named = set(re.findall(r"(?<!\d)\d+(?:\.\d+)?(?!\d)", path_text))
    present = set(re.findall(r"(?<!\d)\d+(?:\.\d+)?(?!\d)", behavior_text))
    missing = sorted(named - present)
    if missing:
        raise RunError(f"reference_behavior omitted named numeric value(s): {', '.join(missing)}")


def _validate_reference_prefix(source_state: dict[str, Any]) -> list[dict[str, str]]:
    rows = _reference_rows(source_state)
    expected = source_state["selected_rows"]
    actual_ids = [row["feature_id"] for row in rows]
    expected_ids = [row["feature_id"] for row in expected[: len(rows)]]
    if actual_ids != expected_ids:
        raise RunError("source-reference rows are not an exact ordered prefix")
    android = Path(source_state["android_repo"])
    for index, row in enumerate(rows):
        raw = expected[index]
        for key in ("feature_id", "feature_path", "priority", "raw_sheet"):
            if row[key] != str(raw[key]):
                raise RunError(f"source-reference row {index + 2} changed frozen raw field {key}")
        if row["raw_row"] != str(raw["raw_row"]):
            raise RunError(f"source-reference row {index + 2} changed frozen raw field raw_row")
        basis = row["source_basis"]
        if basis not in SOURCE_BASES:
            raise RunError(f"source-reference row {index + 2} has invalid source_basis")
        anchor = row["android_anchor"]
        errors = validate_anchor_field(anchor, android, "android_anchor", index + 2, allow_not_found=False)
        if errors:
            raise RunError("; ".join(errors))
        if row["reference_behavior"].count(" -> ") != 2 or "\n" in row["reference_behavior"]:
            raise RunError(f"source-reference row {index + 2} has invalid reference_behavior")
        _validate_named_numeric_values(row["feature_path"], row["reference_behavior"])
    return rows


def _source_basis_counts(rows: list[dict[str, str]]) -> dict[str, int]:
    return {basis: sum(row["source_basis"] == basis for row in rows) for basis in ("EXACT", "BOUNDARY")}


def _validate_evidence_prefix(target_state: dict[str, Any]) -> list[dict[str, str]]:
    evidence_path = Path(target_state["workdir"]) / EVIDENCE_FILE
    rows = read_csv_exact(evidence_path, EVIDENCE_COLUMNS, missing_ok=True)
    references = read_csv_exact(Path(target_state["source_reference_path"]), REFERENCE_COLUMNS)
    actual_ids = [row["feature_id"] for row in rows]
    expected_ids = [row["feature_id"] for row in references[: len(rows)]]
    if actual_ids != expected_ids:
        raise RunError("target evidence rows are not an exact ordered prefix")
    harmony = Path(target_state["harmony_repo"])
    for index, row in enumerate(rows, start=2):
        if any(value != value.strip() for value in row.values()):
            raise RunError(f"evidence row {index} has leading or trailing whitespace")
        status = row["status"]
        if status not in STATUSES:
            raise RunError(f"evidence row {index} has invalid status {status}")
        anchor = row["target_anchor"]
        errors = validate_anchor_field(anchor, harmony, "target_anchor", index, allow_not_found=True)
        if errors:
            raise RunError("; ".join(errors))
        if status in {"FULL", "PARTIAL"} and anchor == "NOT_FOUND":
            raise RunError(f"evidence row {index}: {status} requires concrete target evidence")
        if status == "MISSING" and anchor != "NOT_FOUND":
            raise RunError(f"evidence row {index}: MISSING requires target_anchor=NOT_FOUND")
        rationale = row["rationale"]
        if "\n" in rationale or "\r" in rationale or len(rationale) > 120:
            raise RunError(f"evidence row {index} rationale must be one line of at most 120 Unicode characters")
        if status != "FULL" and not rationale:
            raise RunError(f"evidence row {index} rationale is required for {status}")
    return rows


def _source_status(source_state: dict[str, Any]) -> dict[str, Any]:
    rows = _validate_reference_prefix(source_state)
    total = len(source_state["selected_rows"])
    next_row = source_state["selected_rows"][len(rows)] if len(rows) < total else None
    if source_state.get("complete"):
        phase = "SOURCE_REFERENCE_COMPLETE"
    elif len(rows) == total:
        phase = "SOURCE_AUDIT"
    else:
        phase = "SOURCE_REFERENCE"
    return {
        "run_dir": source_state["workdir"],
        "phase": phase,
        "completed": len(rows),
        "total": total,
        "next": next_row,
        "session_count": source_state["session_count"],
        "source_basis_counts": _source_basis_counts(rows),
        "source_reference_path": source_state.get("reference_path"),
        "source_reference_sha256": source_state.get("reference_sha256"),
    }


def seal_reference(run_dir: Path | str) -> dict[str, Any]:
    run_dir = Path(run_dir).resolve()
    state_path = run_dir / SOURCE_STATE_FILE
    source_state = read_json(state_path)
    if source_state.get("kind") != "source_reference" or source_state.get("protocol") != PROTOCOL:
        raise RunError("run directory is not a compatible source-reference state")
    if source_state.get("complete"):
        return _source_status(source_state)
    _verify_source_state(source_state, rehash_repositories=False)
    rows = _validate_reference_prefix(source_state)
    if len(rows) != len(source_state["selected_rows"]):
        raise RunError(f"source draft is incomplete: {len(rows)}/{len(source_state['selected_rows'])}")
    partial = Path(source_state["partial_path"])
    reference = Path(source_state["reference_path"])
    atomic_copy(partial, reference)
    source_state["complete"] = True
    source_state["source_basis_counts"] = _source_basis_counts(rows)
    source_state["reference_sha256"] = sha256_file(reference)
    source_state["sealed_at"] = utc_now()
    source_state["updated_at"] = utc_now()
    atomic_write_json(state_path, source_state)
    try:
        partial.unlink()
    except OSError:
        pass
    return _source_status(source_state)


def _target_status(target_state: dict[str, Any]) -> dict[str, Any]:
    rows = _validate_evidence_prefix(target_state)
    references = read_csv_exact(Path(target_state["source_reference_path"]), REFERENCE_COLUMNS)
    next_row = references[len(rows)] if len(rows) < len(references) else None
    return {
        "run_dir": target_state["workdir"],
        "phase": "TARGET_JUDGMENT",
        "completed": len(rows),
        "total": len(references),
        "next": next_row,
        "session_count": target_state["session_count"],
        "source_basis_counts": target_state["source_basis_counts"],
        "source_reference_path": target_state["source_reference_path"],
        "source_reference_sha256": target_state["source_reference_sha256"],
    }


def collect_stats(repo: Path, role: str) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="a2h-stats-") as tmp:
        output = Path(tmp) / "stats.json"
        try:
            completed = subprocess.run(
                [sys.executable, str(SCRIPT_DIR / "collect_code_stats.py"), "--repo", str(repo), "--role", role, "--out", str(output)],
                check=True,
                capture_output=True,
                text=True,
            )
            if completed.stderr.strip():
                raise RunError(f"code statistics wrote to stderr: {completed.stderr.strip()}")
            return json.loads(output.read_text(encoding="utf-8"))
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
            raise RunError(f"code statistics failed for {role}: {exc}") from exc


ALIGN_ALLOWED_KEYS = (
    "tool_status",
    "score_policy",
    "node_similarity_threshold",
    "align_score",
    "node_retention",
    "edge_retention",
    "path_retention",
    "structural_metrics_status",
    "aligned_pairs",
    "aligned_pair_count",
    "retention_denominator",
    "android_nodes",
    "android_edges",
    "harmony_nodes",
    "harmony_edges",
    "runtime",
)


def compact_alignment(value: dict[str, Any]) -> dict[str, Any]:
    compact = {key: value[key] for key in ALIGN_ALLOWED_KEYS if key in value}
    if "aligned_pair_count" not in compact and isinstance(compact.get("aligned_pairs"), int):
        compact["aligned_pair_count"] = compact.pop("aligned_pairs")
    return compact


def _unit_interval(value: Any, name: str, *, allow_none: bool = False) -> float | None:
    if allow_none and value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RunError(f"alignment {name} must be numeric")
    number = float(value)
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise RunError(f"alignment {name} must be finite and within [0, 1]")
    return number


def validate_alignment(value: dict[str, Any]) -> dict[str, Any]:
    align = compact_alignment(value)
    if align.get("tool_status") != "ok":
        raise RunError("alignment tool did not finish successfully")
    if align.get("score_policy") != "node_retention_only":
        raise RunError("alignment score policy is not node_retention_only")
    threshold = _unit_interval(
        align.get("node_similarity_threshold"),
        "node_similarity_threshold",
    )
    if not math.isclose(
        threshold,
        NODE_SIMILARITY_THRESHOLD,
        rel_tol=0.0,
        abs_tol=1e-12,
    ):
        raise RunError(
            "alignment node similarity threshold does not match the frozen protocol"
        )
    score = _unit_interval(align.get("align_score"), "align_score")
    retention = _unit_interval(align.get("node_retention"), "node_retention")
    if not math.isclose(score, retention, rel_tol=0.0, abs_tol=1e-12):
        raise RunError("alignment score is inconsistent with node retention")

    counts: dict[str, int] = {}
    for name in (
        "aligned_pair_count",
        "retention_denominator",
        "android_nodes",
        "android_edges",
        "harmony_nodes",
        "harmony_edges",
    ):
        item = align.get(name)
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            raise RunError(f"alignment {name} must be a non-negative integer")
        counts[name] = item
    if counts["retention_denominator"] == 0:
        raise RunError("alignment retention denominator must be positive")
    if counts["aligned_pair_count"] > min(counts["android_nodes"], counts["harmony_nodes"]):
        raise RunError("alignment pair count exceeds graph node counts")
    expected = counts["aligned_pair_count"] / counts["retention_denominator"]
    if not math.isclose(retention, expected, rel_tol=0.0, abs_tol=1e-12):
        raise RunError("alignment node retention is inconsistent with node counts")

    edge = _unit_interval(align.get("edge_retention"), "edge_retention", allow_none=True)
    path = _unit_interval(align.get("path_retention"), "path_retention", allow_none=True)
    if counts["harmony_edges"] == 0:
        if align.get("structural_metrics_status") != "unavailable_no_target_edges" or edge is not None or path is not None:
            raise RunError("alignment structural metrics are inconsistent with an edgeless target graph")
    elif align.get("structural_metrics_status") != "available" or edge is None or path is None:
        raise RunError("alignment structural metrics are incomplete")

    runtime = align.get("runtime")
    if not isinstance(runtime, dict):
        raise RunError("alignment runtime provenance is missing")
    for name in ("codegraph_version", "node_version"):
        if not isinstance(runtime.get(name), str) or not runtime[name].strip():
            raise RunError(f"alignment runtime {name} is missing")
    for name in ("codegraph_sha256", "node_sha256"):
        digest = runtime.get(name)
        if not isinstance(digest, str) or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise RunError(f"alignment runtime {name} is invalid")
    return align


def check_prerequisites(raw_checklist: Path | str) -> dict[str, str]:
    raw = Path(raw_checklist).resolve()
    if not raw.is_file():
        raise RunError(f"raw checklist not found: {raw}")
    suffix = raw.suffix.lower()
    if suffix not in {".xlsx", ".csv", ".tsv"}:
        raise RunError(f"unsupported raw checklist format: {suffix}")
    if suffix == ".xlsx" and importlib.util.find_spec("openpyxl") is None:
        raise RunError(".xlsx input requires openpyxl")
    bundled_zip_sha256()
    try:
        verify_zip_contents(resolve_zip())
        _, _, runtime = resolve_alignment_runtime()
    except RuntimeError as exc:
        raise RunError(str(exc)) from exc
    return runtime


def collect_alignment(android: Path, harmony: Path) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="a2h-align-") as tmp:
        output = Path(tmp)
        completed = subprocess.run(
            [sys.executable, str(SCRIPT_DIR / "node_align_cli.py"), "--android", str(android), "--harmony", str(harmony), "--out", str(output)],
            check=True,
            capture_output=True,
            text=True,
        )
        if completed.stderr.strip():
            raise RunError(f"Node Align wrote to stderr: {completed.stderr.strip()}")
        result = json.loads((output / "node_align.json").read_text(encoding="utf-8"))
        return compact_alignment(result)


StatsCollector = Callable[[Path, str], dict[str, Any]]
AlignCollector = Callable[[Path, Path], dict[str, Any]]


def start_evaluation(
    android: Path | str,
    harmony: Path | str,
    raw_checklist: Path | str,
    *,
    state_root: Path | str,
    evaluator_model: str,
    stats_collector: StatsCollector = collect_stats,
    align_collector: AlignCollector = collect_alignment,
) -> dict[str, Any]:
    android = Path(android).resolve()
    harmony = Path(harmony).resolve()
    raw = Path(raw_checklist).resolve()
    root = Path(state_root).resolve()
    model = _validate_model(evaluator_model)
    if not android.is_dir():
        raise RunError(f"ANDROID_REPO not found: {android}")
    if not harmony.is_dir():
        raise RunError(f"HARMONY_REPO not found: {harmony}")
    try:
        normalized = normalize_raw_checklist(raw)
    except RawChecklistError as exc:
        raise RunError(str(exc)) from exc

    source_fingerprint = _source_fingerprint(android, normalized, model)
    source_key = source_reference_key(source_fingerprint)
    source_dir = root / "references" / source_key
    source_state_path = source_dir / SOURCE_STATE_FILE
    source_dir.mkdir(parents=True, exist_ok=True)
    if source_state_path.exists():
        source_state = read_json(source_state_path)
        if source_state.get("fingerprint") != source_fingerprint:
            raise RunError("source-reference fingerprint mismatch")
        _verify_source_state(source_state)
        if not source_state.get("complete"):
            source_state["session_count"] += 1
            source_state["updated_at"] = utc_now()
            atomic_write_json(source_state_path, source_state)
    else:
        source_state = {
            "tool": "a2h-evaluate",
            "protocol": PROTOCOL,
            "kind": "source_reference",
            "workdir": str(source_dir),
            "android_repo": str(android),
            "raw_checklist": str(raw),
            "evaluator_model": model,
            "fingerprint": source_fingerprint,
            "source_key": source_key,
            "raw_metadata": {key: normalized[key] for key in ("normalizer_version", "raw_sha256", "raw_row_count", "selected_row_count", "selection_policy", "sheet_names")},
            "selected_rows": normalized["rows"],
            "partial_path": str(source_dir / REFERENCE_PARTIAL_FILE),
            "reference_path": str(source_dir / REFERENCE_FILE),
            "complete": False,
            "session_count": 1,
            "created_at": utc_now(),
            "updated_at": utc_now(),
        }
        atomic_write_json(source_state_path, source_state)

    if not source_state.get("complete"):
        return _source_status(source_state)
    reference_path = Path(source_state["reference_path"])
    if sha256_file(reference_path) != source_state["reference_sha256"]:
        raise RunError("frozen source reference SHA256 changed")

    target_fingerprint = _target_fingerprint(harmony, source_state, model)
    target_key = canonical_hash({"source_key": source_key, **target_fingerprint})
    target_dir = root / "runs" / target_key
    target_state_path = target_dir / TARGET_STATE_FILE
    target_dir.mkdir(parents=True, exist_ok=True)
    if target_state_path.exists():
        target_state = read_json(target_state_path)
        if target_state.get("target_fingerprint") != target_fingerprint:
            raise RunError("target fingerprint mismatch")
        _verify_target_state(target_state)
        target_state["session_count"] += 1
        target_state["updated_at"] = utc_now()
        atomic_write_json(target_state_path, target_state)
    else:
        stats = {
            "android": stats_collector(android, "android"),
            "harmony": stats_collector(harmony, "harmony"),
        }
        align = validate_alignment(align_collector(android, harmony))
        target_state = {
            "tool": "a2h-evaluate",
            "protocol": PROTOCOL,
            "kind": "target_judgment",
            "workdir": str(target_dir),
            "android_repo": str(android),
            "harmony_repo": str(harmony),
            "raw_checklist": str(raw),
            "evaluator_model": model,
            "source_key": source_key,
            "source_fingerprint": source_fingerprint,
            "target_fingerprint": target_fingerprint,
            "source_reference_path": str(reference_path),
            "source_reference_sha256": source_state["reference_sha256"],
            "source_basis_counts": source_state["source_basis_counts"],
            "raw_metadata": source_state["raw_metadata"],
            "source_session_count": source_state["session_count"],
            "session_count": 1,
            "stats": stats,
            "align": align,
            "created_at": utc_now(),
            "updated_at": utc_now(),
        }
        atomic_write_json(target_state_path, target_state)
    return _target_status(target_state)


def record_reference(
    run_dir: Path | str,
    feature_id: str,
    android_anchor: str,
    reference_behavior: str,
    source_basis: str,
) -> dict[str, Any]:
    run_dir = Path(run_dir).resolve()
    state_path = run_dir / SOURCE_STATE_FILE
    state = read_json(state_path)
    if state.get("kind") != "source_reference" or state.get("protocol") != PROTOCOL:
        raise RunError("run directory is not a compatible source-reference state")
    if state.get("complete"):
        raise RunError("source reference is already complete")
    _verify_source_state(state, rehash_repositories=False)
    rows = _validate_reference_prefix(state)
    expected = state["selected_rows"]
    if len(rows) >= len(expected):
        raise RunError("source reference is already complete")
    raw = expected[len(rows)]
    if feature_id != raw["feature_id"]:
        raise RunError(f"next source feature must be {raw['feature_id']}, got {feature_id}")
    if source_basis not in SOURCE_BASES:
        raise RunError(f"source_basis must be one of {sorted(SOURCE_BASES)}")
    if reference_behavior.count(" -> ") != 2 or "\n" in reference_behavior or "\r" in reference_behavior:
        raise RunError("reference_behavior must be one line with exactly two ' -> ' separators")
    _validate_named_numeric_values(raw["feature_path"], reference_behavior)
    errors = validate_anchor_field(android_anchor, Path(state["android_repo"]), "android_anchor", len(rows) + 2, allow_not_found=False)
    if errors:
        raise RunError("; ".join(errors))
    new_row = {
        "feature_id": raw["feature_id"],
        "feature_path": raw["feature_path"],
        "priority": raw["priority"],
        "raw_sheet": raw["raw_sheet"],
        "raw_row": str(raw["raw_row"]),
        "android_anchor": android_anchor,
        "reference_behavior": reference_behavior,
        "source_basis": source_basis,
    }
    partial = Path(state["partial_path"])
    atomic_write_csv(partial, REFERENCE_COLUMNS, [*rows, new_row])
    state["updated_at"] = utc_now()
    atomic_write_json(state_path, state)
    return _source_status(state)


def revise_reference(
    run_dir: Path | str,
    feature_id: str,
    android_anchor: str,
    reference_behavior: str,
    source_basis: str,
) -> dict[str, Any]:
    run_dir = Path(run_dir).resolve()
    state_path = run_dir / SOURCE_STATE_FILE
    state = read_json(state_path)
    if state.get("kind") != "source_reference" or state.get("protocol") != PROTOCOL:
        raise RunError("run directory is not a compatible source-reference state")
    if state.get("complete"):
        raise RunError("sealed source reference is immutable")
    _verify_source_state(state, rehash_repositories=False)
    rows = _validate_reference_prefix(state)
    positions = {row["feature_id"]: index for index, row in enumerate(rows)}
    if feature_id not in positions:
        raise RunError("only an existing source draft row can be revised")
    if source_basis not in SOURCE_BASES:
        raise RunError(f"source_basis must be one of {sorted(SOURCE_BASES)}")
    if reference_behavior.count(" -> ") != 2 or "\n" in reference_behavior or "\r" in reference_behavior:
        raise RunError("reference_behavior must be one line with exactly two ' -> ' separators")
    index = positions[feature_id]
    _validate_named_numeric_values(rows[index]["feature_path"], reference_behavior)
    errors = validate_anchor_field(android_anchor, Path(state["android_repo"]), "android_anchor", index + 2, allow_not_found=False)
    if errors:
        raise RunError("; ".join(errors))
    rows[index] = {
        **rows[index],
        "android_anchor": android_anchor,
        "reference_behavior": reference_behavior,
        "source_basis": source_basis,
    }
    atomic_write_csv(Path(state["partial_path"]), REFERENCE_COLUMNS, rows)
    state["updated_at"] = utc_now()
    state["revision_count"] = int(state.get("revision_count", 0)) + 1
    atomic_write_json(state_path, state)
    return _source_status(state)


def record_evidence(
    run_dir: Path | str,
    feature_id: str,
    status: str,
    target_anchor: str,
    rationale: str,
) -> dict[str, Any]:
    run_dir = Path(run_dir).resolve()
    state_path = run_dir / TARGET_STATE_FILE
    state = read_json(state_path)
    if state.get("kind") != "target_judgment" or state.get("protocol") != PROTOCOL:
        raise RunError("run directory is not a compatible target-judgment state")
    _verify_target_state(state, rehash_repositories=False)
    rows = _validate_evidence_prefix(state)
    references = read_csv_exact(Path(state["source_reference_path"]), REFERENCE_COLUMNS)
    if len(rows) >= len(references):
        raise RunError("target evidence is already complete")
    expected_id = references[len(rows)]["feature_id"]
    if feature_id != expected_id:
        raise RunError(f"next target feature must be {expected_id}, got {feature_id}")
    if status not in STATUSES:
        raise RunError(f"status must be one of {sorted(STATUSES)}")
    if "\n" in rationale or "\r" in rationale or len(rationale) > 120:
        raise RunError("rationale must be one line of at most 120 Unicode characters")
    if status != "FULL" and not rationale:
        raise RunError(f"rationale is required for {status}")
    errors = validate_anchor_field(target_anchor, Path(state["harmony_repo"]), "target_anchor", len(rows) + 2, allow_not_found=True)
    if errors:
        raise RunError("; ".join(errors))
    if status in {"FULL", "PARTIAL"} and target_anchor == "NOT_FOUND":
        raise RunError(f"{status} requires concrete target evidence")
    if status == "MISSING" and target_anchor != "NOT_FOUND":
        raise RunError("MISSING requires target_anchor=NOT_FOUND")
    new_row = {
        "feature_id": feature_id,
        "status": status,
        "target_anchor": target_anchor,
        "rationale": rationale,
    }
    evidence_path = run_dir / EVIDENCE_FILE
    atomic_write_csv(evidence_path, EVIDENCE_COLUMNS, [*rows, new_row])
    _validate_evidence_prefix(state)
    state["updated_at"] = utc_now()
    atomic_write_json(state_path, state)
    return _target_status(state)


def status_run(run_dir: Path | str) -> dict[str, Any]:
    run_dir = Path(run_dir).resolve()
    state = read_json(run_dir / "state.json")
    if state.get("kind") == "source_reference":
        _verify_source_state(state)
        return _source_status(state)
    if state.get("kind") == "target_judgment":
        _verify_target_state(state)
        return _target_status(state)
    raise RunError("unknown evaluation run state")


def _format_score(value: Any) -> str:
    return "NA" if value is None else f"{float(value):.6f}"


def _language_text(stats: dict[str, Any]) -> str:
    return "; ".join(
        f"{name}: {values['files']} files, {values['total_lines']} LOC"
        for name, values in sorted(stats.get("language_stats", {}).items())
    )


def _format_text(result: dict[str, Any]) -> str:
    agent = result["agent"]
    align = result["align"]
    android = result["stats"]["android"]
    harmony = result["stats"]["harmony"]
    raw = result["raw"]
    reproducibility = result["reproducibility"]
    runtime = align["runtime"]
    pair_count = align.get("aligned_pair_count", align.get("aligned_pairs", 0))
    numerator = agent["full_count"] + 0.5 * agent["partial_count"]
    edge = _format_score(align.get("edge_retention"))
    path = _format_score(align.get("path_retention"))
    selection_policy = {
        "P0_only": "P0 only",
        "all_rows_no_p0": "all rows",
    }.get(raw["selection_policy"], raw["selection_policy"])
    policy = harmony.get("stats_policy", {})
    policy_text = (
        f"included={policy.get('included_extensions', [])}; "
        f"excluded={policy.get('excluded_dirs', [])}; "
        f"symlinks={policy.get('symlinks', 'excluded')}; "
        f"lines={policy.get('line_count')}"
    )
    return "\n".join(
        [
            "A2H Evaluation",
            "",
            "Agent:",
            f"- Score: {numerator:.1f} / {agent['denominator']} = {_format_score(result['scores']['Agent'])}",
            f"- FULL/PARTIAL/MISSING: {agent['full_count']}/{agent['partial_count']}/{agent['missing_count']}",
            f"- Denominator: {agent['denominator']}",
            f"- Raw/Selected Rows: {raw['raw_row_count']}/{raw['selected_row_count']}",
            f"- Selection Policy: {selection_policy}",
            "",
            "Align:",
            f"- Score: {_format_score(result['scores']['Align'])}",
            f"- Aligned Nodes: {pair_count}",
            f"- Android/Harmony Nodes: {align.get('android_nodes', 'NA')}/{align.get('harmony_nodes', 'NA')}",
            f"- Retention Denominator: {align.get('retention_denominator', 'NA')}",
            f"- Edge/Path Retention: {edge}/{path}",
            "- Tool: bundled dual-CodeGraph node_align_tool.zip",
            "",
            "Android Source:",
            f"- Repository SHA256: {result['inputs']['sha256']['android_repository']}",
            f"- Source Files: {android['code_file_count']}",
            f"- LOC/Nonblank LOC: {android['code_total_lines']}/{android['code_nonblank_lines']}",
            f"- Languages: {_language_text(android)}",
            f"- Source Basis EXACT/BOUNDARY: {result['source_reference']['basis_counts']['EXACT']}/{result['source_reference']['basis_counts']['BOUNDARY']}",
            "",
            "HarmonyOS Source:",
            f"- Repository SHA256: {result['inputs']['sha256']['harmony_repository']}",
            f"- Source Files: {harmony['code_file_count']}",
            f"- LOC/Nonblank LOC: {harmony['code_total_lines']}/{harmony['code_nonblank_lines']}",
            f"- ETS Files: {harmony['ets_file_count']}",
            f"- ETS LOC/Nonblank LOC: {harmony['ets_total_lines']}/{harmony['ets_nonblank_lines']}",
            f"- Languages: {_language_text(harmony)}",
            "",
            "Reproducibility:",
            f"- Raw Checklist SHA256: {raw['raw_sha256']}",
            f"- Source Reference SHA256: {reproducibility['source_reference_sha256']}",
            f"- Evaluator Model: {reproducibility['evaluator_model']}",
            f"- Source/Target Prompt SHA256: {reproducibility['source_prompt_sha256']}/{reproducibility['target_prompt_sha256']}",
            f"- Protocol/Skill SHA256: {reproducibility['protocol_sha256']}/{reproducibility['skill_sha256']}",
            f"- Node Align ZIP SHA256: {reproducibility['node_align_zip_sha256']}",
            f"- Alignment Runtime: codegraph {runtime['codegraph_version']} ({runtime['codegraph_sha256']}); Node.js {runtime['node_version']} ({runtime['node_sha256']})",
            f"- Source/Target Resume Sessions: {reproducibility['source_session_count']}/{reproducibility['target_session_count']}",
            f"- Code Statistics Policy: {policy_text}",
            "",
            "Verification: OK",
            "",
        ]
    )


def finalize_run(run_dir: Path | str, *, save_result: Path | str | None = None) -> tuple[dict[str, Any], str]:
    run_dir = Path(run_dir).resolve()
    state = read_json(run_dir / TARGET_STATE_FILE)
    if state.get("kind") != "target_judgment" or state.get("protocol") != PROTOCOL:
        raise RunError("run directory is not a compatible target-judgment state")
    _verify_target_state(state)
    rows = _validate_evidence_prefix(state)
    references = read_csv_exact(Path(state["source_reference_path"]), REFERENCE_COLUMNS)
    if len(rows) != len(references):
        raise RunError(f"target evidence is incomplete: {len(rows)}/{len(references)}")
    align = validate_alignment(state.get("align", {}))
    counts = {status: sum(1 for row in rows if row["status"] == status) for status in STATUSES}
    denominator = len(rows)
    agent_score = (counts["FULL"] + 0.5 * counts["PARTIAL"]) / denominator
    align_score = float(align["align_score"])
    protocol_sha = hashlib.sha256(PROTOCOL.encode("utf-8")).hexdigest()
    result = {
        "schema_version": 1,
        "protocol": PROTOCOL,
        "scores": {"Agent": agent_score, "Align": align_score},
        "agent": {
            "full_count": counts["FULL"],
            "partial_count": counts["PARTIAL"],
            "missing_count": counts["MISSING"],
            "denominator": denominator,
            "score_formula": "(FULL + 0.5 * PARTIAL) / denominator",
        },
        "align": align,
        "source_reference": {"basis_counts": state["source_basis_counts"]},
        "raw": state["raw_metadata"],
        "inputs": {
            "android_repo": state["android_repo"],
            "harmony_repo": state["harmony_repo"],
            "raw_checklist": state["raw_checklist"],
            "sha256": {
                "android_repository": state["source_fingerprint"]["android_sha256"],
                "harmony_repository": state["target_fingerprint"]["harmony_sha256"],
                "raw_checklist": state["source_fingerprint"]["raw_sha256"],
            },
        },
        "stats": state["stats"],
        "judgments": rows,
        "reproducibility": {
            "source_reference_sha256": state["source_reference_sha256"],
            "evaluator_model": state["evaluator_model"],
            "source_prompt_sha256": state["source_fingerprint"]["source_prompt_sha256"],
            "target_prompt_sha256": state["target_fingerprint"]["target_prompt_sha256"],
            "protocol_sha256": protocol_sha,
            "skill_sha256": state["target_fingerprint"]["skill_sha256"],
            "node_align_zip_sha256": state["target_fingerprint"]["node_align_zip_sha256"],
            "source_session_count": state["source_session_count"],
            "target_session_count": state["session_count"],
        },
        "verification": {"ok": True, "errors": []},
    }
    report = _format_text(result)
    if save_result is not None:
        save_path = Path(save_result).resolve()
        protected_files = {Path(state["raw_checklist"]).resolve()}
        protected_dirs = {
            Path(state["android_repo"]).resolve(),
            Path(state["harmony_repo"]).resolve(),
            Path(state["source_reference_path"]).resolve().parent,
            run_dir,
            run_dir.parent.parent,
            SKILL_DIR.resolve(),
        }
        protected = save_path in protected_files or any(
            save_path == directory or directory in save_path.parents for directory in protected_dirs
        )
        if protected:
            raise RunError("--save-result points into a protected input, skill, or state path")
        atomic_write_compact_json(save_path, result)
    return result, report


def cleanup_finalized_run(run_dir: Path | str) -> None:
    run_dir = Path(run_dir).resolve()
    state = read_json(run_dir / TARGET_STATE_FILE)
    if state.get("kind") != "target_judgment" or state.get("protocol") != PROTOCOL:
        raise RunError("refusing to delete a non-compatible target run directory")
    rows = _validate_evidence_prefix(state)
    references = read_csv_exact(Path(state["source_reference_path"]), REFERENCE_COLUMNS)
    if len(rows) != len(references):
        raise RunError("refusing to delete incomplete target state")
    try:
        shutil.rmtree(run_dir)
    except OSError as exc:
        raise RunError(f"could not delete finalized target state: {exc}") from exc


def _print_status(value: dict[str, Any], *, as_json: bool = False) -> None:
    if as_json:
        print(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
        return
    print(f"Run: {value['run_dir']}")
    print(f"Phase: {value['phase']}")
    print(f"Progress: {value['completed']}/{value['total']}")
    print("Next: " + (json.dumps(value["next"], ensure_ascii=False, separators=(",", ":")) if value.get("next") else "NONE"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Source-grounded Android-to-HarmonyOS evaluation")
    commands = parser.add_subparsers(dest="command", required=True)

    start = commands.add_parser("start", help="Start or resume source grounding or target judgment")
    start.add_argument("--android", required=True)
    start.add_argument("--harmony", required=True)
    start.add_argument("--checklist", required=True)
    start.add_argument("--state-root")
    start.add_argument("--json", action="store_true")

    reference = commands.add_parser("record-reference", help="Append one verified Android source-reference row")
    reference.add_argument("--run", required=True)
    reference.add_argument("--feature-id", required=True)
    reference.add_argument("--android-anchor", required=True)
    reference.add_argument("--reference-behavior", required=True)
    reference.add_argument("--source-basis", required=True, choices=sorted(SOURCE_BASES))

    revise = commands.add_parser("revise-reference", help="Revise one source draft row before sealing")
    revise.add_argument("--run", required=True)
    revise.add_argument("--feature-id", required=True)
    revise.add_argument("--android-anchor", required=True)
    revise.add_argument("--reference-behavior", required=True)
    revise.add_argument("--source-basis", required=True, choices=sorted(SOURCE_BASES))

    seal = commands.add_parser("seal-reference", help="Audit-complete commit of the full source draft")
    seal.add_argument("--run", required=True)

    evidence = commands.add_parser("record-evidence", help="Append one verified HarmonyOS judgment")
    evidence.add_argument("--run", required=True)
    evidence.add_argument("--feature-id", required=True)
    evidence.add_argument("--status", required=True, choices=sorted(STATUSES))
    evidence.add_argument("--target-anchor", required=True)
    evidence.add_argument("--rationale", default="")

    status = commands.add_parser("status", help="Inspect resumable progress without modifying it")
    status.add_argument("--run", required=True)
    status.add_argument("--json", action="store_true")

    finalize = commands.add_parser("finalize", help="Verify, score, print, and optionally save one result JSON")
    finalize.add_argument("--run", required=True)
    finalize.add_argument("--save-result")
    return parser


def default_state_root() -> Path:
    configured = os.environ.get("A2H_EVAL_STATE_ROOT")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".local" / "state" / "a2h-evaluate"


def main() -> int:
    args = build_parser().parse_args()
    try:
        model = _validate_model(os.environ.get("A2H_EVALUATOR_MODEL", ""))
        if args.command == "start":
            check_prerequisites(args.checklist)
            value = start_evaluation(
                args.android,
                args.harmony,
                args.checklist,
                state_root=args.state_root or default_state_root(),
                evaluator_model=model,
            )
            _print_status(value, as_json=args.json)
        elif args.command == "record-reference":
            _print_status(record_reference(args.run, args.feature_id, args.android_anchor, args.reference_behavior, args.source_basis))
        elif args.command == "revise-reference":
            _print_status(revise_reference(args.run, args.feature_id, args.android_anchor, args.reference_behavior, args.source_basis))
        elif args.command == "seal-reference":
            _print_status(seal_reference(args.run))
        elif args.command == "record-evidence":
            _print_status(record_evidence(args.run, args.feature_id, args.status, args.target_anchor, args.rationale))
        elif args.command == "status":
            _print_status(status_run(args.run), as_json=args.json)
        elif args.command == "finalize":
            _, report = finalize_run(args.run, save_result=args.save_result)
            print(report, end="", flush=True)
            cleanup_finalized_run(args.run)
        else:
            raise RunError(f"unknown command: {args.command}")
    except RunError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
