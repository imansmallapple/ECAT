"""ReportSummary — generate markdown run report with fix details."""

from __future__ import annotations

import json
from pathlib import Path

from ecat.entropy.base import EntropyReport


class ReportSummary:
    """Generates a markdown report from run artifacts."""

    def __init__(self, runs_dir: Path, charts_dir: Path) -> None:
        self.runs_dir = runs_dir
        self.charts_dir = charts_dir

    def generate(self, reports: list[EntropyReport]) -> str:
        lines: list[str] = ["# ECAT Run Report\n"]

        if not reports:
            lines.append("_No iterations recorded._\n")
            return "\n".join(lines)

        first_H = reports[0].total_H
        last_H = reports[-1].total_H
        delta = last_H - first_H

        lines += [
            "## Summary\n",
            f"- **Iterations:** {len(reports)}",
            f"- **Initial H:** {first_H:.4f}",
            f"- **Final H:** {last_H:.4f}",
            f"- **ΔH:** {delta:+.4f}",
            f"- **Improvement:** {abs(delta) / max(first_H, 0.001) * 100:.1f}%\n",
        ]

        # Dimension comparison: first vs last
        lines += self._dimension_comparison(reports[0], reports[-1])

        # Per-iteration details
        lines += ["\n## Iteration Details\n"]
        for i, r in enumerate(reports):
            prev = reports[i - 1] if i > 0 else None
            lines += self._iteration_section(r, prev)

        # Charts
        lines += [
            "\n## Charts\n",
            "![Entropy Trend](charts/entropy_trend.png)",
            "![Dimension Breakdown](charts/dimension_breakdown.png)\n",
        ]

        return "\n".join(lines) + "\n"

    def _dimension_comparison(
        self, first: EntropyReport, last: EntropyReport
    ) -> list[str]:
        """Table comparing dimension scores between first and last iteration."""
        lines = [
            "\n## Dimension Progress\n",
            "| Dimension | Initial | Final | Δ | Status |",
            "|-----------|---------|-------|---|--------|",
        ]
        all_dims = sorted(
            set(first.breakdown) | set(last.breakdown),
            key=lambda d: -(first.breakdown.get(d, 0)),
        )
        for dim in all_dims:
            s0 = first.breakdown.get(dim, 0.0)
            s1 = last.breakdown.get(dim, 0.0)
            d = s1 - s0
            if s1 == 0:
                status = "PASS"
            elif d < -0.05:
                status = "improved"
            elif d > 0.05:
                status = "regressed"
            else:
                status = "unchanged"
            lines.append(f"| {dim} | {s0:.3f} | {s1:.3f} | {d:+.3f} | {status} |")
        return lines

    def _iteration_section(
        self, report: EntropyReport, prev: EntropyReport | None
    ) -> list[str]:
        """Generate a section for one iteration including gradient info."""
        n = report.iteration
        delta_str = ""
        if prev:
            d = report.total_H - prev.total_H
            delta_str = f" ({d:+.4f})"

        worst = max(report.breakdown, key=report.breakdown.__getitem__, default="—")
        lines = [
            f"### Iteration {n}\n",
            f"- **H:** {report.total_H:.4f}{delta_str}",
            f"- **Worst:** {worst} ({report.breakdown.get(worst, 0):.3f})",
        ]

        # Read gradient summary from artifacts
        gradient_file = self.runs_dir / str(n) / "gradient.json"
        if gradient_file.exists():
            try:
                gdata = json.loads(gradient_file.read_text(encoding="utf-8"))
                summary = gdata.get("summary", "")
                if summary:
                    lines.append(f"- **Gradient:** {summary}")
                work_list = gdata.get("work_list", [])
                if work_list:
                    lines.append(f"- **Work items:** {len(work_list)}")
                    for item in work_list[:5]:
                        sev = item.get("severity", "?")
                        fname = item.get("file", "?")
                        issues = item.get("issues", [])
                        issue_str = "; ".join(issues[:2])
                        lines.append(f"  - [{sev}] `{fname}` — {issue_str}")
                    if len(work_list) > 5:
                        lines.append(f"  - ... and {len(work_list) - 5} more")
            except (json.JSONDecodeError, OSError):
                pass

        # Check if diff exists
        diff_file = self.runs_dir / str(n) / "diff.patch"
        if diff_file.exists():
            diff_content = diff_file.read_text(encoding="utf-8")
            if diff_content.strip():
                file_changes = _count_diff_files(diff_content)
                lines.append(f"- **Files changed:** {file_changes}")

        lines.append("")
        return lines

    def write(
        self, reports: list[EntropyReport], output_path: Path | None = None
    ) -> Path:
        content = self.generate(reports)
        path = output_path or (self.runs_dir / "report.md")
        path.write_text(content, encoding="utf-8")
        return path

    def one_liner(self, report: EntropyReport, prev_H: float | None = None) -> str:
        delta_str = ""
        if prev_H is not None:
            delta_str = f" ({report.total_H - prev_H:+.3f})"
        worst = max(report.breakdown, key=report.breakdown.__getitem__, default="—")
        return (
            f"iter {report.iteration} │ H = {report.total_H:.3f}{delta_str} │ "
            f"worst: {worst} ({report.breakdown.get(worst, 0):.2f})"
        )


def _count_diff_files(diff_text: str) -> int:
    """Count unique files in a unified diff."""
    count = 0
    for line in diff_text.splitlines():
        if line.startswith("diff --git"):
            count += 1
    return count
