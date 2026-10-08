import json
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tests.memory_app.v2.test_insight_generation import env, response_for
from backend.memory_app.model_config import ModelConfiguration
from backend.memory_app.v2.insight_generation import generate_insights
from backend.memory_app.v2.settings import install_settings_routes
from backend.security.secrets import InMemorySecretStore


KEY = 'sk-test-DO-NOT-LEAK'


def configured(env, *, local=False, failure=False, before=None):
    calls = []
    def wire(**request):
        calls.append(1)
        if before:
            before()
        if failure:
            raise RuntimeError(KEY + ' provider body')
        content = response_for(request['messages'], json.dumps({'insights': [{'text': 'Safe insight', 'conditions': []}]}))
        return {'choices': [{'message': {'content': content}}],
                'usage': {'prompt_tokens': 11, 'completion_tokens': 3}}
    models = ModelConfiguration(env.records, env.records.database_path.parent,
        InMemorySecretStore(), completion_fn=wire)
    models.update('generation', {'base_url': 'http://localhost:1234/v1' if local else 'https://example.invalid/v1',
        'model': 'safe-model', 'api_key': KEY, 'allow_remote': True, 'expected_revision': 0})
    return models, calls


def receipts(env, models):
    app = FastAPI()
    install_settings_routes(app, runtime_root=env.records.database_path.parent, records=env.records, models=models)
    with TestClient(app) as client:
        response = client.get('/api/v2/settings/egress-receipts')
        assert response.status_code == 200, response.text
        assert KEY not in response.text
        return response.json()


@pytest.mark.parametrize('failure', [False, True])
def test_real_gateway_writes_remote_success_and_failure_receipts_without_secrets(env, failure):
    models, calls = configured(env, failure=failure)
    result = generate_insights(models, env.service, env.documents, 'alpha', env.doc)
    assert bool(result) is (not failure) and calls == [1]
    from tests.memory_app.v2.kernel_receipts import wire_receipts, requests
    rows = wire_receipts(env.records)
    assert len(rows) == 1
    payload = rows[0]
    request = requests(env.records)[0]
    assert request['desired_outcome'] == 'memory.propose_insights'
    assert payload['model_id'] == 'safe-model'
    assert len(request['privacy']['material_refs']) == 1
    assert request['privacy']['allow_remote'] is True
    from backend.memory_app.v2.memory_turn import MemoryTurn
    route = MemoryTurn.store_for(env.records).get_immutable_payload(request['turn_id'], 'memory-model-route-v1')[1]
    assert route['revision'] == 1
    assert request['privacy']['consent_refs'] == ['crp://default/model-settings/generation']
    assert payload['status'] == ('failed_transport' if failure else 'succeeded')
    assert payload['usage'] == (None if failure else {'input_tokens': 11, 'output_tokens': 3, 'total_tokens': 14})
    assert KEY not in str(payload) and '原文证据' not in str(payload)
    assert env.records.list('v2_egress_receipts') == ()



def test_local_model_does_not_write_egress_receipts(env):
    models, calls = configured(env, local=True)
    assert generate_insights(models, env.service, env.documents, 'alpha', env.doc)
    assert calls == [1] and env.records.list('v2_egress_receipts') == ()
    assert [row for row in receipts(env, models) if row['purpose'] == '认识'] == []


def test_preflight_rejection_creates_neither_wire_call_nor_receipt(env):
    models, calls = configured(env)
    models.update('generation', {'allow_remote': False, 'expected_revision': 1})
    assert generate_insights(models, env.service, env.documents, 'alpha', env.doc) == []
    assert calls == [] and env.records.list('v2_egress_receipts') == ()


def test_post_response_source_revocation_keeps_the_actual_egress_receipt(env):
    from backend.memory_app.v2.privacy import set_private_project
    models, calls = configured(env, before=lambda: set_private_project(env.records, 'alpha', True, 0))
    assert generate_insights(models, env.service, env.documents, 'alpha', env.doc) == []
    from tests.memory_app.v2.kernel_receipts import wire_receipts
    assert calls == [1] and len(wire_receipts(env.records)) == 1


def test_legacy_fake_is_called_once_without_fabricating_wire_receipts(env):
    assert generate_insights(env.model, env.service, env.documents, 'alpha', env.doc)
    assert env.model.calls == 1 and env.records.list('v2_egress_receipts') == ()


def wire_receipt(turn, request, location='remote'):
    return {'schema_version': '1.0.0', 'attempt_id': 'model-wire-attempt-' + request,
        'turn_id': turn, 'model_request_id': request, 'attempt_number': 1,
        'routing_snapshot_revision': 'a' * 64, 'provider_id': 'remote-provider', 'model_id': 'safe-model',
        'status': 'succeeded', 'started_at': '2026-10-01T00:00:00Z', 'completed_at': '2026-10-01T00:00:01Z',
        'duration_ms': 1000, 'usage_status': 'unavailable', 'usage': None,
        'cache_status': 'unavailable', 'cache_metadata': None, 'input_stored': False,
        'output_stored': False, 'error_code': None, 'execution_location': location}


def test_task_and_research_calls_merge_once_and_unknown_usage_stays_unknown(env):
    from core.ai_kernel import SQLiteAITurnStore
    from backend.memory_app.v2.settings import _receipts
    root = env.records.database_path.parent
    kernel = SQLiteAITurnStore(root / '.rebuild-data' / 'ai-turns.sqlite3')
    for turn, amount in [('task-turn', 10), ('research-turn', 20), ('child-turn', 5)]:
        kernel.claim_turn({'turn_id': turn, 'session_id': 'session-' + turn,
            'operation_id': 'operation-' + turn, 'idempotency_key': turn,
            'scope': {'kind': 'project', 'project_id': 'alpha'}})
        kernel.put(turn, 'model-call-receipt', {'schema_version': '1.0.0', 'receipt_id': 'model-receipt-' + turn,
            'turn_id': turn, 'model_request_id': 'request-' + turn, 'status': 'completed',
            'requested_at': '2026-10-01T00:00:00Z', 'completed_at': '2026-10-01T00:00:01Z',
            'duration_ms': 1000, 'provider_id': 'remote-provider', 'model_id': 'safe-model',
            'usage_status': 'recorded', 'usage': {'input_tokens': amount, 'output_tokens': 2, 'total_tokens': amount + 2},
            'input_recorded': False, 'output_recorded': False, 'error_code': None})
        kernel.put(turn, 'model-wire-attempt-receipt', wire_receipt(turn, 'request-' + turn))
    from dataclasses import replace
    from core.ai_kernel.agent_store import SQLiteAgentStore
    from tests.rebuild.test_ai_agent_store import _main, _spawn
    agents = SQLiteAgentStore(kernel.database_path) if hasattr(kernel, 'database_path') else SQLiteAgentStore(root / '.rebuild-data' / 'ai-turns.sqlite3')
    main = replace(_main('research-turn'), project_id='alpha')
    agents.register_run(main, operation_id='register-research')
    child, link, reservation = _spawn()
    child = replace(child, turn_id='child-turn', project_id='alpha')
    link = replace(link, parent_project_id='alpha', child_project_id='alpha')
    reservation = replace(reservation, project_id='alpha')
    agents.reserve_spawn(parent=main, child=child, link=link, reservation=reservation)
    with env.records.begin() as tx:
        tx.put('recognition_tasks', 'task-one', {'project_id': 'alpha', 'turn_id': 'task-turn'}, expected_revision=0)
        tx.put('v2_turns', 'workbench-turn', {'project_id': 'alpha', 'intent': 'do',
            'receipt': {'do': {'task_id': 'task-one', 'research_turn_id': 'research-turn'}}}, expected_revision=0)
        tx.commit()
    rows = [row for row in _receipts(env.records, 50, runtime_root=root) if row['purpose'] == '干活']
    assert len(rows) == 1 and rows[0]['usage'] == {'input': 35, 'output': 6}
    assert rows[0]['model'] == 'safe-model' and KEY not in str(rows)
    from backend.memory_app.v2.do_context import task_context
    from backend.recognition import WorkScope
    from backend.memory_app.context_adapter import compile_selected
    packet=compile_selected('alpha', [], [], 'write', 1)
    with env.records.begin() as tx:
        tx.put('recognition_context_packets','packet-f7',packet,expected_revision=0)
        tx.commit()
    kernel.get_or_create_immutable_payload('task-turn','recognition-model-routing-snapshot-v1', {
        'turn_id':'task-turn','project_id':'alpha','execution_location':'remote',
        'configuration':{'allow_remote':True,'model':'safe-model','revision':4}})
    kernel.put('task-turn','recognition-model-routing-snapshot-v1', {
        'turn_id':'task-turn','project_id':'alpha','execution_location':'remote',
        'configuration':{'allow_remote':True,'model':'safe-model','revision':99}})
    view=task_context(env.records,WorkScope('local-user','alpha'),
        {'task_id':'task-one','turn_id':'task-turn','context_packet_id':'packet-f7'},
        {'research_turn_id':'research-turn'},env.model,None)
    assert view['model_usage']=={'input_tokens':35,'output_tokens':6}
    assert view['context']['egress']=={'model':'safe-model','consent_scope':'global_setting',
        'settings_revision':{'generation':4}}
    assert view['egress']==view['context']['egress']

    import sqlite3
    with sqlite3.connect(root / '.rebuild-data' / 'ai-turns.sqlite3') as connection:
        connection.execute("DELETE FROM ai_turn_immutable_payloads WHERE turn_id=? AND kind=?",
            ('task-turn', 'recognition-model-routing-snapshot-v1'))
    kernel.put('task-turn','recognition-model-routing-snapshot-v1', {
        'turn_id':'task-turn','project_id':'alpha','execution_location':'remote',
        'configuration':{'allow_remote':True,'model':'safe-model','revision':999}})
    without_frozen=task_context(env.records,WorkScope('local-user','alpha'),
        {'task_id':'task-one','turn_id':'task-turn','context_packet_id':'packet-f7'},
        {'research_turn_id':'research-turn'},env.model,None)
    assert without_frozen['egress']=={'model':'safe-model','consent_scope':None,'settings_revision':None}
    with sqlite3.connect(root / '.rebuild-data' / 'ai-turns.sqlite3') as connection:
        connection.execute('DROP TABLE ai_turn_immutable_payloads')
    historical=task_context(env.records,WorkScope('local-user','alpha'),
        {'task_id':'task-one','turn_id':'task-turn','context_packet_id':'packet-f7'},
        {'research_turn_id':'research-turn'},env.model,None)
    assert historical['egress']==without_frozen['egress']

    unknown = {'schema_version': '1.0.0', 'receipt_id': 'model-receipt-unknown',
        'turn_id': 'research-turn', 'model_request_id': 'request-unknown', 'status': 'completed',
        'requested_at': '2026-10-01T00:00:00Z', 'completed_at': '2026-10-01T00:00:01Z',
        'duration_ms': 1000, 'provider_id': 'remote-provider', 'model_id': 'safe-model',
        'usage_status': 'not_recorded', 'usage': None, 'input_recorded': False, 'output_recorded': False, 'error_code': None}
    kernel.put('research-turn', 'model-call-receipt', unknown)
    kernel.put('research-turn', 'model-wire-attempt-receipt', wire_receipt('research-turn', 'request-unknown'))
    rows = [row for row in _receipts(env.records, 50, runtime_root=root) if row['purpose'] == '干活']
    assert len(rows) == 1 and rows[0]['usage'] is None

    assert [row for row in receipts(env, env.model) if row['purpose'] == '干活'] == rows


@pytest.mark.parametrize('damage', ['local', 'foreign', 'malformed', 'unknown-location'])
def test_task_requires_valid_remote_wire_evidence(env, damage):
    from core.ai_kernel import SQLiteAITurnStore
    from backend.memory_app.v2.settings import _receipts
    root = env.records.database_path.parent
    kernel = SQLiteAITurnStore(root / '.rebuild-data' / 'ai-turns.sqlite3')
    kernel.claim_turn({'turn_id': 'task-turn', 'session_id': 's', 'operation_id': 'o',
        'idempotency_key': 'k', 'scope': {'kind': 'project', 'project_id': 'alpha'}})
    kernel.put('task-turn', 'model-call-receipt', {'schema_version': '1.0.0', 'receipt_id': 'model-receipt-one',
        'turn_id': 'task-turn', 'model_request_id': 'request-one', 'status': 'completed',
        'requested_at': '2026-10-01T00:00:00Z', 'completed_at': '2026-10-01T00:00:01Z',
        'duration_ms': 1000, 'provider_id': 'remote-provider', 'model_id': 'safe-model',
        'usage_status': 'not_recorded', 'usage': None, 'input_recorded': False, 'output_recorded': False, 'error_code': None})
    wire = wire_receipt('task-turn', 'request-one')
    if damage == 'local': wire['execution_location'] = 'local_loopback'
    if damage == 'foreign': wire['turn_id'] = 'foreign-turn'
    if damage == 'malformed': wire['model_request_id'] = ['invalid']
    if damage == 'unknown-location': wire.pop('execution_location')
    kernel.put('task-turn', 'model-wire-attempt-receipt', wire)
    with env.records.begin() as tx:
        tx.put('recognition_tasks', 'task-one', {'project_id': 'alpha', 'turn_id': 'task-turn'}, expected_revision=0)
        tx.commit()
    assert [row for row in _receipts(env.records, 50, runtime_root=root) if row['purpose'] == '干活'] == []


def test_reading_absent_task_history_never_creates_database(env):
    from backend.memory_app.v2.task_egress import task_call_groups
    root = env.records.database_path.parent / 'empty-runtime'
    assert task_call_groups(env.records, root) == []
    assert not root.exists()
