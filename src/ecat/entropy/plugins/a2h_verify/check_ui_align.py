"""ui_align — UI 视觉对齐检查（CHECK-7，device-driven agentic judge）。

与旧版"读 spec markdown 比 .ets 结构"不同，本版在判别器内**真实驱动两端模拟器**
做视觉对齐检测：

  硬检查（evaluate，快、ThreadPool）:
    1. 解析设备（env 优先，否则 adb devices / hdc list targets 自动探测）。
       任一端缺设备 → 返回 (0.0, {"skipped":"no_device"})，对 H 贡献 0、不产 WorkItem。
    2. 解析 android_pkg / hmos_bundle（fact-tree.app / AndroidManifest / AppScope/app.json5）。
    3. 取 fact-tree：优先用本次 run 的缓存或已有的 spec/toolkit-fact-tree.json，都没有时
       从 AndroidManifest 原生生成一份最小 fact-tree；仍拿不到 → (0.0, {"skipped":"no_facttree"})。
    4. rotating cursor 选 ≤8 页子集：变更页（.ets 自上次检测后改动）优先，其余按游标顺序
       补足，游标前移、绕回 → 跨多轮累积全量覆盖。
    5. 把 device_cfg + subset + 脚本路径写到 .ecat/visual_cache/dispatch.json（旁路文件，
       details 只带其绝对路径，规避判别器 1500 字符截断）。

  截图在插件内完成（Python）：重编译安装鸿蒙 App，两端截图拼成左右对比图。首页启动即达；
  其他页面需要 arkts-visual-verify 的导航脚本（本仓库未附带），缺脚本时只对比首页。

  agentic judge（visual-judge 子代理）:
    读 dispatch.json 和对比图 → 多模态比对 → 输出 {"score", "issues":["pages/X.ets: <gap>", ...]}。
    issues 经 extractors 自动转 WorkItem，不附带推荐 skill。

无设备 / 无 fact-tree / 任何基础设施故障 → 干净 skip，**绝不阻塞 entropy 循环**。
默认 entropy.weights.ui_align = 0.0（opt-in）。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from ecat.entropy.base import EntropyPlugin, should_skip_path
from ecat.entropy.plugins.a2h_verify._device_session import (
    _device_env,
    _dump_hmos_ui,
    _launch_and_dismiss,
    _resolve_hdc_exe,
    _resolve_hmos_ability,
    _resolve_hmos_bundle,
    _resolve_hmos_target,
    _run,
)
from ecat.gradient.schema import WorkItem

# 每轮检测的页面子集上限（一个 visual-judge 上下文装得下的量）。
_SUBSET_CAP = 8

# ECAT 内部缓存目录（cursor / page_queue / dispatch / fact-tree 旁路文件）。相对 ECAT
# 进程 cwd（cli.py os.chdir 到 workspace 根）。属于 ECAT 基础设施，非 fix 产物。
# 兜底缓存目录（仅当 runs_dir 缺失，如单独调插件测试时用）。正常 ecat run 下所有产物
# 落本次 run 文件夹：跨轮通用 → {run}/visual_cache/，每轮不同 → {run}/{iter}/ui_align/。
_VISUAL_CACHE = Path(".ecat/visual_cache")

# fact-tree 旧式候选位置（兼容已存在的手工预生成）。
_FACTREE_CANDIDATES = (
    "spec/toolkit-fact-tree.json",
)


class UiAlignPlugin(EntropyPlugin):
    """Drive both emulators to detect HarmonyOS↔Android UI alignment gaps."""

    name = "ui_align"
    default_weight = 0.0  # opt-in：需设备 + fact-tree，默认不计权
    judge_mode = "agentic"
    judge_subagent_type = "visual-judge"  # 需 Bash 驱动 adb/hdc（见 base.py）
    hard_weight = 0.0  # ui_align 熵分 = judge 视觉分（截图一致=0，差异越大越接近1）；硬检查只截图不打分
    # 不推荐 ECAT skill：UI 对齐修复走 visual-fixer 子代理（生成器派发，读对比图+安卓源改码）。
    default_fix_skill = ""

    judge_prompt = """\
Score the **ui_align** dimension from pre-captured screenshots. The plugin already
drove the emulators and saved the images — you do NOT touch devices.

Read the dispatch JSON at the EXACT `details.dispatch_file` path — never fall back to
any other dispatch.json (stale runs). If absent/missing, output
{"score": 0.0, "rationale": "no dispatch file", "issues": []} and stop.

`subset` lists pages, each with `ets_path` + `compare_image` (Android-left /
HarmonyOS-right, or null). List the folder holding the compare images and Read EVERY
compare image in it (page, menu, dialog, and any kinds added later) — not just the
per-page one; score against all. Also Read every image in `android_screenshots_dir`
(the full Android target).

Score the HarmonyOS UI against the WHOLE Android target: any element present in an
Android screenshot but missing, misplaced, or differently styled on HarmonyOS is a gap
(layout, toolbar/tabs, menu/popup items, components, colors, spacing, icons, or
HarmonyOS content overlapping/clipped by the status bar/notch). Do NOT penalize the
color/tint of system-rendered chrome (system menu/dialog popup background, status bar) —
the app can't restyle those; DO still flag layout it controls there (item spacing/
padding, order, count, missing items). If no images, score 0.0.
For a logo/icon/image-asset gap, add the hint: locate the real asset in the Android
repo (explore its resources/assets wherever it lives) and copy it into
`resources/base/media/` — don't fabricate.

Output ONLY: {"score": <0..1>, "rationale": "...", "issues": ["pages/Foo.ets: <gap> | compare:<image path>"]}.
score 0.0 = identical, 1.0 = severe divergence. Every issue MUST start with the page's
.ets path and end with " | compare:<image path>" (use the Android image you compared).
Do not modify code.
"""

    # ── extraction ────────────────────────────────────────────────────────────

    def extract_work_items(
        self, score: float, details: dict[str, object]
    ) -> list[WorkItem]:
        """Pages with no resolvable .ets → high-severity 'page missing' WorkItems.

        Visual gaps surfaced by the judge are turned into WorkItems by
        ecat.gradient.extractors._agentic_verdict_items. We do NOT attach any
        suggested_skills — UI-alignment fixing is handled by visual-verify's
        visual-fixer flow, which self-routes skills internally (visual-fixer.md
        §3.0); recommending a single ECAT skill here would mis-route most issues.
        """
        items: list[WorkItem] = []
        for entry in details.get("subset", []):
            if not isinstance(entry, dict) or entry.get("ets_path"):
                continue
            page = str(entry.get("page", "?"))
            items.append(WorkItem(
                file=Path("."),
                issues=[f"ui_align: no HarmonyOS .ets found for page '{page}'"],
                suggested_skills=[],
                severity="high",
            ))
        return items

    # ── evaluate ──────────────────────────────────────────────────────────────

    def evaluate(
        self,
        harmony_dir: Path,
        *,
        android_dir: Path | None = None,
        spec_dir: Path | None = None,
        config: dict[str, object] | None = None,
    ) -> tuple[float, dict[str, object]]:
        try:
            return self._evaluate(harmony_dir, android_dir, spec_dir, config or {})
        except Exception as exc:  # 任何基础设施故障都不许阻塞循环
            return 0.0, {"skipped": "infra_error", "error": f"{type(exc).__name__}: {exc}"}

    def _evaluate(
        self,
        harmony_dir: Path,
        android_dir: Path | None,
        spec_dir: Path | None,
        config: dict[str, object],
    ) -> tuple[float, dict[str, object]]:
        # 0. 运行节奏门：iter 0 + 每第 N 轮才跑 ui_align（重编译+截图+判分），其余轮跳过（贡献 0）。
        iteration = int(config.get("iteration", 0) or 0)
        interval = int(config.get("dynamic_render_interval", 5) or 5)
        if iteration > 0 and (interval < 1 or iteration % interval != 0):
            return 0.0, {"skipped": "cadence", "iteration": iteration, "interval": interval}

        # 0.1 落盘位置：所有 ui_align 产物进本次 run 文件夹。
        #   跨轮通用（fact-tree / page_queue / cursor / bootstrap）→ {run}/visual_cache/
        #   每轮不同（dispatch.json / 截图）→ {run}/{iter}/ui_align/
        # runs_dir 缺失（单独调插件测试）时退回 .ecat/visual_cache。
        runs_dir = config.get("runs_dir")
        if runs_dir:
            shared_dir = (Path(str(runs_dir)) / "visual_cache").resolve()
            iter_dir = (Path(str(runs_dir)) / str(iteration) / "ui_align").resolve()
        else:
            shared_dir = _VISUAL_CACHE.resolve()
            iter_dir = (_VISUAL_CACHE / str(iteration)).resolve()
        shared_dir.mkdir(parents=True, exist_ok=True)
        iter_dir.mkdir(parents=True, exist_ok=True)

        # 1. 设备解析 —— adb/hdc 不一定在 PATH（Windows 常见），按 env/config/已知 SDK 位置解析
        deveco = config.get("deveco_studio_path") if isinstance(config, dict) else None
        adb_exe = _resolve_adb_exe()
        hdc_exe = _resolve_hdc_exe(str(deveco) if deveco else None)
        if not adb_exe or not hdc_exe:
            return 0.0, {
                "skipped": "no_device",
                "reason": f"adb_exe={adb_exe!r}, hdc_exe={hdc_exe!r} (not found on PATH/SDK)",
            }
        android_serial = _resolve_android_serial(adb_exe)
        hmos_target = _resolve_hmos_target(hdc_exe)
        if not android_serial or not hmos_target:
            return 0.0, {
                "skipped": "no_device",
                "reason": f"android_serial={android_serial!r}, hmos_target={hmos_target!r} "
                          "(emulator offline / not booted?)",
            }

        # 2. fact-tree（real-click 输入）—— 缺则自动 bootstrap 到 {run}/visual_cache（跨轮通用）
        fact_tree = _locate_fact_tree(harmony_dir, spec_dir, android_dir, shared_dir)
        if fact_tree is None:
            return 0.0, {
                "skipped": "no_facttree",
                "insight": (
                    "ui_align could not obtain a fact-tree: no cached/pre-generated tree, "
                    "and native generation failed (AndroidManifest.xml not found under "
                    "paths.android, or it declares no activities). Check paths.android."
                ),
            }
        try:
            tree = json.loads(fact_tree.read_text(encoding="utf-8"))
        except Exception:
            return 0.0, {"skipped": "no_facttree", "reason": f"unreadable: {fact_tree}"}

        # 3. pkg / bundle / ability
        android_pkg = _resolve_android_pkg(tree, android_dir)
        # Reconcile the build.gradle package with what's installed on the device —
        # the namespace often differs from the real applicationId (flavor/debug
        # suffix). No-op when they match (the common case). Cross-app generic.
        _app = tree.get("app") or {}
        _launcher_fq = next((p.get("android_fq") for p in (tree.get("pages") or [])
                             if p.get("id") == _app.get("launcher_short")), None)
        android_pkg = _installed_pkg(adb_exe, android_serial, android_pkg, _launcher_fq)
        hmos_bundle = _resolve_hmos_bundle(tree, harmony_dir)
        hmos_ability = _resolve_hmos_ability(tree, harmony_dir)
        if not hmos_bundle:
            return 0.0, {"skipped": "no_bundle", "reason": "cannot resolve HarmonyOS bundleName"}

        # 4. page_queue（跨轮通用）+ rotating cursor（跨轮通用）选子集
        script_dir = _resolve_script_dir()
        queue = _build_page_queue(script_dir, fact_tree, harmony_dir, shared_dir)
        if not queue:
            return 0.0, {"skipped": "empty_queue", "reason": "build_page_queue produced no entries"}

        subset = _select_subset(queue, harmony_dir, shared_dir)
        if not subset:
            return 0.0, {"skipped": "empty_subset"}

        # 5. 设备配置
        device_cfg = {
            "android_serial": android_serial,
            "hmos_target": hmos_target,
            "android_pkg": android_pkg,
            "hmos_bundle": hmos_bundle,
            "hmos_ability": hmos_ability,
            "adb_exe": adb_exe,
            "hdc_exe": hdc_exe,
        }

        # 6. 共享设备会话：重编译+安装当前鸿蒙源码（每轮只做一次，与 runtime_liveness
        #    共用，绝不重复编译）+ 冷启动 + 存活探测。App 启动即崩/闪退/白屏 → 截图无意义，
        #    直接 skip（崩溃已由 runtime_liveness 扣分，此处不重复）。
        from ecat.entropy.plugins.a2h_verify._device_session import DEVICE_LOCK, get_session
        session = get_session(harmony_dir, deveco_path=str(deveco) if deveco else None,
                              iteration=iteration, device_cfg=device_cfg)
        if not session.main_page_ok:
            return 0.0, {"skipped": "app_not_alive", "liveness_status": session.status,
                         "insight": ("ui_align skipped — App 未能存活到主页"
                                     f"（{session.status}），详见 runtime_liveness 维度")}

        # 7. 在 Python 里截图（Windows 子代理无 Bash，截图必须在插件内做）。
        #    HMOS 每轮重截（每轮重编译）→ 存本轮 ui_align 目录；Android 每轮不变 →
        #    只截一次缓存到 run 级 visual_cache，后续轮复用。judge 之后只 Read 对比图打分。
        #    持设备锁：与 runtime_liveness 的点击串行，绝不同时驱动同一台模拟器。
        shots_dir = iter_dir / "screenshots"
        with DEVICE_LOCK:
            captured = _capture_subset_sbs(subset, device_cfg, fact_tree, script_dir,
                                           harmony_dir, shots_dir, shared_dir)

        # 截图全失败 → 没有视觉证据 → 贡献 0（绝不因失败抬高分数）
        if captured == 0:
            return 0.0, {"skipped": "no_screenshots_captured",
                         "subset_pages": [e.get("page") for e in subset]}

        # 7. 旁路 dispatch 文件（device_cfg + subset[含 compare_image] + 脚本路径）
        android_shots_dir = str((shared_dir / "android_screenshots").resolve())
        dispatch = {
            "device_cfg": device_cfg,
            "fact_tree": str(fact_tree.resolve()),
            "script_dir": str(script_dir.resolve()) if script_dir else None,
            "subset": subset,  # 每条含 compare_image（截到的对比图）或 None（没截到）
            "screenshots_captured": captured,
            "android_screenshots_dir": android_shots_dir,
        }
        dispatch_file = (iter_dir / "dispatch.json").resolve()
        dispatch_file.write_text(
            json.dumps(dispatch, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        # hard_score 恒为 0：ui_align 的分数完全由视觉 judge 给（截图一致=0，差异越大越接近1）。
        # 硬检查只负责截图+准备，不打分 → 任何失败路径都是 0，绝不误抬。
        hard_score = 0.0

        details: dict[str, object] = {
            "mode": "detect",
            "dispatch_file": str(dispatch_file),
            "device_cfg": device_cfg,
            "subset": subset,           # extract_work_items + 人读用；≤8 条，体积可控
            "subset_pages": [e["page"] for e in subset],
            "screenshots_captured": captured,
            "android_screenshots_dir": android_shots_dir,
        }
        return hard_score, details


# ── device resolution ─────────────────────────────────────────────────────────


def _resolve_adb_exe() -> str | None:
    """Resolve adb executable: PATH → ANDROID_HOME/ANDROID_SDK_ROOT → default SDK location."""
    p = shutil.which("adb")
    if p:
        return p
    cands = []
    for env in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        root = os.environ.get(env)
        if root:
            cands.append(Path(root) / "platform-tools" / "adb.exe")
            cands.append(Path(root) / "platform-tools" / "adb")
    local = os.environ.get("LOCALAPPDATA")
    if local:
        cands.append(Path(local) / "Android" / "Sdk" / "platform-tools" / "adb.exe")
    cands.append(Path.home() / "AppData" / "Local" / "Android" / "Sdk" / "platform-tools" / "adb.exe")
    # Android Studio defaults on macOS and Linux
    cands.append(Path.home() / "Library" / "Android" / "sdk" / "platform-tools" / "adb")
    cands.append(Path.home() / "Android" / "Sdk" / "platform-tools" / "adb")
    for c in cands:
        if c.is_file():
            return str(c)
    return None


def _resolve_android_serial(adb_exe: str) -> str | None:
    env = os.environ.get("ANDROID_SERIAL")
    if env:
        return env
    out = _run([adb_exe, "devices"])
    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith("List of devices"):
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":  # 'offline'/'unauthorized' 不算就绪
            return parts[0]
    return None


# ── pkg / bundle / ability resolution ─────────────────────────────────────────


def _resolve_android_pkg(tree: dict, android_dir: Path | None) -> str | None:
    pkg = ((tree.get("app") or {}).get("package")) if isinstance(tree, dict) else None
    if pkg:
        return pkg
    if android_dir:
        for mf in android_dir.rglob("AndroidManifest.xml"):
            if "/build/" in str(mf).replace("\\", "/"):
                continue
            m = re.search(r'package="([^"]+)"', mf.read_text(encoding="utf-8", errors="ignore"))
            if m:
                return m.group(1)
    return None


def _foreground_pkg(adb: str | None, ser: str | None) -> str | None:
    """Package of the current foreground activity (via dumpsys), or None if unknown."""
    if not adb or not ser:
        return None
    out = _run([adb, "-s", ser, "shell", "dumpsys", "activity", "activities"], timeout=10)
    m = re.search(r"(?:mResumedActivity|mCurrentFocus|topResumedActivity)"
                  r"[^\n]*?\s([a-zA-Z0-9_.]+)/[a-zA-Z0-9_.]+", out)
    return m.group(1) if m else None


def _installed_pkg(adb: str | None, ser: str | None,
                   want_pkg: str | None, launcher_fq: str | None) -> str | None:
    """Reconcile the build.gradle package with what's actually installed.

    The manifest namespace often differs from the installed applicationId
    (flavor/debug suffix or a computed id — e.g. Meshtastic namespace
    `org.meshtastic.app` vs installed `com.geeksville.mesh.*.debug`); using the
    wrong one makes `am start` a no-op and screencap grabs the desktop. Keep
    *want_pkg* if it's installed (the common case → zero change); else find the
    package whose LAUNCHER activity class == *launcher_fq*. Pure device query,
    cross-app generic; falls back to *want_pkg* on any miss."""
    if not adb or not ser:
        return want_pkg
    pkgs = _run([adb, "-s", ser, "shell", "pm", "list", "packages"], timeout=10)
    if want_pkg and f"package:{want_pkg}" in pkgs:
        return want_pkg
    if launcher_fq:
        q = _run([adb, "-s", ser, "shell", "cmd", "package", "query-activities",
                  "--brief", "-a", "android.intent.action.MAIN",
                  "-c", "android.intent.category.LAUNCHER"], timeout=12)
        for line in q.splitlines():
            line = line.strip()
            if "/" in line and line.endswith("/" + launcher_fq):
                return line.split("/", 1)[0]
    return want_pkg


# ── fact-tree + script dir ────────────────────────────────────────────────────


def _locate_fact_tree(harmony_dir: Path, spec_dir: Path | None,
                      android_dir: Path | None, shared_dir: Path) -> Path | None:
    """Prefer the run's shared cache; then legacy hand-generated locations; else
    generate a minimal fact-tree natively (pure Python, from the AndroidManifest)."""
    # 1. 本次 run 的 shared 缓存（首选）
    cache = shared_dir / "toolkit-fact-tree.json"
    if cache.is_file():
        return cache
    # 2. 兼容已有手工/上游预生成（richer tree 时优先用它）
    bases = [Path("."), harmony_dir]
    if spec_dir:
        bases += [spec_dir, spec_dir.parent]
    for base in bases:
        for rel in _FACTREE_CANDIDATES:
            p = base / rel
            if p.is_file():
                return p
        direct = base / "toolkit-fact-tree.json"
        if direct.is_file():
            return direct
    # 3. 原生生成最小 fact-tree（纯 Python，无外部 toolkit / rg / bash）→ run cache
    return _generate_minimal_fact_tree(android_dir, harmony_dir, cache)


def _iter_no_build(root: Path, pattern: str):
    """Yield files matching *pattern* under *root*, pruning build trees so a broken
    deep path (e.g. Gradle .transforms) can't abort the walk the way Path.rglob does."""
    import fnmatch
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in ("build", ".gradle", ".transforms", ".cxx")]
        for fn in fns:
            if fnmatch.fnmatch(fn, pattern):
                yield Path(dp) / fn


def _resolve_android_package(android_dir: Path) -> str | None:
    """Android applicationId: build.gradle namespace/applicationId → version-catalog appId.

    Modern Gradle projects declare the package outside AndroidManifest (which often
    has no `package=` attr). Handles literal values and `libs.versions.*.get()`
    references resolved from gradle/libs.versions.toml.
    """
    for gradle in _iter_no_build(android_dir, "build.gradle*"):
        try:
            txt = gradle.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for key in ("applicationId", "namespace"):
            m = re.search(rf'{key}\s*=\s*"([a-zA-Z0-9_.]+)"', txt)
            if m:
                return m.group(1)
            m = re.search(rf'{key}\s*=\s*libs\.versions\.([a-zA-Z0-9_.]+)\.get\(\)', txt)
            if m:
                cat_key = m.group(1).replace(".", "-")
                for toml in _iter_no_build(android_dir, "libs.versions.toml"):
                    try:
                        ttxt = toml.read_text(encoding="utf-8", errors="ignore")
                    except Exception:
                        continue
                    mt = re.search(rf'{re.escape(cat_key)}\s*=\s*"([a-zA-Z0-9_.]+)"', ttxt)
                    if mt:
                        return mt.group(1)
    return None


def _pick_app_manifest(android_dir: Path) -> Path | None:
    """Pick the real app-module AndroidManifest.xml in a (possibly multi-module) repo.

    A multi-module Gradle app (e.g. Meshtastic: androidApp + feature/* + screenshot
    modules) has several `src/main/AndroidManifest.xml`, most with
    no activities. Taking the first one `rglob` returns is order-dependent and often
    lands on a 0-activity module → empty fact-tree → ui_align skips ("no activities").
    Instead score every candidate and prefer the one declaring a LAUNCHER activity,
    then the most activities, then a `src/main/` path. Single-module apps have one
    real candidate → identical selection. `os.walk` prunes build trees so a broken
    deep path (.transforms) can't abort enumeration.
    """
    import xml.etree.ElementTree as ET

    ns = "{http://schemas.android.com/apk/res/android}"
    best: Path | None = None
    best_key = (-1, -1, -1)
    for dp, dns, fns in os.walk(android_dir):
        dns[:] = [d for d in dns if d not in ("build", ".gradle", ".transforms", ".cxx")]
        sp = dp.replace("\\", "/")
        if "/src/test" in sp or "/src/androidTest" in sp or "AndroidManifest.xml" not in fns:
            continue
        mf = Path(dp) / "AndroidManifest.xml"
        try:
            app = ET.fromstring(mf.read_text(encoding="utf-8", errors="ignore")).find("application")
        except Exception:
            app = None
        acts = (app.findall("activity") + app.findall("activity-alias")) if app is not None else []
        launcher = any(
            "android.intent.action.MAIN" in {x.get(f"{ns}name") for x in itf.findall("action")}
            and "android.intent.category.LAUNCHER" in {x.get(f"{ns}name") for x in itf.findall("category")}
            for a in acts for itf in a.findall("intent-filter")
        )
        n = len(app.findall("activity")) if app is not None else 0
        key = (int(launcher), n, int("/src/main/" in sp))
        if key > best_key:
            best_key, best = key, mf
    return best


def _generate_minimal_fact_tree(android_dir: Path | None, harmony_dir: Path,
                                out_path: Path) -> Path | None:
    """Build a minimal toolkit-fact-tree.json from the AndroidManifest (pure Python).

    Emits exactly what ui_align v1 consumes — app.{package,launcher_short} + the
    activity list as pages[] — without the heavy toolkit (no ripgrep / bash / pip).
    reach_paths/navigation are intentionally empty (v1 captures the main screen via
    am-start; walk_to falls back accordingly). Returns the written path, or None.
    """
    if android_dir is None or not Path(android_dir).exists():
        return None
    import xml.etree.ElementTree as ET

    mf = _pick_app_manifest(Path(android_dir))
    if mf is None:
        return None

    try:
        text = mf.read_text(encoding="utf-8", errors="ignore")
        root = ET.fromstring(text)
    except Exception:
        return None

    ns = "{http://schemas.android.com/apk/res/android}"
    pkg = root.get("package") or _resolve_android_package(Path(android_dir)) or ""

    def _short(name: str) -> str:
        if not name:
            return ""
        if name.startswith("."):
            return name.rsplit(".", 1)[-1]
        return name.rsplit(".", 1)[-1] if "." in name else name

    def _fq(name: str) -> str:
        """Full component class for `am start -n pkg/CLASS`. Manifest names are
        relative (.activities.Foo) or absolute (com.x.Foo); normalize to absolute."""
        if not name:
            return ""
        if name.startswith("."):
            return pkg + name
        return name if "." in name else f"{pkg}.{name}"

    app = root.find("application")
    activities: list[str] = []
    fq_by_short: dict[str, str] = {}
    exported_by_short: dict[str, bool] = {}
    launcher = ""
    if app is not None:
        for act in app.findall("activity"):
            nm = act.get(f"{ns}name") or ""
            short = _short(nm)
            if short and short not in activities:
                activities.append(short)
                fq_by_short[short] = _fq(nm)
                exp = act.get(f"{ns}exported")
                # has launcher intent-filter ⇒ exported implicitly
                has_if = bool(act.findall("intent-filter"))
                exported_by_short[short] = (exp == "true") or (exp is None and has_if)
            for intent in act.findall("intent-filter"):
                actions = {a.get(f"{ns}name") for a in intent.findall("action")}
                cats = {c.get(f"{ns}name") for c in intent.findall("category")}
                if ("android.intent.action.MAIN" in actions
                        and "android.intent.category.LAUNCHER" in cats):
                    launcher = short
        # activity-alias (some apps register the launcher via an alias → targetActivity)
        for al in app.findall("activity-alias"):
            tgt = _short(al.get(f"{ns}targetActivity") or "")
            for intent in al.findall("intent-filter"):
                actions = {a.get(f"{ns}name") for a in intent.findall("action")}
                cats = {c.get(f"{ns}name") for c in intent.findall("category")}
                if ("android.intent.action.MAIN" in actions
                        and "android.intent.category.LAUNCHER" in cats and tgt):
                    launcher = tgt
                    if tgt not in activities:
                        activities.append(tgt)
                        fq_by_short[tgt] = _fq(al.get(f"{ns}targetActivity") or tgt)
                        exported_by_short[tgt] = True

    if not activities:
        return None

    tree = {
        "schema": "minimal-native/1",
        "app": {"package": pkg, "application_id": pkg, "launcher_short": launcher},
        "pages": [{"id": a, "android_fq": fq_by_short.get(a, ""),
                   "android_exported": exported_by_short.get(a, False)} for a in activities],
        "fragments": [],
        "dialogs": [],
    }
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(tree, ensure_ascii=False, indent=2), encoding="utf-8")
        return out_path
    except Exception:
        return None


def _resolve_script_dir() -> Path | None:
    """Vendored detection scripts: prefer seeded workspace copy, fall back to package seed."""
    ws = Path(".ecat/agents/generator/skills/arkts-visual-verify/scripts")
    if ws.is_dir():
        return ws
    seed = (Path(__file__).resolve().parents[3]
            / "seed" / "agents" / "generator" / "skills"
            / "arkts-visual-verify" / "scripts")
    return seed if seed.is_dir() else None


# ── page queue + rotating cursor subset selection ─────────────────────────────


def _build_page_queue(script_dir: Path | None, fact_tree: Path, harmony_dir: Path,
                      shared_dir: Path) -> list[dict]:
    """Run vendored build_page_queue.py (cwd=harmony_dir so features/products resolve)."""
    out_path = (shared_dir / "page_queue.json").resolve()
    shared_dir.mkdir(parents=True, exist_ok=True)
    if script_dir is not None:
        script = script_dir / "build_page_queue.py"
        if script.is_file():
            # Run with cwd=harmony_dir so build_page_queue's features/products
            # .ets resolution is relative to the HarmonyOS project. Use the ECAT
            # interpreter (sys.executable) — "python3" is often absent on Windows.
            with contextlib.suppress(Exception):
                subprocess.run(
                    [sys.executable, str(script.resolve()),
                     "--tree", str(fact_tree.resolve()), "--out", str(out_path)],
                    cwd=str(harmony_dir), capture_output=True, text=True, timeout=60,
                )
    if out_path.is_file():
        try:
            data = json.loads(out_path.read_text(encoding="utf-8"))
            return data.get("entries", []) or []
        except Exception:
            return []
    # Fallback: derive a minimal queue straight from the fact-tree pages.
    try:
        tree = json.loads(fact_tree.read_text(encoding="utf-8"))
        return [{"name": p.get("id"), "kind": "page", "file_resolved": None}
                for p in (tree.get("pages") or []) if p.get("id")]
    except Exception:
        return []


def _ets_hash(harmony_dir: Path, ets_rel: str | None) -> str | None:
    if not ets_rel:
        return None
    p = harmony_dir / ets_rel
    if not p.is_file():
        p = Path(ets_rel)
    if not p.is_file():
        return None
    try:
        return hashlib.md5(p.read_bytes()).hexdigest()[:12]
    except Exception:
        return None


def _harmony_ets_index(harmony_dir: Path) -> dict[str, str]:
    """{stem_lower: relpath} for all .ets under harmony_dir (build/oh_modules excluded).

    Covers entry-module layouts (entry/src/main/ets/pages/*.ets) that
    build_page_queue's features/products search misses.
    """
    idx: dict[str, str] = {}
    for p in harmony_dir.rglob("*.ets"):
        if should_skip_path(p, harmony_dir):
            continue  # 排除 build/oh_modules/.idea 等依赖、缓存、点目录
        try:
            rel = str(p.relative_to(harmony_dir)).replace("\\", "/")
        except ValueError:
            rel = str(p).replace("\\", "/")
        idx.setdefault(p.stem.lower(), rel)
    return idx


def _match_ets(page_name: str, index: dict[str, str]) -> str | None:
    """Match a fact-tree page id to a harmony .ets stem (Activity→Page name variants)."""
    if not page_name:
        return None
    base = page_name.lower().replace("-", "").replace("_", "")
    variants = {base}
    if base.endswith("activity"):
        stem = base[: -len("activity")]
        variants |= {stem, stem + "page"}
    variants.add(base + "page")
    norm = {k.replace("-", "").replace("_", ""): v for k, v in index.items()}
    for v in variants:
        if v in norm:
            return norm[v]
    for k, rel in norm.items():  # substring fallback
        if base and base in k:
            return rel
    return None


def _select_subset(queue: list[dict], harmony_dir: Path, shared_dir: Path) -> list[dict]:
    """Rotating cursor + change-gating.

    Priority: pages whose .ets changed since last detection, then cursor-order
    next pages, capped at _SUBSET_CAP. Cursor advances (wrapping) so coverage
    accumulates across iterations. The cursor state is cross-iteration → shared_dir.
    """
    cursor_path = shared_dir / "cursor.json"
    state = {"cursor": 0, "hashes": {}}
    if cursor_path.exists():
        with contextlib.suppress(Exception):
            state = json.loads(cursor_path.read_text(encoding="utf-8"))
    hashes: dict = state.get("hashes", {}) or {}
    cursor = int(state.get("cursor", 0))

    names = [e.get("name") for e in queue if e.get("name")]
    by_name = {e["name"]: e for e in queue if e.get("name")}
    n = len(names)
    if n == 0:
        return []

    # Backfill .ets paths for entry-module layouts build_page_queue can't resolve.
    ets_index = _harmony_ets_index(harmony_dir)
    for nm in names:
        if not by_name[nm].get("file_resolved"):
            hit = _match_ets(nm, ets_index)
            if hit:
                by_name[nm]["file_resolved"] = hit

    cur_hashes = {nm: _ets_hash(harmony_dir, by_name[nm].get("file_resolved")) for nm in names}
    changed = [nm for nm in names if cur_hashes[nm] is not None and cur_hashes[nm] != hashes.get(nm)]

    selected: list[str] = []
    for nm in changed:
        if nm not in selected:
            selected.append(nm)
        if len(selected) >= _SUBSET_CAP:
            break

    i = 0
    while len(selected) < _SUBSET_CAP and i < n:
        nm = names[(cursor + i) % n]
        if nm not in selected:
            selected.append(nm)
        i += 1

    # advance cursor past what we covered; record current hashes for selected
    state["cursor"] = (cursor + max(i, len(selected))) % n
    for nm in selected:
        if cur_hashes.get(nm) is not None:
            hashes[nm] = cur_hashes[nm]
    state["hashes"] = hashes
    try:
        shared_dir.mkdir(parents=True, exist_ok=True)
        cursor_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass

    out: list[dict] = []
    for nm in selected:
        e = by_name[nm]
        out.append({
            "page": nm,
            "kind": e.get("kind"),
            "ets_path": e.get("file_resolved"),
            "reach_path": e.get("reach_path"),
            "host_page": e.get("host_page"),
            "triggered_by": e.get("triggered_by"),
        })
    return out


# ── screenshot capture (Python; subagents have no Bash on Windows) ─────────────


def _walk_to(script_dir: Path | None, device: str, device_cfg: dict,
             fact_tree: Path, page: str, env: dict) -> bool:
    """Best-effort navigate to `page` on `device`. True if reached (exit 0)."""
    if script_dir is None:
        return False
    script = script_dir / "walk_to.py"
    if not script.is_file():
        return False
    args = [sys.executable, str(script.resolve()), "--device", device,
            "--page-id", page, "--fact-tree", str(fact_tree.resolve())]
    if device == "harmonyos":
        args += ["--target", device_cfg["hmos_target"], "--bundle", device_cfg["hmos_bundle"],
                 "--ability", device_cfg.get("hmos_ability") or "EntryAbility"]
    else:
        args += ["--serial", device_cfg["android_serial"]]
    try:
        r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=env, timeout=90)
        return r.returncode == 0
    except Exception:
        return False


def _screencap_android(device_cfg: dict, out_png: Path, env: dict) -> bool:
    adb, ser = device_cfg["adb_exe"], device_cfg["android_serial"]
    try:
        with open(out_png, "wb") as fh:
            r = subprocess.run([adb, "-s", ser, "exec-out", "screencap", "-p"],
                               stdout=fh, stderr=subprocess.DEVNULL, env=env, timeout=20)
        return r.returncode == 0 and out_png.is_file() and out_png.stat().st_size > 1000
    except Exception:
        return False


def _screencap_hmos(device_cfg: dict, out_png: Path, env: dict) -> bool:
    """HMOS screenshot via `uitest screenCap` (snapshot_display -f rejects non-.jpeg)."""
    hdc, tgt = device_cfg["hdc_exe"], device_cfg["hmos_target"]
    remote = "/data/local/tmp/_ui_align_shot.png"
    try:
        subprocess.run([hdc, "-t", tgt, "shell", "uitest", "screenCap", "-p", remote],
                       capture_output=True, text=True, env=env, timeout=20)
        subprocess.run([hdc, "-t", tgt, "file", "recv", remote, str(out_png)],
                           capture_output=True, text=True, env=env, timeout=20)
        return out_png.is_file() and out_png.stat().st_size > 1000
    except Exception:
        return False


def _compose_compare(android_png: Path, hmos_png: Path, out_jpeg: Path) -> bool:
    """PIL: scale to common height, paste side-by-side w/ labels, cap max-edge ≤1800."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception:
        return False
    try:
        a = Image.open(android_png).convert("RGB")
        h = Image.open(hmos_png).convert("RGB")
        target_h = min(a.height, h.height, 1600) or 1
        a = a.resize((max(1, a.width * target_h // a.height), target_h))
        h = h.resize((max(1, h.width * target_h // h.height), target_h))
        gap, label_h = 20, 60
        canvas = Image.new("RGB", (a.width + gap + h.width, target_h + label_h), (255, 255, 255))
        draw = ImageDraw.Draw(canvas)
        try:
            font = ImageFont.truetype("arial.ttf", 28)
        except Exception:
            font = ImageFont.load_default()
        draw.text((a.width // 2 - 50, 16), "Android", fill=(0, 0, 0), font=font)
        draw.text((a.width + gap + h.width // 2 - 60, 16), "HarmonyOS", fill=(0, 0, 0), font=font)
        canvas.paste(a, (0, label_h))
        canvas.paste(h, (a.width + gap, label_h))
        if max(canvas.size) > 1800:
            r = 1800 / max(canvas.size)
            canvas = canvas.resize((int(canvas.width * r), int(canvas.height * r)))
        out_jpeg.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(out_jpeg, "JPEG", quality=85)
        return out_jpeg.is_file()
    except Exception:
        return False


def _capture_current(device_cfg: dict, env: dict, shots_dir: Path, entry: dict,
                     android_cache_dir: Path) -> bool:
    """Screenshot the current screen → compose → set entry['compare_image'].

    The Android app never changes across iterations (only HarmonyOS is rebuilt),
    so the Android screenshot is captured ONCE and cached in android_cache_dir
    (the run's shared dir); later iterations reuse it and only re-capture HMOS.
    """
    page = entry.get("page") or "screen"
    a_cached = android_cache_dir / f"{page}_android.png"
    a_png = shots_dir / f"{page}_android.png"
    h_png = shots_dir / f"{page}_harmony.png"
    # Android: reuse cached shot if present, else capture once into the cache.
    if a_cached.is_file() and a_cached.stat().st_size > 1000:
        try:
            shutil.copyfile(a_cached, a_png)
        except Exception:
            a_png = a_cached
    else:
        android_cache_dir.mkdir(parents=True, exist_ok=True)
        if not _screencap_android(device_cfg, a_cached, env):
            return False
        try:
            shutil.copyfile(a_cached, a_png)
        except Exception:
            a_png = a_cached
    # HMOS: always re-capture (rebuilt each iteration).
    if not _screencap_hmos(device_cfg, h_png, env):
        return False
    out = shots_dir / f"{page}_compare.jpeg"
    if not _compose_compare(a_png, h_png, out):
        return False
    entry["compare_image"] = str(out.resolve())
    # Menu compare: only when Android has an overflow-menu shot for this page (apps
    # without an overflow menu just skip — no HMOS menu tap, no menu compare). Tap the
    # HMOS overflow, screenshot, and pair it with the Android menu shot so the judge
    # can compare menu contents (e.g. which actions live in the menu).
    a_menu = android_cache_dir / f"{page}_menu_android.png"
    if a_menu.is_file() and a_menu.stat().st_size > 1000:
        h_menu = shots_dir / f"{page}_menu_harmony.png"
        try:
            if _capture_hmos_overflow(device_cfg, env, h_menu):
                menu_out = shots_dir / f"{page}_menu_compare.jpeg"
                _compose_compare(a_menu, h_menu, menu_out)
        except Exception:
            pass
    return True


def _find_overflow_coord(xml: str) -> tuple[int, int] | None:
    """Locate the Android overflow (⋮) button in a uiautomator dump — app-agnostic.

    The Android framework sets a standard `content-desc="More options"` on every
    Toolbar/ActionBar overflow button (localized variants exist, but the English
    string is the default and most common). We match that first, then fall back to
    a resource-id containing 'overflow'. We deliberately do NOT match generic 'menu'
    /'more' to avoid mis-tapping unrelated buttons. Returns the tap center or None.
    """
    def _attr(node: str, name: str) -> str:
        m = re.search(rf'{name}="([^"]*)"', node)
        return m.group(1) if m else ""

    def _center(node: str) -> tuple[int, int] | None:
        b = re.search(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', node)
        if not b:
            return None
        x1, y1, x2, y2 = map(int, b.groups())
        return ((x1 + x2) // 2, (y1 + y2) // 2)

    nodes = re.findall(r"<node[^>]*/?>", xml)
    # Pass 1: standard framework content-desc for the overflow button.
    _OVERFLOW_DESCS = ("more options", "更多选项", "더 보기", "weitere optionen", "más opciones")
    for s in nodes:
        if _attr(s, "clickable").lower() != "true":
            continue
        if _attr(s, "content-desc").strip().lower() in _OVERFLOW_DESCS:
            c = _center(s)
            if c:
                return c
    # Pass 2: resource-id explicitly naming an overflow button.
    for s in nodes:
        if _attr(s, "clickable").lower() != "true":
            continue
        rid = _attr(s, "resource-id").lower()
        if "overflow" in rid or rid.endswith(":id/menu_more") or rid.endswith("_overflow"):
            c = _center(s)
            if c:
                return c
    return None


def _capture_android_overflow(device_cfg: dict[str, Any], env: dict[str, Any], page: str,
                              android_cache_dir: Path) -> bool:
    """Tap the Android top-bar overflow (⋮) menu → screenshot → BACK.

    The overflow menu is a transient popup (not an activity), so a static screenshot
    misses it. We dump the UI, find the overflow button (app-agnostic, see
    _find_overflow_coord), tap it, screenshot the expanded menu to
    {page}_menu_android.png, then BACK to close. Cached in android_cache_dir
    (run-level). Best-effort; never raises. Returns True if a menu shot was captured.
    """
    adb, ser = device_cfg.get("adb_exe"), device_cfg.get("android_serial")
    if not adb or not ser:
        return False
    out = android_cache_dir / f"{page}_menu_android.png"
    if out.is_file() and out.stat().st_size > 1000:
        return True
    try:
        _run([adb, "-s", ser, "shell", "uiautomator", "dump", "/sdcard/_ui_align_d.xml"], timeout=12)
        local = android_cache_dir / "_dump.xml"
        _run([adb, "-s", ser, "pull", "/sdcard/_ui_align_d.xml", str(local)], timeout=12)
        xml = local.read_text(encoding="utf-8", errors="ignore")
        with contextlib.suppress(Exception):
            local.unlink()
    except Exception:
        return False
    coord = _find_overflow_coord(xml)
    if not coord:
        return False  # app has no overflow menu on this screen — fine, skip
    try:
        _run([adb, "-s", ser, "shell", "input", "tap", str(coord[0]), str(coord[1])], timeout=8)
        time.sleep(0.6)
        ok = _screencap_android(device_cfg, out, env)
        _run([adb, "-s", ser, "shell", "input", "keyevent", "KEYCODE_BACK"], timeout=8)  # close menu
        time.sleep(0.3)
        return ok
    except Exception:
        return False


def _find_overflow_coord_hmos(dump_json: str) -> tuple[int, int] | None:
    """Locate a HarmonyOS top-bar overflow (⋮ / more) button in a uitest dumpLayout
    JSON — app-agnostic. HarmonyOS SymbolGlyph/Button icons are usually UNLABELED
    (no content-desc, unlike Android), so text matching alone fails. Strategy:
      1. match a labeled node naming an overflow/more button (rare but cheap), else
      2. heuristic — the RIGHTMOST clickable Button/icon in the top app-bar band
         (y in the top ~15% of the screen), which is conventionally the overflow.
    Returns the tap center, or None when nothing toolbar-like is found (→ skip).
    """
    try:
        data = json.loads(dump_json)
    except Exception:
        return None
    _DESCS = ("more options", "more", "menu", "更多", "更多选项", "overflow")

    def _bounds(attrs: dict[str, Any]) -> tuple[int, ...] | None:
        m = re.search(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", str(attrs.get("bounds") or ""))
        return tuple(map(int, m.groups())) if m else None

    labeled: list[tuple[int, int]] = []
    bar_btns: list[tuple[int, int, int, int]] = []  # x1,y1,x2,y2 of clickable top-bar icons
    screen_h = [0]

    def walk(node: object) -> None:
        if isinstance(node, dict):
            a = node.get("attributes") or node
            bb = _bounds(a)
            if bb:
                screen_h[0] = max(screen_h[0], bb[3])
                text = " ".join(str(a.get(k, "")) for k in
                                ("description", "accessibilityText", "text", "id", "key")).lower()
                if any(d in text for d in _DESCS):
                    labeled.append(((bb[0] + bb[2]) // 2, (bb[1] + bb[3]) // 2))
                if str(a.get("clickable")).lower() == "true" and a.get("type") in (
                        "Button", "SymbolGlyph", "Image"):
                    bar_btns.append(bb)
            for v in (node.get("children") or node.get("childNodes") or []):
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(data)
    if labeled:
        return labeled[0]
    # heuristic: rightmost clickable icon within the top ~15% band → overflow
    h = screen_h[0] or 0
    top_band = [b for b in bar_btns if h and b[1] < h * 0.15]
    if not top_band:
        return None
    rightmost = max(top_band, key=lambda b: b[0])
    return ((rightmost[0] + rightmost[2]) // 2, (rightmost[1] + rightmost[3]) // 2)


def _capture_hmos_overflow(device_cfg: dict[str, Any], env: dict[str, Any], out_png: Path) -> bool:
    """Tap the HarmonyOS overflow (⋮) menu → screenshot to out_png → BACK.

    App-agnostic and best-effort: if the screen has no overflow button (many apps
    don't), return False quietly — no menu compare is produced. Never raises.
    """
    hdc, tgt = device_cfg.get("hdc_exe"), device_cfg.get("hmos_target")
    if not hdc or not tgt:
        return False
    dump = _dump_hmos_ui(device_cfg, out_png.parent)
    if not dump:
        return False
    coord = _find_overflow_coord_hmos(dump)
    if not coord:
        return False  # no HMOS overflow on this screen — skip
    try:
        _run([hdc, "-t", tgt, "shell", "uitest", "uiInput", "click",
              str(coord[0]), str(coord[1])], timeout=8)
        time.sleep(0.6)
        ok = _screencap_hmos(device_cfg, out_png, env)
        _run([hdc, "-t", tgt, "shell", "uitest", "uiInput", "keyEvent", "Back"], timeout=8)
        time.sleep(0.3)
        return ok
    except Exception:
        return False


def _pick_home_entry(subset: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The subset page we land on after launch — prefer Main/Home, then Splash, else first."""
    for kw in ("main", "home", "splash"):
        for e in subset:
            if kw in str(e.get("page", "")).lower():
                return e
    return subset[0] if subset else None


def _shot_signature(png_path: Path) -> list[int] | None:
    """Downscaled grayscale pixel vector for visual near-duplicate detection.

    Returns a small list of pixel values (32x64 grayscale) or None. Two shots are
    near-duplicates when their mean-absolute pixel difference is tiny — robust to
    status-bar clock / 1-px / cursor diffs that defeat md5, so we detect that several
    data-dependent activities all redirected to the SAME screen on blind launch.
    """
    try:
        from PIL import Image
        im = Image.open(png_path).convert("L").resize((32, 64))
        return list(im.getdata())
    except Exception:
        return None


def _shots_near_dup(a: list[int] | None, b: list[int] | None, max_mad: float = 8.0) -> bool:
    """True if two _shot_signature vectors are near-identical (mean abs diff < max_mad)."""
    if a is None or b is None or len(a) != len(b):
        return False
    total = sum(abs(x - y) for x, y in zip(a, b, strict=False))
    return (total / len(a)) < max_mad


def _prime_android_cache(tree: dict[str, Any], device_cfg: dict[str, Any], env: dict[str, Any],
                         android_cache_dir: Path) -> int:
    """First-round full sweep: am-start every reachable Android activity → screenshot →
    cache to android_cache_dir/{page}_android.png (run-level, reused across iterations).

    Android allows `am start -n pkg/Activity` to jump to any exported activity, so we
    capture them all once up-front. Pages already cached are skipped (idempotent).
    Non-exported / crashing activities are skipped silently. HMOS can't do this (single
    ability), so these extra Android shots serve as single-sided reference targets for
    visual-fixer. Returns the number of NEW Android pages cached.
    """
    adb, ser = device_cfg.get("adb_exe"), device_cfg.get("android_serial")
    pkg = device_cfg.get("android_pkg") or (tree.get("app") or {}).get("package")
    if not adb or not ser or not pkg:
        return 0
    android_cache_dir.mkdir(parents=True, exist_ok=True)
    new = 0
    # Visual signatures of pages already kept — to drop near-duplicate blind-launch
    # screens (data-dependent activities that all redirect to the same screen).
    seen_sigs: list[tuple[list[int] | None, str]] = []
    for f in sorted(android_cache_dir.glob("*_android.png")):
        if f.name.endswith("_menu_android.png"):
            continue
        sig = _shot_signature(f)
        if sig is not None:
            seen_sigs.append((sig, f.name))
    for page in (tree.get("pages") or []):
        pid = page.get("id")
        if not pid:
            continue
        cached = android_cache_dir / f"{pid}_android.png"
        if cached.is_file() and cached.stat().st_size > 1000:
            continue  # already have it
        # Try am-start regardless of the manifest `exported` flag: from adb shell
        # (same uid as the app) non-exported activities still launch. Let the device
        # reject (SecurityException / not-exported) — we skip only on actual failure.
        fq = page.get("android_fq") or f"{pkg}.{pid}"
        try:
            r = _run([adb, "-s", ser, "shell", "am", "start", "-n", f"{pkg}/{fq}"], timeout=12)
            low = r.lower()
            if ("error" in low or "exception" in low or "does not exist" in low
                    or "not exported" in low or "permission deni" in low):
                continue
            time.sleep(1.5)
            # Only capture if OUR app is actually foreground: a wrong/stale package
            # or a crash-on-launch leaves the launcher (desktop) on screen, which
            # must not be saved as a reference target. Skip only when we're SURE the
            # foreground is a different app (fg known and != pkg) — never over-skip.
            fg = _foreground_pkg(device_cfg["adb_exe"], device_cfg["android_serial"])
            if fg and pkg and fg != pkg:
                continue
            if _screencap_android(device_cfg, cached, env):
                # Drop visual near-duplicates: data-dependent viewer activities
                # (Photo/Video/etc.) launched without their Intent URI redirect to the
                # same screen, producing near-identical shots that mislead the fixer.
                sig = _shot_signature(cached)
                if sig is not None and any(_shots_near_dup(sig, s) for s, _ in seen_sigs):
                    cached.unlink(missing_ok=True)
                    continue
                if sig is not None:
                    seen_sigs.append((sig, cached.name))
                new += 1
                # Also try the overflow (⋮) menu on this screen — a transient popup
                # that the static shot misses. No-op if the screen has no overflow.
                with contextlib.suppress(Exception):
                    _capture_android_overflow(device_cfg, env, pid, android_cache_dir)
        except Exception:
            continue
    return new


def _capture_subset_sbs(subset: list[dict[str, Any]], device_cfg: dict[str, Any], fact_tree: Path,
                        script_dir: Path | None, harmony_dir: Path,
                        shots_dir: Path, shared_dir: Path) -> int:
    """Capture Android-vs-HarmonyOS comparison images for the subset.

    v1 reliably captures the app's HOME/main screen (reachable via launch, no
    reach_paths needed) and attributes it to the main page; other pages are
    best-effort via walk_to (usually unreachable until the fact-tree has
    reach_paths). Sets entry['compare_image'] to the comparison jpeg or None.

    Per-iteration outputs (compose jpeg + HMOS png) go to shots_dir (the iter dir).
    The Android png — identical every iteration — is cached once under shared_dir
    (the run-level visual_cache) and reused, so we don't re-screenshot Android.
    Returns the count captured. Serial device I/O; never raises.
    """
    shots_dir.mkdir(parents=True, exist_ok=True)
    android_cache_dir = shared_dir / "android_screenshots"
    env = _device_env(device_cfg)
    for e in subset:
        e["compare_image"] = None

    # First round only: sweep ALL reachable Android activities once → cache to
    # shared_dir. Skipped on later iterations (Android never changes) — the cache
    # already holds the shots; re-sweeping would re-launch the app + am-start every
    # page for nothing. Serves as single-sided reference shots for visual-fixer too.
    already_primed = any(
        p.name.endswith("_android.png") and not p.name.endswith("_menu_android.png")
        for p in android_cache_dir.glob("*_android.png")
    ) if android_cache_dir.is_dir() else False
    if not already_primed:
        try:
            tree = json.loads(fact_tree.read_text(encoding="utf-8"))
            _prime_android_cache(tree, device_cfg, env, android_cache_dir)
        except Exception:
            pass

    with contextlib.suppress(Exception):
        _launch_and_dismiss(device_cfg, env, launch_android=not already_primed)
    captured = 0
    # 1) Home/main screen — always reachable after launch (no walk_to).
    home = _pick_home_entry(subset)
    if home is not None:
        try:
            if _capture_current(device_cfg, env, shots_dir, home, android_cache_dir):
                captured += 1
        except Exception:
            pass
    # 2) Other pages — best-effort navigate then capture.
    for entry in subset:
        if entry is home or entry.get("compare_image"):
            continue
        page = entry.get("page")
        if not page:
            continue
        try:
            if (
                _walk_to(script_dir, "android", device_cfg, fact_tree, page, env)
                and _walk_to(script_dir, "harmonyos", device_cfg, fact_tree, page, env)
                and _capture_current(device_cfg, env, shots_dir, entry, android_cache_dir)
            ):
                captured += 1
        except Exception:
            continue
    return captured
