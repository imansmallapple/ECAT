#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path


PREP_TIMEOUT = 900
NODE_SIMILARITY_THRESHOLD = 0.30
INDEX_EXCLUDED = {
    ".cxx",
    ".codegraph",
    ".git",
    ".gradle",
    ".idea",
    "build",
    "node_modules",
    "oh_modules",
    "out",
}
REQUIRED_ZIP_ENTRIES = {
    "node_align/pasg/kg_pipeline.py",
    "node_align/pasg/alignment.py",
}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def run(cmd: list[str], *, cwd: Path | None = None, timeout: int = PREP_TIMEOUT) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=True,
    )


def resolve_zip() -> Path:
    return Path(__file__).resolve().parents[1] / "assets" / "node_align_tool.zip"


def resolve_manifest() -> Path:
    return Path(__file__).resolve().parents[1] / "assets" / "node_align_tool.checksum"


def verify_zip(zip_path: Path, manifest_path: Path) -> str:
    if not zip_path.is_file() or not manifest_path.is_file():
        raise RuntimeError("bundled node_align_tool.zip or checksum file is missing")
    fields = manifest_path.read_text(encoding="utf-8").split()
    if not fields or len(fields[0]) != 64:
        raise RuntimeError("invalid node_align_tool.checksum file")
    expected = fields[0]
    actual = sha256(zip_path)
    if actual != expected:
        raise RuntimeError(f"node_align_tool.zip sha256 mismatch: expected {expected}, got {actual}")
    return actual


def verify_zip_contents(zip_path: Path) -> None:
    try:
        with zipfile.ZipFile(zip_path) as archive:
            names = set(archive.namelist())
    except (OSError, zipfile.BadZipFile) as exc:
        raise RuntimeError(f"invalid bundled node_align_tool.zip: {exc}") from exc
    unsafe = [name for name in names if Path(name).is_absolute() or ".." in Path(name).parts]
    if unsafe:
        raise RuntimeError("bundled node_align_tool.zip contains unsafe paths")
    missing = sorted(REQUIRED_ZIP_ENTRIES - names)
    if missing:
        raise RuntimeError(f"bundled node_align_tool.zip is missing required entries: {missing}")


ZIP_SHA256 = verify_zip(resolve_zip(), resolve_manifest())


def extract_tool(zip_path: Path, manifest_path: Path, work_dir: Path) -> Path:
    actual = verify_zip(zip_path, manifest_path)
    verify_zip_contents(zip_path)
    tool_root = work_dir / "_node_align_tool"
    marker = tool_root / ".extracted.checksum"
    if marker.exists() and marker.read_text(encoding="utf-8").strip() == actual:
        return tool_root / "node_align"
    if tool_root.exists():
        shutil.rmtree(tool_root)
    tool_root.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(tool_root)
    marker.write_text(actual + "\n", encoding="utf-8")
    return tool_root / "node_align"


def resolve_codegraph() -> str | None:
    env = os.environ.get("CODEGRAPH_EXE")
    if env and Path(env).exists():
        return env
    exe = shutil.which("codegraph") or shutil.which("codegraph.cmd")
    if exe:
        return exe
    for base in (
        Path.home() / "AppData" / "Roaming" / "npm",
        Path.home() / ".npm-global" / "bin",
        Path("/usr/local/bin"),
        Path("/usr/bin"),
    ):
        for ext in ("", ".cmd", ".bat", ".ps1"):
            candidate = base / f"codegraph{ext}"
            if candidate.exists():
                return str(candidate)
    return None


def _runtime_component(command: str, label: str) -> dict[str, str]:
    executable = Path(command).resolve()
    if not executable.is_file():
        raise RuntimeError(f"{label} executable is not a file: {executable}")
    try:
        completed = run([str(executable), "--version"], timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"cannot query {label} version: {exc}") from exc
    version = (completed.stdout or completed.stderr).strip().splitlines()
    if not version:
        raise RuntimeError(f"{label} --version returned no version")
    return {"version": version[0], "sha256": sha256(executable)}


def resolve_alignment_runtime() -> tuple[str, str, dict[str, str]]:
    codegraph = resolve_codegraph()
    if codegraph is None:
        raise RuntimeError("codegraph executable not found; install the prerequisite before evaluation")
    node = shutil.which("node") or shutil.which("node.exe")
    if node is None:
        raise RuntimeError("node executable not found; install the prerequisite before evaluation")
    codegraph_meta = _runtime_component(codegraph, "codegraph")
    node_meta = _runtime_component(node, "Node.js")
    metadata = {
        "codegraph_version": codegraph_meta["version"],
        "codegraph_sha256": codegraph_meta["sha256"],
        "node_version": node_meta["version"],
        "node_sha256": node_meta["sha256"],
    }
    return str(Path(codegraph).resolve()), str(Path(node).resolve()), metadata


def copy_index_workspace(repo_dir: Path, out_dir: Path, label: str) -> Path:
    workspace = out_dir / f"{label}_index_workspace"
    marker = workspace / ".a2h_source_path"
    if workspace.exists() and marker.exists() and marker.read_text(encoding="utf-8").strip() == str(repo_dir):
        return workspace
    if workspace.exists():
        shutil.rmtree(workspace)

    def ignore(_dir: str, names: list[str]) -> list[str]:
        return [name for name in names if name in INDEX_EXCLUDED]

    shutil.copytree(repo_dir, workspace, ignore=ignore)
    marker.write_text(str(repo_dir) + "\n", encoding="utf-8")
    return workspace


def ensure_codegraph_workspace(repo_dir: Path, out_dir: Path, codegraph: str, label: str) -> Path:
    index_workspace = copy_index_workspace(repo_dir, out_dir, label)
    src_db = index_workspace / ".codegraph" / "codegraph.db"
    if not src_db.exists():
        run([codegraph, "init", "-i"], cwd=index_workspace)
    if not src_db.exists():
        raise RuntimeError(f"codegraph init completed but DB was not created: {src_db}")
    return index_workspace


def remove_path(path: Path) -> None:
    with contextlib.suppress(OSError):
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()


def cleanup_alignment_work_files(out_dir: Path) -> None:
    """Remove reproducible heavyweight scratch while retaining small diagnostics."""
    out_dir = Path(out_dir)
    for relative in (
        "_node_align_tool",
        "android_index_workspace",
        "harmony_index_workspace",
    ):
        remove_path(out_dir / relative)


def normalize_structural_metrics(result: dict) -> dict:
    target_edges = int(result.get("pasg_tgt_edges", 0) or 0)
    if target_edges == 0:
        return {
            "structural_metrics_status": "unavailable_no_target_edges",
            "edge_retention": None,
            "path_retention": None,
            "overall_score": None,
        }
    return {
        "structural_metrics_status": "available",
        "edge_retention": float(result.get("edge_retention", 0.0) or 0.0),
        "path_retention": float(result.get("path_retention", 0.0) or 0.0),
        "overall_score": float(result.get("overall_score", 0.0) or 0.0),
    }


def compact_summary(result: dict, _report: dict | None = None, runtime: dict[str, str] | None = None) -> dict:
    node_retention = float(result.get("node_retention", 0.0) or 0.0)
    structural = normalize_structural_metrics(result)
    source_graph = result.get("pasg_src")
    source_metadata = getattr(source_graph, "metadata", {})
    retention_denominator = int(source_metadata.get("source_arch_entities", 0) or 0)
    if retention_denominator <= 0:
        raise RuntimeError("alignment result is missing the source retention denominator")
    return {
        "tool_status": "ok",
        "score_policy": "node_retention_only",
        "node_similarity_threshold": NODE_SIMILARITY_THRESHOLD,
        "align_score": node_retention,
        "node_retention": node_retention,
        **structural,
        "aligned_pair_count": int(result.get("aligned_pairs", 0) or 0),
        "retention_denominator": retention_denominator,
        "android_nodes": int(result.get("pasg_src_nodes", 0) or 0),
        "android_edges": int(result.get("pasg_src_edges", 0) or 0),
        "harmony_nodes": int(result.get("pasg_tgt_nodes", 0) or 0),
        "harmony_edges": int(result.get("pasg_tgt_edges", 0) or 0),
        **({"runtime": runtime} if runtime is not None else {}),
    }


def run_alignment(android: Path, harmony: Path, out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    tool_dir = extract_tool(resolve_zip(), resolve_manifest(), out_dir)
    codegraph, _node, runtime = resolve_alignment_runtime()
    android_index_workspace = ensure_codegraph_workspace(
        android, out_dir, codegraph, "android"
    )
    harmony_index_workspace = ensure_codegraph_workspace(
        harmony, out_dir, codegraph, "harmony"
    )

    sys.path.insert(0, str(tool_dir))
    from pasg.kg_pipeline import run_pasg  # type: ignore

    result = run_pasg(
        android_project_dir=str(android_index_workspace),
        harmony_project_dir=str(harmony_index_workspace),
        name_mappings={},
        no_bootstrap=False,
        verbose=False,
        node_similarity_threshold=NODE_SIMILARITY_THRESHOLD,
    )
    alignment = result.get("alignment_result")
    report = alignment.to_dict() if alignment is not None else {}

    summary = compact_summary(result, report, runtime)

    (out_dir / "node_align.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    cleanup_alignment_work_files(out_dir)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--android", required=True)
    parser.add_argument("--harmony", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    android = Path(args.android).resolve()
    harmony = Path(args.harmony).resolve()
    out_dir = Path(args.out).resolve()

    try:
        result = run_alignment(android, harmony, out_dir)
    except Exception:
        cleanup_alignment_work_files(out_dir)
        raise

    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
