import pytest

from backend.memory_app.v2.task_do import TaskDo
from backend.memory_app.v2.task_drafts import TaskDrafts
from backend.memory_app.v2.workbench import persist_workbench_turn
from core.document_engine import SQLiteDocumentRepository
from core.storage_provider import SQLiteStructuredRecordStore
from tests.memory_app.v2.test_turn_requests import Models
from tests.backend.unit.api.test_agent_organization_e2e import _organization
from tests.memory_app.v2.test_workbench_do import env


@pytest.mark.asyncio
async def test_do_starts_one_real_organization_and_never_an_old_task(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path/'records.sqlite3')
    composition, runner, organization = _organization(tmp_path)
    service = TaskDo(records, Models(), TaskDrafts(records, SQLiteDocumentRepository(records)),
                     organization, lambda identity, project: {'status':'running','summary':''},
                     lambda identity, project: [])
    receipt, state = service.initial('turn-v2-task', 'project-alpha', '写一份方案', None)
    with records.begin() as tx:
        # 沿真实工作台写入生成同项目线程与完整 Turn 事实。
        persist_workbench_turn(
            tx, turn_id='turn-v2-task', project='project-alpha', thread_id='thread-v2-task',
            cleaned='写一份方案', now='2026-10-08T00:00:00+00:00', created_at='2026-10-08T00:00:00+00:00',
            intent='do', receipt={'do': receipt}, item_id=None, item=None,
            instance=None, run_id=None, title_prefix='', replace_turn=None, research_state=state,
        )
        tx.commit()
    await service.advance('turn-v2-task')
    mains = [run for run in composition.store.list_runs(project_id='project-alpha') if run.role == 'main']
    assert len(mains) == 1
    request = composition.request_loader(mains[0].turn_id)
    assert request['desired_outcome'] == 'project.task'
    assert request['privacy']['material_refs'] == []
    assert not records.list('tasks')
    assert not records.list('v2_do_research')
    await service.advance('turn-v2-task')
    assert len([run for run in composition.store.list_runs(project_id='project-alpha') if run.role == 'main']) == 1
    service.reader = lambda identity, project: {'status':'completed','summary':'已完成的方案'}
    await service.advance('turn-v2-task')
    receipt = records.read('v2_turns','turn-v2-task').payload['receipt']['do']
    assert receipt['state'] == 'done'
    assert receipt['verified'] is False
    assert len(service.drafts.documents.list()) == 1
    assert service.divisions.read('turn-v2-task','project-alpha')['outcome'] == 'done'
    await service.advance('turn-v2-task')
    assert len(service.drafts.documents.list()) == 1


def test_http_do_uses_kernel_without_creating_legacy_tasks(env):
    client, model = env
    response = client.post('/api/v2/workbench/turns', json={
        'project_id':'project-a', 'intent':'do', 'text':'写一段总结'})
    assert response.status_code == 200, response.text
    data = response.json()
    receipt = data['turn']['receipt']['do']
    assert receipt['kernel_turn_id'].startswith('turn-')
    assert receipt['state'] == 'running'
    assert not client.app.state.recognition_service.records.list('recognition_tasks')
    read = client.get(f"/api/v2/workbench/threads/{data['thread_id']}?project_id=project-a")
    assert read.status_code == 200, read.text


def test_division_http_edit_conflict_and_rerun_preserve_old_turn(env):
    from backend.memory_app.v2.task_divisions import TaskDivisions
    client, model = env
    created = client.post('/api/v2/workbench/turns',json={
        'project_id':'project-a','intent':'do','text':'写一段总结'}).json()
    identity = created['turn']['id']
    samples = TaskDivisions(client.app.state.recognition_service.records)
    items = [{'goal':'总结','deliverable':'整理稿','capabilities':['memory.recall'],'depends_on':[]}]
    samples.complete(identity,project='project-a',text='写一段总结',items=items,outcome='done')
    path = f'/api/v2/workbench/turns/{identity}/division'
    assert client.get(path+'?project_id=other').status_code == 404
    edited = client.patch(path,json={'project_id':'project-a','items':[{**items[0],'goal':'调整目标'}],'expected_revision':1})
    assert edited.status_code == 200, edited.text
    assert edited.json()['adjusted'] is True
    stale = client.patch(path,json={'project_id':'project-a','items':items,'expected_revision':1})
    assert stale.status_code == 409, stale.text
    redone = client.post(f'/api/v2/workbench/turns/{identity}/redo',json={'project_id':'project-a','expected_revision':2})
    assert redone.status_code == 200, redone.text
    assert redone.json()['turn']['id'] != identity
    assert redone.json()['thread_id'] == created['thread_id']
    assert samples.read(identity,'project-a')['items'][0]['goal'] == '调整目标'


def test_real_kernel_three_drafts_finish_without_approval(env):
    _real_kernel_drafts(env)


def test_main_wait_allows_two_drafts_within_three_seconds(env):
    _real_kernel_drafts(env, check_wait_deadline=True)


def test_real_kernel_failed_item_keeps_other_results_and_names_missing_goal(env):
    _real_kernel_drafts(env, fail_second=True)


def test_real_kernel_item_budget_stops_only_that_item(env):
    _real_kernel_drafts(env, fail_second='budget')


def _real_kernel_drafts(env, *, check_wait_deadline=False, fail_second=False):
    import json
    import time
    client, model = env
    def respond(messages, **kwargs):
        context = json.loads(messages[-1]['content'])
        if 'output' in context:
            return json.dumps({'mode':'cluster','assignments':[
                {'profile_id':'subagent.worker','task':f'第{i}份草稿','goal':f'目标{i}',
                 'deliverable':f'草稿{i}','capabilities':['document.draft.propose'],
                 'depends_on':[1,2] if i == 3 else []} for i in range(1,4)]})
        if any(item['capability_id'] == 'agent.list' for item in context.get('capabilities',[])):
            return json.dumps({'type':'complete','summary':'三部分合成成果'})
        second = '第2份草稿' in str(context.get('input', {}))
        if fail_second is True and second:
            raise RuntimeError('synthetic worker failure')
        if any(event.get('type') == 'tool.completed' for event in context.get('events',[])) and not (second and fail_second == 'budget'):
            return json.dumps({'type':'complete','summary':'已创建部分草稿'})
        return json.dumps({'type':'tool','capability_id':'document.draft.propose',
                           'arguments':{'title':'部分成果','markdown':'草稿正文'}})
    model.handler = respond
    client.app.state.recognition_turn_dispatcher._runtime()
    response = client.post('/api/v2/workbench/turns',json={
        'project_id':'project-a','intent':'do','text':'分别准备三部分方案并汇总'})
    assert response.status_code == 200, response.text
    data = response.json()
    deadline = time.monotonic()+90
    waiting_since = None
    while True:
        read = client.get(f"/api/v2/workbench/threads/{data['thread_id']}?project_id=project-a")
        assert read.status_code == 200, read.text
        receipt = read.json()['turns'][0]['receipt']['do']
        if check_wait_deadline:
            runs = client.app.state.agent_runtime_composition.store.list_runs(project_id='project-a')
            store = client.app.state.ai_turn_store
            main = next((run for run in runs if run.role == 'main'), None)
            events = store.events_after(main.turn_id) if main else ()
            child_intents = [event for run in runs if run.profile_id == 'subagent.worker'
                for event in store.events_after(run.turn_id)
                if event['type'] == 'tool.intent.recorded' and
                event.get('data', {}).get('capability_id') == 'document.draft.propose']
            completed_calls = {event['correlation']['tool_call_id'] for event in events
                               if event['type'] == 'tool.completed'}
            if waiting_since is None and len(child_intents) >= 2 and any(event['type'] == 'tool.started' and
                    event.get('data', {}).get('capability_id') == 'agent.wait' and
                    event['correlation']['tool_call_id'] not in completed_calls for event in events):
                waiting_since = time.monotonic()
            drafts = client.app.state.recognition_service.records.list('v2_task_draft_operations')
            if waiting_since is not None and time.monotonic() - waiting_since >= 3:
                assert len(drafts) >= 2, 'two child drafts must finish within 3 seconds of main agent.wait'
                return
        if receipt['state'] in {'done','failed','partial'} or time.monotonic() > deadline:
            break
        time.sleep(.1)
    composition = client.app.state.agent_runtime_composition
    runs = composition.store.list_runs(project_id='project-a')
    diagnostics = {'receipt':receipt,'calls':len(model.calls),
        'runs':[(run.profile_id,run.status) for run in runs],
        'events':{run.turn_id:[(event['type'],event.get('data',{}))
            for event in client.app.state.ai_turn_store.events_after(run.turn_id)[-4:]] for run in runs}}
    expected_state = 'partial' if fail_second else 'done'
    if receipt['state'] != expected_state:
        print(json.dumps(diagnostics,ensure_ascii=False))
    assert receipt['state'] == expected_state, diagnostics
    if check_wait_deadline:
        assert waiting_since is not None, 'the real main must execute agent.wait'
    assert len(receipt['division']) == 3
    assert [item['state'] for item in receipt['division']] == ['done', 'failed' if fail_second else 'done', 'done']
    assert receipt['document_id']
    if fail_second is True:
        assert receipt['model_usage'] is None
    else:
        assert receipt['model_usage']['input_tokens'] > 0
    from backend.memory_app.v2.settings import _receipts
    records = client.app.state.recognition_service.records
    egress = [row for row in _receipts(records, 50, runtime_root=model.root) if row['purpose'] == '干活']
    assert len(egress) == 1
    expected_usage = ({'input': receipt['model_usage']['input_tokens'],
                       'output': receipt['model_usage']['output_tokens']} if receipt['model_usage'] else None)
    assert egress[0]['usage'] == expected_usage
    assert receipt['context']['egress'] == receipt['egress']
    if fail_second:
        output = client.get(f"/api/recognition/documents/{receipt['document_id']}?project_id=project-a")
        assert output.status_code == 200
        assert '未完成：目标2' in output.json()['markdown']
    if fail_second == 'budget':
        worker = next(run for run in runs if run.turn_id == receipt['division'][1]['turn_id'])
        events = client.app.state.ai_turn_store.events_after(worker.turn_id)
        sent = sum(event['type'] == 'model.attempt.dispatched' for event in events)
        # Input/output reservations may exhaust before the call-count ceiling.
        assert 0 < sent <= worker.budget_limit.model_calls
        assert sum(event['type'] == 'model.requested' for event in events) == sent + 1
    assert not client.app.state.recognition_service.records.list('recognition_tasks')
