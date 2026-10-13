"""真实工作台、组织内核、工具边界和 SQLite；仅模型运输为替身。"""
import json
import time

import pytest

from backend.memory_app.v2.agent_data_tools import AgentDataTools, CAPABILITIES, OPERATIONS
from tests.memory_app.v2.test_workbench_do import env


@pytest.mark.parametrize('authorized', [True, False])
@pytest.mark.parametrize('window', [16000, 200000])
def test_workbench_agent_reaches_lexicon_tools_and_finishes_without_extra_popup(env, authorized, window, monkeypatch):
    client, model = env
    records = client.app.state.recognition_service.records
    if window == 200000:
        model.update('generation', {'model': 'deepseek-flash', 'base_url': 'https://api.deepseek.com/v1',
            'expected_revision': model.public()['generation']['revision']})
    assert model._generation_gateway(model.snapshot('generation')).input_budget_limits(max_tokens=2000) == {
        'window': window, 'reserve': 2000}
    contexts = []
    transport_errors = []
    governed_errors = []
    original_complete = model.complete_governed
    def observe_complete(*args, **kwargs):
        try:
            return original_complete(*args, **kwargs)
        except Exception as error:
            governed_errors.append((type(error).__name__, str(error)))
            raise
    monkeypatch.setattr(model, 'complete_governed', observe_complete)
    original_validate = AgentDataTools.validate_context
    def observe_validate(self, request):
        try:
            return original_validate(self, request)
        except Exception as error:
            governed_errors.append((type(error).__name__, str(error)))
            raise
    monkeypatch.setattr(AgentDataTools, 'validate_context', observe_validate)
    from core.ai_kernel.model_planner import ModelGatewayAgentPlanner
    original_plan = ModelGatewayAgentPlanner.plan
    def observe_plan(self, *args, **kwargs):
        try:
            return original_plan(self, *args, **kwargs)
        except Exception as error:
            governed_errors.append((type(error).__name__, str(error)))
            raise
    monkeypatch.setattr(ModelGatewayAgentPlanner, 'plan', observe_plan)

    def respond(messages, **kwargs):
        context = json.loads(messages[-1]['content'])
        if 'original_text' in context:
            return json.dumps({'parts': [{'intent': 'do', 'span': context['original_text'],
                'instruction': None, 'depends_on': []}]})
        if 'output' in context:
            return json.dumps({'mode': 'main_only'})
        contexts.append(context)
        completed = [event for event in context.get('events', []) if event['type'] == 'tool.completed']
        reads = [event for event in completed if event['data']['capability_id'] == CAPABILITIES[0]]
        previews = [event for event in completed if event['data']['capability_id'] == CAPABILITIES[1]]
        imports = [event for event in completed if event['data']['capability_id'] == CAPABILITIES[2]]
        def tool(identity, args):
            return json.dumps({'type': 'tool', 'capability_id': identity, 'arguments': args})
        if not reads:
            return tool(CAPABILITIES[0], {'view': 'skill'})
        if len(reads) == 1:
            return tool(CAPABILITIES[0], {'view': 'terms'})
        if not previews:
            return tool(CAPABILITIES[1], {'text': 'Incremind\n程虹', 'format': 'txt',
                'expected_revision': reads[-1]['resolved_payload']['revision']})
        if not imports:
            return tool(CAPABILITIES[2], {'preview_id': previews[-1]['resolved_payload']['preview_id']})
        if len(reads) == 2:
            return tool(CAPABILITIES[0], {'view': 'terms'})
        return json.dumps({'type': 'complete', 'summary': '已保存热词并回读验证'})

    def transport(messages, **kwargs):
        try:
            return respond(messages, **kwargs)
        except Exception as error:
            transport_errors.append((type(error).__name__, str(error)))
            raise
    model.handler = transport
    client.app.state.recognition_turn_dispatcher._runtime()
    text = '更新热词表：\nIncremind\n程虹' if authorized else '帮我分析 Incremind 和程虹是否适合放入热词表，先给建议'
    response = client.post('/api/v2/workbench/turns', json={'project_id': 'project-a', 'text': text})
    assert response.status_code == 200, response.text
    data = response.json()
    deadline = time.monotonic() + 60
    while True:
        receipt = client.get(f"/api/v2/workbench/threads/{data['thread_id']}?project_id=project-a").json()['turns'][0]['receipt']['do']
        if receipt['state'] in {'done', 'partial', 'failed'} or time.monotonic() >= deadline:
            break
        time.sleep(.1)
    assert receipt['state'] == 'done', (receipt, transport_errors)
    assert not governed_errors, governed_errors
    assert not transport_errors, transport_errors
    assert contexts
    store = client.app.state.ai_turn_store
    identity = receipt['kernel_turn_id']
    events = tuple(store.events_after(identity))
    assert any(event['type'] == 'tool.completed' and event['data']['capability_id'] == CAPABILITIES[1] for event in events)
    terms = client.app.state.asr_lexicon.read()['terms']
    if authorized:
        assert {row['term'] for row in terms} == {'Incremind', '程虹'}
        assert len(records.list(OPERATIONS)) == 1
        assert sum(event['type'] == 'approval.resolved' for event in events) == 1
        assert sum(event['type'] == 'tool.completed' and event['data']['capability_id'] == CAPABILITIES[0] for event in events) == 3
        assert any(event['type'] == 'model.routed' for event in events)
        assert all(not any(event['type'] == 'model.routed' for event in context['events'])
            for context in contexts if any(event['type'] == 'tool.completed' for event in context['events']))
        documents = client.app.state.task_drafts.documents
        previous_document = documents.read(receipt['document_id'])
        revision = client.app.state.asr_lexicon.read()['revision']
        updated = client.post('/api/v2/asr/lexicon/import', json={'text': '程虹\t5', 'format': 'txt',
            'expected_revision': revision, 'preview': False})
        assert updated.status_code == 200, updated.text
        first_context_count = len(contexts)
        second = client.post('/api/v2/workbench/turns', json={'project_id': 'project-a',
            'thread_id': data['thread_id'], 'text': '帮我分析 Incremind 和程虹是否适合放入热词表，先给建议'})
        assert second.status_code == 200, second.text
        second_user_id = second.json()['turn']['id']
        deadline = time.monotonic() + 60
        while True:
            history = client.get(f"/api/v2/workbench/threads/{data['thread_id']}?project_id=project-a").json()
            second_receipt = next(row for row in history['turns'] if row['id'] == second_user_id)['receipt']['do']
            if second_receipt['state'] in {'done', 'partial', 'failed'} or time.monotonic() >= deadline:
                break
            time.sleep(.1)
        assert second_receipt['state'] == 'done', (second_receipt, governed_errors, transport_errors)
        assert second_receipt['fallback_new'] is True and second_receipt['continues'] is None
        assert second_receipt['document_id'] != previous_document['id']
        second_identity = second_receipt['kernel_turn_id']
        frozen = store.get_request(second_identity)
        assert json.loads(frozen['input']['text'])['outcome_input']['document_id'] == previous_document['id']
        from backend.memory_app.v2.outcomes import COMPOSITION
        composition = store.get_immutable_payload(second_identity, COMPOSITION)[1]
        assert composition['fallback_new'] is True and composition['changes'] == []
        assert composition['input'] == frozen['input']
        assert '热词表尚未保存' in composition['markdown']
        second_events = tuple(store.events_after(second_identity))
        assert any(event.get('correlation', {}).get('model_request_id') == composition['model_request_id']
            for event in second_events)
        assert not any(event['type'] == 'approval.required' for event in second_events)
        assert not any(event['type'] == 'tool.completed' and event['data']['capability_id'] == CAPABILITIES[2]
            for event in second_events)
        preview_events = [event for event in second_events if event['type'] == 'tool.completed'
            and event['data']['capability_id'] == CAPABILITIES[1]]
        assert len(preview_events) == 1
        assert store.get(preview_events[0]['data']['payload_ref'])['authorized'] is False
        assert len(contexts) - first_context_count == 4
        assert documents.read(previous_document['id']) == previous_document
        assert next(row['weight'] for row in client.app.state.asr_lexicon.read()['terms'] if row['term'] == '程虹') == 5
        assert len(records.list(OPERATIONS)) == 1
        assert not governed_errors, governed_errors
        assert not transport_errors, transport_errors
    else:
        assert terms == []
        assert not records.list(OPERATIONS)
        assert not any(event['type'] == 'approval.required' for event in events)


@pytest.mark.parametrize('failure', ['false_claim', 'capacity'])
def test_explicit_update_cannot_publish_success_without_actual_save(env, failure, monkeypatch):
    client, model = env
    errors = []
    budget_errors = []
    from backend.shared.llm.litellm_gateway import LiteLLMCompletionGateway
    enforce = LiteLLMCompletionGateway._enforce_input_token_budget
    def observe_budget(self, *args, **kwargs):
        try:
            return enforce(self, *args, **kwargs)
        except RuntimeError as error:
            budget_errors.append(str(error))
            raise
    monkeypatch.setattr(LiteLLMCompletionGateway, '_enforce_input_token_budget', observe_budget)
    original = model.complete_governed
    def observe(*args, **kwargs):
        try:
            return original(*args, **kwargs)
        except Exception as error:
            errors.append((type(error).__name__, str(error)))
            raise
    monkeypatch.setattr(model, 'complete_governed', observe)
    def respond(messages, **kwargs):
        context = json.loads(messages[-1]['content'])
        if 'output' in context:
            return json.dumps({'mode': 'main_only'})
        return json.dumps({'type': 'complete', 'summary': '已保存所有热词'})
    model.handler = respond
    body = 'Incremind\n程虹' if failure == 'false_claim' else '\n'.join(f'专业领域复杂术语{i:04d}' for i in range(2500))
    response = client.post('/api/v2/workbench/turns', json={'project_id': 'project-a', 'text': '更新热词表：\n' + body})
    assert response.status_code == 200, response.text
    data = response.json()
    deadline = time.monotonic() + 60
    while True:
        receipt = client.get(f"/api/v2/workbench/threads/{data['thread_id']}?project_id=project-a").json()['turns'][0]['receipt']['do']
        if receipt['state'] in {'done', 'partial', 'failed'} or time.monotonic() >= deadline:
            break
        time.sleep(.1)
    assert receipt['state'] == 'failed', receipt
    assert receipt['error'] == 'asr_lexicon_update_not_completed'
    assert receipt['document_id'] is None
    assert client.app.state.asr_lexicon.read()['terms'] == []
    assert not client.app.state.recognition_service.records.list(OPERATIONS)
    if failure == 'capacity':
        assert any('硬预算' in message for message in budget_errors), (errors, budget_errors)
    else:
        assert not errors, errors
        assert not budget_errors, budget_errors


@pytest.mark.parametrize('boundary', ['missing_skill', 'custom_permissions'])
def test_unavailable_lexicon_tools_preserve_ordinary_workbench_task(request, monkeypatch, tmp_path, boundary):
    original = AgentDataTools.__init__
    def missing_source(self, *args, **kwargs):
        # 只将包来源换成真实空目录，目录扫描、构造和应用降级都运行实际实现。
        kwargs['skill_root'] = tmp_path / 'missing-application-skills'
        return original(self, *args, **kwargs)
    if boundary == 'missing_skill':
        monkeypatch.setattr(AgentDataTools, '__init__', missing_source)
    client, model = request.getfixturevalue('env')
    if boundary == 'custom_permissions':
        from dataclasses import replace
        profiles = client.app.state.agent_profile_registry
        main = profiles.get('main.orchestrator')
        profiles.update(replace(main, revision=main.revision + 1, instructions='仅完成已允许的普通任务',
            capability_ids=tuple(identity for identity in main.capability_ids if identity not in CAPABILITIES)),
            expected_revision=main.revision)
    def respond(messages, **kwargs):
        context = json.loads(messages[-1]['content'])
        if 'output' in context:
            return json.dumps({'mode': 'main_only'})
        assert not any(item['capability_id'] in CAPABILITIES for item in context['capabilities'])
        return json.dumps({'type': 'complete', 'summary': '普通任务完成'})
    model.handler = respond
    assert (client.app.state.ai_runtime.agent_data_tools is None) == (boundary == 'missing_skill')
    response = client.post('/api/v2/workbench/turns', json={'project_id': 'project-a', 'intent': 'do', 'text': '写一段普通总结'})
    assert response.status_code == 200, response.text
    data = response.json()
    deadline = time.monotonic() + 60
    while True:
        receipt = client.get(f"/api/v2/workbench/threads/{data['thread_id']}?project_id=project-a").json()['turns'][0]['receipt']['do']
        if receipt['state'] in {'done', 'partial', 'failed'} or time.monotonic() >= deadline:
            break
        time.sleep(.1)
    assert receipt['state'] == 'done', receipt
