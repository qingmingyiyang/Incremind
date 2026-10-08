"""Qualified external handoff uses the original durable Kernel and domain stores."""
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

from jsonschema import Draft202012Validator
import pytest

from backend.memory_app.kernel.ai_runtime import get_or_build_ai_runtime
from backend.memory_app.v2.external_agent_settings import external_agent_settings, replace_external_agent_settings
from backend.memory_app.v2.privacy import set_private_project
from backend.memory_app.v2.external_context import ExternalContextError, ARCHIVE, DELIVERIES, USES, delivery_receipts
from backend.memory_app.document_recognition import ensure_document_experience
from backend.memory_app.source_egress import SourceEgressService
from backend.memory_app.v2.settings import _receipts
from backend.recognition import WorkScope
from core.ai_kernel import AIKernelContractError, validate_turn_request
from core.ai_kernel.turn_kinds import freeze_turn_request
from core.document_engine.ports import DocumentDraft
from core.effect_log import EffectState
from tests.memory_app.v2.test_workbench_ask import env as _env_fixture, add_document


env = _env_fixture
ROOT = Path(__file__).resolve().parents[3]
DAY = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)
TURN = 'turn-' + 'a' * 32


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def settings(env, **changes):
    current = external_agent_settings(env.records)
    return replace_external_agent_settings(env.records,
        {key: value for key, value in current.items() if key != 'revision'} | changes,
        expected_revision=current['revision'])


def request(**changes):
    return {'client': 'codex', 'tool': 'read', 'query': '合成问题 😀',
        'scope': {'user_id': 'local-user', 'project_id': 'alpha'}, 'budget': 3000, **changes}


def setup(env):
    runtime = get_or_build_ai_runtime(SimpleNamespace(app=env.http.app), SimpleNamespace(root_dir=env.root))
    return env.http.app.state.external_context, runtime, env.http.app.state.ai_turn_runner


def material(env):
    row = env.domains.items.create('alpha', 'text', '合成原件', '完整且可定位的原文 😀')
    return {'type': 'original_item', 'id': row['id'], 'project_id': 'alpha', 'revision': 1,
            'layer': 'L0', 'windows': []}


def prepare(env, identity=TURN, selections=None, **changes):
    api, runtime, runner = setup(env)
    settings(env, allow_remote=True)
    selections = [material(env)] if selections is None else selections
    try:
        frozen = api.prepare(identity, request(**changes), selections,
            session_id='session-external', operation_id='op-external', idempotency_key=identity,
            created_at=DAY.isoformat())
    except Exception as error:
        # These test objects contain only synthetic text. Show the actual
        # cause while keeping the public service's fixed-error boundary intact.
        if error.__context__ is not None:
            raise error.__context__
        raise
    return api, runtime, runner, frozen


def test_external_context_uses_real_kernel_and_completed_effect(env):
    api, runtime, runner, frozen = prepare(env)
    assert env.records.list('v2_external_agent_reservations') == ()
    delivered = api.execute(TURN, runtime=runtime, runner=runner)
    store = env.http.app.state.ai_turn_store
    assert delivered['entries'][0]['id'] == 'M1'
    assert delivered['entries'][0]['excerpt'] == '完整且可定位的原文 😀'
    assert store.get_request(TURN) == frozen
    events = store.events_after(TURN)
    assert events[-1]['type'] == 'turn.completed'
    assert not any(event['type'].startswith('model.') for event in events)
    tool = next(event for event in events if event['type'] == 'tool.outcome.recorded')
    assert store.effect_runner.log.get(tool['correlation']['tool_call_id']).state is EffectState.SETTLED_OK
    assert sum(event['type'] == 'tool.intent.recorded' for event in events) == 1
    assert any(event['type'] == 'tool.requested' for event in events)
    before = tuple(events)
    assert api.execute(TURN, runtime=runtime, runner=runner) == delivered
    assert store.events_after(TURN) == before
    assert len(env.records.list('v2_external_agent_reservations')) == 1
    assert len(env.records.list('v2_external_agent_deliveries')) == 1
    assert env.records.list('v2_usage_document') == env.records.list('v2_usage_insight') == ()
    assert env.model.calls == 0
    Draft202012Validator(json.loads((ROOT / 'core-contracts/ai/turn-request.schema.json').read_text())).validate(frozen)
    Draft202012Validator(json.loads((ROOT / 'core-contracts/ai/external-context-result.schema.json').read_text())).validate(delivered)


def test_builtin_answer_owner_baseline(env, monkeypatch):
    from backend.memory_app.kernel import answer_turns
    monkeypatch.setattr(answer_turns, '_now', lambda: DAY.isoformat())
    query = env.domains.query
    result = asyncio.run(query.answer_turns.run(turn_id='turn-builtin-baseline', project='alpha',
        question='baseline empty project?', operation=lambda: query.ask({'project_id':'alpha', 'question':'baseline empty project?'})))
    assert result['no_match'] is True
    original = env.http.app.state.ai_turn_store.get_request('turn-builtin-baseline')
    # Captured from the actual original owner in the first RED run, before
    # external composition existed. Compare the whole serialized request.
    baseline = r'''{"approval_policy":{"auto_approve_read_only":true,"mode":"risk_based"},"capability_policy":{"allowed":["workbench.answer.execute"],"denied":[],"require_approval":[]},"capability_request":{"arguments":{"query":"baseline empty project?"},"capability_id":"workbench.answer.execute","mode":"execute_exact_v1"},"context_policy":{"include_memory":false,"include_project_skill":false,"include_session_history":false,"max_context_bytes":262144},"created_at":"2026-10-05T12:00:00+00:00","desired_outcome":"project.answer","execution_policy":{"budget":{"max_steps":2,"planner_timeout_ms":120000},"purpose":"primary","template_version":2},"idempotency_key":"answer-turn-builtin-baseline","input":{"kind":"text","refs":[],"text":"baseline empty project?"},"operation_id":"answer-turn-builtin-baseline","policy_versions":{"compose":"@3","enough":"@1","place":"@1","rank":"@1","retrieve":"@3","route":"@1","scope":"@2","strength":"@1"},"privacy":{"allow_remote":true,"consent_refs":["crp://default/model-settings/generation"],"excluded_refs":[],"material_refs":[],"mode":"remote_allowed","pii":"possible","privacy_revision":0,"retention":"session","source_snapshots":[]},"schema_version":"1.0.0","scope":{"kind":"project","project_id":"alpha","series_id":null},"session_id":"session-turn-builtin-baseline","turn_id":"turn-builtin-baseline"}'''
    assert encoded(original) == baseline.encode()


@pytest.mark.parametrize('entry', ['validator', 'runner'])
def test_external_kind_without_policy_is_rejected_before_accept(env, entry):
    value = freeze_turn_request('external.context', turn_id='turn-no-policy', session_id='session-contract',
        operation_id='op-contract', idempotency_key='contract', project_id='alpha', created_at=DAY.isoformat(),
        text='', privacy={'mode':'local_only', 'allow_remote':False, 'pii':'none', 'consent_refs':[], 'retention':'session'},
        capability_request={'mode':'execute_exact_v1', 'capability_id':'external.context.execute', 'arguments':request()})
    del value['execution_policy']
    runtime = get_or_build_ai_runtime(SimpleNamespace(app=env.http.app), SimpleNamespace(root_dir=env.root))
    with pytest.raises(AIKernelContractError):
        if entry == 'validator':
            validate_turn_request(value)
        else:
            env.http.app.state.ai_turn_runner.accept_and_submit(value)
    assert env.http.app.state.ai_turn_store.get_request('turn-no-policy') is None


def document(env, project='alpha'):
    # Original intake/processing/confirmation with a fake external model channel;
    # actual domain services, provenance, SQLite and Kernel are untouched.
    identity, original = add_document(env, project=project, summary='完整摘要 😀', body='完整正文', original='合成原件正文')
    row, item = env.documents.read(identity), env.records.read('workspace_items', original)
    assert item.payload['status'] == 'confirmed' and item.payload['document_id'] == identity
    source = {'type':'original_item', 'id':original, 'project_id':project, 'revision':item.revision,
              'layer':'L0', 'windows':[]}
    selection = {'type': 'document', 'id': row['id'], 'revision': row['revision'], 'project_id': project,
                 'layer': 'L1', 'windows': []}
    return selection, source


def formal_snapshot(env):
    store = env.http.app.state.ai_turn_store
    return (tuple(store.events_after(TURN)), store.get_immutable_payload(TURN, ARCHIVE),
            env.records.list(DELIVERIES), env.records.list(USES),
            env.records.list('v2_usage_document'), env.records.list('v2_usage_insight'),
            env.records.list('v2_external_agent_reservations'))


def test_prepare_idempotent_full_archive_and_conflicting_selection_are_immutable(env):
    selection = material(env)
    api, _, _, frozen = prepare(env, selections=[selection])
    store = env.http.app.state.ai_turn_store
    before = formal_snapshot(env)
    assert api.prepare(TURN, request(), [selection], session_id='session-external',
        operation_id='op-external', idempotency_key=TURN, created_at=DAY.isoformat()) == frozen
    assert formal_snapshot(env) == before
    with pytest.raises(ValueError):
        api.prepare(TURN, request(), [{**selection, 'windows':[{'start':0, 'end':2}]}],
            session_id='session-external', operation_id='op-external', idempotency_key=TURN, created_at=DAY.isoformat())
    assert formal_snapshot(env) == before
    assert store.events_after(TURN)[-1]['type'] == 'turn.accepted'
    assert env.model.calls == 0


@pytest.mark.parametrize('conflict', ['kind', 'owner', 'query'])
def test_prepare_cannot_attach_archive_to_another_accepted_request(env, conflict):
    api, runtime, _ = setup(env)
    settings(env, allow_remote=True)
    selection = material(env)
    old = freeze_turn_request('project.answer' if conflict == 'kind' else 'external.context',
        turn_id=TURN, session_id='session-external', operation_id='op-external', idempotency_key=TURN,
        project_id='alpha', created_at=DAY.isoformat(), text='old accepted request',
        privacy={'mode':'local_only','allow_remote':False,'pii':'none','consent_refs':[],'retention':'session'},
        capability_request=None if conflict == 'kind' else {'mode':'execute_exact_v1',
            'capability_id':'external.context.execute', 'arguments':request(
                scope={'user_id':'other-owner','project_id':'alpha'} if conflict == 'owner'
                else {'user_id':'local-user','project_id':'alpha'})})
    runtime.accept_turn(old)
    store = env.http.app.state.ai_turn_store
    before = tuple(store.events_after(TURN))
    with pytest.raises(ValueError):
        api.prepare(TURN, request(), [selection], session_id='session-external',
            operation_id='op-external', idempotency_key=TURN, created_at=DAY.isoformat())
    assert encoded(store.get_request(TURN)) == encoded(old)
    assert store.events_after(TURN) == before
    assert store.get_immutable_payload(TURN, ARCHIVE) is None
    assert env.records.list(DELIVERIES) == env.records.list('v2_external_agent_reservations') == ()
    assert env.model.calls == 0


@pytest.mark.parametrize('change', ['settings', 'private', 'source'])
def test_pre_delivery_revoke_creates_no_tool_or_delivery(env, change):
    selection = material(env)
    api, runtime, runner, _ = prepare(env, selections=[selection])
    before = formal_snapshot(env)
    if change == 'settings':
        settings(env, allow_remote=False)
    elif change == 'private':
        set_private_project(env.records, 'alpha', True, 0)
    else:
        SourceEgressService(env.records).set_policy(WorkScope('local-user', 'alpha'),
            'original_item', selection['id'], 1, 0, [])
    with pytest.raises(ValueError):
        api.execute(TURN, runtime=runtime, runner=runner)
    assert formal_snapshot(env) == before
    assert env.model.calls == 0


def test_real_layers_usage_is_fact_after_delivery_deduplicated_across_numbers(env):
    selection, _ = document(env)
    api, runtime, runner, _ = prepare(env, selections=[selection, {**selection, 'layer':'L2'}])
    with pytest.raises(ValueError):
        api.report_use(TURN, ['M1'])
    assert env.records.list(USES) == env.records.list('v2_usage_document') == ()
    handoff = api.execute(TURN, runtime=runtime, runner=runner)
    assert [entry['layer'] for entry in handoff['entries']] == ['L1', 'L2']
    assert handoff['entries'][0]['excerpt'] == env.documents.markdown(selection['id'], revision=selection['revision'])
    assert handoff['entries'][1]['excerpt'] == '完整摘要 😀'
    settings(env, allow_remote=False)
    assert api.report_use(TURN, ['M1']) == {'turn_id':TURN, 'ids':['M1']}
    used = env.records.read('v2_usage_document', selection['id'])
    assert used.payload['count'] == 2
    assert used.payload['events'][-1]['kind'] == 'citation'
    assert api.report_use(TURN, ['M2']) == {'turn_id':TURN, 'ids':['M2']}
    assert env.records.read('v2_usage_document', selection['id']) == used
    assert env.records.read(USES, TURN).payload['ids'] == ['M1', 'M2']
    before = formal_snapshot(env)
    api.report_use(TURN, ['M1', 'M2'])
    assert formal_snapshot(env) == before
    assert env.model.calls == 0


@pytest.mark.parametrize('ids', [['M1', 'unknown'], ['M1', 'M1'], [{'id':'M1'}], 'M1'])
def test_invalid_citation_batch_cannot_partially_mark_usage(env, ids):
    selection, _ = document(env)
    api, runtime, runner, _ = prepare(env, selections=[selection])
    api.execute(TURN, runtime=runtime, runner=runner)
    before = formal_snapshot(env)
    with pytest.raises(ValueError):
        api.report_use(TURN, ids)
    assert formal_snapshot(env) == before


def test_usage_and_number_marker_rollback_together_on_real_sqlite_abort(env):
    selection, _ = document(env)
    api, runtime, runner, _ = prepare(env, selections=[selection])
    api.execute(TURN, runtime=runtime, runner=runner)
    before = formal_snapshot(env)
    with sqlite3.connect(env.records.database_path) as db:
        db.execute("CREATE TRIGGER reject_external_citation BEFORE INSERT ON crp_structured_records "
            "WHEN NEW.collection = 'v2_external_agent_citations' BEGIN SELECT RAISE(ABORT, 'synthetic'); END")
    with pytest.raises(ValueError, match='^external_context_unavailable$'):
        api.report_use(TURN, ['M1'])
    assert formal_snapshot(env) == before
    with sqlite3.connect(env.records.database_path) as db:
        db.execute('DROP TRIGGER reject_external_citation')
    api.report_use(TURN, ['M1'])
    assert env.records.read('v2_usage_document', selection['id']).payload['count'] == 2
    assert env.records.read(USES, TURN).payload['ids'] == ['M1']


def test_source_change_after_delivery_rejects_whole_citation_batch(env):
    selection, source = document(env)
    api, runtime, runner, _ = prepare(env, selections=[source, selection])
    api.execute(TURN, runtime=runtime, runner=runner)
    with env.records.begin() as tx:
        original = tx.read('workspace_items', source['id'])
        tx.put('workspace_items', source['id'], {**original.payload, 'source_text':'changed'}, expected_revision=original.revision)
        tx.commit()
    before = formal_snapshot(env)
    with pytest.raises(ValueError):
        api.report_use(TURN, ['M1', 'M2'])
    assert formal_snapshot(env) == before
    assert env.records.list(USES) == env.records.list('v2_usage_document') == ()


def test_real_recognition_conditions_profile_and_usage_owner(env):
    selection, _ = document(env)
    ids = []
    for project in ('alpha', 'me'):
        if project == 'alpha':
            experience = ensure_document_experience(env.documents, env.service, project, selection['id'])[0]
        else:
            persona, _ = document(env, project='me')
            experience = ensure_document_experience(env.documents, env.service, project, persona['id'])[0]
        candidate = env.service.propose(scope=WorkScope('local-user', project), content='完整认识正文',
            conditions=['限定条件必须保留'], source_experience_ids=[experience])
        recognition = env.service.publish(scope=WorkScope('local-user', project), candidate_id=candidate.id,
            expected_revision=1, reviewer='local-user')
        ids.append({'type':'recognition','id':recognition.id,'revision':recognition.revision,
                    'project_id':project,'layer':'L3','windows':[]})
    api, runtime, runner, _ = prepare(env, selections=ids)
    handoff = api.execute(TURN, runtime=runtime, runner=runner)
    for entry in [*handoff['entries'], *handoff['profile']]:
        assert entry['conditions'] == ['限定条件必须保留']
        assert '完整认识正文' in entry['excerpt'] and '限定条件必须保留' in entry['excerpt']
    assert handoff['profile'][0]['id'] == 'P1'
    api.report_use(TURN, ['M1', 'P1'])
    for selected in ids:
        used = env.records.read('v2_usage_insight', selected['id']).payload
        assert used['project_id'] == selected['project_id'] and used['count'] == 2
    assert env.model.calls == 0


def test_settings_receipt_uses_actual_completed_handoff_and_is_read_only(env):
    api, runtime, runner, _ = prepare(env)
    assert delivery_receipts(env.records, env.root) == []
    api.execute(TURN, runtime=runtime, runner=runner)
    before = formal_snapshot(env)
    rows = _receipts(env.records, 50, runtime_root=env.root)
    assert len(rows) == 1
    assert rows[0]['purpose'] == '外部 agent' and rows[0]['items'] == 1
    assert rows[0]['model'] is rows[0]['usage'] is rows[0]['model_cost'] is None
    assert formal_snapshot(env) == before
    missing = env.root / 'absent-root'
    assert delivery_receipts(env.records, missing) == []
    assert not missing.exists()


@pytest.mark.parametrize('change', ['missing', 'mode', 'capability', 'extra', 'bool', 'scope', 'client_type', 'tool_type'])
def test_external_builder_rejects_missing_or_invalid_exact_request(env, change):
    exact = {'mode':'execute_exact_v1', 'capability_id':'external.context.execute', 'arguments':request()}
    if change == 'missing':
        exact = None
    elif change == 'mode':
        exact['mode'] = 'execute_invented'
    elif change == 'capability':
        exact['capability_id'] = 'workbench.answer.execute'
    elif change == 'extra':
        exact['arguments']['usage_target'] = 'client-controlled'
    elif change == 'bool':
        exact['arguments']['budget'] = True
    elif change == 'scope':
        exact['arguments']['scope']['project_id'] = 'different'
    elif change == 'client_type':
        exact['arguments']['client'] = []
    else:
        exact['arguments']['tool'] = {}
    with pytest.raises(AIKernelContractError):
        freeze_turn_request('external.context', turn_id=TURN, session_id='session-contract',
            operation_id='op-contract', idempotency_key=TURN, project_id='alpha', created_at=DAY.isoformat(),
            text='', privacy={'mode':'local_only','allow_remote':False,'pii':'none','consent_refs':[],'retention':'session'},
            capability_request=exact)
    setup(env)
    assert env.http.app.state.ai_turn_store.get_request(TURN) is None
    assert env.records.list(DELIVERIES) == ()


def test_real_kernel_manifest_denial_prevents_invocation_reservation_and_delivery(env):
    from backend.security.project_capability_profiles import ProjectCapabilityProfileStore
    profiles = ProjectCapabilityProfileStore(env.root)
    old = profiles.get('alpha')
    profiles.update('alpha', expected_revision=old.store_revision,
        boundary_profile_id=old.profile.boundary_profile_id,
        boundary_profile_revision=old.profile.boundary_profile_revision,
        denied_tool_ids=('external.context.execute',))
    api, runtime, runner, _ = prepare(env)
    with pytest.raises(ValueError):
        api.execute(TURN, runtime=runtime, runner=runner)
    events = env.http.app.state.ai_turn_store.events_after(TURN)
    assert events[-1]['type'] == 'turn.failed'
    assert not any(event['type'] in {'tool.intent.recorded', 'tool.requested', 'tool.outcome.recorded'} for event in events)
    assert env.records.list('v2_external_agent_reservations') == env.records.list(DELIVERIES) == ()
    assert env.model.calls == 0


def test_whole_budget_windows_l0_markers_and_no_strength_invention(env):
    selection = material(env)
    api, runtime, runner, _ = prepare(env, selections=[{**selection, 'windows':[{'start':0, 'end':5}]}])
    delivered = api.execute(TURN, runtime=runtime, runner=runner)
    assert delivered['entries'][0]['excerpt'] == '完整且可定'
    api.report_use(TURN, ['M1'])
    assert env.records.read(USES, TURN).payload['objects'] == [{
        'type':'original_item','id':selection['id'],'revision':1,'project_id':'alpha','kind':None}]
    assert env.records.list('v2_usage_document') == env.records.list('v2_usage_insight') == ()
    other = 'turn-' + 'b' * 32
    api.prepare(other, request(budget=1), [selection], session_id='session-external',
        operation_id='op-external-small', idempotency_key=other, created_at=DAY.isoformat())
    tiny = api.execute(other, runtime=runtime, runner=runner)
    assert tiny == {'version':'handoff@1','budget':1,'tokens':0,'entries':[],'profile':[],'text':''}
    with pytest.raises(ValueError):
        api.report_use(other, ['M1'])
    assert env.records.read(USES, other) is None


def test_real_restarted_instances_serialize_one_citation_fact(env):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from fastapi.testclient import TestClient
    from tests.memory_app.v2.test_workbench_ask import assemble
    from core.storage_provider import SQLiteStructuredRecordStore
    from core.document_engine import SQLiteDocumentRepository
    from backend.memory_app.source_egress import recognition_service
    selection, _ = document(env)
    api, runtime, runner, _ = prepare(env, selections=[selection])
    api.execute(TURN, runtime=runtime, runner=runner)
    records = SQLiteStructuredRecordStore(env.records.database_path)
    app, _ = assemble(env.root, records, SQLiteDocumentRepository(records), recognition_service(records), env.model)
    with TestClient(app) as client:
        get_or_build_ai_runtime(SimpleNamespace(app=app), SimpleNamespace(root_dir=env.root))
        restarted = app.state.external_context
        barrier = Barrier(2)
        def report(service):
            barrier.wait(timeout=5)
            return service.report_use(TURN, ['M1'])
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert list(pool.map(report, (api, restarted))) == [{'turn_id':TURN, 'ids':['M1']}] * 2
    assert env.records.read(USES, TURN).revision == 1
    assert env.records.read('v2_usage_document', selection['id']).payload['count'] == 2
    assert len(env.records.list('v2_external_agent_reservations')) == 1
    assert sum(event['type'] == 'tool.intent.recorded'
        for event in env.http.app.state.ai_turn_store.events_after(TURN)) == 1


def test_delivery_index_failure_cannot_expose_body_and_completed_replay_is_safe(env):
    api, runtime, runner, _ = prepare(env)
    with sqlite3.connect(env.records.database_path) as db:
        db.execute("CREATE TRIGGER reject_external_delivery BEFORE INSERT ON crp_structured_records "
            "WHEN NEW.collection = 'v2_external_agent_deliveries' BEGIN SELECT RAISE(ABORT, 'synthetic'); END")
    with pytest.raises(ValueError, match='^external_context_unavailable$'):
        api.execute(TURN, runtime=runtime, runner=runner)
    store = env.http.app.state.ai_turn_store
    events = tuple(store.events_after(TURN))
    assert events[-1]['type'] == 'turn.completed'
    assert env.records.list(DELIVERIES) == ()
    with pytest.raises(ValueError):
        api.report_use(TURN, ['M1'])
    with sqlite3.connect(env.records.database_path) as db:
        db.execute('DROP TRIGGER reject_external_delivery')
    assert api.execute(TURN, runtime=runtime, runner=runner)['entries'][0]['id'] == 'M1'
    assert store.events_after(TURN) == events
    assert len(env.records.list('v2_external_agent_reservations')) == 1


@pytest.mark.parametrize('corruption', ['objects', 'unknown', 'duplicate', 'extra'])
def test_malformed_citation_marker_is_rejected_not_accepted_as_prior_fact(env, corruption):
    selection, _ = document(env)
    api, runtime, runner, _ = prepare(env, selections=[selection])
    api.execute(TURN, runtime=runtime, runner=runner)
    ref = env.http.app.state.ai_turn_store.get_immutable_payload(TURN, ARCHIVE)[0]
    payload = {'owner_id':'local-user', 'immutable_ref':ref, 'ids':['M1'], 'objects':[]}
    if corruption == 'unknown':
        payload['ids'] = ['unknown']
    elif corruption == 'duplicate':
        payload['ids'] = ['M1', 'M1']
    elif corruption == 'extra':
        payload['unexpected'] = True
    with env.records.begin() as tx:
        tx.put(USES, TURN, payload, expected_revision=0)
        tx.commit()
    before = formal_snapshot(env)
    with pytest.raises(ValueError, match='^external_context_binding_invalid$'):
        api.report_use(TURN, ['M1'])
    assert formal_snapshot(env) == before


def test_original_json_delete_recreation_invalidates_delivered_number(env):
    from backend.memory_app.original_sources import source_store
    _, item = document(env)
    row = env.records.read('workspace_items', item['id'])
    store = source_store(env.records)
    source = store.read('sources', row.payload['source_id'])
    selection = {'type':'original_source','id':source['id'],'revision':1,
                 'project_id':'alpha','layer':'L0','windows':[]}
    api, runtime, runner, _ = prepare(env, selections=[selection])
    handoff = api.execute(TURN, runtime=runtime, runner=runner)
    old_incarnation = handoff['entries'][0]['sources'][0]['incarnation']
    store.delete('sources', source['id'])
    store.write('sources', source['id'], source, expected_revision=0)
    changed = SourceEgressService(env.records).snapshot(WorkScope('local-user', 'alpha'),
        [{'type':'original_source', 'id':source['id'], 'revision':1}])
    node = next(node for node in changed['nodes'] if node['type'] == 'original_source')
    assert node['source_revision'] == 1 and node['incarnation'] != old_incarnation
    before = formal_snapshot(env)
    with pytest.raises(ValueError):
        api.report_use(TURN, ['M1'])
    assert formal_snapshot(env) == before
    assert env.records.list(USES) == env.records.list('v2_usage_document') == ()


def test_delivery_sidecar_alone_does_not_make_a_receipt(env):
    api, _, _, _ = prepare(env)
    store = env.http.app.state.ai_turn_store
    ref = store.get_immutable_payload(TURN, ARCHIVE)[0]
    with env.records.begin() as tx:
        tx.put(DELIVERIES, TURN, {'owner_id':'local-user','turn_id':TURN,'immutable_ref':ref,
            'outcome_ref':'crp://default/tool/absent','at':DAY.isoformat()}, expected_revision=0)
        tx.commit()
    before = formal_snapshot(env)
    assert delivery_receipts(env.records, env.root) == []
    with pytest.raises(ValueError):
        api.report_use(TURN, ['M1'])
    assert formal_snapshot(env) == before


def test_handoff_policy_version_is_frozen_before_actual_policy_return(env):
    import sys
    from backend.memory_app.v2 import policies
    from backend.memory_app.v2.policies import handoff
    from backend.memory_app.v2.budget import text_tokens
    old_version, old_profile = policies.ACTIVE['handoff'], sys.getprofile()
    assert old_version == '@1'
    @policies.register('handoff', '@91001')
    def narrower(entries, *, profile=(), count_tokens, budget=3000):
        return handoff.v1(entries, profile=profile, count_tokens=count_tokens, budget=1)
    observed = {'active':True, 'returns':0}
    def observe(frame, event, value):
        if observed['active'] and event == 'return' and frame.f_code is handoff.v1.__code__:
            observed['active'] = False
            observed['returns'] += 1
            policies.ACTIVE['handoff'] = '@91001'
    try:
        sys.setprofile(observe)
        api, _, _, frozen = prepare(env)
    finally:
        observed['active'] = False
        sys.setprofile(old_profile)
        policies.ACTIVE['handoff'] = old_version
    assert observed['returns'] == 1
    assert sys.getprofile() is old_profile and policies.ACTIVE['handoff'] == old_version
    archive = env.http.app.state.ai_turn_store.get_immutable_payload(TURN, ARCHIVE)[1]
    assert archive['handoff']['entries'][0]['excerpt'] == '完整且可定位的原文 😀'
    assert archive['handoff']['budget'] == 3000
    # The newly selected real implementation would omit this whole entry.
    assert narrower([{key:value for key,value in archive['handoff']['entries'][0].items() if key != 'id'}],
        count_tokens=text_tokens)['entries'] == []
    assert frozen['policy_versions']['handoff'] == old_version
    assert env.records.list('v2_external_agent_reservations') == () and env.model.calls == 0
