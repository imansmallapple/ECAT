"""permission_parity —— 权限与能力对账（N4，硬检查）。

把 Android 工程声明的 `<uses-permission>` 逐项映射到 HarmonyOS 等价权限，核对
鸿蒙 `entry/src/main/module.json5` 的 `requestPermissions` 是否都声明了。漏声明
= 对应功能在鸿蒙上运行时会被系统拒绝（壁纸、相机、通知等静默失效）。

判定逻辑：
  1. 扫描 android_dir 下所有 AndroidManifest.xml（排除 build/test），收集
     `android.permission.*` 集合。
  2. 扫描 harmony module.json5，收集 `ohos.permission.*` 集合。
  3. 对每个**有已知 OHOS 等价**的 Android 权限：其等价权限只要有一个被声明即算满足，
     否则记为 missing（FAIL）。无已知等价的 Android 权限只作信息记录，不计分（避免误报）。
  4. score = missing / max(mapped_total, 1)。

无 Android 权限 / 无 module.json5 → 干净 skip，贡献 0。
"""

from __future__ import annotations

import re
from pathlib import Path

from ecat.entropy.base import EntropyPlugin, should_skip_path
from ecat.gradient.schema import WorkItem

# Android 权限 → 可接受的 HarmonyOS 等价权限（任一声明即算满足）。
#
# 反假阳性原则：只收录**确定存在、且普通应用可申请(normal/system_basic 且可动态/
# ACL 授予)**的 OHOS 权限。凡是 OHOS 没有对应物、或对应物是普通应用拿不到的
# system_core 权限（如通知发布、开机自启、长时任务等——硬填映射会导致每个 app 都
# 被误报缺失），一律不收录，按"无等价"处理、不参与计分。多对一映射里给出全部可接受项，
# 任一声明即算满足（如 scoped 媒体权限覆盖安卓的 MANAGE_EXTERNAL_STORAGE 场景）。
_PERM_MAP: dict[str, tuple[str, ...]] = {
    "READ_EXTERNAL_STORAGE": ("READ_IMAGEVIDEO", "READ_MEDIA"),
    "WRITE_EXTERNAL_STORAGE": ("WRITE_IMAGEVIDEO", "WRITE_MEDIA"),
    "MANAGE_EXTERNAL_STORAGE": ("WRITE_IMAGEVIDEO", "WRITE_MEDIA", "READ_IMAGEVIDEO"),
    "MANAGE_MEDIA": ("WRITE_IMAGEVIDEO", "WRITE_MEDIA"),
    "READ_MEDIA_IMAGES": ("READ_IMAGEVIDEO", "READ_MEDIA"),
    "READ_MEDIA_VIDEO": ("READ_IMAGEVIDEO", "READ_MEDIA"),
    "READ_MEDIA_AUDIO": ("READ_AUDIO", "READ_MEDIA"),
    "ACCESS_MEDIA_LOCATION": ("MEDIA_LOCATION",),
    "CAMERA": ("CAMERA",),
    "RECORD_AUDIO": ("MICROPHONE",),
    "INTERNET": ("INTERNET",),
    "ACCESS_NETWORK_STATE": ("GET_NETWORK_INFO",),
    "ACCESS_WIFI_STATE": ("GET_WIFI_INFO",),
    "ACCESS_FINE_LOCATION": ("LOCATION",),
    "ACCESS_COARSE_LOCATION": ("APPROXIMATELY_LOCATION", "LOCATION"),
    "SET_WALLPAPER": ("SET_WALLPAPER",),
    "VIBRATE": ("VIBRATE",),
    "READ_CONTACTS": ("READ_CONTACTS",),
    "WRITE_CONTACTS": ("WRITE_CONTACTS",),
    "READ_CALENDAR": ("READ_CALENDAR",),
    "WRITE_CALENDAR": ("WRITE_CALENDAR",),
    "CALL_PHONE": ("PLACE_CALL",),
    "READ_PHONE_STATE": ("GET_TELEPHONY_STATE",),
}

_ANDROID_PERM_RE = re.compile(r'android\.permission\.([A-Z_]+)')
_OHOS_PERM_RE = re.compile(r'ohos\.permission\.([A-Z_]+)')

# 受保护能力在 Android 源码里的使用痕迹。仅当 Android 确实**用到**该能力，
# "鸿蒙未声明"才算真缺失；只在 manifest 声明、代码却没用的（常见于广告/支付/统计
# SDK 合并进来的权限）→ 归 declared_unused，不计分、不出工单（反软误报）。
# 每个出现在 _PERM_MAP 里的权限都应在此有一条痕迹。
_PERM_USAGE: dict[str, str] = {
    "READ_EXTERNAL_STORAGE": r'MediaStore|getExternalStorage|ExternalStoragePublicDirectory|contentResolver|openInputStream',
    "WRITE_EXTERNAL_STORAGE": r'MediaStore|getExternalStorage|FileOutputStream|openOutputStream|contentResolver',
    "MANAGE_EXTERNAL_STORAGE": r'MediaStore|getExternalStorage|isExternalStorageManager|MANAGE_EXTERNAL_STORAGE',
    "MANAGE_MEDIA": r'MediaStore',
    "READ_MEDIA_IMAGES": r'MediaStore|Images\.Media|contentResolver',
    "READ_MEDIA_VIDEO": r'MediaStore|Video\.Media|contentResolver',
    "READ_MEDIA_AUDIO": r'MediaStore|Audio\.Media|contentResolver',
    "ACCESS_MEDIA_LOCATION": r'ACCESS_MEDIA_LOCATION|setRequireOriginal|ExifInterface',
    "CAMERA": r'\bCamera\b|camera2|takePicture|ACTION_IMAGE_CAPTURE|FEATURE_CAMERA|androidx\.camera',
    "RECORD_AUDIO": r'MediaRecorder|AudioRecord',
    "INTERNET": r'HttpURLConnection|OkHttp|okhttp3|Retrofit|java\.net\.URL|\bSocket\b|Volley|\.openConnection',
    "ACCESS_NETWORK_STATE": r'ConnectivityManager|getActiveNetwork|NetworkCapabilities|NetworkInfo|isConnected',
    "ACCESS_WIFI_STATE": r'WifiManager',
    "ACCESS_FINE_LOCATION": r'LocationManager|FusedLocation|requestLocationUpdates|getLastLocation|LocationRequest',
    "ACCESS_COARSE_LOCATION": r'LocationManager|FusedLocation|requestLocationUpdates|getLastLocation|LocationRequest',
    "SET_WALLPAPER": r'WallpaperManager|setWallpaper',
    "VIBRATE": r'\bVibrator\b|\.vibrate\s*\(|VibrationEffect',
    "READ_CONTACTS": r'ContactsContract',
    "WRITE_CONTACTS": r'ContactsContract',
    "READ_CALENDAR": r'CalendarContract',
    "WRITE_CALENDAR": r'CalendarContract',
    "CALL_PHONE": r'ACTION_CALL|TelecomManager|placeCall',
    "READ_PHONE_STATE": r'TelephonyManager|getDeviceId|getImei|getSubscriberId|getSimSerial',
}


class PermissionParityPlugin(EntropyPlugin):
    """N4: Android uses-permission ↔ HarmonyOS requestPermissions parity."""

    name = "permission_parity"
    default_weight = 0.10

    def extract_work_items(
        self, score: float, details: dict[str, object]
    ) -> list[WorkItem]:
        items: list[WorkItem] = []
        for miss in details.get("missing", []):
            items.append(WorkItem(
                file=Path("entry/src/main/module.json5"),
                issues=[str(miss)],
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
        if android_dir is None or not Path(android_dir).exists():
            return 0.0, {"skipped": "no_android_dir"}

        android_perms = _collect_android_permissions(Path(android_dir))
        if not android_perms:
            return 0.0, {"skipped": "no_android_permissions"}

        ohos_perms, n_module_files = _collect_ohos_permissions(harmony_dir)
        if n_module_files == 0:
            return 0.0, {"skipped": "no_module_json5"}

        mapped: list[str] = []          # android perms that have an OHOS equivalent
        declared: list[str] = []        # mapped + OHOS equivalent declared on harmony
        undeclared: list[str] = []      # mapped but no OHOS equivalent declared
        no_equivalent: list[str] = []   # android perms with no known OHOS mapping
        for ap in sorted(android_perms):
            equivalents = _PERM_MAP.get(ap)
            if not equivalents:
                no_equivalent.append(ap)
                continue
            mapped.append(ap)
            (declared if any(e in ohos_perms for e in equivalents) else undeclared).append(ap)

        # Usage gate (anti-false-positive): an undeclared permission is a REAL gap only
        # if the Android code actually exercises the capability. Permissions merely
        # present in the manifest (commonly merged from bundled SDKs) but never used →
        # declared_unused, excluded from both the score and the work items.
        used = _detect_used_permissions(Path(android_dir), undeclared)
        missing_perms = [ap for ap in undeclared if ap in used]
        declared_unused = [ap for ap in undeclared if ap not in used]

        missing = [
            f"Android android.permission.{ap} has no HarmonyOS equivalent declared "
            f"(expected one of: {', '.join('ohos.permission.' + e for e in _PERM_MAP[ap])})"
            for ap in missing_perms
        ]
        relevant = len(declared) + len(missing_perms)
        score = min(1.0, len(missing) / max(relevant, 1))
        return score, {
            "android_permission_count": len(android_perms),
            "ohos_permission_count": len(ohos_perms),
            "mapped_count": len(mapped),
            "missing_count": len(missing),
            "missing": missing,
            "declared_unused": declared_unused,
            "no_equivalent": no_equivalent,
        }


def _collect_ohos_permissions(harmony_dir: Path) -> tuple[set[str], int]:
    """Union of ohos.permission.* across ALL module.json5 under harmony_dir.

    Scanning every module (entry + HSP/HAR + even ohosTest) is deliberately
    lenient: a permission declared anywhere counts as satisfied, so we never
    falsely flag a permission as missing just because it lives in another module.
    Returns (permission set, number of module.json5 files seen).
    """
    perms: set[str] = set()
    count = 0
    for mj in harmony_dir.rglob("module.json5"):
        if should_skip_path(mj, harmony_dir):
            continue
        count += 1
        try:
            perms.update(_OHOS_PERM_RE.findall(mj.read_text(encoding="utf-8", errors="ignore")))
        except OSError:
            continue
    return perms, count


def _detect_used_permissions(android_dir: Path, candidates: list[str]) -> set[str]:
    """Return the subset of `candidates` whose protected-API usage appears in source.

    Scans .kt/.java once, short-circuiting per permission as soon as its usage marker
    is found. A permission with no usage marker found → treated as unused (so it won't
    be reported missing — avoids flagging SDK-merged, never-exercised permissions)."""
    patterns = {p: re.compile(_PERM_USAGE[p]) for p in candidates if p in _PERM_USAGE}
    found: set[str] = set()
    if not patterns:
        return found
    for f in android_dir.rglob("*"):
        if f.suffix not in (".kt", ".java"):
            continue
        sp = str(f).replace("\\", "/")
        if "/build/" in sp or "/src/test" in sp or "/src/androidTest" in sp:
            continue
        if should_skip_path(f, android_dir):
            continue  # 排除 .gradle/.idea 等点目录及依赖目录
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for p in list(patterns):
            if patterns[p].search(text):
                found.add(p)
                del patterns[p]
        if not patterns:
            break
    return found


def _collect_android_permissions(android_dir: Path) -> set[str]:
    """Union of android.permission.* across all non-build/test AndroidManifest.xml."""
    perms: set[str] = set()
    for mf in android_dir.rglob("AndroidManifest.xml"):
        sp = str(mf).replace("\\", "/")
        if "/build/" in sp or "/src/test" in sp or "/src/androidTest" in sp:
            continue
        try:
            text = mf.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        # only count <uses-permission ...> declarations, not <permission> defs
        for m in re.finditer(r"<uses-permission[^>]*>", text):
            perms.update(_ANDROID_PERM_RE.findall(m.group(0)))
    return perms
