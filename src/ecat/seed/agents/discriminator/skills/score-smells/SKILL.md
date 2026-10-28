# score-smells

**Purpose**: LLM judge + simple rules — detect code smells in harmony/:
duplicate code, long functions, missing error handling.

## Checks
- Functions > 60 lines
- Duplicated `build()` blocks
- Empty catch blocks
- Missing `aboutToDisappear` lifecycle cleanup

## Score
0.0 = clean, 1.0 = heavily smelly.
