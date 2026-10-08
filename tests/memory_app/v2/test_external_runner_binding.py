"""运行器绑定真实交付与原 SQLite Turn，不调用模型或实际 agent。"""
from copy import deepcopy
import importlib
import json
from pathlib import Path

import pytest

from backend.memory_app.v2.external_adapters import build_launch_plan
from backend.memory_app.v2.external_workspace import create_task_workspace
from backend.memory_app.v2.privacy import set_private_project
from core.ai_kernel.turn_kinds import freeze_turn_request
from core.ai_tooling import tool_contract_identity
from core.ai_tooling.contracts import is_external_task_execution_contract
from tests.memory_app.v2.test_external_context import env, prepare, TURN, DAY
from tests.memory_app.v2.test_external_host import setup_host


def module():
    return importlib.import_module('backend.memory_app.v2.external_runner')


def test_native_definition_is_the_exact_parallel_never_retry_contract():
    capability = module().definition()
    tool = capability.tool_definition
    assert is_external_task_execution_contract(tool_contract_identity(tool))
    assert capability.requires_approval is False
    assert tool.resource_locks == ()
    assert tool.timeout_ms == 1_230_000
    for uri in (tool.input_schema_uri, tool.output_schema_uri, tool.receipt_schema_uri):
        name = uri.rsplit('/', 1)[1]
        schema = Path(__file__).resolve().parents[3] / 'core-contracts/ai' / name
        assert json.loads(schema.read_text(encoding='utf-8'))['additionalProperties'] is False


@pytest.fixture
def bound(env, setup_host):
    api, runtime, runner, _ = prepare(env)
    api.execute(TURN, runtime=runtime, runner=runner)
    delivery = api.qualified_delivery(TURN)
    _, host, _, old_plan, config, _, root, _ = setup_host()
    host.records = env.records
    turn_id = 'turn-' + 'b' * 32
    frozen = freeze_turn_request('project.task', template_version=2, turn_id=turn_id,
        session_id='session-runner', operation_id='operation-runner', idempotency_key=turn_id,
        project_id=delivery['project_id'], created_at=DAY.isoformat(), text='核查合成资料',
        privacy=delivery['privacy'], refs=delivery['refs'],
        capability_request={'mode':'execute_exact_v1','capability_id':'external.task.execute',
            'arguments':{'binding_ref':'crp://session/' + turn_id + '/external-task-run-v1'}})
    frozen['policy_versions'] = delivery['policy_versions']
    turns = env.http.app.state.ai_turn_store
    assert turns.claim_turn(frozen) == (turn_id, True)
    cwd = create_task_workspace(root, turn_id, task=frozen['input']['text'],
        handoff=delivery['handoff'], mcp_config=config)
    plan = build_launch_plan('codex', cli_version=old_plan.cli_version,
        executable=Path(old_plan.command[0]), cwd=cwd, task=frozen['input']['text'], mcp_config=config)
    owner = module().ExternalRunner(env.records, owner_id='local-user',
        context=api, host=host, turns=turns)
    return owner, frozen, plan, config, turns, delivery


def bind(values):
    owner, frozen, plan, config, *_ = values
    return owner.bind(frozen, delivery_turn_id=TURN, plan=plan, mcp_config=config)


def test_binding_has_real_delivery_proof_and_is_immutable(bound):
    owner, frozen, plan, config, turns, delivery = bound
    ref = bind(bound)
    saved = turns.get_immutable_payload(frozen['turn_id'], 'external-task-run-v1')
    assert ref == frozen['capability_request']['arguments']['binding_ref'] == saved[0]
    assert saved[1]['delivery'] == delivery
    assert saved[1]['request'] == frozen
    assert bind(bound) == ref
    assert owner.records.list('v2_external_runs') == ()


@pytest.mark.parametrize('change', ['request','context','task','mcp'])
def test_binding_rejects_changed_bytes_without_run_or_archive(bound, change):
    owner, frozen, plan, config, turns, _ = bound
    frozen, config = deepcopy(frozen), deepcopy(config)
    if change == 'request':
        frozen['input']['text'] = '改动的任务'
    elif change == 'context':
        (plan.cwd / 'CONTEXT.md').write_text('改动的材料', encoding='utf-8')
    elif change == 'task':
        (plan.cwd / 'TASK.md').write_text('改动的任务', encoding='utf-8')
    else:
        (plan.cwd / 'memory-mcp.json').write_text('{}', encoding='utf-8')
    with pytest.raises(module().ExternalRunnerError):
        owner.bind(frozen, delivery_turn_id=TURN, plan=plan, mcp_config=config)
    assert turns.get_immutable_payload(frozen['turn_id'], 'external-task-run-v1') is None
    assert owner.records.list('v2_external_runs') == ()


def test_missing_original_frozen_authority_cannot_invoke(bound):
    owner, frozen, *_ = bound
    bind(bound)
    with pytest.raises(module().ToolProviderFailure) as error:
        owner.invoke({'turn_id':frozen['turn_id']})
    assert error.value.effect_certainty == 'confirmed_none'
    assert owner.records.list('v2_external_runs') == ()


def test_fresh_private_project_rejects_previous_delivery_before_binding(bound):
    owner, frozen, *_ = bound
    set_private_project(owner.records, 'alpha', True, 0)
    with pytest.raises(module().ExternalRunnerError):
        bind(bound)
    assert owner.turns.get_immutable_payload(frozen['turn_id'], 'external-task-run-v1') is None
    assert owner.records.list('v2_external_runs') == ()


def test_other_record_owner_cannot_construct_runner(bound):
    owner, *_ = bound
    from core.storage_provider import SQLiteStructuredRecordStore
    other = SQLiteStructuredRecordStore(owner.host.deployment.user_root / 'other.sqlite3')
    with pytest.raises(module().ExternalRunnerError):
        module().ExternalRunner(other, owner_id='local-user', context=owner.context,
            host=owner.host, turns=owner.turns)


def test_version_probe_cannot_hide_a_revoked_delivery(bound, monkeypatch):
    owner, frozen, plan, *_ = bound
    original = owner.host.prepare

    def observed_probe(*args, **kwargs):
        lease = original(*args, **kwargs)
        set_private_project(owner.records, 'alpha', True, 0)
        return lease

    monkeypatch.setattr(owner.host, 'prepare', observed_probe)
    with pytest.raises(module().ExternalRunnerError):
        bind(bound)
    assert Path(plan.command[0]).with_name('version-called').read_text() == 'called'
    assert owner.turns.get_immutable_payload(frozen['turn_id'], 'external-task-run-v1') is None
    assert owner.records.list('v2_external_runs') == ()
