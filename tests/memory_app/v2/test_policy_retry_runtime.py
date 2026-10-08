"""Frozen retry versions execute against actual SQLite attempts and gateway."""
import pytest
import json
from pathlib import Path

from backend.memory_app.kernel.policy_runtime import ProductPolicyRuntime
from backend.memory_app.model_config import ModelConfiguration
from backend.memory_app.v2.policies import ACTIVE, get, override, register, version
from backend.memory_app.v2.policies.pipelines import TURN_PIPELINES, versions_for_turn
from core.ai_kernel import ScopedCapabilityRegistry, SQLiteAITurnStore
from backend.security.secrets import InMemorySecretStore
from core.storage_provider import SQLiteStructuredRecordStore
from tests.rebuild.test_product_turn_kinds import request
from core.ai_kernel.contracts import _POLICY_INTERFACES


class TransientFailure(RuntimeError):
    status_code = 503


@pytest.fixture(scope='module', autouse=True)
def future_version():
    # A real registered alternate recipe makes ACTIVE drift observable at wire.
    def future(value):
        if value.get('kind') == 'limits':
            return {'header_timeout': 7, 'idle_timeout': 3, 'total_timeout': 30}
        return {'retry': False}
    register('retry', '@14501')(future)
    previous = get('retry', version='@1')

    def display_future(value):
        if value.get('kind') == 'retry_display':
            return {'reason': value['reason'], 'used': value['counts'][value['budget']], 'limit': 23}
        return previous(value)

    register('retry', '@14502')(display_future)


def runtime(path, calls, observed, secrets=None, on_retry=None, retry_after=None):
    store = SQLiteAITurnStore(path)

    def provider(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            error = TransientFailure('synthetic')
            if retry_after is not None:
                error.headers = {'Retry-After': str(retry_after)}
            raise error
        return {'choices': [{'message': {'content': 'done'}, 'finish_reason': 'stop'}],
                'usage': {'prompt_tokens': 2, 'completion_tokens': 1, 'total_tokens': 3}}

    records = SQLiteStructuredRecordStore(path.parent / 'records.sqlite3')
    models = ModelConfiguration(records, path.parent, secrets or InMemorySecretStore(), completion_fn=provider)
    if records.read('recognition_model_config', 'generation') is None:
        models.update('generation', {'base_url': 'https://synthetic.invalid/v1', 'model': 'synthetic',
            'api_key': 'synthetic-only', 'allow_remote': True, 'expected_revision': 0})

    class Planner:
        def plan(self, value, events, capabilities, payloads, execution_control):
            observed.append((version('retry'), get('retry')({'kind': 'limits'})))
            control = execution_control
            cfg = models.public()['generation']
            public = {key: cfg[key] for key in ('purpose', 'provider', 'base_url', 'model',
                'allow_remote', 'revision', 'configured', 'has_api_key')}
            ref = store.get_or_create_immutable_payload(value['turn_id'], 'memory-model-route-v1', public)
            binding = {'payload_ref': ref, 'revision': 'a' * 64,
                'prompt_cache_scope_identity': 'b' * 64, 'configuration': public, 'execution_location': 'remote'}
            models.complete_governed([{'role': 'user', 'content': 'synthetic'}],
                routing_snapshot=binding, execution_control=control, metadata_sink=control,
                wire_attempt_sink=control, retry_policy=get('retry'), on_retry=on_retry)
            return {'type': 'complete', 'summary': 'done'}

    return ProductPolicyRuntime(planner=Planner(), registry=ScopedCapabilityRegistry(),
        events=store, payloads=store, state=store), store


@pytest.mark.parametrize('kind', list(TURN_PIPELINES))
def test_every_actual_model_recipe_freezes_registered_retry(kind):
    assert versions_for_turn(kind)['retry'] == '@1'
    assert get('retry')({'kind': 'limits'}) == {
        'header_timeout': 180, 'idle_timeout': 20, 'total_timeout': 600}


def test_contract_adds_only_retry_to_existing_eighteen_interface_names():
    previous = {'organize', 'place', 'extract', 'route', 'steer', 'scope', 'retrieve',
        'rank', 'strength', 'forget', 'enough', 'compose', 'trigger', 'consolidate',
        'search', 'nudge', 'handoff', 'image_read'}
    schema = json.loads((Path(__file__).resolve().parents[3] /
        'core-contracts/ai/turn-request.schema.json').read_text(encoding='utf-8'))
    assert _POLICY_INTERFACES == previous | {'retry'}
    assert set(schema['properties']['policy_versions']['propertyNames']['enum']) == previous | {'retry'}


@pytest.mark.parametrize('saved_map', [None, {'rank': '@1'}, {'retry': '@1'}])
def test_old_or_explicit_map_survives_restart_active_change_and_real_attempts(tmp_path, monkeypatch, saved_map):
    calls, seen = [], []
    path = tmp_path / 'turns.sqlite3'
    secrets = InMemorySecretStore()
    initial, store = runtime(path, calls, seen, secrets=secrets)
    value = request('project.answer', turn_id='turn-' + 'a' * 32,
        operation_id='op-retry-policy', idempotency_key='retry-policy', capabilities=[])
    if saved_map is not None:
        value['policy_versions'] = saved_map
    initial.accept_turn(value)
    restored, reopened = runtime(path, calls, seen, secrets=secrets)
    monkeypatch.setitem(ACTIVE, 'retry', '@14501')
    with override(retry='@14501'):
        receipt = restored.run_accepted_turn(value['turn_id'])
        assert version('retry') == '@14501'
    assert receipt.status == 'completed'
    assert len(calls) == 2
    assert seen == [('@1', {'header_timeout': 180, 'idle_timeout': 20, 'total_timeout': 600})]
    assert reopened.get_request(value['turn_id']) == value
    terminals = [reopened.get(e['data']['receipt_ref']) for e in reopened.events_after(value['turn_id'])
                 if e['type'] == 'model.attempt.terminal']
    assert [r['status'] for r in terminals] == ['failed_transport', 'succeeded']
    assert len({r['attempt_id'] for r in terminals}) == 2


def test_unknown_frozen_retry_fails_before_provider_and_restores_caller(tmp_path):
    calls, seen = [], []
    runner, store = runtime(tmp_path / 'turns.sqlite3', calls, seen)
    value = request('project.answer', turn_id='turn-' + 'b' * 32,
        operation_id='op-retry-unknown', idempotency_key='retry-unknown', capabilities=[])
    value['policy_versions'] = {'retry': '@14599'}
    runner.accept_turn(value)
    with override(retry='@14501'):
        with pytest.raises(ValueError, match='unknown_policy_version'):
            runner.run_accepted_turn(value['turn_id'])
        assert version('retry') == '@14501'
    assert calls == seen == []
    assert not any(e['type'] == 'model.attempt.dispatched' for e in store.events_after(value['turn_id']))


def test_new_selected_future_recipe_is_not_reinterpreted_as_current_default(tmp_path):
    calls, seen = [], []
    runner, store = runtime(tmp_path / 'turns.sqlite3', calls, seen)
    value = request('project.answer', turn_id='turn-' + 'c' * 32,
        operation_id='op-retry-future', idempotency_key='retry-future', capabilities=[])
    value['policy_versions'] = {'retry': '@14501'}
    result = runner.submit_turn(value)
    assert result.status == 'failed'
    assert len(calls) == 1
    assert seen == [('@14501', {'header_timeout': 7, 'idle_timeout': 3, 'total_timeout': 30})]
    assert store.get_request(value['turn_id']) == value


@pytest.mark.parametrize('budget,reason,limit', [
    ('before_output', 'server', 10), ('thinking_error', 'connection', 2),
    ('thinking_stall', 'stalled', 1), ('header', 'header_timeout', 1),
    ('fallback', 'malformed_stream', 1),
])
def test_frozen_retry_display_reports_only_safe_classification_and_its_budget(budget, reason, limit):
    selected = get('retry', version='@1')
    counts = {budget: limit}
    assert selected({'kind': 'retry_display', 'budget': budget, 'reason': reason,
                     'counts': counts}) == {'reason': reason, 'used': limit, 'limit': limit}
    assert counts == {budget: limit}
    assert selected({'kind': 'limits'}) == {
        'header_timeout': 180, 'idle_timeout': 20, 'total_timeout': 600}
    assert selected({'kind': 'server', 'phase': 'before_output', 'counts': {},
                     'remaining': 600, 'jitter': 0}) == {
        'retry': True, 'budget': 'before_output', 'extra_budget': None,
        'delay': 1, 'non_stream': False}
    assert selected({'kind': 'server', 'phase': 'body'}) == {'retry': False}


@pytest.mark.parametrize('changes', [
    {'budget': 'unknown'}, {'reason': 'provider-body'},
    {'counts': {'before_output': True}}, {'counts': {'before_output': -1}},
    {'counts': {'before_output': 11}}, {'counts': {}}, {'counts': []},
])
def test_retry_display_rejects_unsafe_or_uncommitted_counts(changes):
    value = {'kind': 'retry_display', 'budget': 'before_output',
             'reason': 'server', 'counts': {'before_output': 1}, **changes}
    with pytest.raises(ValueError, match='invalid_retry_display'):
        get('retry', version='@1')(value)


@pytest.mark.parametrize('saved_version,expected_limit', [(None, 10), ('@1', 10), ('@14502', 23)])
def test_retry_display_uses_saved_policy_even_when_active_and_caller_change(tmp_path, monkeypatch,
                                                                         saved_version, expected_limit):
    calls, seen, notices = [], [], []
    runner, store = runtime(tmp_path / 'turns.sqlite3', calls, seen,
        on_retry=notices.append, retry_after=1)
    value = request('project.answer', turn_id='turn-' + 'd' * 32,
        operation_id='op-retry-display', idempotency_key='retry-display', capabilities=[])
    if saved_version is not None:
        value['policy_versions'] = {'retry': saved_version}
    runner.accept_turn(value)
    monkeypatch.setitem(ACTIVE, 'retry', '@14501')
    with override(retry='@14501'):
        assert runner.run_accepted_turn(value['turn_id']).status == 'completed'
        assert version('retry') == '@14501'
    assert notices == [{'attempt': 1, 'delay': 1.0, 'budget': 'before_output',
                        'reason': 'server', 'used': 1, 'limit': expected_limit}]
    assert len(calls) == 2
    assert seen == [(saved_version or '@1', {'header_timeout': 180, 'idle_timeout': 20,
                                            'total_timeout': 600})]
    assert store.get_request(value['turn_id']) == value
    terminals = [store.get(e['data']['receipt_ref']) for e in store.events_after(value['turn_id'])
                 if e['type'] == 'model.attempt.terminal']
    assert [r['status'] for r in terminals] == ['failed_transport', 'succeeded']
    assert len({r['attempt_id'] for r in terminals}) == 2
