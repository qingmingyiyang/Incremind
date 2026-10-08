"""宿主准入使用临时原生 CLI、真实许可记录和内存秘密，不代替隔离验收。"""
from dataclasses import replace
import importlib
import io
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import zipfile

import pytest

from backend.memory_app.v2.external_adapters import build_launch_plan
from backend.shared.deployment import DeploymentLayout
from core.ai_kernel.turn_kinds import freeze_turn_request
from core.storage_provider import SQLiteStructuredRecordStore


def native_cli(path, executor, *, version=None, exit_code=0):
    """Windows 使用现有 pip 的原生启动器；POSIX 使用最小合成 ELF。"""
    version = version or ('codex-cli 0.156.1' if executor == 'codex' else '2.1.257 (Claude Code)')
    if os.name == 'nt':
        from pip._vendor.distlib.resources import finder
        source = ('import os, sys\nfrom pathlib import Path\n'
            'assert sys.argv[1:] == ["--version"]\n'
            'Path(sys.argv[0]).with_name("version-called").write_text("called")\n'
            'print(' + repr(version) + ')\n'
            'print(os.environ.get("DEVICE_KEY", "")) if os.environ.get("DEVICE_KEY") else None\n'
            'sys.exit(' + str(exit_code) + ')\n')
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as archive:
            archive.writestr('__main__.py', source)
        launcher = finder('pip._vendor.distlib').find('t64.exe').bytes
        path.write_bytes(launcher + ('#!"' + sys._base_executable + '" -I -S\n').encode() + buffer.getvalue())
    else:
        # 合成 ELF 只向 stdout 写版本，不运行模型、shell 或认证代码。
        payload = (version + '\n').encode()
        code = bytes.fromhex('b801000000bf01000000488d3513000000ba') + struct.pack('<I', len(payload))
        code += bytes.fromhex('0f05b83c000000bf') + struct.pack('<I', exit_code) + bytes.fromhex('0f05')
        header = b'\x7fELF\x02\x01\x01' + b'\0' * 9
        header += struct.pack('<HHIQQQIHHHHHH', 2, 62, 1, 0x400078, 64, 0, 0, 64, 56, 1, 0, 0, 0)
        program = struct.pack('<IIQQQQQQ', 1, 5, 0, 0x400000, 0x400000, 120 + len(code) + len(payload),
            120 + len(code) + len(payload), 4096)
        path.write_bytes(header + program + code + payload)
        path.chmod(0o700)
    return path


def frozen_turn():
    turn_id = 'turn-' + 'a' * 32
    return freeze_turn_request('project.task', template_version=2, turn_id=turn_id,
        session_id='session-alpha', operation_id='operation-alpha', idempotency_key='host-alpha',
        project_id='project-alpha', created_at='2026-10-06T00:00:00Z', text='合成任务',
        privacy={'mode':'remote_allowed','allow_remote':True,'pii':'none','consent_refs':[], 'retention':'session'},
        capability_request={'mode':'execute_exact_v1','capability_id':'external.task.execute',
            'arguments':{'binding_ref':'crp://session/' + turn_id + '/external-task-run-v1'}})


@pytest.fixture
def setup_host(tmp_path):
    # pytest 原临时根位于工具的 .codex 树；另建受本 fixture 管理的同盘临时根。
    temporary = tempfile.TemporaryDirectory(prefix='T16.4-host-', dir=tmp_path.anchor)
    base = Path(temporary.name)
    def build(executor='codex', *, preset='workspace', commands='disabled', version=None,
            environment=None, secret_environment=None, mode='desktop', cwd=None):
        module = importlib.import_module('backend.memory_app.v2.external_host')
        root = base / ('user-' + executor)
        root.mkdir(exist_ok=True)
        auth = root / 'auth'
        auth.mkdir(exist_ok=True)
        executable = native_cli(root / (executor + ('.exe' if os.name == 'nt' else '')), executor, version=version)
        turn = frozen_turn()
        task = root / 'agent_workspaces' / turn['turn_id'] if cwd is None else cwd
        task.mkdir(parents=True, exist_ok=True)
        config = {'mcpServers':{'chriptmas-memory':{'command':sys.executable,
            'args':['-I','-m','backend.memory_app.mcp'], 'env':{'DEVICE_KEY':'${APPROVED_DEVICE_KEY}'}}}}
        plan = build_launch_plan(executor, cli_version='0.156.1' if executor == 'codex' else '2.1.257',
            executable=executable, cwd=task, task='合成任务', mcp_config=config, preset=preset, commands=commands)
        records = SQLiteStructuredRecordStore(root / 'test.sqlite3')
        layout = DeploymentLayout(mode, root, tmp_path if mode == 'server' else None)
        host = module.HostAdmission(deployment=layout, owner_id='local-user',
            registrations={executor:module.ExecutorRegistration(executor, executable, auth)},
            records=records, environment=environment or {},
            secret_environment=secret_environment or {'APPROVED_DEVICE_KEY':'synthetic-host-secret'})
        return module, host, turn, plan, config, records, root, auth
    yield build
    temporary.cleanup()


@pytest.mark.parametrize('executor', ['codex','claude-code'])
def test_empty_auth_native_version_and_memory_only_lease(setup_host, executor):
    module, host, turn, plan, config, records, root, auth = setup_host(executor)
    before = records.list_all()
    lease = host.prepare(turn, plan, mcp_config=config)
    assert lease.plan == plan and lease.owner_id == 'local-user'
    assert lease.accepted_turn == turn
    projection = lease.accepted_turn
    projection['input']['text'] = '更换任务'
    turn['input']['text'] = '调用方修改'
    assert lease.accepted_turn['input']['text'] == '合成任务'
    assert lease.isolation == 'desktop-cli-permissions' and lease.commands == 'disabled'
    assert lease.environment['DEVICE_KEY'] == 'synthetic-host-secret'
    assert lease.environment['APPROVED_DEVICE_KEY'] == 'synthetic-host-secret'
    assert lease.environment['CODEX_HOME' if executor == 'codex' else 'CLAUDE_CONFIG_DIR'] == str(auth)
    assert 'synthetic-host-secret' not in repr(lease)
    assert lease.redact('x synthetic-host-secret y') == 'x [REDACTED_SECRET] y'
    assert set(lease.secret_values) == {'synthetic-host-secret'}
    lease.validate()
    assert records.list_all() == before
    lease.close()
    lease.close()
    with pytest.raises(module.ExternalHostError, match='^external_host_lease_closed$'):
        lease.validate()
    assert lease.environment == {} and lease.secret_values == ()


@pytest.mark.parametrize('preset,commands,mode', [('research','disabled','desktop'),
    ('workspace','sandboxed','desktop'),('workspace','disabled','server'),('folder','disabled','server')])
def test_missing_real_os_launcher_refuses_before_any_version_spawn(setup_host, preset, commands, mode):
    module, host, turn, plan, config, _, root, _ = setup_host(preset=preset, commands=commands, mode=mode)
    with pytest.raises(module.ExternalHostError, match='^external_host_isolation_unavailable$'):
        host.prepare(turn, plan, mcp_config=config)
    assert not list(root.rglob('version-called'))


@pytest.mark.parametrize('key', ['PYTHONPATH','PYTHONHOME','NODE_OPTIONS','LD_PRELOAD','DYLD_INSERT_LIBRARIES',
    'CODEX_HOME','CLAUDE_CONFIG_DIR','PATH','HOME','USERPROFILE'])
def test_interpreter_and_configuration_environment_expansion_is_rejected(setup_host, key):
    module, host, turn, plan, config, _, root, _ = setup_host(environment={key:'synthetic-injection'})
    with pytest.raises(module.ExternalHostError, match='^external_host_environment_invalid$'):
        host.prepare(turn, plan, mcp_config=config)
    assert not list(root.rglob('version-called'))


@pytest.mark.parametrize('change', ['cwd','argv','aliases','adapter','task','remote','legacy'])
def test_plan_and_frozen_turn_cannot_expand_owned_request(setup_host, tmp_path, change):
    module, host, turn, plan, config, _, root, _ = setup_host()
    if change == 'cwd':
        other = tmp_path / 'other'; other.mkdir()
        plan = replace(plan, cwd=other)
    elif change == 'argv':
        plan = replace(plan, command=(*plan.command, '--dangerously-bypass-approvals-and-sandbox'))
    elif change == 'aliases':
        plan = replace(plan, environment_aliases=(('NODE_OPTIONS','APPROVED_DEVICE_KEY'),))
    elif change == 'adapter':
        plan = replace(plan, adapter_version='codex@2')
    elif change == 'task':
        plan = replace(plan, input_text='其它任务')
    elif change == 'remote':
        turn['privacy']['allow_remote'] = False
    else:
        turn.pop('execution_policy')
    with pytest.raises(module.ExternalHostError, match='^external_host_request_invalid$'):
        host.prepare(turn, plan, mcp_config=config)
    assert not list(root.rglob('version-called'))


@pytest.mark.parametrize('executor,version', [('codex','codex-cli 0.156.0'),('codex','codex-cli 0.156.1 extra'),
    ('claude-code','2.1.256 (Claude Code)'),('codex','sk-' + 'Q' * 24)])
def test_real_version_probe_has_exact_supported_output_and_fixed_error(setup_host, executor, version):
    module, host, turn, plan, config, _, _, _ = setup_host(executor, version=version)
    with pytest.raises(module.ExternalHostError, match='^external_host_version_unavailable$') as error:
        host.prepare(turn, plan, mcp_config=config)
    assert version not in str(error.value)


@pytest.mark.parametrize('name', ['auth.json','managed_config.toml','requirements.toml','settings.json'])
def test_unknown_existing_auth_and_managed_configuration_are_not_read_or_admitted(setup_host, name):
    module, host, turn, plan, config, _, root, auth = setup_host()
    secret = 'unread-existing-secret'
    (auth / name).write_text(secret, encoding='utf-8')
    with pytest.raises(module.ExternalHostError, match='^external_host_configuration_unknown$') as error:
        host.prepare(turn, plan, mcp_config=config)
    assert not list(root.rglob('version-called')) and secret not in str(error.value)
    assert (auth / name).read_text(encoding='utf-8') == secret


def test_new_project_configuration_invalidates_live_lease(setup_host):
    module, host, turn, plan, config, _, _, _ = setup_host()
    lease = host.prepare(turn, plan, mcp_config=config)
    try:
        (plan.cwd / '.codex').mkdir()
        with pytest.raises(module.ExternalHostError, match='^external_host_configuration_changed$'):
            lease.validate()
    finally:
        lease.close()


def test_folder_and_explicit_commands_require_actual_saved_owner_permission(setup_host):
    module, host, turn, _, config, records, root, _ = setup_host()
    folder = root / 'approved-folder'
    folder.mkdir()
    plan = build_launch_plan('codex', cli_version='0.156.1', executable=host.registrations['codex'].executable,
        cwd=folder, task='合成任务', mcp_config=config, preset='folder', commands='explicit')
    refs = {'folder':{'id':'folder-proof','revision':1}, 'commands':{'id':'command-proof','revision':1}}
    with pytest.raises(module.ExternalHostError, match='^external_host_permission_unavailable$'):
        host.prepare(turn, plan, mcp_config=config, host_permission_refs=refs)
    with records.begin() as tx:
        tx.put(module.PERMISSIONS, 'folder-proof', {'owner_id':'local-user','scope':'folder',
            'path':str(folder),'turn_id':None}, expected_revision=0)
        tx.put(module.PERMISSIONS, 'command-proof', {'owner_id':'local-user','scope':'commands',
            'path':str(folder),'turn_id':turn['turn_id']}, expected_revision=0)
        tx.commit()
    lease = host.prepare(turn, plan, mcp_config=config, host_permission_refs=refs)
    assert lease.commands == 'explicit' and lease.plan.cwd == folder
    try:
        with records.begin() as tx:
            tx.put(module.PERMISSIONS, 'command-proof', {'owner_id':'local-user','scope':'commands',
                'path':str(folder),'turn_id':turn['turn_id']}, expected_revision=1)
            tx.commit()
        with pytest.raises(module.ExternalHostError, match='^external_host_permission_unavailable$'):
            lease.validate()
    finally:
        lease.close()


def test_permission_refs_are_detached_from_callers_mutable_mapping(setup_host):
    module, host, turn, _, config, records, root, _ = setup_host()
    folder = root / 'approved-folder'; folder.mkdir()
    plan = build_launch_plan('codex', cli_version='0.156.1', executable=host.registrations['codex'].executable,
        cwd=folder, task='合成任务', mcp_config=config, preset='folder', commands='explicit')
    body = {'owner_id':'local-user','scope':'commands','path':str(folder),'turn_id':turn['turn_id']}
    with records.begin() as tx:
        tx.put(module.PERMISSIONS, 'folder-proof', {'owner_id':'local-user','scope':'folder',
            'path':str(folder),'turn_id':None}, expected_revision=0)
        tx.put(module.PERMISSIONS, 'command-proof', body, expected_revision=0)
        tx.commit()
    refs = {'folder':{'id':'folder-proof','revision':1},'commands':{'id':'command-proof','revision':1}}
    lease = host.prepare(turn, plan, mcp_config=config, host_permission_refs=refs)
    try:
        with records.begin() as tx:
            tx.put(module.PERMISSIONS, 'command-proof', body, expected_revision=1)
            tx.commit()
        refs['commands']['revision'] = 2
        with pytest.raises(module.ExternalHostError, match='^external_host_permission_unavailable$'):
            lease.validate()
    finally:
        lease.close()


@pytest.mark.parametrize('key', ['GIT_CONFIG_GLOBAL','SSL_CERT_FILE','HTTP_PROXY','BUN_OPTIONS','NODE_EXTRA_CA_CERTS'])
def test_secret_environment_is_not_an_arbitrary_environment_injection_channel(setup_host, key):
    module, host, turn, plan, config, _, root, _ = setup_host(secret_environment={
        'APPROVED_DEVICE_KEY':'synthetic-host-secret',key:'synthetic-config-injection'})
    with pytest.raises(module.ExternalHostError, match='^external_host_environment_invalid$'):
        host.prepare(turn, plan, mcp_config=config)
    assert not list(root.rglob('version-called'))
