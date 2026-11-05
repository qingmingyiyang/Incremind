import asyncio
import io
import json
from pathlib import Path
import sys
import tempfile
from threading import Event
import time
from types import SimpleNamespace
import zipfile

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from backend.api.ai_turn_runner import AITurnRunner
from backend.memory_app.kernel.policy_runtime import ProductPolicyRuntime
from backend.memory_app.kernel.task_continuations import ProductStops
from backend.memory_app.v2.external_adapters import build_launch_plan
from backend.memory_app.v2.external_execution import execute_external
from backend.memory_app.v2.external_host import ExecutorRegistration, HostAdmission
from backend.memory_app.v2.external_runner import ARCHIVE, definition
from backend.memory_app.v2.external_runs import ExternalRuns
from backend.memory_app.v2.external_task_do import ExternalTaskDo
from backend.memory_app.v2.task_do import TASK_EXECUTIONS
from backend.memory_app.v2.task_drafts import TaskDrafts
from backend.memory_app.v2.signals import SignalService
from backend.memory_app.v2.workbench import install_workbench_routes
from backend.shared.deployment import DeploymentLayout
from core.ai_kernel import SQLiteAITurnStore, ScopedCapabilityRegistry
from core.ai_kernel.turn_kinds import freeze_turn_request
from core.ai_kernel.tool_invocation import outcome_from_payload
from core.document_engine.sqlite_runtime import SQLiteDocumentRepository
from core.storage_provider import SQLiteStructuredRecordStore
from tests.memory_app.v2.test_external_process import assert_tree_stopped


class LocalProcessExecutor:
    def __init__(self, records, turns, leases, barriers):
        self.records, self.turns, self.leases, self.barriers = records, turns, leases, barriers

    def invoke(self, request):
        identity = request['turn_id']
        def before_launch():
            if identity in self.barriers:
                entered, release = self.barriers[identity]
                entered.set()
                assert release.wait(15)
        return execute_external(self.leases[identity], request['execution_context'],
            records=self.records, turns=self.turns, turn_id=identity, owner_id='local-user', before_launch=before_launch)


@pytest.fixture
def local_execution(tmp_path):
    from pip._vendor.distlib.resources import finder

    temporary = tempfile.TemporaryDirectory(prefix='R5-process-', dir=tmp_path.anchor)
    tmp_path = Path(temporary.name)
    records = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    turns = SQLiteAITurnStore(tmp_path / 'turns.sqlite3')
    executable = tmp_path / 'controlled-cli.exe'
    grand = "import os,time; from pathlib import Path; Path('grand.pid').write_text(str(os.getpid())); time.sleep(90)"
    child = ("import os,sys,time,subprocess; from pathlib import Path; "
        "Path('child.pid').write_text(str(os.getpid())); "
        "subprocess.Popen([sys.executable, '-I', '-S', '-c', " + repr(grand) + "]); time.sleep(90)")
    source = '''import sys,os,subprocess,time
from pathlib import Path
if sys.argv[1:] == ['--version']:
 print('codex-cli 0.156.1'); sys.exit(0)
sys.stdin.buffer.read()
if Path('fail-now').exists(): sys.exit(7)
Path('tree.pid').write_text(str(os.getpid()))
subprocess.Popen([sys.executable, '-I', '-S', '-c', CHILD])
while not Path('grand.pid').exists(): time.sleep(.01)
print('{"type":"turn.started"}', flush=True)
time.sleep(90)
'''
    source = 'CHILD=' + repr(child) + '\n' + source
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w') as output:
        output.writestr('__main__.py', source)
    executable.write_bytes(finder('pip._vendor.distlib').find('t64.exe').bytes
        + ('#!"' + sys._base_executable + '" -I -S\n').encode() + archive.getvalue())
    auth = tmp_path / 'auth'
    auth.mkdir()
    host = HostAdmission(deployment=DeploymentLayout('desktop', tmp_path, None), owner_id='local-user',
        records=records, registrations={'codex': ExecutorRegistration('codex', executable, auth)}, concurrency_limit=2)
    leases, barriers = {}, {}
    registry = ScopedCapabilityRegistry()
    registry.register(definition(), LocalProcessExecutor(records, turns, leases, barriers))
    runtime = ProductPolicyRuntime(planner=None, registry=registry, events=turns, payloads=turns,
        state=turns, effect_runner=turns.effect_runner)
    runtime.task_continuations = None
    runtime.product_stops = ProductStops(records, turns, None)
    runtime.product_stops.runtime = runtime
    runner = AITurnRunner(runtime, max_workers=2)
    query = SimpleNamespace(answer_turns=SimpleNamespace(_runtime=lambda: runtime))
    application = FastAPI()
    application.state.ai_runtime, application.state.ai_turn_store = runtime, turns
    application.state.ai_turn_runner = runner
    application.state.external_context = SimpleNamespace(owner_id='local-user')
    application.state.external_execution_host = host
    application.state.memory_signals = SignalService(records)
    install_workbench_routes(application, records=records, models=None, documents=SQLiteDocumentRepository(records),
        service=None, workspace=SimpleNamespace(query=query))
    service = ExternalTaskDo(application, records, None,
        TaskDrafts(records, SQLiteDocumentRepository(records)), query)

    def prepare(name, *, crash=False):
        identity, product = 'turn-' + name, 'product-' + name
        request = freeze_turn_request('project.task', template_version=2, turn_id=identity,
            session_id='session-' + product, operation_id='op-' + name, idempotency_key='key-' + name,
            project_id='project-alpha', created_at='2026-10-24T00:00:00Z', text=json.dumps({'task': '检查进程'}),
            privacy={'mode':'remote_allowed', 'allow_remote':True, 'pii':'none', 'consent_refs':[], 'retention':'session'},
            capability_request={'mode':'execute_exact_v1', 'capability_id':'external.task.execute',
                'arguments': {'binding_ref':'crp://session/' + identity + '/external-task-run-v1'}})
        runtime.accept_turn(request)
        cwd = tmp_path / 'agent_workspaces' / identity
        cwd.mkdir(parents=True)
        if crash:
            (cwd / 'fail-now').touch()
        config = {'mcpServers': {'chriptmas-memory': {'command': sys.executable,
            'args': ['-I', '-m', 'backend.memory_app.mcp']}}}
        plan = build_launch_plan('codex', cli_version='0.156.1', executable=executable,
            cwd=cwd, task=request['input']['text'], mcp_config=config)
        leases[identity] = host.prepare(request, plan, mcp_config=config)
        turns.get_or_create_immutable_payload(identity, ARCHIVE, {'request':request, 'owner_id':'local-user',
            'turn_id':identity, 'launch': {'executor':'codex'}})
        with records.begin() as tx:
            tx.put('v2_turns', product, {'project_id':'project-alpha', 'intent':'do', 'thread_id':'thread-alpha',
                'receipt': {'do': {'state':'running', 'executor':'codex', 'kernel_turn_id':identity,
                    'title':'检查进程', 'progress':{'done':0, 'total':3}}}}, expected_revision=0)
            tx.put(TASK_EXECUTIONS, product, {'project_id':'project-alpha', 'request':request, 'task_text':'检查进程',
                'executor':'codex', 'started':True, 'owner':None, 'claimed_at':None, 'scene':None}, expected_revision=0)
            tx.commit()
        return request, product, cwd

    yield SimpleNamespace(records=records, turns=turns, runtime=runtime, runner=runner, host=host,
        service=service, prepare=prepare, barriers=barriers, client=TestClient(application))
    for _, release in barriers.values():
        release.set()
    host.close()
    runner.shutdown(timeout_seconds=10)
    import gc
    gc.collect()
    temporary.cleanup()


def wait_ready(folder, runner, identity):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if (folder / 'grand.pid').exists():
            return
        if runner.wait_for_terminal(identity, timeout_seconds=.01) is not None:
            break
        time.sleep(.02)
    raise AssertionError([(event['type'], event['data']) for event in runner._runtime.events_after(identity)])


def test_stop_closes_only_its_real_process_domain_and_is_idempotent(local_execution):
    env = local_execution
    first, first_product, first_folder = env.prepare('first')
    second, second_product, second_folder = env.prepare('second')
    env.runner.accept_and_submit(first)
    env.runner.accept_and_submit(second)
    wait_ready(first_folder, env.runner, first['turn_id'])
    wait_ready(second_folder, env.runner, second['turn_id'])
    assert env.runtime.product_stops.external_stop_closed(first['turn_id']) is False
    wrong_scope = env.client.post('/api/v2/workbench/turns/' + first_product + '/stop', json={'project_id':'project-other'})
    assert wrong_scope.status_code == 404
    stopped = env.client.post('/api/v2/workbench/turns/' + first_product + '/stop', json={'project_id':'project-alpha'})
    assert stopped.status_code == 202 and stopped.json()['state'] in {'stopping', 'stopped'}
    terminal = env.runner.wait_for_terminal(first['turn_id'], timeout_seconds=15)
    assert terminal is not None and terminal.status in {'cancelled', 'failed'}
    assert_tree_stopped(first_folder)
    assert env.runtime.product_stops.state(first['turn_id'], persist=True) == 'stopped'
    asyncio.run(env.service.advance(first_product, observe_only=True))
    receipt = env.records.read('v2_turns', first_product).payload['receipt']['do']
    assert receipt['state'] == 'stopped' and receipt['interruption'] == 'stopped'
    assert 'error' not in receipt
    assert env.runtime.product_stops.request(first_product, 'project-alpha', env.runner) == ('stopped', 1)
    repeated = env.client.post('/api/v2/workbench/turns/' + first_product + '/stop', json={'project_id':'project-alpha'})
    assert repeated.status_code == 202 and repeated.json() == {'state':'stopped'}
    continued = env.client.post('/api/v2/workbench/turns/' + first_product + '/continue',
        json={'project_id':'project-alpha'}, headers={'idempotency-key':'continue-external-stop'})
    assert continued.status_code == 409 and continued.json()['detail'] == 'external_task_continuation_unsupported'
    assert env.runner.wait_for_terminal(second['turn_id'], timeout_seconds=.05) is None
    assert ExternalRuns(env.records).read(second['turn_id'], owner_id='local-user')['status'] == 'running'
    env.runtime.product_stops.request(second_product, 'project-alpha', env.runner)
    assert env.runner.wait_for_terminal(second['turn_id'], timeout_seconds=15) is not None
    assert_tree_stopped(second_folder)


def test_stop_before_submission_never_launches_cli(local_execution):
    env = local_execution
    request, product, folder = env.prepare('unstarted')
    env.runtime.product_stops.request(product, 'project-alpha', env.runner)
    assert env.runner.wait_for_terminal(request['turn_id'], timeout_seconds=15).status == 'cancelled'
    assert not (folder / 'tree.pid').exists()
    assert env.records.read('v2_external_runs', request['turn_id']) is None
    outcomes = [event for event in env.turns.events_after(request['turn_id']) if event['type'] == 'tool.outcome.recorded']
    assert len(outcomes) == 1
    closed = outcome_from_payload(env.turns.get(outcomes[0]['data']['payload_ref']))
    assert closed.status == 'cancelled' and closed.effect_certainty == 'confirmed_none'
    assert env.runtime.product_stops.state(request['turn_id'], persist=True) == 'stopped'
    asyncio.run(env.service.advance(product, observe_only=True))
    assert env.records.read('v2_turns', product).payload['receipt']['do']['state'] == 'stopped'


def test_stop_during_preparation_waits_for_real_cleanup_before_reporting_stopped(local_execution):
    env = local_execution
    request, product, folder = env.prepare('preparing')
    entered, release = Event(), Event()
    env.barriers[request['turn_id']] = entered, release
    env.runner.accept_and_submit(request)
    assert entered.wait(15)
    try:
        assert env.runtime.product_stops.request(product, 'project-alpha', env.runner) == ('stopping', 1)
        assert env.runtime.product_stops.external_stop_closed(request['turn_id']) is False
        assert env.records.read('v2_turns', product).payload['receipt']['do']['state'] == 'running'
        assert ExternalRuns(env.records).read(request['turn_id'], owner_id='local-user')['status'] == 'reserved'
    finally:
        release.set()
    assert env.runner.wait_for_terminal(request['turn_id'], timeout_seconds=15) is not None
    assert not (folder / 'tree.pid').exists()
    assert env.runtime.product_stops.state(request['turn_id'], persist=True) == 'stopped'
    asyncio.run(env.service.advance(product, observe_only=True))
    assert env.records.read('v2_turns', product).payload['receipt']['do']['state'] == 'stopped'


def test_unrequested_process_failure_keeps_the_existing_failed_receipt(local_execution):
    env = local_execution
    request, product, folder = env.prepare('crashed', crash=True)
    env.runner.accept_and_submit(request)
    assert env.runner.wait_for_terminal(request['turn_id'], timeout_seconds=15).status == 'failed'
    assert env.runtime.product_stops.external_stop_closed(request['turn_id']) is False
    asyncio.run(env.service.advance(product, observe_only=True))
    receipt = env.records.read('v2_turns', product).payload['receipt']['do']
    assert receipt['state'] == 'failed' and receipt['error'] == 'external_task_interrupted'
    assert 'interruption' not in receipt
    assert ExternalRuns(env.records).read(request['turn_id'], owner_id='local-user')['exit_code'] == 7
