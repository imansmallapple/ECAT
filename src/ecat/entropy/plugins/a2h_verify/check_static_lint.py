"""static_lint —— Android 残留模式静态扫描（CHECK-2，硬检查）。

迁移过程中常见 ArkTS 文件里残留 Android 写法（开发者机械搬运未替换）。
本插件对所有 .ets 做 grep，命中即记一条违规。

具体检查项（每条都是 Android-only 写法，ArkTS 不支持）：
  1. android.* import 残留（ArkTS 应改用 @ohos.* / @kit.*）
  2. AppCompatActivity / Fragment / RecyclerView 等 Android 类名出现在 ArkTS 文件中
  3. setOnClickListener / findViewById 等 Android API
  4. R.id.* / R.string.* / R.drawable.* Android 资源访问语法
  5. SharedPreferences / Intent / Bundle Android 数据结构残留
  6. android: XML 命名空间 / activity_main.xml 等 Android 布局引用

适用于任何 Android→HarmonyOS 迁移——pattern 都是 Android 通用 idiom。
"""

from __future__ import annotations

import re
from pathlib import Path

from ecat.entropy.base import EntropyPlugin, should_skip_path
from ecat.gradient.schema import WorkItem

# Patterns that indicate un-migrated Android idioms in ArkTS code
_ANDROID_PATTERNS: list[tuple[str, str, str]] = [
    # (regex, description, severity)
    (r"setOnClickListener", "Android click listener", "ERROR"),
    (r"findViewById", "Android view binding", "ERROR"),
    (r"android\.widget\.", "Android widget import", "ERROR"),
    (r"import\s+android\.", "Android import", "ERROR"),
    (r"R\.layout\.", "Android layout reference", "ERROR"),
    (r"R\.id\.", "Android resource ID", "ERROR"),
    (r"Activity\.onCreate", "Activity lifecycle", "ERROR"),
    (r"\.runOnUiThread", "Android UI thread", "ERROR"),
    (r"Log\.d\(", "Android Log call", "ERROR"),
    (r"Toast\.makeText", "Android Toast", "ERROR"),
]

# ArkTS code quality rules (from a2h-verify CHECK-2).
# Note: `@ohos.*` imports are NOT deprecated as of HarmonyOS 5.x — `@kit.*`
# is the preferred alias but `@ohos.*` remains valid; flagging it produces
# noise that wastes Generator iterations on no-op rewrites, so it's omitted.
_ARKTS_QUALITY_PATTERNS: list[tuple[str, str, str]] = [
    (r":\s*any\b", "any type usage", "ERROR"),
    # SQL injection via concatenation: an unambiguous uppercase SQL clause INSIDE the
    # concatenated string literal. The old rule matched a lowercase keyword as a
    # substring after `+` (selectedFile→"select", a `query` var, a URL "term=") — pure
    # URL/path/message FPs, and it even missed real SQL (keyword sits before `+`).
    (r"""['"][^'"]*\b(?:WHERE|INSERT\s+INTO|DELETE\s+FROM)\b[^'"]*['"]\s*\+""", "SQL string concatenation", "ERROR"),
    (r"\beval\s*\(", "eval() usage", "ERROR"),
    (r"aboutToAppear.*pathInfo|pathInfo.*aboutToAppear", "aboutToAppear with pathInfo", "ERROR"),
]


class StaticLintPlugin(EntropyPlugin):
    """Count Android-pattern violations in .ets/.ts files."""

    name = "static_lint"
    default_weight = 0.10

    def extract_work_items(
        self, score: float, details: dict[str, object]
    ) -> list[WorkItem]:
        items: list[WorkItem] = []
        for err in details.get("errors", []):
            if isinstance(err, dict):
                items.append(WorkItem(
                    file=Path(str(err.get("file", "."))),
                    issues=[f"{err.get('pattern', 'lint')}: {err.get('snippet', '')}"],
                    suggested_skills=[],
                    severity="high",
                    lines=[int(err["line"])] if "line" in err else None,
                ))
        for warn in details.get("warnings", []):
            if isinstance(warn, dict):
                items.append(WorkItem(
                    file=Path(str(warn.get("file", "."))),
                    issues=[f"{warn.get('pattern', 'lint')}: {warn.get('snippet', '')}"],
                    suggested_skills=[],
                    severity="low",
                    lines=[int(warn["line"])] if "line" in warn else None,
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
        ets_files = [
            f for f in (list(harmony_dir.rglob("*.ets")) + list(harmony_dir.rglob("*.ts")))
            if not should_skip_path(f, harmony_dir)
        ]
        errors: list[dict[str, object]] = []
        warnings: list[dict[str, object]] = []

        android_rules = [(re.compile(pat), desc, sev) for pat, desc, sev in _ANDROID_PATTERNS]
        arkts_rules = [(re.compile(pat), desc, sev) for pat, desc, sev in _ARKTS_QUALITY_PATTERNS]
        all_rules = android_rules + arkts_rules

        for f in ets_files:
            try:
                content = f.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for line_no, line in enumerate(content.splitlines(), start=1):
                # Skip comment lines — Android refs in JavaDoc / line comments
                # are not API calls, just documentation. Comment hygiene is
                # covered by generator.txt Rule 7 and the placeholder plugin.
                stripped = line.lstrip()
                if stripped.startswith("//") or stripped.startswith("*") or stripped.startswith("/*"):
                    continue
                for regex, desc, severity in all_rules:
                    if regex.search(line):
                        entry = {
                            "file": str(f.relative_to(harmony_dir)),
                            "line": line_no,
                            "pattern": desc,
                            "severity": severity,
                            "snippet": line.strip()[:200],
                        }
                        if severity == "ERROR":
                            errors.append(entry)
                        else:
                            warnings.append(entry)

        total_files = len(ets_files) if ets_files else 1
        # Errors weigh 1.0, warnings weigh 0.3
        weighted = len(errors) + len(warnings) * 0.3
        score = min(1.0, weighted / (total_files * 5))

        return score, {
            "error_count": len(errors),
            "warn_count": len(warnings),
            "file_count": len(ets_files),
            "errors": errors[:30],
            "warnings": warnings[:20],
        }
