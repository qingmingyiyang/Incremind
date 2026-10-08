"""Production composition for governed main and child Agent Runs.

This module only connects the existing durable Turn, runner, Profile and
AgentStore authorities. It never owns a second executor, Turn store, model
route authority, or recovery loop.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock

from backend.api.agent_capabilities import (
    AGENT_CAPABILITY_IDS,
    AgentCapabilityProvider,
    agent_capability_definitions,
)
from backend.memory_app.kernel.agent_coordinator import AgentCoordinator, AgentCoordinatorError, AgentPolicySnapshotPort
from backend.api.agent_message_authority import AgentMessagePayloadAuthority
from backend.api.agent_dispatch_runtime import AgentDispatchRuntime, OpaqueAssignmentResolver
from backend.api.capability_admission import ReviewedCoreCapabilityRegistry
from core.ai_kernel import AgentProfileRegistry, SQLiteAgentStore, TurnReceipt
from core.ai_kernel.agent_dispatch_store import SQLiteAgentDispatchStore


class AgentRuntimeCompositionError(RuntimeError):
    """A production Agent dependency was unavailable or drifted."""


class _LateBoundRuntimePort:
    def __init__(self) -> None:
        self._runtime: object | None = None

    def bind(self, runtime: object) -> None:
        if self._runtime is not None and self._runtime is not runtime:
            raise AgentRuntimeCompositionError("Agent runtime binding already exists")
        self._runtime = runtime

    def accept_turn(self, request: Mapping[str, object]) -> TurnReceipt:
        target = self._require()
        accept = getattr(target, "accept_turn", None)
        if not callable(accept):
            raise AgentRuntimeCompositionError("AI Turn runtime is unavailable")
        return accept(request)

    def receipt_for(self, turn_id: str, *, replayed: bool = False) -> TurnReceipt:
        target = self._require()
        receipt_for = getattr(target, "receipt_for", None)
        if not callable(receipt_for):
            raise AgentRuntimeCompositionError("AI Turn receipt authority is unavailable")
        return receipt_for(turn_id, replayed=replayed)

    def _require(self) -> object:
        if self._runtime is None:
            raise AgentRuntimeCompositionError("AI Turn runtime is not bound")
        return self._runtime


class _LateBoundRunnerPort:
    def __init__(self) -> None:
        self._runner: object | None = None

    def bind(self, runner: object) -> None:
        if self._runner is not None and self._runner is not runner:
            raise AgentRuntimeCompositionError("Agent runner binding already exists")
        self._runner = runner

    def accept_and_submit(self, request: Mapping[str, object]) -> TurnReceipt:
        return self._call("accept_and_submit", request)

    def request_turn_cancel(self, turn_id: str, *, reason: str) -> bool:
        return self._call("request_turn_cancel", turn_id, reason=reason)

    def wait_for_terminal(
        self, turn_id: str, *, timeout_seconds: float | None = None,
        poll_interval_seconds: float = 0.05,
    ) -> TurnReceipt | None:
        return self._call(
            "wait_for_terminal", turn_id, timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )

    def terminal_receipt(self, turn_id: str) -> TurnReceipt | None:
        return self._call("terminal_receipt", turn_id)

    def subscribe_terminal(self, observer: Callable[[TurnReceipt], object]) -> Callable[[], None]:
        target = self._require()
        subscribe = getattr(target, "subscribe_terminal", None)
        if not callable(subscribe):
            raise AgentRuntimeCompositionError("AI Turn runner cannot subscribe terminal observers")
        return subscribe(observer)

    def _call(self, name: str, *args: object, **kwargs: object):
        target = self._require()
        method = getattr(target, name, None)
        if not callable(method):
            raise AgentRuntimeCompositionError(f"AI Turn runner {name} is unavailable")
        return method(*args, **kwargs)

    def _require(self) -> object:
        if self._runner is None:
            raise AgentRuntimeCompositionError("AI Turn runner is not bound")
        return self._runner


@dataclass(slots=True)
class AgentRuntimeComposition:
    """The application-owned projection over the canonical Turn path."""

    store: SQLiteAgentStore
    dispatch_store: SQLiteAgentDispatchStore
    dispatch_runtime: AgentDispatchRuntime
    profiles: AgentProfileRegistry
    coordinator: AgentCoordinator
    request_loader: Callable[[str], Mapping[str, object]]
    _runtime_port: _LateBoundRuntimePort
    _runner_port: _LateBoundRunnerPort
    _unsubscribe_terminal: Callable[[], None] | None = None
    _organization_runtime: object | None = None
    _supervision_observer: object | None = None
    last_terminal_reconciliation_status: str = "idle"
    last_terminal_reconciliation_error: str | None = None
    last_organization_progress_status: str = "idle"
    last_organization_progress_error: str | None = None
    last_supervision_observation_status: str = "idle"
    last_supervision_observation_error: str | None = None
    _reconciliation_lock: Lock = field(default_factory=Lock, repr=False)

    def bind_runtime(self, runtime: object) -> None:
        self._runtime_port.bind(runtime)
        configure_budget = getattr(runtime, 'configure_agent_tool_budget_reader', None)
        if callable(configure_budget):
            configure_budget(self._frozen_tool_call_limit)

    def _frozen_tool_call_limit(self, request: Mapping[str, object]) -> int:
        binding = request.get('agent_binding')
        if not isinstance(binding, Mapping):
            raise AgentRuntimeCompositionError('Agent budget binding is unavailable')
        self.coordinator.verify_agent_binding(request, binding)
        run = self.store.get_run(str(binding['run_id']))
        if run is None or run.turn_id != request.get('turn_id'):
            raise AgentRuntimeCompositionError('Agent budget Run is unavailable')
        # AgentStore keeps this Run snapshot immutable across lifecycle updates.
        # Current Profile changes cannot replace an accepted Turn's allocation.
        if (run.profile_id != binding.get('profile_id')
            or run.profile_revision != binding.get('profile_revision')
            or run.budget_snapshot_ref != binding.get('budget_snapshot_ref')):
            raise AgentRuntimeCompositionError('Agent budget snapshot drifted')
        return run.budget_limit.tool_calls

    def bind_runner(self, runner: object) -> None:
        self._runner_port.bind(runner)
        if self._unsubscribe_terminal is None:
            # Reconciliation remains the existing recovery worker's authority.
            # This is the narrow future fan-in handoff, not a second reaper.
            self._unsubscribe_terminal = self._runner_port.subscribe_terminal(
                self._observe_terminal_receipt
            )

    def bind_organization_runtime(self, runtime: object) -> None:
        """Bind the sole durable organization progressor to the observer."""

        if not callable(getattr(runtime, "on_terminal", None)):
            raise AgentRuntimeCompositionError("Agent organization runtime is invalid")
        if self._organization_runtime is not None and self._organization_runtime is not runtime:
            raise AgentRuntimeCompositionError("Agent organization runtime binding already exists")
        self._organization_runtime = runtime

    def bind_supervision_observer(self, observer: object) -> None:
        """Bind the receipt-only World supervision observer once."""

        if not callable(getattr(observer, "observe", None)):
            raise AgentRuntimeCompositionError("World supervision observer is invalid")
        if self._supervision_observer is not None and self._supervision_observer is not observer:
            raise AgentRuntimeCompositionError("World supervision observer binding already exists")
        self._supervision_observer = observer

    def _observe_terminal_receipt(self, receipt: TurnReceipt) -> None:
        try:
            result = self.coordinator.reconcile_terminal_turn(receipt.turn_id)
        except Exception as error:
            # A terminal Turn is already durable.  Reconciliation failure is
            # diagnostic only and must never advance or rewrite that Turn.
            with self._reconciliation_lock:
                self.last_terminal_reconciliation_status = "failed"
                self.last_terminal_reconciliation_error = type(error).__name__
            return
        else:
            with self._reconciliation_lock:
                self.last_terminal_reconciliation_status = (
                    "reconciled" if result is not None else "noop"
                )
                self.last_terminal_reconciliation_error = None
        if result is not None and self._organization_runtime is not None:
            try:
                progress = self._organization_runtime.on_terminal(receipt.turn_id)
            except Exception as error:
                # Organization progression is durable and replayable.  Keep its
                # diagnostic separate from the already-converged child receipt.
                with self._reconciliation_lock:
                    self.last_organization_progress_status = "failed"
                    self.last_organization_progress_error = type(error).__name__
                return
            else:
                with self._reconciliation_lock:
                    self.last_organization_progress_status = (
                        "progressed" if progress is not None else "noop"
                    )
                    self.last_organization_progress_error = None
        # A reconciliation no-op is an idempotent replay and may still expose
        # a fan-in completed by a later child callback.  Failures above stop
        # this ordered chain and remain diagnostics only.
        if self._supervision_observer is None:
            return
        try:
            observed = self._supervision_observer.observe(receipt.turn_id)
        except Exception as error:
            with self._reconciliation_lock:
                self.last_supervision_observation_status = "failed"
                self.last_supervision_observation_error = type(error).__name__
            return
        with self._reconciliation_lock:
            self.last_supervision_observation_status = str(observed)
            self.last_supervision_observation_error = None


def build_agent_runtime_composition(
    *, runtime_root: Path, session_store: object,
    registry: ReviewedCoreCapabilityRegistry,
    expert_assignment_resolver: OpaqueAssignmentResolver | None = None,
    skill_assignment_resolver: OpaqueAssignmentResolver | None = None,
    agent_policy_snapshots: AgentPolicySnapshotPort | None = None,
    profiles: AgentProfileRegistry | None = None,
) -> AgentRuntimeComposition:
    """Create one durable Agent topology on the existing AI Turn database."""

    database_path = runtime_root / ".rebuild-data" / "ai-turns.sqlite3"
    store = SQLiteAgentStore(database_path)
    dispatch_store = SQLiteAgentDispatchStore(database_path)
    # Recursive-evolution policy must be constructed before the coordinator so
    # every newly accepted Turn receives one immutable policy snapshot.  The
    # application may therefore provide its already-open profile authority.
    # Standalone callers retain the original durable default.
    profiles = profiles or AgentProfileRegistry(store)
    runtime_port, runner_port = _LateBoundRuntimePort(), _LateBoundRunnerPort()

    def request_loader(turn_id: str) -> Mapping[str, object]:
        getter = getattr(session_store, "get_request", None)
        if not callable(getter):
            raise AgentRuntimeCompositionError("AI Turn request authority is unavailable")
        request = getter(turn_id)
        if not isinstance(request, Mapping):
            raise AgentCoordinatorError("AI Turn request is unavailable")
        return request

    dispatch_runtime = AgentDispatchRuntime(
        payload_writer=lambda turn_id, kind, payload: session_store.get_or_create_immutable_payload(
            turn_id, kind, payload,
        ),
        topology=store, profiles=profiles, dispatch_store=dispatch_store,
        expert_resolver=expert_assignment_resolver,
        skill_resolver=skill_assignment_resolver,
    )
    coordinator = AgentCoordinator(
        runtime=runtime_port, runner=runner_port, store=store,
        profiles=profiles, request_loader=request_loader,
        events_loader=session_store.events_after,
        payload_loader=session_store.get,
        terminal_receipt_writer=lambda turn_id, payload: (
            session_store.get_or_create_immutable_payload(
                turn_id, "agent-turn-terminal-receipt-v1", payload,
            )
        ),
        immutable_payload_writer=lambda turn_id, kind, payload: (
            session_store.get_or_create_immutable_payload(turn_id, kind, payload)
        ),
        immutable_payload_reference=lambda turn_id, kind: (
            session_store.immutable_payload_reference(turn_id, kind)
        ),
        agent_policy_snapshots=agent_policy_snapshots,
        message_payload_authority=AgentMessagePayloadAuthority(
            payload_reader=session_store, immutable_payloads=session_store,
        ),
        dispatch_runtime=dispatch_runtime,
        permit_store=dispatch_store,
    )
    definitions = agent_capability_definitions()
    if tuple(item.capability_id for item in definitions) != AGENT_CAPABILITY_IDS:
        raise AgentRuntimeCompositionError("Agent capability inventory drifted")
    for definition in definitions:
        registry.register(
            definition,
            AgentCapabilityProvider(
                coordinator=coordinator, capability_id=definition.capability_id,
            ),
        )
    return AgentRuntimeComposition(
        store, dispatch_store, dispatch_runtime, profiles, coordinator,
        request_loader, runtime_port, runner_port,
    )
