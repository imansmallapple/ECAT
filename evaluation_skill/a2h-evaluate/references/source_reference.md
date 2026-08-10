# Android Source Reference

Use only the current normalized row and the Android repository. The raw
checklist defines evaluation scope; source grounding supplies evidence and a
minimal behavior, but never removes a selected row.

1. Inspect a valid `source_hint` first; otherwise search repository-relative
   paths, resources, control names, feature-name tokens, and hierarchy tokens.
2. Trace the feature from a UI, route, lifecycle, callback, service, or other
   application entry to its principal action and essential outcome.
3. Assign one source basis:
   - `EXACT`: direct app-code anchors support the complete behavior.
   - `BOUNDARY`: concrete app-code anchors support the nearest UI, resource,
     route, handler, wrapper, or dependency boundary, while the checklist names
     a finer behavior detail not directly encoded there.
4. Cite the smallest repository-relative `path:line` or `path:start-end` ranges.
   Separate multiple anchors with `; `.
5. Write one minimal behavior:

```text
minimal precondition -> named action or render event -> essential observable outcome
```

Keep the raw row's named feature as the boundary. Exclude neighboring features,
incidental navigation, styling, exact widget types, platform APIs, performance
details, secondary error paths, and optional subflows unless the row explicitly
names them. For display-only rows, rendering the named information is the action
and visible content is the outcome.

Map related checklist rows as a feature family: inspect their shared
implementation once, then reuse the same Android anchors when the evidence is
identical. Rows may share anchors while retaining distinct feature identities.
Do not infer exact values from nearby rows, enum positions, or ordering. Preserve
numbers, names, units, and limits exactly when the row names them.

Record rows only through the command in `SKILL.md`. When all rows exist, audit
the complete draft against the checklist and cited snippets. Revise mistakes,
then seal explicitly. Sealing is the only transition that makes the source
reference immutable. Every row requires a concrete source anchor; `NOT_FOUND`
is invalid. If no application boundary supports a selected row, report the
three inputs as inconsistent instead of fabricating a source-grounded result.
