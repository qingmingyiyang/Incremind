"""Pure immutable trajectory and checkpoint contracts for long-running graphs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
import re
from uuid import NAMESPACE_URL, uuid5

from .task_graph import TaskBudget
from core.ai_kernel.agent_contracts import AgentBudget, agent_budget_from_payload, agent_budget_to_payload


TRAJECTORY_SCHEMA_VERSION = "1.0.0"
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_REF = re.compile(r"^crp://[A-Za-z0-9][A-Za-z0-9._~:/?#%+=@-]{1,511}$")
_TIER = frozenset({"fast", "standard", "deep"})
_VALIDITY = frozenset({"pending", "verified", "stale", "invalidated"})


class TrajectoryContractError(ValueError):
    pass


def trajectory_checkpoint_world_identity(checkpoint: "TrustCheckpoint") -> tuple[str, str, str]:
    """Return the canonical system-owned World envelope for a checkpoint."""

    if not isinstance(checkpoint, TrustCheckpoint):
        raise TrajectoryContractError("trust checkpoint is invalid")
    canonical = json.dumps(
        checkpoint.to_payload(), ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    event_id = f"trajectory-checkpoint-{uuid5(NAMESPACE_URL, canonical).hex}"
    return (
        event_id,
        f"crp://world-trajectory/{checkpoint.project_id}/{checkpoint.graph_id}/{event_id}",
        "trajectory-v1",
    )


@dataclass(frozen=True, slots=True)
class NodeBudget:
    node_id: str
    allocated: TaskBudget
    remaining: TaskBudget

    def __post_init__(self) -> None:
        _id(self.node_id, "node id")
        if self.remaining.units > self.allocated.units:
            raise TrajectoryContractError("node remaining budget exceeds allocation")

    def to_payload(self) -> dict[str, object]:
        return {"node_id": self.node_id, "allocated": self.allocated.to_payload(), "remaining": self.remaining.to_payload()}

    @classmethod
    def from_payload(cls, value: object) -> "NodeBudget":
        item = _mapping(value, {"node_id", "allocated", "remaining"}, "node budget")
        return cls(str(item["node_id"]), TaskBudget.from_payload(item["allocated"]), TaskBudget.from_payload(item["remaining"]))


@dataclass(frozen=True, slots=True)
class PendingExecution:
    node_id: str
    kind: str
    authority_ref: str

    def __post_init__(self) -> None:
        _id(self.node_id, "pending node id")
        if self.kind not in {"agent", "job", "effect"}:
            raise TrajectoryContractError("pending execution kind is invalid")
        if not isinstance(self.authority_ref, str) or _REF.fullmatch(self.authority_ref) is None:
            raise TrajectoryContractError("pending execution authority is invalid")

    def to_payload(self) -> dict[str, object]:
        return {"node_id": self.node_id, "kind": self.kind, "authority_ref": self.authority_ref}

    @classmethod
    def from_payload(cls, value: object) -> "PendingExecution":
        item = _mapping(value, {"node_id", "kind", "authority_ref"}, "pending execution")
        return cls(str(item["node_id"]), str(item["kind"]), str(item["authority_ref"]))


@dataclass(frozen=True, slots=True)
class TrajectorySegment:
    project_id: str
    graph_id: str
    segment_id: str
    revision: int
    previous_segment_id: str | None
    basis_world_sequence: int
    basis_graph_sequence: int
    provenance_sequence: int
    route_revision: int
    route_ref: str
    profile_id: str
    profile_revision: int
    model_tier: str
    capability_ids: tuple[str, ...]
    node_budgets: tuple[NodeBudget, ...]
    verified_artifact_refs: tuple[str, ...]
    pending_executions: tuple[PendingExecution, ...]
    reason: str
    evidence_ref: str
    authorization_ref: str | None = None

    def __post_init__(self) -> None:
        for label, value in (("project id", self.project_id), ("graph id", self.graph_id), ("segment id", self.segment_id)):
            _id(value, label)
        _id(self.profile_id, "profile id")
        if self.previous_segment_id is not None: _id(self.previous_segment_id, "previous segment")
        for label, value in (("segment revision", self.revision), ("world sequence", self.basis_world_sequence), ("graph sequence", self.basis_graph_sequence), ("provenance sequence", self.provenance_sequence), ("route revision", self.route_revision), ("profile revision", self.profile_revision)):
            if not isinstance(value, int) or isinstance(value, bool) or value < 1: raise TrajectoryContractError(f"{label} is invalid")
        if self.model_tier not in _TIER: raise TrajectoryContractError("model tier is invalid")
        _ref(self.route_ref, self.project_id, "route")
        _ids(self.capability_ids, "capability ids")
        if not self.node_budgets or len({item.node_id for item in self.node_budgets}) != len(self.node_budgets): raise TrajectoryContractError("node budgets are invalid")
        _refs(self.verified_artifact_refs, self.project_id, "verified artifacts")
        if len({item.node_id for item in self.pending_executions}) != len(self.pending_executions): raise TrajectoryContractError("pending execution nodes are duplicated")
        for item in self.pending_executions: _ref(item.authority_ref, self.project_id, "pending execution")
        _ref(self.evidence_ref, self.project_id, "evidence")
        if self.authorization_ref is not None: _ref(self.authorization_ref, self.project_id, "authorization")
        if not isinstance(self.reason, str) or not self.reason.strip() or len(self.reason) > 512: raise TrajectoryContractError("segment reason is invalid")

    @property
    def allocated_budget(self) -> int: return sum(item.allocated.units for item in self.node_budgets)
    @property
    def remaining_budget(self) -> int: return sum(item.remaining.units for item in self.node_budgets)

    def to_payload(self) -> dict[str, object]:
        return {"schema_version": TRAJECTORY_SCHEMA_VERSION, "project_id": self.project_id, "graph_id": self.graph_id, "segment_id": self.segment_id, "revision": self.revision, "previous_segment_id": self.previous_segment_id, "basis_world_sequence": self.basis_world_sequence, "basis_graph_sequence": self.basis_graph_sequence, "provenance_sequence": self.provenance_sequence, "route_revision": self.route_revision, "route_ref": self.route_ref, "profile_id": self.profile_id, "profile_revision": self.profile_revision, "model_tier": self.model_tier, "capability_ids": list(self.capability_ids), "node_budgets": [item.to_payload() for item in self.node_budgets], "verified_artifact_refs": list(self.verified_artifact_refs), "pending_executions": [item.to_payload() for item in self.pending_executions], "reason": self.reason, "evidence_ref": self.evidence_ref, "authorization_ref": self.authorization_ref}

    @classmethod
    def from_payload(cls, value: object) -> "TrajectorySegment":
        fields = {"project_id", "graph_id", "segment_id", "revision", "previous_segment_id", "basis_world_sequence", "basis_graph_sequence", "provenance_sequence", "route_revision", "route_ref", "profile_id", "profile_revision", "model_tier", "capability_ids", "node_budgets", "verified_artifact_refs", "pending_executions", "reason", "evidence_ref", "authorization_ref"}
        item = _mapping(value, fields | {"schema_version"}, "trajectory segment")
        if item["schema_version"] != TRAJECTORY_SCHEMA_VERSION: raise TrajectoryContractError("trajectory schema is invalid")
        authorization = item["authorization_ref"]
        if authorization is not None and not isinstance(authorization, str): raise TrajectoryContractError("authorization reference is invalid")
        return cls(str(item["project_id"]), str(item["graph_id"]), str(item["segment_id"]), item["revision"], item["previous_segment_id"], item["basis_world_sequence"], item["basis_graph_sequence"], item["provenance_sequence"], item["route_revision"], str(item["route_ref"]), str(item["profile_id"]), item["profile_revision"], str(item["model_tier"]), _strings(item["capability_ids"], "capability ids"), _node_budgets(item["node_budgets"]), _strings(item["verified_artifact_refs"], "verified artifacts"), _pending(item["pending_executions"]), str(item["reason"]), str(item["evidence_ref"]), authorization)  # type: ignore[arg-type]


def validate_segment_transition(previous: TrajectorySegment, current: TrajectorySegment) -> TrajectorySegment:
    if (previous.project_id, previous.graph_id) != (current.project_id, current.graph_id) or current.previous_segment_id != previous.segment_id or current.revision != previous.revision + 1:
        raise TrajectoryContractError("trajectory segment lineage drifted")
    if not set(current.capability_ids).issubset(previous.capability_ids) and current.authorization_ref is None:
        raise TrajectoryContractError("trajectory capability expansion requires authorization")
    if any(getattr(current, name) < getattr(previous, name) for name in ("basis_world_sequence", "basis_graph_sequence", "provenance_sequence")):
        raise TrajectoryContractError("trajectory cursor regressed")
    if current.route_revision < previous.route_revision:
        raise TrajectoryContractError("trajectory route revision regressed")
    if current.profile_id == previous.profile_id and current.profile_revision < previous.profile_revision:
        raise TrajectoryContractError("trajectory profile revision regressed")
    if previous.allocated_budget != current.allocated_budget or current.remaining_budget > previous.remaining_budget:
        raise TrajectoryContractError("trajectory budget is not conserved")
    return current


@dataclass(frozen=True, slots=True)
class TrustCheckpoint:
    checkpoint_id: str
    project_id: str
    graph_id: str
    checkpoint_revision: int
    segment: TrajectorySegment
    dag_cursor: int
    world_cursor: int
    provenance_cursor: int
    verified_node_ids: tuple[str, ...]
    verified_artifact_refs: tuple[str, ...]
    frozen_capability_ids: tuple[str, ...]
    frozen_remaining_budget: TaskBudget
    pending_executions: tuple[PendingExecution, ...]
    workload_snapshot_ref: str
    workload_snapshot_revision: int
    capacity_snapshot_ref: str
    capacity_snapshot_revision: int
    main_run_id: str
    main_cancel_epoch: int
    frozen_agent_budget: AgentBudget
    frozen_workload_queued: int
    frozen_workload_active: int
    frozen_workload_reserved_budget: AgentBudget
    frozen_capacity_available: int
    frozen_capacity_maximum: int

    def __post_init__(self) -> None:
        for label, value in (("checkpoint id", self.checkpoint_id), ("project id", self.project_id), ("graph id", self.graph_id)):
            _id(value, label)
        if not isinstance(self.checkpoint_revision, int) or isinstance(self.checkpoint_revision, bool) or self.checkpoint_revision < 1: raise TrajectoryContractError("checkpoint revision is invalid")
        if (self.segment.project_id, self.segment.graph_id) != (self.project_id, self.graph_id): raise TrajectoryContractError("checkpoint segment scope drifted")
        if self.dag_cursor != self.segment.basis_graph_sequence or self.world_cursor != self.segment.basis_world_sequence or self.provenance_cursor != self.segment.provenance_sequence: raise TrajectoryContractError("checkpoint cursor drifted")
        _ids(self.verified_node_ids, "verified node ids"); _refs(self.verified_artifact_refs, self.project_id, "verified artifacts"); _ids(self.frozen_capability_ids, "frozen capability ids")
        if set(self.frozen_capability_ids) != set(self.segment.capability_ids): raise TrajectoryContractError("checkpoint frozen capability drifted")
        if self.frozen_remaining_budget.units != self.segment.remaining_budget: raise TrajectoryContractError("checkpoint budget drifted")
        if len({item.node_id for item in self.pending_executions}) != len(self.pending_executions): raise TrajectoryContractError("checkpoint pending nodes are duplicated")
        for item in self.pending_executions: _ref(item.authority_ref, self.project_id, "pending execution")
        _dispatch_ref(self.workload_snapshot_ref, "workload")
        _dispatch_ref(self.capacity_snapshot_ref, "capacity")
        _id(self.main_run_id, "main run id")
        if not isinstance(self.workload_snapshot_revision, int) or self.workload_snapshot_revision < 1 or not isinstance(self.capacity_snapshot_revision, int) or self.capacity_snapshot_revision < 1 or not isinstance(self.main_cancel_epoch, int) or self.main_cancel_epoch < 0 or not isinstance(self.frozen_agent_budget, AgentBudget): raise TrajectoryContractError("checkpoint dispatch budget is invalid")
        for label, value in (
            ("workload queued", self.frozen_workload_queued),
            ("workload active", self.frozen_workload_active),
            ("capacity available", self.frozen_capacity_available),
            ("capacity maximum", self.frozen_capacity_maximum),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise TrajectoryContractError(f"checkpoint {label} is invalid")
        if self.frozen_capacity_available > self.frozen_capacity_maximum:
            raise TrajectoryContractError("checkpoint capacity exceeds maximum")
        if not isinstance(self.frozen_workload_reserved_budget, AgentBudget):
            raise TrajectoryContractError("checkpoint workload budget is invalid")

    def to_payload(self) -> dict[str, object]:
        return {"schema_version": TRAJECTORY_SCHEMA_VERSION, "checkpoint_id": self.checkpoint_id, "project_id": self.project_id, "graph_id": self.graph_id, "checkpoint_revision": self.checkpoint_revision, "segment": self.segment.to_payload(), "dag_cursor": self.dag_cursor, "world_cursor": self.world_cursor, "provenance_cursor": self.provenance_cursor, "verified_node_ids": list(self.verified_node_ids), "verified_artifact_refs": list(self.verified_artifact_refs), "frozen_capability_ids": list(self.frozen_capability_ids), "frozen_remaining_budget": self.frozen_remaining_budget.to_payload(), "pending_executions": [item.to_payload() for item in self.pending_executions], "workload_snapshot_ref": self.workload_snapshot_ref, "workload_snapshot_revision": self.workload_snapshot_revision, "capacity_snapshot_ref": self.capacity_snapshot_ref, "capacity_snapshot_revision": self.capacity_snapshot_revision, "main_run_id": self.main_run_id, "main_cancel_epoch": self.main_cancel_epoch, "frozen_agent_budget": agent_budget_to_payload(self.frozen_agent_budget), "frozen_workload_queued": self.frozen_workload_queued, "frozen_workload_active": self.frozen_workload_active, "frozen_workload_reserved_budget": agent_budget_to_payload(self.frozen_workload_reserved_budget), "frozen_capacity_available": self.frozen_capacity_available, "frozen_capacity_maximum": self.frozen_capacity_maximum}

    @classmethod
    def from_payload(cls, value: object) -> "TrustCheckpoint":
        fields = {"checkpoint_id", "project_id", "graph_id", "checkpoint_revision", "segment", "dag_cursor", "world_cursor", "provenance_cursor", "verified_node_ids", "verified_artifact_refs", "frozen_capability_ids", "frozen_remaining_budget", "pending_executions", "workload_snapshot_ref", "workload_snapshot_revision", "capacity_snapshot_ref", "capacity_snapshot_revision", "main_run_id", "main_cancel_epoch", "frozen_agent_budget", "frozen_workload_queued", "frozen_workload_active", "frozen_workload_reserved_budget", "frozen_capacity_available", "frozen_capacity_maximum"}
        item = _mapping(value, fields | {"schema_version"}, "trust checkpoint")
        if item["schema_version"] != TRAJECTORY_SCHEMA_VERSION: raise TrajectoryContractError("trajectory schema is invalid")
        frozen = item["frozen_agent_budget"]
        if frozen is None:
            raise TrajectoryContractError("checkpoint Agent budget is unavailable")
        return cls(str(item["checkpoint_id"]), str(item["project_id"]), str(item["graph_id"]), item["checkpoint_revision"], TrajectorySegment.from_payload(item["segment"]), item["dag_cursor"], item["world_cursor"], item["provenance_cursor"], _strings(item["verified_node_ids"], "verified node ids"), _strings(item["verified_artifact_refs"], "verified artifacts"), _strings(item["frozen_capability_ids"], "frozen capability ids"), TaskBudget.from_payload(item["frozen_remaining_budget"]), _pending(item["pending_executions"]), item["workload_snapshot_ref"], item["workload_snapshot_revision"], item["capacity_snapshot_ref"], item["capacity_snapshot_revision"], item["main_run_id"], item["main_cancel_epoch"], agent_budget_from_payload(frozen), item["frozen_workload_queued"], item["frozen_workload_active"], agent_budget_from_payload(item["frozen_workload_reserved_budget"]), item["frozen_capacity_available"], item["frozen_capacity_maximum"])  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class RecoveryProjection:
    skip_verified: tuple[str, ...]
    resume_pending: tuple[str, ...]
    redispatch_ready: tuple[str, ...]


def project_checkpoint_recovery(checkpoint: TrustCheckpoint, *, world_cursor: int, dag_cursor: int, provenance_cursor: int, node_validity: Mapping[str, str]) -> RecoveryProjection:
    if world_cursor != checkpoint.world_cursor or dag_cursor != checkpoint.dag_cursor or provenance_cursor != checkpoint.provenance_cursor: raise TrajectoryContractError("checkpoint cursor drifted")
    if any(status not in _VALIDITY for status in node_validity.values()) or any(status in {"stale", "invalidated"} for status in node_validity.values()): raise TrajectoryContractError("checkpoint provenance is stale or invalidated")
    if any(node_validity.get(node_id) != "verified" for node_id in checkpoint.verified_node_ids): raise TrajectoryContractError("checkpoint verified node drifted")
    pending_nodes = {item.node_id for item in checkpoint.pending_executions}
    pending = tuple(sorted(pending_nodes))
    verified = tuple(sorted(checkpoint.verified_node_ids))
    ready = tuple(sorted(node_id for node_id, status in node_validity.items() if status == "pending" and node_id not in checkpoint.verified_node_ids and node_id not in pending_nodes))
    return RecoveryProjection(verified, pending, ready)


def _mapping(value: object, fields: set[str], label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != fields: raise TrajectoryContractError(f"{label} shape is invalid")
    return value
def _id(value: object, label: str) -> None:
    if not isinstance(value, str) or _ID.fullmatch(value) is None: raise TrajectoryContractError(f"{label} is invalid")
def _ids(values: object, label: str) -> None:
    if not isinstance(values, tuple) or len(values) != len(set(values)): raise TrajectoryContractError(f"{label} are invalid")
    for value in values: _id(value, label)
def _ref(value: object, project: str, label: str) -> None:
    if not isinstance(value, str) or _REF.fullmatch(value) is None: raise TrajectoryContractError(f"{label} crossed project scope")
    parts = value.removeprefix("crp://").split("/")
    if len(parts) < 3 or parts[1] != project: raise TrajectoryContractError(f"{label} crossed project scope")
def _refs(values: object, project: str, label: str) -> None:
    if not isinstance(values, tuple) or len(values) != len(set(values)): raise TrajectoryContractError(f"{label} are invalid")
    for value in values: _ref(value, project, label)
def _strings(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) != len(set(value)) or not all(isinstance(item, str) for item in value): raise TrajectoryContractError(f"{label} are invalid")
    return tuple(value)
def _node_budgets(value: object) -> tuple[NodeBudget, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)): raise TrajectoryContractError("node budgets are invalid")
    return tuple(NodeBudget.from_payload(item) for item in value)
def _pending(value: object) -> tuple[PendingExecution, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)): raise TrajectoryContractError("pending executions are invalid")
    return tuple(PendingExecution.from_payload(item) for item in value)
def _dispatch_ref(value: object, kind: str) -> None:
    if not isinstance(value, str) or not value.startswith(f"crp://dispatch/{kind}/"):
        raise TrajectoryContractError("checkpoint dispatch reference is invalid")
