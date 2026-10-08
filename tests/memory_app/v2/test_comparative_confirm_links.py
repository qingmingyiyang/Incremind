import json

import pytest

from backend.memory_app.v2.insight_generation import generate_insights
from backend.memory_app.v2.policies import override
from backend.recognition import WorkScope
from tests.memory_app.v2.test_workbench_ask import env, add_document


@pytest.mark.parametrize('relation,kind', [('supplement', 'supports'), ('differs', 'refutes'),
    ('may_supersede', 'supersedes')])
def test_candidate_confirmation_reuses_pending_relation_and_human_review(env, relation, kind):
    document, _ = add_document(env, summary='新方法证据')
    scope = WorkScope('local-user', 'alpha')
    old_source = env.service.stage_experience(scope=scope, content='新方法旧证据')
    candidate = env.service.propose(scope=scope, content='新方法旧认识', source_experience_ids=[old_source])
    old = env.service.publish(scope=scope, candidate_id=candidate.id, expected_revision=1, reviewer='local-user')
    # The transport is the external boundary; extraction, publication and review remain real.
    from tests.memory_app.v2.test_insight_generation import Model
    model = Model()
    model.intake = False
    model.response = json.dumps({'insights': [{'kind': 'supplement' if relation == 'supplement' else 'differs',
        'relation': relation, 'text': '新方法新认识', 'conditions': [],
        'target_id': old.id, 'scope_hint': None}], 'supports': []})
    with override(extract='@2'):
        rows = generate_insights(model, env.service, env.documents, 'alpha', document)
    assert len(rows) == 1
    assert env.records.list('recognition_relation_proposals') == ()
    published = env.http.post(f"/api/v2/library/insights/{rows[0]['id']}/confirm", json={
        'project_id': 'alpha', 'expected_revision': 1})
    assert published.status_code == 200, published.text
    matching = [row for row in env.records.list('recognition_relation_proposals')
        if row.payload['from_id'] == published.json()['id'] and row.payload['to_id'] == old.id
        and row.payload['relation'] == kind]
    assert len(matching) == 1 and matching[0].payload['state'] == 'pending'
    assert env.records.list('recognition_relations') == ()
    assert env.records.read('v2_insight_interference', old.id) is None
    result = env.http.post(f'/api/v2/library/link-suggestions/{matching[0].object_id}/accept', json={
        'project_id': 'alpha', 'expected_revision': 1})
    assert result.status_code == 200, result.text
    assert len(env.records.list('recognition_relations')) == 1
    if kind == 'supersedes':
        assert env.records.read('v2_insight_interference', old.id).payload['superseding_id'] == published.json()['id']
