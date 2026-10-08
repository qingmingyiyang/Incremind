"""运行器与目录写入复用同一份记忆 MCP 配置校验。"""
import json
import sys

import pytest

from backend.memory_app.v2.external_workspace import (
    ExternalWorkspaceError, create_task_workspace, validate_memory_mcp_config,
)


def stdio():
    return {'mcpServers': {'chriptmas-memory': {'command': sys.executable,
        'args': ['-I', '-m', 'backend.memory_app.mcp'],
        'env': {'CHRIPTMAS_DEVICE_KEY': '${DEVICE_TOKEN}'}}}}


def test_public_validation_preserves_aliases_and_matches_actual_workspace_bytes(tmp_path):
    config = stdio()
    encoded = validate_memory_mcp_config(config)
    assert isinstance(encoded, bytes) and json.loads(encoded) == config
    workspace = create_task_workspace(tmp_path, 'turn-mcp', task='合成任务',
        handoff={'version': 'handoff@1', 'text': '', 'entries': [], 'profile': []}, mcp_config=config)
    assert (workspace / 'memory-mcp.json').read_bytes() == encoded
    config['mcpServers']['chriptmas-memory']['env']['CHRIPTMAS_DEVICE_KEY'] = 'changed'
    assert json.loads(encoded)['mcpServers']['chriptmas-memory']['env'] == {
        'CHRIPTMAS_DEVICE_KEY': '${DEVICE_TOKEN}'}


def test_public_http_validation_is_bound_to_the_host_endpoint():
    endpoint = 'https://memory.example/mcp'
    config = {'mcpServers': {'chriptmas-memory': {'type': 'http', 'url': endpoint,
        'headers': {'Authorization': 'Bearer ${CHRIPTMAS_DEVICE_KEY}'}}}}
    assert json.loads(validate_memory_mcp_config(config, memory_endpoint=endpoint)) == config
    with pytest.raises(ExternalWorkspaceError, match='^external_workspace_invalid$'):
        validate_memory_mcp_config(config, memory_endpoint='https://other.example/mcp')


@pytest.mark.parametrize('kind', ['extra_server', 'extra_arg', 'literal_credential'])
def test_public_validation_rejects_extra_capabilities_and_literal_credentials(kind):
    config = stdio()
    server = config['mcpServers']['chriptmas-memory']
    if kind == 'extra_server':
        config['mcpServers']['untrusted'] = dict(server)
    elif kind == 'extra_arg':
        server['args'].append('untrusted')
    else:
        server['env']['CHRIPTMAS_DEVICE_KEY'] = 'synthetic-value'
    with pytest.raises(ExternalWorkspaceError, match='^external_workspace_invalid$'):
        validate_memory_mcp_config(config)
