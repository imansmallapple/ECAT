# Raw Checklist

The original checklist defines feature identity and evaluation scope. Normalize
its columns into these semantic roles without changing row granularity:

```text
feature_id? feature_name? feature_path_or_hierarchy source_hint?
control_hint? interaction_type? priority? depth?
```

Rules:

- Preserve an explicit stable feature ID when present.
- Otherwise generate `S<sheet-index>-R<source-row-number>` from the original
  sheet and physical row.
- Use an explicit feature path when present. Otherwise join non-empty hierarchy
  columns in their original order.
- Preserve feature name, path, priority, sheet name, and source row number.
- Treat source paths and control identifiers as search hints, never as verified
  anchors or behavior definitions.
- Reject duplicate IDs, empty paths, ambiguous header mappings, selected hidden
  rows, merged feature rows, or reordered input.

Select the primary scope deterministically:

- If any normalized priority is exactly `P0`, evaluate only exact `P0` rows.
- If no exact `P0` exists, evaluate every non-empty feature row.

Record raw and selected row counts, selection policy, sheet names, checklist
SHA256, and normalizer identifier. Read underlying workbook rows rather than
trusting an active spreadsheet filter. Never overwrite or filter the workbook.
