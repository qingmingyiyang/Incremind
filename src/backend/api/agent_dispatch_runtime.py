"""Provider-free intake and steward planning for governed Agent dispatch.

This module is intentionally only a planning seam.  It freezes small,
non-prompt intake facts and derives capacity from durable Agent records; it
does not create Turns, run a model, call a provider, or consume permits.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
import re
from typing import Protocol
from uuid import NAMESPACE_URL, uuid5

from core.ai_kernel import AgentBudget, AgentChildLink, AgentProfile, AgentRun
from core.ai_kernel.contracts import validate_turn_request
from core.ai_kernel.agent_dispatch_contracts import (
    AgentDispatchPlan,
    CapacitySnapshot,
    DispatchPermit,
    ExpertAssignment,
    ExpertCluster,
    IntakeRoutingReceipt,
    WorkloadSnapshot,
    canonical_dispatch_ref,
)


class AgentDispatchRuntimeError(ValueError):
    """A proposed dispatch plan is not a safe derivative of durable facts."""


class ImmutablePayloadWriter(Protocol):
    def __call__(self, turn_id: str, kind: str, payload: Mapping[str, object]) -> str: ...


class AgentTopologyReader(Protocol):
    def list_runs(self, *, project_id: str, parent_run_id: str | None = None) -> tuple[AgentRun, ...]: ...
    def list_child_links(self, *, project_id: str, parent_run_id: str | None = None) -> tuple[AgentChildLink, ...]: ...
    def list_reservations(self, *, project_id: str, parent_run_id: str | None = None) -> tuple[object, ...]: ...


class AgentProfileReader(Protocol):
    def get(self, profile_id: str) -> AgentProfile | None: ...


class DispatchStorePort(Protocol):
    def put_intake_receipt(self, value: IntakeRoutingReceipt, *, operation_id: str) -> tuple[IntakeRoutingReceipt, bool]: ...
    def put_workload_snapshot(self, value: WorkloadSnapshot, *, operation_id: str) -> tuple[WorkloadSnapshot, bool]: ...
    def put_capacity_snapshot(self, value: CapacitySnapshot, *, operation_id: str) -> tuple[CapacitySnapshot, bool]: ...
    def put_expert_cluster(self, value: ExpertCluster, *, operation_id: str) -> tuple[ExpertCluster, bool]: ...
    def put_assignment(self, value: ExpertAssignment, *, operation_id: str) -> tuple[ExpertAssignment, bool]: ...
    def create_plan(self, value: AgentDispatchPlan, *, operation_id: str) -> tuple[AgentDispatchPlan, bool]: ...
    def transition_plan(self, value: AgentDispatchPlan, *, expected_revision: int, operation_id: str) -> AgentDispatchPlan: ...
    def issue_permit(self, value: DispatchPermit) -> tuple[DispatchPermit, bool]: ...
    def publish_ready_plan(self, draft: AgentDispatchPlan, *, cluster: ExpertCluster | None, assignments: tuple[ExpertAssignment, ...], permits: tuple[DispatchPermit, ...], operation_id: str) -> tuple[AgentDispatchPlan, tuple[DispatchPermit, ...]]: ...
    def get_assignment(self, assignment_id: str, *, project_id: str) -> ExpertAssignment | None: ...
    def get_assignment_for_permit(self, permit_id: str, *, project_id: str) -> tuple[DispatchPermit, ExpertAssignment] | None: ...
    def claim_permit_assignment(self, permit_id: str, *, project_id: str, operation_id: str) -> tuple[DispatchPermit, ExpertAssignment]: ...


OpaqueAssignmentResolver = Callable[[str, Mapping[str, object], str], tuple[str, int] | None]
_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


@dataclass(frozen=True, slots=True)
class IntakeRouteResult:
    receipt: IntakeRoutingReceipt
    input_features_ref: str


@dataclass(frozen=True, slots=True)
class DispatchLoadResult:
    workload: WorkloadSnapshot
    capacity: CapacitySnapshot


@dataclass(frozen=True, slots=True)
class StewardPlanResult:
    plan: AgentDispatchPlan
    cluster: ExpertCluster | None
    assignments: tuple[ExpertAssignment, ...]
    permits: tuple[DispatchPermit, ...]


class AgentDispatchRuntime:
    """Derive safe dispatch facts without acquiring any execution authority."""

    def __init__(
        self,
        *,
        payload_writer: ImmutablePayloadWriter,
        topology: AgentTopologyReader,
        profiles: AgentProfileReader,
        dispatch_store: DispatchStorePort,
        expert_resolver: OpaqueAssignmentResolver | None = None,
        skill_resolver: OpaqueAssignmentResolver | None = None,
    ) -> None:
        self._payload_writer = payload_writer
        self._topology = topology
        self._profiles = profiles
        self._store = dispatch_store
        self._expert_resolver = expert_resolver
        self._skill_resolver = skill_resolver

    def route_intake(self, request: Mapping[str, object]) -> IntakeRouteResult:
        """Freeze deterministic, non-content intake facts for a steward only."""
        payload = validate_turn_request(request)
        if "agent_binding" in payload:
            raise AgentDispatchRuntimeError("intake routing accepts external main requests only")
        project_id = _project_id(payload)
        turn_id, operation_id = _text(payload.get("turn_id"), "turn id"), _text(payload.get("operation_id"), "operation id")
        input_value = payload.get("input")
        policy = payload.get("context_policy")
        privacy = payload.get("privacy")
        capability_policy = payload.get("capability_policy")
        if not isinstance(input_value, Mapping) or not isinstance(policy, Mapping) or not isinstance(privacy, Mapping) or not isinstance(capability_policy, Mapping):
            raise AgentDispatchRuntimeError("intake request facts are unavailable")
        text_value = input_value.get("text")
        refs = input_value.get("refs")
        features = {
            "schema_version": "1.0.0",
            "kind": "agent.intake.features.v1",
            "input_kind": input_value.get("kind"),
            "text_length": len(text_value) if isinstance(text_value, str) else 0,
            "reference_count": len(refs) if isinstance(refs, list) else 0,
            "desired_outcome": payload.get("desired_outcome"),
            "allow_remote": privacy.get("allow_remote"),
            "pii": privacy.get("pii"),
            "approval_capability_count": len(capability_policy.get("require_approval", ())),
        }
        input_ref = self._write(turn_id, "agent-intake-features", features)
        routing_ref = self._write(turn_id, "agent-intake-routing", {
            "schema_version": "1.0.0", "kind": "agent.intake.routing.v1",
            "route": "steward_required", "input_features_ref": input_ref,
            "policy": {"deterministic": True, "model_used": False},
        })
        context_ref = self._write(turn_id, "agent-intake-context-policy", {
            "schema_version": "1.0.0", "kind": "agent.intake.context-policy.v1",
            "context_policy": dict(policy),
        })
        receipt = IntakeRoutingReceipt(
            _derived_id("intake", turn_id, operation_id), project_id, turn_id, 1,
            "steward_required", input_ref, routing_ref, context_ref, 1,
        )
        stored, _ = self._store.put_intake_receipt(
            receipt,
            operation_id=_derived_id("intake-operation", project_id, turn_id, operation_id),
        )
        return IntakeRouteResult(stored, input_ref)

    def snapshot_load(self, main_run: AgentRun, *, operation_id: str) -> DispatchLoadResult:
        """Derive workload/capacity exclusively from persistent topology records."""
        _require_main(main_run)
        project_id = main_run.project_id
        links = self._topology.list_child_links(project_id=project_id, parent_run_id=main_run.run_id)
        reservations = self._topology.list_reservations(project_id=project_id, parent_run_id=main_run.run_id)
        active_states = {"spawned", "started", "cancelling"}
        queued = sum(link.status == "reserved" for link in links)
        active = sum(link.status in active_states for link in links)
        committed = _sum_budgets(
            _reservation_budget(item)
            for item in reservations
            if getattr(item, "status", None) in {"reserved", "settled"}
        )
        try:
            remaining = main_run.budget_limit.remaining_after(committed)
        except Exception as error:
            raise AgentDispatchRuntimeError("durable reservations exceed main budget") from error
        maximum_slots = main_run.max_concurrent_children
        capacity = max(0, maximum_slots - active - queued)
        seed = f"{main_run.run_id}:{operation_id}"
        workload = WorkloadSnapshot(_derived_id("workload", seed), project_id, 1, queued, active, committed)
        snapshot = CapacitySnapshot(_derived_id("capacity", seed), project_id, 1, capacity, maximum_slots, remaining)
        snapshot_operation = _derived_id("snapshot-operation", project_id, main_run.run_id, operation_id)
        stored_workload, _ = self._store.put_workload_snapshot(workload, operation_id=f"{snapshot_operation}.workload")
        stored_capacity, _ = self._store.put_capacity_snapshot(snapshot, operation_id=f"{snapshot_operation}.capacity")
        return DispatchLoadResult(stored_workload, stored_capacity)

    def publish_steward_plan(
        self,
        *,
        main_run: AgentRun,
        steward_run: AgentRun,
        intake: IntakeRouteResult,
        load: DispatchLoadResult,
        proposal: Mapping[str, object],
        operation_id: str,
    ) -> StewardPlanResult:
        """Validate a steward proposal, persist ready plan, and issue one permit/item."""
        _require_main(main_run)
        _require_steward(steward_run, main_run, self._topology, self._profiles)
        if not isinstance(intake, IntakeRouteResult):
            raise AgentDispatchRuntimeError(
                "steward planning requires the frozen intake route result"
            )
        receipt = intake.receipt
        if not isinstance(receipt, IntakeRoutingReceipt):
            raise AgentDispatchRuntimeError("frozen intake route result is invalid")
        if not isinstance(load, DispatchLoadResult):
            raise AgentDispatchRuntimeError("steward planning requires a durable load snapshot")
        if receipt.project_id != main_run.project_id or load.workload.project_id != main_run.project_id or load.capacity.project_id != main_run.project_id:
            raise AgentDispatchRuntimeError("dispatch facts cross project scope")
        if receipt.route != "steward_required":
            raise AgentDispatchRuntimeError("steward plan requires steward intake route")
        if not isinstance(proposal, Mapping):
            raise AgentDispatchRuntimeError("steward proposal is invalid")
        mode = proposal.get("mode")
        plan_nonce = _text(proposal.get("plan_id"), "plan id")
        plan_id = _scoped_dispatch_id("plan", main_run, steward_run, operation_id, plan_nonce)
        if mode == "main_only":
            if set(proposal) != {"mode", "plan_id"}:
                raise AgentDispatchRuntimeError("main-only proposal shape is invalid")
            plan = AgentDispatchPlan(
                plan_id, main_run.project_id, main_run.run_id, steward_run.run_id, 1, "draft", "main_only",
                canonical_dispatch_ref("intake", receipt.receipt_id), receipt.revision,
                canonical_dispatch_ref("workload", load.workload.snapshot_id), load.workload.revision,
                canonical_dispatch_ref("capacity", load.capacity.snapshot_id), load.capacity.revision,
                None, None, (), AgentBudget(0, 0, 0, 0, 0), 0,
            )
            ready, permits = self._store.publish_ready_plan(
                plan, cluster=None, assignments=(), permits=(),
                operation_id=_scoped_dispatch_id("publish", main_run, steward_run, operation_id, "main-only"),
            )
            return StewardPlanResult(ready, None, (), permits)
        if mode != "cluster" or set(proposal) != {"mode", "plan_id", "cluster_id", "assignments"}:
            raise AgentDispatchRuntimeError("cluster proposal shape is invalid")
        cluster_nonce = _text(proposal.get("cluster_id"), "cluster id")
        cluster_id = _scoped_dispatch_id("cluster", main_run, steward_run, operation_id, cluster_nonce)
        specs = proposal.get("assignments")
        divided = isinstance(specs, list) and any(isinstance(item, Mapping) and 'division' in item for item in specs)
        if not isinstance(specs, list) or not specs or (not divided and len(specs) > load.capacity.available_slots) or load.capacity.available_slots < 1:
            raise AgentDispatchRuntimeError("cluster assignments exceed durable capacity")
        divisions = {}
        if divided:
            from backend.shared.task_division_graph import validate_divisions
            identities = {_proposal_assignment_nonce(spec): _scoped_dispatch_id(
                "assignment", main_run, steward_run, operation_id, _proposal_assignment_nonce(spec)) for spec in specs}
            if len(identities) != len(specs):
                raise AgentDispatchRuntimeError("cluster assignment identity is duplicated")
            divisions = validate_divisions(specs, identities)
        assignments: list[ExpertAssignment] = []
        expert_refs: list[str] = []
        skill_refs: list[str] = []
        seen: set[str] = set()
        for spec in specs:
            assignment_nonce = _proposal_assignment_nonce(spec)
            assignment_id = _scoped_dispatch_id("assignment", main_run, steward_run, operation_id, assignment_nonce)
            assignment = self._assignment(
                {**spec, 'division':divisions[assignment_nonce]} if divided else spec, main_run=main_run, intake=receipt, cluster_id=cluster_id,
                cluster_revision=1, remaining=load.capacity.remaining_budget, assignment_id=assignment_id,
            )
            if assignment.assignment_id in seen:
                raise AgentDispatchRuntimeError("cluster assignment identity is duplicated")
            seen.add(assignment.assignment_id)
            assignments.append(assignment)
            if assignment.expert_snapshot_ref is not None: expert_refs.append(assignment.expert_snapshot_ref)
            if assignment.skill_snapshot_ref is not None: skill_refs.append(assignment.skill_snapshot_ref)
        total = _sum_budgets(item.delegated_budget for item in assignments)
        if not total.is_subset_of(load.capacity.remaining_budget) or not total.is_subset_of(main_run.budget_limit):
            raise AgentDispatchRuntimeError("cluster budget exceeds durable capacity")
        cluster = ExpertCluster(cluster_id, main_run.project_id, 1, tuple(sorted(set(expert_refs))), tuple(sorted(set(skill_refs))))
        plan = AgentDispatchPlan(
            plan_id, main_run.project_id, main_run.run_id, steward_run.run_id, 1, "draft", "cluster",
            canonical_dispatch_ref("intake", receipt.receipt_id), receipt.revision,
            canonical_dispatch_ref("workload", load.workload.snapshot_id), load.workload.revision,
            canonical_dispatch_ref("capacity", load.capacity.snapshot_id), load.capacity.revision,
            canonical_dispatch_ref("cluster", cluster.cluster_id), cluster.revision,
            tuple(item.assignment_id for item in assignments), total, min(len(assignments), load.capacity.available_slots),
        )
        permits = tuple(
            DispatchPermit(
                _derived_id("permit", plan.plan_id, assignment.assignment_id),
                plan.project_id, plan.plan_id, plan.revision + 1, assignment.assignment_id,
                _derived_id("permit-operation", plan.plan_id, assignment.assignment_id),
            )
            for assignment in assignments
        )
        ready, published_permits = self._store.publish_ready_plan(
            plan, cluster=cluster, assignments=tuple(assignments), permits=permits,
            operation_id=_scoped_dispatch_id("publish", main_run, steward_run, operation_id, "cluster"),
        )
        return StewardPlanResult(ready, cluster, tuple(assignments), published_permits)

    def _assignment(self, spec: object, *, main_run: AgentRun, intake: IntakeRoutingReceipt, cluster_id: str, cluster_revision: int, remaining: AgentBudget, assignment_id: str) -> ExpertAssignment:
        if not isinstance(spec, Mapping) or set(spec) - {'division'} != {"assignment_id", "profile_id", "profile_revision", "task", "budget", "capability_ids", "expert", "skill"}:
            raise AgentDispatchRuntimeError("assignment proposal shape is invalid")
        profile_id = _text(spec.get("profile_id"), "assignment profile id")
        revision = spec.get("profile_revision")
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
            raise AgentDispatchRuntimeError("assignment profile revision is invalid")
        profile = self._profiles.get(profile_id)
        if profile is None or not profile.enabled or profile.role != "subagent" or profile.revision != revision:
            raise AgentDispatchRuntimeError("assignment profile is unavailable or drifted")
        budget = _budget(spec.get("budget"))
        capabilities = _identifiers(spec.get("capability_ids"), "assignment capabilities", allow_empty=False)
        if not budget.is_subset_of(main_run.budget_limit) or not budget.is_subset_of(profile.budget_limit) or not budget.is_subset_of(remaining):
            raise AgentDispatchRuntimeError("assignment budget exceeds frozen ceiling")
        if not set(capabilities).issubset(main_run.capability_ids) or not set(capabilities).issubset(profile.capability_ids):
            raise AgentDispatchRuntimeError("assignment capabilities exceed frozen ceiling")
        expert_request = spec.get("expert")
        skill_request = spec.get("skill")
        expert = self._resolve(self._expert_resolver, "expert", expert_request, main_run.project_id)
        skill = self._resolve(self._skill_resolver, "skill", skill_request, main_run.project_id)
        expert_id = (
            _text(expert_request.get("expert_id"), "assignment expert id")
            if isinstance(expert_request, Mapping) else None
        )
        skill_ids = (
            _identifiers(skill_request.get("skill_ids"), "assignment skill ids", allow_empty=False)
            if isinstance(skill_request, Mapping) else ()
        )
        task = spec.get("task")
        if not isinstance(task, str) or not task.strip() or len(task) > 16_000:
            raise AgentDispatchRuntimeError("assignment task is invalid")
        task_payload_ref = self._write(
            main_run.turn_id,
            f"agent.dispatch.task.{assignment_id}.v1",
            {
                "schema_version": "1.0.0",
                "assignment_id": assignment_id,
                "task": task,
                # Keep the exact, already schema-checked expert request beside
                # the task payload.  AgentStore only receives its opaque ref;
                # permit materialization can later reproduce the same explicit
                # selection instead of inventing new intents or budget labels.
                "expert": dict(expert_request) if isinstance(expert_request, Mapping) else None,
                **({'division':spec['division']} if 'division' in spec else {}),
            },
        )
        return ExpertAssignment(
            assignment_id, main_run.project_id, cluster_id, cluster_revision,
            f"crp://agent/profiles/{profile.profile_id}", profile.revision,
            task_payload_ref, 1, capabilities, expert_id, skill_ids,
            *(expert if expert is not None else (None, None)),
            *(skill if skill is not None else (None, None)),
            intake.context_policy_ref, intake.context_policy_revision, budget,
        )

    @staticmethod
    def _resolve(resolver: OpaqueAssignmentResolver | None, kind: str, value: object, project_id: str) -> tuple[str, int] | None:
        if value is None:
            return None
        if not isinstance(value, Mapping):
            raise AgentDispatchRuntimeError(f"{kind} assignment proposal is invalid")
        if resolver is None:
            raise AgentDispatchRuntimeError(f"{kind} assignment authority is unavailable")
        resolved = resolver(project_id, value, kind)
        if resolved is None:
            raise AgentDispatchRuntimeError(f"{kind} assignment could not be resolved")
        reference, revision = resolved
        if not isinstance(reference, str) or not reference.startswith("crp://") or not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
            raise AgentDispatchRuntimeError(f"{kind} assignment authority returned an invalid reference")
        return reference, revision

    def _ready(self, draft: AgentDispatchPlan, operation_id: str) -> AgentDispatchPlan:
        stored, _ = self._store.create_plan(draft, operation_id=f"{operation_id}.create")
        if stored.status == "ready":
            return stored
        return self._store.transition_plan(replace(stored, revision=stored.revision + 1, status="ready"), expected_revision=stored.revision, operation_id=f"{operation_id}.ready")

    def _write(self, turn_id: str, kind: str, payload: Mapping[str, object]) -> str:
        reference = self._payload_writer(turn_id, kind, payload)
        if not isinstance(reference, str) or not reference.startswith("crp://"):
            raise AgentDispatchRuntimeError("immutable payload writer returned an invalid reference")
        return reference


def _require_main(run: AgentRun) -> None:
    if run.role != "main" or run.parent_run_id is not None:
        raise AgentDispatchRuntimeError("dispatch requires the durable main run")


def _require_steward(steward: AgentRun, main: AgentRun, topology: AgentTopologyReader, profiles: AgentProfileReader) -> None:
    profile = profiles.get("steward.scheduler")
    if (
        profile is None or not profile.enabled or profile.role != "subagent"
        or steward.profile_id != "steward.scheduler" or steward.profile_revision != profile.revision
        or steward.role != "subagent" or steward.parent_run_id != main.run_id
        or steward.project_id != main.project_id
    ):
        raise AgentDispatchRuntimeError("steward run identity is invalid")
    links = topology.list_child_links(project_id=main.project_id, parent_run_id=main.run_id)
    if not any(link.child_run_id == steward.run_id and link.child_project_id == main.project_id for link in links):
        raise AgentDispatchRuntimeError("steward run lacks a durable direct link")


def _project_id(request: Mapping[str, object]) -> str:
    scope = request.get("scope")
    project_id = scope.get("project_id") if isinstance(scope, Mapping) else None
    if not isinstance(project_id, str) or not project_id:
        raise AgentDispatchRuntimeError("intake routing requires a project scope")
    return project_id


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AgentDispatchRuntimeError(f"{label} is invalid")
    return value.strip()


def _budget(value: object) -> AgentBudget:
    if not isinstance(value, Mapping) or set(value) != {"model_calls", "tool_calls", "input_tokens", "output_tokens", "wall_time_ms"}:
        raise AgentDispatchRuntimeError("assignment budget is invalid")
    try:
        return AgentBudget(**dict(value))
    except (TypeError, ValueError) as error:
        raise AgentDispatchRuntimeError("assignment budget is invalid") from error


def _identifiers(value: object, label: str, *, allow_empty: bool) -> tuple[str, ...]:
    if not isinstance(value, list) or (not allow_empty and not value) or not all(isinstance(item, str) and _IDENTITY.fullmatch(item) for item in value):
        raise AgentDispatchRuntimeError(f"{label} are invalid")
    if len(value) != len(set(value)):
        raise AgentDispatchRuntimeError(f"{label} are invalid")
    return tuple(value)


def _reservation_budget(value: object) -> AgentBudget:
    reserved = getattr(value, "reserved_budget", None)
    settled = getattr(value, "settled_budget", None)
    if getattr(value, "status", None) == "settled" and isinstance(settled, AgentBudget):
        return settled
    if not isinstance(reserved, AgentBudget):
        raise AgentDispatchRuntimeError("durable reservation budget is invalid")
    return reserved


def _sum_budgets(values: Sequence[AgentBudget] | object) -> AgentBudget:
    total = AgentBudget(0, 0, 0, 0, 0)
    for value in values:  # type: ignore[union-attr]
        if not isinstance(value, AgentBudget):
            raise AgentDispatchRuntimeError("durable budget is invalid")
        total = total.plus(value)
    return total


def _proposal_assignment_nonce(value: object) -> str:
    if not isinstance(value, Mapping):
        raise AgentDispatchRuntimeError("assignment proposal shape is invalid")
    return _text(value.get("assignment_id"), "assignment id")


def _scoped_dispatch_id(kind: str, main_run: AgentRun, steward_run: AgentRun, operation_id: str, nonce: str) -> str:
    return _derived_id(kind, main_run.project_id, main_run.run_id, steward_run.run_id, operation_id, nonce)


def _derived_id(kind: str, *parts: str) -> str:
    return f"{kind}-{uuid5(NAMESPACE_URL, ':'.join(parts)).hex}"
