"""Base classes for entropy plugins and reports."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from ecat.gradient.schema import WorkItem


@dataclass
class EntropyReport:
    """Result of a single entropy evaluation pass."""

    iteration: int
    total_H: float  # ∈ [0, 1], lower is better
    breakdown: dict[str, float] = field(default_factory=dict)  # plugin → score ∈ [0,1]
    details: dict[str, dict[str, Any]] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    tokens_in: int = 0  # discriminator agentic judge input tokens (non-cached)
    tokens_out: int = 0  # discriminator agentic judge output tokens
    cache_read: int = 0  # discriminator agentic judge cache-read tokens

    def to_dict(self) -> dict[str, object]:
        return {
            "iteration": self.iteration,
            "total_H": self.total_H,
            "breakdown": self.breakdown,
            "details": self.details,
            "timestamp": self.timestamp.isoformat(),
        }


class EntropyPlugin(ABC):
    """Abstract base class for a single entropy dimension."""

    #: Unique name used in config weights and breakdown keys
    name: str = ""

    #: Default weight when not specified in config
    default_weight: float = 0.1

    #: Shared work-item fix directive, promoted from the generator prompt so a weak
    #: model acts per work item. WebSearch is scoped to third-party SDK / @kit only.
    FIX_HINT: str = "implement for real, no stub/defer/fabricate; WebSearch docs only for a third-party SDK or unfamiliar @kit"

    #: Judge prompt — describes how to assess this dimension (used by llm/agentic modes)
    judge_prompt: str = ""

    #: Evaluation mode: hard = deterministic check only,
    #: agentic = + agent subprocess with full codebase access
    judge_mode: Literal["hard", "agentic"] = "hard"

    #: Subagent type the discriminator dispatches for this dimension's agentic
    #: judge (deferred-judge path). Defaults to the generic read-only
    #: "entropy-judge". Plugins needing extra tool grants (e.g. ui_align, whose
    #: judge drives on-device capture and therefore needs Bash) override this
    #: with a custom agent definition under seed/agents/discriminator/agents/.
    judge_subagent_type: str = "entropy-judge"

    #: Default fix skill suggested on WorkItems derived from this plugin's
    #: agentic verdict issues (used by gradient.extractors). "" = no default
    #: (the discriminator may still map a skill).
    default_fix_skill: str = ""

    #: Weight of hard check score in blend with judge score.
    #: 0.0 = judge score only, 1.0 = hard score only, default 0.4.
    hard_weight: float = 0.4

    @abstractmethod
    def evaluate(
        self,
        harmony_dir: Path,
        *,
        android_dir: Path | None = None,
        spec_dir: Path | None = None,
        config: dict[str, object] | None = None,
    ) -> tuple[float, dict[str, object]]:
        """Return (score ∈ [0,1], details).

        score == 0 → perfect (no entropy in this dimension)
        score == 1 → worst
        """

    def extract_work_items(self, score: float, details: dict[str, object]) -> list[WorkItem]:
        """Extract actionable WorkItems from this plugin's evaluation details.

        Default: return empty list. Override in subclass to provide extraction.
        """
        return []


# ── shared utilities for plugins ──────────────────────────────────────────────

# HarmonyOS project directories that contain third-party deps or build artifacts.
# oh_modules/node_modules: package manager dependencies
# build: hvigor compilation cache (contains copies of source files)
# .preview/.hvigor/.cxx: IDE and toolchain caches
# .claude: agent/skill config (e.g. skill template .ets files) — not project code
_SKIP_DIRS = frozenset(
    (
        "oh_modules",
        "node_modules",
        "build",
        ".preview",
        ".hvigor",
        ".cxx",
        ".claude",
        # ECAT workspace 数据目录。当 `ecat init`/`ecat run` 从鸿蒙仓根启动时，`.ecat`
        # 会落在仓根下（build logs、agents/、runs_*/、state.json 等）。这些不是 target
        # source —— 判别器插件遍历 harmony_dir 时绝不能扫到 `.ecat` 里的 .json/.ets 等
        # （agent 配置、runs 数据会被当成真实源码，污染评估）。git 层已有 `.gitignore`
        # 忽略它，这里补齐遍历层的排除。与 `_HARMONY_GITIGNORE_ENTRIES`（workspace.py）呼应。
        ".ecat",
        # IDE / agent 配置目录（e.g. DevEco 的 .idea 含 .deveco/project.cache.json）。
        # should_skip_path 已用“点目录一律跳过”覆盖遍历层；这里显式列出是为了让
        # skip_dir_names / git_exclude_pathspecs（git 层，不认点前缀）也排除它们，
        # 免被 ecat 每轮的 `git add .` 扫进 checkpoint 提交。
        ".idea",
        ".codex",
        ".deveco",
    )
)


def skip_dir_names() -> list[str]:
    """Plain directory names to unstage after a bulk `git add`.

    `git add -- . :(exclude)build` EXITS 1 with "The following paths are ignored by
    one of your .gitignore files" — git treats an exclude pathspec as explicitly
    naming that path, and refuses when it is gitignored. That silently broke the
    iteration checkpoint (caught by `except`, logged at debug only): 8 iterations
    ran with no commit at all. So staging must be "add everything, then unstage
    these", never "add with excludes". `git reset -- <path>` ignores .gitignore and
    exits 0 even when the path does not exist, so it is safe for every entry here.
    """
    return sorted(_SKIP_DIRS)


def git_exclude_pathspecs() -> list[str]:
    """`:(exclude)` pathspecs for the same dirs plugins ignore.

    Safe for `git diff` / `checkout` / `clean` (they do not consult .gitignore).
    NOT safe for `git add` — see `skip_dir_names`.

    Keeps one source of truth with `_SKIP_DIRS`. Without this the iteration
    checkpoint runs a bare `git add .`, which sweeps the whole working tree into
    the commit — build artifacts, dependencies and `.claude/` agent config that
    the Generator never touched. Observed damage: one iteration was recorded as
    "354 files +374/-90555 lines" when the Generator had actually changed 5 source
    files by 123 lines; the other 90k deletions were a pre-existing `.claude/skills`
    removal sitting unstaged in the working tree. That mis-attributes change volume
    and, worse, makes `revert_on_regression` roll back unrelated work.
    """
    return [f":(exclude){d}" for d in sorted(_SKIP_DIRS)]


def should_skip_path(filepath: Path, base: Path) -> bool:
    """Return True if filepath is inside a dependency/build directory that plugins should ignore.

    两条规则（满足任一即跳过）：
      1. 路径任一段命中 `_SKIP_DIRS`（依赖/构建目录白名单）。
      2. 路径任一段**以 `.` 开头**——点目录是工具/IDE/agent 配置（.idea/.codex/.deveco/.git/
         .vscode 等），不是项目源码。逐个项目往 `_SKIP_DIRS` 加白名单会永远追不完新工具；
         实测鸿蒙仓里所有点目录下的 `.ets` 全在 `oh_modules/.ohpm/`（依赖，已被规则 1 覆盖），
         没有任何项目源码藏在点目录，所以"点目录一律跳过"是安全的通用规则。
    """
    try:
        rel = filepath.relative_to(base)
    except ValueError:
        return False
    parts = set(rel.parts)
    if _SKIP_DIRS & parts:
        return True
    # 只对“目录段”判点前缀（rel.parts[:-1]），最后一段是文件名，不当作目录处理。
    return any(p.startswith(".") for p in rel.parts[:-1])
