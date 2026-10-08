"""临时假 CLI 经真实目录、进程与解析器交付两种事件格式。"""
import json
import os
from pathlib import Path
import sys

import pytest

from backend.memory_app.v2.budget import text_tokens
from backend.memory_app.v2.external_events import ExternalEventParser
from backend.memory_app.v2.external_process import run_process
from backend.memory_app.v2.external_workspace import create_task_workspace
from backend.memory_app.v2.policies import get


CLI = r'''
import json, os, sys
from pathlib import Path
sys.stdout.reconfigure(encoding='utf-8', newline='\n')
sys.stderr.reconfigure(encoding='utf-8', newline='\n')
executor = sys.argv[1]
task = sys.stdin.buffer.read()
context = Path('CONTEXT.md').read_bytes()
config = json.loads(Path('memory-mcp.json').read_text(encoding='utf-8'))
proof = {'args': sys.argv[1:], 'cwd': os.getcwd(), 'stdin': list(task),
    'task': list(Path('TASK.md').read_bytes()), 'context': list(context),
    'mcp': config, 'secret_received': bool(os.environ.get('SYNTHETIC_VALUE'))}
Path('pipeline-proof.json').write_text(json.dumps(proof, ensure_ascii=False), encoding='utf-8')
secret = os.environ['SYNTHETIC_VALUE']
failed = FAIL
def emit(event):
    print(json.dumps(event, ensure_ascii=False), flush=True)
if executor == 'codex':
    emit({'type':'thread.started', 'thread_id':'synthetic-thread',
        'environment':{'SYNTHETIC_VALUE':secret}, 'cwd':os.getcwd()})
    emit({'type':'turn.started'})
    emit({'type':'item.started', 'item':{'type':'command_execution',
        'command':'private command '+secret, 'arguments':{'value':secret}}})
    emit({'type':'item.completed', 'item':{'type':'agent_message',
        'text':'合成 CLI 答复 '+secret}})
    emit({'type':'turn.failed' if failed else 'turn.completed',
        'usage':{'input_tokens':17, 'output_tokens':5, 'cached_input_tokens':3},
        'error':{'message':secret}})
else:
    emit({'type':'system', 'subtype':'init', 'environment':{'SYNTHETIC_VALUE':secret},
        'cwd':os.getcwd(), 'tools':['Bash']})
    emit({'type':'stream_event', 'event':{'type':'message_start', 'message':{'id':'m1'}}})
    emit({'type':'stream_event', 'event':{'type':'content_block_start', 'index':0,
        'content_block':{'type':'tool_use', 'name':'Bash', 'input':{'command':secret}}}})
    for text in ('合成 CLI 答复 ', secret):
        emit({'type':'stream_event', 'event':{'type':'content_block_delta', 'index':1,
            'delta':{'type':'text_delta', 'text':text}}})
    emit({'type':'assistant', 'message':{'id':'m1', 'content':[
        {'type':'text', 'text':'合成 CLI 答复 '+secret}]}})
    emit({'type':'result', 'subtype':'error_during_execution' if failed else 'success',
        'is_error':failed, 'usage':{'input_tokens':17, 'output_tokens':5,
            'cache_read_input_tokens':3}, 'errors':[secret] if failed else []})
print(secret, file=sys.stderr, flush=True)
'''


@pytest.mark.parametrize('executor', ['codex', 'claude-code'])
@pytest.mark.parametrize('failed', [False, True], ids=['completed', 'protocol-failed'])
def test_real_fake_cli_uses_workspace_and_parser_without_secret_leaks(tmp_path, executor, failed):
    user = tmp_path / 'synthetic-user'
    user.mkdir()
    entry = {'object_id':'synthetic-source', 'layer':'L0', 'revision':2,
        'title':'合成来源', 'excerpt':'只使用已交接材料。', 'conditions':['本项目'],
        'sources':[{'type':'original_item', 'id':'synthetic-source', 'revision':2}]}
    handoff = get('handoff', version='@1')([entry], count_tokens=text_tokens)
    assert handoff['entries'][0]['id'] == 'M1'
    config = {'mcpServers':{'chriptmas-memory':{'command':sys.executable,
        'args':['-I', '-m', 'backend.memory_app.mcp'],
        'env':{'CHRIPTMAS_DEVICE_KEY':'${CHRIPTMAS_DEVICE_KEY}'}}}}
    task = '核查合成来源\n保留编号 M1 和任务说明😀'
    workspace = create_task_workspace(user, 'synthetic-turn', task=task,
        handoff=handoff, mcp_config=config)
    script = tmp_path / 'fake-cli.py'
    script.write_text(CLI.replace('failed = FAIL', 'failed = ' + repr(failed)), encoding='utf-8')
    secret = '-'.join(('synthetic', 'pipeline', 'secret', 'value'))
    environment = {'SYNTHETIC_VALUE':secret}
    if os.name == 'nt':
        environment['SystemRoot'] = os.environ['SystemRoot']
    parser = ExternalEventParser(executor, secret_values=(secret,))
    events = []
    def receive(line):
        events.extend(parser.feed_line(line))
    result = run_process([sys.executable, str(script), executor], cwd=workspace,
        environment=environment, input_text=task, timeout=10, output_limit=65536,
        on_line=receive, secret_values=(secret,))
    # 协议失败故意仍退出 0，调用方必须同时判断解析器终态。
    assert result.status == 'completed' and result.exit_code == 0
    assert parser.failed is failed
    assert events[0] == {'kind':'started'}
    assert {'kind':'tool', 'name':'command_execution'} in events
    assert [item for item in events if item['kind'] == 'message'] == [
        {'kind':'message', 'text':'合成 CLI 答复 [REDACTED_SECRET]'}]
    assert events[-1] == {'kind':'finished', 'status':'failed' if failed else 'completed'}
    cache = 'cached_input_tokens' if executor == 'codex' else 'cache_read_input_tokens'
    assert parser.usage == {'input_tokens':17, 'output_tokens':5, cache:3}
    assert secret not in repr(events) and secret not in repr(result.tail)
    assert '[REDACTED_SECRET]' in result.tail
    assert all(set(item) <= {'kind','stage','name','text','status'} for item in events)
    assert 'SYNTHETIC_VALUE' not in repr(events) and str(workspace) not in repr(events)
    assert 'private command' not in repr(events)
    proof = json.loads((workspace / 'pipeline-proof.json').read_text(encoding='utf-8'))
    assert proof['args'] == [executor] and Path(proof['cwd']) == workspace
    assert bytes(proof['stdin']) == bytes(proof['task']) == task.encode('utf-8')
    assert bytes(proof['context']) == handoff['text'].encode('utf-8')
    assert json.loads(bytes(proof['context']))['entries'][0]['id'] == 'M1'
    assert proof['mcp'] == config and proof['secret_received'] is True
    assert secret not in (workspace / 'pipeline-proof.json').read_text(encoding='utf-8')
