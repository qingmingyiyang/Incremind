import os
import time
from pathlib import Path
from shutil import copyfile

from fastapi.testclient import TestClient
from fastapi import FastAPI


def build_app(root):
    os.environ['CHRIPTMAS_APP_ROOT'] = str(root)
    (root / 'config').mkdir(exist_ok=True)
    copyfile(Path(__file__).resolve().parents[3] / 'config/settings.toml.example', root / 'config/settings.toml')
    from backend.memory_app.model_config import ModelConfiguration
    from backend.memory_app.source_egress import recognition_service
    from backend.memory_app.workspace import install_workspace_routes
    from backend.memory_app.v2.workbench import install_workbench_routes
    from backend.memory_app.v2.library import install_library_routes
    from backend.memory_app.v2.privacy import is_private_project, set_private_project
    from core.storage_provider import SQLiteStructuredRecordStore
    from core.document_engine import SQLiteDocumentRepository
    app = FastAPI()
    records = SQLiteStructuredRecordStore(root / 'records.sqlite3')
    documents = SQLiteDocumentRepository(records)
    models = ModelConfiguration(records, root)
    service = recognition_service(records)
    workspace = install_workspace_routes(app, runtime_root=root, records=records, models=models,
        documents=documents, service=service)
    install_workbench_routes(app, records=records, models=models, documents=documents,
        service=service, workspace=workspace)
    install_library_routes(app, records=records, service=service, documents=documents, workspace=workspace, models=models)
    app.state.recognition_records = records
    app.state.workspace_domains = workspace
    if not is_private_project(records, 'alpha'):
        set_private_project(records, 'alpha', True, 0)
    if records.read('workspace_items', 'unfinished-original') is None:
        seed_original(records)
    return app


def seed_original(records, *, identity='unfinished-original', turn_id='unfinished-turn', state='failed', instance='old-instance'):
    now = '2026-10-25T00:00:00+00:00'
    item = {'id': identity, 'project_id': 'alpha', 'input_kind': 'text', 'title': '未完成原件',
        'source_text': '原文内容保持完整', 'status': 'failed', 'document_id': None,
        'created_at': now, 'updated_at': now, 'draft': None}
    memory = {'item_id': identity, 'title': item['title'], 'state': state,
        'document_id': None, 'progress': {'done': 2, 'total': 4}, 'verified': False,
        'insights': [], 'related': [], 'error': 'processing_failed' if state == 'failed' else None}
    with records.begin() as tx:
        tx.put('workspace_items', identity, item, expected_revision=0)
        tx.put('v2_threads', turn_id, {'project_id': 'alpha', 'title': item['title'],
            'created_at': now, 'updated_at': now}, expected_revision=0)
        tx.put('v2_turns', turn_id, {'project_id': 'alpha', 'thread_id': turn_id,
            'intent': 'remember', 'user_text': item['source_text'], 'item_id': identity,
            'instance': instance, 'run_id': 'seed-run', 'created_at': now, 'updated_at': now,
            'receipt': {'remember': memory}}, expected_revision=0)
        tx.put('v2_workbench_item_states', turn_id, {'project_id': 'alpha', 'item_id': identity,
            'thread_id': turn_id, 'turn_id': turn_id, 'state': state, 'error': memory['error'],
            'created_at': now, 'updated_at': now}, expected_revision=0)
        tx.commit()


def test_unfinished_original_read_is_scoped_and_read_only(tmp_path):
    app = build_app(tmp_path)
    records = app.state.recognition_records
    from backend.memory_app.v2.projects import assign_scene
    assign_scene(records, 'item', 'unfinished-original', 'alpha', '阅读')
    before = records.list('v2_workbench_item_states')
    with TestClient(app) as client:
        response = client.get('/api/v2/library/sources?project_id=alpha')
        assert response.status_code == 200, response.text
        row, = response.json()['items']
        assert (row['id'], row['state'], row['turn_id']) == ('unfinished-original', 'failed', 'unfinished-turn')
        assert client.get('/api/v2/library/sources?project_id=beta').json()['items'] == []
        assert len(client.get('/api/v2/library/sources?project_id=alpha&scene=阅读&q=完整').json()['items']) == 1
        assert client.get('/api/v2/library/sources?project_id=alpha&scene=其他').json()['items'] == []
        assert client.get('/api/v2/library/sources?project_id=alpha&q=不存在的文字').json()['items'] == []
        assert client.get('/api/v2/library/sources/unfinished-original/text?project_id=alpha').json()['text'] == '原文内容保持完整'
        assert client.get('/api/v2/library/sources/unfinished-original/text?project_id=beta').status_code == 404
        assert client.get('/api/v2/library/drill?from=source&id=unfinished-original&project_id=alpha').status_code == 200
        assert app.state.workspace_domains.query.query_entries('alpha') == []
        from backend.memory_app.v2.ladder import plan_ladder
        collected = app.state.workspace_domains.query.collect_candidates('alpha', '原文内容')
        assert plan_ladder(collected['candidates'], '原文内容')['chosen'] == []
        assert client.get('/api/v2/library/summaries?project_id=alpha').json()['items'] == []
        from backend.memory_app.v2.library import LibraryRead
        reader = LibraryRead(records, app.state.workspace_domains.query.service,
            app.state.workspace_domains.review.documents, app.state.workspace_domains,
            remember_observer=app.state.workbench_remember_observer)
        assert reader.list('sources', 'alpha', None, '', state='pending')['items'] == []
        for state in ('pending', 'active', 'forgotten'):
            assert client.get('/api/v2/library/insights', params={'project_id': 'alpha', 'state': state}).json()['items'] == []
    assert records.list('v2_workbench_item_states') == before


def test_external_recall_cannot_discover_unfinished_original(tmp_path):
    app = build_app(tmp_path)
    from backend.memory_app.v2.external_recall import ExternalRecall
    from backend.memory_app.v2.external_agent_settings import external_agent_settings, replace_external_agent_settings
    from backend.memory_app.v2.privacy import set_private_project
    records = app.state.recognition_records
    set_private_project(records, 'alpha', False, 1)
    settings = external_agent_settings(records)
    replace_external_agent_settings(records, {key: value for key, value in settings.items() if key != 'revision'}
        | {'allow_remote': True, 'include_profile': False}, expected_revision=settings['revision'])
    recall = ExternalRecall(app.state.workspace_domains.query, owner_id='local-user')
    plan = recall.plan({'client': 'codex', 'tool': 'recall', 'query': '原文内容',
        'scope': {'user_id': 'local-user', 'project_id': 'alpha'}, 'budget': 3000})
    assert plan['candidates'] == [] and plan['selections'] == [] and plan['materials'] == []


def test_latest_legal_turn_wins_over_older_and_forged_states(tmp_path):
    app = build_app(tmp_path)
    records = app.state.recognition_records
    turn = records.read('v2_turns', 'unfinished-turn')
    state = records.read('v2_workbench_item_states', turn.object_id)
    with records.begin() as tx:
        for identity, created, thread in [('newer-turn', '2026-10-25T01:00:00+00:00', 'unfinished-turn'),
                ('forged-turn', '2026-10-25T02:00:00+00:00', 'foreign-thread')]:
            tx.put('v2_turns', identity, {**turn.payload, 'thread_id': thread, 'created_at': created}, expected_revision=0)
            tx.put('v2_workbench_item_states', identity, {**state.payload, 'turn_id': identity,
                'thread_id': thread, 'created_at': created}, expected_revision=0)
        tx.put('v2_threads', 'foreign-thread', {'project_id': 'beta'}, expected_revision=0)
        tx.commit()
    with TestClient(app) as client:
        row, = client.get('/api/v2/library/sources?project_id=alpha').json()['items']
        assert row['turn_id'] == 'newer-turn'


def test_retry_confirms_same_original_without_model_sends(tmp_path):
    app = build_app(tmp_path)
    records = app.state.recognition_records
    prepare_ready(records)
    item = records.read('workspace_items', 'unfinished-original')
    with TestClient(app) as client:
        response = client.post('/api/v2/workbench/turns/unfinished-turn/retry', json={'project_id': 'alpha'})
        assert response.status_code == 200, response.text
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            memory = client.get('/api/v2/workbench/threads/unfinished-turn?project_id=alpha').json()['turns'][0]['receipt']['remember']
            if memory['state'] != 'processing':
                break
            time.sleep(.05)
        assert memory['state'] == 'done', memory
        rows = client.get('/api/v2/library/sources?project_id=alpha').json()['items']
        row = next(row for row in rows if row['id'] == item.object_id)
        assert row.get('state') is None and row.get('turn_id') is None
        assert records.read('workspace_items', item.object_id).payload['status'] == 'confirmed'
        with records.begin() as tx:
            turn = tx.read('v2_turns', 'unfinished-turn')
            memory = {**turn.payload['receipt']['remember'], 'state': 'failed', 'error': 'insight_generation_failed'}
            tx.put('v2_turns', turn.object_id, {**turn.payload, 'receipt': {'remember': memory}}, expected_revision=turn.revision)
            state = tx.read('v2_workbench_item_states', turn.object_id)
            tx.put('v2_workbench_item_states', turn.object_id, {**state.payload, 'state': 'failed',
                'error': 'insight_generation_failed'}, expected_revision=state.revision)
            tx.commit()
        row = next(row for row in client.get('/api/v2/library/sources?project_id=alpha').json()['items'] if row['id'] == item.object_id)
        assert row.get('state') is None


def prepare_ready(records):
    item = records.read('workspace_items', 'unfinished-original')
    with records.begin() as tx:
        tx.put('workspace_items', item.object_id, {**item.payload, 'status': 'ready',
            'draft': {'title': '未完成原件', 'summary': '原文摘要', 'facts': [], 'todos': [],
                'topics': [], 'uncertainties': [], 'people': [], 'dates': [], 'suggestions': []}}, expected_revision=item.revision)
        tx.commit()


def test_restart_projects_processing_as_retryable_failure(tmp_path):
    app = build_app(tmp_path)
    records = app.state.recognition_records
    seed_original(records, identity='restart-original', turn_id='restart-turn', state='processing')
    item = records.read('workspace_items', 'restart-original')
    with records.begin() as tx:
        tx.put('workspace_items', item.object_id, {**item.payload, 'status': 'staged'}, expected_revision=item.revision)
        tx.commit()
    restarted = build_app(tmp_path)
    with TestClient(restarted) as client:
        row = next(row for row in client.get('/api/v2/library/sources?project_id=alpha').json()['items'] if row['id'] == item.object_id)
        assert (row['state'], row['error'], row['turn_id']) == ('failed', 'interrupted', 'restart-turn')
