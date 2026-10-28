"""crash_risk —— 点击闪退风险静态检测（硬检查）。

`runtime_liveness`(N1) 已经在真机上做 smoke-tap 抓闪退，但它受设备、
`dynamic_render_interval` 节流和探索深度（depth 2 / 每页 cap 20）限制，够不到
需要登录或有数据才能进的深层页；而且它只在"崩了"时报警，发现不了"兜底层缺失"
这种本身不崩、却让所有别的崩溃变致命的问题。

本插件是它的**静态前置筛选**：纯规则、秒级、每轮可跑、全量覆盖所有 onClick，
给出精确 file:line + 按钮 + 调用链，直接喂 WorkItem。两者是筛选与终审的关系。

规则只收"确定性可验证"的一类 —— 每条都能靠解析给出是/否，不需要语义判断：

  L1 框架契约（ArkUI 硬约束，违反即崩，编译器查不到）
    R1  @Consume(k) 的渲染路径祖先链上必须有 @Provide(k)
    R2  navDestination builder 渲染的组件，build() 根节点必须是 NavDestination
    R3  pushPathByName 的目标必须在 navDestination builder 中注册
    R4  @CustomDialog struct 内不得声明 @Consume（弹窗树与页面树分离）
    R8  CustomDialogController 必须是组件成员变量，不能是方法内的局部变量
  L3 资源契约
    R5  $r('app.string.X') 的 X 必须存在（取值位抛异常 / 渲染位空白）
    R6  getStringByNameSync('X') 的 X 必须存在（一定抛异常）
  L4 兜底层
    R7  必须注册全局未捕获异常观察者

反假阳性是本插件的第一优先级，四道防线：
  1. 注释剥离（保留偏移与行号），避免文档示例被当成代码
  2. try 覆盖判定做大括号配对，不用行距启发式
  3. 活代码基线：modules.abc 可达性过滤，缺失时退回 import 图 BFS
  4. 证据不足即跳过：拿不到入口组件/渲染路径/资源索引的规则直接不报

issue 文本一律用英文（要进 Generator prompt），docstring 与注释用中文（对齐仓内
其他插件）。同文件同规则的发现会聚合成一条 issue，避免 9 个缺失资源刷屏。

适用于任何 Android→HarmonyOS 迁移 —— 所有判断都是 ArkUI 框架约定，不依赖
任何 app-specific 字符串。
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from ecat.entropy.base import EntropyPlugin, should_skip_path
from ecat.gradient.schema import WorkItem

log = logging.getLogger(__name__)

# ── 截断预算 ──────────────────────────────────────────────────────────────────
# details 整段进 discriminator prompt，issues 进 generator prompt，两边都要限长。
# 聚合之后单文件最多 7 条 issue（规则总数），所以单条可以给得宽一些。
_MAX_FINDINGS_PER_RULE = 12      # details.samples 每条规则最多列这么多
_MAX_ISSUE_CHARS = 560           # 单条 issue 上限（_clip 会保住句尾指令）
_MAX_SUBJECTS_IN_ISSUE = 8       # 聚合 issue 里最多点名这么多个具体对象
_MAX_LINES_IN_ISSUE = 10         # 聚合 issue 里最多列这么多行号
_MAX_WORK_ITEMS = 40             # extract 总上限（driver 只渲染前 15，其余落盘备查）
_MAX_PATH_NODES = 6              # 渲染路径最多展示这么多节点
_BUTTON_LOOKBACK = 900           # 从 onClick 往回找按钮标签的最大字符距离

# ── 严重度 → 分数权重 ─────────────────────────────────────────────────────────
# 必崩类记满权，"渲染空白"这类只记零头，避免大量低危把分数刷爆。
_RULE_WEIGHT: dict[str, float] = {
    "R1_consume_unresolved": 1.0,
    "R2_navdest_not_root": 1.0,
    "R3_route_unregistered": 1.0,
    "R4_dialog_consume": 1.0,
    "R5_string_res_throw": 0.6,
    "R5_string_res_blank": 0.15,
    "R6_res_name_missing": 0.6,
    "R7_no_global_handler": 1.0,
    "R8_dialog_controller_local": 1.0,
}
# 触发"必崩下限"的规则。R7 故意不在其中：它本身不是崩溃点而是放大器，且在 8 个
# 实测迁移仓上 100% 命中，让它触发下限会把所有仓的分数压平到同一个值，做变体
# 横向对比时完全失去区分度。它仍以权重 1.0 计入分子。
_CERTAIN_CRASH_RULES = frozenset((
    "R1_consume_unresolved", "R2_navdest_not_root", "R3_route_unregistered",
    "R4_dialog_consume", "R6_res_name_missing", "R8_dialog_controller_local",
))
# WorkItem 严重度与评分下限解耦：R7 不进下限（会压平分数），但对 Generator 仍是
# 高优先级修复项 —— 它决定其余所有风险点是"可恢复"还是"必闪退"。
_HIGH_SEVERITY_RULES = _CERTAIN_CRASH_RULES | {"R7_no_global_handler"}

# 每条规则的修复 skill —— hard 插件的 default_fix_skill 不生效
# （extractors._agentic_verdict_items 首行就 return 掉非 agentic 插件），
# 必须在 extract_work_items 里逐条填 suggested_skills。
_RULE_SKILL: dict[str, str] = {
    "R1_consume_unresolved": "arkts-state-manager",
    "R2_navdest_not_root": "arkts-navigation-builder",
    "R3_route_unregistered": "arkts-navigation-builder",
    "R4_dialog_consume": "arkts-state-manager",
    "R5_string_res_throw": "android2hmos_resources_convert",
    "R5_string_res_blank": "android2hmos_resources_convert",
    "R6_res_name_missing": "android2hmos_resources_convert",
    "R7_no_global_handler": "",   # 无对应 skill —— 宁可留空也不挂错
    "R8_dialog_controller_local": "arkts-component-builder",  # 无弹窗专用 skill，取组件结构类
}

# generator.txt 把 android_dir 设成 REQUIRED reference，每个 work item 都会先去读
# 安卓源码。框架契约类问题在 Android 侧没有对应物，不显式标注就是白烧 token。
_NO_ANDROID = ("HarmonyOS framework constraint with no Android counterpart — "
               "do not consult the Android source for this fix.")
_SEE_ANDROID = ("Port the original text from the Android "
                "res/values/strings.xml entry of the same name.")


# ══════════════════════════════════════════════════════════════════════════════
# 防线 1：注释剥离（保留长度与行号，偏移可直接复用）
# ══════════════════════════════════════════════════════════════════════════════

# 正则字面量 /…/ 出现的“表达式开头”上下文: 前一非空字符是这些之一时, 一个 / 更
# 可能是正则字面量而不是除法。TS/ArkTS 里正则内容可以含引号, 若不识别, /'/ 里
# 的引号会把注释剥离状态机翻进字符串位, 之后整文件的 // 注释都不再被剥离。
_REGEX_LITERAL_PRECEDERS = frozenset(
    "=([{,?:!&|;+-*%^~<>"
)


def _starts_regex_literal(src: str, pos: int) -> bool:
    """Return True if the ``/`` at *pos* likely opens a regex literal.

    Look back to the previous non-whitespace character. A ``/`` after an
    expression-opening char (assignment, paren, comma, brace, colon, return…)
    starts a literal; after an identifier/number/literal it is division. This is
    a heuristic — good enough to stop ``/…/`` contents from corrupting the string
    state machine in ``_blank_comments``, which is the actual fragility the
    review flagged.
    """
    j = pos - 1
    while j >= 0 and src[j] in " \t\r\n":
        j -= 1
    if j < 0:
        return True
    return src[j] in _REGEX_LITERAL_PRECEDERS


def _blank_comments(src: str) -> str:
    """把注释内容替换成空格，保留所有换行与字符偏移。

    上一轮实测：不剥注释时，"CustomDialogController 在 struct 外构造"这条规则的
    命中 100% 是文档里的 usage 示例。字符串字面量内的 `//`（如 URL）不能误判，
    所以要跟踪引号状态而不是简单正则。
    """
    out = list(src)
    i, n = 0, len(src)
    # 状态机: None | line | block | sq | dq | tpl | regex。regex 位不是字符串: 正
    # 则字面量内的引号(如 s.replace(/'/g, ''))不能把状态翻进字符串,否则该行之后
    # 所有 // 注释都因 state 卡在字符串位而保留原文,被下面的 RE 误当成真实调用。
    state: str | None = None
    while i < n:
        c = src[i]
        nxt = src[i + 1] if i + 1 < n else ""
        if state is None:
            if c == "/" and nxt == "/":
                state = "line"
                out[i] = out[i + 1] = " "
                i += 2
                continue
            if c == "/" and nxt == "*":
                state = "block"
                out[i] = out[i + 1] = " "
                i += 2
                continue
            # 正则字面量以 / 开头且不是 / 或 /*。TS/ArkTS 里正则内容可能含引号,
            # 若当字符串处理会被错误闭合。粗略界定: 往前看最近一个非空字符是不是
            # 赋值/括号/冒号/逗号/return/等号这类"表达式开头"的上下文。保守起见
            # 这里的识别只解决 review 指出的 /'|' | /"/g 这类替换字面量。
            if c == "/" and _starts_regex_literal(src, i):
                state = "regex"
                out[i] = " "
                i += 1
                continue
            if c == "'":
                state = "sq"
            elif c == '"':
                state = "dq"
            elif c == "`":
                state = "tpl"
            i += 1
            continue
        if state == "regex":
            # 正则字面量内部: 处理转义与 / 闭合, 保留内容(引号不再影响状态)。
            # 字符类 [..] 里的 / 不闭合, 需额外计数。
            if c == "\\":
                i += 2
                continue
            if c == "[":
                depth = 0
                while i < n:
                    if src[i] == "\\":
                        i += 2
                        continue
                    if src[i] == "[":
                        depth += 1
                    elif src[i] == "]":
                        depth -= 1
                        if depth == 0:
                            i += 1
                            break
                    i += 1
                continue
            if c == "/":
                state = None
            i += 1
            continue
        if state == "line":
            if c == "\n":
                state = None
            else:
                out[i] = " "
            i += 1
            continue
        if state == "block":
            if c == "*" and nxt == "/":
                out[i] = out[i + 1] = " "
                state = None
                i += 2
                continue
            if c != "\n":
                out[i] = " "
            i += 1
            continue
        # 字符串内部：只处理转义与闭合，内容原样保留
        if c == "\\":
            i += 2
            continue
        if (state == "sq" and c == "'") or (state == "dq" and c == '"') or (state == "tpl" and c == "`"):
            state = None
        i += 1
    return "".join(out)


def _line_of(src: str, off: int) -> int:
    return src.count("\n", 0, off) + 1


def _match_block(src: str, open_pos: int) -> int:
    """返回与 src[open_pos]（'{' / '(' / '['）配对的收尾偏移；失配返回 len(src)。"""
    opener = src[open_pos]
    closer = {"{": "}", "(": ")", "[": "]"}[opener]
    depth = 0
    j = open_pos
    n = len(src)
    while j < n:
        if src[j] == opener:
            depth += 1
        elif src[j] == closer:
            depth -= 1
            if depth == 0:
                return j
        j += 1
    return n


# ══════════════════════════════════════════════════════════════════════════════
# 防线 2：try 覆盖判定（大括号配对，非行距启发式）
# ══════════════════════════════════════════════════════════════════════════════

_TRY_RE = re.compile(r"\btry\s*\{")


def _try_spans(src: str) -> list[tuple[int, int]]:
    """所有 try 块体的 [start, end) 区间。上一轮按行距估算造成 12 个假阳性。"""
    spans: list[tuple[int, int]] = []
    for m in _TRY_RE.finditer(src):
        brace = src.index("{", m.start())
        spans.append((brace, _match_block(src, brace)))
    return spans


def _in_try(spans: list[tuple[int, int]], off: int) -> bool:
    return any(s <= off < e for s, e in spans)


# ══════════════════════════════════════════════════════════════════════════════
# 源码模型
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class _Struct:
    name: str
    rel: str                 # 相对 harmony_dir 的 posix 路径
    line: int
    decorators: set[str]
    body: str                # 已剥注释的结构体内容（含首尾大括号）
    body_off: int            # body 在文件中的起始偏移


@dataclass
class _FileModel:
    rel: str
    src: str                                        # 已剥注释
    try_spans: list[tuple[int, int]] = field(default_factory=list)
    onclick_spans: list[tuple[int, int]] = field(default_factory=list)
    structs: list[_Struct] = field(default_factory=list)
    global_builders: dict[str, str] = field(default_factory=dict)


_STRUCT_RE = re.compile(r"\bstruct\s+(\w+)\s*\{")
_DECO_RE = re.compile(r"@(\w+)")
_GLOBAL_BUILDER_RE = re.compile(r"@Builder\s+function\s+(\w+)\s*\(")
_ONCLICK_RE = re.compile(r"\.onClick\s*\(")
# 组件调用：项目内 struct 才算，`new Foo(` 排除（那是构造对象不是渲染组件）
_CALL_RE = re.compile(r"(?<![\w.])(new\s+)?([A-Z]\w*)\s*\(")
_METHOD_RE = re.compile(
    r"\n[ \t]{1,8}(?:private |public |protected |static |async |override )*(\w+)\s*\([^)]*\)\s*"
    r"(?::\s*[\w<>|,.\[\] ]+\s*)?\{"
)
# 与方法签名同形的控制流关键字，必须从 _enclosing_method 里排掉
_KEYWORDS = frozenset((
    "if", "for", "while", "switch", "catch", "return", "do", "else", "with",
))


def _parse_file(rel: str, raw: str) -> _FileModel:
    src = _blank_comments(raw)
    fm = _FileModel(rel=rel, src=src, try_spans=_try_spans(src))

    for m in _ONCLICK_RE.finditer(src):
        paren = src.index("(", m.start())
        fm.onclick_spans.append((paren, _match_block(src, paren)))

    for m in _STRUCT_RE.finditer(src):
        brace = src.index("{", m.start(1))
        end = _match_block(src, brace)
        # 往回扫紧邻的装饰器行；遇到非装饰器非 export 的实义行就停，
        # 避免把上一个声明的装饰器算到本 struct 头上。
        head = src[max(0, src.rfind("\n\n", 0, m.start())) : m.start()]
        decos: set[str] = set()
        for line in reversed(head.splitlines()):
            t = line.strip()
            if not t:
                continue
            if t.startswith("@"):
                decos.update(_DECO_RE.findall(t))
                continue
            if t.startswith("export"):
                continue
            break
        fm.structs.append(_Struct(
            name=m.group(1), rel=rel, line=_line_of(src, m.start()),
            decorators=decos, body=src[brace : end + 1], body_off=brace,
        ))

    for m in _GLOBAL_BUILDER_RE.finditer(src):
        paren = src.index("(", m.end(1) - 1)
        brace = src.find("{", _match_block(src, paren))
        if brace != -1:
            fm.global_builders[m.group(1)] = src[brace : _match_block(src, brace) + 1]
    return fm


def _invoked_structs(body: str, known: set[str], exclude: str = "") -> set[str]:
    """body 里出现的项目内组件调用。一次扫描后与已知 struct 名求交，天然排除
    ArkUI 内建组件；逐个 struct 名做 re.search 在 300+ struct 的仓上会退化。"""
    out: set[str] = set()
    for m in _CALL_RE.finditer(body):
        if m.group(1):          # new Foo(...)
            continue
        n = m.group(2)
        if n in known and n != exclude:
            out.add(n)
    return out


# ══════════════════════════════════════════════════════════════════════════════
# 按钮归属：把 file:offset 翻译成"哪个按钮"
# ══════════════════════════════════════════════════════════════════════════════

_LABEL_RES_RE = re.compile(r"\$r\(\s*['\"]app\.string\.([\w.]+)['\"]")
_LABEL_LIT_RE = re.compile(r"^\s*['\"]([^'\"]{1,24})['\"]")
_WIDGET_RE = re.compile(r"(?<![\w.])(Button|Text|Image|SymbolGlyph|MenuItem|ListItem)\s*\(")


def _widget_label(fm: _FileModel, off: int, res_index: dict[str, str]) -> str:
    """从偏移处回溯最近的可点控件标签，形如 Button "Choose template"；找不到返回 ''。"""
    lo = max(0, off - _BUTTON_LOOKBACK)
    seg = fm.src[lo:off]
    last = None
    for m in _WIDGET_RE.finditer(seg):
        last = m
    if last is None:
        return ""
    kind = last.group(1)
    arg_start = seg.index("(", last.start())
    arg = seg[arg_start + 1 : arg_start + 140]
    label = ""
    rm = _LABEL_RES_RE.search(arg)
    if rm:
        label = res_index.get(rm.group(1), rm.group(1))
    else:
        lm = _LABEL_LIT_RE.match(arg)
        if lm:
            label = lm.group(1)
    if not label:
        return ""
    label = " ".join(label.split())[:24]
    return f'{kind} "{label}"'


def _onclick_label(fm: _FileModel, off: int, res_index: dict[str, str]) -> str:
    """off 若落在某个 onClick 体内，返回该按钮标签。"""
    for s, e in fm.onclick_spans:
        if s <= off < e:
            return _widget_label(fm, s, res_index)
    return ""


def _enclosing_method(fm: _FileModel, off: int) -> str:
    """off 所在的成员方法名（取 off 之前最近的一个方法签名）。

    必须排掉控制流关键字：`    if (cond) {` 在正则上和方法签名同形，不排的话
    `best` 会停在 "if" 上，后续 `this.if(` 永远找不到，按钮链就断了。
    """
    best = ""
    for m in _METHOD_RE.finditer(fm.src, 0, off):
        if m.group(1) in _KEYWORDS:
            continue
        best = m.group(1)
    return best


def _caller_button(fm: _FileModel, off: int, res_index: dict[str, str]) -> str:
    """push 不在 onClick 里时再跳一跳：找包裹它的方法，再找调用该方法的 onClick。"""
    mname = _enclosing_method(fm, off)
    if not mname:
        return ""
    for cm in re.finditer(r"this\." + re.escape(mname) + r"\s*\(", fm.src):
        label = _onclick_label(fm, cm.start(), res_index)
        if label:
            return f"{label} -> {mname}()"
    return ""


# ══════════════════════════════════════════════════════════════════════════════
# 防线 3：活代码基线
# ══════════════════════════════════════════════════════════════════════════════

def _live_set(harmony_dir: Path, files: list[Path]) -> tuple[set[str] | None, str]:
    """返回 (可达文件相对路径集合, 判定方式)。拿不到证据时 (None, 'unfiltered')。

    实测四个迁移仓的死代码率在 45%~55%，不过滤会让报告淹没在不会执行的代码里。

    产物位置一律用 rglob 发现，不写死 `entry/build/default/...`：手上的样本恰好
    都是单模块且模块名为 entry，写死能跑通，但多模块工程（entry + feature HSP）
    或改过模块名/产品名的工程会静默退化成 unfiltered。多模块时取所有 modules.abc
    的并集。
    """
    blobs: list[bytes] = []
    for abc in harmony_dir.rglob("modules.abc"):
        try:
            data = abc.read_bytes()
        except OSError:
            continue
        if data:
            blobs.append(data)
    if blobs:
        live: set[str] = set()
        for f in files:
            rel = f.relative_to(harmony_dir).as_posix()
            stem = f.stem
            key = rel.split("/ets/", 1)[-1][:-4] if "/ets/" in rel else stem
            kb, sb = key.encode(), ("/" + stem).encode()
            if any(kb in b or sb in b for b in blobs):
                live.add(rel)
        if live:
            return live, "modules.abc"

    # 没有构建产物（CI 首轮）→ 退回 import 图 BFS
    by_stem: dict[str, Path] = {f.stem: f for f in files}
    roots: list[Path] = [f for f in files if f.name.endswith("Ability.ets")]
    # should_skip_path 过滤掉 build/ 下的陈旧副本 —— 每个仓的 main_pages.json 都
    # 有 src 与 build 两份，命中副本会用到上一次构建的路由表。
    for mp in harmony_dir.rglob("main_pages.json"):
        if should_skip_path(mp, harmony_dir):
            continue
        try:
            for s in json.loads(mp.read_text(encoding="utf-8")).get("src", []):
                cand = by_stem.get(Path(str(s)).name)
                if cand:
                    roots.append(cand)
        except (OSError, ValueError):
            continue
    if not roots:
        return None, "unfiltered"

    seen: set[Path] = set()
    dq = deque(roots)
    while dq:
        cur = dq.popleft()
        if cur in seen:
            continue
        seen.add(cur)
        try:
            text = cur.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for m in re.finditer(r"from\s+['\"]([^'\"]+)['\"]", text):
            spec = m.group(1)
            if spec.startswith("@"):
                continue
            nxt = by_stem.get(Path(spec).name)
            if nxt and nxt not in seen:
                dq.append(nxt)
    return {p.relative_to(harmony_dir).as_posix() for p in seen}, "import-graph"


# ══════════════════════════════════════════════════════════════════════════════
# 资源索引
# ══════════════════════════════════════════════════════════════════════════════

def _string_resources(harmony_dir: Path) -> dict[str, str]:
    """name -> value。空 dict 表示没找到资源文件，此时 R5/R6 必须整条跳过。"""
    out: dict[str, str] = {}
    for p in harmony_dir.rglob("element/string.json"):
        if should_skip_path(p, harmony_dir):
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for it in data.get("string", []):
            if isinstance(it, dict) and "name" in it:
                out[str(it["name"])] = str(it.get("value", ""))
    return out


# ══════════════════════════════════════════════════════════════════════════════
# 聚合：同文件同规则合并成一条 issue（信息完整 + 体积可控）
# ══════════════════════════════════════════════════════════════════════════════

def _clip(text: str, limit: int = _MAX_ISSUE_CHARS) -> str:
    """截断但保住句尾的指令。

    直接 `text[:limit]` 会把 "do not consult the Android source" 这类尾部指令切掉，
    而恰恰是最长的 R1/R2 文本最需要它 —— 那是唯一能阻止 Generator 去空读安卓源码
    的手段。所以尾部指令整段保留，只压缩中间的描述。
    """
    if len(text) <= limit:
        return text
    for d in (_NO_ANDROID, _SEE_ANDROID):
        if text.endswith(d):
            head_room = limit - len(d) - 5
            if head_room > 80:
                return f"{text[:head_room].rstrip()} ... {d}"
    return text[: limit - 3].rstrip() + "..."


def _subject(f: dict) -> str:
    for k in ("resource", "target", "component", "struct", "key"):
        v = f.get(k)
        if v:
            return str(v)
    return ""


_AGG_TEMPLATE: dict[str, str] = {
    "R5_string_res_blank":
        "{rel} lines {lines}: {n} $r('app.string.*') references point at string "
        "resources that do not exist ({subs}); the text renders blank. " + _SEE_ANDROID,
    "R5_string_res_throw":
        "{rel} lines {lines}: {n} getStringSync($r('app.string.*')) calls reference "
        "missing string resources ({subs}); each throws a BusinessError at runtime. "
        + _SEE_ANDROID,
    "R6_res_name_missing":
        "{rel} lines {lines}: {n} getStringByNameSync() calls reference resource names "
        "that do not exist ({subs}); each throws a BusinessError at runtime. "
        + _SEE_ANDROID,
    "R3_route_unregistered":
        "{rel} lines {lines}: {n} push() calls target routes not registered in any "
        "navDestination builder ({subs}); the taps do nothing. " + _NO_ANDROID,
    "R8_dialog_controller_local":
        "{rel} lines {lines}: {n} CustomDialogController instances are created as local "
        "variables inside component methods ({subs}). ArkUI only injects the `controller` "
        "field of a @CustomDialog when the controller is a member variable of the "
        "component, so each dialog opens but every button in it that calls "
        "this.controller.* throws on tap. Move each controller to a struct member field. "
        + _NO_ANDROID,
    "R4_dialog_consume":
        "{rel} lines {lines}: {n} @CustomDialog structs declare @Consume ({subs}). A "
        "dialog tree is detached from the page tree, so @Consume cannot reach the "
        "page-side @Provide and the dialog crashes on open. Pass the value in as a "
        "constructor parameter instead. " + _NO_ANDROID,
}


def _aggregate_issue(rule: str, group: list[dict]) -> str:
    """单条发现直接用其 issue；多条同规则合并，点名前 N 个对象并给出全部行号。"""
    if len(group) == 1:
        return str(group[0].get("issue", ""))
    tmpl = _AGG_TEMPLATE.get(rule)
    if tmpl is None:
        # 没有聚合模板的规则（R1/R2/R7）保留首条完整因果链，其余只计数
        head = str(group[0].get("issue", ""))
        return f"{head} (+{len(group) - 1} more of the same kind in this file)"
    lines = sorted({int(g["line"]) for g in group if isinstance(g.get("line"), int)})
    line_txt = ",".join(str(x) for x in lines[:_MAX_LINES_IN_ISSUE])
    if len(lines) > _MAX_LINES_IN_ISSUE:
        line_txt += f",+{len(lines) - _MAX_LINES_IN_ISSUE}"
    subs = [s for s in dict.fromkeys(_subject(g) for g in group) if s]
    sub_txt = ", ".join(subs[:_MAX_SUBJECTS_IN_ISSUE])
    if len(subs) > _MAX_SUBJECTS_IN_ISSUE:
        sub_txt += f", +{len(subs) - _MAX_SUBJECTS_IN_ISSUE} more"
    return tmpl.format(rel=group[0].get("file", "."), lines=line_txt,
                       n=len(group), subs=sub_txt)


# ══════════════════════════════════════════════════════════════════════════════
# 插件本体
# ══════════════════════════════════════════════════════════════════════════════

class CrashRiskPlugin(EntropyPlugin):
    """静态检测点击闪退风险：ArkUI 框架契约 + 资源契约 + 全局兜底。"""

    name = "crash_risk"
    default_weight = 0.10
    judge_mode = "hard"

    # ── 评分 ─────────────────────────────────────────────────────────────────
    @staticmethod
    def _score(findings: list[dict], onclick_count: int) -> float:
        # 每条规则内部次线性累加：同一个反模式重复 N 次通常是同一个根因（实测某仓
        # 把局部 controller 写了 5 遍、另一仓把同一批资源漏了 9 个），线性累加会让
        # "一个错误重复很多次"压过"多个不同类别的错误"，跨 app 比较时失真。
        # 取 1+log2(N)：第 1 次记满权，之后每翻一倍加一个单位。
        per_rule: dict[str, int] = {}
        for f in findings:
            r = str(f["rule"])
            per_rule[r] = per_rule.get(r, 0) + 1
        weighted = sum(
            _RULE_WEIGHT.get(r, 0.3) * (1.0 + math.log2(n))
            for r, n in per_rule.items()
        )
        # 分母用交互点数量而非文件数 —— 崩溃风险的自然分母是可点击面积，这样
        # 大小差 3 倍的 app 之间分数仍可比（跨 app 泛化的关键）。
        #
        # 对 ECAT 的收敛循环而言，重要的是同一个仓内 H 随修复单调下降：
        # onclick_count 在迭代间近似恒定，任何单调分母都满足这点。常数下限只影响
        # 小 app 的绝对水位 —— 取 8 表示"少于 160 个交互点的 app 按 160 个算"，
        # 避免单个发现在小 app 上直接把分数打满；早先取 20 相当于把一个 157 点的
        # app 当成 400 点，会系统性低估小 app 的缺陷密度。
        denom = max(8.0, onclick_count * 0.05)
        score = min(1.0, weighted / denom)
        # 大仓里一两个必崩点会被分母稀释到看不见，给个下限让它在 H 里留下痕迹。
        if any(str(f["rule"]) in _CERTAIN_CRASH_RULES for f in findings):
            score = max(score, 0.15)
        return round(score, 4)

    # ── WorkItem ─────────────────────────────────────────────────────────────
    def extract_work_items(
        self, score: float, details: dict[str, object]
    ) -> list[WorkItem]:
        findings = details.get("findings")
        if not isinstance(findings, list):
            return []
        by_file: dict[str, list[dict]] = {}
        for f in findings:
            if isinstance(f, dict):
                by_file.setdefault(str(f.get("file", ".")), []).append(f)

        items: list[WorkItem] = []
        for rel, group in by_file.items():
            by_rule: dict[str, list[dict]] = {}
            for g in group:
                by_rule.setdefault(str(g.get("rule")), []).append(g)
            ordered = sorted(by_rule.items(),
                             key=lambda kv: -_RULE_WEIGHT.get(kv[0], 0.0))
            issues = [_clip(_aggregate_issue(r, gs)) for r, gs in ordered]
            skills: list[str] = []
            for r, _ in ordered:
                s = _RULE_SKILL.get(r, "")
                if s and s not in skills:
                    skills.append(s)
            top_rule = ordered[0][0]
            severity = ("high" if top_rule in _HIGH_SEVERITY_RULES
                        else "medium" if _RULE_WEIGHT.get(top_rule, 0.0) >= 0.5
                        else "low")
            lines = sorted({int(g["line"]) for g in group
                            if isinstance(g.get("line"), int)})
            items.append(WorkItem(
                file=Path(rel), issues=issues, suggested_skills=skills[:2],
                severity=severity, lines=lines[:20] or None,
            ))
        order = {"high": 0, "medium": 1, "low": 2}
        items.sort(key=lambda w: order.get(w.severity, 9))
        return items[:_MAX_WORK_ITEMS]

    # ── 主流程 ───────────────────────────────────────────────────────────────
    def evaluate(
        self,
        harmony_dir: Path,
        *,
        android_dir: Path | None = None,
        spec_dir: Path | None = None,
        config: dict[str, object] | None = None,
    ) -> tuple[float, dict[str, object]]:
        files = [f for f in harmony_dir.rglob("*.ets")
                 if not should_skip_path(f, harmony_dir)]
        if not files:
            return 0.0, {"skipped": "no_ets_files"}

        live, live_mode = _live_set(harmony_dir, files)
        res_index = _string_resources(harmony_dir)

        models: dict[str, _FileModel] = {}
        for f in files:
            rel = f.relative_to(harmony_dir).as_posix()
            if live is not None and rel not in live:
                continue
            try:
                raw = f.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            models[rel] = _parse_file(rel, raw)

        if not models:
            return 0.0, {"skipped": "no_live_files", "liveness": live_mode}

        onclick_count = sum(len(fm.onclick_spans) for fm in models.values())
        structs: dict[str, _Struct] = {}
        for fm in models.values():
            for s in fm.structs:
                structs.setdefault(s.name, s)
        known = set(structs)

        findings: list[dict] = []
        if known:
            findings += self._r1_consume(models, structs, known, res_index)
            findings += self._r2_r3_navigation(models, structs, known, res_index)
            findings += self._r4_dialog_consume(models)
            findings += self._r8_dialog_controller(models, structs, res_index)
        if res_index:
            findings += self._r5_r6_resources(models, res_index)
        findings += self._r7_global_handler(models)

        # 同一 (file,line,rule) 只保留一条
        dedup: dict[tuple, dict] = {}
        for f in findings:
            dedup.setdefault((f["file"], f.get("line"), f["rule"]), f)
        findings = list(dedup.values())

        score = self._score(findings, onclick_count)

        by_rule: dict[str, int] = {}
        for f in findings:
            by_rule[str(f["rule"])] = by_rule.get(str(f["rule"]), 0) + 1
        samples: dict[str, list[dict]] = {}
        for f in sorted(findings, key=lambda x: -_RULE_WEIGHT.get(str(x["rule"]), 0.0)):
            samples.setdefault(str(f["rule"]), []).append(f)
        for k in samples:
            samples[k] = samples[k][:_MAX_FINDINGS_PER_RULE]

        return score, {
            "findings": findings,
            "by_rule": by_rule,
            "truncated_per_rule": {k: v for k, v in by_rule.items()
                                   if v > _MAX_FINDINGS_PER_RULE},
            "samples": samples,
            "onclick_count": onclick_count,
            "live_file_count": len(models),
            "total_file_count": len(files),
            "liveness": live_mode,
            "string_resource_count": len(res_index),
            "certain_crash_count": sum(
                1 for f in findings if str(f["rule"]) in _CERTAIN_CRASH_RULES
            ),
        }

    # ── R1: @Consume 解析 ────────────────────────────────────────────────────
    def _r1_consume(
        self, models: dict[str, _FileModel], structs: dict[str, _Struct],
        known: set[str], res_index: dict[str, str],
    ) -> list[dict]:
        prov_re = re.compile(r"@Provide\(\s*['\"]([^'\"]+)['\"]\s*\)|@Provide\s+(\w+)\s*:")
        cons_re = re.compile(
            r"@Consume\(\s*['\"]([^'\"]+)['\"]\s*\)\s*(\w+)|@Consume\s+(\w+)\s*:")

        provides: dict[str, set[str]] = {}
        consumes: list[tuple[str, str, int]] = []
        for s in structs.values():
            fm = models[s.rel]
            for m in prov_re.finditer(s.body):
                key = m.group(1) or m.group(2)
                if key:
                    provides.setdefault(s.name, set()).add(key)
            for m in cons_re.finditer(s.body):
                key = m.group(1) or m.group(3)
                if key:
                    consumes.append(
                        (s.name, key, _line_of(fm.src, s.body_off + m.start())))
        if not consumes:
            return []

        # 渲染图。全局 @Builder 的内容记到引用它的 struct 名下 —— builder 会被内联
        # 到调用点，祖先链等同于宿主组件（某些应用的 sharedPageMap 就是这种）。
        children: dict[str, set[str]] = {}
        for s in structs.values():
            fm = models[s.rel]
            kids = _invoked_structs(s.body, known, exclude=s.name)
            for gname, gbody in fm.global_builders.items():
                if re.search(r"(?<![\w.])" + re.escape(gname) + r"\b", s.body):
                    kids |= _invoked_structs(gbody, known, exclude=s.name)
            children[s.name] = kids

        roots = [n for n, s in structs.items() if "Entry" in s.decorators]
        if not roots:
            return []  # 找不到入口 → 无法判定祖先链，整条跳过（反假阳性）

        parents: dict[str, set[str]] = {}
        for p, kids in children.items():
            for c in kids:
                parents.setdefault(c, set()).add(p)

        out: list[dict] = []
        for cname, key, line in consumes:
            if key in provides.get(cname, ()):
                continue  # 自己既 provide 又 consume，不判
            # reach = 从入口出发、整条路径（含自身）都没有 @Provide(key) 的节点
            reach: dict[str, str | None] = {}
            dq: deque[str] = deque()
            for r in roots:
                if key not in provides.get(r, ()):
                    reach[r] = None
                    dq.append(r)
            while dq:
                cur = dq.popleft()
                for kid in children.get(cur, ()):
                    if kid in reach or key in provides.get(kid, ()):
                        continue
                    reach[kid] = cur
                    dq.append(kid)

            bad_parent = next((p for p in parents.get(cname, ()) if p in reach), None)
            if cname in roots:
                chain = [cname]                    # 自己就是入口，没有祖先可依
            elif bad_parent is not None:
                chain = [cname]
                cur2: str | None = bad_parent
                while cur2 is not None and len(chain) < _MAX_PATH_NODES:
                    chain.append(cur2)
                    cur2 = reach.get(cur2)
            else:
                continue   # 所有已知渲染路径上都有 provider，或根本不可达 → 不报

            path = " -> ".join(reversed(chain))
            owner = structs[cname]
            holders = sorted(n for n, ks in provides.items() if key in ks)
            entry = self._entry_hint(models, structs, cname, res_index)
            issue = (
                f"{owner.rel}:{line} @Consume('{key}') cannot resolve: on render path "
                f"{path} no ancestor declares @Provide('{key}')"
                + (f" (providers exist in {', '.join(holders[:3])}, none on this path)"
                   if holders else "")
                + f". {entry}ArkUI throws when @Consume finds no matching @Provide, so "
                f"reaching this component crashes the app. {_NO_ANDROID}"
            )
            out.append({"rule": "R1_consume_unresolved", "file": owner.rel,
                        "line": line, "key": key, "path": path, "issue": issue})
        return out

    def _entry_hint(
        self, models: dict[str, _FileModel], structs: dict[str, _Struct],
        target: str, res_index: dict[str, str],
    ) -> str:
        """找到能进入 target 的 push 点及其按钮，拼成 'Entry: …'。找不到返回 ''。"""
        names = {target} | {
            n for n, s in structs.items()
            if re.search(r"(?<![\w.])" + re.escape(target) + r"\s*\(", s.body)
        }
        for fm in models.values():
            for nm in names:
                m = re.search(
                    r"push(?:Path|PathByName|Destination|DestinationByName)\("
                    r"\s*\{?\s*(?:name:\s*)?['\"]" + re.escape(nm) + r"['\"]", fm.src)
                if not m:
                    continue
                label = (_onclick_label(fm, m.start(), res_index)
                         or _caller_button(fm, m.start(), res_index))
                loc = f"{fm.rel}:{_line_of(fm.src, m.start())}"
                return (f"Entry: {loc} push('{nm}')"
                        + (f" from {label}" if label else "") + ". ")
        return ""

    # ── R2 / R3: 导航契约 ────────────────────────────────────────────────────
    def _r2_r3_navigation(
        self, models: dict[str, _FileModel], structs: dict[str, _Struct],
        known: set[str], res_index: dict[str, str],
    ) -> list[dict]:
        out: list[dict] = []
        builder_bodies: list[str] = []
        registered: set[str] = set()

        for fm in models.values():
            for m in re.finditer(r"\.navDestination\(\s*(?:this\.)?(\w+)", fm.src):
                bname = m.group(1)
                body = fm.global_builders.get(bname)
                if body is None:
                    bm = re.search(r"@Builder\s+(" + re.escape(bname) + r")\s*\(", fm.src)
                    if bm:
                        paren = fm.src.index("(", bm.end(1) - 1)
                        brace = fm.src.find("{", _match_block(fm.src, paren))
                        if brace != -1:
                            body = fm.src[brace : _match_block(fm.src, brace) + 1]
                if body:
                    builder_bodies.append(body)
                    registered.update(
                        re.findall(r"name\s*===?\s*['\"](\w+)['\"]", body))

        if not builder_bodies:
            return []  # 项目没用 Navigation → R2/R3 都不适用

        # R2：builder 顶层（未被 NavDestination 包裹）的组件必须自带 NavDestination 根
        seen_comp: set[str] = set()
        for body in builder_bodies:
            wrapped: list[tuple[int, int]] = []
            for nm in re.finditer(r"(?<![\w.])NavDestination\s*\(", body):
                brace = body.find("{", nm.end())
                if brace != -1:
                    wrapped.append((brace, _match_block(body, brace)))
            for cname in _invoked_structs(body, known):
                if cname in seen_comp:
                    continue
                st = structs.get(cname)
                if st is None:
                    continue
                bare = any(
                    not any(s <= cm.start() < e for s, e in wrapped)
                    for cm in re.finditer(
                        r"(?<![\w.])" + re.escape(cname) + r"\s*\(", body)
                )
                if not bare:
                    continue          # builder 已经包了 NavDestination，合法
                root = self._build_root(st.body, structs)      # 穿透条件包裹与组件委托
                if root is None or root == "NavDestination":
                    continue
                immediate = self._build_root(st.body)          # 直接根，供报告区分两层
                seen_comp.add(cname)
                inner = re.search(r"(?<![\w.])NavDestination\s*\(", st.body)
                nline = (_line_of(models[st.rel].src, st.body_off + inner.start())
                         if inner else None)
                entry = self._entry_hint(models, structs, cname, res_index)
                delegated = ("" if immediate in (None, root)
                             else f" via {immediate}, whose own build() root is {root}")
                issue = (
                    f"{st.rel}:{st.line} {cname} is rendered directly by a "
                    f"navDestination builder but its build() root is "
                    f"{immediate or root}{delegated}, not NavDestination"
                    + (f" (NavDestination is nested at line {nline})" if nline else "")
                    + f". ArkUI requires NavDestination to be the root node of the "
                    f"destination branch, otherwise the page never appears. {entry}"
                    f"{_NO_ANDROID}"
                )
                out.append({"rule": "R2_navdest_not_root", "file": st.rel,
                            "line": st.line, "component": cname, "issue": issue})

        # R3：push 目标必须已注册
        if registered:
            for fm in models.values():
                for m in re.finditer(
                    r"(?:push|replace)(?:Path|PathByName|Destination|DestinationByName)\("
                    r"\s*\{?\s*(?:name:\s*)?['\"](\w+)['\"]", fm.src
                ):
                    tgt = m.group(1)
                    if tgt in registered:
                        continue
                    line = _line_of(fm.src, m.start())
                    label = _onclick_label(fm, m.start(), res_index)
                    issue = (
                        f"{fm.rel}:{line} push('{tgt}') targets a route not registered "
                        f"in any navDestination builder ({len(registered)} routes are "
                        f"registered)"
                        + (f", triggered from {label}" if label else "")
                        + f". The tap does nothing. {_NO_ANDROID}"
                    )
                    out.append({"rule": "R3_route_unregistered", "file": fm.rel,
                                "line": line, "target": tgt, "issue": issue})
        return out

    @staticmethod
    def _build_root(
        struct_body: str,
        structs: dict[str, _Struct] | None = None,
        _seen: frozenset[str] = frozenset(),
    ) -> str | None:
        """build() 的根组件名。

        两种必须穿透的合法形态，否则会误报：
          - `if (cond) { NavDestination() ... }` 条件包裹 → 递归进分支
          - `build() { OtherPage() }` 委托给另一个项目组件 → 递归进被委托组件
            （实测 Meshtastic 的 ThemePickerPage 就是这样转发给 ThemeConfigPage，
            后者根节点是 NavDestination，运行时完全合法）
        传 structs=None 可拿到"直接根节点"，用于在报告里区分两层。
        """
        bm = re.search(r"\bbuild\s*\(\s*\)\s*\{", struct_body)
        if not bm:
            return None
        brace = struct_body.index("{", bm.end() - 1)
        body = struct_body[brace + 1 : _match_block(struct_body, brace)]
        for _ in range(4):
            body = body.strip()
            if body.startswith("if"):
                b = body.find("{")
                if b == -1:
                    return None
                body = body[b + 1 : _match_block(body, b)]
                continue
            m = re.match(r"(\w+)\s*[({]", body)
            if not m:
                return None
            name = m.group(1)
            if structs and name in structs and name not in _seen:
                inner = CrashRiskPlugin._build_root(
                    structs[name].body, structs, _seen | {name})
                return inner or name
            return name
        return None

    # ── R4: @CustomDialog 内的 @Consume ──────────────────────────────────────
    @staticmethod
    def _r4_dialog_consume(models: dict[str, _FileModel]) -> list[dict]:
        out: list[dict] = []
        cons_re = re.compile(
            r"@Consume\(\s*['\"]([^'\"]+)['\"]\s*\)\s*(\w+)|@Consume\s+(\w+)\s*:")
        for fm in models.values():
            for s in fm.structs:
                if "CustomDialog" not in s.decorators:
                    continue
                for m in cons_re.finditer(s.body):
                    key = m.group(1) or m.group(3)
                    line = _line_of(fm.src, s.body_off + m.start())
                    issue = (
                        f"{fm.rel}:{line} @CustomDialog struct {s.name} declares "
                        f"@Consume('{key}'). A dialog's component tree is detached from "
                        f"the page tree, so @Consume cannot reach the page-side "
                        f"@Provide and the dialog crashes on open. Pass the value in as "
                        f"a constructor parameter instead. {_NO_ANDROID}"
                    )
                    out.append({"rule": "R4_dialog_consume", "file": fm.rel,
                                "line": line, "struct": s.name, "key": key,
                                "issue": issue})
        return out

    # ── R8: CustomDialogController 作用域 ────────────────────────────────────
    def _r8_dialog_controller(
        self, models: dict[str, _FileModel], structs: dict[str, _Struct],
        res_index: dict[str, str],
    ) -> list[dict]:
        """局部变量形式的 CustomDialogController → 弹窗按钮点击必崩。

        真机对照实验确认（aippt_cc_glm_0705_ecat_ds）：除 controller 作用域外
        全部同构的两条路径 —— 都是 @CustomDialog、都声明 controller 字段、都在
        按钮里调 this.controller.close()、都从 @Component 方法打开、资源都齐全 ——

          我的→联系我们   MinePage:312      const dialogController（局部）  → 闪退
          我的→关于我们→算法备案  AboutUsPage:54  this.xxxController（成员）  → 正常

        唯一自变量就是局部 vs 成员，所以判别式成立。注意命中数可能很高（实测某
        Gallery 迁移仓 39 个构造点里 37 个是局部写法），那是这批生成式迁移系统性
        用错了写法，不是误报 —— 逐个核对过构造点，解析没有偏差。

        三个条件缺一不报，每一条都是为了压掉一类假阳性：

        1. 赋值目标必须是 const/let/var。`this.x = new ...` 与字段初始化
           `private x: CustomDialogController = new ...` 都是合法的成员变量形态；
           裸赋值 `x = new ...` 归属不明（可能是先声明后赋值的成员），一律不报。
        2. 构造点必须在 @Component/@CustomDialog 的方法体内。构造在 struct 外
           （模块级函数）时 open() 当场就失败，症状是"点了没反应"而不是闪退 ——
           实测该 app 的"版本更新"就是这一类，混进来就是假阳性。
        3. 被构造的 @CustomDialog 内部必须真的用到 this.controller。字段没被注入
           但也没人碰，就不会抛。这条把"注入失败"和"实际触碰"连起来。
        """
        out: list[dict] = []
        for fm in models.values():
            # 结构体区间，用于判定构造点落在哪个 struct 里
            spans = [(s.body_off, s.body_off + len(s.body), s) for s in fm.structs]
            for m in re.finditer(
                r"\b(?:const|let|var)\s+(\w+)\s*(?::[^=;]{0,80})?=\s*"
                r"new\s+CustomDialogController\s*\(", fm.src
            ):
                var = m.group(1)
                host = next((s for lo, hi, s in spans if lo <= m.start() < hi), None)
                if host is None or not (host.decorators & {"Component", "CustomDialog"}):
                    continue                                   # 条件 2
                paren = fm.src.index("(", m.end() - 1)
                args = fm.src[paren : _match_block(fm.src, paren) + 1]
                bm = re.search(r"builder:\s*(\w+)\s*[({]", args)
                if bm is None:
                    continue
                dlg = structs.get(bm.group(1))
                if dlg is None:
                    continue
                use = re.search(r"this\.controller\s*\.\s*(\w+)", dlg.body)
                if use is None:
                    continue                                   # 条件 3
                line = _line_of(fm.src, m.start())
                dlg_line = _line_of(models[dlg.rel].src, dlg.body_off + use.start())
                method = _enclosing_method(fm, m.start())
                label = _caller_button(fm, m.start(), res_index)
                issue = (
                    f"{fm.rel}:{line} CustomDialogController is created as a local "
                    f"variable `{var}` inside {host.name}"
                    + (f".{method}()" if method else "")
                    + f". ArkUI only injects the `controller` field of a @CustomDialog "
                    f"when the controller is a member variable of the component, so "
                    f"{dlg.name}.controller stays undefined: the dialog opens, but every "
                    f"button calling this.controller.{use.group(1)}() "
                    f"({dlg.rel}:{dlg_line}) throws on tap"
                    + (f". Reached from {label}" if label else "")
                    + f". Move the controller to a member field of {host.name}. "
                    f"{_NO_ANDROID}"
                )
                out.append({
                    "rule": "R8_dialog_controller_local", "file": fm.rel, "line": line,
                    "struct": f"{host.name}.{var}", "dialog": dlg.name, "issue": issue,
                })
        return out

    # ── R5 / R6: 资源契约 ────────────────────────────────────────────────────
    @staticmethod
    def _r5_r6_resources(
        models: dict[str, _FileModel], res_index: dict[str, str]
    ) -> list[dict]:
        out: list[dict] = []
        for fm in models.values():
            for m in re.finditer(r"\$r\(\s*['\"]app\.string\.([\w.]+)['\"]", fm.src):
                nm = m.group(1)
                if nm in res_index:
                    continue
                line = _line_of(fm.src, m.start())
                # 取值位（getStringSync($r(...))）抛异常，渲染位只是空白 —— 严重度差一档
                throws = "getStringSync(" in fm.src[max(0, m.start() - 60) : m.start()]
                label = _onclick_label(fm, m.start(), res_index)
                if throws:
                    issue = (
                        f"{fm.rel}:{line} getStringSync($r('app.string.{nm}')) "
                        f"references a string resource that does not exist and throws a "
                        f"BusinessError at runtime"
                        + (f", triggered from {label}" if label else "")
                        + f". {_SEE_ANDROID}"
                    )
                    rule = "R5_string_res_throw"
                else:
                    issue = (
                        f"{fm.rel}:{line} $r('app.string.{nm}') references a string "
                        f"resource that does not exist; the text renders blank. "
                        f"{_SEE_ANDROID}"
                    )
                    rule = "R5_string_res_blank"
                out.append({"rule": rule, "file": fm.rel, "line": line,
                            "resource": nm, "issue": issue})

            for m in re.finditer(r"getStringByNameSync\(\s*['\"]([\w.]+)['\"]", fm.src):
                nm = m.group(1)
                if nm in res_index:
                    continue
                line = _line_of(fm.src, m.start())
                label = _onclick_label(fm, m.start(), res_index)
                guarded = _in_try(fm.try_spans, m.start())
                issue = (
                    f"{fm.rel}:{line} getStringByNameSync('{nm}') references a resource "
                    f"name that does not exist and throws a BusinessError at runtime"
                    + (" (wrapped in try, so the app survives but the text is lost)"
                       if guarded else " and is not wrapped in try")
                    + (f", triggered from {label}" if label else "")
                    + f". {_SEE_ANDROID}"
                )
                out.append({"rule": "R6_res_name_missing", "file": fm.rel,
                            "line": line, "resource": nm, "issue": issue})
        return out

    # ── R7: 全局兜底 ─────────────────────────────────────────────────────────
    @staticmethod
    def _r7_global_handler(models: dict[str, _FileModel]) -> list[dict]:
        # 接受面要宽：只要工程用了任何一种官方兜底机制就不报，宁可漏报不可误报。
        # 注意这些匹配跑在已剥注释的源码上 —— 实测 sonnet48 仓里唯一的
        # "errorManager.on('error')" 出现在文档注释里，不剥注释就会漏掉真实缺陷。
        accept = (
            r"errorManager\.on\s*\(", r"errorManager\.addObserver",
            r"appRecovery\.enableAppRecovery", r"appRecovery\.setRestartWant",
            r"hiAppEvent\.addWatcher", r"onUnhandledException", r"onUnhandledRejection",
        )
        for fm in models.values():
            for pat in accept:
                if re.search(pat, fm.src):
                    return []
        anchor = next((fm.rel for fm in models.values()
                       if fm.rel.endswith("EntryAbility.ets")), ".")
        issue = (
            "No global uncaught-exception observer is registered anywhere in the app: "
            "no errorManager.on('error'/'globalError'), no appRecovery, no hiAppEvent "
            "crash watcher. Every uncaught exception terminates the process with no "
            "crash report, which turns every other risk in this report from recoverable "
            "into a hard crash and leaves production crashes untraceable. Register an "
            f"observer in AbilityStage or EntryAbility.onCreate. {_NO_ANDROID}"
        )
        return [{"rule": "R7_no_global_handler", "file": anchor, "line": 1,
                 "issue": issue}]
