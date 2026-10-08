"""Bind domain question orchestration to the application's existing Turn runner."""
from core.storage_provider.connection_scope import with_connection_scope
import asyncio
from copy import deepcopy
from contextvars import ContextVar
from dataclasses import replace
from threading import RLock
from time import monotonic
from types import SimpleNamespace

from fastapi import HTTPException

from core.ai_kernel import CapabilityDefinition, validate_turn_request
from .turn_requests import freeze_turn_request
from core.ai_tooling import tool_from_capability
from ..workspace_contracts import _now
from .ai_execution_control import begin_nested_model_call, execution_control_from
from .policy_runtime import retry_policy_for


ACTIVE_ANSWER = ContextVar("product_answer_execution", default=None)
RESULT_KIND = "product-answer-result-v2"


class _AnswerHandles(dict):
    def __init__(self):
        super().__init__()
        self.invocation_lock = RLock()


class AnswerInterrupted(RuntimeError):
    def __init__(self, result):
        self.result = result
        super().__init__('answer_interrupted')


class _AnswerCall:
    """First durable terminal wins even when a timed-out transport returns."""
    def __init__(self, handle, runtime, turn_id):
        self.handle, self.runtime, self.turn_id = handle, runtime, turn_id
        self.lock = RLock()
        self.terminal = None
        self.refs = ()
        self.discarded = False

    def __getattr__(self, name):
        return getattr(self.handle, name)

    def finalize(self, *, error_code):
        with self.lock:
            if self.terminal is None:
                self.refs = self.handle.finalize(error_code=error_code)
                self.terminal = "completed" if error_code is None else "failed"
            return self.refs

    def discard(self):
        with self.lock:
            if not self.discarded:
                self.runtime._append(self.turn_id, "model.result.discarded", "running",
                    "Late answer model result discarded", model_request_id=self.model_request_id,
                    model_call_purpose=self.handle.control.purpose)
                self.discarded = True


def abandon_answer_model(invocation_key):
    active = ACTIVE_ANSWER.get()
    if active is not None:
        call = active[3].get(invocation_key)
        if call is not None:
            call.finalize(error_code="ai.answer_aux_timeout")


def answer_definition():
    """One read tool owns at most two rewrites and one answer, without retries."""
    definition = CapabilityDefinition("workbench.answer.execute", 1, "read", False, "read_only",
        "crp://default/contracts/workbench-question-request.schema.json",
        "crp://default/contracts/workbench-question-presentation.schema.json")
    tool = tool_from_capability(definition)
    return replace(definition, tool_definition=replace(tool,
        nested_model_handle_budget=3, timeout_ms=240_000, idempotency="never_retry",
        retry_policy=replace(tool.retry_policy, max_attempts=1, retryable_error_codes=())))


class ProductAnswerCapability:
    def __init__(self, service, store):
        self.service, self.store = service, store

    def paused_invocation(self, intent):
        with self.service.lock:
            execution = self.service.active.get(intent.turn_id)
        if (execution is None or execution.get('paused_invocation') != intent.invocation_id
                or intent.capability_id != 'workbench.answer.execute'):
            return None
        return self.service.paused(intent.turn_id, require_bundle=False)

    @with_connection_scope
    def invoke(self, request):
        frozen = self.store.get_request(request["turn_id"])
        if not frozen or frozen.get("execution_policy", {}).get("template_version") != 2:
            raise ValueError("product answer template is unavailable")
        if frozen["desired_outcome"] != "project.answer":
            raise ValueError("product answer kind changed")
        identity = request["turn_id"]
        saved = self.store.get_immutable_payload(identity, RESULT_KIND)
        if saved is None:
            with self.service.lock:
                execution = self.service.active.get(identity)
            if execution is None:
                # Recovery owns uncertain attempts. A lost HTTP closure never
                # authorizes issuing a fresh model request.
                raise ValueError("product answer requires recovery review")
            handles = _AnswerHandles()
            token = ACTIVE_ANSWER.set((request, self.store, self.service.query, handles))
            try:
                result = asyncio.run(execution["operation"]())
                execution_control_from(request).checkpoint()
                answer = result.get('receipt', {}).get('ask', result)
                if 'interruption' in answer:
                    self.service.preserve_interruption(request, execution, result, handles)
                    raise AnswerInterrupted(result)
                ref = self.store.get_or_create_immutable_payload(identity, RESULT_KIND, result)
            except BaseException as error:
                execution["error"] = error
                raise ValueError("product answer execution failed") from None
            finally:
                ACTIVE_ANSWER.reset(token)
        else:
            ref = saved[0]
        return {"summary": "Question completed", "payload_ref": ref, "evidence_refs": []}


class ProductAnswerTurns:
    def __init__(self, application, root, query):
        self.application, self.root, self.query = application, root, query
        self.active, self.lock = {}, RLock()

    def _runtime(self):
        from .ai_runtime import get_or_build_ai_runtime
        with self.lock:
            return get_or_build_ai_runtime(SimpleNamespace(app=self.application), SimpleNamespace(root_dir=self.root))

    def preserve_interruption(self, request, execution, result, handles):
        from backend.shared.llm.model_transport import is_provider_closed_witness
        from .answer_continuations import COLLECTION, plan_binding
        from .receipt_projection import closed_model_attempt_binding
        invocation = execution.get('invocation_key', 'answer')
        call = handles.get(invocation)
        if call is None or not is_provider_closed_witness(getattr(call, 'close_witness', None)):
            return
        lease = call.runtime._run_lease_context.get()
        if lease is None or lease.turn_id != request['turn_id']:
            return
        frozen = self.store_request(request['turn_id'])
        owner = {'owner_id': lease.owner_id, 'generation': lease.generation}
        if self.application.state.ai_turn_store.assert_active_run_lease(lease) is None:
            return
        attempts = closed_model_attempt_binding(self.root, request['turn_id'], frozen['scope']['project_id'],
            question=frozen['input']['text'], model_request_id=call.model_request_id, lease=owner)
        if attempts is None:
            return
        prepared = self.query.records.read(COLLECTION, request['turn_id'])
        if prepared is None:
            return
        plan_binding(self.query, prepared)
        binding = {'turn_id': request['turn_id'], 'project_id': frozen['scope']['project_id'],
            'question': frozen['input']['text'], 'invocation_id': request['tool_call_id'],
            'model_request_id': call.model_request_id, 'lease': owner, 'attempts': attempts,
            'plan_ref': prepared.payload['plan_ref'], 'result': result}
        store = self.application.state.ai_turn_store
        ref = store.get_or_create_immutable_payload(request['turn_id'],
            'product-answer-closed-' + call.model_request_id, binding)
        if store.assert_active_run_lease(lease) is None:
            return
        with self.query.records.begin() as tx:
            row = tx.read(COLLECTION, request['turn_id'])
            if (row is None or row.payload.get('project_id') != binding['project_id']
                    or row.payload.get('question') != binding['question']
                    or row.payload.get('state') not in {'prepared', 'continuing'}):
                return
            tx.put(COLLECTION, row.object_id, {**row.payload, 'state': 'paused', 'result': result,
                'invocation_id': binding['invocation_id'],
                'close_refs': [*row.payload.get('close_refs', []), ref]}, expected_revision=row.revision)
            tx.commit()
        execution['paused_invocation'] = binding['invocation_id']

    def store_request(self, identity):
        return self.application.state.ai_turn_store.get_request(identity)

    def suspended_without_executor(self, identity):
        from .answer_continuations import COLLECTION
        with self.lock:
            executing = identity in self.active
        return not executing and self.query.records.read(COLLECTION, identity) is not None

    def admit_continuation(self, action, row):
        from .answer_continuations import COLLECTION
        with self.lock:
            execution = self.active.get(row.object_id)
        if execution is None or execution.get('continuation') != row:
            raise ValueError('answer_continuation_requires_explicit_executor')
        execution['validate_current'](row)
        with self.query.records.begin() as tx:
            current = tx.read(COLLECTION, row.object_id)
            if current != row:
                raise ValueError('answer_continuation_revision_changed')
            tx.put(COLLECTION, row.object_id, {**row.payload, 'state': 'continuing',
                'action_id': action['action_id']}, expected_revision=row.revision)
            tx.commit()

    def paused(self, identity, *, require_bundle=True):
        from .answer_continuations import COLLECTION, plan_binding
        from .receipt_projection import closed_model_attempt_binding
        store = self.application.state.ai_turn_store
        frozen = store.get_request(identity)
        row = self.query.records.read(COLLECTION, identity)
        if (frozen is None or row is None or row.payload.get('state') != 'paused'
                or row.payload.get('project_id') != frozen['scope']['project_id']
                or row.payload.get('question') != frozen['input']['text']
                or frozen.get('desired_outcome') != 'project.answer'):
            return None
        refs = row.payload.get('close_refs')
        if not isinstance(refs, list) or not refs:
            return None
        try:
            original = plan_binding(self.query, row)
        except (ValueError, TypeError, KeyError):
            return None
        events = tuple(store.events_after(identity))
        requests = set()
        for ref in refs:
            try:
                if not isinstance(ref, str):
                    return None
                value = store.get(ref)
                if (type(value) is not dict or set(value) != {'turn_id', 'project_id', 'question', 'invocation_id',
                        'model_request_id', 'lease', 'attempts', 'plan_ref', 'result'}
                        or value['turn_id'] != identity or value['project_id'] != row.payload['project_id']
                        or value['question'] != row.payload['question'] or value['plan_ref'] != row.payload['plan_ref']
                        or value['model_request_id'] in requests):
                    return None
                immutable = store.get_immutable_payload(identity, 'product-answer-closed-' + value['model_request_id'])
                if immutable is None or immutable != (ref, value):
                    return None
                current = closed_model_attempt_binding(self.root, identity, value['project_id'], question=value['question'],
                    model_request_id=value['model_request_id'], lease=value['lease'])
                if current is None or current != value['attempts']:
                    return None
                if require_bundle:
                    bundles = [event for event in events if event['type'] == 'tool.outcome.recorded'
                        and event['correlation']['tool_call_id'] == value['invocation_id']
                        and set(event['data']['evidence_refs']) == {ref, value['plan_ref']}]
                    if len(bundles) != 1:
                        return None
                    outcome = store.get(bundles[0]['data']['payload_ref'])
                    if (outcome['invocation_id'] != value['invocation_id'] or outcome['turn_id'] != identity
                            or outcome['capability_id'] != 'workbench.answer.execute' or outcome['status'] != 'failed'
                            or outcome['effect_certainty'] != 'confirmed_none'):
                        return None
                if not requests and value['lease'] != original['lease']:
                    return None
                requests.add(value['model_request_id'])
            except (ValueError, TypeError, KeyError):
                return None
        if (value['invocation_id'] != row.payload.get('invocation_id')
                or value['result'] != row.payload.get('result')):
            return None
        dispatched = {event['data']['payload_ref'] for event in events if event['type'] == 'model.attempt.dispatched'}
        terminals = [store.get(event['data']['receipt_ref']) for event in events if event['type'] == 'model.attempt.terminal']
        if len(dispatched) != len(terminals) or any(value['status'] == 'consumer_cancelled' for value in terminals):
            return None
        completed = {event['correlation']['model_request_id'] for event in events if event['type'] == 'model.completed'}
        if any(value['status'] != 'succeeded' and value['model_request_id'] not in requests | completed for value in terminals):
            return None
        return row

    def _clear_prepared_plan(self, identity):
        from .answer_continuations import COLLECTION
        with self.query.records.begin() as tx:
            row = tx.read(COLLECTION, identity)
            if row is not None and row.payload.get('state') in {'prepared', 'continuing'}:
                tx.delete(COLLECTION, identity, expected_revision=row.revision)
                tx.commit()

    @with_connection_scope
    async def continue_on_request(self, *, identity, project, key, operation, validate_current):
        from uuid import uuid4
        from .answer_continuations import COLLECTION
        from core.ai_kernel import validate_turn_action
        runtime = await asyncio.to_thread(self._runtime)
        state = self.application.state
        store, runner = state.ai_turn_store, state.ai_turn_runner
        frozen = store.get_request(identity)
        if frozen is None or frozen['scope']['project_id'] != project:
            raise HTTPException(404, 'workbench_not_found')
        prior = store.get_action(key)
        if prior is not None:
            payload, _ = prior
            if payload['turn_id'] != identity or payload['type'] != 'resume':
                raise HTTPException(409, 'idempotency_key_conflict')
            result = store.get_immutable_payload(identity, RESULT_KIND)
            if result is not None:
                return result[1]
            row = self.paused(identity)
            if row is not None:
                return row.payload['result']
            raise HTTPException(409, 'turn_interrupted')
        row = self.paused(identity)
        if row is None or runtime.receipt_for(identity).status != 'waiting_approval':
            raise HTTPException(409, 'turn_interrupted')
        validate_current(row)
        action = validate_turn_action({'schema_version': '1.0.0', 'action_id': 'action-' + uuid4().hex,
            'turn_id': identity, 'type': 'resume', 'target_event_id': None, 'reason': 'continue partial answer',
            'actor': 'user', 'expected_sequence': len(tuple(store.events_after(identity))),
            'idempotency_key': key, 'created_at': _now()})
        invocation = 'answer-continue-' + action['action_id']
        execution = {'operation': lambda: operation(row, invocation), 'invocation_key': invocation,
            'continuation': row, 'validate_current': validate_current}
        with self.lock:
            if identity in self.active:
                raise HTTPException(409, 'turn_in_progress')
            self.active[identity] = execution
        try:
            await asyncio.to_thread(runner.apply_action_and_wait, action)
            if isinstance(execution.get('error'), AnswerInterrupted):
                return execution['error'].result
            if 'error' in execution:
                raise execution['error']
            result = store.get_immutable_payload(identity, RESULT_KIND)
            if result is None or runtime.receipt_for(identity).status != 'completed':
                raise HTTPException(502, 'answer_generation_failed')
            with self.query.records.begin() as tx:
                current = tx.read(COLLECTION, identity)
                tx.delete(COLLECTION, identity, expected_revision=current.revision)
                tx.commit()
            return result[1]
        finally:
            with self.lock:
                self.active.pop(identity, None)

    @with_connection_scope
    async def run(self, *, turn_id, project, question, operation, policy_versions=None,
                  situation=None, part_context=None):
        if ACTIVE_ANSWER.get() is not None:
            return await operation()
        runtime = await asyncio.to_thread(self._runtime)
        state = self.application.state
        store, runner = state.ai_turn_store, state.ai_turn_runner
        existing = store.get_request(turn_id)
        if existing is not None:
            if existing["scope"]["project_id"] != project or existing["input"]["text"] != question:
                raise HTTPException(409, "idempotency_key_conflict")
            result = store.get_immutable_payload(turn_id, RESULT_KIND)
            if result is not None and runtime.receipt_for(turn_id).status == "completed":
                return result[1]
            raise HTTPException(409, "turn_interrupted")
        target = self.query.ask_target()
        privacy = self.query.freeze_answer_privacy(project, local_only=target["execution_location"] == "local")
        request = freeze_turn_request("project.answer", turn_id=turn_id, session_id="session-" + turn_id,
            operation_id="answer-" + turn_id, idempotency_key="answer-" + turn_id,
            project_id=project, created_at=_now(), text=question, privacy=privacy,
            capabilities=["workbench.answer.execute"], template_version=2, situation=situation)
        if policy_versions is not None:
            request['policy_versions'] = dict(policy_versions)
        request["context_policy"].update(include_memory=False)
        from ..part_context_binding import bind_context
        bind_context(self.query.records, request, part_context)
        request["capability_request"] = {"mode": "execute_exact_v1",
            "capability_id": "workbench.answer.execute", "arguments": {"query": question}}
        validate_turn_request(request)
        execution = {"operation": operation}
        with self.lock:
            if turn_id in self.active:
                raise HTTPException(409, "turn_in_progress")
            self.active[turn_id] = execution
        try:
            await asyncio.to_thread(runner.accept_and_submit, request)
            deadline = monotonic() + 250
            receipt = None
            while monotonic() < deadline:
                receipt = await asyncio.to_thread(runner.wait_for_terminal, turn_id, timeout_seconds=.05)
                if receipt is not None:
                    break
                if self.paused(turn_id) is not None and runtime.receipt_for(turn_id).status == 'waiting_approval':
                    break
            if "error" in execution:
                if isinstance(execution['error'], AnswerInterrupted):
                    return execution['error'].result
                raise execution["error"]
            result = store.get_immutable_payload(turn_id, RESULT_KIND)
            if receipt is None or receipt.status != "completed" or result is None:
                raise HTTPException(502, "answer_generation_failed")
            return result[1]
        finally:
            if runtime.receipt_for(turn_id).status in {'completed', 'failed', 'cancelled'}:
                self._clear_prepared_plan(turn_id)
            with self.lock:
                self.active.pop(turn_id, None)


def generate_answer(models, messages, *, response_model, max_tokens, validate_current,
                    on_delta=None, purpose="primary", invocation_key="answer", timeout_seconds=None, gap_plan=None, on_retry=None, **unused):
    """Use the tool-owned model lifecycle; domain code never owns a wire call."""
    active = ACTIVE_ANSWER.get()
    if active is None:
        raise RuntimeError("answer model call requires an active kernel Turn")
    if gap_plan is not None:
        if purpose != 'aux' or invocation_key != 'gap-drilldown':
            raise ValueError('gap_invocation_changed')
        return _generate_gap_answer(models, messages, response_model=response_model, max_tokens=max_tokens,
            validate_current=validate_current, on_delta=on_delta, timeout_seconds=timeout_seconds, plan=gap_plan)
    request, store, query, handles = active
    def validate_wire():
        validate_current()
        query.validate_answer_request(models, store.get_request(request["turn_id"]))
    validate_wire()
    kind = "answer-model-result-" + invocation_key
    existing = store.get_immutable_payload(request["turn_id"], kind)
    if existing is not None:
        payload = existing[1]
        return response_model.model_validate(payload["output"]), payload["metadata"]
    store.get_or_create_immutable_payload(request["turn_id"], "answer-model-input-" + invocation_key,
        {"messages": messages, "purpose": purpose, "response_schema": response_model.model_json_schema()})
    from ..turn_routing import RecognitionRoutingSnapshot, SNAPSHOT_KIND, _revision
    frozen = store.get_immutable_payload(request["turn_id"], SNAPSHOT_KIND)
    if frozen is None:
        raise RuntimeError("answer model routing is unavailable")
    route = RecognitionRoutingSnapshot(frozen[0], _revision(frozen[1]), frozen[1]).generation_binding()
    main_models = models
    if purpose == 'aux':
        from .aux_routing import auxiliary_route
        models, route = auxiliary_route(models, store, request['turn_id'], request['scope']['project_id'], route)
    activation = None
    if purpose == 'primary':
        from .provider_store_binding import main_provider_store_activation
        activation = main_provider_store_activation(models, query.records,
            store.get_request(request['turn_id']), frozen[1])
    elif purpose == 'aux':
        from .provider_store_binding import auxiliary_provider_store_activation
        activation = auxiliary_provider_store_activation(main_models, models, query.records,
            store, store.get_request(request['turn_id']), frozen[1], route)
    inner = begin_nested_model_call(request, invocation_key=invocation_key, purpose=purpose)
    handle = _AnswerCall(inner, inner.runtime, request["turn_id"])
    handles[invocation_key] = handle
    try:
        output, metadata = models.complete_governed(messages, routing_snapshot=route,
            execution_control=execution_control_from(request), metadata_sink=handle, wire_attempt_sink=handle,
            response_model=response_model, max_tokens=max_tokens, validate_current=validate_wire,
            on_delta=on_delta, purpose=purpose, timeout_seconds=timeout_seconds, retry_policy=retry_policy_for(),
            **({'provider_store_activation': activation} if activation is not None else {}),
            **({'on_retry': on_retry} if on_retry is not None else {}))
        validate_wire()
        with handle.lock:
            if handle.terminal == "failed":
                handle.discard()
                raise TimeoutError("answer model result arrived after terminal")
            metadata = {**metadata, "kernel_receipt_refs": list(handle.finalize(error_code=None))}
            store.get_or_create_immutable_payload(request["turn_id"], kind,
                {"output": output.model_dump(), "metadata": metadata})
            return output, metadata
    except BaseException as error:
        from backend.shared.llm.model_transport import ModelInterrupted
        if isinstance(error, ModelInterrupted):
            handle.close_witness = error.close_witness
        if handle.terminal is not None:
            handle.discard()
        handle.finalize(error_code="ai.answer_model_failed")
        raise


def gap_answer_input(models, plan):
    """Prepare proof with the fixed original auxiliary route; allocate no call."""
    active = ACTIVE_ANSWER.get()
    if active is None:
        raise RuntimeError('gap model call requires an active kernel Turn')
    request, store, query, _ = active
    frozen_request = store.get_request(request['turn_id'])
    if (plan['project_id'] != request['scope']['project_id'] or plan['question'] != frozen_request['input']['text']
            or '_drilldown' not in plan
            or plan['policy_versions'] != frozen_request['policy_versions']):
        raise ValueError('gap_turn_identity_changed')
    query.validate_answer_request(models, frozen_request)
    from ..turn_routing import RecognitionRoutingSnapshot, SNAPSHOT_KIND, _revision
    from .aux_routing import auxiliary_route
    frozen = store.get_immutable_payload(request['turn_id'], SNAPSHOT_KIND)
    if frozen is None:
        raise RuntimeError('answer model routing is unavailable')
    primary = RecognitionRoutingSnapshot(frozen[0], _revision(frozen[1]), frozen[1]).generation_binding()
    selected, route = auxiliary_route(models, store, request['turn_id'], request['scope']['project_id'], primary)
    materials = query.gap_materials(plan, selected, local_only=route['execution_location'] == 'local_loopback')
    proof = {key: materials[key] for key in ('privacy', 'selection')}
    return selected, route, {'messages': materials['messages'], 'material_proof': proof, 'policy_versions': dict(plan['policy_versions'])}


def _generate_gap_answer(models, messages, *, response_model, max_tokens, validate_current, on_delta, timeout_seconds, plan):
    request, store, query, handles = ACTIVE_ANSWER.get()
    turn_id, invocation = request['turn_id'], 'gap-drilldown'
    input_kind, binding_kind = 'answer-gap-input-gap-drilldown-v1', 'answer-gap-binding-gap-drilldown-v1'
    result_kind = 'answer-model-result-' + invocation
    validate_current()
    selected, route, prepared = gap_answer_input(models, plan)
    if messages != prepared['messages']:
        raise ValueError('gap_messages_changed')
    payload = deepcopy({**prepared, 'purpose': 'aux', 'response_schema': response_model.model_json_schema(),
        'max_tokens': max_tokens, 'route_ref': route['payload_ref'], 'route_revision': route['revision']})

    def validate_wire():
        validate_current()
        _, current_route, current = gap_answer_input(models, plan)
        if (messages != payload['messages'] or current != {key: payload[key] for key in prepared}
                or current_route != route):
            raise ValueError('gap_inputs_changed')

    with handles.invocation_lock:
        validate_wire()
        saved_input = store.get_immutable_payload(turn_id, input_kind)
        binding = store.get_immutable_payload(turn_id, binding_kind)
        result = store.get_immutable_payload(turn_id, result_kind)
        if saved_input is not None or binding is not None or result is not None:
            if saved_input is None or saved_input[1] != payload or binding is None:
                raise ValueError('gap_invocation_unbound_or_changed')
            expected = {'version': 1, 'turn_id': turn_id, 'invocation_key': invocation, 'purpose': 'aux',
                'input_ref': saved_input[0], 'route_ref': route['payload_ref'], 'route_revision': route['revision']}
            fact = binding[1]
            if (not isinstance(fact, dict) or set(fact) != {*expected, 'model_request_id'}
                    or type(fact['version']) is not int
                    or any(fact[key] != value for key, value in expected.items())
                    or not isinstance(fact['model_request_id'], str) or not fact['model_request_id']):
                raise ValueError('gap_invocation_binding_invalid')
            events = [event for event in store.events_after(turn_id)
                if event.get('correlation', {}).get('model_request_id') == fact['model_request_id']]
            if not any(event['type'] == 'model.requested' and event['data'].get('model_call_purpose') == 'aux'
                    for event in events):
                raise ValueError('gap_invocation_call_unavailable')
            if result is None or not any(event['type'] == 'model.completed' for event in events):
                raise ValueError('gap_invocation_interrupted')
            return response_model.model_validate(result[1]['output']), result[1]['metadata']
        input_ref = store.get_or_create_immutable_payload(turn_id, input_kind, payload)
        inner = begin_nested_model_call(request, invocation_key=invocation, purpose='aux')
        handle = _AnswerCall(inner, inner.runtime, turn_id)
        handles[invocation] = handle
        try:
            store.get_or_create_immutable_payload(turn_id, binding_kind,
                {'version': 1, 'turn_id': turn_id, 'invocation_key': invocation, 'purpose': 'aux',
                 'model_request_id': handle.model_request_id, 'input_ref': input_ref,
                 'route_ref': route['payload_ref'], 'route_revision': route['revision']})
        except BaseException:
            handle.finalize(error_code='ai.answer_gap_binding_failed')
            raise
    try:
        output, metadata = selected.complete_governed(messages, routing_snapshot=route,
            execution_control=execution_control_from(request), metadata_sink=handle, wire_attempt_sink=handle,
            response_model=response_model, max_tokens=max_tokens, validate_current=validate_wire,
            on_delta=on_delta, purpose='aux', timeout_seconds=timeout_seconds)
        validate_wire()
        with handle.lock:
            if handle.terminal == 'failed':
                handle.discard()
                raise TimeoutError('answer model result arrived after terminal')
            metadata = {**metadata, 'kernel_receipt_refs': list(handle.finalize(error_code=None))}
            store.get_or_create_immutable_payload(turn_id, result_kind,
                {'output': output.model_dump(), 'metadata': metadata})
            return output, metadata
    except BaseException:
        if handle.terminal is not None:
            handle.discard()
        handle.finalize(error_code='ai.answer_model_failed')
        raise


def auxiliary_answer_models(models):
    active = ACTIVE_ANSWER.get()
    if active is None:
        return models
    request, store, _, _ = active
    from .aux_routing import auxiliary_models
    return auxiliary_models(models, store, request['turn_id'])


def answer_observation(invocation_key):
    """Read durable wire facts even when output validation or a deadline failed."""
    active = ACTIVE_ANSWER.get()
    if active is None:
        return {"receipt_ids": [], "observations": [], "complete": False}
    request, store, _, handles = active
    handle = handles.get(invocation_key)
    if handle is None:
        return {"receipt_ids": [], "observations": [], "complete": False}
    events = [event for event in store.events_after(request["turn_id"])
              if event.get("correlation", {}).get("model_request_id") == handle.model_request_id]
    dispatched = [event for event in events if event["type"] == "model.attempt.dispatched"]
    terminals = [event for event in events if event["type"] == "model.attempt.terminal"]
    refs = [event["data"]["receipt_ref"] for event in terminals]
    usage = [store.get(ref).get("usage") or {} for ref in refs]
    # An inflight attempt has only its immutable dispatch reference. The
    # eventual receipt can be located by its model_request_id/attempt_id.
    if len(terminals) < len(dispatched):
        refs.extend(event["data"]["payload_ref"] for event in dispatched[len(terminals):])
    return {"receipt_ids": refs, "observations": usage,
            "complete": bool(dispatched) and len(terminals) == len(dispatched) and all(usage)}
