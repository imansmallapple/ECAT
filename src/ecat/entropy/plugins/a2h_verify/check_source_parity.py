"""source_parity —— Android↔HarmonyOS 源码对等度检查（CHECK-12，硬检查）。

跨 spec/baseline 之外，按文件名 diff Android 工程根目录与 Harmony 工程根目录，
捕捉"Android 有这个文件，Harmony 没有"的整批缺失——这类漏迁只看 spec 列表
是发现不了的。

按缺失文件的类型分四种动作建议：

  1. **copy**   —— 纯 C++ 算法/库文件（无 JNI、无 Android NDK 头依赖）。
                  WorkItem 文本里直接写 "Copy verbatim from <android_path>"，
                  让 generator 知道这种文件不需要重写，cp 即可。
  2. **translate** —— JNI 绑定层文件（`<jni.h>` / `JNIEnv*` / `Java_*`）。
                      不能 copy，但语义可以 1:1 翻译为 NAPI。
  3. **port**   —— Kotlin / Java 源文件。提示 generator 落到对应 ArkTS 位置。
  4. **wire**   —— Harmony 侧"有同名文件、却没人 import"的死副本（见下）。

「存在性」之外的三层语义校验（避免"文件在=已迁移"的假象）：

  * **可达性闸门**：从框架入口（pages/、*ability/、workers/）和模块再导出
    桶（index.ets）出发,沿相对 import 建图。只有可达的 ArkTS 文件才算"活的"
    对等实现；匹配到同名但不可达的文件 → orphan_matched（动作 wire），不再
    把分数刷高。Native（.cpp/.h）由 CMake 接线、非 import，故不走此闸门。
  * **纯孤儿**：不可达且不对应任何 Android 文件的 ArkTS 文件 = 死代码
    （orphan_files），单独报出。
  * **Native 接线**：ETS↔.so↔注册三角。ETS 调用、但没有任何 .cpp 注册的符号
    （napi_unregistered，运行期会抛）；以及函数体打 "not (yet) implemented"
    的桩（native_stub_funcs，注册了但空实现）。

跨应用通用：仅基于文件扩展名、JNI/NDK 标准头检测、Activity/Fragment/Page
常用后缀规范化、HarmonyOS 标准入口约定与 import/.so 语法，不绑业务概念；
忽略模式经 config 配置。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ecat.entropy.base import EntropyPlugin
from ecat.entropy.plugins.a2h_verify.check_crash_risk import _blank_comments
from ecat.gradient.schema import WorkItem

_NATIVE_EXTS = frozenset((".cpp", ".cc", ".cxx", ".c", ".h", ".hpp", ".hh"))
_JVM_EXTS = frozenset((".kt", ".java"))
# Text-format languages whose semantics carry verbatim between Android and
# HarmonyOS — shader source (GLSL/HLSL/Metal/OpenCL), protobuf schemas. These
# go through the "copy" action: byte-identical `cp`, no rewrite.
_SHAREABLE_TEXT_EXTS = frozenset((
    ".glsl", ".vert", ".frag", ".comp", ".tesc", ".tese", ".geom",
    ".hlsl", ".metal", ".cl", ".proto",
))
_HARMONY_SRC_EXTS = frozenset((".ets", ".ts", ".js"))

# Android source directories that should never be considered "missing in Harmony".
# Build/test/generated artifacts are obvious. The `samples` / `demo` / `example`
# branches are the Android-Studio standard convention for sub-modules that ship
# tutorials or runtime-validation utilities — they are demo code and must NOT
# be forced into a production HarmonyOS port. Skipping them prevents an
# expensive "port → orphan → delete → port" oscillation when generator
# correctly refuses to wire debug-only classes into production paths.
_ANDROID_SKIP_DIR_NAMES = frozenset((
    "build", ".gradle", ".idea", "test", "androidTest", "generated",
    "intermediates", "outputs", "captures",
    "samples", "sample", "demo", "demos", "example", "examples",
))

# Stems that are pure build-system artefacts; not real source to port.
_GENERATED_STEMS = frozenset(("R", "BuildConfig", "Manifest"))

# Android UI/threading/persistence framework glue that has NO standalone file
# counterpart in idiomatic ArkTS — demanding a 1:1 `.ets` for these only
# manufactures orphan dead code (the port→orphan→wire churn that never
# converges), because their behaviour is absorbed elsewhere or covered by a
# different dimension:
#   * Adapter / ViewHolder / *Binding (RecyclerView, ViewBinding, DataBinding)
#       → list rendering lives INSIDE the page as ForEach/LazyForEach and ArkTS
#         has no ViewBinding; the behaviour is verified by feature_check /
#         page_audit, not by a same-named file.
#   * AsyncTask  → becomes a Promise/async or a Worker; no file mapping.
#   * Room Database / Dao  → the persistence layer is checked at table/key level
#         by `data_layer_parity`, so a same-named file here is redundant.
#   * Kotlin extension-function files (the `extensions/` package convention —
#         `String.kt`, `Context.kt`, `Activity.kt`, … named after the RECEIVER
#         type) → ArkTS has no per-type extension file; the helpers fold into
#         ordinary utils. A 1:1 `String.ets` / `Int.ets` is meaningless.
# Detected by BOTH the Android class-name suffix and the conventional Android
# package-directory name (apps that follow neither still get caught by the
# other). Both are framework conventions — not business names — so they
# generalize across Android apps, and both are extended (never shrunk) per
# project via config `source_parity.arch_only_suffixes` / `.arch_only_dirs`.
# Scoped to JVM (.kt/.java) only: native/shader files don't follow these
# conventions and a `foo_adapter.cpp` may be a genuine native port target.
_ARCH_ONLY_STEM_SUFFIXES = frozenset((
    "adapter", "viewholder", "binding", "asynctask",
    "diffcallback", "itemdecoration", "itemtouchhelper", "dao", "database",
    # Callback/observer interface contracts → in ArkTS these are inline function
    # types (`(x: T) => void`), never a standalone file/symbol.
    "listener", "callback", "observer",
))
_ARCH_ONLY_DIR_NAMES = frozenset((
    "adapters", "asynctasks", "databases", "dao", "extensions",
))


def _is_arch_only_jvm(
    f: Path,
    rel_parts: set[str],
    arch_suffixes: frozenset[str],
    arch_dirs: frozenset[str],
) -> bool:
    """True if *f* is Android framework glue with no standalone ArkTS file
    counterpart (adapter / view-holder / *binding / async-task / Room db/dao).

    JVM-only by design (see `_ARCH_ONLY_STEM_SUFFIXES`). Matches on a
    conventional package directory OR a class-name suffix; both signal sets are
    caller-supplied so config can extend them per project.
    """
    if f.suffix.lower() not in _JVM_EXTS:
        return False
    if rel_parts & arch_dirs:
        return True
    stem = f.stem.lower()
    return any(
        stem.endswith(suf) and len(stem) > len(suf) for suf in arch_suffixes
    )


# Android-only UI/image libraries with NO HarmonyOS counterpart. A file that
# *implements one of these libraries' extension points* (a Glide decoder /
# transcoder / module, a Picasso transformation, a ViewPager PageTransformer,
# a subsampling-scale-image-view region decoder, …) is pure integration glue:
# on HarmonyOS the capability comes from @ohos.multimedia.image / the system
# image loader, so the glue class has nothing to port to. Generic across
# Android apps; extended per project via config `source_parity.ui_lib_imports`.
_ANDROID_ONLY_UI_LIB_IMPORTS = (
    "com.bumptech.glide",                  # Glide
    "com.squareup.picasso",                # Picasso
    "com.facebook.drawee", "com.facebook.imagepipeline",   # Fresco
    "com.nostra13.universalimageloader",   # Universal Image Loader
    "com.davemorrissey.labs.subscaleview", # subsampling-scale-image-view
    "jp.wasabeef",                         # glide-transformations
    "androidx.viewpager.widget",           # ViewPager (PageTransformer / PagerAdapter)
    "coil.", "coil3.",                     # Coil
)
# The stable base classes / interfaces those libraries expose as extension
# points. Subclassing one of these IS the glue — and crucially distinguishes it
# from an Activity/Fragment that merely *uses* the library to load an image
# (that extends Activity/Fragment and must still be ported). Generic names.
_UI_LIB_EXTPOINT_BASES = (
    "AppGlideModule", "LibraryGlideModule", "GlideModule",
    "ResourceDecoder", "ResourceTranscoder", "BitmapTransformation",
    "RequestListener", "ModelLoader", "DataFetcher",
    "ImageDecoder", "ImageRegionDecoder",   # subsampling-scale-image-view
    "Transformation", "RequestHandler",     # Picasso
    "PageTransformer",                      # androidx ViewPager
)
# Supertype list of a class/object declaration: `class Foo(args) : Base1, Base2 {`.
_CLASS_BASE_RE = re.compile(r"\b(?:class|object)\s+\w+[^:{]*:\s*([^{]+)")


def _is_ui_lib_glue(text: str, lib_imports: tuple[str, ...]) -> bool:
    """True if *text* implements an Android-only UI/image-library extension point.

    Requires BOTH a library import AND a class that subclasses one of the
    library's extension-point base types — so a UI unit that only *uses* the
    library (extends Activity/Fragment) is never mistaken for glue.
    """
    if not any(p in text for p in lib_imports):
        return False
    for supertypes in _CLASS_BASE_RE.findall(text):
        if any(re.search(rf"\b{b}\b", supertypes) for b in _UI_LIB_EXTPOINT_BASES):
            return True
    return False

# JNI / NDK detection patterns. Used to classify a C++ file as copy-able
# (pure algorithm) vs translate-only (binding layer).
_JNI_PATTERNS = re.compile(
    r"\bJNIEnv\b|\bJNIEXPORT\b|\bJNICALL\b|^Java_\w+\s*\(|#include\s*[<\"]jni\.h[>\"]",
    re.MULTILINE,
)
_ANDROID_NDK_HEADER = re.compile(
    r"#include\s*<android/(log|asset_manager|native_window|bitmap|"
    r"hardware_buffer|hardware_buffer_jni|native_window_jni|configuration|"
    r"input|sensor|api-level|file_descriptor_jni)\.h>",
    re.MULTILINE,
)

# Common Android source-class suffixes that don't carry over verbatim to
# HarmonyOS file names (Activity → Page/Ability, etc.). Normalising lets
# `MainActivity.kt` match `MainPage.ets` by stem.
_ANDROID_SUFFIXES = ("activity", "fragment", "service", "receiver", "provider", "application")
# Harmony file-name suffixes stripped during normalization. Beyond the
# platform suffixes (Activity → Page/Ability), ArkTS ports routinely rename a
# Kotlin data class `Foo` to `FooModel` (also `Entity`/`Dto`/`Bean`), so strip
# those too — lets `Directory.kt` line up with `DirectoryModel.ets`. Stripping
# only WIDENS matching (fewer "missing" flags); it can add a false negative
# (the FP-averse direction this plugin favours) but never a new false positive.
_HARMONY_SUFFIXES = (
    "page", "ability", "component", "view",
    "model", "entity", "dto", "bean",
)


class SourceParityPlugin(EntropyPlugin):
    """Diff Android↔Harmony source file inventory by basename + classify gaps."""

    name = "source_parity"
    default_weight = 0.10
    judge_mode = "agentic"
    # Hard check is a STEM-based prefilter that over-reports (renamed/folded files);
    # the agentic judge prunes those false positives and owns the score.
    hard_weight = 0.0
    judge_prompt = """\
Verify the **source_parity** dimension. `details.missing` lists Android source files with \
no same-stem HarmonyOS file — a STEM prefilter that OVER-reports, because the port often \
RENAMES (`XxxPreferenceFragment`→`XxxSettingsPage`, `XxxFragment`→`XxxSheet`/component) or \
FOLDS a class into another file.

For each candidate, search the HarmonyOS `entry/src/main/ets` tree by CONCEPT (purpose / \
renamed name / key symbols), not just the stem. Treat it as missing ONLY if there is \
genuinely NO HarmonyOS counterpart (not renamed, not folded) — prefer a miss when unsure.

Output ONLY {"score": <0..1>, "issues": [...]}: score = genuinely-missing / total \
candidates; each issue `<harmony_target_hint>: create by porting <android_path> — no \
HarmonyOS counterpart`. If every candidate has a counterpart, score 0.0."""

    def extract_work_items(
        self, score: float, details: dict[str, object]
    ) -> list[WorkItem]:
        gaps = [g for g in details.get("missing", []) if isinstance(g, dict)]
        # Agentic backstop: keep only candidates the judge CONFIRMED genuinely missing
        # (it prunes renamed/folded false positives). Fall back to all when no verdict.
        verdict = details.get("agentic_verdict")
        if isinstance(verdict, dict) and verdict.get("issues"):
            blob = " ".join(str(i) for i in verdict["issues"]).lower()  # type: ignore[union-attr]
            # Whole-word stem match: a confirmed "PlayerWidget" must not re-admit "Player".
            gaps = [g for g in gaps if (s := str(g.get("stem", "")).lower())
                    and re.search(rf"\b{re.escape(s)}\b", blob)]
        items: list[WorkItem] = []
        for gap in gaps:
            action = str(gap.get("action", "port"))
            android_path = str(gap.get("android_path", ""))
            target_hint = str(gap.get("harmony_target_hint", ""))

            skills: list[str] = []
            if action == "copy":
                issue = (
                    f"Shareable source — `cp \"{android_path}\" "
                    f"\"{target_hint}\"`. Add to CMakeLists.txt SRC if "
                    f".cpp/.c/.h. Use cp, not Write."
                )
                sev = "high"
            elif action == "translate":
                issue = (
                    f"JNI binding — translate to NAPI at {target_hint} "
                    f"(Java_* → *_impl, JNIEnv* → napi_env)."
                )
                sev = "high"
            elif action == "header-swap":
                issue = (
                    f"Android NDK headers — `cp \"{android_path}\" "
                    f"\"{target_hint}\"`, then Edit-swap "
                    f"<android/log.h>→<hilog/log.h>, ANativeWindow→OH "
                    f"XComponent. No Write rewrite."
                )
                sev = "medium"
            elif action == "wire":
                issue = (
                    f"Counterpart {target_hint} exists but is unreachable "
                    f"(orphan/dead) — no entry page, ability, or module "
                    f"Index.ets imports it. Wire it into the import graph or "
                    f"delete if superseded by another file."
                )
                sev = "medium"
            else:  # port (Kotlin/Java → ArkTS)
                issue = (
                    f"Port {android_path} → "
                    f"{target_hint or 'entry/src/main/ets/'}. {self.FIX_HINT}"
                )
                sev = "medium"
                skills = []

            items.append(WorkItem(
                file=Path(target_hint) if target_hint else Path("."),
                issues=[issue],
                suggested_skills=skills,
                severity=sev,  # type: ignore[arg-type]
                lines=None,
            ))

        # Dead-code orphans (no Android counterpart, imported by nobody).
        for orphan in details.get("orphan_files", []):
            items.append(WorkItem(
                file=Path(str(orphan)),
                issues=[
                    "Orphan file — not imported from any entry page, ability, "
                    "or module Index.ets, and matches no Android source. Wire "
                    "it in or delete it (dead code inflates apparent coverage)."
                ],
                suggested_skills=[],
                severity="low",
                lines=None,
            ))

        # ETS calls a native symbol that no .cpp registers → runtime throw.
        for sym in details.get("napi_unregistered", []):
            items.append(WorkItem(
                file=Path("."),
                issues=[
                    f"ETS calls native `{sym}` but no .cpp registers it — "
                    f"register + implement the NAPI or the call throws at "
                    f"runtime."
                ],
                suggested_skills=[],
                severity="high",
                lines=None,
            ))

        # Native stub bodies (present but hollow).
        for stub in details.get("native_stub_funcs", []):
            if not isinstance(stub, dict):
                continue
            items.append(WorkItem(
                file=Path(str(stub.get("file", "."))),
                issues=[
                    f"Native stub — `{stub.get('snippet', '')}`. Function is "
                    f"registered/defined but not implemented."
                ],
                suggested_skills=[],
                severity="high",
                lines=[int(stub["line"])] if "line" in stub else None,
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
        if android_dir is None or not android_dir.exists():
            return 0.0, {"skipped": "android_dir not provided or missing"}

        cfg = (config or {}).get("source_parity", {}) or {}
        if not isinstance(cfg, dict):
            cfg = {}
        ignore_patterns: list[str] = [
            str(p) for p in cfg.get("ignore_patterns", []) if isinstance(p, str)
        ]
        # Per-project extensions (union, never shrink) to the built-in Android
        # framework-glue filter — for apps whose adapters/tasks/db follow a
        # non-standard suffix or package name.
        arch_suffixes = _ARCH_ONLY_STEM_SUFFIXES | {
            str(s).lower().lstrip("*").lstrip(".")
            for s in cfg.get("arch_only_suffixes", []) if isinstance(s, str)
        }
        arch_dirs = _ARCH_ONLY_DIR_NAMES | {
            str(d).lower() for d in cfg.get("arch_only_dirs", []) if isinstance(d, str)
        }
        ui_lib_imports = _ANDROID_ONLY_UI_LIB_IMPORTS + tuple(
            str(p) for p in cfg.get("ui_lib_imports", []) if isinstance(p, str)
        )
        # Vendored / third-party directory names to skip on BOTH sides.
        # Auto-detected via upstream-library markers (LICENSE / COPYING /
        # Doxyfile) plus version-suffix naming (`cryptopp890`, `boost_1_82_0`,
        # `openssl-3.2`). Vendored libraries are intentional drop-ins — their
        # internal stubs (e.g. Crypto++ virtual `NotImplemented` markers) are
        # upstream design, not project bugs. Skipping covers:
        #   * scanning Android for files to port (`_scan_android`)
        #   * scanning Harmony for stubs (false-positive cluster source)
        #   * scanning Harmony for registered NAPI symbols
        #   * scanning Harmony for orphan ArkTS / reachable graph
        # Auto-detected set unions with the optional `vendor_dirs` config
        # override (for projects whose vendored libraries don't carry any
        # standard marker — rare but possible).
        vendor_dirs: set[str] = _detect_vendor_dirs(harmony_dir)
        if android_dir is not None:
            vendor_dirs |= _detect_vendor_dirs(android_dir)
        vendor_dirs |= {
            str(d) for d in cfg.get("vendor_dirs", []) if isinstance(d, str)
        }

        # 1. Inventory Android sources
        android_files = _scan_android(
            android_dir, vendor_dirs, ignore_patterns,
            arch_suffixes, arch_dirs, ui_lib_imports,
        )

        # 1b. Filter out stems that source-parity.md marks as action=delete.
        # These are Android debug utilities (e.g. *Sample, *Verifier) that were
        # intentionally never ported. Reading the spec baseline here makes the
        # generator's source-parity.md edits take effect on the entropy score.
        delete_stems = _load_delete_stems_from_spec(spec_dir)
        if delete_stems:
            android_files = [f for f in android_files if f.stem.lower() not in delete_stems]

        # 2. Index Harmony by normalized stem so we can answer "does any file
        #    with this stem exist?" cheaply.
        harmony_index = _build_harmony_stem_index(harmony_dir, vendor_dirs)

        # 2b. Reachability: a counterpart that no entry page / ability / module
        #     Index.ets imports is dead code. Reachability is still computed to
        #     REPORT orphan/dead counterparts, but — FP-averse policy — an
        #     existing counterpart counts as ported even if currently unreachable
        #     (don't flag "created but not yet wired" as missing).
        reachable = _collect_reachable_ets(harmony_dir, vendor_dirs)
        # Every top-level symbol Harmony declares, so a class ported INTO another
        # file (no same-named .ets) still counts as present. App-agnostic.
        harmony_symbols = _collect_harmony_symbols(harmony_dir, vendor_dirs)

        # 3. Diff — bias toward UNDER-reporting: flag an Android file missing ONLY
        #    when its concept is found NOWHERE in Harmony (no same-stem file, no
        #    same-named symbol, not folded into a present base type). All three
        #    signals are generic (filename / symbol / class-header parsing) — no
        #    business names — so they generalize across apps.
        missing: list[dict[str, Any]] = []
        present: list[str] = []
        orphan_matched: list[dict[str, Any]] = []
        for af in android_files:
            stem_key = _normalize_stem(af.stem)
            matches = harmony_index.get(stem_key, [])
            # (a) a same-stem Harmony file exists → ported. Reachability no longer
            #     gates this; an orphan/dead counterpart is reported (advisory),
            #     not counted as missing.
            if matches:
                present.append(str(af))
                arkts_matches = [
                    m for m in matches if m.suffix.lower() in _HARMONY_SRC_EXTS
                ]
                native_match = any(
                    m.suffix.lower() in _NATIVE_EXTS
                    or m.suffix.lower() in _SHAREABLE_TEXT_EXTS
                    for m in matches
                )
                if arkts_matches and not native_match and not any(
                    m.resolve() in reachable for m in arkts_matches
                ):
                    orphan_matched.append({
                        "android_path": str(af),
                        "stem": af.stem,
                        "harmony_file": str(arkts_matches[0]),
                    })
                continue
            # (b) the class is ported as a SYMBOL into another file (data class
            #     merged into a consumer/aggregator, e.g. FilterItem).
            if af.stem.lower() in harmony_symbols:
                present.append(str(af))
                continue
            # (c) a trivial data/marker class folded into a present base type
            #     (sealed-subclass → discriminated field, e.g. ThumbnailSection).
            if _is_folded_into_present_base(af, harmony_index, harmony_symbols):
                present.append(str(af))
                continue
            action, hint = _classify(af, harmony_dir)
            missing.append({
                "android_path": str(af),
                "stem": af.stem,
                "action": action,
                "harmony_target_hint": hint,
            })

        # 3b. Pure orphans: harmony ArkTS files imported by nobody that also
        #     match no Android source — dead code that silently inflates the
        #     apparent port surface.
        android_stems = {_normalize_stem(af.stem) for af in android_files}
        orphan_files = _collect_orphan_files(
            harmony_dir, reachable, android_stems, vendor_dirs
        )

        # 3c. Native wiring: ETS↔.so↔registration triangle. Symbols the ETS
        #     layer calls that no .cpp registers will throw at runtime; native
        #     functions logging "not (yet) implemented" are present-but-hollow.
        registered = _collect_registered_napi_names(harmony_dir, vendor_dirs)
        called = _collect_ets_native_calls(harmony_dir, vendor_dirs)
        napi_unregistered = sorted(called - registered)
        native_stub_funcs = _collect_native_stubs(harmony_dir, vendor_dirs)

        # 4. Score: baseline = first-iter missing count, persisted to
        #    <runs_dir>/entropy_baselines.json. score = current / baseline,
        #    so each ported file moves the dial linearly toward 0.
        n_missing = len(missing)
        runs_dir = (
            Path(str(config["runs_dir"]))
            if config and config.get("runs_dir") else None
        )
        baseline = _load_or_init_baseline(
            runs_dir, "source_parity_missing", n_missing
        )
        score = min(1.0, n_missing / baseline) if baseline > 0 else 0.0

        # Action breakdown for fast triage in reports.
        by_action: dict[str, int] = {}
        for g in missing:
            by_action[g["action"]] = by_action.get(g["action"], 0) + 1

        details: dict[str, object] = {
            "android_total_scanned": len(android_files),
            "harmony_indexed_stems": len(harmony_index),
            "reachable_ets_count": len(reachable),
            "missing_count": n_missing,
            "baseline_missing_count": baseline,
            "present_count": len(present),
            "by_action": by_action,
            # Cap the per-file list in details to keep gradient.json compact;
            # severity/action breakdown above is the headline signal.
            "missing": missing[:60],
            "missing_truncated": n_missing > 60,
            # Present-but-dead ArkTS counterparts (orphan duplicates).
            "orphan_matched": orphan_matched[:60],
            "orphan_matched_count": len(orphan_matched),
            # Pure dead code (no Android counterpart, imported by nobody).
            "orphan_files": orphan_files[:60],
            "orphan_count": len(orphan_files),
            # Native wiring gaps.
            "napi_unregistered": napi_unregistered[:60],
            "napi_unregistered_count": len(napi_unregistered),
            "native_stub_funcs": native_stub_funcs[:60],
            "native_stub_count": len(native_stub_funcs),
        }
        return score, details


# ── helpers ─────────────────────────────────────────────────────────────────


# Standard upstream-library marker filenames. A directory containing any of
# these at its top level is overwhelmingly likely a third-party drop-in
# (Crypto++, Boost, OpenSSL, SQLite amalgamation, …) and must not be scanned
# for stubs / orphans. Case-insensitive match. Generic across libraries.
_VENDOR_MARKER_FILES = frozenset((
    "license", "license.txt", "license.md", "license.rst",
    "copying", "copying.txt", "copying.md",
    "copyright", "copyright.txt",
    "doxyfile", "doxyfile.in",
))

# Directory-name suffix indicating a versioned upstream tarball drop. Examples:
#   cryptopp890        → trailing version digits (>=2 digits to avoid `r2`-style noise)
#   boost_1_82_0       → underscore-separated dotted version
#   openssl-3.2.1      → dash-separated dotted version
#   sqlite-amalgamation-3450000  → dash + long digit run
# Conservative: requires at least one digit-only group; matches at the end of name.
_VERSION_SUFFIX_RE = re.compile(
    r"(?:_\d+(?:[._]\d+)+|-\d+(?:\.\d+)+|\d{3,})$"
)


def _detect_vendor_dirs(root: Path) -> set[str]:
    """Auto-detect vendored/third-party directory names under *root*.

    A directory is treated as vendored when EITHER:
      a) it contains a standard upstream marker file at its top level
         (LICENSE / COPYING / Doxyfile — strong evidence of a third-party
         drop-in), OR
      b) its name carries a version suffix (`cryptopp890`, `boost_1_82_0`,
         `openssl-3.2`, `sqlite-amalgamation-3450000`).

    Excludes the project root itself and any directory that declares itself
    as an HSP/HAR module (has its own `oh-package.json5`) — those are the
    project's own modules, not vendored. Skips dependency/build dirs by name.
    Returns directory basenames (matches the `vendor_dirs` filter convention).

    Generic across HarmonyOS / Android codebases — no hardcoded library names.
    """
    found: set[str] = set()
    try:
        root_resolved = root.resolve()
    except OSError:
        return found
    for cand in root.rglob("*"):
        if not cand.is_dir():
            continue
        rel_parts = set(cand.relative_to(root).parts)
        if rel_parts & _HARMONY_SKIP_DIRS:
            continue
        if rel_parts & _ANDROID_SKIP_DIR_NAMES:
            continue
        try:
            if cand.resolve() == root_resolved:
                continue
        except OSError:
            continue
        # Project's own HSP/HAR modules carry oh-package.json5 — never vendor.
        if (cand / "oh-package.json5").is_file():
            continue
        # Heuristic A: upstream marker file present at this dir's root.
        has_marker = any(
            (cand / m).is_file() or (cand / m.upper()).is_file()
            for m in _VENDOR_MARKER_FILES
        )
        # Heuristic B: version-suffix naming convention.
        has_version = bool(_VERSION_SUFFIX_RE.search(cand.name))
        if has_marker or has_version:
            found.add(cand.name)
    return found


def _load_delete_stems_from_spec(spec_dir: Path | None) -> frozenset[str]:
    """Return lower-case stems for Android files marked action=delete in source-parity.md.

    Reads spec_dir/baseline/source-parity.md and parses the markdown table for
    rows whose action column equals 'delete'. Those stems are excluded from the
    Android source scan so that intentionally-unported debug utilities
    (*Sample, *Verifier, *Test) do not inflate source_parity entropy.
    """
    if spec_dir is None:
        return frozenset()
    parity_md = spec_dir / "baseline" / "source-parity.md"
    if not parity_md.exists():
        return frozenset()
    stems: set[str] = set()
    try:
        for line in parity_md.read_text(encoding="utf-8", errors="ignore").splitlines():
            cols = line.split("|")
            if len(cols) < 5:
                continue
            # Table columns: | android_path | harmony_target | action | reason |
            action = cols[3].strip().strip("`").lower()
            if action != "delete":
                continue
            android_path_col = cols[1].strip().strip("`")
            if android_path_col:
                stem = Path(android_path_col).stem
                if stem:
                    stems.add(stem.lower())
    except OSError:
        pass
    return frozenset(stems)


def _scan_android(
    android_dir: Path,
    vendor_dirs: set[str],
    ignore_patterns: list[str],
    arch_suffixes: frozenset[str] = _ARCH_ONLY_STEM_SUFFIXES,
    arch_dirs: frozenset[str] = _ARCH_ONLY_DIR_NAMES,
    ui_lib_imports: tuple[str, ...] = _ANDROID_ONLY_UI_LIB_IMPORTS,
) -> list[Path]:
    """Return Android source files worth considering as port targets."""
    results: list[Path] = []
    for f in android_dir.rglob("*"):
        if not f.is_file():
            continue
        suf = f.suffix.lower()
        if (suf not in _NATIVE_EXTS and suf not in _JVM_EXTS
                and suf not in _SHAREABLE_TEXT_EXTS):
            continue
        # Skip build/test/generated directory branches.
        rel_parts = set(f.relative_to(android_dir).parts)
        if rel_parts & _ANDROID_SKIP_DIR_NAMES:
            continue
        if rel_parts & vendor_dirs:
            continue
        if f.stem in _GENERATED_STEMS:
            continue
        # Framework glue (adapters, view-holders, *Binding, AsyncTask, Room
        # db/dao) has no standalone ArkTS file — never demand a 1:1 port.
        if _is_arch_only_jvm(f, rel_parts, arch_suffixes, arch_dirs):
            continue
        # Integration glue for Android-only UI/image libs (Glide/Picasso/
        # ViewPager extension points) likewise maps to no ArkTS file.
        if suf in _JVM_EXTS and ui_lib_imports:
            try:
                if _is_ui_lib_glue(
                    f.read_text(encoding="utf-8", errors="ignore"), ui_lib_imports
                ):
                    continue
            except OSError:
                pass
        if any(f.match(pat) for pat in ignore_patterns):
            continue
        results.append(f)
    return results


def _build_harmony_stem_index(
    harmony_dir: Path, vendor_dirs: set[str] | None = None,
) -> dict[str, list[Path]]:
    """Map normalized stem → list of harmony files with that stem.

    Indexes ArkTS (.ets/.ts/.js) AND native (.cpp/.cc/.h/...) so that
    `clipper.engine.cpp` in Android matches `clipper.engine.cpp` in Harmony,
    and `MainActivity.kt` matches `MainPage.ets` after suffix normalization.
    Vendored directories (e.g. cryptopp890) are skipped — they're upstream
    libraries, not project port targets.
    """
    vendors: set[str] = vendor_dirs or set()
    index: dict[str, list[Path]] = {}
    for f in harmony_dir.rglob("*"):
        if not f.is_file():
            continue
        suf = f.suffix.lower()
        if (suf not in _HARMONY_SRC_EXTS and suf not in _NATIVE_EXTS
                and suf not in _SHAREABLE_TEXT_EXTS):
            continue
        # Skip dependency / build directories on the Harmony side.
        rel_parts = set(f.relative_to(harmony_dir).parts)
        if rel_parts & _HARMONY_SKIP_DIRS:
            continue
        # 点目录（.idea/.codex/.deveco/.git/.claude 等）是工具/agent 配置，不是源码。
        if any(p.startswith(".") for p in f.relative_to(harmony_dir).parts[:-1]):
            continue
        if rel_parts & vendors:
            continue
        key = _normalize_stem(f.stem)
        index.setdefault(key, []).append(f)
    return index


def _normalize_stem(stem: str) -> str:
    """Lower-case + strip well-known platform suffixes.

    `MainActivity` → `main`, `EditorPage` → `editor`. Lets a Kotlin Activity
    line up with its Harmony Page counterpart even though the suffix changed.
    """
    s = stem.lower()
    for suf in _ANDROID_SUFFIXES:
        if len(s) > len(suf) and s.endswith(suf):
            s = s[: -len(suf)]
            break
    for suf in _HARMONY_SUFFIXES:
        if len(s) > len(suf) and s.endswith(suf):
            s = s[: -len(suf)]
            break
    return s


# Top-level symbol declarations in ArkTS/TS (class/interface/enum/type/…). Used
# so an Android class ported as a SYMBOL into another file (no same-named .ets —
# e.g. a small data class merged into a consumer/aggregator file) still counts
# as present. FP-averse: presence of the concept, not of a same-named file.
_SYMBOL_DEF_RE = re.compile(
    r"\b(?:export\s+)?(?:default\s+)?(?:declare\s+)?(?:abstract\s+)?"
    r"(?:class|interface|enum|namespace|module|type|function)\s+([A-Za-z_]\w*)"
)


def _collect_harmony_symbols(
    harmony_dir: Path, vendor_dirs: set[str] | None = None,
) -> set[str]:
    """Lower-cased names of every top-level symbol Harmony declares."""
    syms: set[str] = set()
    for f in _harmony_arkts_files(harmony_dir, vendor_dirs):
        try:
            content = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for m in _SYMBOL_DEF_RE.finditer(content):
            syms.add(m.group(1).lower())
    return syms


def _android_base_names(text: str) -> set[str]:
    """Best-effort superclass/interface names from a Kotlin/Java class header."""
    bases: set[str] = set()
    for m in re.finditer(r"\bclass\s+\w+([^{\n]*)", text):
        header = re.sub(r"\([^()]*\)", "", m.group(1))   # drop ctor / call args
        km = re.search(r":\s*([^{]+)", header)            # Kotlin supertypes
        if km:
            for part in km.group(1).split(","):
                name = part.strip().split(".")[-1].split("<")[0].strip()
                if name[:1].isupper():
                    bases.add(name)
        for jm in re.finditer(r"\b(?:extends|implements)\s+([A-Za-z_][\w.]*)", header):
            bases.add(jm.group(1).split(".")[-1])
    return bases


def _is_folded_into_present_base(
    af: Path,
    harmony_index: dict[str, list[Path]],
    harmony_symbols: set[str],
    max_lines: int = 25,
) -> bool:
    """A trivial Android data/marker class whose base type IS present in Harmony
    was almost certainly folded into that base's representation (a Kotlin
    sealed-subclass collapsed into a discriminated interface field, e.g.
    ThumbnailSection → ThumbnailItem.isSectionTitle). FP-averse: treat as ported.
    Bounded to small files so a substantial unported subclass still surfaces."""
    if af.suffix.lower() not in _JVM_EXTS:
        return False
    try:
        text = af.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    if sum(1 for ln in text.splitlines() if ln.strip()) > max_lines:
        return False
    for base in _android_base_names(text):
        if (base.lower() in harmony_symbols
                or _normalize_stem(base.lower()) in harmony_index):
            return True
    return False


def _classify(android_file: Path, harmony_dir: Path) -> tuple[str, str]:
    """Decide whether to copy / translate / header-swap / port.

    Returns (action, harmony_target_hint).
    """
    suf = android_file.suffix.lower()

    # Kotlin / Java → port to ArkTS.
    if suf in _JVM_EXTS:
        hint = str(harmony_dir / "entry" / "src" / "main" / "ets" /
                   f"{android_file.stem}.ets")
        return "port", hint

    # Shader / schema text languages — byte-identical copy. Target lands
    # under entry/src/main/resources/rawfile/ (the HarmonyOS convention for
    # runtime-loaded text resources); generator can relocate to cpp/ if
    # the file is meant to be embedded as a C string instead.
    if suf in _SHAREABLE_TEXT_EXTS:
        hint = str(harmony_dir / "entry" / "src" / "main" / "resources" /
                   "rawfile" / android_file.name)
        return "copy", hint

    # C/C++ — distinguish copy vs translate vs header-swap.
    try:
        text = android_file.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        text = ""

    has_jni = bool(_JNI_PATTERNS.search(text))
    has_ndk_header = bool(_ANDROID_NDK_HEADER.search(text))

    cpp_target = str(harmony_dir / "entry" / "src" / "main" / "cpp" /
                     android_file.name)
    if has_jni:
        # The JNI file itself must become a NAPI rewrite. Target name is
        # the project's existing *_napi.cpp convention if any, else suggest
        # a generic name and let the project's CMakeLists wire it.
        return "translate", cpp_target
    if has_ndk_header:
        return "header-swap", cpp_target
    return "copy", cpp_target


# ── reachability / orphan detection ──────────────────────────────────────────

# Harmony dependency/build directory names to skip everywhere.
_HARMONY_SKIP_DIRS = frozenset((
    "oh_modules", "node_modules", "build", ".preview", ".hvigor", ".cxx",
    ".idea", ".claude", ".ecat", ".git", "ohosTest",
))
# Path parts that mark a file as a framework-loaded ENTRY POINT (an import-graph
# root): the framework instantiates these without an explicit `import`, so they
# seed reachability. Generic HarmonyOS conventions — no business coupling.
_ENTRY_ROOT_PARTS = frozenset((
    "pages", "entryability", "entrybackupability", "testability", "testrunner",
    # worker scripts are launched by path string (new worker.ThreadWorker(...)),
    # never via a static `import`, so they are roots, not dead code.
    "workers",
))
# Framework-loaded / non-source files that are never `import`-ed and must not be
# mistaken for dead code: build scripts and ambient type declarations.
_NON_SOURCE_NAMES = frozenset(("hvigorfile.ts", "hvigorfile.js"))
# Re-export barrel / module public-API entry filenames (also roots).
_ROOT_FILENAMES = frozenset(("index.ets", "index.ts", "index.js"))

_IMPORT_RE = re.compile(r"""(?:from|import)\s+['"]([^'"]+)['"]""")

# oh-package.json5 field extractors. json5 allows unquoted keys and trailing
# commas; we don't need a full parser — just the two scalars we follow. The
# value side is always quoted in practice. Tolerates both `"name"` and `name`
# key syntax.
_PKG_NAME_RE = re.compile(r"""["']?name["']?\s*:\s*["']([^"']+)["']""")
_PKG_MAIN_RE = re.compile(r"""["']?main["']?\s*:\s*["']([^"']+)["']""")


def _harmony_arkts_files(
    harmony_dir: Path, vendor_dirs: set[str] | None = None,
) -> list[Path]:
    """All ArkTS/TS/JS source files under harmony_dir, skipping deps/build/test
    and any user-declared vendored directories (cryptopp890, etc.)."""
    vendors: set[str] = vendor_dirs or set()
    out: list[Path] = []
    for f in harmony_dir.rglob("*"):
        if not f.is_file() or f.suffix.lower() not in _HARMONY_SRC_EXTS:
            continue
        rel_parts = set(f.relative_to(harmony_dir).parts)
        if rel_parts & _HARMONY_SKIP_DIRS:
            continue
        if rel_parts & vendors:
            continue
        name = f.name.lower()
        if ".test." in name or name.endswith(".d.ts") or name in _NON_SOURCE_NAMES:
            continue
        out.append(f)
    return out


def _is_entry_root(f: Path) -> bool:
    parts = {p.lower() for p in f.parts}
    if parts & _ENTRY_ROOT_PARTS:
        return True
    return f.name.lower() in _ROOT_FILENAMES


def _resolve_import(base_dir: Path, spec: str) -> Path | None:
    """Resolve a relative import specifier to a concrete file, ArkTS-style."""
    raw = base_dir / spec
    for ext in ("", ".ets", ".ts", ".js"):
        cand = Path(str(raw) + ext) if ext else raw
        if cand.is_file():
            return cand.resolve()
    for idx in _ROOT_FILENAMES:
        cand = raw / idx
        if cand.is_file():
            return cand.resolve()
    return None


def _load_module_map(harmony_dir: Path) -> dict[str, Path]:
    """HSP/HAR module name → resolved entry file (`<module>/<main>`).

    Reads every `oh-package.json5` under harmony_dir (skipping dependency
    directories) and pulls `name` + `main`. The standard HarmonyOS multi-module
    layout — entry app + N HSP/HAR libraries linked via `"dep": "file:../lib"`
    — relies on this mapping for cross-module imports. Generic across apps.
    """
    out: dict[str, Path] = {}
    for pkg in harmony_dir.rglob("oh-package.json5"):
        if set(pkg.relative_to(harmony_dir).parts) & _HARMONY_SKIP_DIRS:
            continue
        try:
            text = pkg.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        name_m = _PKG_NAME_RE.search(text)
        main_m = _PKG_MAIN_RE.search(text)
        if not name_m or not main_m:
            continue
        main_path = (pkg.parent / main_m.group(1)).resolve()
        if main_path.is_file():
            out[name_m.group(1)] = main_path
    return out


def _collect_reachable_ets(
    harmony_dir: Path, vendor_dirs: set[str] | None = None,
) -> set[Path]:
    """Resolved ArkTS files reachable from entry roots.

    Roots = framework-loaded entry points (pages/, *ability/, workers/) and
    every module's `main` entry from `oh-package.json5`. Follows:
      * relative imports (`./foo`, `../bar`) inside the current module;
      * non-relative imports that match a known HSP/HAR module name (cross-
        module HSP/HAR deps declared via `"dep": "file:../lib"`).
    System imports (`@kit.*`, `@ohos.*`), native libs (`*.so`) and any other
    unresolved bare specifier are external and not followed. Generic across
    HarmonyOS apps.
    """
    files = _harmony_arkts_files(harmony_dir, vendor_dirs)
    module_map = _load_module_map(harmony_dir)
    roots = [f.resolve() for f in files if _is_entry_root(f)]
    # Every module's declared entry is also a root — covers HSP/HAR modules
    # whose `Index.ets` re-exports everything but is consumed by name, not
    # by relative path.
    roots.extend(module_map.values())
    visited: set[Path] = set()
    stack: list[Path] = list(roots)
    while stack:
        cur = stack.pop()
        if cur in visited:
            continue
        visited.add(cur)
        try:
            content = cur.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for spec in _IMPORT_RE.findall(content):
            target: Path | None = None
            if spec.startswith("."):
                target = _resolve_import(cur.parent, spec)
            elif spec in module_map:
                target = module_map[spec]
            if target and target not in visited:
                stack.append(target)
    return visited


def _collect_orphan_files(
    harmony_dir: Path,
    reachable: set[Path],
    android_stems: set[str],
    vendor_dirs: set[str] | None = None,
) -> list[str]:
    """Harmony ArkTS files that are unreachable AND match no Android source.

    Excludes entry roots (they're reachable by definition) and files whose
    stem matches an Android source (those surface as orphan_matched instead).
    Vendored dirs are skipped — upstream code isn't ours to declare orphan.
    """
    out: list[str] = []
    for f in _harmony_arkts_files(harmony_dir, vendor_dirs):
        if f.resolve() in reachable:
            continue
        if _normalize_stem(f.stem) in android_stems:
            continue
        out.append(str(f.relative_to(harmony_dir)))
    return out


# ── native wiring: ETS ↔ .so ↔ registration triangle ─────────────────────────

_NATIVE_SCAN_EXTS = frozenset((".cpp", ".cc", ".cxx", ".c", ".h", ".hpp", ".hh"))
# `not implemented`, `not yet implemented`, `implemented yet`, `unimplemented`
# — the stub-marker family. Critically broader than a bare `NOT IMPLEMENTED`,
# which misses the common `not yet implemented` wording (the "yet" breaks it).
_NATIVE_STUB_RE = re.compile(
    r"not\s+(?:yet\s+)?implemented|implemented\s+yet|unimplemented",
    re.IGNORECASE,
)
_QUOTED_IDENT_RE = re.compile(r"""["']([A-Za-z_]\w*)["']""")
# Bare-identifier NAPI registration via stringifying macros. The macro
# expansion runs through `#name` at preprocess time, so the source file only
# carries the unquoted token. List covers the common HarmonyOS conventions —
# OpenHarmony official samples, NAPI helper headers, and typical project-side
# wrappers. Generic across apps; extend the union if you encounter a custom
# macro family.
_MACRO_NAPI_RE = re.compile(
    r"\b(?:"
    r"DECLARE_NAPI_FUNCTION|DECLARE_NAPI_PROPERTY|DECLARE_NAPI_GETTER_SETTER|"
    r"DECLARE_NAPI_STATIC_FUNCTION|DECLARE_NAPI_STATIC_PROPERTY|"
    r"NAPI_FUNCTION|NAPI_METHOD|NAPI_PROPERTY|NAPI_GETTER_SETTER|"
    r"EXPORT_ENTRY|EXPORT_NAPI|EXPORT_FUNCTION|EXPORT_METHOD|EXPORT_PROPERTY|"
    r"REGISTER_NAPI|REGISTER_METHOD|REGISTER_FUNCTION|REGISTER_PROPERTY"
    r")\s*\(\s*([A-Za-z_]\w*)\s*[,)]"
)
# default/namespace import bound to a `*.so` module → `import foo from 'x.so'`.
_SO_DEFAULT_IMPORT_RE = re.compile(
    r"""import\s+(?:\*\s+as\s+)?(\w+)\s+from\s+['"][^'"]*\.so['"]"""
)
_SO_NAMED_IMPORT_RE = re.compile(
    r"""import\s+\{([^}]+)\}\s+from\s+['"][^'"]*\.so['"]"""
)


def _native_files(
    harmony_dir: Path, vendor_dirs: set[str] | None = None,
) -> list[Path]:
    """Native C/C++ source files under harmony_dir, skipping build dirs and
    any user-declared vendored libraries (cryptopp890, boost, opencv, ...).

    Vendored skip is critical for stub detection: third-party libraries are
    full of intentional `NotImplemented` virtual-base markers, Doxygen
    `not implemented` comments, etc. — generator must never touch them.
    """
    vendors: set[str] = vendor_dirs or set()
    out: list[Path] = []
    for f in harmony_dir.rglob("*"):
        if not f.is_file() or f.suffix.lower() not in _NATIVE_SCAN_EXTS:
            continue
        rel_parts = set(f.relative_to(harmony_dir).parts)
        if rel_parts & _HARMONY_SKIP_DIRS:
            continue
        if rel_parts & vendors:
            continue
        out.append(f)
    return out


def _collect_registered_napi_names(
    harmony_dir: Path, vendor_dirs: set[str] | None = None,
) -> set[str]:
    """Every quoted identifier in native source — plus bare identifiers passed
    to common NAPI-registration macros.

    A registered NAPI exposes its JS name either:
      a) as a quoted string literal in a `napi_property_descriptor` initializer
         (`{ "foo", ..., foo_impl, ... }`), captured by `_QUOTED_IDENT_RE`; or
      b) as a bare identifier passed to a stringifying macro
         (`DECLARE_NAPI_FUNCTION(foo, foo_impl)`, `EXPORT_ENTRY(foo, ...)`),
         where `#name` runs at preprocess time so the source has only the bare
         token. Without (b) every macro-style codebase produces a flood of
         false "unregistered" alarms.

    Both forms widen the registered-symbol set — false positive here is
    harmless (would only mask a real unregistered symbol), false negative is
    not (drives generator into already-done work).
    """
    names: set[str] = set()
    for f in _native_files(harmony_dir, vendor_dirs):
        try:
            content = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        names.update(_QUOTED_IDENT_RE.findall(content))
        names.update(_MACRO_NAPI_RE.findall(content))
    return names


def _collect_ets_native_calls(
    harmony_dir: Path, vendor_dirs: set[str] | None = None,
) -> set[str]:
    """Symbols the ArkTS layer invokes on a `*.so` native module."""
    called: set[str] = set()
    for f in _harmony_arkts_files(harmony_dir, vendor_dirs):
        try:
            content = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for binding in _SO_DEFAULT_IMPORT_RE.findall(content):
            for m in re.finditer(
                rf"\b{re.escape(binding)}\.(\w+)\s*\(", content
            ):
                called.add(m.group(1))
        for group in _SO_NAMED_IMPORT_RE.findall(content):
            for name in group.split(","):
                sym = name.strip().split(" as ")[-1].strip()
                if sym and re.fullmatch(r"\w+", sym):
                    called.add(sym)
    return called


def _collect_native_stubs(
    harmony_dir: Path, vendor_dirs: set[str] | None = None,
) -> list[dict[str, object]]:
    """Native source lines flagged as unimplemented stubs.

    Vendored libraries (cryptopp890, boost, opencv, ...) are skipped — their
    `NotImplemented` markers are upstream design (base-class virtual stubs,
    Doxygen "not implemented" notes), not project bugs to fix.
    """
    out: list[dict[str, object]] = []
    for f in _native_files(harmony_dir, vendor_dirs):
        try:
            content = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        rel = str(f.relative_to(harmony_dir))
        # 剥注释后再匹配：Doxygen/行注释里的 "not implemented"/"unimplemented" 是文档
        # 说明，不是实现里的桩。不剥会把注释当桩误报（_blank_comments 保留换行与偏移，
        # 行号不变，snippet 仍取原文行）。
        orig_lines = content.splitlines()
        for line_no, line in enumerate(_blank_comments(content).splitlines(), start=1):
            if _NATIVE_STUB_RE.search(line):
                snippet = orig_lines[line_no - 1].strip()[:200] if line_no <= len(orig_lines) else ""
                out.append({"file": rel, "line": line_no, "snippet": snippet})
    return out


_BASELINE_FILENAME = "entropy_baselines.json"


def _load_or_init_baseline(
    runs_dir: Path | None, key: str, current_value: int
) -> int:
    """Return the persisted baseline for *key* under <runs_dir>/entropy_baselines.json.

    First call for a given *key* writes *current_value* (giving score=1.0).
    Falls back to *current_value* when *runs_dir* is unavailable. Shared with
    check_placeholder — both plugins read/write the same file.
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
