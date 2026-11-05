# 本文件由显式命令运行，前置条件与命令见 NATIVE_CHECKS.md。
import base64
from dataclasses import replace
import json
import os
from pathlib import Path
from shutil import which
import sys

import pytest

from backend.memory_app.model_config import ModelConfiguration
from backend.memory_app.v2.external_adapters import build_launch_plan
from backend.memory_app.v2.external_host import ExternalHostError, HostAdmission, create_executor_home
from backend.memory_app.v2.external_host_bootstrap import _architecture, _discover, _profile_directory
from backend.shared.deployment import DeploymentLayout
from core.ai_kernel.turn_kinds import freeze_turn_request
from core.storage_provider import SQLiteStructuredRecordStore
from tests.memory_app.v2.test_external_codex_api import API, NETWORK_NAMES, command_config, memory_config


@pytest.fixture
def actual_host(tmp_path):
    if os.name != 'nt':
        raise RuntimeError('native_check_requires_windows_discovery')
    candidate = which('codex')
    if candidate is None:
        raise RuntimeError('native_check_requires_official_codex_on_path')
    # 显式检查解析安装锚点的物理目录，之后仍由生产发现器核元数据和原生版本。
    directory = Path(candidate).resolve().parent
    root = tmp_path
    registration = _discover('codex', [directory], {'PATH': str(directory)},
        _profile_directory(), _architecture(), runtime_root=root)
    assert registration is not None and registration.controlled_api and registration.cli_version == '0.156.1'
    executable = registration.executable
    assert executable.is_file() and executable == executable.resolve()
    turn = freeze_turn_request('project.task', template_version=2, turn_id='turn-' + 'c' * 32,
        session_id='session-codex', operation_id='operation-codex', idempotency_key='codex-api',
        project_id='default', created_at='2026-11-04T00:00:00Z', text='合成任务',
        privacy={'mode': 'remote_allowed', 'allow_remote': True, 'pii': 'none', 'consent_refs': [], 'retention': 'session'},
        capability_request={'mode': 'execute_exact_v1', 'capability_id': 'external.task.execute',
            'arguments': {'binding_ref': 'crp://session/turn-' + 'c' * 32 + '/external-task-run-v1'}})
    cwd = root / 'agent_workspaces' / turn['turn_id']
    cwd.mkdir(parents=True)
    create_executor_home(cwd)
    records = SQLiteStructuredRecordStore(root / 'records.sqlite3')
    models = ModelConfiguration(records, root)
    models.update('generation', {**API, 'api_key': 'synthetic-codex-api-only',
        'allow_remote': True, 'enabled': True, 'expected_revision': 0})
    host = HostAdmission(deployment=DeploymentLayout('desktop', root), owner_id='local-user', records=records,
        registrations={'codex': registration}, models=models, secret_environment={'CODEX_API_KEY': 'synthetic-codex-api-only'},
        network_environment={'HTTPS_PROXY': 'http://synthetic-user:synthetic-password@127.0.0.1:9', 'NO_PROXY': '127.0.0.1',
            'CODEX_CA_CERTIFICATE': str(root / 'private-ca.pem'), 'SSL_CERT_FILE': str(root / 'private-ca.pem')})
    plan = build_launch_plan('codex', cli_version='0.156.1', executable=executable, cwd=cwd,
        task=turn['input']['text'], mcp_config=memory_config(), codex_api=host.codex_api_configuration('codex'))
    return host, models, turn, plan


def test_actual_native_version_lease_keeps_network_private_and_mcp_clean(actual_host):
    host, _, turn, plan = actual_host
    before = host.records.list_all()
    lease = host.prepare(turn, plan, mcp_config=memory_config())
    try:
        assert lease.environment['CODEX_API_KEY'] == 'synthetic-codex-api-only'
        assert lease.environment['HTTPS_PROXY'] == 'http://synthetic-user:synthetic-password@127.0.0.1:9'
        assert lease.environment['CODEX_HOME'] == str(plan.cwd / 'executor-home')
        assert lease.environment['CODEX_CA_CERTIFICATE'] == str(host.deployment.user_root / 'private-ca.pem')
        assert lease.environment['SSL_CERT_FILE'] == lease.environment['CODEX_CA_CERTIFICATE']
        assert all(command_config(plan)['mcp_servers']['chriptmas-memory']['env'][name] == '' for name in NETWORK_NAMES)
        assert lease.redact('synthetic-password') == '[REDACTED_SECRET]'
        basic = base64.b64encode(b'synthetic-user:synthetic-password').decode('ascii')
        assert lease.redact('Basic ' + basic) == 'Basic [REDACTED_SECRET]'
        assert 'synthetic-password' not in repr(lease.environment) + repr(host._network_environment) + repr(lease)
        assert 'HTTPS_PROXY' not in host._memory_environment(memory_config())
        assert 'synthetic-password' not in repr(plan)
        assert not list(host.deployment.user_root.rglob('auth.json'))
        assert list((plan.cwd / 'executor-home').iterdir()) == []
        lease.validate()
        assert host.records.list_all() == before
    finally:
        lease.close()
    assert lease.environment == {} and lease.secret_values == ()


def test_actual_model_revision_change_invalidates_original_host_and_lease(actual_host):
    host, models, turn, plan = actual_host
    lease = host.prepare(turn, plan, mcp_config=memory_config())
    try:
        current = models.public_for_purpose('generation')
        models.update('generation', {**API, 'model': 'another-model', 'enabled': True,
            'allow_remote': True, 'expected_revision': current['revision']})
        assert dict(plan.codex_api) == API
        with pytest.raises(ExternalHostError, match='^external_host_model_changed$'):
            lease.validate()
        with pytest.raises(ExternalHostError, match='^external_host_model_changed$'):
            host.codex_api_configuration('codex')
    finally:
        lease.close()


def test_host_rejects_missing_managed_argv_before_native(actual_host):
    host, _, turn, plan = actual_host
    legacy = build_launch_plan('codex', cli_version=plan.cli_version, executable=Path(plan.command[0]),
        cwd=plan.cwd, task=plan.input_text, mcp_config=memory_config())
    with pytest.raises(ExternalHostError, match='^external_host_request_invalid$'):
        host.prepare(turn, legacy, mcp_config=memory_config())


def test_client_managed_registration_does_not_adopt_application_model_or_network(actual_host):
    host, models, _, plan = actual_host
    registration = replace(host.registrations['codex'], controlled_api=False)
    client_host = HostAdmission(deployment=host.deployment, owner_id=host.owner_id, records=host.records,
        registrations={'codex': registration}, models=models,
        network_environment={'HTTPS_PROXY': 'http://127.0.0.1:9'})
    assert client_host.codex_api_configuration('codex') is None
    environment, _ = client_host._environment_for(registration, plan)
    assert 'HTTPS_PROXY' not in environment
    assert environment['CODEX_HOME'] == str(registration.authentication_root)


def test_native_lease_rejects_network_change_before_execution(actual_host):
    host, _, turn, plan = actual_host
    lease = host.prepare(turn, plan, mcp_config=memory_config())
    try:
        host._network_environment = {'HTTPS_PROXY': 'http://127.0.0.1:8'}
        with pytest.raises(ExternalHostError, match='^external_host_model_changed$'):
            lease.validate()
    finally:
        lease.close()


@pytest.mark.parametrize('environment', [{'NODE_OPTIONS': '--inspect'}, {'HTTPS_PROXY': 'invalid\nvalue'}])
def test_host_refuses_unlisted_or_control_character_network_fields(actual_host, environment):
    host, models, _, _ = actual_host
    with pytest.raises(ExternalHostError, match='^external_host_environment_invalid$'):
        HostAdmission(deployment=host.deployment, owner_id=host.owner_id, records=host.records,
            registrations=host.registrations, models=models, network_environment=environment)


@pytest.mark.parametrize('shape', ['legacy', 'materials', 'managed'])
def test_real_runner_reads_original_and_managed_archive_shapes(actual_host, shape):
    from backend.memory_app.v2.external_context import ExternalContext
    from backend.memory_app.v2.external_runner import ExternalRunner
    from core.document_engine import SQLiteDocumentRepository
    host, _, turn, plan = actual_host
    context = ExternalContext(host.records, owner_id=host.owner_id, documents=SQLiteDocumentRepository(host.records))
    runner = ExternalRunner(host.records, owner_id=host.owner_id, context=context, host=host, turns=context.turns)
    launch = {'executor': 'codex', 'cli_version': '0.156.1', 'preset': 'workspace',
        'commands': 'disabled', 'cwd': str(plan.cwd)}
    if shape != 'legacy':
        launch.update(material_directory=None, input_policy=None, stdin_text=plan.stdin_text, materials={})
    if shape == 'managed':
        launch['codex_api'] = dict(plan.codex_api)
    archive = {'launch': launch, 'request': turn, 'turn_id': turn['turn_id'], 'mcp_config': memory_config()}
    before = host.records.list_all()
    recovered = runner._archive_plan(json.loads(json.dumps(archive)))
    if shape == 'managed':
        assert recovered == plan and recovered.command == plan.command
    else:
        snapshot = json.loads((Path(__file__).parents[2] / 'fixtures/codex-01561-legacy-argv.json').read_text(encoding='utf-8'))
        substitutions = {'EXE': str(host.registrations['codex'].executable), 'CWD': str(plan.cwd),
            'PYTHON_TOML': 'mcp_servers.chriptmas-memory.command=' + json.dumps(sys.executable),
            'ARGS_TOML': 'mcp_servers.chriptmas-memory.args=' + json.dumps(
                memory_config()['mcpServers']['chriptmas-memory']['args'], separators=(',', ':'))}
        historical = tuple(substitutions.get(value, value) for value in snapshot)
        trimmed, index = [], 0
        while index < len(recovered.command):
            value = recovered.command[index]
            if (value == '-c' and recovered.command[index + 1].split('=', 1)[0]
                    in {'mcp_servers.chriptmas-memory.env.' + name for name in NETWORK_NAMES}):
                index += 2
                continue
            trimmed.append(value)
            index += 1
        assert tuple(trimmed) == historical
        config = command_config(recovered)
        assert not {'model', 'model_provider', 'model_providers'} & config.keys()
        assert all(config['mcp_servers']['chriptmas-memory']['env'][name] == '' for name in NETWORK_NAMES)
        with pytest.raises(ExternalHostError, match='^external_host_request_invalid$'):
            host.prepare(turn, recovered, mcp_config=memory_config())
    assert host.records.list_all() == before
    assert 'synthetic-codex-api-only' not in json.dumps(archive)
