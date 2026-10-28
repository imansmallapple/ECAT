"""GradientBuilder — runs the Discriminator agent and parses its text gradient output.

Named *Builder* (not *Generator*) to avoid confusion with the Generator agent in
the adversarial loop. This module assembles the TextGradient artifact that is
then fed to the Generator.
"""

from __future__ import annotations

import json
import logging
import re as _re
from pathlib import Path
from typing import Any, Literal, cast

from ecat.entropy.base import EntropyPlugin, EntropyReport
from ecat.gradient.extractors import extract_work_list
from ecat.gradient.schema import TextGradient, WorkItem
from ecat.prompts import load_prompt
from ecat.runner.base import AgentRunner

log = logging.getLogger(__name__)


class GradientBuilder:
    """Produces a TextGradient by prompting the Discriminator runner."""

    def __init__(
        self,
        runner: AgentRunner,
        model: str,
        plugins: list[EntropyPlugin],
        env: dict[str, str] | None = None,
        cwd: Path | None = None,
        prompt_template: str | None = None,
        skill_root: Path | None = None,
        catalog_path: Path | None = None,
        weights: dict[str, float] | None = None,
        subagent_tool: str = "Agent",
        memory_evolution: bool = False,
        memory_dir: Path | None = None,
        judge_max_issues: int = 10,
        max_items_per_iter: int = 15,
    ) -> None:
        self.runner = runner
        self.model = model
        self.plugins = plugins
        self.env = env
        self.cwd = cwd
        self.template = prompt_template or load_prompt("discriminator")
        self.skill_root = skill_root
        self.catalog_path = catalog_path
        # Dimensions without a positive weight are hidden from the
        # discriminator prompt (breakdown table, details JSON, work_list,
        # judge dispatch) so it doesn't waste tokens on dimensions the user
        # disabled. "Disabled" means weight <= 0 OR absent from the weights
        # map (a commented-out / omitted entry) — matching how the engine
        # treats weights via `.get(name, 0.0)`. Keying on `weights.items()`
        # alone would miss omitted dims, which still run and would leak in.
        self.zero_weight_dims: set[str] = (
            {p.name for p in plugins if (weights or {}).get(p.name, 0.0) <= 0}
        )
        # Full weight map used to recompute total_H after agentic verdicts
        # come back from the discriminator's subagent-dispatch path.
        self.weights = weights or {}
        # Tool name for subagent dispatch — "Agent" for Claude Code, "task"
        # for opencode. Set by the caller based on cfg.discriminator.runtime.
        self.subagent_tool = subagent_tool
        self.memory_evolution = memory_evolution
        self.memory_dir = memory_dir
        self.judge_max_issues = judge_max_issues
        self.max_items_per_iter = max_items_per_iter

    def produce(
        self,
        report: EntropyReport,
        *,
        prompt_dump_path: Path | None = None,
        h_delta: float | None = None,
    ) -> TextGradient:
        from ecat.gradient.skill_catalog import get_or_refresh_catalog

        skill_catalog_hint = ""
        valid_skill_names: set[str] = set()
        if self.skill_root and self.catalog_path:
            try:
                catalog = get_or_refresh_catalog(self.skill_root, self.catalog_path)
                valid_skill_names = {s["name"] for s in catalog if s.get("name")}
                skill_catalog_hint = (
                    f"The skill catalog is pre-built and available at: {self.catalog_path.resolve()}\n"
                    f"Use /recommend-generator-skills to read it — no Glob or per-file reads needed."
                )
            except Exception as exc:
                log.warning("Skill catalog refresh failed: %s", exc)

        # Identify agentic plugins whose verdict the discriminator should
        # collect via subagent dispatch (i.e. those EntropyEngine deferred).
        deferred_agentic = _select_deferred_agentic(self.plugins, report, self.weights)
        judge_dispatch_section = _build_judge_dispatch_section(
            deferred_agentic, report, self.subagent_tool, self.judge_max_issues,
        )
        verdicts_section_instruction = _build_verdicts_section_instruction(
            deferred_agentic,
        )

        breakdown_table = "\n".join(
            f"  {name}: {score:.4f}"
            for name, score in sorted(
                report.breakdown.items(), key=lambda x: -x[1]
            )
            if name not in self.zero_weight_dims
        )
        prompt = self.template.format(
            iteration=report.iteration,
            total_H=report.total_H,
            breakdown_table=breakdown_table,
            details_json=_build_sorted_details(
                report, max_chars=6000, exclude=self.zero_weight_dims,
            ),
            work_list_summary=_format_work_list_summary(
                extract_work_list(report, self.plugins, self.zero_weight_dims),
                max_items=self.max_items_per_iter,
            ),
            work_list_count=min(self.max_items_per_iter, len(extract_work_list(report, self.plugins, self.zero_weight_dims))),
            work_list_total=len(extract_work_list(report, self.plugins, self.zero_weight_dims)),
            skill_catalog_hint=skill_catalog_hint,
            judge_dispatch_section=judge_dispatch_section,
            verdicts_section_instruction=verdicts_section_instruction,
            memory_section=_build_memory_section(
                self.memory_evolution, self.memory_dir, h_delta,
            ),
        )

        if prompt_dump_path is not None:
            try:
                prompt_dump_path.parent.mkdir(parents=True, exist_ok=True)
                prompt_dump_path.write_text(prompt, encoding="utf-8")
            except OSError as exc:
                log.debug("Failed to dump discriminator prompt: %s", exc)

        run_args = dict(
            prompt=prompt,
            model=self.model,
            env=self.env,
            cwd=self.cwd,
            role="discriminator",
        )
        result = self.runner.run(**run_args)

        # Accumulate discriminator runner token usage into the report so the
        # driver can surface it in iter/cumulative stats.
        report.tokens_in += result.tokens_in
        report.tokens_out += result.tokens_out
        report.cache_read += result.cache_read

        if not result.success:
            log.warning(
                "Discriminator exited %d; using fallback gradient",
                result.returncode,
            )
            if result.stderr.strip():
                log.warning("Discriminator stderr: %s", result.stderr.strip()[:500])
            return _fallback_gradient(report, extract_work_list(report, self.plugins, self.zero_weight_dims))

        # Parse the discriminator's response.  Primary path is a single JSON
        # document (per discriminator.txt protocol); markdown sections are
        # tolerated as fallback when the LLM ignores format instructions.
        parsed = _parse_discriminator_response(result.stdout, report, stream=result.raw_stream)

        # Apply deferred agentic verdicts BEFORE extracting work_list so that
        # WorkItem.issues can include verdict-derived issues from each plugin's
        # extract_work_items() (which reads details["agentic_verdict"]).
        if deferred_agentic:
            verdicts = parsed.get("verdicts") or {}
            if isinstance(verdicts, dict):
                _apply_agentic_verdicts(report, deferred_agentic, verdicts, self.weights)

        work_list = extract_work_list(report, self.plugins, self.zero_weight_dims)

        if valid_skill_names:
            _filter_work_list_skills(work_list, valid_skill_names)

        overrides = parsed.get("priority_overrides", [])
        work_list = _apply_priority_overrides(
            work_list, overrides if isinstance(overrides, list) else [],
        )

        skill_map = parsed.get("suggested_skills", {})
        if isinstance(skill_map, dict) and skill_map:
            if valid_skill_names:
                skill_map = _filter_invalid_skills(skill_map, valid_skill_names)
            if skill_map:
                work_list = _apply_suggested_skills(work_list, skill_map)

        summary = parsed.get("summary", "")
        insights_raw = parsed.get("insights", [])
        return TextGradient(
            summary=str(summary),
            work_list=work_list,
            insights=insights_raw if isinstance(insights_raw, list) else [],
        )



def _build_memory_section(
    memory_evolution: bool,
    memory_dir: Path | None,
    h_delta: float | None,
) -> str:
    """Return the memory-writing instructions for the discriminator prompt.

    Returns empty string when memory_evolution is disabled or no memory_dir is set.
    """
    if not memory_evolution or memory_dir is None:
        return ""
    delta_hint = ""
    if h_delta is not None:
        if h_delta < -0.005:
            delta_hint = f" (H ↓{abs(h_delta):.4f} — generator made progress)"
        elif h_delta > 0.01:
            delta_hint = f" (H ↑{h_delta:.4f} — generator regressed)"
    mem_dir = memory_dir.resolve().as_posix()
    return (
        f"## Generator memory{delta_hint}\n\n"
        "Write ≤2 entries ONLY for **generalizable** ArkTS/HarmonyOS fix patterns. "
        "Skip if nothing new or app-specific.\n\n"
        f"Memory is a multi-level category tree under `{mem_dir}/`. To add an entry:\n"
        f"1. **Open `{mem_dir}/MEMORY.md`** (top-level categories). Pick the best-fitting "
        "category; if none fits, create a new top-level category (kebab-case, reuse "
        "existing granularity).\n"
        "2. **Open that category's `_index.md`** to see its direct entries and "
        "sub-directories. Reuse an existing sub-path before creating one. If a similar "
        "entry already exists (same concept, any slug), update it instead of adding a file.\n"
        "3. **Placement:** put `feedback_<kebab-slug>.md` at the most specific level that "
        "fits. Stay flat at the current level unless it already has **>8 direct feedback "
        "files** (sub-directory pointers don't count) AND your entry belongs to a nameable "
        "sub-topic — only then create a sub-directory (max **depth 3**: `category/sub/subsub`) "
        "and move related entries into it (delete the originals), removing their old lines "
        "from the parent `_index.md`.\n"
        "4. **Write** the file:\n"
        "```\n---\nname: <kebab-slug>\ndescription: <one-line>\n"
        "metadata:\n  type: feedback\n---\n"
        "<Rule>. **Why:** <reason>. **How to apply:** <when/where>.\n```\n"
        "5. **Update every `_index.md` along the path** (add the entry's hook line / "
        "register any new sub-dir), and register a new top-level category (with a "
        f"one-line hint) in `{mem_dir}/MEMORY.md` if you created one. "
        "Use kebab-case slugs only."
    )


def _build_judge_subagent_suffix(judge_max_issues: int) -> str:
    n = judge_max_issues
    return (
        f"\n\n## Early-stop rule\n"
        f"Stop exploring as soon as you have found **{n} distinct issues**.\n"
        f"Do not read any more files once you reach this limit — output your verdict immediately.\n"
        f"The first {n} issues are enough signal for the Generator to act on.\n"
        f"\n## Required output\n"
        f"After exploring the codebase (or hitting the {n}-issue limit), output ONLY this JSON as your final message:\n"
        f'{{\"score\": 0.75, \"rationale\": \"one-sentence explanation\", \"issues\": [\"missing FeatureX implementation\", \"FeatureY is stub/TODO\"]}}\n'
        f"score: float 0.0 (fully implemented, no issues) to 1.0 (severely missing/broken).\n"
        f"issues: list specific missing feature names or broken pattern names found (cap at {n} items).\n"
        f"\n## Hard rule on score / issues consistency\n"
        f"- If score > 0.0, `issues` MUST contain at least one specific item.\n"
        f"- If you cannot articulate any specific issue, score MUST be exactly 0.0.\n"
        f"- Do NOT hedge with a small non-zero score and an empty issues list.\n"
        f"- Each issue should reference a concrete file / method / feature name.\n"
        f"Do not output anything else in the final message."
    )


def _select_deferred_agentic(
    plugins: list[EntropyPlugin],
    report: EntropyReport,
    weights: dict[str, float],
) -> list[EntropyPlugin]:
    """Agentic plugins whose verdict the discriminator should collect via dispatch.

    Excludes: zero-weight dims, dims absent from the breakdown, ones already judged
    in-engine, and — critically — ones whose hard-check SKIPPED. A skipped hard-check
    (e.g. ui_align with no fact-tree / no device) captured no artifacts, so there is
    nothing for the judge to read; dispatching it risks the judge scoring stale images
    from a previous run. A skipped dimension stays at its 0.0 score.

    The one exception is ``skipped == "awaiting_llm_parse"`` (feature_checklist): that
    is not a real skip but a request for the parser subagent to write the manifest this
    round. The plugin sets ``judge_subagent_type``/``judge_prompt`` to the parser BEFORE
    the deferred dispatch renders prompt + agent, so exempting it here is what lets the
    checklist parser run at all. It still contributes 0 this round (same as any skip) —
    we only keep it in the dispatch list.
    """
    return [
        p for p in plugins
        if p.judge_mode == "agentic"
        and p.judge_prompt
        and weights.get(p.name, 0.0) > 0
        and p.name in report.breakdown
        and "agentic_verdict" not in (report.details.get(p.name) or {})
        and not (
            (report.details.get(p.name) or {}).get("skipped")
            and (report.details.get(p.name) or {}).get("skipped") != "awaiting_llm_parse"
        )
    ]


def _compact_dispatch_details(
    details: dict[str, object], *, list_cap: int = 40, max_chars: int = 6000
) -> str:
    """Bound dispatch-details size across app sizes — pure Python, no LLM.

    Details grow with app size mainly via list length (more features / candidates
    / pages), so cap each list by item count (+ "…N more") rather than chopping
    the JSON tail at a fixed char count (which silently drops whole items and
    breaks structure). Before capping, PROBLEM items (MISSING / PARTIAL / stub /
    todo / …) are stably moved to the front, so the cap only drops the
    verified/uninteresting tail — actionable items survive. Paths are left intact
    — the judge opens them. The char cap is only a pathological backstop.
    """
    problem_re = _re.compile(
        r"missing|partial|stub|todo|fixme|fail|severe|incomplete|degraded|absent|"
        r"not[ _]implemented",
        _re.IGNORECASE,
    )

    def is_problem(item: object) -> bool:
        if isinstance(item, str):
            return bool(problem_re.search(item))
        if isinstance(item, dict):
            return any(isinstance(x, str) and problem_re.search(x) for x in item.values())
        return False

    def shrink(v: object) -> object:
        if isinstance(v, list):
            # Stable problem-first ordering (False<True), then cap.
            ordered = sorted(v, key=lambda x: not is_problem(x))
            head: list[object] = [shrink(x) for x in ordered[:list_cap]]
            if len(ordered) > list_cap:
                head.append(f"...(+{len(ordered) - list_cap} more)")
            return head
        if isinstance(v, dict):
            return {k: shrink(x) for k, x in v.items()}
        return v

    s = json.dumps(shrink(details), default=str)
    return s if len(s) <= max_chars else s[:max_chars] + "...(truncated)"


def _build_judge_dispatch_section(
    deferred_agentic: list[EntropyPlugin],
    report: EntropyReport,
    subagent_tool: str,
    judge_max_issues: int = 10,
) -> str:
    """Build the per-dimension subagent dispatch instructions for the prompt.

    Empty string when no agentic plugins are deferred — keeps the prompt clean
    for runs where the engine already judged everything (legacy mode).
    """
    if not deferred_agentic:
        return ""
    lines: list[str] = [
        "## Step 0 (REQUIRED before writing your response): Dispatch entropy judges",
        "",
        f"Before everything else, dispatch the {len(deferred_agentic)} judge "
        f"subagent(s) below IN A SINGLE RESPONSE so they run in parallel "
        f"(use the exact subagent_type shown in each block):",
        "",
    ]
    for p in deferred_agentic:
        hard_score = report.breakdown.get(p.name, 0.0)
        details = report.details.get(p.name, {}) or {}
        # Compact details so the dispatch stays bounded regardless of app size
        # (pure Python, no LLM). Growth comes from list length, so cap each list
        # by item count instead of chopping the JSON tail at a fixed char count.
        details_str = _compact_dispatch_details(details)
        rubric = (p.judge_prompt or "").strip()
        # Per-plugin subagent type: most dimensions use the generic read-only
        # "entropy-judge"; device-driven dimensions (ui_align) override with a
        # Bash-capable judge agent.
        judge_agent = getattr(p, "judge_subagent_type", "entropy-judge")
        lines.append(f"### Dispatch {judge_agent} for `{p.name}`")
        lines.append("")
        lines.append("```")
        lines.append(
            f'{subagent_tool}(subagent_type="{judge_agent}", '
            f'description="Judge {p.name} dimension", '
            f'prompt="""'
        )
        lines.append(
            "Start evaluating immediately. The HarmonyOS codebase is in the current "
            "working directory — use Read/Glob/Grep tools to explore it. "
            "Ignore non-source dirs: any dot-dir (.git/.idea/.hvigor/.claude/.ecat/...) "
            "and oh_modules/node_modules/build — they are tools/deps/caches, not project code. "
            "Do NOT ask for clarification or additional context."
        )
        lines.append("")
        lines.append(f"Dimension: {p.name}")
        lines.append(f"Hard-check score: {hard_score:.3f}")
        lines.append(f"Hard-check details: {details_str}")
        lines.append("")
        lines.append("## What to evaluate")
        lines.append(rubric)
        lines.append(_build_judge_subagent_suffix(judge_max_issues))
        lines.append('""")')
        lines.append("```")
        lines.append("")
    lines.append(
        "After all subagents return, write the response below using EXACTLY "
        "the section headers, starting with `## Agentic Verdicts`."
    )
    return "\n".join(lines)


def _build_verdicts_section_instruction(deferred_agentic: list[EntropyPlugin]) -> str:
    """Hint listing the dimension names that must appear as keys in `verdicts`."""
    if not deferred_agentic:
        return ""
    names = ", ".join(f"`{p.name}`" for p in deferred_agentic)
    return f"After dispatching the entropy-judge subagents above, paste each verdict as a value under the `verdicts` object in your final JSON. Required keys: {names}."


def _parse_agentic_verdicts(sections: dict[str, str]) -> dict[str, dict[str, Any]]:
    """Parse `## Agentic Verdicts` section into {dim_name: verdict_dict}.

    Each dimension is expected as `### dim_name` followed by a JSON object.
    Tolerates optional markdown code fences (```json ... ```) and field-name
    drift (`reasoning` vs `rationale`, `key_issues` vs `issues`) — different
    runtimes / models output the verdict differently. Lenient by design;
    never raises.
    """
    text = sections.get("agentic verdicts", "").strip()
    if not text:
        return {}
    result: dict[str, dict[str, Any]] = {}
    # Match `### dim_name` then capture the FIRST balanced JSON object that
    # follows, optionally wrapped in ```json fences. The fence prefix
    # `(?:```\w*\s*\n)?` is non-capturing and optional.
    pattern = _re.compile(
        r"###\s+([A-Za-z_][\w\-]*)\s*\n+\s*"
        r"(?:```[a-zA-Z]*\s*\n)?\s*"
        r"(\{[\s\S]*?\})\s*"
        r"(?:```)?",
    )
    for match in pattern.finditer(text):
        dim_name = match.group(1).strip()
        try:
            data = json.loads(match.group(2))
            score = max(0.0, min(1.0, float(data.get("score", 0.5))))
            # Field-name aliases: some models output `reasoning` instead of
            # `rationale`, or `key_issues` instead of `issues`. Accept both.
            rationale = str(
                data.get("rationale")
                or data.get("reasoning")
                or data.get("explanation")
                or ""
            )
            issues_raw = (
                data.get("issues")
                or data.get("key_issues")
                or data.get("findings")
                or []
            )
            issues = list(issues_raw) if isinstance(issues_raw, list) else []
            result[dim_name] = {
                "score": score,
                "rationale": rationale,
                "issues": issues,
            }
        except (json.JSONDecodeError, ValueError, TypeError):
            continue
    return result


def _apply_agentic_verdicts(
    report: EntropyReport,
    deferred_agentic: list[EntropyPlugin],
    verdicts: dict[str, dict[str, Any]],
    weights: dict[str, float],
) -> None:
    """Mutate report: blend verdicts into breakdown, attach to details, recompute total_H."""
    from ecat.entropy.engine import _blend  # local import avoids cycle at module load

    for plugin in deferred_agentic:
        v = verdicts.get(plugin.name)
        hard_score = report.breakdown.get(plugin.name, 0.0)
        if v is None:
            log.warning(
                "Discriminator did not return verdict for %s — keeping hard score %.3f",
                plugin.name, hard_score,
            )
            continue
        soft_score = float(v["score"])
        issues = list(v.get("issues") or [])
        if soft_score > 0 and not issues:
            log.warning(
                "%s subagent gave score=%.3f with empty issues list — clamping to 0.0",
                plugin.name, soft_score,
            )
            soft_score = 0.0
            v["score"] = 0.0
        hw = plugin.hard_weight
        blended = _blend(hard_score, soft_score, hw)
        report.breakdown[plugin.name] = blended
        if plugin.name not in report.details:
            report.details[plugin.name] = {}
        report.details[plugin.name]["agentic_verdict"] = v
        # Merge soft (agentic) issues with hard-check fails so an agentic-mode
        # dimension whose score this round came from the hard layer (soft empty)
        # still prints its real issue instead of "none".
        hard_fails = [
            f for f in ((report.details.get(plugin.name) or {}).get("fails") or [])
            if isinstance(f, str)
        ]
        display_issues = issues + [f for f in hard_fails if f not in issues]
        log.info(
            "[cyan]%s[/] agentic verdict: sub_entropy=%.3f "
            "(hard (rule check)=%.3f, soft (agentic judge)=%.3f) | issues: %s",
            plugin.name, blended, hard_score, soft_score,
            (", ".join(display_issues[:3]) + (" …" if len(display_issues) > 3 else "")) or "none",
        )

    # Recompute total_H using the same weight normalisation as EntropyEngine.
    # Deterministically-skipped dimensions (e.g. feature_checklist unconfigured)
    # are excluded from the denominator so they don't dilute the other dims.
    if weights:
        from ecat.entropy.engine import _normalized_total_h

        report.total_H = _normalized_total_h(
            report.breakdown, report.details, weights
        )


def _last_json_verdict_from_stream(stream: str) -> dict[str, object] | None:
    """Scan a Claude Code stream-json stream and return the LAST assistant turn
    that parses as a valid discriminator JSON document.

    Handles models that write the JSON verdict mid-turn and then append prose
    (e.g. "the JSON above is authoritative"), which leaves the final message —
    and thus RunResult.stdout — without parseable JSON. Taking the LAST valid
    turn keeps the authoritative verdict (written after the deferred judges
    returned), not an earlier preliminary one. Returns None if none parse."""
    found: dict[str, object] | None = None
    for line in stream.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") != "assistant":
            continue
        content = (ev.get("message") or {}).get("content") or []
        if not isinstance(content, list):
            continue
        texts = [b["text"] for b in content
                 if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)]
        if not texts:
            continue
        data = _try_parse_json_document("\n".join(texts))
        if data is not None:
            found = data
    return found


def _parse_discriminator_response(
    raw: str, report: EntropyReport, *, stream: str | None = None,
) -> dict[str, object]:
    """Parse the discriminator's final message.

    Primary path: single JSON document per discriminator.txt protocol.
    Recovery (only if the primary parse fails and *stream* is given): some models
    emit the JSON verdict mid-turn then append a prose closing remark, so the last
    message alone doesn't parse — recover it from the last JSON-bearing turn.
    Fallback: markdown sections (older format, kept for resilience when the LLM
    ignores instructions).

    Returns shape:
      {"summary": str, "verdicts": {dim: {...}}, "priority_overrides": list,
       "suggested_skills": dict[file -> list[skill]], "insights": list[str]}
    """
    data = _try_parse_json_document(raw)
    if data is None and stream:
        data = _last_json_verdict_from_stream(stream)
    if data is not None:
        return _normalise_json_response(data, report)
    log.warning(
        "Discriminator output is not valid JSON — falling back to markdown section parser",
    )
    sections = _split_sections(raw)
    parsed = _parse_discriminator_output_from_sections(sections, report)
    parsed["verdicts"] = _parse_agentic_verdicts(sections)
    return parsed


# Top-level fields that identify a valid discriminator JSON response.  A
# parsed dict must contain at least one of these to be accepted; otherwise we
# fall back to the markdown section parser (avoids matching stray inner JSON
# fragments like `{"score": 0.18}` that may appear inside narrative text).
_DISCRIMINATOR_EXPECTED_KEYS = frozenset((
    "summary", "verdicts", "priority_overrides", "suggested_skills", "insights",
))


def _try_parse_json_document(raw: str) -> dict[str, object] | None:
    """Find and json.loads the outermost discriminator JSON object in `raw`.

    Tolerates leading prose, trailing prose, and ```json``` fences.  Returns
    None if no balanced top-level object parses cleanly OR the parsed object
    lacks any expected discriminator field.
    """
    if not raw or not raw.strip():
        return None
    text = raw.strip()
    fence_match = _re.search(r"```(?:json)?\s*\n([\s\S]*?)\n```", text)
    if fence_match:
        text = fence_match.group(1).strip()

    # Scan every `{...}` balanced object; return the first one whose top-level
    # keys overlap our schema. Models sometimes prepend a brief preamble before
    # the actual response object, so we don't insist on the very first `{`.
    pos = 0
    while pos < len(text):
        start = text.find("{", pos)
        if start < 0:
            return None
        depth = 0
        in_str = False
        esc = False
        end = -1
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end < 0:
            return None
        try:
            obj = json.loads(text[start:end + 1])
            if isinstance(obj, dict) and _DISCRIMINATOR_EXPECTED_KEYS & obj.keys():
                return obj
        except json.JSONDecodeError:
            pass
        pos = end + 1
    return None


def _normalise_json_response(data: dict[str, object], report: EntropyReport) -> dict[str, object]:
    """Coerce the LLM's JSON into the canonical shape used downstream."""
    summary = str(data.get("summary") or "").strip() or _fallback_summary(report)

    verdicts_raw = data.get("verdicts") or {}
    verdicts: dict[str, dict[str, Any]] = {}
    if isinstance(verdicts_raw, dict):
        for dim, v in verdicts_raw.items():
            if not isinstance(v, dict):
                continue
            try:
                score = max(0.0, min(1.0, float(v.get("score", 0.0))))
            except (TypeError, ValueError):
                continue
            rationale = str(
                v.get("rationale") or v.get("reasoning") or v.get("explanation") or ""
            )
            issues_raw = v.get("issues") or v.get("key_issues") or v.get("findings") or []
            issues = [str(i) for i in issues_raw] if isinstance(issues_raw, list) else []
            verdicts[str(dim)] = {"score": score, "rationale": rationale, "issues": issues}

    overrides: list[object] = []
    overrides_raw = data.get("priority_overrides") or []
    if isinstance(overrides_raw, list):
        for entry in overrides_raw:
            if not isinstance(entry, dict):
                continue
            sev = str(entry.get("severity") or "").strip().lower()
            if sev not in ("high", "medium", "low"):
                continue
            overrides.append({
                "file": str(entry.get("file") or ""),
                "severity": sev,
                "reason": str(entry.get("reason") or ""),
            })

    skill_map: dict[str, list[str]] = {}
    skills_raw = data.get("suggested_skills") or []
    if isinstance(skills_raw, list):
        for entry in skills_raw:
            if not isinstance(entry, dict):
                continue
            f = str(entry.get("file") or "").strip()
            s = str(entry.get("skill") or "").strip()
            if f and s:
                skill_map.setdefault(f, []).append(s)

    insights_raw = data.get("insights") or []
    insights = [str(i) for i in insights_raw] if isinstance(insights_raw, list) else []

    return {
        "summary": summary,
        "verdicts": verdicts,
        "priority_overrides": overrides,
        "suggested_skills": skill_map,
        "insights": insights,
    }


def _parse_discriminator_output(raw: str, report: EntropyReport) -> dict[str, object]:
    """Back-compat wrapper used by tests; routes through the new entry point."""
    return _parse_discriminator_response(raw, report)


def _parse_discriminator_output_from_sections(
    sections: dict[str, str], report: EntropyReport,
) -> dict[str, object]:
    """Parse the discriminator's section-based output into summary, priority_overrides, insights.

    Extracts content from ## Summary, ## Priority Overrides, ## Insights sections.
    Never raises — always returns usable defaults.
    """
    summary = sections.get("summary", "").strip()
    if not summary:
        summary = _fallback_summary(report)

    overrides: list[object] = []
    overrides_text = sections.get("priority overrides", "").strip()
    if overrides_text and overrides_text != "(none)":
        for line in overrides_text.splitlines():
            line = line.strip().lstrip("- ")
            parts = [p.strip() for p in line.split("|")]
            if len(parts) >= 3 and parts[1] in ("high", "medium", "low"):
                overrides.append({"file": parts[0], "severity": parts[1], "reason": parts[2]})

    insights: list[str] = []
    insights_text = sections.get("insights", "").strip()
    if insights_text and insights_text != "(none)":
        for line in insights_text.splitlines():
            line = line.strip().lstrip("- ")
            if line:
                insights.append(line)

    return {"summary": summary, "priority_overrides": overrides, "insights": insights, "suggested_skills": _parse_suggested_skills(sections)}


def _split_sections(text: str) -> dict[str, str]:
    """Split text by ## headers into a dict of section_name_lower -> content."""
    parts = _re.split(r"^##\s+", text, flags=_re.MULTILINE)
    sections: dict[str, str] = {}
    for part in parts:
        if not part.strip():
            continue
        first_nl = part.find("\n")
        if first_nl == -1:
            sections[part.strip().lower()] = ""
        else:
            header = part[:first_nl].strip().lower()
            body = part[first_nl + 1:]
            sections[header] = body
    return sections


def _parse_suggested_skills(sections: dict[str, str]) -> dict[str, list[str]]:
    """Parse ## Suggested Skills section into file -> [skill-name] mapping."""
    skills_text = sections.get("suggested skills", "").strip()
    if not skills_text or skills_text == "(none)":
        return {}

    result: dict[str, list[str]] = {}
    for line in skills_text.splitlines():
        line = line.strip().lstrip("- ")
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 2:
            file_path = parts[0]
            skill_name = parts[1]
            if file_path and skill_name:
                result.setdefault(file_path, []).append(skill_name)
    return result


def _clean_skill_name(s: str) -> str:
    """Strip markdown decoration (backticks, surrounding quotes, whitespace)
    so LLM-emitted skill names match the catalog whitelist."""
    return s.strip().strip("`'\"").strip()


def _filter_work_list_skills(
    work_list: list[WorkItem],
    valid_names: set[str],
) -> None:
    """Strip non-catalog skill names from each WorkItem.suggested_skills (in place).

    Plugin extractors set defaults per issue type. If a default name has drifted
    from the catalog (rename, deletion, typo), filter it here so the Generator
    never receives an invalid name. Logs a single aggregate warning if anything
    was dropped.
    """
    dropped_total: dict[str, int] = {}
    for item in work_list:
        if not item.suggested_skills:
            continue
        cleaned = [(s, _clean_skill_name(s)) for s in item.suggested_skills]
        kept = [c for _, c in cleaned if c in valid_names]
        for orig, c in cleaned:
            if c not in valid_names:
                dropped_total[orig] = dropped_total.get(orig, 0) + 1
        item.suggested_skills = kept
    if dropped_total:
        log.warning(
            "Stripped %d non-catalog skill name(s) from plugin-extracted work_list: %s",
            sum(dropped_total.values()),
            dropped_total,
        )


def _filter_invalid_skills(
    skill_map: dict[str, list[str]],
    valid_names: set[str],
) -> dict[str, list[str]]:
    """Drop any skill name that is not in the catalog (LLM hallucinations).

    The discriminator sometimes invents plausible-sounding skill names like
    `hmos_implement_feature` that don't exist on disk. Forwarding those to the
    Generator wastes tool calls (failed Skill lookup). We silently filter them
    here and log the dropped names so the issue is visible in the run log.
    """
    filtered: dict[str, list[str]] = {}
    dropped: dict[str, list[str]] = {}
    for file_path, skills in skill_map.items():
        clean_path = _clean_skill_name(file_path)
        cleaned = [(s, _clean_skill_name(s)) for s in skills]
        kept = [c for _, c in cleaned if c in valid_names]
        bad = [orig for orig, c in cleaned if c not in valid_names]
        if kept:
            filtered[clean_path] = kept
        if bad:
            dropped[file_path] = bad
    if dropped:
        log.warning(
            "Discriminator suggested %d non-existent skill(s); dropped: %s",
            sum(len(v) for v in dropped.values()),
            {k: v for k, v in dropped.items()},
        )
    return filtered


def _apply_priority_overrides(
    work_list: list[WorkItem],
    overrides: list[object],
) -> list[WorkItem]:
    """Apply priority_overrides from the discriminator to adjust work_list severity."""
    if not overrides:
        return work_list

    override_map: dict[str, str] = {}
    for item in overrides:
        if isinstance(item, dict):
            file_path = str(item.get("file", ""))
            severity = str(item.get("severity", ""))
            if file_path and severity in ("high", "medium", "low"):
                override_map[file_path] = severity

    if not override_map:
        return work_list

    for wi in work_list:
        key = str(wi.file)
        if key in override_map:
            wi.severity = cast(Literal["high", "medium", "low"], override_map[key])

    return work_list


def _apply_suggested_skills(
    work_list: list[WorkItem],
    skill_map: dict[str, list[str]],
) -> list[WorkItem]:
    """Override work_list suggested_skills with discriminator's recommendations."""
    for wi in work_list:
        key = str(wi.file)
        # Try exact match first, then check if any skill_map key is a suffix of the file path
        if key in skill_map:
            wi.suggested_skills = skill_map[key]
        else:
            normalized = key.replace("\\", "/")
            for map_key, skills in skill_map.items():
                map_normalized = map_key.replace("\\", "/")
                if normalized.endswith(map_normalized) or map_normalized.endswith(normalized):
                    wi.suggested_skills = skills
                    break
    return work_list


def _build_sorted_details(
    report: EntropyReport,
    *,
    max_chars: int = 6000,
    exclude: set[str] | None = None,
) -> str:
    """Serialize details by score descending, truncating when over max_chars."""
    exclude = exclude or set()
    parts: list[str] = []
    total = 0
    sorted_dims = sorted(report.breakdown.items(), key=lambda x: -x[1])
    truncated = 0

    for name, _score in sorted_dims:
        if name in exclude:
            continue
        dim_details = report.details.get(name)
        if dim_details is None:
            continue
        chunk = json.dumps({name: dim_details}, indent=2, default=str)
        if total + len(chunk) > max_chars:
            truncated += 1
            continue
        parts.append(chunk)
        total += len(chunk)

    result = "\n".join(parts)
    if truncated > 0:
        result += f"\n...(truncated {truncated} dimensions with lower scores)"
    return result


def _format_work_list_summary(work_list: list[WorkItem], *, max_items: int = 15) -> str:
    """Format top WorkItems as concise text for the LLM prompt."""
    if not work_list:
        return "(no issues extracted)"
    lines: list[str] = []
    for item in work_list[:max_items]:
        issue_text = "; ".join(item.issues[:3])
        lines.append(f"- [{item.severity}] {item.file}: {issue_text}")
    if len(work_list) > max_items:
        lines.append(f"  ... and {len(work_list) - max_items} more items")
    return "\n".join(lines)


def _fallback_summary(report: EntropyReport) -> str:
    worst = max(report.breakdown, key=report.breakdown.__getitem__, default="unknown")
    return (
        f"Fallback: focus on improving {worst} "
        f"(score={report.breakdown.get(worst, 1.0):.3f}). "
        "Discriminator output could not be parsed."
    )


def _fallback_gradient(report: EntropyReport, work_list: list[WorkItem] | None = None) -> TextGradient:
    return TextGradient(
        summary=_fallback_summary(report),
        work_list=work_list or [],
    )
