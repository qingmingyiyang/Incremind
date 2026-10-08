"""Projects are actual metadata deliveries, never fabricated memory entries."""
from copy import deepcopy
import json
from pathlib import Path
import sys

from jsonschema import Draft202012Validator
import pytest

from backend.memory_app.original_sources import source_store
from backend.memory_app.v2.budget import text_tokens
from backend.memory_app.v2.external_context import ARCHIVE, DELIVERIES, USES
from backend.memory_app.v2.policies import ACTIVE, get
from backend.memory_app.v2.projects import assign_scene
from backend.memory_app.v2.privacy import set_private_project
from backend.recognition import WorkScope
from core.effect_log import EffectState
from tests.memory_app.v2.test_external_context import env, settings, setup, request, DAY
from tests.memory_app.v2.test_workbench_ask import add_document, publish
from tests.memory_app.v2.test_overviews import OverviewModel


TURN = 'turn-' + 'c' * 32
ROOT = Path(__file__).resolve().parents[3]


def seed(env, *, name='公开项目 😀', scene='工程'):
    project = env.http.post('/api/v2/projects', json={'name': name}).json()['id']
    doc, item = add_document(env, project=project, scene=scene)
    recognition, _ = publish(env, project=project, doc=doc)
    assign_scene(env.records, 'recognition', recognition.id, project, scene)
    row = env.records.read('v2_projects', project)
    assert env.http.patch('/api/v2/projects/' + project,
        json={'scenes': [scene], 'expected_revision': row.revision}).status_code == 200
    return project, doc, item, recognition


def projects(env, client='codex', arguments=None):
    return env.http.post('/api/v2/external-agent/mcp/projects',
        json={'client': client, 'arguments': {} if arguments is None else arguments})


def prepare(env, *, budget=3000):
    api, runtime, runner = setup(env)
    settings(env, allow_remote=True)
    frozen = api.prepare_projects(TURN,
        request(tool='projects', query='', scope={'user_id': 'local-user', 'project_id': 'default'}, budget=budget),
        session_id='session-catalog', operation_id='op-catalog', idempotency_key=TURN,
        created_at=DAY.isoformat())
    return api, runtime, runner, frozen


def test_projects_are_frozen_metadata_in_the_original_completed_kernel(env):
    project, _doc, _item, _recognition = seed(env)
    settings(env, allow_remote=True)
    env.model.allowed = False
    before = env.records.list('v2_projects')
    usage = (env.records.list('v2_usage_document'), env.records.list('v2_usage_insight'))
    response = projects(env)
    assert response.status_code == 200, response.text
    output = response.json()
    assert set(output) == {'turn_id', 'result'}
    result = output['result']
    assert result['version'] == 'handoff@2'
    assert result['entries'] == result['profile'] == []
    row = next(row for row in result['projects'] if row['id'] == project)
    assert row == {'id': project, 'name': '公开项目 😀', 'overview': '资料1份 · 认识1条',
        'scenes': [{'name': '工程', 'overview': '资料1份 · 认识1条'}]}
    assert json.loads(result['text']) == {'projects': result['projects']}
    assert result['tokens'] == text_tokens(result['text']) <= result['budget']
    store = env.http.app.state.ai_turn_store
    events = store.events_after(output['turn_id'])
    assert events[-1]['type'] == 'turn.completed'
    assert not any(event['type'].startswith('model.') for event in events)
    outcome = next(event for event in events if event['type'] == 'tool.outcome.recorded')
    assert store.effect_runner.log.get(outcome['correlation']['tool_call_id']).state is EffectState.SETTLED_OK
    _ref, archive = env.http.app.state.external_context._archive(output['turn_id'])
    assert archive['schema_version'] == '3.0.0' and archive['mapping'] == {}
    assert archive['handoff'] == result and archive['request']['policy_versions']['handoff'] == '@2'
    assert archive['binding']['material_refs'] == archive['binding']['source_snapshots'] == []
    assert env.records.list('v2_projects') == before
    assert len(env.records.list(DELIVERIES)) == len(env.records.list('v2_external_agent_reservations')) == 1
    assert env.records.list(USES) == ()
    assert (env.records.list('v2_usage_document'), env.records.list('v2_usage_insight')) == usage
    assert env.model.calls == 0
    schema = json.loads((ROOT / 'core-contracts/ai/external-context-result.schema.json').read_text())
    Draft202012Validator(schema).validate(result)


@pytest.mark.parametrize('change', [{'allow_remote': False}, {'clients': {'claude': True, 'codex': False}}])
def test_projects_closed_external_admission_has_no_delivery_or_quota(env, change):
    seed(env)
    settings(env, **{'allow_remote': True, **change})
    assert projects(env).status_code == 409
    assert env.records.list(DELIVERIES) == env.records.list('v2_external_agent_reservations') == ()
    assert env.model.calls == 0


def test_projects_exclude_private_and_profile_but_private_anchor_is_not_a_catalog(env):
    public, *_ = seed(env)
    private, *_ = seed(env, name='私密名字不得交出')
    add_document(env, project='me')
    env.http.get('/api/v2/projects')  # Actual registration before the read-only call.
    set_private_project(env.records, private, True, 0)
    set_private_project(env.records, 'default', True, 0)
    settings(env, allow_remote=True, include_profile=False)
    response = projects(env)
    assert response.status_code == 200, response.text
    rows = response.json()['result']['projects']
    assert public in {row['id'] for row in rows}
    assert {private, 'default', 'me'}.isdisjoint(row['id'] for row in rows)
    assert '私密名字不得交出' not in response.text
    assert env.model.calls == 0


def test_projects_budget_counts_the_complete_long_names_and_scenes_without_mutation(env):
    project, *_ = seed(env, name='长名字' * 5000, scene='长场景' * 2000)
    row = env.records.read('v2_projects', project)
    api, runtime, runner, _frozen = prepare(env, budget=180)
    result = api.execute(TURN, runtime=runtime, runner=runner)
    assert result['tokens'] == text_tokens(result['text']) <= 180
    assert project not in {item['id'] for item in result['projects']}
    assert env.records.read('v2_projects', project) == row
    assert result['entries'] == result['profile'] == []
    assert env.model.calls == 0


def test_projects_uses_current_overview_without_generation_or_new_attempt(env):
    from backend.memory_app.v2.overviews import ScopeOverviews
    project, *_ = seed(env)
    model = OverviewModel()
    overview = ScopeOverviews(env.records, env.documents, model).update(project)
    assert overview and model.calls == 1
    settings(env, allow_remote=True)
    env.model.allowed = False
    response = projects(env)
    assert response.status_code == 200, response.text
    row = next(row for row in response.json()['result']['projects'] if row['id'] == project)
    assert row['overview'] == overview['text'] and model.calls == 1 and env.model.calls == 0


@pytest.mark.parametrize('change', ['metadata', 'privacy', 'source', 'overview_source'])
def test_catalog_drift_before_execution_cannot_publish_old_frozen_metadata(env, change):
    project, _doc, item, _recognition = seed(env)
    store, body = None, None
    if change == 'source':
        store = source_store(env.records)
        body = {'id': 'catalog-original', 'project_id': project, 'title': '真实目录原件',
                'metadata': {'content_snapshot': '完整且仅由原 Source owner 保存的合成正文'}}
        store.write('sources', body['id'], body, expected_revision=0)
    if change == 'overview_source':
        from backend.memory_app.v2.overviews import ScopeOverviews
        assert ScopeOverviews(env.records, env.documents, OverviewModel()).update(project)
    api, runtime, runner, _frozen = prepare(env)
    if change == 'metadata':
        row = env.records.read('v2_projects', project)
        assert env.http.patch('/api/v2/projects/' + project,
            json={'name': '变更后的名字', 'expected_revision': row.revision}).status_code == 200
    elif change == 'privacy':
        set_private_project(env.records, project, True, 0)
    elif change == 'source':
        _ref, archive = api._archive(TURN)
        assert any(node.get('incarnation') == store.incarnation('sources', body['id'])
            for proof in archive['catalog']['proofs'] for obj in proof['objects']
            for node in obj['snapshot']['nodes'] if node['type'] == 'original_source' and node['id'] == body['id'])
        before = store.incarnation('sources', body['id'])
        assert store.delete('sources', body['id'])
        store.write('sources', body['id'], body, expected_revision=0)
        assert store.revision('sources', body['id']) == 1
        assert store.incarnation('sources', body['id']) != before
    else:
        from backend.memory_app.source_egress import SourceEgressService
        _ref, archive = api._archive(TURN)
        assert next(proof for proof in archive['catalog']['proofs'] if proof['project_id'] == project)['overview']
        row = env.records.read('workspace_items', item)
        SourceEgressService(env.records).set_policy(WorkScope('local-user', project),
            'original_item', item, row.revision, 0, [])
    with pytest.raises(ValueError):
        api.execute(TURN, runtime=runtime, runner=runner)
    assert env.records.list(DELIVERIES) == env.records.list('v2_external_agent_reservations') == ()
    assert not any(event['type'] == 'tool.intent.recorded' for event in api.turns.events_after(TURN))
    assert env.model.calls == 0


def test_catalog_privacy_withdrawal_after_real_completion_preserves_facts_not_delivery(env):
    project, *_ = seed(env)
    api, runtime, runner, _frozen = prepare(env)
    observed = []

    def after_actual_completion(frame, event, _argument):
        if (event == 'return' and frame.f_code is api._completed.__func__.__code__
                and frame.f_locals.get('turn_id') == TURN and not observed):
            observed.append(True)
            set_private_project(env.records, project, True, 0)

    previous = sys.getprofile()
    try:
        sys.setprofile(after_actual_completion)
        with pytest.raises(ValueError):
            api.execute(TURN, runtime=runtime, runner=runner)
    finally:
        sys.setprofile(previous)
    assert observed == [True]
    assert api.turns.events_after(TURN)[-1]['type'] == 'turn.completed'
    assert env.records.list(DELIVERIES) == ()
    assert len(env.records.list('v2_external_agent_reservations')) == 1
    assert env.model.calls == 0


def test_catalog_policy_full_json_budget_keeps_old_active_and_old_bytes():
    before = deepcopy(ACTIVE)
    old = get('handoff')([], count_tokens=len, budget=300)
    policy = get('handoff', version='@2')
    rows = [{'id': 'alpha', 'name': '甲' * 1000, 'overview': '资料1份 · 认识0条',
             'scenes': [{'name': '乙' * 1000, 'overview': '资料1份 · 认识0条'}]},
            {'id': 'beta', 'name': '乙', 'overview': '资料0份 · 认识0条', 'scenes': []}]
    result = policy(rows, count_tokens=len, budget=180)
    assert result['projects'] == [rows[1]] and result['tokens'] == len(result['text']) <= 180
    assert json.loads(result['text']) == {'projects': [rows[1]]}
    assert ACTIVE == before and ACTIVE['handoff'] == '@1'
    assert get('handoff')([], count_tokens=len, budget=300) == old


def test_projects_arguments_cannot_select_client_scope_or_budget(env):
    settings(env, allow_remote=True)
    for arguments in [{'client': 'claude'}, {'project': 'alpha'}, {'budget': 1}]:
        assert projects(env, arguments=arguments).status_code == 400
    assert env.records.list(DELIVERIES) == env.records.list('v2_external_agent_reservations') == ()


def test_unregistered_default_uses_the_existing_display_name_without_registration(env):
    add_document(env, project='default')
    assert env.records.read('v2_projects', 'default') is None
    settings(env, allow_remote=True)
    response = projects(env)
    assert response.status_code == 200, response.text
    row = next(row for row in response.json()['result']['projects'] if row['id'] == 'default')
    assert row['name'] == '默认'
    assert row['overview'] == '资料1份 · 认识0条'
    assert env.records.read('v2_projects', 'default') is None
    assert env.model.calls == 0


def test_source_only_projects_are_qualified_without_registering_or_leaking_private_metadata(env):
    from backend.memory_app.source_egress import SourceEgressService

    store = env.domains.query.source_store
    bodies = [
        {'id': 'catalog-source-default', 'title': '默认原件',
         'metadata': {'content_snapshot': '默认原件的完整合成正文'}},
        {'id': 'catalog-source-public', 'project_id': 'source-only', 'title': '公开原件',
         'metadata': {'content_snapshot': '公开原件的完整合成正文'}},
        {'id': 'catalog-source-private', 'project_id': 'private-source-name', 'title': '私密原件名字',
         'metadata': {'content_snapshot': '私密原件正文不得交出'}},
        {'id': 'catalog-source-empty', 'title': '缺正文不能计数', 'metadata': {}},
        {'id': 'catalog-source-invalid', 'project_id': 'invalid/scope', 'title': '非法范围不能交出',
         'metadata': {'content_snapshot': '非法范围中的合成正文'}},
    ]
    for body in bodies:
        store.write('sources', body['id'], body, expected_revision=0)
    for collection in ('v2_projects', 'workspace_items', 'documents', 'recognitions'):
        assert env.records.list(collection) == ()
    set_private_project(env.records, 'private-source-name', True, 0)
    authority = SourceEgressService(env.records)
    for project, identity in [('default', 'catalog-source-default'), ('source-only', 'catalog-source-public')]:
        entries = env.domains.query.query_entries(project)
        assert [(entry['kind'], entry['id'], entry['revision']) for entry in entries] == [('source', identity, 1)]
        scope = WorkScope('local-user', project)
        proof = authority.snapshot(scope, [{'type': 'original_source', 'id': identity, 'revision': 1}])
        authority.require(proof, 'generation')
        authority.validate_snapshot(scope, proof)
        assert proof['nodes'][0]['incarnation'] == store.incarnation('sources', identity)
    settings(env, allow_remote=True)
    response = projects(env)
    assert response.status_code == 200, response.text
    output = response.json()
    rows = {row['id']: row for row in output['result']['projects']}
    assert {'default', 'source-only'} <= rows.keys()
    assert rows['default'] == {'id': 'default', 'name': '默认', 'overview': '资料1份 · 认识0条', 'scenes': []}
    assert rows['source-only'] == {'id': 'source-only', 'name': 'source-only', 'overview': '资料1份 · 认识0条', 'scenes': []}
    assert {'private-source-name', 'invalid/scope'}.isdisjoint(rows)
    assert all(body['title'] not in response.text for body in bodies)
    assert all(body['metadata'].get('content_snapshot', '') not in response.text
               for body in bodies if body['metadata'].get('content_snapshot'))
    api = env.http.app.state.external_context
    _ref, archive = api._archive(output['turn_id'])
    originals = {(node['id'], node['incarnation']) for project in archive['catalog']['proofs']
                 for obj in project['objects'] for node in obj['snapshot']['nodes']
                 if node['type'] == 'original_source'}
    assert originals == {(identity, store.incarnation('sources', identity))
                         for identity in ('catalog-source-default', 'catalog-source-public')}
    for collection in ('v2_projects', 'workspace_items', 'documents', 'recognitions'):
        assert env.records.list(collection) == ()
    assert all(store.read('sources', body['id']) == body and store.revision('sources', body['id']) == 1
               for body in bodies)
    assert env.model.calls == 0


@pytest.mark.parametrize('tool', ['recall', 'read', 'methods', 'projects'])
def test_global_catalog_admission_never_grants_private_default_materials(env, tool):
    from backend.memory_app.v2.external_agent_guard import ExternalAgentGuard
    settings(env, allow_remote=True)
    set_private_project(env.records, 'default', True, 0)
    item = env.domains.items.create('default', 'text', '私密原件', '绝不交出的正文')
    materials = [] if tool != 'projects' else [{'type': 'original_item', 'id': item['id'],
        'revision': 1, 'project_id': 'default'}]
    with pytest.raises(ValueError):
        ExternalAgentGuard(env.records, owner_id='local-user').freeze('turn-private-default',
            request(tool=tool, scope={'user_id': 'local-user', 'project_id': 'default'}), materials)
    assert env.records.read('v2_external_agent_bindings', 'turn-private-default') is None
