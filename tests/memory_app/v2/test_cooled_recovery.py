import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from backend.memory_app.v2.layers import mark_verified
from backend.memory_app.v2.privacy import set_private_project
from backend.memory_app.privacy_state import _SCOPES
from tests.memory_app.v2.test_signal_reviews import action, five_answers, real_owner
from tests.memory_app.v2.test_workbench_ask import env, add_document, publish


def cool_document(env):
    identity, _ = add_document(env)
    mark_verified(env.records, identity, 2, expected_current_revision=2)
    with env.records.begin() as tx:
        pref = tx.put('v2_document_recall', identity,
            {'state': 'cooled', 'by': 'user', 'changed_at': '2026-10-07T00:00:00+00:00'},
            expected_revision=0)
        tx.commit()
    return identity, pref


def restore(env, identity, *, project='alpha', document_revision=2, preference_revision=1):
    return env.http.post(f'/api/v2/library/notes/{identity}/restore-recall', json={
        'project_id': project, 'document_revision': document_revision,
        'preference_revision': preference_revision})


@pytest.mark.parametrize('kind', ['insight', 'document'])
@pytest.mark.parametrize('private', [False, True])
def test_actual_unused_confirmation_then_http_restore_preserves_facts(env, kind, private):
    if kind == 'insight':
        recognition, _ = publish(env)
        identity = recognition.id
    else:
        identity, _ = add_document(env)
        mark_verified(env.records, identity, 2, expected_current_revision=2)
    five_answers(env)
    owner = real_owner(env)
    items = [row for row in owner.current('alpha')['items'] if row['kind'] == 'unused']
    assert len(items) == 1
    assert items[0]['evidence']['object']['id'] == identity
    owner.decide('alpha', [action(items[0])])
    collection = 'recognition_recall_preferences' if kind == 'insight' else 'v2_document_recall'
    cooled = env.records.read(collection, identity)
    assert cooled.payload['state'] == 'cooled'
    if private:
        set_private_project(env.records, 'alpha', True, 0)
    settings = env.http.get('/api/v2/settings/signals').json()
    assert env.http.patch('/api/v2/settings/signals', json={
        'enabled': False, 'expected_revision': settings['revision']}).status_code == 200
    protected = {name: env.records.list(name) for name in (
        'documents', 'document_revisions', 'document_markdown', 'workspace_items',
        'recognitions', 'experiences', 'v2_verifications', 'v2_signal_decisions', 'v2_signal_settings', _SCOPES)}
    calls = env.model.calls
    if kind == 'insight':
        view = env.http.get('/api/v2/library/drill', params={
            'project_id': 'alpha', 'from': 'insight', 'id': identity}).json()['insight']
        assert view['state'] == 'active' and view['recall_state'] == 'cooled'
        response = env.http.post(f'/api/v2/library/insights/{identity}/forget',
            json={'project_id': 'alpha', 'forgotten': False})
        assert response.status_code == 200, response.text
        assert response.json()['state'] == 'active' and response.json()['recall_state'] == 'normal'
    else:
        view = env.http.get('/api/v2/library/drill', params={
            'project_id': 'alpha', 'from': 'note', 'id': identity}).json()['note']
        assert view['recall_state'] == 'cooled'
        assert view['recall_preference_revision'] == cooled.revision
        response = restore(env, identity, preference_revision=cooled.revision)
        assert response.status_code == 200, response.text
        assert response.json() == {'document_id': identity, 'recall_state': 'normal',
                                   'recall_preference_revision': cooled.revision + 1}
        assert env.http.get('/api/v2/library/drill', params={
            'project_id': 'alpha', 'from': 'note', 'id': identity}).json()['note']['verified'] is True
    assert env.records.read(collection, identity).payload['state'] == 'normal'
    assert env.model.calls == calls
    assert {name: env.records.list(name) for name in protected} == protected


@pytest.mark.parametrize('drift', ['document', 'preference', 'project', 'archived'])
def test_document_restore_rejects_actual_stale_or_wrong_owner(env, drift):
    identity, pref = cool_document(env)
    if drift == 'document':
        env.documents.save_user_edit(identity, expected_revision=2, markdown='# Changed')
    elif drift == 'preference':
        with env.records.begin() as tx:
            tx.put('v2_document_recall', identity, dict(pref.payload), expected_revision=pref.revision)
            tx.commit()
    elif drift == 'archived':
        env.documents.archive(identity, expected_revision=2)
    before = {name: env.records.list(name) for name in (
        'documents', 'document_revisions', 'document_markdown', 'v2_document_recall', 'v2_verifications', 'v2_usage_document')}
    response = restore(env, identity, project='beta' if drift == 'project' else 'alpha',
        document_revision=3 if drift == 'archived' else 2)
    assert response.status_code == (404 if drift == 'project' else 409), response.text
    assert {name: env.records.list(name) for name in before} == before


@pytest.mark.parametrize('field,value', [('document_revision', True), ('preference_revision', False),
                                        ('document_revision', 0), ('preference_revision', -1)])
def test_document_restore_rejects_invalid_revisions(env, field, value):
    identity, _ = cool_document(env)
    before = env.records.read('v2_document_recall', identity)
    response = restore(env, identity, **{field: value})
    assert response.status_code == 400, response.text
    assert env.records.read('v2_document_recall', identity) == before


def test_document_restore_does_not_clear_a_new_normal_preference(env):
    identity, pref = cool_document(env)
    with env.records.begin() as tx:
        normal = tx.put('v2_document_recall', identity, {**pref.payload, 'state': 'normal'},
                        expected_revision=pref.revision)
        tx.commit()
    assert restore(env, identity, preference_revision=normal.revision).status_code == 409
    assert env.records.read('v2_document_recall', identity) == normal


def test_document_restore_sql_failure_rolls_back_preference(env):
    identity, pref = cool_document(env)
    with env.records._connect() as connection:
        connection.execute("CREATE TRIGGER refuse_recovery BEFORE UPDATE ON crp_structured_records "
            "WHEN NEW.collection = 'v2_document_recall' BEGIN SELECT RAISE(ABORT, 'controlled'); END")
    with pytest.raises(sqlite3.IntegrityError):
        restore(env, identity)
    assert env.records.read('v2_document_recall', identity) == pref


def test_concurrent_http_restores_have_one_cas_winner(env):
    identity, pref = cool_document(env)
    barrier = Barrier(2)
    def request():
        barrier.wait(timeout=10)
        return restore(env, identity).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: request(), range(2)))
    assert sorted(results) == [200, 409]
    saved = env.records.read('v2_document_recall', identity)
    assert saved.revision == pref.revision + 1 and saved.payload['state'] == 'normal'
