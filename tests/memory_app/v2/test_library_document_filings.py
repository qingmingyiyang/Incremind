"""Product routes expose explicit move/undo and bound readonly original drill."""
from tests.memory_app.v2.test_workbench_placement import workbench, finish
from tests.memory_app.v2.test_auto_confirm import runtime
from tests.memory_app.v2.test_insight_generation import env
from backend.memory_app.app import _router
import pytest


def source(app, http):
    response = http.post('/api/v2/workbench/turns', json={
        'project_id': 'alpha', 'text': '原文证据', 'intent': 'remember'})
    assert response.status_code == 200, response.text
    return finish(app, http, response.json())['receipt']['remember']['document_id']


@pytest.fixture
def published_source_workbench(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from fastapi.testclient import TestClient
    from backend.recognition import RecognitionService
    from core.document_engine import SQLiteDocumentRepository
    from core.storage_provider import SQLiteStructuredRecordStore
    from tests.memory_app.v2.test_workbench_remember import assemble, Model
    from tests.memory_app.v2.test_placement import projects
    monkeypatch.setenv('CHRIPTMAS_APP_ROOT', str(tmp_path))
    records = SQLiteStructuredRecordStore(tmp_path / '.rebuild-data' / 'structured-records.sqlite3')
    documents = SQLiteDocumentRepository(records, namespace_id='default')
    model = Model()
    model.insights = []
    runtime = SimpleNamespace(records=records, documents=documents, model=model)
    service = projects(runtime)
    app, domains = assemble(tmp_path, records, documents, service, model)
    runtime.domains = domains
    with TestClient(app) as http:
        yield runtime, app, http


def test_http_move_list_readonly_bound_original_and_undo(workbench):
    runtime, app, http = workbench
    document = source(app, http)
    original = runtime.documents.read(document)
    response = http.post(f'/api/v2/library/notes/{document}/file', json={
        'project_id': 'alpha', 'target_project_id': 'beta', 'scene': '简历', 'expected_revision': 1})
    assert response.status_code == 200, response.text
    filed = response.json()
    target = filed['target_document_id']
    listed = http.get('/api/v2/library/notes?project_id=alpha').json()['items']
    moved = next(row for row in listed if row['document_id'] == document)
    assert moved['filing']['target_project_id'] == 'beta'
    assert moved['filing']['target_document_id'] == target
    response = http.get('/api/v2/library/drill', params={
        'project_id': 'beta', 'from': 'note', 'id': target})
    assert response.status_code == 200, response.text
    drill = response.json()
    assert drill['note']['document_id'] == target and drill['source']['title']
    assert drill['source_project_id'] == 'alpha' and drill['source_readonly'] is True
    assert drill['source']['id'] in {row.object_id for row in runtime.records.list('workspace_items')}
    assert http.get('/api/v2/library/drill', params={
        'project_id': 'beta', 'from': 'note', 'id': target, 'source_id': 'unrelated'}).status_code == 404
    response = http.post(f'/api/v2/library/notes/{target}/unfile', json={
        'project_id': 'beta', 'expected_revision': filed['filing_revision']})
    assert response.status_code == 200, response.text
    assert runtime.records.read('v2_document_recall', document) is None
    assert runtime.documents.read(document) == original
    turn = http.get('/api/v2/workbench/threads', params={'project_id': 'alpha'})
    assert turn.status_code == 200


def test_http_scene_correction_reuses_real_owner_and_rejects_unknown_fields(workbench):
    runtime, app, http = workbench
    project = runtime.records.read('v2_projects', 'alpha')
    updated = http.patch('/api/v2/projects/alpha', json={
        'scenes': [*project.payload['scenes'], '复盘'], 'expected_revision': project.revision})
    assert updated.status_code == 200, updated.text
    document = source(app, http)
    assignment = runtime.records.read('v2_scene_assignments_document', document)
    assert assignment.revision == 1 and assignment.payload == {'project_id': 'alpha', 'scene': '阅读'}
    facts = {collection: runtime.records.list(collection) for collection in ('documents', 'workspace_items')}
    before = runtime.records.list_all()
    stale = http.patch(f'/api/v2/library/notes/{document}/scene', json={
        'project_id': 'alpha', 'scene': '复盘', 'expected_revision': 1, 'assignment_revision': 0})
    assert stale.status_code == 409, stale.text
    assert runtime.records.list_all() == before
    response = http.patch(f'/api/v2/library/notes/{document}/scene', json={
        'project_id': 'alpha', 'scene': '复盘', 'expected_revision': 1,
        'assignment_revision': assignment.revision})
    assert response.status_code == 200, response.text
    assert len(runtime.records.list('v2_place_examples')) == 1
    assert len(runtime.records.list('v2_place_corrections')) == 1
    corrected = runtime.records.read('v2_scene_assignments_document', document)
    assert corrected.revision == 2 and corrected.payload == {'project_id': 'alpha', 'scene': '复盘'}
    assert {collection: runtime.records.list(collection) for collection in facts} == facts
    assert http.patch(f'/api/v2/library/notes/{document}/scene', json={
        'project_id': 'alpha', 'scene': '阅读', 'expected_revision': 1,
        'assignment_revision': 1, 'unknown': True}).status_code == 400


def test_legitimate_http_copy_edit_retains_library_drill_recall_and_undo(workbench):
    from threading import RLock
    from backend.memory_app.turn_dispatch import RecognitionTurnDispatcher
    from backend.memory_app.document_recognition import ensure_document_experience
    from backend.recognition import WorkScope, RecognitionService
    runtime, app, http = workbench
    service = RecognitionService(runtime.records)
    lock = RLock()
    dispatcher = RecognitionTurnDispatcher(application=app,
        runtime_root=runtime.records.database_path.parent, records=runtime.records, mutation_lock=lock)
    app.include_router(_router(service, runtime.model, runtime.documents,
        mutation_lock=lock, dispatcher=dispatcher))
    document = source(app, http)
    filed = http.post(f'/api/v2/library/notes/{document}/file', json={
        'project_id': 'alpha', 'target_project_id': 'beta', 'expected_revision': 1}).json()
    target = filed['target_document_id']
    response = http.patch(f'/api/recognition/documents/{target}', json={
        'project_id': 'beta', 'expected_revision': 1, 'markdown': '用户补充的摘要与证据'})
    assert response.status_code == 200, response.text
    assert response.json()['revision'] == 2
    listed = http.get('/api/v2/library/notes?project_id=beta').json()['items']
    assert target in {row['document_id'] for row in listed}
    drill = http.get('/api/v2/library/drill', params={'project_id': 'beta', 'from': 'note', 'id': target})
    assert drill.status_code == 200, drill.text
    assert drill.json()['note']['markdown'] == '用户补充的摘要与证据'
    assert target in {row['entry']['id'] for row in runtime.domains.query.prepare_ask('beta', '摘要')['chosen']
        if row['kind'] == 'document'}
    experience, revision = ensure_document_experience(runtime.documents, service, 'beta', target)
    assert revision == 2
    assert service.read_candidate_experiences(scope=WorkScope('local-user', 'beta'),
        experience_ids=[experience])[0].content == '用户补充的摘要与证据'
    response = http.post(f'/api/v2/library/notes/{target}/unfile', json={
        'project_id': 'beta', 'expected_revision': filed['filing_revision']})
    assert response.status_code == 200, response.text
    assert runtime.documents.markdown(target) == '用户补充的摘要与证据'


@pytest.mark.parametrize('edited', [False, True])
def test_http_second_filing_drills_to_bound_original_across_real_owner_chain(workbench, edited):
    runtime, app, http = workbench
    document = source(app, http)
    original = runtime.documents.read(document)
    originals = runtime.records.list('workspace_items')
    initial = http.get('/api/v2/library/drill', params={
        'project_id': 'alpha', 'from': 'note', 'id': document})
    assert initial.status_code == 200, initial.text
    bound = initial.json()
    original_text = http.get(f"/api/v2/library/sources/{bound['source']['id']}/text", params={
        'project_id': 'alpha'})
    assert original_text.status_code == 200, original_text.text
    first = http.post(f'/api/v2/library/notes/{document}/file', json={
        'project_id': 'alpha', 'target_project_id': 'beta', 'expected_revision': 1})
    assert first.status_code == 200, first.text
    intermediate = first.json()['target_document_id']
    revision = 1
    if edited:
        from threading import RLock
        from backend.memory_app.turn_dispatch import RecognitionTurnDispatcher
        from backend.recognition import RecognitionService
        lock = RLock()
        dispatcher = RecognitionTurnDispatcher(application=app,
            runtime_root=runtime.records.database_path.parent, records=runtime.records, mutation_lock=lock)
        app.include_router(_router(RecognitionService(runtime.records), runtime.model, runtime.documents,
            mutation_lock=lock, dispatcher=dispatcher))
        updated = http.patch(f'/api/recognition/documents/{intermediate}', json={
            'project_id': 'beta', 'expected_revision': 1, 'markdown': '用户编辑的第二项目整理稿'})
        assert updated.status_code == 200, updated.text
        revision = updated.json()['revision']
        assert revision == 2
    created = http.post('/api/v2/projects', json={'name': '第三项目'})
    assert created.status_code == 200, created.text
    project = created.json()['id']
    second = http.post(f'/api/v2/library/notes/{intermediate}/file', json={
        'project_id': 'beta', 'target_project_id': project, 'expected_revision': revision})
    assert second.status_code == 200, second.text
    target = second.json()['target_document_id']
    response = http.get('/api/v2/library/drill', params={
        'project_id': project, 'from': 'note', 'id': target})
    assert response.status_code == 200, response.text
    drill = response.json()
    assert drill['note']['document_id'] == target
    assert drill['note']['markdown'] == runtime.documents.markdown(intermediate)
    assert drill['source_project_id'] == 'alpha' and drill['source_readonly'] is True
    assert drill['sources'] == bound['sources']
    assert drill['source'] == {**bound['source'], 'window': None}
    current_text = http.get(f"/api/v2/library/sources/{drill['source']['id']}/text", params={
        'project_id': drill['source_project_id']})
    assert current_text.status_code == 200, current_text.text
    assert current_text.json() == original_text.json()
    assert drill['source']['id'] in {row.object_id for row in originals}
    assert runtime.documents.read(target)['source_refs'] == original['source_refs']
    assert runtime.documents.read(document) == original
    assert runtime.records.list('workspace_items') == originals


@pytest.mark.parametrize('fixture_name', ['workbench', 'published_source_workbench'])
def test_http_filed_drill_excludes_unrelated_real_standalone_source_and_preserves_aliases(request, fixture_name):
    from tests.memory_app.v2.test_library_read import legacy_source
    runtime, app, http = request.getfixturevalue(fixture_name)
    document = source(app, http)
    unrelated = 'unrelated-owner-original'
    legacy_source(runtime, identity=unrelated, content_snapshot='不属于这份整理稿的真实独立原件')
    assert http.get(f'/api/v2/library/sources/{unrelated}/text', params={
        'project_id': 'alpha'}).status_code == 200
    original = http.get('/api/v2/library/drill', params={
        'project_id': 'alpha', 'from': 'note', 'id': document})
    assert original.status_code == 200, original.text
    bound = original.json()['sources']
    assert bound and unrelated not in {row['id'] for row in bound}
    confirmed = next(row for row in runtime.records.list('workspace_items')
        if row.payload.get('document_id') == document)
    alias = confirmed.payload['source_id']
    original_alias = runtime.domains.confirmations.source_store.read('sources', alias)
    assert original_alias['identity_method'] == 'workspace_confirmation'
    qualified_alias = runtime.domains.query.source_store.read('sources', alias)
    assert (qualified_alias is None if fixture_name == 'workbench' else qualified_alias == original_alias)
    assert {row['id'] for row in bound} == {confirmed.object_id}
    assert http.get('/api/v2/library/drill', params={
        'project_id': 'alpha', 'from': 'note', 'id': document, 'source_id': alias}).status_code == 404
    assert http.get(f'/api/v2/library/sources/{alias}/text', params={
        'project_id': 'alpha'}).status_code == 404
    filed = http.post(f'/api/v2/library/notes/{document}/file', json={
        'project_id': 'alpha', 'target_project_id': 'beta', 'expected_revision': 1})
    assert filed.status_code == 200, filed.text
    target = filed.json()['target_document_id']
    before, calls = runtime.records.list_all(), runtime.model.calls
    for identity in [unrelated, *(row['id'] for row in bound)]:
        response = http.get('/api/v2/library/drill', params={
            'project_id': 'beta', 'from': 'note', 'id': target, 'source_id': identity})
        if identity == unrelated:
            assert response.status_code == 404, response.text
        else:
            assert response.status_code == 200, response.text
            assert response.json()['source']['id'] == identity
            assert response.json()['sources'] == bound
            assert response.json()['source_project_id'] == 'alpha'
    assert http.get('/api/v2/library/drill', params={
        'project_id': 'beta', 'from': 'note', 'id': target, 'source_id': alias}).status_code == 404
    assert runtime.records.list_all() == before and runtime.model.calls == calls


def test_http_foreign_insight_from_edited_filing_keeps_recursive_original_scope(env):
    import json
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.memory_app.workspace import install_workspace_routes
    from backend.memory_app.v2.library import install_library_routes
    from backend.memory_app.v2.projects import install_project_routes
    from backend.memory_app.v2.insight_generation import generate_insights
    from tests.memory_app.v2.test_document_filings import setup_projects

    setup_projects(env)
    app = FastAPI()
    domains = install_workspace_routes(app, runtime_root=env.records.database_path.parent,
        records=env.records, documents=env.documents, service=env.service, models=env.model)
    install_project_routes(app, records=env.records)
    install_library_routes(app, records=env.records, service=env.service,
        documents=env.documents, workspace=domains, models=env.model)
    original_document = env.documents.read(env.doc)
    originals = env.records.list('workspace_items')
    with TestClient(app) as http:
        assert http.get('/api/v2/projects').status_code == 200
        moved = http.post(f'/api/v2/library/notes/{env.doc}/file', json={
            'project_id': 'alpha', 'target_project_id': 'beta', 'expected_revision': 1})
        assert moved.status_code == 200, moved.text
        target = moved.json()['target_document_id']
        env.documents.save_user_edit(target, markdown='用户补充证据用于写材料', expected_revision=1)
        assert env.documents.read(target)['revision'] == 2
        env.model.response = json.dumps({'insights': [{
            'kind': 'new_method', 'relation': 'new', 'text': '补充证据可供写材料',
            'conditions': ['写材料时'], 'target_id': None, 'scope_hint': 'me'}], 'supports': []})
        candidates = generate_insights(env.model, env.service, env.documents, 'beta', target)
        assert len(candidates) == 1
        candidate = candidates[0]
        personal = http.post(f"/api/v2/library/inbox/insight/{candidate['id']}/file", json={
            'source_project_id': 'beta', 'target_project_id': 'me',
            'expected_revision': candidate['revision'], 'confirm': True})
        assert personal.status_code == 200, personal.text
        confirmed = personal.json()
        assert confirmed['state'] == 'active'
        assert confirmed['source_documents'] == [{'project_id': 'beta', 'document_id': target}]
        before, calls = env.records.list_all(), env.model.calls
        note = http.get('/api/v2/library/drill', params={
            'project_id': 'beta', 'from': 'note', 'id': target})
        assert note.status_code == 200, note.text
        note_data = note.json()
        foreign = http.get('/api/v2/library/drill', params={
            'project_id': 'me', 'from': 'insight', 'id': confirmed['id']})
        assert foreign.status_code == 200, foreign.text
        drill = foreign.json()
        assert note_data['source_project_id'] == 'alpha'
        assert drill['source'] == note_data['source'] and drill['sources'] == note_data['sources']
        returned_text = http.get(f"/api/v2/library/sources/{drill['source']['id']}/text", params={
            'project_id': drill['source_project_id']})
        original_text = http.get(f"/api/v2/library/sources/{drill['source']['id']}/text", params={
            'project_id': 'alpha'})
        assert original_text.status_code == 200, original_text.text
        assert returned_text.status_code == 200, returned_text.text
        assert returned_text.json() == original_text.json()
        assert drill['source_project_id'] == 'alpha'
        assert drill['readonly'] is True and drill['source_readonly'] is True
        assert drill['note']['document_id'] == target
        assert env.documents.read(env.doc) == original_document
        assert env.records.list('workspace_items') == originals
        assert env.records.list_all() == before and env.model.calls == calls == 2
