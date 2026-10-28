"""a2h_verify plugin set — registers all evaluation plugins.

File-naming convention: every check module is `check_<dim>.py` where `<dim>`
matches the plugin's `name` field (the key users reference in
`entropy.weights` config). To add a new plugin, follow the same pattern and
register it in `_PLUGINS` below.
"""

from ecat.entropy.plugins.a2h_verify.check_app_identity import AppIdentityPlugin
from ecat.entropy.plugins.a2h_verify.check_compile import CompilePlugin
from ecat.entropy.plugins.a2h_verify.check_crash_risk import CrashRiskPlugin
from ecat.entropy.plugins.a2h_verify.check_data_layer_parity import DataLayerParityPlugin
from ecat.entropy.plugins.a2h_verify.check_entry_nav import EntryNavPlugin
from ecat.entropy.plugins.a2h_verify.check_feature_check import FeatureCheckPlugin
from ecat.entropy.plugins.a2h_verify.check_feature_checklist import FeatureChecklistPlugin
from ecat.entropy.plugins.a2h_verify.check_hallucination import HallucinationPlugin
from ecat.entropy.plugins.a2h_verify.check_page_audit import PageAuditPlugin
from ecat.entropy.plugins.a2h_verify.check_permission_parity import PermissionParityPlugin
from ecat.entropy.plugins.a2h_verify.check_placeholder import PlaceholderPlugin
from ecat.entropy.plugins.a2h_verify.check_runtime_liveness import RuntimeLivenessPlugin
from ecat.entropy.plugins.a2h_verify.check_source_parity import SourceParityPlugin
from ecat.entropy.plugins.a2h_verify.check_static_lint import StaticLintPlugin
from ecat.entropy.plugins.a2h_verify.check_style import StylePlugin
from ecat.entropy.plugins.a2h_verify.check_ui_align import UiAlignPlugin
from ecat.entropy.registry import PluginRegistry

_PLUGINS = [
    CompilePlugin(),          # CHECK-1: compile
    StaticLintPlugin(),       # CHECK-2: static lint
    PlaceholderPlugin(),      # CHECK-3: placeholder scan
    AppIdentityPlugin(),      # CHECK-4: app identity
    PageAuditPlugin(),        # CHECK-5: page status audit
    FeatureCheckPlugin(),     # CHECK-6: feature check
    UiAlignPlugin(),          # CHECK-7: UI alignment
    EntryNavPlugin(),         # CHECK-10: entry navigation completeness
    SourceParityPlugin(),     # CHECK-12: Android↔Harmony source file inventory diff
    StylePlugin(),            # style: ArkTS style violations (supplementary)
    PermissionParityPlugin(),  # N4: Android↔HarmonyOS permission/capability parity
    DataLayerParityPlugin(),  # N3: Android↔HarmonyOS DB table + preference-key parity
    RuntimeLivenessPlugin(),  # N1: cold-launch liveness + main-page smoke-tap (device, weight 0 default)
    HallucinationPlugin(),    # N5: Harmony→Android reverse direction — invented (no-origin) features (weight 0 default)
    CrashRiskPlugin(),        # N6: click-crash static risk — ArkUI contract + resource + global handler
    FeatureChecklistPlugin(),  # N7: 人工功能清单逐条核对（需 paths.feature_checklist，weight 0 default）
]

PluginRegistry.register("a2h_verify", _PLUGINS)

__all__ = [
    "AppIdentityPlugin",
    "CompilePlugin",
    "CrashRiskPlugin",
    "DataLayerParityPlugin",
    "EntryNavPlugin",
    "FeatureCheckPlugin",
    "FeatureChecklistPlugin",
    "HallucinationPlugin",
    "PageAuditPlugin",
    "PermissionParityPlugin",
    "PlaceholderPlugin",
    "RuntimeLivenessPlugin",
    "SourceParityPlugin",
    "StaticLintPlugin",
    "StylePlugin",
    "UiAlignPlugin",
]
