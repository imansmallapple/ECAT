"""crash_risk 插件单测。

重点锁的是**反假阳性行为**，不是"能不能报出来" —— 这轮在 8 个真实迁移仓上跑下来，
出问题的从来不是检出，而是误报：注释里的示例代码、组件委托、build 目录里的陈旧
副本、多 struct 文件里取错 build()。这些一旦回归就会污染 H 值误导 Generator。
"""

from __future__ import annotations

from pathlib import Path

from ecat.entropy.plugins.a2h_verify.check_crash_risk import CrashRiskPlugin


def _mk(tmp_path: Path, files: dict[str, str], strings: list[str] | None = None) -> Path:
    ets = tmp_path / "entry" / "src" / "main" / "ets"
    for rel, body in files.items():
        p = ets / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    res = tmp_path / "entry" / "src" / "main" / "resources" / "base" / "element"
    res.mkdir(parents=True, exist_ok=True)
    items = ", ".join(f'{{"name": "{n}", "value": "v"}}' for n in (strings or ["ok"]))
    (res / "string.json").write_text(f'{{"string": [{items}]}}', encoding="utf-8")
    return tmp_path


def _rules(details: dict) -> dict[str, int]:
    return dict(details.get("by_rule", {}))


# ── R7: 全局兜底 ──────────────────────────────────────────────────────────────

def test_r7_fires_when_no_global_handler(tmp_path: Path):
    root = _mk(tmp_path, {"pages/Index.ets": "@Entry\n@Component\nstruct Index {\n"
                                             "  build() { Column() {} }\n}\n"})
    _, details = CrashRiskPlugin().evaluate(root)
    assert _rules(details).get("R7_no_global_handler") == 1


def test_r7_silent_when_handler_registered(tmp_path: Path):
    root = _mk(tmp_path, {
        "pages/Index.ets": "@Entry\n@Component\nstruct Index {\n"
                           "  build() { Column() {} }\n}\n",
        "entryability/EntryAbility.ets":
            "import { errorManager } from '@kit.AbilityKit';\n"
            "export default class EntryAbility {\n"
            "  onCreate() { errorManager.on('error', {}); }\n}\n",
    })
    _, details = CrashRiskPlugin().evaluate(root)
    assert "R7_no_global_handler" not in _rules(details)


def test_r7_ignores_handler_mentioned_only_in_comment(tmp_path: Path):
    """实测 sonnet48 仓唯一的 errorManager.on 就长在文档注释里 —— 不剥注释会漏报。"""
    root = _mk(tmp_path, {
        "pages/Index.ets": "@Entry\n@Component\nstruct Index {\n"
                           "  build() { Column() {} }\n}\n",
        "CrashHandler.ets":
            "/**\n * HarmonyOS errorManager.on('error') / errorManager.on('globalError')\n"
            " * would go here.\n */\n"
            "export class CrashHandler {\n  static install(): void {}\n}\n",
    })
    _, details = CrashRiskPlugin().evaluate(root)
    assert _rules(details).get("R7_no_global_handler") == 1


# ── R1: @Provide / @Consume ───────────────────────────────────────────────────

_CONSUMER = ("@Component\nexport struct Leaf {\n"
             "  @Consume('navPathStack') stack: NavPathStack;\n"
             "  build() { Column() {} }\n}\n")


def test_r1_fires_when_no_provider_on_path(tmp_path: Path):
    root = _mk(tmp_path, {
        "pages/Leaf.ets": _CONSUMER,
        "pages/Index.ets": "@Entry\n@Component\nstruct Index {\n"
                           "  build() { Column() { Leaf() } }\n}\n",
    })
    _, details = CrashRiskPlugin().evaluate(root)
    assert _rules(details).get("R1_consume_unresolved") == 1


def test_r1_silent_when_ancestor_provides(tmp_path: Path):
    root = _mk(tmp_path, {
        "pages/Leaf.ets": _CONSUMER,
        "pages/Index.ets": "@Entry\n@Component\nstruct Index {\n"
                           "  @Provide('navPathStack') stack: NavPathStack = new NavPathStack();\n"
                           "  build() { Column() { Leaf() } }\n}\n",
    })
    _, details = CrashRiskPlugin().evaluate(root)
    assert "R1_consume_unresolved" not in _rules(details)


def test_r1_silent_when_no_entry_struct(tmp_path: Path):
    """拿不到入口就判定不了祖先链 —— 证据不足必须跳过，不能瞎报。"""
    root = _mk(tmp_path, {"pages/Leaf.ets": _CONSUMER})
    _, details = CrashRiskPlugin().evaluate(root)
    assert "R1_consume_unresolved" not in _rules(details)


# ── R2: NavDestination 根节点 ─────────────────────────────────────────────────

def _nav_project(page_body: str, extra: dict[str, str] | None = None) -> dict[str, str]:
    files = {
        "pages/Index.ets":
            "@Builder\nfunction pageMap(name: string) {\n"
            "  if (name === 'Detail') {\n    Detail()\n  }\n}\n\n"
            "@Entry\n@Component\nstruct Index {\n"
            "  build() {\n    Navigation() {\n      Column() {}\n"
            "    }.navDestination(pageMap)\n  }\n}\n",
        "pages/Detail.ets": page_body,
    }
    files.update(extra or {})
    return files


def test_r2_fires_when_root_is_not_navdestination(tmp_path: Path):
    root = _mk(tmp_path, _nav_project(
        "@Component\nexport struct Detail {\n"
        "  build() { Stack() { NavDestination() { Column() {} } } }\n}\n"))
    _, details = CrashRiskPlugin().evaluate(root)
    assert _rules(details).get("R2_navdest_not_root") == 1


def test_r2_silent_when_root_is_navdestination(tmp_path: Path):
    root = _mk(tmp_path, _nav_project(
        "@Component\nexport struct Detail {\n"
        "  build() { NavDestination() { Column() {} } }\n}\n"))
    _, details = CrashRiskPlugin().evaluate(root)
    assert "R2_navdest_not_root" not in _rules(details)


def test_r2_silent_when_delegating_to_navdestination_component(tmp_path: Path):
    """实测 Meshtastic 的 ThemePickerPage 就是纯转发给 ThemeConfigPage，运行时合法。"""
    root = _mk(tmp_path, _nav_project(
        "@Component\nexport struct Detail {\n  build() { Inner() }\n}\n",
        extra={"pages/Inner.ets":
               "@Component\nexport struct Inner {\n"
               "  build() { NavDestination() { Column() {} } }\n}\n"},
    ))
    _, details = CrashRiskPlugin().evaluate(root)
    assert "R2_navdest_not_root" not in _rules(details)


def test_r2_silent_when_builder_wraps_the_page(tmp_path: Path):
    files = _nav_project("@Component\nexport struct Detail {\n"
                         "  build() { Column() {} }\n}\n")
    files["pages/Index.ets"] = files["pages/Index.ets"].replace(
        "    Detail()\n", "    NavDestination() { Detail() }\n")
    root = _mk(tmp_path, files)
    _, details = CrashRiskPlugin().evaluate(root)
    assert "R2_navdest_not_root" not in _rules(details)


# ── R8: CustomDialogController 作用域 ─────────────────────────────────────────

_DLG = ("@CustomDialog\nexport struct Confirm {\n"
        "  controller: CustomDialogController;\n"
        "  build() { Button('ok').onClick(() => { this.controller.close(); }) }\n}\n")


def _host(stmt: str) -> str:
    return ("@Entry\n@Component\nstruct Index {\n"
            f"  open(): void {{\n    {stmt}\n    c.open();\n  }}\n"
            "  build() { Column() {} }\n}\n")


def test_r8_fires_for_local_controller_in_component_method(tmp_path: Path):
    root = _mk(tmp_path, {
        "pages/Confirm.ets": _DLG,
        "pages/Index.ets": _host(
            "const c: CustomDialogController = new CustomDialogController("
            "{ builder: Confirm() });"),
    })
    _, details = CrashRiskPlugin().evaluate(root)
    assert _rules(details).get("R8_dialog_controller_local") == 1


def test_r8_silent_for_member_controller(tmp_path: Path):
    root = _mk(tmp_path, {
        "pages/Confirm.ets": _DLG,
        "pages/Index.ets": _host(
            "this.c = new CustomDialogController({ builder: Confirm() });"),
    })
    _, details = CrashRiskPlugin().evaluate(root)
    assert "R8_dialog_controller_local" not in _rules(details)


def test_r8_silent_for_field_initializer(tmp_path: Path):
    """`private c: CustomDialogController = new ...` 是合法成员变量形态，不能报。"""
    root = _mk(tmp_path, {
        "pages/Confirm.ets": _DLG,
        "pages/Index.ets": "@Entry\n@Component\nstruct Index {\n"
                           "  private c: CustomDialogController = "
                           "new CustomDialogController({ builder: Confirm() });\n"
                           "  build() { Column() {} }\n}\n",
    })
    _, details = CrashRiskPlugin().evaluate(root)
    assert "R8_dialog_controller_local" not in _rules(details)


def test_r8_silent_when_constructed_outside_any_struct(tmp_path: Path):
    """模块级函数里构造 → open() 当场失败，症状是"点了没反应"而非闪退。

    实测该 app 的"版本更新"就是这一类，混进 crash_risk 就是假阳性。
    """
    root = _mk(tmp_path, {
        "pages/Confirm.ets": _DLG,
        "CheckUpdate.ets":
            "export function show(): void {\n"
            "  const c: CustomDialogController = new CustomDialogController("
            "{ builder: Confirm() });\n  c.open();\n}\n",
        "pages/Index.ets": "@Entry\n@Component\nstruct Index {\n"
                           "  build() { Column() {} }\n}\n",
    })
    _, details = CrashRiskPlugin().evaluate(root)
    assert "R8_dialog_controller_local" not in _rules(details)


def test_r8_silent_when_dialog_never_touches_controller(tmp_path: Path):
    """字段没被注入但也没人碰，就不会抛 —— 这条连接"注入失败"与"实际触碰"。"""
    root = _mk(tmp_path, {
        "pages/Confirm.ets": "@CustomDialog\nexport struct Confirm {\n"
                             "  controller: CustomDialogController;\n"
                             "  onDone: () => void = () => {};\n"
                             "  build() { Button('ok').onClick(() => { this.onDone(); }) }\n}\n",
        "pages/Index.ets": _host(
            "const c: CustomDialogController = new CustomDialogController("
            "{ builder: Confirm() });"),
    })
    _, details = CrashRiskPlugin().evaluate(root)
    assert "R8_dialog_controller_local" not in _rules(details)


# ── R5 / R6: 资源 ─────────────────────────────────────────────────────────────

def test_r5_flags_missing_string_resource(tmp_path: Path):
    root = _mk(tmp_path, {
        "pages/Index.ets": "@Entry\n@Component\nstruct Index {\n"
                           "  build() { Text($r('app.string.nope')) }\n}\n",
    }, strings=["other"])
    _, details = CrashRiskPlugin().evaluate(root)
    assert _rules(details).get("R5_string_res_blank") == 1


def test_r5_silent_for_existing_resource(tmp_path: Path):
    root = _mk(tmp_path, {
        "pages/Index.ets": "@Entry\n@Component\nstruct Index {\n"
                           "  build() { Text($r('app.string.here')) }\n}\n",
    }, strings=["here"])
    _, details = CrashRiskPlugin().evaluate(root)
    assert "R5_string_res_blank" not in _rules(details)


def test_r6_flags_missing_resource_name(tmp_path: Path):
    root = _mk(tmp_path, {
        "pages/Index.ets": "@Entry\n@Component\nstruct Index {\n"
                           "  label(): string {\n"
                           "    return getContext(this).resourceManager"
                           ".getStringByNameSync('gone');\n  }\n"
                           "  build() { Column() {} }\n}\n",
    }, strings=["other"])
    _, details = CrashRiskPlugin().evaluate(root)
    assert _rules(details).get("R6_res_name_missing") == 1


# ── WorkItem 契约 ─────────────────────────────────────────────────────────────

def test_work_items_carry_skill_and_keep_directive_after_clipping(tmp_path: Path):
    root = _mk(tmp_path, {
        "pages/Leaf.ets": _CONSUMER,
        "pages/Index.ets": "@Entry\n@Component\nstruct Index {\n"
                           "  build() { Column() { Leaf() } }\n}\n",
    })
    plugin = CrashRiskPlugin()
    score, details = plugin.evaluate(root)
    items = plugin.extract_work_items(score, details)
    leaf = next(i for i in items if i.file.name == "Leaf.ets")
    assert leaf.severity == "high"
    assert "arkts-state-manager" in leaf.suggested_skills
    # 行号只能靠 issue 文本传给 Generator（driver 的 formatter 不读 WorkItem.lines）
    assert ":3" in leaf.issues[0]
    # 尾部指令必须活过截断，否则 Generator 会去空读安卓源码
    assert leaf.issues[0].rstrip().endswith("do not consult the Android source for this fix.")


def test_same_rule_findings_are_aggregated_into_one_issue(tmp_path: Path):
    body = "@Entry\n@Component\nstruct Index {\n  build() {\n    Column() {\n" + "".join(
        f"      Text($r('app.string.miss{i}'))\n" for i in range(9)
    ) + "    }\n  }\n}\n"
    root = _mk(tmp_path, {"pages/Index.ets": body}, strings=["other"])
    plugin = CrashRiskPlugin()
    score, details = plugin.evaluate(root)
    assert _rules(details).get("R5_string_res_blank") == 9
    item = next(i for i in plugin.extract_work_items(score, details)
                if i.file.name == "Index.ets")
    r5 = [s for s in item.issues if "app.string" in s]
    assert len(r5) == 1           # 9 条发现合并成 1 条 issue
    assert "miss0" in r5[0]
    assert item.lines is not None and len(item.lines) == 9


def test_empty_project_scores_zero(tmp_path: Path):
    (tmp_path / "entry").mkdir()
    score, details = CrashRiskPlugin().evaluate(tmp_path)
    assert score == 0.0
    assert details.get("skipped") == "no_ets_files"
