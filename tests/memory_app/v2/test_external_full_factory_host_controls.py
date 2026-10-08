"""原 factory 本机 Host 与原 server Host 的零启动拒绝控制。"""
import sys

import pytest

from backend.memory_app.storage_authority import resolve_recognition_document_store
from backend.memory_app.v2.external_adapters import build_launch_plan
from backend.memory_app.v2.external_host import ExecutorRegistration, ExternalHostError, HostAdmission
from backend.shared.deployment import resolve_deployment
from tests.memory_app.v2.test_external_host import frozen_turn, native_cli

from .test_external_full_factory_admission import ROOT, original_factory


def test_original_host_login_and_unisolated_modes_refuse_before_native_spawn(original_factory):
    actual = original_factory
    turn = frozen_turn()
    config = {'mcpServers': {'chriptmas-memory': {'command': sys.executable,
        'args': ['-I', '-m', 'backend.memory_app.mcp']}}}
    cwd = actual.root / 'agent_workspaces' / turn['turn_id']
    cwd.mkdir(parents=True)
    registration = actual.host.registrations['codex']

    def plan(executable, directory, preset):
        return build_launch_plan('codex', cli_version='0.156.1', executable=executable,
            cwd=directory, task=turn['input']['text'], mcp_config=config, preset=preset,
            commands='disabled')

    baseline = actual.records.list_all()
    connections = list(actual.audit['connections'])
    research = plan(registration.executable, cwd, 'research')
    with pytest.raises(ExternalHostError, match='^external_host_isolation_unavailable$'):
        actual.host.prepare(turn, research, mcp_config=config)
    assert actual.records.list_all() == baseline
    assert actual.host._issued_leases == {}

    # marker 只表达目录非空，不写入或读取登录正文。
    marker = registration.authentication_root / 'synthetic-login-metadata'
    marker.touch(exist_ok=False)
    assert marker.stat().st_size == 0
    workspace = plan(registration.executable, cwd, 'workspace')
    with pytest.raises(ExternalHostError, match='^external_host_configuration_unknown$'):
        actual.host.prepare(turn, workspace, mcp_config=config)
    assert actual.records.list_all() == baseline
    assert actual.host._issued_leases == {}
    assert not (registration.executable.parent / 'version-called').exists()

    # server 控制用原 resolver/Host 和独立用户根；不代签 server factory 或用户装配。
    server_root = actual.root / 'synthetic-server'
    layout = resolve_deployment(server_root,
        environment={'CHRIPTMAS_DEPLOY': 'server', 'CHRIPTMAS_APP_ROOT': str(server_root)})
    layout.user_root.mkdir(parents=True)
    (layout.user_root / 'config').mkdir()
    (layout.user_root / 'config/settings.toml').write_bytes((ROOT / 'config/settings.toml.example').read_bytes())
    authentication = layout.user_root / 'synthetic-auth'
    authentication.mkdir()
    executable = native_cli(layout.user_root / 'codex.exe', 'codex')
    server_records, namespace = resolve_recognition_document_store(layout.user_root)
    assert namespace == 'default'
    server = HostAdmission(deployment=layout, owner_id='local-user', records=server_records,
        registrations={'codex': ExecutorRegistration('codex', executable, authentication)})
    server_cwd = layout.user_root / 'agent_workspaces' / turn['turn_id']
    server_cwd.mkdir(parents=True)
    server_before = server_records.list_all()
    try:
        server_plan = plan(executable, server_cwd, 'workspace')
        with pytest.raises(ExternalHostError, match='^external_host_isolation_unavailable$'):
            server.prepare(turn, server_plan, mcp_config=config)
        assert server_records.list_all() == server_before
        assert server._issued_leases == {}
        assert not (executable.parent / 'version-called').exists()
        assert server_records.list('v2_external_runs') == ()
        assert server_records.list('v2_external_task_preparations') == ()
    finally:
        server.close()
    assert actual.audit['spawns'] == 0
    assert actual.audit['connections'] == connections
    assert actual.audit['blocked_connections'] == []
    assert actual.records.list_all() == baseline
    assert actual.turns.get_request(turn['turn_id']) is None
    assert actual.records.list('v2_external_runs') == ()
    assert actual.records.list('v2_external_task_preparations') == ()
    assert actual.provider_calls == []
