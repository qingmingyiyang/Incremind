"""Application-facing, provider-free orchestration of the Agent organization.

The organization runtime is intentionally a thin progression layer.  It does
not decide assignments, manufacture dispatch permits, or execute turns.  Those
are respectively the steward/dispatch authority, the dispatch store, and the
existing governed coordinator.  Keeping this boundary explicit prevents a
startup/recovery path from becoming a second scheduler.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from typing import Protocol
from uuid import NAMESPACE_URL, uuid5

from backend.memory_app.kernel.agent_coordinator import (
    AgentCoordinatorError,
    PreparedChildTurn,
    PreparedMainTurn,
    SpawnRequest,
)
from core.ai_kernel import AgentBudget, AgentFanIn, AgentRun, TurnReceipt, validate_turn_request
from core.ai_kernel.agent_dispatch_contracts import AgentDispatchPlan, DispatchPermit


class AgentOrganizationError(RuntimeError):
    """The external organization entrypoint cannot safely progress a plan."""


class OrganizationCoordinatorPort(Protocol):
    def assignment_dependencies(self, *, project_id: str, permit_id: str) -> tuple[str, ...]: ...
    def accept_and_register_main(self, request: Mapping[str, object]) -> PreparedMainTurn: ...
    def submit_accepted_turn(self, prepared: PreparedMainTurn | PreparedChildTurn) -> TurnReceipt: ...
    def resubmit_existing_turn(self, *, turn_id: str, project_id: str) -> TurnReceipt: ...
    def prepare_child(self, *, parent_turn_id: str, request: SpawnRequest, project_id: str, scope: Mapping[str, object], privacy: Mapping[str, object], child_request_factory: Callable[[Mapping[str, object]], Mapping[str, object]] | None = None) -> PreparedChildTurn: ...
    def prepare_child_from_permit(self, *, project_id: str, permit_id: str, operation_id: str) -> PreparedChildTurn: ...
    def fan_in(self, *, parent_turn_id: str, fan_in: AgentFanIn, operation_id: str, project_id: str, scope: Mapping[str, object], privacy: Mapping[str, object]) -> Mapping[str, object]: ...


class OrganizationDispatchStorePort(Protocol):
    def get_permit_child_run_binding(self, permit_id: str, *, project_id: str) -> str | None: ...
    def list_plans_for_main(self, *, project_id: str, main_run_id: str) -> tuple[AgentDispatchPlan, ...]: ...
    def list_permits(self, *, project_id: str, plan_id: str | None = None) -> tuple[DispatchPermit, ...]: ...
    def get_plan(self, plan_id: str, *, project_id: str) -> AgentDispatchPlan | None: ...
    def transition_plan(self, value: AgentDispatchPlan, *, expected_revision: int, operation_id: str) -> AgentDispatchPlan: ...
    def list_recovery_plans(self, *, limit: int = 64) -> tuple[AgentDispatchPlan, ...]: ...


class OrganizationRunStorePort(Protocol):
    def get_run_by_turn_id(self, turn_id: str, *, project_id: str) -> tuple[AgentRun, int] | None: ...
    def get_run_with_revision(self, run_id: str, *, project_id: str) -> tuple[AgentRun, int] | None: ...
    def get_fan_in_result(self, fan_in_id: str, *, project_id: str) -> object | None: ...


_HOST_AGENT_CAPABILITIES = frozenset({
    "agent.spawn", "agent.message", "agent.interrupt", "agent.wait",
    "agent.fan_in", "agent.list", "agent.plan",
})


class AgentOrganizationRuntime:
    """Starts the fixed main → steward organization and progresses permits."""

    def __init__(
        self,
        *,
        coordinator: OrganizationCoordinatorPort,
        dispatch_store: OrganizationDispatchStorePort,
        run_store: OrganizationRunStorePort,
        request_loader: Callable[[str], Mapping[str, object]],
        freshness_gate: Callable[[str], bool] | None = None,
        start_pair_scanner: Callable[[int], Sequence[tuple[AgentRun, AgentRun]]] | None = None,
        recovery_plan_scanner: Callable[[int], Sequence[AgentDispatchPlan]] | None = None,
    ) -> None:
        self._coordinator = coordinator
        self._dispatch_store = dispatch_store
        self._run_store = run_store
        self._request_loader = request_loader
        self._freshness_gate = freshness_gate
        self._start_pair_scanner = start_pair_scanner
        self._recovery_plan_scanner = recovery_plan_scanner

    def start(self, request: Mapping[str, object], *, agent_turn_mode: bool = False) -> Mapping[str, object]:
        """Accept a host-owned external Turn, then submit steward before main.

        Caller-supplied agent routing fields are rejected rather than silently
        overwritten.  The optional mode is an application decision, not a Turn
        request field, so the external Turn contract remains unchanged.
        """
        if any(name in request for name in ("agent_binding", "expert_request", "capability_request")):
            raise AgentOrganizationError("external organization Turn cannot select agent routing")
        payload = validate_turn_request(request)
        if payload["scope"].get("kind") not in {"project", "series"}:
            raise AgentOrganizationError("agent organization requires a project-scoped Turn")
        payload = self._with_host_capabilities(payload) if agent_turn_mode else payload
        try:
            main = self._coordinator.accept_and_register_main(payload)
            steward = self._prepare_steward(main)
            # The main can observe a durable steward identity only after that
            # child was accepted and scheduled.
            self._coordinator.submit_accepted_turn(steward)
            self._coordinator.submit_accepted_turn(main)
        except (AgentCoordinatorError, ValueError, TypeError) as error:
            raise AgentOrganizationError("organization start was rejected") from error
        return {
            "status_code": 202,
            "status": "accepted",
            "main": _safe_run(main.run),
            "steward": _safe_run(steward.run),
        }

    def on_terminal(self, turn_id: str) -> Mapping[str, object]:
        """Progress a terminal steward or converge its cluster fan-in plan."""
        if not isinstance(turn_id, str) or not turn_id:
            raise AgentOrganizationError("organization terminal Turn identity is invalid")
        request = validate_turn_request(self._request_loader(turn_id))
        project_id = _project_id(request)
        found = self._run_store.get_run_by_turn_id(turn_id, project_id=project_id)
        if found is None:
            return {"status": "ignored", "reason": "not_governed"}
        run, _ = found
        if not run.is_terminal or run.role != "subagent" or run.parent_run_id is None:
            return {"status": "ignored", "reason": "not_converged_child"}
        main = self._run(run.parent_run_id, project_id)
        if main.role != "main" or main.parent_run_id is not None:
            return {"status": "ignored", "reason": "not_organization_child"}
        if run.profile_id == "steward.scheduler":
            progressed = self._progress_pair(project_id=project_id, main=main, steward=run)
        else:
            progressed = self._converge_main_plans(project_id=project_id, main=main)
        return {"status": "progressed", "plans": tuple(progressed)}

    def recover(self, limit: int = 64) -> Mapping[str, object]:
        """Boundedly replay registered starts, then advance durable plans.

        The scanner supplies only topology and plan identities.  Replays are
        restricted to frozen, registered Agent Turns; terminal Turns are never
        resubmitted, and dispatch progression keeps its existing permit fence.
        """
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 256:
            raise AgentOrganizationError("organization recovery limit is invalid")
        start_pairs = self._scan_start_pairs(limit)
        plans = self._scan_recovery_plans(limit)
        results: list[Mapping[str, object]] = []
        replayed: list[str] = []
        progressed_plan_ids: set[str] = set()
        for main, steward in start_pairs:
            if not _is_start_pair(main, steward):
                continue
            # Steward first so the main cannot synthesize before its durable
            # placement decision is available.  Both calls are runner replay,
            # never fresh Turn/model authority creation.
            if not steward.is_terminal:
                self._coordinator.resubmit_existing_turn(
                    turn_id=steward.turn_id, project_id=steward.project_id,
                )
                replayed.append(steward.turn_id)
            self._coordinator.resubmit_existing_turn(
                turn_id=main.turn_id, project_id=main.project_id,
            )
            replayed.append(main.turn_id)
            if _is_converged_steward(steward):
                pair_results = self._progress_pair(
                    project_id=main.project_id, main=main, steward=steward,
                )
                results.extend(pair_results)
                progressed_plan_ids.update(
                    value["plan_id"] for value in pair_results
                    if isinstance(value.get("plan_id"), str)
                )
        for plan in plans:
            main_request = self._request_loader(self._run(plan.main_run_id, plan.project_id).turn_id)
            if main_request.get('desired_outcome') == 'project.task':
                for permit in self._dispatch_store.list_permits(project_id=plan.project_id, plan_id=plan.plan_id):
                    child_id = self._dispatch_store.get_permit_child_run_binding(permit.permit_id, project_id=plan.project_id)
                    if child_id is None:
                        continue
                    child = self._run(child_id, plan.project_id)
                    if not child.is_terminal:
                        self._coordinator.resubmit_existing_turn(turn_id=child.turn_id, project_id=plan.project_id)
                        replayed.append(child.turn_id)
            if plan.plan_id in progressed_plan_ids:
                continue
            main = self._run(plan.main_run_id, plan.project_id)
            steward = self._run(plan.steward_run_id, plan.project_id)
            if not _is_start_pair(main, steward) or plan.status not in {"ready", "dispatching", "dispatched"}:
                continue
            if plan.mode == "main_only" and _is_converged_steward(steward):
                results.append(_safe_plan(self._complete_main_only(plan)))
            elif plan.mode == "cluster":
                if plan.status in {"ready", "dispatching"} and _is_converged_steward(steward):
                    plan = self._dispatch_cluster(plan, main)
                if plan.status == "dispatched":
                    final = self._converge_dispatched_plan(plan, main)
                    if final != plan:
                        results.append(_safe_plan(final))
        return {
            "status": "recovered", "plans": tuple(results),
            "scanned": len(start_pairs) + len(plans), "replayed_turn_ids": tuple(replayed),
        }

    def _scan_start_pairs(self, limit: int) -> tuple[tuple[AgentRun, AgentRun], ...]:
        scanner = self._start_pair_scanner
        if scanner is None:
            return ()
        values = tuple(scanner(limit))
        if len(values) > limit:
            raise AgentOrganizationError("organization start scanner exceeded its bound")
        if not all(isinstance(item, tuple) and len(item) == 2 and all(isinstance(run, AgentRun) for run in item) for item in values):
            raise AgentOrganizationError("organization start pair locator is invalid")
        return values

    def _scan_recovery_plans(self, limit: int) -> tuple[AgentDispatchPlan, ...]:
        scanner = self._recovery_plan_scanner
        if scanner is None:
            return ()
        values = tuple(scanner(limit))
        if len(values) > limit or not all(isinstance(value, AgentDispatchPlan) for value in values):
            raise AgentOrganizationError("organization recovery plan locator is invalid")
        return values

    def _prepare_steward(self, main: PreparedMainTurn) -> PreparedChildTurn:
        payload = validate_turn_request(main.request)
        run = main.run
        project_id = _project_id(payload)
        seed = f"{project_id}:{run.turn_id}:{payload['operation_id']}:steward"
        refs = tuple(
            item["uri"] for item in payload["input"]["refs"]
            if isinstance(item, Mapping) and isinstance(item.get("uri"), str)
        )
        child = SpawnRequest(
            operation_id=_id("op-organization-steward", seed),
            child_run_id=_id("organization-steward-run", seed),
            child_turn_id="turn-" + uuid5(NAMESPACE_URL, "organization-steward-turn:" + seed).hex,
            link_id=_id("organization-steward-link", seed),
            reservation_id=_id("organization-steward-reservation", seed),
            profile_id="steward.scheduler",
            child_session_id=_id("organization-steward-session", seed),
            idempotency_key=_id("organization-steward-idempotency", seed),
            input_text=str(payload["input"]["text"]),
            requested_capability_ids=("agent.plan",),
            requested_budget=run.budget_limit,
            requested_max_steps=run.max_steps,
            requested_timeout_ms=run.timeout_ms,
            requested_max_concurrent_children=0,
            requested_max_depth=run.max_depth,
            allow_child_spawn=False,
            input_refs=refs,
        )
        return self._coordinator.prepare_child(
            parent_turn_id=run.turn_id, request=child, project_id=project_id,
            scope=payload["scope"], privacy=payload["privacy"],
            child_request_factory=_steward_turn_request,
        )

    def _progress_pair(self, *, project_id: str, main: AgentRun, steward: AgentRun) -> list[Mapping[str, object]]:
        values: list[Mapping[str, object]] = []
        for plan in self._dispatch_store.list_plans_for_main(
            project_id=project_id, main_run_id=main.run_id,
        ):
            if plan.steward_run_id != steward.run_id or plan.status not in {
                "ready", "dispatching", "dispatched",
            }:
                continue
            if plan.mode == "main_only":
                final = self._complete_main_only(plan)
                values.append(_safe_plan(final))
            elif plan.mode == "cluster":
                final = (
                    self._dispatch_cluster(plan, main)
                    if plan.status in {"ready", "dispatching"} else plan
                )
                if final.status == "dispatched":
                    final = self._converge_dispatched_plan(final, main)
                values.append(_safe_plan(final))
            else:  # contracts normally make this impossible; fail closed here.
                raise AgentOrganizationError("organization plan mode is invalid")
        return values

    def _complete_main_only(self, plan: AgentDispatchPlan) -> AgentDispatchPlan:
        # Dispatch contracts intentionally require every transition.  There is
        # no child effect for main_only; this is a bounded, replay-safe chain.
        current = plan
        states = ("ready", "dispatching", "dispatched", "completed")
        try:
            start = states.index(current.status)
        except ValueError as error:
            raise AgentOrganizationError("main-only plan state is invalid") from error
        for state in states[start + 1:]:
            current = self._transition(current, state, f"organization-main-only-{state}")
        return current

    def _dispatch_cluster(self, plan: AgentDispatchPlan, main: AgentRun) -> AgentDispatchPlan:
        if self._request_loader(main.turn_id).get('desired_outcome') == 'project.task':
            return self._dispatch_divided_cluster(plan, main)
        permits = tuple(sorted(
            self._dispatch_store.list_permits(project_id=plan.project_id, plan_id=plan.plan_id),
            key=lambda value: value.permit_id,
        ))
        if len(permits) != len(plan.assignment_ids) or any(
            permit.project_id != plan.project_id or permit.plan_id != plan.plan_id
            or permit.assignment_id not in plan.assignment_ids
            for permit in permits
        ):
            raise AgentOrganizationError("organization dispatch permits do not match plan")
        children: list[PreparedChildTurn] = []
        if plan.status == "ready":
            if not self._freshness_allows(plan.project_id):
                return plan
            # Fence rescheduling before the first possibly effectful child
            # submission.  A crash or partial failure stays dispatching and is
            # replayed only through the same stable permit bindings.
            plan = self._transition(plan, "dispatching", "organization-dispatching")
        if plan.status == "dispatching":
            for permit in permits:
                prepared = self._coordinator.prepare_child_from_permit(
                    project_id=plan.project_id, permit_id=permit.permit_id,
                    operation_id=_id(
                        "op-organization-permit", plan.plan_id, permit.permit_id,
                    ),
                )
                self._coordinator.submit_accepted_turn(prepared)
                children.append(prepared)
            # On replay after dispatching was persisted, derive the same child
            # identities from the frozen permit rather than launching anything.
            child_ids = tuple(
                item.run.run_id for item in children
            ) or tuple(_permit_child_run_id(permit) for permit in permits)
            self._create_fan_in(main, plan, child_ids)
            plan = self._transition(plan, "dispatched", "organization-dispatched")
        return plan

    def _dispatch_divided_cluster(self, plan: AgentDispatchPlan, main: AgentRun) -> AgentDispatchPlan:
        permits = tuple(sorted(
            self._dispatch_store.list_permits(project_id=plan.project_id, plan_id=plan.plan_id),
            key=lambda value: value.permit_id,
        ))
        if len(permits) != len(plan.assignment_ids) or any(
            permit.project_id != plan.project_id or permit.plan_id != plan.plan_id
            or permit.assignment_id not in plan.assignment_ids
            for permit in permits
        ):
            raise AgentOrganizationError("organization dispatch permits do not match plan")
        children: list[PreparedChildTurn] = []
        if plan.status == "ready":
            if not self._freshness_allows(plan.project_id):
                return plan
            # Fence rescheduling before the first possibly effectful child
            # submission.  A crash or partial failure stays dispatching and is
            # replayed only through the same stable permit bindings.
            plan = self._transition(plan, "dispatching", "organization-dispatching")
        if plan.status == "dispatching":
            bound = {}
            for permit in permits:
                run_id = self._dispatch_store.get_permit_child_run_binding(permit.permit_id, project_id=plan.project_id)
                if run_id is not None:
                    found = self._run_store.get_run_with_revision(run_id, project_id=plan.project_id)
                    if found is not None:
                        bound[permit.assignment_id] = found[0]
            completed = {key for key, run in bound.items() if run.is_terminal}
            active = sum(not run.is_terminal for run in bound.values())
            for permit in permits:
                if permit.assignment_id in bound:
                    continue
                dependencies = self._coordinator.assignment_dependencies(project_id=plan.project_id, permit_id=permit.permit_id)
                if not set(dependencies).issubset(completed) or active >= plan.max_concurrent_assignments:
                    continue
                prepared = self._coordinator.prepare_child_from_permit(
                    project_id=plan.project_id, permit_id=permit.permit_id,
                    operation_id=_id(
                        "op-organization-permit", plan.plan_id, permit.permit_id,
                    ),
                )
                self._coordinator.submit_accepted_turn(prepared)
                children.append(prepared)
                bound[permit.assignment_id] = prepared.run
                active += 1
            if len(bound) != len(permits):
                return plan
            # On replay after dispatching was persisted, derive the same child
            # identities from the frozen permit rather than launching anything.
            child_ids = tuple(bound[permit.assignment_id].run_id for permit in permits)
            self._create_fan_in(main, plan, child_ids)
            plan = self._transition(plan, "dispatched", "organization-dispatched")
        return plan

    def _freshness_allows(self, project_id: str) -> bool:
        if self._freshness_gate is None:
            return True
        try:
            return self._freshness_gate(project_id) is True
        except Exception:
            return False

    def _converge_main_plans(
        self, *, project_id: str, main: AgentRun,
    ) -> list[Mapping[str, object]]:
        values: list[Mapping[str, object]] = []
        for plan in self._dispatch_store.list_plans_for_main(
            project_id=project_id, main_run_id=main.run_id,
        ):
            if plan.mode != "cluster" or plan.status not in {"dispatching", "dispatched"}:
                continue
            if plan.status == 'dispatching':
                plan = self._dispatch_cluster(plan, main)
            if plan.status != 'dispatched':
                continue
            final = self._converge_dispatched_plan(plan, main)
            if final != plan:
                values.append(_safe_plan(final))
        return values

    def _converge_dispatched_plan(
        self, plan: AgentDispatchPlan, main: AgentRun,
    ) -> AgentDispatchPlan:
        fan_in_id = _id("organization-fan-in", plan.plan_id)
        result = self._run_store.get_fan_in_result(
            fan_in_id, project_id=plan.project_id,
        )
        if result is None:
            return plan
        if (
            getattr(result, "fan_in_id", None) != fan_in_id
            or getattr(result, "project_id", None) != plan.project_id
            or getattr(result, "parent_run_id", None) != main.run_id
        ):
            raise AgentOrganizationError("organization fan-in result identity drifted")
        result_status = getattr(result, "status", None)
        if result_status not in {"completed", "failed", "cancelled", "timed_out"}:
            raise AgentOrganizationError("organization fan-in result status is invalid")
        status = "completed" if result_status == "completed" else "failed"
        return self._transition(plan, status, f"organization-{status}")

    def _create_fan_in(self, main: AgentRun, plan: AgentDispatchPlan, child_ids: tuple[str, ...]) -> None:
        request = validate_turn_request(self._request_loader(main.turn_id))
        operation_id = _id("organization-fan-in-operation", plan.plan_id)
        fan_in = AgentFanIn(
            _id("organization-fan-in", plan.plan_id), plan.project_id, main.run_id,
            operation_id, child_ids, "all", None, main.cancel_epoch, "open",
        )
        self._coordinator.fan_in(
            parent_turn_id=main.turn_id, fan_in=fan_in, operation_id=operation_id,
            project_id=plan.project_id, scope=request["scope"], privacy=request["privacy"],
        )

    def _transition(self, plan: AgentDispatchPlan, status: str, operation: str) -> AgentDispatchPlan:
        if plan.status == status:
            return plan
        value = replace(plan, revision=plan.revision + 1, status=status)
        try:
            return self._dispatch_store.transition_plan(
                value, expected_revision=plan.revision,
                operation_id=_id(operation, plan.plan_id, str(plan.revision)),
            )
        except Exception as error:
            # A concurrent/replayed writer may have completed this precise
            # progression.  Re-read once; any different state remains unsafe.
            current = self._dispatch_store.get_plan(plan.plan_id, project_id=plan.project_id)
            if current is not None and current.status == status:
                return current
            raise AgentOrganizationError("organization plan transition failed") from error

    def _run(self, run_id: str, project_id: str) -> AgentRun:
        found = self._run_store.get_run_with_revision(run_id, project_id=project_id)
        if found is None:
            raise AgentOrganizationError("organization topology run is unavailable")
        return found[0]

    @staticmethod
    def _with_host_capabilities(request: Mapping[str, object]) -> dict[str, object]:
        payload = dict(validate_turn_request(request))
        policy = payload["capability_policy"]
        denied = set(policy["denied"])
        payload["capability_policy"] = {
            "allowed": sorted(set(policy["allowed"]) | (_HOST_AGENT_CAPABILITIES - denied)),
            "denied": list(policy["denied"]),
            "require_approval": list(policy["require_approval"]),
        }
        return validate_turn_request(payload)


def _project_id(request: Mapping[str, object]) -> str:
    scope = request.get("scope")
    project_id = scope.get("project_id") if isinstance(scope, Mapping) else None
    if not isinstance(project_id, str) or not project_id:
        raise AgentOrganizationError("organization project scope is invalid")
    return project_id


def _is_converged_steward(run: AgentRun) -> bool:
    return (
        run.is_terminal and run.profile_id == "steward.scheduler"
        and run.role == "subagent" and run.parent_run_id is not None
    )


def _is_start_pair(main: AgentRun, steward: AgentRun) -> bool:
    return (
        main.role == "main" and main.profile_id == "main.orchestrator"
        and main.parent_run_id is None and not main.is_terminal
        and steward.role == "subagent" and steward.profile_id == "steward.scheduler"
        and steward.parent_run_id == main.run_id
    )


def _id(kind: str, *parts: str) -> str:
    return f"{kind}-{uuid5(NAMESPACE_URL, ':'.join(parts)).hex}"


def _permit_child_run_id(permit: DispatchPermit) -> str:
    return _id("permit-run", f"{permit.project_id}:{permit.permit_id}:{permit.assignment_id}")


def _steward_turn_request(base: Mapping[str, object]) -> Mapping[str, object]:
    """Narrow the generic child Turn to the steward's one planning outcome."""
    payload = dict(validate_turn_request(base))
    payload["desired_outcome"] = "agent.steward.plan"
    # The steward is the existing stage-9 coordination Turn, not a product
    # template. Its frozen AgentRun retains the delegated budget and limits.
    payload.pop('execution_policy', None)
    return validate_turn_request(payload)


def _safe_run(run: AgentRun) -> Mapping[str, object]:
    return {
        "run_id": run.run_id, "turn_id": run.turn_id,
        "profile_id": run.profile_id, "role": run.role,
        "status": run.status, "depth": run.depth,
    }


def _safe_plan(plan: AgentDispatchPlan) -> Mapping[str, object]:
    return {"plan_id": plan.plan_id, "status": plan.status, "mode": plan.mode, "revision": plan.revision}
