"""skills — discover, validate and fetch agent skills from the Matrix registry.

Three separate jobs, each with the cheapest reliable backend:

  find(query)      discovery.  Matrix's own search is a literal AND match, so it
                   only works for single domain words; ``engine="skills_sh"``
                   shells out to ``npx skills find`` which indexes the same
                   skills under the publisher ``matrix.openharmony.cn`` and
                   handles natural-language queries.
  validate(names)  existence.  ``GET /api/registry/skill/details?enName=`` returns
                   ``data: null`` for unknown names while still answering 200, so
                   the check must inspect ``data``, never the status code.
  ensure(names)    fetching.  The official zip endpoint
                   (``.../install?format=zip``) is used instead of skills.sh so a
                   third-party fork with the same name can never be installed.

Nothing here talks to an LLM; every call is deterministic and cheap, which is why
the driver can run it before each round.
"""
from __future__ import annotations

import io
import json
import logging
import re
import shutil
import subprocess
import tempfile
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

log = logging.getLogger(__name__)

REGISTRY = "https://matrix.openharmony.cn"
TIMEOUT = 60
FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---", re.S)
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _get(url: str, timeout: int = TIMEOUT) -> bytes:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def details(name: str) -> dict | None:
    """Registry metadata for one skill, or None when the name does not exist."""
    url = f"{REGISTRY}/api/registry/skill/details?enName={urllib.parse.quote(name)}"
    try:
        payload = json.loads(_get(url).decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001 - network problems must not kill a run
        log.warning("skills: details(%s) failed: %s", name, exc)
        return None
    return payload.get("data") or None


def exists(name: str) -> bool:
    return details(name) is not None


def validate(names: list[str]) -> tuple[list[str], list[str]]:
    """Split names into (known, unknown) using the registry."""
    known, unknown = [], []
    for name in names:
        (known if exists(name) else unknown).append(name)
    return known, unknown


def find(query: str, *, limit: int = 5, engine: str = "matrix") -> list[dict]:
    """Search the registry.

    engine="matrix"    literal keyword match, fast, single words only
    engine="skills_sh" npx skills find, semantic, also indexes third-party forks
    """
    if engine == "skills_sh":
        return _find_skills_sh(query, limit)
    body = json.dumps({"pageNum": 1, "pageSize": limit, "keyword": query}).encode()
    req = urllib.request.Request(
        f"{REGISTRY}/api/registry/skill/skills",
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001
        log.warning("skills: find(%r) failed: %s", query, exc)
        return []
    data = payload.get("data") or {}
    return [
        {"name": item.get("enName"), "description": (item.get("description") or "")[:160],
         "publisher": "matrix.openharmony.cn"}
        for item in (data.get("list") or [])
    ][:limit]


def _find_skills_sh(query: str, limit: int) -> list[dict]:
    # npx is a .cmd shim on Windows, so it needs a shell to resolve.
    cmd = f'npx -y skills@latest find "{query}"'
    try:
        out = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=180,
        ).stdout
    except Exception as exc:  # noqa: BLE001
        log.warning("skills: npx find(%r) failed: %s", query, exc)
        return []
    results, pending = [], None
    for line in ANSI_RE.sub("", out).splitlines():
        line = line.strip()
        m = re.match(r"^([\w.\-]+@[\w.\-]+)\s+(\d+)\s+installs?$", line)
        if m:
            pending = m.group(1)
            continue
        if pending and line.startswith("└"):
            results.append({"ref": pending, "url": line.lstrip("└ ").strip()})
            pending = None
    return results[:limit]


def _check_frontmatter(skill_md: Path) -> str | None:
    """opencode only advertises skills whose SKILL.md has name + description."""
    try:
        text = skill_md.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "SKILL.md unreadable"
    m = FRONTMATTER_RE.match(text)
    if not m:
        return "SKILL.md has no YAML frontmatter"
    block = m.group(1)
    for field in ("name:", "description:"):
        if field not in block:
            return f"frontmatter missing {field.rstrip(':')}"
    return None


def ensure(names: list[str], dest: Path | None = None) -> dict[str, str]:
    """Make sure every name is installed under dest; fetch the missing ones.

    Returns {name: "present" | "fetched" | "unknown" | "failed"}.
    """
    dest = Path(dest) if dest else Path.home() / ".agents" / "skills"
    dest.mkdir(parents=True, exist_ok=True)
    status: dict[str, str] = {}
    for name in names:
        target = dest / name
        if (target / "SKILL.md").exists():
            status[name] = "present"
            continue
        if details(name) is None:
            status[name] = "unknown"
            log.warning("skills: %s does not exist in the registry — skipped", name)
            continue
        try:
            blob = _get(f"{REGISTRY}/api/registry/skill/{urllib.parse.quote(name)}/install?format=zip", timeout=180)
            with zipfile.ZipFile(io.BytesIO(blob)) as zf:
                members = [m for m in zf.namelist() if not m.startswith(("/", ".."))]
                with tempfile.TemporaryDirectory() as tmp:
                    zf.extractall(tmp, members=members)
                    src = Path(tmp)
                    problem = _check_frontmatter(src / "SKILL.md")
                    if problem:
                        status[name] = "failed"
                        log.warning("skills: %s rejected: %s", name, problem)
                        continue
                    if target.exists():
                        shutil.rmtree(target, ignore_errors=True)
                    shutil.copytree(src, target)
            status[name] = "fetched"
            log.info("skills: fetched %s", name)
        except Exception as exc:  # noqa: BLE001
            status[name] = "failed"
            log.warning("skills: fetch %s failed: %s", name, exc)
    return status
