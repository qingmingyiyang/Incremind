"""真实临时目录验证外部任务材料及路径边界，不启动模型。"""
import json
import sys
import pytest


def test_python_memory_entry_rejects_nonisolated_module_argv():
    from backend.memory_app.v2.external_workspace import validate_memory_mcp_config
    value = {'mcpServers': {'chriptmas-memory': {
        'command': sys.executable, 'args': ['-m', 'backend.memory_app.mcp']}}}
    with pytest.raises(ExternalWorkspaceError, match='^external_workspace_invalid$'):
        validate_memory_mcp_config(value)

from backend.memory_app.v2.external_workspace import (
    ExternalWorkspaceError, create_task_workspace,
)


def handoff():
    from backend.memory_app.v2.policies import get
    from backend.memory_app.v2.budget import text_tokens
    entry = {'object_id': 'source-one', 'layer': 'L0', 'revision': 2,
             'title': '研究依据', 'excerpt': '先核查来源。', 'conditions': ['仅本项目'],
             'sources': [{'type': 'original_item', 'id': 'source-one', 'revision': 2}]}
    return get('handoff', version='@1')([entry], count_tokens=text_tokens)


def mcp():
    return {'mcpServers': {'chriptmas-memory': {
        'command': sys.executable, 'args': ['-I', '-m', 'backend.memory_app.mcp'],
        'env': {'CHRIPTMAS_DEVICE_KEY': '${CHRIPTMAS_DEVICE_KEY}'},
    }}}


def create(root, turn='turn-one', **kw):
    return create_task_workspace(root, turn, task='核查资料来源',
                                handoff=handoff(), mcp_config=mcp(), **kw)


def files(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}


def test_materials_are_numbered_complete_and_confined(tmp_path):
    root = tmp_path / 'user-one'
    root.mkdir()
    before = files(root)
    received = handoff()
    workspace = create(root, attachments={'资料证据.txt': '附件'.encode()})
    assert workspace == root / 'agent_workspaces' / 'turn-one'
    assert set(p.name for p in workspace.iterdir()) == {
        'CONTEXT.md', 'TASK.md', 'memory-mcp.json', '资料证据.txt'}
    assert (workspace / 'CONTEXT.md').read_text('utf-8') == received['text']
    assert json.loads((workspace / 'CONTEXT.md').read_text('utf-8'))['entries'][0]['id'] == 'M1'
    assert (workspace / 'TASK.md').read_text('utf-8') == '核查资料来源'
    assert json.loads((workspace / 'memory-mcp.json').read_text('utf-8')) == mcp()
    assert (workspace / '资料证据.txt').read_bytes() == '附件'.encode()
    assert before == {}


def test_existing_directory_is_never_overwritten(tmp_path):
    root = tmp_path / 'user'
    root.mkdir()
    target = create(root)
    (target / 'TASK.md').write_text('用户后来改的任务', encoding='utf-8')
    before = files(root)
    with pytest.raises(ExternalWorkspaceError, match='external_workspace_exists'):
        create(root)
    assert files(root) == before


@pytest.mark.parametrize('turn', ['../other', '..', '.', 'a/b', r'a\b', 'C:other',
                                  'CON', 'aux.txt', 'tail.', 'tail ', '', 'x' * 129])
def test_invalid_turn_has_zero_writes(tmp_path, turn):
    with pytest.raises(ExternalWorkspaceError, match='external_workspace_invalid'):
        create(tmp_path, turn)
    assert files(tmp_path) == {} and list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('name', ['../outside', 'sub/file', r'sub\file', 'CONTEXT.md',
                                  'context.MD', 'TASK.md', 'memory-mcp.json', 'NUL.txt'])
def test_attachment_cannot_escape_or_replace_control_files(tmp_path, name):
    with pytest.raises(ExternalWorkspaceError, match='external_workspace_invalid'):
        create(tmp_path, attachments={name: b'user bytes'})
    assert list(tmp_path.iterdir()) == []


def test_inconsistent_frozen_handoff_rejected_before_writes(tmp_path):
    value = handoff()
    value['entries'][0]['excerpt'] = '变更后的正文'
    with pytest.raises(ExternalWorkspaceError, match='external_workspace_invalid'):
        create_task_workspace(tmp_path, 'turn-one', task='任务', handoff=value, mcp_config=mcp())
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('values', [
    {'mcpServers': {'chriptmas-memory': {'command': sys.executable,
                                     'args': ['-I', '-m', 'backend.memory_app.mcp'],
                                     'env': {'CHRIPTMAS_DEVICE_KEY': 'synthetic-private-value'}}}},
    {'mcpServers': {'other-service': {'command': 'python', 'args': []}}},
    {'mcpServers': {'chriptmas-memory': {'url': 'https://example.test/mcp?key=secret'}}},
])
def test_mcp_rejects_literal_credentials_and_uncontrolled_servers(tmp_path, values):
    with pytest.raises(ExternalWorkspaceError, match='external_workspace_invalid'):
        create_task_workspace(tmp_path, 'turn-one', task='任务', handoff=handoff(), mcp_config=values)
    assert list(tmp_path.iterdir()) == []


def test_mcp_http_uses_bearer_environment_reference(tmp_path):
    config = {'mcpServers': {'chriptmas-memory': {
        'type': 'http', 'url': 'https://example.test/mcp',
        'headers': {'Authorization': 'Bearer ${CHRIPTMAS_DEVICE_KEY}'},
    }}}
    target = create_task_workspace(tmp_path, 'turn-one', task='任务',
                                   handoff=handoff(), mcp_config=config,
                                   memory_endpoint='https://example.test/mcp')
    assert json.loads((target / 'memory-mcp.json').read_text('utf-8')) == config


@pytest.mark.parametrize('server', [
    {'command': sys.executable, 'args': ['-c', 'print(1)']},
    {'command': sys.executable, 'args': ['-I', '-m', 'backend.memory_app.mcp', '--device-key', 'synthetic-value']},
    {'command': sys.executable, 'args': ['-m', 'another.service']},
    {'type': 'http', 'url': 'https://another.test/mcp',
     'headers': {'Authorization': 'Bearer ${CHRIPTMAS_DEVICE_KEY}'}},
])
def test_only_host_memory_endpoint_and_pinned_stdio_entry_are_written(tmp_path, server):
    with pytest.raises(ExternalWorkspaceError, match='external_workspace_invalid'):
        create_task_workspace(tmp_path, 'turn-one', task='任务', handoff=handoff(),
                              mcp_config={'mcpServers': {'chriptmas-memory': server}},
                              memory_endpoint='https://example.test/mcp')
    assert list(tmp_path.iterdir()) == []


def test_relative_python_alias_cannot_change_mcp_executable_after_chdir(tmp_path):
    config = mcp()
    config['mcpServers']['chriptmas-memory']['command'] = '.venv/Scripts/python.exe'
    with pytest.raises(ExternalWorkspaceError, match='external_workspace_invalid'):
        create_task_workspace(tmp_path, 'turn-one', task='任务', handoff=handoff(), mcp_config=config)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('address', ['https://[broken', 'https://example.test:bad/mcp'])
def test_bad_http_address_returns_fixed_code_before_any_write(tmp_path, address):
    server = {'type': 'http', 'url': address,
              'headers': {'Authorization': 'Bearer ${CHRIPTMAS_DEVICE_KEY}'}}
    with pytest.raises(ExternalWorkspaceError, match='external_workspace_invalid'):
        create_task_workspace(tmp_path, 'turn-one', task='任务', handoff=handoff(),
                              mcp_config={'mcpServers': {'chriptmas-memory': server}},
                              memory_endpoint=address)
    assert list(tmp_path.iterdir()) == []


def test_parent_swap_after_preflight_cannot_redirect_writes(tmp_path, monkeypatch):
    import os
    import subprocess
    from backend.memory_app.v2 import external_workspace as module
    root, outside = tmp_path / 'user', tmp_path / 'outside'
    root.mkdir()
    outside.mkdir()
    parent = root / 'agent_workspaces'
    original_context = module._context

    def swap_after_check(value):
        # 保留真实材料校验，在路径预检之后模拟另一线程换掉父目录。
        result = original_context(value)
        if os.name == 'nt':
            made = subprocess.run(['cmd', '/c', 'mklink', '/J', str(parent), str(outside)],
                                  capture_output=True, text=True)
            assert made.returncode == 0
        else:
            parent.symlink_to(outside, target_is_directory=True)
        return result

    monkeypatch.setattr(module, '_context', swap_after_check)
    try:
        with pytest.raises(ExternalWorkspaceError):
            create(root)
        assert list(outside.iterdir()) == []
    finally:
        if parent.exists():
            if os.name == 'nt':
                os.rmdir(parent)
            else:
                parent.unlink()


def test_linked_workspace_parent_rejected_without_writes(tmp_path):
    import os
    import subprocess
    root, outside = tmp_path / 'user', tmp_path / 'outside'
    root.mkdir()
    outside.mkdir()
    parent = root / 'agent_workspaces'
    if os.name == 'nt':
        # junction 不要求符号链接特权；这里只连接两个测试临时目录。
        result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(parent), str(outside)],
                                capture_output=True, text=True)
        assert result.returncode == 0
    else:
        parent.symlink_to(outside, target_is_directory=True)
    try:
        with pytest.raises(ExternalWorkspaceError, match='external_workspace_invalid'):
            create(root)
        assert list(outside.iterdir()) == []
    finally:
        # 只移除测试创建的连接本身，不递归操作被连接的目录。
        if os.name == 'nt':
            os.rmdir(parent)
        else:
            parent.unlink()
