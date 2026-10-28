# evolve-skill-create  [Phase 3 — DISABLED in Phase 1]

**Purpose**: Distil a new Generator skill from a successful fix trajectory.

## Gate
Only active when `features.skill_evolution = true`.

## Behaviour
1. Identify a fix pattern that lowered H by ≥ 0.05 in one iteration.
2. Abstract the pattern into a reusable SKILL.md.
3. Write to `.ecat/agents/generator/skills/<new-skill-name>/SKILL.md`.
4. Record rationale in `.ecat/agents/generator/skills/<new-skill-name>/RATIONALE.md`.

## In Phase 1
Return an empty `skill_evolution` list. Do not write any files.
