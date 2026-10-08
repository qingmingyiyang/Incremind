"""Registered entrypoints dispatch actual services and the profile composer."""
from datetime import datetime, timedelta, timezone

import pytest

from backend.memory_app.v2.policies import get, override, register
from tests.memory_app.v2.test_workbench_ask import env, add_document, publish

CALLS = []


@pytest.fixture(scope='module', autouse=True)
def observed_versions():
    for interface in ('organize', 'route', 'retrieve', 'compose', 'consolidate', 'scope', 'rank', 'place'):
        def observe(*args, _interface=interface, **kwargs):
            CALLS.append(_interface)
            return get(_interface, version='@1')(*args, **kwargs)
        register(interface, '@9301')(observe)


@pytest.fixture(autouse=True)
def clear_observations():
    CALLS.clear()


def test_organize_registration_dispatches_real_intake(env):
    with override(organize='@9301'):
        document, _ = add_document(env)
    assert env.documents.read(document)
    assert CALLS == ['organize']


def test_retrieve_scope_and_rank_dispatch_existing_selection(env):
    add_document(env, scene='reading')
    publish(env, 'alpha beta gamma')
    CALLS.clear()
    with override(retrieve='@9301', scope='@9301', rank='@9301'):
        result = env.domains.query.collect_candidates('alpha', 'alpha', scene=None)
    assert result['candidates']
    assert CALLS[0] == 'retrieve'
    assert 'scope' in CALLS and 'rank' in CALLS


def test_compose_registration_includes_existing_profile_block():
    from backend.memory_app.v2.profile import profile_messages
    profile, messages = {'text': 'Synthetic confirmed profile'}, [{'role': 'user', 'content': 'ask'}]
    with override(compose='@9301'):
        result = profile_messages(profile, messages)
    assert result == [{'role': 'system', 'content': profile['text']}, *messages]
    assert CALLS == ['compose']
    assert profile_messages({}, messages) is messages


def test_compose_registration_dispatches_real_profile_construction(env):
    from backend.memory_app.v2.profile import confirmed_profile
    publish(env, 'I prefer precise answers', project='me')
    with override(compose='@9301'):
        profile = confirmed_profile(env.records, env.service)
    assert profile['count'] == 1
    assert 'I prefer precise answers' in profile['text']
    assert CALLS == ['compose']


def test_route_registration_dispatches_current_fast_path(env):
    from backend.memory_app.v2.route import RouteService
    with override(route='@9301'):
        result = RouteService(env.records, env.model).route('记下测试', project_id='alpha', request_key='policy-route')
    assert result.parts[0].intent == 'remember'
    assert CALLS == ['route']


@pytest.mark.parametrize(('text', 'project'), [('灵感测试', 'inbox'), ('#我 灵感测试', 'me')])
def test_place_registration_dispatches_real_inspiration_ownership(env, text, project):
    with override(place='@9301'):
        response = env.http.post('/api/v2/workbench/turns', json={
            'project_id': 'alpha', 'text': text, 'intent': 'inspiration'})
    assert response.status_code == 200, response.text
    insight = response.json()['turn']['receipt']['inspiration']['insight']
    assert env.records.read('recognition_candidates', insight['id']).payload['project_id'] == project
    assert env.model.calls == 0
    assert CALLS == ['place']


def test_consolidate_registration_dispatches_real_daily_transaction(env):
    from backend.memory_app.v2.consolidation import Consolidation
    with override(consolidate='@9301'):
        result = Consolidation(env.records, env.service, env.documents, env.model,
            now=lambda: datetime.now(timezone.utc) + timedelta(days=1)).run()
    assert result['processed'] == 0
    assert CALLS == ['consolidate']
    assert env.records.list('v2_consolidation_runs')[0].payload['status'] == 'completed'
