import os
from pathlib import Path
from shutil import copyfile
import sqlite3
from uuid import uuid4
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json

import pytest
from fastapi.testclient import TestClient


def _topology_event(store, request, kind):
    return {'schema_version': '1.0.0', 'event_id': 'event-' + uuid4().hex, 'turn_id': request['turn_id'],
        'session_id': request['session_id'], 'sequence': len(store.events_after(request['turn_id'])) + 1,
        'type': kind, 'actor': 'user' if kind == 'turn.steer.received' else 'kernel',
        'correlation': {'step_id': None, 'tool_call_id': None, 'model_request_id': None, 'operation_id': request['operation_id']},
        'data': {'status': 'accepted' if kind == 'turn.accepted' else 'running', 'summary': '真实分工读取检查',
            'capability_id': None, 'payload_ref': None, 'receipt_ref': None, 'evidence_refs': [],
            'error_code': None, 'retryable': False}, 'occurred_at': datetime.now(timezone.utc).isoformat()}


@pytest.mark.parametrize('bound_first', [True, False])
def test_actual_effective_assignments_reach_public_topology_without_model_execution(installed, bound_first):
    from backend.memory_app.v2 import TaskRuntimeProjection
    from core.ai_kernel.agent_contracts import AgentBudget, AgentBudgetReservation, AgentChildLink, AgentRun
    from core.ai_kernel.agent_dispatch_contracts import (
        AgentDispatchPlan, CapacitySnapshot, DispatchPermit, ExpertAssignment, ExpertCluster,
        IntakeRoutingReceipt, WorkloadSnapshot, canonical_dispatch_ref,
    )
    from backend.memory_app.workbench_identity_binding import capture_identity, prepare_admission
    from backend.memory_app.v2.inspirations import freeze_task_turn
    app, _, project = installed
    turns, composition = app.state.ai_turn_store, app.state.agent_runtime_composition
    dispatch, agents = composition.dispatch_store, composition.store
    suffix = uuid4().hex[:10]
    public = 'topology-' + suffix
    request = freeze_task_turn(records=app.state.recognition_records, models=app.state.recognition_models,
        query=app.state.workspace_domains.query, project_id=project, materials=(), load_text=lambda item: '',
        text=json.dumps({'task': '真实分工投影检查', 'division_examples': [], 'division_override': None}, ensure_ascii=False),
        local_only=True, turn_id=public, operation_id='operation-' + public, idempotency_key=public,
        session_id='session-' + public, created_at=datetime.now(timezone.utc).isoformat(), template_version=3)
    binding = capture_identity(app, app.state.recognition_records, app.state.recognition_documents,
        project=project, scene=None, thread_id='thread-' + public, turn_id=public)
    assert binding['part_turn_ids'] == [public]
    bundle = prepare_admission(app, request, binding, turns)
    with turns.admission_bundle(request, bundle):
        turns.claim_turn(request)
    turns.append(_topology_event(turns, request, 'turn.accepted'), expected_sequence=0)
    now = datetime.now(timezone.utc)
    lease = turns.try_acquire_run_lease(public, 'topology-sql-owner', now=now, stale_after=now + timedelta(minutes=5))

    def run(identity, profile_id, turn_id, *, parent=None):
        profile = composition.profiles.get(profile_id)
        value = AgentRun(identity, turn_id, project, profile_id, profile.revision, profile.role, profile.model_tier,
            'queued', 1 if parent else 0, 0, profile.budget_limit, profile.capability_ids,
            profile.max_concurrent_children, profile.max_depth, profile.max_steps, profile.timeout_ms,
            profile.allow_child_spawn, None, None, None, None, None, parent,
            profile.model_route_key, profile.model_route_revision)
        if parent:
            accepted_parent = agents.get_run_with_revision(parent, project_id=project)[0]
            return replace(value, budget_limit=AgentBudget(1, 1, 10, 10, 500), capability_ids=('memory.recall',),
                max_concurrent_children=0, allow_child_spawn=False,
                max_depth=min(value.max_depth, accepted_parent.max_depth),
                max_steps=min(value.max_steps, accepted_parent.max_steps),
                timeout_ms=min(value.timeout_ms, accepted_parent.timeout_ms))
        agents.register_run(value, operation_id='register-' + identity)
        return value

    main = run('main-' + suffix, 'main.orchestrator', public)
    budget = AgentBudget(2, 2, 20, 20, 1000)
    intake, workload, capacity, cluster = (name + '-' + suffix for name in ('intake', 'workload', 'capacity', 'cluster'))
    reference = lambda name: 'crp://dispatch/' + name + '/snapshot'
    dispatch.put_intake_receipt(IntakeRoutingReceipt(intake, project, public, 1, 'steward_required',
        reference('input'), reference('route'), reference('context-policy'), 1), operation_id='save-' + intake)
    dispatch.put_workload_snapshot(WorkloadSnapshot(workload, project, 1, 0, 0, budget), operation_id='save-' + workload)
    dispatch.put_capacity_snapshot(CapacitySnapshot(capacity, project, 1, 2, 2, budget), operation_id='save-' + capacity)
    expert_cluster = ExpertCluster(cluster, project, 1, (reference('expert'),), ())
    worker = composition.profiles.get('subagent.worker')

    def assignment(identity, goal, dependencies=()):
        payload = {'schema_version': '1.0.0', 'assignment_id': identity, 'task': goal,
            'expert': {'id': 'topology-expert'},
            'division': {'goal': goal, 'deliverable': '人工分工检查', 'depends_on': list(dependencies)}}
        body_ref = turns.get_or_create_immutable_payload(public, 'agent.dispatch.task.' + identity + '.v1', payload)
        return ExpertAssignment(identity, project, cluster, 1, 'crp://agent/profiles/subagent.worker', worker.revision,
            body_ref, 1, ('memory.recall',), 'topology-expert', (), reference('expert'), 1, None, None,
            reference('context-policy'), 1, AgentBudget(1, 1, 10, 10, 500))

    first, withdrawn = assignment('first-' + suffix, '保持已经绑定的原目标'), assignment('old-' + suffix, '尚未开始的旧目标')
    plan = AgentDispatchPlan('plan-' + suffix, project, main.run_id, 'steward-' + suffix, 1, 'draft', 'cluster',
        canonical_dispatch_ref('intake', intake), 1, canonical_dispatch_ref('workload', workload), 1,
        canonical_dispatch_ref('capacity', capacity), 1, canonical_dispatch_ref('cluster', cluster), 1,
        (first.assignment_id, withdrawn.assignment_id), budget, 1)
    permits = tuple(DispatchPermit('permit-' + row.assignment_id, project, plan.plan_id, 2,
        row.assignment_id, 'publish-' + suffix) for row in (first, withdrawn))
    ready, _ = dispatch.publish_ready_plan(draft=plan, cluster=expert_cluster,
        assignments=(first, withdrawn), permits=permits, operation_id='publish-' + suffix)
    child = None
    if bound_first:
        child_request = deepcopy(request)
        child_request.update(turn_id='child-turn-' + suffix, operation_id='child-operation-' + suffix,
            idempotency_key='child-key-' + suffix)
        child_request['input']['refs'] = []
        turns.claim_turn(child_request)
        child = run('child-' + suffix, 'subagent.worker', child_request['turn_id'], parent=main.run_id)
        spawn = 'spawn-' + suffix
        link = AgentChildLink('link-' + suffix, main.run_id, child.run_id, project, project, spawn,
            main.cancel_epoch, child.depth, child.budget_limit, child.capability_ids, 'reserved')
        reservation = AgentBudgetReservation('reservation-' + suffix, project, main.run_id, child.run_id,
            spawn, main.cancel_epoch, child.budget_limit, None, 'reserved')
        agents.reserve_spawn(parent=main, child=child, link=link, reservation=reservation)
        agents.finalize_spawn(link.link_id, operation_id='finalize-' + suffix, expected_cancel_epoch=0)
        dispatch.bind_permit_to_child_run(permits[0].permit_id, project_id=project,
            child_run_id=child.run_id, operation_id='bind-' + suffix)
    projection = TaskRuntimeProjection(runtime=app.state.ai_runtime, turn_store=turns,
        composition=composition, organization=app.state.agent_organization_runtime)
    before = projection.topology(public, project)
    assert [row['assignment_id'] for row in before] == [first.assignment_id, withdrawn.assignment_id]
    assert projection.empty_effective_division(public, project) is False

    def steer(key):
        accepted = _topology_event(turns, request, 'turn.steer.received')
        turns.accept_steer_bundle(accepted, expected_sequence=accepted['sequence'] - 1, run_lease=lease,
            submission_key=key, text='修改尚未开始的目标', snapshot={'answer_tail': ''},
            visible_sequence=None, maximum_count=5, policy_version='@2')
        row = turns.workbench_steers(public)[-1]
        applied = _topology_event(turns, request, 'turn.steer.applied')
        result = turns.apply_steer_input(applied, expected_sequence=applied['sequence'] - 1, run_lease=lease,
            steer_ids=[row['payload']['id']], kind='turn-steer-input-' + key,
            payload={'turn_id': public, 'project_id': project, 'steer_refs': [row['payload_ref']],
                'run_generation': lease.generation, 'run_owner_id': lease.owner_id, 'messages': []})
        return result.immutable_payload_ref

    new = assignment('new-' + suffix, '新的未开始目标', (first.assignment_id,) if bound_first else ())
    new_permit = replace(permits[1], permit_id='permit-' + new.assignment_id, assignment_id=new.assignment_id,
        operation_id='amend-' + suffix)
    options = dict(project_id=project, plan_id=ready.plan_id, main_run_id=main.run_id,
        expected_plan_revision=ready.revision,
        expected_effective_revision=dispatch.effective_snapshot(ready.plan_id, project_id=project).revision,
        expected_main_revision=agents.get_run_with_revision(main.run_id, project_id=project)[1],
        expected_cancel_epoch=0,
        replace_permit_ids=(permits[1].permit_id,) if bound_first else tuple(permit.permit_id for permit in permits),
        new_assignments=(new,),
        new_permits=(new_permit,), steer_input_ref=steer('replace-' + suffix),
        operation_id='amend-' + suffix, agent_store=agents)
    effective = dispatch.amend_unbound(**options)
    assert dispatch.amend_unbound(**options) == effective
    assert effective.plan.assignment_ids == plan.assignment_ids
    after = projection.topology(public, project)
    assert [row['assignment_id'] for row in after] == ([first.assignment_id] if bound_first else []) + [new.assignment_id]
    assert [row['goal'] for row in after] == (['保持已经绑定的原目标'] if bound_first else []) + ['新的未开始目标']
    if bound_first:
        assert after[0]['turn_id'] == child.turn_id and after[1]['depends_on'] == [0]
    assert after[-1]['turn_id'] is None and after[-1]['state'] == 'waiting'
    assert projection.topology(public, project) == after
    options.update(expected_effective_revision=effective.revision, replace_permit_ids=(new_permit.permit_id,),
        new_assignments=(), new_permits=(), steer_input_ref=steer('remove-' + suffix), operation_id='remove-' + suffix)
    dispatch.amend_unbound(**options)
    assert [row['assignment_id'] for row in projection.topology(public, project)] == ([first.assignment_id] if bound_first else [])
    assert projection.empty_effective_division(public, project) is (not bound_first)
    assert not any(event['type'].startswith('model.') for event in turns.events_after(public))


@pytest.fixture(scope='module')
def installed():
    root = (Path(os.environ['CHRIPTMAS_APP_ROOT']).resolve(strict=True) / ('i-' + uuid4().hex[:8]))
    root.mkdir(parents=True)
    previous = os.environ.get('CHRIPTMAS_APP_ROOT')
    os.environ['CHRIPTMAS_APP_ROOT'] = str(root)
    config = root / 'config/settings.toml'
    config.parent.mkdir()
    copyfile(Path(__file__).parents[3] / 'config/settings.toml.example', config)
    from backend.memory_app.app import create_app
    application = create_app(runtime_root=root)
    models = application.state.recognition_models
    models.update('generation', {'expected_revision': models.public()['generation']['revision'],
        'base_url': 'https://127.0.0.2:9/v1', 'model': 'gpt-4o-mini',
        'api_key': 'isolated-no-wire-placeholder', 'enabled': True, 'allow_remote': False})
    try:
        with TestClient(application) as client:
            response = client.post('/api/v2/projects', json={'name': '运行中补充隔离检查'})
            assert response.status_code == 200
            yield application, client, response.json()['id']
    finally:
        if previous is None:
            os.environ.pop('CHRIPTMAS_APP_ROOT', None)
        else:
            os.environ['CHRIPTMAS_APP_ROOT'] = previous


def test_installed_steer_routes_reject_unknown_owner_and_invalid_body(installed):
    application, client, project = installed
    assert application.state.product_steers.runtime is application.state.ai_runtime
    assert application.state.workspace_domains.query.outcome_freshness is application.state.product_outcome_freshness
    unknown = client.get('/api/v2/workbench/turns/missing/steer', params={'project_id': project})
    assert unknown.status_code == 404
    invalid = client.post('/api/v2/workbench/turns/missing/steer', headers={'Idempotency-Key': 'first'},
        json={'project_id': project, 'text': '补充', 'part': 0})
    assert invalid.status_code == 400
    missing = client.post('/api/v2/workbench/turns/missing/steer', headers={'Idempotency-Key': 'first'},
        json={'project_id': project, 'text': '补充'})
    assert missing.status_code == 404
    assert not application.state.product_steers._rows('missing')


def test_update_marker_requires_real_preparation_and_replays_same_refusal(installed):
    application, client, project = installed
    body = {'project_id': project, 'intent': 'do', 'executor': 'builtin', 'text': '更新合成成果',
        'continue_from': 'missing-document', 'context_ids': [
            {'kind': 'outcome_update', 'id': 'missing-preparation', 'revision': 1, 'project_id': project}]}
    first = client.post('/api/v2/workbench/turns', json=body, headers={'Idempotency-Key': 'g11-missing-update'})
    assert first.status_code == 409
    replay = client.post('/api/v2/workbench/turns', json=body, headers={'Idempotency-Key': 'g11-missing-update'})
    assert replay.status_code == first.status_code
    state = application.state.recognition_records.read('v2_turn_requests', 'g11-missing-update')
    assert state is not None and state.payload['state'] == 'failed' and state.payload['status_code'] == 409
    assert not application.state.recognition_records.list('v2_task_executions')
    path = Path(application.state.ai_turn_store._path).resolve()
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM ai_turn_events WHERE json_extract(event_json, '$.type') = ?",
            ('model.attempt.dispatched',)).fetchone()[0] == 0


def test_completed_empty_ask_retains_real_terminal_and_refuses_intervention(installed):
    application, client, project = installed
    response = client.post('/api/v2/workbench/turns', json={'project_id': project, 'intent': 'ask',
        'text': '这个隔离空项目有哪些已经确认的资料？'}, headers={'Idempotency-Key': 'g11-empty-answer'})
    assert response.status_code == 200, response.json()
    identity = response.json()['turn']['id']
    assert response.json()['turn']['receipt']['ask']['no_match'] is True
    from backend.memory_app.kernel.route_source_snapshot import require_closed_answer
    row = application.state.recognition_records.read('v2_turns', identity)
    assert require_closed_answer(application.state.recognition_records, row) == 'completed'
    projection = client.get('/api/v2/workbench/turns/' + identity + '/steer', params={'project_id': project})
    assert projection.status_code == 200 and projection.json() == {
        'steers': [], 'steer_target': None, 'steer_remaining': 5}
    refused = client.post('/api/v2/workbench/turns/' + identity + '/steer',
        headers={'Idempotency-Key': 'after-completion'}, json={'project_id': project, 'text': '以后调整写法'})
    assert refused.status_code == 409 and refused.json()['detail'] == 'turn_finished'
    assert not application.state.ai_turn_store.workbench_steers(identity)
    assert not any(event['type'].startswith('model.') for event in application.state.ai_turn_store.events_after(identity))


def test_document_material_uses_real_retained_roots_and_rejects_unconsumed_native_owner(installed):
    application, client, project = installed
    from core.document_engine.ports import DocumentDraft
    from backend.recognition import WorkScope
    from backend.recognition.product_draft_dependencies import (
        _material_document, ProductDraftDependencies, ProductDraftDependencyError)
    from backend.memory_app.original_sources import resolve_turn_material
    from backend.memory_app.source_egress import SourceEgressService
    from backend.memory_app.v2.outcome_freshness import OutcomeFreshnessError
    records = application.state.recognition_records
    item = application.state.workspace_domains.items.create(project, 'text', '合成原件', '交付日期为六月')
    document = application.state.recognition_documents.create(DocumentDraft(title='合成资料',
        document_type='note', markdown='交付日期为六月', project_id=project,
        source_refs=({'source_id': item['id'], 'locator': 'workspace://' + item['id']},)))
    material = {'type': 'document', 'id': document['id'], 'revision': document['revision'], 'project_id': project}
    scope = WorkScope('local-user', project)
    _, roots = resolve_turn_material(records, scope, material)
    snapshot = SourceEgressService(records).snapshot(scope, roots)
    inherited, retained = _material_document(records, scope, material, snapshot)
    assert inherited == [(project, 'original_item', item['id'], 1)]
    assert retained['markdown'] == '交付日期为六月'
    altered = deepcopy(snapshot)
    altered['roots'][0]['id'] = 'different-source'
    with pytest.raises(ProductDraftDependencyError, match='document roots changed'):
        _material_document(records, scope, material, altered)
    response = client.post('/api/v2/workbench/turns', json={'project_id': project, 'intent': 'ask',
        'text': '这个隔离空项目有哪些已经确认的资料？'}, headers={'Idempotency-Key': 'g11-empty-answer'})
    assert response.status_code == 200
    native = application.state.ai_turn_store.get_request(response.json()['turn']['id'])
    draft = ProductDraftDependencies((), {}, (), native, material_documents=(retained,))
    with records.begin() as tx:
        with pytest.raises(OutcomeFreshnessError, match='document_native_owner_changed'):
            records.product_task_document_validator(tx, draft)
        tx.rollback()


def test_document_source_matching_rejects_another_revision_of_same_birth(installed):
    application, _, project = installed
    from core.document_engine.ports import DocumentDraft
    from backend.memory_app.v2.outcome_freshness import _matching_document_sources
    item = application.state.workspace_domains.items.create(project, 'text', '修订原件', '交付日期为六月')
    documents = application.state.recognition_documents
    document = documents.create(DocumentDraft(title='修订资料', document_type='note', markdown='交付日期为六月',
        project_id=project, source_refs=({'source_id': item['id'], 'locator': 'workspace://' + item['id']},)))
    freshness = application.state.product_outcome_freshness
    material = {'type': 'document', 'id': document['id'], 'revision': 1, 'project_id': project}
    proof = freshness._freeze_material(freshness._source_owner(project),
        {**material, 'role': 'evidence', 'proof_ref': 'real-document-revision'})
    assert _matching_document_sources(proof, material, '交付日期为六月') == proof
    documents.save_user_edit(document['id'], markdown='交付日期为七月', expected_revision=1)
    current = {**material, 'revision': 2}
    new_proof = freshness._freeze_material(freshness._source_owner(project),
        {**current, 'role': 'evidence', 'proof_ref': 'real-document-revision-two'})
    assert proof[0]['frozen']['incarnation'] == new_proof[0]['frozen']['incarnation']
    assert _matching_document_sources(proof, current, '交付日期为七月') == []
    assert _matching_document_sources(new_proof, current, '交付日期为七月') == new_proof
    assert _matching_document_sources(new_proof, current, '另一段正文') == []


def test_historical_original_privacy_survives_document_source_replacement(installed):
    application, _, project = installed
    from core.document_engine.ports import DocumentDraft
    from backend.recognition import WorkScope
    from backend.memory_app.source_egress import SourceEgressService
    from backend.memory_app.v2.outcome_freshness import OutcomeFreshnessError
    from backend.memory_app.v2.outcomes import update_materials
    records, documents = application.state.recognition_records, application.state.recognition_documents
    first = application.state.workspace_domains.items.create(project, 'text', '原有依据', '交付日期为六月')
    second = application.state.workspace_domains.items.create(project, 'text', '当前依据', '交付日期为七月')
    document = documents.create(DocumentDraft(title='替换依据资料', document_type='note', markdown='交付日期为六月',
        project_id=project, source_refs=({'source_id': first['id'], 'locator': 'workspace://' + first['id']},)))
    freshness = application.state.product_outcome_freshness
    proof = freshness._freeze_material(freshness._source_owner(project), {'type': 'document',
        'id': document['id'], 'revision': 1, 'project_id': project, 'role': 'evidence', 'proof_ref': 'real-old-original'})
    documents.save_user_edit(document['id'], markdown='交付日期为七月', expected_revision=1,
        source_refs=({'source_id': second['id'], 'locator': 'workspace://' + second['id']},))
    SourceEgressService(records).set_policy(WorkScope('local-user', project), 'original_item', first['id'], 1, 0, [])
    with freshness._read_scope() as view:
        qualified = freshness._qualify_source_proofs(proof, reader=view, local_only=True)
        assert qualified['complete'] is True and qualified['privacy_ceiling']['effective_purposes'] == []
        current = qualified['privacy_ceiling']['sources'][0]
        assert any(root['id'] == first['id'] for snapshot in current['history_current'] for root in snapshot['roots'])
        with pytest.raises(OutcomeFreshnessError, match='private_source_remote_blocked'):
            freshness._qualify_source_proofs(proof, reader=view, local_only=False)
    prepared = update_materials({'current_sources': [], 'privacy_ceiling': qualified['privacy_ceiling']})
    assert all(material['type'] != 'document' for material in prepared)
    assert {first['id'], second['id']} <= {material['id'] for material in prepared}
