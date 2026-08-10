# Static Target Judge

Judge each frozen behavior using only the named Android evidence and the current
HarmonyOS repository. Search bounded target snippets for integration, principal
action, state or data flow, and observable outcome.

## Status rubric

- `FULL`: static code coherently implements the essential feature intent,
  including its principal action, outcome, and a registered route, invocation,
  callback, lifecycle hook, exported ability or service, or another statically
  evidenced integration path. Runtime proof and exact Android UI structure are
  not required.
- `PARTIAL`: substantive code implements a meaningful part of the same intent,
  but integration is uncertain or an essential action, outcome, state/data flow,
  or named subflow is incomplete, disconnected, mocked, or materially degraded.
- `MISSING`: no substantive implementation of the named behavior exists.

Apply the strongest status supported by concrete static evidence:

- Accept platform-native alternatives and equivalent behavior on another screen
  or navigation structure.
- Do not downgrade complete static code because runtime execution is absent.
- A substantive but disconnected implementation is `PARTIAL`, not `MISSING`.
- Names, comments, declarations, models, interfaces, constants, placeholders,
  empty callbacks, hard-coded demos, mocks, boilerplate, and UI shells without
  the behavior are `MISSING`.
- Static presentation counts only when the row itself defines a display or
  information feature.
- One implementation may satisfy multiple rows only when it independently
  implements each row's essential behavior.
- Downgrade only for a specific missing behavior or integration gap, not naming,
  architecture, UI structure, or missing runtime confirmation.

## Evidence

Record exactly:

```text
feature_id,status,target_anchor,rationale
```

`FULL` and `PARTIAL` require repository-relative `path:line` anchors. `MISSING`
requires `NOT_FOUND`. A rationale is optional for `FULL` and required for the
other statuses; when present it must be one line of at most 120 Unicode
characters:

- For `FULL`, leave it empty unless the anchor needs a short clarification.
- For `PARTIAL`, state the implemented portion and decisive gap.
- For `MISSING`, state the relevant area searched and why no substantive
  implementation was found.

Do not restate the source reference. Do not edit evidence manually, create
batches, slices, checklist copies, progress summaries, or duplicate evidence.
