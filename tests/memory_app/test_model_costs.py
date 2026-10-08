"""Actual SQLite Turn, ModelConfiguration and gateway; only the wire is synthetic."""
import json
import sqlite3

import pytest
from pydantic import BaseModel

from backend.memory_app.model_config import ModelConfiguration, ModelConfigurationError
from backend.security.secrets import InMemorySecretStore
from core.ai_kernel import SQLiteAITurnStore, SynchronousAIRuntime, ScopedCapabilityRegistry
from core.ai_kernel.turn_kinds import freeze_turn_request
from core.storage_provider import SQLiteStructuredRecordStore, SQLiteUnitOfWorkConflict


RATES = {'input_per_million': '2', 'output_per_million': '3', 'cache_read_per_million': '0.1'}


def configured(tmp_path, completion=None):
    records = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    models = ModelConfiguration(records, tmp_path, InMemorySecretStore(), completion_fn=completion)
    models.update('generation', {'base_url': 'https://proxy.invalid/v1', 'model': 'writer',
        'api_key': 'test-private-value', 'allow_remote': True, 'expected_revision': 0})
    return records, models


def save_prices(models, rates=RATES, revision=0):
    return models.update_model_prices('generation', rates, expected_revision=revision,
        expected_configuration_revision=models.public()['generation']['revision'])


def test_manual_prices_use_independent_cas_and_do_not_mutate_configuration(tmp_path):
    records, models = configured(tmp_path)
    before, public = records.read('recognition_model_config', 'generation'), models.public()
    assert models.model_prices('generation')['rates'] is None
    saved = save_prices(models)
    assert saved['rates'] == RATES and saved['revision'] == 1 and saved['source'] == 'manual'
    assert records.read('recognition_model_config', 'generation') == before
    assert models.public() == public
    with pytest.raises(SQLiteUnitOfWorkConflict):
        save_prices(models, revision=0)
    assert models.model_prices('generation') == saved
    cleared = save_prices(models, {key: None for key in RATES}, revision=1)
    assert all(value is None for value in cleared['rates'].values())


def test_manual_price_binding_does_not_follow_a_different_model_or_configuration(tmp_path):
    _, models = configured(tmp_path)
    save_prices(models)
    models.update('generation', {'model': 'another-writer', 'expected_revision': 1})
    assert models.model_prices('generation')['rates'] is None
    with pytest.raises(ModelConfigurationError, match='model_price_configuration_changed'):
        models.update_model_prices('generation', RATES, expected_revision=1, expected_configuration_revision=1)


def run_wire(tmp_path, *, usage=None, output='ok', failure=False, change_prices=False, configured_price=True,
             validation_failure=False):
    wire_calls = []
    records, models = configured(tmp_path)
    if configured_price:
        save_prices(models)
    def completion(**request):
        wire_calls.append(request)
        snapshots = records.list('v2_model_wire_prices')
        assert len(snapshots) == 1, 'price must be durable before the real wire'
        assert 'test-private-value' not in json.dumps(snapshots[0].payload)
        assert 'proxy.invalid' not in json.dumps(snapshots[0].payload)
        if change_prices:
            save_prices(models, {key: '99' for key in RATES}, revision=1)
        if failure:
            raise RuntimeError('private provider exception')
        return {'choices': [{'message': {'content': output}, 'finish_reason': 'stop'}],
                'usage': usage if usage is not None else {'prompt_tokens': 1000, 'completion_tokens': 600,
                    'prompt_cache_hit_tokens': 200, 'prompt_cache_miss_tokens': 800}}
    models._completion_fn = completion
    store = SQLiteAITurnStore(tmp_path / '.rebuild-data/ai-turns.sqlite3')
    identity = 'turn-cost-real'
    class Output(BaseModel):
        value: int
    class Planner:
        def plan(self, request, events, capabilities, payloads, execution_control):
            cfg = models.public()['generation']
            public = {key: cfg[key] for key in ('purpose', 'provider', 'base_url', 'model',
                'allow_remote', 'revision', 'configured', 'has_api_key')}
            ref = store.get_or_create_immutable_payload(identity, 'memory-model-route-v1', public)
            route = {'payload_ref': ref, 'revision': 'a' * 64, 'prompt_cache_scope_identity': 'b' * 64,
                'configuration': public, 'execution_location': 'remote'}
            models.complete_governed([{'role': 'user', 'content': 'synthetic private input'}],
                routing_snapshot=route, execution_control=execution_control,
                metadata_sink=execution_control, wire_attempt_sink=execution_control,
                response_model=Output if validation_failure else None)
            return {'type': 'complete', 'summary': 'Synthetic completed'}
    runtime = SynchronousAIRuntime(planner=Planner(), registry=ScopedCapabilityRegistry(),
        events=store, payloads=store, state=store)
    request = freeze_turn_request('project.answer', turn_id=identity, session_id='session-cost',
        operation_id='operation-cost', idempotency_key=identity, project_id='alpha',
        created_at='2026-10-04T00:00:00Z', text='synthetic', capabilities=[],
        privacy={'mode': 'remote_allowed', 'allow_remote': True, 'pii': 'possible',
            'consent_refs': ['crp://default/model-settings/generation'], 'retention': 'session'})
    receipt = runtime.submit_turn(request)
    return records, models, store, receipt, wire_calls


def test_failed_logical_validation_keeps_successful_paid_wire_cost(tmp_path):
    from backend.memory_app.kernel.receipt_projection import kernel_call_groups
    records, _, _, receipt, wires = run_wire(tmp_path, output='{"value":"invalid"}', validation_failure=True)
    assert receipt.status == 'failed' and len(wires) == 1
    calls = kernel_call_groups(tmp_path, records=records)[0]['calls']
    assert len(calls) == 1 and calls[0]['status'] == 'failed'
    assert calls[0]['cost'] == {'currency': 'CNY', 'amount': '0.00342'}


@pytest.mark.parametrize('first_unknown', [False, True])
def test_two_real_attempts_count_once_each_and_a_missing_charge_is_not_a_complete_total(tmp_path, first_unknown):
    from backend.memory_app.kernel.receipt_projection import kernel_call_groups, aggregate_cost
    from backend.memory_app.v2.settings import _receipts
    records, models = configured(tmp_path)
    save_prices(models)
    wires = []
    def completion(**request):
        wires.append(request)
        assert len(records.list('v2_model_wire_prices')) == len(wires)
        if first_unknown and len(wires) == 1:
            raise RuntimeError('synthetic transport failure')
        return {'choices': [{'message': {'content': 'ok'}, 'finish_reason': 'stop'}],
            'usage': {'prompt_tokens':1000,'completion_tokens':600,'prompt_cache_hit_tokens':200,'prompt_cache_miss_tokens':800}}
    models._completion_fn = completion
    store = SQLiteAITurnStore(tmp_path / '.rebuild-data/ai-turns.sqlite3')
    identity = 'turn-cost-two'
    class Planner:
        def plan(self, request, events, capabilities, payloads, execution_control):
            cfg = models.public()['generation']
            ref = store.get_or_create_immutable_payload(identity, 'memory-model-route-v1', cfg)
            execution_control.model_call_routed(snapshot_ref=ref, snapshot_revision='a'*64,
                prompt_cache_scope_identity='b'*64, provider='openai', model='writer', execution_location='remote')
            execution_control.model_call_started(provider='openai', model='writer')
            for index in range(2):
                if index == 0 and first_unknown:
                    with pytest.raises(ModelConfigurationError, match='model_request_failed'):
                        models.complete([{'role':'user','content':'synthetic'}], wire_attempt_sink=execution_control)
                else:
                    models.complete([{'role':'user','content':'synthetic'}], wire_attempt_sink=execution_control)
            execution_control.model_call_completed(usage={'input_tokens':1000,'output_tokens':600})
            return {'type':'complete','summary':'two attempts'}
    runtime = SynchronousAIRuntime(planner=Planner(), registry=ScopedCapabilityRegistry(), events=store, payloads=store, state=store)
    request = freeze_turn_request('project.answer', turn_id=identity, session_id='session-two', operation_id='op-two',
        idempotency_key=identity, project_id='alpha', created_at='2026-10-04T00:00:00Z', text='synthetic', capabilities=[],
        privacy={'mode':'remote_allowed','allow_remote':True,'pii':'possible',
            'consent_refs':['crp://default/model-settings/generation'],'retention':'session'})
    assert runtime.submit_turn(request).status == 'completed'
    assert len(wires) == 2 and len(records.list('v2_model_wire_prices')) == 2
    expected = None if first_unknown else {'currency':'CNY','amount':'0.00684'}
    calls = kernel_call_groups(tmp_path, records=records)[0]['calls']
    assert len(calls) == 1 and aggregate_cost(calls) == expected
    assert _receipts(records, 50, runtime_root=tmp_path)[0]['model_cost'] == expected
    # A second recognized DB containing the same actual facts must not double count.
    source = sqlite3.connect(tmp_path / '.rebuild-data/ai-turns.sqlite3')
    duplicate = sqlite3.connect(tmp_path / 'ai-turns.sqlite3')
    try:
        source.backup(duplicate)
    finally:
        source.close(); duplicate.close()
    groups = kernel_call_groups(tmp_path, records=records)
    assert len(groups) == 1 and len(groups[0]['calls']) == 1 and aggregate_cost(groups[0]['calls']) == expected


def test_successful_real_wire_uses_frozen_price_after_manual_prices_change_and_restart(tmp_path):
    from backend.memory_app.kernel.receipt_projection import kernel_call_groups, aggregate_cost, question_receipt
    records, _, store, receipt, wires = run_wire(tmp_path, change_prices=True)
    assert receipt.status == 'completed' and len(wires) == 1
    records = SQLiteStructuredRecordStore(records.database_path)
    calls = kernel_call_groups(tmp_path, records=records)[0]['calls']
    assert len(calls) == 1 and calls[0]['cost'] == {'currency': 'CNY', 'amount': '0.00342'}
    assert aggregate_cost(calls) == calls[0]['cost']
    answer = question_receipt(tmp_path, 'turn-cost-real', 'alpha', {}, records=records)
    assert answer['model_cost'] == calls[0]['cost']
    assert len(store.events_after('turn-cost-real')) > 0
    assert 'synthetic private input' not in json.dumps(records.list('v2_model_wire_prices')[0].payload)


@pytest.mark.parametrize('usage', [
    {'total_tokens': 1000}, {'prompt_tokens': 1000},
    {'prompt_tokens': 1000, 'completion_tokens': 600},
    {'prompt_tokens': 1000, 'completion_tokens': 600, 'prompt_cache_hit_tokens': 200, 'prompt_cache_miss_tokens': 900},
])
def test_real_partial_usage_and_missing_cache_split_never_claim_full_cost(tmp_path, usage):
    from backend.memory_app.kernel.receipt_projection import kernel_call_groups, aggregate_cost
    records, _, _, receipt, wires = run_wire(tmp_path, usage=usage)
    assert receipt.status == 'completed' and len(wires) == 1
    calls = kernel_call_groups(tmp_path, records=records)[0]['calls']
    assert calls[0]['cost'] is None and aggregate_cost(calls) is None


@pytest.mark.parametrize('failure', [False, True])
def test_unknown_prices_and_transport_failure_still_record_real_wire_without_zero_cost(tmp_path, failure):
    from backend.memory_app.kernel.receipt_projection import kernel_call_groups, aggregate_cost
    records, _, _, receipt, wires = run_wire(tmp_path, failure=failure, configured_price=False)
    assert receipt.status == ('failed' if failure else 'completed') and len(wires) == 1
    calls = kernel_call_groups(tmp_path, records=records)[0]['calls']
    assert calls[0]['cost'] is None and aggregate_cost(calls) is None


@pytest.mark.parametrize('field', ['turn_id', 'model_request_id', 'provider_id', 'model_id', 'routing_snapshot_revision', 'dispatched_at'])
def test_mismatched_price_snapshot_is_not_evidence_of_a_charge(tmp_path, field):
    from backend.memory_app.kernel.receipt_projection import kernel_call_groups
    records, _, _, receipt, _ = run_wire(tmp_path)
    assert receipt.status == 'completed'
    row = records.list('v2_model_wire_prices')[0]
    with records.begin() as tx:
        tx.put('v2_model_wire_prices', row.object_id, {**row.payload, field: 'wrong'}, expected_revision=row.revision)
        tx.commit()
    assert kernel_call_groups(tmp_path, records=records)[0]['calls'][0]['cost'] is None


def test_absent_history_is_read_only_and_old_receipts_do_not_use_current_prices(tmp_path):
    from backend.memory_app.kernel.receipt_projection import kernel_call_groups
    records, models = configured(tmp_path)
    save_prices(models)
    missing = tmp_path / 'missing'
    assert kernel_call_groups(missing, records=records) == [] and not missing.exists()
    records, _, _, _, _ = run_wire(tmp_path / 'old')
    database = records.database_path
    with records.begin() as tx:
        row = tx.list('v2_model_wire_prices')[0]
        tx.delete('v2_model_wire_prices', row.object_id, expected_revision=row.revision)
        tx.commit()
    before = sqlite3.connect(database).execute('SELECT name,sql FROM sqlite_master ORDER BY name').fetchall()
    assert kernel_call_groups(tmp_path / 'old', records=records)[0]['calls'][0]['cost'] is None
    assert sqlite3.connect(database).execute('SELECT name,sql FROM sqlite_master ORDER BY name').fetchall() == before


def test_embedding_freezes_manual_price_before_wire_without_inventing_cache_counts(tmp_path):
    from backend.memory_app.v2.memory_turn import embedding_request
    from backend.memory_app.kernel.receipt_projection import kernel_call_groups
    records, models = configured(tmp_path)
    models.update('embedding', {'base_url': 'https://proxy.invalid/v1', 'model': 'vector',
        'api_key': 'test-private-value', 'allow_remote': True, 'enabled': True, 'expected_revision': 0})
    models.update_model_prices('embedding', RATES, expected_revision=0, expected_configuration_revision=1)
    wires = []
    response = {'data': [{'index': 0, 'embedding': [1.0, 0.0]}], 'usage': {'prompt_tokens': 5}}
    class Transport:
        def post_json(self, **request):
            wires.append(request)
            row = records.list('v2_model_wire_prices')[0]
            assert row.payload['model_purpose'] == 'embedding' and row.payload['model_id'] == 'vector'
            assert row.payload['rates'] == RATES
            assert all(value not in json.dumps(row.payload) for value in
                       ('test-private-value', 'proxy.invalid', 'Synthetic private embedding input'))
            return response
    request = {'endpoint': 'https://proxy.invalid/v1/embeddings',
               'payload': {'input': ['Synthetic private embedding input'], 'model': 'vector'}}
    for _ in range(2):
        assert embedding_request(records, models, 'alpha', [], 'cost-embedding', lambda: None,
                                 Transport(), request) == response
    assert len(wires) == 1 and len(records.list('v2_model_wire_prices')) == 1
    calls = kernel_call_groups(tmp_path, records=records)[0]['calls']
    assert len(calls) == 1 and calls[0]['cost'] is None
    assert calls[0]['usage'] == {'input_tokens': 5, 'output_tokens': 0, 'total_tokens': 5}
