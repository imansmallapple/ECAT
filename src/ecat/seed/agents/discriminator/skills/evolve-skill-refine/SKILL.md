# evolve-skill-refine  [Phase 3 — DISABLED in Phase 1]

**Purpose**: Refine an existing Generator skill based on observed failures.

## Gate
Only active when `features.skill_evolution = true`.

## Behaviour
1. Identify a skill that was invoked but did not reduce H.
2. Back up the current version to `.ecat/agents/generator/.history/<ts>/`.
3. Rewrite the SKILL.md with improved instructions.

## In Phase 1
Return an empty `skill_evolution` list. Do not write any files.
