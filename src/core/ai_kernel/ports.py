from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import re
from typing import TYPE_CHECKING, ContextManager, Literal, Protocol, runtime_checkable

from core.ai_tooling.contracts import ToolDefinition
from core.model_gateway import ModelExecutionControlPort

if TYPE_CHECKING:
    from .dispatcher import ToolDispatchRequest


TurnStatus = Literal[
    "accepted",
    "running",
    "waiting_approval",
    "completed",
    "failed",
    "cancelled",
]

RunLeaseStatus = Literal["active", "recovery_required", "quarantined"]
RunLeaseRecoveryDisposition = Literal["safe", "quarantined"]
RecoveryDisposition = Literal["terminal_noop", "waiting_noop", "safe_resume", "quarantine"]
RecoveryReviewStatus = Literal[
    "quarantined", "kept_quarantined", "resume_queued", "turn_completed", "turn_failed",
    "turn_cancelled", "waiting_approval", "resume_failed",
]
_RECOVERY_REASON = re.compile(r"^ai\.recovery_[a-z0-9_]{3,96}$")
_RECOVERY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$")
_RECOVERY_EVENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,159}$")
_RECOVERY_EVENT_TYPE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")
_BOUNDARY_REASON = re.compile(r"^[a-z0-9][a-z0-9_]{2,127}$")
_RECOVERY_TERMINAL_TYPES = frozenset({"turn.completed", "turn.failed", "turn.cancelled"})
_DURABLE_MODEL_WIRE_COMMIT_SEAL = object()


class DurableModelWireCommitWitness:
    """Process-local proof that the SQLite dispatch bundle committed."""

    __slots__ = ("_seal",)

    def __init__(self, seal: object) -> None:
        if seal is not _DURABLE_MODEL_WIRE_COMMIT_SEAL:
            raise TypeError("durable model wire commit witness is authority-owned")
        self._seal = seal


def _issue_durable_model_wire_commit_witness() -> DurableModelWireCommitWitness:
    return DurableModelWireCommitWitness(_DURABLE_MODEL_WIRE_COMMIT_SEAL)


def is_durable_model_wire_commit_witness(value: object) -> bool:
    return (
        isinstance(value, DurableModelWireCommitWitness)
        and value._seal is _DURABLE_MODEL_WIRE_COMMIT_SEAL
    )


@dataclass(frozen=True, slots=True)
class RecoveryDecision:
    turn_id: str
    generation: int
    disposition: RecoveryDisposition
    reason_code: str
    last_sequence: int
    last_event_id: str
    last_event_type: str


@dataclass(frozen=True, slots=True)
class RecoveryQueueItem:
    """A server-issued fenced token for one audited safe recovery attempt."""
    turn_id: str
    prior_generation: int
    reason_code: str
    attempt: int
    run_lease: RunLeaseToken


@dataclass(frozen=True, slots=True)
class PublicRecoveryReview:
    """Opaque, metadata-only recovery review projection for authenticated callers."""

    review_id: str
    project_id: str | None
    status: RecoveryReviewStatus
    revision: int
    reason_code: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class RecoveryReviewAuthorization:
    """Metadata-only evidence that the API has authenticated and evaluated an action."""

    actor_id: str
    boundary_outcome: Literal["allow", "ask"]
    boundary_reason_codes: tuple[str, ...]
    policy_revision: int
    human_confirmed: bool


def validate_recovery_decision(value: RecoveryDecision) -> RecoveryDecision:
    if not isinstance(value, RecoveryDecision) or not _RECOVERY_ID.fullmatch(value.turn_id):
        raise ValueError("recovery decision Turn identity is invalid")
    if not isinstance(value.generation, int) or isinstance(value.generation, bool) or value.generation < 1:
        raise ValueError("recovery decision generation is invalid")
    if value.disposition not in {"terminal_noop", "waiting_noop", "safe_resume", "quarantine"}:
        raise ValueError("recovery decision disposition is invalid")
    if not isinstance(value.reason_code, str) or not _RECOVERY_REASON.fullmatch(value.reason_code):
        raise ValueError("recovery decision reason is invalid")
    if not isinstance(value.last_sequence, int) or isinstance(value.last_sequence, bool) or value.last_sequence < 0:
        raise ValueError("recovery decision event sequence is invalid")
    identity = (value.last_event_id, value.last_event_type)
    if value.last_sequence == 0:
        if identity != ("", ""):
            raise ValueError("empty recovery event identity is invalid")
    elif (
        not isinstance(value.last_event_id, str)
        or len(value.last_event_id) > 160
        or not isinstance(value.last_event_type, str)
        or len(value.last_event_type) > 160
    ):
        raise ValueError("recovery decision event identity is invalid")
    if value.last_sequence > 0 and value.disposition != "quarantine" and (
        not _RECOVERY_EVENT_ID.fullmatch(value.last_event_id)
        or not _RECOVERY_EVENT_TYPE.fullmatch(value.last_event_type)
    ):
        raise ValueError("recovery decision event identity is invalid")
    expected = {
        "terminal_noop": (value.reason_code == "ai.recovery_terminal" and value.last_event_type in _RECOVERY_TERMINAL_TYPES),
        "waiting_noop": (value.reason_code == "ai.recovery_waiting_approval" and value.last_event_type == "approval.required"),
        "safe_resume": (value.reason_code == "ai.recovery_no_effect_started" and value.last_sequence > 0),
        "quarantine": value.reason_code not in {
            "ai.recovery_terminal", "ai.recovery_waiting_approval", "ai.recovery_no_effect_started",
        },
    }
    if not expected[value.disposition]:
        raise ValueError("recovery decision reason does not match disposition")
    return value


@dataclass(frozen=True, slots=True)
class RunLeaseToken:
    turn_id: str
    owner_id: str
    generation: int


@dataclass(frozen=True, slots=True)
class RunLeaseRecord:
    token: RunLeaseToken
    status: RunLeaseStatus
    acquired_at: datetime
    heartbeat_at: datetime
    stale_after: datetime


def validate_run_lease_time(value: datetime, *, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is not timezone.utc:
        raise ValueError(f"{name} must be an UTC datetime")
    return value


def validate_run_lease_token(value: RunLeaseToken) -> RunLeaseToken:
    if not isinstance(value, RunLeaseToken) or not value.turn_id or not value.owner_id:
        raise ValueError("run lease token identity is invalid")
    if not isinstance(value.generation, int) or isinstance(value.generation, bool) or value.generation < 1:
        raise ValueError("run lease token generation is invalid")
    return value


def validate_recovery_queue_item(value: RecoveryQueueItem) -> RecoveryQueueItem:
    if not isinstance(value, RecoveryQueueItem):
        raise ValueError("recovery queue item is invalid")
    validate_run_lease_token(value.run_lease)
    if value.turn_id != value.run_lease.turn_id or not _RECOVERY_ID.fullmatch(value.turn_id):
        raise ValueError("recovery queue Turn identity is invalid")
    if (
        not isinstance(value.prior_generation, int)
        or isinstance(value.prior_generation, bool)
        or value.prior_generation < 1
        or value.run_lease.generation != value.prior_generation + 1
    ):
        raise ValueError("recovery queue generation is invalid")
    if not isinstance(value.attempt, int) or isinstance(value.attempt, bool) or value.attempt < 1:
        raise ValueError("recovery queue attempt is invalid")
    if value.reason_code not in {
        "ai.recovery_no_effect_started",
        "ai.recovery_manual_confirmed_no_effect",
    }:
        raise ValueError("recovery queue reason is invalid")
    if not value.run_lease.owner_id.startswith("recovery-"):
        raise ValueError("recovery queue owner is invalid")
    return value


def validate_recovery_review_identity(value: str, *, name: str = "recovery review identity") -> str:
    if not isinstance(value, str) or not _RECOVERY_ID.fullmatch(value):
        raise ValueError(f"{name} is invalid")
    return value


def validate_public_recovery_review(value: PublicRecoveryReview) -> PublicRecoveryReview:
    if not isinstance(value, PublicRecoveryReview):
        raise ValueError("recovery review is invalid")
    validate_recovery_review_identity(value.review_id)
    if value.project_id is not None and (
        not isinstance(value.project_id, str) or not value.project_id or len(value.project_id) > 128
    ):
        raise ValueError("recovery review project identity is invalid")
    if value.status not in {
        "quarantined", "kept_quarantined", "resume_queued", "turn_completed", "turn_failed",
        "turn_cancelled", "waiting_approval", "resume_failed",
    }:
        raise ValueError("recovery review status is invalid")
    if not isinstance(value.revision, int) or isinstance(value.revision, bool) or value.revision < 1:
        raise ValueError("recovery review revision is invalid")
    if not isinstance(value.reason_code, str) or not _RECOVERY_REASON.fullmatch(value.reason_code):
        raise ValueError("recovery review reason is invalid")
    validate_run_lease_time(value.created_at, name="recovery review creation time")
    validate_run_lease_time(value.updated_at, name="recovery review update time")
    if value.updated_at < value.created_at:
        raise ValueError("recovery review time ordering is invalid")
    return value


def validate_recovery_review_authorization(value: RecoveryReviewAuthorization) -> RecoveryReviewAuthorization:
    if not isinstance(value, RecoveryReviewAuthorization) or not _RECOVERY_ID.fullmatch(value.actor_id):
        raise ValueError("recovery review actor identity is invalid")
    if value.boundary_outcome not in {"allow", "ask"}:
        raise ValueError("recovery review Boundary outcome is invalid")
    if not isinstance(value.boundary_reason_codes, tuple) or not value.boundary_reason_codes or any(
        not isinstance(reason, str) or not _BOUNDARY_REASON.fullmatch(reason)
        for reason in value.boundary_reason_codes
    ):
        raise ValueError("recovery review Boundary reasons are invalid")
    if not isinstance(value.policy_revision, int) or isinstance(value.policy_revision, bool) or value.policy_revision < 1:
        raise ValueError("recovery review policy revision is invalid")
    if not isinstance(value.human_confirmed, bool):
        raise ValueError("recovery review confirmation is invalid")
    if value.boundary_outcome == "ask" and not value.human_confirmed:
        raise ValueError("recovery review ask must include human confirmation")
    return value


def validate_run_lease_record(value: RunLeaseRecord) -> RunLeaseRecord:
    if not isinstance(value, RunLeaseRecord):
        raise ValueError("run lease record is invalid")
    validate_run_lease_token(value.token)
    if value.status not in {"active", "recovery_required", "quarantined"}:
        raise ValueError("run lease status is invalid")
    acquired_at = validate_run_lease_time(value.acquired_at, name="run lease acquired time")
    heartbeat_at = validate_run_lease_time(value.heartbeat_at, name="run lease heartbeat time")
    stale_after = validate_run_lease_time(value.stale_after, name="run lease stale time")
    if heartbeat_at < acquired_at or stale_after < heartbeat_at:
        raise ValueError("run lease time ordering is invalid")
    return value


@dataclass(frozen=True, slots=True)
class TurnReceipt:
    turn_id: str
    session_id: str
    operation_id: str
    status: TurnStatus
    current_sequence: int
    replayed: bool


@dataclass(frozen=True, slots=True)
class CapabilityDefinition:
    capability_id: str
    version: int
    mode: str
    requires_approval: bool
    operation_semantics: str
    input_schema_uri: str
    output_schema_uri: str
    tool_definition: ToolDefinition | None = None


@dataclass(frozen=True, slots=True)
class CapabilityManifest:
    manifest_id: str
    turn_id: str
    resolver_id: str
    profile_id: str
    profile_revision: int
    capability_ids: tuple[str, ...]
    excluded_reason_counts: tuple[tuple[str, int], ...]
    descriptor_bytes: int
    # V1 fixtures predate project Boundary binding.  Project-aware resolvers
    # must set both values; the codec retains their absence only for reading
    # historical V1 payloads.
    boundary_profile_id: str | None = None
    boundary_profile_revision: int | None = None
    # Project-scoped Turn initialization may freeze an Application Skill
    # selection before the model context is assembled. Both manifests bind to
    # this opaque, immutable Turn payload when present.
    application_skill_snapshot_ref: str | None = None
    application_skill_snapshot_revision: str | None = None
    model_routing_snapshot_ref: str | None = None
    model_routing_snapshot_revision: str | None = None


@dataclass(frozen=True, slots=True)
class ContextEntry:
    entry_id: str
    kind: str
    source_ref: str | None
    payload_ref: str | None
    source_project_id: str | None
    revision_identity: str | None
    content_fingerprint: str | None
    provenance_refs: tuple[str, ...]
    disclosure: str
    selection_reason: str
    content_bytes: int


@dataclass(frozen=True, slots=True)
class ContextCompaction:
    compaction_id: str
    strategy: str
    source_entry_ids: tuple[str, ...]
    output_entry_id: str
    input_bytes: int
    output_bytes: int


@dataclass(frozen=True, slots=True)
class ContextManifest:
    manifest_id: str
    turn_id: str
    resolver_id: str
    project_id: str | None
    series_id: str | None
    project_profile_id: str
    project_profile_revision: int
    boundary_profile_id: str
    boundary_profile_revision: int
    capability_manifest_ref: str
    entries: tuple[ContextEntry, ...]
    compactions: tuple[ContextCompaction, ...]
    excluded_reason_counts: tuple[tuple[str, int], ...]
    max_context_bytes: int
    selected_context_bytes: int


class CapabilityManifestResolverPort(Protocol):
    def resolve(
        self,
        request: Mapping[str, object],
        capabilities: Sequence[CapabilityDefinition],
    ) -> CapabilityManifest: ...


class ContextManifestResolverPort(Protocol):
    def resolve(
        self,
        request: Mapping[str, object],
        capability_manifest_ref: str,
        capability_manifest: CapabilityManifest,
    ) -> ContextManifest: ...


@dataclass(frozen=True, slots=True)
class ToolExecutionBoundaryDecision:
    outcome: Literal["allow", "allow_redacted", "ask", "deny"]
    reason_codes: tuple[str, ...]
    matched_grant_ids: tuple[str, ...]
    policy_revision: int
    requires_receipt: bool
    redaction_required: bool
    arguments: Mapping[str, object]


class ToolExecutionBoundaryPort(Protocol):
    def evaluate(
        self,
        request: Mapping[str, object],
        capability: CapabilityDefinition,
        decision: Mapping[str, object],
    ) -> ToolExecutionBoundaryDecision: ...


class AIRuntimePort(Protocol):
    """The sole intelligent command surface used by UI and external clients."""

    def submit_turn(self, request: Mapping[str, object]) -> TurnReceipt: ...

    def accept_turn(self, request: Mapping[str, object]) -> TurnReceipt: ...

    def run_accepted_turn(self, turn_id: str, run_lease: RunLeaseToken | None = None) -> TurnReceipt: ...

    def recover_accepted_turn(self, turn_id: str, run_lease: RunLeaseToken) -> TurnReceipt: ...

    def fail_accepted_turn(self, turn_id: str, run_lease: RunLeaseToken | None = None) -> TurnReceipt: ...

    def try_claim_run_lease(self, turn_id: str, owner_id: str) -> int | None: ...

    def release_run_lease(self, turn_id: str, owner_id: str, generation: int) -> None: ...

    def request_background_cancel(self, turn_id: str) -> bool: ...

    def events_after(self, turn_id: str, after_sequence: int = 0) -> Iterable[Mapping[str, object]]: ...

    def receipt_for(self, turn_id: str, *, replayed: bool = False) -> TurnReceipt: ...

    def apply_action(
        self,
        action: Mapping[str, object],
        run_lease: RunLeaseToken | None = None,
    ) -> TurnReceipt: ...

    def presentation_for(self, turn_id: str) -> Mapping[str, object] | None: ...

    def execution_projection_for(
        self,
        turn_id: str,
        view: Literal["simple", "developer"] = "simple",
    ) -> Mapping[str, object]: ...


class CapabilityProviderPort(Protocol):
    """Executes one bounded domain capability without owning the agent loop."""

    def invoke(self, request: Mapping[str, object]) -> Mapping[str, object]: ...


class ToolDispatchObserverPort(Protocol):
    def claimed(self) -> None: ...

    def started(self) -> None: ...

    def fence(self) -> ContextManager[None]: ...


class ToolDispatcherPort(Protocol):
    """The sole host-side entry for invoking a capability provider."""

    def prepare(self, invocation_id: str) -> None: ...

    def abandon_prepared(self, invocation_id: str) -> None: ...

    def dispatch(
        self,
        provider: CapabilityProviderPort,
        request: ToolDispatchRequest,
        observer: ToolDispatchObserverPort,
    ) -> Mapping[str, object]: ...

    def request_cancel(self, invocation_id: str) -> bool: ...


class CapabilityRegistrationPort(Protocol):
    """A reversible registration effect."""

    def close(self) -> None: ...


class CapabilityRegistryPort(Protocol):
    """Registers model-facing capabilities behind stable domain seams."""

    def register(
        self,
        definition: CapabilityDefinition,
        provider: CapabilityProviderPort,
    ) -> CapabilityRegistrationPort: ...

    def get(self, capability_id: str) -> CapabilityDefinition | None: ...

    def list(self) -> Sequence[CapabilityDefinition]: ...

    def resolve(
        self,
        capability_id: str,
    ) -> tuple[CapabilityDefinition, CapabilityProviderPort] | None: ...


class AgentPlannerPort(Protocol):
    """Chooses the next bounded tool call or a final referenced result."""

    def plan(
        self,
        request: Mapping[str, object],
        events: Sequence[Mapping[str, object]],
        capabilities: Sequence[CapabilityDefinition],
        payloads: "TurnPayloadStorePort",
        execution_control: ModelExecutionControlPort | None = None,
    ) -> Mapping[str, object]: ...


class ExpertTurnBindingPort(Protocol):
    """Selects and freezes one project-bound expert without owning Turn state."""

    def select(
        self,
        request: Mapping[str, object],
        capability_manifest: CapabilityManifest,
        capabilities: Sequence[CapabilityDefinition],
    ) -> Mapping[str, object]: ...

    def freeze(
        self,
        request: Mapping[str, object],
        selection_receipt: Mapping[str, object],
        capability_manifest: CapabilityManifest,
        context_manifest: ContextManifest,
        capabilities: Sequence[CapabilityDefinition],
    ) -> Mapping[str, object] | None: ...

    def verify_replay(
        self,
        request: Mapping[str, object],
        selection_receipt: Mapping[str, object],
        snapshot: Mapping[str, object],
        capability_manifest: CapabilityManifest,
        context_manifest: ContextManifest,
        capabilities: Sequence[CapabilityDefinition],
    ) -> None: ...


class TurnEventStorePort(Protocol):
    """Append-only event authority; model-visible state must be reconstructable from it."""

    def append(
        self,
        event: Mapping[str, object],
        *,
        expected_sequence: int,
        run_lease: RunLeaseToken | None = None,
    ) -> Mapping[str, object]: ...

    def events_after(
        self,
        turn_id: str,
        after_sequence: int = 0,
    ) -> Sequence[Mapping[str, object]]: ...


@dataclass(frozen=True, slots=True)
class ImmutablePayloadAppendReceipt:
    """The durable result of one event and its immutable evidence payload."""

    event: Mapping[str, object]
    immutable_payload_ref: str


@dataclass(frozen=True, slots=True)
class IntentBundleReceipt:
    """The durable result of a tool-intent event and its referenced payload."""

    event: Mapping[str, object]
    intent_payload_ref: str


@dataclass(frozen=True, slots=True)
class HookReceiptBundleReceipt:
    """The durable result of one Hook Event and its safe receipt."""

    event: Mapping[str, object]
    hook_receipt_ref: str


@dataclass(frozen=True, slots=True)
class ApprovalBundleReceipt:
    """The durable result of an approval resolution and its two authorities."""

    event: Mapping[str, object]
    action_payload_ref: str
    approval_payload_ref: str


@dataclass(frozen=True, slots=True)
class ToolOutcomeBundleReceipt:
    """The durable result of a Tool outcome, its Event, and optional receipt."""

    event: Mapping[str, object]
    outcome_payload_ref: str
    operation_receipt_ref: str | None
    result_payload_ref: str | None = None


@dataclass(frozen=True, slots=True)
class ModelTerminalBundleReceipt:
    """One model terminal Event and all of its optional metadata receipts."""

    event: Mapping[str, object]
    model_receipt_ref: str | None
    dispatch_authority_receipt_ref: str | None
    prompt_cache_receipt_ref: str | None


@dataclass(frozen=True, slots=True)
class ModelAttemptDispatchBundleReceipt:
    """One committed, metadata-only model wire reservation and dispatch Event."""

    event: Mapping[str, object]
    dispatch_payload_ref: str
    attempt_id: str
    model_request_id: str
    attempt_number: int


@dataclass(frozen=True, slots=True)
class ModelAttemptTerminalBundleReceipt:
    """One terminal model wire receipt, Event, and its closed reservation."""

    event: Mapping[str, object]
    attempt_receipt_ref: str
    attempt_id: str
    model_request_id: str
    attempt_number: int


@runtime_checkable
class AtomicTurnBundlePort(Protocol):
    """Optional same-authority commits used only by durable Turn stores.

    The legacy event, payload and state ports intentionally remain independent
    for existing in-memory fixtures.  A production store may additionally
    expose this narrow port when all records are owned by one transaction.
    """

    def append_event_with_immutable_payload(
        self,
        event: Mapping[str, object],
        *,
        expected_sequence: int,
        immutable_kind: str,
        immutable_payload: object,
        run_lease: RunLeaseToken | None = None,
    ) -> ImmutablePayloadAppendReceipt: ...

    def append_waiting_mcp_continuation_bundle(
        self,
        event: Mapping[str, object],
        *,
        expected_sequence: int,
        immutable_kind: str,
        immutable_payload: object,
        pending: Mapping[str, object],
        run_lease: RunLeaseToken | None = None,
    ) -> ImmutablePayloadAppendReceipt: ...

    def append_intent_bundle(
        self,
        event: Mapping[str, object],
        *,
        expected_sequence: int,
        intent_kind: str,
        intent_payload: object,
        run_lease: RunLeaseToken | None = None,
    ) -> IntentBundleReceipt: ...

    def append_hook_receipt_bundle(
        self,
        event: Mapping[str, object],
        *,
        expected_sequence: int,
        receipt_kind: str,
        receipt_payload: object,
        run_lease: RunLeaseToken | None = None,
    ) -> HookReceiptBundleReceipt: ...

    def append_approval_bundle(
        self,
        event: Mapping[str, object],
        *,
        expected_sequence: int,
        action_kind: str,
        action_payload: object,
        approval_kind: str,
        approval_payload: object,
        run_lease: RunLeaseToken | None = None,
    ) -> ApprovalBundleReceipt: ...

    def append_tool_outcome_bundle(
        self,
        event: Mapping[str, object],
        *,
        expected_sequence: int,
        outcome_kind: str,
        outcome_payload: object,
        result_kind: str | None = None,
        result_payload: object | None = None,
        operation_receipt_kind: str | None = None,
        operation_receipt_payload: object | None = None,
        run_lease: RunLeaseToken | None = None,
    ) -> ToolOutcomeBundleReceipt: ...

    def append_model_terminal_bundle(
        self,
        event: Mapping[str, object],
        *,
        expected_sequence: int,
        model_receipt_payload: object | None = None,
        dispatch_authority_receipt_payload: object | None = None,
        prompt_cache_receipt_payload: object | None = None,
        run_lease: RunLeaseToken | None = None,
    ) -> ModelTerminalBundleReceipt: ...

    def commit_model_attempt_dispatch_bundle(
        self,
        event: Mapping[str, object],
        *,
        expected_sequence: int,
        dispatch_payload: object,
        run_lease: RunLeaseToken | None = None,
    ) -> ModelAttemptDispatchBundleReceipt: ...

    def append_model_attempt_terminal_bundle(
        self,
        event: Mapping[str, object],
        *,
        expected_sequence: int,
        attempt_receipt_payload: object,
        run_lease: RunLeaseToken | None = None,
    ) -> ModelAttemptTerminalBundleReceipt: ...


class TurnPayloadStorePort(Protocol):
    """Stores governed model-visible payloads addressed by opaque crp references."""

    def put(self, turn_id: str, kind: str, payload: object) -> str: ...

    def get(self, payload_ref: str) -> object: ...

    def get_or_create_immutable_payload(
        self, turn_id: str, kind: str, payload: object,
    ) -> str: ...

    def reserve_immutable_payload(
        self, turn_id: str, kind: str, payload: object,
    ) -> tuple[str, bool]: ...

    def get_immutable_payload(self, turn_id: str, kind: str) -> tuple[str, object] | None: ...


class TurnStateStorePort(Protocol):
    """Durable Turn request, idempotency, approval and action authority."""

    def claim_turn(self, request: Mapping[str, object]) -> tuple[str, bool]: ...

    def get_request(self, turn_id: str) -> Mapping[str, object] | None: ...

    def try_claim_run_lease(self, turn_id: str, owner_id: str) -> int | None: ...

    def release_run_lease(self, turn_id: str, owner_id: str, generation: int) -> None: ...

    def try_acquire_run_lease(self, turn_id: str, owner_id: str, *, now: datetime, stale_after: datetime) -> RunLeaseToken | None: ...

    def renew_run_lease(self, token: RunLeaseToken, *, now: datetime, stale_after: datetime) -> RunLeaseRecord | None: ...

    def mark_run_lease_stale(self, token: RunLeaseToken, *, now: datetime) -> RunLeaseRecord | None: ...

    def takeover_run_lease(self, turn_id: str, *, expected_generation: int, owner_id: str, now: datetime, stale_after: datetime, disposition: RunLeaseRecoveryDisposition) -> RunLeaseRecord | None: ...

    def assert_active_run_lease(self, token: RunLeaseToken) -> RunLeaseRecord | None: ...

    def get_run_lease(self, turn_id: str) -> RunLeaseRecord | None: ...

    def claim_due_run_leases(self, *, now: datetime, limit: int) -> Sequence[RunLeaseRecord]: ...

    def record_recovery_decision(self, decision: RecoveryDecision, *, observed_at: datetime) -> bool: ...

    def claim_safe_recovery_queue(self, *, now: datetime, stale_after: datetime, limit: int) -> Sequence[RecoveryQueueItem]: ...

    def has_pending_safe_recovery(self) -> bool: ...

    def complete_recovery_queue(
        self,
        item: RecoveryQueueItem,
        *,
        observed_at: datetime,
        result_status: TurnStatus = "completed",
    ) -> bool: ...

    def quarantine_recovery_queue(self, item: RecoveryQueueItem, *, reason_code: str, observed_at: datetime) -> bool: ...

    def list_recovery_reviews(
        self, *, project_id: str | None = None, limit: int = 128,
    ) -> Sequence[PublicRecoveryReview]: ...

    def get_recovery_review(self, review_id: str) -> PublicRecoveryReview | None: ...

    def get_recovery_review_request(self, review_id: str) -> Mapping[str, object] | None: ...

    def keep_recovery_review(
        self,
        review_id: str,
        *,
        expected_revision: int,
        authorization: RecoveryReviewAuthorization,
        observed_at: datetime,
    ) -> PublicRecoveryReview | None: ...

    def confirm_no_effect_and_queue_recovery_review(
        self,
        review_id: str,
        *,
        expected_revision: int,
        authorization: RecoveryReviewAuthorization,
        observed_at: datetime,
    ) -> PublicRecoveryReview | None: ...

    def release_strict_run_lease(self, token: RunLeaseToken) -> None: ...

    def put_pending(self, turn_id: str, decision: Mapping[str, object]) -> None: ...

    def get_pending(self, turn_id: str) -> Mapping[str, object] | None: ...

    def clear_pending(self, turn_id: str) -> None: ...

    def get_action(self, idempotency_key: str) -> tuple[Mapping[str, object], TurnReceipt] | None: ...

    def save_action(self, action: Mapping[str, object], receipt: TurnReceipt) -> None: ...
