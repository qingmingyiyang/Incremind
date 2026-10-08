"""Immutable, provider-free dispatch planning contracts.

These records deliberately carry only opaque authority references.  They are
not prompts, model-routing inputs, provider configuration, or a second Turn
authority.  ``AgentDispatchPlan`` is a versioned intent that the coordinator
may compare-and-swap; a permit is consumed once before a later stage creates a
child Turn through the existing governed runtime.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import re

from .agent_contracts import AgentBudget, AgentContractError, agent_budget_from_payload, agent_budget_to_payload


AGENT_DISPATCH_SCHEMA_VERSION = "1.1.0"
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_REF = re.compile(r"^crp://[A-Za-z0-9._~-]{1,64}/[A-Za-z0-9._~/-]{1,384}$")
_MAX = 2_147_483_647
_SENSITIVE = frozenset({"prompt", "provider", "provider_id", "model", "model_name", "endpoint", "secret", "token", "api_key"})
_PLAN_STATES = frozenset({"draft", "ready", "dispatching", "dispatched", "completed", "failed", "cancelled", "superseded"})
_PERMIT_STATES = frozenset({"issued", "consumed", "revoked"})
_EFFECT_STATES = frozenset({"none", "known", "unknown"})
_PLAN_MODES = frozenset({"main_only", "cluster"})
_DISPATCH_REF_KINDS = frozenset({"intake", "workload", "capacity", "cluster"})
_INTAKE_ROUTES = frozenset({"steward_required", "main_direct"})


class AgentDispatchContractError(AgentContractError):
    """Raised when a dispatch-only coordination value is malformed."""


def canonical_dispatch_ref(kind: str, identity: str) -> str:
    """Return the sole local-reference format accepted by the dispatch store."""
    if kind not in _DISPATCH_REF_KINDS:
        raise AgentDispatchContractError("dispatch reference kind is invalid")
    _id(identity, "dispatch reference identity")
    return f"crp://dispatch/{kind}/{identity}"


def parse_canonical_dispatch_ref(value: object, expected_kind: str) -> str:
    if expected_kind not in _DISPATCH_REF_KINDS or not isinstance(value, str):
        raise AgentDispatchContractError("dispatch reference is invalid")
    prefix = f"crp://dispatch/{expected_kind}/"
    if not value.startswith(prefix):
        raise AgentDispatchContractError("dispatch reference kind is invalid")
    identity = value.removeprefix(prefix)
    _id(identity, "dispatch reference identity")
    if value != canonical_dispatch_ref(expected_kind, identity):
        raise AgentDispatchContractError("dispatch reference is noncanonical")
    return identity


@dataclass(frozen=True, slots=True)
class IntakeRoutingReceipt:
    receipt_id: str
    project_id: str
    turn_id: str
    revision: int
    route: str
    input_ref: str
    routing_snapshot_ref: str
    context_policy_ref: str
    context_policy_revision: int

    def __post_init__(self) -> None:
        _id(self.receipt_id, "receipt_id"); _id(self.project_id, "project_id"); _id(self.turn_id, "turn_id")
        _positive(self.revision, "receipt revision")
        if self.route not in _INTAKE_ROUTES: raise AgentDispatchContractError("intake route is invalid")
        _ref(self.input_ref, "input_ref"); _ref(self.routing_snapshot_ref, "routing_snapshot_ref"); _ref(self.context_policy_ref, "context_policy_ref"); _positive(self.context_policy_revision, "context_policy_revision")


@dataclass(frozen=True, slots=True)
class WorkloadSnapshot:
    snapshot_id: str
    project_id: str
    revision: int
    queued_assignments: int
    active_assignments: int
    reserved_budget: AgentBudget

    def __post_init__(self) -> None:
        _id(self.snapshot_id, "workload snapshot_id"); _id(self.project_id, "workload project_id"); _positive(self.revision, "workload revision")
        _count(self.queued_assignments, "queued_assignments"); _count(self.active_assignments, "active_assignments")


@dataclass(frozen=True, slots=True)
class CapacitySnapshot:
    snapshot_id: str
    project_id: str
    revision: int
    available_slots: int
    maximum_slots: int
    remaining_budget: AgentBudget

    def __post_init__(self) -> None:
        _id(self.snapshot_id, "capacity snapshot_id"); _id(self.project_id, "capacity project_id"); _positive(self.revision, "capacity revision")
        _count(self.available_slots, "available_slots"); _count(self.maximum_slots, "maximum_slots")
        if self.available_slots > self.maximum_slots:
            raise AgentDispatchContractError("available slots exceed maximum slots")


@dataclass(frozen=True, slots=True)
class ExpertCluster:
    cluster_id: str
    project_id: str
    revision: int
    expert_snapshot_refs: tuple[str, ...]
    skill_snapshot_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        _id(self.cluster_id, "cluster_id"); _id(self.project_id, "cluster project_id"); _positive(self.revision, "cluster revision")
        _refs(self.expert_snapshot_refs, "expert_snapshot_refs"); _refs(self.skill_snapshot_refs, "skill_snapshot_refs")


@dataclass(frozen=True, slots=True)
class ExpertAssignment:
    assignment_id: str
    project_id: str
    cluster_id: str
    cluster_revision: int
    profile_ref: str
    profile_revision: int
    task_payload_ref: str
    task_payload_revision: int
    capability_ids: tuple[str, ...]
    expert_id: str | None
    skill_ids: tuple[str, ...]
    expert_snapshot_ref: str | None
    expert_revision: int | None
    skill_snapshot_ref: str | None
    skill_revision: int | None
    context_policy_ref: str
    context_policy_revision: int
    delegated_budget: AgentBudget

    def __post_init__(self) -> None:
        for label, value in (("assignment_id", self.assignment_id), ("assignment project_id", self.project_id), ("cluster_id", self.cluster_id)):
            _id(value, label)
        for label, value in (("cluster_revision", self.cluster_revision), ("profile_revision", self.profile_revision), ("task_payload_revision", self.task_payload_revision), ("context_policy_revision", self.context_policy_revision)):
            _positive(value, label)
        for label, value in (("profile_ref", self.profile_ref), ("task_payload_ref", self.task_payload_ref), ("context_policy_ref", self.context_policy_ref)):
            _ref(value, label)
        _ids(self.capability_ids, "assignment capability_ids")
        if self.expert_id is not None:
            _id(self.expert_id, "assignment expert_id")
        _ids(self.skill_ids, "assignment skill_ids")
        _optional_ref_revision(self.expert_snapshot_ref, self.expert_revision, "expert")
        _optional_ref_revision(self.skill_snapshot_ref, self.skill_revision, "skill")


@dataclass(frozen=True, slots=True)
class AgentDispatchPlan:
    plan_id: str
    project_id: str
    main_run_id: str
    steward_run_id: str
    revision: int
    status: str
    mode: str
    intake_receipt_ref: str
    intake_revision: int
    workload_snapshot_ref: str
    workload_revision: int
    capacity_snapshot_ref: str
    capacity_revision: int
    expert_cluster_ref: str | None
    expert_cluster_revision: int | None
    assignment_ids: tuple[str, ...]
    budget_limit: AgentBudget
    max_concurrent_assignments: int
    effect_state: str = "none"

    def __post_init__(self) -> None:
        _id(self.plan_id, "plan_id"); _id(self.project_id, "plan project_id"); _id(self.main_run_id, "plan main_run_id"); _id(self.steward_run_id, "plan steward_run_id"); _positive(self.revision, "plan revision")
        if self.main_run_id == self.steward_run_id: raise AgentDispatchContractError("plan Run identities must be distinct")
        if self.status not in _PLAN_STATES: raise AgentDispatchContractError("plan status is invalid")
        if self.mode not in _PLAN_MODES: raise AgentDispatchContractError("plan mode is invalid")
        if self.effect_state not in _EFFECT_STATES: raise AgentDispatchContractError("plan effect state is invalid")
        for label, value in (("intake_revision", self.intake_revision), ("workload_revision", self.workload_revision), ("capacity_revision", self.capacity_revision)):
            _positive(value, label)
        for label, value in (("intake_receipt_ref", self.intake_receipt_ref), ("workload_snapshot_ref", self.workload_snapshot_ref), ("capacity_snapshot_ref", self.capacity_snapshot_ref)):
            _ref(value, label)
        if not isinstance(self.assignment_ids, tuple) or len(self.assignment_ids) != len(set(self.assignment_ids)):
            raise AgentDispatchContractError("plan assignment ids are invalid")
        for assignment_id in self.assignment_ids: _id(assignment_id, "plan assignment id")
        _count(self.max_concurrent_assignments, "max_concurrent_assignments")
        if self.mode == "main_only":
            if self.expert_cluster_ref is not None or self.expert_cluster_revision is not None or self.assignment_ids or self.max_concurrent_assignments != 0:
                raise AgentDispatchContractError("main-only plan cannot carry a cluster assignment")
        elif self.expert_cluster_ref is None or self.expert_cluster_revision is None or not self.assignment_ids or self.max_concurrent_assignments == 0 or self.max_concurrent_assignments > len(self.assignment_ids):
            raise AgentDispatchContractError("plan concurrency is invalid")
        if self.expert_cluster_ref is not None:
            _ref(self.expert_cluster_ref, "expert_cluster_ref")
        if self.expert_cluster_revision is not None:
            _positive(self.expert_cluster_revision, "expert_cluster_revision")

    @property
    def may_reschedule(self) -> bool:
        return self.effect_state == "none" and self.status in {"draft", "ready", "failed", "cancelled"}


@dataclass(frozen=True, slots=True)
class DispatchPermit:
    permit_id: str
    project_id: str
    plan_id: str
    plan_revision: int
    assignment_id: str
    operation_id: str
    status: str = "issued"

    def __post_init__(self) -> None:
        for label, value in (("permit_id", self.permit_id), ("permit project_id", self.project_id), ("plan_id", self.plan_id), ("assignment_id", self.assignment_id), ("permit operation_id", self.operation_id)):
            _id(value, label)
        _positive(self.plan_revision, "permit plan revision")
        if self.status not in _PERMIT_STATES: raise AgentDispatchContractError("permit status is invalid")


def intake_routing_receipt_to_payload(value: IntakeRoutingReceipt) -> dict[str, object]:
    return _payload(value)


def intake_routing_receipt_from_payload(value: object) -> IntakeRoutingReceipt:
    return _decode(value, IntakeRoutingReceipt, {"receipt_id", "project_id", "turn_id", "revision", "route", "input_ref", "routing_snapshot_ref", "context_policy_ref", "context_policy_revision"})


def workload_snapshot_to_payload(value: WorkloadSnapshot) -> dict[str, object]: return _payload(value)
def workload_snapshot_from_payload(value: object) -> WorkloadSnapshot: return _decode(value, WorkloadSnapshot, {"snapshot_id", "project_id", "revision", "queued_assignments", "active_assignments", "reserved_budget"})
def capacity_snapshot_to_payload(value: CapacitySnapshot) -> dict[str, object]: return _payload(value)
def capacity_snapshot_from_payload(value: object) -> CapacitySnapshot: return _decode(value, CapacitySnapshot, {"snapshot_id", "project_id", "revision", "available_slots", "maximum_slots", "remaining_budget"})
def expert_cluster_to_payload(value: ExpertCluster) -> dict[str, object]: return _payload(value)
def expert_cluster_from_payload(value: object) -> ExpertCluster: return _decode(value, ExpertCluster, {"cluster_id", "project_id", "revision", "expert_snapshot_refs", "skill_snapshot_refs"})
def expert_assignment_to_payload(value: ExpertAssignment) -> dict[str, object]: return _payload(value)
def expert_assignment_from_payload(value: object) -> ExpertAssignment: return _decode(value, ExpertAssignment, {"assignment_id", "project_id", "cluster_id", "cluster_revision", "profile_ref", "profile_revision", "task_payload_ref", "task_payload_revision", "capability_ids", "expert_id", "skill_ids", "expert_snapshot_ref", "expert_revision", "skill_snapshot_ref", "skill_revision", "context_policy_ref", "context_policy_revision", "delegated_budget"})
def agent_dispatch_plan_to_payload(value: AgentDispatchPlan) -> dict[str, object]: return _payload(value)
def agent_dispatch_plan_from_payload(value: object) -> AgentDispatchPlan: return _decode(value, AgentDispatchPlan, {"plan_id", "project_id", "main_run_id", "steward_run_id", "revision", "status", "mode", "intake_receipt_ref", "intake_revision", "workload_snapshot_ref", "workload_revision", "capacity_snapshot_ref", "capacity_revision", "expert_cluster_ref", "expert_cluster_revision", "assignment_ids", "budget_limit", "max_concurrent_assignments", "effect_state"})
def dispatch_permit_to_payload(value: DispatchPermit) -> dict[str, object]: return _payload(value)
def dispatch_permit_from_payload(value: object) -> DispatchPermit: return _decode(value, DispatchPermit, {"permit_id", "project_id", "plan_id", "plan_revision", "assignment_id", "operation_id", "status"})


def _payload(value: object) -> dict[str, object]:
    result = {"schema_version": AGENT_DISPATCH_SCHEMA_VERSION}
    for name in value.__dataclass_fields__:  # type: ignore[attr-defined]
        item = getattr(value, name)
        result[name] = agent_budget_to_payload(item) if isinstance(item, AgentBudget) else list(item) if isinstance(item, tuple) else item
    return result


def _decode(value: object, kind: object, fields: set[str]) -> object:
    if not isinstance(value, Mapping) or any(not isinstance(k, str) for k in value): raise AgentDispatchContractError("dispatch payload must be a string-keyed mapping")
    _reject_sensitive(value)
    required = fields | {"schema_version"}
    if set(value) != required: raise AgentDispatchContractError("dispatch payload shape is invalid")
    if value["schema_version"] != AGENT_DISPATCH_SCHEMA_VERSION: raise AgentDispatchContractError("dispatch schema version is unsupported")
    payload = dict(value); payload.pop("schema_version")
    for name in ("reserved_budget", "remaining_budget", "delegated_budget", "budget_limit"):
        if name in payload: payload[name] = agent_budget_from_payload(payload[name])
    for name in ("expert_snapshot_refs", "skill_snapshot_refs", "assignment_ids", "capability_ids", "skill_ids"):
        if name in payload and isinstance(payload[name], list): payload[name] = tuple(payload[name])
    try: return kind(**payload)  # type: ignore[operator]
    except TypeError as error: raise AgentDispatchContractError("dispatch payload is invalid") from error


def _id(value: object, label: str) -> None:
    if not isinstance(value, str) or not _ID.fullmatch(value): raise AgentDispatchContractError(f"{label} is invalid")
def _ref(value: object, label: str) -> None:
    if not isinstance(value, str) or not _REF.fullmatch(value): raise AgentDispatchContractError(f"{label} is invalid")
def _positive(value: object, label: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1: raise AgentDispatchContractError(f"{label} is invalid")
def _count(value: object, label: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= _MAX: raise AgentDispatchContractError(f"{label} is invalid")
def _refs(values: tuple[str, ...], label: str) -> None:
    if not isinstance(values, tuple) or len(values) != len(set(values)): raise AgentDispatchContractError(f"{label} are invalid")
    for value in values: _ref(value, label)
def _ids(values: tuple[str, ...], label: str) -> None:
    if not isinstance(values, tuple) or len(values) != len(set(values)): raise AgentDispatchContractError(f"{label} are invalid")
    for value in values: _id(value, label)
def _optional_ref_revision(reference: str | None, revision: int | None, label: str) -> None:
    if (reference is None) != (revision is None): raise AgentDispatchContractError(f"{label} reference and revision must be paired")
    if reference is not None:
        _ref(reference, f"{label}_snapshot_ref"); _positive(revision, f"{label}_revision")
def _reject_sensitive(value: object) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if normalized in _SENSITIVE or normalized.endswith("_secret") or normalized.endswith("_token"): raise AgentDispatchContractError(f"sensitive field is not allowed: {key}")
            _reject_sensitive(nested)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for nested in value: _reject_sensitive(nested)
