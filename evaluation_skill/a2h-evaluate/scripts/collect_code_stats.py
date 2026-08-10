#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path


EXCLUDED_DIRS = {
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

LANG_BY_EXT = {
    ".c": "C",
    ".cc": "C++",
    ".cpp": "C++",
    ".css": "CSS",
    ".cfg": "Config",
    ".gradle": "Gradle",
    ".h": "C/C++ Header",
    ".hpp": "C++ Header",
    ".html": "HTML",
    ".ini": "INI",
    ".java": "Java",
    ".js": "JavaScript",
    ".json": "JSON",
    ".json5": "JSON5",
    ".kt": "Kotlin",
    ".kts": "Kotlin Script",
    ".md": "Markdown",
    ".properties": "Properties",
    ".ts": "TypeScript",
    ".txt": "Text",
    ".xml": "XML",
    ".ets": "ArkTS/ETS",
}


def should_skip(path: Path, root: Path) -> bool:
    rel_parts = path.relative_to(root).parts
    return any(part in EXCLUDED_DIRS for part in rel_parts)


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise RuntimeError(f"cannot read source file {path}: {exc}") from exc


def collect(repo: Path, role: str) -> dict:
    repo = repo.resolve()
    language_stats: dict[str, dict] = {}
    code_file_count = 0
    code_total_lines = 0
    code_nonblank_lines = 0
    code_bytes = 0
    ets_file_count = 0
    ets_total_lines = 0
    ets_nonblank_lines = 0

    for path in repo.rglob("*"):
        if path.is_symlink() or not path.is_file() or should_skip(path, repo):
            continue
        ext = path.suffix.lower()
        language = LANG_BY_EXT.get(ext)
        if language is None:
            continue

        text = read_text(path)
        lines = text.splitlines()
        nonblank = sum(1 for line in lines if line.strip())
        size = path.stat().st_size

        code_file_count += 1
        code_total_lines += len(lines)
        code_nonblank_lines += nonblank
        code_bytes += size

        entry = language_stats.setdefault(
            language,
            {
                "files": 0,
                "total_lines": 0,
                "nonblank_lines": 0,
                "bytes": 0,
                "extensions": set(),
            },
        )
        entry["files"] += 1
        entry["total_lines"] += len(lines)
        entry["nonblank_lines"] += nonblank
        entry["bytes"] += size
        entry["extensions"].add(ext)

        if ext == ".ets":
            ets_file_count += 1
            ets_total_lines += len(lines)
            ets_nonblank_lines += nonblank

    normalized_lang_stats = {}
    for language, value in sorted(language_stats.items()):
        normalized_lang_stats[language] = {
            "files": value["files"],
            "total_lines": value["total_lines"],
            "nonblank_lines": value["nonblank_lines"],
            "bytes": value["bytes"],
            "extensions": sorted(value["extensions"]),
        }

    return {
        "role": role,
        "repo": str(repo),
        "ets_file_count": ets_file_count,
        "ets_total_lines": ets_total_lines,
        "ets_nonblank_lines": ets_nonblank_lines,
        "code_file_count": code_file_count,
        "code_total_lines": code_total_lines,
        "code_nonblank_lines": code_nonblank_lines,
        "code_bytes": code_bytes,
        "language_stats": normalized_lang_stats,
        "stats_policy": {
            "included_extensions": sorted(LANG_BY_EXT),
            "excluded_dirs": sorted(EXCLUDED_DIRS),
            "symlinks": "excluded from source statistics",
            "line_count": "Python splitlines physical lines; nonblank excludes whitespace-only lines",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--role", default="repo")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    repo = Path(args.repo)
    if not repo.is_dir():
        raise SystemExit(f"repo not found: {repo}")

    result = collect(repo, args.role)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
