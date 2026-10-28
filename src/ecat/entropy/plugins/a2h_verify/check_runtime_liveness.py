"""runtime_liveness —— 运行时存活性 + 主页交互冒烟（N1，device-driven，默认关闭）。

前 12 个维度全是静态分析：能编译、像安卓、页面有实质内容——但一个迁移可以**全部
静态通过、却一启动就崩**，或**点一下按钮就报错**。N1 是唯一真实把鸿蒙 App 跑起来的检查：

  1. 冷启动 + 基础存活（由共享会话 `_device_session` 完成，与 ui_align 共用一次编译安装）：
     进程在?有崩溃日志?首屏白屏?App 自身 hilog 有运行时报错?
  2. 主页 smoke-tap（本插件，可关）：把主页上**所有能点的元素**逐个点一遍，每点后观测
     是否崩溃 / 触发运行时错误 / 跳到空白子页——抓只在交互时暴露的 bug。

只从 `_device_session`（中立设备层）取依赖，不 import ui_align —— 两插件可单独插拔。
受 dynamic_render_interval 节流、默认权重 0 opt-in；任何基础设施故障 → skip、贡献 0、
绝不误抬分；只有明确崩溃证据才判 1.0（反假阳性）。
"""

from __future__ import annotations

import re
import signal
import tempfile
import threading
import time
from pathlib import Path

from ecat.entropy.base import EntropyPlugin
from ecat.entropy.plugins.a2h_verify._device_session import (
    _CAT_LABELS,
    _CRASH_MARKERS,
    DEVICE_LOCK,
    _basename,
    _dump_hmos_ui,
    _faultlog_entries,
    _meaningful_node_count,
    _process_pid,
    find_clickables,
    get_session,
    make_sh,
    scan_runtime_errors,
)
from ecat.gradient.schema import WorkItem

# smoke-tap 每页最多点这么多可点元素（保守上限，防止异常多的页面把一轮拖太久）。
_TAP_CAP = 20
# 同页内 label 相同的兄弟元素最多探这么多个：8 张同构 'Column' 模板卡 / 一堆 'Row'
# 产品行都走同一段代码，探前几个足矣，省下 budget 给广度（避免一个列表页吃光预算）。
_LABEL_DEDUP = 2
# 一页最多向下滚动这么多屏，露出折叠线以下的元素（长菜单/列表：uitest 只 dump 屏内）。
_MAX_SCROLLS = 2
# 递归探索深度：0 = 仅主页（等价旧行为），1 = 主页 + 一级子页，2 = 再下一层。
_DEFAULT_DEPTH = 2
# 全轮总探测次数硬上限（墙钟护栏，跨所有页面累计）。
_DEFAULT_BUDGET = 60
# 点击后 settle 延时（等 UI 反应 / 崩溃暴露 / faultlog 落盘）。调小加快、过小易假阴。
_LAUNCH_SETTLE = 1.5  # 冷启动后先等进程能响应 uitest；页面加载靠 _stable_dump 轮询兜住
_TAP_SETTLE = 0.4     # 每次点击后等页面反应（从0.6s降低，大多数tap是fast-path无需长等）

# Cooperative-cancel flag for Ctrl+C. smoke-tap runs in a ThreadPoolExecutor worker
# thread, which NEVER receives KeyboardInterrupt (Python delivers SIGINT only to the
# main thread). Without this, Ctrl+C can't stop an in-flight smoke-tap: the main
# thread catches the interrupt but then blocks in pool.shutdown(wait=True) joining
# this worker, which keeps tapping for its full budget. The SIGINT handler below
# sets this event BEFORE re-raising, so the worker bails at its next loop check and
# the join completes in <1s. Installed once, from the main thread at import time.
_STOP = threading.Event()
try:
    _prev_sigint = signal.getsignal(signal.SIGINT)

    def _on_sigint(signum, frame):
        _STOP.set()
        if callable(_prev_sigint) and _prev_sigint not in (signal.SIG_DFL, signal.SIG_IGN):
            _prev_sigint(signum, frame)  # chain → default handler raises KeyboardInterrupt
        else:
            raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _on_sigint)
except (ValueError, OSError):
    pass  # not the main thread / signals unsupported — degrade to old behavior


class RuntimeLivenessPlugin(EntropyPlugin):
    """N1: cold-launch the HarmonyOS app, probe liveness, and tap the main page."""

    name = "runtime_liveness"
    default_weight = 0.0  # opt-in：需设备，默认不计权（与 ui_align 一致）

    def extract_work_items(
        self, score: float, details: dict[str, object]
    ) -> list[WorkItem]:
        issues = list(details.get("issues", []))
        if not issues:
            return []
        # Map faultlog basename -> full device path so each crash issue can carry
        # the exact hdc command to read its OWN stack, inlined into the same issue
        # line (not a separate issue) — the generator prompt only keeps issues[:3]
        # per work item, so extra lines would be truncated away.
        faultlogs = details.get("crash_faultlogs") or []
        hdc, tgt = details.get("hdc_exe"), details.get("hmos_target")
        by_name: dict[str, str] = {}
        if faultlogs and hdc and tgt:
            for p in faultlogs:
                by_name[str(p).rsplit("/", 1)[-1]] = str(p)

        lines: list[str] = []
        for i in issues:
            line = f"runtime_liveness: {i}"
            for name, path in by_name.items():
                if name in i:  # this issue names a faultlog we can read
                    # MSYS_NO_PATHCONV=1: the Generator's Bash is Git Bash, which
                    # would otherwise rewrite the leading /data/... device path to
                    # a Windows path (D:\...\Git\data\...) and hdc would 404.
                    line += (f' — READ the real crash stack first: '
                             f'MSYS_NO_PATHCONV=1 "{hdc}" -t {tgt} shell cat {path}')
                    break
            lines.append(line)
        return [WorkItem(
            file=Path("."),
            issues=lines,
            suggested_skills=[],
            severity="high",
        )]

    def evaluate(
        self,
        harmony_dir: Path,
        *,
        android_dir: Path | None = None,
        spec_dir: Path | None = None,
        config: dict[str, object] | None = None,
    ) -> tuple[float, dict[str, object]]:
        try:
            return self._evaluate(harmony_dir, config or {})
        except Exception as exc:  # 任何基础设施故障都不许阻塞循环
            return 0.0, {"skipped": "infra_error", "error": f"{type(exc).__name__}: {exc}"}

    def _evaluate(
        self, harmony_dir: Path, config: dict[str, object]
    ) -> tuple[float, dict[str, object]]:
        # 0. 节奏门：iter 0 + 每第 N 轮才跑（启动真机较重），其余轮跳过。
        iteration = int(config.get("iteration", 0) or 0)
        interval = int(config.get("dynamic_render_interval", 5) or 5)
        if iteration > 0 and (interval < 1 or iteration % interval != 0):
            return 0.0, {"skipped": "cadence", "iteration": iteration, "interval": interval}

        deveco = config.get("deveco_studio_path")
        session = get_session(
            harmony_dir, deveco_path=str(deveco) if deveco else None, iteration=iteration,
        )
        if not session.ok:
            return 0.0, {"skipped": session.skipped, **session.details}

        status = session.status
        score = session.liveness_score
        issues = list(session.issues)
        details: dict[str, object] = {"status": status, **session.details}

        # 1. smoke-tap（可关）。只有 App 存活+渲染了主页才点；崩溃/白屏时无从点起。
        #    默认递归探索到 depth 层子页，每次探测仍从干净重启重放路径到达（可归因）。
        if config.get("liveness_smoke_tap", True) and session.main_page_ok:
            with DEVICE_LOCK:  # 独占设备，与 ui_align 截图串行
                taps = _smoke_tap(
                    session.device_cfg,
                    depth=int(config.get("liveness_smoke_tap_depth", _DEFAULT_DEPTH) or 0),
                    per_page_cap=int(config.get("liveness_smoke_tap_cap", _TAP_CAP) or _TAP_CAP),
                    budget=int(config.get("liveness_smoke_tap_budget", _DEFAULT_BUDGET) or _DEFAULT_BUDGET),
                )
            details["tapped"] = len(taps)
            details["tap_problems"] = [t for t in taps if t["outcome"] != "clean"]
            details["tap_results"] = taps  # full list for debugging
            status, score, issues = _merge_tap(status, score, issues, taps)
            details["status"] = status

        details["issues"] = issues

        # When the app crashed, pull + distill the faultlog HERE — we already
        # hold the resolved hdc + target, so the discriminator gets the ArkTS
        # crash stack (and the culprit .ets files) handed to it in details
        # instead of hunting for hdc and tracing the import graph itself.
        if status == "crashed":
            excerpt = _pull_crash_excerpt(session.device_cfg, details.get("new_faultlogs"))
            if excerpt:
                details["crash_excerpt"] = excerpt

        # Collect every faultlog device path (launch + tap) plus the resolved
        # hdc/target, so extract_work_items can hand the Generator the exact
        # `hdc … shell cat <path>` command to read the real crash stacks itself
        # — same way the discriminator does. No extra device I/O here.
        all_faultlogs: list[str] = []
        if details.get("new_faultlogs"):
            all_faultlogs.extend(str(p) for p in details["new_faultlogs"])
        for t in details.get("tap_problems", []):
            if isinstance(t, dict) and t.get("faultlogs"):
                all_faultlogs.extend(str(p) for p in t["faultlogs"])
        if all_faultlogs:
            details["crash_faultlogs"] = list(dict.fromkeys(all_faultlogs))  # dedup, keep order
            details["hdc_exe"] = session.device_cfg.get("hdc_exe")
            details["hmos_target"] = session.device_cfg.get("hmos_target")

        return score, details


# ── crash faultlog distillation ───────────────────────────────────────────────

# Match `foo/bar/Baz.ets` (and .ts) references anywhere in a stack frame.
_ETS_REF_RE = re.compile(r"[\w./-]+\.(?:ets|ts)\b")


def _pull_crash_excerpt(
    device_cfg: dict, faultlogs: object, *, max_lines: int = 40
) -> dict | None:
    """Best-effort: `cat` the newest crash faultlog via the session's already
    resolved hdc and distill it to (a) the header lines (Reason / Error message /
    top of stack) and (b) the distinct `.ets`/`.ts` files named in the stack —
    the crash culprits. Returns None on any failure so the caller falls back to
    the unenriched details (never blocks the loop)."""
    if not isinstance(faultlogs, (list, tuple)) or not faultlogs:
        return None
    path = str(faultlogs[0])
    try:
        sh = make_sh(device_cfg)
        content = sh(["cat", path], timeout=20)
    except Exception:
        return None
    if not content or not content.strip():
        return None

    lines = [ln.rstrip() for ln in content.splitlines()]
    head = [ln[:300] for ln in lines if ln.strip()][:max_lines]
    ets_refs: list[str] = []
    seen: set[str] = set()
    for m in _ETS_REF_RE.finditer(content):
        ref = m.group(0)
        if ref not in seen:
            seen.add(ref)
            ets_refs.append(ref)
        if len(ets_refs) >= 10:
            break
    return {
        "faultlog": path,
        "head": "\n".join(head),
        "ets_refs": ets_refs,
    }


# ── main-page + subpage smoke-tap ─────────────────────────────────────────────


def _page_sig(dump_json: str | None) -> str:
    """Order-insensitive page fingerprint = its set of clickable labels, with
    VOLATILE labels dropped (anything containing a digit — clocks like '07:27',
    counters like '0/10000'). Otherwise a ticking clock changes the signature
    every second and on-demand / back-recovery think the page changed on every
    probe → relaunch storm. Used to dedup pages and verify replayed navigation."""
    stable = (c["label"] for c in find_clickables(dump_json)
              if not any(ch.isdigit() for ch in c["label"]))
    return "|".join(sorted(stable))


def _same_page(sig_a: str, sig_b: str, thresh: float = 0.35) -> bool:
    """Fuzzy page-identity test via Jaccard overlap of clickable-label sets.
    Exact equality is too strict: opening the soft keyboard, a focus highlight,
    or a Back that leaves minor residue changes a few labels but it's still the
    same page — with exact match those all read as 'navigated' and force a
    relaunch. Real navigations overlap little (≈0.15–0.31 measured); the same page
    with cosmetic churn overlaps a lot, so 0.35 is more forgiving to avoid
    unnecessary relaunches while still detecting real navigation."""
    a = set(sig_a.split("|")) - {""}
    b = set(sig_b.split("|")) - {""}
    if not a and not b:
        return True
    return len(a & b) / max(1, len(a | b)) >= thresh


def _scroll_down(sh) -> None:
    """Swipe up to scroll the page content DOWN, revealing below-the-fold items."""
    sh(["uitest", "uiInput", "swipe", "636", "1900", "636", "900", "600"], 10)
    time.sleep(_TAP_SETTLE)


def _locate(dump_json: str | None, label: str) -> tuple[int, int] | None:
    """Find a clickable by label in a dump (coords drift after scrolling, so a
    below-the-fold item is re-located by label rather than trusting old coords)."""
    for c in find_clickables(dump_json):
        if c["label"] == label:
            return c["cx"], c["cy"]
    return None


def _collect_scrolled(sh, device_cfg: dict, scratch: Path, first_dump: str | None) -> list[tuple[dict, int]]:
    """From the page top (first_dump in hand), collect clickables across up to
    _MAX_SCROLLS downward scrolls, each tagged with the scroll depth needed to
    reveal it. Stops early when a scroll surfaces nothing new (bottom reached).
    Leaves the page scrolled — the caller must reset position (cur = None)."""
    items: list[tuple[dict, int]] = []
    seen_labels: set[str] = set()
    dump = first_dump
    for sd in range(_MAX_SCROLLS + 1):
        added = 0
        for c in find_clickables(dump):
            label = c["label"]
            # Skip VOLATILE labels (containing digits — clocks, counters, timestamps).
            # Otherwise a ticking clock changes every second and we click it repeatedly.
            if any(ch.isdigit() for ch in label):
                continue
            if label not in seen_labels:
                seen_labels.add(label)
                items.append((c, sd))
                added += 1
        if sd == _MAX_SCROLLS or (sd > 0 and added == 0):
            break
        _scroll_down(sh)
        dump = _stable_dump(device_cfg, scratch)
    return items


def _stable_dump(device_cfg: dict, scratch: Path, *, tries: int = 4, gap: float = 0.3) -> str | None:
    """Dump the UI repeatedly until two consecutive dumps share the same page
    signature (page finished loading / animating) or `tries` is exhausted.

    Absorbs the cold-launch async-load window (a page may show a 1-element splash
    for ~2s before its real content renders) so the page signature is reliable —
    without betting on a fixed settle constant. Returns the last dump. Fast pages
    return after 2 dumps; a dead/blank app also converges immediately (empty sig).
    Reduced tries (8→4) and gap (0.5→0.3) to speed up testing while still catching
    most async loads."""
    prev = _dump_hmos_ui(device_cfg, scratch)
    prev_sig = _page_sig(prev)
    for _ in range(tries - 1):
        time.sleep(gap)
        cur = _dump_hmos_ui(device_cfg, scratch)
        cur_sig = _page_sig(cur)
        if cur_sig == prev_sig:
            return cur
        prev, prev_sig = cur, cur_sig
    return prev


def _label_chain(path: list[dict], c: dict) -> str:
    """Human-readable click path, e.g. ``'Mine' → 'Settings'`` — goes into the
    issue text so a crash is attributable to the exact route that triggered it."""
    labels = [p["label"] for p in path] + [c["label"]]
    return " → ".join(f"'{lbl}'" for lbl in labels)


def _issue_text(outcome: str, chain: str, core: str) -> str | None:
    if outcome == "crash":
        return f"tapping {chain} crashed the app: {core}"
    if outcome == "runtime_error":
        return f"tapping {chain} triggered {core}"
    if outcome == "subpage_white":
        return f"tapping {chain} led to a {core}"
    return None  # clean


def _observe_tap(sh, device_cfg: dict, scratch: Path, bundle: str, short: str,
                 before: set[str], page_sig: str) -> tuple[str, str, list[str], str | None]:
    """Observe device state after a tap has been issued + settled.

    Returns (outcome, core_msg, new_faults, last_dump). ``last_dump`` is the UI
    dump used for the same-page / white-screen decision — reused by the caller to
    descend; None for crash/runtime_error (app not inspectable). ``page_sig`` is
    the signature of the page we tapped ON: if the post-tap page still matches it,
    the tap didn't navigate and we skip the (slow) multi-dump stabilization —
    that fast path is what keeps the per-click delay low on the common stay-taps."""
    new_faults = sorted(_faultlog_entries(sh, bundle) - before)
    pid = _process_pid(sh, bundle)
    alive = bool(pid)
    hilog = sh(["hilog", "-x"], 15)
    markers = [m for m in _CRASH_MARKERS if m in hilog] if (bundle in hilog or short in hilog) else []
    if new_faults or (not alive and markers):
        ev = _basename(new_faults[0]) if new_faults else ", ".join(markers[:2])
        return "crash", ev, new_faults, None
    rerrs = scan_runtime_errors(hilog, pid, short) if alive else {}
    if rerrs:
        cat = next(iter(rerrs))
        return "runtime_error", f"{_CAT_LABELS.get(cat, cat)}: {rerrs[cat][0][:140]}", new_faults, None
    # Fast path: ONE dump. If the page still has content and matches the page we
    # tapped on, the tap didn't navigate — no need to wait for stability.
    dump = _dump_hmos_ui(device_cfg, scratch)
    if _meaningful_node_count(dump) > 0 and _same_page(_page_sig(dump), page_sig):
        return "clean", "", new_faults, dump
    # Changed or looks blank — a real navigation or still mid-load. Only this
    # uncommon case pays for the multi-dump stabilization.
    dump = _stable_dump(device_cfg, scratch)
    if _meaningful_node_count(dump) == 0:
        return "subpage_white", "blank page (subpage white screen)", new_faults, dump
    return "clean", "", new_faults, dump


def _smoke_tap(
    device_cfg: dict,
    *,
    depth: int = _DEFAULT_DEPTH,
    per_page_cap: int = _TAP_CAP,
    budget: int = _DEFAULT_BUDGET,
) -> list[dict]:
    """Relaunch → replay a tap-path from a fresh launch → tap one target → observe.

    Explores up to ``depth`` taps deep. Relaunch is ON-DEMAND: the app is kept
    running and buttons on the same page are tapped back-to-back WITHOUT restart
    for speed. A relaunch (+ replay of the ancestor path) happens only when the
    device is no longer cleanly sitting on the page we need to tap next, i.e.
    after the previous tap: crashed the app, raised a runtime error, whitescreened,
    or navigated to a different page (we must return to test the siblings).

    hilog is still cleared and faultlogs snapshotted immediately before each tap,
    so a crash/faultlog is still attributable to THAT tap — only the accumulated
    UI state (harmless toggles) differs from a full-relaunch-per-tap run. Pages
    are de-duped by clickable-label signature; a replayed path that doesn't land
    on the expected page is skipped (guards misattribution). ``budget`` caps total
    probes for wall-clock."""
    sh = make_sh(device_cfg)
    bundle = device_cfg["hmos_bundle"]
    ability = device_cfg.get("hmos_ability") or "EntryAbility"
    short = bundle.split(".")[-1]
    scratch = Path(tempfile.gettempdir())

    def relaunch() -> None:
        sh(["aa", "force-stop", bundle], 10)
        sh(["aa", "start", "-b", bundle, "-a", ability], 15)
        time.sleep(_LAUNCH_SETTLE)
        _stable_dump(device_cfg, scratch)  # wait until the main page finishes
        #   loading BEFORE anyone taps at its coordinates (a splash still shows at
        #   _LAUNCH_SETTLE; tapping then would hit empty space / the wrong element)

    def replay(path: list[dict]) -> None:
        for step in path:
            sh(["uitest", "uiInput", "click", str(step["cx"]), str(step["cy"])], 10)
            time.sleep(_TAP_SETTLE)
            _stable_dump(device_cfg, scratch)  # let the navigated page settle before
            #                                    the next replay tap lands

    results: list[dict] = []
    seen_pages: set[str] = set()   # pages already enumerated
    queued_sigs: set[str] = set()  # pages already scheduled — avoid dup relaunches
    # Buttons already probed anywhere, keyed by (label, coarse x, coarse y).
    # Persistent chrome — the bottom tab bar, status bar, a floating button —
    # appears on every page at the same spot; without this it gets re-probed once
    # per page. A same-label button at a different position is still probed.
    seen_buttons: set[tuple[str, int, int]] = set()
    probes = 0
    # Device starts on the main page (the session already cold-launched it and
    # confirmed main_page_ok), so cur == [] up front — the first root enumeration
    # needs no relaunch. None → not cleanly on any page, must relaunch.
    cur: list[dict] | None = []

    def ensure_at(path: list[dict]) -> None:
        """Make the device sit on `path`, healthy. ON-DEMAND: relaunch + replay
        only when it isn't already cleanly there. Returning to the main page also
        needs a relaunch once we have navigated away (replay of [] is a no-op, but
        the force-stop/start is what brings us back)."""
        nonlocal cur
        if cur != path:
            relaunch()
            replay(path)
            cur = path

    # BFS over tap-paths. Each queue entry is (path, expected_sig) whose
    # DESTINATION page we enumerate + probe. Empty path == the main page; its
    # expected_sig is None (we take whatever the current main page is).
    queue: list[tuple[list[dict], str | None]] = [([], None)]
    while queue and probes < budget:
        if _STOP.is_set():
            break  # Ctrl+C — bail so the pool join returns immediately
        path, expect_sig = queue.pop(0)

        # 1. Reach this page + dump it to enumerate its clickables. ensure_at
        #    already left the page loaded (relaunch/replay stabilize), so a plain
        #    dump is enough here — no need to stabilize again.
        ensure_at(path)
        dump = _dump_hmos_ui(device_cfg, scratch)
        sig = _page_sig(dump)
        # Replay didn't land where we expected (async/non-deterministic page) —
        # skip rather than probe the wrong page and misattribute a crash.
        if expect_sig is not None and not _same_page(sig, expect_sig):
            cur = None
            continue
        if sig in seen_pages:
            continue
        seen_pages.add(sig)

        # Enumerate across scroll positions so below-the-fold items (long menus /
        # lists — uitest only dumps what's on screen) are reached too. Collection
        # scrolls the page away from the top, so reset cur.
        items = _collect_scrolled(sh, device_cfg, scratch, dump)
        cur = None

        # Probe, skipping near-duplicate siblings: at most _LABEL_DEDUP taps share
        # one label. per_page_cap bounds probes per page (applied AFTER dedup so
        # duplicates don't crowd out distinct buttons).
        label_seen: dict[str, int] = {}
        page_probes = 0
        for c, sd in items:
            if probes >= budget or page_probes >= per_page_cap or _STOP.is_set():
                break
            seen_n = label_seen.get(c["label"], 0)
            if seen_n >= _LABEL_DEDUP:
                continue  # already probed enough same-label siblings on this page
            # 48px grid: same label + coords within ~50px → same button (avoid repeats).
            # Coarser than old 8px to catch scrolled-slightly-different positions.
            gkey = (c["label"], c["cx"] // 48, c["cy"] // 48)
            if gkey in seen_buttons:
                continue  # this exact button was already probed on another page
            seen_buttons.add(gkey)
            label_seen[c["label"]] = seen_n + 1
            page_probes += 1
            probes += 1
            # 2. Probe. ON-DEMAND: ensure_at skips relaunch when we're already on
            #    this page's top. For a below-the-fold item, scroll down to it and
            #    re-locate by label (coords drift), then it's a one-shot probe.
            ensure_at(path)
            cx, cy = c["cx"], c["cy"]
            if sd > 0:
                for _ in range(sd):
                    _scroll_down(sh)
                loc = _locate(_stable_dump(device_cfg, scratch), c["label"])
                cur = None
                if loc is None:
                    continue  # couldn't re-find after scrolling
                cx, cy = loc
            sh(["hilog", "-r"], 8)  # clear buffer so new lines belong to this tap
            before = _faultlog_entries(sh, bundle)
            sh(["uitest", "uiInput", "click", str(cx), str(cy)], 10)
            time.sleep(_TAP_SETTLE)

            outcome, core, new_faults, last_dump = _observe_tap(
                sh, device_cfg, scratch, bundle, short, before, sig)
            chain = _label_chain(path, c)
            results.append({"label": c["label"], "cx": cx, "cy": cy,
                            "outcome": outcome,
                            "issue": _issue_text(outcome, chain, core),
                            "faultlogs": new_faults})

            # 3. Update device position + maybe descend. Only on-screen (sd == 0)
            #    clean taps use on-demand stay / Back-recovery / descent — a
            #    below-the-fold tap needs scrolling that the replay model can't
            #    reproduce, so it is one-shot: cur = None, no descent.
            if sd > 0 or outcome != "clean":
                cur = None
            else:
                csig = _page_sig(last_dump) if last_dump is not None else ""
                if _same_page(csig, sig):
                    cur = path  # same page (incl. keyboard/focus churn) — no Back
                else:
                    if (len(path) < depth and csig and csig not in seen_pages
                            and csig not in queued_sigs
                            and last_dump is not None and _meaningful_node_count(last_dump) > 0):
                        queued_sigs.add(csig)
                        queue.append((path + [{"cx": cx, "cy": cy,
                                               "label": c["label"]}], csig))
                    sh(["uitest", "uiInput", "keyEvent", "Back"], 10)
                    time.sleep(_TAP_SETTLE)
                    # Trust the return only if Back landed us back on this page
                    # (fuzzy); otherwise fall back to a relaunch next probe.
                    cur = path if _same_page(_page_sig(_stable_dump(device_cfg, scratch)), sig) else None
    return results


def _merge_tap(
    status: str, score: float, issues: list[str], taps: list[dict]
) -> tuple[str, float, list[str]]:
    """Fold tap findings into the launch-time verdict. A tap that crashes the app is
    serious (1.0); softer tap problems lift an otherwise-healthy app to runtime_errors."""
    crash = [t for t in taps if t["outcome"] == "crash"]
    soft = [t for t in taps if t["outcome"] in ("runtime_error", "subpage_white")]
    if crash:
        return "crashed", 1.0, [t["issue"] for t in crash] + issues
    if soft:
        new_status = "runtime_errors" if status == "alive_ok" else status
        return new_status, max(score, 0.3), issues + [t["issue"] for t in soft]
    return status, score, issues
