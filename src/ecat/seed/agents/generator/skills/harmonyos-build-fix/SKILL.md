---
name: harmonyos-build-fix
description: Build a HarmonyOS project via CLI and automatically fix compile errors in a loop until the build succeeds. Default unsigned HAP; pass --signed to build a signed HAP (signing config must already exist in the project's build-profile.json5).
argument-hint: <harmonyos-project-path> <deveco-studio-path> [--signed]
allowed-tools: Agent, Task, Read, Write, Edit, Glob, Grep, Bash
type: tool
domain: engineering
---

# HarmonyOS Auto Build & Fix

Automatically build a HarmonyOS NEXT project from the command line, parse compile errors, fix them, and retry — repeating until the build succeeds.

- **HarmonyOS Project**: `$ARGUMENTS[0]` (the HarmonyOS project root, e.g. `D:/MyHmosApp` or `/Users/me/MyHmosApp`)
- **DevEco Studio Path**: `$ARGUMENTS[1]` (DevEco Studio installation root, e.g. `D:/DevEco Studio` on Windows, `/Applications/DevEco-Studio.app/Contents` on macOS)
- **--signed** (optional): `$ARGUMENTS[2]` — if set to `--signed`, the build produces a **signed HAP**. If omitted, the build produces an **unsigned HAP** (default).

---

## Step 0: Validate Inputs & Setup Environment

1. **Verify project exists** — Check that `$ARGUMENTS[0]` contains a valid HarmonyOS project (look for `build-profile.json5`, `entry/src` directory, `oh-package.json5`).

2. **Verify DevEco installation** — Check that `$ARGUMENTS[1]` contains:
   - `tools/node/node.exe` (Windows) or `tools/node/bin/node` (macOS / Linux)
   - `tools/hvigor/bin/hvigorw.js`
   - `tools/ohpm/bin/ohpm`
   - `sdk/` directory

3. **Set up `local.properties`** — Ensure the project root has `local.properties` with:
   ```properties
   hwsdk.dir=<deveco-path>/sdk
   ```
   Create it if missing. Use forward slashes in the path.

4. **Run `ohpm install`** — Install dependencies before first build:
   ```bash
   cd "<project-dir>"
   export PATH="<deveco-path>/tools/ohpm/bin:$PATH"
   "<deveco-path>/tools/ohpm/bin/ohpm" install
   ```

5. **Determine Build Mode** — Check if `$ARGUMENTS[2]` equals `--signed`:
   - **If NOT `--signed`** → **Unsigned build mode**. Do NOT modify `build-profile.json5` signing — leave any existing `signingConfigs` and product `signingConfig` references exactly as they are (an unsigned build works fine with them present; removing them destroys the project's signing setup). Go to Step 1.
   - **If `--signed`** → **Signed build mode**. Proceed to Step 0.5 to validate signing config.

---

## Step 0.5: Validate Signing Config (Only for --signed Builds)

This step is ONLY executed when `--signed` is specified.

Signing information is read directly from the project's own `build-profile.json5`. The user must have already configured signing in DevEco Studio before running this skill.

### Steps:

1. **Read `build-profile.json5`** in the project root.

2. **Check for `signingConfigs`** — Look for `app.signingConfigs` array in the file.
   - If `signingConfigs` exists and has at least one entry with valid `material` fields (`certpath`, `storeFile`, `profile`), proceed to step 3.
   - If `signingConfigs` is missing or empty, **STOP and report to the user**:
     > Signing configuration not found in `build-profile.json5`.
     > Please open the project in DevEco Studio, go to **File → Project Structure → Signing Configs**, enable **Automatically generate signature**, then re-run this skill with `--signed`.

3. **Validate signing material files exist** — For the first entry in `signingConfigs`, check that the files referenced by `material.certpath`, `material.storeFile`, and `material.profile` actually exist on disk.
   - If any file is missing, **STOP and report** which files are missing. Suggest the user re-open DevEco Studio and re-generate the signing config.

4. **Ensure product references signing** — Check that the product entry in `products` array has `"signingConfig": "default"` (or matching the signing config name). Add it if missing.

5. Proceed to Step 1.

---

## Step 1: Build-Fix Loop

Execute the following loop. **Maximum 20 iterations** to prevent infinite loops.

### 1.1 Run CLI Build

Pick the section for the current OS (check with `uname -s`: `Darwin` = macOS, `Linux` = Linux; anything else, or `cmd.exe` available, = Windows).

#### macOS / Linux

Run in a single Bash call (works for both unsigned and signed builds):

```bash
DEVECO="<deveco-path>"
JDK="$DEVECO/jbr"; [ -d "$JDK/Contents/Home" ] && JDK="$JDK/Contents/Home"   # macOS bundles the JDK under Contents/Home
NODE_BIN="$DEVECO/tools/node/bin"; [ -d "$NODE_BIN" ] || NODE_BIN="$DEVECO/tools/node"
cd "<project-dir>" && \
JAVA_HOME="$JDK" DEVECO_SDK_HOME="$DEVECO/sdk" PATH="$JDK/bin:$NODE_BIN:$PATH" \
  node "$DEVECO/tools/hvigor/bin/hvigorw.js" assembleHap --mode module -p module=entry --no-daemon 2>&1
```

`JAVA_HOME` is required even for unsigned builds on macOS: the `PackageHap` step spawns `java` and fails with `00308018 Unknown Error` without it.
No temporary script is needed; skip straight to 1.2.

#### Windows

**IMPORTANT (Windows)**: On Windows, bash `export PATH` does NOT propagate to Windows native child processes. You **must** use a temporary `.bat` file to set `PATH` and `JAVA_HOME`.

1. **Write a temporary batch file** (e.g. `<project-dir>/build_temp.bat`):

   **For unsigned builds** (no `--signed`):
   ```bat
   @echo off
   set "DEVECO_SDK_HOME=<deveco-path>\sdk"
   cd /d "<project-dir>"
   "<deveco-path>\tools\node\node.exe" "<deveco-path>\tools\hvigor\bin\hvigorw.js" assembleHap --mode module -p module=entry --no-daemon
   ```

   **For signed builds** (`--signed`):
   ```bat
   @echo off
   set "PATH=<deveco-path>\jbr\bin;%PATH%"
   set "JAVA_HOME=<deveco-path>\jbr"
   set "DEVECO_SDK_HOME=<deveco-path>\sdk"
   cd /d "<project-dir>"
   "<deveco-path>\tools\node\node.exe" "<deveco-path>\tools\hvigor\bin\hvigorw.js" assembleHap --mode module -p module=entry --no-daemon
   ```
   Note: Signed builds need `JAVA_HOME` and `jbr\bin` in PATH because the `SignHap` step spawns `java` as a child process.

   Use backslashes (`\`) in paths inside the `.bat` file (Windows convention).

2. **Run the batch file** via `cmd.exe`:
   ```bash
   cmd.exe //c "<project-dir>/build_temp.bat" 2>&1
   ```

   **No `>` / `2>>` redirection.** Bash tool already returns stdout+stderr.
   Redirecting to a Windows absolute path triggers backslash-stripping and
   creates garbage filenames like `D:<project>build_out.txt` in CWD.

3. **Delete `build_temp.bat` (mandatory, both on success and failure)** before Step 1.2.

- Build takes 1-3 min. Timeout 300000ms.

### 1.2 Check Build Result

- If output contains `BUILD SUCCESSFUL` → **Build succeeded!** Exit the loop, go to Step 2.
- If output contains `ERROR` or `BUILD FAILED` → Parse errors and continue to 1.3.

### 1.3 Parse Errors

Extract error information from the build output. Errors typically appear in these formats:

```
ERROR: <file-path>:<line>:<col> - <error-code>: <message>
```

or

```
ArkTS:ERROR File: <file-path>:<line>:<col>
  <error message>
```

Group errors by file. Focus on **actual errors**, not warnings.

### 1.4 Fix Errors

Read each file that has errors and apply fixes. Use the error reference table below to identify and fix common issues:

| Error Code / Pattern | Message | Fix |
|---|---|---|
| `arkts-limited-throw` | "throw statements cannot accept values of arbitrary types" | Change `throw err` to `throw (err instanceof Error) ? err : new Error(String(err))` |
| `arkts-no-obj-literals-as-types` | "Object literals cannot be used as type declarations" | Define a named `interface` instead of inline `{ key: Type }` |
| `arkts-no-untyped-obj-literals` | "Object literal must correspond to some explicitly declared class or interface" | Assign to typed variable: `const r: MyInterface = {...}; return r;` |
| `arkts-no-any-type` / `any` type usage | "Use explicit types instead of any" | Replace `any` with the correct concrete type or `object` |
| `arkts-no-var` | "Use 'let' or 'const' instead of 'var'" | Replace `var` with `let` or `const` |
| `10903329` | "Unknown resource name 'xxx'" | Verify resource exists in `resources/base/media/` or `element/*.json`. Use `layered_image` as fallback for missing images. **Special case**: `$r('sys.media.ohos_ic_public_xxx')` references system icons by SDK-specific names that may not exist in the build SDK — replace with `$r('app.media.ic_public_xxx')` and add the icon file to `resources/base/media/` |
| `10505001` | "Resource[] is not assignable to ResourceColor" | Remove array brackets: `.fontColor($r('app.color.x'))` not `.fontColor([$r('app.color.x')])` |
| `00303221` | "permission must be a value that is predefined within the SDK" | Remove invalid permission from `module.json5`. See valid permissions list below |
| Missing import | "Cannot find name 'xxx'" | Add the correct import (see import reference below) |
| Missing `async` | "await expression requires async function" | Add `async` to the enclosing function |
| Missing `build()` | "@Component must have build() method" | Add a `build() {}` method to the @Component struct |
| Type mismatch | Various type errors | Fix the type annotation or cast appropriately |
| Duplicate identifier | "Duplicate identifier 'xxx'" | Remove or rename the duplicate declaration |

**For errors NOT in the table above**: Read the error message carefully, read the relevant source file, understand the context, and apply an appropriate fix. Use your knowledge of ArkTS/HarmonyOS to determine the correct solution.

### 1.5 Log Progress

After each fix iteration, briefly report:
- Iteration number
- Number of errors found
- Summary of fixes applied
- Whether re-building

Then go back to **1.1** and rebuild.

---

## Step 2: Build Success Report

When the build succeeds, present a summary:

1. **Build Status**: SUCCESS
2. **Build Type**: Signed HAP or Unsigned HAP
3. **Signing** (if signed): Confirm signing config from `build-profile.json5` was used
4. **Iterations**: How many build-fix cycles were needed
5. **Total Errors Fixed**: Count of errors fixed across all iterations
6. **Summary of Changes**: List of files modified and what was fixed in each
7. **Output HAP Path**:
   - Signed: `<project>/entry/build/default/outputs/default/entry-default-signed.hap`
   - Unsigned: `<project>/entry/build/default/outputs/default/entry-default-unsigned.hap`

---

## Reference: Common HarmonyOS Imports

```typescript
// Network
import { http } from '@kit.NetworkKit';

// Data persistence
import { preferences } from '@kit.ArkData';
import { relationalStore } from '@kit.ArkData';

// UI utilities
import { router } from '@kit.ArkUI';
import { promptAction } from '@kit.ArkUI';

// Ability & Context
import { UIAbility, AbilityConstant, Want } from '@kit.AbilityKit';
import { common } from '@kit.AbilityKit';

// File I/O
import { fileIo } from '@kit.CoreFileKit';

// Logging
import { hilog } from '@kit.PerformanceAnalysisKit';

// JSON parsing — built-in, no import needed
// ArkUI built-in components (Text, Column, Row, List, Button, Image, etc.) — NO import needed
```

## Reference: Valid Permission Names

Commonly used SDK-validated permissions for `module.json5`:

- `ohos.permission.INTERNET`
- `ohos.permission.GET_NETWORK_INFO`
- `ohos.permission.GET_WIFI_INFO`
- `ohos.permission.KEEP_BACKGROUND_RUNNING`
- `ohos.permission.PUBLISH_AGENT_REMINDER`
- `ohos.permission.CAMERA`
- `ohos.permission.MICROPHONE`
- `ohos.permission.APPROXIMATELY_LOCATION`
- `ohos.permission.LOCATION`
- `ohos.permission.READ_MEDIA`
- `ohos.permission.WRITE_MEDIA`
- `ohos.permission.USE_BLUETOOTH`
- `ohos.permission.VIBRATE`

**Note**: `ohos.permission.NOTIFICATION` does NOT exist. When in doubt, omit the permission.

## Reference: ArkTS Strict Mode Rules

All code must comply with ArkTS strict mode:

1. **No `any` type** — Use explicit types or `object`
2. **No `var`** — Only `let` and `const`
3. **No dynamic property access** — Use typed interfaces instead of `obj['key']` on typed objects
4. **`throw` must throw Error instances** — Never `throw 'string'` or `throw unknownVar`
5. **All object literals must match declared interfaces** — No anonymous `{ key: val }` returns without a matching interface
6. **No inline object literal types** — `function(): { a: string }` is forbidden; define a named `interface`
7. **All `@Component` structs must have `build()`** — Missing build method is a compile error
8. **`$r()` resource references validated at compile time** — All referenced resources must exist
9. **`fontColor()` expects `ResourceColor`**, not `Resource[]` — Don't wrap in array brackets (exception: `SymbolGlyph`)
10. **Permission names in `module.json5`** — Must be SDK-predefined values

## Important Notes

- **Timeout**: Individual build commands may take up to 5 minutes. Use a 300000ms timeout.
- **Max iterations**: Stop after 20 iterations to prevent infinite loops. If build still fails after 20 attempts, report the remaining errors to the user.
- **Don't over-fix**: Only fix errors reported by the compiler. Don't proactively refactor unrelated code.
- **Read before edit**: Always read a file before modifying it. Understand the surrounding context.
- **One error can cause many**: A single root-cause fix (like adding a missing interface) may resolve multiple reported errors. After fixing root causes, rebuild to see remaining issues.
- **ohpm errors**: If the build fails because of missing packages, run `ohpm install` again.

---

## References

- `references/arkts-strict-patterns.md` — ArkTS 严格模式编译错误的确定性修复 Pattern（throw/any/var/interface 等）
- `references/known-patterns.md` — 已知常见编译错误 Pattern 及修复方案
- `references/rdb-entity-pattern.md` — RDB 实体类编译错误 Pattern（数据库实体相关）
