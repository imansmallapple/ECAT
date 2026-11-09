"""LoopDriver — adversarial loop orchestration."""

from __future__ import annotations

import json
import logging
import re
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

from ecat.entropy.base import EntropyReport, git_exclude_pathspecs, skip_dir_names
from ecat.entropy.engine import EntropyEngine
from ecat import porter
from ecat.errors import AgentQuotaExhausted, IterationFailed, Oscillation, Regression
from ecat.errors import TimeoutError as EcatTimeout
from ecat.gradient.builder import GradientBuilder
from ecat.gradient.schema import TextGradient, WorkItem
from ecat.loop.policies import OscillationPolicy, RegressionPolicy
from ecat.memory.tree import render_injection
from ecat.prompts import load_prompt
from ecat.report.charts import ChartRenderer
from ecat.report.timeseries import EntropyTimeseries
from ecat.runner.base import AgentRunner

log = logging.getLogger(__name__)


class LoopDriver:
    """Drives the Generator ↔ Discriminator adversarial loop."""

    def __init__(
        self,
        *,
        harmony_dir: Path,
        android_dir: Path | None,
        spec_dir: Path | None,
        runs_dir: Path,
        entropy_engine: EntropyEngine,
        gradient_builder: GradientBuilder,
        generator_runner: AgentRunner,
        generator_model: str,
        generator_env: dict[str, str] | None,
        max_iterations: int,
        entropy_threshold: float,
        regression_policy: RegressionPolicy,
        oscillation_policy: OscillationPolicy,
        timeseries: EntropyTimeseries,
        generator_prompt_template: str | None = None,
        pause_callback: Callable[[int, float], bool] | None = None,
        subagent_tool: str = "Agent",
        deveco_studio_path: str | None = None,
        generator_compact_interval: int = 15,
        memory_dir: Path | None = None,
        max_items_per_iter: int = 15,
        chart_renderer: ChartRenderer | None = None,
        stable_iters_to_stop: int = 2,
        resource_porter: bool = True,
        required_skills: list[str] | None = None,
        auto_fetch_skills: bool = True,
        skills_dir: Path | None = None,
    ) -> None:
        self.harmony_dir = harmony_dir
        self.android_dir = android_dir
        self.spec_dir = spec_dir
        self.runs_dir = runs_dir
        self.entropy_engine = entropy_engine
        self.gradient_builder = gradient_builder
        self.generator_runner = generator_runner
        self.generator_model = generator_model
        self.generator_env = generator_env
        self.max_iterations = max_iterations
        self.entropy_threshold = entropy_threshold
        self.regression_policy = regression_policy
        self.oscillation_policy = oscillation_policy
        self.timeseries = timeseries
        self.generator_prompt_template = generator_prompt_template or _get_gen_prompt()
        self.pause_callback = pause_callback
        self.subagent_tool = subagent_tool
        self.deveco_studio_path = deveco_studio_path
        self.generator_compact_interval = generator_compact_interval
        self.memory_dir = memory_dir
        self.chart_renderer = chart_renderer
        self.max_items_per_iter = max_items_per_iter
        # Require H to stay below the threshold for this many CONSECUTIVE iters
        # before stopping. The discriminator samples a subset of issues each
        # round, so one quiet round isn't proof of convergence; >=2 guards
        # against premature stop. Reset whenever H rises back to/above ε.
        self.stable_iters_to_stop = max(1, int(stable_iters_to_stop))
        self.resource_porter = resource_porter
        self.required_skills = list(required_skills or [])
        self.auto_fetch_skills = auto_fetch_skills
        self.skills_dir = skills_dir
        self._below_streak = 0

    def run(self, start_iteration: int = 0) -> list[EntropyReport]:
        reports: list[EntropyReport] = []
        h_prev: float | None = None

        _check_git(self.harmony_dir)

        try:
            reports = self._loop(start_iteration, h_prev)
        except KeyboardInterrupt:
            log.warning("[bold red]Interrupted[/] — reverting harmony/ to last checkpoint")
            _git_revert(self.harmony_dir)

        return reports

    def _loop(
        self, start_iteration: int, h_prev: float | None,
    ) -> list[EntropyReport]:
        reports: list[EntropyReport] = []
        pending_iter: int | None = None
        prev_gradient: TextGradient | None = None
        iter_states: list[str] = []
        total_discrim_s = 0.0
        total_gen_s = 0.0
        total_discrim_tok_in = 0
        total_discrim_tok_out = 0
        total_discrim_cache = 0
        total_gen_tok_in = 0
        total_gen_tok_out = 0
        total_gen_cache = 0
        loop_start = time.monotonic()
        quota_exhausted: AgentQuotaExhausted | None = None

        for n in range(start_iteration, start_iteration + self.max_iterations):
            log.info("[bold cyan]%s Iteration %d %s[/]", "━" * 20, n, "━" * 20)
            iter_dir = self.runs_dir / str(n)
            iter_dir.mkdir(parents=True, exist_ok=True)

            # Step 1: Discriminator phase — runs ALL evaluation plugins
            # (hard checks + LLM judges via Python engine; agentic judges via
            # subagent dispatch inside the discriminator session) and then
            # writes the text gradient.
            if self.required_skills and self.auto_fetch_skills:
                try:
                    from ecat import skills as skills_mod

                    skill_status = skills_mod.ensure(self.required_skills, dest=self.skills_dir)
                    (iter_dir / "skills.json").write_text(
                        json.dumps(skill_status, indent=2, ensure_ascii=False), encoding="utf-8")
                    unknown = [n for n, st in skill_status.items() if st == "unknown"]
                    if unknown:
                        log.warning("skills not in registry (skipped): %s", ", ".join(unknown))
                except Exception as exc:  # never fail a round because of skills
                    log.warning("skills ensure skipped: %s", exc)

            if self.resource_porter:
                try:
                    pstats = porter.run(self.harmony_dir, self.android_dir)
                    if pstats.placeholders or pstats.from_android or pstats.rewritten_refs:
                        (iter_dir / "resource_porter.json").write_text(
                            json.dumps(pstats.as_dict() | {"placeholder_names": pstats.placeholders},
                                       indent=2, ensure_ascii=False),
                            encoding="utf-8",
                        )
                except Exception as exc:  # never fail a round because of the porter
                    log.warning("resource porter skipped: %s", exc)

            log.info("[bold magenta]Discriminator[/] running …")
            discrim_start = time.monotonic()
            try:
                report = self.entropy_engine.evaluate(self.harmony_dir, iteration=n)
            except AgentQuotaExhausted as exc:
                quota_exhausted = exc
                break
            except Exception as exc:
                log.error("Entropy evaluation failed at iter %d: %s", n, exc)
                _write_error(iter_dir, str(exc))
                raise IterationFailed(n, f"entropy evaluation: {exc}") from exc

            self.gradient_builder.runner.transcript_dir = iter_dir
            try:
                gradient = self.gradient_builder.produce(
                    report,
                    prompt_dump_path=iter_dir / "discriminator_prompt.md",
                    h_delta=(report.total_H - h_prev) if h_prev is not None else None,
                )
            except AgentQuotaExhausted as exc:
                quota_exhausted = exc
                break
            except Exception as exc:
                log.error("Gradient generation failed at iter %d: %s", n, exc)
                _write_error(iter_dir, str(exc))
                if prev_gradient is not None:
                    log.warning(
                        "Gradient generation failed — reusing iter %d gradient as fallback",
                        n - 1,
                    )
                    gradient = prev_gradient
                else:
                    gradient = _empty_gradient()
            else:
                prev_gradient = gradient

            # Step 2: aggregate / record / show total entropy
            reports.append(report)
            self.timeseries.append(report)
            iter_states.append("unknown")  # refined in step 5 once delta is known

            worst = max(report.breakdown, key=report.breakdown.__getitem__, default="n/a")
            discrim_s = time.monotonic() - discrim_start
            total_discrim_s += discrim_s
            discrim_tok_in = report.tokens_in
            discrim_tok_out = report.tokens_out
            discrim_cache = report.cache_read
            total_discrim_tok_in += discrim_tok_in
            total_discrim_tok_out += discrim_tok_out
            total_discrim_cache += discrim_cache
            log.info(
                "iter %d | H = [bold %s]%.4f[/] | worst: [yellow]%s[/]",
                n,
                _h_color(report.total_H),
                report.total_H,
                worst,
            )

            # Step 3: commit previous iteration's changes if H improved
            # (using the FINAL H now that agentic verdicts are folded in)
            if pending_iter is not None and h_prev is not None:
                prev_dir = self.runs_dir / str(pending_iter)
                _capture_diff(self.harmony_dir, prev_dir)
                delta = report.total_H - h_prev
                if delta < 0:
                    stat = _git_checkpoint(self.harmony_dir, pending_iter, report.total_H)
                    files, ins, dels = _parse_stat(stat)
                    stat_suffix = f" | {files} files +{ins}/-{dels} lines" if stat else ""
                    log.info(
                        "[bold green]Accepted[/] iter %d changes: H %.4f → %.4f ([green]ΔH = %.4f[/])%s",
                        pending_iter, h_prev, report.total_H, delta, stat_suffix,
                    )
                else:
                    log.info(
                        "iter %d not committed (H %.4f → %.4f, ΔH = +%.4f) — kept in working tree, next round builds on it",
                        pending_iter, h_prev, report.total_H, delta,
                    )
                pending_iter = None

            # Step 4: early stop — only after H stays below ε for N consecutive
            # iters (a single quiet discriminator round isn't proof of convergence).
            if report.total_H < self.entropy_threshold:
                self._below_streak += 1
            else:
                self._below_streak = 0
            if self._below_streak >= self.stable_iters_to_stop:
                log.info(
                    "[bold green]Early stop:[/] H=%.4f < ε=%.4f for %d consecutive iter(s)",
                    report.total_H, self.entropy_threshold, self._below_streak,
                )
                _log_iter_stats(n, discrim_s, discrim_tok_in, discrim_tok_out, discrim_cache,
                                0.0, 0, 0, 0)
                _log_cumul_stats(n + 1, time.monotonic() - loop_start,
                                 total_discrim_s, total_gen_s,
                                 total_discrim_tok_in, total_discrim_tok_out, total_discrim_cache,
                                 total_gen_tok_in, total_gen_tok_out, total_gen_cache)
                if self.chart_renderer is not None:
                    _render_html(self.chart_renderer, reports, iter_states, self.entropy_threshold)
                break
            if report.total_H < self.entropy_threshold:
                log.info(
                    "H=%.4f < ε=%.4f (%d/%d consecutive) — continuing to confirm convergence",
                    report.total_H, self.entropy_threshold,
                    self._below_streak, self.stable_iters_to_stop,
                )

            # Step 5: regression check
            if h_prev is not None:
                try:
                    state = self.regression_policy.assess(h_prev, report.total_H, n)
                    iter_states[-1] = state
                    rise = report.total_H - h_prev
                    if state == "noise":
                        log.info(
                            "iter %d: H rose +%.4f (≤ tolerance) — change [bold]kept[/], NOT reverted",
                            n, rise,
                        )
                    elif state == "regression_grace":
                        log.warning(
                            "iter %d: H regressed ([red]ΔH = +%.4f[/]) — change kept; Generator gets one round to fix (revert only if it regresses again)",
                            n, rise,
                        )
                    elif state != "improvement":
                        log.info("iter %d regression state: [bold %s]%s[/]", n, _state_color(state), state)
                except Regression as exc:
                    log.warning("%s — reverting harmony/", exc)
                    _git_revert(self.harmony_dir)
                    iter_states[-1] = "regression_reverted"
                    if self.chart_renderer is not None:
                        _render_html(self.chart_renderer, reports, iter_states, self.entropy_threshold)
                    h_prev = report.total_H
                    continue

            # Render after discriminator so users see the new H immediately,
            # without waiting for the full generator phase to complete.
            if self.chart_renderer is not None:
                _render_html(self.chart_renderer, reports, iter_states, self.entropy_threshold)

            # Step 6: oscillation check
            self.oscillation_policy.record(report.total_H)
            try:
                self.oscillation_policy.check()
            except Oscillation as exc:
                log.warning("Oscillation detected: %s", exc)
                break

            # Step 7: emit text gradient (summary + work_list preview)
            #         only AFTER all H-based decisions resolve and we know
            #         we're actually going to run the Generator next
            _write_gradient(iter_dir, gradient)
            _log_gradient_summary(gradient)

            # Step 8: run Generator with gradient
            log.info("[bold blue]Generator[/] running …")
            compile_errors = _format_compile_errors(report)
            max_items = self.max_items_per_iter
            delta_section = ""
            if h_prev is not None:
                delta = report.total_H - h_prev
                delta_section = f"Previous H = {h_prev:.4f}, ΔH = {delta:+.4f}"
            insights_section = ""
            if gradient.insights:
                insights_section = "## Cross-cutting insights\n" + "\n".join(
                    f"- {ins}" for ins in gradient.insights
                )
            gen_prompt = self.generator_prompt_template.format(
                iteration=n,
                total_H=report.total_H,
                delta_section=delta_section,
                compile_errors=compile_errors or "(none)",
                summary=gradient.summary,
                insights_section=insights_section,
                work_list_text=_format_work_list_for_generator(gradient.work_list, max_items),
                work_list_count=min(max_items, len(gradient.work_list)),
                max_items_per_iter=max_items,
                # Resolve to absolute paths so the Generator can find them
                # regardless of its cwd (which is harmony_dir, not workspace root).
                # A bare relative path like "./Android-Simple-Gallery" would be
                # interpreted relative to harmony_dir and resolve to a non-existent
                # location, making the "Source of truth" prompt section useless.
                android_dir=(
                    Path(self.android_dir).resolve().as_posix()
                    if self.android_dir else "(no Android source available)"
                ),
                spec_dir=(
                    Path(self.spec_dir).resolve().as_posix()
                    if self.spec_dir else "(no spec directory)"
                ),
                subagent_tool=self.subagent_tool,
            )
            # ui_align active this iteration → tell the Generator to reference the
            # full Android screenshot set when dispatching visual-fixer. Only added
            # when ui_align actually ran (weight>0 + captured), never otherwise.
            ua = (report.details.get("ui_align") or {})
            ua_dir = ua.get("android_screenshots_dir")
            if ua_dir and ua.get("screenshots_captured"):
                gen_prompt += (
                    "\n\n## UI alignment (ui_align)\n"
                    "For work-list items with a `compare:<path>` marker, dispatch the "
                    "**visual-fixer** subagent (one per `.ets`, don't edit directly). Pass it the "
                    f"finding, the `compare:` path, and the Android screenshot dir `{ua_dir}`. The "
                    "subagent MUST Read every image in that dir before editing — align to the whole "
                    "app's Android UI, not just the compare image."
                )
            mem_ctx = _load_memory_context(self.memory_dir)
            if mem_ctx:
                gen_prompt += mem_ctx
            if self.deveco_studio_path:
                harmony_abs = self.harmony_dir.resolve().as_posix()
                gen_prompt += (
                    f"\n\n## Mandatory build verification (do this LAST)\n\n"
                    f"After completing ALL improvements above (work-list items and/or strategic direction), invoke the build skill via the **Skill tool** (not as slash text, not by hand-rolling hvigorw in Bash):\n\n"
                    f"`Skill(skill=\"harmonyos-build-fix\", args='\"{harmony_abs}\" \"{self.deveco_studio_path}\"')`\n\n"
                    f"- Fix every compile error the skill reports before finishing.\n"
                    f"- A failing build blocks the Discriminator — do not skip this step.\n"
                )
            try:
                (iter_dir / "generator_prompt.md").write_text(gen_prompt, encoding="utf-8")
            except OSError as exc:
                log.debug("Failed to dump generator prompt: %s", exc)
            self.generator_runner.transcript_dir = iter_dir
            gen_result = None
            try:
                # Start heartbeat thread to show progress
                stop_heartbeat = threading.Event()
                start_time = time.monotonic()

                def heartbeat(_evt: threading.Event = stop_heartbeat, _t0: float = start_time) -> None:
                    while not _evt.wait(timeout=300):
                        log.info("Generator running... (elapsed: %s)", _fmt_dur(time.monotonic() - _t0))

                heartbeat_thread = threading.Thread(target=heartbeat, daemon=True)
                heartbeat_thread.start()

                try:
                    gen_args = dict(
                        prompt=gen_prompt,
                        model=self.generator_model,
                        env=self.generator_env,
                        cwd=self.harmony_dir,
                        role="generator",
                    )
                    gen_result = self.generator_runner.run(**gen_args)
                    if not gen_result.success:
                        log.warning("Generator exited [red]%d[/] at iter %d", gen_result.returncode, n)
                        if gen_result.stderr.strip():
                            log.warning("Generator stderr: %s", gen_result.stderr.strip()[:500])
                finally:
                    stop_heartbeat.set()
                    heartbeat_thread.join(timeout=1)
            except AgentQuotaExhausted as exc:
                quota_exhausted = exc
                break
            except EcatTimeout:
                log.warning(
                    "[bold red]Generator timed out[/] at iter %d — evaluating partial changes",
                    n,
                )
                gen_result = None  # no stats available

            gen_s = gen_result.duration_s if gen_result is not None else 0.0
            gen_tok_in = gen_result.tokens_in if gen_result is not None else 0
            gen_tok_out = gen_result.tokens_out if gen_result is not None else 0
            gen_cache = gen_result.cache_read if gen_result is not None else 0
            total_gen_s += gen_s
            total_gen_tok_in += gen_tok_in
            total_gen_tok_out += gen_tok_out
            total_gen_cache += gen_cache
            _log_iter_stats(n, discrim_s, discrim_tok_in, discrim_tok_out, discrim_cache,
                            gen_s, gen_tok_in, gen_tok_out, gen_cache)
            _log_cumul_stats(n + 1, time.monotonic() - loop_start,
                             total_discrim_s, total_gen_s,
                             total_discrim_tok_in, total_discrim_tok_out, total_discrim_cache,
                             total_gen_tok_in, total_gen_tok_out, total_gen_cache)

            # Compact generator session every N iterations (between-iteration boundary).
            if (self.generator_compact_interval > 0
                    and (n + 1) % self.generator_compact_interval == 0):
                log.info(
                    "Iter %d: compacting generator session (every %d iters) …",
                    n, self.generator_compact_interval,
                )
                if self.generator_runner.compact(
                    model=self.generator_model,
                    env=self.generator_env,
                    cwd=self.harmony_dir,
                ):
                    log.info("Generator session compressed.")
                else:
                    log.debug("Generator compact returned False (no active session or unsupported runtime).")

            if self.chart_renderer is not None:
                _render_html(self.chart_renderer, reports, iter_states, self.entropy_threshold)

            h_prev = report.total_H
            pending_iter = n

            # Step 8: pause for user confirmation if callback is set
            if self.pause_callback is not None and not self.pause_callback(n, report.total_H):
                log.info("User requested stop after iter %d", n)
                break

        if quota_exhausted is not None:
            log.error(
                "[bold red]Run terminated:[/] %s quota exhausted — %s",
                quota_exhausted.role, quota_exhausted.reason,
            )
            log.info(
                "Wait for the quota to reset (or top up credits) and rerun "
                "`ecat refine` — the loop will resume from the last committed iter."
            )

        # Evaluate and commit any remaining pending changes from the last iteration
        # (skip the LLM-driven gradient produce when quota is dead — it'd just fail).
        if pending_iter is not None and h_prev is not None and quota_exhausted is None:
            log.info("Evaluating final pending changes from iter %d …", pending_iter)
            try:
                final_report = self.entropy_engine.evaluate(self.harmony_dir, iteration=pending_iter)
                prev_dir = self.runs_dir / str(pending_iter)
                # Dispatch agentic judges so final H includes soft scores, not just hard checks.
                try:
                    self.gradient_builder.produce(
                        final_report,
                        prompt_dump_path=prev_dir / "discriminator_prompt.md",
                    )
                except Exception as exc:
                    log.warning("Final agentic judge dispatch failed: %s", exc)
                _capture_diff(self.harmony_dir, prev_dir)
                delta = final_report.total_H - h_prev
                if delta < 0:
                    stat = _git_checkpoint(self.harmony_dir, pending_iter, final_report.total_H)
                    files, ins, dels = _parse_stat(stat)
                    stat_suffix = f" | {files} files +{ins}/-{dels} lines" if stat else ""
                    log.info(
                        "[bold green]Accepted[/] iter %d changes: H %.4f → %.4f ([green]ΔH = %.4f[/])%s",
                        pending_iter, h_prev, final_report.total_H, delta, stat_suffix,
                    )
                    reports.append(final_report)
                    self.timeseries.append(final_report)
                else:
                    log.info(
                        "iter %d changes not committed: H %.4f → %.4f (ΔH = +%.4f)",
                        pending_iter, h_prev, final_report.total_H, delta,
                    )
            except Exception as exc:
                log.warning("Final evaluation failed: %s", exc)

        return reports


# ── helpers ──────────────────────────────────────────────────────────────────


def _write_gradient(iter_dir: Path, gradient: TextGradient) -> None:
    (iter_dir / "gradient.json").write_text(
        json.dumps(gradient.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _write_error(iter_dir: Path, message: str) -> None:
    (iter_dir / "error.json").write_text(
        json.dumps({"error": message}, ensure_ascii=False), encoding="utf-8"
    )


def _capture_diff(harmony_dir: Path, iter_dir: Path) -> None:
    try:
        result = subprocess.run(
            ["git", "diff", "--", ".", *git_exclude_pathspecs()],
            cwd=harmony_dir,
            capture_output=True,
            text=True,
            timeout=30,
        )
        (iter_dir / "diff.patch").write_text(result.stdout, encoding="utf-8")
    except Exception as exc:
        log.debug("git diff failed: %s", exc)


def _check_git(harmony_dir: Path) -> None:
    """Warn if harmony_dir is not a git repo — init should have handled this."""
    if not (harmony_dir / ".git").is_dir():
        log.warning(
            "harmony_dir %s is not a git repo — revert/diff will not work. "
            "Run `ecat init` or `git init` in the harmony directory.",
            harmony_dir,
        )


def _git_checkpoint(harmony_dir: Path, iteration: int, total_h: float) -> str:
    """Commit all changes as a checkpoint after the generator runs."""
    try:
        # 只入栈 ECAT 关心的路径：`git add .` 会把整个工作区（构建产物、依赖、
        # .claude/ agent 配置）都扫进本轮提交，导致改动量归因错误、且
        # revert_on_regression 会回滚掉无关的既有改动。
        #
        # 必须"先全量 add、再 reset 掉忽略目录"，不能用 `git add -- . :(exclude)X`：
        # git 把 exclude pathspec 也当成显式命名该路径，若 X 已被 .gitignore 忽略
        # 就退出 1，整个 checkpoint 静默失败（实测连续 8 轮迭代一个提交都没产生）。
        excludes = git_exclude_pathspecs()
        subprocess.run(
            ["git", "add", "-A", "--", "."],
            cwd=harmony_dir, check=True, capture_output=True, timeout=30,
        )
        subprocess.run(
            ["git", "reset", "-q", "--", *skip_dir_names()],
            cwd=harmony_dir, check=True, capture_output=True, timeout=30,
        )
        diff_stat = subprocess.run(
            ["git", "diff", "--cached", "--stat", "--shortstat", "--", ".", *excludes],
            cwd=harmony_dir, capture_output=True, text=True, timeout=30,
        )
        stat_line = diff_stat.stdout.strip().splitlines()[-1] if diff_stat.stdout.strip() else ""

        msg = f"ecat: iter {iteration} | H={total_h:.4f}"
        subprocess.run(
            ["git", "commit", "-m", msg, "--allow-empty"],
            cwd=harmony_dir, check=True, capture_output=True, text=True, timeout=30,
        )

        rev = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=harmony_dir, capture_output=True, text=True, timeout=10,
        )
        short_hash = rev.stdout.strip() if rev.returncode == 0 else "?"

        log.info(
            "[bold green]Committed[/] iter %d → [cyan]%s[/]",
            iteration, short_hash,
        )
        return stat_line
    except Exception as exc:
        # 不能降级成 debug：checkpoint 失败等于本轮没有回滚点，且 revert_on_regression
        # 会回滚到过时的提交。必须让用户看见。
        log.error("git checkpoint failed — no rollback point for this iteration: %s", exc)
        return ""


def _git_revert(harmony_dir: Path) -> None:
    """Revert harmony_dir to the last committed state."""
    try:
        excludes = git_exclude_pathspecs()
        subprocess.run(
            ["git", "checkout", "--", ".", *excludes],
            cwd=harmony_dir, check=True, capture_output=True, timeout=30,
        )
        # clean 同样限定范围：否则会删掉 .claude/ 下未跟踪的 agent 配置
        subprocess.run(
            ["git", "clean", "-fd", "--", ".", *excludes],
            cwd=harmony_dir, check=True, capture_output=True, timeout=30,
        )
        log.info("Reverted harmony/ to last checkpoint")
    except Exception as exc:
        log.error("git revert failed: %s", exc)


def _empty_gradient() -> TextGradient:
    return TextGradient(summary="(gradient unavailable)")


_GRADIENT_MAX_CHARS = 120
_SEV_COLOR: dict[str, str] = {"high": "red", "medium": "yellow", "low": "dim"}


def _log_gradient_summary(gradient: TextGradient) -> None:
    summary = gradient.summary
    truncated = summary[:_GRADIENT_MAX_CHARS] + "…" if len(summary) > _GRADIENT_MAX_CHARS else summary
    log.info("[bold]Gradient:[/] %s", truncated)
    for item in gradient.work_list[:5]:
        color = _SEV_COLOR.get(item.severity, "white")
        issues_text = "; ".join(item.issues[:2])
        if len(item.issues) > 2:
            issues_text += " …"
        log.info("  [%s]\\[%s][/] %s — %s", color, item.severity, item.file, issues_text)
    remaining = len(gradient.work_list) - 5
    if remaining > 0:
        log.info("  [dim]… and %d more items[/]", remaining)


def _format_compile_errors(report: EntropyReport) -> str:
    """Extract compile error lines from the entropy report details."""
    compile_detail = report.details.get("compile", {})
    if not isinstance(compile_detail, dict):
        return ""
    errors = compile_detail.get("errors", [])
    if not isinstance(errors, list) or not errors:
        return ""
    lines = [f"  {e}" for e in errors[:30]]
    count = compile_detail.get("error_count", len(errors))
    return f"Compile errors ({count}):\n" + "\n".join(lines)


def _format_work_list_for_generator(work_list: list[WorkItem], max_items: int = 15) -> str:
    """Format top WorkItems as numbered plain text for the Generator prompt.

    Each item shows ``N. [severity] file — issues`` plus an optional
    ``(suggested_skills: ...)`` hint. The literal key ``suggested_skills``
    matches the trigger phrase in the a2h-migration-worker agent rules, so a
    delegated subagent invokes those skills before writing code.
    """
    if not work_list:
        return "(no extracted items — implement the improvements described in the Strategic direction above)"
    lines: list[str] = []
    for i, item in enumerate(work_list[:max_items], 1):
        issue_text = "; ".join(item.issues[:15])
        skills_part = ""
        if item.suggested_skills:
            skills_part = f"  (suggested_skills: {', '.join(item.suggested_skills)})"
        lines.append(f"{i}. [{item.severity}] {item.file} — {issue_text}{skills_part}")
    remaining = len(work_list) - max_items
    if remaining > 0:
        lines.append(f"   ... and {remaining} more items (lower priority)")
    return "\n".join(lines)


def _h_color(h: float) -> str:
    if h < 0.3:
        return "green"
    if h < 0.6:
        return "yellow"
    return "red"


def _fmt_dur(seconds: float) -> str:
    """Format seconds as 'Xm YYs' or 'Xs'."""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    return f"{s // 60}m{s % 60:02d}s"


def _fmt_tok(n: int) -> str:
    """Format token count as '1.2k' or '1.5M'."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


def _parse_stat(stat: str) -> tuple[int, int, int]:
    """Parse 'N files changed, M insertions(+), K deletions(-)' -> (files, ins, dels)."""
    files = ins = dels = 0
    m = re.search(r"(\d+) files? changed", stat)
    if m:
        files = int(m.group(1))
    m = re.search(r"(\d+) insertions?", stat)
    if m:
        ins = int(m.group(1))
    m = re.search(r"(\d+) deletions?", stat)
    if m:
        dels = int(m.group(1))
    return files, ins, dels


def _tok_str(tok_in: int, tok_out: int, cache: int) -> str:
    s = f"{_fmt_tok(tok_in)} in / {_fmt_tok(tok_out)} out"
    if cache:
        s += f" (+{_fmt_tok(cache)} cached)"
    return s


def _log_iter_stats(
    n: int,
    discrim_s: float,
    discrim_tok_in: int,
    discrim_tok_out: int,
    discrim_cache: int,
    gen_s: float,
    gen_tok_in: int,
    gen_tok_out: int,
    gen_cache: int,
) -> None:
    parts = [f"discriminator: {_fmt_dur(discrim_s)} | {_tok_str(discrim_tok_in, discrim_tok_out, discrim_cache)}"]
    if gen_s > 0:
        parts.append(f"generator: {_fmt_dur(gen_s)} | {_tok_str(gen_tok_in, gen_tok_out, gen_cache)}")
        parts.append(f"iteration total: {_fmt_dur(discrim_s + gen_s)}")
    log.info("[dim][iter %d] %s[/]", n, "  |  ".join(parts))


def _log_cumul_stats(
    iters: int,
    total_s: float,
    total_discrim_s: float,
    total_gen_s: float,
    total_discrim_tok_in: int,
    total_discrim_tok_out: int,
    total_discrim_cache: int,
    total_gen_tok_in: int,
    total_gen_tok_out: int,
    total_gen_cache: int,
) -> None:
    parts = [
        f"{iters} iteration(s)",
        f"{_fmt_dur(total_s)} total",
        f"discriminator: {_fmt_dur(total_discrim_s)} | {_tok_str(total_discrim_tok_in, total_discrim_tok_out, total_discrim_cache)}",
    ]
    if total_gen_s > 0:
        parts.append(
            f"generator: {_fmt_dur(total_gen_s)} | {_tok_str(total_gen_tok_in, total_gen_tok_out, total_gen_cache)}"
        )
    log.info("[dim][cumulative] %s[/]", " | ".join(parts))


def _state_color(state: str) -> str:
    if state == "ok":
        return "green"
    if state == "regression_grace":
        return "yellow"
    return "red"


def _render_html(
    renderer: ChartRenderer,
    reports: list[EntropyReport],
    states: list[str],
    epsilon: float,
) -> None:
    try:
        iters = [r.iteration for r in reports]
        hs = [r.total_H for r in reports]
        bds = [r.breakdown for r in reports]
        renderer.render_html(iters, hs, bds, epsilon=epsilon, states=states)
    except Exception as exc:
        log.warning("chart render failed: %s", exc)


def _load_memory_context(memory_dir: Path | None) -> str:
    """Render the generator memory-tree prompt section (top index + drill-down).

    Thin wrapper over ecat.memory.tree.render_injection. Returns "" when
    memory_dir is None, missing, or empty — preserving the disabled-feature
    and claude_code-runtime behaviour unchanged.
    """
    return render_injection(memory_dir)


_DEFAULT_GEN_PROMPT: str | None = None  # loaded lazily from prompts/generator.txt


def _get_gen_prompt() -> str:
    global _DEFAULT_GEN_PROMPT  # noqa: PLW0603
    if _DEFAULT_GEN_PROMPT is None:
        _DEFAULT_GEN_PROMPT = load_prompt("generator")
    return _DEFAULT_GEN_PROMPT
