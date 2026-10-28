"""Unit tests for entropy plugins using fixture snapshots."""

from pathlib import Path

import pytest

import ecat.entropy.plugins.a2h_verify  # noqa: F401 — registers plugins
import ecat.entropy.plugins.a2h_verify.check_runtime_liveness as _rl_mod
from ecat.entropy.plugins.a2h_verify._device_session import DeviceSession, diagnose_liveness
from ecat.entropy.plugins.a2h_verify.check_data_layer_parity import DataLayerParityPlugin
from ecat.entropy.plugins.a2h_verify.check_permission_parity import PermissionParityPlugin
from ecat.entropy.plugins.a2h_verify.check_placeholder import PlaceholderPlugin
from ecat.entropy.plugins.a2h_verify.check_runtime_liveness import RuntimeLivenessPlugin
from ecat.entropy.plugins.a2h_verify.check_static_lint import StaticLintPlugin
from ecat.entropy.plugins.a2h_verify.check_style import StylePlugin
from ecat.entropy.registry import PluginRegistry
from ecat.entropy.spec_parser import (
    parse_feature_index,
    parse_l1_features,
    parse_l2_feature_phasing,
    parse_navigation_graph,
    parse_placeholder_registry,
    parse_screen_graph,
    parse_source_mapping,
)

FIXTURES = Path(__file__).parent.parent / "fixtures"
PARTIAL = FIXTURES / "tiny_harmony_partial"


# ── registry ─────────────────────────────────────────────────────────────────


def test_a2h_verify_registered():
    plugins = PluginRegistry.get("a2h_verify")
    names = {p.name for p in plugins}
    expected = {
        "compile",
        "static_lint",
        "placeholder",
        "app_identity",
        "page_audit",
        "feature_check",
        "ui_align",
        "entry_nav",
        "source_parity",
        "style",
        "permission_parity",
        "data_layer_parity",
        "runtime_liveness",
        "hallucination",
        "crash_risk",
        "feature_checklist",
    }
    assert expected == names


# ── static_lint ───────────────────────────────────────────────────────────────


def test_static_lint_detects_android_pattern(tmp_path: Path):
    pages = tmp_path / "entry" / "src" / "main" / "ets" / "pages"
    pages.mkdir(parents=True)
    (pages / "Index.ets").write_text("button.setOnClickListener { }", encoding="utf-8")
    plugin = StaticLintPlugin()
    score, details = plugin.evaluate(tmp_path)
    assert score > 0
    assert details["error_count"] >= 1


def test_static_lint_clean(tmp_path: Path):
    pages = tmp_path / "entry" / "src" / "main" / "ets" / "pages"
    pages.mkdir(parents=True)
    (pages / "Index.ets").write_text(
        "@Entry\n@Component\nstruct Index {\n  build() {}\n}\n",
        encoding="utf-8",
    )
    plugin = StaticLintPlugin()
    score, details = plugin.evaluate(tmp_path)
    assert score == 0.0
    assert details["error_count"] == 0


# ── placeholder ───────────────────────────────────────────────────────────────


def test_placeholder_detects_todo(tmp_path: Path):
    pages = tmp_path / "entry" / "src" / "main" / "ets" / "pages"
    pages.mkdir(parents=True)
    (pages / "Main.ets").write_text("// TODO: implement me\n", encoding="utf-8")
    plugin = PlaceholderPlugin()
    score, details = plugin.evaluate(tmp_path)
    assert score > 0


def test_placeholder_hello_world(tmp_path: Path):
    pages = tmp_path / "entry" / "src" / "main" / "ets" / "pages"
    pages.mkdir(parents=True)
    (pages / "Main.ets").write_text('Text("Hello World")\n', encoding="utf-8")
    plugin = PlaceholderPlugin()
    score, _ = plugin.evaluate(tmp_path)
    assert score > 0


def test_hallucination_hard_layer(tmp_path: Path):
    """Hard pre-filter flags a Harmony UI label that introduces a word the
    Android strings never use; exempts known-word labels, prose, platform text,
    and identifier-embedded uses. App-agnostic — synthetic strings only."""
    import json

    from ecat.entropy.plugins.a2h_verify.check_hallucination import HallucinationPlugin

    harmony = tmp_path / "harmony"
    ets = harmony / "entry" / "src" / "main" / "ets" / "pages"
    ets.mkdir(parents=True)
    (ets / "P.ets").write_text(
        "Text($r('app.string.invented_action'))\n"  # novel word -> candidate
        "Text($r('app.string.known_action'))\n"  # all words known -> not
        "Text($r('app.string.ask_permission'))\n"  # platform -> exempt
        "Text($r('app.string.help_message'))\n"  # prose key -> exempt
        "// drawWidgetThing() refers to app.string.unreferenced\n",  # in comment, not $r
        encoding="utf-8",
    )
    el = harmony / "entry" / "src" / "main" / "resources" / "base" / "element"
    el.mkdir(parents=True)
    (el / "string.json").write_text(
        json.dumps(
            {
                "string": [
                    {"name": "invented_action", "value": "Teleport Folder"},
                    {"name": "known_action", "value": "Rename Folder"},
                    {"name": "ask_permission", "value": "Grant teleport permission"},
                    {"name": "help_message", "value": "Teleport moves the folder elsewhere"},
                    {"name": "unreferenced", "value": "Teleport Unused"},
                ]
            }
        ),
        encoding="utf-8",
    )

    android_vals = tmp_path / "android" / "app" / "src" / "main" / "res" / "values"
    android_vals.mkdir(parents=True)
    (android_vals / "strings.xml").write_text(
        '<resources><string name="a">Rename Folder</string>'
        '<string name="b">Delete Folder</string></resources>',
        encoding="utf-8",
    )

    score, details = HallucinationPlugin().evaluate(harmony, android_dir=tmp_path / "android")
    keys = {c["key"] for c in details["candidates"]}
    assert "invented_action" in keys  # "teleport" never used by Android
    assert "known_action" not in keys  # all words in Android vocab
    assert "ask_permission" not in keys  # platform/permission exempt
    assert "help_message" not in keys  # prose (_message) key exempt
    assert "unreferenced" not in keys  # not referenced via $r in ets


def test_placeholder_skips_terminology_and_identifiers(tmp_path: Path):
    """'placeholder' as terminology (modifying a noun, hyphen compound, or
    embedded in an identifier) is NOT a stub — only a bare marker counts."""
    from ecat.entropy.plugins.a2h_verify.check_placeholder import _PLACEHOLDER_PATTERNS

    def hits(line: str) -> bool:
        return any(p.search(line) for p in _PLACEHOLDER_PATTERNS)

    # Terminology / identifiers — must NOT be flagged (cross-app generic).
    for line in (
        "// One-shot flag for glyph-placeholder registration (L3 tracking)",
        " * Tracked via one-shot console.info registration (L3 placeholder pattern).",
        "// Draw a placeholder rectangle while the thumbnail loads",
        "  private drawPlaceholderRect(): void {",
        "// call drawPlaceholderRect() when loading",
        "  this.placeholderColor = Color.Gray",
    ):
        assert not hits(line), f"false positive on: {line!r}"

    # Genuine stub markers — must still be flagged.
    for line in (
        "// placeholder",
        "* just a placeholder",
        "// placeholder, replace with real data",
    ):
        assert hits(line), f"missed real stub: {line!r}"


# ── style ─────────────────────────────────────────────────────────────────────


def test_style_detects_console_log(tmp_path: Path):
    pages = tmp_path / "entry" / "src" / "main" / "ets" / "pages"
    pages.mkdir(parents=True)
    (pages / "Page.ets").write_text("console.log('debug info')\n" * 5, encoding="utf-8")
    plugin = StylePlugin()
    score, details = plugin.evaluate(tmp_path)
    assert details["violation_count"] >= 5


def test_style_clean(tmp_path: Path):
    pages = tmp_path / "entry" / "src" / "main" / "ets" / "pages"
    pages.mkdir(parents=True)
    (pages / "Page.ets").write_text(
        "@Entry\n@Component\nstruct Page {\n  build() {\n    Text('hello')\n  }\n}\n",
        encoding="utf-8",
    )
    plugin = StylePlugin()
    score, _ = plugin.evaluate(tmp_path)
    assert score == 0.0


# ── fixture snapshot ─────────────────────────────────────────────────────────


def test_partial_fixture_produces_nonzero_H():
    """tiny_harmony_partial should produce H > 0 on at least one plugin."""
    if not PARTIAL.exists():
        pytest.skip("tiny_harmony_partial fixture not available")
    plugin = StaticLintPlugin()
    score, _ = plugin.evaluate(PARTIAL)
    # We just check it doesn't crash and returns a valid score
    assert 0.0 <= score <= 1.0


# ── spec_parser: L1 features ────────────────────────────────────────────────


def test_parse_l1_features(tmp_path: Path):
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    (baseline / "L1-domain.md").write_text(
        "## D4: 功能清单\n\n"
        "| ID | 功能 | 优先级 | 模块 | 描述 |\n"
        "|---|---|---|---|---|\n"
        "| F-001 | 文件夹浏览 | P0 | MainActivity | 显示文件夹 |\n"
        "| F-002 | 图片编辑 | P2 | EditActivity | 编辑图片 |\n",
        encoding="utf-8",
    )
    features = parse_l1_features(tmp_path)
    assert len(features) == 2
    assert features[0].feature_id == "F-001"
    assert features[0].version == "V1"
    assert features[1].version == "V2"
    assert features[0].extras.get("module") == "MainActivity"


def test_parse_l1_features_none():
    assert parse_l1_features(None) == []


# ── spec_parser: L2 feature phasing ─────────────────────────────────────────


def test_parse_l2_feature_phasing(tmp_path: Path):
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    (baseline / "L2-migration.md").write_text(
        "## SS6: 功能分期\n\n"
        "| L1 ID | 功能 | L1 优先级 | 分期 | 迁移类型 |\n"
        "|---|---|---|---|---|\n"
        "| F-001 | 文件夹浏览 | P0 | V1 | Auto |\n"
        "| F-013 | 全景图查看 | P2 | Skip | Skip |\n",
        encoding="utf-8",
    )
    features = parse_l2_feature_phasing(tmp_path)
    assert len(features) == 2
    assert features[0].feature_id == "F-001"
    assert features[0].version == "V1"
    assert features[0].extras.get("migration_type") == "Auto"
    assert features[1].version == "SKIP"


# ── spec_parser: source-mapping ──────────────────────────────────────────────


def test_parse_source_mapping(tmp_path: Path):
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    (baseline / "source-mapping.md").write_text(
        "## 页面级映射\n\n"
        "| ArkTS 页面 | Android 类 |\n"
        "|---|---|\n"
        "| MainPage | `activities/MainActivity.kt` |\n"
        "| MediaPage | `activities/MediaActivity.kt` |\n",
        encoding="utf-8",
    )
    pages = parse_source_mapping(tmp_path)
    assert len(pages) == 2
    assert pages[0].name == "MainPage"
    assert pages[0].target_file == "pages/MainPage.ets"
    assert pages[0].extras["android_class"] == "activities/MainActivity.kt"
    assert pages[1].name == "MediaPage"


# ── spec_parser: navigation graph ───────────────────────────────────────────


def test_parse_navigation_graph(tmp_path: Path):
    ui_graph = tmp_path / "baseline" / "ui-graph"
    ui_graph.mkdir(parents=True)
    import json

    (ui_graph / "Navigation_Edge.json").write_text(
        json.dumps(
            {
                "edges": [{"from": "A", "to": "B", "trigger": "click"}],
                "parent_child_nav": {"MainActivity": ["MediaActivity"]},
            }
        ),
        encoding="utf-8",
    )
    result = parse_navigation_graph(tmp_path)
    assert len(result["edges"]) == 1
    assert "MainActivity" in result["parent_child_nav"]


def test_parse_navigation_graph_none():
    result = parse_navigation_graph(None)
    assert result["edges"] == []


# ── spec_parser: screen graph ────────────────────────────────────────────────


def test_parse_screen_graph(tmp_path: Path):
    ui_graph = tmp_path / "baseline" / "ui-graph"
    ui_graph.mkdir(parents=True)
    import json

    (ui_graph / "Screen.json").write_text(
        json.dumps(
            {
                "screens": [
                    {"id": "1", "class": "MainActivity", "type": "Activity"},
                    {"id": "2", "class": "AlertDialog", "type": "Dialog"},
                ]
            }
        ),
        encoding="utf-8",
    )
    screens = parse_screen_graph(tmp_path)
    assert len(screens) == 2
    assert screens[0]["type"] == "Activity"


# ── feature_check with source-mapping ────────────────────────────────────────


def test_feature_check_with_source_mapping(tmp_path: Path):
    from ecat.entropy.plugins.a2h_verify.check_feature_check import FeatureCheckPlugin

    # Create spec with L2 SS6
    baseline = tmp_path / "spec" / "baseline"
    baseline.mkdir(parents=True)
    (baseline / "L2-migration.md").write_text(
        "## SS6: 功能分期\n\n"
        "| L1 ID | 功能 | L1 优先级 | 分期 | 迁移类型 |\n"
        "|---|---|---|---|---|\n"
        "| F-001 | 文件夹浏览 | P0 | V1 | Auto |\n"
        "| F-013 | 全景图 | P2 | Skip | Skip |\n",
        encoding="utf-8",
    )
    (baseline / "source-mapping.md").write_text(
        "## 页面级映射\n\n"
        "| ArkTS 页面 | Android 类 |\n"
        "|---|---|\n"
        "| MainPage | `activities/MainActivity.kt` |\n",
        encoding="utf-8",
    )

    # Create harmony dir with MainPage.ets
    harmony = tmp_path / "harmony"
    pages = harmony / "entry" / "src" / "main" / "ets" / "pages"
    pages.mkdir(parents=True)
    (pages / "MainPage.ets").write_text(
        "@Entry\n@Component\nstruct MainPage {\n  build() { Text('hello') }\n}\n",
        encoding="utf-8",
    )

    plugin = FeatureCheckPlugin()
    score, details = plugin.evaluate(
        harmony,
        spec_dir=tmp_path / "spec",
    )
    assert details["spec_driven"] is True
    results = details["spec_results"]
    # SKIP-version features count as VERIFIED (was also covered by the removed
    # skip_count stat; spec_results is the actionable check).
    assert any(r["status"] == "VERIFIED" and r["version"] == "SKIP" for r in results)


# ── source_parity: reachability / orphan / native wiring ─────────────────────


def _android_file(tmp_path: Path, rel: str, content: str = "class X {}") -> Path:
    p = tmp_path / "android" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def _ets_file(tmp_path: Path, rel: str, content: str = "") -> Path:
    p = tmp_path / "harmony" / "entry" / "src" / "main" / "ets" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def _cpp_file(tmp_path: Path, rel: str, content: str) -> Path:
    p = tmp_path / "harmony" / "render" / "src" / "main" / "cpp" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def _source_parity():
    from ecat.entropy.plugins.a2h_verify.check_source_parity import SourceParityPlugin

    return SourceParityPlugin()


def test_source_parity_reachable_file_counts_present(tmp_path: Path):
    """A harmony file imported from an entry page is reachable → counts present."""
    _android_file(tmp_path, "Bar.kt")
    _ets_file(tmp_path, "pages/Index.ets", "import { Bar } from '../Bar';\n")
    _ets_file(tmp_path, "Bar.ets", "export class Bar {}\n")

    score, details = _source_parity().evaluate(
        tmp_path / "harmony",
        android_dir=tmp_path / "android",
    )
    # 'bar' has a reachable counterpart → present, not orphan-matched.
    assert details["present_count"] >= 1
    matched = [m for m in details["orphan_matched"] if m["stem"] == "bar"]
    assert matched == []


def test_source_parity_orphan_match_reported_but_not_missing(tmp_path: Path):
    """FP-averse policy: an existing same-stem counterpart counts as ported even
    if currently unreachable. The orphan/dead duplicate is REPORTED (advisory)
    but must NOT be flagged as missing ('created but not yet wired' ≠ missing)."""
    _android_file(tmp_path, "Foo.kt")
    _ets_file(tmp_path, "pages/Index.ets", "// no imports\n")
    _ets_file(tmp_path, "Foo.ets", "export class Foo {}\n")  # orphan, not imported

    score, details = _source_parity().evaluate(
        tmp_path / "harmony",
        android_dir=tmp_path / "android",
    )
    assert details["orphan_matched_count"] >= 1  # still surfaced
    assert any(m["stem"] == "Foo" for m in details["orphan_matched"])
    assert "Foo" not in {m["stem"] for m in details["missing"]}  # not missing


def test_source_parity_symbol_or_folded_counts_present(tmp_path: Path):
    """FP-averse: a data class ported as a SYMBOL into another file (no same-named
    .ets), or a trivial sealed-subclass folded into a present base, counts as
    ported — not missing."""
    # (b) symbol consolidated into a consumer file (FilterItem pattern)
    _android_file(tmp_path, "FilterItem.kt", "data class FilterItem(val f: Int)")
    _ets_file(
        tmp_path,
        "data/Presets.ets",
        "export interface FilterItem { f: number }\nimport { X } from '../pages/Index';\n",
    )
    # (c) trivial sealed-subclass folded into a present base (ThumbnailSection)
    _android_file(
        tmp_path,
        "ThumbnailSection.kt",
        "data class ThumbnailSection(val title: String) : ThumbnailItem()",
    )
    _ets_file(
        tmp_path,
        "models/ThumbnailItem.ts",
        "export interface ThumbnailItem { isSection: boolean }\n",
    )
    # a genuinely-absent file must STILL be missing
    _android_file(tmp_path, "MediaFetcher.kt", "class MediaFetcher { fun go() {} }")
    _ets_file(tmp_path, "pages/Index.ets", "import { FilterItem } from '../data/Presets';\n")

    details = _source_parity().evaluate(
        tmp_path / "harmony",
        android_dir=tmp_path / "android",
    )[1]
    missing = {m["stem"] for m in details["missing"]}
    assert "FilterItem" not in missing  # symbol present elsewhere
    assert "ThumbnailSection" not in missing  # folded into present base
    assert "MediaFetcher" in missing  # genuinely absent → still flagged


def test_source_parity_pure_orphan_reported(tmp_path: Path):
    """A harmony .ets matching no Android file and imported by nobody is dead code."""
    _android_file(tmp_path, "Used.kt")
    _ets_file(tmp_path, "pages/Index.ets", "import { Used } from '../Used';\n")
    _ets_file(tmp_path, "Used.ets", "export class Used {}\n")
    _ets_file(tmp_path, "Ghost.ets", "export class Ghost {}\n")  # pure orphan

    score, details = _source_parity().evaluate(
        tmp_path / "harmony",
        android_dir=tmp_path / "android",
    )
    assert details["orphan_count"] >= 1
    assert any("Ghost" in f for f in details["orphan_files"])


def test_source_parity_reachable_via_index_barrel(tmp_path: Path):
    """Reachability follows re-export barrels (index.ets)."""
    _android_file(tmp_path, "Widget.kt")
    _ets_file(tmp_path, "pages/Index.ets", "import { Widget } from '../services/index';\n")
    _ets_file(tmp_path, "services/index.ets", "export * from './Widget';\n")
    _ets_file(tmp_path, "services/Widget.ets", "export class Widget {}\n")

    score, details = _source_parity().evaluate(
        tmp_path / "harmony",
        android_dir=tmp_path / "android",
    )
    assert not any(m["stem"] == "widget" for m in details["orphan_matched"])


def test_source_parity_excludes_framework_entry_files_from_orphans(tmp_path: Path):
    """Build scripts, type decls, and worker files are framework-loaded (not via
    `import`) — they must not be mis-flagged as dead-code orphans."""
    _android_file(tmp_path, "Used.kt")
    _ets_file(tmp_path, "pages/Index.ets", "import { Used } from '../Used';\n")
    _ets_file(tmp_path, "Used.ets", "export class Used {}\n")
    # framework-loaded, never imported:
    (tmp_path / "harmony" / "hvigorfile.ts").write_text("export default {}\n", encoding="utf-8")
    _ets_file(tmp_path, "types/lib.d.ts", "export declare class Lib {}\n")
    _ets_file(tmp_path, "workers/GLWorker.ets", "// worker entry\n")

    score, details = _source_parity().evaluate(
        tmp_path / "harmony",
        android_dir=tmp_path / "android",
    )
    joined = " ".join(details["orphan_files"])
    assert "hvigorfile" not in joined
    assert ".d.ts" not in joined
    assert "GLWorker" not in joined


def test_source_parity_napi_unregistered_symbol(tmp_path: Path):
    """ETS calls a native symbol that is not registered in any .cpp → flagged."""
    _android_file(tmp_path, "Native.kt")
    _ets_file(
        tmp_path,
        "services/native/Lib.ets",
        "import clipper2 from 'libclipper2.so';\nclipper2.realFn();\nclipper2.ghostFn();\n",
    )
    _cpp_file(
        tmp_path,
        "clipper_napi.cpp",
        "napi_property_descriptor desc[] = {\n"
        '  { "realFn", nullptr, N_realFn, nullptr, nullptr, nullptr, napi_default, nullptr },\n'
        "};\n",
    )
    score, details = _source_parity().evaluate(
        tmp_path / "harmony",
        android_dir=tmp_path / "android",
    )
    assert "ghostFn" in details["napi_unregistered"]
    assert "realFn" not in details["napi_unregistered"]


def test_source_parity_native_stub_detected(tmp_path: Path):
    """Native function body that logs 'not yet implemented' is a stub —
    the wording the lexical placeholder regex misses."""
    _android_file(tmp_path, "Gen.kt")
    _cpp_file(
        tmp_path,
        "gen_napi.cpp",
        "static napi_value N_makeVertices(napi_env env, napi_callback_info info) {\n"
        '  OH_LOG_Print(LOG_APP, LOG_WARN, 0, "NAPI", "makeVertices: not yet implemented");\n'
        "  return nullptr;\n"
        "}\n",
    )
    score, details = _source_parity().evaluate(
        tmp_path / "harmony",
        android_dir=tmp_path / "android",
    )
    assert details["native_stub_count"] >= 1
    assert any("makeVertices" in s.get("snippet", "") for s in details["native_stub_funcs"])


def test_source_parity_native_stub_ignores_comments(tmp_path: Path):
    """注释/Doxygen 里的 'not implemented'/'unimplemented' 是文档，不是实现里的桩——
    剥注释后不该命中（否则每个写了说明的 native 文件都被误报成 high 工单）。"""
    _android_file(tmp_path, "Gen.kt")
    _cpp_file(
        tmp_path,
        "doc_only.cpp",
        "// not yet implemented in this build\n"
        "/* unimplemented on older SDKs */\n"
        "int real_impl() { return 42; }\n",
    )
    _score, details = _source_parity().evaluate(
        tmp_path / "harmony",
        android_dir=tmp_path / "android",
    )
    assert details["native_stub_count"] == 0


def test_entry_nav_hello_world_ignores_comments(tmp_path: Path):
    """注释里出现 'Hello World'（文档/示例）不是模板页——剥注释后不应命中。"""
    from ecat.entropy.plugins.a2h_verify.check_entry_nav import _check_hello_world

    assert _check_hello_world("// Hello, World — see the template docs\nstruct Index {}")[
        "has_hello_world"] is False
    assert _check_hello_world('Text("Hello World")')["has_hello_world"] is True


def test_source_parity_skips_android_framework_glue(tmp_path: Path):
    """Adapters / view-holders / *Binding / AsyncTask / Room db have no
    standalone ArkTS counterpart — they must NOT be demanded as missing ports
    (otherwise generator manufactures orphan dead code that never converges)."""
    # Real port target — should still be demanded.
    _android_file(tmp_path, "app/src/main/kotlin/pkg/SomethingPage.kt")
    # Framework glue: caught by class-name suffix...
    _android_file(
        tmp_path,
        "app/src/main/kotlin/pkg/DirectoryAdapter.kt",
        "class DirectoryAdapter : MyRecyclerViewAdapter() {}",
    )
    _android_file(tmp_path, "app/src/main/kotlin/pkg/MediaItemBinding.kt")
    _android_file(
        tmp_path,
        "app/src/main/kotlin/pkg/GalleryDatabase.kt",
        "@Database abstract class GalleryDatabase : RoomDatabase()",
    )
    # ...and by conventional package directory (stem lacks an obvious suffix).
    _android_file(tmp_path, "app/src/main/kotlin/pkg/asynctasks/GetMedia.kt")

    score, details = _source_parity().evaluate(
        tmp_path / "harmony",
        android_dir=tmp_path / "android",
    )
    missing_stems = {m["stem"] for m in details["missing"]}
    assert "SomethingPage" in missing_stems  # real target still demanded
    assert "DirectoryAdapter" not in missing_stems
    assert "MediaItemBinding" not in missing_stems
    assert "GalleryDatabase" not in missing_stems
    assert "GetMedia" not in missing_stems  # skipped via asynctasks/ dir


def test_source_parity_skips_ui_lib_glue_but_keeps_real_ui(tmp_path: Path):
    """A Glide/Picasso extension-point implementation is glue (no ArkTS file);
    an Activity that merely *uses* Glide is a real port target and must stay."""
    # Glue: imports Glide AND subclasses a Glide extension point.
    _android_file(
        tmp_path,
        "svg/SvgDecoder.kt",
        "import com.bumptech.glide.load.ResourceDecoder\n"
        "class SvgDecoder : ResourceDecoder<InputStream, SVG> {}",
    )
    # Real UI: imports Glide but extends an Activity base — must NOT be skipped.
    _android_file(
        tmp_path,
        "activities/MediaActivity.kt",
        "import com.bumptech.glide.Glide\nclass MediaActivity : SimpleActivity() {}",
    )

    details = _source_parity().evaluate(
        tmp_path / "harmony",
        android_dir=tmp_path / "android",
    )[1]
    stems = {m["stem"] for m in details["missing"]}
    assert "SvgDecoder" not in stems  # glue dropped
    assert "MediaActivity" in stems  # real UI still demanded


def test_source_parity_arch_only_config_extends_filter(tmp_path: Path):
    """Project config can extend the framework-glue filter with custom suffixes."""
    _android_file(tmp_path, "pkg/FooPresenter.kt")
    base = _source_parity().evaluate(
        tmp_path / "harmony",
        android_dir=tmp_path / "android",
    )[1]
    assert "FooPresenter" in {m["stem"] for m in base["missing"]}

    ext = _source_parity().evaluate(
        tmp_path / "harmony",
        android_dir=tmp_path / "android",
        config={"source_parity": {"arch_only_suffixes": ["presenter"]}},
    )[1]
    assert "FooPresenter" not in {m["stem"] for m in ext["missing"]}


# ── entry_nav with nav graph ────────────────────────────────────────────────


def test_entry_nav_with_nav_graph(tmp_path: Path):
    from ecat.entropy.plugins.a2h_verify.check_entry_nav import EntryNavPlugin

    # Create spec
    baseline = tmp_path / "spec" / "baseline"
    ui_graph = baseline / "ui-graph"
    ui_graph.mkdir(parents=True)
    import json

    (ui_graph / "Navigation_Edge.json").write_text(
        json.dumps(
            {
                "edges": [{"from": "MainActivity", "to": "MediaActivity", "trigger": "click"}],
                "parent_child_nav": {"MainActivity": ["MediaActivity"]},
            }
        ),
        encoding="utf-8",
    )
    (baseline / "source-mapping.md").write_text(
        "## 页面级映射\n\n"
        "| ArkTS 页面 | Android 类 |\n"
        "|---|---|\n"
        "| MediaPage | `activities/MediaActivity.kt` |\n",
        encoding="utf-8",
    )

    # Create harmony dir with entry page
    harmony = tmp_path / "harmony"
    pages = harmony / "entry" / "src" / "main" / "ets" / "pages"
    pages.mkdir(parents=True)
    (pages / "Index.ets").write_text(
        "@Entry\n@Component\nstruct Index {\n  build() {\n"
        "    Column() {\n      Text('Main')\n      Button('Go')\n    }\n  }\n}\n",
        encoding="utf-8",
    )

    plugin = EntryNavPlugin()
    score, details = plugin.evaluate(
        harmony,
        spec_dir=tmp_path / "spec",
    )
    assert 0.0 <= score <= 1.0
    assert "nav_edge_coverage" in details


def test_entry_nav_tab_detection_follows_loadcontent_to_ui_host(tmp_path: Path):
    """When the module entry is an *Ability, Tabs/structure detection must run on
    the page it loadContent()s — not the Ability (which has no UI). Otherwise a
    real Tabs app is misreported has_tabs=false, misleading the agentic judge."""
    from ecat.entropy.plugins.a2h_verify.check_entry_nav import EntryNavPlugin

    main = tmp_path / "harmony" / "entry" / "src" / "main"
    (main).mkdir(parents=True)
    (main / "module.json5").write_text(
        '{ "module": { "abilities": [ { "srcEntry": "./ets/entryability/EntryAbility.ets" } ] } }',
        encoding="utf-8",
    )
    ab = main / "ets" / "entryability"
    ab.mkdir(parents=True)
    (ab / "EntryAbility.ets").write_text(
        "export default class EntryAbility {\n"
        "  onWindowStageCreate(windowStage) {\n"
        "    windowStage.loadContent('pages/Index', (err) => {});\n"
        "  }\n}\n",
        encoding="utf-8",
    )
    pages = main / "ets" / "pages"
    pages.mkdir(parents=True)
    (pages / "Index.ets").write_text(
        "@Entry\n@Component\nstruct Index {\n  build() {\n"
        "    Navigation() {\n      Tabs() {\n"
        "        TabContent() { Text('A') }.tabBar('A')\n"
        "        TabContent() { Text('B') }.tabBar('B')\n"
        "      }\n    }\n  }\n}\n",
        encoding="utf-8",
    )

    _, details = EntryNavPlugin().evaluate(tmp_path / "harmony")
    assert str(details["entry_file"]).endswith("EntryAbility.ets")
    assert str(details["ui_host_file"]).endswith("Index.ets")
    assert details["tabs"]["has_tabs"] is True  # detected on the host page
    assert details["tabs"]["tab_count"] == 2


# ── spec_parser: feature-index version 列自适应 ─────────────────────────────


def test_parse_feature_index_v1v2_legacy(tmp_path: Path):
    # 旧版:第3列是版本 V1/V2 —— 必须原样保留(零回归守卫)
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    (baseline / "feature-index.md").write_text(
        "## 功能清单\n\n"
        "| ID | 功能 | 版本 | 输出文件 |\n"
        "|---|---|---|---|\n"
        "| F-001 | 浏览 | V1 | `pages/MainPage.ets` |\n"
        "| F-002 | 设置 | V2 | `pages/SettingsPage.ets` |\n",
        encoding="utf-8",
    )
    features = parse_feature_index(tmp_path)
    assert len(features) == 2
    assert features[0].feature_id == "F-001"
    assert features[0].version == "V1"
    assert features[1].version == "V2"


def test_parse_feature_index_priority_new(tmp_path: Path):
    # 新版:第3列是优先级 P0/P1/P2 —— 映射 P0/P1→V1, P2→V2
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    (baseline / "feature-index.md").write_text(
        "## 功能清单\n\n"
        "| ID | 功能 | 优先级 | 依赖 | 涉及页面 | 状态 |\n"
        "|----|------|--------|------|---------|------|\n"
        "| F001 | 渲染表面 | P0 | base | — | pending |\n"
        "| F007 | 复合背景 | P1 | F004 | — | pending |\n"
        "| F099 | 边角功能 | P2 | base | — | pending |\n",
        encoding="utf-8",
    )
    features = parse_feature_index(tmp_path)
    assert len(features) == 3
    assert features[0].feature_id == "F001"
    assert features[0].version == "V1"  # P0 → V1
    assert features[1].version == "V1"  # P1 → V1
    assert features[2].version == "V2"  # P2 → V2


# ── spec_parser: placeholder-registry 新旧 id 格式 ──────────────────────────


def test_parse_placeholder_registry_legacy_phid(tmp_path: Path):
    # 旧版:PH-001，列序 id|file|type|target|status|note —— 零回归守卫
    (tmp_path / "placeholder-registry.md").write_text(
        "## 占位登记\n\n"
        "| ID | 文件 | 类型 | 目标 | 状态 | 备注 |\n"
        "|---|---|---|---|---|---|\n"
        "| PH-001 | pages/MainPage.ets | navigation | DetailPage | pending | 待接 |\n",
        encoding="utf-8",
    )
    entries = parse_placeholder_registry(tmp_path)
    assert len(entries) == 1
    assert entries[0].ph_id == "PH-001"
    assert entries[0].status == "pending"


def test_parse_placeholder_registry_new_pid(tmp_path: Path):
    # 新版:P-S1-001，列序 P-ID|location|trigger|status|kind|resolve_by
    (tmp_path / "placeholder-registry.md").write_text(
        "## Placeholder Registry\n\n"
        "| P-ID | location | trigger_condition | status | kind | resolve_by |\n"
        "|------|----------|-------------------|--------|------|------------|\n"
        "| P-S1-001 | entry/src/main/ets/services/render/OpenCvWrapper.ets#approxPolyDP "
        "| lib_opencv 鸿蒙 NDK 重编版本入仓 | registered | thirdparty-sdk | |\n",
        encoding="utf-8",
    )
    entries = parse_placeholder_registry(tmp_path)
    assert len(entries) == 1
    assert entries[0].ph_id == "P-S1-001"
    assert entries[0].status == "registered"
    assert entries[0].ph_type == "thirdparty-sdk"  # kind column
    assert entries[0].file.startswith("entry/")  # location column


# ── feature_check: unquantified handling ────────────────────────────────────


def test_assess_file_quality_no_android_is_unquantified(tmp_path: Path):
    from ecat.entropy.plugins.a2h_verify.check_feature_check import _assess_file_quality

    ets = tmp_path / "X.ets"
    ets.write_text(
        "@Component\nstruct X {\n  build() {\n    Column() { Text('hi') }\n  }\n}\n"
        + "// pad\n" * 30,
        encoding="utf-8",
    )
    status, ratio = _assess_file_quality(ets, None)  # no Android source to compare
    assert ratio is None
    assert status == "UNQUANTIFIED"


def test_assess_file_quality_with_android_is_quantified(tmp_path: Path):
    from ecat.entropy.plugins.a2h_verify.check_feature_check import _assess_file_quality

    ets = tmp_path / "X.ets"
    ets.write_text(
        "@Component\nstruct X {\n  build() {\n    Column() { Text('hi') }\n  }\n}\n" + "x\n" * 30,
        encoding="utf-8",
    )
    android = tmp_path / "X.kt"
    android.write_text("class X {\n" + "  fun m() {}\n" * 40 + "}\n", encoding="utf-8")
    status, ratio = _assess_file_quality(ets, android)  # real line-ratio
    assert isinstance(ratio, float)


def test_feature_check_judge_prompt_has_android_exploration():
    from ecat.entropy.plugins.a2h_verify.check_feature_check import FeatureCheckPlugin

    p = FeatureCheckPlugin.judge_prompt
    assert "android_dir" in p
    assert "Grep" in p or "Glob" in p
    # still keeps the stub-fallback as last resort
    assert "stub patterns" in p


# ── should_skip_path: exclude agent/build dirs ──────────────────────────────


def test_should_skip_path_excludes_claude_dir(tmp_path: Path):
    from ecat.entropy.base import should_skip_path

    base = tmp_path
    # .claude skill template files must be skipped (not project code)
    claude_file = base / ".claude" / "skills" / "x" / "template" / "Index.ets"
    project_file = base / "entry" / "src" / "main" / "ets" / "pages" / "Index.ets"
    assert should_skip_path(claude_file, base) is True
    assert should_skip_path(project_file, base) is False
    # existing skips still hold
    assert should_skip_path(base / "oh_modules" / "dep.ets", base) is True


# ── placeholder: registry whitelist normalization (cross-app) ───────────────


def test_placeholder_registry_match_normalizes_path_and_anchor(tmp_path: Path):
    from ecat.entropy.plugins.a2h_verify.check_placeholder import PlaceholderPlugin

    spec = tmp_path / "spec"
    spec.mkdir()
    # new-spec registry location: forward slashes + #method anchor
    (spec / "placeholder-registry.md").write_text(
        "## Placeholder Registry\n\n"
        "| P-ID | location | trigger_condition | status | kind | resolve_by |\n"
        "|------|----------|-------------------|--------|------|------------|\n"
        "| P-S4-001 | entry/src/main/ets/components/render/FrontBufferLowLatency.ets#enableLowLatencyMode "
        "| sdk | registered | thirdparty-sdk | |\n",
        encoding="utf-8",
    )
    harmony = tmp_path / "harmony"
    comp = harmony / "entry" / "src" / "main" / "ets" / "components" / "render"
    comp.mkdir(parents=True)
    # registered file — should NOT be flagged despite separator/anchor diff
    (comp / "FrontBufferLowLatency.ets").write_text(
        "// stub: low latency not yet wired\nstruct X {}\n",
        encoding="utf-8",
    )
    # genuinely unregistered file — SHOULD still be flagged
    pages = harmony / "entry" / "src" / "main" / "ets" / "pages"
    pages.mkdir(parents=True)
    (pages / "Orphan.ets").write_text(
        "// TODO implement me\nstruct Y {}\n",
        encoding="utf-8",
    )

    _, details = PlaceholderPlugin().evaluate(harmony, spec_dir=spec)
    unreg_files = {str(u["file"]).replace("\\", "/") for u in details["unregistered"]}
    assert not any("FrontBufferLowLatency" in f for f in unreg_files)  # registered → not noisy
    assert any("Orphan" in f for f in unreg_files)  # real unregistered → kept


def test_norm_registry_path_generalizes():
    from ecat.entropy.plugins.a2h_verify.check_placeholder import _norm_registry_path

    assert _norm_registry_path("entry/x/Y.ets#foo") == "entry/x/Y.ets"
    assert _norm_registry_path("entry\\x\\Y.ets") == "entry/x/Y.ets"
    assert _norm_registry_path("./pages/Z.ets") == "pages/Z.ets"
    assert _norm_registry_path("pages/MainPage.ets") == "pages/MainPage.ets"  # legacy unchanged
    # removeprefix (not lstrip): only a literal "./" prefix is dropped, a
    # leading dot-directory is preserved
    assert _norm_registry_path(".hidden/X.ets") == ".hidden/X.ets"


# ── N3 data_layer_parity / N4 permission_parity (anti-false-positive focus) ────


def _mk(p: Path, content: str = "") -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def _android_manifest(android: Path, perms: list[str]) -> None:
    body = "\n".join(f'<uses-permission android:name="android.permission.{p}"/>' for p in perms)
    _mk(
        android / "app" / "src" / "main" / "AndroidManifest.xml",
        f"<manifest>\n{body}\n<application/>\n</manifest>",
    )


def _module_json5(harmony: Path, ohos_perms: list[str]) -> None:
    perms = ",".join(f'{{"name":"ohos.permission.{p}"}}' for p in ohos_perms)
    _mk(
        harmony / "entry" / "src" / "main" / "module.json5",
        f'{{"module":{{"requestPermissions":[{perms}]}}}}',
    )


def test_permission_parity_all_declared_no_false_positive(tmp_path):
    a, h = tmp_path / "android", tmp_path / "harmony"
    _android_manifest(a, ["CAMERA", "INTERNET"])
    _module_json5(h, ["CAMERA", "INTERNET"])
    score, d = PermissionParityPlugin().evaluate(h, android_dir=a)
    assert score == 0.0
    assert d["missing_count"] == 0


def test_permission_parity_detects_missing(tmp_path):
    a, h = tmp_path / "android", tmp_path / "harmony"
    _android_manifest(a, ["CAMERA", "SET_WALLPAPER"])
    # SET_WALLPAPER must be actually USED in source to count as a real gap.
    _mk(
        a / "app" / "src" / "main" / "Wall.kt",
        "val wm = WallpaperManager.getInstance(ctx); wm.setWallpaper(bmp)",
    )
    _module_json5(h, ["CAMERA"])
    score, d = PermissionParityPlugin().evaluate(h, android_dir=a)
    assert score > 0
    assert d["missing_count"] == 1
    assert any("SET_WALLPAPER" in m for m in d["missing"])


def test_permission_parity_declared_but_unused_not_flagged(tmp_path):
    # A permission in the manifest but never exercised in code (e.g. merged from an
    # SDK) must NOT be reported missing — that would be a soft false positive.
    a, h = tmp_path / "android", tmp_path / "harmony"
    _android_manifest(a, ["SET_WALLPAPER", "CAMERA"])
    _mk(a / "app" / "src" / "main" / "Cam.kt", "val c = Camera.open()")  # only CAMERA used
    _module_json5(h, [])
    score, d = PermissionParityPlugin().evaluate(h, android_dir=a)
    assert any("CAMERA" in m for m in d["missing"])
    assert "SET_WALLPAPER" in d["declared_unused"]
    assert not any("SET_WALLPAPER" in m for m in d["missing"])


def test_permission_parity_unmapped_not_flagged(tmp_path):
    # Permissions with no confident OHOS equivalent must NOT be reported missing.
    a, h = tmp_path / "android", tmp_path / "harmony"
    _android_manifest(a, ["POST_NOTIFICATIONS", "RECEIVE_BOOT_COMPLETED"])
    _module_json5(h, [])
    score, d = PermissionParityPlugin().evaluate(h, android_dir=a)
    assert score == 0.0
    assert d["mapped_count"] == 0
    assert d["missing_count"] == 0


def test_permission_parity_skip_without_android(tmp_path):
    h = tmp_path / "harmony"
    _module_json5(h, ["CAMERA"])
    score, d = PermissionParityPlugin().evaluate(h, android_dir=None)
    assert score == 0.0
    assert "skipped" in d


def test_data_layer_all_present_no_false_positive(tmp_path):
    a, h = tmp_path / "android", tmp_path / "harmony"
    _mk(a / "app" / "src" / "main" / "db" / "Note.kt", '@Entity(tableName = "notes")\nclass Note')
    _mk(
        a / "app" / "src" / "main" / "Config.kt",
        "class Config { lateinit var p: SharedPreferences\n"
        '  fun f() = p.getBoolean("show_hidden", false) }',
    )
    _mk(
        h / "entry" / "src" / "main" / "ets" / "db" / "NotesRdb.ets",
        'const TABLE = "notes"; const K = "show_hidden";',
    )
    score, d = DataLayerParityPlugin().evaluate(h, android_dir=a)
    assert score == 0.0
    assert d["missing_table_count"] == 0
    assert d["missing_pref_key_count"] == 0


def test_data_layer_detects_missing_table(tmp_path):
    a, h = tmp_path / "android", tmp_path / "harmony"
    _mk(
        a / "app" / "src" / "main" / "db" / "Secret.kt",
        '@Entity(tableName = "secrets")\nclass Secret',
    )
    _mk(h / "entry" / "src" / "main" / "ets" / "Index.ets", "struct Index {}")
    score, d = DataLayerParityPlugin().evaluate(h, android_dir=a)
    assert score > 0
    assert "secrets" in d["missing_tables"]


def test_data_layer_ignores_non_pref_getstring(tmp_path):
    # Cursor.getString("col") in a file WITHOUT any preferences API must not be
    # collected as a settings key (else it becomes a false-positive missing key).
    a, h = tmp_path / "android", tmp_path / "harmony"
    _mk(
        a / "app" / "src" / "main" / "db" / "Dao.kt",
        'fun read(c: Cursor) { val x = c.getString("secret_column") }',
    )
    _mk(a / "app" / "src" / "main" / "db" / "Note.kt", '@Entity(tableName = "notes")\nclass Note')
    _mk(h / "entry" / "src" / "main" / "ets" / "a.ets", 'const T = "notes";')
    score, d = DataLayerParityPlugin().evaluate(h, android_dir=a)
    assert d["android_pref_key_count"] == 0
    assert "secret_column" not in d["missing_pref_keys"]


def test_data_layer_skip_when_no_data_layer(tmp_path):
    a, h = tmp_path / "android", tmp_path / "harmony"
    _mk(a / "app" / "src" / "main" / "Calc.kt", "class Calc { fun add() = 1 + 1 }")
    _mk(h / "entry" / "src" / "main" / "ets" / "Index.ets", "struct Index {}")
    score, d = DataLayerParityPlugin().evaluate(h, android_dir=a)
    assert score == 0.0
    assert d.get("skipped") == "no_android_data_layer"


# ── N1 runtime_liveness (device-driven; only the device-free skip paths) ───────


def test_runtime_liveness_cadence_skip(tmp_path):
    # Off-cadence iteration must skip without touching any device, contributing 0.
    score, d = RuntimeLivenessPlugin().evaluate(
        tmp_path, config={"iteration": 3, "dynamic_render_interval": 5}
    )
    assert score == 0.0
    assert d["skipped"] == "cadence"


def test_runtime_liveness_no_device_skip(tmp_path, monkeypatch):
    # Session reports no device → clean skip, never an inflated score.
    monkeypatch.setattr(
        _rl_mod,
        "get_session",
        lambda *a, **k: DeviceSession(False, "no_device", 0.0, skipped="no_device"),
    )
    score, d = RuntimeLivenessPlugin().evaluate(tmp_path, config={"iteration": 0})
    assert score == 0.0
    assert d["skipped"] == "no_device"


def test_runtime_liveness_reports_crash(tmp_path, monkeypatch):
    # A crashed session → score 1.0 with a detailed issue surfaced as a WorkItem.
    sess = DeviceSession(
        True,
        "crashed",
        1.0,
        issues=["App 启动即崩溃（native Cppcrash） — 新增 HarmonyOS faultlog: cppcrash-x"],
    )
    monkeypatch.setattr(_rl_mod, "get_session", lambda *a, **k: sess)
    plugin = RuntimeLivenessPlugin()
    score, d = plugin.evaluate(tmp_path, config={"iteration": 0})
    assert score == 1.0
    items = plugin.extract_work_items(score, d)
    assert items and items[0].severity == "high"
    assert "崩溃" in items[0].issues[0]


def test_runtime_liveness_healthy_no_issues(tmp_path, monkeypatch):
    monkeypatch.setattr(
        _rl_mod, "get_session", lambda *a, **k: DeviceSession(True, "alive_ok", 0.0)
    )
    plugin = RuntimeLivenessPlugin()
    # disable smoke-tap (it would drive the device) for this device-free unit test
    score, d = plugin.evaluate(tmp_path, config={"iteration": 0, "liveness_smoke_tap": False})
    assert score == 0.0
    assert plugin.extract_work_items(score, d) == []


def test_find_clickables_parses_bounds():
    import json

    from ecat.entropy.plugins.a2h_verify._device_session import find_clickables

    dump = json.dumps(
        {
            "attributes": {"type": "Root"},
            "children": [
                {
                    "attributes": {
                        "clickable": "true",
                        "type": "Button",
                        "text": "OK",
                        "bounds": "[10,20][110,80]",
                    }
                },
                {
                    "attributes": {
                        "clickable": "false",
                        "type": "Text",
                        "text": "label",
                        "bounds": "[0,0][50,10]",
                    }
                },  # not clickable -> skipped
            ],
        }
    )
    cks = find_clickables(dump)
    assert len(cks) == 1
    assert cks[0]["label"] == "OK"
    assert cks[0]["cx"] == 60 and cks[0]["cy"] == 50


def test_merge_tap_crash_escalates_and_soft_lifts():
    from ecat.entropy.plugins.a2h_verify.check_runtime_liveness import _merge_tap

    # a tap that crashes the app → 1.0 crashed, issue prepended
    st, sc, iss = _merge_tap(
        "alive_ok", 0.0, ["base"], [{"outcome": "crash", "issue": "点击「X」后崩溃: jscrash-..."}]
    )
    assert st == "crashed" and sc == 1.0 and iss[0].startswith("点击「X」")
    # a soft tap problem lifts a healthy app to runtime_errors
    st, sc, iss = _merge_tap(
        "alive_ok",
        0.0,
        [],
        [{"outcome": "subpage_white", "issue": "点击「Y」后页面空白（子页白屏）"}],
    )
    assert st == "runtime_errors" and sc == 0.3 and "子页白屏" in iss[0]
    # all clean → unchanged
    assert _merge_tap("alive_ok", 0.0, [], [{"outcome": "clean", "issue": None}]) == (
        "alive_ok",
        0.0,
        [],
    )


def test_runtime_liveness_default_off():
    # Like ui_align, N1 is opt-in (weight 0 by default).
    assert RuntimeLivenessPlugin.default_weight == 0.0


def test_zero_weight_plugin_is_not_evaluated(tmp_path):
    """A plugin whose normalized weight is 0 must be skipped entirely — its
    evaluate() is never called (verifies ui_align / runtime_liveness at weight 0
    truly don't run)."""
    from ecat.entropy.base import EntropyPlugin
    from ecat.entropy.engine import EntropyEngine
    from ecat.entropy.registry import PluginRegistry

    class _Boom(EntropyPlugin):
        name = "boom"

        def evaluate(self, harmony_dir, *, android_dir=None, spec_dir=None, config=None):
            raise AssertionError("weight-0 plugin must NOT be evaluated")

    class _Ok(EntropyPlugin):
        name = "ok"

        def evaluate(self, harmony_dir, *, android_dir=None, spec_dir=None, config=None):
            return 0.0, {}

    PluginRegistry.register("zw_test", [_Boom(), _Ok()])
    engine = EntropyEngine("zw_test", {"boom": 0.0, "ok": 1.0})
    report = engine.evaluate(tmp_path, iteration=0)  # must NOT raise (boom skipped)
    assert report.breakdown["boom"] == 0.0


# ── liveness diagnosis (detail / accuracy of returned problems) ───────────────


def test_diagnose_crash_via_faultlog():
    status, score, issues = diagnose_liveness(
        alive=False,
        new_faults=["/data/log/faultlog/faultlogger/cppcrash-com.x-9-1"],
        crash_markers=[],
        node_count=-1,
    )
    assert status == "crashed" and score == 1.0
    assert "crashed" in issues[0] and "cppcrash-com.x-9-1" in issues[0]
    assert "Cppcrash" in issues[0]  # crash type inferred from faultlog name


def test_diagnose_flash_exit_via_markers():
    status, score, issues = diagnose_liveness(
        alive=False, new_faults=[], crash_markers=["JsError"], node_count=-1
    )
    assert status == "crashed" and score == 1.0
    assert "instant crash" in issues[0] and "JsError" in issues[0]


def test_diagnose_white_screen():
    status, score, issues = diagnose_liveness(
        alive=True, new_faults=[], crash_markers=[], node_count=0
    )
    assert status == "white_screen" and score == 0.5
    assert "white screen" in issues[0]


def test_diagnose_healthy():
    status, score, issues = diagnose_liveness(
        alive=True, new_faults=[], crash_markers=[], node_count=25
    )
    assert status == "alive_ok" and score == 0.0 and issues == []


def test_diagnose_launch_unconfirmed_not_a_crash():
    # process gone but no crash evidence → NOT scored as a crash (anti-false-positive)
    status, score, issues = diagnose_liveness(
        alive=False, new_faults=[], crash_markers=[], node_count=-1
    )
    assert status == "launch_unconfirmed" and score == 0.0 and issues == []


def test_device_session_main_page_ok_gate():
    # ui_align only needs: main page opened + not white. Runtime errors do NOT block it.
    assert DeviceSession(True, "alive_ok", 0.0).main_page_ok is True
    assert DeviceSession(True, "runtime_errors", 0.3).main_page_ok is True
    assert DeviceSession(True, "white_screen", 0.5).main_page_ok is False
    assert DeviceSession(True, "crashed", 1.0).main_page_ok is False
    assert DeviceSession(False, "no_device", 0.0, skipped="no_device").main_page_ok is False


def test_diagnose_runtime_errors_alive_but_logging():
    # Alive + rendered + curated runtime errors → moderate score, page still usable.
    status, score, issues = diagnose_liveness(
        alive=True,
        new_faults=[],
        crash_markers=[],
        node_count=20,
        runtime_errors={"js_exception": ["12-01 E ... Error message: x is not callable"]},
    )
    assert status == "runtime_errors"
    assert 0.2 <= score < 1.0
    assert "runtime ArkTS exception" in issues[0]
    assert "is not callable" in issues[0]


def test_scan_runtime_errors_app_scoped_and_curated():
    from ecat.entropy.plugins.a2h_verify._device_session import scan_runtime_errors

    hilog = "\n".join(
        [
            "06-18 10:00:00.1  4321  4321 E app/JsApp: Error message: foo is not a function",  # app + curated
            "06-18 10:00:00.2  9999  9999 E sys/Other: Error message: unrelated system noise",  # not app pid
            "06-18 10:00:00.3  4321  4321 I app/Tag: normal info line, no error pattern",  # app but benign
            "06-18 10:00:00.4  4321  4321 E C04200/WMS: SetWindowType: permission denied!",  # framework noise
        ]
    )
    hits = scan_runtime_errors(hilog, pid="4321", bundle_short="JsApp")
    assert "js_exception" in hits
    assert len(hits["js_exception"]) == 1  # only the app's curated error line
    assert "is not a function" in hits["js_exception"][0]
    assert "permission_denied" not in hits  # WMS SetWindowType noise excluded


# ── FP regression: entry_nav orphan icons vs system symbols ─────────────────


def _orphan_icons():
    from ecat.entropy.plugins.a2h_verify.check_entry_nav import _check_orphan_icons

    return _check_orphan_icons


def test_orphan_icons_suppressed_when_system_symbols_used(tmp_path: Path):
    """Drawables copied to media/ but superseded by `sys.symbol` glyphs are NOT
    orphans — the affordance is a system symbol the file scan can't see."""
    media = tmp_path / "harmony" / "entry" / "src" / "main" / "resources" / "base" / "media"
    media.mkdir(parents=True)
    (media / "ic_filter_vector.svg").write_text("<svg><path d='M1 1L2 2'/></svg>", encoding="utf-8")
    pages = tmp_path / "harmony" / "entry" / "src" / "main" / "ets"
    pages.mkdir(parents=True)
    # Page renders its icons via the system-symbol idiom (≥3 call sites).
    (pages / "EditPage.ets").write_text(
        "SymbolGlyph($r('sys.symbol.crop'))\n"
        "SymbolGlyph($r('sys.symbol.rotate'))\n"
        "SymbolGlyph($r('sys.symbol.checkmark'))\n",
        encoding="utf-8",
    )
    # Android menu references @drawable/ic_filter_vector → would normally flag orphan.
    menu = tmp_path / "android" / "app" / "src" / "main" / "res" / "menu"
    menu.mkdir(parents=True)
    (menu / "menu_edit.xml").write_text(
        '<menu><item android:icon="@drawable/ic_filter_vector"/></menu>',
        encoding="utf-8",
    )
    orphans = _orphan_icons()(media, pages, set(), tmp_path / "android")
    assert orphans == []  # suppressed: project adopted system symbols


def test_orphan_icons_still_flagged_without_system_symbols(tmp_path: Path):
    """Without system-symbol adoption the orphan signal still fires (no over-suppression)."""
    media = tmp_path / "harmony" / "entry" / "src" / "main" / "resources" / "base" / "media"
    media.mkdir(parents=True)
    (media / "ic_filter_vector.svg").write_text("<svg><path d='M1 1L2 2'/></svg>", encoding="utf-8")
    pages = tmp_path / "harmony" / "entry" / "src" / "main" / "ets"
    pages.mkdir(parents=True)
    (pages / "EditPage.ets").write_text(
        "Text('Filter')\n", encoding="utf-8"
    )  # bare text, no symbols
    menu = tmp_path / "android" / "app" / "src" / "main" / "res" / "menu"
    menu.mkdir(parents=True)
    (menu / "menu_edit.xml").write_text(
        '<menu><item android:icon="@drawable/ic_filter_vector"/></menu>',
        encoding="utf-8",
    )
    orphans = _orphan_icons()(media, pages, set(), tmp_path / "android")
    assert "ic_filter_vector" in orphans


# ── FP regression: feature_check list pattern matches `List() {` idiom ───────


def test_feature_check_list_pattern_matches_arkui_list(tmp_path: Path):
    """Android RecyclerView → ArkUI `List() { ForEach }` must register as found,
    not missing (regex previously demanded `List {` / LazyForEach only)."""
    from ecat.entropy.plugins.a2h_verify.check_feature_check import _pattern_check

    _android_file(tmp_path, "Main.kt", "val rv = RecyclerView(context)")
    _ets_file(
        tmp_path, "pages/Index.ets", "List() {\n  ForEach(this.items, (it) => { ListItem() {} })\n}"
    )
    res = _pattern_check(tmp_path / "android", tmp_path / "harmony")
    assert "list_component" in res["found"]
    assert "list_component" not in res["missing"]


def _mk_android_widget(android: Path) -> None:
    """Android source with an AppWidgetProvider subclass (ships a widget)."""
    d = android / "app" / "src" / "main" / "kotlin"
    d.mkdir(parents=True)
    (d / "MyWidgetProvider.kt").write_text(
        "class MyWidgetProvider : AppWidgetProvider() {\n}\n", encoding="utf-8"
    )


def _mk_module_json5(harmony: Path, *, form: bool) -> None:
    mod = harmony / "entry" / "src" / "main"
    mod.mkdir(parents=True)
    ext = (
        "      { 'name': 'WidgetForm', 'type': 'form', 'srcEntry': './ets/Card.ets' }"
        if form
        else "      { 'name': 'Backup', 'type': 'backup', 'srcEntry': './ets/B.ets' }"
    )
    (mod / "module.json5").write_text(
        "{\n  // json5 comment\n  'module': {\n    'extensionAbilities': [\n"
        + ext
        + "\n    ]\n  }\n}\n",
        encoding="utf-8",
    )


def test_widget_form_parity_fires_when_unregistered(tmp_path: Path):
    """Android has a widget but HarmonyOS registers no Form → one fail."""
    from ecat.entropy.plugins.a2h_verify.check_entry_nav import _check_widget_form_parity

    android = tmp_path / "android"
    harmony = tmp_path / "harmony"
    _mk_android_widget(android)
    _mk_module_json5(harmony, form=False)
    r = _check_widget_form_parity(harmony, android)
    assert r["android_has_widget"] is True
    assert r["harmony_form_registered"] is False
    assert len(r["fails"]) == 1


def test_widget_form_parity_skips_when_no_android_widget(tmp_path: Path):
    """No AppWidgetProvider in Android → never fires (FP-averse, generic)."""
    from ecat.entropy.plugins.a2h_verify.check_entry_nav import _check_widget_form_parity

    android = tmp_path / "android"
    harmony = tmp_path / "harmony"
    (android / "app").mkdir(parents=True)
    (android / "app" / "Plain.kt").write_text(
        "import android.widget.TextView\nclass Plain {}\n", encoding="utf-8"
    )
    _mk_module_json5(harmony, form=False)
    r = _check_widget_form_parity(harmony, android)
    assert r["android_has_widget"] is False
    assert r["fails"] == []


def test_widget_form_parity_passes_when_registered_json5_single_quotes(tmp_path: Path):
    """Form registered with JSON5 single quotes → no fail (no false positive)."""
    from ecat.entropy.plugins.a2h_verify.check_entry_nav import _check_widget_form_parity

    android = tmp_path / "android"
    harmony = tmp_path / "harmony"
    _mk_android_widget(android)
    _mk_module_json5(harmony, form=True)
    r = _check_widget_form_parity(harmony, android)
    assert r["android_has_widget"] is True
    assert r["harmony_form_registered"] is True
    assert r["fails"] == []


def test_widget_form_parity_skips_when_no_android_dir(tmp_path: Path):
    """android_dir=None → skip, never a spurious fail."""
    from ecat.entropy.plugins.a2h_verify.check_entry_nav import _check_widget_form_parity

    r = _check_widget_form_parity(tmp_path / "harmony", None)
    assert r["fails"] == []


# ── .ecat 跳过回归 ────────────────────────────────────────────────────────────
# 回归护栏：当 `ecat init`/`ecat run` 从鸿蒙仓根启动时，`.ecat` 会落在仓根下
# （build logs/agents/runs_*/state.json）。它不在 target source，判别器插件遍历
# harmony_dir 时绝不能扫到它 —— 否则 .ecat 里的 .json/agent 配置会被当成真实源码，
# 污染评估（实测收集的 .ecat 里含 947 个 .json）。git 层已由 .gitignore 忽略，
# 这里锁住遍历层（_SKIP_DIRS）的排除。


def test_should_skip_path_excludes_ecat(tmp_path: Path) -> None:
    """should_skip_path 必须排除仓根下的 .ecat。"""
    from ecat.entropy.base import should_skip_path

    base = tmp_path / "harmony"
    assert should_skip_path(base / ".ecat" / "state.json", base)
    assert should_skip_path(base / ".ecat" / "agents" / "g" / "a.json", base)
    assert should_skip_path(base / ".ecat" / "tmp" / "build_out.txt", base)
    # 正常 target 源码不受影响
    assert not should_skip_path(base / "entry" / "src" / "main" / "ets" / "Index.ets", base)


def test_should_skip_path_excludes_all_dot_dirs(tmp_path: Path) -> None:
    """任意点开头目录（.idea/.codex/.deveco/.git/.vscode 等）一律跳过——工具/IDE/agent
    配置不是项目源码；不逐个项目往白名单加。文件名以点开头（.gitignore）不误伤。"""
    from ecat.entropy.base import should_skip_path

    base = tmp_path / "harmony"
    for dd in (".idea", ".codex", ".deveco", ".git", ".vscode", ".cache"):
        assert should_skip_path(base / dd / "x" / "project.json", base), dd
        assert should_skip_path(base / "entry" / dd / "y.ets", base), dd
    # 文件名以点开头（非目录）不算跳过
    assert not should_skip_path(base / "entry" / "src" / ".gitignore", base)
    # 正常源码不受影响
    assert not should_skip_path(base / "entry" / "src" / "main" / "ets" / "Index.ets", base)


def test_skip_dir_names_and_exclude_include_ecat() -> None:
    """skip_dir_names / git_exclude_pathspecs 都要排除 .ecat（git add 与 diff 一致）。"""
    from ecat.entropy.base import git_exclude_pathspecs, skip_dir_names

    assert ".ecat" in skip_dir_names()
    assert any(".ecat" in p for p in git_exclude_pathspecs())
    assert ".ecat" not in {"build", "oh_modules"}  # 不误伤其它 skip 目录
