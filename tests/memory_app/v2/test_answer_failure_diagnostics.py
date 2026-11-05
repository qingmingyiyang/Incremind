"""Synthetic kernel failure archival; no model, transport, or source services."""
from copy import deepcopy
import asyncio
from hashlib import sha256
import json
from types import SimpleNamespace

import pytest
from pydantic import BaseModel, ValidationError

from backend.memory_app.kernel import answer_turns
from backend.memory_app.model_config import ModelConfigurationError, ModelResponseDecodeError
from backend.memory_app.turn_routing import SNAPSHOT_KIND
from backend.recognition import RecognitionConflict, RecognitionError
from backend.shared.llm.json_mode import StructuredResponseDecodeError
from backend.shared.llm.model_transport import ModelInterrupted
from core.ai_kernel.event_store import RunLeaseRevoked


INVOCATION = 'evidence-answer-v1'
FAILURE = 'answer-model-failure-' + INVOCATION
RAW_MARKER = 'SYNTHETIC_PRIVATE_PROVIDER_BODY_NOT_FOR_DIAGNOSTICS'
USAGE = {'input_tokens': 4, 'output_tokens': 2, 'total_tokens': 6}


class Output(BaseModel):
    answer: str


class FrozenStore:
    def __init__(self, request):
        self.request = request
        self.payloads = {SNAPSHOT_KIND: ('route-ref', {})}
        self.fail_diagnostic = False

    def get_request(self, identity):
        assert identity == self.request['turn_id']
        return self.request

    def get_immutable_payload(self, identity, kind):
        assert identity == self.request['turn_id']
        return self.payloads.get(kind)

    def get_or_create_immutable_payload(self, identity, kind, payload):
        assert identity == self.request['turn_id']
        if self.fail_diagnostic and kind == FAILURE:
            raise OSError(RAW_MARKER)
        if kind in self.payloads:
            if self.payloads[kind][1] != payload:
                raise ValueError('immutable payload changed')
            return self.payloads[kind][0]
        ref = 'ref-' + kind
        self.payloads[kind] = ref, deepcopy(payload)
        return ref


@pytest.fixture
def env(monkeypatch):
    request = {'turn_id': 'diagnostic-turn', 'scope': {'project_id': 'synthetic-project'}}
    store, finalized, discarded, validation = FrozenStore(request), [], [], []
    runtime = SimpleNamespace(product_stops=SimpleNamespace(checkpoint=lambda identity: None),
        _append=lambda *args, **kwargs: discarded.append((args, kwargs)))
    inner = SimpleNamespace(runtime=runtime, model_request_id='model-request-synthetic',
        control=SimpleNamespace(purpose='primary'),
        finalize=lambda **kwargs: finalized.append(kwargs) or ('logical-receipt',))
    query = SimpleNamespace(records=object(),
        answer_turns=SimpleNamespace(active={request['turn_id']: {}}),
        validate_answer_request=lambda models, current: validation.append(current))
    value = SimpleNamespace(request=request, store=store, finalized=finalized, discarded=discarded,
        validation=validation, inner=inner, query=query, handles={}, calls=[], error=None,
        before_return=None, observed={'receipt_ids': ['wire-receipt-one'],
            'observations': [deepcopy(USAGE)], 'complete': True})

    def complete(messages, **kwargs):
        value.calls.append((messages, kwargs))
        if value.error is not None:
            raise value.error
        if value.before_return is not None:
            value.before_return(kwargs)
        return Output(answer='Synthetic answer'), {'usage': deepcopy(USAGE)}

    value.models = SimpleNamespace(complete_governed=complete)
    monkeypatch.setattr(answer_turns, 'begin_nested_model_call', lambda *args, **kwargs: inner)
    monkeypatch.setattr(answer_turns, 'answer_observation', lambda invocation: deepcopy(value.observed))
    from backend.memory_app import turn_routing
    monkeypatch.setattr(turn_routing, 'RecognitionRoutingSnapshot',
        lambda *args, **kwargs: SimpleNamespace(generation_binding=lambda: {}))
    from backend.memory_app.kernel import provider_store_binding
    monkeypatch.setattr(provider_store_binding, 'main_provider_store_activation', lambda *args: None)
    token = answer_turns.ACTIVE_ANSWER.set((request, store, query, value.handles))
    try:
        yield value
    finally:
        answer_turns.ACTIVE_ANSWER.reset(token)


def generate(env, *, enabled=True, validate=lambda: None):
    return answer_turns.generate_answer(env.models, [{'role': 'user', 'content': 'Synthetic question'}],
        response_model=Output, max_tokens=400, validate_current=validate,
        invocation_key=INVOCATION, failure_diagnostics=enabled)


def diagnostic(env):
    return env.store.payloads[FAILURE][1]


@pytest.mark.parametrize('code', ['json_empty', 'json_object_required', 'json_invalid', 'schema_invalid'])
@pytest.mark.parametrize('usage', [USAGE, {}, {'input_tokens': 4}])
def test_completed_decode_failure_binds_original_input_call_and_observations(env, code, usage):
    error = ModelResponseDecodeError(StructuredResponseDecodeError(code, 'model_response_decoding_failed',
        completed_wire=True, usage=usage))
    env.error = error
    env.observed = {'receipt_ids': ['earlier-wire', 'latest-wire', 'pending-dispatch'],
        'observations': [{'input_tokens': 3}, deepcopy(usage)], 'complete': False}
    with pytest.raises(ModelResponseDecodeError) as rejected:
        generate(env)
    assert rejected.value is error
    record = diagnostic(env)
    sent = env.store.payloads['answer-model-input-' + INVOCATION]
    assert record == {'version': 'answer-model-failure-v1', 'turn_id': env.request['turn_id'],
        'invocation': INVOCATION, 'model_request_id': env.inner.model_request_id, 'input_ref': sent[0],
        'response_schema_sha256': sha256(json.dumps(Output.model_json_schema(), ensure_ascii=False,
            sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest(),
        'category': 'decode', 'code': code, 'completed_wire': True, 'usage': usage,
        'observations': [{'input_tokens': 3}, usage],
        'receipt_ids': ['earlier-wire', 'latest-wire', 'pending-dispatch'], 'usage_complete': False}
    assert 'answer-model-result-' + INVOCATION not in env.store.payloads
    assert not any('review' in kind for kind in env.store.payloads)
    assert len(env.calls) == 1
    assert env.finalized == [{'error_code': 'ai.answer_model_failed'}]


@pytest.mark.parametrize('error,category,code', [
    (TimeoutError(RAW_MARKER), 'deadline', 'timeout'),
    (RecognitionConflict(RAW_MARKER), 'source', 'recognition_error'),
    (RecognitionError(RAW_MARKER), 'source', 'recognition_error'),
    (RunLeaseRevoked(), 'control', 'lease_revoked'),
    (asyncio.CancelledError(), 'control', 'cancelled'),
    (ModelInterrupted(partial='', interruption='connection'), 'control', 'interrupted'),
    (ModelConfigurationError('model_output_incomplete'), 'provider_output', 'model_output_incomplete'),
    (ModelConfigurationError('model_output_missing_content'), 'provider_output', 'model_output_missing_content'),
    (ModelConfigurationError('model_response_invalid'), 'provider_output', 'model_response_invalid'),
    (ModelConfigurationError('model_configuration_changed_before_request'), 'configuration',
        'model_configuration_changed_before_request'),
    (ModelConfigurationError('model_configuration_changed_during_request'), 'configuration',
        'model_configuration_changed_during_request'),
    (ModelConfigurationError('model_egress_remote_not_consented'), 'configuration', 'model_egress_remote_not_consented'),
    (ModelConfigurationError('model_request_failed: check endpoint, model and credentials'), 'model', 'model_request_failed'),
    (ModelConfigurationError(RAW_MARKER), 'other', 'unclassified_failure'),
    (ValueError(RAW_MARKER), 'other', 'unclassified_failure'),
])
def test_safe_classes_never_change_original_exception_or_claim_completed_wire(env, error, category, code):
    env.error = error
    with pytest.raises(type(error)) as rejected:
        generate(env)
    assert rejected.value is error
    record = diagnostic(env)
    assert (record['category'], record['code']) == (category, code)
    assert record['completed_wire'] is None
    assert record['usage'] == {}
    assert record['observations'] == [USAGE]
    assert record['usage_complete'] is True
    assert RAW_MARKER not in json.dumps(record)


def test_failure_archival_is_opt_in_and_does_not_run_for_success(env):
    env.error = ValueError(RAW_MARKER)
    with pytest.raises(ValueError):
        generate(env, enabled=False)
    assert FAILURE not in env.store.payloads
    env.error = None
    output, metadata = generate(env)
    assert output.answer == 'Synthetic answer'
    assert metadata['usage'] == USAGE
    assert FAILURE not in env.store.payloads
    assert 'answer-model-result-' + INVOCATION in env.store.payloads


def test_diagnostic_store_failure_preserves_the_original_provider_failure(env):
    env.error = ValueError(RAW_MARKER)
    env.store.fail_diagnostic = True
    with pytest.raises(ValueError) as rejected:
        generate(env)
    assert rejected.value is env.error
    assert FAILURE not in env.store.payloads
    assert env.finalized == [{'error_code': 'ai.answer_model_failed'}]
    assert len(env.calls) == 1


def test_diagnostic_observation_failure_preserves_the_original_source_failure(env, monkeypatch):
    def unavailable(invocation):
        raise OSError(RAW_MARKER)
    monkeypatch.setattr(answer_turns, 'answer_observation', unavailable)
    env.error = RecognitionConflict(RAW_MARKER)
    with pytest.raises(RecognitionConflict) as rejected:
        generate(env)
    assert rejected.value is env.error
    assert FAILURE not in env.store.payloads


def test_diagnostics_keep_only_normalized_usage_and_preserve_unknown_observations(env):
    env.error = ModelResponseDecodeError(StructuredResponseDecodeError('json_invalid', 'decode failed',
        completed_wire=True, usage={}))
    env.error.usage = {'input_tokens': 4, 'output_tokens': True, 'total_tokens': -1, 'provider_detail': RAW_MARKER}
    env.observed = {'receipt_ids': ['first-wire', 'second-wire'], 'observations': [
        {}, {'input_tokens': 2, 'output_tokens': RAW_MARKER}], 'complete': False}
    with pytest.raises(ModelResponseDecodeError):
        generate(env)
    record = diagnostic(env)
    assert record['usage'] == {'input_tokens': 4}
    assert record['observations'] == [{}, {'input_tokens': 2}]
    assert record['usage_complete'] is False
    assert RAW_MARKER not in json.dumps(record)


def test_first_immutable_failure_snapshot_is_not_overwritten(env):
    env.error = ModelConfigurationError('model_output_incomplete')
    with pytest.raises(ModelConfigurationError):
        generate(env)
    first = deepcopy(diagnostic(env))
    env.error = TimeoutError(RAW_MARKER)
    with pytest.raises(TimeoutError) as rejected:
        generate(env)
    assert rejected.value is env.error
    assert diagnostic(env) == first


def test_pydantic_inputs_and_exception_messages_are_never_archived(env):
    try:
        Output.model_validate({'answer': [RAW_MARKER]})
    except ValidationError as error:
        env.error = error
    with pytest.raises(ValidationError) as rejected:
        generate(env)
    assert rejected.value is env.error
    assert diagnostic(env)['code'] == 'unclassified_failure'
    assert RAW_MARKER not in json.dumps(diagnostic(env))


def test_failure_diagnostic_never_stringifies_unknown_errors(env):
    class UnsafeError(ValueError):
        def __str__(self):
            pytest.fail('Diagnostics must not stringify unknown provider errors.')
    env.error = UnsafeError()
    with pytest.raises(UnsafeError) as rejected:
        generate(env)
    assert rejected.value is env.error
    assert diagnostic(env)['code'] == 'unclassified_failure'


def test_source_guard_before_dispatch_is_unchanged_and_does_not_create_a_call(env):
    error = RecognitionConflict(RAW_MARKER)
    def revoked():
        raise error
    with pytest.raises(RecognitionConflict) as rejected:
        generate(env, validate=revoked)
    assert rejected.value is error
    assert env.calls == [] and env.finalized == []
    assert FAILURE not in env.store.payloads


def test_source_guard_after_response_cannot_become_an_accepted_output(env):
    error = RecognitionConflict(RAW_MARKER)
    calls = []
    def guard():
        calls.append(True)
        if len(calls) > 1:
            raise error
    with pytest.raises(RecognitionConflict) as rejected:
        generate(env, validate=guard)
    assert rejected.value is error
    assert diagnostic(env)['category'] == 'source'
    assert 'answer-model-result-' + INVOCATION not in env.store.payloads


def test_late_output_keeps_first_terminal_and_only_adds_a_failure_snapshot(env):
    env.before_return = lambda options: options['metadata_sink'].finalize(error_code='ai.answer_aux_timeout')
    with pytest.raises(TimeoutError):
        generate(env)
    assert env.finalized == [{'error_code': 'ai.answer_aux_timeout'}]
    assert len(env.discarded) == 1
    assert diagnostic(env)['code'] == 'timeout'
    assert 'answer-model-result-' + INVOCATION not in env.store.payloads
