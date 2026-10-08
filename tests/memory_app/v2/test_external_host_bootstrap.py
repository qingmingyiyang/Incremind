"""可信本机候选元数据与生产前装配；不执行真实 CLI 或读取认证正文。"""
from copy import deepcopy
import gc
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.memory_app.kernel.ai_runtime import get_or_build_ai_runtime
from backend.memory_app.v2 import install_v2_routes
from backend.memory_app.v2.external_adapters import build_launch_plan
from backend.memory_app.v2.external_host import HostAdmission, ExternalHostError
from backend.memory_app.workspace import install_workspace_routes
from backend.recognition import RecognitionService
from backend.shared.deployment import DeploymentLayout
from core.document_engine import SQLiteDocumentRepository
from core.storage_provider import SQLiteStructuredRecordStore
from tests.memory_app.v2.test_external_host import native_cli, frozen_turn
from tests.memory_app.v2.test_workbench_ask import Model


def module():
    return importlib.import_module('backend.memory_app.v2.external_host_bootstrap')


@pytest.fixture
def installed(tmp_path, monkeypatch):
    temporary = tempfile.TemporaryDirectory(prefix='T16.4-bootstrap-', dir=tmp_path.anchor)
    root = Path(temporary.name)
    profile = root / 'profile'; profile.mkdir()
    (profile / '.codex').mkdir(); (profile / '.claude').mkdir()
    binaries = root / 'trusted-bin'; binaries.mkdir()
    startup = {'PATH':str(binaries)}
    monkeypatch.setattr(module(), '_startup_environment', lambda: dict(startup))
    monkeypatch.setattr(module(), '_profile_directory', lambda: profile)
    monkeypatch.setattr(module(), '_architecture', lambda: 'x64')
    yield SimpleNamespace(root=root, profile=profile, binaries=binaries, startup=startup)
    gc.collect()
    temporary.cleanup()


def package(installation, layout='nested', architecture='x64'):
    root = installation.binaries / 'node_modules' / '@openai' / 'codex'
    root.mkdir(parents=True)
    (installation.binaries / 'codex.ps1').write_text('unexecuted-standard-anchor', encoding='utf-8')
    name = '@openai/codex-win32-' + architecture
    meta = {'name':'@openai/codex', 'version':'0.156.1', 'bin':{'codex':'bin/codex.js'},
        'optionalDependencies':{name:'npm:@openai/codex@0.156.1-win32-' + architecture}}
    (root / 'package.json').write_text(json.dumps(meta), encoding='utf-8')
    if layout == 'bundled':
        vendor = root / 'vendor'
    else:
        platform = (root / 'node_modules' if layout == 'nested' else installation.binaries / 'node_modules')
        platform = platform / '@openai' / ('codex-win32-' + architecture)
        platform.mkdir(parents=True)
        (platform / 'package.json').write_text(json.dumps({'name':'@openai/codex',
            'version':'0.156.1-win32-' + architecture, 'os':['win32'], 'cpu':[architecture]}), encoding='utf-8')
        vendor = platform / 'vendor'
    executable = vendor / ('x86_64-pc-windows-msvc' if architecture == 'x64' else 'aarch64-pc-windows-msvc') / 'bin' / 'codex.exe'
    executable.parent.mkdir(parents=True)
    native_cli(executable, 'codex')
    return root, executable


@pytest.mark.parametrize('layout,architecture', [('nested','x64'),('hoisted','x64'),('bundled','x64'),('nested','arm64')])
def test_known_npm_metadata_resolves_native_bin_without_running_wrapper(installed, monkeypatch, layout, architecture):
    monkeypatch.setattr(module(), '_architecture', lambda: architecture)
    _, executable = package(installed, layout, architecture)
    registration = module().discover_local_executors()['codex']
    assert registration.executable == executable
    assert registration.authentication_root == installed.profile / '.codex'
    assert registration.discovery_stamps
    assert not list(installed.root.rglob('version-called'))


def test_native_path_order_and_os_profile_fallback_do_not_search_cwd(installed):
    first = native_cli(installed.binaries / 'codex.exe', 'codex')
    later = installed.root / 'later'; later.mkdir()
    native_cli(later / 'codex.exe', 'codex')
    installed.startup['PATH'] += ';' + str(later)
    local = installed.profile / '.local' / 'bin'; local.mkdir(parents=True)
    claude = native_cli(local / 'claude.exe', 'claude-code')
    registrations = module().discover_local_executors()
    assert registrations['codex'].executable == first
    assert registrations['claude-code'].executable == claude
    assert registrations['claude-code'].authentication_root == installed.profile / '.claude'
    assert not list(installed.root.rglob('version-called'))


@pytest.mark.parametrize('change', ['missing','relative_path','script','unknown_meta','wrong_alias','oversized','junction','missing_auth','relative_auth','unknown_arch'])
def test_untrusted_or_missing_candidate_is_not_registered_or_replaced_by_fallback(installed, monkeypatch, change):
    root, executable = package(installed)
    fallback = installed.profile / '.local' / 'bin'; fallback.mkdir(parents=True)
    native_cli(fallback / 'codex.exe', 'codex')
    if change == 'missing':
        executable.unlink(); (fallback / 'codex.exe').unlink()
    elif change == 'relative_path': installed.startup['PATH'] = '.'
    elif change == 'script': (root / 'package.json').unlink()
    elif change in {'unknown_meta','wrong_alias','oversized'}:
        path = root / 'package.json'
        value = json.loads(path.read_text(encoding='utf-8'))
        if change == 'unknown_meta': value['version'] = '9.0.0'
        elif change == 'wrong_alias': value['optionalDependencies']['@openai/codex-win32-x64'] = 'file:../untrusted'
        else: value['untrusted_padding'] = 'x' * 65537
        path.write_text(json.dumps(value), encoding='utf-8')
    elif change == 'junction':
        import subprocess
        original = installed.binaries.with_name('original-bin')
        installed.binaries.rename(original)
        result = subprocess.run(['cmd','/c','mklink','/J',str(installed.binaries),str(original)], capture_output=True)
        assert result.returncode == 0
    elif change == 'missing_auth': (installed.profile / '.codex').rmdir()
    elif change == 'relative_auth': installed.startup['CODEX_HOME'] = '.codex'
    else: monkeypatch.setattr(module(), '_architecture', lambda: 'unknown')
    try:
        assert 'codex' not in module().discover_local_executors()
        assert not list(installed.root.rglob('version-called'))
    finally:
        if change == 'junction':
            # 仅移除本测试创建的连接，保留实际安装目录。
            import os
            os.rmdir(installed.binaries)


def test_authentication_override_uses_existing_root_without_reading_body_or_creating_login(installed, monkeypatch):
    native_cli(installed.binaries / 'codex.exe', 'codex')
    auth = installed.root / 'existing-auth'; auth.mkdir()
    body = auth / 'auth.json'; body.write_bytes(b'unread-synthetic-authentication')
    installed.startup['CODEX_HOME'] = str(auth)
    original = Path.open
    def checked(path, *args, **kwargs):
        assert path != body
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', checked)
    assert module().discover_local_executors()['codex'].authentication_root == auth
    assert set(auth.iterdir()) == {body}
    assert not list(installed.root.rglob('version-called'))


@pytest.mark.parametrize('change', ['executable','authentication','package'])
def test_discovered_identity_replacement_is_rejected_by_original_host_before_version(installed, change):
    package_root, executable = package(installed)
    registrations = module().discover_local_executors()
    records = SQLiteStructuredRecordStore(installed.root / 'records.sqlite3')
    host = HostAdmission(deployment=DeploymentLayout('desktop', installed.root), owner_id='local-user',
        records=records, registrations=registrations)
    turn = frozen_turn()
    cwd = installed.root / 'agent_workspaces' / turn['turn_id']; cwd.mkdir(parents=True)
    config = {'mcpServers':{'chriptmas-memory':{'command':sys.executable, 'args':['-I','-m','backend.memory_app.mcp']}}}
    plan = build_launch_plan('codex', cli_version='0.156.1', executable=executable,
        cwd=cwd, task=turn['input']['text'], mcp_config=config)
    if change == 'executable':
        executable.rename(executable.with_suffix('.old')); native_cli(executable, 'codex')
    elif change == 'authentication':
        auth = installed.profile / '.codex'; auth.rename(installed.profile / 'original-auth'); auth.mkdir()
    else:
        path = package_root / 'package.json'; text = path.read_bytes()
        path.rename(package_root / 'original-package.json'); path.write_bytes(text)
    with pytest.raises(ExternalHostError, match='^external_host_resource_changed$'):
        host.prepare(turn, plan, mcp_config=config)
    assert not list(installed.root.rglob('version-called'))


def assemble_installed(installation, *, layout=None, records=None, existing_host=None):
    root = installation.root
    records = records or SQLiteStructuredRecordStore(root / 'records.sqlite3')
    documents, service, model = SQLiteDocumentRepository(records), RecognitionService(records), Model()
    app = FastAPI()
    app.state.deployment = layout or DeploymentLayout('desktop', root)
    if existing_host is not None:
        app.state.external_execution_host = existing_host
    from backend.memory_app.v2.devices import DeviceRegistry
    app.state.device_registry = DeviceRegistry(root / 'server')
    domains = install_workspace_routes(app, runtime_root=root, records=records, models=model,
        documents=documents, service=service)
    install_v2_routes(app, runtime_root=root, records=records, models=model, documents=documents,
        service=service, workspace=domains)
    return SimpleNamespace(app=app, records=records, documents=documents, service=service,
        model=model, domains=domains, root=root)


def test_production_route_install_owns_host_before_original_runtime_and_reuses_it(installed):
    native_cli(installed.binaries / 'codex.exe', 'codex')
    env = assemble_installed(installed)
    host = getattr(env.app.state, 'external_execution_host', None)
    assert isinstance(host, HostAdmission)
    assert host.deployment is env.app.state.deployment
    assert host.records is env.records and host.owner_id == env.app.state.external_context.owner_id == 'local-user'
    assert getattr(env.app.state, 'ai_runtime', None) is None
    with TestClient(env.app):
        runtime = get_or_build_ai_runtime(SimpleNamespace(app=env.app), SimpleNamespace(root_dir=env.root))
        owner = env.app.state.external_runner
        assert owner.host is host and owner.runtime is runtime
        installed.startup['PATH'] = 'relative-after-startup'
        assert get_or_build_ai_runtime(SimpleNamespace(app=env.app), SimpleNamespace(root_dir=env.root)) is runtime
        assert env.app.state.external_execution_host is host and env.app.state.external_runner is owner
        assert not list(env.root.rglob('version-called'))
    env.app.state.ai_turn_runner.shutdown()


@pytest.mark.parametrize('mode', ['server','missing_cli'])
def test_server_or_no_cli_keeps_external_execution_unregistered(installed, mode):
    if mode == 'server': native_cli(installed.binaries / 'codex.exe', 'codex')
    env = assemble_installed(installed, layout=DeploymentLayout(mode if mode == 'server' else 'desktop', installed.root))
    assert getattr(env.app.state, 'external_execution_host', None) is None
    with TestClient(env.app):
        assert env.app.state.external_runner is None
        assert env.model.calls == 0
    env.app.state.ai_turn_runner.shutdown()


def test_production_layout_root_mismatch_is_fixed_rejection_without_discovery(installed):
    native_cli(installed.binaries / 'codex.exe', 'codex')
    wrong = installed.root / 'wrong'; wrong.mkdir()
    with pytest.raises(ValueError, match='^external_host_bootstrap_invalid$'):
        assemble_installed(installed, layout=DeploymentLayout('desktop', wrong))
    assert not list(installed.root.rglob('version-called'))


@pytest.mark.parametrize('target', ['path','authentication','device_path'])
def test_network_namespace_is_rejected_before_any_remote_metadata_access(installed, monkeypatch, target):
    native_cli(installed.binaries / 'codex.exe', 'codex')
    original = Path.lstat
    touched = []
    def local_only(path, *args, **kwargs):
        if str(path).startswith('\\\\'):
            touched.append(str(path))
            raise AssertionError('remote metadata must not be requested')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'lstat', local_only)
    if target == 'path': installed.startup['PATH'] = r'\\unreachable\share\bin'
    elif target == 'device_path': installed.startup['PATH'] = r'\\?\C:\device-path'
    else: installed.startup['CODEX_HOME'] = r'\\unreachable\share\authentication'
    assert 'codex' not in module().discover_local_executors()
    assert touched == []
    assert not list(installed.root.rglob('version-called'))


def test_remote_mapped_drive_is_rejected_before_filesystem_metadata(installed, monkeypatch):
    native_cli(installed.binaries / 'codex.exe', 'codex')
    # 只隔离 OS 驱动类型环境依赖，发现器和候选过滤仍为真实实现。
    monkeypatch.setattr(module(), '_drive_kind', lambda anchor: 4, raising=False)
    touched = []
    original = Path.lstat
    def observed(path, *args, **kwargs):
        touched.append(path)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'lstat', observed)
    assert module().discover_local_executors() == {}
    assert touched == []


def test_package_replaced_during_bounded_read_is_not_a_discovery_proof(installed, monkeypatch):
    from core.external_extension_runtime.windows_handle_io import _Nt
    root, _ = package(installed)
    metadata = root / 'package.json'
    original = Path.open
    original_native = _Nt.open_file
    changed = []
    def replace():
        if not changed:
            changed.append(True)
            metadata.rename(root / 'original-package.json')
            metadata.write_text(json.dumps({'name':'@openai/codex','version':'0.156.1',
                'bin':{'codex':'bin/codex.js'}, 'optionalDependencies':{
                    '@openai/codex-win32-x64':'npm:@openai/codex@0.156.1-win32-x64'}}), encoding='utf-8')
    def observed(path, *args, **kwargs):
        if path == metadata and not changed:
            replace()
        return original(path, *args, **kwargs)
    def observed_native(api, parent, name):
        if name == 'package.json': replace()
        return original_native(api, parent, name)
    monkeypatch.setattr(Path, 'open', observed)
    monkeypatch.setattr(_Nt, 'open_file', observed_native)
    assert 'codex' not in module().discover_local_executors()
    assert changed == [True]
    assert not list(installed.root.rglob('version-called'))


@pytest.mark.parametrize('change', ['file','parent_junction'])
def test_metadata_name_replacement_never_reads_external_body(installed, monkeypatch, change):
    from contextlib import contextmanager
    import subprocess
    from core.external_extension_runtime.windows_handle_io import _Nt
    root, _ = package(installed)
    metadata = root / 'package.json'
    outside = installed.root / 'synthetic-outside'; outside.mkdir()
    outside_body = b'{"public":"synthetic-outside-only"}'
    outside_file = outside / 'package.json'; outside_file.write_bytes(outside_body)
    original_open, original_native, original_read = Path.open, _Nt.open_file, _Nt.read
    changed, outside_reads = [], []
    def replace():
        if changed: return
        changed.append(True)
        if change == 'file':
            metadata.rename(root / 'original-package.json')
            os.link(outside_file, metadata)
        else:
            root.rename(root.with_name('original-codex'))
            assert root.is_relative_to(installed.root) and outside.is_relative_to(installed.root)
            result = subprocess.run(['cmd','/c','mklink','/J',str(root),str(outside)], capture_output=True)
            assert result.returncode == 0
    class ObservedStream:
        def __init__(self, stream): self.stream = stream
        def read(self, *args, **kwargs):
            raw = self.stream.read(*args, **kwargs)
            if raw == outside_body: outside_reads.append(True)
            return raw
    @contextmanager
    def observed_open(path, *args, **kwargs):
        if path == metadata: replace()
        with original_open(path, *args, **kwargs) as stream:
            yield ObservedStream(stream)
    def observed_native(api, parent, name):
        if name == 'package.json': replace()
        return original_native(api, parent, name)
    def observed_read(api, handle, *, maximum):
        raw = original_read(api, handle, maximum=maximum)
        if raw == outside_body: outside_reads.append(True)
        return raw
    # 只观察真实文件 I/O，并在实际打开边界替换合成文件名；不替换发现器或读取结果。
    monkeypatch.setattr(Path, 'open', observed_open)
    monkeypatch.setattr(_Nt, 'open_file', observed_native)
    monkeypatch.setattr(_Nt, 'read', observed_read)
    try:
        assert 'codex' not in module().discover_local_executors()
        assert changed == [True]
        assert outside_reads == []
        with original_open(outside_file, 'rb') as stream:
            assert stream.read() == outside_body
        assert not list(installed.root.rglob('version-called'))
    finally:
        if change == 'parent_junction' and changed:
            os.rmdir(root)


@pytest.mark.parametrize('phase', ['discovery','prepare','lease'])
def test_static_parent_junction_is_rejected_before_descendant_metadata(installed, monkeypatch, phase):
    import subprocess
    root, executable = package(installed)
    lease = None
    if phase != 'discovery':
        records = SQLiteStructuredRecordStore(installed.root / 'records.sqlite3')
        host = HostAdmission(deployment=DeploymentLayout('desktop', installed.root), owner_id='local-user',
            records=records, registrations=module().discover_local_executors())
        turn = frozen_turn()
        cwd = installed.root / 'agent_workspaces' / turn['turn_id']; cwd.mkdir(parents=True)
        config = {'mcpServers':{'chriptmas-memory':{'command':sys.executable,'args':['-I','-m','backend.memory_app.mcp']}}}
        plan = build_launch_plan('codex', cli_version='0.156.1', executable=executable,
            cwd=cwd, task=turn['input']['text'], mcp_config=config)
        if phase == 'lease': lease = host.prepare(turn, plan, mcp_config=config)
    junction = root.parent
    outside = installed.root / 'synthetic-outside-openai'
    junction.rename(outside)
    assert junction.is_relative_to(installed.root) and outside.is_relative_to(installed.root)
    result = subprocess.run(['cmd','/c','mklink','/J',str(junction),str(outside)], capture_output=True)
    assert result.returncode == 0
    descendants = []
    original = Path.lstat
    def observed(path, *args, **kwargs):
        if path != junction and path.is_relative_to(junction): descendants.append(True)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'lstat', observed)
    try:
        if phase == 'discovery':
            assert 'codex' not in module().discover_local_executors()
        else:
            with pytest.raises(ExternalHostError):
                if phase == 'prepare': host.prepare(turn, plan, mcp_config=config)
                else: lease.validate()
        assert descendants == []
    finally:
        if lease is not None: lease.close()
        os.rmdir(junction)


@pytest.mark.parametrize('change', ['wrapper','platform_meta'])
def test_discovered_public_metadata_stays_frozen_after_original_host_lease(installed, change):
    root, executable = package(installed)
    records = SQLiteStructuredRecordStore(installed.root / 'records.sqlite3')
    host = HostAdmission(deployment=DeploymentLayout('desktop', installed.root), owner_id='local-user',
        records=records, registrations=module().discover_local_executors())
    turn = frozen_turn()
    cwd = installed.root / 'agent_workspaces' / turn['turn_id']; cwd.mkdir(parents=True)
    config = {'mcpServers':{'chriptmas-memory':{'command':sys.executable,'args':['-I','-m','backend.memory_app.mcp']}}}
    plan = build_launch_plan('codex', cli_version='0.156.1', executable=executable,
        cwd=cwd, task=turn['input']['text'], mcp_config=config)
    lease = host.prepare(turn, plan, mcp_config=config)
    path = (installed.binaries / 'codex.ps1' if change == 'wrapper' else
        root / 'node_modules' / '@openai' / 'codex-win32-x64' / 'package.json')
    path.rename(path.with_suffix('.original'))
    path.write_bytes(b'replaced-public-metadata')
    try:
        with pytest.raises(ExternalHostError, match='^external_host_resource_changed$'):
            lease.validate()
    finally:
        lease.close()


@pytest.mark.parametrize('change', ['none','owner','records','layout'])
def test_explicit_existing_host_is_preserved_only_with_original_binding(installed, change):
    records = SQLiteStructuredRecordStore(installed.root / 'records.sqlite3')
    layout = DeploymentLayout('desktop', installed.root)
    host = HostAdmission(deployment=layout, owner_id='local-user', records=records, registrations={})
    if change == 'owner': host.owner_id = 'other-user'
    elif change == 'records': host.records = SQLiteStructuredRecordStore(installed.root / 'other.sqlite3')
    elif change == 'layout': host.deployment = DeploymentLayout('desktop', installed.profile)
    if change == 'none':
        env = assemble_installed(installed, records=records, existing_host=host)
        assert env.app.state.external_execution_host is host
    else:
        with pytest.raises(ValueError, match='^external_host_bootstrap_invalid$'):
            assemble_installed(installed, records=records, existing_host=host)
    assert not list(installed.root.rglob('version-called'))


def test_replaced_production_host_cannot_be_adopted_by_cached_original_runner(installed):
    from backend.memory_app.v2.external_runner import ExternalRunnerError
    from tests.memory_app.v2.test_external_context import TURN, DAY
    native_cli(installed.binaries / 'codex.exe', 'codex')
    env = assemble_installed(installed)
    with TestClient(env.app):
        owner = env.app.state.external_runner
        env.app.state.external_execution_host = HostAdmission(deployment=env.app.state.deployment,
            owner_id='local-user', records=env.records, registrations={})
        with pytest.raises(ExternalRunnerError, match='^external_runner_binding_invalid$'):
            owner.prepare('turn-host-replaced', delivery_turn_id=TURN, executor='codex', cli_version='0.156.1',
                task='合成任务', mcp_config={}, session_id='session-replaced', operation_id='operation-replaced',
                idempotency_key='turn-host-replaced', created_at=DAY.isoformat())
        assert owner.turns.get_request('turn-host-replaced') is None
        assert env.records.list('v2_external_runs') == () and env.model.calls == 0
        assert not list(env.root.rglob('version-called'))
    env.app.state.ai_turn_runner.shutdown()


def test_discovered_lease_rechecks_local_drive_before_path_metadata(installed, monkeypatch):
    from backend.memory_app.v2 import external_host as host_module
    _, executable = package(installed)
    records = SQLiteStructuredRecordStore(installed.root / 'records.sqlite3')
    host = HostAdmission(deployment=DeploymentLayout('desktop', installed.root), owner_id='local-user',
        records=records, registrations=module().discover_local_executors())
    turn = frozen_turn()
    cwd = installed.root / 'agent_workspaces' / turn['turn_id']; cwd.mkdir(parents=True)
    config = {'mcpServers':{'chriptmas-memory':{'command':sys.executable,'args':['-I','-m','backend.memory_app.mcp']}}}
    plan = build_launch_plan('codex', cli_version='0.156.1', executable=executable,
        cwd=cwd, task=turn['input']['text'], mcp_config=config)
    lease = host.prepare(turn, plan, mcp_config=config)
    touched = []
    original = Path.lstat
    def observed(path, *args, **kwargs):
        touched.append(path)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(host_module, '_local_drive_kind', lambda anchor: 4, raising=False)
    monkeypatch.setattr(Path, 'lstat', observed)
    try:
        with pytest.raises(ExternalHostError, match='^external_host_resource_changed$'):
            lease.validate()
        assert touched == []
    finally:
        lease.close()
