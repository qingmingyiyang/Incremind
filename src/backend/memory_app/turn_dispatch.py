"""HTTP-facing admission for recognition tasks on the preserved Turn runner.

The recognition store remains the authority for packets and documents.  This
module only constructs the deliberately body-free Turn envelope and projects
the old runtime's durable state back to that store.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from types import SimpleNamespace
from urllib.parse import urlsplit

from backend.memory_app.kernel.ai_runtime import get_or_build_ai_runtime
from backend.recognition import RecognitionConflict, WorkScope
from core.ai_kernel.runtime import AIKernelRuntimeError

from .turn_capability import (RECOGNITION_TASK_CAPABILITY, RECOGNITION_TASK_LOCAL_CAPABILITY,
                              RECOGNITION_TASK_OUTCOME)


_TERMINAL = frozenset({"completed", "failed", "cancelled", "stale", "interrupted"})


class RecognitionTurnDispatcher:
    def __init__(self, *, application, runtime_root, records, mutation_lock):
        self.application, self.runtime_root = application, runtime_root
        self.records, self.lock = records, mutation_lock

    def submit(self, *, task_id: str, scope: WorkScope, packet_id: str) -> dict[str, object]:
        try:
            self._runtime()
            coordinator = self.application.state.agent_runtime_composition.coordinator
            request = _request(task_id=task_id, project_id=str(scope.project_id), packet_id=packet_id,
                               remote=self._remote_allowed())
            prepared = coordinator.accept_and_register_main(request)
            coordinator.submit_accepted_turn(prepared)
        except Exception:
            self._mark_failed(task_id, scope)
            raise
        return self.sync(task_id=task_id, scope=scope)

    def sync(self, *, task_id: str, scope: WorkScope) -> dict[str, object]:
        task = self._task(task_id, scope)
        # Terminal recognition state is already published by the authority.
        # Reading a restored result must not require the original Turn store
        # or initialize the execution runtime merely to observe that result.
        if task.payload.get("state") in _TERMINAL:
            return dict(task.payload)
        turn_id = task.payload.get("turn_id")
        if not isinstance(turn_id, str) or not turn_id:
            return dict(task.payload)
        runtime = self._runtime()
        try:
            receipt = runtime.receipt_for(turn_id)
            events = tuple(runtime.events_after(turn_id))
        except AIKernelRuntimeError as error:
            # A process can stop after the recognition+packet transaction but
            # before old Turn acceptance. There is no durable Turn to replay.
            if "turn request was not found" not in str(error):
                raise
            self._set_interrupted(task_id, scope)
            return dict(self._task(task_id, scope).payload)
        state = str(receipt.status)
        sequence = int(receipt.current_sequence)
        # Receipt and event reads can straddle an approval. Interpret only the
        # immutable event prefix represented by this receipt, not later events.
        events = tuple(event for event in events if event.get("sequence", 0) <= sequence)
        patch: dict[str, object] = {}
        if state in {"accepted", "queued", "starting"}:
            state = "queued"
        elif state == "waiting_approval":
            approval = _waiting_approval(events)
            if approval is None:
                state = "failed"
            else:
                state = "waiting_approval"
                patch.update(approval)
        elif state in {"running", "waiting", "cancelling"}:
            state = "running"
        elif state == "recovery_required":
            state = "interrupted"
        elif state in {"quarantined", "timed_out"}:
            state = "failed"
        elif state in {"completed", "failed", "cancelled"}:
            # The existing recognition authority owns the terminal transition:
            # it verifies result_ready and current sources before publishing a
            # document.  Never duplicate that decision in this HTTP projector.
            authority = getattr(self.application.state, "recognition_turn_authority", None)
            observe = getattr(authority, "observe_terminal", None)
            if callable(observe):
                observe(receipt)
            return dict(self._task(task_id, scope).payload)
        else:
            state = "failed"
        current = self._task(task_id, scope)
        # A committed document waits for the terminal observer. It is still
        # projected as running, but no poll may move it back to running.
        if current.payload.get("state") not in _TERMINAL and (current.payload.get("state") != state
                or sequence > current.payload.get("turn_projection_sequence", 0)
                or any(current.payload.get(k) != v for k, v in patch.items())):
            with self.lock:
                current = self._task(task_id, scope)
                if current.payload.get("state") == "result_ready":
                    return dict(current.payload)
                # A GET can read the old waiting receipt just before approval
                # advances the worker. It must not overwrite load_task's newer
                # running state with that same (or older) approval projection.
                if sequence <= current.payload.get("turn_projection_sequence", 0):
                    return dict(current.payload)
                if current.payload.get("state") not in _TERMINAL:
                    payload = {**current.payload, "state": state, "turn_projection_sequence": sequence, **patch}
                    if state in _TERMINAL:
                        payload["finished_at"] = _now()
                    with self.records.begin() as tx:
                        tx.put("recognition_tasks", task_id, payload, expected_revision=current.revision)
                        tx.commit()
                    current = self._task(task_id, scope)
        return dict(current.payload)

    def approve(self, *, task_id: str, scope: WorkScope, target_event_id: str, expected_sequence: int) -> dict[str, object]:
        task = self._task(task_id, scope)
        if task.payload.get("approval_event_id") != target_event_id or task.payload.get("approval_sequence") != expected_sequence:
            raise RecognitionConflict("approval is no longer current")
        if task.payload.get("state") != "waiting_approval":
            # The exact action was already admitted. Browser retries observe
            # its projection; they never manufacture a second approval.
            return self.sync(task_id=task_id, scope=scope)
        turn_id = task.payload.get("turn_id")
        if not isinstance(turn_id, str):
            raise RecognitionConflict("task turn is unavailable")
        # Stable identity makes a browser retry submit the same old Turn action.
        identity = f"recognition-approve-{task_id}-{expected_sequence}"
        action = {"schema_version": "1.0.0", "action_id": identity, "turn_id": turn_id,
                  "type": "approve", "target_event_id": target_event_id,
                  "reason": "User approved this confirmed recognition task", "actor": "user",
                  "expected_sequence": expected_sequence, "idempotency_key": identity,
                  "created_at": str(task.payload.get("created_at") or _now())}
        self._runner().accept_action_and_submit(action)
        return self.sync(task_id=task_id, scope=scope)

    def cancel(self, *, task_id: str, scope: WorkScope) -> dict[str, object]:
        # Commit cancellation before forwarding it: a late model completion then
        # fails the recognition authority's current-state check.
        with self.lock:
            task = self._task(task_id, scope)
            if task.payload.get("state") not in _TERMINAL:
                with self.records.begin() as tx:
                    tx.put("recognition_tasks", task_id, {**task.payload, "state": "cancelled", "finished_at": _now()}, expected_revision=task.revision)
                    tx.commit()
        task = self._task(task_id, scope)
        turn_id = task.payload.get("turn_id")
        if isinstance(turn_id, str) and turn_id:
            self._runner().request_turn_cancel(turn_id, reason="recognition task cancelled by user")
        return dict(self._task(task_id, scope).payload)

    def _runtime(self):
        return get_or_build_ai_runtime(SimpleNamespace(app=self.application), SimpleNamespace(root_dir=self.runtime_root))

    def _runner(self):
        self._runtime()
        return self.application.state.ai_turn_runner

    def _remote_allowed(self) -> bool:
        public = self.application.state.recognition_models.public()["generation"]
        host = urlsplit(str(public.get("base_url", ""))).hostname
        return bool(public.get("allow_remote") and public.get("configured")
                    and host not in {"localhost", "127.0.0.1", "::1"})

    def _task(self, task_id: str, scope: WorkScope):
        record = self.records.read("recognition_tasks", task_id)
        if record is None or record.payload.get("project_id") != scope.project_id:
            raise RecognitionConflict("task is unavailable in this project")
        return record

    def _mark_failed(self, task_id: str, scope: WorkScope) -> None:
        with self.lock:
            task = self._task(task_id, scope)
            if task.payload.get("state") not in _TERMINAL:
                with self.records.begin() as tx:
                    tx.put("recognition_tasks", task_id, {**task.payload, "state": "failed", "finished_at": _now()}, expected_revision=task.revision)
                    tx.commit()

    def _set_interrupted(self, task_id: str, scope: WorkScope) -> None:
        with self.lock:
            task = self._task(task_id, scope)
            if task.payload.get("state") not in _TERMINAL:
                with self.records.begin() as tx:
                    tx.put("recognition_tasks", task_id, {**task.payload, "state": "interrupted", "finished_at": _now()}, expected_revision=task.revision)
                    tx.commit()


def _request(*, task_id: str, project_id: str, packet_id: str, remote: bool) -> dict[str, object]:
    turn_id = f"turn-recognition-{task_id.removeprefix('task-')}"
    capability_id = RECOGNITION_TASK_CAPABILITY if remote else RECOGNITION_TASK_LOCAL_CAPABILITY
    privacy = {"mode": "remote_allowed" if remote else "local_only", "allow_remote": remote,
               "pii": "possible", "consent_refs": [f"crp://default/consent/{packet_id}"] if remote else [],
               "retention": "local_durable"}
    return {"schema_version": "1.0.0", "turn_id": turn_id, "session_id": f"session-{task_id}",
            "operation_id": f"operation-{task_id}", "idempotency_key": f"recognition-{task_id}",
            "scope": {"kind": "project", "project_id": project_id, "series_id": None},
            "input": {"kind": "text", "text": "Execute confirmed recognition task", "refs": [
                {"kind": "recognition_context", "object_id": packet_id, "uri": f"crp://default/recognition/contexts/{packet_id}"}]},
            "desired_outcome": RECOGNITION_TASK_OUTCOME, "privacy": privacy,
            "capability_policy": {"allowed": [capability_id], "denied": [], "require_approval": [capability_id]},
            "capability_request": {"mode": "execute_exact_v1", "capability_id": capability_id,
                                   "arguments": {"task_id": task_id, "context_packet_id": packet_id}},
            "context_policy": {"include_project_skill": False, "include_memory": False, "include_session_history": False, "max_context_bytes": 11744},
            "approval_policy": {"mode": "explicit", "auto_approve_read_only": True}, "created_at": _now()}


def _waiting_approval(events: tuple[Mapping[str, object], ...]) -> dict[str, object] | None:
    for event in reversed(events):
        if event.get("type") == "approval.required":
            event_id, sequence = event.get("event_id"), event.get("sequence")
            if isinstance(event_id, str) and isinstance(sequence, int) and sequence >= 0:
                return {"approval_event_id": event_id, "approval_sequence": sequence}
            return None
        if event.get("type") in {"approval.resolved", "turn.completed", "turn.failed", "turn.cancelled"}:
            return None
    return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
