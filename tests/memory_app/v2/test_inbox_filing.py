import pytest
from tests.memory_app.v2.test_workbench_ask import env, publish
from backend.recognition import WorkScope
from backend.memory_app.v2.projects import assign_scene


def pending(env, text='alpha useful', project='inbox'):
    scope = WorkScope('local-user', project)
    experience = env.service.stage_experience(scope=scope, content=text,
        provenance={'kind': 'user_statement', 'actor': 'local-user'})
    return env.service.propose(scope=scope, content=text, source_experience_ids=[experience])


def project(env, scenes=()):
    env.http.get('/api/v2/projects')
    with env.records.begin() as tx:
        row = tx.read('v2_projects', 'alpha')
        tx.put('v2_projects', 'alpha', {'name': 'alpha', 'scenes': list(scenes), 'builtin': None},
            expected_revision=row.revision if row else 0)
        tx.commit()


def file(env, identity, revision=1, scene=None):
    return env.http.post(f'/api/v2/library/inbox/insight/{identity}/file',
        json={'target_project_id': 'alpha', 'scene': scene, 'expected_revision': revision})


def test_pending_filing_rejects_original_and_requires_confirmation_before_recall(env):
    project(env, ['Writing']); old = pending(env)
    response = file(env, old.id, scene='Writing')
    assert response.status_code == 200, response.text
    new = response.json()
    assert new['state'] == 'pending' and new['scene'] == 'Writing'
    assert env.records.read('recognition_candidates', old.id).payload['state'] == 'rejected'
    assert env.domains.query.prepare_ask('alpha', 'alpha useful?')['chosen'] == []
    confirmed = env.http.post(f"/api/v2/library/insights/{new['id']}/confirm",
        json={'project_id': 'alpha', 'expected_revision': new['revision']})
    assert confirmed.status_code == 200, confirmed.text
    assert any(row['id'] == confirmed.json()['id'] for row in env.domains.query.prepare_ask('alpha', 'alpha useful?')['chosen'])


def test_stale_revision_never_creates_target_records(env):
    project(env); old = pending(env)
    before = env.records.list_all()
    assert file(env, old.id, revision=2).status_code == 409
    assert env.records.list_all() == before


@pytest.mark.parametrize('active', [False, True])
def test_repeated_filing_does_not_create_another_candidate(env, active):
    project(env); old = publish(env, project='inbox')[0] if active else pending(env)
    assert file(env, old.id).status_code == 200
    before = env.records.list_all()
    assert file(env, old.id).status_code == 409
    assert env.records.list_all() == before
    if active:
        assert env.records.read('recognitions', old.id).revision == 1
        assert env.records.read('recognition_recall_preferences', old.id).payload['state'] == 'forgotten'


def test_only_inbox_insights_and_real_target_scene_are_accepted(env):
    project(env, ['Writing']); old = pending(env, project='alpha')
    assert file(env, old.id).status_code == 400
    old = pending(env); before = env.records.list_all()
    assert file(env, old.id, scene='Unknown').status_code == 400
    assert env.records.list_all() == before


def test_filing_rolls_back_created_records_if_original_update_fails(env, monkeypatch):
    from backend.recognition import RecognitionService, RecognitionConflict
    project(env); old = pending(env); before = env.records.list_all()
    def fail(*args, **kwargs):
        raise RecognitionConflict('synthetic conflict')
    monkeypatch.setattr(RecognitionService, 'reject_candidate', fail)
    assert file(env, old.id).status_code == 409
    assert env.records.list_all() == before


def test_suggestions_use_scene_name_and_assigned_insights_without_model(env):
    project(env, ['Writing', 'Travel'])
    existing, _ = publish(env, text='alpha useful')
    assign_scene(env.records, 'recognition', existing.id, 'alpha', 'Writing')
    a, b = pending(env), pending(env, text='Travel')
    response = env.http.get('/api/v2/library/inbox/suggestions', params={'target_project_id': 'alpha'})
    assert response.status_code == 200, response.text
    suggestions = {row['id']: row['scene'] for row in response.json()['items']}
    assert suggestions[a.id] == 'Writing' and suggestions[b.id] == 'Travel'
    assert env.model.calls == 0


def test_tied_or_zero_overlap_suggestions_are_null(env):
    project(env, ['alpha one', 'alpha two']); tied, zero = pending(env, 'alpha'), pending(env, 'unmatched')
    response = env.http.get('/api/v2/library/inbox/suggestions', params={'target_project_id': 'alpha'})
    assert response.status_code == 200, response.text
    suggestions = {row['id']: row['scene'] for row in response.json()['items']}
    assert suggestions[tied.id] is None and suggestions[zero.id] is None


@pytest.mark.parametrize('step', ['stage_experience', 'propose', 'scene', 'preference'])
def test_failure_after_each_domain_write_rolls_back_every_collection(env, monkeypatch, step):
    import backend.memory_app.v2.inbox as inbox
    from backend.recognition import RecognitionService, RecognitionConflict
    project(env, ['Writing'])
    old = publish(env, project='inbox')[0] if step == 'preference' else pending(env)
    before = env.records.list_all()
    owner, name = (inbox, 'assign_scene') if step == 'scene' else (inbox, 'set_preference') if step == 'preference' else (RecognitionService, step)
    original = getattr(owner, name)
    def fail_after(*args, **kwargs):
        original(*args, **kwargs)
        raise RecognitionConflict('synthetic conflict after domain write')
    monkeypatch.setattr(owner, name, fail_after)
    assert file(env, old.id, scene='Writing').status_code == 409
    assert env.records.list_all() == before


def test_active_alias_and_restore_cannot_replay_the_same_filing_revision(env):
    project(env); old, _ = publish(env, project='inbox')
    alias = next(row.object_id for row in env.records.list('recognition_candidates')
                 if row.payload.get('recognition_id') == old.id)
    assert file(env, alias).status_code == 200
    restored = env.http.post(f'/api/v2/library/insights/{old.id}/forget',
        json={'project_id': 'inbox', 'forgotten': False})
    assert restored.status_code == 200
    before = env.records.list_all()
    assert file(env, old.id).status_code == 409
    assert file(env, alias).status_code == 409
    assert env.records.list_all() == before


def test_simultaneous_requests_create_only_one_target_candidate(env):
    from concurrent.futures import ThreadPoolExecutor
    project(env); old = pending(env)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: file(env, old.id), range(2)))
    assert sorted(response.status_code for response in responses) == [200, 409]
    assert len([row for row in env.records.list('recognition_candidates')
                if row.payload['project_id'] == 'alpha']) == 1
