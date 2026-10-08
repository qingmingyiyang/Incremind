from types import SimpleNamespace

import pytest

from backend.recognition import RecognitionService, RecognitionError, WorkScope
from core.storage_provider import SQLiteStructuredRecordStore
from backend.memory_app.v2.privacy import set_private_project
from backend.memory_app.recall_preferences import set_preference


@pytest.fixture
def profile_env(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    service = RecognitionService(records)
    return SimpleNamespace(records=records, service=service)


def publish(env, content):
    scope = WorkScope('local-user', 'me')
    source = env.service.stage_experience(scope=scope, content='Synthetic profile evidence')
    candidate = env.service.propose(scope=scope, content=content, source_experience_ids=[source])
    return env.service.publish(scope=scope, candidate_id=candidate.id, expected_revision=1, reviewer='local-user')


def test_profile_is_pinned_without_query_matches_and_keeps_stable_bytes(profile_env):
    from backend.memory_app.v2.profile import confirmed_profile
    env = profile_env
    one = publish(env, 'I prefer concise answers.')
    two = publish(env, 'I work as an engineer.')
    before = confirmed_profile(env.records, env.service)
    with env.records.begin() as tx:
        tx.put('v2_usage_insight', two.id, {'project_id':'me', 'score':20, 'count':4,
            'updated_at':'2026-10-03T00:00:00+00:00'}, expected_revision=0)
        tx.commit()
    after = confirmed_profile(env.records, env.service)
    assert before['text'].encode() == after['text'].encode()
    assert before['count'] == 2
    assert before['tokens'] <= 600
    assert env.records.read('recognitions', one.id).revision == 1


def test_unrelated_privacy_epoch_keeps_prefix_bytes_and_refreshes_authority(profile_env):
    from backend.memory_app.v2.profile import confirmed_profile, validate_profile
    from backend.shared.memory_sidecars import utc_now
    env = profile_env
    publish(env, 'First stable preference')
    publish(env, 'Second stable preference')
    before = confirmed_profile(env.records, env.service)
    stronger = before['items'][-1]['id']
    with env.records.begin() as tx:
        tx.put('v2_usage_insight', stronger, {'project_id':'me', 'score':20, 'count':4,
            'updated_at':utc_now().isoformat()}, expected_revision=0)
        tx.commit()
    set_private_project(env.records, 'unrelated', True, 0)
    after = confirmed_profile(env.records, env.service)
    assert after['text'].encode() == before['text'].encode()
    assert after['basis']['privacy_revision'] != before['basis']['privacy_revision']
    assert after['items'][0]['snapshot'] != before['items'][0]['snapshot']
    from backend.memory_app.v2.profile import COLLECTION
    assert len(env.records.list(COLLECTION)) == 1
    validate_profile(env.records, env.service, after)
    with pytest.raises(RecognitionError):
        validate_profile(env.records, env.service, before)


def test_profile_private_and_forgotten_are_revalidated(profile_env):
    from backend.memory_app.v2.profile import confirmed_profile, validate_profile
    env = profile_env
    item = publish(env, 'I prefer detailed explanations.')
    before = confirmed_profile(env.records, env.service)
    set_private_project(env.records, 'me', True, 0)
    assert confirmed_profile(env.records, env.service)['text'] == ''
    with pytest.raises(RecognitionError):
        validate_profile(env.records, env.service, before)


def test_empty_profile_has_no_header(profile_env):
    from backend.memory_app.v2.profile import confirmed_profile
    result = confirmed_profile(profile_env.records, profile_env.service)
    assert result['text'] == '' and result['count'] == 0 and result['tokens'] == 0


def test_profile_omits_source_bookkeeping_timestamps_but_preserves_conditions(profile_env):
    from backend.memory_app.v2.profile import confirmed_profile
    env = profile_env
    scope = WorkScope('local-user','me')
    source = env.service.stage_experience(scope=scope, content='Synthetic user statement')
    proposal = env.service.propose(scope=scope, content='I use concise replies',
        conditions=['Only for routine questions'], source_experience_ids=[source])
    env.service.publish(scope=scope, candidate_id=proposal.id, expected_revision=1, reviewer='local-user')
    text = confirmed_profile(env.records, env.service)['text']
    assert 'Only for routine questions' in text
    assert '"epistemic_status":"unknown"' in text
    assert 'recorded_at' not in text and 'occurred_at' not in text
    from backend.memory_app.context_adapter import format_recognition_content, ContextSelectionError
    entry = env.service.retrieval_entries(scope=scope)[0]
    ordinary = format_recognition_content(entry)
    assert 'recorded_at' in ordinary and 'occurred_at' in ordinary
    assert 'I use concise replies\n\n适用条件：\n- Only for routine questions' in text
    with pytest.raises(ContextSelectionError):
        format_recognition_content({**entry, 'source_evidence_complete':False}, profile=True)


def test_forgetting_and_revision_invalidate_frozen_prefix(profile_env):
    from backend.memory_app.v2.profile import confirmed_profile, validate_profile
    env = profile_env
    item = publish(env, 'A confirmed preference')
    frozen = confirmed_profile(env.records, env.service)
    set_preference(env.records, WorkScope('local-user','me'), item.id,
        recognition_revision=1, preference_revision=0, state='forgotten')
    assert confirmed_profile(env.records, env.service)['count'] == 0
    with pytest.raises(RecognitionError):
        validate_profile(env.records, env.service, frozen)


def test_source_private_and_revision_changes_invalidate_cached_profile(profile_env):
    from backend.memory_app.v2.profile import confirmed_profile, validate_profile
    from backend.memory_app.source_egress import SourceEgressService
    env = profile_env
    item = publish(env, 'A preference with a private source')
    before = confirmed_profile(env.records, env.service)
    SourceEgressService(env.records).set_policy(WorkScope('local-user','me'),
        'recognition', item.id, 1, 0, [])
    assert confirmed_profile(env.records, env.service)['count'] == 0
    with pytest.raises(RecognitionError):
        validate_profile(env.records, env.service, before)


def test_first_order_uses_actual_strength_and_revisions_start_a_new_projection(profile_env):
    from backend.memory_app.v2.profile import confirmed_profile
    env = profile_env
    first = publish(env, 'First preference')
    strong = publish(env, 'Strong preference')
    with env.records.begin() as tx:
        tx.put('v2_usage_insight', strong.id, {'project_id':'me','score':4,'count':2,
            'updated_at':'2026-10-03T00:00:00+00:00'}, expected_revision=0)
        tx.commit()
    before = confirmed_profile(env.records, env.service)
    assert before['text'].index('Strong preference') < before['text'].index('First preference')
    assert before['items'][0]['id'] == strong.id
    env.service.revise(scope=WorkScope('local-user','me'), recognition_id=first.id,
                       expected_revision=1, content='Revised preference')
    after = confirmed_profile(env.records, env.service)
    assert 'Revised preference' in after['text'] and 'First preference' not in after['text']
    assert before['basis'] != after['basis']


def test_budget_and_concurrent_first_construction(profile_env):
    from concurrent.futures import ThreadPoolExecutor
    from backend.memory_app.v2.profile import confirmed_profile, COLLECTION
    env = profile_env
    for index in range(16):
        publish(env, f'Preference {index}: ' + 'detailed synthetic sentence. ' * 8)
    with ThreadPoolExecutor(max_workers=3) as executor:
        results = list(executor.map(lambda _:confirmed_profile(env.records, env.service), range(3)))
    assert all(result['text'].encode() == results[0]['text'].encode() for result in results)
    assert 0 < results[0]['count'] < 16
    assert results[0]['tokens'] <= 600
    assert len(env.records.list(COLLECTION)) == 1


def test_task_profile_requires_frozen_authority_and_current_eligibility(profile_env):
    from copy import deepcopy
    from backend.memory_app.v2.profile import frozen_task_profile
    from backend.memory_app.v2.task_do import TaskDo
    from tests.memory_app.v2.test_turn_requests import Models
    env = profile_env
    item = publish(env, 'Preference shared with every authorized expert')
    task = TaskDo(env.records, Models(), None, None, None, None)
    _, state = task.initial('turn-parent', 'alpha', 'A synthetic task', None)
    request = state['request']
    assert frozen_task_profile(env.records, env.service, request)['count'] == 1
    forged = deepcopy(request)
    forged['privacy']['source_snapshots'] = []
    with pytest.raises(RecognitionError):
        frozen_task_profile(env.records, env.service, forged)
    set_preference(env.records, WorkScope('local-user','me'), item.id,
        recognition_revision=1, preference_revision=0, state='forgotten')
    with pytest.raises(RecognitionError):
        frozen_task_profile(env.records, env.service, request)


from tests.memory_app.v2.test_workbench_ask import env as ask_env, ask


def test_profile_only_question_reaches_real_answer_turn_without_fake_citations(ask_env):
    publish(ask_env, 'I prefer concise answers in Chinese.')
    response = ask(ask_env, text='我的回答风格是什么？', intent='ask')
    assert response.status_code == 200, response.text
    receipt = response.json()['turn']['receipt']['ask']
    assert not receipt['no_match'] and receipt['citations'] == []
    # Insufficient ordinary recall still runs its existing rewrite attempt.
    assert ask_env.model.calls == 2
    assert ask_env.model.messages[0]['content'].startswith('已确认的画像')
    assert 'I prefer concise' in ask_env.model.messages[0]['content']
    assert receipt['layers']['persona'] == 1
    assert receipt['layers']['insight'] == 0
    parts = {item['key']:item for item in receipt['context']['parts']}
    assert parts['persona']['count'] == 1 and parts['persona']['tokens'] > 0


def test_profile_does_not_enter_ladder_or_duplicate_in_prompt(ask_env):
    from tests.memory_app.v2.test_workbench_ask import publish as publish_any
    publish(ask_env, 'Unrelated confirmed personal background')
    publish_any(ask_env, 'alpha beta gamma')
    response = ask(ask_env)
    assert response.status_code == 200, response.text
    receipt = response.json()['turn']['receipt']['ask']
    assert len(receipt['citations']) == 1 and not receipt['citations'][0]['persona']
    assert sum(message['content'].count('Unrelated confirmed personal background') for message in ask_env.model.messages) == 1
    prefix = ask_env.model.messages[0]['content'].encode()
    second = ask(ask_env, text='alpha beta?')
    assert second.status_code == 200, second.text
    assert ask_env.model.messages[0]['content'].encode() == prefix


def test_permission_revoked_after_assembly_prevents_profile_wire_send(ask_env):
    from tests.memory_app.v2.test_workbench_ask import publish as publish_any
    publish(ask_env, 'Private personal preference')
    publish_any(ask_env, 'alpha beta gamma')
    ask_env.model.before = lambda:set_private_project(ask_env.records,'me',True,0)
    response = ask(ask_env)
    assert response.status_code == 409 and response.json()['detail'] == 'source_changed_retry'
    assert ask_env.model.calls == 0


from tests.memory_app.v2.test_workbench_do import env as do_env


def test_real_main_steward_and_experts_all_send_the_frozen_profile(do_env):
    import json
    from tests.memory_app.v2.test_divided_do import _real_kernel_drafts
    client, model = do_env
    env = SimpleNamespace(records=client.app.state.recognition_service.records,
                          service=client.app.state.recognition_service)
    item = publish(env, 'I prefer explicit verifiable conclusions.')
    _real_kernel_drafts(do_env)
    assert len(model.calls) >= 5
    prefixes = [messages[0]['content'] for messages in model.calls]
    assert all(prefix.startswith('已确认的画像') for prefix in prefixes)
    assert len(set(prefixes)) == 1
    contexts = [json.loads(messages[-1]['content']) for messages in model.calls]
    assert any('output' in context for context in contexts)
    assert any(any(capability['capability_id'] == 'agent.list' for capability in context.get('capabilities', [])) for context in contexts)
    assert any(any(capability['capability_id'] == 'document.draft.propose' for capability in context.get('capabilities', [])) for context in contexts)
    assert env.records.read('recognitions', item.id).revision == 1
    from backend.memory_app.v2.do_context import kernel_task_context
    executions = env.records.list('v2_task_executions')
    context = kernel_task_context(env.records, 'project-a', executions[0].payload['request']['turn_id'])['context']
    parts = {part['key']:part for part in context['parts']}
    assert parts['persona']['count'] == 1 and parts['persona']['tokens'] > 0
    assert parts['source']['count'] == 0
