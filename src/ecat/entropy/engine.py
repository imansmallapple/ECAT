"""EntropyEngine — evaluates all plugins and computes weighted total H."""

from __future__ import annotations

import json
import logging
import random
import re
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

from ecat.entropy.base import EntropyPlugin, EntropyReport
from ecat.entropy.registry import PluginRegistry

if TYPE_CHECKING:
    from ecat.runner.base import AgentRunner

log = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are a HarmonyOS code quality judge. Your response must be ONLY a JSON object — no prose, no markdown:
{"score": 0.35, "rationale": "one-sentence explanation", "issues": ["issue1", "issue2"]}
The score is a float 0.0–1.0 where 0.0 = no issues, 1.0 = severe issues.
"""

# Max workers for plugin evaluation parallelism
_MAX_WORKERS = 8


class _PluginResult(NamedTuple):
    name: str
    hard_score: float
    detail: dict[str, Any]
    verdict: dict[str, Any] | None
    judge_mode: str
    error: str | None


class EntropyEngine:
    """Runs the registered plugin set and returns an EntropyReport."""

    def __init__(
        self,
        plugin_set: str,
        weights: dict[str, float],
        *,
        android_dir: Path | None = None,
        spec_dir: Path | None = None,
        judge_client: object | None = None,
        agentic_runner: AgentRunner | None = None,
        judge_model: str = "claude-sonnet-4-6",
        agentic_env: dict[str, str] | None = None,
        defer_agentic_judges: bool = False,
        deveco_studio_path: str | None = None,
        runs_dir: Path | None = None,
        judge_max_issues: int = 10,
        dynamic_render_interval: int = 5,
        feature_checklist: Path | None = None,
    ) -> None:
        self.plugins: list[EntropyPlugin] = PluginRegistry.get(plugin_set)
        self.weights = weights
        self.android_dir = android_dir
        self.spec_dir = spec_dir
        self.deveco_studio_path = deveco_studio_path
        self.runs_dir = runs_dir
        self.judge_client = judge_client
        self.agentic_runner = agentic_runner
        self.judge_model = judge_model
        self.agentic_env = agentic_env
        # When True, agentic judges are NOT run as separate subprocesses here.
        # Instead the GradientBuilder asks the discriminator session to dispatch
        # them as parallel subagents — saves CLAUDE.md / system-prompt repeat
        # cost and lets the discriminator see all verdicts before writing the
        # gradient.  Hard checks and LLM judges still run normally.
        self.defer_agentic_judges = defer_agentic_judges
        self.judge_max_issues = judge_max_issues
        self.dynamic_render_interval = dynamic_render_interval
        self.feature_checklist = feature_checklist

        # Normalise weights
        total = sum(weights.values())
        if total <= 0:
            raise ValueError("entropy.weights must sum to a positive number")
        self._norm_weights = {k: v / total for k, v in weights.items()}

    def evaluate(self, harmony_dir: Path, iteration: int = 0) -> EntropyReport:
        # ── Phase 1: run all hard checks in parallel ──────────────────────────
        plugin_results: dict[str, _PluginResult] = {}
        plugins_to_judge: list[tuple[EntropyPlugin, float, dict[str, object]]] = []
        plugin_map: dict[str, EntropyPlugin] = {p.name: p for p in self.plugins}

        def _run_plugin(plugin: EntropyPlugin) -> _PluginResult:
            w = self._norm_weights.get(plugin.name, 0.0)
            if w == 0.0:
                return _PluginResult(plugin.name, 0.0, {}, None, "hard", None)
            try:
                hard_score, detail = plugin.evaluate(
                    harmony_dir,
                    android_dir=self.android_dir,
                    spec_dir=self.spec_dir,
                    config={"deveco_studio_path": self.deveco_studio_path,
                            "runs_dir": self.runs_dir, "iteration": iteration,
                            "dynamic_render_interval": self.dynamic_render_interval,
                            "feature_checklist": self.feature_checklist,
                            # 该维度由谁判(判别器 runner)就取谁的 timeout, 让
                            # feature_checklist 之类的按真实预算推轮次上限, 而不是写死 3600s。
                            "judge_timeout_s": getattr(
                                self.agentic_runner, "timeout_s", None)},
                )
                return _PluginResult(plugin.name, hard_score, detail, None, plugin.judge_mode, None)
            except Exception as exc:
                log.warning("Plugin %s hard check failed: %s", plugin.name, exc)
                return _PluginResult(plugin.name, 1.0, {"error": str(exc)}, None, plugin.judge_mode, str(exc))

        with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
            futures = {pool.submit(_run_plugin, p): p for p in self.plugins}
            for future in as_completed(futures):
                result = future.result()
                plugin_results[result.name] = result
                if result.error is None and result.judge_mode == "agentic":
                    plugins_to_judge.append((self.plugins[self.plugins.index(
                        next(p for p in self.plugins if p.name == result.name)
                    )], result.hard_score, result.detail))

        # Warn when a WEIGHTED plugin skipped its evaluation for an infrastructure
        # reason (e.g. ui_align with an emulator offline). Otherwise the skip is
        # invisible — the dimension just shows 0.0 and the user can't tell it never
        # ran. Cadence skips are routine and stay quiet.
        for name, result in plugin_results.items():
            if self._norm_weights.get(name, 0.0) <= 0:
                continue
            skipped = (result.detail or {}).get("skipped")
            if not skipped or skipped == "cadence":
                continue
            reason = (result.detail or {}).get("reason") or (result.detail or {}).get("insight") or ""
            log.warning(
                "[yellow]%s skipped (%s)[/] — dimension contributes 0 this iteration. %s",
                name, skipped, reason,
            )

        # Log hard-only plugin results (no judge attached) so every dimension
        # appears in the run log. LLM / agentic plugins are logged later when
        # their soft score lands. Issues (top 3) are appended to the same
        # verdict line via " | issues: ..." — same format as agentic / llm
        # verdicts so the log reads uniformly.
        # Note: full details are always passed to the discriminator via the
        # gradient builder's prompt; this is only for human-readable logs.
        for name, result in plugin_results.items():
            if result.judge_mode != "hard" or self._norm_weights.get(name, 0.0) <= 0:
                continue
            issues_suffix = ""
            if result.hard_score > 0:
                preview = _summarize_hard_issues(plugin_map[name], result.hard_score, result.detail)
                if preview:
                    issues_suffix = f" | issues: {preview}"
            log.info(
                "[cyan]%s[/] hard verdict: sub_entropy=%.3f "
                "(hard (rule check)=%.3f, soft (no judge)=N/A)%s",
                name, result.hard_score, result.hard_score, issues_suffix,
            )

        # ── Phase 2: run agentic judges in parallel ─────────────────────────────
        agentic_results: dict[str, dict[str, Any] | None] = {}

        def _run_agentic(plugin: EntropyPlugin, hard_score: float, detail: dict[str, object]) -> tuple[str, dict[str, Any] | None, int, int, int]:
            if not (plugin.judge_prompt and self.agentic_runner):
                return plugin.name, None, 0, 0, 0
            verdict, tok_in, tok_out, cache = _run_agentic_judge(
                self.agentic_runner, self.judge_model,
                plugin, harmony_dir, hard_score, detail,
                judge_client=self.judge_client,
                env=self.agentic_env,
                max_issues=self.judge_max_issues,
            )
            return plugin.name, verdict, tok_in, tok_out, cache

        agentic_plugins = [
            (p, hs, det)
            for p, hs, det in plugins_to_judge
            if p.judge_mode == "agentic"
        ]

        if agentic_plugins and self.defer_agentic_judges:
            log.debug(
                "Deferring %d agentic judge(s) to discriminator session: %s",
                len(agentic_plugins),
                ", ".join(p.name for p, _, _ in agentic_plugins),
            )
            agentic_plugins = []  # GradientBuilder will dispatch them as subagents

        agentic_tok_in = agentic_tok_out = agentic_cache = 0

        if agentic_plugins:
            _checkpoint_opencode_global_db()
            _agentic_concurrency = max(1, min(
                len(agentic_plugins),
                getattr(self.agentic_runner, "max_concurrent", 4),
            ))
            with ThreadPoolExecutor(max_workers=_agentic_concurrency) as pool:
                agentic_futures: list[Future[tuple[str, dict[str, Any] | None, int, int, int]]] = [
                    pool.submit(_run_agentic, p, hs, det)
                    for p, hs, det in agentic_plugins
                ]
                for agentic_future in as_completed(agentic_futures):
                    name, verdict, tok_in, tok_out, cache = agentic_future.result()
                    agentic_tok_in += tok_in
                    agentic_tok_out += tok_out
                    agentic_cache += cache
                    agentic_results[name] = verdict
                    if verdict is not None:
                        hw = plugin_map[name].hard_weight
                        issues = verdict.get("issues", []) or []
                        # Merge in hard-check fails so an agentic dimension scored
                        # by its hard layer (soft empty) still prints its real issue.
                        hard_fails = [
                            f for f in (plugin_results[name].detail.get("fails") or [])
                            if isinstance(f, str)
                        ]
                        display_issues = issues + [f for f in hard_fails if f not in issues]
                        issues_preview = (", ".join(display_issues[:3]) + (" …" if len(display_issues) > 3 else "")) or "none"
                        log.info(
                            "[cyan]%s[/] agentic verdict: sub_entropy=%.3f (hard (rule check)=%.3f, soft (agentic judge)=%.3f) | issues: %s",
                            name,
                            _blend(plugin_results[name].hard_score, verdict["score"], hw),
                            plugin_results[name].hard_score,
                            verdict["score"],
                            issues_preview,
                        )
                        if verdict["score"] > 0 and not issues:
                            log.warning(
                                "%s judge gave score=%.3f with empty issues list — "
                                "violates prompt rule; clamping soft score to 0.0",
                                name, verdict["score"],
                            )
                            verdict["score"] = 0.0
                    else:
                        log.info("[cyan]%s[/] agentic judge returned no verdict — using hard score", name)

        # ── Assemble final breakdown ────────────────────────────────────────────
        breakdown: dict[str, float] = {}
        details: dict[str, dict[str, object]] = {}

        for name, result in plugin_results.items():
            breakdown[name] = result.hard_score
            details[name] = result.detail

            if result.judge_mode == "agentic" and name in agentic_results:
                verdict = agentic_results[name]
                if verdict is not None:
                    hw = plugin_map[name].hard_weight
                    breakdown[name] = _blend(result.hard_score, verdict["score"], hw)
                    details[name]["agentic_verdict"] = verdict

        total_H = _normalized_total_h(breakdown, details, self.weights)
        total_H = max(0.0, min(1.0, total_H))

        return EntropyReport(
            iteration=iteration,
            total_H=total_H,
            breakdown=breakdown,
            details=details,
            tokens_in=agentic_tok_in,
            tokens_out=agentic_tok_out,
            cache_read=agentic_cache,
        )


def _normalized_total_h(
    breakdown: dict[str, float],
    details: dict[str, dict[str, Any]],
    weights: dict[str, float],
) -> float:
    """Aggregate entropy across dimensions, excluding deterministically-skipped ones.

    A dimension is "skipped" when its evaluate() detail carries a ``skipped`` key
    that is not ``"cadence"`` (cadence is routine throttling — the dimension is
    still scored, just this round it was not due). Skipped dims contribute 0 by
    construction, so keeping their weight in the normalization sum would dilute
    every other dimension's H (e.g. feature_checklist at 0.10 while unconfigured
    pushes data_layer_parity from 0.30 -> 0.27). That deflation can trip the
    entropy threshold early.

    The weight map is re-normalised over the *active* dimensions only, so the
    returned H stays in [0, 1] and reflects the dimensions that actually ran.
    Both EntropyEngine.evaluate and GradientBuilder._apply_agentic_verdicts use
    this — they must agree or the discriminator's recomputed H drifts from the
    engine's.
    """
    active_w = sum(
        w for name, w in weights.items()
        if w > 0 and not (
            (details.get(name) or {}).get("skipped")
            and (details.get(name) or {}).get("skipped") != "cadence"
        )
    )
    if active_w <= 0:
        return 0.0
    h = sum(
        (weights.get(name, 0.0) / active_w) * score
        for name, score in breakdown.items()
        if weights.get(name, 0.0) > 0
        and not (
            (details.get(name) or {}).get("skipped")
            and (details.get(name) or {}).get("skipped") != "cadence"
        )
    )
    return max(0.0, min(1.0, h))


def _blend(hard_score: float, judge_score: float, hard_weight: float = 0.4) -> float:
    return hard_score * hard_weight + judge_score * (1.0 - hard_weight)


def _summarize_hard_issues(
    plugin: EntropyPlugin, hard_score: float, detail: dict[str, object]
) -> str:
    """Extract a one-line preview of the top issues a hard-only plugin found.

    Calls the plugin's own extract_work_items() to get its WorkItem list, then
    formats the top 3 issue strings. Returns "" if extract fails or none.
    """
    try:
        items = plugin.extract_work_items(hard_score, detail)
    except Exception:
        return ""
    issues: list[str] = []
    overflow = False
    for it in items:
        for issue in it.issues:
            if issue and issue not in issues:
                if len(issues) >= 3:
                    overflow = True
                    break
                issues.append(issue[:120])
        if overflow:
            break
    suffix = " …" if overflow else ""
    return ", ".join(issues) + suffix


def _checkpoint_opencode_global_db() -> None:
    """Checkpoint opencode's global WAL database before concurrent judge runs.

    opencode stores session data in a single shared SQLite database at
    ~/.local/share/opencode/opencode.db.  Multiple concurrent opencode
    processes all try to open that DB at start-up (setting WAL mode), which
    can fail with SQLITE_BUSY if the WAL has grown large from prior runs.
    A PASSIVE checkpoint flushes the WAL to the main file so the DB is in a
    clean state before we launch concurrent judges.

    Only called when at least one agentic plugin is about to run; has no
    effect if the file does not exist (e.g. when using ClaudeCodeRunner).
    """
    import sqlite3

    db = Path.home() / ".local" / "share" / "opencode" / "opencode.db"
    if not db.exists():
        return
    try:
        with sqlite3.connect(str(db), timeout=10.0) as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        log.debug("Checkpointed opencode global WAL database: %s", db)
    except Exception as exc:
        log.debug("opencode WAL checkpoint skipped: %s", exc)


def _run_llm_judge(
    judge_client: object,
    plugin: EntropyPlugin,
    harmony_dir: Path,
    hard_score: float,
    details: dict[str, object],
) -> dict[str, object] | None:
    from ecat.llm.judge import JudgeClient

    if not isinstance(judge_client, JudgeClient):
        return None

    user_prompt = _build_judge_user_prompt(plugin, hard_score, details)
    verdict = judge_client.judge(_SYSTEM_PROMPT, user_prompt)
    return _parse_verdict(verdict.raw)


_ANSI_ESCAPE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def _strip_ansi(text: str) -> str:
    return _ANSI_ESCAPE.sub("", text)


_DB_BUSY_RETRIES = 2
_DB_BUSY_BACKOFF_S = 1.5
# opencode / 底层 SQLite 在库被占用时的两种措辞
_DB_BUSY_MARKS = ("SQLITE_BUSY", "database is locked")


def _is_db_busy(result: object) -> bool:
    blob = f"{getattr(result, 'stderr', '')}{getattr(result, 'stdout', '')}"
    return any(m in blob for m in _DB_BUSY_MARKS)


def _run_agentic_judge(
    runner: AgentRunner,
    model: str,
    plugin: EntropyPlugin,
    harmony_dir: Path,
    hard_score: float,
    details: dict[str, object],
    judge_client: object | None = None,
    env: dict[str, str] | None = None,
    max_issues: int = 10,
) -> tuple[dict[str, Any] | None, int, int, int]:
    prompt = _build_agentic_judge_prompt(plugin, hard_score, details, max_issues=max_issues)
    # 高并发下 opencode 抢全局 SQLite 会在**启动期**失败（实测 20 并发约 1/20 撞
    # SQLITE_BUSY）。这类失败与判据无关，重试即可；不重试的话该维度会静默退回
    # hard-only 分数，日志里只留一行 WARNING。Claude Code 不共享库，走不到这里。
    run_args = dict(
        prompt=prompt,
        model=model,
        cwd=harmony_dir,
        role=f"judge_{plugin.name}",
        env=env,
    )
    for attempt in range(_DB_BUSY_RETRIES + 1):
        result = runner.run(**run_args)
        if result.success or not _is_db_busy(result) or attempt == _DB_BUSY_RETRIES:
            break
        delay = _DB_BUSY_BACKOFF_S * (attempt + 1) + random.uniform(0, 0.5)
        log.warning("Agentic judge %s: 数据库繁忙，%.1fs 后重试（%d/%d）",
                    plugin.name, delay, attempt + 1, _DB_BUSY_RETRIES)
        time.sleep(delay)
    tok_in = result.tokens_in
    tok_out = result.tokens_out
    cache = result.cache_read
    if not result.success:
        log.warning(
            "Agentic judge for %s exited %d: %s",
            plugin.name, result.returncode, result.stderr[:200],
        )
        return None, tok_in, tok_out, cache
    clean = _strip_ansi(result.stdout)
    verdict = _parse_verdict(clean)
    if verdict is None and judge_client is not None:
        log.debug("Agentic judge %s: no structured verdict, attempting prose extraction", plugin.name)
        verdict = _extract_verdict_from_prose(judge_client, plugin.name, clean, plugin.judge_prompt)
    if verdict is None:
        log.warning("Agentic judge %s: could not extract verdict — using hard score", plugin.name)
        log.debug("Agentic judge %s stdout tail:\n%s", plugin.name, clean[-2000:])
    return verdict, tok_in, tok_out, cache


def _build_judge_user_prompt(
    plugin: EntropyPlugin,
    hard_score: float,
    details: dict[str, object],
) -> str:
    return (
        f"## Hard check results\n"
        f"Score: {hard_score:.3f}\n"
        f"Details:\n```\n{_truncate_details(details)}\n```\n\n"
        f"## Your task\n{plugin.judge_prompt}"
    )


def _build_agentic_judge_prompt(
    plugin: EntropyPlugin,
    hard_score: float,
    details: dict[str, object],
    max_issues: int = 10,
) -> str:
    json_example = (
        '{"score": 0.75, "rationale": "one-sentence explanation", '
        '"issues": ["missing FeatureX implementation", "FeatureY is stub/TODO"]}'
    )
    return (
        f"You are an automated HarmonyOS code quality evaluator for the **{plugin.name}** dimension.\n\n"
        f"Start evaluating immediately. The HarmonyOS codebase is in the current working directory — "
        f"use Read/Glob/Grep tools to explore it. Do NOT ask for clarification or additional context. "
        f"Ignore non-source dirs: any dot-dir (.git/.idea/.hvigor/.claude/.ecat/...) and "
        f"oh_modules/node_modules/build — they are tools/deps/caches, not project code.\n\n"
        f"## What to evaluate\n{plugin.judge_prompt}\n\n"
        f"## Automated hard-check baseline\n"
        f"Hard score: {hard_score:.3f} (0.0 = no issues, 1.0 = severe issues)\n"
        f"Details:\n```json\n{_truncate_details(details)}\n```\n\n"
        f"## Early-stop rule\n"
        f"Stop exploring as soon as you have found **{max_issues} distinct issues**. "
        f"Do not read any more files once you reach this limit — output your verdict immediately. "
        f"The first {max_issues} issues are enough signal for the Generator to act on.\n\n"
        f"## Required output\n"
        f"After exploring the codebase (or hitting the {max_issues}-issue limit), "
        f"output ONLY this JSON as your final message:\n"
        f"{json_example}\n"
        f"score: float 0.0 (fully implemented, no issues) to 1.0 (severely missing/broken).\n"
        f"issues: list specific missing feature names or broken pattern names found "
        f"(cap at {max_issues} items).\n\n"
        f"## Hard rule on score / issues consistency\n"
        f"- If score > 0.0, `issues` MUST contain at least one specific item.\n"
        f"- If you cannot articulate any specific issue, score MUST be exactly 0.0.\n"
        f"- Do NOT hedge with a small non-zero score and an empty issues list — this\n"
        f"  forces the Generator to act on a vague signal and wastes its budget.\n"
        f"- Each issue should reference a concrete file / method / feature name.\n"
        f"Do not output anything else in the final message."
    )


def _truncate_details(details: dict[str, object], max_chars: int = 3000) -> str:
    raw = json.dumps(details, indent=2, default=str)
    if len(raw) <= max_chars:
        return raw
    return raw[:max_chars] + "\n...(truncated)"


def _parse_verdict(raw: str) -> dict[str, object] | None:
    """Parse judge output with robust fallback for non-JSON or markdown-wrapped output.

    Agentic runners (opencode) emit a full conversation stream that may contain
    many JSON-like blocks (tool calls, results, etc.).  The verdict is always the
    LAST structured output, so we try all candidate blocks from end to start.
    """
    if not raw or not raw.strip():
        return None

    # Attempt 1: scan all JSON objects with up to one level of nesting, last-first.
    # Pattern matches {...} blocks that may contain one level of inner {...}.
    for match in reversed(list(re.finditer(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", raw))):
        try:
            data = json.loads(match.group())
            if "score" not in data:
                continue
            score = max(0.0, min(1.0, float(data["score"])))
            return {
                "score": score,
                "rationale": str(data.get("rationale", "")),
                "issues": list(data.get("issues", [])),
            }
        except (json.JSONDecodeError, ValueError, TypeError):
            pass

    # Attempt 2: extract "score": <float> from raw text (last occurrence wins)
    score_matches = re.findall(r'"score"\s*:\s*([\d.]+)', raw)
    if score_matches:
        try:
            return {
                "score": max(0.0, min(1.0, float(score_matches[-1]))),
                "rationale": "",
                "issues": [],
            }
        except ValueError:
            pass

    # Attempt 3: extract a bare float that looks like a score (0.0-1.0)
    bare_scores = re.findall(r"\b([01]\.(?:[0-9]+)?)\b", raw)
    if bare_scores:
        try:
            return {
                "score": max(0.0, min(1.0, float(bare_scores[-1]))),
                "rationale": "",
                "issues": [],
            }
        except ValueError:
            pass

    # Attempt 4: "X/10" or "X.X/10" — model used a 10-point scale; normalize to 0.0-1.0
    tenscale = re.findall(r'\b(\d+(?:\.\d+)?)\s*/\s*10\b', raw)
    if tenscale:
        try:
            return {
                "score": max(0.0, min(1.0, float(tenscale[-1]) / 10.0)),
                "rationale": "",
                "issues": [],
            }
        except ValueError:
            pass

    log.debug("Could not parse structured verdict from output")
    return None


def _extract_verdict_from_prose(
    judge_client: object,
    plugin_name: str,
    prose: str,
    judge_prompt: str | None = None,
) -> dict[str, object] | None:
    """Fallback: use the LLM judge client to extract a structured verdict from prose output.

    Called when _parse_verdict() finds no structured data in the agentic judge's stdout.
    Safe for both anthropic_sdk and openai_sdk runtimes — the extraction prompt is simple
    enough that Claude follows it without response_format, and DeepSeek uses json_object mode.
    """
    from ecat.llm.judge import JudgeClient

    if not isinstance(judge_client, JudgeClient):
        return None

    scoring_context = (
        f"\n\nScoring criteria for '{plugin_name}':\n{judge_prompt}"
        if judge_prompt else ""
    )
    system = (
        "Extract a quality score and specific issues from the code analysis below. "
        "Return ONLY a JSON object — no prose, no markdown:\n"
        '{"score": 0.5, "rationale": "brief one-sentence reason", "issues": ["specific issue 1", "specific issue 2"]}\n'
        "score: float 0.0 (no issues found) to 1.0 (severe/many issues). "
        "issues: list specific missing feature names or broken patterns mentioned in the analysis. "
        "If no specific issues are described, use []."
        + scoring_context
    )
    user = f"Analysis of **{plugin_name}** dimension:\n\n{prose[-3000:]}"
    _FAIL_PREFIXES = ("unparseable response", "parse error:", "judge error:")
    try:
        verdict_obj = judge_client.judge(system, user)
        if any(verdict_obj.rationale.startswith(p) for p in _FAIL_PREFIXES):
            return None
        issues: list[str] = []
        if verdict_obj.raw:
            try:
                m = re.search(r"\{[\s\S]*\}", verdict_obj.raw)
                if m:
                    raw_data = json.loads(m.group())
                    issues = [str(i) for i in raw_data.get("issues", [])]
            except Exception:
                pass
        return {
            "score": verdict_obj.score,
            "rationale": verdict_obj.rationale,
            "issues": issues,
        }
    except Exception as exc:
        log.debug("Prose extraction for %s failed: %s", plugin_name, exc)
        return None
