"""Frozen auxiliary callers use real configuration, source owners and SQLite."""
import json
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from backend.memory_app.kernel.image_read import read_images
from backend.memory_app.model_config import ModelConfiguration, ModelConfigurationError
from backend.memory_app.structured_generation import generate_structured
from backend.memory_app.v2.image_read import (
    freeze_image_request, frozen_image_messages, validate_image_request,
)
from backend.memory_app.v2.memory_turn import MemoryTurn
from backend.memory_app.v2.policies import ACTIVE, register, version
from backend.memory_app.v2.privacy import egress_allowed, set_private_project
from backend.recognition import WorkScope
from backend.security.secrets import InMemorySecretStore
from core.storage_provider import SQLiteStructuredRecordStore
from tests.memory_app.test_fast_generation import choose
from tests.memory_app.v2.test_fast_aux_calls import fast_env, MemoryOutput
from tests.memory_app.v2.test_image_read import upload_fixture


class TransientFailure(RuntimeError):
    status_code = 503


def future_retry(request):
    if request['kind'] == 'limits':
        return {'header_timeout': 7, 'idle_timeout': 3, 'total_timeout': 30}
    return {'retry': False}


register('retry', '@14503')(future_retry)


def attempts(store, identity):
    return [store.get(event['data']['receipt_ref']) for event in store.events_after(identity)
        if event['type'] == 'model.attempt.terminal']


def memory(env, key):
    source = env.service.stage_experience(scope=WorkScope('local-user', 'alpha'),
        content='Synthetic memory')
    return MemoryTurn(env.records, env.model, kind='memory.overview', project='alpha', key=key,
        materials=[{'type': 'experience', 'id': source, 'project_id': 'alpha', 'revision': 1}],
        validate=lambda: None)


def test_memory_selected_fast_model_retries_frozen_recipe_and_replays_without_wire(fast_env, monkeypatch):
    env = fast_env
    choose(env.model, 'quick')
    turn = memory(env, 'frozen-memory-retry')
    original, calls, observed = env.model._completion_fn, [], []

    def provider(**request):
        calls.append(request)
        observed.append(version('retry'))
        if len(calls) == 1:
            raise TransientFailure('synthetic transient')
        return original(**request)

    env.model._completion_fn = provider
    monkeypatch.setitem(ACTIVE, 'retry', '@14503')
    values = dict(response_model=MemoryOutput, max_tokens=200)
    messages = [{'role': 'user', 'content': 'Synthetic memory'}]
    result = turn.generate(messages, **values)
    assert result[0].value == 'Synthetic result' and result[1]['model'] == 'quick'
    assert len(calls) == 2 and observed == ['@1', '@1']
    assert all(60 < float(call['timeout']) <= 120 for call in calls)
    assert calls[0]['messages'] == calls[1]['messages']
    assert [call['model'] for call in calls] == ['openai/quick', 'openai/quick']
    assert [row['status'] for row in attempts(turn.store, turn.turn_id)] == ['failed_transport', 'succeeded']
    assert len({row['attempt_id'] for row in attempts(turn.store, turn.turn_id)}) == 2
    assert turn.store.get_request(turn.turn_id)['policy_versions']['retry'] == '@1'
    assert turn.generate(messages, **values) == result and len(calls) == 2
    assert version('retry') == '@14503'


@pytest.mark.parametrize('changed', ['source', 'configuration', 'fast_selection'])
def test_memory_rechecks_source_and_selected_identity_before_retry(fast_env, changed):
    env = fast_env
    choose(env.model, 'quick')
    turn, calls = memory(env, 'memory-revoke-' + changed), []

    def provider(**request):
        calls.append(request)
        if changed == 'source':
            set_private_project(env.records, 'alpha', True, 0)
        elif changed == 'configuration':
            env.model.update('generation', {'model': 'changed', 'expected_revision': 1})
        else:
            choose(env.model, 'changed', 1)
        raise TransientFailure('synthetic transient')

    env.model._completion_fn = provider
    with pytest.raises(ValueError):
        turn.generate([{'role': 'user', 'content': 'Synthetic memory'}],
            response_model=MemoryOutput, max_tokens=200)
    assert len(calls) == 1
    assert [row['status'] for row in attempts(turn.store, turn.turn_id)] == ['failed_transport']
    assert turn.store.get_immutable_payload(turn.turn_id, 'memory-generation-output-v1') is None


@pytest.fixture
def vision(tmp_path):
    import litellm  # Existing gateway metadata is loaded outside the execution budget.
    records = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    calls = []
    def provider(**request):
        calls.append(request)
        return {'choices': [{'message': {'content': json.dumps({'images': [
            {'text': 'Synthetic original', 'description': 'Synthetic description'}]})}, 'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 24, 'completion_tokens': 12}}
    models = ModelConfiguration(records, tmp_path, InMemorySecretStore(), completion_fn=provider)
    models.update('vision', {'base_url': 'https://vision.invalid/v1', 'model': 'vision-fake',
        'api_key': 'synthetic-only', 'allow_remote': True, 'expected_revision': 0})
    models.update_vision_mode(mode='remote', expected_revision=0)
    messages = [{'role': 'user', 'content': upload_fixture(records, tmp_path, 'image-1')}]
    args = dict(project='alpha', key='vision-retry',
        materials=[{'type': 'original_item', 'id': 'image-1', 'revision': 1, 'project_id': 'alpha'}],
        validate=lambda: None,
        freeze_request=lambda kind, **values: freeze_image_request(kind, messages=messages, **values),
        validate_request=validate_image_request, remote_allowed=egress_allowed, messages=messages,
        load_messages=lambda request: frozen_image_messages(records, models, request))
    return SimpleNamespace(records=records, models=models, calls=calls, messages=messages,
        arguments=args, root=tmp_path)


def test_image_read_uses_frozen_retry_and_actual_remaining_budget_with_completed_replay(vision, monkeypatch):
    env = vision
    original, seen = env.models._completion_fn, []
    def provider(**request):
        seen.append(request)
        if len(seen) == 1:
            # ACTIVE changes after the durable request has been frozen.
            monkeypatch.setitem(ACTIVE, 'retry', '@14503')
            raise TransientFailure('synthetic transient')
        return original(**request)
    env.models._completion_fn = provider
    result = read_images(env.records, env.models, **env.arguments)
    assert result[0].images[0].text == 'Synthetic original'
    assert len(seen) == 2 and all(60 < float(call['timeout']) <= 120 for call in seen)
    assert seen[0]['messages'] == seen[1]['messages']
    store = MemoryTurn.store_for(env.records)
    assert store.get_request(result[2])['policy_versions'] == {'image_read': '@1', 'retry': '@1'}
    assert [row['status'] for row in attempts(store, result[2])] == ['failed_transport', 'succeeded']
    assert read_images(env.records, env.models, **env.arguments) == result and len(seen) == 2
    assert version('retry') == '@14503'


@pytest.mark.parametrize('changed', ['source', 'configuration', 'permission', 'mode', 'file'])
def test_image_retry_rechecks_real_frozen_original_configuration_and_consent(vision, changed):
    env = vision
    calls = []
    def provider(**request):
        calls.append(request)
        if changed == 'source':
            set_private_project(env.records, 'alpha', True, 0)
        elif changed == 'configuration':
            env.models.update('vision', {'model': 'changed', 'expected_revision': 1})
        elif changed == 'permission':
            env.models.update('vision', {'allow_remote': False, 'expected_revision': 1})
        elif changed == 'mode':
            env.models.update_vision_mode(mode='local', expected_revision=1)
        else:
            (env.root / 'workspace/image-1-0.png').write_bytes(b'synthetic changed original')
        raise TransientFailure('synthetic transient')
    env.models._completion_fn = provider
    with pytest.raises(ValueError):
        read_images(env.records, env.models, **env.arguments)
    identity = env.records.list('v2_memory_turn_keys')[0].object_id
    store = MemoryTurn.store_for(env.records)
    assert len(calls) == 1
    assert [row['status'] for row in attempts(store, identity)] == ['failed_transport']
    assert store.get_immutable_payload(identity, 'memory-generation-output-v1') is None


def test_default_none_keeps_original_direct_vision_single_wire_and_sixty_second_timeout(vision):
    env = vision
    calls = []
    def provider(**request):
        calls.append(request)
        raise TransientFailure('synthetic transient')
    env.models._completion_fn = provider
    from backend.memory_app.kernel.image_read import ImageReadOutput
    with pytest.raises(ModelConfigurationError, match='vision_request_failed'):
        env.models.complete_vision(env.messages, response_model=ImageReadOutput)
    assert len(calls) == 1 and calls[0]['timeout'] == 60


def test_structured_adapter_forwards_supported_optional_retry_and_timeout_only():
    class Output(BaseModel):
        value: str
    calls = []
    sink, policy = object(), future_retry
    class Native:
        def complete_structured(self, messages, *, response_model, max_tokens, validate_current,
                                wire_attempt_sink, timeout_seconds=None, retry_policy=None):
            calls.append((messages, wire_attempt_sink, timeout_seconds, retry_policy))
            validate_current()
            return response_model(value='native'), {}
    messages = [{'role': 'user', 'content': 'Synthetic adapter'}]
    result = generate_structured(Native(), messages, response_model=Output, max_tokens=10,
        validate_current=lambda: None, wire_attempt_sink=sink, timeout_seconds=9, retry_policy=policy)
    assert result[0].value == 'native' and calls == [(messages, sink, 9, policy)]
    class Legacy:
        def complete(self, messages, *, max_tokens, validate_current):
            validate_current()
            return '{"value":"legacy"}', {}
    assert generate_structured(Legacy(), messages, response_model=Output, max_tokens=10,
        validate_current=lambda: None, timeout_seconds=9, retry_policy=policy)[0].value == 'legacy'
