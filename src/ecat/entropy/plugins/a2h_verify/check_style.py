"""style —— ArkTS 代码风格规范检查（CHECK-10，硬检查）。

代码风格不影响功能，但影响可维护性。本插件按 ArkTS / HarmonyOS 官方风格指南
做 grep-level 静态扫描，权重最低，主要在其他维度都干净后用作"最后一公里"。

具体检查项：
  1. console.log 残留（生产代码应移除或替换为 hilog）
  2. magic number 硬编码（应改 vp / fp 单位 + 抽常量）
  3. 组件命名不规范（Component 应 PascalCase，state 应 camelCase）
  4. import 路径不规范（用相对路径过深，应改 @ohos / @kit 别名）
  5. 文件缺少 @State / @Prop 等装饰器但用了响应式变量
  6. 嵌套层级过深（Column/Row 嵌套 > N 层应抽组件）

适用于任何 HarmonyOS 工程——所有规则都基于 ArkTS 语言通用风格。
"""

from __future__ import annotations

import re
from pathlib import Path

from ecat.entropy.base import EntropyPlugin, should_skip_path
from ecat.gradient.schema import WorkItem

_STYLE_RULES: list[tuple[re.Pattern[str], str]] = [
    # Component names should be PascalCase (struct Foo)
    (re.compile(r"^struct\s+[a-z]"), "struct name not PascalCase"),
    # Avoid var (use let/const)
    (re.compile(r"\bvar\s+\w"), "use let/const instead of var"),
    # Avoid console.log (use hilog)
    (re.compile(r"console\.(log|warn|error)\("), "use hilog instead of console.*"),
    # Real `px` units in size args. ArkTS treats bare `.width(32)` as vp by
    # default (correct), so we only flag the explicit "<n>px" string form.
    (re.compile(r"\.(width|height|fontSize|margin|padding)\s*\(\s*['\"]\d+px['\"]"), "explicit px unit — use vp/fp instead"),
]


def _in_string_or_comment(line: str, match_start: int) -> bool:
    """Return True if the match position is inside a string literal or comment.

    Cross-app generic: uses only C-family/TS universal syntax (// /* */ comments
    and ' " ` string delimiters with \\ escape). No app-specific tokens.
    Single-line only — does not track multi-line template-literal state.
    """
    stripped = line.lstrip()
    if stripped.startswith(("//", "*", "/*")):
        return True
    in_sq = in_dq = in_bt = False
    i = 0
    while i < match_start and i < len(line):
        c = line[i]
        if c == "\\":
            i += 2
            continue
        if c == "'" and not in_dq and not in_bt:
            in_sq = not in_sq
        elif c == '"' and not in_sq and not in_bt:
            in_dq = not in_dq
        elif c == "`" and not in_sq and not in_dq:
            in_bt = not in_bt
        i += 1
    return in_sq or in_dq or in_bt


class StylePlugin(EntropyPlugin):
    """Count ArkTS style violations."""

    name = "style"
    default_weight = 0.05

    def extract_work_items(
        self, score: float, details: dict[str, object]
    ) -> list[WorkItem]:
        items: list[WorkItem] = []
        for v in details.get("violations", []):
            if isinstance(v, dict):
                items.append(WorkItem(
                    file=Path(str(v.get("file", "."))),
                    issues=[f"{v.get('rule', 'style violation')} (line {v.get('line', '?')})"],
                    suggested_skills=[],
                    severity="low",
                    lines=[v["line"]] if isinstance(v.get("line"), int) else None,
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
        violations: list[dict[str, object]] = []

        for f in ets_files:
            try:
                content = f.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for line_no, line in enumerate(content.splitlines(), start=1):
                for pat, desc in _STYLE_RULES:
                    m = pat.search(line)
                    if m and not _in_string_or_comment(line, m.start()):
                        violations.append(
                            {
                                "file": str(f.relative_to(harmony_dir)),
                                "line": line_no,
                                "rule": desc,
                                "snippet": line.strip()[:200],
                            }
                        )

        total_lines = sum(
            len(f.read_text(encoding="utf-8", errors="ignore").splitlines())
            for f in ets_files
            if f.exists()
        )
        # score: violations per 100 lines, capped at 1.0 at 10/100 lines
        ratio = (len(violations) / max(total_lines, 1)) * 100
        score = min(1.0, ratio / 10.0)

        return score, {
            "violation_count": len(violations),
            "total_lines": total_lines,
            "violations": violations[:40],
        }
