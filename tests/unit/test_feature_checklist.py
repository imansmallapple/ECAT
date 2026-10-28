"""feature_checklist 插件单测。

重点锁两类行为：
  1. **跳过语义** —— 未配置 / 文件不存在 / 解析不出内容时必须 skipped 且贡献 0，
     绝不能因为缺清单就把这一维刷成有问题。
  2. **解析泛化性** —— 实测手上 9 份清单存在三种形态差异，每一条都曾把解析带偏：
     尾部非层级列（GAP/说明/是否已实现）、首个 sheet 是封面报告、层级路径单列格式。
     这些一旦回归，功能名会取成 GAP 值或整表解析为空。
"""

from __future__ import annotations

import csv
import json
import re
import sys
from pathlib import Path

import pytest

from ecat.entropy.plugins.a2h_verify.check_feature_checklist import (
    FeatureChecklistPlugin,
    _load_checklist,
)

openpyxl = pytest.importorskip("openpyxl")


def _xlsx(tmp_path: Path, sheets: dict[str, list[list[str]]]) -> Path:
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for title, rows in sheets.items():
        ws = wb.create_sheet(title=title)
        for r in rows:
            ws.append(r)
    p = tmp_path / "checklist.xlsx"
    wb.save(p)
    return p


def _harmony(tmp_path: Path) -> Path:
    root = tmp_path / "hm"
    ets = root / "entry" / "src" / "main" / "ets" / "pages"
    ets.mkdir(parents=True)
    (ets / "Index.ets").write_text(
        "@Entry\n@Component\nstruct Index {\n"
        "  build() {\n    Tabs() {\n"
        "      TabContent() { Navigation(this.a) { HomePage() } }\n"
        "        .navDestination(pageMap)\n"
        "      TabContent() { Navigation(this.b) { MinePage() } }\n"
        "    }\n  }\n}\n"
        "@Builder\nfunction pageMap(name: string) {\n"
        "  if (name === 'DetailPage') { DetailPage() }\n"
        "  else if (name === 'OrphanPage') { OrphanPage() }\n}\n",
        encoding="utf-8")
    (ets / "MinePage.ets").write_text(
        "@Component\nexport struct MinePage {\n"
        "  go(): void { this.stack.pushPathByName('DetailPage', undefined); }\n"
        "  build() { Column() {} }\n}\n", encoding="utf-8")
    res = root / "entry" / "src" / "main" / "resources" / "base" / "element"
    res.mkdir(parents=True)
    (res / "string.json").write_text(
        '{"string": [{"name": "act_pay", "value": "立即开通"}]}',
        encoding="utf-8")
    return root


# ── 跳过语义 ──────────────────────────────────────────────────────────────────

def test_skips_when_not_configured(tmp_path: Path):
    score, d = FeatureChecklistPlugin().evaluate(_harmony(tmp_path), config={})
    assert score == 0.0
    assert d["skipped"] == "no_feature_checklist_configured"


def test_skips_when_file_missing(tmp_path: Path):
    score, d = FeatureChecklistPlugin().evaluate(
        _harmony(tmp_path), config={"feature_checklist": tmp_path / "nope.xlsx"})
    assert score == 0.0
    assert d["skipped"] == "feature_checklist_not_found"


def test_skips_when_unparsable(tmp_path: Path):
    p = _xlsx(tmp_path, {"s": [["随便", "写点东西"], ["没有", "表头"]]})
    score, d = FeatureChecklistPlugin().evaluate(
        _harmony(tmp_path), config={"feature_checklist": p})
    # 2 列无表头 → 走退化路径也能解析出来；这里只断言不炸
    assert "skipped" in d or d["checklist_total"] >= 1


# ── 解析泛化性 ────────────────────────────────────────────────────────────────

def test_parses_level_columns(tmp_path: Path):
    p = _xlsx(tmp_path, {"功能清单": [
        ["一级模块", "二级模块", "三级模块"],
        ["我的", "在线客服", "客服会话"],
        ["我的", "关于我们", "用户协议"],
    ]})
    feats = _load_checklist(p)
    assert [f.path_text for f in feats] == [
        "我的 > 在线客服 > 客服会话", "我的 > 关于我们 > 用户协议"]
    assert feats[0].name == "客服会话"
    assert feats[0].fid == "F001"


def test_trailing_non_level_columns_are_not_hierarchy(tmp_path: Path):
    """尾列 GAP/是否已实现 不能被当成最深一级 —— 否则功能名会变成 'yes'。"""
    p = _xlsx(tmp_path, {"功能清单": [
        ["一级模块", "二级模块", "三级模块", "来源", "说明", "GAP", "是否已实现"],
        ["设置", "自定义外观", "应用主题", "src/A.kt", "note", "yes", "no"],
    ]})
    feats = _load_checklist(p)
    assert len(feats) == 1
    assert feats[0].name == "应用主题"
    assert feats[0].path_text == "设置 > 自定义外观 > 应用主题"
    assert feats[0].meta["GAP"] == "yes"
    assert feats[0].meta["是否已实现"] == "no"


def test_sparse_middle_levels_keep_order(tmp_path: Path):
    p = _xlsx(tmp_path, {"功能清单": [
        ["一级模块", "二级模块", "三级模块", "四级模块"],
        ["首页", "切换文件夹视图", "", "返回"],
    ]})
    feats = _load_checklist(p)
    assert feats[0].path_text == "首页 > 切换文件夹视图 > 返回"


def test_picks_the_sheet_that_has_the_header(tmp_path: Path):
    """首个 sheet 是封面报告时必须继续往后找（实测 -L3 样本就是这样）。"""
    p = _xlsx(tmp_path, {
        "首页": [["测试报告"], ["创建时间", "2026-03-20"]],
        "功能完备度评估清单": [["一级模块", "二级模块"], ["首页", "队列"]],
    })
    feats = _load_checklist(p)
    assert len(feats) == 1
    assert feats[0].path_text == "首页 > 队列"


def test_parses_flat_path_format(tmp_path: Path):
    """第二种格式：层级路径单列 + 源码位置/控件标识符（antennapod / meshtatic）。"""
    p = _xlsx(tmp_path, {"功能清单": [
        ["ID", "功能名", "层级路径", "源码位置", "控件标识符", "交互类型", "优先级", "深度"],
        ["FC001", "播放暂停", "播放器 > 迷你播放器 > 播放暂停",
         "app/src/P.java", "btn_play", "click", "P0", "3"],
    ]})
    feats = _load_checklist(p)
    assert len(feats) == 1
    assert feats[0].path_text == "播放器 > 迷你播放器 > 播放暂停"
    assert feats[0].name == "播放暂停"
    assert feats[0].meta["控件标识符"] == "btn_play"
    assert feats[0].meta["优先级"] == "P0"


def test_slash_in_feature_name_is_not_a_separator(tmp_path: Path):
    """「导入/导出」是一个功能名，不能被切成两级。"""
    p = _xlsx(tmp_path, {"功能清单": [
        ["ID", "功能名", "层级路径"],
        ["FC001", "导入/导出", "设置 > 导入/导出"],
    ]})
    feats = _load_checklist(p)
    assert feats[0].path == ["设置", "导入/导出"]


def test_parses_csv_and_md(tmp_path: Path):
    c = tmp_path / "cl.csv"
    with c.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["一级模块", "二级模块"])
        w.writerow(["我的", "退出登录"])
    assert _load_checklist(c)[0].path_text == "我的 > 退出登录"

    m = tmp_path / "cl.md"
    m.write_text("| 一级模块 | 二级模块 |\n|---|---|\n| 我的 | 清除缓存 |\n",
                 encoding="utf-8")
    assert _load_checklist(m)[0].path_text == "我的 > 清除缓存"


# ── 证据包 ────────────────────────────────────────────────────────────────────

def test_evidence_pack_shape_and_budget(tmp_path: Path):
    rows = [["一级模块", "二级模块"]] + [["我的", f"功能{i}"] for i in range(60)]
    p = _xlsx(tmp_path, {"功能清单": rows})
    plugin = FeatureChecklistPlugin()
    score, d = plugin.evaluate(_harmony(tmp_path),
                               config={"feature_checklist": p, "iteration": 0})
    # 分数 = (total-OK)/total：一条都还没取证判 OK → 满分
    assert score == 1.0
    assert d["ok_count"] == 0
    assert d["checklist_total"] == 60
    # 全量遍历，不分批 —— 分批会让相邻两轮量的不是同一批功能，early-stop 失去意义
    assert "batch" not in d and "batch_index" not in d
    # 全量清单落盘，details 只放路径 —— judge 见不到全量就会按常识编造功能
    mf = Path(d["manifest"])
    assert mf.is_file()
    body = mf.read_text(encoding="utf-8")
    assert body.count("| F0") == 60, "manifest 必须含全部功能，不能是子集"
    assert "Only these features are in scope" in body
    assert d["nav"]["tab_order"] == ["HomePage", "MinePage"]
    assert "DetailPage" in d["nav"]["routes"]
    # OrphanPage 注册了、名字也没在别处出现过 → 最强嫌疑档
    assert "OrphanPage" in d["routes_no_entry"]
    assert "DetailPage" not in d["routes_no_entry"]
    assert "DetailPage" not in d["routes_indirect_entry"]
    import json
    assert len(json.dumps(d, ensure_ascii=False, default=str)) < 3000


def test_full_checklist_scanned_every_iteration(tmp_path: Path):
    """每轮都覆盖全清单，且结果与 iteration 无关。

    早先按 iteration 轮转批次，导致相邻两轮判的不是同一批功能，
    "H 连续 2 轮低于阈值即收敛"这条早停规则因此失去意义。
    """
    rows = [["一级模块", "二级模块"]] + [["我的", f"功能{i}"] for i in range(60)]
    p = _xlsx(tmp_path, {"功能清单": rows})
    plugin = FeatureChecklistPlugin()
    hm = _harmony(tmp_path)
    seen = []
    for it in (0, 1, 7):
        _, d = plugin.evaluate(hm, config={"feature_checklist": p, "iteration": it})
        assert d["checklist_total"] == 60
        seen.append((d["anchored"], Path(d["manifest"]).read_text(encoding="utf-8")))
    assert len(set(seen)) == 1, f"结果随 iteration 变化了: {seen}"


def test_plugin_is_agentic_and_opt_out_by_default():
    p = FeatureChecklistPlugin()
    assert p.judge_mode == "agentic"
    # 分数取自 judge 的 verdict（hw=0）：evaluate 先于 judge 跑，iter 0 的 manifest
    # 是空的，拿它当分数会把"发现最多的一轮"记成 0 分。
    assert p.hard_weight == 0.0
    # judge 必须拿到机械的计分口径，否则它会随手填一个数（实测 1713：29 行 ISSUE 却报 0.03）
    assert "(Total - OK) / Total" in p.judge_prompt
    # 无清单时靠 evaluate 的 skipped + judge 早退保护，不再依赖 weight 0
    assert p.default_weight > 0.0
    assert p.default_fix_skill       # agentic 路径下 extractors 会用它填 suggested_skills
    assert "skipped" in p.judge_prompt   # 跳过时必须让 judge 早退，避免凭空编 issue


def test_indirect_entry_softens_the_unreachable_claim(tmp_path: Path):
    """动态 push（路由名为变量）时不能断言不可达。

    实测该 app 有三种间接入口全被字面量正则漏掉：SplashPage 经 onNavigate 回调
    透传、EntryAbility 把路由名放进对象字段再 pushPathByName(变量)。二值断言会把
    引导页 7 条 + 快捷退订/退款 6 条误报成不可达。
    """
    root = _harmony(tmp_path)
    ability = root / "entry" / "src" / "main" / "ets" / "entryability"
    ability.mkdir(parents=True)
    (ability / "EntryAbility.ets").write_text(
        "export default class EntryAbility {\n"
        "  onCreate(): void {\n"
        "    const r = { pageName: 'OrphanPage' };\n"
        "    this.stack.pushPathByName(r.pageName, undefined);\n"
        "  }\n}\n", encoding="utf-8")
    rows = [["一级模块", "二级模块"], ["我的", "功能"]]
    p = _xlsx(tmp_path, {"功能清单": rows})
    _, d = FeatureChecklistPlugin().evaluate(
        root, config={"feature_checklist": p, "iteration": 0})
    # 有间接线索 → 降级到 indirect，不再出现在 no_entry
    assert "OrphanPage" not in d["routes_no_entry"]
    assert "OrphanPage" in d["routes_indirect_entry"]
    assert any("EntryAbility" in f for f in d["dynamic_push_files"])


def test_route_registration_and_self_reference_are_not_entries(tmp_path: Path):
    """`name === 'X'` 的注册分支与同名文件里的 TAG 常量都不算入口线索。

    不排除的话 GuidePage 会因为自己文件里的 hilog TAG 被误降级成 indirect，
    真阳性就被软化掉了。
    """
    root = _harmony(tmp_path)
    pages = root / "entry" / "src" / "main" / "ets" / "pages"
    (pages / "OrphanPage.ets").write_text(
        "const TAG = 'OrphanPage';\n"
        "@Component\nexport struct OrphanPage {\n  build() { Column() {} }\n}\n",
        encoding="utf-8")
    p = _xlsx(tmp_path, {"功能清单": [["一级模块", "二级模块"], ["我的", "功能"]]})
    _, d = FeatureChecklistPlugin().evaluate(
        root, config={"feature_checklist": p, "iteration": 0})
    assert "OrphanPage" in d["routes_no_entry"]
    assert "OrphanPage" not in d["routes_indirect_entry"]


# ── LLM 兜底解析 ──────────────────────────────────────────────────────────────
#
# 触发口径只有一个：确定性解析出 0 条、且 manifest 里也没有可用清单。
# 正常解析路径必须完全不受影响 —— 这组用例的一半都在守这条。

def _opaque(tmp_path: Path) -> Path:
    """有内容、但三层确定性策略全部落空的表格。

    无「N级模块」表头 → 格式一不认；无「层级路径」列 → 格式二不认；
    非空列数 8 > 6 → 退化路径也拒绝（宁可跳过，也不把「结论」列当成功能名）。
    """
    return _xlsx(tmp_path, {"清单": [
        ["功能模块说明", "备注", "负责人", "状态", "优先级", "版本", "评审", "结论"],
        ["我的", "在线客服", "DetailPage", "导入/导出", "P0", "1.0", "通过", "ok"],
    ]})


def _manifest_rows(*rows: str) -> str:
    return ("| id | status | checklist path | anchor | visibility | note |\n"
            "| --- | --- | --- | --- | --- | --- |\n" + "".join(rows))


def test_opaque_grid_requests_llm_parse_without_writing_anything(tmp_path: Path):
    src, runs = _opaque(tmp_path), tmp_path / "runs"
    plugin = FeatureChecklistPlugin()
    score, d = plugin.evaluate(_harmony(tmp_path), config={
        "feature_checklist": src, "runs_dir": runs, "iteration": 0})

    assert score == 0.0
    assert d["skipped"] == "awaiting_llm_parse"
    # 本轮改派 parser，并带上具体路径与解释器
    assert plugin.judge_subagent_type == "checklist-parser"
    assert str(src) in plugin.judge_prompt
    assert str(Path(d["manifest"])) in plugin.judge_prompt
    assert sys.executable in plugin.judge_prompt
    assert "<SRC>" not in plugin.judge_prompt      # 占位符必须全部替换掉
    # 插件这一轮什么都不写 —— manifest 由 parser 产出，写了就会被空表覆盖
    assert not Path(d["manifest"]).exists()
    assert not runs.exists()


def test_unreadable_file_does_not_reach_the_llm(tmp_path: Path):
    """读不出网格（后缀不认 / 损坏）与读出网格但认不出结构，是两种失败。

    前者连原始数据都没有，交给 LLM 只会让它凭空编造，必须停在 unparsable。
    """
    hm = _harmony(tmp_path)
    bad = tmp_path / "cl.txt"
    bad.write_text("我的 在线客服", encoding="utf-8")
    plugin = FeatureChecklistPlugin()
    _, d = plugin.evaluate(hm, config={"feature_checklist": bad, "iteration": 0})
    assert d["skipped"] == "feature_checklist_unparsable"
    assert plugin.judge_subagent_type == "checklist-judge"

    blank = _xlsx(tmp_path, {"s": [["", ""], ["", ""]]})
    _, d = plugin.evaluate(hm, config={"feature_checklist": blank, "iteration": 0})
    assert d["skipped"] == "feature_checklist_unparsable"


def test_llm_parse_gives_up_after_the_first_few_iterations(tmp_path: Path):
    """尝试次数靠 iteration 记，不靠写进 manifest 的标记 —— 标记会被 parser 覆写掉。"""
    src = _opaque(tmp_path)
    plugin = FeatureChecklistPlugin()
    hm = _harmony(tmp_path)
    for it in (0, 1, 2):
        _, d = plugin.evaluate(hm, config={"feature_checklist": src, "iteration": it})
        assert d["skipped"] == "awaiting_llm_parse", f"iter {it}"
    _, d = plugin.evaluate(hm, config={"feature_checklist": src, "iteration": 3})
    assert d["skipped"] == "llm_parse_failed"
    assert plugin.judge_subagent_type == "checklist-judge"


def test_awaiting_round_never_overwrites_what_the_parser_wrote(tmp_path: Path):
    """evaluate 之后 parser 才写 manifest，而 extract_work_items 在更后面。

    _state 不置空的话，这里会拿上一轮的空 feats 把 parser 刚写好的表整个覆盖掉。
    """
    src, runs = _opaque(tmp_path), tmp_path / "runs"
    runs.mkdir()
    plugin = FeatureChecklistPlugin()
    _, d = plugin.evaluate(_harmony(tmp_path), config={
        "feature_checklist": src, "runs_dir": runs, "iteration": 0})
    mf = Path(d["manifest"])
    written = _manifest_rows("| F001 | TODO | 我的 > 在线客服 |  |  |  |\n")
    mf.write_text(written, encoding="utf-8")          # 模拟 parser 落盘

    assert plugin.extract_work_items(0.0, {"agentic_verdict": {"issues": []}}) == []
    assert mf.read_text(encoding="utf-8") == written


def test_manifest_becomes_the_checklist_source_next_round(tmp_path: Path):
    """parser 写完之后，下一轮从 manifest 读回清单并补上 anchor / visibility。"""
    src, runs = _opaque(tmp_path), tmp_path / "runs"
    runs.mkdir()
    mf = runs / "feature_checklist_manifest.md"
    mf.write_text(_manifest_rows(
        "| F001 | TODO | 我的 > DetailPage |  |  |  |\n",
        "| F002 | TODO | 我的 > 导入/导出 |  |  |  |\n",
    ), encoding="utf-8")

    plugin = FeatureChecklistPlugin()
    score, d = plugin.evaluate(_harmony(tmp_path), config={
        "feature_checklist": src, "runs_dir": runs, "iteration": 1})

    assert score == 1.0          # 2 条都还没取证判 OK
    assert "skipped" not in d
    assert d["checklist_total"] == 2
    assert plugin.judge_subagent_type == "checklist-judge"   # 回到评分 agent
    body = mf.read_text(encoding="utf-8")
    # anchor 列由插件从代码算出 —— parser 填不了，正是每轮重渲染的意义
    assert d["anchored"] == 1
    assert re.search(r"\| hint: \S+\.ets:\d+ \|", body), "anchor 列没被算出来"
    # 「导入/导出」不能被 / 切成两级
    assert "我的 > 导入/导出" in body
    assert d["nav"]["tab_order"] == ["HomePage", "MinePage"]


def test_manifest_readback_is_idempotent(tmp_path: Path):
    """连续两轮的产物必须逐字节相同。

    note 列是 meta 拼出来的，而弱锚点行还会前置一段 "weak anchor: …; "。若把整个
    note 原样塞回 meta，下一轮会再拼一次，逐轮膨胀。所以 meta 只按值的形态
    （_SRC_PATH）从 note 里抠源码路径。
    """
    src, runs = _opaque(tmp_path), tmp_path / "runs"
    runs.mkdir()
    mf = runs / "feature_checklist_manifest.md"
    mf.write_text(_manifest_rows(
        "| F001 | TODO | 我的 > 在线客服 |  |  | app/src/PayActivity.java |\n",
        "| F002 | TODO | 我的 > DetailPage |  |  |  |\n",   # 弱锚点行
        "| F003 | ISSUE | 我的 > 导入/导出 |  |  | F003 客服会话不可达 |\n",
    ), encoding="utf-8")

    plugin = FeatureChecklistPlugin()
    hm = _harmony(tmp_path)
    cfg = {"feature_checklist": src, "runs_dir": runs, "iteration": 1}
    plugin.evaluate(hm, config=cfg)
    first = mf.read_text(encoding="utf-8")
    plugin.evaluate(hm, config=cfg)
    assert mf.read_text(encoding="utf-8") == first, "note 列逐轮膨胀了"
    assert "weak anchor" in first, "用例没覆盖到弱锚点行，幂等性没真正被验证"
    assert first.count("src=app/src/PayActivity.java") == 1
    assert "src=src=" not in first


def test_normal_parsing_never_touches_the_fallback(tmp_path: Path):
    """源文件解析得出来时，manifest 不参与清单来源，parser 也永不派发。"""
    good = _xlsx(tmp_path, {"功能清单": [
        ["一级模块", "二级模块"], ["我的", "退出登录"]]})
    runs = tmp_path / "runs"
    runs.mkdir()
    # 一份内容完全不同的旧 manifest：绝不能被当成清单来源
    (runs / "feature_checklist_manifest.md").write_text(
        _manifest_rows("| F001 | OK | 别的应用 > 别的功能 |  |  |  |\n"),
        encoding="utf-8")

    plugin = FeatureChecklistPlugin()
    _, d = plugin.evaluate(_harmony(tmp_path), config={
        "feature_checklist": good, "runs_dir": runs, "iteration": 0})
    assert d["checklist_total"] == 1
    assert plugin.judge_subagent_type == "checklist-judge"
    body = (runs / "feature_checklist_manifest.md").read_text(encoding="utf-8")
    assert "我的 > 退出登录" in body
    assert "别的应用" not in body


def test_plugin_instance_recovers_after_a_fallback_round(tmp_path: Path):
    """插件在 _PLUGINS 里是单例 —— 换过的 agent 与 prompt 必须每轮复位。

    不复位的话，某一轮解析失效之后，即使后面清单恢复正常也会一直派发 parser。
    """
    plugin = FeatureChecklistPlugin()
    hm = _harmony(tmp_path)
    plugin.evaluate(hm, config={"feature_checklist": _opaque(tmp_path),
                                "iteration": 0})
    assert plugin.judge_subagent_type == "checklist-parser"

    good_dir = tmp_path / "g"
    good_dir.mkdir()
    good = _xlsx(good_dir, {"功能清单": [
        ["一级模块", "二级模块"], ["我的", "退出登录"]]})
    _, d = plugin.evaluate(hm, config={"feature_checklist": good, "iteration": 1})
    assert "skipped" not in d
    assert plugin.judge_subagent_type == "checklist-judge"
    assert plugin.judge_prompt is FeatureChecklistPlugin.judge_prompt
    assert "Verify a human-authored feature checklist" in plugin.judge_prompt


def test_manifest_copied_from_another_checklist_is_rejected(tmp_path: Path):
    """agent 抄隔壁 run 的 manifest 必须被丢弃。

    实测（deepseek-v4-flash）：它验证过 openpyxl 可用，却没读源文件，而是 Glob 到
    别的 runs_* 目录读了六份**别的清单**的 manifest 照抄 141 行，还谎称没有 Bash。
    prompt 拦不住，靠插件手里 agent 伪造不了的事实 —— 源文件的原始单元格文本 —— 校验。
    """
    src, runs = _opaque(tmp_path), tmp_path / "runs"
    runs.mkdir()
    mf = runs / "feature_checklist_manifest.md"
    mf.write_text(_manifest_rows(*[
        f"| F{i:03d} | TODO | PPT创作 > 生成大纲 > 功能{i} |  |  |  |\n"
        for i in range(1, 12)]), encoding="utf-8")

    hm = _harmony(tmp_path)
    plugin = FeatureChecklistPlugin()
    _, d = plugin.evaluate(hm, config={
        "feature_checklist": src, "runs_dir": runs, "iteration": 1})
    assert d["skipped"] == "awaiting_llm_parse", "抄来的清单被当成了本清单"
    assert plugin.judge_subagent_type == "checklist-parser"   # 重新派发

    # 如实提取的（功能名逐字出自源文件）照常放行
    mf.write_text(_manifest_rows(
        "| F001 | TODO | 我的 > 在线客服 |  |  |  |\n",
        "| F002 | TODO | 我的 > DetailPage |  |  |  |\n",
    ), encoding="utf-8")
    plugin2 = FeatureChecklistPlugin()
    _, d2 = plugin2.evaluate(hm, config={
        "feature_checklist": src, "runs_dir": runs, "iteration": 1})
    assert "skipped" not in d2
    assert d2["checklist_total"] == 2


def test_issue_gets_full_checklist_path_for_the_generator(tmp_path: Path):
    """judge 常只写叶子名，而生成器拿不到 manifest —— 路径由插件机械补上。"""
    p = _xlsx(tmp_path, {"功能清单": [
        ["一级模块", "二级模块", "三级模块"],
        ["我的", "联系我们", "客服上传文件"],
        ["作品", "我的作品", "返回"],
    ]})
    plugin = FeatureChecklistPlugin()
    plugin.evaluate(_harmony(tmp_path),
                    config={"feature_checklist": p, "iteration": 0})
    verdict = {"issues": [
        "CustomerServicePage.ets:166 picker has no UI context (checklist F001 客服上传文件)",
        "WorksPage.ets:815 dead-end handler (checklist F002 作品>我的作品>返回)",
    ]}
    plugin.extract_work_items(0.0, {"agentic_verdict": verdict})

    # 只有叶子名的那条被补全
    assert "[checklist path: F001 我的 > 联系我们 > 客服上传文件]" in verdict["issues"][0]
    # judge 已写全的不重复追加（路径文本已在 issue 里）
    assert "checklist path:" not in verdict["issues"][1]


def test_out_of_scope_issues_still_dropped_after_path_enrichment(tmp_path: Path):
    p = _xlsx(tmp_path, {"功能清单": [
        ["一级模块", "二级模块"], ["我的", "退出登录"]]})
    plugin = FeatureChecklistPlugin()
    plugin.evaluate(_harmony(tmp_path),
                    config={"feature_checklist": p, "iteration": 0})
    verdict = {"issues": ["荣耀登录永久隐藏（清单里没有这条）",
                          "LoginPage.ets:1 broken (checklist F001)"]}
    plugin.extract_work_items(0.0, {"agentic_verdict": verdict})
    assert len(verdict["issues"]) == 1
    assert "[checklist path: F001 我的 > 退出登录]" in verdict["issues"][0]


def test_score_is_fraction_not_yet_proven_ok(tmp_path: Path):
    """(total-OK)/total：TODO 与 ISSUE 同权，只有取证判过的 OK 才降分。

    换掉的旧口径是"未修 ISSUE 条数×0.02"，它只数已知缺陷 —— 没检查过的行贡献 0，
    于是已知的几条一修完这一维就读作干净。实测 runs_20260811_1933 就这样在 926 条
    里只判定 162 条（17.5%）时 H 归零早停，764 条从没被看过。
    """
    rows = [["一级模块", "二级模块"]] + [["我的", f"功能{i}"] for i in range(10)]
    p = _xlsx(tmp_path, {"功能清单": rows})
    runs = tmp_path / "runs"
    runs.mkdir()
    hm = _harmony(tmp_path)
    cfg = {"feature_checklist": p, "runs_dir": runs, "iteration": 1}
    mf = runs / "feature_checklist_manifest.md"

    plugin = FeatureChecklistPlugin()
    score, d = plugin.evaluate(hm, config=cfg)
    assert (score, d["ok_count"]) == (1.0, 0)        # 全 TODO

    # 标 ISSUE 不降分 —— "发现了问题"不等于"这一条核完了"
    body = mf.read_text(encoding="utf-8")
    body = body.replace("| F001 | TODO |", "| F001 | ISSUE |", 1)
    mf.write_text(body, encoding="utf-8")
    score, d = plugin.evaluate(hm, config=cfg)
    assert (score, d["ok_count"], d["open_issue_count"]) == (1.0, 0, 1)

    # 带证据的 OK 才降分：2/10 → 0.8
    body = mf.read_text(encoding="utf-8")
    for fid, ln in (("F002", 3), ("F003", 4)):     # 各自的行 —— 共用会被整组退回
        body = body.replace(f"| {fid} | TODO |",
                            f"| {fid} | OK | 我的 > 功能x | pages/MinePage.ets:{ln} |", 1)
    mf.write_text(body, encoding="utf-8")
    score, d = plugin.evaluate(hm, config=cfg)
    assert d["ok_count"] == 2
    assert score == pytest.approx(0.8)

    # 证据不成立的 OK 退回 TODO，分数不降 —— 橡皮图章拿不到分
    body = mf.read_text(encoding="utf-8").replace(
        "| F004 | TODO |", "| F004 | OK | 我的 > 功能y | | | 看着没问题 |", 1)
    mf.write_text(body, encoding="utf-8")
    score, d = plugin.evaluate(hm, config=cfg)
    assert d["unproven_ok_reset"] == 1
    assert score == pytest.approx(0.8)


def test_ok_evidence_text_survives_rerender(tmp_path: Path):
    """judge 抄的引用行原文必须留在 manifest 里。

    早先重渲染用 marks.proof（只有归一化后的 file:line），judge 每轮辛苦抄的行文本
    与解释被抹掉，下一轮它看不见自己上一轮的取证，只能重查。实测行号常不准（7%）
    而抄的原文可靠（84%），丢掉的恰是更有用的那半。
    """
    p = _xlsx(tmp_path, {"功能清单": [
        ["一级模块", "二级模块"], ["我的", "退出登录"], ["我的", "清除缓存"]]})
    runs = tmp_path / "runs"
    runs.mkdir()
    hm = _harmony(tmp_path)
    cfg = {"feature_checklist": p, "runs_dir": runs, "iteration": 1}
    mf = runs / "feature_checklist_manifest.md"

    plugin = FeatureChecklistPlugin()
    plugin.evaluate(hm, config=cfg)
    evidence = "pages/MinePage.ets:3 `.onClick(() => this.go())` — 退出按钮已接线"
    body = re.sub(r"^\| F001 \|.*$",
                  f"| F001 | OK | 我的 > 退出登录 |  |  | {evidence} |",
                  mf.read_text(encoding="utf-8"), count=1, flags=re.M)
    mf.write_text(body, encoding="utf-8")

    plugin.evaluate(hm, config=cfg)
    out = mf.read_text(encoding="utf-8")
    assert evidence in out, "重渲染把 judge 的取证原文丢了"
    first = out

    # 幂等：再跑一轮逐字节相同，note 不增长也不被逐步截短
    plugin.evaluate(hm, config=cfg)
    assert mf.read_text(encoding="utf-8") == first

    # 证据仍照常参与判定：这一条算 OK，分数相应下降
    _, d = plugin.evaluate(hm, config=cfg)
    assert d["ok_count"] == 1
    assert d["unproven_ok_reset"] == 0


def test_shared_evidence_rule_converges_on_duplicate_checklists(tmp_path: Path):
    """重复/同控件多路径的清单不能被卡死 —— 组不超上限就稳定。

    早先"只要共用就退"不收敛：用户清单里两行完全相同时，judge 无论怎么取证都会被退，
    每轮重做每轮被退。上限由清单自身推出（同名同上级组的最大规模，下限 2）。
    """
    p = _xlsx(tmp_path, {"功能清单": [
        ["一级模块", "二级模块", "三级模块"],
        ["我的", "设置", "清除缓存"],      # F001 ┐ 完全重复的两行
        ["我的", "设置", "清除缓存"],      # F002 ┘ → cap = 2
        ["我的", "关于", "清除缓存"],      # F003   同名不同上级
    ]})
    runs = tmp_path / "runs"
    runs.mkdir()
    hm = _harmony(tmp_path)
    cfg = {"feature_checklist": p, "runs_dir": runs, "iteration": 1}
    mf = runs / "feature_checklist_manifest.md"

    plugin = FeatureChecklistPlugin()
    plugin.evaluate(hm, config=cfg)

    def mark(fids, line=3):
        b = mf.read_text(encoding="utf-8")
        for fid in fids:
            b = re.sub(rf"^\| {fid} \|.*$",
                       f"| {fid} | OK | a > b > c |  |  | pages/MinePage.ets:{line} `x` |",
                       b, count=1, flags=re.M)
        mf.write_text(b, encoding="utf-8")

    # 两行重复 + 一行同名不同上级，共 3 行压在一行证据上 → 3 > cap(2) → 退回
    mark(["F001", "F002", "F003"])
    _, d = plugin.evaluate(hm, config=cfg)
    assert d["shared_proof_reset"] == 3

    # judge 重做，只给重复的那两行 → 2 <= cap → 稳定，不再被退（收敛）
    mark(["F001", "F002"])
    _, d = plugin.evaluate(hm, config=cfg)
    assert d["shared_proof_reset"] == 0
    assert d["ok_count"] == 2
    _, d = plugin.evaluate(hm, config=cfg)          # 再跑一轮仍稳定
    assert (d["shared_proof_reset"], d["ok_count"]) == (0, 2)


def test_same_control_multiple_paths_is_exempt(tmp_path: Path):
    """同名同上级 = 同一控件多路径进入，如实取证本就共用一行，任何规模都豁免。"""
    rows = [["一级模块", "二级模块", "三级模块"]]
    rows += [[f"入口{i}", "分享弹窗", "包括播放位置"] for i in range(6)]
    p = _xlsx(tmp_path, {"功能清单": rows})
    runs = tmp_path / "runs"
    runs.mkdir()
    hm = _harmony(tmp_path)
    cfg = {"feature_checklist": p, "runs_dir": runs, "iteration": 1}
    mf = runs / "feature_checklist_manifest.md"

    plugin = FeatureChecklistPlugin()
    plugin.evaluate(hm, config=cfg)
    b = mf.read_text(encoding="utf-8")
    for i in range(1, 7):
        b = re.sub(rf"^\| F{i:03d} \|.*$",
                   f"| F{i:03d} | OK | a > 分享弹窗 > 包括播放位置 |  |  | pages/MinePage.ets:3 `x` |",
                   b, count=1, flags=re.M)
    mf.write_text(b, encoding="utf-8")

    _, d = plugin.evaluate(hm, config=cfg)
    assert d["shared_proof_reset"] == 0
    assert d["ok_count"] == 6


def test_batching_rules_are_in_both_prompts():
    """判据同时写在插件 rubric 与 agent 定义里 —— 两处都改，避免只改一处失效。"""
    p = FeatureChecklistPlugin()
    assert "in one\nresponse" in p.judge_prompt or "in one response" in p.judge_prompt
    assert "may not cite the same line" in p.judge_prompt


@pytest.mark.parametrize("total,expect", [
    (141, 141),    # 141 行：小于上限 → 一轮做完（≈15 分钟）
    (926, 266),    # 926 行：按判别器预算切（2400s×预算/行 ≈ 266），走完约 4 轮
    (1102, 266),   # Meshtastic
    (5000, 266),   # 超长清单同样吃上限
    (3, 3),        # 剩余极少 → 就是剩余量
])
def test_round_target_is_bounded_by_the_round_budget(tmp_path: Path, total: int, expect: int):
    """一轮的行目标 = min(上限, 剩余)。上限由判别器 timeout_s 推出，不是比例。

    早先按"剩余的 1/20"给目标，926 行只要求 47 行 —— 与 judge_max_issues=50
    差 14 倍量级（按实测 7.6% 缺陷密度，47 行只对应 3.6 条 issue），AND 语义下 issue
    那一条永远达不成。
    """
    rows = [["一级模块", "二级模块"]] + [["我的", f"功能{i}"] for i in range(total)]
    d = tmp_path / str(total)
    d.mkdir()
    p = _xlsx(d, {"功能清单": rows})
    _, det = FeatureChecklistPlugin().evaluate(
        _harmony(d), config={"feature_checklist": p, "iteration": 0})
    assert det["checklist_total"] == total
    assert det["round_target_rows"] == expect


def test_stop_rule_overrides_engine_early_stop():
    """引擎注入的 Early-stop rule 是 issue 上限；本维度需要的是工作量下限，必须写明谁压过谁。"""
    jp = FeatureChecklistPlugin().judge_prompt
    assert "It is overridden here" in jp
    assert "details.round_target_rows" in jp
    # 实测退出理由（1340 轮 "verified a cross-section"）必须被点名否掉
    assert "do not qualify" in jp
    # 两个目标必须同时满足，否则永远是便宜的那半先达成
    assert "only when **both** are done" in jp
    assert "Never invent an issue to reach the cap" in jp
    # 下限不能变成刷量许可
    assert "floor on work, not on marks" in jp
    # 停止条件只讲一处，避免两节互相矛盾
    assert jp.count("when to stop") == 1
    # prompt 全英文、不含任何 app 特有的数字
    assert not re.search(r"[一-鿿]", jp.split("## Order of work")[1])
    assert "926" not in jp


def test_last_round_advance_is_fed_back(tmp_path: Path):
    """judge 每轮从零起意，看不到自己上轮交付了多少 —— 回灌进 details 才能自省。

    实测 runs_20260814_1340：926 行里只判了 9 行就以"verified a cross-section"收工，
    而引擎注入的 Early-stop rule 只封顶不设底，字面上它没违规。
    """
    rows = [["一级模块", "二级模块"]] + [["我的", f"功能{i}"] for i in range(40)]
    p = _xlsx(tmp_path, {"功能清单": rows})
    runs = tmp_path / "runs"
    runs.mkdir()
    hm = _harmony(tmp_path)
    cfg = {"feature_checklist": p, "runs_dir": runs, "iteration": 1}
    mf = runs / "feature_checklist_manifest.md"
    hist = runs / "entropy_history.jsonl"

    plugin = FeatureChecklistPlugin()
    _, d = plugin.evaluate(hm, config=cfg)
    assert d["last_round_advanced"] is None          # 没有历史 → 不编造
    assert d["round_target_rows"] == 40              # 小清单 = 剩余量本身

    # 模拟上一轮落了 2 行判定 + 历史记录
    hist.write_text(json.dumps({"details": {"feature_checklist": {"checked_count": 2}}}) + "\n",
                    encoding="utf-8")
    body = mf.read_text(encoding="utf-8")
    for fid, ln in (("F001", 3), ("F002", 4)):
        body = re.sub(rf"^\| {fid} \|.*$",
                      f"| {fid} | OK | 我的 > 功能x |  |  | pages/MinePage.ets:{ln} `x` |",
                      body, count=1, flags=re.M)
    body = re.sub(r"^\| F003 \|.*$", "| F003 | ISSUE | 我的 > 功能y |  |  | 坏了 |",
                  body, count=1, flags=re.M)
    mf.write_text(body, encoding="utf-8")

    _, d = plugin.evaluate(hm, config=cfg)
    assert d["checked_count"] == 3                   # 2 OK + 1 ISSUE
    assert d["last_round_advanced"] == 1             # 3 - 上一轮的 2


def test_shared_evidence_cap_generalises_across_checklists(tmp_path: Path):
    """上限不能取"最大组"——会被单个极端值支配，整份清单的门槛跟着失守。

    实测十份清单最大组从 2 到 24（meshtatic 1102 行有个控件被记 24 次）。取 max 时
    judge 在任意一行上摊派 24 行都不拦，而 2152 那次作弊正好是一行撑 22 条。
    极端组本身走同名同上级豁免，不需要 cap 放行，所以 cap 取 P95 夹在 [2,4]。
    """
    from collections import Counter

    from ecat.entropy.plugins.a2h_verify.check_feature_checklist import _drop_shared_proof

    def cap_for(feats):
        where = {f.fid: (f.name, f.path[-2] if len(f.path) > 1 else "") for f in feats}
        sizes = sorted(Counter(where.values()).values()) or [1]
        return max(2, min(4, sizes[max(0, min(len(sizes) - 1,
                                              round(0.95 * (len(sizes) - 1))))]))

    # 一个控件被记 24 次 + 大量单行控件 —— 极端值不该抬高门槛
    rows = [["一级模块", "二级模块", "三级模块"]]
    rows += [[f"入口{i}", "分享弹窗", "包括播放位置"] for i in range(24)]
    rows += [["我的", "设置", f"项{i}"] for i in range(200)]
    p = _xlsx(tmp_path, {"功能清单": rows})
    feats = _load_checklist(p)
    assert cap_for(feats) <= 4, "极端组把门槛抬上去了"

    # 极端组自身仍然豁免：24 行同名同上级共用一行证据，一条都不该退
    from ecat.entropy.plugins.a2h_verify.check_feature_checklist import _Marks
    m = _Marks()
    for f in feats[:24]:
        m.checked.add(f.fid)
        m.proof[f.fid] = "pages/X.ets:3"
    _drop_shared_proof(m, feats)
    assert m.shared_proof == set()
    assert len(m.proof) == 24


# ── iter 0 基线快照 ───────────────────────────────────────────────────────────
# 关注点只有一个：ECAT 跑起来之后这份文件不能再变。它是"动工前 OK 数"的唯一凭据，
# 被后续轮次覆盖就等于把 ECAT 自己修出来的功能算进了基线。

def _checklist_xlsx(tmp_path: Path) -> Path:
    return _xlsx(tmp_path, {"s": [
        ["一级模块", "二级模块", "三级模块"],
        ["我的", "联系我们", "客服会话"],
        ["首页", "创作", "立即开通"],
    ]})


def _run_iter(plugin, tmp_path, harmony, runs, iteration, verdict=None):
    """跑一轮：evaluate + （可选）带 verdict 的 extract_work_items。"""
    score, details = plugin.evaluate(
        harmony,
        config={"feature_checklist": _checklist_xlsx(tmp_path),
                "runs_dir": runs, "iteration": iteration})
    if verdict is not None:
        details["agentic_verdict"] = verdict
        plugin.extract_work_items(score, details)
    return score, details


def test_baseline_snapshot_written_at_iter0(tmp_path: Path):
    harmony, runs = _harmony(tmp_path), tmp_path / "runs"
    plugin = FeatureChecklistPlugin()
    _run_iter(plugin, tmp_path, harmony, runs, 0, verdict={"score": 0.5, "issues": []})
    snap = runs / "initial_checklist_manifest.md"
    assert snap.is_file(), "iter 0 必须留下基线快照"
    assert "ECAT baseline" in snap.read_text(encoding="utf-8")


def test_baseline_not_touched_after_iter0(tmp_path: Path):
    """核心约束：iter>0 的 evaluate 与 extract 都不得改动基线。"""
    harmony, runs = _harmony(tmp_path), tmp_path / "runs"
    plugin = FeatureChecklistPlugin()
    _run_iter(plugin, tmp_path, harmony, runs, 0, verdict={"score": 0.5, "issues": []})
    snap = runs / "initial_checklist_manifest.md"
    before = snap.read_text(encoding="utf-8")

    # 后续轮次：判定状态发生变化（issue 落到 F001），基线仍须原样
    for it in (1, 2, 7):
        _run_iter(plugin, tmp_path, harmony, runs, it,
                  verdict={"score": 0.3, "issues": ["F001 客服会话入口缺失"]})
    assert snap.read_text(encoding="utf-8") == before, "基线在 ECAT 过程中被改写了"

    # 而工作 manifest 确实在动 —— 否则上面那条断言是空的
    live = (runs / "feature_checklist_manifest.md").read_text(encoding="utf-8")
    assert "ISSUE" in live


def test_baseline_ok_exposed_in_details(tmp_path: Path):
    """基线 OK 数要能读回 details，否则算不出增量。"""
    harmony, runs = _harmony(tmp_path), tmp_path / "runs"
    plugin = FeatureChecklistPlugin()
    _run_iter(plugin, tmp_path, harmony, runs, 0, verdict={"score": 1.0, "issues": []})
    _, details = _run_iter(plugin, tmp_path, harmony, runs, 1)
    assert details["baseline_ok"] == 0          # iter 0 无人取证 → 基线 OK=0
