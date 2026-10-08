import pytest

from backend.memory_app.v2.policies import ACTIVE
from tests.memory_app.v2.test_workbench_ask import env, ask, publish
from tests.memory_app.v2.test_workbench_do import env as do_env
from tests.memory_app.v2.test_divided_do import _real_kernel_drafts
from types import SimpleNamespace
from copy import deepcopy
import json

from backend.recognition import RecognitionConflict, WorkScope
from backend.memory_app.source_egress import SourceEgressService
from backend.memory_app.v2.privacy import set_private_project
from backend.memory_app.v2.policies import override
from tests.memory_app.v2.test_inbox_filing import project, file


@pytest.fixture
def inspiration_policy(monkeypatch):
    monkeypatch.setitem(ACTIVE, 'scope', '@3')
    monkeypatch.setitem(ACTIVE, 'compose', '@4')


def capture(env, text='alpha beta gamma 用折纸讲解', project='alpha'):
    response = env.http.post('/api/v2/workbench/turns', json={
        'project_id': project, 'intent': 'inspiration', 'text': text})
    assert response.status_code == 200, response.text
    return response.json()['turn']['receipt']['inspiration']['insight']


def test_raw_inspiration_cross_project_citation_and_readonly_drill(env, inspiration_policy):
    publish(env)
    original = 'alpha beta gamma 用折纸讲解'
    candidate = capture(env, original)
    row = env.records.read('recognition_candidates', candidate['id'])
    experience = row.payload['source_experience_ids'][0]
    changed = env.http.patch('/api/v2/library/insights/' + candidate['id'], json={
        'project_id': 'inbox', 'expected_revision': row.revision,
        'text': 'mutable inference must never become original evidence', 'conditions': []})
    assert changed.status_code == 200, changed.text
    before = env.model.calls
    response = ask(env)
    assert response.status_code == 200, response.text
    receipt = response.json()['turn']['receipt']['ask']
    inspiration = [row for row in receipt['citations'] if row['layer'] == 'inspiration']
    assert len(inspiration) == 1
    cited = inspiration[0]
    assert set(cited) == {'n', 'layer', 'persona', 'id', 'title', 'quote', 'locator'}
    assert cited['id'] == experience and cited['quote'] == original and not cited['persona']
    assert cited['locator'] == {'coordinate_space': 'recognition_experience_content_v1',
                               'windows': [{'start': 0, 'end': len(original)}]}
    assert receipt['layers']['inspiration'] == 1
    assert receipt['trace'][-1]['layer'] == 'insight' and receipt['trace'][-1]['stopped']
    parts = {row['key']: row for row in receipt['context']['parts']}
    assert parts['inspiration']['count'] == 1
    assert '你的灵感' in env.model.messages[-1]['content']
    assert 'mutable inference' not in env.model.messages[-1]['content']
    drill = env.http.get('/api/v2/library/drill', params={
        'project_id': 'alpha', 'from': 'inspiration', 'id': experience})
    assert drill.status_code == 200, drill.text
    detail = drill.json()
    assert detail['readonly'] is True and detail['source_project_id'] == 'inbox'
    assert detail['inspiration']['id'] == experience
    assert detail['inspiration']['text'] == original
    assert env.model.calls == before + 1
    calls = env.model.calls
    saved = response.json()['turn']
    replay = env.http.get('/api/v2/workbench/threads/' + response.json()['thread_id'],
                          params={'project_id': 'alpha'})
    assert replay.status_code == 200, replay.text
    assert next(row for row in replay.json()['turns'] if row['id'] == saved['id'])['receipt']['ask'] == receipt
    assert env.model.calls == calls
    usage = env.records.read('v2_usage_inspiration', experience)
    assert usage.payload['events'][-1]['kind'] == 'citation'


def test_inspiration_reaches_real_main_steward_and_every_expert(do_env, inspiration_policy):
    client, model = do_env
    records = client.app.state.recognition_service.records
    original = '分别准备三部分方案并汇总：用折纸讲解'
    candidate = capture(SimpleNamespace(http=client), original, project='project-a')
    experience = records.read('recognition_candidates', candidate['id']).payload['source_experience_ids'][0]
    _real_kernel_drafts(do_env)
    assert len(model.calls) >= 5
    request = records.list('v2_task_executions')[0].payload['request']
    assert request['policy_versions']['scope'] == '@3'
    assert request['policy_versions']['compose'] == '@4'
    descriptor = {'type': 'experience', 'id': experience, 'revision': 1, 'project_id': 'inbox'}
    assert descriptor in request['privacy']['material_refs']
    frozen = records.read('v2_task_methods', request['turn_id']).payload
    assert len(frozen['inspirations']) == 1
    for messages in model.calls:
        assert any(message == {'role': 'system', 'content': frozen['text']} for message in messages)
        assert sum(message['content'].count(original) for message in messages) == 1
    turn = records.read('v2_turns', frozen['turn_id'])
    response = client.get('/api/v2/workbench/threads/' + turn.payload['thread_id'],
                          params={'project_id': 'project-a'})
    assert response.status_code == 200, response.text
    context = response.json()['turns'][0]['receipt']['do']['context']
    assert {'layer': 'inspiration', 'id': experience, 'title': '你的灵感'} in context['entries']
    assert next(row for row in context['parts'] if row['key'] == 'inspiration')['count'] == 1


@pytest.mark.parametrize('state', ['pending', 'confirmed', 'discarded', 'faded', 'forgotten', 'private', 'inbox_private'])
def test_original_state_and_privacy_are_applied_before_selection(env, inspiration_policy, state):
    candidate = capture(env)
    row = env.records.read('recognition_candidates', candidate['id'])
    experience = row.payload['source_experience_ids'][0]
    if state in {'confirmed', 'forgotten'}:
        response = env.http.post('/api/v2/library/insights/' + candidate['id'] + '/confirm',
            json={'project_id': 'inbox', 'expected_revision': row.revision})
        assert response.status_code == 200, response.text
        if state == 'forgotten':
            response = env.http.post('/api/v2/library/insights/' + response.json()['id'] + '/forget',
                                    json={'project_id': 'inbox', 'forgotten': True})
            assert response.status_code == 200, response.text
    elif state == 'discarded':
        response = env.http.post('/api/v2/library/insights/' + candidate['id'] + '/drop',
                                json={'project_id': 'inbox', 'expected_revision': row.revision})
        assert response.status_code == 200, response.text
    elif state == 'faded':
        with env.records.begin() as tx:
            tx.put('v2_candidate_fade', candidate['id'], {'project_id': 'inbox'}, expected_revision=0)
            tx.commit()
    elif state == 'private':
        authority = SourceEgressService(env.records)
        policy = authority.policy(WorkScope('local-user', 'inbox'), 'experience', experience)
        authority.set_policy(WorkScope('local-user', 'inbox'), 'experience', experience,
                             1, policy['policy_revision'], [])
    elif state == 'inbox_private':
        set_private_project(env.records, 'inbox', True, 0)
    before = env.model.calls
    plan = env.domains.query.prepare_ask('alpha', 'alpha beta gamma?')
    selected = [row['id'] for row in plan['chosen'] if row.get('inspiration')]
    assert selected == ([experience] if state in {'pending', 'confirmed'} else [])
    assert env.model.calls == before


def test_limits_budget_and_ordinary_stop_are_independent(env, inspiration_policy):
    publish(env)
    baseline = env.domains.query.prepare_ask('alpha', 'alpha beta gamma?')
    for number in range(6):
        capture(env, 'alpha beta gamma 折纸星球方案' + str(number))
    ordinary = env.domains.query.prepare_ask('alpha', 'alpha beta gamma?')
    ideas = env.domains.query.prepare_ask('alpha', 'alpha beta gamma 有什么点子？')
    assert len([row for row in ordinary['chosen'] if row.get('inspiration')]) == 2
    assert len([row for row in ideas['chosen'] if row.get('inspiration')]) == 5
    assert ordinary['trace'] == baseline['trace']
    with override(scope='@2', compose='@3'):
        legacy = env.domains.query.prepare_ask('alpha', 'alpha beta gamma 有什么点子？')
    assert ideas['trace'] == legacy['trace']
    assert [(row['id'], row['excerpt']) for row in ideas['chosen'] if not row.get('inspiration')] == [
        (row['id'], row['excerpt']) for row in legacy['chosen']]
    from backend.memory_app.v2.budget import evidence_tokens, input_tokens
    assert evidence_tokens(ideas['chosen']) <= int(ideas['budget'] * .8)
    assert input_tokens(ideas['chosen'], ideas['question']) + ideas['prompt_overhead'] <= ideas['budget']
    collected = env.domains.query.collect_candidates('alpha', 'alpha beta gamma?')
    from backend.memory_app.v2.ladder import plan_ladder
    tiny = plan_ladder(collected['candidates'], 'alpha beta gamma?', token_budget=80,
                       inspirations=collected['inspiration_candidates'])
    assert input_tokens([], 'alpha beta gamma?') > 80
    assert tiny['chosen'] == []


def test_filed_original_only_supplements_ideas_in_its_own_project(env, inspiration_policy):
    project(env, ['Writing'])
    candidate = capture(env)
    original = env.records.read('recognition_candidates', candidate['id']).payload['source_experience_ids'][0]
    filed = file(env, candidate['id'], scene='Writing')
    assert filed.status_code == 200, filed.text
    target = filed.json()
    copied = env.records.read('recognition_candidates', target['id']).payload['source_experience_ids'][0]
    assert copied != original
    assert not env.domains.query.prepare_ask('alpha', 'alpha beta gamma?', scene='Writing')['chosen']
    ideas = env.domains.query.prepare_ask('alpha', 'alpha beta gamma 有什么点子？', scene='Writing')
    assert [row['id'] for row in ideas['chosen']] == [copied]
    assert not env.domains.query.prepare_ask('alpha', 'alpha beta gamma 有什么点子？', scene='Other')['chosen']
    assert not env.domains.query.prepare_ask('other', 'alpha beta gamma 有什么点子？')['chosen']
    drill = env.http.get('/api/v2/library/drill', params={'project_id': 'alpha', 'from': 'inspiration', 'id': copied})
    assert drill.status_code == 200, drill.text
    assert drill.json()['inspiration']['text'] == 'alpha beta gamma 用折纸讲解'
    assert drill.json()['source_project_id'] == 'alpha'
    assert env.http.get('/api/v2/library/drill', params={
        'project_id': 'other', 'from': 'inspiration', 'id': copied}).status_code == 404
    confirmed = env.http.post('/api/v2/library/insights/' + target['id'] + '/confirm',
        json={'project_id': 'alpha', 'expected_revision': target['revision']})
    assert confirmed.status_code == 200, confirmed.text
    regular = env.domains.query.prepare_ask('alpha', 'alpha beta gamma?', scene='Writing')
    assert any(row['id'] == confirmed.json()['id'] for row in regular['chosen'])
    assert all(not row.get('inspiration') for row in regular['chosen'])


@pytest.mark.parametrize('damage', ['provenance', 'turn_mapping', 'experience_revision', 'candidate_mapping'])
def test_broken_original_proof_is_excluded_and_frozen_plan_is_rejected(env, inspiration_policy, damage):
    candidate = capture(env)
    row = env.records.read('recognition_candidates', candidate['id'])
    identity = row.payload['source_experience_ids'][0]
    plan = env.domains.query.prepare_ask('alpha', 'alpha beta gamma?')
    assert [row['id'] for row in plan['chosen']] == [identity]
    with env.records.begin() as tx:
        if damage in {'provenance', 'experience_revision'}:
            raw = tx.read('recognition_experiences', identity)
            body = dict(raw.payload)
            if damage == 'provenance':
                from backend.recognition.provenance import ExperienceProvenance
                body['provenance'] = ExperienceProvenance.legacy(recorded_at=body['created_at']).to_payload()
            changed = tx.put('recognition_experiences', identity, body, expected_revision=raw.revision)
            if damage == 'provenance':
                tx.put('recognition_candidates', row.object_id, {**row.payload,
                    'source_experience_revisions': {identity: changed.revision}}, expected_revision=row.revision)
        elif damage == 'turn_mapping':
            turn = next(row for row in tx.list('v2_turns') if row.payload['intent'] == 'inspiration')
            body = deepcopy(turn.payload)
            body['receipt']['inspiration']['insight']['id'] = 'unavailable-original'
            tx.put('v2_turns', turn.object_id, body, expected_revision=turn.revision)
        else:
            tx.put('recognition_candidates', row.object_id, {**row.payload,
                'source_experience_ids': ['unavailable-original']}, expected_revision=row.revision)
        tx.commit()
    assert not env.domains.query.prepare_ask('alpha', 'alpha beta gamma?')['chosen']
    with pytest.raises(RecognitionConflict):
        env.domains.query.validate_ask_plan(plan)


@pytest.mark.parametrize('timing', ['before', 'after'])
def test_private_change_at_real_ask_boundary_prevents_completed_answer(env, inspiration_policy, timing):
    publish(env)
    capture(env)
    setattr(env.model, timing, lambda: set_private_project(env.records, 'inbox', True, 0))
    before = env.model.calls
    response = ask(env)
    assert response.status_code == 409, response.text
    assert response.json()['detail'] == 'source_changed_retry'
    assert env.model.calls == before + int(timing == 'after')


def test_frozen_task_original_and_text_do_not_follow_active_version(env, inspiration_policy, monkeypatch):
    capture(env)
    from backend.memory_app.v2.method_context import prepare_methods, freeze_task_methods, frozen_task_methods
    from backend.memory_app.v2.inspirations import freeze_task_turn, validate_task_inputs
    plan = prepare_methods(env.domains.query, 'alpha', 'alpha beta gamma?', None)
    selected = [row for row in plan['chosen'] if row.get('inspiration')]
    arguments = dict(records=env.records, models=env.model, project_id='alpha',
        text='alpha beta gamma?', load_text=lambda row: '', inspirations=selected,
        materials=[{'type': 'experience', 'id': row['id'], 'revision': row['entry']['revision'],
                    'project_id': row['scope'].project_id} for row in selected],
        turn_id='turn-frozen-inspiration', session_id='session-frozen-inspiration',
        operation_id='op-frozen-inspiration', idempotency_key='frozen-inspiration',
        created_at='2026-10-06T00:00:00+00:00')
    first, second = freeze_task_turn(**arguments), freeze_task_turn(**arguments)
    encode = lambda row: json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    assert encode(first) == encode(second)
    freeze_task_methods(env.records, first, plan, 'turn-original')
    frozen = frozen_task_methods(env.records, env.domains.query, first)
    monkeypatch.setitem(ACTIVE, 'scope', '@2')
    monkeypatch.setitem(ACTIVE, 'compose', '@3')
    assert frozen_task_methods(env.records, env.domains.query, first) == frozen
    validate_task_inputs(env.records, env.model, first)
    assert env.model.calls == 0
