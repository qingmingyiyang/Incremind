from datetime import timedelta

from backend.memory_app.v2.candidate_fade import CandidateFade
from backend.memory_app.v2.insight_generation import generate_insights
from backend.recognition import WorkScope
from tests.memory_app.v2.test_workbench_ask import env, add_document
from tests.memory_app.v2.test_insight_generation import Model
from tests.memory_app.v2.test_auto_forget import START, age


def candidates(env, project='alpha'):
    doc, _ = add_document(env, project=project)
    model = Model()
    model.intake = False
    rows = generate_insights(model, env.service, env.documents, project, doc)
    assert len(rows) == 2
    for row in rows:
        age(env, 'recognition_candidates', row['id'])
    return rows


def fade(env, days):
    return CandidateFade(env.records, now=lambda: START + timedelta(days=days)).run()


def test_thirty_day_boundary_idempotence_and_originals_unchanged(env):
    rows = candidates(env)
    before = env.records.list('recognition_candidates')
    activity_before = env.records.list('v2_activity')
    assert fade(env, 30 - 1 / 86400) == 0
    assert env.records.list('v2_activity') == activity_before
    assert fade(env, 30) == 2
    markers = env.records.list('v2_candidate_fade')
    assert {row.object_id for row in markers} == {row['id'] for row in rows}
    assert all(row.payload == {'faded_at': (START + timedelta(days=30)).isoformat()} for row in markers)
    activity = env.records.list('v2_activity')
    previous_ids = {row.object_id for row in activity_before}
    added = [row for row in activity if row.object_id not in previous_ids]
    assert len(activity) == len(activity_before) + 2
    assert len(added) == 2 and all(row.payload['by'] == 'auto' for row in added)
    assert all(row.payload['kind'] == 'fade' and row.payload['project_id'] == 'alpha' for row in added)
    assert {row.payload['object_id'] for row in added} == {row['id'] for row in rows}
    assert all(row in activity for row in activity_before)
    assert fade(env, 31) == 0
    assert env.records.list('v2_activity') == activity
    assert env.records.list('recognition_candidates') == before


def test_handwritten_persona_and_confirmed_candidates_are_preserved(env):
    rows = candidates(env)
    persona = candidates(env, 'me')
    scope = WorkScope('local-user', 'alpha')
    experience = env.service.stage_experience(scope=scope, content='手写灵感',
        provenance={'kind': 'user_statement', 'actor': 'local-user'})
    manual = env.service.propose(scope=scope, content='保留灵感', source_experience_ids=[experience])
    age(env, 'recognition_candidates', manual.id)
    saved = env.records.read('recognition_candidates', rows[0]['id'])
    env.service.publish(scope=scope, candidate_id=saved.object_id, expected_revision=saved.revision, reviewer='local-user')
    assert fade(env, 60) == 1
    assert all(env.records.read('v2_candidate_fade', identity) is None
        for identity in [manual.id, rows[0]['id'], *[r['id'] for r in persona]])


def test_pattern_source_is_protected_but_foreign_pattern_cannot_protect(env):
    rows = candidates(env)
    pattern = env.service.propose(scope=WorkScope('local-user', 'alpha'), content='规律',
        source_experience_ids=env.records.read('recognition_candidates', rows[0]['id']).payload['source_experience_ids'])
    foreign_scope = WorkScope('local-user', 'beta')
    experience = env.service.stage_experience(scope=foreign_scope, content='其他项目材料')
    foreign = env.service.propose(scope=foreign_scope, content='其他项目规律', source_experience_ids=[experience])
    with env.records.begin() as tx:
        tx.put('v2_insight_patterns', pattern.id, {'source_candidate_ids': [rows[0]['id']]}, expected_revision=0)
        tx.put('v2_insight_patterns', foreign.id, {'source_candidate_ids': [rows[1]['id']]}, expected_revision=0)
        tx.commit()
    assert fade(env, 30) == 1
    assert env.records.read('v2_candidate_fade', rows[0]['id']) is None
    assert env.records.read('v2_candidate_fade', rows[1]['id']) is not None


def test_list_pick_scope_and_field_guards_without_changing_candidate(env):
    rows = candidates(env)
    fade(env, 30)
    identity = rows[0]['id']
    before = env.records.read('recognition_candidates', identity)
    def listing(state):
        return env.http.get('/api/v2/library/insights', params={'project_id': 'alpha', 'state': state}).json()['items']
    assert listing('pending') == []
    assert {row['id'] for row in listing('forgotten')} == {row['id'] for row in rows}
    body = {'project_id': 'alpha', 'forgotten': False}
    url = f'/api/v2/library/insights/{identity}/forget'
    assert env.http.post(url, json={**body, 'project_id': 'beta'}).status_code == 404
    assert env.http.post(url, json={**body, 'forgotten': True}).status_code == 409
    assert env.http.post(url, json={**body, 'forgotten': 'false'}).status_code == 400
    response = env.http.post(url, json=body)
    assert response.status_code == 200 and response.json()['state'] == 'pending'
    assert [row['id'] for row in listing('pending')] == [identity]
    assert env.records.read('v2_candidate_fade', identity) is None
    assert env.records.read('recognition_candidates', identity) == before
    assert env.http.post(url, json=body).status_code == 200


def test_daily_registry_contains_fade_job(env):
    assert 'candidate_fade' in env.http.app.state.memory_daily_jobs.jobs


def test_workbench_hides_faded_chips_but_keeps_turn_and_pick_restores(env):
    rows = candidates(env)
    doc = rows[0]['document_ids'][0]
    item = next(row for row in env.records.list('workspace_items') if row.payload.get('document_id') == doc)
    receipt = {'remember': {'item_id': item.object_id, 'title': '临时整理稿', 'state': 'done',
        'progress': {'done': 4, 'total': 4}, 'document_id': doc, 'verified': False,
        'insights': rows, 'related': [], 'error': None}}
    with env.records.begin() as tx:
        tx.put('v2_threads', 'thread-fade-test', {'project_id': 'alpha', 'title': '临时对话',
            'created_at': START.isoformat(), 'updated_at': START.isoformat()}, expected_revision=0)
        before = tx.put('v2_turns', 'turn-fade-test', {'project_id': 'alpha', 'thread_id': 'thread-fade-test',
            'intent': 'remember', 'user_text': '临时原文', 'created_at': START.isoformat(),
            'receipt': receipt}, expected_revision=0)
        tx.commit()
    assert fade(env, 30) == 2
    url = '/api/v2/workbench/threads/thread-fade-test?project_id=alpha'
    hidden = env.http.get(url).json()['turns'][0]
    assert hidden['receipt']['remember']['insights'] == []
    assert hidden['user_text'] == '临时原文'
    assert env.records.read('v2_turns', before.object_id) == before
    jobs = env.http.get('/api/v2/jobs', params={'project_id': 'alpha'}).json()
    assert all(row.get('pending_count', 0) == 0 for row in jobs['items'])
    candidate = env.records.read('recognition_candidates', rows[0]['id'])
    response = env.http.post(f"/api/v2/library/insights/{candidate.object_id}/forget",
        json={'project_id': 'alpha', 'forgotten': False})
    assert response.status_code == 200
    restored = env.http.get(url).json()['turns'][0]['receipt']['remember']['insights']
    assert [row['id'] for row in restored] == [candidate.object_id]
