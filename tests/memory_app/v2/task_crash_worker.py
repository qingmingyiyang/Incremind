"""Subprocess fixture: a real organization with an interrupted model transport."""
import tests._path_setup  # noqa: F401
import json
import os
from pathlib import Path
import sys
import time
from threading import Event

root, phase = Path(sys.argv[1]), sys.argv[2]
os.environ['CHRIPTMAS_APP_ROOT'] = str(root)
root.mkdir(exist_ok=True)
(root/'config').mkdir(exist_ok=True)
(root/'config/settings.toml').write_bytes((Path(__file__).parents[3]/'config/settings.toml.example').read_bytes())
from tests.memory_app.test_api import TurnModels, _shutdown
from backend.memory_app.storage_authority import resolve_recognition_document_store
from backend.memory_app.app import create_app
from fastapi import FastAPI
from fastapi.testclient import TestClient
from backend.api.ai_turn_recovery_startup import scan_due_ai_turn_recovery

records, _ = resolve_recognition_document_store(root)
models = TurnModels(records, root)
config = records.read('recognition_model_config', 'generation')
if config is None:
    models.update('generation', {'base_url':'https://example.test', 'model':'test-model',
        'api_key':'synthetic-only', 'allow_remote':True, 'expected_revision':0})
else:
    models.secrets.set(config.payload['secret_ref'], 'synthetic-only')
blocked = Event()


def respond(messages, **kwargs):
    context = json.loads(messages[-1]['content'])
    if 'output' in context:
        return json.dumps({'mode':'cluster', 'assignments':[
            {'profile_id':'subagent.worker','task':f'part-{i}', 'goal':f'goal-{i}',
             'deliverable':'draft','capabilities':['document.draft.propose'], 'depends_on':[]}
            for i in (1,2)]})
    if any(item['capability_id']=='agent.list' for item in context.get('capabilities', [])):
        return json.dumps({'type':'complete','summary':'available parts'})
    part = 2 if 'part-2' in str(context['input']) else 1
    with (root/'calls.jsonl').open('a') as output:
        output.write(json.dumps({'phase':phase,'part':part})+'\n')
    if phase == 'start' and part == 2:
        blocked.set()
        Event().wait(180)
        raise RuntimeError('parent failed to terminate worker')
    if any(event['type']=='tool.completed' for event in context.get('events', [])):
        return json.dumps({'type':'complete','summary':'part complete'})
    return json.dumps({'type':'tool','capability_id':'document.draft.propose',
        'arguments':{'title':f'part-{part}','markdown':'fixture body'}})


models.handler = respond
client = TestClient(create_app(runtime_root=root, legacy_app=FastAPI(), model_configuration=models))
with client:
    client.app.state.recognition_turn_dispatcher._runtime()
    if phase == 'start':
        response = client.post('/api/v2/workbench/turns',json={
            'project_id':'project-a','intent':'do','text':'prepare independent drafts'})
        assert response.status_code == 200, response.text
        data = response.json()
        (root/'accepted.json').write_text(json.dumps(data))
    else:
        data = json.loads((root/'accepted.json').read_text())
        client.app.state.agent_organization_runtime.recover()
    deadline = time.monotonic()+100
    while time.monotonic()<deadline:
        response = client.get(f"/api/v2/workbench/threads/{data['thread_id']}?project_id=project-a")
        assert response.status_code == 200, response.text
        receipt = response.json()['turns'][0]['receipt']['do']
        rows = receipt.get('division', [])
        if phase == 'start' and blocked.is_set() and any(row['state']=='done' for row in rows):
            (root/'ready.json').write_text(json.dumps(receipt))
            Event().wait(180)
            raise RuntimeError('parent failed to terminate process')
        if phase == 'recover':
            store = client.app.state.ai_turn_store
            scan_due_ai_turn_recovery(store)
            unresolved = next((row for row in rows if row['goal']=='goal-2'), None)
            lease = store.get_run_lease(unresolved['turn_id']) if unresolved else None
            if lease is not None and lease.status == 'quarantined':
                client.app.state.agent_organization_runtime.recover()
                (root/'recovered.json').write_text(json.dumps({'receipt':receipt, 'lease_status':lease.status}))
                break
        time.sleep(.1)
    else:
        raise AssertionError(receipt)
_shutdown(client)
