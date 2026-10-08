import asyncio

import pytest

from tests.memory_app.v2.test_workbench_ask import env
from backend.memory_app.document_recognition import ensure_document_experience
from backend.memory_app.source_egress import SourceEgressService
from backend.memory_app.v2.auto_confirm import process_and_confirm
from backend.memory_app.v2.privacy import set_private_project
from backend.recognition import RecognitionConflict, WorkScope


def seed(env, target='beta'):
    env.model.intake = True
    item = asyncio.run(env.domains.intake.add_text({'project_id': 'alpha', 'text': '原件只在alpha。新方法。'}))
    document = asyncio.run(process_and_confirm(env.domains, item['id'], 'alpha'))['document_id']
    env.model.intake = False
    experience, _ = ensure_document_experience(env.documents, env.service, 'alpha', document)
    old = env.service.propose(scope=WorkScope('local-user', 'alpha'), content='新方法',
        conditions=['送礼时'], source_experience_ids=[experience])
    env.http.get('/api/v2/projects')
    with env.records.begin() as tx:
        if tx.read('v2_projects', target) is None:
            tx.put('v2_projects', target, {'name': target, 'scenes': [], 'builtin': None}, expected_revision=0)
        tx.put('v2_candidate_hints', old.id, {'project_id': 'alpha', 'relation': 'new',
            'target_id': None, 'scope_hint': target}, expected_revision=0)
        tx.commit()
    return old, experience, document, item


def confirm_to(env, old, target='beta'):
    return env.http.post(f'/api/v2/library/inbox/insight/{old.id}/file', json={
        'source_project_id': 'alpha', 'target_project_id': target,
        'expected_revision': old.revision, 'confirm': True})


@pytest.mark.parametrize('target', ['beta', 'me'])
def test_explicit_confirmation_copies_true_provenance_and_retires_original(env, target):
    old, experience, document, item = seed(env, target)
    before_doc = env.documents.read(document)
    before_item = env.records.read('workspace_items', item['id'])
    result = confirm_to(env, old, target)
    assert result.status_code == 200, result.text
    new = result.json()
    assert new['kind'] == 'recognition' and new['state'] == 'active'
    assert env.records.read('recognition_candidates', old.id).payload['state'] == 'rejected'
    new_row = env.records.read('recognitions', new['id'])
    copied = env.records.read('recognition_experiences', new_row.payload['source_experience_ids'][0])
    original = env.records.read('recognition_experiences', experience)
    assert copied.payload['provenance'] == original.payload['provenance']
    assert copied.payload['content'] == original.payload['content']
    marker = env.records.read('v2_experience_origins', copied.object_id)
    assert marker.payload['source_experience_id'] == experience
    assert env.documents.read(document) == before_doc
    assert env.records.read('workspace_items', item['id']) == before_item
    assert new['document_ids'] == []
    assert new['source_documents'] == [{'project_id': 'alpha', 'document_id': document}]
    qualified = env.service.get_recognition(scope=WorkScope('local-user', target), recognition_id=new['id'])
    assert qualified.authorized
    assert not any(row.get('document_id') == document or row['id'] in {document, item['id']}
        for row in env.domains.query.query_entries(target))
    drill = env.http.get('/api/v2/library/drill', params={'project_id': target, 'from': 'insight', 'id': new['id']})
    assert drill.status_code == 200, drill.text
    assert drill.json()['readonly'] is True and drill.json()['source_project_id'] == 'alpha'
    assert drill.json()['note']['document_id'] == document
    count = len(env.records.list('recognition_experiences'))
    assert confirm_to(env, old, target).status_code == 409
    assert len(env.records.list('recognition_experiences')) == count


def test_private_source_cannot_move_and_later_privacy_revokes_only_egress(env):
    old, experience, _, _ = seed(env)
    set_private_project(env.records, 'alpha', True, 0)
    before = env.records.list_all()
    assert confirm_to(env, old).status_code == 400
    assert env.records.list_all() == before
    private = env.records.read('v2_private_scopes', 'alpha')
    set_private_project(env.records, 'alpha', False, private.revision)
    result = confirm_to(env, old)
    assert result.status_code == 200, result.text
    identity = result.json()['id']
    scope = WorkScope('local-user', 'beta')
    authority = SourceEgressService(env.records)
    snapshot = authority.snapshot(scope, [{'type': 'recognition', 'id': identity, 'revision': 1}])
    private = env.records.read('v2_private_scopes', 'alpha')
    set_private_project(env.records, 'alpha', True, private.revision)
    assert env.service.get_recognition(scope=scope, recognition_id=identity).authorized
    with pytest.raises(RecognitionConflict):
        authority.validate_snapshot(scope, snapshot)
    with pytest.raises(RecognitionConflict):
        authority.require(authority.snapshot(scope, [{'type': 'recognition', 'id': identity, 'revision': 1}]), 'generation')


@pytest.mark.parametrize('field,value', [('source_user_id', 'other-user'), ('source_revision', 999),
    ('source_experience_id', 'absent'), ('target_project_id', 'wrong')])
def test_forged_origin_never_grants_qualification_or_egress(env, field, value):
    old, _, _, _ = seed(env)
    result = confirm_to(env, old)
    assert result.status_code == 200, result.text
    identity = result.json()['id']
    recognition = env.records.read('recognitions', identity)
    copied_id = recognition.payload['source_experience_ids'][0]
    with env.records.begin() as tx:
        marker = tx.read('v2_experience_origins', copied_id)
        tx.put('v2_experience_origins', copied_id, {**marker.payload, field: value}, expected_revision=marker.revision)
        tx.commit()
    scope = WorkScope('local-user', 'beta')
    assert not env.service.get_recognition(scope=scope, recognition_id=identity).authorized
    with pytest.raises(RecognitionConflict):
        SourceEgressService(env.records).snapshot(scope, [{'type': 'recognition', 'id': identity, 'revision': 1}])


def test_explicit_destination_rejects_a_changed_frozen_parent_before_writing(env):
    old, experience, _, _ = seed(env)
    scope = WorkScope('local-user', 'alpha')
    parent = env.service.publish(scope=scope, candidate_id=old.id, expected_revision=1, reviewer='local-user')
    child = env.service.propose(scope=scope, content='另一个结论', source_experience_ids=[],
        source_recognition_ids=[parent.id])
    with env.records.begin() as tx:
        tx.put('v2_candidate_hints', child.id, {'project_id': 'alpha', 'relation': 'supplement',
            'target_id': parent.id, 'scope_hint': 'beta'}, expected_revision=0)
        current = tx.read('recognitions', parent.id)
        tx.put('recognitions', parent.id, dict(current.payload), expected_revision=current.revision)
        tx.commit()
    before = env.records.list_all()
    result = confirm_to(env, child)
    assert result.status_code == 409, result.text
    assert env.records.list_all() == before


@pytest.mark.parametrize('change', ['missing', 'bool', 'float', 'extra'])
def test_destination_requires_original_strict_frozen_revision_maps(env, change):
    old, experience, _, _ = seed(env)
    with env.records.begin() as tx:
        row = tx.read('recognition_candidates', old.id)
        payload = dict(row.payload)
        if change == 'missing':
            payload.pop('source_experience_revisions')
        else:
            payload['source_experience_revisions'] = {
                experience: True if change == 'bool' else 1.0 if change == 'float' else 1}
            if change == 'extra':
                payload['source_experience_revisions']['unrelated'] = 1
        changed = tx.put('recognition_candidates', old.id, payload, expected_revision=row.revision)
        tx.commit()
    before = env.records.list_all()
    result = env.http.post(f'/api/v2/library/inbox/insight/{old.id}/file', json={
        'source_project_id': 'alpha', 'target_project_id': 'beta',
        'expected_revision': changed.revision, 'confirm': True})
    assert result.status_code == 400, result.text
    assert env.records.list_all() == before


@pytest.mark.parametrize('private', ['project', 'source'])
def test_private_candidate_hides_destination_but_keeps_local_content(env, private):
    old, experience, _, _ = seed(env)
    if private == 'project':
        set_private_project(env.records, 'alpha', True, 0)
    else:
        SourceEgressService(env.records).set_policy(WorkScope('local-user', 'alpha'),
            'experience', experience, 1, 0, [])
    rows = env.http.get('/api/v2/library/insights', params={'project_id': 'alpha'}).json()['items']
    shown = next(row for row in rows if row['id'] == old.id)
    assert shown['text'] == old.content and shown['state'] == 'pending'
    assert shown['hint']['scope_hint'] is None


def test_old_insight_hint_resolves_qualified_text_and_survives_explicit_confirmation(env):
    old, experience, _, _ = seed(env)
    scope = WorkScope('local-user', 'alpha')
    prior = env.service.propose(scope=scope, content='旧认识', source_experience_ids=[experience])
    published = env.service.publish(scope=scope, candidate_id=prior.id, expected_revision=1, reviewer='local-user')
    with env.records.begin() as tx:
        marker = tx.read('v2_candidate_hints', old.id)
        tx.put('v2_candidate_hints', old.id, {**marker.payload, 'relation': 'may_supersede',
            'target_id': published.id}, expected_revision=marker.revision)
        tx.commit()
    shown = env.http.get('/api/v2/library/drill', params={
        'project_id': 'alpha', 'from': 'insight', 'id': old.id}).json()['insight']
    assert shown['hint']['target'] == {'id': published.id, 'project_id': 'alpha',
        'text': '旧认识', 'conditions': [], 'revision': 1}
    confirmed = env.http.post(f'/api/v2/library/insights/{old.id}/confirm', json={
        'project_id': 'alpha', 'expected_revision': 1})
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()['hint']['target'] == shown['hint']['target']
    assert env.records.list('recognition_relations') == ()


def test_destination_confirmation_uses_existing_activity_and_usage_once(env):
    old, _, _, _ = seed(env)
    result = confirm_to(env, old)
    assert result.status_code == 200, result.text
    identity = result.json()['id']
    assert env.records.read('v2_usage_insight', identity) is not None
    events = [row for row in env.records.list('v2_activity')
        if row.payload.get('kind') == 'confirm' and row.payload.get('object_id') == identity]
    assert len(events) == 1
    usage = env.records.read('v2_usage_insight', identity)
    assert confirm_to(env, old).status_code == 409
    assert env.records.read('v2_usage_insight', identity) == usage
    assert [row for row in env.records.list('v2_activity')
        if row.payload.get('kind') == 'confirm' and row.payload.get('object_id') == identity] == events
