from tests.memory_app.v2.test_workbench_ask import env, add_document, publish
from backend.recognition import WorkScope


def pending(env):
    experience = env.service.stage_experience(scope=WorkScope('local-user', 'alpha'), content='Evidence')
    return env.service.propose(scope=WorkScope('local-user', 'alpha'), content='alpha useful',
                               source_experience_ids=[experience])


def action(env, identity, verb, **body):
    return env.http.post(f'/api/v2/library/insights/{identity}/{verb}', json={'project_id': 'alpha', **body})


def test_confirm_conflict_then_recall(env):
    candidate = pending(env)
    assert action(env, candidate.id, 'confirm', expected_revision=2).status_code == 409
    response = action(env, candidate.id, 'confirm', expected_revision=1)
    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved['state'] == 'active'
    assert any(c['id'] == saved['id'] for c in env.domains.query.prepare_ask('alpha', 'alpha useful?')['chosen'])


def test_drop_conflict_then_absent_from_list(env):
    candidate = pending(env)
    assert action(env, candidate.id, 'drop', expected_revision=2).status_code == 409
    assert action(env, candidate.id, 'drop', expected_revision=1).status_code == 200
    assert env.http.get('/api/v2/library/insights', params={'project_id': 'alpha'}).json()['items'] == []


def test_edit_candidate_cas_preserves_sources(env):
    candidate = pending(env)
    path = f'/api/v2/library/insights/{candidate.id}'
    body = {'project_id': 'alpha', 'expected_revision': 2, 'text': 'Edited', 'conditions': ['condition']}
    assert env.http.patch(path, json=body).status_code == 409
    before = env.records.read('recognition_candidates', candidate.id).payload['source_experience_ids']
    body['expected_revision'] = 1
    response = env.http.patch(path, json=body)
    assert response.status_code == 200, response.text
    assert response.json()['text'] == 'Edited'
    assert response.json()['conditions'] == ['condition']
    assert env.records.read('recognition_candidates', candidate.id).payload['source_experience_ids'] == before


def test_revise_recognition_and_stale_conflict(env):
    recognition, _ = publish(env)
    path = f'/api/v2/library/insights/{recognition.id}'
    body = {'project_id': 'alpha', 'expected_revision': 1, 'text': 'Revised alpha', 'conditions': []}
    response = env.http.patch(path, json=body)
    assert response.status_code == 200, response.text
    assert response.json()['id'] == recognition.id
    assert response.json()['revision'] == 2
    assert response.json()['text'] == 'Revised alpha'
    assert env.http.patch(path, json=body).status_code == 409


def test_forget_restore_and_pending_conflict_preserve_original(env):
    candidate = pending(env)
    assert action(env, candidate.id, 'forget', forgotten=True).status_code == 409
    recognition, _ = publish(env)
    before = env.records.read('recognitions', recognition.id)
    response = action(env, recognition.id, 'forget', forgotten=True)
    assert response.status_code == 200, response.text
    assert response.json()['state'] == 'forgotten'
    assert env.domains.query.prepare_ask('alpha', 'alpha beta gamma?')['chosen'] == []
    assert action(env, recognition.id, 'forget', forgotten=False).json()['state'] == 'active'
    assert env.records.read('recognitions', recognition.id) == before


def test_verify_current_revision_and_conflict_keep_document(env):
    doc, _ = add_document(env)
    before = env.records.read('documents', doc)
    path = f'/api/v2/library/notes/{doc}/verify'
    assert env.http.post(path, json={'project_id': 'alpha', 'document_revision': 1}).status_code == 409
    response = env.http.post(path, json={'project_id': 'alpha', 'document_revision': 2})
    assert response.status_code == 200, response.text
    assert response.json()['verified'] is True
    assert env.records.read('v2_verifications', doc).payload['document_revision'] == 2
    assert env.records.read('documents', doc) == before
    assert env.http.post(path, json={'project_id': 'beta', 'document_revision': 2}).status_code == 404


def test_verify_rechecks_current_revision_inside_transaction(env, monkeypatch):
    doc, _ = add_document(env)
    read = type(env.documents).read
    changed = False
    def competing_read(repository, identity):
        nonlocal changed
        value = read(repository, identity)
        if repository is env.documents and identity == doc and not changed:
            changed = True
            env.documents.save_user_edit(doc, expected_revision=2, markdown='# Concurrent revision')
        return value
    monkeypatch.setattr(type(env.documents), 'read', competing_read)
    response = env.http.post(f'/api/v2/library/notes/{doc}/verify',
                             json={'project_id': 'alpha', 'document_revision': 2})
    assert response.status_code == 409
    assert env.records.read('v2_verifications', doc) is None
