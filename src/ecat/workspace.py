"""Workspace management — create and validate .ecat/ directory skeleton."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from ecat.errors import WorkspaceError

log = logging.getLogger(__name__)

# Directories that must be version-controlled (created on init, not gitignored)
_VERSIONED_DIRS: list[str] = [
    ".ecat/agents/generator/skills",
    ".ecat/agents/generator/agents",
    ".ecat/agents/discriminator/skills",
    ".ecat/agents/discriminator/agents",
    ".ecat/agents/prompts",
]

# Directories that are runtime-only (gitignored)
_RUNTIME_DIRS: list[str] = [
    ".ecat/agents/generator/memory",
    ".ecat/agents/discriminator/memory",
]

_GITIGNORE_ENTRIES: list[str] = [
    "agents/generator/memory/",
    "agents/discriminator/memory/",
    "runs_*/",
    "state.json",
]

# Written to <harmony>/.gitignore at git-init so ecat's auto-commits never
# capture ECAT runtime data (build logs under .ecat/tmp/), HarmonyOS/native
# build artifacts, dependencies, or IDE/OS cruft. Cross-app generic.
_HARMONY_GITIGNORE_ENTRIES: list[str] = [
    ".ecat/",
    "oh_modules/",
    "node_modules/",
    "build/",
    ".hvigor/",
    ".cxx/",
    ".preview/",
    ".clangd/",
    ".idea/",
    ".DS_Store",
    "*.log",
]


class Workspace:
    """Represents the .ecat/ workspace rooted at *project_dir*."""

    def __init__(self, project_dir: Path) -> None:
        self.root = project_dir
        self.ecat = project_dir / ".ecat"

    # ── property helpers ────────────────────────────────────────────────────

    @property
    def state_file(self) -> Path:
        return self.ecat / "state.json"

    def current_runs_dir(self) -> Path:
        """Return the active runs directory from state.json."""
        state = self.read_state()
        rel = state.get("current_runs_dir")
        if isinstance(rel, str):
            return self.ecat / rel
        # Fallback: find the most recent runs_* directory
        candidates = sorted(self.ecat.glob("runs_*"), reverse=True)
        if candidates:
            return candidates[0]
        raise WorkspaceError("No runs directory found. Run `ecat refine` first.")

    def create_runs_dir(self) -> Path:
        """Create a new timestamped runs directory and record it in state.json."""
        stamp = datetime.now().strftime("%Y%m%d_%H%M")
        name = f"runs_{stamp}"
        runs = self.ecat / name
        runs.mkdir(parents=True, exist_ok=True)
        (runs / "charts").mkdir(exist_ok=True)
        self.write_state({"current_runs_dir": name})
        log.info("Created runs directory: %s", runs)
        return runs

    def agent_config_dir(self, role: str) -> Path:
        return self.ecat / "agents" / role

    # ── harmony git ────────────────────────────────────────────────────────

    def ensure_harmony_git(self, harmony_dir: Path) -> None:
        """Initialise a git repo in harmony_dir if one doesn't exist.

        Always writes/merges a .gitignore FIRST so the initial `git add .` (and
        every later ecat auto-commit) never captures build logs, deps, or .ecat
        runtime data.
        """
        self._ensure_harmony_gitignore(harmony_dir)
        if (harmony_dir / ".git").is_dir():
            return
        try:
            subprocess.run(
                ["git", "init"], cwd=harmony_dir,
                check=True, capture_output=True, timeout=30,
            )
            subprocess.run(
                ["git", "add", "."], cwd=harmony_dir,
                check=True, capture_output=True, timeout=30,
            )
            subprocess.run(
                ["git", "commit", "-m", "ecat: initial checkpoint", "--allow-empty"],
                cwd=harmony_dir, check=True, capture_output=True, timeout=30,
            )
            log.info("Initialised git repo in %s", harmony_dir)
        except Exception as exc:
            log.warning("Failed to initialise git in %s: %s", harmony_dir, exc)

    def _ensure_harmony_gitignore(self, harmony_dir: Path) -> None:
        """Append any missing _HARMONY_GITIGNORE_ENTRIES to <harmony>/.gitignore.

        Only adds entries not already present (idempotent). Verified to match
        runtime/build/dep artifacts only — never HarmonyOS source (ets/resources/
        *.json5/hvigorfile are files, not the ignored dirs).
        """
        if not harmony_dir.exists():
            return
        gi = harmony_dir / ".gitignore"
        try:
            existing = gi.read_text(encoding="utf-8") if gi.exists() else ""
        except OSError:
            return
        present = {ln.strip() for ln in existing.splitlines()}
        additions = [e for e in _HARMONY_GITIGNORE_ENTRIES if e not in present]
        if not additions:
            return
        try:
            with gi.open("a", encoding="utf-8") as fh:
                if not existing:
                    fh.write("# ECAT: build artifacts, dependencies, runtime data\n")
                elif not existing.endswith("\n"):
                    fh.write("\n")
                for entry in additions:
                    fh.write(entry + "\n")
            log.info("Ensured %d .gitignore entries in %s", len(additions), harmony_dir.name)
        except OSError as exc:
            log.warning("Could not update %s/.gitignore: %s", harmony_dir, exc)

    # ── init ────────────────────────────────────────────────────────────────

    def init(self, force: bool = False) -> None:
        """Create/validate workspace skeleton.

        If *force* is True, wipe memory/ and runs/ (but keep skills/agents/CLAUDE.md).
        """
        self._ensure_versioned_dirs()
        self._ensure_runtime_dirs(force=force)
        self._seed_agents(overwrite=force)
        self._seed_opencode_config(overwrite=force)
        self._ensure_gitignore()
        self._ensure_env_file()
        self._ensure_state_json(force=force)
        log.info("Workspace initialised at %s", self.ecat)

    def _ensure_versioned_dirs(self) -> None:
        for rel in _VERSIONED_DIRS:
            (self.root / rel).mkdir(parents=True, exist_ok=True)

    def _ensure_runtime_dirs(self, force: bool) -> None:
        for rel in _RUNTIME_DIRS:
            d = self.root / rel
            if force and d.exists():
                shutil.rmtree(d)
                log.info("Wiped %s (--force)", d)
            d.mkdir(parents=True, exist_ok=True)
        if force:
            for runs in self.ecat.glob("runs_*"):
                if runs.is_dir():
                    shutil.rmtree(runs)
                    log.info("Wiped %s (--force)", runs)

    def _ensure_gitignore(self) -> None:
        gi = self.ecat / ".gitignore"
        existing = gi.read_text(encoding="utf-8") if gi.exists() else ""
        additions = [e for e in _GITIGNORE_ENTRIES if e not in existing]
        if additions:
            with gi.open("a", encoding="utf-8") as fh:
                if not existing:
                    fh.write("# ECAT runtime files (not version-controlled)\n")
                for entry in additions:
                    fh.write(entry + "\n")
            log.info("Updated .ecat/.gitignore with %d entries", len(additions))

    def _ensure_env_file(self) -> None:
        env_file = self.root / ".env"
        if env_file.exists():
            return
        env_file.write_text(
            "# ECAT — LLM API Keys\n"
            "# .env only stores secrets. model and base_url go in ecat.config.yaml.\n"
            "\n"
            "ANTHROPIC_API_KEY=\n"
            "# OPENAI_API_KEY=\n"
            "# DEEPSEEK_API_KEY=\n"
            "# GOOGLE_API_KEY=\n"
            "\n"
            "# Claude subscription on macOS (api_key_env: \"\"): run `claude setup-token`\n"
            "# and paste the token here.\n"
            "# CLAUDE_CODE_OAUTH_TOKEN=\n",
            encoding="utf-8",
        )
        log.info("Created .env template")

    def _ensure_state_json(self, force: bool) -> None:
        if force or not self.state_file.exists():
            initial: dict[str, object] = {
                "created_at": datetime.now(UTC).isoformat(),
                "iteration": 0,
                "last_H": None,
                "status": "idle",
            }
            self.state_file.write_text(
                json.dumps(initial, indent=2), encoding="utf-8"
            )

    def _seed_agents(self, overwrite: bool = False) -> None:
        """Copy CLAUDE.md, skills/, and agents/ from package seed into workspace.

        Skips files that already exist unless *overwrite* is True.
        """
        seed = _seed_dir()
        if seed is None:
            log.debug("Seed directory not found; skipping agent seeding")
            return

        seed_agents = seed / "agents"
        if not seed_agents.is_dir():
            return

        copied = 0
        for role_dir in seed_agents.iterdir():
            if not role_dir.is_dir():
                continue
            role = role_dir.name
            if role == "prompts":
                target = self.ecat / "agents" / "prompts"
            else:
                target = self.agent_config_dir(role)
            target.mkdir(parents=True, exist_ok=True)

            for src_file in role_dir.rglob("*"):
                if not src_file.is_file():
                    continue
                rel = src_file.relative_to(role_dir)
                dst = target / rel
                if dst.exists() and not overwrite:
                    continue
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_file, dst)
                copied += 1

        if copied:
            log.info("Seeded %d agent config files into .ecat/agents/", copied)

    def _seed_opencode_config(self, overwrite: bool = False) -> None:
        """Write opencode.jsonc into each agent config dir so opencode discovers skills.

        opencode scans directories listed in the config's "plugin" array for
        {skill,skills}/**/SKILL.md.  Pointing the plugin at the agent config dir
        itself causes it to pick up the skills/ sub-directory automatically.
        Absolute paths are required because opencode resolves them before the
        server changes directory.  Re-run `ecat init` if the project moves.
        """
        for role in ("generator", "discriminator"):
            config_dir = self.agent_config_dir(role).resolve()
            dest = config_dir / "opencode.jsonc"
            if dest.exists() and not overwrite:
                continue
            content = json.dumps(
                {
                    "$schema": "https://opencode.ai/config.json",
                    "plugin": [str(config_dir)],
                },
                indent=2,
            )
            dest.write_text(content, encoding="utf-8")
            log.info("Generated %s", dest)

    # ── validation ──────────────────────────────────────────────────────────

    def validate(self) -> None:
        """Raise WorkspaceError if the workspace is not properly initialised."""
        missing = []
        for rel in _VERSIONED_DIRS:
            d = self.root / rel
            if not d.is_dir():
                missing.append(rel)
        if missing:
            raise WorkspaceError(
                f"Workspace missing directories (run `ecat init`): {missing}"
            )

    # ── subprocess environment injection ────────────────────────────────────

    def agent_env(self, role: str) -> dict[str, str]:
        """Return env-var overrides to isolate the agent *role*."""
        import os

        env = dict(os.environ)
        config_dir = str(self.agent_config_dir(role).resolve())
        env["CLAUDE_CONFIG_DIR"] = config_dir
        # Opencode equivalent (speculative — adjust when opencode exposes this)
        env["OPENCODE_CONFIG_DIR"] = config_dir
        return env

    # ── state helpers ───────────────────────────────────────────────────────

    def read_state(self) -> dict[str, object]:
        if not self.state_file.exists():
            return {}
        result: dict[str, object] = json.loads(self.state_file.read_text(encoding="utf-8"))
        return result

    def write_state(self, updates: dict[str, object]) -> None:
        state = self.read_state()
        state.update(updates)
        self.state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _seed_dir() -> Path | None:
    """Locate the package's seed/ directory containing agent templates."""
    pkg_dir = Path(__file__).resolve().parent
    candidate = pkg_dir / "seed"
    if candidate.is_dir():
        return candidate
    return None
