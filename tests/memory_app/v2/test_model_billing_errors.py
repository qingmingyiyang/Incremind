import asyncio
import json

import pytest
from fastapi import HTTPException

from backend.memory_app.kernel.ai_runtime import RecallFirstLocalPlanner
from backend.memory_app.kernel.answer_turns import (
    ProductAnswerCapability, ProductAnswerTurns, _answer_failure_classification, answer_definition,
)
from backend.memory_app.model_config import ModelConfigurationError
from backend.memory_app.v2.policies import get
from backend.memory_app.v2.turn_execution import REQUESTS, SAFE_ERRORS, TurnExecutionService, safe_failure
from backend.memory_app.v2.turn_frames import STREAMS, TurnFrames, failed_stream_event
from backend.memory_app.v2.workbench_stream import read_turn_stream
from core.ai_kernel import ScopedCapabilityRegistry, SynchronousAIRuntime
from core.ai_kernel.sqlite_store import SQLiteAITurnStore
from core.ai_kernel.turn_kinds import freeze_turn_request
from core.storage_provider.sqlite_uow import SQLiteStructuredRecordStore


CASES = (
    ('server_model_balance_insufficient', 402),
    ('server_model_balance_account_blocked', 402),
    ('server_model_balance_bound_unavailable', 409),
    ('server_model_balance_price_unavailable', 409),
    ('server_model_balance_model_forbidden', 403),
    ('server_model_binding_changed', 409),
    ('server_model_purpose_unavailable', 409),
    ('server_model_profile_disabled', 409),
    ('server_model_credential_unavailable', 409),
    ('server_model_secret_unavailable', 409),
    ('server_model_not_allowed', 403),
    ('server_model_balance_job_unauthorized', 403),
    ('server_model_billing_owner_unavailable', 409),
    ('server_model_billing_unavailable', 409),
)
PRIVATE = 'PRIVATE_PROVIDER_TEXT_FOR_ERROR_BOUNDARY'


@pytest.mark.parametrize('code,status', CASES)
def test_finite_configuration_failures_remain_safe_and_classified(code, status):
    error = ModelConfigurationError(code)
    assert _answer_failure_classification(error) == ('server_model', code, None)
    assert safe_failure(error) == code
    assert safe_failure(HTTPException(status, code)) == code
    assert code in SAFE_ERRORS


@pytest.mark.parametrize('error', (
    ModelConfigurationError(PRIVATE),
    ModelConfigurationError('server_model_balance_' + PRIVATE),
    ModelConfigurationError('server_model_balance_insufficient', PRIVATE),
    ModelConfigurationError({'code': 'server_model_balance_insufficient', 'body': PRIVATE}),
    ValueError('server_model_balance_insufficient'),
    ValueError(PRIVATE),
))
def test_unknown_failure_text_and_wrong_exception_types_stay_private(error):
    assert _answer_failure_classification(error) == ('other', 'unclassified_failure', None)
    assert safe_failure(error) == 'answer_generation_failed'


@pytest.mark.parametrize('code', (
    'server_model_billing_binding_invalid', 'server_model_billing_wire_not_reserved',
    'server_model_billing_wire_already_invoked',
))
def test_internal_billing_protocol_details_are_not_public(code):
    assert _answer_failure_classification(ModelConfigurationError(code)) == ('other', 'unclassified_failure', None)
    assert safe_failure(ModelConfigurationError(code)) == 'answer_generation_failed'
    assert safe_failure(HTTPException(409, code)) == 'answer_generation_failed'


def test_wrong_exception_cannot_advertise_a_billing_failure_through_detail():
    error = ValueError(PRIVATE)
    error.detail = 'server_model_balance_insufficient'
    error.code = 'insufficient_quota'
    assert safe_failure(error) == 'answer_generation_failed'


@pytest.mark.parametrize('code', (
    'source_changed_retry', 'remote_disabled', 'private_project_remote_blocked', 'answer_generation_failed',
))
def test_existing_source_privacy_and_generic_failures_keep_their_codes(code):
    assert safe_failure(HTTPException(409, code)) == code


@pytest.mark.parametrize('code,status', CASES)
def test_real_request_owner_persists_and_replays_finite_failure(tmp_path, code, status):
    records = SQLiteStructuredRecordStore(tmp_path / 'requests.sqlite3')
    calls = []

    async def reject(body, **unused):
        calls.append(dict(body))
        raise ModelConfigurationError(code)

    owner = TurnExecutionService(records, reject, instance='billing-error-instance')
    body = {'intent': 'ask', 'project_id': 'billing-project', 'text': '问题'}
    for _ in range(2):
        with pytest.raises(HTTPException) as rejected:
            asyncio.run(owner.run(body, 'billing-error-request'))
        assert rejected.value.status_code == status
        assert rejected.value.detail == code
    assert calls == [body]
    row = records.read(REQUESTS, 'billing-error-request')
    assert (row.payload['state'], row.payload['error'], row.payload['status_code']) == ('failed', code, status)


@pytest.mark.parametrize('code,status', CASES)
@pytest.mark.parametrize('kind', ('configuration', 'http'))
def test_actual_answer_capability_terminal_reconnects_without_dispatch_or_writes(tmp_path, code, status, kind):
    root = tmp_path / 'isolated-root'
    store = SQLiteAITurnStore(root / '.rebuild-data/ai-turns.sqlite3')
    records = SQLiteStructuredRecordStore(root / 'product.sqlite3')
    identity = 'turn-billing-error'
    request = freeze_turn_request('project.answer', turn_id=identity, session_id='session-billing-error',
        operation_id='operation-billing-error', idempotency_key='request-billing-error',
        project_id='billing-project', created_at='2026-10-24T00:00:00Z', text='问题',
        privacy={'mode': 'local_only', 'allow_remote': False, 'pii': 'possible',
                 'consent_refs': [], 'retention': 'session'},
        template_version=2, capabilities=['workbench.answer.execute'])
    request['capability_request'] = {'mode': 'execute_exact_v1',
        'capability_id': 'workbench.answer.execute', 'arguments': {'query': '问题'}}
    service = ProductAnswerTurns(None, root, None)
    calls = []

    async def reject():
        calls.append(identity)
        raise ModelConfigurationError(code) if kind == 'configuration' else HTTPException(status, code)

    service.active[identity] = {'operation': reject}
    registry = ScopedCapabilityRegistry()
    registry.register(answer_definition(), ProductAnswerCapability(service, store))
    runtime = SynchronousAIRuntime(planner=RecallFirstLocalPlanner(), registry=registry,
        events=store, payloads=store, state=store)
    receipt = runtime.submit_turn(request)
    events = tuple(store.events_after(identity))
    assert receipt.status == 'failed'
    assert events[-1]['type'] == 'turn.failed'
    assert events[-1]['data']['error_code'] == code
    outcome = next(event for event in events if event['type'] == 'tool.outcome.recorded')
    assert store.get(outcome['data']['payload_ref'])['effect_certainty'] == 'confirmed_none'
    # 此处仅验证真实工具失败边界，没有创建模型请求或声称发生外发。
    assert not any(event['type'].startswith('model.') for event in events)
    assert store.get_immutable_payload(identity, 'product-answer-result-v2') is None
    assert failed_stream_event(root, identity, request, safe_errors=SAFE_ERRORS) == {'code': code}
    turn = {'id': identity, 'thread_id': 'billing-thread', 'intent': 'ask',
        'user_text': '问题', 'created_at': '2026-10-24T00:00:00Z'}
    assert TurnFrames(records, turn_id=identity, project_id='billing-project', recipe=get('retry'),
        turn=turn, request=request, schedule=False).start()
    before = records.read(STREAMS, identity)

    async def reconnect():
        response = read_turn_stream(records, root, identity, 'billing-project', 0,
            view=None, safe_errors=SAFE_ERRORS)
        return ''.join([chunk async for chunk in response.body_iterator])

    raw = asyncio.run(reconnect())
    assert raw == 'event: error\ndata: ' + json.dumps({'code': code}) + '\n\n'
    assert records.read(STREAMS, identity) == before
    assert tuple(store.events_after(identity)) == events
    assert runtime.submit_turn(request).status == 'failed'
    assert calls == [identity]

