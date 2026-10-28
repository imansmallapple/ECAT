---
name: recommend-generator-skills
description: Read the pre-built skill catalog and recommend relevant Generator skills for specific code issues.
type: tool
---

# Recommend Generator Skills

The skill catalog is pre-built by ECAT before the discriminator runs. You do NOT need to Glob or read individual SKILL.md files — just read the catalog JSON at the path provided in your prompt under "Skill catalog".

## Workflow

### Step 1: Read the catalog (1 Read call)

Read the catalog file at the absolute path given in your prompt:

```
Read: <path from "Skill catalog" section of your prompt>
```

The catalog JSON has this structure:

```json
{
  "skill_count": 46,
  "skills": [
    {"name": "arkts-navigation-builder", "description": "...", "type": "domain", "domain": "navigation"},
    {"name": "harmonyos-build-fix", "description": "...", "type": "tool", "domain": ""},
    ...
  ]
}
```

### Step 2: Match skills to issues

For each work_list item, scan the catalog entries and match `description` to the issue type.

Common mappings (use description to confirm):

| Issue Type | Skill name |
|------------|------------|
| Navigation / routing / tab | `arkts-navigation-builder` |
| Build / compile errors | `harmonyos-build-fix` |
| Resource conversion (icons, drawables) | `android2hmos_resources_convert` |
| Component structure | `arkts-component-builder` |
| State management | `arkts-state-manager` |
| Animation | `arkts-animation-builder` |
| Accessibility | `arkts-accessibility` |
| i18n / localization | `arkts-i18n` |
| Dark mode | `arkts-dark-mode` |
| Large font | `arkts-large-font` |
| Data layer | `arkts-data-layer` |
| WebView | `arkts-webview-manager` |
| Multi-device | `arkts-multi-device` |
| App identity (name, icon, bundle ID) | `arkts-app-identity` |

### Step 3: Populate suggested_skills

Rules:
1. Only recommend skills that **clearly match** the specific issue
2. Use the **exact `name` field** from the catalog JSON — copy-paste it; do not paraphrase, abbreviate, or invent
3. Recommend 1-3 skills per work item
4. If no skill is relevant, leave `suggested_skills` empty

### Anti-hallucination check (MUST do before writing each line)

Before you emit any `file | skill-name | reason` line:

1. Open the catalog JSON you just read.
2. Find the entry whose `name` field exactly equals the skill name you intend to recommend.
3. If you cannot find an exact match, DO NOT WRITE THAT LINE.

ECAT post-processes your output and **silently drops any name not present in the catalog**. Common hallucinations to avoid:

| Don't write | Why | What to do |
|---|---|---|
| `hmos_implement_feature` | Not in catalog — invented from issue text | Find a real domain skill or omit |
| `fix_stub_pages`, `complete_pages` | Made up to match the issue | Use `arkts-component-builder` if structural, or omit |
| Truncated names like `arkts-component` | Missing suffix | Always copy the full `name` field |
| `arkts-page-builder` | Catalog has `arkts-component-builder` instead | Use the actual catalog name |

If you cannot find a clearly matching skill in the catalog for an issue, that issue gets **no suggested skill**. Empty is correct; fabricated is wrong.

## Notes

- The catalog is automatically refreshed when skills change — always read it fresh each session
- If the catalog path is missing from your prompt, write (none) for all suggested_skills
