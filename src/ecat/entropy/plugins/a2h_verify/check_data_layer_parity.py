"""data_layer_parity —— 数据层对账（N3，硬检查）。

迁移时界面容易被盯着，看不见的"数据层"却常被悄悄丢掉。本插件拿 Android 的
**数据库表/实体**和**本地设置键**当账本，核对 HarmonyOS 是否都搬齐了——专抓
"界面做好了、数据层缺斤少两"这类编译不报错、一用才露馅的迁移损耗。

v1 覆盖到**表级 + 键级**（字段/列级留 v2）。

防假阳性是第一原则——宁可漏报缺失，绝不误报：
  - 匹配采用"字面子串出现即算已迁"的**宽松存在性**判定（HMOS 语料里出现该名字/键
    即视为已迁），天然偏向漏报，不会把已迁的报成缺失。
  - pref 键**只在引用了 SharedPreferences/PreferenceManager/DataStore 的文件里**采集，
    避免把 Cursor/JSONObject/Bundle 的 `getString("col")` 误当成设置键。
  - 过滤 Room/SQLite 内部表与过短名字。

Android 侧无数据层 → 干净 skip，贡献 0。
"""

from __future__ import annotations

import re
from pathlib import Path

from ecat.entropy.base import EntropyPlugin, should_skip_path
from ecat.gradient.schema import WorkItem

# Android 侧采集
_TABLENAME_RE = re.compile(r'tableName\s*=\s*"([A-Za-z_]\w*)"')
_DB_ENTITIES_RE = re.compile(r'entities\s*=\s*\[([^\]]+)\]')
_ENTITY_CLASS_RE = re.compile(r'(\w+)\s*::\s*class')
_CREATE_TABLE_RE = re.compile(
    r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[`"\']?([A-Za-z_]\w*)', re.IGNORECASE)
# Capture the receiver of a get/put call so we can reject non-preferences receivers
# (Intent/Bundle extras, Cursor columns, JSON fields) that would otherwise look like
# settings keys — e.g. `extras.getInt("fileType")` is NOT a preference key.
_PREF_KEY_RE = re.compile(
    r'(?:(\w+)\s*\.\s*)?'
    r'(?:get|put)(?:Boolean|Int|Long|Float|String|StringSet)\s*\(\s*"([^"]+)"')
_DATASTORE_KEY_RE = re.compile(
    r'(?:boolean|int|long|float|double|string|stringSet|byteArray)PreferencesKey\s*\(\s*"([^"]+)"')

# Receivers whose get/putXxx calls are NOT preference access — exclude their keys.
_NON_PREF_RECEIVERS = frozenset((
    "extras", "intent", "bundle", "arguments", "args", "savedinstancestate",
    "savedstate", "cursor", "json", "jsonobject", "jsonobj", "data", "result",
    "values", "contentvalues", "map", "hashmap", "props", "attrs",
    "getintent", "getarguments", "getextras", "it",
))

# 标记一个文件"确实在做偏好存储"——只有这种文件里的 get/putXxx 才当作 pref 键，
# 否则 Cursor/JSONObject/Bundle 的同名调用会污染键集（假阳性来源）。
_PREFS_FILE_MARKERS = ("SharedPreferences", "PreferenceManager",
                       "DataStore", "preferencesDataStore", "PreferenceFragment")

# Room/SQLite 内部表，永远不算业务表
_INTERNAL_TABLES = frozenset((
    "sqlite_sequence", "room_master_table", "android_metadata",
    "room_table_modification_log",
))

_HMOS_SUFFIXES = (".ets", ".ts")


class DataLayerParityPlugin(EntropyPlugin):
    """N3: Android DB tables/entities + preference keys ↔ HarmonyOS data layer."""

    name = "data_layer_parity"
    default_weight = 0.10

    def extract_work_items(
        self, score: float, details: dict[str, object]
    ) -> list[WorkItem]:
        items: list[WorkItem] = []
        for t in details.get("missing_tables", []):
            items.append(WorkItem(
                file=Path("."),
                issues=[f"data_layer: Android data table/entity '{t}' has no "
                        f"HarmonyOS counterpart (RDB table / model missing)"],
                suggested_skills=[],
                severity="high",
            ))
        missing_keys = list(details.get("missing_pref_keys", []))
        if missing_keys:
            preview = ", ".join(missing_keys[:15]) + (" …" if len(missing_keys) > 15 else "")
            items.append(WorkItem(
                file=Path("."),
                issues=[f"data_layer: {len(missing_keys)} Android preference key(s) "
                        f"not found on HarmonyOS side ({preview})"],
                suggested_skills=[],
                severity="medium",
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

        tables, pref_keys = _collect_android_data_layer(Path(android_dir))
        if not tables and not pref_keys:
            return 0.0, {"skipped": "no_android_data_layer"}

        corpus = _harmony_corpus(harmony_dir)
        if not corpus:
            # 没有鸿蒙源可比对 → 不臆断，干净 skip
            return 0.0, {"skipped": "no_harmony_sources"}

        missing_tables = sorted(t for t in tables if t.lower() not in corpus)
        missing_keys = sorted(k for k in pref_keys if k.lower() not in corpus)

        t_ratio = (len(missing_tables) / len(tables)) if tables else None
        k_ratio = (len(missing_keys) / len(pref_keys)) if pref_keys else None
        if t_ratio is not None and k_ratio is not None:
            score = 0.6 * t_ratio + 0.4 * k_ratio
        else:
            score = t_ratio if t_ratio is not None else (k_ratio or 0.0)
        score = max(0.0, min(1.0, score))

        return score, {
            "android_table_count": len(tables),
            "android_pref_key_count": len(pref_keys),
            "missing_table_count": len(missing_tables),
            "missing_pref_key_count": len(missing_keys),
            "missing_tables": missing_tables,
            "missing_pref_keys": missing_keys[:50],  # cap payload
        }


def _collect_android_data_layer(android_dir: Path) -> tuple[set[str], set[str]]:
    """Return (table/entity names, preference keys) from Android sources.

    Tables come from any source file (Room annotations + raw SQL). Pref keys are
    collected ONLY from files that actually reference a preferences API, so that
    Cursor/JSON/Bundle `getString("…")` calls don't masquerade as settings keys.
    """
    tables: set[str] = set()
    pref_keys: set[str] = set()
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
        tables.update(_TABLENAME_RE.findall(text))
        tables.update(_CREATE_TABLE_RE.findall(text))
        for block in _DB_ENTITIES_RE.findall(text):
            tables.update(_ENTITY_CLASS_RE.findall(block))
        # pref keys: only from genuine preferences files AND only get/put calls whose
        # receiver isn't a known non-pref object (Intent/Bundle/Cursor/JSON) — both
        # guards together keep `extras.getInt("x")` etc. out of the key set.
        if any(m in text for m in _PREFS_FILE_MARKERS):
            for recv, key in _PREF_KEY_RE.findall(text):
                if recv and recv.lower() in _NON_PREF_RECEIVERS:
                    continue
                pref_keys.add(key)
        pref_keys.update(_DATASTORE_KEY_RE.findall(text))  # DataStore keys are unambiguous
    tables = {t for t in tables
              if len(t) >= 3 and t.lower() not in _INTERNAL_TABLES}
    pref_keys = {k for k in pref_keys if len(k) >= 3}
    return tables, pref_keys


def _harmony_corpus(harmony_dir: Path) -> str:
    """Lower-cased concatenation of all HarmonyOS .ets/.ts source (excl deps/build)."""
    parts: list[str] = []
    for f in harmony_dir.rglob("*"):
        if f.suffix not in _HMOS_SUFFIXES or should_skip_path(f, harmony_dir):
            continue
        try:
            parts.append(f.read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            continue
    return "\n".join(parts).lower()
