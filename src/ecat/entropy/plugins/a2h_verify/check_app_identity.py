"""app_identity —— 应用身份完整性检查（CHECK-4，硬检查）。

确保 AppScope/app.json5 / 资源里不再是 DevEco Studio 工程模板的默认值，
即真正"成为这个 app"——bundle / vendor / 图标 / 名称都来源于 Android 工程。

具体检查项：
  1. bundleName 不能是 com.example.* 模板默认
  2. vendor 不能等于 "example"
  3. versionName 不能等于模板默认 "1.0.0"
  4. app_name（label）与 spec/baseline/feature-index.md 中声明的 D1 应用名一致
  5. foreground.png / background.png 文件大小 > 0.3KB（否则是模板透明占位；
     注意纯色背景层本就只有几百字节，阈值取 0.3KB 以免误报合法纯色图标）
  6. layered_image.json 存在且引用真实 media 资源（否则桌面图标是空白）

适用于任何 HarmonyOS 工程——所有判断都基于 DevEco 模板的固定 magic values。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ecat.entropy.base import EntropyPlugin
from ecat.gradient.schema import WorkItem


class AppIdentityPlugin(EntropyPlugin):
    """Check that app identity fields are properly migrated, not template defaults."""

    name = "app_identity"
    default_weight = 0.05

    def extract_work_items(
        self, score: float, details: dict[str, object]
    ) -> list[WorkItem]:
        items: list[WorkItem] = []
        for fail in details.get("fails", []):
            items.append(WorkItem(
                file=Path("AppScope/app.json5"),
                issues=[str(fail)],
                suggested_skills=[],
                severity="high",
            ))
        for warn in details.get("warns", []):
            items.append(WorkItem(
                file=Path("AppScope/app.json5"),
                issues=[str(warn)],
                suggested_skills=[],
                severity="low",
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

        app_json = harmony_dir / "AppScope" / "app.json5"
        parsed_app = _parse_json5(app_json)

        if parsed_app is None:
            fails.append("AppScope/app.json5 missing or unparseable")
        else:
            app_block = parsed_app.get("app", {})

            # 1. bundleName
            bundle = app_block.get("bundleName", "")
            details["bundleName"] = bundle
            if not bundle or re.match(r"com\.example\.", bundle):
                fails.append(f"bundleName is template default: {bundle!r}")

            # 2. vendor
            vendor = app_block.get("vendor", "")
            details["vendor"] = vendor
            if vendor.lower() == "example" or not vendor:
                fails.append(f"vendor is template default: {vendor!r}")

            # 3. versionName — compare against Android source if available so
            # apps whose Android versionName is genuinely "1.0.0" don't get a
            # permanent 0.05 floor on H. Only warn when (a) we can read the
            # Android version and it differs, or (b) we can't read it and the
            # Harmony side is the DevEco template default.
            version = app_block.get("versionName", "")
            details["versionName"] = version
            android_version = _extract_android_version_name(android_dir)
            if android_version:
                details["android_versionName"] = android_version
                if android_version != version:
                    warns.append(
                        f"versionName out of sync: Harmony={version!r} vs "
                        f"Android={android_version!r}"
                    )
            elif version == "1.0.0":
                warns.append("versionName is 1.0.0 — may not be synced from Android")

        # 4. app_name in string.json + Spec consistency
        string_json = harmony_dir / "AppScope" / "resources" / "base" / "element" / "string.json"
        app_name_value = _check_app_name(string_json, spec_dir, fails, details)
        details["app_name"] = app_name_value

        # 5. Icon file sizes
        media_dir = harmony_dir / "AppScope" / "resources" / "base" / "media"
        for icon_name in ("foreground.png", "background.png"):
            icon_path = media_dir / icon_name
            if not icon_path.exists():
                warns.append(f"{icon_name} not found in AppScope media")
            else:
                size_kb = icon_path.stat().st_size / 1024
                details[f"{icon_name}_size_kb"] = round(size_kb, 1)
                if size_kb <= 0.3:
                    warns.append(f"{icon_name} is {size_kb:.2f}KB — likely default placeholder")

        # 6. layered_image.json
        layered = media_dir / "layered_image.json" if media_dir.exists() else None
        if layered is None or not layered.exists():
            fails.append("layered_image.json missing in AppScope media")
        else:
            try:
                li_data = json.loads(layered.read_text(encoding="utf-8"))
                li_str = json.dumps(li_data)
                if "$media:background" not in li_str or "$media:foreground" not in li_str:
                    fails.append("layered_image.json missing $media:background or $media:foreground reference")
                details["layered_image_valid"] = True
            except Exception:
                fails.append("layered_image.json unparseable")
                details["layered_image_valid"] = False

        # Score: each FAIL = 1.0, each WARN = 0.3, max 6 possible fails
        score = min(1.0, (len(fails) + len(warns) * 0.3) / 6.0)
        details["fails"] = fails
        details["warns"] = warns
        return score, details


def _parse_json5(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
        cleaned = re.sub(r"//.*", "", raw)
        cleaned = re.sub(r"/\*.*?\*/", "", cleaned, flags=re.DOTALL)
        return json.loads(cleaned)  # type: ignore[no-any-return]
    except Exception:
        return None


def _check_app_name(
    string_json: Path,
    spec_dir: Path | None,
    fails: list[str],
    details: dict[str, object],
) -> str:
    if not string_json.exists():
        fails.append("AppScope string.json missing")
        return ""
    try:
        data = json.loads(string_json.read_text(encoding="utf-8"))
        entries = data.get("string", [])
        app_name_entry = next((e for e in entries if e.get("name") == "app_name"), None)
        if app_name_entry is None:
            fails.append("app_name not found in string.json")
            return ""
        app_name_val: str = app_name_entry.get("value", "")
        details["string_names"] = [e.get("name") for e in entries]

        if spec_dir and spec_dir.exists():
            spec_app_name = _extract_app_name_from_spec(spec_dir)
            if spec_app_name and spec_app_name.lower() != app_name_val.lower():
                fails.append(
                    f"app_name mismatch: string.json={app_name_val!r}, spec={spec_app_name!r}"
                )
                details["spec_app_name"] = spec_app_name
        return app_name_val
    except Exception as exc:
        fails.append(f"Could not parse string.json: {exc}")
        return ""


_VERSION_NAME_RE = re.compile(
    r"""versionName\s*[=]?\s*['"]([^'"]+)['"]""",
    re.MULTILINE,
)


def _extract_android_version_name(android_dir: Path | None) -> str:
    """Read versionName from any Gradle build file under android_dir.

    Matches both Groovy DSL (`versionName "1.2.3"`) and Kotlin DSL
    (`versionName = "1.2.3"`). Returns the first match found, empty string if
    no gradle file or no versionName declared.
    """
    if android_dir is None or not android_dir.exists():
        return ""
    for name in ("app/build.gradle", "app/build.gradle.kts",
                 "build.gradle", "build.gradle.kts"):
        candidate = android_dir / name
        if not candidate.is_file():
            continue
        try:
            text = candidate.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        match = _VERSION_NAME_RE.search(text)
        if match:
            return match.group(1).strip()
    return ""


def _extract_app_name_from_spec(spec_dir: Path) -> str:
    """Try to find app name from feature-index.md or ui-manifest.md."""
    for name in ("baseline/feature-index.md", "baseline/ui-manifest.md"):
        candidate = spec_dir / name
        if not candidate.exists():
            continue
        text = candidate.read_text(encoding="utf-8", errors="ignore")
        match = re.search(r"[Aa]pp\s*[Nn]ame\s*[:：]\s*(.+)", text)
        if match:
            return match.group(1).strip()
    return ""
