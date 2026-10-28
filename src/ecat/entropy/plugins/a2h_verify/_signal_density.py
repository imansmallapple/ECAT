"""signal_density —— 共享 helper：用语言级 idiom 衡量"功能完整度"。

行数比例不可靠 —— ArkTS 声明式 UI（@State / $r / build()）天然比 Kotlin/Java
紧凑 2-4 倍，单纯按行数会把"真实迁移完成"误判为 stub。

本模块按 Android 和 ArkTS 共有的 6 类语言 idiom 计数，每类做
``min(1.0, arkts_count / android_count)`` 然后求平均，得到 0.0-1.0 的
"功能密度"。这是比行数更稳健的完整度信号。

适用于任何 Android→HarmonyOS 迁移 —— 信号清单完全是语言级 idiom，
不依赖任何 app-specific 字符串。
"""

from __future__ import annotations

import re
from pathlib import Path

# Each signal is a (android_regex, arkts_regex) pair where both sides count
# functionally equivalent constructs. Add more pairs only when both sides
# actually map 1:1 — false equivalence will skew the density.
_SIGNAL_PAIRS: list[tuple[str, str, str]] = [
    # name, Android pattern, ArkTS pattern
    (
        "click_handler",
        # setOnClickListener / onClick = { ... } / setOnLongClickListener
        r"\b(setOn(?:Click|LongClick|Item(?:Click|LongClick))Listener|onClick\s*=)",
        # .onClick( / onClick: { / onTouch( / onLongPress(
        r"\.(onClick|onChange|onSubmit|onTouch|onLongPress|onItemClick)\s*\(",
    ),
    (
        "state_var",
        # private var / val ... by mutableStateOf / @JvmField var
        r"\b(private\s+(?:lateinit\s+)?var\b|by\s+mutableStateOf\b|@JvmField\s+var\b)",
        # @State / @Prop / @Link / @StorageLink / @ObjectLink / @Observed
        r"@(State|Prop|Link|StorageLink|ObjectLink|Observed|Provide|Consume)\b",
    ),
    (
        "list_or_menu_item",
        # addPreference / adapter.add / private fun setupXxx() / R.menu.* item
        r"\b(addPreference\s*\(|adapter\.add\s*\(|private\s+fun\s+setup\w+\s*\(|menu\.add\s*\(|<item\s+android:)",
        # ListItem( / @Builder fn / MenuItem( / TabContent(
        r"\b(ListItem|TabContent|MenuItem)\s*\(|^\s*@Builder\b",
    ),
    (
        "string_resource",
        # R.string.* / getString(R.string.*) / context.getString(
        r"\bR\.string\.\w+|getString\s*\(\s*R\.string\.",
        # $r('app.string.* / $r("app.string.*
        r"\$r\s*\(\s*['\"]app\.string\.\w+",
    ),
    (
        "media_resource",
        # R.drawable.* / R.mipmap.* / R.id.*_image
        r"\bR\.(drawable|mipmap)\.\w+",
        # $r('app.media.* / $r('sys.media.*
        r"\$r\s*\(\s*['\"](?:app|sys)\.media\.\w+",
    ),
    (
        "navigation_jump",
        # Intent(this, X.class) / startActivity( / startActivityForResult(
        r"\b(Intent\s*\([^)]*::class|startActivity(?:ForResult)?\s*\(|navigateUp\s*\()",
        # HMOS NEXT NavPathStack API + legacy router. Standard SDK names —
        # generalizes to any HarmonyOS project.
        r"\b("
        r"router\.(?:pushUrl|replaceUrl|push|back)|"
        r"navigateTo\w*\s*\(|"
        r"(?:pushPath|replacePath|popTo|pushDestination)(?:ByName)?\s*\(|"
        r"navPathStack\.\w+\s*\("
        r")",
    ),
    (
        "dialog_or_toast",
        # AlertDialog.Builder / Toast.makeText / DialogFragment / showDialog
        r"\b(AlertDialog\.Builder\b|Toast\.makeText\s*\(|DialogFragment\b|showDialog\s*\()",
        # AlertDialog.show / CustomDialog / promptAction.showToast
        r"\b(AlertDialog\.show|CustomDialog\b|promptAction\.\w+|ShowDialog\b)",
    ),
]

_REGEX_CACHE: dict[tuple[str, str], tuple[re.Pattern[str], re.Pattern[str]]] = {}


def _compile_pair(android_pat: str, arkts_pat: str) -> tuple[re.Pattern[str], re.Pattern[str]]:
    key = (android_pat, arkts_pat)
    if key not in _REGEX_CACHE:
        _REGEX_CACHE[key] = (
            re.compile(android_pat, re.MULTILINE),
            re.compile(arkts_pat, re.MULTILINE),
        )
    return _REGEX_CACHE[key]


def count_signals(android_text: str, arkts_text: str) -> dict[str, dict[str, int | float]]:
    """Count each (Android, ArkTS) idiom pair and compute per-signal ratio.

    Returns mapping ``signal_name -> {"android": N, "arkts": M, "ratio": min(1, M/N)}``.
    Signals where Android count is 0 are excluded (no anchor for comparison).
    """
    out: dict[str, dict[str, int | float]] = {}
    for name, a_pat, h_pat in _SIGNAL_PAIRS:
        a_re, h_re = _compile_pair(a_pat, h_pat)
        a_count = len(a_re.findall(android_text))
        h_count = len(h_re.findall(arkts_text))
        if a_count == 0:
            continue
        ratio = min(1.0, h_count / a_count)
        out[name] = {"android": a_count, "arkts": h_count, "ratio": ratio}
    return out


def density_score(
    android_path: Path | None,
    arkts_path: Path | None,
) -> tuple[float, dict[str, dict[str, int | float]]]:
    """Compute functional density between an Android source and an ArkTS file.

    Returns ``(density, per_signal_breakdown)`` where density is the mean of
    per-signal ratios (0.0 = ArkTS implements none of Android's features,
    1.0 = ArkTS matches or exceeds every signal). Returns (1.0, {}) when
    either side is unreadable — caller should treat absence of data as
    "no penalty".
    """
    if not android_path or not arkts_path:
        return 1.0, {}
    if not android_path.exists() or not arkts_path.exists():
        return 1.0, {}
    try:
        android_text = android_path.read_text(encoding="utf-8", errors="ignore")
        arkts_text = arkts_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return 1.0, {}
    signals = count_signals(android_text, arkts_text)
    if not signals:
        return 1.0, {}
    mean_ratio = sum(float(s["ratio"]) for s in signals.values()) / len(signals)
    return mean_ratio, signals


def combined_completeness(
    line_ratio: float | None,
    density: float,
    *,
    line_weight: float = 0.4,
    density_weight: float = 0.6,
) -> float:
    """Blend line-count ratio with signal density into a single completeness score.

    Density is weighted higher because it directly measures *functional*
    coverage rather than just *physical* size. When line_ratio is unknown
    the result equals density alone.
    """
    if line_ratio is None:
        return density
    line_ratio = max(0.0, min(1.0, line_ratio))
    density = max(0.0, min(1.0, density))
    return line_ratio * line_weight + density * density_weight
