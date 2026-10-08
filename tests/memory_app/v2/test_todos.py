from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha1

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.memory_app.v2.test_workbench_ask import env, add_document
from backend.memory_app.v2.todos import TodoService, install_todo_routes
from backend.memory_app.v2.library import LibraryRead


NOW = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)


@pytest.fixture
def todos(env):
    read = LibraryRead(env.records, env.service, env.documents, env.domains)
    return TodoService(env.records, read, now=lambda: NOW)


def document(env, text='取资料', project='alpha'):
    doc, _ = add_document(env, project=project)
    env.documents.save_user_edit(doc, expected_revision=2, markdown='# 标题\n\n## 待办\n' + text)
    return doc


def test_stable_normalized_identity_and_duplicate_occurrence(env, todos):
    doc = document(env, '- 取  资料\n- 取 资料\n- 下一步')
    first = todos.list('alpha')['items']
    assert first == todos.list('alpha')['items']
    assert len({row['id'] for row in first}) == 3
    assert all(row['id'].startswith('td') and len(row['id']) == 26 for row in first)
    assert first[0]['id'] == 'td' + sha1((doc + '\n0\n取 资料').encode()).hexdigest()[:24]
    todos.set_done(first[0]['id'], True, None)
    env.documents.save_user_edit(doc, expected_revision=3, markdown='# 标题\n\n## 待办\n- 取\t资料\n- 取 资料\n- 改字')
    after = todos.list('alpha', include_done=True)['items']
    assert {row['id'] for row in first[:2]} <= {row['id'] for row in after}
    assert first[2]['id'] not in {row['id'] for row in after}
    assert next(row for row in after if row['text'] == '改字')['done'] is False


def test_done_undo_idempotency_conflict_and_original_unchanged(env, todos):
    doc = document(env, '- 取资料')
    before = (env.documents.read(doc), env.documents.markdown(doc), env.documents.revisions(doc))
    activity = env.records.list('v2_activity')
    item = todos.list('alpha')['items'][0]
    assert item['revision'] is None and item['done_at'] is None
    saved = todos.set_done(item['id'], True, None)
    assert saved['done'] is True and saved['revision'] == 1 and saved['done_at'] == NOW.isoformat()
    assert todos.set_done(item['id'], True, None) == saved
    assert todos.list('alpha')['items'] == []
    with pytest.raises(Exception) as error:
        todos.set_done(item['id'], False, None)
    assert error.value.status_code == 409
    undone = todos.set_done(item['id'], False, 1)
    assert undone['revision'] == 2 and undone['done_at'] is None
    assert todos.set_done(item['id'], False, 1) == undone
    assert before == (env.documents.read(doc), env.documents.markdown(doc), env.documents.revisions(doc))
    assert env.records.list('v2_activity') == activity


def test_removed_and_hidden_todo_are_not_writable(env, todos):
    doc = document(env, '- 取资料')
    item = todos.list('alpha')['items'][0]
    todos.set_done(item['id'], True, None)
    env.documents.save_user_edit(doc, expected_revision=3, markdown='# 标题\n\n## 待办\n- 新文字')
    with pytest.raises(Exception) as error:
        todos.set_done(item['id'], False, 1)
    assert error.value.status_code == 404 and error.value.detail == 'todo_not_found'
    assert env.records.read('v2_todo_state', item['id']).payload['done'] is True


def test_scope_visibility_forgotten_and_cooled(env, todos):
    visible = document(env, '- alpha')
    archived = document(env, '- archived')
    forgotten = document(env, '- forgotten')
    cooled = document(env, '- cooled')
    document(env, '- beta', project='beta')
    document(env, '- me', project='me')
    env.documents.archive(archived, expected_revision=3)
    with env.records.begin() as tx:
        tx.put('v2_document_recall', forgotten, {'state': 'forgotten', 'by': 'auto'}, expected_revision=0)
        tx.put('v2_document_recall', cooled, {'state': 'cooled', 'by': 'auto'}, expected_revision=0)
        tx.commit()
    assert {row['document_id'] for row in todos.list('alpha')['items']} == {visible, cooled}
    assert {row['project_id'] for row in todos.list()['items']} == {'alpha', 'beta', 'me'}
    with env.records.begin() as tx:
        tx.put('workspace_review_intents', 'pending-todo', {'source_id': env.documents.read(visible)['source_refs'][0]['source_id'], 'project_id':'alpha', 'state':'pending'}, expected_revision=0)
        tx.commit()
    assert [row['document_id'] for row in todos.list('alpha')['items']] == [cooled]


def test_current_forgotten_or_pending_todo_returns_404(env, todos):
    doc = document(env, '- private current')
    item = todos.list('alpha')['items'][0]
    with env.records.begin() as tx:
        tx.put('v2_document_recall', doc, {'state':'forgotten', 'by':'manual'}, expected_revision=0)
        tx.commit()
    with pytest.raises(Exception) as error:
        todos.set_done(item['id'], True, None)
    assert error.value.status_code == 404
    assert env.records.read('v2_todo_state', item['id']) is None


def test_parallel_completion_is_idempotent_and_single_revision(env, todos):
    document(env, '- concurrent')
    identity = todos.list('alpha')['items'][0]['id']
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: todos.set_done(identity, True, None), range(2)))
    assert results[0] == results[1]
    assert env.records.read('v2_todo_state', identity).revision == 1


def test_updated_documents_sort_and_registered_project_name(env, todos):
    old = document(env, '- old')
    new = document(env, '- new')
    with env.records.begin() as tx:
        for identity, updated in [(old, '2026-10-01T23:00:00+08:00'), (new, '2026-10-02T09:00:00+08:00')]:
            row = tx.read('documents', identity)
            tx.put('documents', identity, {**row.payload, 'updated_at':updated}, expected_revision=row.revision)
        tx.put('v2_projects', 'alpha', {'name':'阅读', 'scenes':[]}, expected_revision=0)
        tx.commit()
    rows = todos.list('alpha')['items']
    assert [row['document_id'] for row in rows] == [new, old]
    assert all(row['project_name'] == '阅读' for row in rows)


def test_completed_window_order_and_limit(env, todos):
    doc = document(env, '\n'.join('- 条目' + str(n) for n in range(105)))
    all_items = todos.list('alpha')['items']
    assert len(all_items) == 100
    completed = todos.set_done(all_items[0]['id'], True, None)
    assert len(todos.list('alpha', include_done=True)['items']) == 100
    assert all(not row['done'] for row in todos.list('alpha', include_done=True)['items'])
    env.documents.save_user_edit(doc, expected_revision=3, markdown='# 标题\n\n## 待办\n- 条目0\n- 新条目')
    assert todos.list('alpha', include_done=True)['items'][-1]['id'] == completed['id']
    later = TodoService(env.records, todos.read, now=lambda: NOW + timedelta(days=7, seconds=1))
    assert [row['text'] for row in later.list('alpha', include_done=True)['items']] == ['新条目']


def test_http_routes_and_validation(env):
    document(env, '- HTTP待办')
    app = FastAPI()
    install_todo_routes(app, records=env.records, service=env.service, documents=env.documents, workspace=env.domains)
    with TestClient(app) as client:
        row = client.get('/api/v2/todos', params={'project_id':'alpha'}).json()['items'][0]
        assert client.post('/api/v2/todos/' + row['id'] + '/done', json={'expected_revision':None}).status_code == 200
        assert client.post('/api/v2/todos/' + row['id'] + '/undo', json={'expected_revision':None}).status_code == 409
        assert client.post('/api/v2/todos/' + row['id'] + '/undo', json={'expected_revision':True}).status_code == 400
        assert client.post('/api/v2/todos/not-here/done', json={'expected_revision':None}).status_code == 404
