"""Own execution lifetime and durable idempotency independently of transport."""
import asyncio
import re

from fastapi import HTTPException
from ..workspace_contracts import _now
from ..model_billing_errors import MODEL_BILLING_HTTP_STATUS, model_billing_code, model_billing_status
from core.storage_provider.observability import current_observation
from core.storage_provider.connection_scope import with_connection_scope, create_scoped_task
from .turn_timings import turn_timing


REQUESTS = "v2_turn_requests"
_REQUEST_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
_INTERNAL_REQUEST_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}(?::(?:[0-9]|1[01]))?")
SAFE_ERRORS = {'external_host_isolation_unavailable', 'external_executor_not_installed',
    'external_executor_version_unverified', 'external_executor_version_unsupported',
    'external_executor_credentials_unavailable', 'external_agent_disabled',
    'external_task_runtime_unavailable', 'external_task_not_prepared', 'external_task_backend_unavailable',
    'invalid_executor', 'invalid_executor_request',"answer_generation_failed", "source_changed_retry", "remote_disabled",
    "private_project_remote_blocked", "ask_model_target_changed", "turn_in_progress",
    "turn_interrupted", "workbench_not_found", "invalid_turn", "question_too_long",
    "dependency_failed", "dependency_timeout", "invalid_idempotency_key", "idempotency_key_conflict",
    "shared_forbidden", "shared_project_unavailable", "shared_authority_changed",
    "shared_remember_owner_changed", "shared_remember_thread_changed", "shared_intake_command_conflict",
    "invalid_text", *MODEL_BILLING_HTTP_STATUS}


def safe_failure(error):
    from ..model_config import ModelConfigurationError
    billing = model_billing_code(error, ModelConfigurationError)
    if billing is not None:
        return billing
    code = getattr(error, "detail", None)
    if model_billing_status(code) is not None and type(error) is not HTTPException:
        return 'answer_generation_failed'
    return code if isinstance(code, str) and code in SAFE_ERRORS else "answer_generation_failed"


class TurnExecutionService:
    def __init__(self, records, execute, *, instance, read_result=None, internal_keys=False, internal_identity=None,
                 accept=None, replay=None):
        if accept is not None and not callable(accept):
            raise ValueError('turn acceptance callback invalid')
        if replay is not None and (not callable(replay) or accept is None or internal_keys):
            raise ValueError('turn replay callback invalid')
        self.records, self.execute, self.instance = records, execute, instance
        self.read_result = read_result or (lambda row: row["result"])
        self.tasks = {}
        self.internal_keys = internal_keys
        self.internal_identity = internal_identity
        self.accept = accept
        self.replay = replay

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

    def replay_completed(self, body, key):
        if key is None:
            return None
        if self.internal_keys or not isinstance(key, str) or not _REQUEST_KEY.fullmatch(key):
            raise HTTPException(400, 'invalid_idempotency_key')
        with self.records.begin_read() as tx:
            row = tx.read(REQUESTS, key)
            if row is None:
                return None
            if row.payload['body'] != body:
                raise HTTPException(409, 'idempotency_key_conflict')
            if row.payload['state'] != 'completed':
                return None
            if self.replay is not None:
                self.replay(tx, body, key)
            return self.read_result(row.payload)

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
        acceptance = None
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
                    if self.replay is not None:
                        acceptance = self.replay(tx, body, key)
                    state = row.payload["state"]
                    if state == "completed":
                        return self.read_result(row.payload)
                    if state == "running" and row.payload["instance"] == self.instance:
                        raise HTTPException(409, "turn_in_progress")
                    if acceptance is not None:
                        tx.put(REQUESTS, key, {"body": body, "state": "running", "instance": self.instance,
                            "created_at": row.payload["created_at"], "updated_at": _now()}, expected_revision=row.revision)
                    elif state == "running":
                        tx.put(REQUESTS, key, {**row.payload, "state": "interrupted",
                            "error": "turn_interrupted", "status_code": 409, "updated_at": _now()},
                            expected_revision=row.revision)
                        tx.commit()
                        raise HTTPException(409, "turn_interrupted")
                    else:
                        raise HTTPException(row.payload.get("status_code", 409), row.payload.get("error", "turn_interrupted"))
                else:
                    accepted_at = _now()
                    tx.put(REQUESTS, key, {"body": body, "state": "running", "instance": self.instance,
                        **({'idempotency_key':logical_key} if self.internal_keys else {}),
                        "created_at": accepted_at, "updated_at": accepted_at}, expected_revision=0)
                    if self.accept is not None:
                        acceptance = self.accept(tx, body, request_key=key, accepted_at=accepted_at)
                tx.commit()
        elif self.accept is not None:
            with self.records.begin() as tx:
                acceptance = self.accept(tx, body, request_key=None, accepted_at=_now())
                tx.commit()

        timing = current_observation()

        async def execute():
            try:
                options = {'on_frame': on_frame} if on_frame is not None else {}
                if self.accept is not None:
                    options['acceptance'] = acceptance
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
                from ..model_config import ModelConfigurationError
                billing = model_billing_code(error, ModelConfigurationError)
                converted = HTTPException(model_billing_status(billing), billing) if billing is not None else None
                failure = converted if converted is not None else error
                if key:
                    with self.records.begin() as tx:
                        row = tx.read(REQUESTS, key)
                        if row.payload["state"] == "running":
                            tx.put(REQUESTS, key, {**row.payload, "state": "failed",
                                "error": safe_failure(failure), "status_code": getattr(failure, "status_code", 502),
                                "updated_at": _now()}, expected_revision=row.revision)
                            tx.commit()
                if converted is not None:
                    raise converted from None
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
