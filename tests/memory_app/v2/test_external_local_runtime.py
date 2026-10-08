"""原 Runtime、facts、invoke 与模拟本机 CLI；只隔离缺失的三项 MCP 资格接点。"""
import io
import json
import sys
import zipfile

import pytest

from core.ai_kernel.tool_invocation import intent_from_payload, outcome_from_payload
from core.effect_log import EffectState
from tests.memory_app.v2.test_external_app_composition import host_env
from tests.memory_app.v2.test_external_local_materials import local, historical_workspace
from backend.memory_app.v2.external_runner import ARCHIVE
from backend.memory_app.v2.external_execution import RESULT_KIND, RECEIPT_KIND


def runtime_cli(executable):
    from pip._vendor.distlib.resources import finder

    source = '''import json,sys,re
from pathlib import Path
sys.stdout.reconfigure(encoding='utf-8')
if sys.argv[1:]==['--version']:
 print('codex-cli 0.156.1'); sys.exit(0)
text=sys.stdin.buffer.read().decode('utf-8')
match=re.search(r'agent_workspaces/[A-Za-z0-9._-]+',text)
materials=Path(match.group(0)) if match else Path.cwd()
assert (materials/'TASK.md').read_text(encoding='utf-8')=='核查合成资料'
assert json.loads((materials/'CONTEXT.md').read_text(encoding='utf-8'))['entries'][0]['id']=='M1'
assert 'chriptmas-memory' in json.loads((materials/'memory-mcp.json').read_text(encoding='utf-8'))['mcpServers']
if match:
 assert (materials/'资料.txt').read_bytes()==b'synthetic-attachment'
 assert Path('user-file').read_bytes()==b'user-file'
else:
 assert text=='核查合成资料'
marker=Path('invoke-count')
marker.write_text(str(int(marker.read_text())+1) if marker.exists() else '1')
Path('invoke-stdin').write_text(text,encoding='utf-8')
print(json.dumps({'type':'turn.started'}),flush=True)
print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'合成完成'}}),flush=True)
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':2,'output_tokens':1}}),flush=True)
'''
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('__main__.py', source)
    executable.write_bytes(finder('pip._vendor.distlib').find('t64.exe').bytes
        + ('#!"' + sys._base_executable + '" -I -S\n').encode() + buffer.getvalue())


@pytest.mark.parametrize('legacy', [False, True])
def test_actual_runtime_freezes_facts_invokes_native_cli_and_replays_terminal(local, legacy):
    env, owner, identity, kw, probes = local
    runtime_cli(owner.host.registrations['codex'].executable)
    if legacy:
        frozen, _, plan = historical_workspace(local)
        # 原五字段档案在完整 Runtime 路径继续接受语义同值的 MCP JSON。
        (plan.cwd / 'memory-mcp.json').write_text(json.dumps(kw['mcp_config'], indent=4) + '\n', encoding='utf-8')
    else:
        folder = env.root / 'selected'; folder.mkdir()
        (folder / 'user-file').write_bytes(b'user-file')
        permission = owner.host.permissions.confirm_folder(folder, confirmed=True, expected_revision=0)
        frozen = owner.prepare(identity, **kw, preset='folder', folder=folder,
            attachments={'资料.txt':b'synthetic-attachment'},
            host_permission_refs={'folder':permission, 'commands':None})
        plan = owner._archive_plan(owner.turns.get_immutable_payload(identity, ARCHIVE)[1])
    saved = owner.turns.get_immutable_payload(identity, ARCHIVE)
    runner = env.http.app.state.ai_turn_runner
    assert owner.runtime is env.http.app.state.ai_runtime and owner.frozen_authorization is not None
    assert runner.accept_and_submit(frozen).turn_id == identity
    terminal = runner.wait_for_terminal(identity, timeout_seconds=12)
    events = owner.turns.events_after(identity)
    diagnostic = [(event['type'], event['data']['error_code']) for event in events]
    assert terminal is not None and terminal.status == 'completed', diagnostic
    assert events[-1]['type'] == 'turn.completed'
    assert not any(event['type'].startswith('model.') for event in events)
    assert (plan.cwd / 'invoke-count').read_text() == '1'
    assert (plan.cwd / 'invoke-stdin').read_text(encoding='utf-8') == plan.stdin_text
    if not legacy:
        assert (plan.cwd / 'user-file').read_bytes() == b'user-file'
    intents = [event for event in events if event['type'] == 'tool.intent.recorded']
    assert len(intents) == 1
    intent = intent_from_payload(owner.turns.get(intents[0]['data']['payload_ref']))
    assert intent.capability_id == 'external.task.execute'
    assert intent.arguments == {'binding_ref':saved[0]}
    assert intent.authorization_facts_ref is not None and intent.authorization_facts_revision is not None
    facts = owner.frozen_authorization.load(turn_id=identity, facts_ref=intent.authorization_facts_ref,
        facts_revision=intent.authorization_facts_revision)
    assert facts.facts.turn_id == identity
    outcomes = [event for event in events if event['type'] == 'tool.outcome.recorded']
    assert len(outcomes) == 1
    outcome = outcome_from_payload(owner.turns.get(outcomes[0]['data']['payload_ref']))
    assert outcome.status == 'completed' and outcome.effect_certainty == 'confirmed_applied'
    assert owner.turns.effect_runner.log.get(intent.invocation_id).state is EffectState.SETTLED_OK
    result = owner.turns.get_immutable_payload(identity, RESULT_KIND)
    receipt = owner.turns.get_immutable_payload(identity, RECEIPT_KIND)
    assert result[1]['status'] == receipt[1]['status'] == 'completed'
    assert receipt[1]['effect_certainty'] == 'confirmed_applied'
    assert outcome.payload_ref == result[0] and outcome.receipt_ref == receipt[0]
    run = env.records.read('v2_external_runs', identity)
    assert run.payload['status'] == 'completed'
    assert run.payload['started_at'] is not None and run.payload['ended_at'] is not None
    assert runner.accept_and_submit(frozen).status == 'completed'
    assert runner.wait_for_terminal(identity, timeout_seconds=1).status == 'completed'
    assert owner.turns.events_after(identity) == events
    assert owner.turns.get_immutable_payload(identity, ARCHIVE) == saved
    assert env.records.read('v2_external_runs', identity) == run
    assert (plan.cwd / 'invoke-count').read_text() == '1'
    assert probes == [True] and env.model.calls == 0
