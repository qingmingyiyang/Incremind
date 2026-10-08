"""可信 Host 限额沿真实 lease/SQLite 接线；只执行合成 native CLI。"""
from concurrent.futures import ThreadPoolExecutor
from copy import copy
from dataclasses import replace
import gc
import io
import json
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import zipfile

import pytest

from backend.memory_app.v2.external_adapters import build_launch_plan
from backend.memory_app.v2.external_execution import execute_external
from backend.memory_app.v2.external_host import ExecutorRegistration, HostAdmission, HostLease, ExternalHostError
from backend.memory_app.v2.external_runs import ExternalRuns
from backend.shared.deployment import DeploymentLayout
from core.ai_kernel import SQLiteAITurnStore
from core.ai_kernel.turn_kinds import freeze_turn_request
from core.ai_kernel.dispatcher import ToolProviderFailure
from core.storage_provider import SQLiteStructuredRecordStore
from tests.memory_app.v2.test_external_dispatch import context


@pytest.fixture
def configured(tmp_path):
    from pip._vendor.distlib.resources import finder
    temporary = tempfile.TemporaryDirectory(prefix='T16.4-limits-', dir=tmp_path.anchor)
    root = Path(temporary.name)
    auth = root / 'authentication'; auth.mkdir()
    executable = root / 'synthetic.exe'
    source = '''import json,sys,time,os
from pathlib import Path
if sys.argv[1:]==['--version']:
 print('codex-cli 0.156.1'); sys.exit(0)
assert sys.stdin.buffer.read()=='合成任务'.encode('utf-8')
Path('observed-environment.json').write_text(json.dumps({key:os.environ.get(key) for key in ('HOME','USERPROFILE','CODEX_HOME')}))
with Path('spawn-count').open('a') as stream: stream.write('1')
Path('cli.pid').write_text(str(os.getpid()))
print(json.dumps({'type':'turn.started'}),flush=True)
while not Path('release').exists(): time.sleep(.01)
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':1,'output_tokens':1}}),flush=True)
'''
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w') as bundle: bundle.writestr('__main__.py', source)
    executable.write_bytes(finder('pip._vendor.distlib').find('t64.exe').bytes
        + ('#!"' + sys._base_executable + '" -I -S\n').encode() + archive.getvalue())
    records = SQLiteStructuredRecordStore(root / 'records.sqlite3')
    turns = SQLiteAITurnStore(root / 'turns.sqlite3')
    leases = []
    def host(**kwargs):
        return HostAdmission(deployment=DeploymentLayout('desktop', root), owner_id='local-user',
            records=records, registrations={'codex':ExecutorRegistration('codex', executable, auth)}, **kwargs)
    def task(owner, index, *, released=False):
        identity = 'turn-limit-' + str(index)
        turn = freeze_turn_request('project.task', template_version=2, turn_id=identity,
            session_id='session-limits', operation_id='operation-' + str(index), idempotency_key=identity,
            project_id='project-limits', created_at='2026-10-07T00:00:00Z', text='合成任务',
            privacy={'mode':'remote_allowed','allow_remote':True,'pii':'none','consent_refs':[], 'retention':'session'},
            capability_request={'mode':'execute_exact_v1','capability_id':'external.task.execute',
                'arguments':{'binding_ref':'crp://session/' + identity + '/external-task-run-v1'}})
        cwd = root / 'agent_workspaces' / identity; cwd.mkdir(parents=True)
        if released: (cwd / 'release').touch()
        config = {'mcpServers':{'chriptmas-memory':{'command':sys.executable,
            'args':['-I','-m','backend.memory_app.mcp']}}}
        plan = build_launch_plan('codex', cli_version='0.156.1', executable=executable,
            cwd=cwd, task=turn['input']['text'], mcp_config=config)
        lease = owner.prepare(turn, plan, mcp_config=config); leases.append(lease)
        # 该接点控制复用原执行 fixture 的真实冻结请求存储，不声称是 Runtime 准入控制。
        turns.claim_turn(turn)
        return SimpleNamespace(lease=lease, identity=identity, cwd=cwd, context=context(15000))
    yield SimpleNamespace(root=root, records=records, turns=turns, host=host, task=task)
    for lease in leases: lease.close()
    gc.collect()
    temporary.cleanup()


def execute(env, task, **kwargs):
    return execute_external(task.lease, task.context, records=env.records, turns=env.turns,
        turn_id=task.identity, owner_id='local-user', **kwargs)


def wait_for(predicate):
    deadline = time.monotonic() + 5
    while not predicate() and time.monotonic() < deadline: time.sleep(.01)
    assert predicate()


def claims(env):
    return env.records.read('v2_external_run_slots', 'local-user').payload['claims']


@pytest.mark.parametrize('limit', [1, 2])
def test_trusted_limit_controls_real_spawn_count_and_completion_releases_slots(configured, limit):
    env = configured
    host = env.host() if limit == 1 else env.host(concurrency_limit=limit)
    tasks = [env.task(host, index) for index in range(limit + 2)]
    with ThreadPoolExecutor(max_workers=limit) as pool:
        active = [pool.submit(execute, env, task) for task in tasks[:limit]]
        try:
            wait_for(lambda: all((task.cwd / 'spawn-count').exists() for task in tasks[:limit]))
            assert len(claims(env)) == limit
            assert all(ExternalRuns(env.records).read(task.identity, owner_id='local-user')['status'] == 'running'
                for task in tasks[:limit])
            with pytest.raises(ToolProviderFailure) as caught: execute(env, tasks[limit])
            assert caught.value.effect_certainty == 'confirmed_none'
            assert not (tasks[limit].cwd / 'spawn-count').exists()
            assert env.records.read('v2_external_runs', tasks[limit].identity) is None
        finally:
            for task in tasks[:limit]: (task.cwd / 'release').touch()
        for result in active: assert result.result(timeout=5)['receipt_ref']
    assert claims(env) == []
    for task in tasks[:limit]:
        assert (task.cwd / 'spawn-count').read_text() == '1'
        saved = env.turns.get_immutable_payload(task.identity, 'external-task-result-v1')
        assert execute(env, task)['payload_ref'] == saved[0]
        assert (task.cwd / 'spawn-count').read_text() == '1'
    (tasks[-1].cwd / 'release').touch()
    assert execute(env, tasks[-1])['receipt_ref']
    assert claims(env) == []


def test_cancellation_releases_only_its_slot_and_unknown_replay_never_restarts(configured):
    env = configured
    host = env.host(concurrency_limit=2)
    first, second, third = [env.task(host, index) for index in range(3)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        one, two = pool.submit(execute, env, first), pool.submit(execute, env, second)
        try:
            wait_for(lambda: (first.cwd / 'spawn-count').exists() and (second.cwd / 'spawn-count').exists())
            first.context.cancellation.request()
            with pytest.raises(ToolProviderFailure) as caught: one.result(timeout=5)
            assert caught.value.effect_certainty == 'unknown'
            row = ExternalRuns(env.records).read(first.identity, owner_id='local-user')
            assert row['status'] == 'cancelled' and row['started_at'] is not None
            assert [item['turn_id'] for item in claims(env)] == [second.identity]
            three = pool.submit(execute, env, third)
            wait_for(lambda: (third.cwd / 'spawn-count').exists())
            before = env.records.read('v2_external_run_slots', 'local-user')
            first.context = context()
            with pytest.raises(ToolProviderFailure) as replay: execute(env, first)
            assert replay.value.effect_certainty == 'unknown'
            assert (first.cwd / 'spawn-count').read_text() == '1'
            assert env.records.read('v2_external_run_slots', 'local-user') == before
        finally:
            for task in (first, second, third): (task.cwd / 'release').touch()
        assert two.result(timeout=5)['receipt_ref'] and three.result(timeout=5)['receipt_ref']
    assert claims(env) == []


@pytest.mark.parametrize('limit', [True,False,0,-1,1.5,'2',None])
def test_invalid_trusted_configuration_is_rejected_without_fallback(configured, limit):
    with pytest.raises(ExternalHostError, match='^external_host_concurrency_invalid$'):
        configured.host(concurrency_limit=limit)
    assert configured.records.list('v2_external_runs') == ()
    assert not list(configured.root.rglob('spawn-count'))


@pytest.mark.parametrize('change', ['host','host_bool','lease','lease_bool','copy','direct','records'])
def test_changed_or_unissued_lease_and_wrong_record_owner_never_spawn(configured, change):
    env = configured
    host = env.host()
    task = env.task(host, 1, released=True)
    lease, records = task.lease, env.records
    if change == 'host': host.concurrency_limit = 2
    elif change == 'host_bool': host.concurrency_limit = True
    elif change == 'lease': lease.concurrency_limit = 2
    elif change == 'lease_bool': lease.concurrency_limit = True
    elif change == 'copy': lease = copy(lease)
    elif change == 'direct':
        lease = HostLease(host, task.lease.accepted_turn, task.lease.plan, {}, (), task.lease._validate)
    else: records = SQLiteStructuredRecordStore(env.root / 'different.sqlite3')
    try:
        with pytest.raises(ToolProviderFailure) as caught:
            execute_external(lease, task.context, records=records, turns=env.turns,
                turn_id=task.identity, owner_id='local-user')
        assert caught.value.effect_certainty == 'confirmed_none'
        assert not (task.cwd / 'spawn-count').exists()
        assert env.records.list('v2_external_runs') == () and records.list('v2_external_runs') == ()
    finally:
        if lease is not task.lease: lease.close()


@pytest.mark.parametrize('change', ['host','lease'])
def test_before_launch_limit_change_is_rejected_and_reserved_slot_is_released(configured, change):
    env = configured
    host = env.host()
    task = env.task(host, 1, released=True)
    def late_change():
        if change == 'host': host.concurrency_limit = 2
        else: task.lease.concurrency_limit = 2
    with pytest.raises(ToolProviderFailure) as caught: execute(env, task, before_launch=late_change)
    assert caught.value.effect_certainty == 'confirmed_none'
    assert not (task.cwd / 'spawn-count').exists()
    assert claims(env) == []
    row = ExternalRuns(env.records).read(task.identity, owner_id='local-user')
    assert row['status'] == 'failed' and row['started_at'] is None


def test_unissued_close_preserves_signed_environment_and_real_owner_still_clears(configured):
    env = configured
    host = env.host(secret_environment={'APPROVED_DEVICE_KEY':'synthetic-limit-secret'})
    task = env.task(host, 1, released=True)
    lease = task.lease
    before, secrets, view = dict(lease.environment), lease.secret_values, lease.environment
    clone = copy(lease)
    with pytest.raises(ToolProviderFailure) as caught:
        execute_external(clone, task.context, records=env.records, turns=env.turns,
            turn_id=task.identity, owner_id='local-user')
    assert caught.value.effect_certainty == 'confirmed_none'
    clone.close()
    assert set(lease.environment) == set(before)
    for key, value in before.items(): assert lease.environment[key] == value
    assert lease.secret_values == secrets
    assert clone.environment == {} and clone.secret_values == ()
    lease.validate()
    assert execute(env, task)['receipt_ref']
    observed = json.loads((task.cwd / 'observed-environment.json').read_text())
    assert observed == {key:before[key] for key in ('HOME','USERPROFILE','CODEX_HOME')}
    assert observed['CODEX_HOME'] == str(env.root / 'authentication')
    lease.close()
    assert dict(view) == {} and lease.environment == {} and lease.secret_values == ()


@pytest.mark.parametrize('phase', ['entry','before_launch'])
def test_approved_plan_reference_cannot_be_replaced_before_spawn(configured, phase):
    env = configured
    task = env.task(env.host(), 1, released=True)
    (env.root / 'release').touch()
    changed = replace(task.lease.plan, cwd=env.root)
    def rebind(): task.lease.plan = changed
    if phase == 'entry': rebind()
    with pytest.raises(ToolProviderFailure) as caught:
        execute(env, task, before_launch=rebind if phase == 'before_launch' else None)
    assert caught.value.effect_certainty == 'confirmed_none'
    assert not (task.cwd / 'spawn-count').exists() and not (env.root / 'spawn-count').exists()
    if phase == 'entry': assert env.records.list('v2_external_runs') == ()
    else:
        assert claims(env) == []
        assert ExternalRuns(env.records).read(task.identity, owner_id='local-user')['started_at'] is None
