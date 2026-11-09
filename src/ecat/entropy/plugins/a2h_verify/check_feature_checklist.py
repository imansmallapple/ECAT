"""feature_checklist —— 人工功能清单逐条核对（agentic judge 主导，默认关闭）。

现有功能维度的判据只有"存在性"三类：文件在不在（source_parity）、页面文件有没有
内容（page_audit / feature_check 的行数比）、路由名注册了没有（entry_nav）。而人工
功能清单是**交互点粒度**（「勾选支付协议」「取消全选」「客服会话」），差异恰好发生在
两个没人检查的层面：

  * 可达性 —— 功能有实现、路由也注册了，但没有任何入口 push 它
  * 路径一致性 —— Android 侧的条件分支在鸿蒙侧被塌缩成单一路径

实测例（aippt 迁移）：`CustomerServicePage.ets` 455 行真实实现、路由已注册，但只能从
退款进度页进入；「我的→联系我们」走的是电话弹窗，Android 的
`showService()` 里那个 `contactUsUrl` 非空判断整个丢了。清单里「客服会话」「客服上传
文件」两条因此不可达，而所有现有插件全部判通过。

## 为什么是 agentic 而不是 hard

清单标签多是分析师写的**行为描述**（「切换输入主题页签」「确认以该文件生成」），不是 UI
文案。实测在 141 条功能清单上做确定性映射：资源精确匹配只能覆盖 70 条，放宽到子串/
模糊后「勾选支付协议」会退化匹配到一级模块「PPT创作」并锚到无关文案上——错锚比不锚更坏。
所以本维度归 agentic，硬检查**只取证不打分**（`hard_weight = 0.0`），与 ui_align
的「硬检查只截图不打分」同形。

## 硬检查产出什么

details 体积由两层机制约束：deferred 判别路径 `builder._compact_dispatch_details`
（list_cap=40 / max_chars=6000，且把含 missing/stub 字样的项排到最前），engine 直连
路径 `_truncate_details`（3000 字符盲截）。所以：

  * 全量遍历清单（不分批），只把**可疑项**发给 judge：锚点落在无入口路由上的、
    只能间接进入的、弱锚点的。分批会让相邻两轮量的不是同一批功能，early-stop
    因此失去意义——实测出现过"最后两批恰好没问题→H=0 提前收敛"，而前面报过
    0.4 的问题一条没修。
  * 定位不到的功能点单列并封顶。**不是**缺失证据——清单标签多是行为描述而非 UI
    文案，锚不上很常见，judge 需自行 Grep 确认。
  * 导航索引：注册路由、push 边、tab 根组件顺序——这是路径一致性的判据来源。
  * 可达性三态：`routes_no_entry`（无任何入口线索）/ `routes_indirect_entry`
    （疑似动态 push）/ 有字面量入口。动态 push 的文件单列，供 judge 复核。

清单路径来自 `config["feature_checklist"]`（yaml 的 `paths.feature_checklist`，与
`paths.spec → spec_dir` 同一条链）。未配置 / 文件不存在 / 解析不出内容 → 跳过，
贡献 0，绝不误抬分。
"""

from __future__ import annotations

import contextlib
import csv
import json
import logging
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from ecat.entropy.base import EntropyPlugin, should_skip_path

# 复用 crash_risk 的注释剥离（保留偏移与行号）。不剥注释时，文档注释里引用的
# `pushPathByName('X')` 会被当成真实入口 —— 实测 ShortCutPage.ets:12 的注释就让
# 「快捷退订/快捷退款」6 条功能看起来可达，掩盖了它其实无人 push。
from ecat.entropy.plugins.a2h_verify.check_crash_risk import _blank_comments
from ecat.gradient.schema import WorkItem

log = logging.getLogger(__name__)

# 全量清单落盘到 runs_dir，details 只放路径与导航索引。
_MANIFEST_NAME = "feature_checklist_manifest.md"
# iter 0 判别完成后的 manifest 快照 —— ECAT 动工前的基线，此后只读不写。
# 与 manifest 同目录（runs_dir），所以每次 run 各有一份自己的基线。
_INITIAL_MANIFEST_NAME = "initial_checklist_manifest.md"
# 导航索引的列表上限：details 仍受 builder._compact_dispatch_details
# （list_cap=40 / max_chars=6000）与 engine._truncate_details（3000）约束。
_MAX_ROUTES = 24
_MAX_PUSH_ENTRIES = 12
# 单个路由最多列这么多入口位置（多入口路由要能看出"缺哪个入口"）
_MAX_LOCS_PER_ROUTE = 4


# ══════════════════════════════════════════════════════════════════════════════
# 清单解析（xlsx / csv / md 层级表格）
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class _Feature:
    fid: str                 # 行序生成的稳定 ID，保证跨迭代批次可复现
    module: str              # 一级模块，用于定位 tab / 顶层入口
    path: list[str]          # 层级（只含层级列，已去空）
    name: str                # 最深一级 = 功能点名
    meta: dict[str, str]     # 非层级列（是否已实现 / GAP / 说明 …），清单自带的信息

    @property
    def path_text(self) -> str:
        return " > ".join(self.path)


# 层级列表头：一级模块 / 二级模块 … 也认 L1 / Level 2 / 模块3 等写法。
# 实测 6 份不同应用的清单
# 全部用「N级模块」，但列数从 3 到 6 不等，且后面还挂着
# 来源|说明|类型|GAP|验收|缺失|BUG 或 是否已实现 这类非层级列 —— 必须靠表头定位，
# 不能把所有非空单元格当层级，否则功能名会取成 GAP 值。
_LEVEL_HDR = re.compile(
    r"^(?:[一二三四五六七八九十]+级模块|模块\s*[1-9]|L(?:evel)?[\s_]*[1-9])$", re.I)


def _sheets_xlsx(p: Path) -> list[list[list[str]]]:
    """返回每个 sheet 的行矩阵。首个 sheet 不一定是数据表 —— 实测
    `<app> feature list -L3.xlsx` 第一个 sheet 是封面测试报告，真正的清单在
    第二个 sheet（功能完备度评估清单），所以全部 sheet 都取回来交给表头探测。"""
    try:
        import openpyxl  # noqa: PLC0415 — 可选依赖，缺失时本维度跳过而非崩掉
    except ImportError:
        log.warning("feature_checklist: openpyxl 未安装，xlsx 清单无法解析")
        return []
    try:
        # read_only=True 让 openpyxl 流式读、但 zip handle 直到 wb.close() 才释放。
        # evaluate 每次会开 2-3 次工作簿(_has_grid_content / _load_checklist /
        # _grounded_in_source),不关的话长期 refine 每轮都泄漏句柄;在 Windows 上
        # 打开的句柄还会锁住 xlsx 让用户没法改。用 contextlib.closing 兜底关闭。
        with contextlib.closing(
            openpyxl.load_workbook(p, data_only=True, read_only=True)
        ) as wb:
            return [[[("" if c is None else str(c).strip()) for c in row]
                     for row in ws.iter_rows(values_only=True)] for ws in wb.worksheets]
    except Exception as exc:  # noqa: BLE001 — 损坏/加密表格一律当不可用
        log.warning("feature_checklist: 无法打开 %s: %s", p.name, exc)
        return []


def _rows_csv(p: Path) -> list[list[str]]:
    delim = "\t" if p.suffix.lower() == ".tsv" else ","
    try:
        with p.open(encoding="utf-8-sig", newline="") as fh:
            return [[c.strip() for c in row] for row in csv.reader(fh, delimiter=delim)]
    except (OSError, csv.Error):
        return []


def _rows_md(p: Path) -> list[list[str]]:
    try:
        text = p.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    rows: list[list[str]] = []
    for line in text.splitlines():
        s = line.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if all(set(c) <= set("-: ") for c in cells):   # markdown 表格分隔行
            continue
        rows.append(cells)
    return rows


def _find_header(rows: list[list[str]]) -> tuple[int, list[int], dict[int, str]] | None:
    """在前若干行里找「N级模块」宽表头，返回 (表头行号, 层级列下标, 非层级列->列名)。"""
    for ri, row in enumerate(rows[:8]):
        levels = [ci for ci, c in enumerate(row) if c and _LEVEL_HDR.match(c)]
        if len(levels) >= 2:
            metas = {ci: c for ci, c in enumerate(row) if c and ci not in levels}
            return ri, levels, metas
    return None


# 第二种清单格式（实测 antennapod / meshtatic feature_checklist.xlsx）：
#   ID | 功能名 | 层级路径 | 源码位置 | 控件标识符 | 交互类型 | 优先级 | 深度
# 层级已经是单列 " > " 串，且自带安卓源码位置与控件标识符 —— 比资源匹配的锚点强得多。
_PATH_HDR = ("层级路径", "路径", "path", "hierarchy")
_NAME_HDR = ("功能名", "功能", "name", "feature")


def _find_flat_header(rows: list[list[str]]) -> tuple[int, int, int, dict[int, str]] | None:
    """找「层级路径」单列表头，返回 (表头行号, 路径列, 功能名列, 其余列->列名)。"""
    for ri, row in enumerate(rows[:8]):
        pi = ni = -1
        for ci, c in enumerate(row):
            lc = c.lower()
            if pi < 0 and any(h in c or h in lc for h in _PATH_HDR):
                pi = ci
            elif ni < 0 and any(h in c or h in lc for h in _NAME_HDR):
                ni = ci
        if pi >= 0:
            metas = {ci: c for ci, c in enumerate(row) if c and ci not in (pi, ni)}
            return ri, pi, ni, metas
    return None


# 只认 > 系分隔符：功能名里合法含「/」（如「导入/导出」），把它当分隔符会把层级
# 切碎（实测 antennapod 清单深度被切到 13 级）。
_PATH_SEP = re.compile(r"\s*[>›»→]\s*")


def _flat_to_features(
    rows: list[list[str]], pi: int, ni: int, metas: dict[int, str], start: int
) -> list[_Feature]:
    feats: list[_Feature] = []
    for raw in rows[start:]:
        if pi >= len(raw) or not raw[pi]:
            continue
        path = [seg for seg in _PATH_SEP.split(raw[pi]) if seg]
        if not path:
            continue
        name = raw[ni] if 0 <= ni < len(raw) and raw[ni] else path[-1]
        meta = {metas[ci]: raw[ci] for ci in metas if ci < len(raw) and raw[ci]}
        feats.append(_Feature(fid=f"F{len(feats) + 1:03d}", module=path[0],
                              path=path, name=name, meta=meta))
    return feats


def _rows_to_features(
    rows: list[list[str]], levels: list[int], metas: dict[int, str], start: int
) -> list[_Feature]:
    feats: list[_Feature] = []
    for raw in rows[start:]:
        # 按列下标取，不压缩 —— 中间列为空是合法的稀疏层级
        # （实测「首页 | 切换文件夹视图 | 长按-文件夹 | 返回」中间有空列）
        path = [raw[ci] for ci in levels if ci < len(raw) and raw[ci]]
        if not path:
            continue
        meta = {metas[ci]: raw[ci] for ci in metas
                if ci < len(raw) and raw[ci]}
        feats.append(_Feature(fid=f"F{len(feats) + 1:03d}", module=path[0],
                              path=path, name=path[-1], meta=meta))
    return feats


def _read_sheets(path: Path) -> list[list[list[str]]]:
    """按后缀取行矩阵；后缀不认、依赖缺失或文件损坏一律返回 []。"""
    suf = path.suffix.lower()
    if suf in (".xlsx", ".xlsm"):
        return _sheets_xlsx(path)
    if suf in (".csv", ".tsv"):
        return [_rows_csv(path)]
    if suf in (".md", ".markdown"):
        return [_rows_md(path)]
    return []


def _has_grid_content(path: Path) -> bool:
    """文件里到底有没有可供 LLM 解析的内容。

    区分两种解析失败：**读不出网格**（后缀不认 / openpyxl 缺失 / 文件损坏）与
    **读出网格但认不出层级结构**。只有后者值得回退给 LLM —— 前者连原始数据都没有，
    交给 LLM 只会让它凭空编造。
    """
    return any(c for rows in _read_sheets(path) for row in rows for c in row)


# 本轮至少要判定多少行 = min(上限, 剩余 TODO)。上限由**判别器的超时预算**推出，不是
# 拍脑袋。上一版按 runner 默认 timeout_s=3600 推 (3600×0.7/6.3≈400), 但判别器
# 默认实际是 2400s (config.AgentConfig discriminator timeout_s=2400), 在 926 行大清单
# 上 400 行会跑 ~2520s > 2400s, 被看门狗杀进程树、verdict 整个丢失——比少判几行严重。
# 留三成余量、其余同前: 2400×0.7/6.3 ≈ 267 行。若把判据器 timeout 传进
# config["judge_timeout_s"], 则按实际值推导。
# 取 min(上限, 剩余) 而不是比例：清单小于上限时就该一轮做完（141 行的清单 ≈ 15 分钟），
# 大清单则按上限切，走完 926 行约 3 轮。
_SEC_PER_ROW = 6.3
_ROUND_TARGET_MAX = 267


def _judge_timeout_s(cfg: dict[str, object]) -> float | None:
    """Judge timeout from the evaluate config, if the caller supplies it."""
    v = (cfg.get("judge_timeout_s") or 0)
    try:
        return float(v) if float(v) > 0 else None
    except (TypeError, ValueError):
        return None


def _round_target_rows(timeout_s: float | None) -> int:
    """Rows one judge round should cover, from the judge's timeout budget.

    Leaves 30% headroom for turnarounds and the verdict write. Falls back to the
    discriminator default (2400s) when the caller doesn't pass its timeout.
    """
    budget = timeout_s if timeout_s and timeout_s > 0 else 2400.0
    return max(100, min(int(budget * 0.7 / _SEC_PER_ROW), _ROUND_TARGET_MAX))

_MIN_GROUNDED = 0.6      # 低于这个命中率就不是这份清单（抄来的接近 0，如实提取接近 1）


def _grounded_in_source(feats: list[_Feature], path: Path) -> bool:
    """manifest 里的功能名是否真的出自这份源文件。

    实测 agent 会 Glob 到隔壁 runs_* 读别的清单照抄交差，还谎称读不了源文件；prompt
    拦不住。插件认不出层级但读得出网格，如实提取的功能名必然逐字出现在原始单元格里。
    """
    blob = "\n".join(c for rows in _read_sheets(path) for row in rows for c in row if c)
    hit = sum(1 for f in feats if f.name in blob)
    if blob and hit < len(feats) * _MIN_GROUNDED:
        log.warning("feature_checklist: %d/%d 个功能名不在 %s 里，丢弃该 manifest",
                    len(feats) - hit, len(feats), path.name)
        return False
    return True


def _load_checklist(path: Path) -> list[_Feature]:
    """层级表格 → 功能点列表。

    优先靠表头定位层级列；多 sheet 时选第一个能识别出表头的 sheet。完全找不到表头
    时退化为"所有非空单元格都是层级"，但仅当列数 <= 6（否则极可能把 GAP/说明 这类
    尾列当成功能名，宁可跳过）。
    """
    sheets = _read_sheets(path)

    # 格式一：N级模块 宽表头
    for rows in sheets:
        found = _find_header(rows) if rows else None
        if found is None:
            continue
        ri, levels, metas = found
        feats = _rows_to_features(rows, levels, metas, ri + 1)
        if feats:
            return feats

    # 格式二：层级路径 单列
    for rows in sheets:
        flat = _find_flat_header(rows) if rows else None
        if flat is None:
            continue
        ri, pi, ni, metas = flat
        feats = _flat_to_features(rows, pi, ni, metas, ri + 1)
        if feats:
            return feats

    # 无表头退化路径
    for rows in sorted(sheets, key=len, reverse=True):
        width = max((len([c for c in r if c]) for r in rows), default=0)
        if not rows or width > 6:
            continue
        feats = [
            _Feature(fid=f"F{i + 1:03d}", module=cells[0], path=cells,
                     name=cells[-1], meta={})
            for i, cells in enumerate(
                [c for c in ([x for x in r if x] for r in rows) if c])
        ]
        if feats:
            return feats
    return []


# ══════════════════════════════════════════════════════════════════════════════
# 代码侧取证
# ══════════════════════════════════════════════════════════════════════════════

_RES_REF = re.compile(r"\$r\(\s*['\"]app\.string\.([\w.]+)['\"]")
_PUSH = re.compile(
    r"(?:push|replace)(?:Path|PathByName|Destination|DestinationByName)\("
    r"\s*\{?\s*(?:name:\s*)?['\"](\w+)['\"]")
# 间接入口：路由名先写进对象字段（pageName / routeName / target…），再由某处统一
# push。实测 EntryAbility.ets 用 `{pageName:'ShortCutPage', params:{...}}` 描述推送
# 目标，最后 `activeStack.pushPathByName(pushRoute.pageName, ...)` 动态 push ——
# 只认字面量会把这类路由误判为"无人 push"。
# 不猜字段名（pageName / route / dest / to … 各 app 不同），而是拿 app 自己注册的
# 路由名去反查任意位置的字符串字面量：只要某路由名在别处以字面量出现过，就说明存在
# 间接入口的可能，据此把"不可达"降级为"疑似间接入口"。过度匹配（如日志字符串里
# 提到路由名）只会让结论更保守，方向是安全的。
# 动态 push：push 的第一个实参是标识符而非字面量。出现即说明 unreachable 列表
# 不再权威，必须在证据里标出来，让 judge 知道不能仅凭它下"不可达"的结论。
_DYN_PUSH = re.compile(
    r"(?:push|replace)(?:Path|PathByName|Destination|DestinationByName)\("
    r"\s*(?!['\"])[A-Za-z_$][\w.$]*\s*[,)]")
_ROUTE_BRANCH = re.compile(r"name\s*===?\s*['\"](\w+)['\"]")
_NAV_DEST = re.compile(r"\.navDestination\(")
# 任意 ASCII 标识符样式的字符串字面量，用于按注册路由名反查间接入口
_ASCII_LIT = re.compile(r"['\"]([A-Za-z_][A-Za-z0-9_]{2,})['\"]")
_TAB_CONTENT = re.compile(r"TabContent\s*\(\s*\)\s*\{")
# 兜底层：任意展示型字面量。与语种无关（英文清单也能用），但排除明显不是 UI 文案的
# 形态 —— 全小写下划线的键名、URL、路径、纯数字。命中只作低置信锚点，会被标 weak。
_ANY_LIT = re.compile(r"['\"]([^'\"\n]{2,40})['\"]")
_NOT_LABEL = re.compile(r"^[a-z0-9_.]+$|://|^/|^\d+$")
# UI 组件的首个字面量实参 —— 这是"控件标签"最可靠的来源，且与语种无关。
# 早先只索引 CJK 字面量，英文清单（后续 app 可能出现）会拿不到任何字面量锚点；
# 而无脑索引所有字面量会把日志/键名/URL 全灌进来。限定在组件首参是有界且有意义的。
_WIDGET_LIT = re.compile(
    r"(?<![\w.])(?:Button|Text|Toggle|Checkbox|Radio|MenuItem|TabContent|SymbolGlyph)"
    r"\s*\(\s*['\"]([^'\"]{1,40})['\"]")


def _match_block(src: str, open_pos: int) -> int:
    opener = src[open_pos]
    closer = {"{": "}", "(": ")"}[opener]
    depth, j, n = 0, open_pos, len(src)
    while j < n:
        if src[j] == opener:
            depth += 1
        elif src[j] == closer:
            depth -= 1
            if depth == 0:
                return j
        j += 1
    return n


@dataclass
class _CodeIndex:
    routes: list[str]                       # navDestination builder 里注册的路由名
    push: dict[str, list[str]]              # 路由 -> push 点 file:line
    tab_order: list[str]                    # tab 根组件，按 TabContent 出现顺序
    res_anchor: dict[str, list[str]]        # 中文文案 -> $r 使用点 file:line
    lit_anchor: dict[str, list[str]]        # UI 组件首参字面量 -> 位置（高置信）
    text_anchor: dict[str, list[str]]       # 任意展示型字面量 -> 位置（低置信，标 weak）
    name_refs: dict[str, list[str]]         # 路由名以字面量出现的其它位置（间接入口线索）
    dyn_push: list[str]                     # 存在动态 push（路由名为变量）的文件
    ets_stems: dict[str, str]               # .ets 文件 stem -> 相对路径
    ets_lines: dict[str, int]               # 相对路径 -> 行数，用于校验 OK 证据真实存在
    ets_paths: dict[str, Path]              # 相对路径 -> 实际路径，供 _GuardFinder 读源码
    ets_count: int


def _build_index(harmony_dir: Path) -> _CodeIndex:
    files = [f for f in harmony_dir.rglob("*.ets")
             if not should_skip_path(f, harmony_dir)]

    # 资源表：中文 value -> 资源名
    val2name: dict[str, str] = {}
    for rp in harmony_dir.rglob("element/string.json"):
        if should_skip_path(rp, harmony_dir):
            continue
        try:
            data = json.loads(rp.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for it in data.get("string", []):
            v = str(it.get("value", "")).strip()
            if v and isinstance(it.get("name"), str):
                val2name.setdefault(v, it["name"])

    routes: list[str] = []
    push: dict[str, list[str]] = {}
    tab_order: list[str] = []
    name2use: dict[str, list[str]] = {}
    lit_anchor: dict[str, list[str]] = {}
    text_anchor: dict[str, list[str]] = {}
    dyn_push: list[str] = []
    all_str_lits: list[tuple[str, str]] = []   # (字面量, file:line) 用于路由名反查

    for f in files:
        rel = f.relative_to(harmony_dir).as_posix()
        try:
            src = _blank_comments(f.read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            continue
        short = rel.split("ets/", 1)[-1]

        for m in _RES_REF.finditer(src):
            name2use.setdefault(m.group(1), []).append(
                f"{short}:{src.count(chr(10), 0, m.start()) + 1}")
        for m in _WIDGET_LIT.finditer(src):
            lit_anchor.setdefault(m.group(1).strip(), []).append(
                f"{short}:{src.count(chr(10), 0, m.start()) + 1}")
        for m in _ANY_LIT.finditer(src):
            t = m.group(1).strip()
            if t and not _NOT_LABEL.search(t):
                text_anchor.setdefault(t, []).append(
                    f"{short}:{src.count(chr(10), 0, m.start()) + 1}")
        for m in _PUSH.finditer(src):
            push.setdefault(m.group(1), []).append(
                f"{short}:{src.count(chr(10), 0, m.start()) + 1}")
        if _NAV_DEST.search(src):
            routes.extend(_ROUTE_BRANCH.findall(src))
        if _DYN_PUSH.search(src) and short not in dyn_push:
            dyn_push.append(short)
        # 路由名反查用的字面量。排除两类不是入口的出现：
        #   a) 路由注册分支本身（`name === 'X'`）——那是被进入方，不是入口
        #   b) 与路由同名文件内部的自引用（多为 hilog 的 TAG 常量）
        branch_spans = [mm.span(1) for mm in _ROUTE_BRANCH.finditer(src)]
        for m in _ASCII_LIT.finditer(src):
            if any(bs <= m.start(1) < be for bs, be in branch_spans):
                continue
            if m.group(1) == f.stem:
                continue
            all_str_lits.append(
                (m.group(1), f"{short}:{src.count(chr(10), 0, m.start()) + 1}"))

        # Tab 根组件，按 TabContent 出现顺序记录。不试图解析 tab 标签文案 ——
        # 实测标签在 tabBarBuilder(index) 里通过 this.tabItems[index] 间接取，
        # 各 app 写法不一，解析出来大多是空串，反而给 judge 添噪。顺序本身足够：
        # 清单一级模块的顺序通常与 tab 顺序一致，judge 可据此自行确认。
        for tm in _TAB_CONTENT.finditer(src):
            brace = src.index("{", tm.end() - 1)
            block = src[brace:_match_block(src, brace) + 1]
            nav = re.search(r"Navigation\s*\([^)]*\)\s*\{\s*(\w+)", block)
            comp = nav.group(1) if nav else ""
            if comp and comp not in tab_order:
                tab_order.append(comp)

    # 中文文案 -> 使用点（只保留真的被代码引用的资源）
    res_anchor: dict[str, list[str]] = {}
    for v, n in val2name.items():
        if n in name2use:
            res_anchor[v] = name2use[n]

    # 路由名反查：注册路由名在别处以字面量出现 → 疑似间接入口
    route_set = set(routes)
    name_refs: dict[str, list[str]] = {}
    for lit, loc in all_str_lits:
        if lit in route_set:
            name_refs.setdefault(lit, []).append(loc)

    ets_stems = {f.stem: f.relative_to(harmony_dir).as_posix().split("ets/", 1)[-1]
                 for f in files}

    # 行数索引，只用于校验 OK 证据里的 file:line 真实存在。范围比锚点提取宽一层：
    # 也收 .ts —— ArkUI 的控件必在 .ets，但 handler 常落在 .ts 的 ViewModel/util 里，
    # judge 引用那里是合理的。只 rglob .ets 会把这类合法证据判成不成立，导致该行被
    # 永久退回 TODO。不读文件内容，只数行，代价可忽略。
    ets_lines: dict[str, int] = {}
    ets_paths: dict[str, Path] = {}
    for f in (*files, *(t for t in harmony_dir.rglob("*.ts")
                        if not should_skip_path(t, harmony_dir))):
        k = f.relative_to(harmony_dir).as_posix().split("ets/", 1)[-1]
        try:
            with f.open("rb") as fh:
                ets_lines[k] = sum(1 for _ in fh) or 1
        except OSError:
            continue
        ets_paths[k] = f

    return _CodeIndex(routes=sorted(set(routes)), push=push, tab_order=tab_order,
                      name_refs=name_refs, dyn_push=dyn_push, ets_stems=ets_stems,
                      res_anchor=res_anchor, lit_anchor=lit_anchor,
                      text_anchor=text_anchor, ets_lines=ets_lines,
                      ets_paths=ets_paths, ets_count=len(files))


# 清单自带的「源码位置」列：值形如 app/src/main/java/.../AudioPlayerFragment.java。
# 按**值的形态**识别而不是列名（各清单列名可能是 源码位置/source/位置/…，也可能是
# 英文），只要某列的值看起来是源码路径就用它 —— 这是跨 app 最稳的锚点来源，
# 实测新格式清单（antennapod/meshtatic）资源文案匹配几乎全军覆没（0~4/1000），
# 而这一列几乎每条都有。
_SRC_PATH = re.compile(r"[\w./-]*[\w-]+\.(?:java|kt|kts|xml)")


def _android_stem(feat: _Feature) -> str:
    """从清单的源码位置列取 Android 文件 stem；没有则返回 ''。"""
    for v in feat.meta.values():
        m = _SRC_PATH.search(v)
        if m:
            return m.group(0).rsplit("/", 1)[-1].rsplit(".", 1)[0]
    return ""


# Android→HarmonyOS 的常见改名后缀。只用于"同一功能的对应文件"猜测，猜不中就留空。
_STEM_SWAPS = (("Activity", "Page"), ("Fragment", "Page"), ("Activity", ""),
               ("Fragment", ""), ("", "Page"))


def _counterpart(stem: str, ets_stems: dict[str, str]) -> str:
    """Android stem → HarmonyOS .ets 相对路径；找不到返回 ''。"""
    if not stem:
        return ""
    if stem in ets_stems:
        return ets_stems[stem]
    for a, b in _STEM_SWAPS:
        if a and stem.endswith(a):
            cand = stem[: -len(a)] + b
        elif not a:
            cand = stem + b
        else:
            continue
        if cand and cand in ets_stems:
            return ets_stems[cand]
    return ""


_FID_RE = re.compile(r"\bF\d{3,}\b")
# manifest 行：| F001 | OK | path | anchor | guard | note |
# 只锚定前两个单元格，其余整行交给 Python 切 —— 列数变化（早先没有 guard 列）不能
# 让状态读回失效，也绝不能让 `[^|]*` 跨行吃到下一行去（那会把好行读成坏行）。
_ROW_RE = re.compile(r"^\|\s*(F\d{3,})\s*\|\s*(TODO|OK|ISSUE)\s*\|([^\n]*)$", re.M)


def _row_note(rest: str) -> str:
    """行尾 note 单元格。兼容 5 列（无 guard）与 6 列两种格式。"""
    cells = rest.split("|")
    return cells[-2].strip() if len(cells) >= 2 else ""


def _cell(text: object) -> str:
    """写进表格前必须过这里：单元格内的 `|` 会多撑出一列。

    实测 meshtatic 清单里有功能名自带 `|`，渲染出的行多一格，插件按列号读回 note 时
    整行错位（读到了 anchor 列）。清单文本来自人工填写，什么字符都可能有。
    """
    return str(text).replace("|", "/").replace("\n", " ").strip()


def _with_paths(issue: str, paths: dict[str, str]) -> str:
    """给 issue 里引用的清单 id 补上完整层级路径；已写全的不重复追加。

    判重前先去空格 —— judge 写 `作品>我的作品` 而 path_text 是 `作品 > 我的作品`。
    """
    flat = issue.replace(" ", "")
    add = [f"{fid} {paths[fid]}" for fid in dict.fromkeys(_FID_RE.findall(issue))
           if paths.get(fid) and paths[fid].replace(" ", "") not in flat]
    return f"{issue}  [checklist path: {'; '.join(add)}]" if add else issue


def _feats_from_manifest(manifest: Path) -> list[_Feature]:
    """manifest 的表格行 → 功能点列表。确定性解析失效时的清单来源。

    源文件解析不出来时（多级合并表头、说明段夹在表中间、单 sheet 混排两张表…），
    清单由 checklist-parser 一次性写进 manifest，此后 manifest 就是清单的权威来源。
    它本来已经是 status 的权威来源（`_marks_in_manifest` 每轮读它），这里只是让它在
    硬解析失效时同时承担 feature list，不引入新文件、不引入新的权威。

    **meta 按值的形态提取，不按位置** —— 这是幂等性的关键。渲染 TODO 行时 note 由
    meta 拼出，弱锚点还会前置一段 "weak anchor: …; "（见 `_render_manifest`）。若把整个
    note 原样塞回 meta，下一轮渲染会再拼一次，note 逐轮膨胀。改用 `_SRC_PATH` 从 note
    里抠源码路径：它只匹配 .java/.kt/.kts/.xml 形态，"weak anchor: …" 这类文本不会命中，
    于是 `src=<path>` 写出去、下一轮抠回同一个 `<path>`，构造上幂等。副作用是
    `_android_stem` / `_counterpart` 的锚点继续可用。
    """
    try:
        text = manifest.read_text(encoding="utf-8")
    except OSError:
        return []
    feats: list[_Feature] = []
    for fid, _status, rest in _ROW_RE.findall(text):
        cells = rest.split("|")
        path = [s for s in _PATH_SEP.split(cells[0].strip() if cells else "") if s]
        if not path:
            continue
        src = _SRC_PATH.search(_row_note(rest))
        feats.append(_Feature(fid=fid, module=path[0], path=path, name=path[-1],
                              meta={"src": src.group(0)} if src else {}))
    return feats


_IF = re.compile(r"(?<![\w.])if\s*\(")
_MAX_GUARDS = 2          # 只留最内两层，再往外基本是与可见性无关的流程分支
# 滚动容器与普通布局容器。ArkUI 内置组件名，与 app 无关。
_SCROLLER = re.compile(r"(?<![\w.])(Scroll|List|Grid|WaterFlow)\s*\(")
_BOX = re.compile(r"(?<![\w.])(Column|Row|Stack|Flex|RelativeContainer)\s*\(")
# 有界高度的修饰符。滚动容器缺这些时自身高度取内容高度，超出父容器的部分被裁掉且
# 滚不到 —— 这是 ArkUI 的既定行为，不是某个 app 的写法问题。
_BOUNDED = re.compile(r"\.(?:layoutWeight|height|constraintSize|flexGrow)\s*\(")
# 父容器的高度约束（要拿到实参用于说明）。只认 height —— 纵向裁剪由高度决定。
_HEIGHT = re.compile(r"\.(?:height|layoutWeight|constraintSize)\s*\(\s*([^)]{0,24})")
# 声明为横向滚动 —— 此时"纵向内容被裁"不成立。Scroll 默认纵向、List 默认纵向。
_HORIZONTAL = re.compile(
    r"\.(?:scrollable\s*\(\s*ScrollDirection\.Horizontal|listDirection\s*\(\s*Axis\.Horizontal)")
# 语句开头的 UI 组件构造（`Text(`/`Row() {`）或 @Builder 调用（`this.tabBar(`）。
# 用来区分"分支 UI 树"与"分支控制流"：后者不影响任何控件是否渲染。
_UI_STMT = re.compile(r"^[ \t]*(?:[A-Z]\w*\s*\(|this\.\w+\s*\()", re.M)
# 单项守卫条件（无 && || ?: ），可以确定性代入初值求值
_SIMPLE_COND = re.compile(r"^[^&|?]+$")
_FALSY_LIT = frozenset(("false", "0", "''", '""', "``", "undefined", "null", "[]", "{}"))


class _GuardFinder:
    """算出某一行的**可见性前提**：外围 `if` 守卫链 + 滚动容器是否会把它裁掉。
    带文件缓存，一轮内每个文件只读一次。

    为什么需要：实测（runs_20260804_1721）judge 把「我的 > 快捷退订」标成 OK 并引用
    `MinePage.ets:628` —— 那行**确实**是快捷退订控件本身，证据校验当然通过。但整块被
    `if (this.showServiceCenter === 1)` 包住，而该 @State 初值为 0（Android 侧同名字段
    默认 1），于是控件在 UI 上永远看不到。证据校验只能证明"这一行存在"，证不了"用户
    看得见"。

    第二类同源：实测「我的」页的在线客服/意见反馈/售后服务三项在真机上找不到。它们
    没有任何 `if` 包裹、onClick 也接对了，`MinePage.ets:540` 的 `Scroll` 尾随修饰符只有
    `.scrollBar(BarState.Off)`，父 `Column` 是 `.height('100%')` —— 首屏之下的项被裁掉
    且滚不到。控件"存在且接线正确"与"用户看得见"之间的第二个缺口。

    插件在这里**只陈述事实、不下判断**：把守卫条件、守卫变量的声明初值、以及滚动容器
    的尺寸缺失抄进 manifest，举证责任留给 judge。不做"初值代入为假即判缺陷"的硬规则
    —— 实测三个仓 12%~20% 的 onClick 都会命中，绝大多数是合法的瞬态 UI 状态（菜单点开
    才显示、有输入才显示清除按钮、列表空态），而 `showServiceCenter` 与 `showSortMenu`
    在语法上完全同形，无法区分。硬判会制造大量假阳性，还会让 judge 学会忽略这个字段。
    """

    def __init__(self, harmony_dir: Path, files: dict[str, Path]) -> None:
        # files: manifest 里用的相对路径 -> 实际路径。一次建好，不要按需 rglob ——
        # 清单可达千余条，每次 rglob 会让 evaluate 从 ~2s 涨到 10s+。
        self._files = files
        self._root = harmony_dir
        self._src: dict[str, str | None] = {}
        # 同一位置一轮内会被问两次（渲染 + details 统计），块匹配不便宜，缓存结果
        self._at: dict[str, str] = {}

    def _source(self, rel: str) -> str | None:
        if rel not in self._src:
            path, text = self._files.get(rel), None
            if path is not None:
                try:
                    text = _blank_comments(
                        path.read_text(encoding="utf-8", errors="ignore"))
                except OSError:
                    text = None
            self._src[rel] = text
        return self._src[rel]

    def at(self, location: str) -> str:
        """`rel/path.ets:123` → 该行的可见性前提；无前提或定位失败返回 ''。"""
        if location in self._at:
            return self._at[location]
        rel, _, num = location.rpartition(":")
        out = ""
        if rel and num.isdigit() and (src := self._source(rel)):
            line = int(num)
            out = " · ".join(
                p for p in (self._guards(src, line), self._clip(src, line)) if p)
        self._at[location] = out
        return out

    def _clip(self, src: str, line: int) -> str:
        """该行是否落在一个"高度未定"的滚动容器里 → 会被裁掉且滚不到。

        只在**父容器有确定高度**时才报，这是唯一的假阳性闸门：滚动容器套在另一个滚动
        容器里、或父容器自身高度未定时，不给 layoutWeight 是正常写法。没有这一层判断
        时实测命中率 16%~49%（Meshtastic 167 个滚动容器命中 82 个），绝大多数不是缺陷。
        """
        inner = None                       # 最内层包含该行的滚动容器
        for sm in _SCROLLER.finditer(src):
            lp = src.index("(", sm.end() - 1)
            rp = _match_block(src, lp)
            bo = src.find("{", rp)
            if bo < 0 or src[rp + 1:bo].strip():
                continue                   # 必须是 `Scroll(…) { … }` 形态
            be = _match_block(src, bo)
            if src.count("\n", 0, bo) + 1 <= line <= src.count("\n", 0, be) + 1:
                start = src.count("\n", 0, sm.start()) + 1
                if inner is None or start > inner[0]:
                    inner = (start, sm.group(1), sm.start(), be)
        if inner is None:
            return ""
        start, kind, sfrom, send = inner
        smods = self._modifiers(src, send)
        if _BOUNDED.search(smods) or _HORIZONTAL.search(smods):
            return ""                      # 自身有界、或横向滚动（纵向裁剪不成立）
        # 父容器：包含该滚动容器且最内层的布局容器
        parent = None
        for bm in _BOX.finditer(src):
            lp = src.index("(", bm.end() - 1)
            rp = _match_block(src, lp)
            bo = src.find("{", rp)
            if bo < 0 or src[rp + 1:bo].strip() or bo >= sfrom:
                continue
            be = _match_block(src, bo)
            if be > send and (parent is None or bo > parent[1]):
                parent = (bm.group(1), bo, be)
        # 只认纵向堆叠的父容器。父容器是 Row 时子元素按横向排布，父高度约束与"纵向
        # 裁剪"无因果关系 —— 实测 Meshtastic `ConversationPage.ets:2097` 是
        # `Row(){ Scroll(){ Row(){ Button… } } }` 的横向快捷短语条，按 Row 父容器
        # 报裁剪是假阳性。
        if parent is None or parent[0] == "Row":
            return ""
        psize = _HEIGHT.search(self._modifiers(src, parent[2]))
        if not psize:
            return ""                      # 父容器高度未定 → 结论不成立，保持沉默
        return (f"inside {kind}@{start} which has no layoutWeight/height, under a "
                f"{parent[0]} of {psize.group(1)} — content past the first screen is "
                f"clipped and cannot be scrolled to")

    @staticmethod
    def _modifiers(src: str, block_end: int) -> str:
        """组件块 `}` 之后的尾随链式修饰符文本。

        按平衡括号逐个吃 `.name(...)`，不能用"遇到 `}` 就停"那种截断 —— 修饰符实参
        本身可以是多行对象字面量，它的闭合行 `})` 会被误判成链结束。实测因此把
        `EpisodesListFragment.ets:986` 的 List 判成"无尺寸约束"，而它
        `.divider({…})` 之后紧跟着 `.layoutWeight(1)`，是个假阳性。
        """
        i, n, out = block_end + 1, len(src), []
        while i < n:
            while i < n and src[i] in " \t\r\n":
                i += 1
            if i >= n or src[i] != ".":
                break
            j = i + 1
            while j < n and (src[j].isalnum() or src[j] == "_"):
                j += 1
            while j < n and src[j] in " \t\r\n":
                j += 1
            if j >= n or src[j] != "(":
                break
            end = _match_block(src, j)
            out.append(src[i:end + 1])
            i = end + 1
        return "".join(out)

    def _guards(self, src: str, line: int) -> str:
        found: list[str] = []
        for mm in _IF.finditer(src):
            lp = src.index("(", mm.end() - 1)
            rp = _match_block(src, lp)
            # 条件后必须紧跟 `{` 才形成块作用域；ArkUI 的 UI 分支恒带花括号，
            # 而无花括号的单语句 if 包不住控件声明，跳过不会漏。
            rest = src[rp + 1:]
            pad = len(rest) - len(rest.lstrip())
            if rest[pad:pad + 1] != "{":
                continue
            bo = rp + 1 + pad
            be = _match_block(src, bo)
            if not (src.count("\n", 0, bo) + 1 <= line <= src.count("\n", 0, be) + 1):
                continue
            # 分支的是控制流而不是 UI 树 → 与"控件是否渲染"无关。实测
            # `AccountInfoPage.ets:86` 的 `if (result.index === 1)` 在
            # `.then(async (result) => {…})` 里，块内只有 await/赋值，报出来是纯噪声。
            # 判据用"块内有没有语句开头的组件构造/@Builder 调用"而不是"是否在闭包里"
            # —— ForEach 的 item builder 也是箭头闭包，但它包的确实是控件。
            if not _UI_STMT.search(src[bo:be]):
                continue
            cond = " ".join(src[lp + 1:rp].split())
            init = self._initial_value(src, cond)
            # 代入声明初值后守卫为真 → 首屏用户就看得见，不必占用 judge 的注意力。
            # 只在能确定为真时才静音（单项条件 + 字面量初值），方向安全：判不准就照报。
            if init and self._initially_true(cond, init):
                continue
            found.append(f"{cond[:70]}{init}")
        return " ∧ ".join(found[-_MAX_GUARDS:])

    @staticmethod
    def _initially_true(cond: str, init: str) -> bool:
        """代入 `_initial_value` 取到的初值后，守卫是否**确定**为真。

        只处理单项条件，遇到 `&&`/`||`/三元一律返回 False（照报）。这个方向的错误
        是"多报一条噪声"，而反向（误静音）会藏掉真缺陷，所以宁可保守。
        """
        c = cond.strip()
        if not _SIMPLE_COND.match(c):
            return False
        val = init.rsplit("=", 1)[-1].rstrip(")").strip()
        if not val:
            return False
        truthy = val not in _FALSY_LIT
        if re.fullmatch(r"!\s*this\.\w+", c):
            return not truthy
        if re.fullmatch(r"this\.\w+", c):
            return truthy
        eq = re.fullmatch(r"this\.\w+\s*(===?|!==?)\s*(.+)", c)
        if eq:
            same = val == eq.group(2).strip()
            return same if eq.group(1).startswith("==") else not same
        return False

    @staticmethod
    def _initial_value(src: str, cond: str) -> str:
        """守卫里第一个 `this.X` 的声明初值，形如 ` (@State X = 0)`；查不到返回 ''。"""
        var = re.search(r"this\.(\w+)", cond)
        if not var:
            return ""
        decl = re.search(
            rf"^\s*((?:@\w+(?:\([^)]*\))?\s+)*)(?:private\s+|readonly\s+)*"
            rf"{re.escape(var.group(1))}\s*(?::[^=\n]+)?=\s*([^;\n]{{1,24}})",
            src, re.M)
        if not decl:
            return ""
        deco = decl.group(1).split()[0] + " " if decl.group(1).strip() else ""
        return f" ({deco}{var.group(1)} = {decl.group(2).strip()})"


# OK 行的证据：必须指到鸿蒙源码的具体行。限定 .ets/.ts 是有意的 —— 清单 meta 列
# 里带的是 Android 的 .kt/.java/.xml 路径，TODO 行渲染时会把 meta 写进 note，
# 若放宽后缀，一行只改 status 不填证据就能"继承"出假证据。
_EVIDENCE = re.compile(r"([\w./-]+\.(?:ets|ts)):(\d+)")
# 插件自填锚点的前缀，证据校验时剔除
_HINT = re.compile(r"hint:\s*[\w./-]+\.(?:ets|ts):\d+")
# OK 行保留的证据原文上限。够放 `file:line \`那一行\` — 一句解释`，又不让 926 行的
# manifest 失控（ISSUE 说明同理限 100）。
_MAX_PROOF_NOTE = 160


@dataclass
class _Marks:
    """清单的判定状态。OK 必须带可校验的证据，否则不算判定过。"""

    checked: set[str] = field(default_factory=set)      # 已判定（OK ∪ ISSUE）
    flagged: dict[str, str] = field(default_factory=dict)  # fid -> ISSUE 说明
    proof: dict[str, str] = field(default_factory=dict)    # fid -> OK 证据 file:line
    # fid -> judge 写的证据原文（含引用行文本与那句解释）。proof 只留归一化后的
    # file:line 供守卫定位，重渲染若也用它，judge 抄的行文本每轮都会被抹掉 —— 它下一轮
    # 就看不见自己上一轮的取证，只能重查。实测行号常不准（7%）而抄的原文可靠（84%），
    # 丢掉的恰是更有用的那半。
    proof_note: dict[str, str] = field(default_factory=dict)
    reverted: set[str] = field(default_factory=set)        # 标了 OK 但证据不成立 → 退回 TODO
    shared_proof: set[str] = field(default_factory=set)    # 与别行共用 file:line → 退回 TODO


def _check_evidence(note: str, by_base: dict[str, list[tuple[str, int]]]) -> str:
    """从 note 里取出**经校验存在**的 file:line；取不到返回 ''。

    校验而不是只做正则匹配，是因为"标 OK"必须和"标 ISSUE"一样费力，否则 judge 会
    走成本最低的路。实测（runs_20260804_1616）：judge 在 63 次探索调用后把 141 条
    全标 OK、0 条 ISSUE、score=0.0，而同一个仓在此前三个 run 里已被逐条核实存在真
    缺陷。当时 OK 只要改一个单元格、note 可留空，而 ISSUE 要写问题说明 —— 于是
    "清空 TODO 列"被最省力地满足了。

    按文件名分桶查（judge 可能写 entry/src/main/ets/pages/X.ets 也可能写 pages/X.ets，
    同名文件也可能存在于多个目录），命中后行号必须落在该文件实际行数内。文件不存在
    或行号越界 → 不算证据。分桶而不是逐个比对全表：清单可达千余条、文件也可达千余
    个，遍历会退化成百万级比较。
    """
    for m in _EVIDENCE.finditer(note):
        cited, line = m.group(1).replace("\\", "/"), int(m.group(2))
        if line < 1:
            continue
        for rel, n_lines in by_base.get(cited.rsplit("/", 1)[-1], ()):
            # 目录部分必须是彼此的后缀，避免 pages/X.ets 命中 sub/other/X.ets
            if line <= n_lines and (rel == cited or rel.endswith("/" + cited)
                                    or cited.endswith("/" + rel)):
                return f"{rel}:{line}"
    return ""


def _marks_in_manifest(manifest: Path, idx: _CodeIndex,
                       feats: list[_Feature]) -> _Marks:
    """读回 manifest 里已有的标记；OK 行的证据不成立就退回 TODO。

    judge（checklist-judge）会逐条 Edit 状态以便运行中可见进度，而插件在轮末也要
    重渲染整表。不先读回就会把 judge 刚写的标记覆盖成 TODO。所以文件里的标记是第一
    手事实 —— 但 OK 要过证据校验才算数，这道校验是防橡皮图章的唯一硬约束。
    """
    marks = _Marks()
    try:
        text = manifest.read_text(encoding="utf-8")
    except OSError:
        return marks
    by_base: dict[str, list[tuple[str, int]]] = {}
    for rel, n in idx.ets_lines.items():
        by_base.setdefault(rel.rsplit("/", 1)[-1], []).append((rel, n))
    for fid, status, rest in _ROW_RE.findall(text):
        if status == "TODO":
            continue
        note = _row_note(rest)
        if status == "ISSUE":
            marks.checked.add(fid)
            marks.flagged[fid] = note[:100]
            continue
        # 证据认整行，不限 note 单元格（judge 常写进 anchor 列）；但插件自己填的锚点
        # 带 `hint:` 前缀，先剔掉 —— 否则只改状态就白得一个"证据"。用前缀而不是拿
        # 当前锚点做文本相减：生成器每轮改代码会让行号漂移，相减会失配。
        proof = _check_evidence(_HINT.sub(" ", rest), by_base)
        if proof:
            marks.checked.add(fid)
            marks.proof[fid] = proof
            # note 单元格自带 file:line 时按原文留住（judge 的取证就写在这里）；
            # 只写进 anchor 列的少数情况退回归一化位置，至少不丢定位。
            kept = _HINT.sub(" ", note).strip()
            marks.proof_note[fid] = (kept[:_MAX_PROOF_NOTE]
                                     if _EVIDENCE.search(kept) else proof)
        else:
            marks.reverted.add(fid)
    _drop_shared_proof(marks, feats)
    return marks


def _drop_shared_proof(marks: _Marks, feats: list[_Feature]) -> None:
    """一行 file:line 撑起过多 OK —— 超出清单自身能解释的数量就退回 TODO。

    judge 按文件批量标记省掉了每行一次的模型往返，但也让"少数几条证据摊给一整页"
    变成最省力的路径 —— runs_20260811_2152 那次 924 条 OK 里 63% 共用位置，一行
    `SwipeActionsDialog.ets:145` 撑了 22 条。

    判据分两层，都由清单自身推出，不写死常数：
      1. 同名同上级的一组豁免。清单会把**同一控件从不同路径进入**记成多行（同一控件
         926 行里 342 行属此类，如「分享弹窗 > 分享-包括播放位置勾选框」4 行），如实
         取证本就该共用一行。
      2. 其余（混杂组：一行代码被多个不同控件引用）按组大小判，上限取清单里同名同上级
         组规模的 P95、夹在 [2,4]。**不能取最大值**——十份真实清单里最大组从 2 到 24
         不等（meshtatic 1102 行有个控件被记了 24 次），取 max 会让整份清单的门槛被
         单个极端值抬到 24，摊派 24 行都不拦。极端组本身走第 1 层豁免，不需要 cap 放行。

    第 2 层是收敛性的关键：只要组不超上限就稳定，judge 不会被永久卡住。早先"只要共用
    就退"会让重复清单陷入死循环——用户清单里两行完全相同、或同一控件记成不同名字时，
    judge 无论怎么取证都会被退，每轮重做每轮被退。宁可放过少量摊派，不能不收敛。
    """
    where = {f.fid: (f.name, f.path[-2] if len(f.path) > 1 else "") for f in feats}
    sizes = sorted(Counter(where.values()).values()) or [1]
    p95 = sizes[max(0, min(len(sizes) - 1, round(0.95 * (len(sizes) - 1))))]
    cap = max(2, min(4, p95))
    seen: dict[str, list[str]] = {}
    for fid, loc in marks.proof.items():
        seen.setdefault(loc, []).append(fid)
    dup = [fid for fids in seen.values()
           if len({where.get(f) for f in fids}) > 1 and len(fids) > cap
           for fid in fids]
    for fid in dup:
        marks.checked.discard(fid)
        marks.proof.pop(fid, None)
        marks.proof_note.pop(fid, None)
        marks.reverted.add(fid)
        marks.shared_proof.add(fid)
    if dup:
        log.warning("feature_checklist: %d 行 OK 摊派在同一 file:line 上（上限 %d），退回 TODO",
                    len(dup), cap)


def _prev_checked(runs_dir: Path | None) -> int | None:
    """上一轮 details 里的 checked_count；取不到返回 None。

    用来算"上一轮实际推进了多少行"。judge 每轮从零起意，看不到自己上轮交付了多少 ——
    把这个数回灌进 details（details 会被拼进派发 prompt），它才知道自己是不是在敷衍。
    """
    if runs_dir is None:
        return None
    hist = Path(runs_dir) / "entropy_history.jsonl"
    if not hist.is_file():
        return None
    try:
        lines = hist.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        fc = (rec.get("details") or {}).get("feature_checklist") or {}
        n = fc.get("checked_count")
        if isinstance(n, int):
            return n
    return None


def _issues_so_far(runs_dir: Path | None) -> dict[str, str]:
    """从历史 verdict 恢复 ISSUE 结论，返回 fid -> issue 摘要。

    只恢复 ISSUE，不恢复 OK。历史 verdict 里没有 file:line 证据，而 OK 现在必须带
    经校验的证据（见 `_check_evidence`）—— 无证据的 OK 一律退回 TODO 重新核，否则
    这条路径就成了绕过证据要求的后门。丢掉的只是"重复核一遍已通过的功能"，方向安全。

    仅在 manifest 被删或换了 runs_dir 时才起作用（正常情况下 manifest 自身就是权威
    态）。issue 的 fid 从文本里抽 —— prompt 强制每条 issue 引用清单 id。
    """
    flagged: dict[str, str] = {}
    if runs_dir is None:
        return flagged
    hist = Path(runs_dir) / "entropy_history.jsonl"
    if not hist.is_file():
        return flagged
    try:
        lines = hist.read_text(encoding="utf-8").splitlines()
    except OSError:
        return flagged
    for line in lines:                      # 按迭代顺序，后一轮结论覆盖前一轮
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        v = ((rec.get("details") or {}).get("feature_checklist") or {}).get(
            "agentic_verdict") or {}
        for issue in v.get("issues") or []:
            if not isinstance(issue, str):
                continue
            for fid in _FID_RE.findall(issue):
                flagged[fid] = issue.strip()[:100]
    return flagged


def _render_manifest(feats: list[_Feature], idx: _CodeIndex, marks: _Marks,
                     guards: _GuardFinder | None = None) -> str:
    """整份清单渲染成 judge 可 Read 的表格，并标出已核查过的行。

    走文件而不是 details：141 条约 4.5KB 勉强能塞，1102 条 47KB 完全塞不下，而
    judge 有 Read 工具，落盘后规模再大也能全量看到。judge 见不到全量清单就会按
    常识补全——实测它编出过清单里根本不存在的一级模块。

    `status` 列是跨迭代进度：judge 一轮只能抽样（实测 71 次工具调用覆盖 141 条），
    标记让它每轮优先看 TODO 行，多轮累积走完全表，而不是每轮从头重新抽样。
    """
    todo = [f for f in feats if f.fid not in marks.checked]
    issue_n = sum(1 for f in feats if f.fid in marks.flagged)
    ok_n = len(feats) - len(todo) - issue_n
    lines = [
        "# Feature checklist (full, authoritative)",
        "",
        f"Total {len(feats)} · OK {ok_n} · ISSUE {issue_n} · TODO {len(todo)}",
        "",
        "Only these features are in scope. Every issue must cite an id below.",
        "",
        "status: `TODO` not verified yet · `OK` reachable by clicking through the UI, "
        "proven by `file:line` in the note · `ISSUE` verified, problem found (see note)",
        "**An `OK` row must carry ``<file>.ets:<line> `the text of that line` `` you "
        "found yourself, or it is reset to `TODO`.** The cited line must be the one "
        "binding the interaction (`.onClick(`, `.onChange(`, `.gesture(`, an `onX:` "
        "field, or the handler's declaration) — not a container, modifier, brace or "
        "data statement. The `hint:` location in the anchor column is the plugin's "
        "guess, not your evidence.",
        "`visibility` = what stands between the cited line and the user's eyes: the "
        "`if (…)` it sits inside (with the guard variable's declared initial value), "
        "and/or a scroll container that cannot show it. **A row with anything here is "
        "`OK` only if the note also says why the user still sees the control** — what "
        "makes the guard true, or why the clipping does not apply. Otherwise `ISSUE`.",
        "**Take the TODO rows first.** Re-check an OK row only with a concrete "
        "reason; re-check an ISSUE row to confirm whether the fix landed.",
    ]
    if marks.reverted:
        lines += [
            "",
            f"⚠ reset to TODO last round — `OK` without usable evidence "
            f"({len(marks.reverted)}): {', '.join(sorted(marks.reverted)[:20])}"
            f"{' …' if len(marks.reverted) > 20 else ''}. The cited file:line was "
            f"missing, out of range, or absent. Re-verify and cite the onClick site.",
        ]
    lines += [
        "",
        "| id | status | checklist path | anchor | visibility | note |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for f in feats:
        a = _anchor_for(f, idx)
        loc = a.lstrip("~")
        if f.fid in marks.flagged:
            status, note = "ISSUE", marks.flagged[f.fid]
        elif f.fid in marks.proof:
            status = "OK"
            note = marks.proof_note.get(f.fid) or marks.proof[f.fid]
        else:
            status = "TODO"
            note = "; ".join(f"{k}={v}" for k, v in list(f.meta.items())[:3])[:80]
            if a.startswith("~"):
                note = ("weak anchor: may be a toast/log with this wording, not "
                        "the control; " + note).rstrip("; ")
        # 守卫按该行**当前携带的位置**算：OK 行用 judge 引用的证据行（这才是它据以
        # 判定的那一行），其余行用锚点。判定过的行下一轮就会看到守卫，从而被迫复核。
        guard = guards.at(marks.proof.get(f.fid) or loc) if guards else ""
        lines.append(
            f"| {f.fid} | {status} | {_cell(f.path_text)} | "
            f"{('hint: ' + _cell(loc)) if loc else '(not found — locate it yourself)'} | "
            f"{_cell(guard)} | {_cell(note)} |")
    return "\n".join(lines) + "\n"


def _anchor_for(feat: _Feature, idx: _CodeIndex) -> str:
    """只用精确匹配找锚点，且只看最深两级；找不到返回 ''。

    两条约束都是实测逼出来的：
      * 放宽到子串/模糊 → 「勾选支付协议」会锚到一级模块「PPT创作」的无关文案；
      * 允许一路往上退 → 「编辑大纲主题」退到二级模块「生成大纲」，锚到步骤指示器
        的面包屑 Text（ChoiceTemplatePage.ets:339），而那根本不是被检查的控件。
    错锚比不锚更坏（会把 judge 直接带偏），所以宁缺毋滥 —— 空锚点由 judge 自行 Grep。
    """
    for label in reversed(feat.path[-2:]):
        hits = idx.res_anchor.get(label) or idx.lit_anchor.get(label)
        if hits:
            return hits[0]
    # 清单自带源码位置 → 鸿蒙同名/改名文件。文件级而非行级，但比弱字面量可靠得多。
    cp = _counterpart(_android_stem(feat), idx.ets_stems)
    if cp:
        return cp
    # 兜底层：任意展示型字面量。覆盖率换来的是置信度下降，所以调用方会标 weak。
    for label in reversed(feat.path[-2:]):
        hits = idx.text_anchor.get(label)
        if hits:
            return f"~{hits[0]}"
    return ""




# ══════════════════════════════════════════════════════════════════════════════
# LLM 兜底解析
# ══════════════════════════════════════════════════════════════════════════════

_JUDGE_AGENT = "checklist-judge"
# 用 .md agent 定义而不是 Claude Code 内置的 general-purpose：opencode 侧没有同名内置
# agent，而 `.md` 有现成的翻译层（`runner/opencode.py` 的 _stage_agents 会把 frontmatter
# 改写成 opencode schema）。插件这边只出 agent 名，`Agent` 还是 `task` 由 cli 按 runtime
# 决定。codex 没有 runner、也没有 subagent 概念，届时派发不了 → manifest 写不出 →
# iteration 超限 → 永久 skipped，退化成没有本兜底时的行为，不需要额外代码。
_PARSER_AGENT = "checklist-parser"
# 兜底解析最多在前几轮尝试。用 iteration 而不是写进文件的计数器：manifest 是唯一
# 产物，而它会被 parser 整体覆写，任何写在里面的标记都可能被抹掉，抹掉就无限重试。
# iteration 由 engine 传进 config（engine.py），无状态、agent 动不了。
_MAX_PARSE_ITER = 2

# 占位符用尖括号而非 str.format —— prompt 里有 JSON 花括号，format 需要全部转义。
_PARSE_PROMPT = """\
Convert a human-authored feature checklist into the manifest table this dimension
scores against. You are a parser, not a judge: you score nothing.

The checklist is at:
  <SRC>
ECAT's deterministic parser could not find a hierarchy in it — merged header cells,
a prose block in the middle of the sheet, unusual column names, or two tables on one
sheet. Read it yourself and write the table.

## How to read it

Run Python with THIS interpreter — it is the one that has openpyxl installed. Your
shell's `python` is very likely a different one:
  <PYTHON>

For .xlsx, also read `ws.merged_cells.ranges`. `iter_rows(values_only=True)` leaves
every merged cell except the top-left as None, so a multi-level merged header looks
like holes without it. Inspect a few rows first, then dump the rest once you know the
shape. Text formats (.csv/.tsv/.md) you may simply Read.

Rules:
- Skip sheets that are not the checklist (cover pages, test reports, statistics).
- A row whose upper levels are blank inherits them from the row above — that is what
  a merged cell looks like once flattened.
- The deepest level is the feature name.
- If a column holds Android source paths (.java/.kt/.kts/.xml), carry the value into
  the note cell. It is the strongest anchor the judge will have.

## What to write

Overwrite exactly this one file, and nothing else:
  <MANIFEST>

```
| id | status | checklist path | anchor | visibility | note |
| --- | --- | --- | --- | --- | --- |
| F001 | TODO | 我的 > 联系我们 > 客服会话 |  |  | app/src/ServiceActivity.java |
```

`id` is `F` + a zero-padded 3-digit row number from `F001`, in checklist order.
`status` is `TODO` on every row. Leave `anchor` and `visibility` empty — the plugin
computes them next round. Replace any `|` inside a cell with `/`.

Do not touch the HarmonyOS or Android source trees. Your Bash access is for reading
the checklist file only.

## Every row comes from that file, and this is checked

Read only the checklist named above — not another run's manifest, not your own
transcript. The plugin can read the raw cells even though it cannot find the hierarchy,
and checks your feature names against them: a table from anywhere else is discarded
whole and you are dispatched again, so copying one costs more than parsing.

If you truly cannot read it, write nothing and say why — but try the interpreter above
first, it is installed and it works.

## Output

Always end with exactly this, regardless of how it went:
{"score": 0.0, "rationale": "<what you wrote, or why you could not>", "issues": []}
"""


def _snapshot_initial(manifest: Path, text: str, iteration: int) -> None:
    """iter 0 的 manifest 另存一份基线，之后的轮次一律不碰。

    用途是留住**ECAT 动工前**的 OK 数：生成器要到 iter 0 的 gradient 产出之后才第一次
    改代码，所以 iter 0 判别阶段结束时的 manifest 就是基线。后续轮次的 OK 数与它相减
    才是 ECAT 真正修出来的增量 —— 否则清单里本来就通过的功能会被算进成绩。

    iteration != 0 直接返回，这是"过程中不修改"的唯一闸门。iter 0 内部允许覆盖：
    `evaluate()` 先写一份（judge 尚未打分，多半全 TODO），轮末 `extract_work_items()`
    带着 verdict 再覆盖一次，最终留下的是判别完成后的状态。先写那次是兜底 —— judge
    派发失败时至少还有一份基线，不至于整个 run 没有参照。
    """
    if iteration != 0:
        return
    snap = manifest.with_name(_INITIAL_MANIFEST_NAME)
    banner = (
        "<!-- ECAT baseline: iteration 0 判别完成后的快照，后续轮次不再写入。\n"
        "     与 " + _MANIFEST_NAME + " 的 OK 数相减 = ECAT 修出来的增量。 -->\n\n"
    )
    with contextlib.suppress(OSError):
        snap.write_text(banner + text, encoding="utf-8")


def _baseline_ok(manifest: Path) -> int | None:
    """基线快照里的 OK 行数；快照不存在或读不出返回 None。"""
    snap = manifest.with_name(_INITIAL_MANIFEST_NAME)
    try:
        text = snap.read_text(encoding="utf-8")
    except OSError:
        return None
    return sum(1 for _fid, status, _rest in _ROW_RE.findall(text) if status == "OK")


# ══════════════════════════════════════════════════════════════════════════════
# 插件
# ══════════════════════════════════════════════════════════════════════════════

class FeatureChecklistPlugin(EntropyPlugin):
    """按人工功能清单逐条核对实现、路径一致性与可用性。"""

    name = "feature_checklist"
    # 注意：EntropyPlugin.default_weight 全仓无读取点，只是文档；真正生效的是
    # config.py 的 _DEFAULT_WEIGHTS 与 yaml 的 entropy.weights。
    # 给 0.1 是安全的：没配清单时 evaluate 直接 skipped 贡献 0，judge 也被 prompt
    # 里的早退指令挡掉，不会误抬 H。
    default_weight = 0.1
    judge_mode = "agentic"
    # 分数完全取自 judge 的 verdict（hw=0 → blended = soft）。插件自己从 manifest 算的
    # 分数只作 details 里的诊断信号：它在 evaluate() 里算，那时 judge 还没跑，iter 0
    # 的 manifest 是空的，拿它当分数会让"发现最多的一轮"得 0 分（实测 1713：10 条
    # issue、29 行 ISSUE，H=0.0000，还被 extract_work_list 的 score<=0 跳过）。
    # 代价：judge 少报数字就少扣分，靠 prompt 里的机械口径顶着，不是硬约束。
    hard_weight = 0.0
    # 专用 judge：需要 Edit 权限逐条回写 manifest 状态，让进度在轮次运行中就可见。
    # 该 agent 定义里明确限定只能编辑 manifest，不得改动任何源码。
    # 硬解析失效的轮次会被 evaluate() 临时换成 _PARSER_AGENT（见 _request_llm_parse）。
    judge_subagent_type = _JUDGE_AGENT
    default_fix_skill = "hmos-arkui-develop-skill"

    # evaluate() 存下本轮上下文，供 extract_work_items() 回写 manifest。
    # 插件实例在 _PLUGINS 里是单例，evaluate 与 extract 在同一轮内先后执行。
    _state: tuple[list[_Feature], _CodeIndex, Path, _Marks, _GuardFinder] | None = None
    # evaluate() 记下本轮轮次号 —— extract_work_items() 拿不到 iteration，而基线快照
    # 只在 iter 0 写，闸门要靠它。单例实例，每轮 evaluate 开头覆盖。
    _iteration: int = 0

    judge_prompt = """\
Verify a human-authored feature checklist against this HarmonyOS migration.

If details has a `skipped` key, output {"score": 0.0, "issues": []} at once — do
not explore.

A checklist `path` reads `<top-level module> > <entry> > … > <control>` and is the
EXPECTED user route: enter the top-level module (usually a tab), then each level
in order. Check each feature for:

1. **Implemented** — does the code exist at all?
2. **Path matches** — a screen that exists and is registered but is only pushed
   from an unrelated page is a PATH VIOLATION, not a success. A route with three
   entries can still be missing the one this feature needs.
3. **Usable** — onClick wired, no TODO/stub, handler does not dead-end, and no
   Android branch dropped (collapsing two paths into one is a defect).

## The bar for `OK`

A feature is `OK` only when **a user could reach and see the control by clicking
through the UI along the checklist path** — the control is rendered (not behind a
condition that is never true, not in dead code), and every step of the path has a
real entry. Existing in the source is not enough.

Prove it in the note cell, in exactly this shape:

    <file>.ets:<line> `<the text of that line, copied>`

Copy the line from the file you just read — do not reconstruct it from memory and do
not guess a number. The line you cite must be the one that **binds the interaction**:
`.onClick(`, `.onChange(`, `.onCancel(`, `.gesture(`, `.bindMenu(`, an `onX:` field in
a dialog's parameter object, or the declaration line of the handler method those call.

These are not evidence, even in the right file — they are what a line looks like when
the number was guessed rather than read:

    QueuePage.ets:1546 `Row() {`
    SubscriptionsPage.ets:1564 `.width('100%')`
    EpisodeDetailPage.ets:1043 `}`
    QueuePage.ets:214 `const ep = stored[i] as Record<string, Object>;`

Getting the file right is the easy half and it is not enough: a citation whose line is
a layout container, a modifier, a brace, or a data statement leaves the row unproven,
and an unproven `OK` is reset to `TODO` — you will meet the row again, having spent
the round. Marking OK costs exactly what marking ISSUE costs; both are cheaper than
being wrong. If you cannot find the binding line, the honest mark is `TODO`, not `OK`.

The manifest's `visibility` column lists what stands between your cited line and the
user: the `if (…)` it sits inside (with the guard variable's declared initial value),
and/or a scroll container that cannot show it. A row with anything there is `OK` only
if the note ALSO says why the user still sees the control — what makes the guard true
(a gesture, or the data that sets it), or why the clipping does not apply. If nothing
a user can do makes it visible, that is an `ISSUE`.

Two real cases, both wired correctly and both invisible: a control at
`MinePage.ets:628` inside a flag whose default was migrated from Android's 1 to 0;
and a whole settings list clipped because its `Scroll` had no `layoutWeight` under a
`.height('100%')` parent.

Two failures the column cannot show you, both of which leave the button visible and
the click responsive:

- **A `push` issued in the same tick as the state change that swaps the navigation
  root is dropped** — the target `Navigation` has not mounted yet. When a checklist
  path starts at app launch or at a root swap, read the callback that does both.
- **A hand-off with no way back.** When a handler passes control to another app or
  the browser (`openLink`, `startAbility`, an OAuth URL, a deep link), the flow is
  complete only if BOTH directions are declared and consumed.
  *Outbound*: `canOpenLink`/`openLink` on a scheme needs that scheme in this module's
  own `querySchemes`, or the call throws — and a `catch` that returns `false` turns a
  missing declaration into "the other app is not installed", which reads as normal
  degradation.
  *Inbound*: the return scheme must be in `skills.uris`, and some code must receive
  the callback and turn its result into app state.
  `querySchemes` and `skills.uris` are different fields; one being present says
  nothing about the other. A missing link is an `ISSUE`. And if a comment says the
  native SDK is unavailable on HarmonyOS, that is a claim — check that the fallback
  it chose can actually complete.

**Read `details.manifest` first** — it is the full, authoritative checklist
(id | path | anchor | note). Work from it; it is the only list of features in
scope. An empty `anchor` means the wording matched nothing in code, which is NOT
evidence of absence — checklist labels are behaviour descriptions, not UI
strings. Grep before judging those.

`details.nav` is your routing evidence: `tab_order` (tab roots, usually in the
same order as the checklist's top-level modules), `routes`, `push` (entry sites),
`push_counts`. Reachability is three-state and none of it is final —
`routes_no_entry` are the strongest suspects, `routes_indirect_entry` are pushed
dynamically (check `dynamic_push_files` before calling them unreachable).

Android source is the ground truth for expected paths — read it when a path looks
wrong. Find the counterpart by screen name, or from the checklist's own source
column when it has one (`details.suspects[].at` already resolves that where
available).

Two hard limits on what counts as an issue:
- **Checklist scope only.** Every issue must cite an id that exists in the manifest,
  and the path you quote must match that id exactly. **An issue citing no valid id is
  discarded before the Generator sees it**, so an out-of-scope finding is wasted work,
  not a bonus. Reading the Android source will show you real defects the checklist
  never listed — another dimension owns those.
- **Comments are not evidence.** A TODO / "may not work" / "platform limitation"
  note in the code is a claim, not a defect. Verify the behaviour yourself before
  reporting it; if you cannot, do not report it.

Each issue must be actionable without re-derivation:
`<file>.ets: <what to change> (checklist <path>)` — name the target route/entry
and the Android source. Prefer "wire A into B" over "B is missing".

## Your `score` is this dimension's score — compute it, do not estimate it

When you are done editing the manifest, read its summary line and report:

    score = (Total - OK) / Total

`Total 100 · OK 25` → `0.75`. `OK 99` → `0.01`. `OK 100` → `0.0`. Round to 3 decimals.

Only a row you proved `OK` lowers this. `TODO` and `ISSUE` count the same, on purpose:
a row nobody has looked at is not evidence that the feature works. So the number falls
as you work down the checklist and reaches 0 only when every row is verified — that is
the intended shape, not a problem to optimise around. Nothing else derives this number;
an estimate or a placeholder tells the loop the port is finished when it is not.

## Work by file, not by row

Many checklist rows end at controls in the same source file. Open that file once,
decide every row whose control belongs to it, and emit all those Edits **in one
response**. Turns are what a round costs — a measured run spent 18 minutes advancing 7
rows because each row took its own turn.

Batching groups rows that share evidence; it does not spread one row's evidence over
its neighbours. Rows whose control you did not actually locate stay `TODO` — being on a
page you read proves nothing. **Two rows may not cite the same line**: if you cannot
separate them well enough to point at different lines, neither is proven and both stay
`TODO`. The plugin resets duplicate-cited `OK` rows and reports the count as
`shared_proof_reset`.

## Order of work, and when to stop

**Re-check every `ISSUE` row before you touch a `TODO` row.** Those are last round's
work list, so that is where the code just changed: confirm the fix and flip the row to
`OK` with fresh evidence, or leave it `ISSUE` and say what is still wrong. Only a row
proven `OK` lowers the score, so this is the only work that can move it. Then work down
the `TODO` rows.

**Stopping.** The dispatch prompt's early-stop rule caps how many issues you may
**open**; it is not permission to end the round. It is overridden here. A round has two
jobs and normally ends only when **both** are done:

1. you opened `judge_max_issues` issues, **and**
2. you moved `details.round_target_rows` rows out of `TODO` (the plugin sizes this from
   what is left, so it is always reachable in one round).

Meeting the row target alone is not enough — deciding rows is the cheaper half, and a
round that only does the cheap half leaves the defects for a later round that may never
come. Confirming a fix does not count against the issue cap, so never skip a re-check
to stay under it.

**Two ways out when 1 cannot be met**, both requiring you to have met 2 first:

- no `TODO` row is left that you can honestly decide, or
- you swept the remaining areas and they genuinely yield no more issues — then the
  rationale must name the areas you swept and why each produced nothing. "I sampled a
  cross-section" and "the port looks broadly complete" do not qualify; they describe
  what you skipped, not what you checked.

Never invent an issue to reach the cap. A fabricated issue sends the Generator to fix
something that is not broken — worse than ending the round short and saying so.

`details.last_round_advanced` is how many rows the previous round actually decided. If
it is well under the target, that round under-delivered; do not repeat it.

**The target is a floor on work, not on marks.** Rows you cannot decide stay `TODO` and
carry into the next round; that is intended. Marking them anyway buys nothing — an `OK`
without its own `file:line` + copied line is reset, and rows sharing one cited line are
all reset.

Progress lives in the manifest, so your row edits ARE the record. Nothing extra is
needed in the verdict JSON beyond `score`, `rationale` and `issues`.
"""

    def _request_llm_parse(
        self, src: Path, manifest: Path, iteration: int
    ) -> tuple[float, dict[str, object]]:
        """硬解析与 manifest 都拿不到清单 —— 这一轮改派 parser 去写 manifest。

        `judge_subagent_type` / `judge_prompt` 在 `_build_agentic_judge_prompt` 与
        `builder._render_deferred_dispatch` 里都是**从插件实例**上读的，而两者都在
        evaluate() 之后执行，所以在这里换掉就能让本轮派发另一个 agent、带另一套指令。
        不需要新的派发机制，也不需要插件自己调 LLM。

        本轮该维度照常贡献 0，与原来的 unparsable 行为一致；`_state = None` 让
        `extract_work_items` 直接返回，不会拿空 feats 去覆写 parser 刚写好的 manifest。
        """
        if not _has_grid_content(src):
            return 0.0, {"skipped": "feature_checklist_unparsable",
                         "reason": f"{src.name} 读不出任何内容"
                                   f"（后缀不支持 / 文件损坏；xlsx 需已安装 openpyxl）"}
        if iteration > _MAX_PARSE_ITER:
            return 0.0, {"skipped": "llm_parse_failed",
                         "reason": f"{src.name} 的兜底解析在前 {_MAX_PARSE_ITER + 1} 轮"
                                   f"均未产出可用 manifest —— 该维度本次运行内关闭"}
        self.judge_subagent_type = _PARSER_AGENT
        self.judge_prompt = (_PARSE_PROMPT
                             .replace("<SRC>", str(src))
                             .replace("<MANIFEST>", str(manifest))
                             .replace("<PYTHON>", sys.executable))
        self._state = None
        log.info("feature_checklist: %s 硬解析失效，本轮改派 %s 写 manifest",
                 src.name, _PARSER_AGENT)
        return 0.0, {"skipped": "awaiting_llm_parse",
                     "reason": f"{src.name} 的层级结构未能确定性解析，本轮由 "
                               f"{_PARSER_AGENT} 提取清单写入 manifest，下一轮生效",
                     "checklist": src.name,
                     "manifest": str(manifest)}

    def extract_work_items(
        self, score: float, details: dict[str, object]
    ) -> list[WorkItem]:
        """本轮 verdict 落地后立刻把状态写回 manifest。

        这是插件唯一的**后置**钩子：`builder` 先 `_apply_agentic_verdicts`（把 verdict
        存进 details）再 `extract_work_list`，注释里明确说了这个顺序就是为了让
        extract_work_items 能读到 verdict。借它写 manifest 是副作用，但换来的是标记
        当轮可见——否则只能等下一轮从 entropy_history.jsonl 读回来，滞后一整轮。
        checklist-judge 自己有 Edit 会逐条改 manifest，这里是对账：把它漏改的行、以及
        它只写进 verdict.issues 的结论补齐，所以要先读文件再合并 verdict。

        本方法在一轮里会被调用多次（builder 为拼 prompt 先调了几次），verdict 未落地
        时直接返回；渲染是幂等的，重复写同样内容无害。

        WorkItem 仍由 `extractors._agentic_verdict_items` 从 verdict 生成，所以这里
        固定返回空列表，只做状态回写。
        """
        verdict = details.get("agentic_verdict")
        if not isinstance(verdict, dict) or self._state is None:
            return []
        feats, idx, manifest, _, guards = self._state
        # ── 越界 issue 就地丢弃 ────────────────────────────────────────────────
        # `extract_work_list` 先调本方法（:86）再调 `_agentic_verdict_items`（:90），
        # 所以在这里改 verdict["issues"] 能决定生成器最终收到什么，无需改引擎。
        #
        # 判据是"必须引用清单里真实存在的 id"。安卓源码是 judge 的判据来源，它读
        # `LoginActivity.kt` 核对在范围内的登录条目时，会顺带看到清单没收录的功能并
        # 报出来 —— 实测 runs_20260804_1013 就报了「荣耀登录永久隐藏」，那个功能安卓
        # 侧真实存在但清单 141 条里一条都没有。这类越界不是幻觉，而是把别的维度该管
        # 的事揽了过来，会让生成器去改清单没要求的代码。
        #
        # 丢弃是安全方向：对应的行仍是 TODO，下一轮会重新核；而放过去就会污染 work
        # list。prompt 里那条 "Checklist scope only" 单靠自觉，实测 1914~1013 有
        # 21%~50% 的 issue 不带 id，加了 manifest 与 scope 规则后 1156/1721 归零，
        # 但样本只有 9 条 —— 机械强制才留得住。
        valid = {f.fid for f in feats}
        raw = [i for i in (verdict.get("issues") or []) if isinstance(i, str)]
        in_scope = [i for i in raw if valid & set(_FID_RE.findall(i))]
        if len(in_scope) != len(raw):
            for dropped in (i for i in raw if i not in in_scope):
                log.info("feature_checklist: 丢弃越界 issue（未引用清单 id）: %s",
                         dropped[:120])
        # 文件是权威态：judge 逐条 Edit 过，且 OK 行在这里过证据校验
        marks = _marks_in_manifest(manifest, idx, feats)
        # verdict.issues 里的 fid 补标 —— judge 可能报了问题却没改行
        for issue in in_scope:
            for fid in _FID_RE.findall(issue):
                if fid not in valid:
                    continue
                marks.checked.add(fid)
                marks.proof.pop(fid, None)
                marks.proof_note.pop(fid, None)
                marks.flagged[fid] = issue.strip()[:100]
        # 补全层级路径再交给生成器：judge 常只写叶子名（实测 1156 四条里三条如此），
        # 而生成器拿不到 manifest，查不回路径 —— 路径正是本维度的判据。放在 marks 之后，
        # manifest 的 note 保持原文。
        paths = {f.fid: f.path_text for f in feats}
        verdict["issues"] = [_with_paths(i, paths) for i in in_scope]
        self._state = (feats, idx, manifest, marks, guards)
        rendered = _render_manifest(feats, idx, marks, guards)
        with contextlib.suppress(OSError):
            manifest.write_text(rendered, encoding="utf-8")
        # iter 0 的判别到这里才算完（verdict 已合并），此刻的状态就是 ECAT 动工前的
        # 基线。iter>0 时 _snapshot_initial 直接返回，不会动那份文件。
        _snapshot_initial(manifest, rendered, self._iteration)
        return []

    def evaluate(
        self,
        harmony_dir: Path,
        *,
        android_dir: Path | None = None,
        spec_dir: Path | None = None,
        config: dict[str, object] | None = None,
    ) -> tuple[float, dict[str, object]]:
        cfg = config or {}
        raw = cfg.get("feature_checklist")
        if not raw:
            return 0.0, {"skipped": "no_feature_checklist_configured",
                         "reason": "paths.feature_checklist 未配置 —— 该维度默认关闭"}
        path = Path(str(raw))
        if not path.is_file():
            return 0.0, {"skipped": "feature_checklist_not_found",
                         "reason": f"paths.feature_checklist 指向的文件不存在: {path}"}

        # manifest 路径要在解析之前定出来 —— 硬解析失效时它是清单的备用来源。
        # 只算路径，不建目录：mkdir 仍留在下面原来的位置，跳过的轮次不该有副作用。
        runs_dir = Path(str(cfg["runs_dir"])) if cfg.get("runs_dir") else None
        base = runs_dir or (harmony_dir / ".ecat")
        manifest = (base / _MANIFEST_NAME).resolve()
        # extract_work_items() 看不到 config，基线闸门靠这个实例字段传递
        self._iteration = int(cfg.get("iteration") or 0)

        # 实例是 _PLUGINS 里的单例，上一轮 _request_llm_parse 设过的实例属性会残留，
        # 每轮开头必须复位回类属性，否则解析恢复正常后仍会去派发 parser。
        self.__dict__.pop("judge_subagent_type", None)
        self.__dict__.pop("judge_prompt", None)

        feats = _load_checklist(path)
        if not feats:
            # 源文件认不出层级 → 退到 manifest（由 checklist-parser 一次性写出）。
            # 必须校验它确实出自这份源文件：agent 会去抄隔壁 runs_* 的 manifest。
            feats = _feats_from_manifest(manifest)
            if feats and not _grounded_in_source(feats, path):
                feats = []
        if not feats:
            return self._request_llm_parse(path, manifest,
                                           int(cfg.get("iteration") or 0))

        idx = _build_index(harmony_dir)
        if not idx.ets_count:
            return 0.0, {"skipped": "no_ets_files"}

        # 可达性三态，不做二值判断 —— 实测 EntryAbility 用
        # `pushPathByName(pushRoute.pageName, …)` 动态 push，只认字面量会把
        # ShortCutPage / PushDetailPage 误报成"无人 push"。
        #   no_entry  : 无字面量 push，且路由名在别处也没以字面量出现 → 可信
        #   indirect  : 无字面量 push，但路由名在别处出现过 → 疑似间接入口，judge 复核
        no_entry, indirect = [], {}
        for r in idx.routes:
            if r in idx.push:
                continue
            refs = idx.name_refs.get(r)
            if refs:
                indirect[r] = refs[:2]
            else:
                no_entry.append(r)

        # ── 全量清单落盘，details 只放路径 ────────────────────────────────────
        # 不筛选、不分批。理由是实测教训：
        #   * 分批 → 相邻两轮量的不是同一批功能，early-stop 失去意义（出现过"最后
        #     两批恰好没问题 → H=0 提前收敛"，而前面报过 0.4 的问题一条没修）。
        #   * 只发可疑项 → judge 只见过 28/141 条 path，剩下的它按常识补全，编出了
        #     清单里根本不存在的「首页 > 顶部金刚区」并套上真实但无关的 F038-F047。
        # 而"哪条有问题"恰恰是 judge 才判得了的——硬检查能确定性判的只有可达性，
        # 用一个判不准的信号裁剪 judge 的视野，既没筛出问题又制造了幻觉。
        #
        # 清单塞不进 details（141 条 4.5KB 勉强，1102 条 47KB 完全不行），所以落盘；
        # judge 有 Read 工具，规模再大也能全量看到。
        try:
            base.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return 0.0, {"skipped": "manifest_write_failed", "reason": str(exc)}

        # ── 状态必须先合齐再渲染 ──────────────────────────────────────────────
        # 顺序不能反：先合并，最后才写文件。早先把读回文件的那一步放在写之后，等于
        # 先用未合并的状态覆盖了 manifest，再去读刚被清空的文件，judge 上一轮逐条
        # Edit 的标记全丢。
        #   1. 已有 manifest —— judge（checklist-judge）逐条 Edit 的第一手事实，
        #      轮末又被 extract_work_items() 合并 verdict 重写过，是权威态。
        #      其中 OK 行要过 file:line 证据校验，不过就退回 TODO。
        #   2. entropy_history.jsonl —— 仅作 ISSUE 的冷启动恢复（见 _issues_so_far）
        marks = _marks_in_manifest(manifest, idx, feats)
        for fid, why in _issues_so_far(runs_dir).items():
            if fid not in marks.proof:            # 文件里已有带证据的 OK 则不翻回 ISSUE
                marks.checked.add(fid)
                marks.flagged.setdefault(fid, why)
        guards = _GuardFinder(harmony_dir, idx.ets_paths)
        self._state = (feats, idx, manifest, marks, guards)

        # 状态合齐后才落盘 —— judge 接下来读的就是这份
        rendered = _render_manifest(feats, idx, marks, guards)
        try:
            manifest.write_text(rendered, encoding="utf-8")
        except OSError as exc:
            return 0.0, {"skipped": "manifest_write_failed", "reason": str(exc)}
        # 兜底基线：judge 若派发失败，轮末那次快照不会发生，至少留住这一份。
        # iter 0 正常走完时会被轮末带 verdict 的版本覆盖。
        _snapshot_initial(manifest, rendered, self._iteration)

        remaining = [f.fid for f in feats if f.fid not in marks.checked]
        _prev = _prev_checked(runs_dir)
        anchored = sum(1 for f in feats if _anchor_for(f, idx))
        push_full = {r: v[:_MAX_LOCS_PER_ROUTE] for r, v in sorted(idx.push.items())
                     if len(v) <= 1}
        push_counts = {r: len(v) for r, v in sorted(idx.push.items()) if len(v) > 1}

        details: dict[str, object] = {
            "checklist": path.name,
            "checklist_total": len(feats),
            "anchored": anchored,
            "manifest": str(manifest),
            # 覆盖进度 = 本轮 judge 开跑**之前**的 manifest 状态（evaluate 先于 judge
            # 执行），所以这里的数字比 manifest 文件本身滞后一轮；要看实时进度看
            # manifest 的计数行。
            "checked_count": len(feats) - len(remaining),
            # 分数直接由它算出：(total - ok) / total，放进 details 便于对账
            "ok_count": len(marks.proof),
            # ECAT 动工前的 OK 数（iter 0 快照），以及此后修出来的增量。
            # baseline_ok 在 iter 0 通常是 0（judge 还没判），真正有意义的是从
            # iter 1 起：ok_count - baseline_ok 才是 ECAT 的净贡献。
            "baseline_ok": _baseline_ok(manifest),
            "open_issue_count": len(marks.flagged),
            "remaining_count": len(remaining),
            # 本轮判定行数下限，judge 的 prompt 引用它 —— 数字由插件按剩余量算，
            # prompt 文本不写死，跨 app 自适应
            # 上一轮实际把多少行移出 TODO（judge 看不到自己上轮的交付量，回灌给它）
            "last_round_advanced": (None if _prev is None
                                    else max(0, len(feats) - len(remaining) - _prev)),
            "round_target_rows": min(_round_target_rows(_judge_timeout_s(cfg)), len(remaining)),
            # 上一轮标了 OK 但证据不成立、已被退回 TODO 的条数。持续为正说明 judge 在
            # 不取证地标 OK —— 这个数字本身就是要报的问题。
            "unproven_ok_reset": len(marks.reverted),
            # 其中因与别行共用 file:line 被退回的 —— 持续为正说明 judge 在按页摊证据
            "shared_proof_reset": len(marks.shared_proof),
            # 本轮开局仍未复核的 ISSUE 行 —— 这就是分数（见 return）
            "open_issue_fids": sorted(marks.flagged)[:_MAX_ROUTES],
            # 已判 OK 且其证据行外围有 if 守卫的条数 —— 这些 OK 的可见性取决于守卫，
            # judge 必须在 note 里说明守卫何时为真。
            "visibility_blocked_ok": sum(1 for fid, loc in marks.proof.items()
                              if guards.at(loc)),
            "nav": {
                "routes": idx.routes[:_MAX_ROUTES],
                "tab_order": idx.tab_order,
                "push": dict(list(push_full.items())[:_MAX_PUSH_ENTRIES]),
                "push_counts": dict(list(push_counts.items())[:_MAX_ROUTES]),
            },
            "routes_no_entry": no_entry[:_MAX_ROUTES],
            "routes_indirect_entry": dict(list(indirect.items())[:_MAX_PUSH_ENTRIES]),
            "dynamic_push_files": idx.dyn_push[:6],
        }
        # 分数 = 尚未取证判 OK 的比例。judge 用同一把尺子（见 judge_prompt 计分段），
        # 本轮 H 取它的 verdict（hw=0），这里算的是 verdict 缺失时的回退值。
        #
        # 不用"未修 ISSUE 条数×系数"：那只数**已知**缺陷，没检查过的行贡献 0，于是
        # 已知的几条一修完这一维就读作干净。实测 runs_20260811_1933 就这样在
        # 926 条只判定了 162 条（17.5%）时 H 归零早停，764 条从没被看过。
        # 只有取证判过 OK 的行才降分，所以"没检查"和"检查过没问题"不再等价。
        score = (len(feats) - len(marks.proof)) / len(feats)
        return score, details
