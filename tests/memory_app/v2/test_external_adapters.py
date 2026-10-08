"""纯适配器表达受限启动请求，不把命令请求视为权限证明。"""
from dataclasses import FrozenInstanceError
import importlib
import json
from pathlib import Path
import sys
import tomllib

import pytest


def build(tmp_path, executor='codex', **changes):
    module = importlib.import_module('backend.memory_app.v2.external_adapters')
    values = {'cli_version':'0.156.1' if executor == 'codex' else '2.1.257',
        'executable':tmp_path / ('codex.exe' if executor == 'codex' else 'claude.exe'),
        'cwd':tmp_path / 'task', 'task':'只经标准输入传任务😀',
        'mcp_config':{'mcpServers':{'chriptmas-memory':{'command':sys.executable,
            'args':['-I','-m','backend.memory_app.mcp'],
            'env':{'CHRIPTMAS_DEVICE_KEY':'${CHRIPTMAS_DEVICE_KEY}'}}}}}
    values.update(changes)
    return module.build_launch_plan(executor, **values)


def overrides(command):
    parts = [command[i + 1] for i, token in enumerate(command) if token == '-c']
    return tomllib.loads('\n'.join(parts))


@pytest.mark.parametrize('preset', ['research','workspace','folder'])
def test_codex_disabled_commands_and_fixed_argv_keep_task_outside_args(tmp_path, preset):
    plan = build(tmp_path, preset=preset)
    command = plan.command
    assert isinstance(command, tuple) and command[0] == str(tmp_path / 'codex.exe')
    assert command[1] == 'exec' and command[-1] == '-'
    assert {'--json','--ephemeral','--ignore-user-config','--ignore-rules','--strict-config',
        '--skip-git-repo-check'} <= set(command)
    assert command[command.index('-C') + 1] == str(plan.cwd)
    assert command[command.index('--sandbox') + 1] == ('read-only' if preset == 'research' else 'workspace-write')
    config = overrides(command)
    assert config['approval_policy'] == 'never' and config['web_search'] == 'live'
    assert config['features']['shell_tool'] is False and config['features']['unified_exec'] is False
    assert config['features']['apps'] is False
    assert config['mcp_servers']['chriptmas-memory'] == {'command':sys.executable,
        'args':['-I','-m','backend.memory_app.mcp'], 'env_vars':['CHRIPTMAS_DEVICE_KEY']}
    assert plan.requested_commands == 'disabled' and plan.preset == preset
    assert plan.adapter_version == 'codex@1' and plan.cli_version == '0.156.1'
    assert plan.input_text == '只经标准输入传任务😀' and plan.input_text not in command
    assert not hasattr(plan, 'sandbox_verified') and not hasattr(plan, 'authorized')
    with pytest.raises(FrozenInstanceError):
        plan.preset = 'other'


@pytest.mark.parametrize('commands', ['sandboxed','explicit'])
@pytest.mark.parametrize('executor', ['codex','claude-code'])
def test_research_never_requests_command_tools(tmp_path, executor, commands):
    with pytest.raises(ValueError, match='^external_adapter_commands_invalid$'):
        build(tmp_path, executor, preset='research', commands=commands)


@pytest.mark.parametrize('commands', ['sandboxed','explicit'])
@pytest.mark.parametrize('preset', ['workspace','folder'])
def test_execution_requests_are_visible_not_granted(tmp_path, commands, preset):
    plan = build(tmp_path, commands=commands, preset=preset)
    assert plan.requested_commands == commands
    config = overrides(plan.command)
    assert config['features']['shell_tool'] is True and config['features']['unified_exec'] is True
    assert '--dangerously-bypass-approvals-and-sandbox' not in plan.command


@pytest.mark.parametrize('preset', ['research','workspace','folder'])
def test_claude_has_exact_tools_mcp_read_boundary_and_text_stdin(tmp_path, preset):
    plan = build(tmp_path, 'claude-code', preset=preset)
    command = plan.command
    assert {'--restricted','-p','--verbose','--include-partial-messages','--strict-mcp-config'} <= set(command)
    assert command[command.index('--output-format') + 1] == 'stream-json'
    assert command[command.index('--input-format') + 1] == 'text'
    assert command[command.index('--permission-mode') + 1] == 'dontAsk'
    tools = 'Read,Glob,Grep,WebSearch' + ('' if preset == 'research' else ',Edit,Write')
    assert command[command.index('--tools') + 1] == tools
    assert command[command.index('--allowedTools') + 1] == tools + ',' + ','.join(
        'mcp__chriptmas-memory__' + name for name in
        ('projects','recall','methods','read','remember','propose_insight','report_use'))
    deny = command[command.index('--disallowedTools') + 1].split(',')
    assert {'Bash','PowerShell','Monitor','NotebookEdit','Agent','Task','WebFetch'} <= set(deny)
    settings = json.loads(command[command.index('--settings') + 1])
    assert settings['permissions']['blockReadsOutsideWorkingDirectories'] is True
    assert settings['disableAllHooks'] is True
    mcp = json.loads(command[command.index('--mcp-config') + 1])
    assert set(mcp['mcpServers']) == {'chriptmas-memory'}
    assert mcp['mcpServers']['chriptmas-memory']['env'] == {
        'CHRIPTMAS_DEVICE_KEY':'${CHRIPTMAS_DEVICE_KEY}'}
    assert plan.input_text == '只经标准输入传任务😀' and plan.input_text not in command
    assert plan.adapter_version == 'claude-code@1'


@pytest.mark.parametrize('commands', ['sandboxed','explicit'])
def test_claude_command_request_only_adds_bash_and_keeps_other_denials(tmp_path, commands):
    plan = build(tmp_path, 'claude-code', commands=commands)
    assert plan.requested_commands == commands
    assert plan.command[plan.command.index('--tools') + 1].endswith(',Bash')
    deny = plan.command[plan.command.index('--disallowedTools') + 1].split(',')
    assert 'Bash' not in deny and 'PowerShell' in deny


@pytest.mark.parametrize('executor,version', [('codex','0.156.0'),('codex','0.157.1'),
    ('codex','0.156.1-dev'),('claude-code','2.1.256'),('claude-code','2.2.0'),
    ('claude-code','3.1.257'),('claude-code','2.1.257-beta')])
def test_unknown_cli_version_is_rejected(tmp_path, executor, version):
    with pytest.raises(ValueError, match='^external_adapter_version_unsupported$'):
        build(tmp_path, executor, cli_version=version)


def test_claude_same_minor_newer_patch_is_allowed_as_request_only(tmp_path):
    assert build(tmp_path, 'claude-code', cli_version='2.1.300').cli_version == '2.1.300'


@pytest.mark.parametrize('field,value', [('executable',Path('relative.exe')),
    ('executable',Path('C:/synthetic/cli.ps1')),('executable',Path('C:/synthetic/cli.cmd')),
    ('executable',Path('C:/synthetic/cli.bat')),('cwd',Path('relative')),
    ('task',''),('preset','unknown'),('commands',True),('commands','unrestricted')])
def test_invalid_launch_inputs_fail_without_echoing_values(tmp_path, field, value):
    with pytest.raises(ValueError, match='^external_adapter_request_invalid$'):
        build(tmp_path, **{field:value})


@pytest.mark.parametrize('config', [{'mcpServers':{}}, {'mcpServers':{'other':{}}},
    {'mcpServers':{'chriptmas-memory':{'command':sys.executable,'args':['--arbitrary']}}},
    {'mcpServers':{'chriptmas-memory':{'command':sys.executable,
        'args':['-I','-m','backend.memory_app.mcp'],'env':{'SECRET':'synthetic-full-secret'}}}}])
def test_mcp_uses_original_owner_validation(tmp_path, config):
    with pytest.raises(ValueError):
        build(tmp_path, mcp_config=config)


@pytest.mark.parametrize('executor', ['codex','claude-code'])
def test_http_mcp_only_references_bearer_environment(tmp_path, executor):
    endpoint = 'https://synthetic.example/mcp'
    config = {'mcpServers':{'chriptmas-memory':{'type':'http','url':endpoint,
        'headers':{'Authorization':'Bearer ${CHRIPTMAS_DEVICE_KEY}'}}}}
    plan = build(tmp_path, executor, mcp_config=config, memory_endpoint=endpoint)
    if executor == 'codex':
        assert overrides(plan.command)['mcp_servers']['chriptmas-memory'] == {
            'url':endpoint,'bearer_token_env_var':'CHRIPTMAS_DEVICE_KEY'}
    else:
        assert json.loads(plan.command[plan.command.index('--mcp-config') + 1]) == config


def test_codex_mcp_alias_is_requested_without_literal_env_or_lost_target(tmp_path):
    config = {'mcpServers':{'chriptmas-memory':{'command':sys.executable,
        'args':['-I','-m','backend.memory_app.mcp'], 'env':{'DEVICE_KEY':'${APPROVED_DEVICE_KEY}'}}}}
    plan = build(tmp_path, mcp_config=config)
    assert plan.environment_aliases == (('DEVICE_KEY','APPROVED_DEVICE_KEY'),)
    server = overrides(plan.command)['mcp_servers']['chriptmas-memory']
    assert server['env_vars'] == ['DEVICE_KEY'] and 'env' not in server
    assert '${APPROVED_DEVICE_KEY}' not in repr(plan.command)


@pytest.mark.parametrize('preset,commands', [('research','disabled'),
    ('workspace','disabled'),('workspace','sandboxed'),('workspace','explicit'),
    ('folder','disabled'),('folder','sandboxed'),('folder','explicit')])
def test_codex_controls_remain_disabled_in_every_valid_request(tmp_path, preset, commands):
    config = overrides(build(tmp_path, preset=preset, commands=commands).command)
    assert config['notify'] == []
    for feature in ('hooks','plugins','apps','multi_agent','shell_snapshot',
            'skill_mcp_dependency_install','code_mode_host'):
        assert config['features'][feature] is False
    assert config['features']['shell_tool'] is (commands != 'disabled')
    assert config['features']['unified_exec'] is (commands != 'disabled')


@pytest.mark.parametrize('preset', ['research','workspace','folder'])
def test_claude_disables_session_persistence_and_browser_expansion(tmp_path, preset):
    command = build(tmp_path, 'claude-code', preset=preset).command
    assert '--no-session-persistence' in command
    assert '--no-chrome' in command


@pytest.mark.parametrize('version', ['2.1.' + '9' * 11, '2.1.' + '9' * 5000,
    '9' * 11 + '.1.257', '2.' + '9' * 11 + '.257'])
def test_version_segment_length_has_fixed_error_before_integer_conversion(tmp_path, version):
    with pytest.raises(ValueError, match='^external_adapter_version_unsupported$'):
        build(tmp_path, 'claude-code', cli_version=version)


@pytest.mark.parametrize('preset,commands', [('research','disabled'),
    ('workspace','disabled'),('workspace','explicit'),('folder','disabled'),('folder','explicit')])
def test_claude_memory_permissions_are_exact_for_every_preset(tmp_path, preset, commands):
    command = build(tmp_path, 'claude-code', preset=preset, commands=commands).command
    tools = command[command.index('--tools') + 1].split(',')
    allowed = command[command.index('--allowedTools') + 1].split(',')
    assert allowed == tools + ['mcp__chriptmas-memory__' + name for name in
        ('projects','recall','methods','read','remember','propose_insight','report_use')]
    assert all(not name.startswith('mcp__') for name in tools)
    assert not any('*' in name for name in allowed)
