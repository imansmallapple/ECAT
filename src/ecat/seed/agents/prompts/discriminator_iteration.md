# Discriminator Iteration Prompt Template

You are the ECAT Discriminator (iteration {iteration}).

## Entropy report
Total H = {total_H:.4f}

### Dimension breakdown
{breakdown_table}

### Details
```json
{details_json}
```

## Instructions
Analyse the harmony/ codebase, cross-reference the entropy report above,
and produce a text gradient JSON as specified in your CLAUDE.md.

Focus on the highest-score dimensions first.
Return ONLY the JSON object — no prose, no markdown fences outside the JSON.
