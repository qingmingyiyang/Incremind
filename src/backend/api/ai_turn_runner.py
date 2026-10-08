from __future__ import annotations

from collections.abc import Mapping
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, wait
from contextvars import copy_context
from datetime import datetime, timedelta, timezone
from threading import Event, Lock, Thread, current_thread
from time import monotonic, sleep
from uuid import uuid4

from core.storage_provider.observability import current_observation, observation_scope
from core.storage_provider.connection_scope import capture_connection_scope, connection_scope

from core.ai_kernel import AIKernelRuntimeError, RunLeaseRevoked, RunLeaseToken, TurnReceipt, validate_turn_action, validate_turn_request


_RUNNER_BUILD_LOCK = Lock()
_TERMINAL_TURN_STATUSES = frozenset({"completed", "failed", "cancelled"})


class AITurnRunnerCapacityError(AIKernelRuntimeError):
    pass


class AITurnRunner:
    """Small application-owned executor for already durable AI Turns.

    It intentionally has no recovery scan: accepted work is durable, while
    process-crash recovery is a later operational capability.  The durable
    state-store lease, rather than this local active map, prevents duplicate
    execution across application processes.
    """

    def __init__(
        self,
        runtime: object,
        *,
        max_workers: int = 4,
        max_pending: int = 16,
        max_child_workers: int = 4,
        max_child_pending: int = 16,
        clock: Callable[[], datetime] | None = None,
        lease_ttl: timedelta = timedelta(seconds=30),
        heartbeat_interval_seconds: float | None = None,
        terminal_observer: Callable[[TurnReceipt], object] | None = None,
    ) -> None:
        if not isinstance(max_workers, int) or isinstance(max_workers, bool) or max_workers < 1:
            raise ValueError("AI Turn runner worker count must be positive")
        if not isinstance(max_pending, int) or isinstance(max_pending, bool) or max_pending < max_workers:
            raise ValueError("AI Turn runner pending capacity must cover its workers")
        if not isinstance(max_child_workers, int) or isinstance(max_child_workers, bool) or max_child_workers < 1:
            raise ValueError("AI Turn runner child worker count must be positive")
        if not isinstance(max_child_pending, int) or isinstance(max_child_pending, bool) or max_child_pending < max_child_workers:
            raise ValueError("AI Turn runner child pending capacity must cover its workers")
        if not isinstance(lease_ttl, timedelta) or lease_ttl <= timedelta(0):
            raise ValueError("AI Turn runner lease TTL must be positive")
        interval = lease_ttl.total_seconds() / 3 if heartbeat_interval_seconds is None else heartbeat_interval_seconds
        if not isinstance(interval, (int, float)) or isinstance(interval, bool) or not 0 < float(interval) < lease_ttl.total_seconds():
            raise ValueError("AI Turn runner heartbeat interval must be within lease TTL")
        self._runtime = runtime
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="ai-turn")
        # Child Agents never wait for other Turns; their own pool prevents
        # waiting parent Turns from starving the children they are awaiting.
        self._child_executor = ThreadPoolExecutor(max_workers=max_child_workers, thread_name_prefix="ai-turn-child")
        self._lock = Lock()
        self._max_pending = max_pending
        self._max_child_pending = max_child_pending
        self._child_turn_ids: set[str] = set()
        self._agent_bindings: dict[str, Mapping[str, object]] = {}
        self._pending_cancels: dict[str, str] = {}
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lease_ttl = lease_ttl
        self._heartbeat_interval_seconds = float(interval)
        # Keep the original construction argument as the first best-effort
        # observer.  Additional consumers (such as the agent coordinator) can
        # subscribe without replacing the production world-model projection.
        self._terminal_observers: dict[str, Callable[[TurnReceipt], object]] = {}
        if terminal_observer is not None:
            self._terminal_observers["initial"] = terminal_observer
        self._next_terminal_observer_id = 0
        self._terminal_wakeups: dict[str, Event] = {}
        # A durable terminal receipt can be visible a few instructions before
        # this process finishes its derived terminal observers.  Local parent
        # Turns must not race those observers and consume step budget against
        # stale AgentRun/fan-in state, so local waits distinguish committed
        # terminal state from locally observed terminal state.
        self._terminal_observed: set[str] = set()
        self._owner_id = f"ai-runner-{uuid4().hex}"
        self._active: dict[str, tuple[dict[str, object], TurnReceipt, RunLeaseToken, Future[object]]] = {}
        # Approval actions can begin a model wire attempt too.  They therefore
        # use the exact same durable lease/heartbeat discipline as acceptance.
        self._active_actions: dict[str, tuple[RunLeaseToken, Future[object]]] = {}
        self._active_action_payloads: dict[str, dict[str, object]] = {}
        self._lease_observations = {}
        self._lease_units = {}
        self._lease_contexts = {}
        self._closed = False
        self._lost_leases: set[RunLeaseToken] = set()
        self._heartbeat_wakeup = Event()
        self._heartbeat_stop = Event()
        self._heartbeat_thread = Thread(target=self._heartbeat_loop, name="ai-turn-lease-heartbeat", daemon=True)
        self._heartbeat_thread.start()

    def accept_and_submit(self, request: Mapping[str, object]) -> TurnReceipt:
        payload = validate_turn_request(request)
        submitted: tuple[str, Future[object]] | None = None
        with self._lock:
            requested_turn_id = str(payload["turn_id"])
            active = self._active.get(requested_turn_id)
            if active is not None:
                if active[0] != payload:
                    raise AIKernelRuntimeError("turn idempotency identity conflict")
                prior = active[1]
                return TurnReceipt(
                    prior.turn_id, prior.session_id, prior.operation_id,
                    prior.status, prior.current_sequence, True,
                )
            child = self._is_child_request(payload)
            if self._pending_count(child) >= (self._max_child_pending if child else self._max_pending):
                raise AITurnRunnerCapacityError("AI Turn runner capacity is unavailable")
            if self._closed:
                raise AIKernelRuntimeError("AI Turn runner is shutting down")
            receipt = self._runtime.accept_turn(payload)
            turn_id = receipt.turn_id
            if child:
                self._child_turn_ids.add(turn_id)
            binding = payload.get("agent_binding")
            if isinstance(binding, Mapping):
                self._agent_bindings[turn_id] = dict(binding)
            active = self._active.get(turn_id)
            if active is not None:
                if active[0] != payload:
                    raise AIKernelRuntimeError("turn idempotency identity conflict")
                return TurnReceipt(
                    receipt.turn_id, receipt.session_id, receipt.operation_id,
                    receipt.status, receipt.current_sequence, True,
                )
            if receipt.status not in {"completed", "failed", "cancelled", "waiting_approval"}:
                now = self._clock()
                run_lease = self._runtime.try_acquire_run_lease(turn_id, self._owner_id, now=now, stale_after=now + self._lease_ttl)
                if run_lease is None:
                    return TurnReceipt(
                        receipt.turn_id, receipt.session_id, receipt.operation_id,
                        receipt.status, receipt.current_sequence, True,
                    )
                executor = self._child_executor if child else self._executor
                self._remember_observation(run_lease)
                future = executor.submit(self._run_observed, run_lease, self._run_one, turn_id, run_lease)
                self._active[turn_id] = (dict(payload), receipt, run_lease, future)
                submitted = (turn_id, future)
                self._heartbeat_wakeup.set()
        if submitted is not None:
            # add_done_callback can synchronously run for an already-complete
            # fast task, so this must happen after releasing _lock.
            submitted_turn_id, submitted_future = submitted
            submitted_future.add_done_callback(
                lambda completed, target= submitted_turn_id: self._finished(target, completed)
            )
        elif receipt.status in {"completed", "failed", "cancelled"}:
            # A durable replay can repair a missing downstream observation.
            # The observer remains best-effort and cannot alter this receipt.
            self._notify_terminal(receipt)
        return receipt

    @property
    def active_turn_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._active)

    @staticmethod
    def _is_child_request(request: Mapping[str, object]) -> bool:
        binding = request.get("agent_binding")
        return isinstance(binding, Mapping) and binding.get("role") == "subagent"

    def _pending_count(self, child: bool) -> int:
        # Called under _lock. Actions retain the accepted Turn's pool and
        # consume that same pool's capacity, including queued lease owners.
        return sum(self._is_child_request(item[0]) == child for item in self._active.values()) + sum(
            (turn_id in self._child_turn_ids) == child for turn_id in self._active_actions
        )

    def request_turn_cancel(
        self,
        turn_id: str,
        *,
        reason: str = "cancel requested by coordinator",
    ) -> bool:
        """Request cancellation for one locally leased Turn or action.

        The runner never acquires a fresh lease to cancel another host's work.
        It can only forward a request while it still owns the exact active
        lease, preserving the existing runtime's durable cancellation fence.
        Runtime implementations that accept a reason receive it; the current
        legacy runtime remains compatible until its cancellation payload grows
        that field.
        """
        if not isinstance(turn_id, str) or not turn_id.strip():
            raise ValueError("AI Turn id must be a non-empty string")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("AI Turn cancellation reason must be a non-empty string")
        with self._lock:
            active = self._active.get(turn_id)
            action = self._active_actions.get(turn_id)
            run_lease = active[2] if active is not None else (action[0] if action is not None else None)
        if run_lease is None:
            return False
        requested = self._request_background_cancel(turn_id, run_lease, reason.strip())
        if requested:
            self._cancel_children(turn_id, reason.strip())
        return requested

    def _cancel_children(self, parent_turn_id: str, reason: str) -> None:
        with self._lock:
            parent = self._agent_bindings.get(parent_turn_id, {})
            run_id = parent.get("run_id")
            children = tuple(
                (turn_id, item[2]) for turn_id, item in self._active.items()
                if run_id is not None and self._agent_bindings.get(turn_id, {}).get("parent_run_id") == run_id
            ) + tuple(
                (turn_id, item[0]) for turn_id, item in self._active_actions.items()
                if run_id is not None and self._agent_bindings.get(turn_id, {}).get("parent_run_id") == run_id
            )
            for turn_id, _ in children:
                self._pending_cancels[turn_id] = reason
        for turn_id, lease in children:
            try:
                requested = self._request_background_cancel(turn_id, lease, reason)
            except RunLeaseRevoked:
                # A child may finish and release its lease after the snapshot.
                # Its lifecycle must not break the parent's terminal observer.
                with self._lock:
                    self._pending_cancels.pop(turn_id, None)
                continue
            except Exception:
                # The heartbeat retries transient failure against this exact
                # lease; parent cancellation/terminal observation stays valid.
                continue
            if requested:
                with self._lock:
                    self._pending_cancels.pop(turn_id, None)
        self._heartbeat_wakeup.set()

    def terminal_receipt(self, turn_id: str) -> TurnReceipt | None:
        """Read a durable terminal receipt without advancing a Turn."""
        if not isinstance(turn_id, str) or not turn_id.strip():
            raise ValueError("AI Turn id must be a non-empty string")
        receipt = self._runtime.receipt_for(turn_id)
        if not isinstance(receipt, TurnReceipt):
            raise AIKernelRuntimeError("AI Turn runtime returned an invalid receipt")
        return receipt if receipt.status in _TERMINAL_TURN_STATUSES else None

    def wait_for_terminal(
        self,
        turn_id: str,
        *,
        timeout_seconds: float | None = None,
        poll_interval_seconds: float = 0.05,
    ) -> TurnReceipt | None:
        """Wait for a durable terminal receipt using the existing Runner.

        Notifications wake local waiters promptly.  The small durable polling
        fallback also observes a terminal receipt committed by a different
        process or before this Runner registered its callback.
        """
        if not isinstance(turn_id, str) or not turn_id.strip():
            raise ValueError("AI Turn id must be a non-empty string")
        if timeout_seconds is not None and (
            not isinstance(timeout_seconds, (int, float))
            or isinstance(timeout_seconds, bool)
            or timeout_seconds < 0
        ):
            raise ValueError("AI Turn terminal wait timeout must be non-negative")
        if (
            not isinstance(poll_interval_seconds, (int, float))
            or isinstance(poll_interval_seconds, bool)
            or poll_interval_seconds <= 0
        ):
            raise ValueError("AI Turn terminal poll interval must be positive")
        with self._lock:
            wakeup = self._terminal_wakeups.setdefault(turn_id, Event())
        deadline = None if timeout_seconds is None else monotonic() + float(timeout_seconds)
        while True:
            terminal = self.terminal_receipt(turn_id)
            with self._lock:
                locally_active = (
                    turn_id in self._active or turn_id in self._active_actions
                )
                locally_observed = turn_id in self._terminal_observed
            if terminal is not None and (
                not locally_active or locally_observed
            ):
                return terminal
            if deadline is not None:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    return None
                wait_seconds = min(float(poll_interval_seconds), remaining)
            else:
                wait_seconds = float(poll_interval_seconds)
            wakeup.wait(wait_seconds)
            wakeup.clear()

    def subscribe_terminal(self, observer: Callable[[TurnReceipt], object]) -> Callable[[], None]:
        """Register a best-effort terminal observer and return its remover."""
        if not callable(observer):
            raise ValueError("AI Turn terminal observer must be callable")
        with self._lock:
            observer_id = f"subscriber-{self._next_terminal_observer_id}"
            self._next_terminal_observer_id += 1
            self._terminal_observers[observer_id] = observer

        def unsubscribe() -> None:
            with self._lock:
                self._terminal_observers.pop(observer_id, None)

        return unsubscribe

    def accept_action_and_submit(
        self,
        action: Mapping[str, object],
        *,
        acceptance_timeout_seconds: float = 30.0,
    ) -> TurnReceipt:
        """Return after the action has crossed a durable Session fence.

        The worker keeps the same strict run lease and continues independently.
        Waiting is limited to local validation and persistence; Provider or Tool
        completion is observed later through the existing projection stream.
        """
        if (
            not isinstance(acceptance_timeout_seconds, (int, float))
            or isinstance(acceptance_timeout_seconds, bool)
            or acceptance_timeout_seconds <= 0
        ):
            raise ValueError("AI Turn action acceptance timeout must be positive")
        payload = validate_turn_action(action)
        turn_id = str(payload["turn_id"])
        expected_sequence = int(payload["expected_sequence"])
        replayed = False
        # A Turn worker can finish at waiting_approval immediately before the
        # callback removes it. Converge that completed entry before deciding
        # whether the user's action conflicts with active Turn execution.
        with self._lock:
            prior_turn = self._active.get(turn_id)
            completed_turn = prior_turn[3] if prior_turn is not None and prior_turn[3].done() else None
        if completed_turn is not None:
            self._finished(turn_id, completed_turn)
        with self._lock:
            if self._closed:
                raise AIKernelRuntimeError("AI Turn runner is shutting down")
            if turn_id in self._active:
                raise AIKernelRuntimeError("AI Turn action is already running")
            active_action = self._active_actions.get(turn_id)
            if active_action is not None:
                if self._active_action_payloads.get(turn_id) != payload:
                    raise AIKernelRuntimeError("AI Turn action idempotency identity conflict")
                submitted = active_action[1]
                replayed = True
            else:
                child = turn_id in self._child_turn_ids
                if self._pending_count(child) >= (self._max_child_pending if child else self._max_pending):
                    raise AITurnRunnerCapacityError("AI Turn runner capacity is unavailable")
                now = self._clock()
                run_lease = self._runtime.try_acquire_run_lease(
                    turn_id, self._owner_id, now=now, stale_after=now + self._lease_ttl,
                )
                if run_lease is None:
                    raise AIKernelRuntimeError("AI Turn action lease is unavailable")
                executor = self._child_executor if child else self._executor
                self._remember_observation(run_lease)
                submitted = executor.submit(self._run_observed, run_lease, self._run_action_one, dict(payload), run_lease)
                self._active_actions[turn_id] = (run_lease, submitted)
                self._active_action_payloads[turn_id] = dict(payload)
                self._heartbeat_wakeup.set()
        if not replayed:
            submitted.add_done_callback(
                lambda completed, target=turn_id: self._finished_action(target, completed)
            )

        accepted_types = {
            "approve": {"approval.resolved"},
            "reject": {"approval.resolved", "turn.cancelled"},
            "mcp_continue": {"approval.resolved"},
            "mcp_reject": {"approval.resolved", "turn.cancelled"},
            "resume": {"turn.resumed"},
            "cancel": {"turn.cancel.requested", "turn.cancelled"},
        }[str(payload["type"])]
        accepted_types.update({"turn.completed", "turn.failed", "turn.cancelled"})
        deadline = monotonic() + float(acceptance_timeout_seconds)
        while True:
            events = tuple(self._runtime.events_after(turn_id, expected_sequence))
            if any(event.get("type") in accepted_types for event in events):
                return self._runtime.receipt_for(turn_id, replayed=replayed)
            if submitted.done():
                receipt = submitted.result()
                if not isinstance(receipt, TurnReceipt):
                    raise AIKernelRuntimeError("AI Turn action returned an invalid receipt")
                return receipt
            if monotonic() >= deadline:
                raise AIKernelRuntimeError("AI Turn action durable acceptance timed out")
            sleep(0.002)

    def apply_action_and_wait(self, action: Mapping[str, object]) -> TurnReceipt:
        """Run one approval action under a durable lease and wait for its receipt.

        The Test Lab still has a synchronous HTTP contract.  Waiting here does
        not make it an ungoverned inline call: the executor-owned lease is
        renewed while the action can cross a model wire boundary, and only a
        terminal receipt releases it.  ``BaseException`` intentionally leaves
        the lease to expire so startup recovery can classify the durable event
        history rather than risk a second provider invocation.
        """
        payload = validate_turn_action(action)
        turn_id = str(payload["turn_id"])
        submitted: Future[object]
        with self._lock:
            if self._closed:
                raise AIKernelRuntimeError("AI Turn runner is shutting down")
            if turn_id in self._active or turn_id in self._active_actions:
                raise AIKernelRuntimeError("AI Turn action is already running")
            child = turn_id in self._child_turn_ids
            if self._pending_count(child) >= (self._max_child_pending if child else self._max_pending):
                raise AITurnRunnerCapacityError("AI Turn runner capacity is unavailable")
            now = self._clock()
            run_lease = self._runtime.try_acquire_run_lease(
                turn_id, self._owner_id, now=now, stale_after=now + self._lease_ttl,
            )
            if run_lease is None:
                raise AIKernelRuntimeError("AI Turn action lease is unavailable")
            executor = self._child_executor if child else self._executor
            self._remember_observation(run_lease)
            submitted = executor.submit(self._run_observed, run_lease, self._run_action_one, dict(payload), run_lease)
            self._active_actions[turn_id] = (run_lease, submitted)
            self._active_action_payloads[turn_id] = dict(payload)
            self._heartbeat_wakeup.set()
        # This API is synchronous, so action cleanup is owned by this waiter.
        # That removes the callback/result race and makes an immediate
        # idempotent replay observe the released durable lease.
        try:
            receipt = submitted.result()
        finally:
            self._finished_action(turn_id, submitted)
        if not isinstance(receipt, TurnReceipt):
            raise AIKernelRuntimeError("AI Turn action returned an invalid receipt")
        return receipt

    def shutdown(self, *, timeout_seconds: float = 1.0) -> tuple[str, ...]:
        if not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool) or timeout_seconds < 0:
            raise ValueError("AI Turn runner shutdown timeout must be non-negative")
        with self._lock:
            if self._closed:
                return ()
            self._closed = True
            active = tuple(self._active.items())
            actions = tuple(self._active_actions.items())
        for turn_id, item in active:
            try:
                self._runtime.request_background_cancel(turn_id, item[2])
            except Exception:
                pass
        for turn_id, item in actions:
            try:
                self._runtime.request_background_cancel(turn_id, item[0])
            except Exception:
                pass
        deadline = monotonic() + float(timeout_seconds)
        futures = tuple(item[3] for _, item in active) + tuple(item[1] for _, item in actions)
        if futures:
            wait(futures, timeout=max(0.0, deadline - monotonic()))
        with self._lock:
            still_running = tuple(
                turn_id for turn_id, item in self._active.items() if not item[3].done()
            ) + tuple(
                turn_id for turn_id, item in self._active_actions.items() if not item[1].done()
            )
        # Never block shutdown on an uncooperative planner/provider.  Running
        # leases remain held until their worker actually exits, so another host
        # cannot falsely claim the same potentially-effectful Turn.
        self._executor.shutdown(wait=False, cancel_futures=True)
        self._child_executor.shutdown(wait=False, cancel_futures=True)
        self._stop_heartbeat_if_idle()
        if self._heartbeat_stop.is_set() and current_thread() is not self._heartbeat_thread:
            self._heartbeat_thread.join(timeout=self._heartbeat_interval_seconds + 0.1)
        return still_running

    def _run_one(self, turn_id: str, run_lease: RunLeaseToken) -> bool:
        try:
            with self._lock:
                cancel_reason = self._pending_cancels.get(turn_id)
            if cancel_reason is not None:
                # A queued child has no planner control to cancel yet. Use the
                # existing durable CAS action before advancing its accepted Turn.
                receipt = self._cancel_before_start(turn_id, run_lease, cancel_reason)
            else:
                receipt = self._runtime.run_accepted_turn(turn_id, run_lease)
            self._notify_terminal(receipt)
            return getattr(receipt, "status", None) in {
                "completed", "failed", "cancelled", "waiting_approval", "waiting_job",
            }
        except RunLeaseRevoked:
            return False
        except Exception:
            # The runtime makes execution failures durable.  The runner must
            # isolate one Turn rather than take down unrelated accepted work.
            try:
                receipt = self._runtime.fail_accepted_turn(turn_id, run_lease)
                self._notify_terminal(receipt)
                return getattr(receipt, "status", None) in {"completed", "failed", "cancelled"}
            except Exception:
                # Do not release the durable lease: an unknown provider/tool
                # effect must fail closed rather than being executed again.
                return False

    def _cancel_before_start(self, turn_id: str, run_lease: RunLeaseToken, reason: str) -> TurnReceipt:
        events = tuple(self._runtime.events_after(turn_id))
        return self._runtime.apply_action({
            "schema_version": "1.0.0", "action_id": f"action-{uuid4().hex}",
            "turn_id": turn_id, "type": "cancel", "target_event_id": None,
            "reason": reason, "actor": "user", "expected_sequence": len(events),
            "idempotency_key": f"child-cancel-{uuid4().hex}",
            "created_at": self._clock().isoformat(),
        }, run_lease)

    def _remember_observation(self, run_lease):
        # Called under the runner lock. Keep the request alive through cleanup.
        # 身份和共享资源随本次租约捕获，不能固定为创建 Runner 时的访问者。
        self._lease_contexts[run_lease] = copy_context()
        unit = capture_connection_scope()
        if unit is not None:
            self._lease_units[run_lease] = unit
        observation = current_observation()
        if observation is not None:
            retain = getattr(observation, "defer_finish", None)
            if retain is not None:
                retain()
            self._lease_observations[run_lease] = observation

    def _run_observed(self, run_lease, operation, *args, **kwargs):
        with self._lock:
            unit = self._lease_units.get(run_lease)
            if unit is not None:
                unit.retain()
            observation = self._lease_observations.get(run_lease)
            retain = getattr(observation, "defer_finish", None)
            if retain is not None:
                retain()
            context = self._lease_contexts.get(run_lease)
        if context is not None:
            # 工作、续租与清理可并发，每次进入独立副本。
            return context.copy().run(self._invoke_observed, observation, operation, *args, unit=unit, **kwargs)
        return self._invoke_observed(observation, operation, *args, unit=unit, **kwargs)

    def _invoke_observed(self, observation, operation, *args, unit=None, **kwargs):
        try:
            # Each thread enters its own scope; no Context is concurrently run.
            with observation_scope(observation), connection_scope(unit):
                try:
                    return operation(*args, **kwargs)
                finally:
                    finish = getattr(observation, "finish", None)
                    if finish is not None:
                        finish()
        finally:
            if unit is not None:
                unit.close()

    def _forget_observation(self, run_lease):
        with self._lock:
            observation = self._lease_observations.pop(run_lease, None)
            unit = self._lease_units.pop(run_lease, None)
            context = self._lease_contexts.pop(run_lease, None)
        def finish_observation():
            with connection_scope(unit):
                finish = getattr(observation, "finish", None)
                if finish is not None:
                    finish()
        try:
            if context is not None:
                context.copy().run(finish_observation)
            else:
                finish_observation()
        finally:
            if unit is not None:
                unit.close()

    def _finished(self, turn_id: str, completed: Future[object]) -> None:
        with self._lock:
            active = self._active.get(turn_id)
            lease = active[2] if active is not None and active[3] is completed else None
        try:
            self._run_observed(lease, self._finished_unobserved, turn_id, completed)
        finally:
            self._forget_observation(lease)

    def _finished_unobserved(self, turn_id: str, completed: Future[object]) -> None:
        with self._lock:
            active = self._active.get(turn_id)
            if active is not None and active[3] is completed:
                run_lease = active[2]
                lease_lost = run_lease in self._lost_leases
                safe_to_release = completed.cancelled()
                if not safe_to_release:
                    try:
                        safe_to_release = completed.result() is True
                    except Exception:
                        safe_to_release = False
                # Release the durable lease before removing the local active
                # marker. Otherwise an immediately following approval can see
                # neither the completed worker nor an acquirable lease.
                if safe_to_release and not lease_lost:
                    try:
                        self._runtime.release_strict_run_lease(run_lease)
                    except Exception:
                        pass
                self._active.pop(turn_id, None)
                self._pending_cancels.pop(turn_id, None)
                if turn_id in self._terminal_observed:
                    self._child_turn_ids.discard(turn_id)
                    self._agent_bindings.pop(turn_id, None)
                self._terminal_observed.discard(turn_id)
            else:
                run_lease = None
                lease_lost = False
                safe_to_release = False
        self._stop_heartbeat_if_idle()

    def _run_action_one(
        self, action: Mapping[str, object], run_lease: RunLeaseToken,
    ) -> TurnReceipt:
        turn_id = str(action["turn_id"])
        try:
            with self._lock:
                cancel_reason = self._pending_cancels.get(turn_id)
            receipt = (
                self._cancel_before_start(turn_id, run_lease, cancel_reason)
                if cancel_reason is not None else self._runtime.apply_action(action, run_lease)
            )
        except RunLeaseRevoked:
            raise
        except Exception:
            # Runtime faults that are ordinary exceptions can still converge a
            # terminal state.  Do not catch BaseException: process-abort style
            # exits must retain the lease for scanner classification.
            receipt = self._runtime.fail_accepted_turn(turn_id, run_lease)
        self._notify_terminal(receipt)
        return receipt

    def _notify_terminal(self, receipt: object) -> None:
        if not isinstance(receipt, TurnReceipt) or receipt.status not in _TERMINAL_TURN_STATUSES:
            return
        if receipt.status == "cancelled":
            self._cancel_children(receipt.turn_id, "parent Turn cancelled")
        with self._lock:
            observers = tuple(self._terminal_observers.values())
        for observer in observers:
            try:
                observer(receipt)
            except Exception:
                # Terminal observation is derived, never part of Turn commit.
                pass
        with self._lock:
            self._terminal_observed.add(receipt.turn_id)
            wakeup = self._terminal_wakeups.get(receipt.turn_id)
            if receipt.turn_id not in self._active and receipt.turn_id not in self._active_actions:
                self._child_turn_ids.discard(receipt.turn_id)
                self._agent_bindings.pop(receipt.turn_id, None)
        if wakeup is not None:
            wakeup.set()

    def _request_background_cancel(
        self,
        turn_id: str,
        run_lease: RunLeaseToken,
        reason: str,
    ) -> bool:
        try:
            return bool(
                self._runtime.request_background_cancel(
                    turn_id, run_lease, reason=reason,
                )
            )
        except TypeError as error:
            # The current runtime surface predates a caller-provided reason.
            # Only fall back for that signature mismatch; a TypeError raised
            # inside an implementation remains a real failure.
            if "reason" not in str(error):
                raise
            return bool(self._runtime.request_background_cancel(turn_id, run_lease))

    def _finished_action(self, turn_id: str, completed: Future[object]) -> None:
        with self._lock:
            active = self._active_actions.get(turn_id)
            lease = active[0] if active is not None and active[1] is completed else None
        try:
            self._run_observed(lease, self._finished_action_unobserved, turn_id, completed)
        finally:
            self._forget_observation(lease)

    def _finished_action_unobserved(self, turn_id: str, completed: Future[object]) -> None:
        with self._lock:
            active = self._active_actions.get(turn_id)
            if active is not None and active[1] is completed:
                self._active_actions.pop(turn_id, None)
                self._active_action_payloads.pop(turn_id, None)
                self._pending_cancels.pop(turn_id, None)
                if turn_id in self._terminal_observed:
                    self._child_turn_ids.discard(turn_id)
                    self._agent_bindings.pop(turn_id, None)
                self._terminal_observed.discard(turn_id)
                run_lease = active[0]
                lease_lost = run_lease in self._lost_leases
                safe_to_release = completed.cancelled()
                if not safe_to_release:
                    try:
                        receipt = completed.result()
                        safe_to_release = isinstance(receipt, TurnReceipt) and receipt.status in {
                            "completed", "failed", "cancelled",
                        }
                    except BaseException:
                        # Preserve the lease for process-like aborts and any
                        # non-converged action failure.
                        safe_to_release = False
            else:
                run_lease = None
                lease_lost = False
                safe_to_release = False
        if run_lease is not None and safe_to_release and not lease_lost:
            try:
                self._runtime.release_strict_run_lease(run_lease)
            except Exception:
                pass
        self._stop_heartbeat_if_idle()

    def _heartbeat_loop(self) -> None:
        while not self._heartbeat_stop.is_set():
            with self._lock:
                cancellation_pending = bool(self._pending_cancels)
            self._heartbeat_wakeup.wait(min(self._heartbeat_interval_seconds, 0.05) if cancellation_pending else self._heartbeat_interval_seconds)
            self._heartbeat_wakeup.clear()
            with self._lock:
                active = tuple(
                    item[2] for item in self._active.values() if item[2] not in self._lost_leases
                ) + tuple(
                    item[0] for item in self._active_actions.values() if item[0] not in self._lost_leases
                )
                # Retain while still under the same lock as lease cleanup.
                observed = []
                for run_lease in active:
                    observation = self._lease_observations.get(run_lease)
                    retain = getattr(observation, "defer_finish", None)
                    if retain is not None:
                        retain()
                    unit = self._lease_units.get(run_lease)
                    if unit is not None:
                        unit.retain()
                    context = self._lease_contexts.get(run_lease)
                    observed.append((run_lease, observation, unit, context))
            for run_lease, observation, unit, context in observed:
                try:
                    if context is not None:
                        context.copy().run(self._invoke_observed, observation, self._renew_observed_lease, run_lease, unit=unit)
                    else:
                        self._invoke_observed(observation, self._renew_observed_lease, run_lease, unit=unit)
                except RunLeaseRevoked:
                    with self._lock:
                        self._lost_leases.add(run_lease)
                except Exception:
                    # Transient storage failure is retried next interval; it
                    # never turns a lease into a safe release.
                    pass
            self._stop_heartbeat_if_idle()

    def _renew_observed_lease(self, run_lease):
        with self._lock:
            cancel_reason = self._pending_cancels.get(run_lease.turn_id)
        if cancel_reason is not None:
            if self._request_background_cancel(run_lease.turn_id, run_lease, cancel_reason):
                with self._lock:
                    self._pending_cancels.pop(run_lease.turn_id, None)
        now = self._clock()
        record = self._runtime.renew_run_lease(run_lease, now=now, stale_after=now + self._lease_ttl)
        if record is None:
            with self._lock:
                self._lost_leases.add(run_lease)

    def _stop_heartbeat_if_idle(self) -> None:
        with self._lock:
            if self._closed and not self._active and not self._active_actions:
                self._heartbeat_stop.set()
                self._heartbeat_wakeup.set()


def get_or_build_ai_turn_runner(request: object, runtime: object) -> AITurnRunner:
    application = getattr(request, "app")
    runner = getattr(application.state, "ai_turn_runner", None)
    if runner is not None:
        return runner
    with _RUNNER_BUILD_LOCK:
        runner = getattr(application.state, "ai_turn_runner", None)
        if runner is None:
            runner = AITurnRunner(
                runtime,
                terminal_observer=getattr(
                    application.state,
                    "personal_world_model_terminal_observer",
                    None,
                ),
            )
            application.state.ai_turn_runner = runner
    return runner


def shutdown_ai_turn_runner(application: object) -> None:
    runner = getattr(getattr(application, "state", object()), "ai_turn_runner", None)
    if runner is not None:
        runner.shutdown()
