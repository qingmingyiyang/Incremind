"""The real intake, SQLite and Turn runtime must own organization calls."""
from datetime import datetime, timezone

import pytest

from backend.memory_app.processing_lease import ProcessingLease
from backend.memory_app.v2.turn_requests import freeze_product_turn, validate_frozen_inputs
from core.ai_kernel.sqlite_store import SQLiteAITurnStore
from tests.memory_app.test_workspace import client
from tests.memory_app.test_workspace import Model


def test_real_governed_organize_receipts_are_aux(tmp_path, monkeypatch):
    from pathlib import Path
    (tmp_path / 'config').mkdir()
    (tmp_path / 'config/settings.toml').write_bytes(
        (Path(__file__).resolve().parents[2] / 'config/settings.toml.example').read_bytes())
    monkeypatch.setenv('CHRIPTMAS_APP_ROOT', str(tmp_path))
    from tests.memory_app.test_api import TurnModels
    from core.storage_provider import SQLiteStructuredRecordStore
    import litellm
    records = SQLiteStructuredRecordStore(tmp_path / 'recognition.sqlite3')
    models = TurnModels(records, tmp_path)
    models.update('generation', {'base_url':'https://example.test', 'model':'synthetic-model',
        'api_key':'synthetic-only', 'allow_remote':True, 'expected_revision':0})
    models.handler = lambda messages, **kw: Model().complete(messages, max_tokens=7000)[0]
    http, records = client(tmp_path, models)
    item = http.post('/api/workspace/v1/items/text', json={'project_id':'alpha','text':'原文证据'}).json()
    result = http.post(f"/api/workspace/v1/items/{item['id']}/process", json={'project_id':'alpha'}).json()
    assert result['status'] == 'ready', result
    assert len(models.calls) == 1
    store = SQLiteAITurnStore(tmp_path / '.rebuild-data' / 'ai-turns.sqlite3')
    step = records.list('workspace_organize_steps')[0]
    events = store.events_after(step.payload['turn_id'])
    receipt = next(e for e in events if e['type'] == 'model.completed')
    assert store.get(receipt['data']['receipt_ref'])['model_call_purpose'] == 'aux'
    assert len([e for e in events if e['type'] == 'model.attempt.dispatched']) == 1


def test_completed_output_survives_new_service_without_second_call(tmp_path):
    from backend.memory_app.v2.organize_turns import OrganizeTurns
    http, records = client(tmp_path)
    item = http.post('/api/workspace/v1/items/text', json={'project_id':'alpha','text':'原文证据'}).json()
    calls = []
    class Provider(Model):
        def complete(self, messages, **kw):
            calls.append(messages)
            return super().complete(messages, **kw)
    kwargs = dict(root=tmp_path,records=records,models=Provider(),item_id=item['id'],
                  project_id='alpha',source='原文证据',validate_current=lambda:None)
    messages = [{'role':'user','content':'原文证据'}]
    first = OrganizeTurns(**kwargs).complete(messages,max_tokens=10,validate_current=lambda:None)
    restored = OrganizeTurns(**kwargs).complete(messages,max_tokens=10,validate_current=lambda:None)
    assert first == restored
    assert len(calls) == 1


def test_intake_records_aux_organize_turn(tmp_path):
    http, records = client(tmp_path)
    item = http.post('/api/workspace/v1/items/text', json={'project_id':'alpha', 'text':'原文证据'}).json()
    result = http.post(f"/api/workspace/v1/items/{item['id']}/process", json={'project_id':'alpha'})
    assert result.status_code == 200
    assert result.json()['status'] == 'ready'
    store = SQLiteAITurnStore(tmp_path / '.rebuild-data' / 'ai-turns.sqlite3')
    checkpoints = records.list('workspace_organize_steps')
    assert len(checkpoints) == 1
    turn = store.get_request(checkpoints[0].payload['turn_id'])
    assert turn['desired_outcome'] == 'memory.organize'
    assert turn['execution_policy']['purpose'] == 'aux'
    assert store.events_after(turn['turn_id'])[-1]['type'] == 'turn.completed'


def test_invalid_saved_output_can_be_retried_explicitly(tmp_path):
    from backend.memory_app.v2.organize_turns import OrganizeTurns
    http, records = client(tmp_path)
    item = http.post('/api/workspace/v1/items/text', json={'project_id':'alpha','text':'原文证据'}).json()
    calls = []
    class Provider(Model):
        def complete(self, messages, **kw):
            calls.append(messages)
            return ('invalid' if len(calls) == 1 else 'valid'), {}
    def validate(text):
        if text != 'valid':
            raise ValueError('synthetic invalid schema')
    kwargs = dict(root=tmp_path,records=records,models=Provider(),item_id=item['id'],
                  project_id='alpha',source='原文证据',validate_current=lambda:None)
    call = dict(max_tokens=10,validate_current=lambda:None,validate_output=validate)
    assert OrganizeTurns(**kwargs).complete([], **call)[0] == 'invalid'
    assert OrganizeTurns(**kwargs).complete([], **call)[0] == 'valid'
    assert len(calls) == 2


def test_processing_heartbeat_preserves_frozen_source_authority(tmp_path):
    http, records = client(tmp_path)
    item = http.post('/api/workspace/v1/items/text', json={'project_id':'alpha', 'text':'原文证据'}).json()
    now = [100.0]
    lease = ProcessingLease(records, 'workspace_items', 'test-instance', clock=lambda: now[0])
    lease.claim(item['id'], 'alpha', item['revision'], 'run-one', None, {})
    row = records.read('workspace_items', item['id'])
    models = type('Models', (), {'public': lambda self: {'generation':{'allow_remote':False}}})()
    request = freeze_product_turn('memory.organize', records=records, models=models,
        project_id='alpha', local_only=True,
        materials=[{'type':'original_item','id':item['id'],'revision':row.revision,'project_id':'alpha'}],
        load_text=lambda value:value['payload']['source_text'], turn_id='turn-'+'a'*32,
        session_id='session-alpha', operation_id='operation-organize-001',
        idempotency_key='organize-001', created_at=datetime.now(timezone.utc).isoformat())
    now[0] += 30
    assert lease.heartbeat(item['id'], 'alpha', 'run-one')
    validate_frozen_inputs(records, models, request)
    now[0] += 40
    assert lease.guard(item['id'], 'alpha', 'run-one')


def test_process_restart_reuses_completed_video_chunk(tmp_path):
    import json
    import subprocess
    import sys
    from pathlib import Path
    from backend.memory_app.workspace_generation import _video_source_chunks
    worker = Path(__file__).with_name('organize_crash_worker.py')
    first = subprocess.run([sys.executable,str(worker),str(tmp_path),'crash'],capture_output=True,timeout=30)
    assert first.returncode == 19, first.stderr.decode(errors='replace')
    assert len((tmp_path/'calls.jsonl').read_text(encoding='utf8').splitlines()) == 1
    resumed = subprocess.run([sys.executable,str(worker),str(tmp_path),'resume'],capture_output=True,timeout=30)
    assert resumed.returncode == 0, resumed.stderr.decode(errors='replace')
    count = len(_video_source_chunks('甲'*8000)) + 1
    assert len((tmp_path/'calls.jsonl').read_text(encoding='utf8').splitlines()) == count
    result = json.loads((tmp_path/'result.json').read_text(encoding='utf8'))
    assert result['metadata']['usage']['total_tokens'] == count * 4
    assert result['draft']['title'] == '整体标题'


def test_unknown_model_dispatch_is_not_reissued_after_process_death(tmp_path):
    import subprocess
    import sys
    from pathlib import Path
    worker = Path(__file__).with_name('organize_crash_worker.py')
    first = subprocess.run([sys.executable,str(worker),str(tmp_path),'during'],capture_output=True,timeout=30)
    assert first.returncode == 23
    resumed = subprocess.run([sys.executable,str(worker),str(tmp_path),'resume'],capture_output=True,timeout=30)
    assert resumed.returncode != 0
    assert b'organize_interrupted' in resumed.stderr
    assert len((tmp_path/'calls.jsonl').read_text(encoding='utf8').splitlines()) == 1


def test_process_restart_finishes_receipted_turn_without_rebilling(tmp_path):
    import subprocess
    import sys
    from pathlib import Path
    from core.storage_provider import SQLiteStructuredRecordStore
    from backend.memory_app.workspace_generation import _video_source_chunks
    worker = Path(__file__).with_name('organize_crash_worker.py')
    first = subprocess.run([sys.executable,str(worker),str(tmp_path),'terminal'],capture_output=True,timeout=30)
    assert first.returncode == 26, first.stderr.decode(errors='replace')
    records = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    turn_id = records.list('workspace_organize_steps')[0].payload['turn_id']
    store = SQLiteAITurnStore(tmp_path / '.rebuild-data/ai-turns.sqlite3')
    assert store.events_after(turn_id)[-1]['type'] == 'model.completed'
    resumed = subprocess.run([sys.executable,str(worker),str(tmp_path),'resume'],capture_output=True,timeout=30)
    assert resumed.returncode == 0, resumed.stderr.decode(errors='replace')
    assert store.events_after(turn_id)[-1]['type'] == 'turn.completed'
    assert len((tmp_path/'calls.jsonl').read_text(encoding='utf8').splitlines()) == len(_video_source_chunks('甲'*8000)) + 1


def test_saved_output_without_model_receipt_cannot_charge_again(tmp_path, monkeypatch):
    from backend.memory_app.v2.organize_turns import OrganizeTurns
    http, records = client(tmp_path)
    item = http.post('/api/workspace/v1/items/text',json={'project_id':'alpha','text':'原文证据'}).json()
    calls = []
    class Provider(Model):
        def complete(self, messages, **kw):
            calls.append(messages)
            return super().complete(messages, **kw)
    kwargs = dict(root=tmp_path,records=records,models=Provider(),item_id=item['id'],
                  project_id='alpha',source='原文证据',validate_current=lambda:None)
    original = SQLiteAITurnStore.append_model_terminal_bundle
    def fail_terminal(*args, **kwargs):
        raise OSError('synthetic receipt write interruption')
    monkeypatch.setattr(SQLiteAITurnStore,'append_model_terminal_bundle',fail_terminal)
    with pytest.raises(Exception):
        OrganizeTurns(**kwargs).complete([{'role':'user','content':'原文证据'}],max_tokens=10,validate_current=lambda:None)
    monkeypatch.setattr(SQLiteAITurnStore,'append_model_terminal_bundle',original)
    with pytest.raises(Exception, match='organize_result_unavailable|organize_interrupted'):
        OrganizeTurns(**kwargs).complete([{'role':'user','content':'原文证据'}],max_tokens=10,validate_current=lambda:None)
    assert len(calls) == 1
