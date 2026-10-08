from datetime import datetime, timedelta, timezone

import pytest

from backend.memory_app.v2.usage import UsageService
from backend.memory_app.recall_preferences import set_preference
from backend.recognition import WorkScope
from tests.memory_app.v2.test_workbench_ask import env, publish, add_document, ask


def clock():
    return datetime(2026, 10, 1, tzinfo=timezone.utc)


def usage(env, kind, identity):
    return env.records.read('v2_usage_' + kind, identity).payload


def test_initial_reset_then_use_and_strengthened_half_life(env):
    recognition, _ = publish(env)
    now = [clock()]
    tracker = UsageService(env.records, now=lambda: now[0])
    tracker.initialize('insight', recognition.id, 'alpha')
    assert usage(env, 'insight', recognition.id)['count'] == 1
    assert usage(env, 'insight', recognition.id)['score'] == 1
    before = env.records.read('v2_usage_insight', recognition.id)
    tracker.initialize('insight', recognition.id, 'alpha')
    assert env.records.read('v2_usage_insight', recognition.id) == before
    # T10.6: the initial use makes the half-life 30 * (1 + ln(2)) days.
    now[0] += timedelta(days=50.7944154168)
    tracker.record_usage('insight', recognition.id, 'alpha', .5)
    assert usage(env, 'insight', recognition.id)['score'] == pytest.approx(1)
    assert usage(env, 'insight', recognition.id)['count'] == 2
    tracker.record_usage('insight', recognition.id, 'alpha', .2, count=False)
    assert usage(env, 'insight', recognition.id)['score'] == pytest.approx(1.2)
    assert usage(env, 'insight', recognition.id)['count'] == 2
    tracker.record_usage('insight', recognition.id, 'alpha', 1, reset=True)
    assert usage(env, 'insight', recognition.id)['score'] == 1
    assert usage(env, 'insight', recognition.id)['count'] == 3


def test_summary_and_note_share_document_and_candidates_and_sources_are_ignored(env):
    doc, source = add_document(env)
    tracker = UsageService(env.records)
    original = env.records.read('documents', doc)
    tracker.record_usage('summary', doc, 'alpha', 1)
    tracker.record_usage('document', doc, 'alpha', .5)
    assert usage(env, 'document', doc)['count'] == 3
    assert len(env.records.list('v2_usage_document')) == 1
    experience = env.service.stage_experience(scope=WorkScope('local-user', 'alpha'), content='Evidence')
    candidate = env.service.propose(scope=WorkScope('local-user', 'alpha'), content='alpha', source_experience_ids=[experience])
    assert tracker.record_usage('insight', candidate.id, 'alpha', 1) is None
    assert tracker.record_usage('source', source, 'alpha', 1) is None
    assert env.records.read('documents', doc) == original
    assert env.records.list('v2_usage_insight') == ()


def test_open_scopes_identity_and_resolves_published_alias(env):
    recognition, _ = publish(env)
    candidate = next(row for row in env.records.list('recognition_candidates') if row.payload.get('recognition_id') == recognition.id)
    endpoint = '/api/v2/usage/open'
    response = env.http.post(endpoint, json={'project_id':'alpha','kind':'insight','id':candidate.object_id})
    assert response.status_code == 204 and response.content == b''
    assert usage(env, 'insight', recognition.id)['count'] == 2
    assert env.http.post(endpoint, json={'project_id':'other','kind':'insight','id':recognition.id}).status_code == 404
    assert env.http.post(endpoint, json={'project_id':'alpha','kind':'insight','id':'missing'}).status_code == 404
    assert env.http.post(endpoint, json={'project_id':'alpha','kind':'insight','id':recognition.id,'score':9}).status_code == 400
    assert usage(env, 'insight', recognition.id)['count'] == 2


def test_answer_records_only_cited_after_receipt_and_not_on_replay(env):
    cited, _ = publish(env, 'alpha beta gamma')
    uncited, _ = publish(env, 'alpha')
    env.model.numbers = [1]
    response = ask(env)
    assert response.status_code == 200
    receipt = response.json()['turn']['receipt']['ask']
    selected = env.records.read('workspace_ask_receipts', receipt['egress_receipt_id']).payload['sources']
    assert len(selected) == 2
    for index, source in enumerate(selected, 1):
        if index == 1:
            row = usage(env, 'insight', source['id'])
            assert row['count'] == 2
            assert row['score'] == pytest.approx(2, abs=.01)
        else:
            assert env.records.read('v2_usage_insight', source['id']) is None
    before = {row.object_id: row for row in env.records.list('v2_usage_insight')}
    thread = response.json()['thread_id']
    assert env.http.get(f'/api/v2/workbench/threads/{thread}', params={'project_id':'alpha'}).status_code == 200
    assert before == {row.object_id: row for row in env.records.list('v2_usage_insight')}


def test_document_usage_is_counted_once_across_summary_and_note(env):
    doc, _ = add_document(env, summary='alpha 原文', body='alpha 原文具体')
    response = ask(env, text='alpha 原文具体?')
    assert response.status_code == 200
    assert usage(env, 'document', doc)['count'] == 2


def test_receipt_survives_usage_write_failure(env, monkeypatch):
    from core.storage_provider import SQLiteStructuredRecordUnitOfWork
    original = SQLiteStructuredRecordUnitOfWork.put
    def fail_usage(self, collection, *args, **kwargs):
        if collection.startswith('v2_usage_'):
            raise OSError('private details must not enter logs')
        return original(self, collection, *args, **kwargs)
    monkeypatch.setattr(SQLiteStructuredRecordUnitOfWork, 'put', fail_usage)
    publish(env)
    response = ask(env)
    assert response.status_code == 200
    assert response.json()['turn']['receipt']['ask']['answer'] == 'Synthetic answer'
    assert env.records.list('v2_usage_insight') == ()


def test_edit_and_restore_reset_score_without_changing_content_revision_for_restore(env):
    recognition, _ = publish(env)
    tracker = UsageService(env.records)
    tracker.record_usage('insight', recognition.id, 'alpha', 4)
    response = env.http.patch(f'/api/v2/library/insights/{recognition.id}', json={
        'project_id':'alpha','expected_revision':1,'text':'alpha edited','conditions':[]})
    assert response.status_code == 200
    assert usage(env, 'insight', recognition.id)['score'] == 1
    before = env.records.read('recognitions', recognition.id)
    set_preference(env.records, WorkScope('local-user','alpha'), recognition.id,
        recognition_revision=before.revision, preference_revision=0, state='forgotten')
    set_preference(env.records, WorkScope('local-user','alpha'), recognition.id,
        recognition_revision=before.revision, preference_revision=1, state='normal')
    assert usage(env, 'insight', recognition.id)['score'] == 1
    assert usage(env, 'insight', recognition.id)['count'] == 4
    assert env.records.read('recognitions', recognition.id) == before


def test_confirmation_creates_one_use_but_first_edit_adds_to_implicit_use(env):
    scope = WorkScope('local-user', 'alpha')
    experience = env.service.stage_experience(scope=scope, content='Evidence')
    candidate = env.service.propose(scope=scope, content='alpha', source_experience_ids=[experience])
    response = env.http.post(f'/api/v2/library/insights/{candidate.id}/confirm',
        json={'project_id':'alpha','expected_revision':1})
    assert response.status_code == 200
    assert usage(env, 'insight', response.json()['id'])['count'] == 1
    recognition, _ = publish(env)
    set_preference(env.records, scope, recognition.id,
        recognition_revision=1, preference_revision=0, state='normal')
    assert env.records.read('v2_usage_insight', recognition.id) is None
    UsageService(env.records).record_usage('insight', recognition.id, 'alpha', 1, reset=True)
    assert usage(env, 'insight', recognition.id)['count'] == 2


def test_document_edit_route_resets_usage_and_keeps_conflict_and_project_guards(tmp_path, monkeypatch):
    from tests.memory_app.test_api import _client
    from core.document_engine.ports import DocumentDraft
    monkeypatch.setenv('CHRIPTMAS_APP_ROOT', str(tmp_path))
    client, _ = _client(tmp_path)
    with client:
        documents = client.app.state.recognition_documents
        records = client.app.state.recognition_service.records
        document = documents.create(DocumentDraft(title='Synthetic', document_type='legacy-material',
            markdown='Synthetic evidence', source_refs=({'source_id':'synthetic-source','locator':'text:0'},), project_id='alpha'))
        with records.begin() as tx:
            tx.put('workspace_review_intents', 'synthetic-review', {
                'state':'confirmed','project_id':'alpha','document_id':document['id'],
            }, expected_revision=0)
            tx.commit()
        endpoint = '/api/recognition/documents/' + document['id']
        body = {'project_id':'alpha','expected_revision':1,'markdown':'Edited synthetic evidence'}
        assert client.patch(endpoint, json={**body, 'project_id':'other'}).status_code == 409
        assert client.patch(endpoint, json=body).status_code == 200
        current = records.read('v2_usage_document', document['id'])
        assert current.payload['score'] == 1 and current.payload['count'] == 2
        assert client.patch(endpoint, json=body).status_code == 409
        assert records.read('v2_usage_document', document['id']) == current


@pytest.mark.parametrize("composed", [True, False])
def test_document_restore_route_resets_usage_after_real_restore(env, monkeypatch, composed):
    if not composed:
        delattr(env.http.app.state, "document_record_usage")
    from backend.api.routes.product import documents as routes
    from backend.api.container import get_container
    from types import SimpleNamespace
    env.http.app.include_router(routes.router)
    env.http.app.dependency_overrides[get_container] = lambda: SimpleNamespace(root_dir=env.root)
    # Inject the real temporary repository through its normal resolution boundary.
    monkeypatch.setattr(routes.product_repositories, '_object_store', lambda root: (None, None))
    monkeypatch.setattr(routes.product_repositories, '_document_repository', lambda *args: env.documents)
    doc, _ = add_document(env)
    tracker = UsageService(env.records)
    tracker.record_usage('document', doc, 'alpha', 5)
    archived = env.documents.archive(doc, expected_revision=2)
    path = f'/api/rebuild/documents/{doc}/restore?project_id=alpha'
    assert env.http.post(path.replace('alpha', 'other'), json={'expected_revision':archived['revision']}).status_code == 404
    response = env.http.post(path, json={'expected_revision':archived['revision']})
    assert response.status_code == 200, response.text
    assert usage(env, 'document', doc)['score'] == 1
    assert usage(env, 'document', doc)['count'] == 3
    before = env.records.read('v2_usage_document', doc)
    assert env.http.post(path, json={'expected_revision':archived['revision']}).status_code == 409
    assert env.records.read('v2_usage_document', doc) == before
