"""feature_check —— 功能完整性检查（CHECK-6，硬+agentic 复合）。

按 spec 列出的功能逐项验证 ArkTS 实现是否完整。对每个 V1 feature 算
"完成度 ratio"：ArkTS 行数 / (Android 源行数 × 0.5)，capped 到 1.0。
ratio 直接驱动 work_item severity（<30% high / 30-70% medium / >=70% low）。

具体检查项：
  Layer 1（spec 驱动）：
    1. 解析 spec/baseline/feature-index.md 列出的所有 V1 feature
    2. 每个 feature 的 output_files 是否真的存在
    3. 文件实际内容质量（行数比 Android 源 ≥25% 算 VERIFIED）
    4. 通过 source-mapping.md 关联 Android 源→ArkTS 页面，比较行数比例
  Layer 2（无 spec 时降级）：
    5. 通过 grep 找 Android 常见模式（RecyclerView、SharedPreferences、Retrofit 等）
       并检查是否有 ArkTS 等价实现（List、@ohos.data.preferences、@kit.NetworkKit 等）
  Layer 3（agentic judge）：
    6. LLM 读取 Android 源文件与对应 HarmonyOS 文件，逐功能比对逻辑等价性

适用于任何 Android→HarmonyOS 迁移——所有量化指标都基于行数比、不依赖 app-specific 字眼。
"""

from __future__ import annotations

import contextlib
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from ecat.entropy.base import EntropyPlugin, should_skip_path
from ecat.entropy.spec_parser import parse_feature_index, parse_source_mapping
from ecat.gradient.schema import WorkItem

if TYPE_CHECKING:
    from ecat.entropy.spec_parser import FeatureEntry

log = logging.getLogger(__name__)

# Matches `private fun setupXxx(` / `private fun setupXxx(` in Kotlin — each such
# method represents one discrete user-configurable option in a Settings/Options page.
# App-agnostic: this structural pattern appears in any Android settings activity
# regardless of the underlying storage API (SharedPreferences, DataStore, Room, etc.).
_RE_SETUP_METHOD = re.compile(r"\bprivate\s+fun\s+setup[A-Z]\w+\s*\(")

_FEATURE_MAP: list[tuple[str, str, str]] = [
    # ArkUI scrollable containers are `List() { ... }` / `Grid() { ... }` /
    # `WaterFlow() { ... }` (parens, not `List {`), iterated via LazyForEach OR
    # plain ForEach. Matching only `List\s*\{`/`LazyForEach` misses the common
    # `List() {` + `ForEach` idiom and false-flags the feature as missing.
    (r"RecyclerView|ListView|GridView",
     r"List\s*\(|Grid\s*\(|WaterFlow\s*\(|LazyForEach|ForEach\s*\(",
     "list_component"),
    (r"SharedPreferences", r"preferences\.|@ohos\.data\.preferences", "shared_preferences"),
    (r"Retrofit|OkHttp|HttpURLConnection", r"http\.request|fetch\(|rcp\.", "http_client"),
    (r"Room|SQLiteDatabase", r"relationalStore\.|@ohos\.data\.rdb", "database"),
    (r"Intent\s*\(", r"router\.|want\.", "navigation_intent"),
    (r"Fragment", r"NavDestination|@Component", "fragment_component"),
    (r"ViewModel", r"@Observed|@State.*class", "state_management"),
]


class FeatureCheckPlugin(EntropyPlugin):
    """Verify feature migration completeness via spec + pattern matching."""

    name = "feature_check"
    default_weight = 0.20
    judge_mode = "agentic"
    hard_weight = 0.0
    judge_prompt = """\
Verify HarmonyOS implementation is functionally equivalent to its Android source at the \
finest granularity possible — not just file existence, but every individual feature, option, \
handler, and UI element.
When an item carries an `android_source_file`/`android_source_files`, open it and \
compare surface-by-surface before judging OK — never pass an un-inspected pairing.

━━ Avoid false positives ━━
Before flagging anything missing/stubbed, search the whole HarmonyOS repo for the \
functional equivalent — it may use a different name/key/page/API than Android, and a \
missing literal Android construct is NOT a defect if the behaviour exists elsewhere. \
To confirm "missing", search by the on-screen string or a concept keyword, NOT the \
Android symbol name — the HarmonyOS equivalent is usually renamed. \
NEVER claim a HarmonyOS API doesn't exist without checking the SDK (it has \
@ohos.file.fs.utimes, photoAccessHelper, @ohos.userIAM, …). When unsure, don't flag.
Parity needs a source: flag a feature missing only if you can cite the Android \
file/symbol that implements it; if Android has no such code, it is NOT a defect.
Origin may live in a dependency: if no counterpart is in android_dir, scan declared \
deps (build.gradle / *.toml) for a shared/common library before concluding absent — \
do not infer "missing" from android_dir alone.
Form is not capability: a presentation difference (submenu vs separate buttons, \
dialog vs inline, merged vs separate page/file) is NOT a defect if the action is \
reachable — flag only an absent capability, not its layout.
Default callbacks are not stubs: a component's callback prop with a default \
(log-only/no-op) body is meant to be overridden by callers — judge the feature by \
where it actually runs, not the dormant default of a component nobody instantiates.

━━ How to read large files ━━
Many source files exceed 500 lines. NEVER skip a file because it is large.
Use Read with offset/limit to chunk through it:
- Read lines 1–500 first to enumerate all features/options/methods.
- Then Read lines 501–1000, 1001–1500, etc. until you reach the end.
- Repeat for both the Android file and the HarmonyOS file.
Stop chunking as soon as you hit the early-stop limit (see below).

━━ SET 1 — Feature-level checks ━━
`details.spec_results` lists spec features with:
- `matched_file`: HarmonyOS .ets path relative to cwd
- `android_source_files`: list of absolute paths to Android source files
- `status`: VERIFIED / PARTIAL / MISSING from line-count heuristic
- `ratio`: ArkTS/Android line completeness ratio

For each feature that has `android_source_files`:
1. Chunk-read the Android file completely. Build an exhaustive list of functional surfaces:
   - Interactive elements: every action the user can trigger (tap, swipe, long-press, toggle, input)
   - Data operations: every read/write to persistent storage, network, or local state
   - Navigation: every screen transition or back-stack operation
   - Lifecycle behaviour: what happens on appear/disappear/resume/destroy
2. Chunk-read the HarmonyOS matched_file. For each surface from step 1:
   - Present AND implemented with real logic → OK
   - Present as stub (TODO / throw / empty body, or does no real work — returns a \
constant, logs only, or writes a placeholder/sidecar instead of the real target) → issue
   - Absent entirely → issue
3. Report each missing or stubbed surface as a separate issue string.

For features WITHOUT `android_source_files` (no pre-resolved mapping): do NOT just \
inspect the ArkTS file. First actively locate the Android counterpart yourself under \
`details.android_dir`:
1. Derive search terms from the feature: its `name`, `feature_id`, and key class/concept \
nouns in the name.
2. Use Grep/Glob under `details.android_dir` to find candidate Android source files \
(*.kt / *.java / *.cpp / *.h) whose path or content matches those terms.
3. If you find a credible counterpart, chunk-read it and do the same surface-by-surface \
comparison as features WITH android_source_files.
4. ONLY if no credible Android counterpart can be found after searching, fall back to \
inspecting `matched_file` for stub patterns (throw / empty return / TODO-only bodies / \
missing @Entry).

━━ SET 2 — Page-level checks ━━
`details.page_results` lists Android Activity/Fragment → HarmonyOS page mappings:
- `harmony_ets_file`: HarmonyOS page path relative to cwd
- `android_source_file`: absolute path to Android source (when resolved)
- `harmony_ets_exists`: whether the HarmonyOS page file exists
- `android_setup_method_count` (optional): number of `private fun setupXxx()` in Android
  source — each is a discrete user-configurable option

For each page with `android_source_file`:
1. Chunk-read Android source completely. Enumerate all functional surfaces.
2. Chunk-read HarmonyOS file completely. Verify 1-to-1 coverage.
3. Flag every missing or stubbed surface as an issue.
Pages where `harmony_ets_exists` is false → treat all content as MISSING.

For pages with `android_setup_method_count >= 3`: each Android `setupXxx()` represents
one user-configurable option. For each, verify the ArkTS counterpart has: interactive UI
(not hardcoded), state loaded from storage (not a literal init value), and a write-back
handler. Flag any option missing any of the three — regardless of which storage API
Android uses underneath.

Also: from the Android menu resources (`res/menu*/*.xml` under android_dir), verify the
port implements each action — batch/multi-select especially (single-item ≠ batch). No menus → skip.

━━ Scoring ━━
Score 0.0–1.0 aggregated across both sets:
- 0.0–0.2: HarmonyOS logic matches Android — real API calls, data binding, handlers wired
- 0.2–0.5: structure present but some options/handlers missing or stubbed
- 0.5–0.8: files exist but significant portions are stubs or absent
- 0.8–1.0: stub skeletons or files completely absent"""

    def extract_work_items(
        self, score: float, details: dict[str, object]
    ) -> list[WorkItem]:
        items: list[WorkItem] = []
        for result in details.get("spec_results", []):
            if not isinstance(result, dict):
                continue
            status = result.get("status", "")
            version = result.get("version", "")
            ratio = float(result.get("ratio", 0.0)) if isinstance(result.get("ratio"), (int, float)) else 0.0
            # UNQUANTIFIED features (no Android counterpart to measure against) are
            # intentionally NOT turned into hard work-items here — the agentic judge
            # actively explores the Android repo for them and raises its own issues.
            if status in ("MISSING", "PARTIAL") and version != "SKIP":
                fid = result.get("feature_id", "?")
                name = result.get("name", "")
                matched = result.get("matched_file", "")
                android_lines = result.get("android_lines", 0)
                arkts_lines = result.get("arkts_lines", 0)
                sev: Literal["high", "medium", "low"] = (
                    "high" if ratio < 0.3
                    else "medium" if ratio < 0.7
                    else "low"
                )
                file_path = Path(matched) if matched else Path(".")
                if android_lines and arkts_lines is not None:
                    issue = (
                        f"V1 feature {fid}({name}) port progress {ratio:.0%} "
                        f"(Android {android_lines} lines → ArkTS {arkts_lines} lines)"
                    )
                else:
                    issue = f"V1 feature {fid}({name}) port progress {ratio:.0%} status={status}"
                items.append(WorkItem(
                    file=file_path,
                    issues=[issue],
                    suggested_skills=[],
                    severity=sev,
                ))
        for feat in details.get("pattern_features_missing", []):
            if isinstance(feat, str):
                items.append(WorkItem(
                    file=Path("."),
                    issues=[f"Pattern feature missing: {feat}"],
                    suggested_skills=[],
                    severity="medium",
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
        # Build source-mapping lookups:
        #   module_to_page: Android class name → ArkTS page name
        #   arkts_to_android: ArkTS page basename → Android source file path
        source_pages = parse_source_mapping(spec_dir) if spec_dir else []
        module_to_page: dict[str, str] = {}
        arkts_to_android: dict[str, Path] = {}
        android_index = _index_android_files(android_dir) if android_dir else {}
        for sp in source_pages:
            android_class = sp.extras.get("android_class", "")
            class_basename = android_class.split("/")[-1]  # MainActivity.kt
            class_name = class_basename.replace(".kt", "").replace(".java", "")
            if class_name:
                module_to_page[class_name] = sp.name
            if class_basename and class_basename in android_index:
                arkts_to_android[sp.name] = android_index[class_basename]

        # Layer 1: Spec-driven verification
        features = parse_feature_index(spec_dir) if spec_dir else []
        spec_results: list[dict[str, object]] = []
        v1_verified = 0
        v1_total = 0
        skip_count = 0

        v1_ratios: list[float] = []
        if features:
            for feat in features:
                if feat.version == "SKIP":
                    status = "VERIFIED"
                    matched_file = ""
                    ratio = 1.0
                    skip_count += 1
                else:
                    status, matched_file, ratio = _check_feature(
                        feat, harmony_dir, module_to_page, arkts_to_android,
                    )
                # Compute Android baseline lines for issue text (cheap; cached by OS).
                android_lines = 0
                arkts_lines_total = 0
                # Collect absolute Android source paths for agentic judge comparison.
                android_source_files: list[str] = []
                for of in feat.output_files:
                    cand = harmony_dir / "entry" / "src" / "main" / "ets" / of
                    if cand.exists():
                        with contextlib.suppress(OSError):
                            arkts_lines_total += len([
                                ln for ln in cand.read_text(
                                    encoding="utf-8", errors="ignore",
                                ).splitlines() if ln.strip()
                            ])
                    src = arkts_to_android.get(Path(of).stem)
                    if src and src.exists():
                        with contextlib.suppress(OSError):
                            android_lines += len([
                                ln for ln in src.read_text(
                                    encoding="utf-8", errors="ignore",
                                ).splitlines() if ln.strip()
                            ])
                        android_source_files.append(str(src.resolve()))
                spec_results.append({
                    "feature_id": feat.feature_id,
                    "name": feat.name,
                    "version": feat.version,
                    "status": status,
                    "matched_file": matched_file,
                    "ratio": ratio,
                    "android_lines": android_lines,
                    "arkts_lines": arkts_lines_total,
                    "android_source_files": android_source_files,
                })
                if feat.version == "V1":
                    v1_total += 1
                    if ratio is not None:
                        v1_ratios.append(ratio)
                    if status == "VERIFIED":
                        v1_verified += 1

        # Page-level mapping for agentic judge: Android Activity → HarmonyOS page
        # Built from the same source_pages used for arkts_to_android so no extra I/O.
        page_results: list[dict[str, object]] = []
        for sp in source_pages:
            android_src = arkts_to_android.get(sp.name)
            ets_rel = sp.target_file  # e.g. "pages/MainPage.ets"
            ets_abs = harmony_dir / "entry" / "src" / "main" / "ets" / ets_rel
            entry: dict[str, object] = {
                "page_id": sp.page_id,
                "page_name": sp.name,
                "android_class": sp.extras.get("android_class", ""),
                "harmony_ets_file": ets_rel,
                "harmony_ets_exists": ets_abs.exists(),
            }
            if android_src and android_src.exists():
                entry["android_source_file"] = str(android_src.resolve())
                # Count `private fun setupXxx()` methods as a proxy for "number of
                # discrete user-configurable options" in this page.  This metric is
                # app-agnostic: any Android settings/options Activity uses this
                # pattern regardless of which storage API it uses underneath.
                # The judge uses this count to decide how deeply to audit the ArkTS
                # counterpart — pages with many setupXxx methods need option-by-option
                # verification, not just a line-count check.
                try:
                    android_text = android_src.read_text(encoding="utf-8", errors="ignore")
                    setup_count = len(_RE_SETUP_METHOD.findall(android_text))
                    if setup_count > 0:
                        entry["android_setup_method_count"] = setup_count
                except OSError:
                    pass
            page_results.append(entry)

        # Layer 2: Pattern-based fallback (per-file early-exit, no full concatenation)
        pattern_results = _pattern_check(android_dir, harmony_dir)

        # Scoring: spec-driven uses continuous mean(1 - ratio) when ratios exist.
        if v1_ratios:
            spec_score = 1.0 - (sum(v1_ratios) / len(v1_ratios))
            pattern_score: float = pattern_results["score"]
            score = spec_score * 0.7 + pattern_score * 0.3
        elif pattern_results["total_required"] > 0:
            score = pattern_results["score"]
        else:
            score = 0.0

        details: dict[str, object] = {
            "spec_driven": bool(features),
            "spec_results": spec_results,
            "page_results": page_results,
            "pattern_features_missing": pattern_results["missing"],
        }
        # Expose android_dir as absolute path so the agentic judge can explore
        # sibling Android files for additional context when needed.
        if android_dir and android_dir.exists():
            details["android_dir"] = str(android_dir.resolve())

        return score, details


def _check_feature(
    feat: FeatureEntry,
    harmony_dir: Path,
    module_to_page: dict[str, str] | None = None,
    arkts_to_android: dict[str, Path] | None = None,
) -> tuple[str, str, float | None]:
    """Actively check feature implementation.

    Returns (status, matched_file, completion_ratio) where status is
    VERIFIED / PARTIAL / MISSING / UNQUANTIFIED, matched_file is the
    relative .ets path (or ""), and completion_ratio is 0.0–1.0
    line-based completeness, or None when no Android counterpart exists.
    """
    arkts_to_android = arkts_to_android or {}

    # Try explicit output_files first
    if feat.output_files:
        status, ratio = _check_output_files(feat, harmony_dir, arkts_to_android)
        matched = feat.output_files[0] if status != "MISSING" else ""
        return status, matched, ratio

    # Try to infer target file from module via source-mapping
    module_name = feat.extras.get("module", "")
    if module_name and module_to_page:
        arkts_page = module_to_page.get(module_name)
        if arkts_page:
            rel = f"pages/{arkts_page}.ets"
            candidate = harmony_dir / "entry" / "src" / "main" / "ets" / rel
            if candidate.exists():
                s, r = _assess_file_quality(candidate, arkts_to_android.get(arkts_page))
                return s, rel, r

    pages_dir = harmony_dir / "entry" / "src" / "main" / "ets" / "pages"

    # Content-based search: scan .ets file contents for feature_id or name keywords
    keywords = [feat.feature_id.lower()]
    name_parts = re.split(r"[\s_-]+", feat.name.lower())
    keywords.extend(p for p in name_parts if p)

    if pages_dir.exists():
        for ets_file in pages_dir.rglob("*.ets"):
            try:
                content = ets_file.read_text(encoding="utf-8", errors="ignore").lower()
            except OSError:
                continue
            if any(kw in content for kw in keywords):
                rel = ets_file.relative_to(
                    harmony_dir / "entry" / "src" / "main" / "ets",
                ).as_posix()
                s, r = _assess_file_quality(ets_file, arkts_to_android.get(ets_file.stem))
                return s, rel, r

    # Filename-based fuzzy match as fallback
    meaningful = [p for p in name_parts if len(p) > 2]
    if meaningful and pages_dir.exists():
        for ets_file in pages_dir.rglob("*.ets"):
            stem = ets_file.stem.lower()
            if any(p in stem for p in meaningful):
                rel = ets_file.relative_to(
                    harmony_dir / "entry" / "src" / "main" / "ets",
                ).as_posix()
                s, r = _assess_file_quality(ets_file, arkts_to_android.get(ets_file.stem))
                return s, rel, r

    # Last resort: keyword presence cannot quantify completeness — no Android
    # counterpart is available to compare against, so we cannot fabricate a ratio.
    return "UNQUANTIFIED", "", None


def _harmony_keyword_exists(harmony_dir: Path, keywords: list[str]) -> bool:
    """Return True as soon as any .ets/.ts file contains one of the keywords."""
    for ext in ("*.ets", "*.ts"):
        for f in harmony_dir.rglob(ext):
            if should_skip_path(f, harmony_dir):
                continue
            try:
                content = f.read_text(encoding="utf-8", errors="ignore").lower()
            except OSError:
                continue
            if any(kw in content for kw in keywords):
                return True
    return False


def _check_output_files(
    feat: FeatureEntry,
    harmony_dir: Path,
    arkts_to_android: dict[str, Path] | None = None,
) -> tuple[str, float | None]:
    """Check feature by its declared output files — return (status, avg_ratio).

    avg_ratio is None when ALL files are unquantified (no Android counterpart).
    File-not-exists cases always contribute ratio=0.0 (real MISSING signal).
    """
    arkts_to_android = arkts_to_android or {}
    real_ratios: list[float] = []   # non-None ratios from _assess_file_quality
    all_statuses: list[str] = []

    for output_file in feat.output_files:
        candidate = harmony_dir / "entry" / "src" / "main" / "ets" / output_file
        if not candidate.exists():
            # Fuzzy: try case-insensitive match in the same directory
            parent = candidate.parent
            if parent.exists():
                target_stem = candidate.stem.lower()
                for f in parent.iterdir():
                    if f.stem.lower() == target_stem and f.suffix == ".ets":
                        candidate = f
                        break
                else:
                    real_ratios.append(0.0)
                    all_statuses.append("MISSING")
                    continue
            else:
                real_ratios.append(0.0)
                all_statuses.append("MISSING")
                continue
        s, r = _assess_file_quality(candidate, arkts_to_android.get(candidate.stem))
        all_statuses.append(s)
        if r is not None:
            real_ratios.append(r)

    if not all_statuses:
        return "MISSING", 0.0
    # If no real (non-None) ratios at all → every file was unquantified
    if not real_ratios:
        return "UNQUANTIFIED", None
    avg_ratio = sum(real_ratios) / len(real_ratios)
    # Judge VERIFIED only over the quantified files: an UNQUANTIFIED sibling
    # (file exists but has no Android counterpart) must not block promotion of
    # an otherwise fully-verified feature.
    quantified_statuses = [s for s in all_statuses if s != "UNQUANTIFIED"]
    if avg_ratio >= 0.55 and quantified_statuses and all(
        s == "VERIFIED" for s in quantified_statuses
    ):
        return "VERIFIED", avg_ratio
    if avg_ratio < 0.05:
        return "MISSING", avg_ratio
    return "PARTIAL", avg_ratio


def _assess_file_quality(
    file_path: Path,
    android_src_path: Path | None = None,
) -> tuple[str, float | None]:
    """Assess a single .ets file and return (status, completion_ratio).

    completion_ratio is a 0.0–1.0 blended score: 40% line-count ratio +
    60% functional signal density (events / state vars / list items /
    string resources / nav jumps / dialogs vs Android equivalents).
    Returns ("UNQUANTIFIED", None) when there is no Android counterpart to
    compare against — callers must NOT include None ratios in aggregations.
    """
    from ecat.entropy.plugins.a2h_verify._signal_density import (
        combined_completeness,
        density_score,
    )

    content = file_path.read_text(encoding="utf-8", errors="ignore")
    arkts_lines = [ln for ln in content.splitlines() if ln.strip()]
    arkts_count = len(arkts_lines)

    if arkts_count < 5:
        return "MISSING", 0.0

    has_component = "@Component" in content or "@Entry" in content
    has_build = "build()" in content or "build ()" in content
    has_real_ui = bool(re.search(
        r"\b(Column|Row|List|Grid|Tabs|Stack|Navigation|Text|Image|Button)\s*[({]",
        content,
    ))
    todo_count = len(re.findall(r"\bTODO\b", content, re.IGNORECASE))

    if not has_component and not has_build:
        return "MISSING", 0.0

    android_count = 0
    if android_src_path is not None and android_src_path.exists():
        try:
            android_text = android_src_path.read_text(encoding="utf-8", errors="ignore")
            android_count = len([ln for ln in android_text.splitlines() if ln.strip()])
        except OSError:
            pass

    if android_count > 0:
        line_ratio = min(1.0, arkts_count / max(1.0, android_count * 0.5))
        density, _ = density_score(android_src_path, file_path)
        ratio: float = combined_completeness(line_ratio, density)
    else:
        # No Android counterpart to compare against — cannot quantify completion.
        # Return immediately so we don't fabricate a fake ratio.
        return "UNQUANTIFIED", None

    if ratio >= 0.55 and has_real_ui and todo_count <= 2:
        status = "VERIFIED"
    elif ratio >= 0.20:
        status = "PARTIAL"
    else:
        status = "PARTIAL" if has_component else "MISSING"
    return status, ratio


def _index_android_files(android_dir: Path) -> dict[str, Path]:
    """basename (e.g. MainActivity.kt) → full Path; for source-mapping resolution."""
    index: dict[str, Path] = {}
    if not android_dir.exists():
        return index
    for ext in ("*.kt", "*.java"):
        for f in android_dir.rglob(ext):
            index.setdefault(f.name, f)
    return index


def _android_has_pattern(android_dir: Path, pattern: re.Pattern) -> bool:  # type: ignore[type-arg]
    """Return True as soon as any Android source file matches pattern (early exit)."""
    for ext in ("*.java", "*.kt"):
        for f in android_dir.rglob(ext):
            try:
                if pattern.search(f.read_text(encoding="utf-8", errors="ignore")):
                    return True
            except OSError:
                pass
    return False


def _harmony_has_pattern(harmony_dir: Path, pattern: re.Pattern) -> bool:  # type: ignore[type-arg]
    """Return True as soon as any HarmonyOS source file matches pattern (early exit)."""
    for ext in ("*.ets", "*.ts"):
        for f in harmony_dir.rglob(ext):
            if should_skip_path(f, harmony_dir):
                continue
            try:
                if pattern.search(f.read_text(encoding="utf-8", errors="ignore")):
                    return True
            except OSError:
                pass
    return False


def _pattern_check(
    android_dir: Path | None,
    harmony_dir: Path,
) -> dict[str, Any]:
    if android_dir is None or not android_dir.exists():
        return {"score": 0.0, "total_required": 0, "missing": [], "found": []}

    missing: list[str] = []
    found: list[str] = []

    for android_pat_str, harmony_pat_str, feature in _FEATURE_MAP:
        android_pat = re.compile(android_pat_str)
        harmony_pat = re.compile(harmony_pat_str)
        if _android_has_pattern(android_dir, android_pat):
            if _harmony_has_pattern(harmony_dir, harmony_pat):
                found.append(feature)
            else:
                missing.append(feature)

    total = len(missing) + len(found)
    score = (len(missing) / total) if total > 0 else 0.0
    return {"score": score, "total_required": total, "missing": missing, "found": found}
