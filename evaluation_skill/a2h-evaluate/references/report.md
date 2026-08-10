# Report and Provenance

Print one complete text report by default:

```text
A2H Evaluation

Agent:
- Score: <weighted numerator> / <denominator> = <score>
- FULL/PARTIAL/MISSING: <full>/<partial>/<missing>
- Denominator: <selected feature count>
- Raw/Selected Rows: <raw count>/<selected count>
- Selection Policy: <P0 only | all rows>

Align:
- Score: <node retention>
- Aligned Nodes: <aligned pair count>
- Retention Denominator: <pre-filter Android entity count>
- Android/Harmony Nodes: <source count>/<target count>
- Edge/Path Retention: <value/value | NA/NA>
- Tool: bundled dual-CodeGraph node_align_tool.zip

Android Source:
- Repository SHA256: <hash>
- Source Basis EXACT/BOUNDARY: <count>/<count>
- Source Files: <count>
- LOC/Nonblank LOC: <count>/<count>
- Languages: <language: files, LOC; ...>

HarmonyOS Source:
- Repository SHA256: <hash>
- Source Files: <count>
- LOC/Nonblank LOC: <count>/<count>
- ETS Files: <count>
- ETS LOC/Nonblank LOC: <count>/<count>
- Languages: <language: files, LOC; ...>

Reproducibility:
- Raw Checklist SHA256: <hash>
- Source Reference SHA256: <hash>
- Evaluator Model: <resolved model>
- Source/Target Prompt SHA256: <hash>/<hash>
- Protocol/Skill SHA256: <hash>/<hash>
- Node Align ZIP SHA256: <hash>
- Alignment Runtime: <codegraph and Node.js versions and hashes>
- Source/Target Resume Sessions: <count>/<count>
- Code Statistics Policy: <extensions, exclusions, and line-count rule>

Verification: <OK | FAILED>
```

Count complete multi-module source trees. Exclude generated, dependency, cache,
and output directories including `.git`, `.gradle`, `.hvigor`, `.idea`, `build`,
`dist`, `node_modules`, `oh_modules`, `out`, and `target`. Count physical lines
and separately report nonblank lines. Always disclose included extensions and
excluded directories. Exclude symlinked files from source statistics and fail
instead of silently skipping an unreadable included source file.

Do not print per-feature judgments or full alignment diagnostics in the default
report. When requested, save one compact `result.json` with target judgments and
the source-reference hash. Do not save a result artifact by default.
