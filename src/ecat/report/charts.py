"""ChartRenderer — matplotlib charts and live HTML dashboard for entropy trends."""

from __future__ import annotations

import json as _json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

# matplotlib's font_manager emits INFO/WARNING noise while building its font
# cache on first render — on macOS it logs every unparseable system font
# (bitmap .ttf, emoji/CJK .ttc, LastResort.otf). Those are harmless: it skips
# them and renders with a fallback font, and the cache is built only once. Pin
# the logger to ERROR so a first run doesn't spam the console. Set by name so it
# applies even before matplotlib is imported (import is lazy, below).
logging.getLogger("matplotlib.font_manager").setLevel(logging.ERROR)

_STATE_COLORS = {
    "improvement": "#4caf50",
    "noise": "#9e9e9e",
    "regression_accepted": "#ff9800",
    "regression_reverted": "#f44336",
    "unknown": "#2196f3",
}


class ChartRenderer:
    """Renders entropy_trend.png and dimension_breakdown.png."""

    def __init__(self, charts_dir: Path, fmt: str = "png") -> None:
        self.charts_dir = charts_dir
        self.charts_dir.mkdir(parents=True, exist_ok=True)
        self.fmt = fmt

    def render_trend(
        self,
        iterations: list[int],
        h_values: list[float],
        epsilon: float = 0.05,
        states: list[str] | None = None,
    ) -> Path:
        """Render total-H trend line and save to charts_dir."""
        try:
            import matplotlib

            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
        except ImportError:
            log.warning("matplotlib not available — skipping chart render")
            return self.charts_dir / f"entropy_trend.{self.fmt}"

        fig, ax = plt.subplots(figsize=(10, 5))
        colors = [
            _STATE_COLORS.get(s or "unknown", _STATE_COLORS["unknown"])
            for s in (states or ["unknown"] * len(iterations))
        ]

        ax.plot(iterations, h_values, color="#2196f3", linewidth=1.5, zorder=1)
        ax.scatter(iterations, h_values, c=colors, s=60, zorder=2)
        ax.axhline(y=epsilon, color="red", linestyle="--", linewidth=1, label=f"ε={epsilon}")
        ax.set_xlabel("Iteration")
        ax.set_ylabel("Code Entropy H")
        ax.set_title("ECAT — Entropy Trend")
        ax.legend()
        ax.set_ylim(0, 1.05)
        fig.tight_layout()

        out = self.charts_dir / f"entropy_trend.{self.fmt}"
        fig.savefig(out, dpi=150)
        plt.close(fig)
        log.info("Saved entropy trend chart: %s", out)
        return out

    def render_breakdown(
        self,
        iterations: list[int],
        breakdowns: list[dict[str, float]],
    ) -> Path:
        """Render stacked-area dimension breakdown chart."""
        try:
            import matplotlib

            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
        except ImportError:
            log.warning("matplotlib not available — skipping chart render")
            return self.charts_dir / f"dimension_breakdown.{self.fmt}"

        if not breakdowns:
            return self.charts_dir / f"dimension_breakdown.{self.fmt}"

        dimensions = list(breakdowns[0].keys())
        data: dict[str, list[float]] = {d: [] for d in dimensions}
        for bd in breakdowns:
            for d in dimensions:
                data[d].append(bd.get(d, 0.0))

        fig, ax = plt.subplots(figsize=(10, 5))
        ax.stackplot(
            iterations,
            [data[d] for d in dimensions],
            labels=dimensions,
            alpha=0.7,
        )
        ax.set_xlabel("Iteration")
        ax.set_ylabel("Dimension Score (stacked)")
        ax.set_title("ECAT — Entropy Dimension Breakdown")
        ax.legend(loc="upper right", fontsize=7)
        fig.tight_layout()

        out = self.charts_dir / f"dimension_breakdown.{self.fmt}"
        fig.savefig(out, dpi=150)
        plt.close(fig)
        log.info("Saved breakdown chart: %s", out)
        return out

    def render_html(
        self,
        iterations: list[int],
        h_values: list[float],
        breakdowns: list[dict[str, float]],
        epsilon: float = 0.05,
        states: list[str] | None = None,
        *,
        refresh_seconds: int = 30,
    ) -> Path:
        """Write charts/index.html — auto-refreshing Chart.js dashboard."""
        resolved_states = states or ["unknown"] * len(iterations)
        point_colors = [
            _STATE_COLORS.get(s, _STATE_COLORS["unknown"]) for s in resolved_states
        ]

        dimensions = list(breakdowns[0].keys()) if breakdowns else []
        dim_series = {d: [bd.get(d, 0.0) for bd in breakdowns] for d in dimensions}

        payload = _json.dumps({
            "iterations": iterations,
            "h_values": h_values,
            "epsilon": epsilon,
            "point_colors": point_colors,
            "dimensions": dimensions,
            "dim_series": dim_series,
        })

        # Palette for dimension breakdown (one colour per dimension)
        palette = [
            "#f44336", "#ff9800", "#ffeb3b", "#4caf50",
            "#2196f3", "#9c27b0", "#00bcd4", "#795548",
            "#607d8b", "#e91e63", "#009688", "#3f51b5",
        ]

        html = f"""\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="{refresh_seconds}">
<title>ECAT — Entropy Monitor</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:system-ui,sans-serif;background:#111;color:#ddd;padding:16px}}
  h1{{font-size:1.1rem;margin-bottom:4px}}
  .meta{{font-size:.75rem;color:#666;margin-bottom:14px}}
  .grid{{display:grid;grid-template-columns:1fr 1fr;gap:14px}}
  .card{{background:#1e1e1e;border-radius:8px;padding:14px}}
  @media(max-width:700px){{.grid{{grid-template-columns:1fr}}}}
</style>
</head>
<body>
<h1>ECAT — Entropy Monitor</h1>
<div class="meta">
  Iter <span id="n"></span> &nbsp;|&nbsp;
  H = <span id="h"></span> &nbsp;|&nbsp;
  Refreshes every {refresh_seconds}s &nbsp;|&nbsp;
  <span id="ts"></span>
</div>
<div class="grid">
  <div class="card"><canvas id="trend"></canvas></div>
  <div class="card"><canvas id="bkdown"></canvas></div>
</div>
<script>
const D = {payload};
const PAL = {_json.dumps(palette)};

// Header stats
const last = D.iterations.length - 1;
document.getElementById('n').textContent = last >= 0 ? D.iterations[last] : '—';
document.getElementById('h').textContent = last >= 0 ? D.h_values[last].toFixed(4) : '—';
document.getElementById('ts').textContent = new Date().toLocaleTimeString();

const gridColor = '#2a2a2a';
const tickColor = '#888';
const baseOpts = (title) => ({{
  responsive: true,
  plugins: {{
    title: {{ display: true, text: title, color: '#ccc', font: {{ size: 13 }} }},
    legend: {{ labels: {{ color: '#aaa', boxWidth: 12, font: {{ size: 10 }} }} }},
  }},
  scales: {{
    x: {{ ticks: {{ color: tickColor }}, grid: {{ color: gridColor }} }},
    y: {{ ticks: {{ color: tickColor }}, grid: {{ color: gridColor }} }},
  }},
}});

// ── Trend chart ──────────────────────────────────────────────────────
const tOpts = baseOpts('Total Entropy H');
tOpts.scales.y.min = 0;
tOpts.scales.y.max = D.h_values.length > 0 ? Math.max(...D.h_values, D.epsilon) * 1.1 : 1.05;
new Chart(document.getElementById('trend'), {{
  type: 'line',
  data: {{
    labels: D.iterations,
    datasets: [
      {{
        label: 'Total H',
        data: D.h_values,
        borderColor: '#42a5f5',
        backgroundColor: 'rgba(66,165,245,.08)',
        pointBackgroundColor: D.point_colors,
        pointBorderColor: D.point_colors,
        pointRadius: 6,
        tension: 0.15,
      }},
      {{
        label: 'ε = ' + D.epsilon,
        data: D.iterations.map(() => D.epsilon),
        borderColor: '#ef5350',
        borderDash: [6, 4],
        borderWidth: 1.5,
        pointRadius: 0,
      }},
    ],
  }},
  options: tOpts,
}});

// ── Breakdown chart (stacked area) ───────────────────────────────────
const bOpts = baseOpts('Dimension Breakdown');
bOpts.scales.y.stacked = true;
new Chart(document.getElementById('bkdown'), {{
  type: 'line',
  data: {{
    labels: D.iterations,
    datasets: D.dimensions.map((dim, i) => ({{
      label: dim,
      data: D.dim_series[dim],
      backgroundColor: PAL[i % PAL.length] + '99',
      borderColor:     PAL[i % PAL.length],
      borderWidth: 1,
      fill: true,
      pointRadius: 0,
      tension: 0.1,
    }})),
  }},
  options: bOpts,
}});
</script>
</body>
</html>
"""
        out = self.charts_dir / "entropy.html"
        out.write_text(html, encoding="utf-8")
        log.info("Saved entropy dashboard: %s", out)
        return out
