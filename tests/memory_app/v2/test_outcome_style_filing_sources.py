"""真实成果派生写法归到我时保留原件和出生来源。"""
from backend.recognition import WorkScope
from backend.memory_app.v2.insights import insight_view
from tests.memory_app.v2.test_outcome_style_product_sources import (
    do_env, scenario, style_candidate_from_continued_outcome,
)


def test_original_source_writing_files_to_me_with_real_origin_and_confirmation(scenario):
    values = scenario
    source = style_candidate_from_continued_outcome(values, 'original_source')
    candidate = source.candidate
    original = values.records.read('recognition_experiences', source.experience)
    assert values.client.get('/api/v2/projects').status_code == 200
    with values.records.begin() as tx:
        tx.put('v2_candidate_hints', candidate.id, {
            'project_id': 'project-a', 'relation': 'new', 'target_id': None, 'scope_hint': 'me',
        }, expected_revision=0)
        tx.commit()
    response = values.client.post(f'/api/v2/library/inbox/insight/{candidate.id}/file', json={
        'source_project_id': 'project-a', 'target_project_id': 'me',
        'expected_revision': candidate.revision, 'confirm': True,
    })
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['kind'] == 'recognition' and result['state'] == 'active'
    assert values.records.read('recognition_candidates', candidate.id).payload['state'] == 'rejected'
    recognition = values.records.read('recognitions', result['id'])
    assert recognition.payload['scope'] == {'user_id': 'local-user', 'project_id': 'me'}
    assert len(recognition.payload['source_experience_ids']) == 1
    copied = values.records.read('recognition_experiences', recognition.payload['source_experience_ids'][0])
    assert copied.payload['provenance'] == original.payload['provenance']
    assert copied.payload['content'] == original.payload['content']
    origin = values.records.read('v2_experience_origins', copied.object_id)
    assert origin.payload['source_experience_id'] == source.experience
    assert origin.payload['source_project_id'] == 'project-a'
    assert origin.payload['source_revision'] == original.revision
    assert values.records.read('recognition_experiences', source.experience) == original
    assert source.service.get_recognition(scope=WorkScope('local-user', 'me'), recognition_id=result['id']).authorized
    assert result['document_ids'] == []
    assert all(item['project_id'] == 'project-a' for item in result['source_documents'])
    assert result['source_documents']


def test_typed_writing_hint_keeps_readonly_store_and_transaction_qualification(scenario):
    values = scenario
    source = style_candidate_from_continued_outcome(values, 'original_source')
    published = source.service.publish(scope=source.scope, candidate_id=source.candidate.id,
        expected_revision=source.candidate.revision, reviewer='local-user')
    candidate = source.service.propose(scope=source.scope, content='延续原件写法。',
        source_experience_ids=[source.experience])
    with values.records.begin() as tx:
        tx.put('v2_candidate_hints', candidate.id, {
            'project_id': 'project-a', 'relation': 'may_supersede',
            'target_id': published.id, 'scope_hint': None,
        }, expected_revision=0)
        tx.commit()
    before = values.records.list_all()
    shown = insight_view(values.records, source.scope, candidate.id, service=source.service)
    assert shown['hint']['target'] == {'id': published.id, 'project_id': 'project-a',
        'text': published.content, 'conditions': list(published.conditions), 'revision': published.revision}
    with values.records.begin() as tx:
        transactional = insight_view(tx, source.scope, candidate.id)
        assert transactional['hint']['target'] == shown['hint']['target']
    response = values.client.get('/api/v2/library/drill', params={
        'project_id': 'project-a', 'from': 'insight', 'id': candidate.id})
    assert response.status_code == 200, response.text
    assert response.json()['insight']['hint']['target'] == shown['hint']['target']
    assert values.records.list_all() == before
