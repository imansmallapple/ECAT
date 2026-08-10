# Code Alignment

Compute `Align` with only the bundled `assets/node_align_tool.zip`. Verify it
against `assets/node_align_tool.checksum`; never download, replace, override, or
silently skip the tool. CodeGraph 1.4 or newer parses both the Android and
HarmonyOS repositories into the same graph schema; ArkAnalyzer is not used.

`Align` is the tool's deterministic node-retention value:

```text
aligned pair count / source retention denominator
```

The denominator is the bundled tool's pre-filter Android entity count, not the
post-filter `android_nodes` count. Report both explicitly. Identifier ordering
breaks equal-score ties. When the target graph has no edges, report edge and path
retention as `NA` rather than zero.

The frozen node-similarity threshold is `0.30`. A candidate pair is retained
only when its name/type similarity is strictly greater than this threshold,
before deterministic greedy one-to-one matching. Record the threshold in every
alignment result; a missing or different value is a protocol failure.

A missing prerequisite, hash mismatch, extraction error, or alignment failure
fails the evaluation. It must never become `Align = 0` or target evidence.
Record the versions and executable hashes of codegraph and Node.js used by the
alignment run.

Keep default output compact. Do not place aligned pairs, unaligned nodes, graph
dumps, or full tool diagnostics in stdout or resumable state.
