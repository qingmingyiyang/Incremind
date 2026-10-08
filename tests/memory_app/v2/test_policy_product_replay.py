"""Product boundaries preserve selection from preparation through execution."""
import asyncio

import pytest

from backend.memory_app.v2.policies import ACTIVE, get, override, register
from tests.memory_app.v2.test_workbench_ask import env, add_document, publish

OBSERVED = []


@pytest.fixture(scope='module', autouse=True)
def versions():
    for interface in ('scope', 'rank', 'enough', 'compose', 'strength'):
        for selected in ('@9401', '@9402'):
            def observe(*args, _interface=interface, _selected=selected, **kwargs):
                OBSERVED.append((_interface, _selected))
                return get(_interface, version='@1')(*args, **kwargs)
            register(interface, selected)(observe)


def test_real_preview_keeps_prepared_versions_when_active_changes_before_answer(env, monkeypatch):
    add_document(env)
    profile, _ = publish(env, 'I prefer concise answers', project='me')
    from backend.memory_app.v2.usage import initialize_usage
    initialize_usage(env.records, 'insight', profile.id, 'me')
    env.model.intake = False
    OBSERVED.clear()
    query = env.domains.query
    selections = {name: '@9401' for name in ('scope', 'rank', 'enough', 'compose', 'strength')}
    with override(**selections):
        plan = query.prepare_ask('alpha', 'alpha')
        preview = query.store_ask_preview(plan)
    assert {name: plan['policy_versions'][name] for name in selections} == selections
    for name in selections:
        monkeypatch.setitem(ACTIVE, name, '@9402')
    with override(**{name: '@9402' for name in selections}):
        result = asyncio.run(query.execute_ask(preview, 'alpha', 'alpha', True))
    assert result['answer'] == 'Synthetic answer'
    assert OBSERVED and all(selected == '@9401' for _, selected in OBSERVED)
    assert ('scope', '@9401') in OBSERVED
    assert ('enough', '@9401') in OBSERVED
    assert ('compose', '@9401') in OBSERVED
    assert ('strength', '@9401') in OBSERVED
    assert env.model.messages[0]['content'].startswith('已确认的画像')
    store = query.answer_turns.application.state.ai_turn_store
    from uuid import uuid5, NAMESPACE_URL
    turn_id = 'turn-' + uuid5(NAMESPACE_URL, 'workspace-ask:' + preview).hex
    frozen = store.get_request(turn_id)
    assert {name: frozen['policy_versions'][name] for name in selections} == selections
