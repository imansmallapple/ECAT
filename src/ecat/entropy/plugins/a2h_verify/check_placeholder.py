"""placeholder —— 占位 / stub / TODO 标记检测（CHECK-3,硬检查）。

两步扫描，识别迁移过程中遗留的"骨架代码"：

  Step 1: 结构化扫描 spec/placeholder-registry.md
    - 读注册表中 status=pending 的占位项
    - 检查它对应的 target 文件是否已存在 → 标为 backfillable（可回填）
    - 统计 status=deferred 的项目（已知延期，扣分较轻）

  Step 2: Grep 兜底，在所有 .ets / .ts 文件里找未注册的占位标记
    - TODO / FIXME 注释（限注释行内，避免误报变量名）
    - "Hello World" 字面（ArkTS 项目模板默认值未替换）
    - "Placeholder" 仅在注释里命中（避免 ArkTS UI 组件 placeholder: prop 误报）
    - "NOT IMPLEMENTED" 字样
    - "// stub" 注释

计分：current_plh_count / baseline_plh_count
  - baseline 取首次评估时的含占位标记文件数，持久化到 <runs_dir>/entropy_baselines.json（与 source_parity 共用）
  - 首轮 score = 1.0；随占位标记被清除线性下降至 0.0
  - baseline = 0 时 score 始终为 0.0（工程一开始就干净）
适用于任何 HarmonyOS 工程——所有规则都是语言级 idiom。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from ecat.entropy.base import EntropyPlugin, should_skip_path
from ecat.entropy.spec_parser import parse_placeholder_registry
from ecat.gradient.schema import WorkItem


def _norm_registry_path(p: str) -> str:
    """Normalize a registry `location` for file-level matching across apps.

    Drops the `#method` anchor (new-spec format), unifies path separators to
    forward slash, and strips a leading `./`. App-agnostic: handles Windows
    backslashes and the `file#method` location convention without app-specific
    assumptions.
    """
    return p.split("#", 1)[0].replace("\\", "/").strip().removeprefix("./")


_PLACEHOLDER_PATTERNS = [
    # `TODO` — exempt the L3 callback format `TODO:<ACTION>:<Component>`
    # (uppercase-snake action + identifier), which arkts-component-builder
    # prescribes as an intentional, grep-discoverable placeholder marker.
    re.compile(r"\bTODO\b(?!:(?-i:[A-Z_]+):[A-Za-z_])", re.IGNORECASE),
    re.compile(r"\bFIXME\b", re.IGNORECASE),
    re.compile(r"\bHello\s+World\b", re.IGNORECASE),
    # `placeholder` counts as a stub marker only when it stands ALONE in a
    # comment — i.e. it is not part of a larger name or noun phrase. Two
    # word-boundary guards (app-agnostic, no enumerated words):
    #   - `(?<!-)` skips hyphen compounds ("glyph-placeholder", kebab names).
    #   - `(?!\s+\w)` skips "placeholder <noun>" terminology ("placeholder
    #     pattern/registration/rect/range") — a real, implemented thing.
    # Identifier-embedded uses (`drawPlaceholderRect`, `placeholderColor`) have
    # no `\b` before/after the substring, so they never match. A genuine stub
    # ("// placeholder", "* just a placeholder", "// placeholder, replace")
    # still matches. Also avoids the ArkTS `placeholder:` prop in code.
    re.compile(
        r"(?://|/\*|^\s*\*).*?(?<!-)\bplaceholder\b(?!\s+\w)",
        re.IGNORECASE,
    ),
    re.compile(r"\bNOT\s+IMPLEMENTED\b", re.IGNORECASE),
    re.compile(r"// stub", re.IGNORECASE),
]


class PlaceholderPlugin(EntropyPlugin):
    """Count placeholder comments / stub code and cross-check registry."""

    name = "placeholder"
    default_weight = 0.05

    def extract_work_items(
        self, score: float, details: dict[str, object]
    ) -> list[WorkItem]:
        items: list[WorkItem] = []
        for ph_id in details.get("backfillable", []):
            items.append(WorkItem(
                file=Path("."),
                issues=[f"Backfillable placeholder {ph_id} — {self.FIX_HINT}"],
                suggested_skills=[],
                severity="high",
            ))
        for unreg in details.get("unregistered", []):
            if isinstance(unreg, dict):
                items.append(WorkItem(
                    file=Path(str(unreg.get("file", "."))),
                    issues=[f"Unregistered placeholder ({unreg.get('snippet', '')}) — "
                            f"{self.FIX_HINT}, or delete the comment if already implemented"],
                    suggested_skills=[],
                    severity="high",
                    lines=[int(unreg["line"])] if "line" in unreg else None,
                ))
        return items

    def evaluate(
        self,
        harmony_dir: Path,
        *,
        android_dir: Path | None = None,
        spec_dir: Path | None = None,
        config: dict[str, object] | None = None,
    ) -> tuple[float, dict[str, object]]:
        # Step 1: Structured scan from placeholder-registry.md
        registry_entries = parse_placeholder_registry(spec_dir) if spec_dir else []
        pending_count = 0
        deferred_count = 0
        backfillable: list[str] = []

        for entry in registry_entries:
            if entry.status == "pending":
                pending_count += 1
                if _target_file_exists(harmony_dir, entry.target):
                    backfillable.append(entry.ph_id)
            elif entry.status == "deferred":
                deferred_count += 1

        # File-level whitelist, normalized so registry `location` (forward
        # slashes, optional #method anchor) matches scanned paths (OS separators,
        # no anchor). Conservative: basename fallback tolerates path-prefix
        # differences across apps — we prefer missing a placeholder over emitting
        # a noisy false "unregistered" work-item.
        registered_full = {
            _norm_registry_path(e.file) for e in registry_entries if e.file
        }
        registered_base = {
            p.rsplit("/", 1)[-1] for p in registered_full if p
        }

        # Step 2: Grep fallback — find unregistered placeholders
        ets_files = [
            f for f in (list(harmony_dir.rglob("*.ets")) + list(harmony_dir.rglob("*.ts")))
            if not should_skip_path(f, harmony_dir)
        ]
        grep_hits: list[dict[str, object]] = []
        unregistered: list[dict[str, object]] = []
        plh_files: set[str] = set()

        for f in ets_files:
            try:
                content = f.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            rel = str(f.relative_to(harmony_dir))
            rel_norm = rel.replace("\\", "/")
            rel_base = rel_norm.rsplit("/", 1)[-1]
            is_registered = (
                rel_norm in registered_full or rel_base in registered_base
            )
            for line_no, line in enumerate(content.splitlines(), start=1):
                for pat in _PLACEHOLDER_PATTERNS:
                    if pat.search(line):
                        hit = {
                            "file": rel,
                            "line": line_no,
                            "snippet": line.strip()[:200],
                        }
                        grep_hits.append(hit)
                        plh_files.add(rel)
                        if not is_registered:
                            unregistered.append(hit)
                        break

        plh_file_count = len(plh_files)
        runs_dir = Path(str(config["runs_dir"])) if config and config.get("runs_dir") else None
        baseline = _load_or_init_baseline(runs_dir, "plh_file_count", plh_file_count)
        score = min(1.0, plh_file_count / baseline) if baseline > 0 else 0.0

        return score, {
            "registry_total": len(registry_entries),
            "pending_count": pending_count,
            "deferred_count": deferred_count,
            "backfillable": backfillable,
            "plh_file_count": plh_file_count,
            "baseline_plh_count": baseline,
            "grep_hit_count": len(grep_hits),
            "unregistered_count": len(unregistered),
            "unregistered": unregistered[:20],
            "grep_hits": grep_hits[:30],
        }


_BASELINE_FILENAME = "entropy_baselines.json"


def _load_or_init_baseline(
    runs_dir: Path | None, key: str, current_value: int
) -> int:
    """Return the persisted baseline for *key* under <runs_dir>/entropy_baselines.json.

    First call for a given *key* writes *current_value* (giving score=1.0).
    Falls back to *current_value* when *runs_dir* is unavailable. Shared with
    check_source_parity — both plugins read/write the same file.
    """
    if runs_dir is None:
        return current_value
    path = runs_dir / _BASELINE_FILENAME
    data: dict[str, object] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except Exception:
            data = {}
    if key in data:
        try:
            return int(data[key])
        except (TypeError, ValueError):
            pass
    data[key] = current_value
    try:
        runs_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass
    return current_value


def _target_file_exists(harmony_dir: Path, target: str) -> bool:
    """Check if a placeholder's target page/feature file exists in harmony."""
    if not target:
        return False
    candidate = harmony_dir / "entry" / "src" / "main" / "ets" / target
    if candidate.exists():
        return True
    return any(
        target.lower() in f.name.lower()
        for f in harmony_dir.rglob("*.ets")
        if not should_skip_path(f, harmony_dir)
    )
