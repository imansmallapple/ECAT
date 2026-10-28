"""hallucination —— 幻觉功能检测（精度方向，硬+agentic 复合）。

现有插件都查"安卓有、鸿蒙缺"（召回方向）；本插件相反，查"鸿蒙有、安卓无"——
生成器凭空造出的、安卓源里根本没有的用户可见功能（多余菜单项 / 对话框 / 开关）。

两层：
  硬层（廉价、高精度、低召回的初筛）：
    1. 收集鸿蒙 `$r('app.string.X')` 实际引用到的用户可见文案
    2. 收集安卓所有 values*/strings.xml 的文案，构建"词汇表"
    3. 鸿蒙文案里若出现一个安卓任何字符串都从未用过的实义词（如 spam/splash/
       batch/contact），判为候选幻觉——这是个强信号：安卓连这个词都没出现过
    4. 豁免平台/系统文案（权限、加载、系统能力）
  agentic 层（主力，定真假 + 补召回）：
    judge 对候选逐个反搜整个安卓仓确认确无出处，并自行发现无文案的幻觉，
    排除"安卓以不同名字/位置存在"和"平台适配/UI 增强"的假阳性。

适用于任何 Android→HarmonyOS 迁移——硬层只用语言级 idiom（文案词汇比对），
无任何 app 专属字眼；判定靠 agentic judge，宁可漏报不可误报。
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from ecat.entropy.base import EntropyPlugin
from ecat.gradient.schema import WorkItem

log = logging.getLogger(__name__)

# Generic English stopwords + universal UI-chrome verbs. A token in this set is
# never treated as "distinctive" — its presence/absence proves nothing. Purely
# language-level / cross-app UI chrome; NO domain or app-specific nouns (those
# are handled by the Android vocabulary check, which is app-derived).
_STOPWORDS = frozenset((
    "the", "and", "for", "are", "you", "your", "this", "that", "with", "from",
    "will", "can", "not", "all", "any", "has", "have", "use", "used",
    "into", "out", "off", "set", "get", "new", "old", "now", "see", "via",
    "cancel", "save", "delete", "done", "close", "back", "edit", "share",
    "add", "remove", "rename", "copy", "move", "select", "search", "settings",
    "loading", "error", "retry", "empty", "none", "more", "open", "show", "hide",
))

# Platform / system text the migration legitimately introduces — never a
# "feature". HarmonyOS-generic terms only (no app names).
_PLATFORM_RE = re.compile(
    r"permission|权限|授权|grant|denied|loading|加载|please\s*wait|"
    r"\bsys\b|system|initializ|ohos|arkui|navdestination|navpathstack|"
    r"unsupported|not\s*support|unavailable|网络|network\s*error",
    re.IGNORECASE,
)
# Resource-name suffixes/markers that, by universal convention, do NOT name a
# triggerable feature: explanatory prose (help/body/changelog) and runtime
# STATUS/RESULT text (toasts like "saved", "failed", "processing"). A
# hallucinated *feature* shows up as a short button/menu/toggle label, so we
# mine only those. Cross-app generic naming convention, no app tokens.
_PROSE_KEY_RE = re.compile(
    r"_(text|message|msg|desc|description|hint|summary|warning|disclaimer|"
    r"body|note|tip|info|faq|"
    r"success|failed|fail|done|complete|completed|processing|saving|saved|"
    r"created|applied|updated|deleted|removed|emptied|disabled|enabled|"
    r"incorrect|invalid|empty|loading)$|"
    r"faq|changelog|license|policy|copyright|warranty|privacy|whats_new|terms",
    re.IGNORECASE,
)
# A feature label is short; prose is long. Cap distinctive-ish token count.
_MAX_LABEL_TOKENS = 6

_STRING_REF_RE = re.compile(r"\$r\s*\(\s*['\"]app\.string\.(\w+)['\"]")
_ANDROID_STRING_RE = re.compile(
    r"<string\s+name=\"([^\"]+)\"[^>]*>(.*?)</string>", re.DOTALL
)
_PLACEHOLDER_RE = re.compile(r"%\d*\$?[sd]|\{\d+\}|\$\{?\w+\}?")
_NONWORD_RE = re.compile(r"[^a-z0-9一-鿿]+")


def _normalize(text: str) -> str:
    """Lowercase, drop format placeholders + punctuation, collapse whitespace."""
    text = _PLACEHOLDER_RE.sub(" ", text.lower())
    text = _NONWORD_RE.sub(" ", text)
    return " ".join(text.split())


def _sig_tokens(norm: str) -> list[str]:
    """Distinctive tokens: length >= 4 and not a generic stopword."""
    return [t for t in norm.split() if len(t) >= 4 and t not in _STOPWORDS]


class HallucinationPlugin(EntropyPlugin):
    """Detect HarmonyOS user-facing features with no Android origin."""

    name = "hallucination"
    default_weight = 0.05
    judge_mode = "agentic"
    hard_weight = 0.0  # candidates are noisy hints; the judge owns the score
    default_fix_skill = ""
    judge_prompt = """\
Detect HALLUCINATED features — user-facing functionality in this HarmonyOS repo \
with NO origin in the Android app it was ported from.

`details.candidates` are HarmonyOS UI strings using a word the Android strings \
never use — HINTS, not proof. Android source root: `details.android_dir` (it may \
also pull shared library modules not vendored there).

Evaluate ONLY `details.candidates` — never flag a feature not in that list. For each: \
confirm the string is really referenced and wired to a control in `file` (skip if not \
— prefer a miss), then search the WHOLE Android repo by mechanism/API name, not just \
the label. Flag ONLY if real in code AND no Android origin exists.

Do NOT flag: a feature Android has under a different name/key/file; one likely \
provided by a DECLARED DEPENDENCY not vendored here (scan build.gradle / *.toml for \
shared/common library deps — they implement many features); platform adaptations \
(system symbols, NavPathStack, permission flows, system-capability APIs); \
UI/navigation affordances that add no new user action; renamed equivalents. \
If unsure an origin exists, do NOT flag.

score 0.0 (none) to 1.0 (many invented). Each issue: \
`path/Page.ets: remove <feature> — no Android origin (searched <what>), or confirm intentional`."""

    def extract_work_items(self, score: float, details: dict[str, object]) -> list[WorkItem]:
        # Confirmed hallucinations come from the agentic judge's issues, which the
        # generic verdict→work_item path turns into tasks. The hard candidates are
        # unconfirmed hints, so we emit nothing here (FP-averse).
        return []

    def evaluate(
        self,
        harmony_dir: Path,
        *,
        android_dir: Path | None = None,
        spec_dir: Path | None = None,
        config: dict[str, object] | None = None,
    ) -> tuple[float, dict[str, object]]:
        if android_dir is None or not android_dir.exists():
            return 0.0, {"skipped": "android_dir not provided"}

        ets_root = harmony_dir / "entry" / "src" / "main" / "ets"
        string_json = (
            harmony_dir / "entry" / "src" / "main"
            / "resources" / "base" / "element" / "string.json"
        )
        if not ets_root.exists() or not string_json.exists():
            return 0.0, {"skipped": "harmony ets/string.json missing"}

        # 1. Harmony string name → value.
        try:
            data = json.loads(string_json.read_text(encoding="utf-8", errors="ignore"))
            hm_strings = {
                str(s["name"]): str(s.get("value", ""))
                for s in data.get("string", []) if isinstance(s, dict) and s.get("name")
            }
        except (json.JSONDecodeError, OSError):
            return 0.0, {"skipped": "string.json unreadable"}

        # 2. Which strings are actually referenced (user-visible), + one anchor file.
        ref_anchor: dict[str, tuple[str, int]] = {}
        for f in ets_root.rglob("*.ets"):
            try:
                text = f.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for m in _STRING_REF_RE.finditer(text):
                key = m.group(1)
                if key not in ref_anchor:
                    rel = f.relative_to(harmony_dir).as_posix()
                    ref_anchor[key] = (rel, text[: m.start()].count("\n") + 1)

        # 3. Android string corpus → token vocabulary (all locales/variants ⇒
        #    biggest corpus ⇒ fewest false "Harmony-only" flags).
        android_vocab: set[str] = set()
        for sx in android_dir.rglob("strings.xml"):
            if not sx.parent.name.startswith("values"):
                continue
            try:
                raw = sx.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for m in _ANDROID_STRING_RE.finditer(raw):
                android_vocab.update(_normalize(m.group(2)).split())
        if not android_vocab:
            return 0.0, {"skipped": "no Android strings.xml found"}

        # 4. A referenced Harmony string is a candidate when it carries a
        #    distinctive token Android never uses anywhere.
        candidates: list[dict[str, object]] = []
        for key, (rel, line) in ref_anchor.items():
            value = hm_strings.get(key, "")
            if not value or _PROSE_KEY_RE.search(key) or _PLATFORM_RE.search(f"{key} {value}"):
                continue
            norm = _normalize(value)
            if len(norm.split()) > _MAX_LABEL_TOKENS:  # prose, not a feature label
                continue
            toks = _sig_tokens(norm)
            novel = [t for t in toks if t not in android_vocab]
            if toks and novel:
                candidates.append({
                    "key": key, "text": value[:80], "file": rel,
                    "line": line, "novel_terms": novel[:4],
                })

        # Most-novel first; cap so details fit the judge prompt budget.
        candidates.sort(key=lambda c: -len(c["novel_terms"]))  # type: ignore[arg-type]
        total = len(candidates)
        details: dict[str, object] = {
            "candidates": candidates,
            "android_dir": str(android_dir.resolve()),
        }
        # Candidates are a NOISY, recall-oriented hint (string novelty over-fires
        # when the Android side pulls an unvendored shared library). hard_weight
        # is 0, so this score only surfaces in hard-only mode — keep it a weak,
        # never-dominant "worth a judge" signal. The agentic judge owns the real
        # score and enumerates Harmony-only actions independently of this list.
        score = min(0.25, total * 0.005)
        return score, details
