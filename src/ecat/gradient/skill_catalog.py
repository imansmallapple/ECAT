"""Skill catalog — caches Generator skill descriptions to avoid repeated file reads.

The catalog is built by parsing SKILL.md frontmatter (pure Python, no LLM).
It is stored as a JSON file in the workspace and invalidated whenever the set
of skills changes (detected via a hash of skill names).
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

import yaml

log = logging.getLogger(__name__)


def _parse_frontmatter(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    # Parse the frontmatter block as YAML so block scalars (``description: >``
    # with the body on following indented lines), quoting, and multi-line
    # values are handled correctly. A naive ``key: value`` line split would
    # capture only the ``>``/``|`` marker and drop the actual text.
    try:
        data = yaml.safe_load(text[3:end]) or {}
    except yaml.YAMLError:
        return {}
    if not isinstance(data, dict):
        return {}
    result: dict[str, str] = {}
    for key, val in data.items():
        if val is None:
            continue
        # Collapse folded/literal scalars to a single trimmed line for the catalog.
        result[str(key)] = " ".join(str(val).split())
    return result


def _scan_skill_root(skill_root: Path) -> list[dict[str, str]]:
    skills: list[dict[str, str]] = []
    for skill_md in sorted(skill_root.glob("*/SKILL.md")):
        fm = _parse_frontmatter(skill_md)
        name = fm.get("name", "")
        if not name:
            continue
        skills.append({
            "name": name,
            "description": fm.get("description", ""),
            "type": fm.get("type", ""),
            "domain": fm.get("domain", ""),
        })
    return skills


def _skills_hash(skills: list[dict[str, str]]) -> str:
    names = ",".join(sorted(s["name"] for s in skills))
    return hashlib.md5(names.encode()).hexdigest()[:12]


def get_or_refresh_catalog(
    skill_root: Path,
    catalog_path: Path,
) -> list[dict[str, str]]:
    """Return the skill catalog, refreshing from disk if stale or missing.

    Reads SKILL.md frontmatter directly — no LLM or network call required.
    Regenerates only when the set of skills changes (hash of skill names).
    """
    current_skills = _scan_skill_root(skill_root)
    current_hash = _skills_hash(current_skills)

    if catalog_path.exists():
        try:
            existing = json.loads(catalog_path.read_text(encoding="utf-8"))
            if existing.get("skill_hash") == current_hash:
                log.debug("Skill catalog up-to-date (%d skills)", len(existing["skills"]))
                return existing["skills"]
        except Exception:
            pass

    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    catalog_path.write_text(
        json.dumps(
            {"skill_hash": current_hash, "skill_count": len(current_skills), "skills": current_skills},
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    log.info("Refreshed skill catalog: %d skills → %s", len(current_skills), catalog_path)
    return current_skills
