"""Shared device session for device-driven plugins (runtime_liveness + ui_align).

Per (harmony_dir, iteration) it does, exactly ONCE, under a lock:
  1. rebuild + install the current HarmonyOS source (shared — never compiled twice),
  2. cold-launch the app to its main page,
  3. run the liveness/health probe.

The probe is layered, from hard failure to soft:
  - hard crash      → new faultlog for the bundle, OR process died + hilog crash markers (1.0)
  - white screen    → alive but the首屏 UI tree is empty (0.5)
  - runtime errors  → alive + rendered, but the app's OWN hilog shows high-signal
                      runtime failures (ArkTS uncaught exceptions, resource-load
                      failures, native symbol-not-found, page/route failures,
                      permission denials) — 0.2..0.6, each backed by the actual log line
  - healthy         → alive + rendered + clean (0.0)

Anti-false-positive: faultlog/crash markers are strong; runtime-error scan only
matches the APP's own process lines against a curated high-signal pattern set and
surfaces the real log line as evidence; ambiguous launches are never called a crash.

Both device plugins call get_session(); the first triggers the work, the rest reuse
it (one compile+install, one launch, no UI-driving race). ui_align gates on
session.main_page_ok (alive + rendered). All low-level device primitives live in
this neutral module so neither plugin depends on the other (no import cycle).
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from ecat import deveco as _deveco
from ecat.entropy.plugins.a2h_verify._build_lock import HVIGOR_BUILD_LOCK

# ── crash / white-screen heuristics ───────────────────────────────────────────

_CRASH_MARKERS = (
    "Cppcrash", "cppcrash", "JsError", "jscrash", "appfreeze", "APP_FREEZE",
    "Fault thread", "FATAL EXCEPTION", "must not be null exception",
)
_FAULTLOG_DIRS = ("/data/log/faultlog/faultlogger", "/data/log/faultlog/temp")
_MEANINGFUL_TYPES = (
    "Text", "Image", "Button", "TextInput", "List", "ListItem", "Grid", "Tabs",
    "Search", "Toggle", "Slider", "Symbol", "SymbolGlyph", "Span", "Web",
)

# 运行时 hilog 深度扫描——只用高置信、几乎必是真问题的模式（小写匹配）。每条都会
# 附上实际日志行作证据，分类标签见 _CAT_LABELS。
_RUNTIME_ERROR_PATTERNS: dict[str, tuple[str, ...]] = {
    "js_exception": (
        "error message:", "is not callable", "is not a function",
        "cannot read propert", "typeerror:", "referenceerror:",
        "uncaught", "unhandled promise rejection",
    ),
    "resource_fail": (
        "resource not found", "resource_id_not_found",
        "failed to get resource", "failed to load media",
    ),
    "native_symbol": (
        "undefined symbol", "dlopen failed", "cannot locate symbol",
    ),
    "page_route_fail": (
        "failed to load page", "pushurl failed", "loadcontent failed",
        "page not found",
    ),
    "permission_denied": (
        "permission denied", "is not granted",
    ),
}
_CAT_LABELS = {
    "js_exception": "runtime ArkTS exception",
    "resource_fail": "runtime resource load failure",
    "native_symbol": "runtime native symbol not found/unregistered",
    "page_route_fail": "runtime page/route load failure",
    "permission_denied": "runtime permission denied",
}

# 框架级良性日志——几乎每个 App 都会打、并不代表 App 自身有 bug。命中即整行排除，
# 避免误报（如窗口管理服务 WMS 的 SetWindowType: permission denied!）。
_HILOG_NOISE = (
    "setwindowtype",          # WMS 设置窗口类型，普通应用本就无此系统权限，无害
    "wms: setwindowtype",
)


@dataclass
class DeviceSession:
    ok: bool                       # device + install ready and liveness probed
    status: str                    # alive_ok | runtime_errors | white_screen | crashed | launch_unconfirmed | <skip>
    liveness_score: float          # 0.0 healthy · 0.2-0.6 runtime errors · 0.5 white · 1.0 crash
    issues: list[str] = field(default_factory=list)
    details: dict[str, object] = field(default_factory=dict)
    skipped: str | None = None     # set when ok is False (no device / not built / etc.)
    device_cfg: dict = field(default_factory=dict)  # resolved hdc/target/bundle/ability

    @property
    def main_page_ok(self) -> bool:
        """True when the app launched, stayed alive and rendered a main page — the
        gate ui_align uses to decide whether screenshotting is meaningful. Runtime
        errors don't block it (the page still rendered); white/crash/skip do."""
        return self.status in ("alive_ok", "runtime_errors")


# ── pure diagnosis (unit-testable without a device) ───────────────────────────


def diagnose_liveness(
    alive: bool,
    new_faults: list[str],
    crash_markers: list[str],
    node_count: int,
    runtime_errors: dict[str, list[str]] | None = None,
) -> tuple[str, float, list[str]]:
    """Map raw probe results to (status, score, detailed issues).

    Precedence: crash > white screen > runtime errors > healthy. An unconfirmed
    launch (process gone, no crash evidence — usually env/not-installed) scores 0
    and is NOT called a crash. node_count == -1 means the UI tree was unavailable
    → treat as rendered (never invent a white screen)."""
    runtime_errors = runtime_errors or {}
    if new_faults:
        kinds = _crash_kinds(new_faults)
        names = ", ".join(_basename(f) for f in new_faults[:3])
        kind_s = f" ({'/'.join(kinds)})" if kinds else ""
        return "crashed", 1.0, [f"App crashed on launch{kind_s} — new HarmonyOS faultlog: {names}"]
    if not alive:
        if crash_markers:
            return "crashed", 1.0, [
                f"App process exited immediately after launch (instant crash); hilog crash markers: {', '.join(crash_markers[:4])}"
            ]
        return "launch_unconfirmed", 0.0, []  # 证据不足，不当崩溃、不计分

    if node_count == 0:  # alive but empty首屏
        return "white_screen", 0.5, [
            "App is alive after launch but the first-screen UI tree is empty (0 meaningful components) — likely white screen/render failure"
        ]

    if runtime_errors:  # alive + rendered, 但 hilog 有高置信运行时报错
        issues: list[str] = []
        for cat, lines in runtime_errors.items():
            label = _CAT_LABELS.get(cat, cat)
            more = f" (+{len(lines) - 1} more)" if len(lines) > 1 else ""
            issues.append(f"{label}: {lines[0][:160]}{more}")
        score = min(0.6, 0.2 + 0.1 * len(runtime_errors))
        return "runtime_errors", score, issues

    return "alive_ok", 0.0, []


def _crash_kinds(faults: list[str]) -> list[str]:
    kinds: list[str] = []
    joined = " ".join(faults).lower()
    if "cppcrash" in joined:
        kinds.append("native Cppcrash")
    if "jscrash" in joined or "jserror" in joined:
        kinds.append("ArkTS JsError")
    if "appfreeze" in joined or "freeze" in joined:
        kinds.append("appfreeze/ANR")
    return kinds


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def scan_runtime_errors(hilog: str, pid: str, bundle_short: str) -> dict[str, list[str]]:
    """High-signal runtime-error scan of the APP's own hilog lines.

    A line counts only if it references the app (its pid token OR the bundle short
    name) AND matches a curated high-signal pattern — both guards keep system /
    other-app / generic noise out. Returns {category: up-to-3 sample log lines}."""
    hits: dict[str, list[str]] = {}
    for raw in hilog.splitlines():
        line = raw.strip()
        if not line:
            continue
        refs_app = (pid and pid in line.split()) or (bundle_short and bundle_short in line)
        if not refs_app:
            continue
        low = line.lower()
        if any(n in low for n in _HILOG_NOISE):  # 框架级良性噪声，整行排除
            continue
        for cat, pats in _RUNTIME_ERROR_PATTERNS.items():
            if any(p in low for p in pats):
                bucket = hits.setdefault(cat, [])
                if len(bucket) < 3 and line not in bucket:
                    bucket.append(line)
                break
    return hits


# ── session build (production: rebuild + install + launch + probe) ────────────


# One device, one driver at a time. get_session holds it for build+launch+probe;
# plugins re-acquire it for their own post-session device work (ui_align screenshot,
# runtime_liveness smoke-tap) so the two never drive the emulator concurrently.
DEVICE_LOCK = threading.Lock()
_SESSIONS: dict[tuple[str, int], DeviceSession] = {}


def get_session(
    harmony_dir: Path,
    *,
    deveco_path: str | None = None,
    iteration: int = 0,
    device_cfg: dict | None = None,
) -> DeviceSession:
    """Memoized per (harmony, iteration), built once under the device lock."""
    key = (str(Path(harmony_dir).resolve()), int(iteration))
    with DEVICE_LOCK:
        s = _SESSIONS.get(key)
        if s is None:
            s = _build_session(Path(harmony_dir), deveco_path, device_cfg, rebuild=True)
            _SESSIONS[key] = s
            for k in [k for k in _SESSIONS if k[0] == key[0] and k[1] != key[1]]:
                _SESSIONS.pop(k, None)  # drop other iterations' sessions for this app
        return s


def probe_installed(harmony_dir: Path, deveco_path: str | None = None) -> DeviceSession:
    """Launch + liveness probe WITHOUT rebuilding (assumes app already installed).

    Not used in the loop — handy for fast multi-app testing of the probe/diagnosis
    logic without waiting for a full assembleHap each time."""
    return _build_session(Path(harmony_dir), deveco_path, None, rebuild=False)


def _build_session(
    harmony_dir: Path, deveco_path: str | None, device_cfg: dict | None, *, rebuild: bool
) -> DeviceSession:
    cfg = dict(device_cfg or {})
    hdc = cfg.get("hdc_exe") or _resolve_hdc_exe(str(deveco_path) if deveco_path else None)
    if not hdc:
        return DeviceSession(False, "no_device", 0.0, skipped="no_device",
                             details={"reason": "hdc not found on PATH/SDK"})
    tgt = cfg.get("hmos_target") or _resolve_hmos_target(hdc)
    if not tgt:
        return DeviceSession(False, "no_device", 0.0, skipped="no_device",
                             details={"reason": "no HarmonyOS target (emulator offline?)"})
    bundle = cfg.get("hmos_bundle") or _resolve_hmos_bundle({}, harmony_dir)
    if not bundle:
        return DeviceSession(False, "no_bundle", 0.0, skipped="no_bundle",
                             details={"reason": "cannot resolve HarmonyOS bundleName"})
    ability = cfg.get("hmos_ability") or _resolve_hmos_ability({}, harmony_dir)
    cfg.update(hdc_exe=hdc, hmos_target=tgt, hmos_bundle=bundle, hmos_ability=ability)
    # _launch_and_dismiss reads adb_exe/android_serial even with launch_android=False;
    # keep the keys present (unused on the HMOS-only path).
    cfg.setdefault("adb_exe", None)
    cfg.setdefault("android_serial", None)
    env = _device_env(cfg)

    def sh(args: list[str], timeout: float = 15.0) -> str:
        return _run([hdc, "-t", tgt, "shell", *args], timeout=timeout)

    # hdc_exe is surfaced so the discriminator can pull faultlogs directly
    # (`<hdc_exe> -t <target> shell cat <faultlog>`) instead of hunting for hdc
    # with a slow recursive filesystem search of the DevEco install.
    base = {"bundle": bundle, "ability": ability, "hmos_target": tgt, "hdc_exe": hdc}

    if rebuild:
        # Shared compile+install — done once per (harmony, iteration) under the lock.
        if not _build_and_install_hmos(harmony_dir, cfg, str(deveco_path) if deveco_path else None):
            return DeviceSession(False, "build_or_install_failed", 0.0,
                                 skipped="build_or_install_failed", details=base)
    elif bundle not in sh(["bm", "dump", "-n", bundle], timeout=20):
        return DeviceSession(False, "not_installed", 0.0, skipped="not_installed", details=base)

    # Pre-grant the app's declared permissions via the `acm` CLI so the runtime never
    # blocks on a system permission dialog (which is a separate system window the UI
    # dismissal below can miss). The UI-click dismissal stays as a best-effort fallback.
    _grant_declared_permissions(sh, harmony_dir, bundle)

    # Faultlog baseline → cold launch (HMOS only) → probe.
    before = _faultlog_entries(sh, bundle)
    _launch_and_dismiss(cfg, env, launch_android=False)
    time.sleep(4)

    after = _faultlog_entries(sh, bundle)
    new_faults = sorted(after - before)
    pid = _process_pid(sh, bundle)
    alive = bool(pid)
    hilog = sh(["hilog", "-x"], timeout=20)
    short = bundle.split(".")[-1]
    app_ref = bundle in hilog or short in hilog
    crash_markers = [m for m in _CRASH_MARKERS if m in hilog] if app_ref else []
    runtime_errors = scan_runtime_errors(hilog, pid, short) if alive else {}
    node_count = _meaningful_node_count(_dump_hmos_ui(cfg, Path(tempfile.gettempdir())))

    status, score, issues = diagnose_liveness(
        alive, new_faults, crash_markers, node_count, runtime_errors)
    return DeviceSession(
        ok=True, status=status, liveness_score=score, issues=issues,
        device_cfg=cfg,  # resolved hdc/target/bundle/ability — used by smoke-tap etc.
        details={**base, "pid": pid, "alive": alive, "new_faultlogs": new_faults,
                 "crash_markers": crash_markers,
                 "runtime_errors": {c: v for c, v in runtime_errors.items()},
                 "meaningful_node_count": node_count},
    )


def make_sh(device_cfg: dict):
    """Return a `sh(args, timeout)` closure that runs `hdc -t <target> shell <args>`,
    so plugins can drive the device using a session's resolved device_cfg without
    importing any plugin or re-resolving hdc."""
    hdc, tgt = device_cfg["hdc_exe"], device_cfg["hmos_target"]

    def sh(args: list[str], timeout: float = 15.0) -> str:
        return _run([hdc, "-t", tgt, "shell", *args], timeout=timeout)

    return sh


def find_clickables(dump_json: str | None) -> list[dict]:
    """Parse a uitest dumpLayout JSON for tappable nodes → [{label, cx, cy}].

    A node is a tap target when `clickable=true`. Its label prefers the node's own
    text, else the SHALLOWEST descendant text — many ArkUI containers (tab items,
    menu rows, cards) are the clickable element but keep their caption in a child
    Text, so a node-only label collapses every one of them to a generic
    'Column'/'Row' and same-label dedup then starves the bottom tabs / menu rows
    (e.g. 我的 → 关于我们 never gets probed). Falls back to id/type when there is no
    text anywhere. Dedups by tap centre (6px grid)."""
    if not dump_json:
        return []
    try:
        data = json.loads(dump_json)
    except Exception:
        return []
    out: list[dict] = []
    seen: set[tuple[int, int]] = set()
    emitted: list[tuple[int, int, int, int, str]] = []  # x1,y1,x2,y2,label

    def deep_text(node) -> str:
        """Shallowest human text in the subtree (BFS) — labels a clickable whose
        own node has no text (the caption lives in a descendant Text)."""
        queue = [node]
        while queue:
            n = queue.pop(0)
            if isinstance(n, dict):
                a = n.get("attributes") or n
                for key in ("text", "description", "accessibilityText"):
                    v = str(a.get(key) or "").strip()
                    if v:
                        return v
                queue.extend(n.get("children") or n.get("childNodes") or [])
            elif isinstance(n, list):
                queue.extend(n)
        return ""

    def walk(node):
        if isinstance(node, dict):
            a = node.get("attributes") or node
            if str(a.get("clickable")).lower() == "true":
                m = re.search(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", str(a.get("bounds", "")))
                if m:
                    x1, y1, x2, y2 = map(int, m.groups())
                    if x2 > x1 and y2 > y1:
                        cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
                        k = (cx // 6, cy // 6)
                        label = (deep_text(node)
                                 or str(a.get("id") or a.get("type") or "?"))[:40]
                        # Skip a nested clickable whose centre sits inside an
                        # already-emitted clickable carrying the SAME label — the
                        # outer + inner wrapper are one button (e.g. a tab Column
                        # around its inner Column), tapping both is a duplicate.
                        nested_dup = any(
                            rx1 <= cx <= rx2 and ry1 <= cy <= ry2 and rl == label
                            for rx1, ry1, rx2, ry2, rl in emitted)
                        if k not in seen and not nested_dup:
                            seen.add(k)
                            emitted.append((x1, y1, x2, y2, label))
                            out.append({"label": label, "cx": cx, "cy": cy})
            for v in (node.get("children") or node.get("childNodes") or []):
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(data)
    return out


# ── low-level probes ──────────────────────────────────────────────────────────


def _faultlog_entries(sh, bundle: str) -> set[str]:
    names: set[str] = set()
    short = bundle.split(".")[-1]
    for d in _FAULTLOG_DIRS:
        for line in sh(["ls", d], timeout=10).splitlines():
            line = line.strip()
            if line and (bundle in line or short in line):
                names.add(f"{d}/{line}")
    return names


def _process_pid(sh, bundle: str) -> str:
    """Return the app's pid as a string, or "" if not running."""
    out = sh(["pidof", bundle], timeout=10).strip()
    m = re.search(r"\d+", out)
    if m:
        return m.group(0)
    # fallback: parse `ps -A` for the bundle
    for line in sh(["ps", "-A"], timeout=10).splitlines():
        if bundle in line:
            m = re.search(r"\b(\d+)\b", line)
            if m:
                return m.group(1)
    return ""


def _meaningful_node_count(dump_json: str | None) -> int:
    """Count meaningful (typed or text-bearing) UI nodes; -1 if tree unavailable."""
    if not dump_json:
        return -1
    import json as _json
    try:
        data = _json.loads(dump_json)
    except Exception:
        return -1
    count = [0]

    def walk(node):
        if isinstance(node, dict):
            a = node.get("attributes") or node
            t = str(a.get("type", ""))
            txt = str(a.get("text", "") or a.get("content", ""))
            if t in _MEANINGFUL_TYPES or txt.strip():
                count[0] += 1
            for v in (node.get("children") or node.get("childNodes") or []):
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(data)
    return count[0]


# ── device primitives (relocated here to break the plugin import cycle) ──────────


def _run(cmd: list[str], timeout: float = 10.0) -> str:
    # encoding+errors are REQUIRED: hdc/hilog device output carries non-UTF-8 bytes
    # (GBK, binary). Without errors="replace" the decode raises in subprocess's reader
    # thread (uncatchable here) and kills the call. See also the install/build calls.
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        return (r.stdout or "") + (r.stderr or "")
    except Exception:
        return ""


def _resolve_hdc_exe(deveco_path: str | None) -> str | None:
    """Resolve hdc executable: PATH → DevEco config path → HDC env → DEVECO_HOME."""
    p = shutil.which("hdc")
    if p:
        return p
    rel = Path("sdk") / "default" / "openharmony" / "toolchains"
    roots = []
    root = _resolve_deveco_root(deveco_path)
    if root:
        roots.append(root)
    for env in ("DEVECO_HOME", "DEVECO_STUDIO_PATH"):
        v = os.environ.get(env)
        if v:
            roots.append(Path(v))
    env_hdc = os.environ.get("HDC")
    if env_hdc and Path(env_hdc).is_file():
        return env_hdc
    for root in roots:
        for name in ("hdc.exe", "hdc"):
            c = root / rel / name
            if c.is_file():
                return str(c)
    return None


def _resolve_hmos_target(hdc_exe: str) -> str | None:
    env = os.environ.get("HDC_TARGET")
    if env:
        return env
    out = _run([hdc_exe, "list", "targets"])
    for line in out.splitlines():
        t = line.strip()
        if not t or t.lower().startswith("[empty]") or "empty" in t.lower():
            continue
        # first plausible target token (serial or ip:port)
        return t.split()[0]
    return None


def _resolve_hmos_bundle(tree: dict, harmony_dir: Path) -> str | None:
    """Resolve the HarmonyOS bundleName for `aa start`.

    The HarmonyOS project's own AppScope/app.json5 is the ONLY authority for its
    bundleName, so read it first. The fact-tree's app.* fields describe the ANDROID
    app (application_id = the Android package), so they must NOT be trusted here —
    using them launches the wrong app whenever the HMOS bundleName differs from the
    Android package (the common case, e.g. com.example.foo vs com.android.foo).
    """
    app_json = harmony_dir / "AppScope" / "app.json5"
    if app_json.exists():
        m = re.search(r'"bundleName"\s*:\s*"([^"]+)"',
                      app_json.read_text(encoding="utf-8", errors="ignore"))
        if m:
            return m.group(1)
    # Fallback only if the HMOS app.json5 is missing/unreadable: a fact-tree may
    # carry an explicit HMOS bundle field (not application_id, which is Android).
    app = tree.get("app") if isinstance(tree, dict) else None
    if isinstance(app, dict):
        b = app.get("hmos_bundle") or app.get("bundleName")
        if b:
            return b
    return None


def _resolve_hmos_ability(tree: dict, harmony_dir: Path | None = None) -> str:
    app = tree.get("app") if isinstance(tree, dict) else None
    if isinstance(app, dict):
        ab = app.get("main_ability") or app.get("mainElement")
        if ab:
            return str(ab)
    # read harmony entry module.json5 mainElement
    if harmony_dir is not None:
        mj = harmony_dir / "entry" / "src" / "main" / "module.json5"
        if mj.exists():
            m = re.search(r'"mainElement"\s*:\s*"([^"]+)"',
                          mj.read_text(encoding="utf-8", errors="ignore"))
            if m:
                return m.group(1)
    return "EntryAbility"  # HarmonyOS standard default (not PhoneAbility)


def _device_env(device_cfg: dict) -> dict:
    """os.environ + adb/hdc dirs prepended to PATH + MSYS_NO_PATHCONV.

    Lets walk_to.py / click_and_verify_edge.py (which call bare `hdc`/`adb`)
    resolve the executables, and stops Git-Bash from mangling device-side
    paths like /data/local/tmp/...
    """
    env = dict(os.environ)
    extra = []
    for exe in (device_cfg.get("adb_exe"), device_cfg.get("hdc_exe")):
        if exe:
            extra.append(str(Path(exe).parent))
    if extra:
        env["PATH"] = os.pathsep.join(extra) + os.pathsep + env.get("PATH", "")
    env["MSYS_NO_PATHCONV"] = "1"
    return env


def _resolve_deveco_root(deveco_path: str | None) -> Path | None:
    """DevEco root: config path, else derived from JAVA_HOME, else the default
    macOS install location (same order as the compile plugin and the CLI)."""
    if deveco_path and _deveco.is_deveco_root(Path(deveco_path)):
        return Path(deveco_path)
    return _deveco.root_from_java_home(os.environ.get("JAVA_HOME")) or _deveco.default_root()


def _hvigorw_names() -> tuple[str, ...]:
    """Wrapper names in preference order: the .bat only runs on Windows, and the
    POSIX shell script only runs on macOS/Linux."""
    return ("hvigorw.bat", "hvigorw") if sys.platform == "win32" else ("hvigorw",)


def _resolve_hvigorw(harmony_dir: Path, deveco_path: str | None) -> Path | None:
    """hvigorw wrapper: project root first, then DevEco's bundled one."""
    for name in _hvigorw_names():
        c = harmony_dir / name
        if c.is_file():
            return c
    root = _resolve_deveco_root(deveco_path)
    if root:
        for name in _hvigorw_names():
            c = root / "tools" / "hvigor" / "bin" / name
            if c.is_file():
                return c
    return None


def _hvigorw_cmd(hvigorw: Path) -> list[str]:
    """assembleHap command line: Windows runs the .bat through cmd /c, macOS/Linux
    run the shell wrapper directly (no daemon, same as the compile plugin)."""
    if sys.platform == "win32":
        return ["cmd", "/c", str(hvigorw.resolve()), "assembleHap"]
    return [str(hvigorw.resolve()), "assembleHap", "--no-daemon"]


def _resolve_deveco_sdk(deveco_path: str | None) -> str | None:
    """DEVECO_SDK_HOME — hvigorw requires it; DevEco sets it, a bare CLI call doesn't."""
    env = os.environ.get("DEVECO_SDK_HOME")
    if env and Path(env).is_dir():
        return env
    root = _resolve_deveco_root(deveco_path)
    if root:
        c = root / "sdk"
        if c.is_dir():
            return str(c)
    return None


def _build_and_install_hmos(harmony_dir: Path, device_cfg: dict, deveco_path: str | None) -> bool:
    """Rebuild the HarmonyOS project (hvigorw assembleHap) + hdc install, so the
    emulator runs the CURRENT source before screenshots are taken.

    Returns True only when the build ITSELF succeeds and a freshly-built HAP is
    installed. A failed build → return False (do NOT install a stale prior HAP and
    claim freshness). Best-effort beyond that: capture then runs against whatever
    is installed, but details record build_ok=False so the result is not trusted.
    Slow (a HarmonyOS build is minutes); runs once per ui_align iteration.
    """
    hdc, tgt = device_cfg.get("hdc_exe"), device_cfg.get("hmos_target")
    hvigorw = _resolve_hvigorw(harmony_dir, deveco_path)
    if hvigorw is None or not hdc:
        sys.stderr.write("[ui_align] build skipped: hvigorw not found\n")
        return False
    sdk_home = _resolve_deveco_sdk(deveco_path)
    if not sdk_home:
        sys.stderr.write("[ui_align] build skipped: DEVECO_SDK_HOME unresolved\n")
        return False
    env = _device_env(device_cfg)
    env["DEVECO_SDK_HOME"] = sdk_home
    # PackageHap/SignHap spawn `java` as a child process — provide JAVA_HOME +
    # its bin on PATH (DevEco's bundled JDK: <root>/jbr, or jbr/Contents/Home on
    # macOS), else the build dies with `spawn java ENOENT` / 00308018. hvigorw on
    # macOS/Linux also needs DevEco's node on PATH. Best-effort: only if present.
    root = _resolve_deveco_root(deveco_path)
    if root:
        jdk = _deveco.jdk_home(root)
        if jdk:
            env["JAVA_HOME"] = str(jdk)
            env["PATH"] = str(jdk / "bin") + os.pathsep + env.get("PATH", "")
        node_dir = _deveco.node_bin_dir(root)
        if node_dir:
            env["PATH"] = str(node_dir) + os.pathsep + env.get("PATH", "")

    try:
        # The compile plugin builds the same project in a parallel thread — queue
        # behind it instead of racing on build/ and .hvigor/ (see _build_lock).
        with HVIGOR_BUILD_LOCK:
            build_start = time.time()
            b = subprocess.run(_hvigorw_cmd(hvigorw),
                               cwd=str(harmony_dir), capture_output=True, text=True,
                               encoding="utf-8", errors="replace", env=env, timeout=1200)
    except Exception as exc:
        sys.stderr.write(f"[ui_align] hvigorw build error: {exc}\n")
        return False
    out = (b.stdout or "") + (b.stderr or "")
    if b.returncode != 0 or "BUILD FAILED" in out:
        # surface the real compiler error (ArkTS error / unknown resource / etc.)
        tail = "\n".join(ln for ln in out.splitlines() if "ERROR" in ln or "Error Message" in ln)[-500:]
        sys.stderr.write(f"[ui_align] hvigorw assembleHap FAILED — NOT installing stale build.\n{tail}\n")
        return False

    # Build succeeded → the HAP under outputs/ reflects current source. Install the
    # newest one. (No mtime gate: hvigor's incremental build may not bump the HAP
    # mtime when only already-compiled artifacts are repackaged, which previously
    # made us skip install and screenshot a stale build.)
    haps = [p for p in harmony_dir.rglob("*.hap")
            if "/outputs/" in str(p).replace("\\", "/")
            and "/oh_modules/" not in str(p).replace("\\", "/")]
    if not haps:
        sys.stderr.write("[ui_align] build succeeded but no .hap under outputs/\n")
        return False
    hap = max(haps, key=lambda p: p.stat().st_mtime)

    def _install() -> tuple[bool, str]:
        r = subprocess.run([hdc, "-t", tgt, "install", hap.name],
                           cwd=str(hap.parent), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=env, timeout=120)
        out = (r.stdout or "") + (r.stderr or "")
        return ("successfully" in out.lower()), out

    try:
        ok, out = _install()
        # 版本降级冲突（设备上残留更高/冲突版本）→ 卸载后重装，避免循环里卡死。
        if not ok and ("downgrade" in out.lower() or "version" in out.lower()
                       or "9568263" in out):
            sys.stderr.write("[ui_align] install blocked (version downgrade) — "
                             "uninstalling then reinstalling\n")
            subprocess.run([hdc, "-t", tgt, "uninstall", device_cfg.get("hmos_bundle")],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=env, timeout=60)
            ok, out = _install()
        if not ok:
            sys.stderr.write(f"[ui_align] hdc install failed: {out[-200:]}\n")
        else:
            sys.stderr.write(f"[ui_align] rebuilt + installed {hap.name} "
                             f"(build {time.time()-build_start:.0f}s)\n")
        return ok
    except Exception as exc:
        sys.stderr.write(f"[ui_align] hdc install error: {exc}\n")
        return False


def _launch_and_dismiss(device_cfg: dict, env: dict, launch_android: bool = True) -> None:
    """Cold-launch the apps to their main screen + dismiss the Android compat dialog.

    Android: launch → BACK (dismiss the system 16KB compat dialog) → relaunch
    (warm, so we land back on the app's main screen whether or not BACK exited it,
    and the warm relaunch does not re-show the dialog).

    launch_android=False skips the Android side entirely — used on later iterations
    where the Android screenshots are already cached (Android never changes), so only
    HarmonyOS (rebuilt each round) needs launching.
    """
    adb, ser = device_cfg["adb_exe"], device_cfg["android_serial"]
    hdc, tgt = device_cfg["hdc_exe"], device_cfg["hmos_target"]
    pkg, bundle, ability = (device_cfg.get("android_pkg"), device_cfg["hmos_bundle"],
                            device_cfg.get("hmos_ability") or "EntryAbility")
    if pkg and launch_android:
        _run([adb, "-s", ser, "shell", "am", "force-stop", pkg], timeout=10)
        _run([adb, "-s", ser, "shell", "monkey", "-p", pkg,
              "-c", "android.intent.category.LAUNCHER", "1"], timeout=15)
        time.sleep(2)
        _run([adb, "-s", ser, "shell", "input", "keyevent", "KEYCODE_BACK"], timeout=8)
        time.sleep(1)
        _run([adb, "-s", ser, "shell", "monkey", "-p", pkg,
              "-c", "android.intent.category.LAUNCHER", "1"], timeout=15)
    # HMOS: force-stop first so aa start lands on the cold main page (not a resumed sub-page).
    _run([hdc, "-t", tgt, "shell", "aa", "force-stop", bundle], timeout=10)
    _run([hdc, "-t", tgt, "shell", "aa", "start", "-b", bundle, "-a", ability], timeout=15)
    time.sleep(3)
    # Dismiss first-launch permission / onboarding dialogs (e.g. after a fresh
    # install) that would otherwise occlude the main page in the screenshot. Loop a
    # few times for stacked dialogs (permission + What's-New). Clicking a dialog may
    # finish the app back to the launcher, so if we dismissed anything, relaunch to
    # land on a clean main page. No-op when no dialog is present.
    import tempfile as _tempfile
    scratch = Path(_tempfile.gettempdir())
    dismissed = False
    for _ in range(3):
        dump = _dump_hmos_ui(device_cfg, scratch)
        coord = _find_dismiss_coord_hmos(dump) if dump else None
        if not coord:
            break
        _run([hdc, "-t", tgt, "shell", "uitest", "uiInput", "click",
              str(coord[0]), str(coord[1])], timeout=8)
        dismissed = True
        time.sleep(1)
    if dismissed:
        _run([hdc, "-t", tgt, "shell", "aa", "force-stop", bundle], timeout=10)
        _run([hdc, "-t", tgt, "shell", "aa", "start", "-b", bundle, "-a", ability], timeout=15)
        time.sleep(2)


_PERMISSION_RE = re.compile(r"ohos\.permission\.[A-Z_]+")
_ACCESS_TOKEN_RE = re.compile(r'"?accessTokenId"?\s*:\s*"?(\d+)')


def _declared_permissions(harmony_dir: Path) -> list[str]:
    """Permission names declared in the app's module.json5 (regex — json5-safe).

    App-agnostic: we grant whatever the app actually requests, so this works for any
    migrated app without a hardcoded permission list."""
    mod = harmony_dir / "entry" / "src" / "main" / "module.json5"
    try:
        text = mod.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    return sorted(set(_PERMISSION_RE.findall(text)))


def _grant_declared_permissions(sh, harmony_dir: Path, bundle: str) -> None:
    """Best-effort CLI grant of the app's declared permissions via `acm grant`.

    Deterministic alternative to clicking the system permission dialog: HarmonyOS
    renders that dialog in a separate system window that `uitest dumpLayout` of the
    app can miss, so the click is flaky. Granting by tokenId is reliable.

    Silent no-op when `acm`/tokenId is unavailable (older/locked images) — the
    UI-click dismissal then remains the fallback. system_grant permissions that
    `acm grant` rejects are harmless (already auto-granted at install)."""
    perms = _declared_permissions(harmony_dir)
    if not perms:
        return
    try:
        dump = sh(["bm", "dump", "-n", bundle], timeout=20)
    except Exception:
        return
    m = _ACCESS_TOKEN_RE.search(dump or "")
    if not m:
        return
    token = m.group(1)
    for p in perms:
        with contextlib.suppress(Exception):
            sh(["acm", "grant", "-i", token, "-p", p], timeout=10)


def _dump_hmos_ui(device_cfg: dict, scratch_dir: Path) -> str | None:
    """Dump the current HarmonyOS UI tree (uitest dumpLayout) → JSON text, or None.

    Pulls the dump to scratch_dir, reads it, removes the local file. Best-effort.
    """
    hdc, tgt = device_cfg.get("hdc_exe"), device_cfg.get("hmos_target")
    if not hdc or not tgt:
        return None
    remote = "/data/local/tmp/_ui_align_hdump.json"
    local = scratch_dir / "_hdump.json"  # 固定文件名：每次覆盖，绝不在本地累积
    try:
        _run([hdc, "-t", tgt, "shell", "uitest", "dumpLayout", "-p", remote], timeout=15)
        _run([hdc, "-t", tgt, "file", "recv", remote, str(local)], timeout=12)
        return local.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return None
    finally:
        with contextlib.suppress(Exception):
            local.unlink()  # 读完必删，即使上面抛异常也不留残file


# Affirmative/continue button labels on first-launch permission & onboarding dialogs,
# across common languages. We dismiss by clicking these so the screenshot shows the
# app's real main page, not the dialog. Order matters only loosely — any match wins.
_DIALOG_DISMISS_WORDS = (
    "all files", "allow", "got it", "ok", "agree", "continue", "confirm",
    "always allow", "grant", "enable",
    "允许", "同意", "确定", "好的", "知道了", "继续", "始终允许", "授权", "开启",
)


def _find_dismiss_coord_hmos(dump_json: str) -> tuple[int, int] | None:
    """Find the button that dismisses a first-launch permission/onboarding dialog —
    app-agnostic. Strategy:
      1. keyword — a clickable node whose label is an affirmative/continue word
         (multi-lingual, see _DIALOG_DISMISS_WORDS). This is the ONLY signal we act
         on, because it is specific to dialogs.
    We deliberately do NOT use a pure bottom-of-screen position heuristic: a normal
    main page often has a bottom navigation/tab bar that would be mistaken for a
    dialog's action row (false-tapping into Settings etc.). So position is used only
    as a tie-breaker AMONG keyword hits, never on its own.
    Returns the tap center, or None when no affirmative dialog button is present.

    Honest limit: the keyword list is not exhaustive — a dialog whose button uses
    wording outside _DIALOG_DISMISS_WORDS won't be dismissed (we'd rather miss than
    mis-tap a real UI control).
    """
    try:
        data = json.loads(dump_json)
    except Exception:
        return None

    def _bounds(attrs: dict):
        m = re.search(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", str(attrs.get("bounds") or ""))
        return tuple(map(int, m.groups())) if m else None

    hits: list[tuple[int, int, int, int]] = []  # bounds of affirmative-keyword buttons

    def walk(node):
        if isinstance(node, dict):
            a = node.get("attributes") or node
            bb = _bounds(a)
            if bb:
                label = " ".join(str(a.get(k, "")) for k in
                                 ("text", "description", "accessibilityText")).lower()
                is_click = str(a.get("clickable")).lower() == "true" or a.get("type") == "Button"
                if is_click and any(w in label for w in _DIALOG_DISMISS_WORDS):
                    hits.append(bb)
            for v in (node.get("children") or node.get("childNodes") or []):
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(data)
    if not hits:
        return None
    # If several affirmative buttons (e.g. "Media Only" vs "All Files"), prefer the
    # rightmost — conventionally the grant-most/affirmative choice.
    b = max(hits, key=lambda x: x[0])
    return ((b[0] + b[2]) // 2, (b[1] + b[3]) // 2)
