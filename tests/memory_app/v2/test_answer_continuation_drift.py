"""Fresh domain guards still gate explicit continuation of frozen text."""
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from backend.recognition import WorkScope
from tests.memory_app.v2.test_partial_answer import env, interrupted_app
from tests.memory_app.v2.test_workbench_ask import publish, add_document
from tests.memory_app.v2.test_workbench_stream import events


def partial(http, *, question='alpha?', thread=None):
    body = {'project_id': 'alpha', 'text': question}
    if thread:
        body['thread_id'] = thread
    response = http.post('/api/v2/workbench/turns', json=body,
        headers={'Accept': 'text/event-stream', 'Idempotency-Key': 'drift-partial'})
    result = events(response)[-1]
    assert result[0] == 'done', response.text
    assert result[1]['turn']['receipt']['ask']['interruption'] == 'connection'
    return result[1]['turn']['id']


def rejected(http, app, state, identity, calls, closed):
    before = tuple(app.state.ai_turn_store.events_after(identity))
    row = state.records.read('v2_answer_continuations', identity)
    usages = {name: state.records.list(name) for name in ('v2_usage_insight', 'v2_usage_document')}
    wire_count = len(calls)
    response = http.post(f'/api/v2/workbench/turns/{identity}/continue',
        json={'project_id': 'alpha'}, headers={'Idempotency-Key': 'drift-continue'})
    assert response.status_code == 409, response.text
    assert tuple(app.state.ai_turn_store.events_after(identity)) == before
    assert state.records.read('v2_answer_continuations', identity) == row
    assert app.state.ai_turn_store.get_action('drift-continue') is None
    assert app.state.ai_turn_store.get_immutable_payload(identity, 'product-answer-result-v2') is None
    assert {name: state.records.list(name) for name in usages} == usages
    assert len(calls) == wire_count
    assert sum(call.get('stream') is True for call in calls) == 1 and closed == [True]


def frozen_plan(app, state, identity):
    from backend.memory_app.kernel.answer_continuations import decode
    row = state.records.read('v2_answer_continuations', identity)
    return decode(app.state.ai_turn_store.get(row.payload['plan_ref'])['capsule'])['plan']


@pytest.mark.parametrize('change', ['profile_revoke', 'profile_forget'])
def test_profile_drift_rejects_continuation_without_action_or_wire(env, change):
    profile, _ = publish(env, text='I prefer concise synthetic answers.', project='me')
    app, _, _, calls, closed, _ = interrupted_app(env, continuation=True)
    with TestClient(app) as http:
        identity = partial(http)
        plan = frozen_plan(app, env, identity)
        assert [item['id'] for item in plan['profile']['items']] == [profile.id]
        assert any(profile.content in message['content'] for message in calls[0]['messages'])
        if change == 'profile_revoke':
            env.service.revoke(scope=WorkScope('local-user', 'me'), recognition_id=profile.id,
                expected_revision=profile.revision, reason='synthetic user revocation')
        else:
            from backend.memory_app.recall_preferences import set_preference
            set_preference(env.records, WorkScope('local-user', 'me'), profile.id,
                recognition_revision=profile.revision, preference_revision=0, state='forgotten')
        rejected(http, app, env, identity, calls, closed)


def test_bookshelf_preference_drift_rejects_continuation_without_relearning(env):
    from tests.memory_app.v2.test_bookshelf import forgotten
    from backend.memory_app.recall_preferences import set_preference
    insight, _ = publish(env)
    forgotten(env, insight)
    app, _, _, calls, closed, _ = interrupted_app(env, continuation=True)
    with TestClient(app) as http:
        identity = partial(http)
        plan = frozen_plan(app, env, identity)
        from backend.memory_app.kernel.answer_continuations import decode
        row = env.records.read('v2_answer_continuations', identity)
        guards = decode(app.state.ai_turn_store.get(row.payload['plan_ref'])['capsule'])['guards']
        assert guards['bookshelf_guard']['spines']
        assert any(spine['id'] == insight.id for spine in guards['bookshelf_guard']['spines'])
        preference = env.records.read('recognition_recall_preferences', insight.id)
        set_preference(env.records, WorkScope('local-user', 'alpha'), insight.id,
            recognition_revision=insight.revision, preference_revision=preference.revision, state='forgotten')
        rejected(http, app, env, identity, calls, closed)
        assert env.records.read('recognition_recall_preferences', insight.id).payload['by'] == 'user'


def test_temporal_validity_drift_rejects_frozen_historical_answer(env):
    from backend.memory_app.v2.links import InsightLinks
    with patch('backend.recognition.service._now', return_value='2026-03-12T00:00:00+00:00'):
        old, _ = publish(env, text='alpha 三月在北厅办展')
    app, _, _, calls, closed, _ = interrupted_app(env, continuation=True, history_reply=True)
    with TestClient(app) as http:
        identity = partial(http, question='2026年3月 alpha 展览在哪？')
        plan = frozen_plan(app, env, identity)
        assert plan['time_scope']
        assert any(candidate['entry']['id'] == old.id and candidate['time_scope']
                   for candidate in plan['chosen'])
        newer, _ = publish(env, text='alpha 六月改在南厅办展')
        service = InsightLinks(env.records, env.service)
        proposal = service.propose('alpha', newer.id, old.id, 'supersedes', 'Synthetic user correction')
        service.review('alpha', proposal['id'], proposal['revision'], True)
        rejected(http, app, env, identity, calls, closed)


def test_overview_membership_drift_rejects_continuation_before_wire(env):
    from backend.memory_app.v2.overviews import ScopeOverviews
    from tests.memory_app.v2.test_overviews import OverviewModel
    document, _ = add_document(env, summary='桥梁预算已确认', body='合成桥梁正文')
    overview = ScopeOverviews(env.records, env.documents, OverviewModel()).update('alpha')
    app, _, _, calls, closed, _ = interrupted_app(env, continuation=True, history_reply=True)
    with TestClient(app) as http:
        identity = partial(http, question='最近在忙什么？')
        plan = frozen_plan(app, env, identity)
        assert any(candidate['entry']['id'] == document for candidate in plan['chosen'])
        from backend.memory_app.kernel.answer_continuations import decode
        row = env.records.read('v2_answer_continuations', identity)
        guards = decode(app.state.ai_turn_store.get(row.payload['plan_ref'])['capsule'])['guards']
        assert guards['overview_guard']['overview'] == overview
        add_document(env, summary='新设备验收完成', body='合成新增正文')
        rejected(http, app, env, identity, calls, closed)


def test_history_revision_drift_rejects_continuation_before_wire(env):
    publish(env)
    prior = env.http.post('/api/v2/workbench/turns', json={'project_id': 'alpha', 'text': 'alpha?'})
    assert prior.status_code == 200, prior.text
    previous = prior.json()['turn']
    app, _, _, calls, closed, _ = interrupted_app(env, continuation=True, history_reply=True)
    with TestClient(app) as http:
        identity = partial(http, thread=previous['thread_id'])
        plan = frozen_plan(app, env, identity)
        assert plan['history'] and plan['history_count'] == 1
        primary = next(call for call in calls if call.get('stream') is True)
        assert previous['receipt']['ask']['answer'] in primary['messages'][-1]['content']
        row = env.records.read('v2_turns', previous['id'])
        with env.records.begin() as tx:
            tx.put(row.collection, row.object_id, {**row.payload, 'user_text': 'Synthetic amended history'},
                   expected_revision=row.revision)
            tx.commit()
        rejected(http, app, env, identity, calls, closed)
