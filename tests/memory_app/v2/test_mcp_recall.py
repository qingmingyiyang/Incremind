"""MCP retrieval freezes real qualified objects and uses the original Kernel."""
from copy import deepcopy
import json
import sys
import threading

import pytest

from backend.memory_app.document_recognition import ensure_document_experience
from backend.memory_app.v2.budget import text_tokens
from backend.memory_app.v2.layers import summary_of
from backend.memory_app.v2.policies import ACTIVE
from backend.memory_app.v2.policies.pipelines import interfaces_for_turn
from backend.memory_app.v2.privacy import set_private_project
from backend.recognition import WorkScope
from core.effect_log import EffectState
from tests.memory_app.v2.test_external_context import env, settings, setup, request, DAY
from tests.memory_app.v2.test_workbench_ask import add_document


PREFIX = '/api/v2/external-agent/mcp/'


def method(env, *, project='alpha', content='出发前检查证件有效期并留换货余地', conditions=('挑礼物时',)):
    doc, item = add_document(env, project=project)
    scope = WorkScope('local-user', project)
    experience = ensure_document_experience(env.documents, env.service, project, doc)[0]
    proposal = env.service.propose(scope=scope, content=content, conditions=list(conditions),
        source_experience_ids=[experience])
    recognition = env.service.publish(scope=scope, candidate_id=proposal.id,
        expected_revision=1, reviewer='local-user')
    return recognition, doc, item


def call(env, tool='recall', **arguments):
    return env.http.post(PREFIX + tool, json={'client': 'codex', 'arguments': arguments})


def completed(env, response):
    assert response.status_code == 200, response.text
    output = response.json()
    assert set(output) == {'turn_id', 'result'}
    store = env.http.app.state.ai_turn_store
    events = store.events_after(output['turn_id'])
    assert events[-1]['type'] == 'turn.completed'
    assert not any(event['type'].startswith('model.') for event in events)
    outcomes = [event for event in events if event['type'] == 'tool.outcome.recorded']
    assert len(outcomes) == 1
    assert store.effect_runner.log.get(outcomes[0]['correlation']['tool_call_id']).state is EffectState.SETTLED_OK
    api = env.http.app.state.external_context
    _ref, archive = api._archive(output['turn_id'])
    assert archive['schema_version'] == '4.0.0'
    assert archive['handoff'] == output['result']
    assert archive['recall']['policy_versions']['compose'] == ACTIVE['compose']
    assert set(store.get_request(output['turn_id'])['policy_versions']) == set(interfaces_for_turn('external.context'))
    assert len(env.records.list('v2_external_agent_deliveries')) == 1
    assert len(env.records.list('v2_external_agent_reservations')) == 1
    assert env.model.calls == 0
    assert output['result']['tokens'] == text_tokens(output['result']['text']) <= output['result']['budget']
    return output, archive


@pytest.mark.parametrize('tool,arguments', [
    ('recall', {'query': '证件有效期', 'project': 'alpha'}),
    ('methods', {'situation': '挑生日礼物', 'project': 'alpha'}),
])
def test_mcp_recall_and_methods_use_the_real_model_free_kernel(env, tool, arguments):
    recognition, _doc, _item = method(env)
    settings(env, allow_remote=True)
    usage = (env.records.list('v2_usage_document'), env.records.list('v2_usage_insight'))
    before = deepcopy(ACTIVE)
    output, archive = completed(env, call(env, tool, **arguments))
    assert output['result']['version'] == 'handoff@1'
    row = next(row for row in output['result']['entries'] if row['object_id'] == recognition.id)
    assert row['layer'] == 'L3' and row['conditions'] == ['挑礼物时']
    assert '出发前检查证件有效期并留换货余地' in row['excerpt']
    assert archive['mapping'][row['id']]['material']['id'] == recognition.id
    assert archive['recall']['materials']
    assert (env.records.list('v2_usage_document'), env.records.list('v2_usage_insight')) == usage
    assert ACTIVE == before


def test_mcp_methods_preserve_conditions_and_do_not_admit_body_only_matches(env):
    applicable, *_ = method(env, content='先问清对方近期愿望', conditions=('挑礼物时',))
    irrelevant, *_ = method(env, content='挑礼物先比较尺寸', conditions=('写论文时',))
    settings(env, allow_remote=True)
    output, _archive = completed(env, call(env, 'methods', situation='给朋友挑生日礼物', project='alpha'))
    assert {row['object_id'] for row in output['result']['entries']} == {applicable.id}
    assert irrelevant.id not in {row['object_id'] for row in output['result']['entries']}
    assert output['result']['entries'][0]['conditions'] == ['挑礼物时']


def test_mcp_l2_converts_actual_crlf_markdown_windows_to_summary_relative_coordinates(env):
    doc, _item = add_document(env, summary='海岛航线', body='另一种内容', original='无关的原件')
    markdown = '# Synthetic\r\n\r\n## 摘要\r\n\r\n海岛航线 😀\r\n保留原换行\r\n\r\n## 正文\r\n另一种内容\r\n'
    current = env.documents.read(doc)
    env.documents.save_user_edit(doc, expected_revision=current['revision'], markdown=markdown)
    settings(env, allow_remote=True)
    output, archive = completed(env, call(env, query='海岛航线', project='alpha'))
    summary, start, _end = summary_of(markdown)
    assert start > len(summary)
    row = next(row for row in output['result']['entries'] if row['layer'] == 'L2')
    selection = next(row for row in archive['selections'] if row['layer'] == 'L2')
    assert selection['id'] == doc
    assert all(0 <= window['start'] < window['end'] <= len(summary) for window in selection['windows'])
    assert row['excerpt'] == '\n\n'.join(summary[window['start']:window['end']] for window in selection['windows'])
    assert '海岛航线 😀' in row['excerpt'] and '## 摘要' not in row['excerpt']
    assert env.documents.markdown(doc) == markdown


def test_mcp_recall_small_budget_keeps_all_qualified_source_bindings_but_no_usage(env):
    recognition, *_ = method(env)
    settings(env, allow_remote=True)
    output, archive = completed(env, call(env, query='证件有效期', project='alpha', budget=1))
    assert output['result']['entries'] == output['result']['profile'] == []
    assert output['result']['text'] == '' and output['result']['tokens'] == 0
    assert archive['mapping'] == {}
    assert any(material['id'] == recognition.id for material in archive['recall']['materials'])
    assert archive['binding']['source_snapshots'] and archive['recall']['candidates']
    assert env.records.list('v2_external_agent_citations') == ()


@pytest.mark.parametrize('profile_enabled,private', [(True, False), (False, False), (True, True)])
def test_mcp_recall_profile_uses_confirmed_sources_and_false_never_reads_profile(env, profile_enabled, private):
    profile, *_ = method(env, project='me', content='我偏好明确而简短的答复', conditions=())
    method(env)
    if private:
        set_private_project(env.records, 'me', True, 0)
    settings(env, allow_remote=True, include_profile=profile_enabled)
    observed = []

    def observe_profile(frame, event, _argument):
        if event == 'call' and frame.f_code.co_name == 'confirmed_profile':
            observed.append(True)

    previous_thread, previous_main = threading.getprofile(), sys.getprofile()
    try:
        threading.setprofile_all_threads(observe_profile)
        output, _archive = completed(env, call(env, query='证件有效期', project='alpha'))
    finally:
        threading.setprofile_all_threads(previous_thread)
        sys.setprofile(previous_main)
    if profile_enabled and not private:
        assert observed and {row['object_id'] for row in output['result']['profile']} == {profile.id}
        assert all(row['id'].startswith('P') and row['sources'] for row in output['result']['profile'])
    else:
        assert observed == [] and output['result']['profile'] == []
        assert env.records.list('v2_profile_blocks') == ()


def frozen_recall(env, *, budget=3000, scene=None):
    api, runtime, runner = setup(env)
    settings(env, allow_remote=True)
    turn = 'turn-' + 'd' * 32
    frozen = api.prepare_recall(turn, request(tool='recall', query='证件有效期', budget=budget), scene=scene,
        session_id='session-recall', operation_id='op-recall', idempotency_key=turn, created_at=DAY.isoformat())
    return api, runtime, runner, turn, frozen


@pytest.mark.parametrize('change', ['forget', 'scene', 'source', 'unselected_source'])
def test_frozen_recall_rejects_late_eligibility_and_every_source_change(env, change):
    from backend.memory_app.v2.projects import assign_scene
    from backend.memory_app.v2.recall_preferences import set_preference

    recognition, _doc, _item = method(env)
    assign_scene(env.records, 'recognition', recognition.id, 'alpha', '工程')
    store = env.domains.query.source_store
    source = {'id': 'recall-separate-source', 'project_id': 'alpha', 'title': '合成证件原件',
              'metadata': {'content_snapshot': '证件有效期只有现有原件 owner 保存'}}
    store.write('sources', source['id'], source, expected_revision=0)
    api, runtime, runner, turn, frozen = frozen_recall(env, budget=1 if change == 'unselected_source' else 3000, scene='工程')
    _ref, archive = api._archive(turn)
    before = deepcopy(archive)
    usage = (env.records.list('v2_usage_document'), env.records.list('v2_usage_insight'))
    if change == 'forget':
        set_preference(env.records, WorkScope('local-user', 'alpha'), recognition.id,
            recognition_revision=recognition.revision, preference_revision=0, state='forgotten')
    elif change == 'scene':
        assign_scene(env.records, 'recognition', recognition.id, 'alpha', '另一个场景')
    else:
        if change == 'unselected_source':
            assert archive['handoff']['entries'] == archive['handoff']['profile'] == []
        assert any(node['id'] == source['id'] and node.get('incarnation') == store.incarnation('sources', source['id'])
            for proof in archive['recall']['candidates'] for node in proof['snapshot']['nodes'])
        original_incarnation = store.incarnation('sources', source['id'])
        assert store.delete('sources', source['id'])
        store.write('sources', source['id'], source, expected_revision=0)
        assert store.revision('sources', source['id']) == 1
        assert store.incarnation('sources', source['id']) != original_incarnation
    with pytest.raises(ValueError):
        api.execute(turn, runtime=runtime, runner=runner)
    assert api.turns.get_request(turn) == frozen and api._archive(turn)[1] == before
    assert not any(event['type'] == 'tool.intent.recorded' for event in api.turns.events_after(turn))
    assert env.records.list('v2_external_agent_deliveries') == env.records.list('v2_external_agent_reservations') == ()
    assert (env.records.list('v2_usage_document'), env.records.list('v2_usage_insight')) == usage
    assert env.model.calls == 0


@pytest.mark.parametrize('change', ['forget', 'client', 'source'])
def test_recall_withdrawal_after_actual_completion_keeps_core_facts_without_delivery(env, change):
    from backend.memory_app.v2.recall_preferences import set_preference

    recognition, *_ = method(env)
    store = env.domains.query.source_store
    source = {'id': 'recall-completed-source', 'project_id': 'alpha', 'title': '合成原件',
              'metadata': {'content_snapshot': '证件有效期与完整原件'}}
    store.write('sources', source['id'], source, expected_revision=0)
    api, runtime, runner, turn, frozen = frozen_recall(env)
    _ref, original = api._archive(turn)
    observed = []

    def after_actual_completion(frame, event, _argument):
        if (event == 'return' and frame.f_code is api._completed.__func__.__code__
                and frame.f_locals.get('turn_id') == turn and not observed):
            observed.append(True)
            if change == 'forget':
                set_preference(env.records, WorkScope('local-user', 'alpha'), recognition.id,
                    recognition_revision=recognition.revision, preference_revision=0, state='forgotten')
            elif change == 'client':
                settings(env, clients={'claude': True, 'codex': False})
            else:
                assert store.delete('sources', source['id'])
                store.write('sources', source['id'], source, expected_revision=0)

    previous = sys.getprofile()
    try:
        sys.setprofile(after_actual_completion)
        with pytest.raises(ValueError):
            api.execute(turn, runtime=runtime, runner=runner)
    finally:
        sys.setprofile(previous)
    assert observed == [True]
    events = api.turns.events_after(turn)
    assert events[-1]['type'] == 'turn.completed'
    assert sum(event['type'] == 'tool.intent.recorded' for event in events) == 1
    outcome = next(event for event in events if event['type'] == 'tool.outcome.recorded')
    assert api.turns.effect_runner.log.get(outcome['correlation']['tool_call_id']).state is EffectState.SETTLED_OK
    assert api.turns.get_request(turn) == frozen and api._archive(turn)[1] == original
    assert env.records.list('v2_external_agent_deliveries') == ()
    assert len(env.records.list('v2_external_agent_reservations')) == 1
    assert env.records.list('v2_external_agent_citations') == () and env.model.calls == 0


def test_recall_immutable_compose_version_matches_every_actual_planning_get(env):
    method(env)
    from backend.memory_app.v2.policies import get

    observed = []

    def observe_actual_get(frame, event, _argument):
        if event == 'return' and frame.f_code is get.__code__ and frame.f_locals.get('interface') == 'compose':
            observed.append(frame.f_locals['selected'])

    previous = sys.getprofile()
    try:
        sys.setprofile(observe_actual_get)
        api, _runtime, _runner, turn, _frozen = frozen_recall(env)
    finally:
        sys.setprofile(previous)
    assert observed and set(observed) == {api._archive(turn)[1]['recall']['policy_versions']['compose']}
    assert env.records.list('v2_external_agent_deliveries') == env.records.list('v2_external_agent_reservations') == ()
    assert env.model.calls == 0


def test_delivery_transaction_rechecks_the_complete_profile_basis_after_last_readonly_validation(env):
    from backend.memory_app.v2.profile import validate_profile, _eligible, _basis

    method(env)
    _profile, doc, _item = method(env, project='me', content='已确认的初始画像', conditions=())
    api, runtime, runner, turn, _frozen = frozen_recall(env)
    scope = WorkScope('local-user', 'me')
    experience = ensure_document_experience(env.documents, env.service, 'me', doc)[0]
    observed, additions, proofs = [], [], []
    usage = (env.records.list('v2_usage_document'), env.records.list('v2_usage_insight'))

    def add_after_actual_validation(frame, event, _argument):
        if (event == 'return' and frame.f_code is validate_profile.__code__
                and frame.f_locals.get('records') is env.records):
            observed.append(True)
            if len(observed) == 2:
                before = api.recall._markers(env.records, {'alpha', 'me'})
                proposal = env.service.propose(scope=scope, content='在交付事务开始前确认的新画像',
                    source_experience_ids=[experience])
                added = env.service.publish(scope=scope, candidate_id=proposal.id,
                    expected_revision=1, reviewer='local-user')
                with env.records.begin() as tx:
                    validity = tx.read('v2_insight_validity', added.id)
                    tx.delete('v2_insight_validity', added.id, expected_revision=validity.revision)
                    tx.commit()
                qualified = env.service.get_recognition(scope=scope, recognition_id=added.id)
                proofs.append({'authorized': qualified.authorized and qualified.effective_state == 'active',
                    'retrieval_ids': [row['id'] for row in env.service.retrieval_entries(scope=scope)],
                    'basis_ids': [row['id'] for row in _basis(_eligible(env.records, env.service), env.records)['items']],
                    'markers_equal': api.recall._markers(env.records, {'alpha', 'me'}) == before,
                    'version': env.records.read('recognition_versions', added.id + '~v1').payload['action']})
                additions.append(added.id)

    previous = sys.getprofile()
    failure = None
    try:
        sys.setprofile(add_after_actual_validation)
        try:
            api.execute(turn, runtime=runtime, runner=runner)
        except ValueError as error:
            failure = error
    finally:
        sys.setprofile(previous)
    assert len(observed) == 2 and len(additions) == 1
    assert proofs[0]['authorized'] and proofs[0]['markers_equal'] and proofs[0]['version'] == 'publish'
    assert additions[0] in proofs[0]['retrieval_ids'] and additions[0] in proofs[0]['basis_ids']
    assert failure is not None, {'observed': len(observed), 'new_profile': additions[0], 'proof': proofs[0]}
    assert api.turns.events_after(turn)[-1]['type'] == 'turn.completed'
    assert env.records.list('v2_external_agent_deliveries') == ()
    assert len(env.records.list('v2_external_agent_reservations')) == 1
    assert (env.records.list('v2_usage_document'), env.records.list('v2_usage_insight')) == usage
    assert env.records.list('v2_external_agent_citations') == () and env.model.calls == 0


def test_explicit_me_with_profile_disabled_rejects_before_any_retrieval_or_profile_read(env):
    from backend.memory_app.v2.profile import confirmed_profile

    method(env, project='me', content='真实已确认的本人画像', conditions=())
    settings(env, allow_remote=True, include_profile=False)
    collect_code = env.domains.query.collect_candidates.__func__.__code__
    collect_calls, profile_calls = [], []

    def observe_actual_reads(frame, event, _argument):
        if event != 'call':
            return
        if frame.f_code is collect_code and frame.f_locals.get('self') is env.domains.query:
            collect_calls.append(True)
        if frame.f_code is confirmed_profile.__code__:
            profile_calls.append(True)

    previous_thread, previous_main = threading.getprofile(), sys.getprofile()
    try:
        threading.setprofile_all_threads(observe_actual_reads)
        response = call(env, query='本人画像', project='me')
    finally:
        threading.setprofile_all_threads(previous_thread)
        sys.setprofile(previous_main)
    assert response.status_code == 409 and response.json() == {'detail': 'external_agent_profile_disabled'}
    assert collect_calls == profile_calls == []
    assert env.records.list('v2_profile_blocks') == ()
    assert env.records.list('v2_external_agent_reservations') == env.records.list('v2_external_agent_deliveries') == ()
    assert env.records.list('v2_external_agent_citations') == () and env.model.calls == 0
