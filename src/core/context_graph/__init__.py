"""Platform-owned contracts for the LineMap product capability."""

from .models import (
    ContextBinding,
    ContextGraphEdge,
    ContextGraphNode,
    ContextGraphSnapshot,
    ContextProvenance,
    IntegrityIssue,
)
from .compiler import (
    ContextCompilationError,
    ContextCompiler,
    FrozenContextRevisions,
    StalenessEvaluationInput,
)
from .binding_payload import (
    ContextBindingPayloadError,
    context_binding_from_payload,
    context_binding_to_payload,
    validate_context_binding_payload,
)
from .capability_loader import (
    CapabilityPackageDesiredState,
    CapabilityPackageError,
    CapabilityPackageLoader,
    CapabilityPackageManifest,
)
from .capability_artifact import CapabilityArtifact, CapabilityArtifactError, CapabilityArtifactStore
from .evaluators import (
    ContextBudgetEvaluator,
    ContextBudgetResult,
    ContextPermissionError,
    ContextPermissionGrant,
    ContextStalenessEvaluator,
)
from .proposal_router import ProposalAdapter, ProposalRouteError, ProposalRouter
from .migration import snapshot_from_dict
from .adapter_registry import (
    ContextGraphAdapterRegistry,
    ContextGraphAdapterRegistryError,
    ContextGraphImportRegistration,
)
from .protocols import (
    AuthorizedContextFile,
    AuthorizedContextFileError,
    ContextGraphExporter,
    ContextGraphImporter,
    ImportLimits,
    issue_authorized_context_file,
)
from .staleness import (
    StalenessConfirmation,
    StalenessImpactPreview,
    evaluate_staleness,
    stale_replay_order,
    staleness_impact_preview,
    validate_staleness_confirmation,
)
from .validation import ContextGraphValidationError, validate_snapshot
from .benchmark_models import TurnModelObservation
from .model_projection import (
    ContextModelProjectionError,
    canonical_model_projection_json,
    context_binding_model_projection,
    estimate_model_projection_tokens,
)
from .replay_completion import (
    InMemoryReplayCompletionRepository,
    NodeGenerationReceipt,
    ReplayCompletionAuthority,
    ReplayCompletionConflict,
    ReplayCompletionError,
    ReplayCompletionRepository,
    ReplayPlan,
    ReplayRequest,
    ReplayRevisionAuthority,
    TrustedCompletion,
)

__all__ = [
    "ContextBinding",
    "ContextModelProjectionError",
    "ContextBudgetEvaluator",
    "ContextBudgetResult",
    "CapabilityPackageError",
    "CapabilityPackageDesiredState",
    "CapabilityPackageLoader",
    "CapabilityPackageManifest",
    "CapabilityArtifact",
    "CapabilityArtifactError",
    "CapabilityArtifactStore",
    "ContextCompilationError",
    "ContextCompiler",
    "ContextBindingPayloadError",
    "ContextGraphEdge",
    "ContextGraphAdapterRegistry",
    "ContextGraphAdapterRegistryError",
    "ContextGraphImportRegistration",
    "ContextGraphExporter",
    "ContextGraphImporter",
    "ContextGraphNode",
    "ContextGraphSnapshot",
    "ContextGraphValidationError",
    "ContextProvenance",
    "context_binding_from_payload",
    "context_binding_model_projection",
    "context_binding_to_payload",
    "validate_context_binding_payload",
    "ContextPermissionError",
    "ContextPermissionGrant",
    "ContextStalenessEvaluator",
    "FrozenContextRevisions",
    "StalenessEvaluationInput",
    "StalenessConfirmation",
    "StalenessImpactPreview",
    "AuthorizedContextFile",
    "AuthorizedContextFileError",
    "ImportLimits",
    "IntegrityIssue",
    "InMemoryReplayCompletionRepository",
    "NodeGenerationReceipt",
    "ProposalAdapter",
    "ProposalRouteError",
    "ProposalRouter",
    "ReplayCompletionAuthority",
    "ReplayCompletionConflict",
    "ReplayCompletionError",
    "ReplayCompletionRepository",
    "ReplayPlan",
    "ReplayRequest",
    "ReplayRevisionAuthority",
    "TurnModelObservation",
    "TrustedCompletion",
    "evaluate_staleness",
    "estimate_model_projection_tokens",
    "canonical_model_projection_json",
    "stale_replay_order",
    "staleness_impact_preview",
    "validate_staleness_confirmation",
    "issue_authorized_context_file",
    "snapshot_from_dict",
    "validate_snapshot",
]
