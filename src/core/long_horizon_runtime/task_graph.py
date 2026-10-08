"""Pure, World-event-payload-compatible asynchronous task graph contracts.

This module deliberately has no repository, worker, Agent, Job, Effect or
Receipt implementation.  It projects immutable scheduling facts which a
backend adapter may persist as existing World Events.  External execution is
represented solely by opaque references; terminal truth remains elsewhere.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import re
from types import MappingProxyType
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, uuid5

from .provenance import TraceSubject, VersionBinding, validate_project_version_binding


TASK_GRAPH_SCHEMA_VERSION = "1.0.0"
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_REF = re.compile(r"^crp://[A-Za-z0-9][A-Za-z0-9._~:/?#%+=@-]{1,511}$")
_KINDS = frozenset({
    "graph.created", "node.dispatch_requested", "node.dispatched", "node.result_observed",
    "node.cancel_requested", "node.cancelled", "node.pruned", "node.rescheduled", "node.validation",
})


class TaskGraphContractError(ValueError):
    """A graph fact is malformed, crosses scope, or violates causality."""


@dataclass(frozen=True, slots=True)
class CancellationAcknowledgement:
    """An immutable external acknowledgement for one exact cancellation intent."""

    project_id: str
    graph_id: str
    node_id: str
    operation_id: str
    cancellation_receipt_ref: str

    def __post_init__(self) -> None:
        for label, value in (
            ("project id", self.project_id),
            ("graph id", self.graph_id),
            ("node id", self.node_id),
            ("operation id", self.operation_id),
        ):
            _id(value, label)
        if _REF.fullmatch(self.cancellation_receipt_ref) is None:
            raise TaskGraphContractError("cancellation receipt reference is invalid")
        parsed = urlsplit(self.cancellation_receipt_ref)
        scope_parts = (parsed.netloc, *tuple(
            part for part in parsed.path.split("/") if part
        ))
        if self.project_id not in scope_parts:
            raise TaskGraphContractError(
                "cancellation receipt reference crossed project scope"
            )


def task_graph_event_identity(
    project_id: str,
    graph_id: str,
    kind: str,
    operation_id: str,
) -> str:
    """Return the canonical identity for one deterministic graph operation."""

    for label, value in (
        ("project id", project_id),
        ("graph id", graph_id),
        ("operation id", operation_id),
    ):
        _id(value, label)
    if kind not in _KINDS:
        raise TaskGraphContractError("task graph event kind is invalid")
    seed = repr((project_id, graph_id, kind, operation_id))
    return f"task-graph-event-{uuid5(NAMESPACE_URL, seed).hex}"


def task_graph_world_identity(event: "TaskGraphEvent") -> tuple[str, str, str]:
    """Bind a graph fact to its system-owned World event envelope."""

    if not isinstance(event, TaskGraphEvent):
        raise TaskGraphContractError("task graph event is invalid")
    return (
        f"world-{event.event_id}",
        f"crp://task-graphs/{event.project_id}/{event.graph_id}/{event.event_id}",
        "task-graph-v1",
    )


@dataclass(frozen=True, slots=True)
class TaskBudget:
    units: int

    def __post_init__(self) -> None:
        if not isinstance(self.units, int) or isinstance(self.units, bool) or self.units < 0:
            raise TaskGraphContractError("task budget units are invalid")

    def to_payload(self) -> dict[str, object]:
        return {"units": self.units}

    @classmethod
    def from_payload(cls, value: object) -> "TaskBudget":
        item = _mapping(value, {"units"}, "task budget")
        return cls(item["units"])  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class TaskGraphNode:
    node_id: str
    dependency_ids: tuple[str, ...]
    priority: int
    budget: TaskBudget
    join: str = "all"
    subject: TraceSubject | None = None
    subject_version: VersionBinding | None = None

    def __post_init__(self) -> None:
        _id(self.node_id, "node id")
        if not isinstance(self.dependency_ids, tuple) or len(set(self.dependency_ids)) != len(self.dependency_ids):
            raise TaskGraphContractError("node dependencies are invalid")
        for dependency in self.dependency_ids:
            _id(dependency, "node dependency")
            if dependency == self.node_id:
                raise TaskGraphContractError("node cannot depend on itself")
        if not isinstance(self.priority, int) or isinstance(self.priority, bool) or not 0 <= self.priority <= 100:
            raise TaskGraphContractError("node priority is invalid")
        if self.join not in {"all", "any", "quorum"}:
            raise TaskGraphContractError("node join is invalid")
        if self.join != "all" and not self.dependency_ids:
            raise TaskGraphContractError("non-all join requires dependencies")
        if self.budget.units < 1:
            raise TaskGraphContractError("node budget must be positive")
        if not isinstance(self.subject, TraceSubject) or not isinstance(self.subject_version, VersionBinding):
            raise TaskGraphContractError("node provenance binding is required")
        if self.subject_version.revision is None:
            raise TaskGraphContractError("node provenance revision is required")

    def to_payload(self) -> dict[str, object]:
        return {
            "node_id": self.node_id, "dependency_ids": list(self.dependency_ids),
            "priority": self.priority, "budget": self.budget.to_payload(), "join": self.join,
            "subject": self.subject.to_payload(), "subject_version": self.subject_version.to_payload(),
        }

    @classmethod
    def from_payload(cls, value: object) -> "TaskGraphNode":
        item = _mapping(value, {"node_id", "dependency_ids", "priority", "budget", "join", "subject", "subject_version"}, "task node")
        dependencies = item["dependency_ids"]
        if not isinstance(dependencies, Sequence) or isinstance(dependencies, (str, bytes)) or any(not isinstance(v, str) for v in dependencies):
            raise TaskGraphContractError("node dependencies are invalid")
        return cls(str(item["node_id"]), tuple(dependencies), item["priority"], TaskBudget.from_payload(item["budget"]), str(item["join"]), TraceSubject.from_payload(item["subject"]), VersionBinding.from_payload(item["subject_version"]))  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class TaskGraphSpec:
    graph_id: str
    project_id: str
    command_id: str
    budget_limit: TaskBudget
    max_concurrency: int
    nodes: tuple[TaskGraphNode, ...]

    def __post_init__(self) -> None:
        for label, value in (("graph id", self.graph_id), ("project id", self.project_id), ("command id", self.command_id)):
            _id(value, label)
        if not isinstance(self.nodes, tuple) or not self.nodes or len({node.node_id for node in self.nodes}) != len(self.nodes):
            raise TaskGraphContractError("graph nodes are invalid")
        if not isinstance(self.max_concurrency, int) or isinstance(self.max_concurrency, bool) or not 1 <= self.max_concurrency <= len(self.nodes):
            raise TaskGraphContractError("graph concurrency is invalid")
        if self.budget_limit.units < 1:
            raise TaskGraphContractError("graph budget must be positive")
        known = {node.node_id for node in self.nodes}
        if any(not set(node.dependency_ids).issubset(known) for node in self.nodes):
            raise TaskGraphContractError("graph dependency is unknown")
        if sum(node.budget.units for node in self.nodes) < 1 or any(node.budget.units > self.budget_limit.units for node in self.nodes):
            raise TaskGraphContractError("node budget exceeds graph budget")
        if any(node.subject is None or node.subject.project_id != self.project_id for node in self.nodes):
            raise TaskGraphContractError("node provenance crossed project scope")
        for node in self.nodes:
            assert node.subject_version is not None
            try:
                validate_project_version_binding(node.subject_version, self.project_id)
            except Exception as error:
                raise TaskGraphContractError("node provenance authority crossed project scope") from error
        bindings = {
            (node.subject.kind, node.subject.subject_id, node.subject_version.authority_ref,
             node.subject_version.revision, node.subject_version.content_fingerprint)
            for node in self.nodes if node.subject is not None and node.subject_version is not None
        }
        if len(bindings) != len(self.nodes):
            raise TaskGraphContractError("node provenance subject/version is duplicated")
        _assert_acyclic(self.nodes)

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": TASK_GRAPH_SCHEMA_VERSION, "graph_id": self.graph_id,
            "project_id": self.project_id, "command_id": self.command_id,
            "budget_limit": self.budget_limit.to_payload(), "max_concurrency": self.max_concurrency,
            "nodes": [node.to_payload() for node in self.nodes],
        }

    @classmethod
    def from_payload(cls, value: object) -> "TaskGraphSpec":
        item = _mapping(value, {"schema_version", "graph_id", "project_id", "command_id", "budget_limit", "max_concurrency", "nodes"}, "task graph")
        if item["schema_version"] != TASK_GRAPH_SCHEMA_VERSION or not isinstance(item["nodes"], Sequence) or isinstance(item["nodes"], (str, bytes)):
            raise TaskGraphContractError("task graph payload is invalid")
        return cls(str(item["graph_id"]), str(item["project_id"]), str(item["command_id"]), TaskBudget.from_payload(item["budget_limit"]), item["max_concurrency"], tuple(TaskGraphNode.from_payload(node) for node in item["nodes"]))  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class TaskGraphEvent:
    event_id: str
    project_id: str
    graph_id: str
    sequence: int
    kind: str
    operation_id: str
    payload: Mapping[str, object]

    def __post_init__(self) -> None:
        for label, value in (("event id", self.event_id), ("project id", self.project_id), ("graph id", self.graph_id), ("operation id", self.operation_id)):
            _id(value, label)
        if not isinstance(self.sequence, int) or isinstance(self.sequence, bool) or self.sequence < 1:
            raise TaskGraphContractError("task graph event sequence is invalid")
        if self.kind not in _KINDS or not isinstance(self.payload, Mapping):
            raise TaskGraphContractError("task graph event is invalid")
        _validate_event_payload(self.kind, self.payload)
        object.__setattr__(self, "payload", _freeze(self.payload))

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": TASK_GRAPH_SCHEMA_VERSION, "event_id": self.event_id,
            "project_id": self.project_id, "graph_id": self.graph_id, "sequence": self.sequence,
            "kind": self.kind, "operation_id": self.operation_id, "payload": _json(self.payload),
        }

    @classmethod
    def from_payload(cls, value: object) -> "TaskGraphEvent":
        item = _mapping(value, {"schema_version", "event_id", "project_id", "graph_id", "sequence", "kind", "operation_id", "payload"}, "task graph event")
        if item["schema_version"] != TASK_GRAPH_SCHEMA_VERSION or not isinstance(item["payload"], Mapping):
            raise TaskGraphContractError("task graph event payload is invalid")
        return cls(str(item["event_id"]), str(item["project_id"]), str(item["graph_id"]), item["sequence"], str(item["kind"]), str(item["operation_id"]), dict(item["payload"]))  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class TaskGraphNodeState:
    node_id: str
    status: str
    attempt: int
    execution_ref: str | None
    dispatch_operation_id: str | None
    result_ref: str | None
    validation_status: str
    cancellation_receipt_ref: str | None


@dataclass(frozen=True, slots=True)
class TaskGraphProjection:
    spec: TaskGraphSpec
    through_sequence: int
    nodes: tuple[TaskGraphNodeState, ...]
    ready_node_ids: tuple[str, ...]
    next_dispatchable_node_ids: tuple[str, ...]
    budget_reserved: TaskBudget
    replayed_event_ids: tuple[str, ...]

    def node(self, node_id: str) -> TaskGraphNodeState:
        return next(node for node in self.nodes if node.node_id == node_id)

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": TASK_GRAPH_SCHEMA_VERSION, "spec": self.spec.to_payload(),
            "through_sequence": self.through_sequence,
            "nodes": [
                {"node_id": node.node_id, "status": node.status, "attempt": node.attempt,
                 "execution_ref": node.execution_ref, "result_ref": node.result_ref,
                 "dispatch_operation_id": node.dispatch_operation_id,
                 "validation_status": node.validation_status,
                 "cancellation_receipt_ref": node.cancellation_receipt_ref}
                for node in self.nodes
            ], "ready_node_ids": list(self.ready_node_ids),
            "next_dispatchable_node_ids": list(self.next_dispatchable_node_ids),
            "budget_reserved": self.budget_reserved.to_payload(),
            "replayed_event_ids": list(self.replayed_event_ids),
        }


def project_task_graph(events: Sequence[TaskGraphEvent]) -> TaskGraphProjection:
    """Project one graph's immutable facts without inspecting external state."""

    unique: dict[str, TaskGraphEvent] = {}
    replayed: list[str] = []
    for event in events:
        if not isinstance(event, TaskGraphEvent):
            raise TaskGraphContractError("task graph event is invalid")
        prior = unique.get(event.event_id)
        if prior is not None:
            if prior != event:
                raise TaskGraphContractError("task graph event identity conflicts")
            replayed.append(event.event_id)
        else:
            unique[event.event_id] = event
    ordered = tuple(sorted(unique.values(), key=lambda event: event.sequence))
    if not ordered or ordered[0].kind != "graph.created":
        raise TaskGraphContractError("task graph must begin with creation")
    project_id, graph_id = ordered[0].project_id, ordered[0].graph_id
    if any(event.project_id != project_id or event.graph_id != graph_id for event in ordered):
        raise TaskGraphContractError("task graph event crossed project scope")
    if tuple(event.sequence for event in ordered) != tuple(range(1, len(ordered) + 1)):
        raise TaskGraphContractError("task graph sequence is invalid")
    spec = TaskGraphSpec.from_payload(ordered[0].payload["spec"])
    if (spec.project_id, spec.graph_id) != (project_id, graph_id):
        raise TaskGraphContractError("task graph creation scope drifted")
    raw = {node.node_id: {"attempt": 0, "execution_ref": None, "dispatch_operation_id": None, "result_ref": None, "satisfied": None, "cancel": False, "cancel_operation_id": None, "cancellation_receipt_ref": None, "pruned": False, "validation": "pending"} for node in spec.nodes}
    for event in ordered[1:]:
        _apply_event(raw, spec, event)
    states = tuple(_node_state(node, raw[node.node_id], raw, spec) for node in spec.nodes)
    ready = tuple(node.node_id for node in states if node.status == "ready")
    active = tuple(node for node in states if node.status in {"dispatching", "leased", "cancel_requested"})
    available = max(0, spec.max_concurrency - len(active))
    remaining = spec.budget_limit.units - sum(spec_node.budget.units for spec_node in spec.nodes if raw[spec_node.node_id]["dispatch_operation_id"] is not None and raw[spec_node.node_id]["result_ref"] is None and raw[spec_node.node_id]["cancellation_receipt_ref"] is None)
    candidates = sorted((node for node in states if node.status == "ready" and _budget_for(spec, node.node_id).units <= remaining), key=lambda node: (-_node_for(spec, node.node_id).priority, node.node_id))
    dispatchable = tuple(node.node_id for node in candidates[:available])
    reserved = TaskBudget(sum(_budget_for(spec, node.node_id).units for node in active))
    return TaskGraphProjection(
        spec,
        ordered[-1].sequence,
        states,
        ready,
        dispatchable,
        reserved,
        tuple(replayed),
    )


def _apply_event(raw: dict[str, dict[str, object]], spec: TaskGraphSpec, event: TaskGraphEvent) -> None:
    if event.kind == "graph.created":
        raise TaskGraphContractError("task graph can only be created once")
    node_id = event.payload.get("node_id")
    if not isinstance(node_id, str) or node_id not in raw:
        raise TaskGraphContractError("task graph event node is unknown")
    state = raw[node_id]
    if event.kind == "node.dispatch_requested":
        attempt = event.payload["attempt"]
        if (not _is_dispatchable(node_id, raw, spec) or state["execution_ref"] is not None
                or state["pruned"] or state["cancel"] or state["validation"] != "pending"
                or attempt != int(state["attempt"]) + 1):
            raise TaskGraphContractError("node dispatch causality is invalid")
        state.update(attempt=attempt, dispatch_operation_id=event.operation_id)
    elif event.kind == "node.dispatched":
        if (state["dispatch_operation_id"] != event.operation_id or state["execution_ref"] is not None
                or state["result_ref"] is not None or event.payload["attempt"] != state["attempt"]):
            raise TaskGraphContractError("node dispatch binding is invalid")
        state["execution_ref"] = event.payload["execution_ref"]
    elif event.kind == "node.result_observed":
        if (state["execution_ref"] is None or state["result_ref"] is not None
                or state["cancellation_receipt_ref"] is not None
                or event.payload["attempt"] != state["attempt"]):
            raise TaskGraphContractError("node result causality is invalid")
        state.update(result_ref=event.payload["result_ref"], satisfied=event.payload["satisfied"])
    elif event.kind == "node.cancel_requested":
        if state["result_ref"] is not None:
            raise TaskGraphContractError("settled node cannot be cancelled")
        if state["cancel"]:
            raise TaskGraphContractError("node cancellation is already requested")
        state.update(cancel=True, cancel_operation_id=event.operation_id)
    elif event.kind == "node.cancelled":
        if (state["cancel"] is not True or state["result_ref"] is not None
                or state["cancel_operation_id"] != event.operation_id
                or state["cancellation_receipt_ref"] is not None):
            raise TaskGraphContractError("node cancellation acknowledgement is invalid")
        state["cancellation_receipt_ref"] = event.payload["cancellation_receipt_ref"]
    elif event.kind == "node.pruned":
        if (state["execution_ref"] is not None and state["result_ref"] is None
                and state["cancellation_receipt_ref"] is None):
            raise TaskGraphContractError("active node cannot be pruned")
        state["pruned"] = True
    elif event.kind == "node.rescheduled":
        if (state["execution_ref"] is not None and state["result_ref"] is None
                and state["cancellation_receipt_ref"] is None):
            raise TaskGraphContractError("active node cannot be rescheduled")
        if event.payload["attempt"] != int(state["attempt"]) + 1:
            raise TaskGraphContractError("node reschedule attempt is invalid")
        state.update(attempt=event.payload["attempt"] - 1, execution_ref=None, dispatch_operation_id=None, result_ref=None, satisfied=None, cancel=False, cancel_operation_id=None, cancellation_receipt_ref=None, pruned=False, validation="pending")
    else:  # node.validation
        status = event.payload["status"]
        if status == "verified" and (state["result_ref"] is None or state["satisfied"] is not True):
            raise TaskGraphContractError("verification requires a satisfied external result")
        if status in {"rejected", "inconclusive"} and state["result_ref"] is None:
            raise TaskGraphContractError("validation requires an observed external result")
        if status == "verified" and state["validation"] == "invalidated":
            raise TaskGraphContractError("invalidated node cannot be revalidated")
        state["validation"] = status


def _node_state(node: TaskGraphNode, state: Mapping[str, object], all_states: Mapping[str, Mapping[str, object]], spec: TaskGraphSpec) -> TaskGraphNodeState:
    validation = _effective_validation(node, state, all_states, spec)
    if state["cancellation_receipt_ref"] is not None:
        status = "cancelled"
    elif state["cancel"]:
        status = "cancel_requested"
    elif validation in {"stale", "invalidated"}:
        status = validation
    elif state["pruned"]:
        status = "pruned"
    elif state["dispatch_operation_id"] is not None and state["execution_ref"] is None:
        status = "dispatching"
    elif state["execution_ref"] is not None and state["result_ref"] is None:
        status = "leased"
    elif state["result_ref"] is not None:
        if validation == "pending": status = "awaiting_validation"
        elif validation == "verified": status = "settled"
        else: status = "rejected"
    else:
        dependencies = [_dependency_outcome(dependency, all_states, spec) for dependency in node.dependency_ids]
        successes, possible = dependencies.count(True), dependencies.count(None)
        threshold = len(dependencies) if node.join == "all" else (1 if node.join == "any" else max(1, (len(dependencies) + 1) // 2))
        if successes >= threshold: status = "ready"
        elif successes + possible < threshold: status = "blocked"
        else: status = "waiting_dependency"
    return TaskGraphNodeState(node.node_id, status, int(state["attempt"]), state["execution_ref"] if isinstance(state["execution_ref"], str) else None, state["dispatch_operation_id"] if isinstance(state["dispatch_operation_id"], str) else None, state["result_ref"] if isinstance(state["result_ref"], str) else None, validation, state["cancellation_receipt_ref"] if isinstance(state["cancellation_receipt_ref"], str) else None)


def _validate_event_payload(kind: str, payload: Mapping[str, object]) -> None:
    required = {"spec"} if kind == "graph.created" else {"node_id"}
    extra: set[str]
    if kind == "node.dispatch_requested": extra = {"attempt"}
    elif kind == "node.dispatched": extra = {"attempt", "execution_ref"}
    elif kind == "node.result_observed": extra = {"attempt", "result_ref", "satisfied"}
    elif kind == "node.validation": extra = {"status", "evidence_ref"}
    elif kind == "node.rescheduled": extra = {"attempt", "reason_ref"}
    elif kind == "node.pruned": extra = {"reason_ref"}
    elif kind == "node.cancel_requested": extra = {"reason_ref"}
    elif kind == "node.cancelled": extra = {"cancellation_receipt_ref"}
    else: extra = set()
    if set(payload) != required | extra:
        raise TaskGraphContractError("task graph event payload shape is invalid")
    if kind == "graph.created": TaskGraphSpec.from_payload(payload["spec"])
    else: _id(payload["node_id"], "node id")
    for field in ("execution_ref", "result_ref", "evidence_ref", "reason_ref", "cancellation_receipt_ref"):
        if field in payload and (not isinstance(payload[field], str) or _REF.fullmatch(payload[field]) is None):
            raise TaskGraphContractError("task graph external reference is invalid")
    if "attempt" in payload and (not isinstance(payload["attempt"], int) or isinstance(payload["attempt"], bool) or payload["attempt"] < 1):
        raise TaskGraphContractError("task graph attempt is invalid")
    if kind == "node.result_observed" and not isinstance(payload["satisfied"], bool):
        raise TaskGraphContractError("task graph result decision is invalid")
    if kind == "node.validation" and payload["status"] not in {"verified", "rejected", "inconclusive", "stale", "invalidated"}:
        raise TaskGraphContractError("task graph validation state is invalid")


def _mapping(value: object, keys: set[str], label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise TaskGraphContractError(f"{label} payload shape is invalid")
    return value


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(nested) for key, nested in value.items()})
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(_freeze(nested) for nested in value)
    return value


def _json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _json(nested) for key, nested in value.items()}
    if isinstance(value, tuple):
        return [_json(nested) for nested in value]
    return value


def _id(value: object, label: str) -> None:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise TaskGraphContractError(f"{label} is invalid")


def _node_for(spec: TaskGraphSpec, node_id: str) -> TaskGraphNode:
    return next(node for node in spec.nodes if node.node_id == node_id)


def _budget_for(spec: TaskGraphSpec, node_id: str) -> TaskBudget:
    return _node_for(spec, node_id).budget


def _effective_validation(node: TaskGraphNode, state: Mapping[str, object], all_states: Mapping[str, Mapping[str, object]], spec: TaskGraphSpec) -> str:
    own = str(state["validation"])
    inherited = [
        _effective_validation(_node_for(spec, dependency), all_states[dependency], all_states, spec)
        for dependency in node.dependency_ids
    ]
    if "invalidated" in inherited or own == "invalidated": return "invalidated"
    if "stale" in inherited or own == "stale": return "stale"
    return own


def _dependency_outcome(node_id: str, states: Mapping[str, Mapping[str, object]], spec: TaskGraphSpec) -> bool | None:
    node = _node_for(spec, node_id); state = states[node_id]
    validation = _effective_validation(node, state, states, spec)
    if validation in {"stale", "invalidated", "rejected", "inconclusive"}: return False
    if state["result_ref"] is not None:
        return True if validation == "verified" and state["satisfied"] is True else False if validation == "verified" else None
    if state["pruned"] or state["cancel"]: return False
    return None


def _is_dispatchable(node_id: str, states: Mapping[str, Mapping[str, object]], spec: TaskGraphSpec) -> bool:
    node = _node_for(spec, node_id)
    state = states[node_id]
    if _node_state(node, state, states, spec).status != "ready": return False
    active = [candidate for candidate in spec.nodes if states[candidate.node_id]["dispatch_operation_id"] is not None and states[candidate.node_id]["result_ref"] is None and states[candidate.node_id]["cancellation_receipt_ref"] is None]
    reserved = sum(candidate.budget.units for candidate in active)
    return len(active) < spec.max_concurrency and reserved + node.budget.units <= spec.budget_limit.units


def _assert_acyclic(nodes: tuple[TaskGraphNode, ...]) -> None:
    edges = {node.node_id: set(node.dependency_ids) for node in nodes}
    seen: set[str] = set(); active: set[str] = set()
    def visit(node_id: str) -> None:
        if node_id in active: raise TaskGraphContractError("task graph dependency cycle")
        if node_id in seen: return
        active.add(node_id)
        for dependency in edges[node_id]: visit(dependency)
        active.remove(node_id); seen.add(node_id)
    for node in nodes: visit(node.node_id)
