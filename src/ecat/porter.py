"""resource_porter — deterministic resource porting between migration rounds.

The generator keeps emitting ``$r('app.<type>.<name>')`` for resources that only
exist on the Android side (``@dimen/…``, ``@string/…``, ``@drawable/…``).  Those
references surface later as ArkTS compile errors — in a 5-layout pilot on
Simple-Gallery, 82% of all compile errors were exactly this class.  Fixing them
with an LLM costs a round and tokens; the fix itself is mechanical.

This module runs in the loop right before entropy evaluation:

1. rewrite HarmonyOS-invalid resource types (``dimen`` → ``float``,
   ``drawable``/``mipmap`` → ``media``, ``id`` → ``string``);
2. collect every remaining ``$r('app.…')`` reference;
3. skip names the HarmonyOS project already declares;
4. resolve the rest from the Android ``res/values*`` + ``drawable*``/``mipmap*``
   trees (dp/sp are kept numerically, HarmonyOS treats them as vp/fp);
5. anything still unresolved — typically resources that live in a library AAR —
   gets a typed placeholder so the compile gate can run, and is reported so the
   generator can supply the real value later.

Placeholders never fake fidelity: they are reported in ``stats.placeholders`` and
the caller logs them.  The porter is deterministic and costs no tokens.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import struct
import zlib
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

TYPE_ALIASES = {"dimen": "float", "drawable": "media", "mipmap": "media", "id": "string"}
ELEMENT_TYPES = ("string", "float", "color", "integer", "boolean")
REF_RE = re.compile(r"\$r\('app\.([a-z]+)\.([A-Za-z0-9_.\-]+)'\)")
DIM_RE = re.compile(r"\s*(-?[\d.]+)\s*(dp|dip|sp|px|pt|in|mm)?")
MEDIA_EXT_PRIORITY = {"png": 0, "webp": 1, "jpg": 2, "jpeg": 3}
FALLBACK = {"string": "", "float": "0", "color": "#00000000", "integer": "0", "boolean": "false"}


@dataclass
class PorterStats:
    rewritten_refs: int = 0
    from_android: int = 0
    placeholders: list[str] = field(default_factory=list)
    already_declared: int = 0
    files_touched: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "rewritten_refs": self.rewritten_refs,
            "from_android": self.from_android,
            "placeholders": len(self.placeholders),
            "already_declared": self.already_declared,
            "files_touched": self.files_touched,
        }


def _sanitize(name: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_]", "_", name)
    return ("_" + name if name[:1].isdigit() else name).lower()


def _png_1x1() -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00\x00", 9)) + chunk(b"IEND", b""))


def _android_values(res_dirs: list[Path]) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {t: {} for t in ELEMENT_TYPES}
    tag_map = {"string": "string", "dimen": "float", "color": "color",
               "integer": "integer", "bool": "boolean"}
    for res in res_dirs:
        for vdir in sorted(res.glob("values*")):
            for f in vdir.glob("*.xml"):
                try:
                    root = ET.parse(f).getroot()
                except (ET.ParseError, OSError):
                    continue
                for el in root:
                    name = el.get("name")
                    kind = tag_map.get(el.tag)
                    if not name or not kind:
                        continue
                    value = (el.text or "").strip()
                    if kind == "float":
                        m = DIM_RE.match(value)
                        value = str(float(m.group(1))) if m else "0"
                    out[kind].setdefault(name, value)
    return out


def _android_media(res_dirs: list[Path]) -> dict[str, Path]:
    out: dict[str, Path] = {}
    for res in res_dirs:
        for d in list(res.glob("drawable*")) + list(res.glob("mipmap*")):
            for f in d.iterdir() if d.is_dir() else []:
                ext = f.suffix.lower().lstrip(".")
                if not f.is_file() or ext not in MEDIA_EXT_PRIORITY:
                    continue
                cur = out.get(f.stem)
                if cur is None or MEDIA_EXT_PRIORITY[ext] < MEDIA_EXT_PRIORITY[cur.suffix.lower().lstrip(".")]:
                    out[f.stem] = f
    return out


def _source_dirs(harmony_dir: Path, leaf: str) -> list[Path]:
    """Resource dirs under src/main only — never build outputs or dependency caches."""
    out = []
    for d in Path(harmony_dir).glob(f"**/{leaf}"):
        parts = {p.lower() for p in d.parts}
        if parts & {"build", "oh_modules", ".preview", ".hvigor", "node_modules", "intermediates"}:
            continue
        if "src" not in parts:
            continue
        out.append(d)
    return sorted(set(out))


def _declared(harmony_dir: Path) -> dict[str, set[str]]:
    declared: dict[str, set[str]] = {t: set() for t in ELEMENT_TYPES + ("media",)}
    element_dirs = _source_dirs(harmony_dir, "resources/base/element")
    for el in element_dirs:
        for f in el.glob("*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            for kind, items in data.items():
                if kind in declared and isinstance(items, list):
                    for item in items:
                        if isinstance(item, dict) and item.get("name"):
                            declared[kind].add(item["name"])
    for media in _source_dirs(harmony_dir, "resources/base/media"):
        for p in media.iterdir():
            if p.is_file():
                declared["media"].add(p.stem)
    return declared


def _write_element(element_dir: Path, kind: str, entries: dict[str, str]) -> None:
    path = element_dir / f"{kind}.json"
    existing: dict[str, str] = {}
    if path.exists():
        try:
            for item in json.loads(path.read_text(encoding="utf-8")).get(kind, []):
                existing[item["name"]] = item["value"]
        except (json.JSONDecodeError, OSError, KeyError, TypeError):
            existing = {}
    existing.update(entries)
    path.write_text(json.dumps({kind: [{"name": k, "value": v} for k, v in sorted(existing.items())]},
                               indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def run(harmony_dir: Path, android_dir: Path | None) -> PorterStats:
    """Port missing resources in place.  Never raises for missing inputs."""
    stats = PorterStats()
    harmony_dir = Path(harmony_dir)
    pages = [p for p in harmony_dir.glob("**/*.ets")]
    if not pages:
        return stats

    res_dirs: list[Path] = []
    if android_dir:
        android_root = Path(android_dir)
        if (android_root / "values").is_dir():
            res_dirs = [android_root]          # caller passed the res directory itself
        else:
            res_dirs = sorted(p for p in android_root.glob("**/res") if p.is_dir())
    values = _android_values(res_dirs) if res_dirs else {t: {} for t in ELEMENT_TYPES}
    media_src = _android_media(res_dirs) if res_dirs else {}
    declared = _declared(harmony_dir)

    refs: set[tuple[str, str]] = set()
    for f in pages:
        text = f.read_text(encoding="utf-8", errors="ignore")
        if "$r('app." not in text:
            continue

        def repl(m: re.Match, _f: Path = f) -> str:
            kind, name = m.group(1), _sanitize(m.group(2))
            new_kind = TYPE_ALIASES.get(kind, kind)
            if new_kind != kind:
                stats.rewritten_refs += 1
            refs.add((new_kind, name))
            return f"$r('app.{new_kind}.{name}')"

        new_text = REF_RE.sub(repl, text)
        if new_text != text:
            f.write_text(new_text, encoding="utf-8")
            stats.files_touched += 1

    pending: dict[str, dict[str, str]] = {t: {} for t in ELEMENT_TYPES}
    media_copy: dict[str, Path] = {}
    for kind, name in sorted(refs):
        if name in declared.get(kind, set()):
            stats.already_declared += 1
            continue
        if kind == "media":
            if name in media_src:
                media_copy[name] = media_src[name]
                stats.from_android += 1
            else:
                stats.placeholders.append(f"media.{name}")
            continue
        value = values.get(kind, {}).get(name)
        if value is None:
            stats.placeholders.append(f"{kind}.{name}")
            value = FALLBACK[kind]
        else:
            stats.from_android += 1
        pending[kind][name] = value

    element_dirs = _source_dirs(harmony_dir, "resources/base/element")
    if element_dirs:
        for kind, entries in pending.items():
            if entries:
                _write_element(element_dirs[0], kind, entries)

    for media in _source_dirs(harmony_dir, "resources/base/media"):
        media.mkdir(parents=True, exist_ok=True)
        for name, src in media_copy.items():
            shutil.copy(src, media / f"{name}{src.suffix.lower()}")
        for ph in stats.placeholders:
            if not ph.startswith("media."):
                continue
            target = media / f"{ph.split('.', 1)[1]}.png"
            if not target.exists():
                target.write_bytes(_png_1x1())
        break

    if stats.placeholders or stats.from_android or stats.rewritten_refs:
        log.info("resource porter: %s (placeholders: %s)", stats.as_dict(),
                 ", ".join(stats.placeholders[:8]) or "none")
    return stats
