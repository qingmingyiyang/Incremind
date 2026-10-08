"""原应用的装配与依赖拒绝；不代替尚未合入的记忆 MCP 服务。"""
from pathlib import Path
import sys
import tempfile
import gc
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.memory_app.kernel.ai_runtime import get_or_build_ai_runtime
from backend.memory_app.v2.external_host import ExecutorRegistration, HostAdmission
from backend.memory_app.v2.external_runner import ExternalRunnerError
from backend.shared.deployment import DeploymentLayout
from tests.memory_app.v2.test_external_context import env, prepare, TURN, DAY
from tests.memory_app.v2.test_workbench_ask import assemble, Model
from backend.recognition import RecognitionService
from core.document_engine import SQLiteDocumentRepository
from core.storage_provider import SQLiteStructuredRecordStore


@pytest.fixture
def host_env(tmp_path):
    temporary = tempfile.TemporaryDirectory(prefix='T16.4-app-', dir=tmp_path.anchor)
    root = Path(temporary.name)
    assert root.parent == Path(tmp_path.anchor) and root.name.startswith('T16.4-app-')
    records = SQLiteStructuredRecordStore(root / 'records.sqlite3')
    documents, service, model = SQLiteDocumentRepository(records), RecognitionService(records), Model()
    app, domains = assemble(root, records, documents, service, model)
    # 原应用 startup 会构建 Kernel，可信宿主必须在启动前安装。
    app.state.external_execution_host = HostAdmission(deployment=DeploymentLayout('desktop', root),
        owner_id='local-user', records=records, registrations={})
    with TestClient(app) as http:
        yield SimpleNamespace(root=root, records=records, documents=documents, service=service,
            model=model, http=http, domains=domains)
    app.state.ai_turn_runner.shutdown()
    app = domains = documents = service = model = http = records = None
    gc.collect()
    temporary.cleanup()


def install(env):
    app = env.http.app
    runtime = get_or_build_ai_runtime(SimpleNamespace(app=app), SimpleNamespace(root_dir=env.root))
    return app, runtime, app.state.external_runner


def test_original_runtime_installs_same_owner_and_reuses_it(host_env):
    env = host_env
    app, runtime, owner = install(env)
    assert owner.context is app.state.external_context
    assert owner.turns is app.state.ai_turn_store
    assert owner.runtime is runtime
    assert owner.frozen_authorization is not None
    assert get_or_build_ai_runtime(SimpleNamespace(app=app), SimpleNamespace(root_dir=env.root)) is runtime
    assert app.state.external_runner is owner


def test_default_app_keeps_external_runner_unregistered(env):
    runtime = get_or_build_ai_runtime(SimpleNamespace(app=env.http.app), SimpleNamespace(root_dir=env.root))
    assert env.http.app.state.external_runner is None
    assert env.http.app.state.external_context.runtime is runtime


def test_absent_memory_service_rejects_before_task_admission_or_workspace(host_env):
    env = host_env
    app, runtime, owner = install(env)
    context, _, runner, _ = prepare(env)
    context.execute(TURN, runtime=runtime, runner=runner)
    turn_id = 'turn-' + 'c' * 32
    before = app.state.ai_turn_store.events_after(TURN)
    config = {'mcpServers': {'chriptmas-memory': {'command': str(Path(sys.executable)),
        'args': ['-I', '-m', 'backend.memory_app.mcp']}}}
    with pytest.raises(ExternalRunnerError, match='^external_runner_memory_unavailable$'):
        owner.prepare(turn_id, delivery_turn_id=TURN, executor='codex', cli_version='0.156.1',
            task='核查合成资料', mcp_config=config, session_id='session-native',
            operation_id='operation-native', idempotency_key=turn_id, created_at=DAY.isoformat())
    assert app.state.ai_turn_store.get_request(turn_id) is None
    assert app.state.ai_turn_store.events_after(TURN) == before
    assert env.records.list('v2_external_runs') == ()
    assert env.records.list('v2_external_task_preparations') == ()
    assert not (env.root / 'agent_workspaces').exists()
    assert env.model.calls == 0


def test_host_refusal_converges_new_accepted_turn_and_preserves_terminal_replay(host_env, monkeypatch):
    env = host_env
    # 只隔离尚未部署的 MCP 资格探针；被测原应用、Kernel、Host 和存储均为真实实现。
    # 此失败控制不作为 MCP 或完整 CLI 链已通过的证据。
    from backend.memory_app.v2 import external_memory_admission as memory
    probes = []
    def transport(*args, **kwargs):
        probes.append(True)
        return {'test': 'transport-only'}
    monkeypatch.setattr(memory, 'require_memory_service', transport)
    monkeypatch.setattr(memory, 'check_memory_configuration', lambda *args, **kwargs: None, raising=False)
    monkeypatch.setattr(memory, 'revalidate_memory_service', lambda *args, **kwargs: None)
    app, runtime, owner = install(env)
    authentication_root = env.root / 'empty-auth'
    authentication_root.mkdir()
    owner.host.registrations['codex'] = ExecutorRegistration('codex', Path(sys._base_executable), authentication_root)
    context, _, turn_runner, _ = prepare(env)
    context.execute(TURN, runtime=runtime, runner=turn_runner)
    turn_id = 'turn-' + 'd' * 32
    config = {'mcpServers': {'chriptmas-memory': {'command': str(Path(sys.executable)),
        'args': ['-I', '-m', 'backend.memory_app.mcp']}}}
    kwargs = dict(delivery_turn_id=TURN, executor='codex', cli_version='0.156.1',
        task='核查合成资料', mcp_config=config, session_id='session-refusal',
        operation_id='operation-refusal', idempotency_key=turn_id, created_at=DAY.isoformat())
    with pytest.raises(ExternalRunnerError):
        owner.prepare(turn_id, **kwargs)
    events = app.state.ai_turn_store.events_after(turn_id)
    assert events[0]['type'] == 'turn.accepted'
    assert events[-1]['type'] == 'turn.failed'
    assert not any(event['type'].startswith(('tool.', 'model.')) for event in events)
    assert app.state.ai_turn_store.get_immutable_payload(turn_id, 'external-task-run-v1') is None
    assert env.records.list('v2_external_runs') == ()
    with pytest.raises(ExternalRunnerError):
        owner.prepare(turn_id, **kwargs)
    assert app.state.ai_turn_store.events_after(turn_id) == events
    assert len(probes) == 1
    assert env.model.calls == 0


def test_cached_runner_rejects_replaced_host_before_memory_or_turn_creation(host_env):
    env = host_env
    app, _, owner = install(env)
    app.state.external_execution_host = HostAdmission(deployment=DeploymentLayout('desktop', env.root),
        owner_id='local-user', records=env.records, registrations={})
    turn_id = 'turn-' + 'e' * 32
    with pytest.raises(ExternalRunnerError, match='^external_runner_binding_invalid$'):
        owner.prepare(turn_id, delivery_turn_id=TURN, executor='codex', cli_version='0.156.1',
            task='核查合成资料', mcp_config={}, session_id='session-replaced',
            operation_id='operation-replaced', idempotency_key=turn_id, created_at=DAY.isoformat())
    assert app.state.ai_turn_store.get_request(turn_id) is None
    assert env.records.list('v2_external_runs') == ()


def test_prepared_replay_reuses_immutable_memory_proof_without_new_probe(host_env, monkeypatch):
    # 只隔离外部 MCP 传输资格，验证原 Kernel 接受、真实 Host 版本探测与不可变绑定的回放。
    from backend.memory_app.v2 import external_memory_admission as memory
    from tests.memory_app.v2.test_external_host import native_cli
    env = host_env
    app, runtime, owner = install(env)
    probes = []
    def transport(*args, **kwargs):
        probes.append(True)
        return {'test': 'transport-only'}
    monkeypatch.setattr(memory, 'require_memory_service', transport)
    monkeypatch.setattr(memory, 'check_memory_configuration', lambda *args, **kwargs: None, raising=False)
    monkeypatch.setattr(memory, 'revalidate_memory_service', lambda *args, **kwargs: None)
    authentication_root = env.root / 'empty-auth'
    authentication_root.mkdir()
    executable = native_cli(env.root / 'synthetic-codex.exe', 'codex')
    owner.host.registrations['codex'] = ExecutorRegistration('codex', executable, authentication_root)
    context, _, turn_runner, _ = prepare(env)
    context.execute(TURN, runtime=runtime, runner=turn_runner)
    turn_id = 'turn-' + 'f' * 32
    config = {'mcpServers': {'chriptmas-memory': {'command': str(Path(sys.executable)),
        'args': ['-I', '-m', 'backend.memory_app.mcp']}}}
    kwargs = dict(delivery_turn_id=TURN, executor='codex', cli_version='0.156.1',
        task='核查合成资料', mcp_config=config, session_id='session-replay',
        operation_id='operation-replay', idempotency_key=turn_id, created_at=DAY.isoformat())
    try:
        frozen = owner.prepare(turn_id, **kwargs)
    except ExternalRunnerError as error:
        # 此测试只含合成材料，读取固定 Host 错误以定位真实准备前提。
        if error.__context__ is not None:
            raise error.__context__
        raise
    saved = owner.turns.get_immutable_payload(turn_id, 'external-task-run-v1')
    events = owner.turns.events_after(turn_id)
    deliveries = env.records.list('v2_external_agent_deliveries')
    assert owner.prepare(turn_id, **kwargs) == frozen
    assert len(probes) == 1
    assert owner.turns.get_immutable_payload(turn_id, 'external-task-run-v1') == saved
    assert owner.turns.events_after(turn_id) == events
    assert env.records.list('v2_external_agent_deliveries') == deliveries
    with pytest.raises(ExternalRunnerError):
        owner.prepare(turn_id, **{**kwargs, 'task':'更换任务'})
    assert len(probes) == 1
    assert owner.turns.get_immutable_payload(turn_id, 'external-task-run-v1') == saved
    assert env.records.list('v2_external_runs') == () and env.model.calls == 0
    def missing(*args, **kwargs):
        raise memory.MemoryAdmissionError('external_runner_memory_unavailable')
    monkeypatch.setattr(memory, 'check_memory_configuration', missing)
    with pytest.raises(ExternalRunnerError, match='^external_runner_memory_unavailable$'):
        owner.prepare(turn_id, **kwargs)
    assert len(probes) == 1
    assert owner.turns.get_immutable_payload(turn_id, 'external-task-run-v1') == saved
    assert owner.turns.events_after(turn_id) == events


def test_concurrent_first_prepare_admits_only_one_memory_probe_without_writer_lock(host_env, monkeypatch):
    # MCP 外部资格隔离；并发占位、原 Kernel、Host 和 SQLite 均为被测真实实现。
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from backend.memory_app.v2 import external_memory_admission as memory
    env = host_env
    _, runtime, owner = install(env)
    context, _, turn_runner, _ = prepare(env)
    context.execute(TURN, runtime=runtime, runner=turn_runner)
    root = env.root / 'empty-auth'
    root.mkdir()
    owner.host.registrations['codex'] = ExecutorRegistration('codex', Path(sys._base_executable), root)
    probes, started, release, second_done = [], Event(), Event(), Event()
    def transport(*args, **kwargs):
        probes.append(True)
        started.set()
        assert release.wait(5)
        return {'test': 'transport-only'}
    monkeypatch.setattr(memory, 'check_memory_configuration', lambda *args, **kwargs: None, raising=False)
    monkeypatch.setattr(memory, 'require_memory_service', transport)
    monkeypatch.setattr(memory, 'revalidate_memory_service', lambda *args, **kwargs: None)
    config = {'mcpServers': {'chriptmas-memory': {'command': str(Path(sys.executable)),
        'args': ['-I', '-m', 'backend.memory_app.mcp']}}}
    turn_id = 'turn-' + '1' * 32
    kwargs = dict(delivery_turn_id=TURN, executor='codex', cli_version='0.156.1',
        task='核查合成资料', mcp_config=config, session_id='session-concurrent',
        operation_id='operation-concurrent', idempotency_key=turn_id, created_at=DAY.isoformat())
    def invoke(done=None):
        try:
            owner.prepare(turn_id, **kwargs)
        except ExternalRunnerError:
            return 'refused'
        finally:
            if done is not None:
                done.set()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(invoke)
        assert started.wait(5)
        second = pool.submit(invoke, second_done)
        try:
            with env.records.begin() as tx:
                tx.put('probe_witness', 'independent', {'ok': True}, expected_revision=0)
                tx.commit()
            settled = second_done.wait(1)
        finally:
            release.set()
        assert first.result(timeout=5) == second.result(timeout=5) == 'refused'
    assert settled and len(probes) == 1
    assert env.records.list('v2_external_runs') == () and env.model.calls == 0
