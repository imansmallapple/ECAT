"""entry_nav —— 入口与导航完整性检查（CHECK-11，硬+agentic 复合）。

迁移项目最常见的"启动黑屏"问题就是入口/路由不一致。本插件覆盖入口页质量、
路由注册一致性、Tab 导航完整度、入口图标资源四大类。

具体检查项：
  入口页质量：
    1. 通过 module.json5 找到 EntryAbility 真正的入口 page 文件
    2. 入口 page 不能是 "Hello World" 模板默认
    3. 入口 page 的 build() 不能是空体或单叶子组件（黑屏 / 单按钮）
  Entry/main_pages 一致性（HarmonyOS 强约束，不一致即启动黑屏）：
    4. 解析所有 *Ability.ets 中的 windowStage.loadContent('pages/Xxx')
    5. 该 'pages/Xxx' 必须在 main_pages.json 的 src 数组中注册
  P0 路由覆盖：
    6. ui-manifest.md 中所有 P0 优先级页面都必须在路由表中可达
    7. 路由 target 文件必须真实存在
  导航边覆盖：
    8. spec/baseline/ui-graph/Navigation_Edge.json 中 MainActivity 的子页都要有 ArkTS 实现
  Tab 导航：
    9. TabContent 数量与 spec 期望一致
    10. 每个 Tab 都有 tabBar 图标
  入口图标资源：
    11. 所有 $r('app.media.xxx') 引用的图标资源真实存在
    12. 图标不是占位（PNG 1x1 像素；SVG 无绘制元素/路径退化为≤1个点/含 TODO|placeholder 标记）
    13. Android res/drawable / res/mipmap 中的图标在 HarmonyOS media/ 都有对应

适用于任何 Android→HarmonyOS 迁移——所有判断都是 HarmonyOS 标准约定。
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

from ecat.entropy.base import EntropyPlugin, should_skip_path
from ecat.entropy.plugins.a2h_verify.check_crash_risk import _blank_comments
from ecat.entropy.spec_parser import parse_navigation_graph, parse_source_mapping, parse_ui_manifest
from ecat.gradient.schema import WorkItem

if TYPE_CHECKING:
    from ecat.entropy.spec_parser import PageEntry

log = logging.getLogger(__name__)


class EntryNavPlugin(EntropyPlugin):
    """Audit entry page for Hello World, route coverage, icon completeness."""

    name = "entry_nav"
    default_weight = 0.10
    judge_mode = "agentic"
    judge_prompt = """\
Assess the HarmonyOS entry page navigation completeness.

Check for these issues:
- Navigation/Tabs/router exists but actual reachable pages are too few or empty
- Tabs that don't navigate to real implemented pages (stubs or placeholders)
- Routes registered in main_pages.json but target files missing or trivial
- Entry page has UI but no real navigation structure (single-page app skeleton)
- Navigation targets are duplicated or point to the same page

HarmonyOS navigation architecture — do NOT mis-flag the modern pattern:
- `Navigation` + `NavPathStack` register their destinations in the page's
  `pageMap` (a WrappedBuilder map wired via `.navDestination(this.pageMap)`),
  NOT in `main_pages.json`. Only the `loadContent('pages/Xxx')` host page must
  appear in `main_pages.json`. Do NOT report "pages missing from main_pages.json",
  "no system-level route registration", or "deep-linking/back-nav broken" for
  pages reached through a NavPathStack pageMap — that is the correct idiom, not
  a defect. `NavPathStack` provides OS-managed back navigation natively.
  Conversely, extra or name-mismatched main_pages.json entries for pages routed
  via the pageMap are at most a low-severity naming lint — NOT dead/broken routing.
- A `Tabs` component nested inside the root page's `Navigation`/`NavDestination`
  is valid; per-tab `NavPathStack`s keep the tab bar visible across pushes.
- One parameterized page serving multiple tabs (e.g. a media page taking a
  filter prop) is correct reuse, not duplication.
- A page used as an embedded `@Component` (`Xxx()` in another page's build) is
  reachable via composition, NOT a dead route — confirm no `<Name>(` usage before
  calling a pageMap-registered page "never navigated".
Judge by the page actually loaded via `loadContent` (the structural root), not
by the *Ability file.

For icon quality, use ONLY `details.icons.placeholder_icons` (hard-check output). \
Do NOT flag SVGs by byte size.

Score from 0.0 (navigation fully working) to 1.0 (navigation broken/missing):
- 0.0-0.2: all tabs/nav targets lead to real, implemented pages
- 0.2-0.5: navigation structure exists but some targets are stubs or missing
- 0.5-0.8: navigation skeleton only, most targets not implemented
- 0.8-1.0: no real navigation, single page or broken routes"""

    def extract_work_items(
        self, score: float, details: dict[str, object]
    ) -> list[WorkItem]:
        items: list[WorkItem] = []
        entry_file = details.get("entry_file")
        path = Path(str(entry_file)) if entry_file else Path("pages/Index.ets")
        # NavDestination-root fails carry the offending page's real path; emit
        # WorkItems keyed on THAT file so parallel subagent dispatch routes
        # each fix to its own page. Cross-app generic — uses only the
        # structured details from check_navdestination_root.
        navdest_files: set[str] = set()
        navdest_detail = details.get("missing_nav_destination", {})
        if isinstance(navdest_detail, dict):
            for bad in navdest_detail.get("missing_nav_destination", []):
                if not isinstance(bad, dict):
                    continue
                bad_file = str(bad.get("file", ""))
                struct = bad.get("struct", "?")
                root = bad.get("root", "?")
                navdest_files.add(bad_file)
                items.append(WorkItem(
                    file=Path(bad_file) if bad_file else path,
                    issues=[
                        f"@Entry struct {struct} build() root is {root}, not "
                        f"NavDestination. Pages pushed via "
                        f"NavPathStack.pushPathByName render blank. FIX: wrap "
                        f"the existing root in "
                        f"`NavDestination() {{ ... }}.hideTitleBar(true)`."
                    ],
                    suggested_skills=["hmos-arkui-develop-skill"],
                    severity="high",
                ))
        for fail in details.get("fails", []):
            # Skip navdest fails — already emitted above with correct file.
            if "not NavDestination" in str(fail):
                continue
            items.append(WorkItem(
                file=path,
                issues=[str(fail)],
                suggested_skills=["hmos-arkui-develop-skill"],
                severity="high",
            ))
        for warn in details.get("warns", []):
            items.append(WorkItem(
                file=path,
                issues=[str(warn)],
                suggested_skills=["hmos-arkui-develop-skill"],
                severity="medium",
            ))
        icons = details.get("icons", {})
        if isinstance(icons, dict):
            missing_res = [m for m in icons.get("missing_resources", []) if isinstance(m, str)]
            if missing_res:
                items.append(WorkItem(
                    file=Path("resources/base/media"),
                    issues=[f"Missing icon resources ({len(missing_res)}): "
                            + ", ".join(missing_res[:8])
                            + (f" ... +{len(missing_res)-8} more" if len(missing_res) > 8 else "")
                            + " — copy from Android res/drawable or res/mipmap"],
                    suggested_skills=["hmos-arkui-develop-skill"],
                    severity="high",
                ))
            ph_names: list[str] = []
            for ph in icons.get("placeholder_icons", []):
                if isinstance(ph, dict):
                    ph_names.append(ph.get("name", "?"))
            if ph_names:
                items.append(WorkItem(
                    file=Path("resources/base/media"),
                    issues=[f"Placeholder icons ({len(ph_names)}): "
                            + ", ".join(ph_names[:8])
                            + (f" ... +{len(ph_names)-8} more" if len(ph_names) > 8 else "")
                            + " — replace with real icons from Android res"],
                    suggested_skills=["hmos-arkui-develop-skill"],
                    severity="high",
                ))
            dup_groups = [g for g in icons.get("duplicate_icons", []) if isinstance(g, list) and len(g) > 1]
            if dup_groups:
                dup_desc = "; ".join(", ".join(g[:3]) for g in dup_groups[:4])
                items.append(WorkItem(
                    file=Path("resources/base/media"),
                    issues=[f"Duplicate icon groups ({len(dup_groups)}): {dup_desc} — deduplicate from Android res"],
                    suggested_skills=["hmos-arkui-develop-skill"],
                    severity="medium",
                ))
            android_missing = [ic for ic in icons.get("android_icons_missing", []) if isinstance(ic, str)]
            if android_missing:
                items.append(WorkItem(
                    file=Path("resources/base/media"),
                    issues=[f"Android icons without HarmonyOS equivalent ({len(android_missing)}): "
                            + ", ".join(android_missing[:8])
                            + (f" ... +{len(android_missing)-8} more" if len(android_missing) > 8 else "")
                            + " — convert from Android res"],
                    suggested_skills=["hmos-arkui-develop-skill"],
                    severity="high",
                ))
            orphan = [ic for ic in icons.get("orphan_icons", []) if isinstance(ic, str)]
            if orphan:
                items.append(WorkItem(
                    file=Path("entry/src/main/ets"),
                    issues=[f"Orphan icons ({len(orphan)}): "
                            + ", ".join(orphan[:8])
                            + (f" ... +{len(orphan)-8} more" if len(orphan) > 8 else "")
                            + " — file in media/ but no $r() ref; wire into matching menuItem/toolbar Image"],
                    suggested_skills=[],
                    severity="medium",
                ))

        # Launcher-icon misuses: one work_item per file (not per button).
        misuses_by_file: dict[str, list[dict[str, object]]] = {}
        for m in details.get("launcher_icon_misuses", []):
            if isinstance(m, dict):
                misuses_by_file.setdefault(str(m.get("file", "")), []).append(m)
        for file_path, ms in misuses_by_file.items():
            items.append(WorkItem(
                file=Path(file_path) if file_path else Path("."),
                issues=[f"{len(ms)} nav button(s) use non-icon glyphs "
                        f"({', '.join(set(str(x['icon']) for x in ms))}) — "
                        f"convert from Android res into resources/base/media/, not Text('<char>')"],
                suggested_skills=["hmos-arkui-develop-skill"],
                severity="medium",
            ))

        # Thin nav targets: button works but its target page renders blank.
        # One work_item per target page, listing the upstream call sites so the
        # Generator can verify both ends.
        thin_targets = details.get("nav_target_substance", {}).get("thin_targets", []) \
            if isinstance(details.get("nav_target_substance"), dict) else []
        by_target: dict[str, list[dict[str, object]]] = {}
        for t in thin_targets:
            if isinstance(t, dict):
                by_target.setdefault(str(t.get("target_file", ".")), []).append(t)
        for tgt_file, hits in by_target.items():
            sources = ", ".join(f"{h['source']}:{h['line']}" for h in hits[:3])
            more = f" (+{len(hits)-3} more)" if len(hits) > 3 else ""
            count = hits[0].get("component_count", 0)
            items.append(WorkItem(
                file=Path(tgt_file),
                issues=[f"{tgt_file}: build() renders only {count} substantive component(s); "
                        f"navigated from {sources}{more} — likely empty-shell regression"],
                suggested_skills=[],
                severity="high",
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
        fails: list[str] = []
        warns: list[str] = []
        details: dict[str, object] = {}

        # Step 1: Locate entry file
        entry_file, entry_content = _find_entry_file(harmony_dir)
        details["entry_file"] = str(entry_file) if entry_file else None

        if entry_file is None or entry_content is None:
            return 1.0, {
                **details,
                "fails": ["Entry page file not found"],
                "warns": [],
            }

        # Resolve the real UI host: when the entry is an *Ability file, follow
        # loadContent('pages/Xxx') to the page whose build() carries the actual
        # Tabs/Navigation structure. UI-structure checks (hello-world, tabs) must
        # run on that page, not on the Ability (which has no UI).
        ui_host_file, ui_host_content = _resolve_ui_host(
            harmony_dir, entry_file, entry_content,
        )
        details["ui_host_file"] = str(ui_host_file) if ui_host_file else None

        # Step 2: Hello World / blank detection
        hw_result = _check_hello_world(ui_host_content)
        details["hello_world"] = hw_result
        if hw_result["has_hello_world"]:
            fails.append("Entry page contains Hello World text")
        if hw_result["build_empty"]:
            fails.append("Entry page build() is empty or trivial")
        if hw_result["single_leaf"]:
            warns.append("Entry page build() has only a single leaf component")

        # Step 2b: EntryAbility.loadContent target ↔ main_pages.json consistency.
        # HarmonyOS requires every loadContent('pages/Xxx') target to be listed
        # in main_pages.json, otherwise the launcher gets a blank screen because
        # the page route cannot be resolved at startup.
        load_check = _check_entry_load_target(harmony_dir)
        details["entry_load"] = load_check
        for fail in load_check.get("fails", []):
            fails.append(fail)
        for warn in load_check.get("warns", []):
            warns.append(warn)

        # Step 2c: if Android ships an app widget, HarmonyOS must register a
        # service-card (Form) — the config page/provider source existing is not
        # enough; without the module.json5 form extensionAbility no card renders.
        widget_check = _check_widget_form_parity(harmony_dir, android_dir)
        details["widget_form"] = widget_check
        for fail in widget_check.get("fails", []):
            fails.append(fail)
        for warn in widget_check.get("warns", []):
            warns.append(warn)

        # Step 3: P0 route coverage
        pages = parse_ui_manifest(spec_dir) if spec_dir else []
        p0_pages = [p for p in pages if p.priority == "P0"]

        nav_graph = parse_navigation_graph(spec_dir) if spec_dir else {"edges": [], "parent_child_nav": {}}
        source_pages = parse_source_mapping(spec_dir) if spec_dir else []
        android_to_arkts = {
            p.extras.get("android_class", "").split("/")[-1]: p.name
            for p in source_pages if p.extras.get("android_class")
        }

        if p0_pages:
            route_result = _check_route_coverage(harmony_dir, entry_file, p0_pages)
            details["route_coverage"] = route_result
            missing_routes: list[str] = route_result.get("missing_routes", [])  # type: ignore[assignment]
            for miss in missing_routes:
                fails.append(f"P0 page {miss} not registered in route table")
            missing_targets: list[str] = route_result.get("missing_targets", [])  # type: ignore[assignment]
            for miss in missing_targets:
                fails.append(f"Route {miss} target file missing")

        # Step 3b: Navigation graph edge coverage
        parent_child = nav_graph.get("parent_child_nav", {})
        main_children_raw = parent_child.get("MainActivity", []) if isinstance(parent_child, dict) else []
        main_children: list[str] = list(main_children_raw) if isinstance(main_children_raw, list) else []
        if main_children and android_to_arkts:
            expected_nav_pages = [
                android_to_arkts.get(child, child) for child in main_children
            ]
            nav_edge_result = _check_nav_edge_coverage(harmony_dir, expected_nav_pages)
            details["nav_edge_coverage"] = nav_edge_result
            missing_nav: list[str] = nav_edge_result.get("missing", [])  # type: ignore[assignment]
            for miss in missing_nav:
                fails.append(f"Navigation target {miss} not found in harmony code")

        # Step 3c: launcher-template icons used as in-app nav buttons.
        # Kept separate from fails/warns to avoid scoring saturation when
        # one app reuses the same launcher icon dozens of times — each
        # affected file becomes one work_item via extract_work_items().
        launcher_misuses = _check_launcher_icon_misuse(harmony_dir)
        details["launcher_icon_misuses"] = launcher_misuses[:30]

        # Step 4: Icon completeness
        icon_result = _check_icon_completeness(harmony_dir, android_dir=android_dir)
        details["icons"] = icon_result
        missing_resources: list[str] = icon_result.get("missing_resources", [])  # type: ignore[assignment]
        for miss in missing_resources:
            fails.append(f"Icon resource {miss} not found")
        placeholder_icons: list[dict[str, str]] = icon_result.get("placeholder_icons", [])  # type: ignore[assignment]
        for ph in placeholder_icons:
            if isinstance(ph, dict):
                fails.append(f"Icon {ph.get('name', '?')} is a placeholder: {ph.get('reason', 'unknown')}")
            elif isinstance(ph, str):
                fails.append(f"Icon {ph} is a placeholder")
        duplicate_icons: list[list[str]] = icon_result.get("duplicate_icons", [])  # type: ignore[assignment]
        for group in duplicate_icons:
            if isinstance(group, list) and len(group) > 1:
                warns.append(f"Icons share identical content (likely same placeholder): {', '.join(group)}")
        android_missing: list[str] = icon_result.get("android_icons_missing", [])  # type: ignore[assignment]
        details["android_icons_missing_count"] = len(android_missing)
        orphan_icons: list[str] = icon_result.get("orphan_icons", [])  # type: ignore[assignment]
        details["orphan_icons_count"] = len(orphan_icons)

        # Step 5: Tab coverage (on the real UI host, not the *Ability file)
        tab_result = _check_tab_coverage(ui_host_content, spec_dir)
        details["tabs"] = tab_result
        if tab_result.get("tabs_without_icon"):
            tabs_no_icon: list[int] = tab_result["tabs_without_icon"]  # type: ignore[assignment]
            for idx in tabs_no_icon:
                warns.append(f"Tab {idx} missing icon")

        # Step 5b: Tab count vs expected main nav targets
        if main_children and tab_result.get("has_tabs"):
            expected_count = len(main_children)
            actual_count: int = tab_result.get("tab_count", 0)  # type: ignore[assignment]
            if actual_count != expected_count:
                warns.append(
                    f"Tab count mismatch: {actual_count} tabs vs "
                    f"{expected_count} expected main nav targets"
                )
                details["tab_mismatch"] = {
                    "expected": expected_count,
                    "actual": actual_count,
                    "expected_targets": main_children,
                }

        # Step 5c: Custom tabBar() content must wire onClick → changeIndex.
        # Cross-app rule: when .tabBar() argument is a custom @Component or
        # @Builder (not the SDK's BottomTabBarStyle / SubTabBarStyle), ArkUI
        # does NOT auto-bind click-to-switch; the developer must wire it.
        # Failure mode is silent — tabs render, but clicks do nothing.
        tab_click_result = _check_tab_click_wiring(harmony_dir)
        details["tab_click_wiring"] = tab_click_result
        for miss in tab_click_result.get("missing_click_wiring", []):
            if isinstance(miss, dict):
                target = miss.get("target", "?")
                kind = miss.get("kind", "?")
                file = miss.get("file", "?")
                line = miss.get("line", "?")
                if kind == "component":
                    # ArkUI does not hit-test through an @Component wrapper —
                    # `.onClick` on the instance in the caller is a false fix.
                    fails.append(
                        f"Tab uses @Component {target} at {file}:{line}. Add "
                        f".onClick inside the file that `export struct {target}` "
                        f"(or add an `onTap` prop called from its root node). "
                        f"`.onClick` on the {target}({{...}}) wrapper does NOT "
                        f"work — silent no-op."
                    )
                else:
                    fails.append(
                        f"Tab @Builder {target} in {file}:{line} missing "
                        f".onClick(() => this.tabsController.changeIndex(<idx>)) "
                        f"on its root node — silent no-op."
                    )

        # Step 5d: @Entry page build() root must be NavDestination (or
        # Navigation for the root container). Pages pushed via
        # NavPathStack.pushPathByName render BLANK if their build() root is a
        # plain Stack/Column/Row/etc. ArkUI contract — cross-app generic.
        navdest_result = _check_navdestination_root(harmony_dir)
        details["missing_nav_destination"] = navdest_result
        for bad in navdest_result.get("missing_nav_destination", []):
            if isinstance(bad, dict):
                fails.append(
                    f"@Entry page {bad.get('file', '?')} (struct "
                    f"{bad.get('struct', '?')}) build() root is "
                    f"{bad.get('root', '?')}, not NavDestination. Pages pushed "
                    f"via NavPathStack.pushPathByName render blank without a "
                    f"NavDestination root. FIX: wrap the existing root in "
                    f"`NavDestination() {{ ... }}.hideTitleBar(true)`."
                )

        # Scoring. android_missing, launcher_misuses, and orphan_icons each
        # contribute to the score but are not added to fails/warns so they
        # don't double-count in extract_work_items.  Launcher misuses and
        # orphan icons cap separately since both are UX issues, not
        # functional breaks — file ships and page renders, just without
        # the icon the user expects.
        launcher_pen = min(0.15, len(launcher_misuses) * 0.01)
        orphan_pen = min(0.15, len(orphan_icons) * 0.02)
        score = min(
            1.0,
            len(fails) * 0.25 + len(warns) * 0.05
            + len(android_missing) * 0.15 + launcher_pen + orphan_pen,
        )

        details["nav_target_substance"] = _check_nav_target_substance(harmony_dir)
        details["fails"] = fails
        details["warns"] = warns
        return score, details


# ── Step 1: Entry file discovery ────────────────────────────────────────────


def _check_widget_form_parity(
    harmony_dir: Path,
    android_dir: Path | None,
) -> dict[str, object]:
    """If Android ships a home-screen widget, the HarmonyOS port must register a
    service-card (Form). Having the config-page / provider .ets source is NOT
    enough: without an `extensionAbilities` entry of type "form" in a module.json5,
    no card renders and the widget is dead.

    FP-averse + cross-app generic (no app-specific names):
      - Fires ONLY when Android genuinely declares a widget — a class extending
        `AppWidgetProvider` (the sole way to ship one). Apps without a widget →
        empty result, contributes 0. (Strict match; ignores `android.widget.*`
        view imports.)
      - Fails ONLY when NO module.json5 anywhere declares a form extensionAbility.
        If one exists it passes; a missing forms profile is a warn, not a fail.
        Form-detection accepts JSON5 single OR double quotes so a registered form
        is never mis-read as missing.
      - Missing android_dir / module.json5 → skip (never a spurious fail).
    """
    result: dict[str, object] = {
        "android_has_widget": False,
        "harmony_form_registered": False,
        "fails": [],
        "warns": [],
    }
    fails: list[str] = result["fails"]  # type: ignore[assignment]
    warns: list[str] = result["warns"]  # type: ignore[assignment]

    if android_dir is None or not android_dir.exists():
        return result

    # 1. Android widget signal: a subclass of AppWidgetProvider.
    appwidget_re = re.compile(r"(?::\s*|extends\s+)AppWidgetProvider\b")
    android_has_widget = False
    for ext in (".kt", ".java"):
        for f in android_dir.rglob(f"*{ext}"):
            try:
                if appwidget_re.search(f.read_text(encoding="utf-8", errors="ignore")):
                    android_has_widget = True
                    break
            except OSError:
                continue
        if android_has_widget:
            break
    result["android_has_widget"] = android_has_widget
    if not android_has_widget:
        return result  # nothing to require

    # 2. HarmonyOS Form registration: any module.json5 with an extensionAbilities
    #    entry of type "form" (JSON5 comments stripped; quote-agnostic).
    form_re = re.compile(r"""["']type["']\s*:\s*["']form["']""")
    form_registered = False
    for mj in harmony_dir.rglob("module.json5"):
        if should_skip_path(mj, harmony_dir):
            continue  # 排除 oh_modules/build/.idea 等依赖/缓存目录
        try:
            raw = mj.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        cleaned = re.sub(r"//.*", "", raw)
        cleaned = re.sub(r"/\*.*?\*/", "", cleaned, flags=re.DOTALL)
        if form_re.search(cleaned):
            form_registered = True
            break
    result["harmony_form_registered"] = form_registered

    if not form_registered:
        fails.append(
            'Android ships a home-screen widget (AppWidgetProvider) but no '
            'module.json5 registers a Form extensionAbility (type: "form") — the '
            'service card cannot render; porting only the config page/provider '
            'source is not enough.'
        )
    else:
        profile_dir = (
            harmony_dir / "entry" / "src" / "main" / "resources" / "base" / "profile"
        )
        forms_re = re.compile(r"""["']forms["']\s*:""")
        has_form_profile = False
        if profile_dir.exists():
            for p in profile_dir.glob("*.json"):
                try:
                    if forms_re.search(p.read_text(encoding="utf-8", errors="ignore")):
                        has_form_profile = True
                        break
                except OSError:
                    continue
        if not has_form_profile:
            warns.append(
                'Form extensionAbility registered but no forms profile JSON '
                '(with a "forms" array) found under resources/base/profile.'
            )
    return result


def _check_entry_load_target(harmony_dir: Path) -> dict[str, object]:
    """Verify EntryAbility's loadContent target is registered in main_pages.json.

    HarmonyOS contract: every page string passed to `windowStage.loadContent(...)`
    must appear in the `src` array of `entry/src/main/resources/base/profile/
    main_pages.json` (or the project-specific routerMap file referenced by
    module.json5). If the launcher requests a page that isn't registered, the
    framework cannot resolve it at startup and the user sees a blank window.

    This check is HarmonyOS-generic — no app-specific names. It parses the
    standard *Ability.ets and main_pages.json paths only.

    Returns:
      {
        "load_targets": list[str],          # e.g. ["pages/Index"]
        "registered_pages": list[str],      # contents of main_pages.json src
        "fails": list[str],                 # blocking inconsistencies
        "warns": list[str],                 # non-blocking observations
      }
    """
    result: dict[str, object] = {
        "load_targets": [],
        "registered_pages": [],
        "fails": [],
        "warns": [],
    }
    fails: list[str] = result["fails"]  # type: ignore[assignment]
    warns: list[str] = result["warns"]  # type: ignore[assignment]

    # 1. Collect every loadContent('pages/Xxx') across *Ability.ets files.
    ets_root = harmony_dir / "entry" / "src" / "main" / "ets"
    load_targets: list[str] = []
    if ets_root.exists():
        for ability_file in ets_root.rglob("*Ability.ets"):
            try:
                content = ability_file.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            # Match windowStage.loadContent('pages/Foo' [, ...])
            for m in re.finditer(
                r"loadContent\s*\(\s*['\"]([^'\"]+)['\"]",
                content,
            ):
                load_targets.append(m.group(1))
    result["load_targets"] = load_targets
    if not load_targets:
        warns.append("No *Ability.ets calls loadContent — entry page indeterminate")
        return result

    # 2. Read main_pages.json src list (the canonical HarmonyOS page registry).
    profile_path = (
        harmony_dir / "entry" / "src" / "main"
        / "resources" / "base" / "profile" / "main_pages.json"
    )
    registered_pages: list[str] = []
    if profile_path.exists():
        try:
            raw = profile_path.read_text(encoding="utf-8", errors="ignore")
            cleaned = re.sub(r"//.*", "", raw)
            cleaned = re.sub(r"/\*.*?\*/", "", cleaned, flags=re.DOTALL)
            data = json.loads(cleaned)
            src_list = data.get("src", []) if isinstance(data, dict) else []
            if isinstance(src_list, list):
                registered_pages = [str(s) for s in src_list if isinstance(s, str)]
        except Exception as exc:
            warns.append(f"main_pages.json unreadable: {exc}")
    else:
        fails.append("main_pages.json missing — every page route will fail to resolve")
        return result
    result["registered_pages"] = registered_pages

    # 3. Cross-check.
    registered_set = set(registered_pages)
    for target in load_targets:
        if target not in registered_set:
            fails.append(
                f"EntryAbility.loadContent('{target}') target is NOT in "
                f"main_pages.json — app will launch to a blank screen"
            )

    # 4. Sanity warn: page file under ets/pages/ exists but not registered.
    pages_dir = harmony_dir / "entry" / "src" / "main" / "ets" / "pages"
    if pages_dir.is_dir():
        ets_pages = {f"pages/{f.stem}" for f in pages_dir.glob("*.ets")}
        unregistered = ets_pages - registered_set
        if unregistered and len(unregistered) <= 5:
            # Only warn if it's a small set — large unregistered groups usually
            # mean the project has ets files that genuinely aren't routes.
            warns.append(
                "Page file(s) exist under ets/pages/ but absent from main_pages.json: "
                + ", ".join(sorted(unregistered))
            )

    return result


def _find_entry_file(harmony_dir: Path) -> tuple[Path | None, str | None]:
    module_json = harmony_dir / "entry" / "src" / "main" / "module.json5"
    pages_dir = harmony_dir / "entry" / "src" / "main" / "ets" / "pages"

    if module_json.exists():
        try:
            raw = module_json.read_text(encoding="utf-8")
            cleaned = re.sub(r"//.*", "", raw)
            cleaned = re.sub(r"/\*.*?\*/", "", cleaned, flags=re.DOTALL)
            parsed = json.loads(cleaned)
            module = parsed.get("module", {})
            abilities = module.get("abilities", [])
            for ability in abilities:
                src_entry = ability.get("srcEntry", "")
                if src_entry:
                    candidate = harmony_dir / "entry" / "src" / "main" / src_entry.lstrip("./")
                    if candidate.exists():
                        content = candidate.read_text(encoding="utf-8", errors="ignore")
                        return candidate, content
        except Exception:
            pass

    # Heuristic fallback
    if pages_dir.exists():
        for name in ("Index.ets", "MainPage.ets", "Main.ets", "Home.ets"):
            candidate = pages_dir / name
            if candidate.exists():
                return candidate, candidate.read_text(encoding="utf-8", errors="ignore")
        ets_files = sorted(pages_dir.glob("*.ets"))
        if ets_files:
            f = ets_files[0]
            return f, f.read_text(encoding="utf-8", errors="ignore")

    return None, None


def _resolve_ui_host(
    harmony_dir: Path, entry_file: Path | None, entry_content: str | None,
) -> tuple[Path | None, str]:
    """Follow an *Ability* entry's `loadContent('pages/Xxx')` to the real UI
    host page — the one whose `build()` declares the Tabs/Navigation structure.

    `_find_entry_file` resolves module.json5 `srcEntry` to the Ability file
    (e.g. EntryAbility.ets), which has NO UI: running Tabs/structure detection
    on it yields a false `has_tabs=false`. The Ability hands the UI to a page
    via `windowStage.loadContent('pages/Index')`; that page is the structural
    root the checks must analyse. Returns the host page (path, content); falls
    back to the original entry when it isn't an Ability or the target can't be
    resolved. Generic HarmonyOS convention — no project-specific names.
    """
    if not entry_content:
        return entry_file, entry_content or ""
    m = re.search(r"loadContent\s*\(\s*['\"]([^'\"]+)['\"]", entry_content)
    if not m:
        return entry_file, entry_content
    target = m.group(1).lstrip("./")  # e.g. "pages/Index"
    ets_root = harmony_dir / "entry" / "src" / "main" / "ets"
    for cand in (ets_root / f"{target}.ets", ets_root / target / "Index.ets"):
        if cand.is_file():
            try:
                return cand, cand.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                break
    return entry_file, entry_content


# ── Step 2: Hello World / blank detection ───────────────────────────────────


def _check_hello_world(content: str) -> dict[str, object]:
    # 剥注释后再匹配：注释里写 "Hello World"（如文档示例）不是真实的模板页，
    # 不剥会把注释当硬失败误报（同文件其它检查也剥注释，这里对齐）。
    has_hw = bool(re.search(r"Hello[,\s]+World", _blank_comments(content), re.IGNORECASE))

    # UIAbility classes (extends UIAbility) don't have a build() method by design —
    # they use onWindowStageCreate instead. Skip the build() emptiness check entirely.
    if re.search(r"class\s+\w+\s+extends\s+UIAbility\b", content):
        return {
            "has_hello_world": has_hw,
            "build_empty": False,
            "build_lines": -1,
            "single_leaf": False,
        }

    build_body = _extract_build_body(content)
    stripped = re.sub(r"//.*", "", build_body)
    stripped = re.sub(r"/\*.*?\*/", "", stripped, flags=re.DOTALL)
    lines = [line.strip() for line in stripped.splitlines() if line.strip()]
    build_empty = len(lines) <= 3

    single_leaf = False
    if not build_empty:
        components = re.findall(r"\b(Text|Image|Button|Column|Row)\s*\(", build_body)
        if len(components) == 1:
            single_leaf = True

    return {
        "has_hello_world": has_hw,
        "build_empty": build_empty,
        "build_lines": len(lines),
        "single_leaf": single_leaf,
    }


def _extract_build_body(content: str) -> str:
    match = re.search(r"\bbuild\s*\(\s*\)\s*\{", content)
    if not match:
        return ""
    start = match.end()
    depth = 1
    i = start
    while i < len(content) and depth > 0:
        if content[i] == "{":
            depth += 1
        elif content[i] == "}":
            depth -= 1
        i += 1
    return content[start:i - 1] if depth == 0 else content[start:]


# ── Step 3: Route coverage ──────────────────────────────────────────────────


# Cross-app generic: uses only HarmonyOS NEXT standard nav APIs + ArkUI built-in
# container names. No app-specific class/path/string tokens.
_NAV_CALL_RE = re.compile(
    r"pushPath(?:ByName)?\s*\([^)]*?['\"](\w+)['\"]"
    r"|pushDestination\s*\([^)]*?['\"](\w+)['\"]"
    r"|router\.pushUrl\s*\(\s*\{[^}]*url\s*:\s*['\"]pages/(\w+)['\"]"
)
_LAYOUT_CONTAINERS = frozenset({
    "Column", "Row", "Stack", "Flex", "RelativeContainer", "WaterFlow", "Scroll",
    "FlowItem", "ListItem", "GridItem", "TabContent", "Navigation", "NavDestination",
    "Tab", "SwiperContent",
})
_PASCAL_CALL_RE = re.compile(r"\b([A-Z][A-Za-z0-9_]+)\s*\(")


def _check_nav_target_substance(harmony_dir: Path) -> dict[str, object]:
    """Flag nav targets whose build() renders <2 distinct non-container components.

    Catches "button alive, target blank" regressions.
    """
    pages_dir = harmony_dir / "entry" / "src" / "main" / "ets"
    if not pages_dir.exists():
        return {"thin_targets": []}
    page_files: dict[str, Path] = {}
    for f in pages_dir.rglob("*.ets"):
        page_files.setdefault(f.stem, f)

    cache: dict[str, tuple[int, Path] | None] = {}
    thin: list[dict[str, object]] = []
    for src_file in pages_dir.rglob("*.ets"):
        try:
            text = src_file.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        # Strip // and /* */ comments before scanning so that nav call patterns
        # appearing inside JavaDoc / line-comment strings (e.g.
        # `* pushed via pushPathByName('Foo')`) don't register as real calls.
        # Replace non-newline chars with spaces so byte offsets and line numbers
        # stay aligned with the original file.
        scan_text = re.sub(
            r"//[^\n]*|/\*.*?\*/",
            lambda mm: re.sub(r"[^\n]", " ", mm.group(0)),
            text,
            flags=re.DOTALL,
        )
        for m in _NAV_CALL_RE.finditer(scan_text):
            target = next((g for g in m.groups() if g), None)
            if not target:
                continue
            # Skip self-reference: page X claiming to navigate to itself is
            # always a structural false positive (typically docstring metadata).
            if src_file.stem == target or src_file.stem == target + "Page":
                continue
            if target not in cache:
                tgt = page_files.get(target) or page_files.get(target + "Page")
                if tgt is None:
                    cache[target] = None
                else:
                    try:
                        full_text = tgt.read_text(encoding="utf-8", errors="ignore")
                    except OSError:
                        cache[target] = None
                        continue
                    body = _extract_build_body(full_text)
                    body = re.sub(r"//[^\n]*|/\*.*?\*/", "", body, flags=re.DOTALL)
                    comps = {
                        c.group(1) for c in _PASCAL_CALL_RE.finditer(body)
                        if c.group(1) not in _LAYOUT_CONTAINERS
                    }
                    # @Builder-pattern pages put UI in this.builderMethod() calls
                    # invoked from build(). @Builder is the ArkTS standard
                    # decorator — universal across HarmonyOS projects, no app-
                    # specific tokens. If the file declares any @Builder,
                    # count distinct `this.X()` invocations in build() body
                    # as substantive components too.
                    if re.search(r"@Builder\b", full_text):
                        builder_calls = {
                            mm.group(1) for mm in
                            re.finditer(r"\bthis\.(\w+)\s*\(", body)
                        }
                        comps |= builder_calls
                    # Thin-wrapper exemption: a tiny page that proxies a single
                    # custom component is a legitimate ArkTS architecture
                    # (e.g. N entry pages sharing one Template — widget configs,
                    # tab branches, A/B variants). Cross-app generic — uses
                    # structural metrics, no app-specific tokens.
                    is_thin_wrapper = (
                        len(full_text.splitlines()) < 30
                        and len(body.strip()) < 350
                        and len(comps) == 1
                    )
                    if is_thin_wrapper:
                        comps.add("__thin_wrapper__")  # bump to ≥2 → exempt
                    cache[target] = (len(comps), tgt)
            entry = cache[target]
            if entry is not None and entry[0] < 2:
                thin.append({
                    "source": str(src_file.relative_to(harmony_dir)),
                    "line": scan_text[:m.start()].count("\n") + 1,
                    "target": target,
                    "target_file": str(entry[1].relative_to(harmony_dir)),
                    "component_count": entry[0],
                })
    return {"thin_targets": thin}


def _check_route_coverage(
    harmony_dir: Path,
    entry_file: Path,
    p0_pages: list[PageEntry],
) -> dict[str, object]:
    pages_dir = harmony_dir / "entry" / "src" / "main" / "ets"
    route_names: set[str] = set()

    # Scan main_pages.json / router config for declared routes
    for profile_path in (
        harmony_dir / "entry" / "src" / "main" / "resources" / "base" / "profile" / "main_pages.json",
        harmony_dir / "entry" / "src" / "main" / "resources" / "base" / "profile" / "route_map.json",
        harmony_dir / "entry" / "src" / "main" / "ets" / "main_pages.json",
    ):
        if profile_path.exists():
            try:
                raw = profile_path.read_text(encoding="utf-8", errors="ignore")
                cleaned = re.sub(r"//.*", "", raw)
                cleaned = re.sub(r"/\*.*?\*/", "", cleaned, flags=re.DOTALL)
                data = json.loads(cleaned)
                _collect_route_names_from_json(data, route_names)
            except Exception:
                pass

    # Scan all .ets files for route registrations
    ets_stems: set[str] = set()
    for f in pages_dir.rglob("*.ets") if pages_dir.exists() else []:
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        ets_stems.add(f.stem.lower())
        # NavPathStack pushPath / pushDestination
        route_names.update(re.findall(r"pushPath\s*\(\s*\{[^}]*name\s*:\s*['\"](\w+)['\"]", text))
        route_names.update(re.findall(r"pushDestination\s*\(\s*\{[^}]*name\s*:\s*['\"](\w+)['\"]", text))
        # @Builder route map entries
        route_names.update(re.findall(r"['\"](\w+)['\"].*NavDestination", text))
        route_names.update(re.findall(r"NavDestination.*['\"](\w+)['\"]", text))
        # router.pushUrl
        route_names.update(re.findall(r"router\.pushUrl\s*\(\s*\{[^}]*url\s*:\s*['\"]([^'\"]+)['\"]", text))

    route_lower = {r.lower() for r in route_names}
    route_stems = {r.rsplit("/", 1)[-1].replace(".ets", "").lower() for r in route_names}

    missing_routes: list[str] = []
    missing_targets: list[str] = []

    for page in p0_pages:
        name_lower = page.name.lower().replace(" ", "")
        page_id_lower = page.page_id.lower()
        name_no_page = name_lower.replace("page", "")
        found = (
            name_lower in route_stems
            or name_no_page in route_stems
            or name_lower in ets_stems
            or name_no_page in ets_stems
            or any(name_lower in r or page_id_lower in r or r in name_lower for r in route_lower)
            or (page.target_file and (pages_dir / page.target_file).exists())
        )
        if not found:
            missing_routes.append(f"{page.page_id} ({page.name})")

    return {
        "registered_routes": sorted(route_names),
        "ets_files": sorted(ets_stems),
        "p0_total": len(p0_pages),
        "missing_routes": missing_routes,
        "missing_targets": missing_targets,
    }


def _collect_route_names_from_json(data: object, names: set[str]) -> None:
    """Recursively extract route/page names from main_pages.json or route_map.json."""
    if isinstance(data, dict):
        for key in ("name", "pageName", "routeName", "buildFunction"):
            val = data.get(key)
            if isinstance(val, str) and val:
                names.add(val)
        for key in ("pageSourceFile", "src"):
            val = data.get(key)
            if isinstance(val, str) and val:
                stem = val.rsplit("/", 1)[-1].replace(".ets", "")
                names.add(stem)
            elif isinstance(val, list):
                for item in val:
                    if isinstance(item, str) and item:
                        stem = item.rsplit("/", 1)[-1].replace(".ets", "")
                        names.add(stem)
        for v in data.values():
            _collect_route_names_from_json(v, names)
    elif isinstance(data, list):
        for item in data:
            _collect_route_names_from_json(item, names)


# ── Step 3c: launcher icon misuse ─────────────────────────────────────────


# DevEco template launcher icon names. App-agnostic — these come from the
# project template, not from any specific app.
_LAUNCHER_ICON_NAMES = frozenset({
    "foreground", "background", "startIcon", "app_icon", "layered_image",
    "ic_launcher", "ic_launcher_foreground", "ic_launcher_background",
})
_LAUNCHER_MISUSE_RE = re.compile(
    r"Image\s*\(\s*\$r\s*\(\s*['\"]app\.media\.(\w+)['\"]\s*\)\s*\)"
    r"[\s\S]{0,500}?\.onClick\s*\(",
)
# `Text('🗑')` etc. used as nav button — emoji glyphs are font-dependent and
# may render as tofu on device. Cross-app generic: targets only the high-risk
# Unicode blocks (Pictographs U+1F300+ and Misc Symbols U+2600-U+26FF).
# Excludes Dingbats (U+2700-U+27BF) — ✓ ✗ ✎ are font-reliable like × ⋮.
# Excludes Latin Supplement / General Punctuation / Math Operators — ‹ › × ⋮ all safe.
_SYMBOL_BUTTON_RE = re.compile(
    r"Text\s*\(\s*['\"]"
    r"([\U0001F300-\U0001FAFF☀-⛿])"
    r"['\"]\s*\)[\s\S]{0,500}?\.onClick\s*\(",
)


def _check_launcher_icon_misuse(harmony_dir: Path) -> list[dict[str, object]]:
    """Nav buttons that aren't real icons: launcher template icons OR unicode symbols."""
    pages_root = harmony_dir / "entry" / "src" / "main" / "ets"
    if not pages_root.exists():
        return []
    misuses: list[dict[str, object]] = []
    for ets_file in pages_root.rglob("*.ets"):
        try:
            text = ets_file.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        rel = str(ets_file.relative_to(harmony_dir))
        for m in _LAUNCHER_MISUSE_RE.finditer(text):
            if m.group(1) in _LAUNCHER_ICON_NAMES:
                misuses.append({
                    "file": rel, "line": text[:m.start()].count("\n") + 1,
                    "icon": m.group(1),
                })
        for m in _SYMBOL_BUTTON_RE.finditer(text):
            misuses.append({
                "file": rel, "line": text[:m.start()].count("\n") + 1,
                "icon": f"symbol '{m.group(1)}'",
            })
    return misuses


# ── Step 4: Icon completeness ──────────────────────────────────────────────


def _check_icon_completeness(
    harmony_dir: Path,
    *,
    android_dir: Path | None = None,
) -> dict[str, object]:
    pages_dir = harmony_dir / "entry" / "src" / "main" / "ets"
    media_dir = harmony_dir / "entry" / "src" / "main" / "resources" / "base" / "media"

    icon_refs: set[str] = set()
    if pages_dir.exists():
        for f in pages_dir.rglob("*.ets"):
            try:
                text = f.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            icon_refs.update(re.findall(r"\$r\s*\(\s*['\"]app\.media\.(\w+)['\"]", text))

    missing: list[str] = []
    placeholders: list[dict[str, str]] = []

    for icon_name in icon_refs:
        found = False
        for ext in (".png", ".svg", ".webp", ".jpg"):
            candidate = media_dir / f"{icon_name}{ext}"
            if candidate.exists():
                found = True
                reason = _check_icon_quality(candidate, ext)
                if reason:
                    placeholders.append({"name": icon_name, "reason": reason})
                break
        if not found:
            missing.append(icon_name)

    # Detect duplicate icons (same content hash → likely batch-generated placeholders)
    duplicates = _detect_duplicate_icons(media_dir, icon_refs)

    # Compare against Android drawable/mipmap icons
    android_missing = _compare_android_icons(android_dir, media_dir) if android_dir else []

    # Orphan icons: physical file in media/ that no ets code references.
    # Cross-checked against Android-side `@drawable/X` usage so we only
    # flag icons the Android UI *actually displays* — this is what catches
    # "icon file shipped but UI uses bare text" gaps (e.g. settings cog
    # exists in media/ but the SettingsPage entry is a Text-only menuItem).
    orphans = _check_orphan_icons(media_dir, pages_dir, icon_refs, android_dir)

    return {
        "icon_refs": sorted(icon_refs),
        "missing_resources": missing,
        "placeholder_icons": placeholders,
        "duplicate_icons": duplicates,
        "android_icons_missing": android_missing,
        "orphan_icons": orphans,
    }


# DevEco template / wizard assets that pages don't bind via $r().  Kept as
# a small allow-list; the load-bearing generalisation filter is on the
# Android side (_collect_page_scope_android_icons skips runtime layouts).
_NON_PAGE_ASSET_RE = re.compile(
    r"^(foreground|background|starticon|app_icon|layered_image|"
    r"ic_launcher(_\w+)?|sample_\w+)$",
    re.IGNORECASE,
)


# HarmonyOS SDK tokens for the system-symbol icon idiom. Project-agnostic.
_SYSTEM_SYMBOL_RE = re.compile(r"SymbolGlyph\s*\(|\$r\s*\(\s*['\"]sys\.symbol\.")
# Treat the project as having adopted system symbols once a handful of distinct
# call sites use them — a single stray usage shouldn't suppress orphan checks,
# but broad use signals system symbols are the chosen icon strategy.
_SYSTEM_SYMBOL_MIN_HITS = 3


def _uses_system_symbols(pages_dir: Path) -> bool:
    """True when the ets code renders icons via the HarmonyOS system-symbol font.

    Counts `SymbolGlyph(` / `$r('sys.symbol.X')` call sites across the page
    tree; returns True once the count clears `_SYSTEM_SYMBOL_MIN_HITS`. Generic
    HarmonyOS SDK detection — no app-specific identifiers.
    """
    if not pages_dir.exists():
        return False
    hits = 0
    for f in pages_dir.rglob("*.ets"):
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        hits += len(_SYSTEM_SYMBOL_RE.findall(text))
        if hits >= _SYSTEM_SYMBOL_MIN_HITS:
            return True
    return False


def _check_orphan_icons(
    media_dir: Path,
    pages_dir: Path,
    icon_refs: set[str],
    android_dir: Path | None,
) -> list[str]:
    """Find icons that exist as files but no ets page code references them.

    Filters cross-app generalisation hazards:
      1. Skip stems matching `_NON_PAGE_ASSET_RE` — launcher/widget/form/
         notification/splash assets that pages don't bind via `$r()`.
      2. Restrict to stems whose Android counterpart appears in a `page-scope`
         XML context (menu/*.xml or layout/*.xml whose filename does NOT
         start with `widget_`/`notification_`/`form_`). Widget/notification
         layouts are runtime-only on Android and have no page-code mirror
         on HarmonyOS.

    Only flags icons the Android UI actually displays in normal pages —
    these are the gaps where HarmonyOS code uses bare text where Android
    uses an icon button (e.g. `menuItem(text)` instead of `menuItem(icon, text)`).
    """
    if not media_dir.exists() or android_dir is None:
        return []
    # System-symbol adoption gate. HarmonyOS's idiomatic icon affordance is
    # `SymbolGlyph($r('sys.symbol.X'))` (the SF-style system symbol font), not
    # `Image($r('app.media.ic_X'))`. When a project renders its toolbar/action
    # icons through system symbols, the Android drawables copied into media/ are
    # superseded residue — present as files but correctly never bound via $r().
    # The orphan premise ("file shipped but UI fell back to bare text") does NOT
    # hold here: the affordance IS an icon, just a system glyph the file scan
    # can't see, so flagging it is a false positive. Once the project clearly
    # adopts system symbols, suppress orphan reporting. Cross-app generic —
    # `sys.symbol`/`SymbolGlyph` are HarmonyOS SDK tokens, not app-specific.
    if _uses_system_symbols(pages_dir):
        return []
    refs_lower = {r.lower() for r in icon_refs}
    page_scope_stems = {s.lower() for s in _collect_page_scope_android_icons(android_dir)}
    orphans: list[str] = []
    for f in media_dir.iterdir():
        if not f.is_file() or f.suffix.lower() not in (".png", ".svg", ".webp", ".jpg", ".jpeg"):
            continue
        stem = f.stem
        if _NON_PAGE_ASSET_RE.match(stem):
            continue
        stem_lower = stem.lower()
        if stem_lower in refs_lower:
            continue
        # Reverse the variant rules from _compare_android_icons so Harmony
        # `ic_settings_cog_vector` matches Android `ic_settings_cog_vector`,
        # and Harmony `arrow_back` matches Android `ic_arrow_back_vector`.
        variants = {
            stem_lower,
            f"ic_{stem_lower}",
            f"{stem_lower}_vector",
            f"ic_{stem_lower}_vector",
        }
        if variants & page_scope_stems:
            orphans.append(stem)
    return sorted(orphans)


def _collect_page_scope_android_icons(android_dir: Path) -> set[str]:
    """Subset of _collect_android_icon_names() restricted to page-UI context.

    Page-UI context = `res/menu/*.xml` + `res/layout/X.xml` where X does NOT
    start with `widget_`, `notification_`, `form_`, or `splash_` (those are
    runtime resources bound outside page code on both Android and HarmonyOS).
    """
    res_dirs: list[Path] = []
    for cand in (
        android_dir / "app" / "src" / "main" / "res",
        android_dir / "src" / "main" / "res",
        android_dir / "res",
    ):
        if cand.is_dir():
            res_dirs.append(cand)
            break
    names: set[str] = set()
    skip_layout_re = re.compile(r"^(widget|notification|form|splash)(_|$)")
    icon_attr_re = re.compile(
        r"(?:android|app):(?:icon|src|srcCompat|"
        r"drawableLeft|drawableRight|drawableTop|drawableBottom|"
        r"drawableStart|drawableEnd|tabIcon)\s*=\s*"
        r"\"@(?:drawable|mipmap)/([A-Za-z_][\w]*)\""
    )
    for res in res_dirs:
        for sub in res.iterdir():
            if not sub.is_dir():
                continue
            if sub.name != "menu" and not sub.name.startswith("layout"):
                continue
            for xml_file in sub.glob("*.xml"):
                if sub.name.startswith("layout") and skip_layout_re.match(xml_file.stem):
                    continue
                try:
                    text = xml_file.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                for m in icon_attr_re.finditer(text):
                    names.add(m.group(1))
    return names


def _check_icon_quality(path: Path, ext: str) -> str:
    """Return a reason string if the icon looks like a placeholder, else empty."""
    try:
        size = path.stat().st_size
    except OSError:
        return ""

    if size == 0:
        return "empty file (0 bytes)"

    if ext == ".svg":
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return ""
        if not re.search(r"<(path|rect|circle|ellipse|polygon|polyline|line|image|use)\b", content):
            return "SVG has no drawing elements"
        # Explicit placeholder marker in source or comment.
        if re.search(r"\b(placeholder|TODO|FIXME|stub)\b", content, re.IGNORECASE):
            return "SVG marked as placeholder/TODO in source"
        # Degenerate geometry — a path that draws nothing: empty (d="") or a
        # bare moveto with a single coordinate pair (d="M0 0"). Counting
        # coordinate NUMBERS (not characters) is what makes this FP-safe: a real
        # glyph needs ≥2 points (≥4 numbers) even for a simple play triangle
        # (d="M8 5v14l11-7z" → 5 numbers), while a single point yields ≤2. This
        # is robust to short-but-valid icons and to implicit linetos (extra
        # coordinate pairs after M). Do NOT flag by byte/char length.
        coord_nums = sum(
            len(re.findall(r"-?\d*\.?\d+", m.group(1)))
            for m in re.finditer(r'd\s*=\s*"([^"]*)"', content)
        )
        if coord_nums <= 2 and not re.search(r"<(rect|circle|ellipse|polygon|polyline|line|image|use)\b", content):
            return "SVG path draws nothing (≤1 point) — likely placeholder"
        return ""

    # PNG/WebP/JPG: only flag clearly degenerate dimensions.
    # Small-but-valid icons (simple glyphs, monochrome marks) are allowed.
    w, h = _read_image_dimensions(path, ext)
    if w is not None and h is not None and w == 1 and h == 1:
        return "1x1 pixel placeholder"

    return ""


def _read_image_dimensions(path: Path, ext: str) -> tuple[int | None, int | None]:
    """Read width/height from image header bytes without external libraries."""
    try:
        data = path.read_bytes()
    except OSError:
        return None, None

    if ext == ".png" and len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
        import struct
        w, h = struct.unpack(">II", data[16:24])
        return w, h

    if ext in (".jpg", ".jpeg") and len(data) >= 2 and data[:2] == b"\xff\xd8":
        return _read_jpeg_dimensions(data)

    if ext == ".webp" and len(data) >= 30 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        if data[12:16] == b"VP8 " and len(data) >= 30:
            import struct
            w = struct.unpack("<H", data[26:28])[0] & 0x3FFF
            h = struct.unpack("<H", data[28:30])[0] & 0x3FFF
            return w, h
        if data[12:16] == b"VP8L" and len(data) >= 25:
            import struct
            bits = struct.unpack("<I", data[21:25])[0]
            w = (bits & 0x3FFF) + 1
            h = ((bits >> 14) & 0x3FFF) + 1
            return w, h

    return None, None


def _read_jpeg_dimensions(data: bytes) -> tuple[int | None, int | None]:
    """Parse JPEG SOF marker to extract dimensions."""
    import struct
    i = 2
    while i < len(data) - 1:
        if data[i] != 0xFF:
            break
        marker = data[i + 1]
        if marker in (0xC0, 0xC1, 0xC2):
            if i + 9 < len(data):
                h, w = struct.unpack(">HH", data[i + 5 : i + 9])
                return w, h
            break
        if marker == 0xD9 or marker == 0xDA:
            break
        if i + 3 < len(data):
            length = struct.unpack(">H", data[i + 2 : i + 4])[0]
            i += 2 + length
        else:
            break
    return None, None


def _detect_duplicate_icons(
    media_dir: Path, icon_refs: set[str],
) -> list[list[str]]:
    """Find groups of referenced icons with identical file content."""
    import hashlib
    if not media_dir.exists():
        return []
    hash_to_names: dict[str, list[str]] = {}
    for icon_name in icon_refs:
        for ext in (".png", ".svg", ".webp", ".jpg"):
            candidate = media_dir / f"{icon_name}{ext}"
            if candidate.exists():
                try:
                    digest = hashlib.md5(candidate.read_bytes()).hexdigest()  # noqa: S324
                    hash_to_names.setdefault(digest, []).append(icon_name)
                except OSError:
                    pass
                break
    return [names for names in hash_to_names.values() if len(names) > 1]


_ICON_EXTS = frozenset((".png", ".svg", ".webp", ".jpg", ".jpeg", ".xml"))
_SKIP_PREFIXES = ("ic_launcher",)


def _compare_android_icons(
    android_dir: Path, harmony_media_dir: Path,
) -> list[str]:
    """Find Android drawable/mipmap icons that have no equivalent in HarmonyOS media."""
    android_icons = _collect_android_icon_names(android_dir)
    if not android_icons:
        return []

    harmony_stems: set[str] = set()
    if harmony_media_dir.exists():
        for f in harmony_media_dir.iterdir():
            if f.is_file() and f.suffix.lower() in _ICON_EXTS:
                harmony_stems.add(f.stem.lower())

    missing: list[str] = []
    for icon in sorted(android_icons):
        icon_lower = icon.lower()
        if any(icon_lower.startswith(p) for p in _SKIP_PREFIXES):
            continue
        name_variants = {
            icon_lower,
            icon_lower.replace("ic_", ""),
            icon_lower.replace("_vector", ""),
            icon_lower.replace("ic_", "").replace("_vector", ""),
        }
        if not name_variants & harmony_stems:
            missing.append(icon)

    return missing


def _collect_android_icon_names(android_dir: Path) -> set[str]:
    """Collect Android icon stems needed by the app.

    Two sources:
      1. Physical files under res/drawable*/ and res/mipmap*/
      2. `@drawable/X` / `@mipmap/X` references inside res/menu/*.xml and
         res/layout/*.xml (icon-semantic attributes only). Menu items often
         point at library-provided drawables (e.g. SimpleMobileTools commons
         `ic_settings_cog_vector`) with no physical file in app/res but
         still represent icons the migrated HarmonyOS app must supply.

    Shape/gradient/selector drawables are excluded by a stem blacklist
    built from physical XML inspection — apps frequently misuse
    `android:src="@drawable/some_background"` on an ImageView, so a
    purely attribute-based filter is insufficient.
    """
    names: set[str] = set()
    shape_stems: set[str] = set()
    res_dirs: list[Path] = []
    for res_candidate in (
        android_dir / "app" / "src" / "main" / "res",
        android_dir / "src" / "main" / "res",
        android_dir / "res",
    ):
        if res_candidate.is_dir():
            res_dirs.append(res_candidate)
            break

    for res_dir in res_dirs:
        for sub in res_dir.iterdir():
            if not sub.is_dir():
                continue
            if sub.name.startswith("drawable") or sub.name.startswith("mipmap"):
                for f in sub.iterdir():
                    if not f.is_file():
                        continue
                    if f.suffix.lower() not in _ICON_EXTS:
                        continue
                    if f.suffix.lower() == ".xml" and _is_shape_drawable(f):
                        shape_stems.add(f.stem)
                        continue
                    names.add(f.stem)
            elif sub.name == "menu" or sub.name.startswith("layout"):
                for xml_file in sub.glob("*.xml"):
                    try:
                        text = xml_file.read_text(encoding="utf-8", errors="ignore")
                    except OSError:
                        continue
                    for m in re.finditer(
                        r"(?:android|app):(?:icon|src|srcCompat|"
                        r"drawableLeft|drawableRight|drawableTop|drawableBottom|"
                        r"drawableStart|drawableEnd|tabIcon)\s*=\s*"
                        r"\"@(?:drawable|mipmap)/([A-Za-z_][\w]*)\"",
                        text,
                    ):
                        names.add(m.group(1))

    # Filter stems whose name signals "decoration not icon" — covers
    # lib-provided shape drawables that have no physical XML to inspect.
    decor_suffixes = ("_background", "_gradient", "_shape", "_divider", "_separator")
    decor_stems = {n for n in names if n.endswith(decor_suffixes)}
    return names - shape_stems - decor_stems


def _is_shape_drawable(xml_path: Path) -> bool:
    """Return True if an Android XML drawable is a shape/gradient/selector (not a vector icon)."""
    try:
        head = xml_path.read_text(encoding="utf-8", errors="ignore")[:500]
    except OSError:
        return False
    return bool(re.search(r"<(shape|gradient|layer-list|selector|ripple|inset|clip)\b", head))


# ── Step 5: Tab coverage ───────────────────────────────────────────────────


# Built-in tab-bar styles where ArkUI auto-binds click → changeIndex. Any other
# argument to .tabBar() is treated as a custom @Component/@Builder that must
# implement the click contract itself. SDK-standard names — cross-app generic.
_BUILTIN_TABBAR_STYLES = (
    "BottomTabBarStyle",
    "SubTabBarStyle",
    "BottomNavigationStyle",
)

# ArkUI SDK built-in components/containers — onClick on these IS reliably
# hit-tested by the framework. Anything PascalCase NOT in this set is treated
# as a user-defined @Component, whose internal root node must declare onClick
# (because ArkUI does not propagate hit-test through the @Component wrapper).
# This list intentionally only contains SDK-standard component names — no
# app-specific tokens — so the rule generalizes to any HarmonyOS app.
_BUILTIN_ARKUI_COMPONENTS = frozenset({
    # Layout containers
    "Column", "Row", "Stack", "Flex", "RelativeContainer", "WaterFlow", "Scroll",
    "FlowItem", "ListItem", "GridItem", "TabContent", "Navigation", "NavDestination",
    "Tab", "SwiperContent", "SideBarContainer", "List", "Grid", "Tabs", "Swiper",
    "Refresh", "ColumnSplit", "RowSplit", "Counter", "GridContainer", "Panel",
    # Leaf controls
    "Text", "Image", "Button", "TextInput", "TextArea", "Search", "Toggle",
    "Checkbox", "RadioButton", "Radio", "Slider", "Select", "DatePicker",
    "TimePicker", "TextPicker", "Progress", "LoadingProgress", "Rating",
    "Marquee", "RichText", "Hyperlink", "SymbolGlyph", "QRCode", "DataPanel",
    "Gauge", "Badge", "Web", "Video", "XComponent", "Divider", "Blank",
    "Menu", "MenuItem", "MenuItemGroup", "ActionSheet", "AlertDialog",
    "Circle", "Ellipse", "Line", "Polygon", "Polyline", "Path", "Rect", "Shape",
    "Span", "ImageSpan", "ContainerSpan",
})


def _has_tab_click_wiring(text: str) -> bool:
    """Returns True if text contains onClick wired to a switch action.

    Cross-app generic — uses only ArkUI SDK / ArkTS language tokens:
      - `.onClick(`  — ArkUI standard event handler
      - `changeIndex(` — TabsController.changeIndex() SDK method (definitive signal)
      - `this.X(` inside onClick — generic callback delegation; the item
        forwards the event to a parent-provided callback. We can't statically
        know what the parent does, so accept it as plausibly wired (avoids
        false positives at the cost of missing some pathological cases).
    No state-variable names or callback names are assumed — those vary by app.
    """
    clean = re.sub(r"//[^\n]*|/\*.*?\*/", "", text, flags=re.DOTALL)
    if not re.search(r"\.onClick\s*\(", clean):
        return False
    if re.search(r"\bchangeIndex\s*\(", clean):
        return True
    # Look for `this.<any>(` inside any onClick body.
    for m in re.finditer(r"\.onClick\s*\(\s*(?:\([^)]*\)\s*=>\s*)?\{", clean):
        # Scan ahead within the onClick callback body
        start = m.end()
        depth = 1
        i = start
        while i < len(clean) and depth > 0:
            if clean[i] == "{":
                depth += 1
            elif clean[i] == "}":
                depth -= 1
            i += 1
        body = clean[start:i - 1] if depth == 0 else clean[start:]
        if re.search(r"\bthis\.\w+\s*\(", body):
            return True
    return False


def _find_struct_source(ets_root: Path, struct_name: str) -> str | None:
    """Locate `export struct <Name>` body across all .ets files. ArkTS standard."""
    pat = re.compile(rf"\bexport\s+struct\s+{re.escape(struct_name)}\b")
    for f in ets_root.rglob("*.ets"):
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if pat.search(text):
            return text
    return None


def _extract_builder_body(text: str, builder_name: str) -> str | None:
    """Extract the body of `@Builder private? <name>(...) { ... }`. ArkTS standard."""
    pat = re.compile(
        rf"@Builder\b[^{{]*?\b{re.escape(builder_name)}\s*\([^)]*\)\s*\{{",
        re.DOTALL,
    )
    m = pat.search(text)
    if not m:
        return None
    start = m.end()
    depth = 1
    i = start
    while i < len(text) and depth > 0:
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
        i += 1
    return text[start:i - 1] if depth == 0 else None


def _check_tab_click_wiring(harmony_dir: Path) -> dict[str, object]:
    """Flag Tabs whose .tabBar() uses a custom @Component/@Builder that omits
    onClick → tabsController.changeIndex() wiring.

    Cross-app generic — relies only on ArkUI SDK tokens (Tabs, .tabBar,
    BottomTabBarStyle, SubTabBarStyle, changeIndex, onClick) and the universal
    ArkTS @Builder / @Component / export struct syntax.
    """
    ets_root = harmony_dir / "entry" / "src" / "main" / "ets"
    if not ets_root.exists():
        return {"missing_click_wiring": []}

    missing: list[dict[str, object]] = []
    tabbar_re = re.compile(r"\.tabBar\s*\(\s*([^)]+?)\s*\)", re.DOTALL)
    for f in ets_root.rglob("*.ets"):
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if "Tabs(" not in text or ".tabBar" not in text:
            continue
        clean = re.sub(r"//[^\n]*|/\*.*?\*/", "", text, flags=re.DOTALL)
        for m in tabbar_re.finditer(clean):
            arg = m.group(1).strip()
            if any(arg.startswith(s) for s in _BUILTIN_TABBAR_STYLES):
                continue
            # this.XxxBuilder(...) — a local @Builder
            builder_match = re.match(r"this\.(\w+)\s*\(", arg)
            target_name: str | None = None
            target_text: str | None = None
            kind = "component"
            if builder_match:
                target_name = builder_match.group(1)
                target_text = _extract_builder_body(clean, target_name)
                kind = "builder"
                if target_text is not None:
                    # If builder renders a custom @Component (not a builtin
                    # ArkUI container/control), the .onClick() applied on the
                    # component *instance* in the builder is NOT a reliable
                    # click handler — ArkUI does not hit-test through the
                    # @Component wrapper. The component's internal root node
                    # must declare onClick. So drill into the component source
                    # and check there; the builder's own outer .onClick is
                    # explicitly ignored when wrapping a custom @Component.
                    child = re.search(
                        r"^\s*([A-Z][A-Za-z0-9_]+)\s*\(", target_text, re.MULTILINE,
                    )
                    if child and child.group(1) not in _BUILTIN_ARKUI_COMPONENTS \
                            and not any(
                                child.group(1).startswith(s) for s in _BUILTIN_TABBAR_STYLES
                            ):
                        child_src = _find_struct_source(ets_root, child.group(1))
                        if child_src is not None:
                            if _has_tab_click_wiring(child_src):
                                continue
                            target_text = child_src
                            target_name = child.group(1)
                            kind = "component"
                        # else: external/unresolvable component — fall through
                    else:
                        # Builder content is built from raw ArkUI controls
                        # (Column/Row/Text/Image/etc) — outer .onClick on those
                        # IS valid. Check the builder body itself.
                        if _has_tab_click_wiring(target_text):
                            continue
            else:
                comp_match = re.match(r"([A-Z][A-Za-z0-9_]+)\s*\(", arg)
                if comp_match is None:
                    continue
                target_name = comp_match.group(1)
                target_text = _find_struct_source(ets_root, target_name)
            if target_text is None or _has_tab_click_wiring(target_text):
                continue
            missing.append({
                "file": str(f.relative_to(harmony_dir)),
                "line": clean[:m.start()].count("\n") + 1,
                "target": target_name,
                "kind": kind,
            })
    return {"missing_click_wiring": missing}


def _check_navdestination_root(harmony_dir: Path) -> dict[str, object]:
    """Flag @Entry pages whose build() root is neither NavDestination nor
    Navigation. ArkUI contract: pages pushed via NavPathStack.pushPathByName
    render BLANK if their root is a plain container (Stack/Column/Row/etc.).
    The root Navigation host is acceptable too (Index-style root page).
    Cross-app generic — only ArkTS/ArkUI SDK tokens.
    """
    pages_dir = harmony_dir / "entry" / "src" / "main" / "ets" / "pages"
    if not pages_dir.exists():
        return {"missing_nav_destination": []}
    entry_re = re.compile(
        r"@Entry[\s\S]*?@Component[\s\S]*?\bexport\s+struct\s+(\w+)[\s\S]*?\bbuild\s*\(\s*\)\s*\{"
    )
    bad: list[dict[str, object]] = []
    for f in pages_dir.glob("*.ets"):
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        m = entry_re.search(text)
        if m is None:
            continue
        struct_name = m.group(1)
        depth, i = 1, m.end()
        while i < len(text) and depth > 0:
            c = text[i]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
            i += 1
        body = text[m.end():i - 1]
        body = re.sub(r"//[^\n]*|/\*.*?\*/", "", body, flags=re.DOTALL)
        first = re.search(r"^\s*([A-Z][A-Za-z0-9_]+)\s*\(", body, re.MULTILINE)
        if first is None:
            continue
        root = first.group(1)
        # NavDestination = standard for pushed pages.
        # Navigation     = standard for the root page hosting the pageMap.
        if root in ("NavDestination", "Navigation"):
            continue
        # Per-tab Navigation app-shell exemption: an @Entry page whose build()
        # root is a plain container (Column/Stack/…) but which hosts a `Tabs`
        # and defines per-tab `Navigation(stack)` hosts is a valid multi-tab
        # shell, NOT a pushed destination — every pushPathByName targets a
        # per-tab NavPathStack that IS wrapped in Navigation, so nothing renders
        # blank. The Navigation hosts may live in `@Builder` tab-content methods
        # rather than inline in build(), so we look for them anywhere in the
        # (comment-stripped) file. Cross-app generic — pure ArkUI tokens; matches
        # what the agentic judge is already told to accept (plugin docstring).
        file_code = re.sub(r"//[^\n]*|/\*.*?\*/", "", text, flags=re.DOTALL)
        if re.search(r"\bTabs\s*\(", body) and re.search(r"\bNavigation\s*\(", file_code):
            continue
        bad.append({
            "file": str(f.relative_to(harmony_dir)),
            "struct": struct_name,
            "root": root,
        })
    return {"missing_nav_destination": bad}


def _check_tab_coverage(
    entry_content: str,
    spec_dir: Path | None,
) -> dict[str, object]:
    tab_matches = re.findall(r"TabContent\s*\(", entry_content)
    tab_count = len(tab_matches)

    if tab_count == 0:
        return {"has_tabs": False, "tab_count": 0}

    # Check which tabs have icons
    tabs_without_icon: list[int] = []
    tab_sections = re.split(r"TabContent\s*\(", entry_content)[1:]
    for idx, section in enumerate(tab_sections):
        brace_end = _find_matching_brace(section)
        tab_body = section[:brace_end] if brace_end > 0 else section[:500]
        if "tabBar" not in tab_body and "TabBar" not in tab_body:
            tabs_without_icon.append(idx)

    return {
        "has_tabs": True,
        "tab_count": tab_count,
        "tabs_without_icon": tabs_without_icon,
    }


def _find_matching_brace(text: str) -> int:
    depth = 0
    for i, ch in enumerate(text):
        if ch == "{":
            depth += 1
        elif ch == "}":
            if depth == 0:
                return i
            depth -= 1
    return -1


# ── Step 3b: Navigation edge coverage ────────────────────────────────────


def _check_nav_edge_coverage(
    harmony_dir: Path,
    expected_pages: list[str],
) -> dict[str, object]:
    """Check if expected navigation target pages exist as .ets files or route refs."""
    pages_dir = harmony_dir / "entry" / "src" / "main" / "ets" / "pages"
    ets_stems: set[str] = set()
    if pages_dir.exists():
        ets_stems = {f.stem.lower() for f in pages_dir.rglob("*.ets")}

    all_ets_text = ""
    ets_dir = harmony_dir / "entry" / "src" / "main" / "ets"
    if ets_dir.exists():
        for f in ets_dir.rglob("*.ets"):
            try:
                all_ets_text += f.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue

    found: list[str] = []
    missing: list[str] = []
    for page_name in expected_pages:
        name_lower = page_name.lower().replace("page", "").replace("activity", "").replace("fragment", "")
        if (
            page_name.lower() in ets_stems
            or name_lower in ets_stems
            or any(name_lower == s.replace("page", "") for s in ets_stems)
            or page_name in all_ets_text
            or name_lower in all_ets_text.lower()
        ):
            found.append(page_name)
        else:
            missing.append(page_name)

    return {
        "expected": expected_pages,
        "found": found,
        "missing": missing,
    }
