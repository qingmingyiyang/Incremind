"""Exercise the routed workbench through its real durable execution service."""
import copy
import json
import re
import time

import pytest

from tests.memory_app.v2.test_workbench_ask import env, publish
from tests.memory_app.v2.test_workbench_do import env as do_env


def _context(client):
    from types import SimpleNamespace
    state = client.app.state
    return SimpleNamespace(records=state.recognition_records, documents=state.recognition_documents,
        service=state.recognition_service)


def test_text_intake_preserves_only_explicit_multipart_span(env):
    import asyncio
    from fastapi import HTTPException
    raw = '  预算十万元。\n '
    ordinary = asyncio.run(env.domains.intake.add_text({'project_id':'alpha', 'text':raw}))
    preserved = asyncio.run(env.domains.intake.add_text(
        {'project_id':'alpha', 'text':raw}, preserve_text=True))
    assert ordinary['source_text'] == raw.strip()
    assert preserved['source_text'] == raw
    assert preserved['title'] == ordinary['title']
    with pytest.raises(HTTPException):
        asyncio.run(env.domains.intake.add_text({'project_id':'alpha', 'text':' \n '}, preserve_text=True))


@pytest.mark.parametrize('text', ['https://example.test/article', '灵感先做预算'])
def test_fast_auto_rejects_sse_before_business_writes_or_models(do_env, text):
    client, model = do_env
    records = _context(client).records
    result = client.post('/api/v2/workbench/turns', json={'project_id':'project-a', 'text':text},
        headers={'Accept':'text/event-stream'})
    assert result.status_code == 406, result.text
    assert records.list('workspace_items') == ()
    assert records.list('v2_turns') == ()
    assert records.list('recognition_experiences') == ()
    assert model.calls == []


def test_ambiguous_auto_rejects_sse_after_one_route_without_business_writes(do_env):
    client, model = do_env
    text = '预算已经确定为十万元。'
    model.handler = _handler([{'intent':'remember', 'span':text, 'depends_on':[]}])
    result = client.post('/api/v2/workbench/turns', json={'project_id':'project-a', 'text':text},
        headers={'Accept':'text/event-stream'})
    assert result.status_code == 406, result.text
    records = _context(client).records
    assert records.list('workspace_items') == ()
    assert records.list('v2_turns') == ()
    assert len(model.calls) == 1
    assert '将输入原文分成' in str(model.calls[0])


def _handler(parts, *, answer=None, organize=None):
    def generate(messages, **kwargs):
        text = str(messages)
        schema = str(kwargs.get('response_format', {}))
        if '将输入原文分成' in text:
            return json.dumps({'parts':parts}, ensure_ascii=False)
        if 'condensed_question' in text or 'CondensedQuestion' in schema:
            return json.dumps({'condensed_question':'预算多少？'})
        if 'QueryVariants' in schema or '"queries"' in text:
            return json.dumps({'queries':[]})
        if 'citations' in text or 'AskOutput' in schema:
            return answer(messages) if answer else json.dumps({'answer':'预算十万元', 'citations':[]})
        if 'insights' in text or 'insights' in schema:
            return json.dumps({'insights':[]})
        if organize:
            organize()
        return json.dumps({'title':'预算', 'summary':'预算十万元', 'facts':[], 'topics':[],
            'todos':[], 'uncertainties':[], 'people':[], 'dates':[], 'suggestions':[]})
    return generate


def test_remember_dependency_uses_admitted_original_before_organizing_finishes(do_env):
    from threading import Event
    client, model = do_env
    entered, release = Event(), Event()
    parts = [{'intent':'remember', 'span':'预算十万元。', 'depends_on':[]},
             {'intent':'ask', 'span':'我们有多少资金？', 'depends_on':[0], 'situation':'资金核验'}]
    def organize():
        entered.set()
        assert release.wait(10)
    def answer(messages):
        assert '预算十万元。' in str(messages)
        assert entered.wait(5)
        assert not release.is_set()
        return json.dumps({'answer':'十万元', 'citations':[]})
    model.handler = _handler(parts, answer=answer, organize=organize)
    try:
        result = client.post('/api/v2/workbench/turns', json={
            'project_id':'project-a', 'text':'预算十万元。我们有多少资金？'}, headers={'Idempotency-Key':'admission'})
        assert result.status_code == 200, result.text
        payload = result.json()
        receipt = payload['turn']['receipt']
        assert receipt['parts'][1]['state'] == 'done', receipt
        assert receipt['parts'][1]['receipt']['ask']['answer'] == '十万元'
        assert receipt['parts'][1]['receipt']['ask']['citations'] == []
        assert receipt['parts'][0]['state'] == 'processing'
        assert 'situation' not in json.dumps(receipt)
        records = _context(client).records
        parent = records.read('v2_turns', payload['turn']['id'])
        child = records.read('v2_turns', parent.payload['part_turn_ids'][1])
        from core.ai_kernel import SQLiteAITurnStore
        from backend.memory_app.original_sources import source_store
        frozen = SQLiteAITurnStore(source_store(records).root / 'ai-turns.sqlite3').get_request(child.object_id)
        assert frozen['input']['situation'] == '资金核验'
        binding = records.read('v2_part_contexts', child.object_id)
        assert binding.revision == 1 and binding.payload['input_refs'] == frozen['input']['refs']
        from backend.recognition import RecognitionConflict
        changed = copy.deepcopy(frozen)
        changed['input']['refs'].pop()
        with pytest.raises(RecognitionConflict):
            client.app.state.workspace_domains.query.validate_answer_request(model, changed)
    finally:
        release.set()


def test_failed_answer_blocks_dependent_task_and_preserves_independent_part(do_env):
    client, model = do_env
    context = _context(client)
    publish(context, text='alpha预算', project='project-a')
    from tests.memory_app.v2.test_workbench_do_agents import Organization, install
    org = Organization()
    install(client, org)
    parts = [{'intent':'ask', 'span':'alpha预算是多少？', 'depends_on':[]},
             {'intent':'do', 'span':'帮我按答案写方案。', 'depends_on':[0]},
             {'intent':'inspiration', 'span':'灵感每周复盘。', 'depends_on':[]}]
    def fail(messages):
        raise RuntimeError('synthetic transport failure')
    model.handler = _handler(parts, answer=fail)
    body = {'project_id':'project-a', 'text':''.join(part['span'] for part in parts)}
    result = client.post('/api/v2/workbench/turns', json=body, headers={'Idempotency-Key':'isolated'})
    assert result.status_code == 200, result.text
    receipt = result.json()['turn']['receipt']
    assert [part['state'] for part in receipt['parts']] == ['failed', 'not_started', 'done']
    assert receipt['parts'][1]['error'] == 'dependency_failed' and org.calls == []
    before = len(model.calls)
    assert client.post('/api/v2/workbench/turns', json=body,
        headers={'Idempotency-Key':'isolated'}).json() == result.json()
    assert len(model.calls) == before


@pytest.mark.parametrize('change', ['body', 'scene', 'private'])
def test_original_dependency_is_revalidated_after_the_actual_answer_wire(do_env, change):
    client, model = do_env
    context = _context(client)
    parts = [{'intent':'remember', 'span':'预算十万元。', 'depends_on':[]},
             {'intent':'ask', 'span':'我们有多少资金？', 'depends_on':[0]}]
    def answer(messages):
        item = context.records.list('workspace_items')[0]
        if change == 'private':
            from backend.memory_app.v2.privacy import set_private_project
            set_private_project(context.records, 'project-a', True, 0)
            set_private_project(context.records, 'project-a', False, 1)
        elif change == 'scene':
            from backend.memory_app.v2.projects import assign_scene
            assign_scene(context.records, 'item', item.object_id, 'project-a', 'changed')
        else:
            with context.records.begin() as tx:
                current = tx.read('workspace_items', item.object_id)
                tx.put('workspace_items', item.object_id, {**current.payload, 'source_text':'预算二十万元。'},
                    expected_revision=current.revision)
                tx.commit()
        return json.dumps({'answer':'十万元', 'citations':[]})
    model.handler = _handler(parts, answer=answer)
    result = client.post('/api/v2/workbench/turns', json={
        'project_id':'project-a', 'text':'预算十万元。我们有多少资金？'})
    assert result.status_code == 200, result.text
    receipt = result.json()['turn']['receipt']
    assert receipt['parts'][1]['state'] == 'failed', receipt
    assert receipt['parts'][1]['receipt'] == {}


def test_disconnect_detaches_delivery_and_refresh_reads_all_terminal_parts(do_env):
    import asyncio
    from threading import Event
    client, model = do_env
    entered, release = Event(), Event()
    parts = [{'intent':'remember', 'span':'预算十万元。', 'depends_on':[]},
             {'intent':'ask', 'span':'我们有多少资金？', 'depends_on':[0]}]
    def answer(messages):
        entered.set()
        assert release.wait(10)
        return json.dumps({'answer':'十万元', 'citations':[]})
    original_handler = _handler(parts, answer=answer)
    def mark_stage(stage):
        # 只记录固定阶段时刻，不输出模型消息或配置。
        print('T13_2_DISCONNECT_STAGE', stage, f'{time.monotonic():.6f}', flush=True)
    def handler(messages, **kwargs):
        text, schema = str(messages), str(kwargs.get('response_format', {}))
        if '将输入原文分成' in text:
            mark_stage('route')
        elif 'QueryVariants' in schema or '"queries"' in text:
            mark_stage('queries')
        elif 'citations' in text or 'AskOutput' in schema:
            mark_stage('answer')
        return original_handler(messages, **kwargs)
    model.handler = handler
    body = {'project_id':'project-a', 'text':'预算十万元。我们有多少资金？'}
    service = client.app.state.workbench_turn_execution
    async def scenario():
        mark_stage('scenario')
        delivery = asyncio.create_task(service.run(body, 'disconnect'))
        assert await asyncio.to_thread(entered.wait, 8)
        delivery.cancel()
        with pytest.raises(asyncio.CancelledError):
            await delivery
        release.set()
        await asyncio.gather(*tuple(service.tasks.values()))
        await asyncio.gather(*tuple(client.app.state.workbench_tasks))
        before = len(model.calls)
        result = await service.run(body, 'disconnect')
        assert len(model.calls) == before
        return result
    result = client.portal.call(scenario)
    deadline = time.monotonic() + 10
    while True:
        history = client.get(f"/api/v2/workbench/threads/{result['thread_id']}?project_id=project-a").json()
        states = [part['state'] for part in history['turns'][0]['receipt']['parts']]
        if 'processing' not in states or time.monotonic() > deadline:
            break
        time.sleep(.05)
    assert len(history['turns']) == 1
    assert states == ['done', 'done'], history


def test_multi_sse_has_one_started_event_and_part_indexed_deltas(do_env):
    from tests.memory_app.v2.test_workbench_stream import events
    client, model = do_env
    parts = [{'intent':'remember', 'span':'预算十万元。', 'depends_on':[]},
             {'intent':'ask', 'span':'我们有多少资金？', 'depends_on':[0]}]
    model.handler = _handler(parts)
    original_wire = model._completion_fn
    def wire(**kwargs):
        response = original_wire(**kwargs)
        if not kwargs.get('stream'):
            return response
        def chunks():
            # 原 TurnModels 已返回流迭代器，保留其终态块和 usage。
            for chunk in response:
                choice = chunk['choices'][0]
                content = choice.get('delta', {}).get('content')
                if content:
                    for offset in range(0, len(content), 3):
                        yield {**chunk, 'choices':[{**choice,
                            'delta':{**choice['delta'], 'content':content[offset:offset+3]}}]}
                else:
                    yield chunk
        return chunks()
    model._completion_fn = wire
    response = client.post('/api/v2/workbench/turns', json={
        'project_id':'project-a', 'intent':'auto', 'text':'预算十万元。我们有多少资金？'},
        headers={'Accept':'text/event-stream', 'Idempotency-Key':'stream-parts'})
    assert response.status_code == 200, response.text
    delivered = events(response)
    assert [name for name, _ in delivered].count('started') == 1
    assert delivered[0][1]['parts'] == [{'index':index, 'intent':part['intent'], 'span':part['span'],
        'depends_on':part['depends_on'], 'state':'waiting'} for index, part in enumerate(parts)]
    assert delivered[0][1]['route']['mode'] == 'model'
    assert delivered[-1][0] == 'done'
    deltas = [data for name, data in delivered if name == 'delta']
    assert deltas and all(data['part'] == 1 for data in deltas)
    assert ''.join(data['text'] for data in deltas) == '预算十万元'


@pytest.mark.parametrize('revoke_at_expert, revive', [(False, False), (True, False), (False, True)])
def test_real_kernel_task_experts_receive_the_frozen_answer_dependency(do_env, revoke_at_expert, revive):
    client, model = do_env
    context = _context(client)
    # 恢复样本先满足原书架关键词命中条件，再验问答依赖的恢复与冻结。
    recognition, _ = publish(context, text='alpha预算是十万元' if revive else 'alpha预算十万元', project='project-a')
    if revive:
        from backend.memory_app.v2.recall_preferences import set_preference
        from backend.recognition import WorkScope
        set_preference(context.records, WorkScope('local-user', 'project-a'), recognition.id,
            recognition_revision=recognition.revision, preference_revision=0, state='forgotten')
        with context.records.begin() as tx:
            row = tx.read('recognition_recall_preferences', recognition.id)
            tx.put('recognition_recall_preferences', recognition.id,
                {**row.payload, 'by':'auto'}, expected_revision=row.revision)
            tx.commit()
    parts = [{'intent':'ask', 'span':'alpha预算是多少？', 'depends_on':[]},
             {'intent':'do', 'span':'帮我按答案写方案。', 'depends_on':[0], 'situation':'资金规划'}]
    expert_messages = []
    transport_errors = []
    def respond_inner(messages, **kwargs):
        if '将输入原文分成' in str(messages):
            return json.dumps({'parts':parts}, ensure_ascii=False)
        if 'citations' in str(messages):
            return json.dumps({'answer':'预算十万元', 'citations':[1]}, ensure_ascii=False)
        request = json.loads(messages[-1]['content'])
        if 'output' in request:
            return json.dumps({'mode':'cluster', 'assignments':[
                {'profile_id':'subagent.worker', 'task':'预算方案', 'goal':'规划资金',
                 'deliverable':'预算草稿', 'capabilities':['document.draft.propose'], 'depends_on':[]}]})
        if any(item['capability_id'] == 'agent.list' for item in request.get('capabilities', [])):
            return json.dumps({'type':'complete', 'summary':'资金规划已完成'})
        expert_messages.append(messages)
        assert '预算十万元' in str(messages)
        if revoke_at_expert:
            from backend.memory_app.source_egress import SourceEgressService
            from backend.recognition import WorkScope
            authority = SourceEgressService(context.records)
            for revision, purposes in [(0, []), (1, ['generation', 'embedding', 'rerank'])]:
                authority.set_policy(scope=WorkScope('local-user', 'project-a'), source_type='recognition',
                    source_id=recognition.id, allowed_purposes=purposes,
                    expected_source_revision=recognition.revision, expected_policy_revision=revision)
        if any(event.get('type') == 'tool.completed' for event in request.get('events', [])):
            return json.dumps({'type':'complete', 'summary':'已创建预算草稿'})
        return json.dumps({'type':'tool', 'capability_id':'document.draft.propose',
            'arguments':{'title':'预算方案', 'markdown':'预算十万元用于规划。'}}, ensure_ascii=False)
    def respond(messages, **kwargs):
        try:
            return respond_inner(messages, **kwargs)
        except Exception as error:
            transport_errors.append(f'{type(error).__name__}: {error}')
            raise
    model.handler = respond
    client.app.state.recognition_turn_dispatcher._runtime()
    response = client.post('/api/v2/workbench/turns', json={
        'project_id':'project-a', 'text':'alpha预算是多少？帮我按答案写方案。'})
    assert response.status_code == 200, response.text
    result = response.json()
    deadline = time.monotonic() + 60
    while True:
        history = client.get(f"/api/v2/workbench/threads/{result['thread_id']}?project_id=project-a").json()
        task = history['turns'][0]['receipt']['parts'][1]
        if task['state'] in {'done', 'partial', 'failed'} or time.monotonic() > deadline:
            break
        time.sleep(.1)
    assert task['state'] == ('failed' if revoke_at_expert else 'done'), {'task':task, 'transport_errors':transport_errors, 'calls':model.calls}
    assert expert_messages
    if revive:
        recall = context.records.read('recognition_recall_preferences', recognition.id)
        assert recall.payload['state'] == 'cooled' and recall.payload['by'] == 'auto'
    if revoke_at_expert:
        assert len(expert_messages) == 1
        assert not context.records.list('v2_task_draft_operations')
    request = context.records.read('v2_task_executions',
        context.records.read('v2_turns', result['turn']['id']).payload['part_turn_ids'][1]).payload['request']
    assert request['input']['situation'] == '资金规划'
    assert '预算十万元' in request['input']['text']


def test_three_parts_run_independently_then_pass_the_answer_to_task(do_env):
    from threading import Event
    from types import SimpleNamespace
    from tests.memory_app.v2.test_workbench_do_agents import Organization, install
    client, model = do_env
    state = client.app.state
    context = SimpleNamespace(records=state.recognition_records,
        documents=state.recognition_documents, service=state.recognition_service)
    publish(context, text='alpha 预算十万元', project='project-a')
    text = '记下alpha预算十万元。alpha预算是多少？帮我按答案写方案。'
    parts = [
        {'intent':'remember', 'span':'记下alpha预算十万元。', 'instruction':None, 'depends_on':[]},
        {'intent':'ask', 'span':'alpha预算是多少？', 'instruction':None, 'depends_on':[], 'situation':'预算核验'},
        {'intent':'do', 'span':'帮我按答案写方案。', 'instruction':None, 'depends_on':[1], 'situation':'方案初稿'},
    ]
    entered, release, answered = Event(), Event(), Event()
    def handler(messages, **kwargs):
        schema = json.dumps(kwargs.get('response_format', {}), default=str)
        if '将输入原文分成' in str(messages):
            return json.dumps({'parts':parts}, ensure_ascii=False)
        if 'citations' in schema or 'citations' in str(messages):
            assert entered.wait(5), 'remember must start concurrently with ask'
            answered.set()
            return json.dumps({'answer':'预算十万元', 'citations':[1]}, ensure_ascii=False)
        if 'insights' in schema:
            return json.dumps({'insights':[]})
        entered.set()
        assert release.wait(10), 'organizing should outlive dependent task admission'
        return json.dumps({'title':'预算', 'summary':'alpha预算十万元', 'facts':[],
            'topics':[], 'todos':[], 'uncertainties':[], 'people':[], 'dates':[], 'suggestions':[]})
    model.handler = handler
    class TaskPort(Organization):
        def start(self, request, **kwargs):
            assert answered.is_set(), 'task must wait for answer done'
            super().start(request, **kwargs)
    org = TaskPort()
    org.state = 'completed'
    install(client, org)
    try:
        result = client.post('/api/v2/workbench/turns', json={
            'project_id':'project-a', 'text':text, 'intent':'auto'},
            headers={'Idempotency-Key':'three-parts'})
        assert result.status_code == 200, result.text
        final = result.json()
        if final['turn']['intent'] != 'multi':
            from backend.memory_app.kernel.memory_turn import MemoryTurn
            store = MemoryTurn.store_for(context.records)
            for key in context.records.list('v2_route_turn_keys'):
                print('ROUTE_EVENTS', store.events_after(key.payload['request']['turn_id']))
            print('MODEL_CALLS', model.calls)
        assert final['turn']['intent'] == 'multi', {'final':final, 'calls':model.calls,
            'keys':[row.payload for row in context.records.list('v2_route_turn_keys')]}
        assert len(final['turn']['receipt']['parts']) == 3
        assert final['turn']['receipt']['parts'][1]['state'] == 'done', final['turn']['receipt']
        assert final['turn']['receipt']['parts'][2]['state'] in {'running', 'done'}, final['turn']['receipt']
        assert answered.is_set() and entered.is_set(), final
        replay = client.post('/api/v2/workbench/turns', json={
            'project_id':'project-a', 'text':text, 'intent':'auto'},
            headers={'Idempotency-Key':'three-parts'})
        assert replay.status_code == 200
        assert len([call for call in model.calls if '将输入原文' in str(call)]) == 1
        for index in range(3):
            assert len(context.records.list_matching('v2_turn_requests', idempotency_key=f'three-parts:{index}')) == 1
    finally:
        release.set()


def test_situation_is_optional_bounded_and_only_for_generation_parts():
    from backend.memory_app.v2.route import validate_route_output
    text = '预算多少？'
    part = {'intent': 'ask', 'span': text, 'instruction': None, 'depends_on': []}
    plain = validate_route_output(text, {'parts': [part]})
    planned = validate_route_output(text, {'parts': [{**part, 'situation': '财务规划 初稿'}]})
    assert planned.parts[0].situation == '财务规划 初稿'
    assert plain.parts[0].situation is None
    for change in ({'situation': 'x' * 61}, {'situation': 1},
                   {'intent': 'remember', 'situation': '财务'}):
        with pytest.raises(ValueError):
            validate_route_output(text, {'parts': [{**part, **change}]})


def test_auto_and_omitted_single_part_keep_the_manual_receipt_snapshot(env):
    from pathlib import Path
    publish(env)
    manual = env.http.post('/api/v2/workbench/turns', json={
        'project_id': 'alpha', 'intent': 'ask', 'text': 'alpha?'}).json()
    def stable(value):
        if isinstance(value, dict):
            return {key:stable(item) for key, item in value.items()}
        if isinstance(value, list):
            return [stable(item) for item in value]
        if isinstance(value, str):
            value = re.sub(r'[a-f0-9]{32}', '<generated-id>', value)
            return re.sub(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:\+00:00|Z)', '<generated-time>', value)
        return value
    fixture = json.loads((Path(__file__).parents[2] / 'fixtures/workbench_route/single_ask_receipt.json').read_text(encoding='utf-8'))
    assert fixture['baseline_commit'] == 'a4fe9184c7'
    # 原快照保持归档内容；当前单部分基线精确补入 T14.7 已生效的费用字段。
    snapshot = copy.deepcopy(fixture['response'])
    snapshot['turn']['receipt']['ask']['model_cost'] = None
    assert manual['turn']['receipt']['ask']['model_cost'] is None
    assert stable(manual) == snapshot
    store = env.domains.query.answer_turns.application.state.ai_turn_store
    manual_request = store.get_request(manual['turn']['id'])
    assert 'situation' not in manual_request['input']
    for intent in (None, 'auto'):
        body = {'project_id': 'alpha', 'text': 'alpha?'}
        if intent is not None:
            body['intent'] = intent
        key = 'single-snapshot-' + (intent or 'omitted')
        result = env.http.post('/api/v2/workbench/turns', json=body,
            headers={'Idempotency-Key':key})
        assert result.status_code == 200, result.text
        actual = result.json()
        # 自动单部分沿用旧公开形状，完整比较所有字段，不扩展旧快照。
        assert stable(actual) == snapshot
        assert stable(store.get_request(actual['turn']['id'])) == stable(manual_request)
        refreshed = env.http.get(f"/api/v2/workbench/threads/{actual['thread_id']}?project_id=alpha")
        assert refreshed.status_code == 200, refreshed.text
        assert refreshed.json()['turns'] == [actual['turn']]
        before = env.model.calls
        replay = env.http.post('/api/v2/workbench/turns', json=body,
            headers={'Idempotency-Key':key})
        assert replay.status_code == 200, replay.text
        assert replay.json() == actual and env.model.calls == before
        # 路由事实仍在原旁路记录中，公开兼容不会删除后续纠正需要的输入。
        stored = env.records.read('v2_turns', actual['turn']['id'])
        assert stored.payload['route'] == {'mode':'rules', 'usage':None, 'egress_receipt_id':None}
