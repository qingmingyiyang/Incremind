"""Pure compatibility contracts for quarantined external extensions.

This package intentionally has no acquisition, persistence, activation, or
runtime imports.  It turns a frozen staging inventory into review candidates;
existing Core authorities remain responsible for every later lifecycle step.
"""

from .adapters import StaticExtensionAdapterRegistry, inspect_extension
from .contracts import (
    AdapterResult,
    CompatibilityIssue,
    ContributionCandidate,
    ExtensionContractError,
    ExtensionManifest,
    FrozenRuntimeContract,
    PermissionPlan,
    ResolvedSource,
)
from .detection import (
    ArtifactInventory,
    Detection,
    ExtensionDetectionError,
    StaticExtensionDetectorRegistry,
)
from .source_spec import SourceSpec, SourceSpecError, parse_source_spec
from .install_intent import InstallIntent, InstallIntentError, parse_install_intent
from .review_projection import ExtensionReviewError, ExtensionReviewPlan, derive_review_plan
from .mcp_import_review_input import (
    MCPImportReviewContext,
    MCPImportReviewInput,
    MCPImportReviewInputError,
    derive_mcp_import_review_inputs,
)

__all__ = [
    "AdapterResult",
    "ArtifactInventory",
    "CompatibilityIssue",
    "ContributionCandidate",
    "Detection",
    "ExtensionContractError",
    "ExtensionDetectionError",
    "ExtensionManifest",
    "FrozenRuntimeContract",
    "InstallIntent",
    "InstallIntentError",
    "MCPImportReviewInput",
    "MCPImportReviewContext",
    "MCPImportReviewInputError",
    "PermissionPlan",
    "ResolvedSource",
    "SourceSpec",
    "SourceSpecError",
    "StaticExtensionAdapterRegistry",
    "StaticExtensionDetectorRegistry",
    "inspect_extension",
    "derive_review_plan",
    "derive_mcp_import_review_inputs",
    "parse_install_intent",
    "parse_source_spec",
    "ExtensionReviewError",
    "ExtensionReviewPlan",
]
