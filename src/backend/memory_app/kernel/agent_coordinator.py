"""Governed orchestration for one main Agent and independent child Turns.

This module deliberately owns no Provider, planner, executor, or durable Turn
state.  It coordinates the existing Turn authority through narrow injected
ports and persists only topology and delegation decisions in ``AgentStore``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
import json
import re
from time import monotonic, sleep
from typing import Protocol
from uuid import NAMESPACE_URL, uuid5

from core.ai_kernel import (
    AgentBudget,
    AgentBudgetReservation,
    AgentChildLink,
    AgentFanIn,
    AgentFanInResult,
    AgentMessage,
    AgentProfile,
    AgentRun,
    AgentTerminalChildSummary,
    TurnReceipt,
    context_manifest_from_payload,
    manifest_from_payload,
    validate_turn_request,
)
from core.ai_kernel.contracts import validate_model_call_receipt
from core.ai_kernel.agent_dispatch_contracts import (
    AgentDispatchPlan,
    DispatchPermit,
    ExpertAssignment,
)

from backend.api.agent_capabilities import AGENT_CAPABILITY_IDS


class AgentCoordinatorError(RuntimeError):
    """A caller attempted a delegation outside the governed topology."""


_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_.-]{2,127}$")


class AgentCoordinatorRuntimePort(Protocol):
    def accept_turn(self, request: Mapping[str, object]) -> TurnReceipt: ...

    def receipt_for(self, turn_id: str, *, replayed: bool = False) -> TurnReceipt: ...


class AgentCoordinatorRunnerPort(Protocol):
    def accept_and_submit(self, request: Mapping[str, object]) -> TurnReceipt: ...

    def request_turn_cancel(self, turn_id: str, *, reason: str) -> bool: ...

    def wait_for_terminal(self, turn_id: str, *, timeout_seconds: float | None = None, poll_interval_seconds: float = 0.05) -> TurnReceipt | None: ...

    def terminal_receipt(self, turn_id: str) -> TurnReceipt | None: ...


class AgentCoordinatorStorePort(Protocol):
    def register_run(self, run: AgentRun, *, operation_id: str) -> AgentRun: ...
    def get_run_by_turn_id(self, turn_id: str, *, project_id: str) -> tuple[AgentRun, int] | None: ...
    def get_run_with_revision(self, run_id: str, *, project_id: str) -> tuple[AgentRun, int] | None: ...
    def get_child_link(self, link_id: str, *, project_id: str) -> AgentChildLink | None: ...
    def get_reservation(self, reservation_id: str, *, project_id: str) -> AgentBudgetReservation | None: ...
    def list_child_links(self, *, project_id: str, parent_run_id: str | None = None) -> tuple[AgentChildLink, ...]: ...
    def list_runs(self, *, project_id: str, parent_run_id: str | None = None) -> tuple[AgentRun, ...]: ...
    def list_recovery_candidates(self, *, limit: int = 64) -> tuple[AgentRun, ...]: ...
    def list_messages(self, *, project_id: str, run_id: str, status: str | None = None) -> tuple[AgentMessage, ...]: ...
    def reserve_spawn(self, *, parent: AgentRun, child: AgentRun, link: AgentChildLink, reservation: AgentBudgetReservation) -> tuple[AgentRun, AgentChildLink, AgentBudgetReservation, bool]: ...
    def finalize_spawn(self, link_id: str, *, operation_id: str, expected_cancel_epoch: int) -> AgentChildLink: ...
    def abort_reserved_spawn(self, link_id: str, reservation_id: str, *, operation_id: str, expected_cancel_epoch: int) -> tuple[AgentChildLink, AgentBudgetReservation]: ...
    def transition_child_link(self, link_id: str, *, status: str, operation_id: str, expected_cancel_epoch: int) -> AgentChildLink: ...
    def cancel_parent(self, parent_run_id: str, *, expected_revision: int, operation_id: str) -> AgentRun: ...
    def send_message_next(self, message: AgentMessage) -> tuple[AgentMessage, bool]: ...
    def deliver_message(self, message_id: str, *, operation_id: str, expected_cancel_epoch: int) -> AgentMessage: ...
    def acknowledge_message(self, message_id: str, *, operation_id: str, expected_cancel_epoch: int) -> AgentMessage: ...
    def create_fan_in(self, fan_in: AgentFanIn) -> tuple[AgentFanIn, bool]: ...
    def complete_fan_in(self, result: AgentFanInResult, *, operation_id: str, expected_cancel_epoch: int) -> tuple[AgentFanInResult, bool]: ...
    def get_fan_in(self, fan_in_id: str, *, project_id: str) -> AgentFanIn | None: ...
    def list_fan_ins(self, *, project_id: str, parent_run_id: str | None = None) -> tuple[AgentFanIn, ...]: ...
    def get_fan_in_result(self, fan_in_id: str, *, project_id: str) -> AgentFanInResult | None: ...
    def converge_terminal_child(self, run: AgentRun, *, usage: AgentBudget, operation_id: str) -> tuple[AgentRun, AgentChildLink, AgentBudgetReservation, bool]: ...


class AgentCoordinatorProfilesPort(Protocol):
    def get(self, profile_id: str) -> AgentProfile | None: ...


class AgentPolicySnapshotPort(Protocol):
    """Optional authority that freezes evolution policy before Turn admission."""

    def freeze_for_turn(self, *, project_id: str, turn_id: str) -> object: ...

    def load_turn_snapshot(self, *, project_id: str, turn_id: str) -> object | None: ...


class AgentMessagePayloadAuthorityPort(Protocol):
    """Copies sender-owned payload evidence into the recipient Turn scope."""

    def copy_for_recipient(
        self, *, sender_turn_id: str, recipient_turn_id: str,
        source_payload_ref: str, message_id: str, kind: str,
    ) -> str: ...


class AgentDispatchRuntimePort(Protocol):
    def route_intake(self, request: Mapping[str, object]) -> object: ...
    def snapshot_load(self, main_run: AgentRun, *, operation_id: str) -> object: ...
    def publish_steward_plan(self, *, main_run: AgentRun, steward_run: AgentRun, intake: object, load: object, proposal: Mapping[str, object], operation_id: str) -> object: ...


class AgentPermitStorePort(Protocol):
    """Durable permit authority used only to materialize an approved child."""

    def get_plan(self, plan_id: str, *, project_id: str) -> AgentDispatchPlan | None: ...
    def list_plans_for_main(self, *, project_id: str, main_run_id: str) -> tuple[AgentDispatchPlan, ...]: ...
    def get_assignment_for_permit(self, permit_id: str, *, project_id: str) -> tuple[DispatchPermit, ExpertAssignment] | None: ...
    def bind_permit_to_child_run(self, permit_id: str, *, project_id: str, child_run_id: str, operation_id: str) -> tuple[DispatchPermit, str]: ...


@dataclass(frozen=True, slots=True)
class PreparedMainTurn:
    run: AgentRun
    request: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class PreparedChildTurn:
    run: AgentRun
    link: AgentChildLink
    reservation: AgentBudgetReservation
    request: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class _FrozenPolicyAdmission:
    turn_id: str
    policy_id: str
    revision: int
    snapshot_ref: str
    policy: object
    snapshot_payload: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class SpawnRequest:
    """Caller-provided IDs make a spawn replay-safe without hidden UUID state."""

    operation_id: str
    child_run_id: str
    child_turn_id: str
    link_id: str
    reservation_id: str
    profile_id: str
    child_session_id: str
    idempotency_key: str
    input_text: str
    requested_capability_ids: tuple[str, ...]
    requested_budget: AgentBudget
    requested_max_steps: int
    requested_timeout_ms: int
    requested_max_concurrent_children: int = 0
    requested_max_depth: int = 0
    allow_child_spawn: bool = False
    input_refs: tuple[str, ...] = ()


class AgentCoordinator:
    """Coordinates child Agents as bounded, separately durable AI Turns."""

    def __init__(
        self,
        *,
        runtime: AgentCoordinatorRuntimePort,
        runner: AgentCoordinatorRunnerPort,
        store: AgentCoordinatorStorePort,
        profiles: AgentCoordinatorProfilesPort,
        request_loader: Callable[[str], Mapping[str, object]],
        events_loader: Callable[[str], Sequence[Mapping[str, object]]] | None = None,
        payload_loader: Callable[[str], object] | None = None,
        terminal_receipt_writer: Callable[[str, Mapping[str, object]], str] | None = None,
        immutable_payload_writer: Callable[[str, str, Mapping[str, object]], str] | None = None,
        immutable_payload_reference: Callable[[str, str], str] | None = None,
        agent_policy_snapshots: AgentPolicySnapshotPort | None = None,
        agent_policy_id: str = "workbench.default",
        message_payload_authority: AgentMessagePayloadAuthorityPort | None = None,
        dispatch_runtime: AgentDispatchRuntimePort | None = None,
        permit_store: AgentPermitStorePort | None = None,
        clock_sleep: Callable[[float], None] = sleep,
        monotonic_clock: Callable[[], float] = monotonic,
    ) -> None:
        self._runtime = runtime
        self._runner = runner
        self._store = store
        self._profiles = profiles
        self._request_loader = request_loader
        self._events_loader = events_loader or (lambda _turn_id: ())
        self._payload_loader = payload_loader
        self._terminal_receipt_writer = terminal_receipt_writer
        self._immutable_payload_writer = immutable_payload_writer
        self._immutable_payload_reference = immutable_payload_reference
        self._agent_policy_snapshots = agent_policy_snapshots
        self._agent_policy_id = agent_policy_id
        self._message_payload_authority = message_payload_authority
        self._dispatch_runtime = dispatch_runtime
        self._permit_store = permit_store
        self._sleep = clock_sleep
        self._monotonic = monotonic_clock

    def accept_main_and_submit(self, request: Mapping[str, object]) -> AgentRun:
        """Accept the parent Turn before registering its coordination identity."""
        prepared = self.accept_and_register_main(request)
        self.submit_accepted_turn(prepared)
        return prepared.run

    def accept_and_register_main(self, request: Mapping[str, object]) -> PreparedMainTurn:
        """Durably accept and register a main run without starting its worker."""
        payload = validate_turn_request(request)
        if "agent_binding" in payload or "agent_policy_binding" in payload:
            raise AgentCoordinatorError("main Agent binding is host-owned")
        project_id = _project_id(payload)
        profile = self._require_profile("main.orchestrator", role="main")
        policy = self._freeze_policy_for_new_turn(
            project_id=project_id, turn_id=str(payload["turn_id"]), role="main", profile=profile,
        )
        run = _run_from_profile(
            run_id=f"main-run-{payload['turn_id']}", turn_id=str(payload["turn_id"]),
            project_id=project_id, profile=profile, role="main", depth=0,
            parent_run_id=None, status="queued",
        )
        run = _apply_policy_to_run(run, policy.policy if policy is not None else None)
        capability_policy = payload.get("capability_policy")
        allowed = capability_policy.get("allowed") if isinstance(capability_policy, Mapping) else None
        if not isinstance(allowed, list) or not all(isinstance(item, str) for item in allowed):
            raise AgentCoordinatorError("main Agent capability policy is unavailable")
        original_allowed = set(allowed)
        original_denied = set(capability_policy.get("denied", ()))
        original_approval = set(capability_policy.get("require_approval", ()))
        effective_allowed = set(run.capability_ids) & original_allowed
        run = replace(
            run,
            capability_ids=tuple(sorted(effective_allowed)),
            budget_snapshot_ref=_profile_budget_uri(
                str(payload["turn_id"]), profile.profile_id, profile.revision,
            ),
        )
        payload = dict(payload)
        # The Agent Profile is an upper authority bound, not metadata. Freeze
        # its intersection into the actual Turn request so ordinary tools are
        # governed by the same subset as native agent operations.
        payload["capability_policy"] = {
            "allowed": sorted(effective_allowed),
            "denied": sorted(original_denied | (original_allowed - effective_allowed)),
            "require_approval": sorted(original_approval & effective_allowed),
        }
        payload["agent_binding"] = _agent_binding_from_run(run)
        if policy is not None:
            payload = _apply_policy_to_request(payload, policy)
        payload = validate_turn_request(payload)
        # Turn acceptance remains first: AgentStore never manufactures a Turn
        # record.  Model execution begins only after registration and the
        # runner's idempotent acceptance replay below.
        self._runtime.accept_turn(payload)
        if self._immutable_payload_writer is not None:
            from core.ai_kernel.agent_contracts import agent_role_brief_to_payload
            self._immutable_payload_writer(str(payload["turn_id"]), "agent-role-brief-v1", agent_role_brief_to_payload(profile))
        self._persist_policy_snapshot(policy)
        stored = self._store.register_run(run, operation_id=str(payload["operation_id"]))
        return PreparedMainTurn(stored, payload)

    def submit_accepted_turn(self, prepared: PreparedMainTurn | PreparedChildTurn) -> TurnReceipt:
        """Submit a Turn only after its durable Agent topology is ready."""
        if not isinstance(prepared, (PreparedMainTurn, PreparedChildTurn)):
            raise AgentCoordinatorError("prepared Agent Turn is invalid")
        receipt = self._runner.accept_and_submit(prepared.request)
        if not isinstance(receipt, TurnReceipt):
            raise AgentCoordinatorError("Agent Turn runner returned an invalid receipt")
        return receipt

    def resubmit_existing_turn(
        self, *, turn_id: str, project_id: str,
    ) -> TurnReceipt:
        """Idempotently replay one registered frozen Agent request.

        Recovery never creates a new Turn or invokes model-routing authority
        outside the runner.  A persisted Agent binding must still exactly
        match its registered run before the existing runner is asked to resume
        its normal idempotent acceptance/submission path.
        """
        if not isinstance(turn_id, str) or not turn_id:
            raise AgentCoordinatorError("agent replay Turn identity is invalid")
        if not isinstance(project_id, str) or not project_id:
            raise AgentCoordinatorError("agent replay project identity is invalid")
        request = validate_turn_request(self._request_loader(turn_id))
        if str(request.get("turn_id")) != turn_id or _project_id(request) != project_id:
            raise AgentCoordinatorError("agent replay request identity drifted")
        binding = request.get("agent_binding")
        if not isinstance(binding, Mapping):
            raise AgentCoordinatorError("agent replay binding is unavailable")
        verified = self.verify_agent_binding(request, binding)
        found = self._store.get_run_by_turn_id(turn_id, project_id=project_id)
        if found is None or verified.get("run_id") != found[0].run_id:
            raise AgentCoordinatorError("agent replay run identity drifted")
        receipt = self._runner.accept_and_submit(request)
        if not isinstance(receipt, TurnReceipt):
            raise AgentCoordinatorError("Agent Turn runner returned an invalid receipt")
        return receipt

    def spawn(
        self, *, parent_turn_id: str, request: SpawnRequest | None = None,
        operation_id: str | None = None, tool_call_id: str | None = None,
        project_id: str | None = None, scope: Mapping[str, object] | None = None,
        privacy: Mapping[str, object] | None = None, arguments: Mapping[str, object] | None = None,
    ) -> Mapping[str, object]:
        """Reserve a strict child subset, accept its Turn, then schedule it."""
        prepared = self.prepare_child(
            parent_turn_id=parent_turn_id, request=request, operation_id=operation_id,
            tool_call_id=tool_call_id, project_id=project_id, scope=scope,
            privacy=privacy, arguments=arguments,
        )
        self.submit_accepted_turn(prepared)
        return _operation_result("child Agent spawned", run=_safe_run(prepared.run), link_id=prepared.link.link_id)

    def prepare_child(
        self, *, parent_turn_id: str, request: SpawnRequest | None = None,
        operation_id: str | None = None, tool_call_id: str | None = None,
        project_id: str | None = None, scope: Mapping[str, object] | None = None,
        privacy: Mapping[str, object] | None = None, arguments: Mapping[str, object] | None = None,
        child_request_factory: Callable[[Mapping[str, object]], Mapping[str, object]] | None = None,
    ) -> PreparedChildTurn:
        """Reserve → accept → finalize one child without submitting its worker."""
        parent, _ = self._parent_for_turn(parent_turn_id)
        _assert_native_scope(parent, project_id=project_id, scope=scope, privacy=privacy, request_loader=self._request_loader, turn_id=parent_turn_id)
        _require_agent_capability(parent, "agent.spawn")
        if request is None:
            request = _spawn_request_from_arguments(arguments, parent=parent, operation_id=operation_id, tool_call_id=tool_call_id)
        profile = self._require_profile(request.profile_id, role="subagent")
        if not parent.allow_child_spawn:
            raise AgentCoordinatorError("parent agent is not allowed to spawn children")
        if parent.depth + 1 > parent.max_depth:
            raise AgentCoordinatorError("parent depth limit prevents child delegation")
        policy = self._freeze_policy_for_new_turn(
            project_id=parent.project_id, turn_id=request.child_turn_id, role="subagent", profile=profile,
        )
        child = _child_run(parent, profile, request)
        child = _apply_policy_to_run(child, policy.policy if policy is not None else None)
        link = AgentChildLink(
            request.link_id, parent.run_id, child.run_id, parent.project_id,
            parent.project_id, request.operation_id, parent.cancel_epoch,
            child.depth, child.budget_limit, child.capability_ids, "reserved",
        )
        reservation = AgentBudgetReservation(
            request.reservation_id, parent.project_id, parent.run_id, child.run_id,
            request.operation_id, parent.cancel_epoch, child.budget_limit, None, "reserved",
        )
        child, link, reservation, _created = self._store.reserve_spawn(
            parent=parent, child=child, link=link, reservation=reservation,
        )
        child_request = self._child_turn_request(parent_turn_id, parent, child, link, reservation, request, policy)
        if child_request_factory is not None:
            child_request = validate_turn_request(child_request_factory(child_request))
        if policy is not None:
            child_request = _apply_policy_to_request(child_request, policy)
        accepted = False
        try:
            self._runtime.accept_turn(child_request)
            accepted = True
            if self._immutable_payload_writer is not None:
                from core.ai_kernel.agent_contracts import agent_role_brief_to_payload
                self._immutable_payload_writer(child.turn_id, "agent-role-brief-v1", agent_role_brief_to_payload(profile))
            self._persist_policy_snapshot(policy)
            self._store.finalize_spawn(
                link.link_id, operation_id=f"{request.operation_id}.finalize",
                expected_cancel_epoch=parent.cancel_epoch,
            )
            finalized = self._store.get_child_link(link.link_id, project_id=parent.project_id)
            if finalized is None:
                raise AgentCoordinatorError("accepted child link is unavailable")
            return PreparedChildTurn(child, finalized, reservation, child_request)
        except Exception:
            # Only an unquestionably pre-Turn failure can free the reservation.
            # Once acceptance crossed the Turn authority, recovery owns it.
            if not accepted:
                self._store.abort_reserved_spawn(
                    link.link_id, reservation.reservation_id,
                    operation_id=f"{request.operation_id}.abort",
                    expected_cancel_epoch=parent.cancel_epoch,
                )
            raise

    def plan(
        self, *, parent_turn_id: str, operation_id: str, tool_call_id: str | None = None,
        project_id: str | None = None, scope: Mapping[str, object] | None = None,
        privacy: Mapping[str, object] | None = None, arguments: Mapping[str, object] | None = None,
    ) -> Mapping[str, object]:
        """Publish a steward plan from frozen topology; permits never spawn here."""
        if self._dispatch_runtime is None:
            raise AgentCoordinatorError("agent dispatch runtime is unavailable")
        steward, _ = self._parent_for_turn(parent_turn_id)
        _assert_native_scope(steward, project_id=project_id, scope=scope, privacy=privacy, request_loader=self._request_loader, turn_id=parent_turn_id)
        _require_agent_capability(steward, "agent.plan")
        if steward.profile_id != "steward.scheduler" or steward.role != "subagent" or steward.parent_run_id is None:
            raise AgentCoordinatorError("agent planning requires the steward scheduler child")
        main = self._require_run(steward.parent_run_id, steward.project_id)
        if main.role != "main" or main.parent_run_id is not None:
            raise AgentCoordinatorError("steward parent is not a durable main Agent")
        links = self._store.list_child_links(project_id=main.project_id, parent_run_id=main.run_id)
        if not any(link.child_run_id == steward.run_id and link.status in {"spawned", "started"} for link in links):
            raise AgentCoordinatorError("steward link is not an active direct child")
        proposal = dict(arguments or {})
        host_main_request = dict(validate_turn_request(self._request_loader(main.turn_id)))
        host_main_request.pop("agent_binding", None)
        policy = self._bound_policy(host_main_request, main.project_id)
        _validate_steward_proposal_policy(proposal, policy)
        intake = self._dispatch_runtime.route_intake(host_main_request)
        load = self._dispatch_runtime.snapshot_load(main, operation_id=f"{operation_id}.load")
        published = self._dispatch_runtime.publish_steward_plan(
            main_run=main, steward_run=steward, intake=intake, load=load,
            proposal=proposal, operation_id=operation_id,
        )
        plan = getattr(published, "plan", None)
        permits = getattr(published, "permits", ())
        plan_id = getattr(plan, "plan_id", None)
        if not isinstance(plan_id, str) or not isinstance(permits, tuple):
            raise AgentCoordinatorError("agent dispatch runtime returned an invalid plan")
        permit_ids = tuple(
            value for value in (getattr(item, "permit_id", None) for item in permits)
            if isinstance(value, str)
        )
        if len(permit_ids) != len(permits):
            raise AgentCoordinatorError("agent dispatch runtime returned invalid permits")
        return _operation_result("steward plan published", plan_id=plan_id, permit_ids=permit_ids)

    def prepare_child_from_permit(
        self, *, project_id: str, permit_id: str, operation_id: str,
    ) -> PreparedChildTurn:
        """Materialize one approved dispatch permit without submitting its Turn.

        This is deliberately separate from :meth:`spawn`: callers supply no
        task, profile, budget, capabilities, expert, or parent identity.  Each
        of those inputs is recovered from the durable permit and plan.
        """
        authority = self._permit_store
        if authority is None:
            raise AgentCoordinatorError("agent permit store is unavailable")
        found = authority.get_assignment_for_permit(permit_id, project_id=project_id)
        if found is None:
            raise AgentCoordinatorError("agent dispatch permit is unavailable")
        permit, assignment = found
        plan = authority.get_plan(permit.plan_id, project_id=project_id)
        if plan is None:
            raise AgentCoordinatorError("agent dispatch plan is unavailable")
        self._validate_permit_materialization(
            project_id=project_id, permit=permit, assignment=assignment, plan=plan,
        )
        main = self._require_run(plan.main_run_id, project_id)
        child_ids = _permit_child_ids(permit, operation_id)
        task, expert_request = self._permit_task(assignment)
        dependencies = self.assignment_dependencies(project_id=project_id, permit_id=permit_id)
        if dependencies:
            if not set(dependencies).issubset(plan.assignment_ids):
                raise AgentCoordinatorError('task dependencies are outside the frozen plan')
            permits = authority.list_permits(project_id=project_id, plan_id=plan.plan_id)
            dependency_child_ids = []
            for dependency in dependencies:
                predecessor = next(item for item in permits if item.assignment_id == dependency)
                child_id = authority.get_permit_child_run_binding(predecessor.permit_id, project_id=project_id)
                if child_id is None or not self._require_run(child_id, project_id).is_terminal:
                    raise AgentCoordinatorError('task dependencies are not terminal')
                dependency_child_ids.append(child_id)
            parent_request = self._request_loader(main.turn_id)
            fan_id = _derived_id('task-dependencies', plan.plan_id, assignment.assignment_id)
            self.fan_in(parent_turn_id=main.turn_id, project_id=project_id,
                scope=parent_request['scope'], privacy=parent_request['privacy'],
                operation_id=fan_id, fan_in=AgentFanIn(fan_id, project_id, main.run_id,
                    fan_id, tuple(dependency_child_ids), 'all', None, main.cancel_epoch, 'open'))
            result = self._store.get_fan_in_result(fan_id, project_id=project_id)
            if result is None:
                raise AgentCoordinatorError('task dependency fan-in is unavailable')
            summaries = [_safe_terminal_summary(item, self._payload_loader, main.turn_id) for item in result.child_summaries]
            task += '\n\n主智能体转交的依赖结果：\n' + json.dumps(summaries, ensure_ascii=False)
        bound, bound_run_id = authority.bind_permit_to_child_run(
            permit.permit_id, project_id=project_id,
            child_run_id=child_ids["run_id"], operation_id=operation_id,
        )
        if (
            bound.permit_id != permit.permit_id
            or bound.assignment_id != assignment.assignment_id
            or bound_run_id != child_ids["run_id"]
        ):
            raise AgentCoordinatorError("agent permit binding is invalid")
        permit = bound
        request = SpawnRequest(
            operation_id=operation_id,
            child_run_id=child_ids["run_id"], child_turn_id=child_ids["turn_id"],
            link_id=child_ids["link_id"], reservation_id=child_ids["reservation_id"],
            profile_id=_profile_id_from_ref(assignment.profile_ref),
            child_session_id=child_ids["session_id"], idempotency_key=child_ids["idempotency_key"],
            input_text=task,
            requested_capability_ids=assignment.capability_ids,
            requested_budget=assignment.delegated_budget,
            requested_max_steps=main.max_steps,
            requested_timeout_ms=main.timeout_ms,
            requested_max_concurrent_children=0,
            requested_max_depth=main.max_depth,
            allow_child_spawn=False,
        )
        main_request = validate_turn_request(self._request_loader(main.turn_id))
        prepared = self.prepare_child(
            parent_turn_id=main.turn_id, request=request, project_id=project_id,
            scope=main_request["scope"], privacy=main_request["privacy"],
            child_request_factory=lambda base: self._permit_child_turn_request(
                base, assignment, task=task, expert_request=expert_request,
            ),
        )
        if (prepared.run.budget_limit != assignment.delegated_budget
                or set(prepared.run.capability_ids) != set(assignment.capability_ids)):
            raise AgentCoordinatorError("prepared child drifted from frozen permit limits")
        return prepared

    def _validate_permit_materialization(
        self, *, project_id: str, permit: DispatchPermit, assignment: ExpertAssignment,
        plan: AgentDispatchPlan,
    ) -> None:
        published_plan_revision = (
            plan.revision - 1 if plan.status == "dispatching" else plan.revision
        )
        if (
            permit.project_id != project_id or assignment.project_id != project_id
            or plan.project_id != project_id or permit.assignment_id != assignment.assignment_id
            or permit.plan_id != plan.plan_id
            or permit.plan_revision != published_plan_revision
            or assignment.assignment_id not in plan.assignment_ids
            or plan.status not in {"ready", "dispatching"}
        ):
            raise AgentCoordinatorError("agent permit does not match the frozen dispatch plan")
        main = self._require_run(plan.main_run_id, project_id)
        steward = self._require_run(plan.steward_run_id, project_id)
        if (
            main.role != "main" or main.parent_run_id is not None
            or main.status in {"completed", "failed", "cancelled", "timed_out", "quarantined"}
            or steward.role != "subagent" or steward.profile_id != "steward.scheduler"
            or steward.parent_run_id != main.run_id
        ):
            raise AgentCoordinatorError("agent dispatch topology is invalid")
        main_profile = self._require_profile(main.profile_id, role="main")
        steward_profile = self._require_profile("steward.scheduler", role="subagent")
        if main.profile_revision != main_profile.revision or steward.profile_revision != steward_profile.revision:
            raise AgentCoordinatorError("agent dispatch profile revision drifted")
        links = self._store.list_child_links(project_id=project_id, parent_run_id=main.run_id)
        steward_links = tuple(link for link in links if link.child_run_id == steward.run_id)
        if len(steward_links) != 1:
            raise AgentCoordinatorError("agent steward direct child link is invalid")
        steward_link = steward_links[0]
        active_steward = (
            steward.status not in {"completed", "failed", "cancelled", "timed_out", "quarantined"}
            and steward_link.status in {"spawned", "started"}
        )
        completed_steward = (
            steward.status == "completed" and steward_link.status == "completed"
            and isinstance(steward.terminal_receipt_ref, str)
            and steward.terminal_receipt_ref.startswith("crp://")
        )
        if not (active_steward or completed_steward):
            raise AgentCoordinatorError("agent steward is not authorized for dispatch")
        profile_id = _profile_id_from_ref(assignment.profile_ref)
        profile = self._require_profile(profile_id, role="subagent")
        if assignment.profile_revision != profile.revision:
            raise AgentCoordinatorError("agent assignment profile revision drifted")
        if not assignment.task_payload_ref.startswith("crp://") or assignment.task_payload_revision < 1:
            raise AgentCoordinatorError("agent assignment task snapshot is invalid")
        if not assignment.delegated_budget.is_subset_of(main.budget_limit) or not assignment.delegated_budget.is_subset_of(profile.budget_limit):
            raise AgentCoordinatorError("agent assignment budget exceeds frozen limits")
        if not assignment.capability_ids or not set(assignment.capability_ids).issubset(main.capability_ids) or not set(assignment.capability_ids).issubset(profile.capability_ids):
            raise AgentCoordinatorError("agent assignment capabilities exceed frozen limits")
        _validate_assignment_snapshots(assignment)

    def assignment_dependencies(self, *, project_id: str, permit_id: str) -> tuple[str, ...]:
        if self._permit_store is None:
            raise AgentCoordinatorError('assignment authority is unavailable')
        found = self._permit_store.get_assignment_for_permit(permit_id, project_id=project_id)
        if found is None or self._payload_loader is None:
            raise AgentCoordinatorError('assignment authority is unavailable')
        _, assignment = found
        self._permit_task(assignment)
        value = self._payload_loader(assignment.task_payload_ref)
        division = value.get('division')
        return tuple(division['depends_on']) if division is not None else ()

    def _permit_task(
        self, assignment: ExpertAssignment,
    ) -> tuple[str, Mapping[str, object] | None]:
        if self._payload_loader is None:
            raise AgentCoordinatorError("agent assignment payload authority is unavailable")
        try:
            value = self._payload_loader(assignment.task_payload_ref)
        except Exception as error:
            raise AgentCoordinatorError("agent assignment task payload is unavailable") from error
        if not isinstance(value, Mapping) or set(value) - {'division'} != {
            "schema_version", "assignment_id", "task", "expert",
        }:
            raise AgentCoordinatorError("agent assignment task payload is invalid")
        task = value.get("task")
        expert = value.get("expert")
        if (
            value.get("schema_version") != "1.0.0"
            or value.get("assignment_id") != assignment.assignment_id
            or not isinstance(task, str) or not task.strip() or len(task) > 16_000
        ):
            raise AgentCoordinatorError("agent assignment task payload drifted")
        if assignment.expert_id is None:
            if expert is not None:
                raise AgentCoordinatorError("agent assignment expert request drifted")
            request: dict[str, object] | None = None
        else:
            if not isinstance(expert, Mapping) or set(expert) != {
                "expert_id", "task_intents", "budget",
            } or expert.get("expert_id") != assignment.expert_id:
                raise AgentCoordinatorError("agent assignment expert request is invalid")
            intents, budget = expert.get("task_intents"), expert.get("budget")
            if (
                not isinstance(intents, list) or not intents
                or any(not isinstance(item, str) or not item.strip() for item in intents)
                or not isinstance(budget, str) or not budget.strip()
            ):
                raise AgentCoordinatorError("agent assignment expert request is invalid")
            request = dict(expert)
        if assignment.skill_ids:
            if request is None:
                request = {
                    "expert_id": None,
                    "task_intents": ["agent.permit.execute"],
                    "budget": "agent-permit",
                }
            request["skill_ids"] = list(assignment.skill_ids)
        return task.strip(), request

    def _permit_child_turn_request(
        self, base_request: Mapping[str, object], assignment: ExpertAssignment,
        *, task: str, expert_request: Mapping[str, object] | None,
    ) -> dict[str, object]:
        payload = dict(base_request)
        binding = base_request["agent_binding"]
        parent = self._require_run(binding["parent_run_id"], assignment.project_id)
        parent_request = validate_turn_request(self._request_loader(parent.turn_id))
        parent_refs = parent_request["input"].get("refs", [])
        requested_refs = tuple(str(item["uri"]) for item in parent_refs)
        payload["input"] = {
            "kind": "text", "text": task,
            "refs": _bounded_refs(parent_request, requested_refs),
        }
        if expert_request is not None:
            payload["expert_request"] = dict(expert_request)
        else:
            payload.pop("expert_request", None)
        # A permit freezes which capabilities are available, not fabricated
        # business arguments for one of them.  The child planner chooses among
        # this exact manifest using the actual frozen task text.
        payload.pop("capability_request", None)
        return validate_turn_request(payload)

    def message(
        self, *, parent_turn_id: str | None = None, sender_turn_id: str | None = None,
        recipient_run_id: str | None = None, message_id: str | None = None,
        operation_id: str, tool_call_id: str | None = None, project_id: str | None = None,
        scope: Mapping[str, object] | None = None, privacy: Mapping[str, object] | None = None,
        arguments: Mapping[str, object] | None = None, kind: str | None = None, payload_ref: str | None = None,
    ) -> Mapping[str, object]:
        if arguments is not None:
            recipient_run_id = _text_argument(arguments, "recipient_run_id")
            kind = _text_argument(arguments, "kind")
            payload_ref = _text_argument(arguments, "payload_ref")
        sender_turn_id = sender_turn_id or parent_turn_id
        if sender_turn_id is None or recipient_run_id is None or kind is None or payload_ref is None:
            raise AgentCoordinatorError("agent message arguments are incomplete")
        sender, _ = self._parent_for_turn(sender_turn_id)
        _assert_native_scope(sender, project_id=project_id, scope=scope, privacy=privacy, request_loader=self._request_loader, turn_id=sender_turn_id)
        _require_agent_capability(sender, "agent.message")
        recipient = self._require_run(recipient_run_id, sender.project_id)
        if not _directly_related(sender, recipient):
            raise AgentCoordinatorError("agent messages require a direct parent-child relation")
        message_id = message_id or _derived_id(
            "message", sender.turn_id, operation_id, tool_call_id or "native",
        )
        authority = self._message_payload_authority
        if authority is None:
            raise AgentCoordinatorError("recipient message payload authority is unavailable")
        recipient_payload_ref = authority.copy_for_recipient(
            sender_turn_id=sender.turn_id, recipient_turn_id=recipient.turn_id,
            source_payload_ref=payload_ref, message_id=message_id, kind=kind,
        )
        if not isinstance(recipient_payload_ref, str) or not recipient_payload_ref.startswith("crp://"):
            raise AgentCoordinatorError("recipient message payload authority returned an invalid ref")
        # Sequence 1 is a validated placeholder. The store allocates the real
        # contiguous sequence under BEGIN IMMEDIATE so concurrent sends cannot
        # race between a read and insert.
        message = AgentMessage(message_id, sender.project_id, sender.run_id, recipient.run_id, operation_id, 1, kind, recipient_payload_ref, sender.cancel_epoch, "pending")
        stored, _ = self._store.send_message_next(message)
        return _operation_result("agent message queued", message_id=stored.message_id, status=stored.status)

    def deliver_message(self, *, recipient_turn_id: str, message_id: str, operation_id: str) -> AgentMessage:
        recipient, _ = self._parent_for_turn(recipient_turn_id)
        message = self._store.deliver_message(message_id, operation_id=operation_id, expected_cancel_epoch=recipient.cancel_epoch)
        if message.recipient_run_id != recipient.run_id:
            raise AgentCoordinatorError("message recipient binding is invalid")
        return message

    def acknowledge_message(self, *, recipient_turn_id: str, message_id: str, operation_id: str) -> AgentMessage:
        recipient, _ = self._parent_for_turn(recipient_turn_id)
        message = self._store.acknowledge_message(message_id, operation_id=operation_id, expected_cancel_epoch=recipient.cancel_epoch)
        if message.recipient_run_id != recipient.run_id:
            raise AgentCoordinatorError("message recipient binding is invalid")
        return message

    def interrupt(self, *, parent_turn_id: str, child_run_id: str | None = None, operation_id: str, reason: str | None = None, tool_call_id: str | None = None, project_id: str | None = None, scope: Mapping[str, object] | None = None, privacy: Mapping[str, object] | None = None, arguments: Mapping[str, object] | None = None) -> Mapping[str, object]:
        if arguments is not None:
            child_run_id = _text_argument(arguments, "child_run_id")
            reason = _text_argument(arguments, "reason")
        if child_run_id is None or reason is None:
            raise AgentCoordinatorError("agent interrupt arguments are incomplete")
        parent, _ = self._parent_for_turn(parent_turn_id)
        _assert_native_scope(parent, project_id=project_id, scope=scope, privacy=privacy, request_loader=self._request_loader, turn_id=parent_turn_id)
        _require_agent_capability(parent, "agent.interrupt")
        link = next((item for item in self._store.list_child_links(project_id=parent.project_id, parent_run_id=parent.run_id) if item.child_run_id == child_run_id), None)
        if link is None:
            raise AgentCoordinatorError("child is not owned by this parent")
        child = self._require_run(child_run_id, parent.project_id)
        requested = self._runner.request_turn_cancel(child.turn_id, reason=reason)
        if not requested:
            return _operation_result(
                "child Agent interruption was not requested",
                child_run_id=child.run_id,
                requested=False,
            )
        self._store.transition_child_link(link.link_id, status="cancelling", operation_id=operation_id, expected_cancel_epoch=parent.cancel_epoch)
        return _operation_result("child Agent interruption requested", child_run_id=child.run_id, requested=True)

    def wait(self, *, parent_turn_id: str, child_run_ids: Sequence[str] | None = None, timeout_ms: int | None = None, operation_id: str | None = None, tool_call_id: str | None = None, project_id: str | None = None, scope: Mapping[str, object] | None = None, privacy: Mapping[str, object] | None = None, arguments: Mapping[str, object] | None = None) -> Mapping[str, object]:
        if arguments is not None:
            candidate = arguments.get("child_run_ids")
            child_run_ids = tuple(candidate) if isinstance(candidate, Sequence) and not isinstance(candidate, str) and all(isinstance(item, str) for item in candidate) else None
            timeout_ms = arguments.get("timeout_ms") if isinstance(arguments.get("timeout_ms"), int) else None
        if child_run_ids is None or timeout_ms is None:
            raise AgentCoordinatorError("agent wait arguments are incomplete")
        parent, _ = self._parent_for_turn(parent_turn_id)
        _assert_native_scope(parent, project_id=project_id, scope=scope, privacy=privacy, request_loader=self._request_loader, turn_id=parent_turn_id)
        _require_agent_capability(parent, "agent.wait")
        if not isinstance(timeout_ms, int) or isinstance(timeout_ms, bool) or not 1 <= timeout_ms <= 120_000:
            raise AgentCoordinatorError("wait timeout must be between 1 and 120000 milliseconds")
        allowed = {link.child_run_id for link in self._store.list_child_links(project_id=parent.project_id, parent_run_id=parent.run_id)}
        requested = tuple(child_run_ids)
        if not requested or not set(requested).issubset(allowed):
            raise AgentCoordinatorError("wait children must be direct children")
        deadline = self._monotonic() + timeout_ms / 1000
        observations: list[Mapping[str, object]] = []
        for child_run_id in requested:
            child = self._require_run(child_run_id, parent.project_id)
            remaining = max(0.0, deadline - self._monotonic())
            receipt = self._runner.wait_for_terminal(child.turn_id, timeout_seconds=remaining)
            if receipt is None:
                receipt = self._runner.terminal_receipt(child.turn_id)
            observations.append({
                "run": _safe_run(child),
                "receipt": _safe_receipt(receipt),
            })
        return _operation_result(
            "child Agent wait completed", children=tuple(observations),
            terminal=all(item["receipt"] is not None for item in observations),
        )

    def fan_in(self, *, parent_turn_id: str, fan_in: AgentFanIn | None = None, result: AgentFanInResult | None = None, operation_id: str | None = None, tool_call_id: str | None = None, project_id: str | None = None, scope: Mapping[str, object] | None = None, privacy: Mapping[str, object] | None = None, arguments: Mapping[str, object] | None = None) -> Mapping[str, object]:
        parent, _ = self._parent_for_turn(parent_turn_id)
        _assert_native_scope(parent, project_id=project_id, scope=scope, privacy=privacy, request_loader=self._request_loader, turn_id=parent_turn_id)
        _require_agent_capability(parent, "agent.fan_in")
        if fan_in is None:
            fan_in = _fan_in_from_arguments(arguments, parent=parent, operation_id=operation_id, tool_call_id=tool_call_id)
        if fan_in.parent_run_id != parent.run_id or fan_in.project_id != parent.project_id or fan_in.cancel_epoch != parent.cancel_epoch:
            raise AgentCoordinatorError("fan-in parent binding is invalid")
        stored, _ = self._store.create_fan_in(fan_in)
        automatic = self._reconcile_fan_in(stored, parent)
        if automatic is not None:
            return _fan_in_operation_result(automatic, self._payload_loader, parent.turn_id)
        if result is None:
            return _operation_result("fan-in created", fan_in_id=stored.fan_in_id, status=stored.status)
        if result.parent_run_id != parent.run_id or result.project_id != parent.project_id:
            raise AgentCoordinatorError("fan-in result parent binding is invalid")
        completed, _ = self._store.complete_fan_in(result, operation_id=operation_id or f"{fan_in.operation_id}.complete", expected_cancel_epoch=parent.cancel_epoch)
        return _fan_in_operation_result(completed, self._payload_loader, parent.turn_id)

    def list(self, *, parent_turn_id: str | None = None, turn_id: str | None = None, operation_id: str | None = None, tool_call_id: str | None = None, project_id: str | None = None, scope: Mapping[str, object] | None = None, privacy: Mapping[str, object] | None = None, arguments: Mapping[str, object] | None = None) -> Mapping[str, object]:
        run, _ = self._parent_for_turn(parent_turn_id or turn_id or "")
        _assert_native_scope(run, project_id=project_id, scope=scope, privacy=privacy, request_loader=self._request_loader, turn_id=parent_turn_id or turn_id or "")
        _require_agent_capability(run, "agent.list")
        children = self._store.list_runs(project_id=run.project_id, parent_run_id=run.run_id)
        links = self._store.list_child_links(project_id=run.project_id, parent_run_id=run.run_id)
        include_messages = bool(arguments.get("include_messages")) if isinstance(arguments, Mapping) else False
        result: dict[str, object] = {
            "summary": "agent topology listed",
            "run": _safe_run(run),
            "children": tuple({**_safe_run(child), "organization_role": (
                profile.organization_role if (profile := self._profiles.get(child.profile_id)) is not None else "未配置岗位"
            )} for child in children),
            "links": tuple({"link_id": link.link_id, "child_run_id": link.child_run_id, "status": link.status} for link in links),
            "fan_ins": tuple(
                _safe_fan_in(fan_in, self._store.get_fan_in_result(
                    fan_in.fan_in_id, project_id=run.project_id,
                ), self._payload_loader, run.turn_id)
                for fan_in in self._store.list_fan_ins(
                    project_id=run.project_id, parent_run_id=run.run_id,
                )
            ),
        }
        if self._permit_store is not None:
            result["plans"] = tuple(
                _safe_dispatch_plan(plan)
                for plan in self._permit_store.list_plans_for_main(
                    project_id=run.project_id, main_run_id=run.run_id,
                )
            )
        if include_messages:
            result["messages"] = tuple(
                _safe_message(message)
                for message in self._store.list_messages(
                    project_id=run.project_id, run_id=run.run_id,
                )
            )
        return result

    def verify_agent_binding(
        self, request: Mapping[str, object], binding: Mapping[str, object],
    ) -> Mapping[str, object]:
        """Verify an untrusted Turn binding against the durable topology.

        This is injected into model-routing authority; a structurally valid
        binding alone never grants a child model route.
        """
        payload = validate_turn_request(request)
        project_id = _project_id(payload)
        if payload.get("agent_binding") != binding:
            raise AgentCoordinatorError("agent binding differs from the Turn request")
        self._verify_policy_binding(payload, project_id)
        run_id = binding.get("run_id")
        if not isinstance(run_id, str) or payload.get("turn_id") is None:
            raise AgentCoordinatorError("agent binding identity is invalid")
        found = self._store.get_run_by_turn_id(str(payload["turn_id"]), project_id=project_id)
        if found is None:
            raise AgentCoordinatorError("agent binding Turn is not registered")
        run, _ = found
        if run.run_id != run_id:
            raise AgentCoordinatorError("agent binding run identity drifted")
        expected = {
            "run_id": run.run_id, "role": run.role, "profile_id": run.profile_id,
            "profile_revision": run.profile_revision, "model_tier": run.model_tier,
            "parent_run_id": run.parent_run_id, "depth": run.depth,
            "cancel_epoch": run.cancel_epoch, "budget_snapshot_ref": run.budget_snapshot_ref,
        }
        if any(binding.get(key) != value for key, value in expected.items()):
            raise AgentCoordinatorError("agent binding run snapshot drifted")
        route_fields = {"model_route_key", "model_route_revision"}
        if run.model_route_key is None:
            if any(key in binding for key in route_fields):
                raise AgentCoordinatorError("agent binding model route drifted")
        elif (
            binding.get("model_route_key") != run.model_route_key
            or binding.get("model_route_revision") != run.model_route_revision
        ):
            raise AgentCoordinatorError("agent binding model route drifted")
        if run.role == "main":
            if (
                run.parent_run_id is not None
                or run.profile_id != "main.orchestrator"
                or run.depth != 0
                or run.budget_snapshot_ref
                != _profile_budget_uri(run.turn_id, run.profile_id, run.profile_revision)
                or any(
                    key in binding
                    for key in (
                        "parent_run_id", "link_id", "reservation_id",
                        "spawn_operation_id",
                    )
                )
            ):
                raise AgentCoordinatorError("main Agent binding drifted")
            return dict(binding)
        if run.role != "subagent":
            raise AgentCoordinatorError("agent binding role is invalid")
        link_id = binding.get("link_id")
        reservation_id = binding.get("reservation_id")
        if not isinstance(link_id, str) or not isinstance(reservation_id, str):
            raise AgentCoordinatorError("agent binding topology is invalid")
        link = self._store.get_child_link(link_id, project_id=project_id)
        reservation = self._store.get_reservation(reservation_id, project_id=project_id)
        parent = (
            self._store.get_run_with_revision(str(run.parent_run_id), project_id=project_id)
            if run.parent_run_id is not None else None
        )
        if link is None or reservation is None or (
            parent is None or parent[0].cancel_epoch != run.cancel_epoch
            or link.parent_run_id != run.parent_run_id or link.child_run_id != run.run_id
            or link.parent_project_id != project_id or link.child_project_id != project_id
            or link.spawn_operation_id != binding.get("spawn_operation_id")
            or link.parent_cancel_epoch != run.cancel_epoch or link.child_depth != run.depth
            or link.delegated_budget != run.budget_limit
            or link.delegated_capability_ids != run.capability_ids
            or link.status not in {"spawned", "started", "cancelling", "stopped", "completed", "failed", "cancelled", "timed_out", "quarantined"}
            or reservation.parent_run_id != run.parent_run_id or reservation.child_run_id != run.run_id
            or reservation.project_id != project_id
            or reservation.operation_id != binding.get("spawn_operation_id")
            or reservation.parent_cancel_epoch != run.cancel_epoch
            or reservation.reserved_budget != run.budget_limit
            or reservation.status not in {"reserved", "settled"}
            or run.budget_snapshot_ref != _reservation_uri(run.turn_id, reservation.reservation_id)
        ):
            raise AgentCoordinatorError("agent binding durable topology drifted")
        return dict(binding)

    def authorize_agent_capability(
        self, request: Mapping[str, object], capability_id: str,
    ) -> Mapping[str, object]:
        """Attest one host-internal capability against its durable Agent Run."""

        if (
            not isinstance(capability_id, str)
            or capability_id not in AGENT_CAPABILITY_IDS
        ):
            raise AgentCoordinatorError("host Agent capability identity is invalid")
        binding = request.get("agent_binding")
        if not isinstance(binding, Mapping):
            raise AgentCoordinatorError("host Agent capability requires a binding")
        verified = self.verify_agent_binding(request, binding)
        project_id = _project_id(validate_turn_request(request))
        found = self._store.get_run_by_turn_id(
            str(request.get("turn_id")), project_id=project_id,
        )
        if found is None:
            raise AgentCoordinatorError("host Agent capability Run is unavailable")
        _require_agent_capability(found[0], capability_id)
        return verified

    def reconcile_terminal_turn(self, turn_id: str) -> Mapping[str, object] | None:
        """Converge one already-terminal Agent Turn without advancing it."""
        request = validate_turn_request(self._request_loader(turn_id))
        project_id = _project_id(request)
        found = self._store.get_run_by_turn_id(turn_id, project_id=project_id)
        if found is None:
            return None
        run, _ = found
        if run.role not in {"main", "subagent"}:
            return None
        events = tuple(self._events_loader(turn_id))
        terminal = next(
            (
                event for event in reversed(events)
                if isinstance(event, Mapping)
                and event.get("type") in {"turn.completed", "turn.failed", "turn.cancelled"}
            ),
            None,
        )
        status = _terminal_status(terminal)
        if status is None:
            return None
        receipt = self._runtime.receipt_for(turn_id)
        receipt_ref = self._persist_terminal_receipt(receipt, status=status)
        refs = _terminal_refs(events, self._payload_loader)
        usage = _observed_usage(events, self._payload_loader, run.budget_limit)
        converged = replace(
            run, status=status, model_routing_snapshot_ref=refs["model"],
            capability_manifest_ref=refs["capability"], context_manifest_ref=refs["context"],
            terminal_receipt_ref=receipt_ref,
        )
        if run.role == "main":
            main, changed = self._store.converge_terminal_main(
                converged, operation_id=f"terminal-main-converge-{turn_id}",
            )
            return _operation_result(
                "terminal main Turn reconciled", changed=changed,
                run=_safe_run(main),
            )
        run, link, reservation, changed = self._store.converge_terminal_child(
            converged, usage=usage, operation_id=f"terminal-converge-{turn_id}",
        )
        parent = self._require_run(link.parent_run_id, run.project_id)
        self._reconcile_parent_fan_ins(parent)
        return _operation_result(
            "terminal child Turn reconciled", changed=changed, run=_safe_run(run),
            link_id=link.link_id, reservation_id=reservation.reservation_id,
        )

    def _persist_terminal_receipt(self, receipt: TurnReceipt, *, status: str) -> str:
        if (
            not isinstance(receipt, TurnReceipt)
            or receipt.status != status
            or self._terminal_receipt_writer is None
        ):
            raise AgentCoordinatorError("authoritative terminal receipt is unavailable")
        payload = {
            "schema_version": "1.0.0", "kind": "agent.turn-terminal-receipt.v1",
            "turn_id": receipt.turn_id, "session_id": receipt.session_id,
            "operation_id": receipt.operation_id, "status": receipt.status,
            "sequence": receipt.current_sequence, "replayed": receipt.replayed,
        }
        ref = self._terminal_receipt_writer(receipt.turn_id, payload)
        if not isinstance(ref, str) or not ref.startswith("crp://"):
            raise AgentCoordinatorError("terminal receipt authority returned an invalid ref")
        return ref

    def _reconcile_parent_fan_ins(self, parent: AgentRun) -> None:
        for fan_in in self._store.list_fan_ins(
            project_id=parent.project_id, parent_run_id=parent.run_id,
        ):
            self._reconcile_fan_in(fan_in, parent)

    def _reconcile_fan_in(
        self, fan_in: AgentFanIn, parent: AgentRun,
    ) -> AgentFanInResult | None:
        existing = self._store.get_fan_in_result(
            fan_in.fan_in_id, project_id=parent.project_id,
        )
        if existing is not None:
            return existing
        if fan_in.status not in {"open", "collecting"}:
            return None
        children: list[AgentRun] = []
        for child_run_id in fan_in.child_run_ids:
            found = self._store.get_run_with_revision(
                child_run_id, project_id=parent.project_id,
            )
            if found is None or found[0].parent_run_id != parent.run_id:
                raise AgentCoordinatorError("fan-in child topology is unavailable")
            children.append(found[0])
        terminal = [child for child in children if child.is_terminal]
        completed = [child for child in terminal if child.status == "completed"]
        selected: list[AgentRun]
        status: str
        if fan_in.policy == "all":
            if len(terminal) != len(children):
                return None
            selected, status = terminal, ("completed" if len(completed) == len(children) else "failed")
        elif fan_in.policy == "any":
            if completed:
                selected, status = [completed[0]], "completed"
            elif len(terminal) == len(children):
                selected, status = terminal, "failed"
            else:
                return None
        else:
            quorum = int(fan_in.quorum or 0)
            if len(completed) >= quorum:
                selected, status = completed[:quorum], "completed"
            elif len(terminal) == len(children):
                selected, status = terminal, "failed"
            else:
                return None
        summaries = tuple(self._terminal_child_summary(child, parent) for child in selected)
        receipt_ref = self._write_immutable(
            parent.turn_id, f"agent-fan-in-receipt-v1/{fan_in.fan_in_id}",
            {"schema_version": "1.0.0", "kind": "agent.fan-in-receipt.v1",
             "fan_in_id": fan_in.fan_in_id, "parent_run_id": parent.run_id,
             "status": status, "child_run_ids": [item.child_run_id for item in summaries]},
        )
        result_ref = self._write_immutable(
            parent.turn_id, f"agent-fan-in-result-v1/{fan_in.fan_in_id}",
            {"schema_version": "1.0.0", "kind": "agent.fan-in-result.v1",
             "fan_in_id": fan_in.fan_in_id, "status": status,
             "children": [_safe_terminal_summary(item, self._payload_loader, parent.turn_id) for item in summaries]},
        )
        result = AgentFanInResult(
            _derived_id("fan-in-result", fan_in.fan_in_id), fan_in.fan_in_id,
            parent.project_id, parent.run_id, status, summaries, receipt_ref, result_ref,
        )
        completed_result, _ = self._store.complete_fan_in(
            result, operation_id=f"fan-in-converge-{fan_in.fan_in_id}",
            expected_cancel_epoch=parent.cancel_epoch,
        )
        return completed_result

    def _terminal_child_summary(
        self, child: AgentRun, parent: AgentRun,
    ) -> AgentTerminalChildSummary:
        if child.terminal_receipt_ref is None:
            raise AgentCoordinatorError("terminal child receipt is unavailable")
        events = tuple(self._events_loader(child.turn_id))
        terminal = events[-1] if events else None
        data = terminal.get("data") if isinstance(terminal, Mapping) else None
        final_summary = _child_conclusion(
            data.get("summary") if isinstance(data, Mapping) else None,
        )
        error_code = data.get("error_code") if isinstance(data, Mapping) else None
        if not isinstance(error_code, str) or not _ERROR_CODE.fullmatch(error_code):
            error_code = None
        if child.status == "completed":
            error_code = None
        elif error_code is None:
            error_code = "ai.execution_failed"
        usage = _observed_usage(events, self._payload_loader, child.budget_limit)
        public = {
            "schema_version": "1.0.0", "kind": "agent.child-terminal-summary.v1",
            "child_run_id": child.run_id, "status": child.status,
            "profile_id": child.profile_id, "final_summary": final_summary,
            "error_code": error_code, "usage": _budget_projection(usage),
        }
        self._write_immutable(
            child.turn_id, f"agent-child-terminal-summary-v1/{child.run_id}", public,
        )
        summary_ref = self._write_immutable(
            parent.turn_id, f"agent-child-terminal-summary-v1/{child.run_id}",
            {key: value for key, value in public.items() if key != "usage"},
        )
        return AgentTerminalChildSummary(
            child.run_id, child.project_id, child.status, child.terminal_receipt_ref,
            summary_ref, (), usage, error_code,
        )

    def _write_immutable(
        self, turn_id: str, kind: str, payload: Mapping[str, object],
    ) -> str:
        writer = self._immutable_payload_writer
        if writer is None:
            raise AgentCoordinatorError("immutable fan-in payload authority is unavailable")
        ref = writer(turn_id, kind, payload)
        if not isinstance(ref, str) or not ref.startswith("crp://"):
            raise AgentCoordinatorError("immutable fan-in payload authority returned an invalid ref")
        return ref

    def reconcile_recoverable(self, turn_id: str) -> Mapping[str, object] | None:
        """Recovery entry point: terminal streams converge; others stay untouched."""
        request = self._request_loader(turn_id)
        if not isinstance(request, Mapping):
            return None
        try:
            payload = validate_turn_request(request)
        except Exception:
            return None
        found = self._store.get_run_by_turn_id(turn_id, project_id=_project_id(payload))
        return self.reconcile_recovery_candidate(found[0]) if found is not None else None

    def reconcile_recovery_candidate(self, candidate: AgentRun) -> Mapping[str, object]:
        """Repair only durable spawn windows; never resume a Turn or effect."""
        current = self._store.get_run_with_revision(candidate.run_id, project_id=candidate.project_id)
        if current is None or current[0] != candidate or candidate.role != "subagent" or candidate.is_terminal:
            raise AgentCoordinatorError("recovery candidate snapshot is unavailable")
        run = current[0]
        links = self._store.list_child_links(project_id=run.project_id, parent_run_id=run.parent_run_id)
        link = next((item for item in links if item.child_run_id == run.run_id), None)
        if link is None:
            raise AgentCoordinatorError("recovery candidate topology is unavailable")
        reservation = self._store.get_reservation(_reservation_id_for_child(run, self._store), project_id=run.project_id)
        if reservation is None:
            raise AgentCoordinatorError("recovery candidate reservation is unavailable")
        try:
            request = self._request_loader(run.turn_id)
            exists = isinstance(request, Mapping) and bool(request)
        except Exception:
            exists = False
        if link.status == "reserved" and not exists:
            self._store.abort_reserved_spawn(
                link.link_id, reservation.reservation_id,
                operation_id=f"recovery-abort-{run.turn_id}", expected_cancel_epoch=run.cancel_epoch,
            )
            return _operation_result("reserved child spawn aborted", repaired=True, run_id=run.run_id)
        if link.status == "reserved" and exists:
            self._store.finalize_spawn(
                link.link_id, operation_id=f"recovery-finalize-{run.turn_id}", expected_cancel_epoch=run.cancel_epoch,
            )
            return _operation_result("accepted child spawn finalized", repaired=True, run_id=run.run_id)
        terminal = self.reconcile_terminal_turn(run.turn_id)
        if terminal is not None:
            return terminal
        return _operation_result("recovery candidate left for Turn recovery", repaired=False, run_id=run.run_id)

    def _parent_for_turn(self, turn_id: str) -> tuple[AgentRun, int]:
        request = validate_turn_request(self._request_loader(turn_id))
        run = self._store.get_run_by_turn_id(turn_id, project_id=_project_id(request))
        if run is None:
            raise AgentCoordinatorError("turn is not a governed agent run")
        return run

    def _require_run(self, run_id: str, project_id: str) -> AgentRun:
        found = self._store.get_run_with_revision(run_id, project_id=project_id)
        if found is None:
            raise AgentCoordinatorError("agent run is not in project scope")
        return found[0]

    def _require_profile(self, profile_id: str, *, role: str) -> AgentProfile:
        profile = self._profiles.get(profile_id)
        if profile is None or not profile.enabled or profile.role != role:
            raise AgentCoordinatorError("agent profile is unavailable for this role")
        return profile

    def _child_turn_request(self, parent_turn_id: str, parent: AgentRun, child: AgentRun, link: AgentChildLink, reservation: AgentBudgetReservation, request: SpawnRequest, policy: _FrozenPolicyAdmission | None = None) -> dict[str, object]:
        parent_request = validate_turn_request(self._request_loader(parent_turn_id))
        payload = dict(parent_request)
        payload.update({
            "turn_id": child.turn_id, "session_id": request.child_session_id,
            "operation_id": request.operation_id, "idempotency_key": request.idempotency_key,
            "input": {"kind": "text", "text": request.input_text, "refs": _bounded_refs(parent_request, request.input_refs)},
            "desired_outcome": ("project.task" if parent_request["desired_outcome"] == "project.task"
                                else "agent.child.execute"),
            "capability_policy": {
                "allowed": list(child.capability_ids), "denied": list(sorted(set(parent.capability_ids) - set(child.capability_ids))),
                "require_approval": [item for item in parent_request["capability_policy"]["require_approval"] if item in child.capability_ids],
            },
            "agent_binding": {
                **_agent_binding_from_run(child),
                "parent_run_id": parent.run_id, "link_id": link.link_id,
                "reservation_id": reservation.reservation_id, "spawn_operation_id": link.spawn_operation_id,
            },
        })
        if policy is not None:
            payload = _apply_policy_to_request(payload, policy)
        return validate_turn_request(payload)

    def _freeze_policy_for_new_turn(
        self, *, project_id: str, turn_id: str, role: str, profile: AgentProfile,
    ) -> _FrozenPolicyAdmission | None:
        authority = self._agent_policy_snapshots
        if authority is None:
            return None
        freeze = getattr(authority, "freeze_for_turn", None)
        if callable(freeze):
            snapshot = freeze(project_id=project_id, turn_id=turn_id)
        else:
            freeze = getattr(authority, "freeze_for_new_turn", None)
            if not callable(freeze):
                raise AgentCoordinatorError("agent policy snapshot authority is unavailable")
            snapshot = freeze(self._agent_policy_id, project_id=project_id, turn_id=turn_id)
        policy_id = _policy_value(snapshot, "policy_id")
        revision = _policy_value(snapshot, "selected_revision")
        if not isinstance(policy_id, str) or not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
            raise AgentCoordinatorError("agent policy snapshot is invalid")
        resolver = getattr(authority, "get_revision", None)
        policy = resolver(policy_id, revision) if callable(resolver) else _policy_value(snapshot, "policy")
        if policy is None:
            raise AgentCoordinatorError("agent policy revision is unavailable")
        _require_policy_role_and_profile(policy, role=role, profile_id=profile.profile_id)
        snapshot_payload = {
            "schema_version": "1.0.0", "kind": "agent.policy.snapshot.v1",
            "policy_id": policy_id, "revision": revision,
            "policy": _policy_payload(policy),
        }
        ref = self._immutable_reference(turn_id, "agent-policy-snapshot-v1", snapshot_payload)
        return _FrozenPolicyAdmission(turn_id, policy_id, revision, ref, policy, snapshot_payload)

    def _immutable_reference(
        self, turn_id: str, kind: str, payload: Mapping[str, object],
    ) -> str:
        resolver = self._immutable_payload_reference
        if resolver is None:
            # Existing in-memory and external coordinator embeddings do not
            # expose a read-only reference resolver.  They retain the legacy
            # writer behavior; the SQLite composition supplies the resolver
            # so it can respect the immutable payload foreign key.
            return self._write_immutable(turn_id, kind, payload)
        ref = resolver(turn_id, kind)
        if not isinstance(ref, str) or not ref.startswith("crp://"):
            raise AgentCoordinatorError("immutable payload reference authority returned an invalid ref")
        return ref

    def _persist_policy_snapshot(self, admission: _FrozenPolicyAdmission | None) -> None:
        if admission is None:
            return
        stored_ref = self._write_immutable(
            admission.turn_id, "agent-policy-snapshot-v1", admission.snapshot_payload,
        )
        if stored_ref != admission.snapshot_ref:
            raise AgentCoordinatorError("agent policy immutable snapshot reference drifted")

    def _verify_policy_binding(self, request: Mapping[str, object], project_id: str) -> None:
        binding = request.get("agent_policy_binding")
        authority = self._agent_policy_snapshots
        if binding is None:
            return
        if not isinstance(binding, Mapping):
            raise AgentCoordinatorError("agent policy binding is invalid")
        if authority is None:
            raise AgentCoordinatorError("agent policy snapshot authority is unavailable")
        if self._payload_loader is None:
            raise AgentCoordinatorError("agent policy payload authority is unavailable")
        snapshot_ref = binding.get("snapshot_ref")
        if not isinstance(snapshot_ref, str):
            raise AgentCoordinatorError("agent policy snapshot reference is invalid")
        try:
            frozen_payload = self._payload_loader(snapshot_ref)
        except Exception as error:
            raise AgentCoordinatorError(
                "agent policy immutable snapshot is unavailable"
            ) from error
        if (
            not isinstance(frozen_payload, Mapping)
            or set(frozen_payload) != {
                "schema_version", "kind", "policy_id", "revision", "policy",
            }
            or frozen_payload.get("schema_version") != "1.0.0"
            or frozen_payload.get("kind") != "agent.policy.snapshot.v1"
            or frozen_payload.get("policy_id") != binding.get("policy_id")
            or frozen_payload.get("revision") != binding.get("revision")
        ):
            raise AgentCoordinatorError("agent policy immutable snapshot drifted")
        load = getattr(authority, "load_turn_snapshot", None)
        if not callable(load):
            return
        try:
            snapshot = load(str(binding["policy_id"]), project_id=project_id, turn_id=str(request["turn_id"]))
        except TypeError:
            snapshot = load(project_id=project_id, turn_id=str(request["turn_id"]))
        if (
            snapshot is None
            or _policy_value(snapshot, "policy_id") != binding.get("policy_id")
            or _policy_value(snapshot, "selected_revision") != binding.get("revision")
        ):
            raise AgentCoordinatorError("agent policy snapshot drifted")
        resolver = getattr(authority, "get_revision", None)
        policy = (
            resolver(str(binding["policy_id"]), int(binding["revision"]))
            if callable(resolver) else _policy_value(snapshot, "policy")
        )
        if policy is None or frozen_payload.get("policy") != _policy_payload(policy):
            raise AgentCoordinatorError("agent policy revision snapshot drifted")

    def _bound_policy(
        self, request: Mapping[str, object], project_id: str,
    ) -> object | None:
        binding = request.get("agent_policy_binding")
        if binding is None:
            return None
        self._verify_policy_binding(request, project_id)
        if not isinstance(binding, Mapping) or self._payload_loader is None:
            raise AgentCoordinatorError("agent policy binding is invalid")
        payload = self._payload_loader(str(binding.get("snapshot_ref")))
        if not isinstance(payload, Mapping):
            raise AgentCoordinatorError("agent policy immutable snapshot is unavailable")
        return payload.get("policy")


def _project_id(request: Mapping[str, object]) -> str:
    scope = request.get("scope")
    project_id = scope.get("project_id") if isinstance(scope, Mapping) else None
    if not isinstance(project_id, str):
        raise AgentCoordinatorError("agent coordination requires a project-scoped Turn")
    return project_id


def _run_from_profile(*, run_id: str, turn_id: str, project_id: str, profile: AgentProfile, role: str, depth: int, parent_run_id: str | None, status: str) -> AgentRun:
    return AgentRun(run_id, turn_id, project_id, profile.profile_id, profile.revision, role, profile.model_tier, status, depth, 0, profile.budget_limit, profile.capability_ids, profile.max_concurrent_children, profile.max_depth, profile.max_steps, profile.timeout_ms, profile.allow_child_spawn, None, None, None, None, None, parent_run_id, profile.model_route_key, profile.model_route_revision)


def _child_run(parent: AgentRun, profile: AgentProfile, request: SpawnRequest) -> AgentRun:
    capabilities = tuple(sorted(set(parent.capability_ids) & set(profile.capability_ids) & set(request.requested_capability_ids)))
    budget = _minimum_budget(parent.budget_limit, profile.budget_limit, request.requested_budget)
    allow_spawn = bool(request.allow_child_spawn and profile.allow_child_spawn and parent.allow_child_spawn)
    concurrency = min(parent.max_concurrent_children, profile.max_concurrent_children, request.requested_max_concurrent_children) if allow_spawn else 0
    depth_limit = min(parent.max_depth, profile.max_depth, request.requested_max_depth) if allow_spawn else min(parent.max_depth, profile.max_depth)
    budget_ref = _reservation_uri(request.child_turn_id, request.reservation_id)
    return AgentRun(request.child_run_id, request.child_turn_id, parent.project_id, profile.profile_id, profile.revision, "subagent", profile.model_tier, "queued", parent.depth + 1, parent.cancel_epoch, budget, capabilities, concurrency, depth_limit, min(parent.max_steps, profile.max_steps, request.requested_max_steps), min(parent.timeout_ms, profile.timeout_ms, request.requested_timeout_ms), allow_spawn, None, None, None, budget_ref, None, parent.run_id, profile.model_route_key, profile.model_route_revision)


def _policy_value(value: object, name: str) -> object:
    return value.get(name) if isinstance(value, Mapping) else getattr(value, name, None)


def _policy_payload(policy: object) -> object:
    render = getattr(policy, "to_payload", None)
    value = render() if callable(render) else policy
    try:
        # Policy snapshots cross the SQLite JSON boundary.  Dataclass
        # ``asdict`` retains tuples in memory while JSON restores them as
        # lists, so compare and persist one canonical JSON-compatible shape
        # rather than treating that representation change as a policy change.
        canonical = json.loads(json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ))
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise AgentCoordinatorError("agent policy payload is not JSON-compatible") from error
    if not isinstance(canonical, Mapping):
        raise AgentCoordinatorError("agent policy payload is invalid")
    return dict(canonical)


def _require_policy_role_and_profile(policy: object, *, role: str, profile_id: str) -> None:
    roles = _policy_value(policy, "target_roles")
    routing = _policy_value(policy, "routing")
    profiles = _policy_value(routing, "profile_ids")
    if (
        not isinstance(roles, tuple) or role not in roles
        or not isinstance(profiles, tuple) or profile_id not in profiles
    ):
        raise AgentCoordinatorError("agent policy excludes this role or profile")


def _apply_policy_to_run(run: AgentRun, policy: object | None) -> AgentRun:
    if policy is None:
        return run
    _require_policy_role_and_profile(policy, role=run.role, profile_id=run.profile_id)
    scheduler = _policy_value(policy, "scheduler")
    mode = _policy_value(scheduler, "cluster_mode")
    assignments = _policy_value(scheduler, "max_assignments")
    parallelism = _policy_value(scheduler, "parallelism_cap")
    prefer_main = _policy_value(scheduler, "prefer_main_only")
    if not isinstance(assignments, int) or not isinstance(parallelism, int) or not isinstance(prefer_main, bool):
        raise AgentCoordinatorError("agent policy scheduler is invalid")
    allow_spawn = bool(
        run.allow_child_spawn and mode != "main_only" and not prefer_main
        and assignments > 0 and parallelism > 0
    )
    concurrent = min(run.max_concurrent_children, assignments, parallelism) if allow_spawn else 0
    capabilities = run.capability_ids if allow_spawn else tuple(
        capability for capability in run.capability_ids if capability != "agent.spawn"
    )
    return replace(run, allow_child_spawn=allow_spawn, max_concurrent_children=concurrent, capability_ids=capabilities)


def _apply_policy_to_request(
    request: Mapping[str, object], admission: _FrozenPolicyAdmission,
) -> dict[str, object]:
    payload = dict(request)
    policy = admission.policy
    context = _policy_value(policy, "context")
    current = payload.get("context_policy")
    if not isinstance(current, Mapping):
        raise AgentCoordinatorError("turn context policy is unavailable")
    context_keys = ("include_project_skill", "include_memory", "include_session_history")
    restricted = dict(current)
    for key in context_keys:
        value = _policy_value(context, key)
        if not isinstance(value, bool):
            raise AgentCoordinatorError("agent policy context is invalid")
        restricted[key] = bool(current.get(key)) and value
    max_bytes = _policy_value(context, "max_context_bytes")
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool):
        raise AgentCoordinatorError("agent policy context is invalid")
    current_max = current.get("max_context_bytes")
    if not isinstance(current_max, int) or isinstance(current_max, bool):
        raise AgentCoordinatorError("turn context policy is unavailable")
    restricted["max_context_bytes"] = min(current_max, max_bytes)
    payload["context_policy"] = restricted
    scheduler = _policy_value(policy, "scheduler")
    allowed_experts = _policy_value(scheduler, "allowed_expert_ids")
    allowed_skills = _policy_value(scheduler, "allowed_skill_ids")
    expert = payload.get("expert_request")
    if expert is not None:
        if not isinstance(expert, Mapping):
            raise AgentCoordinatorError("turn expert request is invalid")
        expert_id = expert.get("expert_id")
        skill_ids = expert.get("skill_ids", ())
        if (
            expert_id is not None and (not isinstance(allowed_experts, tuple) or expert_id not in allowed_experts)
            or not isinstance(skill_ids, list) or not isinstance(allowed_skills, tuple)
            or any(skill not in allowed_skills for skill in skill_ids)
        ):
            raise AgentCoordinatorError("agent policy excludes requested expert or skill")
    payload["agent_policy_binding"] = {
        "policy_id": admission.policy_id,
        "revision": admission.revision,
        "snapshot_ref": admission.snapshot_ref,
    }
    return validate_turn_request(payload)


def _validate_steward_proposal_policy(
    proposal: Mapping[str, object], policy: object | None,
) -> None:
    """Apply the frozen scheduler ceiling before durable permits are created."""

    if policy is None or proposal.get("mode") != "cluster":
        return
    scheduler = _policy_value(policy, "scheduler")
    mode = _policy_value(scheduler, "cluster_mode")
    assignments_cap = _policy_value(scheduler, "max_assignments")
    allowed_experts = _policy_value(scheduler, "allowed_expert_ids")
    allowed_skills = _policy_value(scheduler, "allowed_skill_ids")
    prefer_main = _policy_value(scheduler, "prefer_main_only")
    assignments = proposal.get("assignments")
    if (
        mode == "main_only"
        or prefer_main is True
        or not isinstance(assignments_cap, int)
        or isinstance(assignments_cap, bool)
        or not isinstance(assignments, list)
        or len(assignments) > assignments_cap
        or not isinstance(allowed_experts, (tuple, list))
        or not isinstance(allowed_skills, (tuple, list))
    ):
        raise AgentCoordinatorError("agent policy excludes steward cluster plan")
    expert_ceiling, skill_ceiling = set(allowed_experts), set(allowed_skills)
    for assignment in assignments:
        if not isinstance(assignment, Mapping):
            continue
        expert = assignment.get("expert")
        if (
            isinstance(expert, Mapping)
            and expert.get("expert_id") not in expert_ceiling
        ):
            raise AgentCoordinatorError("agent policy excludes steward expert")
        skill = assignment.get("skill")
        skill_ids = skill.get("skill_ids") if isinstance(skill, Mapping) else ()
        if isinstance(skill_ids, list) and any(
            skill_id not in skill_ceiling for skill_id in skill_ids
        ):
            raise AgentCoordinatorError("agent policy excludes steward skill")


def _minimum_budget(*budgets: AgentBudget) -> AgentBudget:
    return AgentBudget(*(min(getattr(value, field) for value in budgets) for field in ("model_calls", "tool_calls", "input_tokens", "output_tokens", "wall_time_ms")))


def _permit_child_ids(permit: DispatchPermit, operation_id: str) -> dict[str, str]:
    if not isinstance(operation_id, str) or not operation_id:
        raise AgentCoordinatorError("agent permit operation identity is invalid")
    seed = f"{permit.project_id}:{permit.permit_id}:{permit.assignment_id}"
    return {
        "run_id": _derived_id("permit-run", seed),
        "turn_id": "turn-" + uuid5(NAMESPACE_URL, f"permit-turn:{seed}").hex,
        "link_id": _derived_id("permit-link", seed),
        "reservation_id": _derived_id("permit-reservation", seed),
        "session_id": _derived_id("permit-session", seed),
        "idempotency_key": _derived_id("permit-idempotency", seed),
    }


def _profile_id_from_ref(reference: str) -> str:
    prefix = "crp://agent/profiles/"
    if not isinstance(reference, str) or not reference.startswith(prefix):
        raise AgentCoordinatorError("agent assignment profile reference is invalid")
    profile_id = reference[len(prefix):]
    if not profile_id or "/" in profile_id:
        raise AgentCoordinatorError("agent assignment profile reference is invalid")
    return profile_id


def _validate_assignment_snapshots(assignment: ExpertAssignment) -> None:
    def require_pair(reference: str | None, revision: int | None, label: str) -> None:
        if not isinstance(reference, str) or not reference.startswith("crp://") or not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
            raise AgentCoordinatorError(f"agent assignment {label} snapshot is invalid")

    if assignment.expert_id is None:
        if assignment.expert_snapshot_ref is not None or assignment.expert_revision is not None:
            raise AgentCoordinatorError("agent assignment expert snapshot drifted")
    else:
        require_pair(assignment.expert_snapshot_ref, assignment.expert_revision, "expert")
    if assignment.skill_ids:
        require_pair(assignment.skill_snapshot_ref, assignment.skill_revision, "skill")
    elif assignment.skill_snapshot_ref is not None or assignment.skill_revision is not None:
        raise AgentCoordinatorError("agent assignment skill snapshot drifted")


def _bounded_refs(request: Mapping[str, object], requested: tuple[str, ...] = ()) -> list[object]:
    source = request.get("input")
    refs = source.get("refs") if isinstance(source, Mapping) else None
    parent_refs = list(refs) if isinstance(refs, list) else []
    if not requested:
        return []
    if not set(requested).issubset({str(item.get("uri")) for item in parent_refs if isinstance(item, Mapping)}):
        raise AgentCoordinatorError("child input references must be a parent reference subset")
    return [item for item in parent_refs if isinstance(item, Mapping) and item.get("uri") in requested]


def _safe_run(run: AgentRun) -> Mapping[str, object]:
    return {"run_id": run.run_id, "turn_id": run.turn_id, "profile_id": run.profile_id, "role": run.role, "model_tier": run.model_tier, "status": run.status, "depth": run.depth, "capability_ids": run.capability_ids}


def _safe_dispatch_plan(plan: AgentDispatchPlan) -> Mapping[str, object]:
    """Expose only dispatch progress needed by the owning main Agent."""
    return {
        "plan_id": plan.plan_id,
        "mode": plan.mode,
        "status": plan.status,
        "revision": plan.revision,
        "steward_run_id": plan.steward_run_id,
    }


def _safe_receipt(receipt: TurnReceipt | None) -> Mapping[str, object] | None:
    if receipt is None:
        return None
    return {
        "turn_id": receipt.turn_id, "operation_id": receipt.operation_id,
        "status": receipt.status, "sequence": receipt.current_sequence,
        "replayed": receipt.replayed,
    }


def _safe_message(message: AgentMessage) -> Mapping[str, object]:
    return {
        "message_id": message.message_id,
        "sender_run_id": message.sender_run_id,
        "recipient_run_id": message.recipient_run_id,
        "sequence": message.sequence,
        "kind": message.kind,
        "payload_ref": message.payload_ref,
        "status": message.status,
    }


def _budget_projection(value: AgentBudget) -> Mapping[str, int]:
    return {
        "model_calls": value.model_calls,
        "tool_calls": value.tool_calls,
        "input_tokens": value.input_tokens,
        "output_tokens": value.output_tokens,
        "wall_time_ms": value.wall_time_ms,
    }


def _child_conclusion(value: object) -> str | None:
    """Retain bounded conclusions while removing credentials and locators."""
    if not isinstance(value, str):
        return None
    clean = value.strip()
    clean = re.sub(r"(?i)authorization[ \t]*:[ \t]*(?:Bearer[ \t]+)?[^\s,;，；]+", "[已移除]", clean)
    clean = re.sub(r"(?i)\bBearer[ \t]+[^\s,;，；]+|\bsk-[A-Za-z0-9_-]+", "[已移除]", clean)
    clean = re.sub(r"(?i)(?:crp|file)://[^\s，；,;]+", "[路径]", clean)
    clean = re.sub(r"(?:[A-Za-z]:[\\/]|\\\\|[\w.~-]+[\\/])[^\s，；,;]+", "[路径]", clean)
    lines = [" ".join(line.split()) for line in clean.splitlines()]
    clean = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    if not clean:
        return None
    return clean if len(clean) <= 2000 else clean[:1999] + "…"


def _safe_terminal_summary(
    value: AgentTerminalChildSummary,
    payload_loader: Callable[[str], object] | None = None,
    parent_turn_id: str | None = None,
) -> Mapping[str, object]:
    conclusion = None
    if callable(payload_loader) and parent_turn_id and value.summary_ref.startswith(f"crp://session/{parent_turn_id}/"):
        try:
            payload = payload_loader(value.summary_ref)
        except Exception:
            payload = None
        if (isinstance(payload, Mapping)
                and payload.get("kind") == "agent.child-terminal-summary.v1"
                and payload.get("child_run_id") == value.child_run_id
                and payload.get("status") == value.status):
            conclusion = _child_conclusion(payload.get("final_summary"))
    return {
        "child_run_id": value.child_run_id,
        "status": value.status,
        "summary_ref": value.summary_ref,
        "conclusion": conclusion,
        "error_code": value.error_code,
        "usage": _budget_projection(value.usage),
    }


def _safe_fan_in(
    fan_in: AgentFanIn, result: AgentFanInResult | None,
    payload_loader: Callable[[str], object] | None = None,
    parent_turn_id: str | None = None,
) -> Mapping[str, object]:
    payload: dict[str, object] = {
        "fan_in_id": fan_in.fan_in_id,
        "status": fan_in.status,
        "policy": fan_in.policy,
        "child_run_ids": fan_in.child_run_ids,
    }
    if result is not None:
        payload["result"] = {
            "status": result.status,
            "result_ref": result.result_ref,
            "receipt_ref": result.receipt_ref,
            "children": tuple(_safe_terminal_summary(item, payload_loader, parent_turn_id) for item in result.child_summaries),
        }
    return payload


def _fan_in_operation_result(
    result: AgentFanInResult,
    payload_loader: Callable[[str], object] | None = None,
    parent_turn_id: str | None = None,
) -> Mapping[str, object]:
    return _operation_result(
        "fan-in completed", fan_in_id=result.fan_in_id, status=result.status,
        result_ref=result.result_ref,
        children=tuple(_safe_terminal_summary(item, payload_loader, parent_turn_id) for item in result.child_summaries),
    )


def _text_argument(arguments: Mapping[str, object], name: str) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value.strip():
        raise AgentCoordinatorError(f"agent operation requires {name}")
    return value.strip()


def _spawn_request_from_arguments(arguments: Mapping[str, object] | None, *, parent: AgentRun, operation_id: str | None, tool_call_id: str | None) -> SpawnRequest:
    if arguments is None or operation_id is None or tool_call_id is None:
        raise AgentCoordinatorError("agent spawn arguments are unavailable")
    profile_id = _text_argument(arguments, "profile_id")
    budget_value = arguments.get("budget", {})
    if not isinstance(budget_value, Mapping):
        raise AgentCoordinatorError("agent spawn budget is invalid")
    base_budget = parent.budget_limit
    try:
        budget = AgentBudget(*(budget_value.get(field, getattr(base_budget, field)) for field in ("model_calls", "tool_calls", "input_tokens", "output_tokens", "wall_time_ms")))
    except (KeyError, TypeError, ValueError) as error:
        raise AgentCoordinatorError("agent spawn budget is invalid") from error
    capabilities = arguments.get("capability_ids", list(parent.capability_ids))
    if not isinstance(capabilities, Sequence) or isinstance(capabilities, str) or not all(isinstance(item, str) for item in capabilities):
        raise AgentCoordinatorError("agent spawn capabilities are invalid")
    input_refs = arguments.get("input_refs", ())
    if not isinstance(input_refs, Sequence) or isinstance(input_refs, str) or not all(isinstance(item, str) for item in input_refs):
        raise AgentCoordinatorError("agent spawn input references are invalid")
    seed = f"{parent.turn_id}:{operation_id}:{tool_call_id}"
    return SpawnRequest(
        operation_id=operation_id, child_run_id=_derived_id("run", seed), child_turn_id="turn-" + uuid5(NAMESPACE_URL, f"turn:{seed}").hex,
        link_id=_derived_id("link", seed), reservation_id=_derived_id("reservation", seed), profile_id=profile_id,
        child_session_id=_derived_id("session", seed), idempotency_key=_derived_id("idempotency", seed),
        input_text=_text_argument(arguments, "task"), requested_capability_ids=tuple(capabilities),
        requested_budget=budget, requested_max_steps=parent.max_steps, requested_timeout_ms=parent.timeout_ms,
        requested_max_concurrent_children=parent.max_concurrent_children,
        requested_max_depth=parent.max_depth, allow_child_spawn=True,
        input_refs=tuple(input_refs),
    )


def _fan_in_from_arguments(arguments: Mapping[str, object] | None, *, parent: AgentRun, operation_id: str | None, tool_call_id: str | None) -> AgentFanIn:
    if arguments is None or operation_id is None or tool_call_id is None:
        raise AgentCoordinatorError("agent fan-in arguments are unavailable")
    children = arguments.get("child_run_ids")
    if not isinstance(children, Sequence) or isinstance(children, str) or not children or not all(isinstance(item, str) for item in children):
        raise AgentCoordinatorError("agent fan-in children are invalid")
    policy = _text_argument(arguments, "policy")
    quorum = arguments.get("quorum")
    return AgentFanIn(
        _derived_id("fan-in", parent.turn_id, operation_id, tool_call_id),
        parent.project_id, parent.run_id,
        operation_id, tuple(children), policy, quorum if isinstance(quorum, int) and not isinstance(quorum, bool) else None,
        parent.cancel_epoch, "open",
    )


def _assert_native_scope(run: AgentRun, *, project_id: str | None, scope: Mapping[str, object] | None, privacy: Mapping[str, object] | None, request_loader: Callable[[str], Mapping[str, object]], turn_id: str) -> None:
    frozen = validate_turn_request(request_loader(turn_id))
    if project_id != run.project_id:
        raise AgentCoordinatorError("agent operation project scope is invalid")
    if not isinstance(scope, Mapping) or dict(scope) != frozen["scope"]:
        raise AgentCoordinatorError("agent operation scope drifted from frozen Turn")
    if not isinstance(privacy, Mapping) or dict(privacy) != frozen["privacy"]:
        raise AgentCoordinatorError("agent operation privacy drifted from frozen Turn")


def _derived_id(kind: str, *parts: str) -> str:
    return f"{kind}-{uuid5(NAMESPACE_URL, ':'.join(parts)).hex}"


def _reservation_uri(turn_id: str, reservation_id: str) -> str:
    return f"crp://{turn_id}/agent/reservations/{reservation_id}"


def _profile_budget_uri(turn_id: str, profile_id: str, profile_revision: int) -> str:
    return f"crp://{turn_id}/agent/profiles/{profile_id}/revisions/{profile_revision}"


def _reservation_id_for_child(run: AgentRun, _store: AgentCoordinatorStorePort) -> str:
    ref = run.budget_snapshot_ref
    marker = "/reservations/"
    if not isinstance(ref, str) or marker not in ref:
        raise AgentCoordinatorError("recovery candidate reservation reference is invalid")
    reservation_id = ref.rsplit(marker, 1)[1]
    if not reservation_id or "/" in reservation_id:
        raise AgentCoordinatorError("recovery candidate reservation reference is invalid")
    return reservation_id


def _agent_binding_from_run(run: AgentRun) -> dict[str, object]:
    if run.budget_snapshot_ref is None:
        raise AgentCoordinatorError("agent budget snapshot is unavailable")
    binding = {
        "schema_version": "1.0.0",
        "kind": "internal_agent_run_v1",
        "run_id": run.run_id,
        "role": run.role,
        "profile_id": run.profile_id,
        "profile_revision": run.profile_revision,
        "model_tier": run.model_tier,
        "depth": run.depth,
        "cancel_epoch": run.cancel_epoch,
        "budget_snapshot_ref": run.budget_snapshot_ref,
    }
    if run.model_route_key is not None:
        binding["model_route_key"] = run.model_route_key
        binding["model_route_revision"] = run.model_route_revision
    return binding


def _directly_related(sender: AgentRun, recipient: AgentRun) -> bool:
    return sender.parent_run_id == recipient.run_id or recipient.parent_run_id == sender.run_id


def _require_agent_capability(run: AgentRun, capability_id: str) -> None:
    if capability_id not in run.capability_ids:
        raise AgentCoordinatorError("agent operation is outside the frozen capability set")


def _operation_result(summary: str, **result: object) -> Mapping[str, object]:
    return {"summary": summary, **result, "evidence_refs": ()}


def _terminal_status(event: Mapping[str, object] | None) -> str | None:
    if not isinstance(event, Mapping):
        return None
    return {
        "turn.completed": "completed", "turn.failed": "failed", "turn.cancelled": "cancelled",
    }.get(event.get("type"))


def _terminal_refs(
    events: Sequence[Mapping[str, object]], payload_loader: Callable[[str], object] | None,
) -> Mapping[str, str]:
    if payload_loader is None:
        raise AgentCoordinatorError("terminal payload authority is unavailable")
    context_event = next((event for event in reversed(events) if event.get("type") == "context.resolved"), None)
    terminal = events[-1] if events else None
    context_ref = _event_payload_ref(context_event)
    if context_ref is None:
        raise AgentCoordinatorError("terminal Turn is missing governed payload references")
    try:
        context = context_manifest_from_payload(payload_loader(context_ref))
        capability_ref = context.capability_manifest_ref
        manifest = manifest_from_payload(payload_loader(capability_ref))
        model_ref = manifest.model_routing_snapshot_ref
    except Exception as error:
        raise AgentCoordinatorError("terminal Turn frozen authority refs are invalid") from error
    if not isinstance(model_ref, str):
        raise AgentCoordinatorError("terminal Turn model routing snapshot is unavailable")
    return {"context": context_ref, "capability": capability_ref, "model": model_ref}


def _event_payload_ref(event: Mapping[str, object] | None) -> str | None:
    data = event.get("data") if isinstance(event, Mapping) else None
    value = data.get("payload_ref") if isinstance(data, Mapping) else None
    return value if isinstance(value, str) else None


def _observed_usage(
    events: Sequence[Mapping[str, object]], payload_loader: Callable[[str], object] | None,
    limit: AgentBudget,
) -> AgentBudget:
    if payload_loader is None:
        raise AgentCoordinatorError("terminal payload authority is unavailable")
    model_calls = tool_calls = input_tokens = output_tokens = 0
    seen_model_requests: set[str] = set()
    for event in events:
        event_type = event.get("type")
        if event_type in {"model.completed", "model.failed", "model.cancelled", "model.timed_out"}:
            data = event.get("data")
            correlation = event.get("correlation")
            model_request_id = (
                correlation.get("model_request_id") if isinstance(correlation, Mapping) else None
            )
            if not isinstance(model_request_id, str) or model_request_id in seen_model_requests:
                continue
            seen_model_requests.add(model_request_id)
            purpose = data.get("model_call_purpose") if isinstance(data, Mapping) else None
            receipt_ref = data.get("receipt_ref") if isinstance(data, Mapping) else None
            if not isinstance(receipt_ref, str):
                # Kernel terminal events retain purpose even when dispatch or
                # receipt persistence failed. Historical absence is primary.
                if purpose != "aux":
                    model_calls += 1
                continue
            try:
                receipt = validate_model_call_receipt(payload_loader(receipt_ref))
            except Exception:
                model_calls += 1
                continue
            if (receipt.get("model_request_id") != model_request_id
                    or (event.get("turn_id") is not None and receipt.get("turn_id") != event["turn_id"])):
                model_calls += 1
                continue
            if receipt.get("model_call_purpose") == "aux" and purpose in {None, "aux"}:
                continue
            model_calls += 1
            if receipt.get("usage_status") != "recorded":
                continue
            usage = receipt.get("usage")
            if isinstance(usage, Mapping):
                inputs = usage.get("input_tokens")
                outputs = usage.get("output_tokens")
                if isinstance(inputs, int) and not isinstance(inputs, bool):
                    input_tokens += inputs
                if isinstance(outputs, int) and not isinstance(outputs, bool):
                    output_tokens += outputs
            continue
        if event_type in {"tool.completed", "tool.failed", "tool.cancelled"}:
            tool_calls += 1
    return AgentBudget(
        min(model_calls, limit.model_calls), min(tool_calls, limit.tool_calls),
        min(input_tokens, limit.input_tokens), min(output_tokens, limit.output_tokens), 0,
    )
