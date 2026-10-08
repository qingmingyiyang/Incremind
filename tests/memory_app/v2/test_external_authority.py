"""当轮批准复用真实归档、项目修订和原扫描器，不代替完整应用接线。"""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import importlib
from types import SimpleNamespace

import pytest

from backend.api.ai_profile_resolvers import (
    ProjectAwareCapabilityManifestResolver,
    ProjectAwareContextManifestResolver,
    TurnProjectProfileSnapshotAuthority,
)
from backend.memory_app.v2.external_runner import ARCHIVE, definition
from backend.memory_app.v2.external_adapters import build_launch_plan
from backend.memory_app.v2.external_workspace import create_task_workspace
from backend.memory_app.v2.privacy import set_private_project
from backend.security.ai_tool_execution_boundary import AIToolExecutionBoundary, TurnCapabilityBindingGuard
from backend.security.project_boundary_profiles import ProjectBoundaryProfileStore
from backend.security.project_capability_profiles import ProjectCapabilityProfileStore
from backend.security.turn_boundary_adapter import TurnBoundaryRequestFactory
from core.ai_boundary import BoundaryGrant, SanitizationResult, ScanSummary
from core.ai_kernel import context_manifest_to_payload, manifest_to_payload
from core.ai_tooling import tool_destination_identity
from tests.memory_app.v2.test_external_context import env, prepare as prepare_context
from tests.memory_app.v2.test_external_host import setup_host
from tests.memory_app.v2.test_external_runner_binding import bound


def module():
    return importlib.import_module('backend.memory_app.v2.external_authority')


def test_authority_export_is_callable():
    assert callable(module().ExternalExecutionAuthority)


@pytest.fixture
def case(bound, env, request):
    runner, frozen, plan, config, turns, delivery = bound
    scenario = getattr(request, 'param', None)
    if scenario in {'email_task', 'hard_task', 'email_handoff'}:
        frozen = deepcopy(frozen)
        identity = 'turn-' + 'c' * 32
        if scenario == 'email_handoff':
            item = env.domains.items.create('alpha', 'text', '合成联系资料',
                '合成联系地址 synthetic-authority@example.test')
            selection = {'type':'original_item', 'id':item['id'], 'project_id':'alpha',
                         'revision':1, 'layer':'L0', 'windows':[]}
            api, runtime, executor, delivery_request = prepare_context(env,
                identity='turn-' + 'd' * 32, selections=[selection])
            api.execute(delivery_request['turn_id'], runtime=runtime, runner=executor)
            delivery = api.qualified_delivery(delivery_request['turn_id'])
            frozen['privacy'] = deepcopy(delivery['privacy'])
            frozen['input']['refs'] = deepcopy(delivery['refs'])
            frozen['policy_versions'] = deepcopy(delivery['policy_versions'])
        else:
            frozen['input']['text'] = ('合成地址 synthetic-authority@example.test'
                if scenario == 'email_task' else '合成证件 ' + '12345678901234567' + '8')
        frozen['turn_id'] = identity
        frozen['idempotency_key'] = identity
        frozen['capability_request']['arguments'] = {
            'binding_ref':'crp://session/' + identity + '/external-task-run-v1'}
        assert turns.claim_turn(frozen) == (identity, True)
        cwd = create_task_workspace(runner.host.deployment.user_root, identity,
            task=frozen['input']['text'], handoff=delivery['handoff'], mcp_config=config)
        plan = build_launch_plan('codex', cli_version=plan.cli_version,
            executable=runner.host.registrations['codex'].executable, cwd=cwd,
            task=frozen['input']['text'], mcp_config=config)
        bound = runner, frozen, plan, config, turns, delivery
    if scenario == 'legacy_archive':
        legacy = {'schema_version':'1.0.0','owner_id':runner.owner_id,'turn_id':frozen['turn_id'],
            'request':frozen,'delivery':delivery,'mcp_config':config,'host_permission_refs':None,'memory_proof':None,
            'launch':{'executor':plan.executor,'cli_version':plan.cli_version,'preset':plan.preset,
                'commands':plan.requested_commands,'cwd':str(plan.cwd)}}
        ref = turns.get_or_create_immutable_payload(frozen['turn_id'], ARCHIVE, legacy)
    else:
        ref = runner.bind(frozen, delivery_turn_id=delivery['turn_id'], plan=plan, mcp_config=config)
    assert runner.context.runtime.accept_turn(frozen).status == 'accepted'
    profiles = ProjectBoundaryProfileStore(env.root)
    capabilities = ProjectCapabilityProfileStore(env.root)
    snapshots = TurnProjectProfileSnapshotAuthority(capabilities, profiles)
    capability = definition()
    manifest = ProjectAwareCapabilityManifestResolver(snapshots).resolve(frozen, [capability])
    manifest_ref = turns.put(frozen['turn_id'], 'capability-manifest', manifest_to_payload(manifest))
    context = ProjectAwareContextManifestResolver(snapshots).resolve(frozen, manifest_ref, manifest)
    context_ref = turns.put(frozen['turn_id'], 'context-manifest', context_manifest_to_payload(context))
    events = turns.events_after(frozen['turn_id'])
    sequence = events[-1]['sequence'] if events else 0
    turns.append({
        'schema_version':'1.0.0', 'event_id':'event-authority-context',
        'turn_id':frozen['turn_id'], 'session_id':frozen['session_id'], 'sequence':sequence + 1,
        'type':'context.resolved', 'actor':'ai-kernel',
        'correlation':{'step_id':None, 'tool_call_id':None, 'model_request_id':None,
                       'operation_id':frozen['operation_id']},
        'data':{'status':'running', 'summary':'', 'capability_id':None, 'payload_ref':context_ref,
                'receipt_ref':None, 'evidence_refs':[], 'error_code':None, 'retryable':False},
        'occurred_at':frozen['created_at'],
    }, expected_sequence=sequence)
    guard = TurnCapabilityBindingGuard(capabilities, profiles, events=turns, payloads=turns)
    boundary = AIToolExecutionBoundary(profiles, binding_guard=guard)
    sanitization = SanitizationResult('clean', '', ScanSummary((), ()), 0)
    request = TurnBoundaryRequestFactory(profiles).evaluate(frozen, capability,
        destination_id=tool_destination_identity(capability.tool_definition, capability.capability_id),
        sanitization=sanitization).request
    authority = module().ExternalExecutionAuthority(runner=runner, boundary_profiles=profiles,
        turn_binding_guard=guard, execution_boundary=boundary)
    return SimpleNamespace(runner=runner, frozen=frozen, capability=capability, request=request,
        profiles=profiles, guard=guard, boundary=boundary, authority=authority, turns=turns,
        records=env.records, env=env, ref=ref, config=config, plan=plan, delivery=delivery)


def test_real_archive_grants_exact_original_profile_without_writing(case):
    profile = case.profiles.get(case.request.project_id)
    assert profile.store_revision == 0 and profile.profile.revision == 1
    facts = case.records.list_all()
    events = case.turns.events_after(case.request.turn_id)
    saved = case.turns.get_immutable_payload(case.request.turn_id, ARCHIVE)
    grant = case.authority(case.frozen, case.capability, case.request)
    assert isinstance(grant, BoundaryGrant)
    assert grant.grant_id == 'external-execution-' + case.request.turn_id
    assert grant.subject_id == case.request.actor_id
    assert grant.project_id == case.request.project_id and grant.target_id == case.request.target_id
    assert grant.actions == (case.request.effect,) and grant.destinations == (case.request.destination_kind,)
    assert grant.data_classes == case.request.data_classes and grant.revision == 1
    assert grant.revoked is False and grant.redaction_required is False
    assert grant.expires_at.utcoffset().total_seconds() == 0
    assert grant.is_active_at(datetime.now(timezone.utc))
    result = TurnBoundaryRequestFactory(case.profiles,
        external_execution_authority=case.authority).evaluate(case.frozen, case.capability,
        destination_id=case.request.destination_id,
        sanitization=SanitizationResult('clean', '', ScanSummary((), ()), 0))
    assert result.decision.outcome == 'allow'
    assert result.decision.matched_grant_ids == (grant.grant_id,)
    assert case.records.list_all() == facts and case.profiles.get(case.request.project_id) == profile
    assert case.turns.events_after(case.request.turn_id) == events
    assert case.turns.get_immutable_payload(case.request.turn_id, ARCHIVE) == saved


@pytest.mark.parametrize('change', ['missing_archive', 'request_bytes', 'archive_owner', 'delivery_bytes'])
def test_unproven_archive_has_no_grant(case, change):
    request = deepcopy(case.frozen)
    boundary_request = case.request
    if change == 'request_bytes':
        request['input']['text'] += ' 更换'
    elif change == 'delivery_bytes':
        path = case.plan.cwd / 'CONTEXT.md'
        path.write_text(path.read_text(encoding='utf-8') + ' 更换', encoding='utf-8')
    else:
        request['turn_id'] = 'turn-' + 'e' * 32
        request['idempotency_key'] = request['turn_id']
        request['capability_request']['arguments'] = {
            'binding_ref':'crp://session/' + request['turn_id'] + '/external-task-run-v1'}
        assert case.turns.claim_turn(request) == (request['turn_id'], True)
        boundary_request = replace(boundary_request, turn_id=request['turn_id'],
            idempotency_key=request['idempotency_key'],
            request_id='boundary-' + request['turn_id'] + '-' + case.capability.tool_definition.tool_id)
        if change == 'archive_owner':
            archive = deepcopy(case.turns.get_immutable_payload(case.request.turn_id, ARCHIVE)[1])
            archive.update(owner_id='another-user', turn_id=request['turn_id'], request=request)
            case.turns.get_or_create_immutable_payload(request['turn_id'], ARCHIVE, archive)
    assert case.authority(request, case.capability, boundary_request) is None
    assert case.records.list('v2_external_runs') == ()


def test_fresh_private_delivery_has_no_grant(case):
    set_private_project(case.records, case.request.project_id, True, 0)
    assert case.authority(case.frozen, case.capability, case.request) is None
    assert case.records.list('v2_external_runs') == ()


def test_profile_revision_drift_has_no_grant(case):
    profile = case.profiles.get(case.request.project_id)
    case.profiles.update(case.request.project_id, mode='guarded', remote_default='review',
        expected_revision=profile.store_revision)
    # 首次持久写仍是 r1；再保存形成与真实冻结 manifest 不同的 r2。
    case.profiles.update(case.request.project_id, mode='guarded', remote_default='review', expected_revision=1)
    assert case.profiles.get(case.request.project_id).profile.revision == 2
    assert case.authority(case.frozen, case.capability, case.request) is None


@pytest.mark.parametrize('field', ['turn_id', 'project_id', 'operation_id', 'target_id'])
def test_other_boundary_identity_has_no_grant(case, field):
    request = replace(case.request, **{field:'other-identity'})
    assert case.authority(case.frozen, case.capability, request) is None


def test_missing_runner_is_closed_before_any_archive_read(case):
    authority = module().ExternalExecutionAuthority(boundary_profiles=case.profiles,
        turn_binding_guard=case.guard, execution_boundary=case.boundary)
    assert authority(case.frozen, case.capability, case.request) is None
    authority.runner = case.runner
    # 合成控制沿真实方法传播，诊断只记录固定错误类型和代码。
    errors = []
    original = authority._grant
    def observed(*args):
        try:
            return original(*args)
        except Exception as error:
            errors.append((type(error).__name__, str(error)))
            raise
    authority._grant = observed
    assert isinstance(authority(case.frozen, case.capability, case.request), BoundaryGrant), errors


@pytest.mark.parametrize('case', ['email_task', 'hard_task', 'email_handoff'], indirect=True)
def test_original_scanner_rejects_changed_or_hard_blocked_body(case):
    archive = case.turns.get_immutable_payload(case.request.turn_id, ARCHIVE)
    material = {'TASK':case.frozen['input']['text'], 'handoff':case.delivery['handoff']}
    sanitized = case.boundary.sanitize_candidate_arguments(case.capability, material,
        turn_id=case.request.turn_id)
    assert sanitized is None or sanitized != material
    assert case.authority(case.frozen, case.capability, case.request) is None
    assert case.turns.get_immutable_payload(case.request.turn_id, ARCHIVE) == archive
    assert case.records.list('v2_external_runs') == ()


@pytest.mark.parametrize('change', ['private', 'profile_revision'])
def test_host_probe_cannot_hide_late_authority_change(case, monkeypatch, change):
    from threading import Event, Thread

    original = case.runner.host.prepare
    closed_leases, workers = [], []
    completed = Event()
    def observed(*args, **kwargs):
        lease = original(*args, **kwargs)
        closed_leases.append(lease)
        def mutate():
            if change == 'private':
                set_private_project(case.records, case.request.project_id, True, 0)
            else:
                case.profiles.update(case.request.project_id, mode='guarded', remote_default='review',
                    expected_revision=0)
                case.profiles.update(case.request.project_id, mode='guarded', remote_default='review',
                    expected_revision=1)
            completed.set()
        worker = Thread(target=mutate)
        workers.append(worker)
        worker.start()
        assert completed.wait(5), '原 profile 或 records 锁不得跨版本探测持有'
        return lease
    monkeypatch.setattr(case.runner.host, 'prepare', observed)
    try:
        assert case.authority(case.frozen, case.capability, case.request) is None
        assert completed.is_set()
        assert closed_leases and all(lease.environment == {} for lease in closed_leases)
        assert case.records.list('v2_external_runs') == ()
    finally:
        for lease in closed_leases:
            lease.close()
        for worker in workers:
            worker.join(5)
        assert all(not worker.is_alive() for worker in workers)


@pytest.mark.parametrize('case', ['legacy_archive'], indirect=True)
def test_original_workspace_archive_keeps_real_authority_and_material_guards(case):
    saved = case.turns.get_immutable_payload(case.request.turn_id, ARCHIVE)
    assert set(saved[1]['launch']) == {'executor','cli_version','preset','commands','cwd'}
    grant = case.authority(case.frozen, case.capability, case.request)
    assert isinstance(grant, BoundaryGrant)
    assert grant.revision == case.profiles.get(case.request.project_id).profile.revision
    assert case.turns.get_immutable_payload(case.request.turn_id, ARCHIVE) == saved
    (case.plan.cwd / 'TASK.md').write_bytes(b'changed-original-task')
    assert case.authority(case.frozen, case.capability, case.request) is None
    assert case.turns.get_immutable_payload(case.request.turn_id, ARCHIVE) == saved
    assert case.records.list('v2_external_runs') == ()


@pytest.mark.parametrize('case', ['legacy_archive'], indirect=True)
def test_original_workspace_mcp_keeps_semantic_validation_and_real_authority(case):
    import json

    saved = case.turns.get_immutable_payload(case.request.turn_id, ARCHIVE)
    path = case.plan.cwd / 'memory-mcp.json'
    # 原 MCP 文件允许合法 JSON 排版差异，仍沿原白名单和冻结配置判断。
    path.write_text(json.dumps(case.config, ensure_ascii=False, indent=4) + '\n', encoding='utf-8')
    assert isinstance(case.authority(case.frozen, case.capability, case.request), BoundaryGrant)
    changed = deepcopy(case.config)
    changed['mcpServers']['chriptmas-memory']['args'].append('--unapproved')
    path.write_text(json.dumps(changed, ensure_ascii=False), encoding='utf-8')
    assert case.authority(case.frozen, case.capability, case.request) is None
    assert case.turns.get_immutable_payload(case.request.turn_id, ARCHIVE) == saved
    assert case.records.list('v2_external_runs') == ()
