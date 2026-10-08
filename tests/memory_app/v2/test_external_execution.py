"""真实原生合成 CLI、宿主准入与原 SQLite 运行事实。"""
import importlib
import gc
import io
import json
from pathlib import Path
import sys
import sqlite3
import tempfile
import zipfile

import pytest
from jsonschema import Draft202012Validator

from backend.memory_app.v2.external_adapters import build_launch_plan
from backend.memory_app.v2.external_host import HostAdmission, ExecutorRegistration
from backend.memory_app.v2.external_runs import ExternalRuns
from backend.shared.deployment import DeploymentLayout
from core.ai_kernel import SQLiteAITurnStore
from core.ai_kernel.dispatcher import ToolProviderFailure, ToolDispatchCancelled
from core.storage_provider import SQLiteStructuredRecordStore
from tests.memory_app.v2.test_external_host import frozen_turn
from tests.memory_app.v2.test_external_dispatch import context


@pytest.fixture
def setup(tmp_path):
    temporary = tempfile.TemporaryDirectory(prefix='T16.4-exec-', dir=tmp_path.anchor)
    root = Path(temporary.name)
    def build(executor='codex', mode='normal'):
        from pip._vendor.distlib.resources import finder
        user = root / (executor + '-' + mode)
        user.mkdir()
        auth = user / 'auth'; auth.mkdir()
        executable = user / 'fake.exe'
        source = '''import json,sys,os
from pathlib import Path
sys.stdout.reconfigure(encoding='utf-8')
if sys.argv[1:]==['--version']:
 print(VERSION)
 sys.exit(0)
Path('spawned').write_bytes(sys.stdin.buffer.read())
with Path('count').open('a') as f: f.write('1')
secret=os.environ.get('DEVICE_KEY','')
print(secret,file=sys.stderr)
if MODE=='crash': sys.exit(7)
def emit(x): print(json.dumps(x,ensure_ascii=False),flush=True)
if EXECUTOR=='codex':
 emit({'type':'turn.started'})
 emit({'type':'item.completed','item':{'type':'agent_message','text':('段落'*280000 if MODE=='many' else '合成消息 '+secret)}})
 emit({'type':'turn.completed','usage':{'input_tokens':4,'output_tokens':2}})
else:
 emit({'type':'system','subtype':'init'})
 emit({'type':'assistant','message':{'content':[{'type':'text','text':'合成消息 '+secret}]}})
 emit({'type':'result','subtype':'success','is_error':False,'usage':{'input_tokens':4,'output_tokens':2}})
'''
        source = 'VERSION='+repr('codex-cli 0.156.1' if executor=='codex' else '2.1.257 (Claude Code)')+'\nMODE='+repr(mode)+'\nEXECUTOR='+repr(executor)+'\n'+source
        buffer=io.BytesIO()
        with zipfile.ZipFile(buffer,'w') as archive: archive.writestr('__main__.py',source)
        executable.write_bytes(finder('pip._vendor.distlib').find('t64.exe').bytes + ('#!"'+sys._base_executable+'" -I -S\n').encode()+buffer.getvalue())
        turn=frozen_turn(); cwd=user/'agent_workspaces'/turn['turn_id']; cwd.mkdir(parents=True)
        config={'mcpServers':{'chriptmas-memory':{'command':sys.executable,'args':['-I','-m','backend.memory_app.mcp'],'env':{'DEVICE_KEY':'${APPROVED_DEVICE_KEY}'}}}}
        plan=build_launch_plan(executor,cli_version='0.156.1' if executor=='codex' else '2.1.257',executable=executable,cwd=cwd,task='合成任务',mcp_config=config)
        records=SQLiteStructuredRecordStore(user/'records.sqlite3')
        host=HostAdmission(deployment=DeploymentLayout('desktop',user,None),owner_id='local-user',registrations={executor:ExecutorRegistration(executor,executable,auth)},records=records,environment={},secret_environment={'APPROVED_DEVICE_KEY':'synthetic-execution-secret'})
        lease=host.prepare(turn,plan,mcp_config=config)
        turns=SQLiteAITurnStore(user/'ai-turns.sqlite3'); turns.claim_turn(turn)
        return lease,records,turns,turn,cwd,auth
    yield build
    # 原 EffectLog 初始化的临时 SQLite 连接由 Python 回收后再清理。
    gc.collect()
    temporary.cleanup()


def execute(values, **kwargs):
    lease,records,turns,turn,*_=values
    return importlib.import_module('backend.memory_app.v2.external_execution').execute_external(lease,context(),records=records,turns=turns,turn_id=turn['turn_id'],owner_id='local-user',**kwargs)


def validate_schema(name,value):
    path=Path(__file__).resolve().parents[3]/'core-contracts'/'ai'/('external-task-'+name+'.schema.json')
    Draft202012Validator(json.loads(path.read_text(encoding='utf-8'))).validate(value)


@pytest.mark.parametrize('executor',['codex','claude-code'])
def test_real_protocol_success_and_immutable_replay(setup,executor):
    values=setup(executor); lease,records,turns,turn,cwd,_=values
    try:
        dto=execute(values)
        assert set(dto)=={'summary','payload_ref','receipt_ref','evidence_refs'}
        validate_schema('result',dto)
        validate_schema('receipt',turns.get_immutable_payload(turn['turn_id'],'external-task-receipt-v1')[1])
        body=turns.get_immutable_payload(turn['turn_id'],'external-task-result-v1')[1]
        assert body['status']=='completed' and body['usage']=={'input_tokens':4,'output_tokens':2}
        assert '合成消息' in body['message'] and 'synthetic-execution-secret' not in json.dumps(body)
        assert cwd.joinpath('spawned').read_bytes()=='合成任务'.encode()
        assert ExternalRuns(records).read(turn['turn_id'],owner_id='local-user')['status']=='completed'
        assert execute(values)==dto and cwd.joinpath('count').read_text()=='1'
    finally: lease.close()


def test_crash_saved_then_unknown_and_no_restart(setup):
    values=setup(mode='crash'); lease,records,turns,turn,cwd,_=values
    try:
        for attempt in range(2):
            current=context()
            with pytest.raises(ToolProviderFailure) as caught:
                importlib.import_module('backend.memory_app.v2.external_execution').execute_external(lease,current,records=records,turns=turns,turn_id=turn['turn_id'],owner_id='local-user')
            assert caught.value.effect_certainty=='unknown'
            current.cancellation.request()
            with pytest.raises(ToolDispatchCancelled) as cancellation: current.checkpoint()
            assert cancellation.value.provider_started is (attempt==0)
        assert cwd.joinpath('count').read_text()=='1'
        assert ExternalRuns(records).read(turn['turn_id'],owner_id='local-user')['status']=='failed'
        assert turns.get_immutable_payload(turn['turn_id'],'external-task-result-v1')[1]['status']=='failed'
        validate_schema('receipt',turns.get_immutable_payload(turn['turn_id'],'external-task-receipt-v1')[1])
    finally: lease.close()


@pytest.mark.parametrize('change',['owner','lease','reserved'])
def test_no_spawn_on_binding_change_or_prior_reservation(setup,change):
    values=setup(); lease,records,turns,turn,cwd,auth=values
    try:
        if change=='lease': (auth/'config.toml').write_text('synthetic')
        if change=='reserved':
            p=lease.plan
            ExternalRuns(records).reserve(turn['turn_id'],owner_id='local-user',executor=p.executor,adapter_version=p.adapter_version,cli_version=p.cli_version,preset=p.preset,workspace=p.cwd)
        with pytest.raises(ToolProviderFailure):
            if change=='owner':
                importlib.import_module('backend.memory_app.v2.external_execution').execute_external(lease,context(),records=records,turns=turns,turn_id=turn['turn_id'],owner_id='other-user')
            else: execute(values)
        assert not cwd.joinpath('spawned').exists()
    finally: lease.close()


def test_complete_text_is_not_dispatch_deque(setup):
    values=setup(mode='many'); lease,_,turns,turn,_,_=values
    try:
        execute(values,output_limit=3000000)
        body=turns.get_immutable_payload(turn['turn_id'],'external-task-result-v1')[1]
        assert body['message']=='段落'*280000
        assert len(body['events'])==128
    finally: lease.close()


def test_terminal_sink_failure_saved_and_never_retried(setup):
    values=setup(); lease,records,turns,turn,cwd,_=values
    calls=[]
    def sink(event):
        calls.append(event['kind'])
        if event['kind']=='finished': raise RuntimeError('synthetic-execution-secret')
    try:
        with pytest.raises(ToolProviderFailure) as caught: execute(values,event_sink=sink)
        assert caught.value.effect_certainty=='unknown'
        assert calls.count('finished')==1
        body=turns.get_immutable_payload(turn['turn_id'],'external-task-result-v1')[1]
        assert body['status']=='failed' and 'synthetic-execution-secret' not in json.dumps(body)
        assert ExternalRuns(records).read(turn['turn_id'],owner_id='local-user')['status']=='failed'
        assert cwd.joinpath('count').read_text()=='1'
    finally: lease.close()


def test_busy_metadata_and_missing_terminal_payload_never_spawn(setup):
    values=setup(); lease,records,turns,turn,cwd,_=values
    try:
        p=lease.plan
        runs=ExternalRuns(records)
        runs.reserve('other-turn',owner_id='local-user',executor=p.executor,adapter_version=p.adapter_version,cli_version=p.cli_version,preset=p.preset,workspace=p.cwd)
        with pytest.raises(ToolProviderFailure) as caught: execute(values)
        assert caught.value.effect_certainty=='confirmed_none'
        assert not cwd.joinpath('spawned').exists()
        runs.finish('other-turn',owner_id='local-user',expected_revision=1,status='failed',exit_code=None)
        runs.reserve(turn['turn_id'],owner_id='local-user',executor=p.executor,adapter_version=p.adapter_version,cli_version=p.cli_version,preset=p.preset,workspace=p.cwd)
        runs.finish(turn['turn_id'],owner_id='local-user',expected_revision=1,status='failed',exit_code=None)
        with pytest.raises(ToolProviderFailure) as caught: execute(values)
        assert caught.value.effect_certainty=='unknown'
        assert not cwd.joinpath('spawned').exists()
    finally: lease.close()


@pytest.mark.parametrize('provider_failure',[False,True])
def test_before_launch_rejection_releases_real_reservation_without_cli(setup,provider_failure):
    values=setup(); lease,records,turns,turn,cwd,_=values
    def recheck():
        if provider_failure: raise ToolProviderFailure('external_material_changed',effect_certainty='unknown')
        raise ValueError('synthetic-execution-secret')
    try:
        with pytest.raises(ToolProviderFailure) as caught: execute(values,before_launch=recheck)
        assert caught.value.effect_certainty=='confirmed_none'
        assert not cwd.joinpath('spawned').exists()
        row=ExternalRuns(records).read(turn['turn_id'],owner_id='local-user')
        assert row['status']=='failed' and row['started_at'] is None
        assert turns.get_immutable_payload(turn['turn_id'],'external-task-receipt-v1')[1]['status']=='failed'
        validate_schema('receipt',turns.get_immutable_payload(turn['turn_id'],'external-task-receipt-v1')[1])
        p=lease.plan
        assert ExternalRuns(records).reserve('next-turn',owner_id='local-user',executor=p.executor,adapter_version=p.adapter_version,cli_version=p.cli_version,preset=p.preset,workspace=p.cwd).reservation_created
    finally: lease.close()


def test_real_spawn_with_sql_started_failure_replays_unknown(setup):
    values=setup(); lease,records,turns,turn,cwd,_=values
    with records.begin() as transaction:
        transaction.commit()
    connection=sqlite3.connect(records.database_path)
    try:
        connection.execute("CREATE TRIGGER deny_running BEFORE UPDATE ON crp_structured_records WHEN NEW.collection='v2_external_runs' AND json_extract(NEW.payload_json,'$.status')='running' BEGIN SELECT RAISE(ABORT,'synthetic started failure'); END")
        connection.commit()
    finally: connection.close()
    try:
        for attempt in range(2):
            current=context()
            with pytest.raises(ToolProviderFailure) as caught:
                importlib.import_module('backend.memory_app.v2.external_execution').execute_external(lease,current,records=records,turns=turns,turn_id=turn['turn_id'],owner_id='local-user')
            assert caught.value.effect_certainty=='unknown'
            current.cancellation.request()
            with pytest.raises(ToolDispatchCancelled) as cancellation: current.checkpoint()
            assert cancellation.value.provider_started is (attempt==0)
        # on_started 失败时 stdin 尚未交付，但真实进程启动已被观察。
        row=ExternalRuns(records).read(turn['turn_id'],owner_id='local-user')
        assert row['status']=='failed' and row['started_at'] is None
        receipt=turns.get_immutable_payload(turn['turn_id'],'external-task-receipt-v1')[1]
        assert receipt['effect_certainty']=='unknown'
        validate_schema('receipt',receipt)
    finally: lease.close()
