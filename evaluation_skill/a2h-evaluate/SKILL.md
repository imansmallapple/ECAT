---
name: a2h-evaluate
description: "Evaluate an Android-to-HarmonyOS migration using an Android repository, a HarmonyOS repository, and an original feature checklist. Reports functional completeness, code alignment, and source statistics."
---

# A2H Evaluate

Evaluate one HarmonyOS migration from exactly three inputs. Derive and seal the
Android source reference before inspecting the HarmonyOS target.

## Inputs

- `ANDROID_REPO`: read-only Android reference repository.
- `HARMONY_REPO`: read-only HarmonyOS/ArkTS repository to evaluate.
- `RAW_FEATURE_CHECKLIST`: original `.xlsx`, `.csv`, or `.tsv` checklist.

Do not accept a previously generated contract as an input. A source reference
may be reused only when this skill created it from identical inputs and its full
fingerprint matches.

## Isolation

- Evaluate one target repository at a time.
- Draft, audit, seal, and hash the Android source reference before reading
  HarmonyOS files.
- Do not read prior evaluations, expected scores, rankings, screenshots, other
  HarmonyOS targets, or derived contracts from another workflow.
- Do not delegate source grounding or target judgment to another agent.
- Do not modify any input repository or the original checklist.
- Record the evaluator model, prompts, skill, inputs, source reference, and
  bundled alignment-tool hashes.

## Start or resume

Read `references/raw_checklist.md` before the first `start`.

Set the skill directory and the evaluator's resolved model identifier. The model
is execution metadata, not a fourth evaluation input.

When a formal launcher supplies `A2H_EVALUATOR_MODEL_LOCK`, every evaluator
command must inherit it unchanged. Never export, unset, or override either model
environment variable.

```bash
export A2H_EVAL_DIR=/path/to/a2h-evaluate
export A2H_EVALUATOR_MODEL=RESOLVED_MODEL_ID

python3 "$A2H_EVAL_DIR/scripts/run_a2h_eval.py" start \
  --android ANDROID_REPO \
  --harmony HARMONY_REPO \
  --checklist RAW_FEATURE_CHECKLIST
```

The command checks local tooling before creating state, normalizes the checklist,
and enters `SOURCE_REFERENCE`, `SOURCE_AUDIT`, or `TARGET_JUDGMENT`. It creates
content-addressed resumable state and prints the next required row. Resume only
through the same command and inputs. A hash mismatch is a hard failure.

## 1. Build the Android source reference

Read `references/source_reference.md` before this phase. Use only the current
checklist row and `ANDROID_REPO` to derive one minimal behavior:

```text
minimal precondition -> named action or render event -> essential observable outcome
```

Persist every completed row immediately:

```bash
python3 "$A2H_EVAL_DIR/scripts/run_a2h_eval.py" record-reference \
  --run RUN_DIR \
  --feature-id FEATURE_ID \
  --android-anchor ANDROID_ANCHOR \
  --reference-behavior "PRECONDITION -> ACTION -> OUTCOME" \
  --source-basis EXACT_OR_BOUNDARY
```

Use `EXACT` when app code directly supports the complete behavior. Use
`BOUNDARY` when a concrete UI, resource, route, handler, wrapper, or dependency
boundary supports the feature but does not encode every checklist detail. Every
row requires a concrete Android anchor. Related rows may reuse anchors when they
share one implementation; never substitute `NOT_FOUND` or generate rows from the
checklist alone.

After the final draft row, the phase becomes `SOURCE_AUDIT`. Review the whole
draft for row identity, anchors, numeric values, and minimal behavior. Correct
any row before sealing:

```bash
python3 "$A2H_EVAL_DIR/scripts/run_a2h_eval.py" revise-reference \
  --run RUN_DIR \
  --feature-id FEATURE_ID \
  --android-anchor ANDROID_ANCHOR \
  --reference-behavior "PRECONDITION -> ACTION -> OUTCOME" \
  --source-basis EXACT_OR_BOUNDARY

python3 "$A2H_EVAL_DIR/scripts/run_a2h_eval.py" seal-reference --run RUN_DIR
```

The sealed reference is immutable. Read `references/alignment.md`, then run
`start` again with the same inputs. The command computes source statistics, runs
the bundled alignment tool, and enters target judgment.

## 2. Judge the HarmonyOS target

Read `references/target_judge.md` before this phase. For every frozen
source-reference row, inspect bounded target snippets and assign exactly one
status:

- `FULL = 1.0`
- `PARTIAL = 0.5`
- `MISSING = 0.0`

Persist each judgment immediately:

```bash
python3 "$A2H_EVAL_DIR/scripts/run_a2h_eval.py" record-evidence \
  --run RUN_DIR \
  --feature-id FEATURE_ID \
  --status FULL_OR_PARTIAL_OR_MISSING \
  --target-anchor TARGET_ANCHOR \
  --rationale "ONE_CONCISE_SENTENCE"
```

`FULL` and `PARTIAL` require concrete repository-relative target anchors.
`MISSING` requires the literal `NOT_FOUND`. Every selected feature remains in
the denominator. A rationale is required for `PARTIAL` and `MISSING`; it may be
empty for `FULL`.

## Scores

Compute only:

```text
Agent = (FULL + 0.5 * PARTIAL) / selected feature count
Align = node retention returned by the bundled alignment tool
```

Report the two metrics independently. Do not add a composite score, app-specific
weights, runtime evidence, build evidence, or UI-test evidence. Equivalent
platform-native implementations count when they satisfy the essential behavior.
The bundled alignment tool uses the frozen node-similarity threshold `0.30`;
the finalizer rejects alignment output that omits or changes this value.

## Finalize

Read `references/report.md` before finalizing.

Finalize only after all source-reference and target-evidence rows are complete:

```bash
python3 "$A2H_EVAL_DIR/scripts/run_a2h_eval.py" finalize --run RUN_DIR
```

To preserve one compact result artifact:

```bash
python3 "$A2H_EVAL_DIR/scripts/run_a2h_eval.py" finalize \
  --run RUN_DIR \
  --save-result RESULT_JSON
```

The finalizer re-hashes every input and evaluation component, validates all
rows, requires successful alignment, computes both scores, and prints the full
text report defined in `references/report.md`.

By default, print the report without saving a result file. With
`--save-result`, write exactly one compact JSON containing target judgments and
the source-reference hash; do not duplicate source-reference rows.

Delete target temporary state only after successful report output. Preserve
verified prefixes after interruption or failure. Never convert infrastructure
failure into evidence or a score.
