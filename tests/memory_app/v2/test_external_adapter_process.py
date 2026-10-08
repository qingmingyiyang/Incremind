"""固定适配器 argv 经真实模拟 CLI 进程；不把模拟检查当 OS 沙箱。"""
import json
import os
from pathlib import Path
import sys

import pytest

from backend.memory_app.v2.external_adapters import build_launch_plan
from backend.memory_app.v2.external_events import ExternalEventParser
from backend.memory_app.v2.external_process import run_process


CLI = r'''
import json, os, sys, tomllib
from pathlib import Path
sys.stdout.reconfigure(encoding='utf-8')
executor = sys.argv[1]
argv = sys.argv[2:]
task = sys.stdin.buffer.read().decode('utf-8')
if executor == 'codex':
    assert argv[0] == 'exec' and argv[-1] == '-'
    assert Path(argv[argv.index('-C') + 1]) == Path.cwd()
    config = tomllib.loads('\n'.join(argv[index + 1] for index, token in enumerate(argv) if token == '-c'))
    assert config['features']['shell_tool'] is False
    assert config['features']['hooks'] is False
    assert config['features']['plugins'] is False
    assert config['notify'] == []
    assert config['mcp_servers']['chriptmas-memory']['env_vars'] == ['DEVICE_KEY']
else:
    assert argv[0] == '-p'
    assert '--restricted' in argv and '--no-session-persistence' in argv
    assert '--no-chrome' in argv
    config = json.loads(argv[argv.index('--mcp-config') + 1])
    assert config['mcpServers']['chriptmas-memory']['env'] == {'DEVICE_KEY':'${APPROVED_DEVICE_KEY}'}
    assert 'mcp__chriptmas-memory__recall' in argv[argv.index('--allowedTools') + 1].split(',')
assert task == '核对合成材料 M1\n只交付草稿😀'
assert os.environ['DEVICE_KEY'] == os.environ['APPROVED_DEVICE_KEY']
secret = os.environ['DEVICE_KEY']
assert all(secret not in value for value in argv)
Path('adapter-proof.json').write_text(json.dumps({'cwd':os.getcwd(), 'task':task,
    'executor':executor, 'stdin_received':True, 'secret_received':True}), encoding='utf-8')
def emit(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)
if executor == 'codex':
    emit({'type':'thread.started'})
    emit({'type':'item.completed','item':{'type':'agent_message','text':'完成 '+secret}})
    emit({'type':'turn.completed','usage':{'input_tokens':2,'output_tokens':3}})
else:
    emit({'type':'system','subtype':'init'})
    emit({'type':'assistant','message':{'content':[{'type':'text','text':'完成 '+secret}]}})
    emit({'type':'result','subtype':'success','is_error':False,'usage':{'input_tokens':2,'output_tokens':3}})
print(secret, file=sys.stderr, flush=True)
'''


@pytest.mark.parametrize('executor', ['codex', 'claude-code'])
@pytest.mark.parametrize('preset', ['research', 'workspace', 'folder'])
def test_fixed_adapter_request_reaches_real_fake_process_without_secret_exposure(tmp_path, executor, preset):
    workspace = tmp_path / '任务目录'
    workspace.mkdir()
    task = '核对合成材料 M1\n只交付草稿😀'
    config = {'mcpServers': {'chriptmas-memory': {'command': sys.executable,
        'args': ['-I', '-m', 'backend.memory_app.mcp'], 'env': {'DEVICE_KEY':'${APPROVED_DEVICE_KEY}'}}}}
    plan = build_launch_plan(executor, cli_version='0.156.1' if executor == 'codex' else '2.1.257',
        executable=Path(sys._base_executable), cwd=workspace, task=task, mcp_config=config, preset=preset)
    script = tmp_path / 'fake-adapter-cli.py'
    script.write_text(CLI, encoding='utf-8')
    secret = '-'.join(('synthetic', 'adapter', 'credential', 'value'))
    environment = {key: os.environ[key] for key in ('SystemRoot', 'WINDIR') if key in os.environ}
    environment.update({'DEVICE_KEY':secret, 'APPROVED_DEVICE_KEY':secret})
    parser = ExternalEventParser(executor, secret_values=(secret,))
    events = []
    starts = []
    def receive(line):
        events.extend(parser.feed_line(line))
    # 假 CLI 由 Python 承载，其余参数逐字使用原适配器；没有替换被测适配器或进程 owner。
    result = run_process([str(sys._base_executable), '-I', '-S', str(script), executor, *plan.command[1:]],
        cwd=plan.cwd, environment=environment, input_text=plan.input_text, secret_values=(secret,),
        on_line=receive, on_started=lambda: starts.append(True), timeout=10, output_limit=65536)
    assert result.status == 'completed' and result.exit_code == 0
    assert starts == [True]
    assert events[0] == {'kind':'started'}
    assert events[-1] == {'kind':'finished', 'status':'completed'} and not parser.failed
    assert [value for value in events if value['kind'] == 'message'] == [
        {'kind':'message', 'text':'完成 [REDACTED_SECRET]'}]
    assert parser.usage == {'input_tokens':2, 'output_tokens':3}
    assert plan.environment_aliases == (('DEVICE_KEY','APPROVED_DEVICE_KEY'),)
    proof = (workspace / 'adapter-proof.json').read_text(encoding='utf-8')
    assert json.loads(proof) == {'cwd':str(workspace), 'task':task, 'executor':executor,
        'stdin_received':True, 'secret_received':True}
    assert secret not in proof and secret not in repr(plan.command)
    assert secret not in repr(events) and secret not in result.tail
    assert '[REDACTED_SECRET]' in result.tail
