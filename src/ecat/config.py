"""ECAT configuration — Pydantic v2 schema + YAML loader + CLI-override merge."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator, model_validator

load_dotenv(Path(".env"))

# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------


class PathsConfig(BaseModel):
    android: Path
    harmony: Path
    # Default to a per-project subfolder so multiple Android apps can coexist
    # in the same workspace without their specs clobbering each other.  Users
    # who edit `paths.spec` in the yaml keep their explicit value.
    spec: Path = Path("./specs/default/spec")
    # 人工维护的功能清单（xlsx / csv / md 表格）——feature_checklist 插件的判据。
    # 不配、或文件不存在 → 该插件自动跳过（贡献 0），不影响其它维度。
    feature_checklist: Path | None = None


class HarmonyConfig(BaseModel):
    initial_state: Literal["empty", "migrated"] = "empty"


class AgentConfig(BaseModel):
    runtime: Literal["claude_code", "opencode", "deveco_code"] = "claude_code"
    model: str = "claude-sonnet-4-6"
    provider: str = "anthropic"
    base_url: str | None = None
    api_key_env: str = "ANTHROPIC_API_KEY"
    config_dir: Path = Path(".ecat/agents/generator")
    timeout_s: int = 3600
    extra_args: list[str] = Field(default_factory=list)
    evolution_write_paths: list[Path] = Field(default_factory=list)
    # When True, reuse the Claude Code session across refine iterations
    # (claude --resume <session_id>) instead of starting fresh each iter.
    # Saves cache_create cost but accumulates context across iters — only
    # safe for the Generator role.  No effect on opencode runtime.
    resume_session: bool = False


class OscillationGuard(BaseModel):
    enabled: bool = True
    window: int = 5
    min_delta: float = 0.02


class LoopConfig(BaseModel):
    max_iterations: int = 100
    entropy_threshold: float = 0.01
    stable_iters_to_stop: int = 2
    regression_tolerance: float = 0.1
    revert_on_regression: bool = True
    oscillation_guard: OscillationGuard = Field(default_factory=OscillationGuard)
    # When true, the Generator is instructed to invoke /harmonyos-build-fix
    # at the end of each iteration so the project compiles before the
    # Discriminator evaluates it.  Requires deveco_studio_path (or JAVA_HOME).
    build_verify: bool = True
    # DevEco Studio installation root (e.g. "D:/DevEco Studio", or
    # "/Applications/DevEco-Studio.app/Contents" on macOS).  If null,
    # auto-detected from JAVA_HOME, then the default macOS install location.
    deveco_studio_path: str | None = None
    # When > 0 and generator.resume_session=True, call /compact every N
    # iterations to prevent context saturation.  0 = disabled.
    generator_compact_interval: int = 15
    # Max work-list items shown to the Generator per iteration.
    # Lower values → smaller per-iteration scope → faster but more iterations.
    max_items_per_iter: int = 25


_DEFAULT_WEIGHTS: dict[str, float] = {
    "compile": 0.30,
    "entry_nav": 0.20,
    "feature_check": 0.20,
    "page_audit": 0.10,
    "source_parity": 0.10,
    "static_lint": 0.10,
    "placeholder": 0.10,
    "app_identity": 0.10,
    "style": 0.10,
    "data_layer_parity": 0.30,  # N3: DB table + preference-key parity
    "permission_parity": 0.30,  # N4: permission/capability parity
    "hallucination": 0.10,  # N5: Harmony→Android invented (no-origin) features
    "crash_risk": 0.10,  # N6: 点击闪退静态风险（ArkUI 契约 + 资源 + 全局兜底）
    "feature_checklist": 0.10,  # N7: 人工功能清单逐条核对（无清单时自动 skip）
    # 默认权重 0（opt-in）：device 维度需模拟器
    "ui_align": 0.00,
    "runtime_liveness": 0.00,  # N1: cold-launch liveness + smoke-tap
}

# ── 版本预设 ──────────────────────────────────────────────────────────────────
# flash：纯静态确定性插件（judge_mode=hard 且不依赖模拟器）+ feature_checklist。
#        排除 runtime_liveness（需模拟器）与所有 agentic 维度。
_FLASH_PLUGINS: tuple[str, ...] = (
    "compile", "static_lint", "placeholder", "app_identity",
    "style", "data_layer_parity", "permission_parity", "crash_risk",
    "feature_checklist",
)

# pro：默认权重下，排除这几个——ui_align/runtime_liveness（需模拟器）
_PRO_EXCLUDE: frozenset[str] = frozenset(
    ("ui_align", "runtime_liveness")
)

# 版本预设的 loop 参数（max_iterations, entropy_threshold）。
# flash 快跑：轮数少、收敛阈值松；pro 标准：轮数多、阈值紧。custom 用 config 里写的。
_PRESET_LOOP: dict[str, tuple[int, float]] = {
    "flash": (30, 0.02),
    "pro": (100, 0.01),
}


def _preset_weights(version: str, current: dict[str, float]) -> dict[str, float]:
    """按 version 套预设覆盖权重。custom 原样返回；flash/pro 用 _DEFAULT_WEIGHTS 的值，
    未列入该预设的插件权重一律 0。"""
    if version == "custom":
        return dict(current)
    if version == "flash":
        return {p: _DEFAULT_WEIGHTS.get(p, 0.10) for p in _FLASH_PLUGINS}
    # pro
    return {p: w for p, w in _DEFAULT_WEIGHTS.items() if p not in _PRO_EXCLUDE}


class JudgeModelConfig(BaseModel):
    runtime: Literal["anthropic_sdk", "openai_sdk"] = "anthropic_sdk"
    model: str = "claude-sonnet-4-6"
    api_key_env: str = "ANTHROPIC_API_KEY"
    base_url: str | None = None


class EntropyConfig(BaseModel):
    plugin_set: str = "a2h_verify"
    # ── 版本预设（flash / pro / custom）────────────────────────────────────────
    # flash：只跑纯静态确定性检查（compile/static_lint/placeholder/app_identity/
    #        crash_risk/data_layer_parity/permission_parity/style）
    #        + feature_checklist（人工清单逐条核对）。其余权重 0 —— 最快，无 LLM judge。
    # pro  ：除 ui_align/runtime_liveness 外全开，
    #        用下面 weights 里配置的权重。
    # custom：完全用下面 weights 里显式配置的权重（不再套预设）。
    # 默认 custom：未写 version 的现有 config 沿用自己配的 weights，行为不变（向后兼容）。
    # 要 flash/pro 必须显式写 version。预设只在 version != custom 时覆盖 weights
    # （未列出的插件权重一律 0）。
    version: Literal["flash", "pro", "custom"] = "custom"
    weights: dict[str, float] = Field(default_factory=lambda: dict(_DEFAULT_WEIGHTS))
    judge_model: JudgeModelConfig = Field(default_factory=JudgeModelConfig)
    # Max issues each agentic judge subagent reports per dimension.
    # Lower values → judge stops exploring sooner → faster discriminator.
    judge_max_issues: int = 10
    # 动态渲染间隔：device-driven 维度（ui_align 视觉对齐、runtime_liveness 冷启动存活）
    # 的统一运行节奏——在 iter 0 以及每第 N 轮真机评估（重编译/安装/截图/启动），其余轮
    # 跳过（贡献 0）。N=1 等于每轮都跑；默认 5。
    # 关闭某个 device 维度：把对应 entropy.weights（ui_align / runtime_liveness）设为 0。
    dynamic_render_interval: int = 5

    @model_validator(mode="after")
    def _apply_version_preset(self) -> EntropyConfig:
        """version=flash/pro 时用预设覆盖 weights；custom 时保留用户显式配置。

        覆盖是**整体替换**：未列入该预设的插件权重一律 0，避免用户手改的 weights 残留
        与预设混在一起（那样 flash 会意外带上某个 agentic 维度）。
        """
        if self.version != "custom":
            self.weights = _preset_weights(self.version, self.weights)
        return self


class RetryConfig(BaseModel):
    max_attempts: int = 3
    base_delay_s: float = 2.0
    max_delay_s: float = 30.0
    jitter: bool = True
    retry_on: list[str] = Field(
        default_factory=lambda: ["timeout", "rate_limit", "server_error", "schema_error"]
    )
    retry_on_subprocess_crash: bool = True


class FeaturesConfig(BaseModel):
    memory_evolution: bool = True
    skill_evolution: bool = False
    multi_discriminator: bool = False
    in_loop_intervention: bool = False
    # Deterministic resource porting before every entropy evaluation (no tokens).
    resource_porter: bool = True


class SkillsConfig(BaseModel):
    """On-demand Agent Skills, fetched from the Matrix registry."""

    registry: str = "https://matrix.openharmony.cn"
    auto_fetch: bool = True
    find_engine: Literal["matrix", "skills_sh"] = "matrix"
    # Names guaranteed to be installed before every round; unknown names are logged, not fatal.
    require: list[str] = Field(default_factory=list)


class UiFlowsConfig(BaseModel):
    """T2 functional gate: run UI flows on a device after each evaluation.

    Off by default — point `spec` at a flows JSON to enable. Failures become
    high-severity work items (injected by code, not by the discriminator).
    """

    enabled: bool = False
    spec: str | None = None
    # Runner is invoked as: <python> <runner> --flows ... --out ...
    python: str = "python"
    runner: str = "tools/ui_runner.py"
    bundle: str | None = None
    device: str | None = None
    timeout_s: float = 300.0


class ReportingConfig(BaseModel):
    chart_format: Literal["png", "html"] = "png"
    chart_on_each_iter: bool = True
    keep_run_artifacts: int = -1
    log_level: str = "info"


# ---------------------------------------------------------------------------
# Root config
# ---------------------------------------------------------------------------


class EcatConfig(BaseModel):
    project: str = "my-android-app"
    work_dir: Path = Path(".")

    paths: PathsConfig
    harmony: HarmonyConfig = Field(default_factory=HarmonyConfig)
    skills: SkillsConfig = Field(default_factory=SkillsConfig)

    generator: AgentConfig = Field(
        default_factory=lambda: AgentConfig(config_dir=Path(".ecat/agents/generator"))
    )
    discriminator: AgentConfig = Field(
        default_factory=lambda: AgentConfig(
            config_dir=Path(".ecat/agents/discriminator"),
            timeout_s=2400,
            evolution_write_paths=[
                Path(".ecat/agents/generator/skills"),
                Path(".ecat/agents/generator/memory"),
            ],
        )
    )

    loop: LoopConfig = Field(default_factory=LoopConfig)
    entropy: EntropyConfig = Field(default_factory=EntropyConfig)
    retry: RetryConfig = Field(default_factory=RetryConfig)
    features: FeaturesConfig = Field(default_factory=FeaturesConfig)
    ui_flows: UiFlowsConfig = Field(default_factory=UiFlowsConfig)
    reporting: ReportingConfig = Field(default_factory=ReportingConfig)

    # ── validators ──────────────────────────────────────────────────────────

    @field_validator("paths", mode="before")
    @classmethod
    def _resolve_paths(cls, v: Any) -> Any:
        return v  # resolution happens in model_validator

    @model_validator(mode="after")
    def _validate_paths_exist(self) -> EcatConfig:
        android = Path(self.paths.android)
        if not android.exists():
            raise ValueError(f"paths.android does not exist: {android}")

        harmony = Path(self.paths.harmony)
        if not harmony.exists():
            raise ValueError(f"paths.harmony does not exist: {harmony}")

        if self.harmony.initial_state == "migrated":
            entry = harmony / "entry"
            app_scope = harmony / "AppScope"
            if not entry.exists() or not app_scope.exists():
                raise ValueError(
                    "harmony.initial_state=migrated but harmony/ is missing "
                    "entry/ or AppScope/ subdirectories"
                )
        return self

    @model_validator(mode="after")
    def _apply_version_loop_preset(self) -> EcatConfig:
        """entropy.version=flash/pro 时，把 loop 的 max_iterations/entropy_threshold
        套上该版本的预设值；custom（默认）则保留 config 里写的 loop 参数。"""
        preset = _PRESET_LOOP.get(self.entropy.version)
        if preset is not None:
            self.loop.max_iterations, self.loop.entropy_threshold = preset
        return self

    @model_validator(mode="after")
    def _validate_api_keys(self) -> EcatConfig:
        # An empty string means "no API key needed" — used for claude_code runtime
        # under a Claude.ai subscription where authentication is handled by the
        # `claude` CLI itself (via `claude login`). Other runtimes still require
        # a key in the environment.
        for agent_name, agent in [
            ("generator", self.generator),
            ("discriminator", self.discriminator),
        ]:
            key = agent.api_key_env
            if not key:
                continue
            if not os.environ.get(key):
                raise ValueError(
                    f"{agent_name}.api_key_env={key!r} is not set in the environment"
                )
        return self

    @model_validator(mode="after")
    def _validate_runtimes_available(self) -> EcatConfig:
        import shutil

        runtime_to_cmd = {"claude_code": "claude", "opencode": "opencode", "deveco_code": "deveco"}
        for agent_name, agent in [
            ("generator", self.generator),
            ("discriminator", self.discriminator),
        ]:
            cmd = runtime_to_cmd[agent.runtime]
            if shutil.which(cmd) is None:
                raise ValueError(
                    f"{agent_name}.runtime={agent.runtime!r} requires `{cmd}` on PATH, "
                    "but it was not found"
                )
        return self


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def load_config(path: Path) -> EcatConfig:
    """Load *path* (YAML) into an EcatConfig."""
    with path.open(encoding="utf-8") as fh:
        data: dict[str, Any] = yaml.safe_load(fh) or {}
    return EcatConfig.model_validate(data)


# ---------------------------------------------------------------------------
# Sample config writer (used by `ecat init`)
# ---------------------------------------------------------------------------

SAMPLE_CONFIG = """\
# ─── Identity ────────────────────────────────────────────────────────────────
project: my-android-app
work_dir: .

# ─── Source / Target paths ───────────────────────────────────────────────────
# `spec` is per-project so the workspace can host several migrations side by
# side (specs/<project>/spec/baseline/...).  `ecat refine` reads the spec/plan
# from this path when it exists.
paths:
  android: ./android
  harmony: ./harmony
  spec:    ./specs/my-android-app/spec

harmony:
  initial_state: empty   # empty | migrated

# ─── Agents ──────────────────────────────────────────────────────────────────
# Each agent can use a different runtime, provider, model, and base_url.
# base_url: set to your provider's OpenAI-compatible endpoint, or null for default.
# api_key_env: the NAME of the env var in .env (not the key itself).

generator:
  runtime: claude_code       # claude_code | opencode
  model: claude-sonnet-4-6   # any model your provider supports
  provider: anthropic
  base_url: null             # e.g. https://api.deepseek.com/v1
  api_key_env: ANTHROPIC_API_KEY
  config_dir: .ecat/agents/generator
  timeout_s: 3600
  extra_args: []
  resume_session: true

discriminator:
  runtime: claude_code
  model: claude-sonnet-4-6
  provider: anthropic
  base_url: null
  api_key_env: ANTHROPIC_API_KEY
  config_dir: .ecat/agents/discriminator
  timeout_s: 2400
  extra_args: []
  evolution_write_paths:
    - .ecat/agents/generator/skills
    - .ecat/agents/generator/memory

# ─── Adversarial loop ────────────────────────────────────────────────────────
loop:
  max_iterations: 100
  entropy_threshold: 0.01
  # stable_iters_to_stop: H 需连续 N 轮低于 entropy_threshold 才提前退出。
  # 判别器每轮只抽查部分问题，单轮"安静"不代表收敛；N>=2 可防过早停止。默认 2。
  stable_iters_to_stop: 2

  # regression_tolerance: 相邻两轮之间的回归容忍度。
  # 如果本轮 H 比上轮 H 增加超过此值，判定为"回归"（代码质量变差）。
  # revert_on_regression=true 时会 git checkout 撤销本轮 Generator 的改动。
  # 作用：防止单次迭代引入破坏性变更。
  regression_tolerance: 0.1
  revert_on_regression: true

  # oscillation_guard: 检测循环是否陷入"震荡"（反复修改同一问题无法收敛）。
  # window: 回看最近 N 轮的 H 值序列。
  # min_delta: 如果最近 window 轮中相邻两轮的 H 变化绝对值都 < min_delta，
  #   说明 Generator 在原地打转，触发提前终止。
  # 作用：避免浪费 token 在无效迭代上。
  oscillation_guard:
    enabled: true
    window: 5
    min_delta: 0.02

  # build_verify: 每轮 Generator 结束后强制调用 harmonyos-build-fix skill 验证编译。
  # deveco_studio_path: DevEco Studio 安装根目录，null 时从 JAVA_HOME 自动推断（macOS 装在 /Applications 下也能自动找到）。
  #   Windows 路径用正斜杠或双反斜杠，含空格时必须加引号，例如：deveco_studio_path: "D:/DevEco Studio"
  #   macOS 例如：deveco_studio_path: /Applications/DevEco-Studio.app/Contents
  build_verify: true
  deveco_studio_path: null
  # generator_compact_interval: resume_session=true 时每隔 N 轮调用 /compact 压缩会话。
  # 0 = 禁用。推荐值 15（每 15 轮压缩一次）。
  generator_compact_interval: 15
  # max_items_per_iter: 每轮传给生成器的 work-list 最大条目数。
  # 调小可缩短每轮耗时，但需要更多轮次收敛。默认 25。
  max_items_per_iter: 25

# ─── Entropy plugin set ───────────────────────────────────────────────────────
entropy:
  plugin_set: a2h_verify
  # judge_max_issues: 每个 agentic judge 子代理最多上报的问题数。
  # 调小可让 judge 更早停止探索文件，加快判别器。默认 10。
  judge_max_issues: 10
  # dynamic_render_interval: device-driven 维度（ui_align 视觉对齐 + runtime_liveness
  # 冷启动存活）的统一运行节奏。在 iter 0 及每第 N 轮真机评估（重编译/安装/截图/启动），
  # 其余轮跳过。N=1=每轮跑。默认 5。关闭某维度：把对应 weights 设为 0。
  dynamic_render_interval: 5
  weights:
    compile:        0.30
    entry_nav:      0.20
    feature_check:  0.20
    static_lint:    0.10
    placeholder:    0.10
    app_identity:   0.10
    page_audit:     0.10
    source_parity:  0.10
    style:          0.10
    data_layer_parity:  0.30   # N3: 数据库表 + 设置键 对账
    permission_parity:  0.30   # N4: 权限/能力 对账
    hallucination:      0.10   # N5: 鸿蒙→安卓 凭空造出的(无出处)功能
    # 默认权重 0（opt-in）：device 维度需模拟器
    ui_align:          0.0
    runtime_liveness:  0.0     # N1: 冷启动存活 + 主页 smoke-tap
# ─── Retry ────────────────────────────────────────────────────────────────────
retry:
  max_attempts: 3
  base_delay_s: 2
  max_delay_s: 30
  jitter: true

# ─── Phase gates ─────────────────────────────────────────────────────────────
features:
  memory_evolution: true
  skill_evolution:  false
  multi_discriminator: false
  in_loop_intervention: false

# ─── Reporting ───────────────────────────────────────────────────────────────
reporting:
  chart_format: png
  chart_on_each_iter: true
  keep_run_artifacts: -1
  log_level: info
"""
