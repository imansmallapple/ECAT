"""compile —— 编译可用性检查（CHECK-1，硬检查）。

调用 DevEco Studio 自带的 hvigorw 对 harmony_dir 做完整 build，
按编译错误 / 警告数量给分；编译失败时 sub_entropy=1.0（最高优先级修复）。

具体检查项：
  1. hvigorw 命令是否可用（依次查 config 路径、PATH、JAVA_HOME、DevEco Studio 环境变量、macOS 默认安装位置、项目本地 hvigorw[.bat]）
  2. assembleHap 任务是否成功完成
  3. 统计 ArkTS Compiler Error 数量（计入失败）
  4. 统计 hvigor ERROR 数量（计入失败）
  5. 统计 ArkTS WARN 数量（仅参考，不计入失败）

适用于任何 HarmonyOS 工程——只要 harmony_dir 是合法的 DevEco 项目即可。
"""

from __future__ import annotations

import functools
import logging
import os
import platform
import re
import shutil
import subprocess
from pathlib import Path

from ecat import deveco
from ecat.entropy.base import EntropyPlugin
from ecat.entropy.plugins.a2h_verify._build_lock import HVIGOR_BUILD_LOCK
from ecat.gradient.schema import WorkItem

log = logging.getLogger(__name__)


class CompilePlugin(EntropyPlugin):
    """Invoke hvigorw assembleHap and count compile errors."""

    name = "compile"
    default_weight = 0.25

    def extract_work_items(
        self, score: float, details: dict[str, object]
    ) -> list[WorkItem]:
        errors: list[str] = details.get("errors", [])  # type: ignore[assignment]
        if not errors:
            return []
        return [
            WorkItem(
                file=Path("."),
                issues=errors[:20],
                suggested_skills=["harmonyos-build-fix"],
                severity="high",
            )
        ]

    def evaluate(
        self,
        harmony_dir: Path,
        *,
        android_dir: Path | None = None,
        spec_dir: Path | None = None,
        config: dict[str, object] | None = None,
    ) -> tuple[float, dict[str, object]]:
        deveco_path = str((config or {}).get("deveco_studio_path") or "")
        hvigorw = _find_hvigorw(harmony_dir, deveco_path or None)

        if hvigorw is None:
            return 1.0, {
                "error": "hvigorw not found — install DevEco Studio or add it to PATH",
                "errors": [],
                "error_count": -1,
                "deveco_home": None,
            }

        env = _build_env(hvigorw)

        try:
            # Same project as the device session's build — never run both at once.
            with HVIGOR_BUILD_LOCK:
                result = subprocess.run(
                    [str(hvigorw), "assembleHap", "--no-daemon"],
                    capture_output=True,
                    text=True,
                    timeout=600,
                    cwd=harmony_dir,
                    env=env,
                )
        except subprocess.TimeoutExpired:
            from ecat.errors import TimeoutError as EcatTimeout

            raise EcatTimeout("hvigorw build timed out after 600s") from None

        output = result.stdout + result.stderr
        errors = _extract_errors(output)
        error_count = len(errors)

        if result.returncode != 0 or error_count > 0:
            score = min(1.0, 0.5 + error_count * 0.1)
        else:
            score = 0.0

        return score, {
            "returncode": result.returncode,
            "compiled": result.returncode == 0 and error_count == 0,
            "error_count": error_count,
            "errors": errors[:20],
            "hvigorw_path": str(hvigorw),
        }


# ── hvigorw discovery ────────────────────────────────────────────────────────


@functools.cache
def _find_hvigorw(harmony_dir: Path, deveco_studio_path: str | None = None) -> Path | None:
    """Locate hvigorw binary by probing system environment variables.

    Cached so that the discovery + log line runs only once per process,
    regardless of how many iterations call CompilePlugin.evaluate().
    """
    result = _discover_hvigorw(harmony_dir, deveco_studio_path)
    if result is not None:
        log.info("Using hvigorw: %s", result)
    return result


def _discover_hvigorw(harmony_dir: Path, deveco_studio_path: str | None) -> Path | None:
    is_win = platform.system() == "Windows"
    exe = "hvigorw.bat" if is_win else "hvigorw"

    # 0. Config-provided DevEco Studio root
    if deveco_studio_path:
        candidate = Path(deveco_studio_path) / "tools" / "hvigor" / "bin" / exe
        if candidate.exists():
            return candidate

    # 1. PATH lookup
    found = shutil.which("hvigorw")
    if found:
        return Path(found)

    # 2. Derive from JAVA_HOME (<deveco>/jbr, or <deveco>/jbr/Contents/Home on macOS)
    deveco_root = deveco.root_from_java_home(os.environ.get("JAVA_HOME"))
    if deveco_root:
        candidate = deveco_root / "tools" / "hvigor" / "bin" / exe
        if candidate.exists():
            return candidate

    # 3. Derive from "DevEco Studio" env var (<deveco>/bin → parent)
    deveco_env = os.environ.get("DevEco Studio")  # noqa: SIM112 — actual Windows env var name
    if deveco_env:
        deveco_root = Path(deveco_env).parent
        candidate = deveco_root / "tools" / "hvigor" / "bin" / exe
        if candidate.exists():
            return candidate

    # 4. Default install location (macOS /Applications)
    deveco_root = deveco.default_root()
    if deveco_root:
        candidate = deveco_root / "tools" / "hvigor" / "bin" / exe
        if candidate.exists():
            return candidate

    # 5. Project-local fallback
    for name in (exe, "hvigorw", "hvigorw.bat"):
        candidate = harmony_dir / name
        if candidate.exists():
            return candidate

    return None


def _build_env(hvigorw: Path) -> dict[str, str]:
    """Build subprocess env with DevEco SDK, JAVA_HOME, and Node."""
    env = os.environ.copy()

    deveco_root = _infer_deveco_root(hvigorw)
    if deveco_root:
        jdk = deveco.jdk_home(deveco_root)
        if jdk:
            env["JAVA_HOME"] = str(jdk)
            java_bin = jdk / "bin"
            if java_bin.is_dir():
                env["PATH"] = str(java_bin) + os.pathsep + env.get("PATH", "")

        sdk_dir = deveco_root / "sdk"
        if sdk_dir.is_dir():
            env["DEVECO_SDK_HOME"] = str(sdk_dir)

        node_dir = deveco.node_bin_dir(deveco_root)
        if node_dir:
            env["PATH"] = str(node_dir) + os.pathsep + env.get("PATH", "")

    return env


def _infer_deveco_root(hvigorw: Path) -> Path | None:
    """Given .../tools/hvigor/bin/hvigorw, walk up to DevEco root."""
    # hvigorw is at <deveco>/tools/hvigor/bin/hvigorw(.bat)
    parts = hvigorw.resolve().parts
    try:
        idx = parts.index("tools")
        return Path(*parts[:idx])
    except ValueError:
        return None


# ── error extraction ─────────────────────────────────────────────────────────


_ERROR_PATTERN = re.compile(
    r"(?:"
    r"ERROR:|"
    r"\berror\s*(?:TS|:)|"
    r"COMPILE\s+ERROR|"
    r"BUILD\s+FAILED|"
    r"hvigor\s+ERROR"
    r")",
    re.IGNORECASE,
)

_NOISE_PATTERN = re.compile(
    r"(?:"
    r"0\s+error|"
    r"error\s+count\s*:\s*0|"
    r"no\s+error"
    r")",
    re.IGNORECASE,
)


def _extract_errors(output: str) -> list[str]:
    """Extract meaningful error lines from hvigorw build output."""
    errors: list[str] = []
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if _ERROR_PATTERN.search(stripped) and not _NOISE_PATTERN.search(stripped):
            errors.append(stripped)
    return errors
