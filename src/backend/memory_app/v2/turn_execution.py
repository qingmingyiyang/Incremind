"""Own execution lifetime and durable idempotency independently of transport."""
import asyncio
import re

from fastapi import HTTPException
from ..workspace_contracts import _now
from core.storage_provider.observability import current_observation
from core.storage_provider.connection_scope import with_connection_scope, create_scoped_task
from .turn_timings import turn_timing


REQUESTS = "v2_turn_requests"
_REQUEST_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
_INTERNAL_REQUEST_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}(?::[0-2])?")
SAFE_ERRORS = {"answer_generation_failed", "source_changed_retry", "remote_disabled",
    "private_project_remote_blocked", "ask_model_target_changed", "turn_in_progress",
    "turn_interrupted", "workbench_not_found", "invalid_turn", "question_too_long",
    "invalid_idempotency_key", "idempotency_key_conflict"}


def safe_failure(error):
    code = getattr(error, "detail", None)
    return code if isinstance(code, str) and code in SAFE_ERRORS else "answer_generation_failed"


class TurnExecutionService:
    def __init__(self, records, execute, *, instance, read_result=None, internal_keys=False, internal_identity=None):
        self.records, self.execute, self.instance = records, execute, instance
        self.read_result = read_result or (lambda row: row["result"])
        self.tasks = {}
        self.internal_keys = internal_keys
        self.internal_identity = internal_identity

    def completed_intent(self, body, key):
        """Read a matching saved result for transport; run still owns replay guards."""
        if not isinstance(key, str) or not _REQUEST_KEY.fullmatch(key):
            return None
        row = self.records.read(REQUESTS, key)
        if row is None or row.payload.get('body') != body or row.payload.get('state') != 'completed':
            return None
        result = row.payload.get('result')
        turn = result.get('turn') if isinstance(result, dict) else None
        intent = turn.get('intent') if isinstance(turn, dict) else None
        return intent if isinstance(intent, str) and intent in {'remember', 'inspiration', 'ask', 'do'} else None

    def complete(self, tx, key, result):
        from .turn_frames import complete_stream
        complete_stream(tx, result)
        if key is None:
            return
        row = tx.read(REQUESTS, key)
        tx.put(REQUESTS, key, {**row.payload, "state": "completed", "result": result,
            "updated_at": _now()}, expected_revision=row.revision)

    @with_connection_scope
    async def run(self, body, key=None, *, on_started=None, on_delta=None, on_plan=None, on_frame=None):
        intent = body.get("intent")
        operation = "task" if intent == "do" else intent if isinstance(intent, str) and intent in {"ask", "remember", "inspiration"} else "workbench"
        with turn_timing(self.records, operation):
            return await self._run(body, key, on_started=on_started, on_delta=on_delta, on_plan=on_plan, on_frame=on_frame)

    async def _run(self, body, key=None, *, on_started=None, on_delta=None, on_plan=None, on_frame=None):
        logical_key = key
        if key is not None:
            pattern = _INTERNAL_REQUEST_KEY if self.internal_keys else _REQUEST_KEY
            if not pattern.fullmatch(key):
                raise HTTPException(400, "invalid_idempotency_key")
            if self.internal_keys:
                if not self.internal_identity:
                    raise HTTPException(400, 'invalid_idempotency_key')
                key = self.internal_identity
            with self.records.begin() as tx:
                row = tx.read(REQUESTS, key)
                if row:
                    if row.payload["body"] != body:
                        raise HTTPException(409, "idempotency_key_conflict")
                    state = row.payload["state"]
                    if state == "completed":
                        return self.read_result(row.payload)
                    if state == "running" and row.payload["instance"] == self.instance:
                        raise HTTPException(409, "turn_in_progress")
                    if state == "running":
                        tx.put(REQUESTS, key, {**row.payload, "state": "interrupted",
                            "error": "turn_interrupted", "status_code": 409, "updated_at": _now()},
                            expected_revision=row.revision)
                        tx.commit()
                        raise HTTPException(409, "turn_interrupted")
                    raise HTTPException(row.payload.get("status_code", 409), row.payload.get("error", "turn_interrupted"))
                tx.put(REQUESTS, key, {"body": body, "state": "running", "instance": self.instance,
                    **({'idempotency_key':logical_key} if self.internal_keys else {}),
                    "created_at": _now(), "updated_at": _now()}, expected_revision=0)
                tx.commit()

        timing = current_observation()

        async def execute():
            try:
                options = {'on_frame': on_frame} if on_frame is not None else {}
                if on_plan:
                    options['on_plan'] = on_plan
                result = await self.execute(body, request_key=key, on_started=on_started, on_delta=on_delta, **options)
                if key:
                    with self.records.begin() as tx:
                        if tx.read(REQUESTS, key).payload["state"] != "completed":
                            self.complete(tx, key, result)
                            tx.commit()
                return result
            except BaseException as error:
                if key:
                    with self.records.begin() as tx:
                        row = tx.read(REQUESTS, key)
                        if row.payload["state"] == "running":
                            tx.put(REQUESTS, key, {**row.payload, "state": "failed",
                                "error": safe_failure(error), "status_code": getattr(error, "status_code", 502),
                                "updated_at": _now()}, expected_revision=row.revision)
                            tx.commit()
                raise
            finally:
                if timing is not None:
                    timing.finish()

        if timing is not None:
            timing.defer_finish()
        task = create_scoped_task(execute())
        identity = key or str(id(task))
        self.tasks[identity] = task
        def finished(done):
            self.tasks.pop(identity, None)
            if not done.cancelled():
                done.exception()  # Detached clients must not produce unhandled task logs.
        task.add_done_callback(finished)
        # Network disconnect detaches the delivery, not this business operation.
        return await asyncio.shield(task)
