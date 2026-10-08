import json
import sqlite3
from pathlib import Path

from core.ai_kernel import SQLiteAITurnStore, SynchronousAIRuntime, ScopedCapabilityRegistry
from core.ai_kernel.turn_kinds import freeze_turn_request
from core.storage_provider import SQLiteStructuredRecordStore
from tests.memory_app.v2.test_workbench_ask import env, publish


KEY = 'sk-test-DO-NOT-LEAK'
KINDS = ('memory.organize', 'memory.propose_insights', 'memory.consolidate',
         'memory.link_suggest', 'project.answer', 'project.task')


def seed(root, kind, index):
    database = root / ('ai-turns.sqlite3' if index % 2 else '.rebuild-data/ai-turns.sqlite3')
    store = SQLiteAITurnStore(database)
    identity = f'turn-projection-{index}'
    usage = {'input_tokens': index + 10, 'output_tokens': 2, 'total_tokens': index + 12}

    class Planner:
        def plan(self, request, events, capabilities, payloads, execution_control):
            control = execution_control
            ref = store.get_or_create_immutable_payload(identity, 'memory-model-route-v1',
                {'provider': 'synthetic', 'model': 'safe-model', 'revision': 4,
                 'allow_remote': True, 'base_url': 'https://example.invalid/v1'})
            control.model_call_routed(snapshot_ref=ref, snapshot_revision='a' * 64,
                prompt_cache_scope_identity='b' * 64, provider='synthetic', model='safe-model',
                execution_location='remote', purpose=request['execution_policy']['purpose'])
            control.model_call_started(provider='synthetic', model='safe-model')
            attempt = control.begin_model_wire_attempt()
            def wire():
                attempt.succeeded(usage=usage, cache_observation=None)
            attempt.invoke_wire(wire)
            control.model_call_completed(usage=usage)
            return {'type': 'complete', 'summary': 'Synthetic completed'}

    runtime = SynchronousAIRuntime(planner=Planner(), registry=ScopedCapabilityRegistry(),
        events=store, payloads=store, state=store)
    request = freeze_turn_request(kind, turn_id=identity, session_id='s-' + identity,
        operation_id='op-' + identity, idempotency_key=identity, project_id='alpha',
        created_at='2026-10-03T00:00:00Z', text='synthetic', capabilities=[],
        privacy={'mode': 'remote_allowed', 'allow_remote': True, 'pii': 'possible',
                 'consent_refs': ['crp://default/model-settings/generation'], 'retention': 'session'})
    assert runtime.submit_turn(request).status == 'completed'
    return identity, usage


def test_six_real_turns_project_once_across_both_kernel_databases(tmp_path):
    from backend.memory_app.v2.settings import _receipts
    records = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    expected = [seed(tmp_path, kind, i)[1] for i, kind in enumerate(KINDS)]
    rows = _receipts(records, 50, runtime_root=tmp_path)
    assert len(rows) == 6
    assert sum(row['usage']['input'] for row in rows) == sum(row['input_tokens'] for row in expected)
    assert sum(row['usage']['output'] for row in rows) == 12
    assert all(row['model'] == 'safe-model' for row in rows)
    assert KEY not in json.dumps(rows)
    assert records.list('v2_egress_receipts') == ()
    assert _receipts(records, 50, runtime_root=tmp_path) == rows


def test_projection_does_not_initialize_or_migrate_absent_history(tmp_path):
    from backend.memory_app.kernel.receipt_projection import kernel_call_groups
    root = tmp_path / 'absent'
    assert kernel_call_groups(root) == []
    assert not root.exists()


def test_question_receipt_uses_kernel_facts_after_legacy_metadata_is_changed(env):
    publish(env)
    response = env.http.post('/api/v2/workbench/turns', json={'project_id': 'alpha', 'text': 'alpha?'})
    assert response.status_code == 200, response.text
    data = response.json()
    identity = data['turn']['id']
    with env.records.begin() as tx:
        row = tx.read('v2_turns', identity)
        ask = {**row.payload['receipt']['ask'], 'model_usage': {'total_tokens': 999},
               'context': {'window': 999, 'parts': [], 'egress': {'model': KEY}}}
        tx.put('v2_turns', identity, {**row.payload, 'receipt': {'ask': ask}}, expected_revision=row.revision)
        tx.commit()
    read = env.http.get(f"/api/v2/workbench/threads/{data['thread_id']}?project_id=alpha")
    assert read.status_code == 200, read.text
    actual = read.json()['turns'][0]['receipt']['ask']
    assert actual['model_usage']['total_tokens'] == 7
    assert actual['context']['window'] != 999
    assert KEY not in read.text


def test_legacy_receipts_remain_readable_without_backfill(tmp_path):
    from backend.memory_app.v2.settings import _receipts
    records = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    with records.begin() as tx:
        tx.put('v2_egress_receipts', 'historical', {'model': 'historical-model',
            'at': '2026-01-01T00:00:00Z', 'usage': {'input_tokens': 2, 'output_tokens': 1}}, expected_revision=0)
        tx.commit()
    before = records.list('v2_egress_receipts')
    rows = _receipts(records, 50, runtime_root=tmp_path)
    assert rows[0]['model'] == 'historical-model'
    assert records.list('v2_egress_receipts') == before


def test_v2_has_no_legacy_receipt_writer():
    root = Path(__file__).parents[3] / 'src/backend/memory_app/v2'
    assert not (root / 'egress.py').exists()


def test_projection_keeps_historical_schema_unchanged_and_filters_project(tmp_path):
    from backend.memory_app.kernel.receipt_projection import kernel_call_groups
    seed(tmp_path, 'project.answer', 0)
    database = tmp_path / '.rebuild-data/ai-turns.sqlite3'
    with sqlite3.connect(database) as connection:
        before = connection.execute('SELECT name,sql FROM sqlite_master ORDER BY name').fetchall()
    assert kernel_call_groups(tmp_path, project='foreign') == []
    assert len(kernel_call_groups(tmp_path, project='alpha')) == 1
    with sqlite3.connect(database) as connection:
        assert connection.execute('SELECT name,sql FROM sqlite_master ORDER BY name').fetchall() == before


def test_partial_usage_reports_only_observed_counters_without_filling_unknowns():
    from backend.memory_app.kernel.receipt_projection import aggregate_usage
    assert aggregate_usage([{'usage': {'total_tokens': 7}}]) == {'total_tokens': 7}
    assert aggregate_usage([{'usage': {'input_tokens': 4}}, {'usage': None}]) == {
        'input_tokens': 4, 'observed_only': True}
    assert aggregate_usage([{'usage': None}]) is None
