"""Memory-tree classifier: TAXONOMY and classify_entry."""
from __future__ import annotations

import re
from collections.abc import Generator
from pathlib import Path

# Ordered: first category whose any keyword appears in "<name> <description>"
# (lowercased) wins. Specific categories precede general ones to win overlaps.
# Coarse on purpose — a one-time migration seed; boundary cases get hand-corrected.
TAXONOMY: list[tuple[str, tuple[str, ...]]] = [
    ("native-napi", ("napi", "cpp", "c++", "shader", "glsl", "gl-", "xcomponent",
                     "native-", "vendor", "opencv", "cryptopp", "jni", "buffer",
                     "geometry", "define-stub", "bezier", "gpu", "renderer", "worker")),
    ("webview", ("webview",)),
    ("touch-gesture", ("touch", "gesture", "drag", "stroke", "pressure",
                       "canvas-touch", "hit-test")),
    ("navigation", ("nav-", "nav_", "navpath", "navdestination", "route", "router",
                    "pagemap", "push", "entry-rout", "splash")),
    ("state-storage", ("appstorage", "persist", "storage", "observed", "consume",
                       "provide", "@state", "dialog-controller", "wrapper-dead")),
    ("settings", ("setting",)),
    ("placeholder-stub", ("placeholder", "plh", "todo", "not-implemented",
                          "sample-verifier", "scaffold", "stub")),
    ("resource", ("resource", "$r", "icon", "svg", "media", "drawable", "color")),
    ("lifecycle", ("abouttoappear", "about-to-appear", "onready", "lifecycle",
                   "async-init", "oncreate", "singleton-init")),
    ("data-pipeline", ("repository", "mock", "pipeline", "cache", "sync", "vo-",
                       "data-", "api-", "server", "auth", "loading")),
    ("ui-completeness", ("completeness", "noop", "handler", "toolbar", "variant",
                         "multiscreen", "exhaustive", "dead-", "feature")),
    ("i18n-locale", ("i18n", "locale", "language", "unicode")),
    ("arkts-idioms", ("arkts-", "no-var", "enum", "static-field", "math",
                      "index-enum", "lint", "console", "hilog", "foreach",
                      "lazyforeach", "no-export")),
]

FALLBACK_CATEGORY = "misc"

# One-line hint per seed category, shown in the top-level MEMORY.md so the
# generator can pick relevant categories without opening indexes speculatively.
# Categories the discriminator creates at runtime register their own hint.
CATEGORY_DESCRIPTIONS: dict[str, str] = {
    "navigation": "routing / NavPathStack / pageMap",
    "state-storage": "@State / AppStorage / persistence",
    "settings": "settings UI & persistence",
    "resource": "$r resources / icons / media",
    "native-napi": "C++ / NAPI / GL bridges & stubs",
    "lifecycle": "aboutToAppear / onReady timing",
    "data-pipeline": "repository / API / mock removal",
    "ui-completeness": "stub handlers / missing screens",
    "placeholder-stub": "PLH / TODO / stub markers",
    "webview": "WebView lifecycle & JS bridge",
    "touch-gesture": "touch / drag / gesture",
    "arkts-idioms": "ArkTS language rules",
    "i18n-locale": "i18n / locale / language",
    "misc": "uncategorized / cross-cutting",
}


def classify_entry(name: str, description: str) -> str:
    """Map a memory entry to a top-level category by keyword match.

    First category in TAXONOMY with any keyword present in "<name> <description>"
    (lowercased) wins; falls back to "misc" when nothing matches.
    """
    haystack = f"{name} {description}".lower()
    for category, keywords in TAXONOMY:
        if any(kw in haystack for kw in keywords):
            return category
    return FALLBACK_CATEGORY


_FRONT_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def _parse_front(text: str) -> dict[str, str]:
    """Extract top-level `key: value` pairs from a YAML front-matter block."""
    m = _FRONT_RE.match(text)
    fields: dict[str, str] = {}
    if not m:
        return fields
    for line in m.group(1).splitlines():
        if line[:1].isspace() or ":" not in line:
            continue  # skip nested (e.g. under metadata:) and non-pairs
        key, _, val = line.partition(":")
        fields[key.strip()] = val.strip()
    return fields


def _iter_flat_entries(memory_dir: Path) -> Generator[tuple[Path, str, str], None, None]:
    """Yield (path, name, description) for feedback*.md directly under memory_dir.

    Matches both `feedback_<slug>.md` (the standard) and the legacy
    `feedback-<slug>.md` hyphen variant so no entry is silently skipped.
    """
    for path in sorted(memory_dir.glob("feedback*.md")):
        fields = _parse_front(path.read_text(encoding="utf-8"))
        name = fields.get("name") or path.stem.removeprefix("feedback_").removeprefix("feedback-")
        yield path, name, fields.get("description", "")


def migrate_flat_to_tree(memory_dir: Path) -> None:
    """One-time: move flat feedback_*.md into <category>/ dirs (one level only).

    Generates each category's _index.md and rewrites the top-level MEMORY.md to
    list categories. Idempotent ONLY for the initial flat corpus (re-running on a
    freshly-migrated, still-flat tree yields identical output). Do NOT re-run after
    the discriminator has grown sub-directories at runtime — step 2 rebuilds each
    _index.md from direct entries only and would erase sub-dir references.
    Deeper sub-levels are intentionally left empty — grown at runtime by the
    discriminator. Boundary mis-classifications are expected; hand-correct after.
    """
    # 1. classify + move root-level flat entries into category dirs
    for path, name, description in _iter_flat_entries(memory_dir):
        category = classify_entry(name, description)
        dest_dir = memory_dir / category
        dest_dir.mkdir(exist_ok=True)
        target = dest_dir / path.name
        if target.resolve() != path.resolve():
            target.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
            path.unlink()

    # 2. (re)build each category's _index.md from its direct entries
    categories: list[tuple[str, int]] = []
    for cat_dir in sorted(p for p in memory_dir.iterdir() if p.is_dir()):
        if cat_dir.name.startswith("."):
            continue
        entries = list(_iter_flat_entries(cat_dir))
        if not entries:
            continue
        lines = [f"# {cat_dir.name}", ""]
        for entry_path, name, description in entries:
            lines.append(f"- [{name}]({entry_path.name}) — {description}")
        (cat_dir / "_index.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        categories.append((cat_dir.name, len(entries)))

    # 3. rewrite top-level MEMORY.md as the category list
    top_lines = ["# Generator Memory Index", ""]
    for cat, count in categories:
        hint = CATEGORY_DESCRIPTIONS.get(cat)
        suffix = f"{hint} ({count})" if hint else str(count)
        top_lines.append(f"- [{cat}]({cat}/_index.md) — {suffix}")
    (memory_dir / "MEMORY.md").write_text("\n".join(top_lines) + "\n", encoding="utf-8")


def render_injection(memory_dir: Path | None) -> str:
    """Render the generator prompt section: top-level category index + drill-down.

    Returns "" when memory_dir is None or its MEMORY.md is missing/empty. Uses
    runtime-agnostic wording ("open/read the file") so both claude_code and
    opencode generators can navigate with their own file-reading tools.
    """
    if memory_dir is None:
        return ""
    index = memory_dir / "MEMORY.md"
    if not index.exists():
        return ""
    content = index.read_text(encoding="utf-8").strip()
    cat_lines = [ln for ln in content.splitlines() if ln.strip() and not ln.startswith("#")]
    if not cat_lines:
        return ""
    root = memory_dir.resolve().as_posix()
    return (
        "\n\n## Learned fix patterns (memory tree)\n"
        "Fix-pattern categories indexed below:\n"
        + "\n".join(cat_lines)
        + "\nOpen the `_index.md` of categories relevant to this work-list (several at "
        "once is fine), drill into relevant sub-dirs, read the matching entries, and "
        "apply them. Prune irrelevant branches.\n"
        f"Memory root: {root}/"
    )
