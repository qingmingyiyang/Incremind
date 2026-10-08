import json

import pytest

from backend.memory_app.source_egress import SourceEgressService
from backend.memory_app.v2.insight_generation import generate_insights
from backend.memory_app.v2.overviews import ScopeOverviews
from backend.memory_app.v2.policies import override
from backend.recognition import WorkScope
from tests.memory_app.v2.test_overviews import OverviewModel
from tests.memory_app.v2.test_workbench_ask import env, add_document


def test_project_description_revalidates_actual_overview_source_without_recompute(env):
    first, _ = add_document(env, project='alpha', summary='提炼原文')
    _, item = add_document(env, project='beta', summary='现在私密的摘要')
    env.http.get('/api/v2/projects')
    with env.records.begin() as tx:
        for project in ('alpha', 'beta'):
            if tx.read('v2_projects', project) is None:
                tx.put('v2_projects', project, {'name': project, 'scenes': [], 'builtin': None}, expected_revision=0)
        tx.commit()
    overview_model = OverviewModel()
    overviews = ScopeOverviews(env.records, env.documents, overview_model)
    assert overviews.update('beta')
    assert overviews.current('beta')
    row = env.records.read('workspace_items', item)
    SourceEgressService(env.records).set_policy(WorkScope('local-user', 'beta'),
        'original_item', item, row.revision, 0, [])
    assert overviews.current('beta') is None
    before = overview_model.calls
    with override(extract='@2'):
        generate_insights(env.model, env.service, env.documents, 'alpha', first)
    sent = json.loads(env.model.messages[1]['content'])
    assert next(row for row in sent['projects'] if row['id'] == 'beta')['overview'] == ''
    assert overview_model.calls == before


def _frozen_choice_context(env, monkeypatch):
    first, _ = add_document(env, project='alpha', summary='提炼原文')
    _, item = add_document(env, project='beta', summary='项目概览合成材料')
    env.http.get('/api/v2/projects')
    with env.records.begin() as tx:
        for project in ('alpha', 'beta'):
            if tx.read('v2_projects', project) is None:
                tx.put('v2_projects', project, {'name': project, 'scenes': [], 'builtin': None}, expected_revision=0)
        tx.commit()
    overview_model = OverviewModel()
    overviews = ScopeOverviews(env.records, env.documents, overview_model)
    assert overviews.update('beta')
    old_text = overviews.current('beta')['text'].split('。')[0] + '。'
    assert old_text
    state = {'failure': None, 'sent': []}
    def provider(messages, *, max_tokens, validate_current, wire_attempt_sink):
        validate_current()
        attempt = wire_attempt_sink.begin_model_wire_attempt()
        def wire():
            state['sent'].append(json.loads(messages[-1]['content']))
            if state['failure'] == 'transport':
                attempt.failed_transport(error_code='synthetic_transport_failure')
                raise RuntimeError('synthetic transport failure')
            attempt.succeeded(usage={}, cache_observation=None)
            return ('invalid structured output' if state['failure'] == 'schema'
                else json.dumps({'insights': [], 'supports': []}))
        return attempt.invoke_wire(wire), {'model': 'fake', 'configuration_revision': 2}
    monkeypatch.setattr(env.model, 'complete', provider)
    return first, item, old_text, state, overviews, overview_model


@pytest.mark.parametrize('failure', ['transport', 'schema'])
def test_explicit_retry_preserves_or_rebuilds_safe_choices_after_source_becomes_private(env, monkeypatch, failure):
    first, item, old_text, state, overviews, model = _frozen_choice_context(env, monkeypatch)
    state['failure'] = failure
    with override(extract='@2'):
        assert generate_insights(env.model, env.service, env.documents, 'alpha', first, retry_token='first') == []
    assert len(state['sent']) == 1
    assert next(row for row in state['sent'][0]['projects'] if row['id'] == 'beta')['overview'] == old_text
    original = env.records.list('v2_extract_inputs')[0]
    row = env.records.read('workspace_items', item)
    SourceEgressService(env.records).set_policy(WorkScope('local-user', 'beta'),
        'original_item', item, row.revision, 0, [])
    assert overviews.current('beta') is None
    state['failure'] = None
    with override(extract='@2'):
        assert generate_insights(env.model, env.service, env.documents, 'alpha', first, retry_token='second') == []
    inputs = env.records.list('v2_extract_inputs')
    if failure == 'transport':
        assert len(state['sent']) == 1
        assert inputs == (original,)
    else:
        assert len(state['sent']) == 2
        assert next(row for row in state['sent'][1]['projects'] if row['id'] == 'beta')['overview'] == ''
        assert len(inputs) == 2
        current = next(row for row in inputs if row.object_id != original.object_id)
        assert current.payload['projects'] == state['sent'][1]['projects']
    assert env.records.read('v2_extract_inputs', original.object_id) == original
    assert env.records.list('recognition_candidates') == ()
    assert model.calls == 1


def test_overview_source_privacy_change_between_choice_capture_and_freeze_never_sends_old_text(env, monkeypatch):
    from backend.memory_app.v2 import comparative_insights
    first, item, old_text, state, overviews, model = _frozen_choice_context(env, monkeypatch)
    original = comparative_insights.public_projects
    changed = False
    def capture(*args):
        nonlocal changed
        result = original(*args)
        if not changed:
            assert next(row for row in result if row['id'] == 'beta')['overview'] == old_text
            changed = True
            row = env.records.read('workspace_items', item)
            SourceEgressService(env.records).set_policy(WorkScope('local-user', 'beta'),
                'original_item', item, row.revision, 0, [])
        return result
    monkeypatch.setattr(comparative_insights, 'public_projects', capture)
    with override(extract='@2'):
        assert generate_insights(env.model, env.service, env.documents, 'alpha', first) == []
    assert changed and overviews.current('beta') is None
    assert state['sent'] == []
    assert env.records.list('recognition_candidates') == ()
    assert model.calls == 1


def test_project_becoming_private_between_choice_capture_and_freeze_never_sends_its_name(env, monkeypatch):
    from backend.memory_app.v2 import comparative_insights
    from backend.memory_app.v2.privacy import set_private_project
    first, item, old_text, state, overviews, model = _frozen_choice_context(env, monkeypatch)
    original = comparative_insights.public_projects
    changed = False
    def capture(*args):
        nonlocal changed
        result = original(*args)
        if not changed:
            assert any(row['id'] == 'beta' for row in result)
            changed = True
            set_private_project(env.records, 'beta', True, 0)
        return result
    monkeypatch.setattr(comparative_insights, 'public_projects', capture)
    with override(extract='@2'):
        assert generate_insights(env.model, env.service, env.documents, 'alpha', first) == []
    assert changed and state['sent'] == []
    assert env.records.list('recognition_candidates') == ()
    assert model.calls == 1
