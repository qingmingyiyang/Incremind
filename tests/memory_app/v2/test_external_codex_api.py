import json
from copy import deepcopy
from pathlib import Path
import sys
import tomllib

import pytest

from backend.memory_app.v2.external_adapters import build_launch_plan
from core.ai_kernel.turn_kinds import freeze_turn_request


API = {'model': 'gpt-6.1-terra', 'base_url': 'https://api.openai.com/v1'}
NETWORK_NAMES = ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY', 'http_proxy', 'https_proxy',
    'all_proxy', 'no_proxy', 'SSL_CERT_FILE', 'SSL_CERT_DIR', 'CODEX_CA_CERTIFICATE')


def command_config(plan):
    return tomllib.loads('\n'.join(plan.command[index + 1]
        for index, token in enumerate(plan.command) if token == '-c'))


def memory_config():
    return {'mcpServers': {'chriptmas-memory': {'command': sys.executable,
        'args': ['-I', '-m', 'backend.memory_app.mcp']}}}


def make_plan(root, *, codex_api=None):
    return build_launch_plan('codex', cli_version='0.156.1', executable=root / 'codex.exe',
        cwd=root / 'task', task='合成任务', mcp_config=memory_config(), codex_api=codex_api)


def test_managed_provider_uses_frozen_model_and_memory_credentials(tmp_path):
    plan = make_plan(tmp_path, codex_api=API)
    config = command_config(plan)
    assert config['model'] == API['model'] and config['model_provider'] == 'chriptmas-managed'
    assert config['cli_auth_credentials_store'] == 'ephemeral'
    assert config['model_providers']['chriptmas-managed'] == {
        'name': 'Chriptmas managed API', 'base_url': API['base_url'], 'wire_api': 'responses',
        'env_key': 'CODEX_API_KEY', 'requires_openai_auth': False, 'supports_websockets': False,
        'request_max_retries': 0, 'stream_max_retries': 0}
    assert dict(plan.codex_api) == API
    assert {name: config['mcp_servers']['chriptmas-memory']['env'][name] for name in NETWORK_NAMES} == {
        name: '' for name in NETWORK_NAMES}
    restored = make_plan(tmp_path, codex_api=json.loads(json.dumps(dict(plan.codex_api))))
    assert restored == plan and restored.command == plan.command


def test_client_managed_launch_keeps_original_model_selection(tmp_path):
    plan = make_plan(tmp_path)
    config = command_config(plan)
    assert plan.codex_api == ()
    assert not {'model', 'model_provider', 'model_providers', 'cli_auth_credentials_store'} & config.keys()
    assert config['web_search'] == 'live'


@pytest.mark.parametrize('base', ['http://api.openai.com/v1', 'https://other.invalid/v1',
    'https://user:password@api.openai.com/v1', 'https://api.openai.com/v1?token=x',
    'https://api.openai.com/v1#fragment', 'http://127.0.0.1:8014/v1',
    'http://198.51.100.1:8014/api/v2/external-agent/proxy/codex/v1'])
def test_managed_model_rejects_other_upstreams(tmp_path, base):
    with pytest.raises(ValueError, match='^external_adapter_model_invalid$'):
        make_plan(tmp_path, codex_api={**API, 'base_url': base})


@pytest.mark.parametrize('base', ['http://127.0.0.1:8014/api/v2/external-agent/proxy/codex/v1',
    'https://[::1]:8014/api/v2/external-agent/proxy/codex/v1'])
def test_explicit_host_can_select_original_loopback_proxy(tmp_path, base):
    assert command_config(make_plan(tmp_path, codex_api={**API, 'base_url': base}))[
        'model_providers']['chriptmas-managed']['base_url'] == base


def test_public_external_output_policy_passes_both_kernel_contract_validators():
    from core.ai_kernel import AIKernelContractError, validate_turn_request
    from jsonschema import Draft202012Validator, ValidationError
    from backend.memory_app.v2.external_output_contract import output_policy, frozen_output_policy
    request = freeze_turn_request('project.task', template_version=2, turn_id='turn-' + 'f' * 32,
        session_id='session-output-policy', operation_id='op-output-policy', idempotency_key='output-policy-contract-20261104',
        project_id='default', created_at='2026-11-04T00:00:00Z',
        text=json.dumps({'task': 'Synthetic output', 'output': output_policy('@1')}, ensure_ascii=False),
        privacy={'mode': 'remote_allowed', 'allow_remote': True, 'pii': 'none', 'consent_refs': [], 'retention': 'session'},
        capability_request={'mode': 'execute_exact_v1', 'capability_id': 'external.task.execute',
            'arguments': {'binding_ref': 'crp://session/turn-' + 'f' * 32 + '/external-task-run-v1'}})
    request['policy_versions'] = {'handoff': '@1', 'external_task_output': '@1'}
    schema = json.loads((Path(__file__).parents[3] / 'core-contracts/ai/turn-request.schema.json').read_text(encoding='utf-8'))
    validator = Draft202012Validator(schema)
    validate_turn_request(request)
    validator.validate(request)
    assert frozen_output_policy(request) == output_policy('@1')
    for versions in ({}, {'external_task_output': '@0'}, {'external_task_output': 'v1'}):
        invalid = deepcopy(request)
        invalid['policy_versions'] = versions
        with pytest.raises(AIKernelContractError, match='policy versions are invalid'):
            validate_turn_request(invalid)
        with pytest.raises(ValidationError):
            validator.validate(invalid)
    missing = deepcopy(request)
    missing.pop('policy_versions')
    with pytest.raises(ValidationError):
        validator.validate(missing)
    request['policy_versions']['unregistered_external_output'] = '@1'
    with pytest.raises(AIKernelContractError, match='policy versions are invalid'):
        validate_turn_request(request)
    with pytest.raises(ValidationError):
        validator.validate(request)
