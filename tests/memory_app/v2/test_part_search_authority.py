"""沿原应用工厂验证搜索首答依赖，仅隔离外部模型传输。"""
import json
import re
import sqlite3
import time
import traceback
from types import SimpleNamespace

import pytest

from backend.memory_app.kernel.memory_turn import _OUTPUT
from backend.memory_app.model_config import ModelConfiguration, ModelConfigurationError
from backend.memory_app.v2.memory_turn import MemoryTurn
from backend.memory_app.v2.part_context import validate_answer_authority, _validate_answer_facts
from backend.memory_app.source_egress import SourceEgressService
from backend.recognition import RecognitionConflict, RecognitionError, WorkScope
from tests.memory_app.v2.test_search_turn import SearchWire
from tests.memory_app.v2.test_workbench_ask import publish
from tests.memory_app.v2.test_workbench_do import env as do_env


def search_dependency(do_env):
    client, model = do_env
    state = client.app.state
    assert isinstance(model, ModelConfiguration)
    query = state.workspace_domains.query
    context = SimpleNamespace(records=state.recognition_records,
        documents=state.recognition_documents, service=state.recognition_service)
    question = '最近 alpha 展览开放吗？'
    insight, _ = publish(context, text='alpha 展览历史档案', project='project-a')
    initial_plan = query.prepare_ask('project-a', question)
    # 用原 Source 与原 query 初读证明样本可召回，同时其旧档案不能满足时效问题。
    assert any(candidate['id'] == insight.id for candidate in initial_plan['chosen'])
    from backend.memory_app.v2.policies import get
    from core.search_and_recall.evidence_windows import query_terms
    search_policy = get('search')
    enough = search_policy.sufficient_input(initial_plan['chosen'], [question], query_terms, detail=False)
    assert search_policy(question, enough, candidates=initial_plan['chosen']) is True
    model.update('search', {'base_url':'https://example.test/v1', 'model':'synthetic-search',
        'api_key':'synthetic-search-private', 'enabled':True, 'allow_remote':True, 'expected_revision':0})
    search_wire, generation_wire = SearchWire(), model._completion_fn

    def wire(**request):
        return search_wire(**request) if 'web_search_options' in request else generation_wire(**request)

    model._completion_fn = wire
    parts = [{'intent':'ask', 'span':question, 'depends_on':[]},
             {'intent':'do', 'span':'帮我按答案写参观方案。', 'depends_on':[0], 'situation':'参观方案'}]
    answer_messages, expert_messages, transport_errors = [], [], []

    def respond_inner(messages, **kwargs):
        text, schema = str(messages), str(kwargs.get('response_format', {}))
        if '将输入原文分成' in text:
            return json.dumps({'parts':parts}, ensure_ascii=False)
        if 'QueryVariants' in schema or '"queries"' in text:
            return json.dumps({'queries':[]})
        if 'citations' in text:
            answer_messages.append(messages)
            available = [int(number) for number in re.findall(r'^\[(\d+)\]', messages[-1]['content'], re.MULTILINE)]
            # 普通材料在前，搜索在后；首答引用实际可用的第一个搜索编号。
            citations = [available[1]] if len(available) > 1 else []
            return json.dumps({'answer':'当前展览开放，费用120元。', 'citations':citations}, ensure_ascii=False)
        request = json.loads(messages[-1]['content'])
        if 'output' in request:
            return json.dumps({'mode':'cluster', 'assignments':[
                {'profile_id':'subagent.worker', 'task':'参观方案', 'goal':'根据开放信息规划参观',
                 'deliverable':'参观草稿', 'capabilities':['document.draft.propose'], 'depends_on':[]}]}, ensure_ascii=False)
        if any(capability['capability_id'] == 'agent.list' for capability in request.get('capabilities', [])):
            return json.dumps({'type':'complete', 'summary':'参观规划已完成'}, ensure_ascii=False)
        expert_messages.append(messages)
        assert '本次输入的依赖上下文' in text and '当前展览开放，费用120元。' in text
        if any(event.get('type') == 'tool.completed' for event in request.get('events', [])):
            return json.dumps({'type':'complete', 'summary':'已创建参观草稿'}, ensure_ascii=False)
        return json.dumps({'type':'tool', 'capability_id':'document.draft.propose',
            'arguments':{'title':'参观方案', 'markdown':'当前展览开放，费用120元。按此信息规划参观。',
                         'final_for':'参观草稿'}}, ensure_ascii=False)

    def respond(messages, **kwargs):
        try:
            return respond_inner(messages, **kwargs)
        except Exception as error:
            transport_errors.append(f'{type(error).__name__}: {error}')
            raise

    model.handler = respond
    execution_errors = []
    from backend.memory_app.v2 import multipart
    original_failure = multipart.safe_failure

    def observe_failure(error):
        # 观察安全错误映射前的原异常，执行和公开错误码仍沿用原函数。
        execution_errors.append(''.join(traceback.format_exception(error)))
        return original_failure(error)

    with pytest.MonkeyPatch.context() as observer:
        observer.setattr(multipart, 'safe_failure', observe_failure)
        response = client.post('/api/v2/workbench/turns', json={
            'project_id':'project-a', 'text':''.join(part['span'] for part in parts)},
            headers={'Idempotency-Key':'search-answer-dependency'})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload['turn']['intent'] == 'multi'
    receipt = payload['turn']['receipt']['parts']
    assert receipt[0]['state'] == 'done', '\n'.join(execution_errors) or receipt
    parent = context.records.read('v2_turns', payload['turn']['id'])
    first_id = parent.payload['part_turn_ids'][0]
    first = context.records.read('v2_turns', first_id)
    assert first.payload['receipt']['ask']['search']['used'] == 1
    assert len(search_wire.calls) == 1
    # 先走原领域复验，让 RED 报出真实缺失的证明，避免只看到父轮安全错误码。
    facts = validate_answer_authority(query, 'project-a', first_id, parent.object_id)
    deadline = time.monotonic() + 60
    while True:
        history = client.get(f"/api/v2/workbench/threads/{payload['thread_id']}?project_id=project-a").json()
        task = history['turns'][0]['receipt']['parts'][1]
        if task['state'] in {'done', 'partial', 'failed', 'not_started'} or time.monotonic() > deadline:
            break
        time.sleep(.1)
    assert task['state'] == 'done', {'task':task, 'transport_errors':transport_errors}
    assert len(answer_messages) == 1 and expert_messages
    assert len(context.records.list('v2_task_draft_operations')) == 1
    assert any(candidate['kind'] == 'search' for candidate in facts['chosen'])
    assert any(candidate['entry']['id'] == insight.id for candidate in facts['chosen'])
    return SimpleNamespace(client=client, model=model, query=query, records=context.records,
        insight=insight, parent=parent.object_id, first_id=first_id, facts=facts, search_wire=search_wire,
        search_id=first.payload['receipt']['ask']['search']['turn_id'])


def test_search_answer_dependency_retains_original_guard_without_new_search(do_env):
    saved = search_dependency(do_env)
    search_store = MemoryTurn.store_for(saved.records)
    before_events = tuple(search_store.events_after(saved.search_id))
    before_keys = saved.records.list('v2_memory_turn_keys')
    before_items = saved.records.list('workspace_items')
    before_calls = len(saved.model.calls)
    for _ in range(3):
        assert validate_answer_authority(saved.query, 'project-a', saved.first_id, saved.parent) == saved.facts
    assert len(saved.search_wire.calls) == 1 and len(saved.model.calls) == before_calls
    assert tuple(search_store.events_after(saved.search_id)) == before_events
    assert saved.records.list('v2_memory_turn_keys') == before_keys
    assert saved.records.list('workspace_items') == before_items


@pytest.mark.parametrize('change', ['model', 'output', 'privacy', 'source'])
def test_search_answer_dependency_rejects_original_authority_drift_without_replay(do_env, change):
    saved = search_dependency(do_env)
    before_calls = len(saved.model.calls)
    before_items = saved.records.list('workspace_items')
    if change == 'model':
        saved.model.update('search', {'model':'changed-search-model', 'expected_revision':1})
    elif change == 'output':
        # 在临时原内核库注入成果漂移，继续通过原 owner 读取并拒绝。
        store = MemoryTurn.store_for(saved.records)
        with sqlite3.connect(store._path) as connection:
            row = connection.execute('SELECT payload_json FROM ai_turn_immutable_payloads '
                'WHERE turn_id=? AND kind=?', (saved.search_id, _OUTPUT)).fetchone()
            assert row is not None
            output = json.loads(row[0])
            output['output'][0]['text'] = '已改变的搜索成果'
            connection.execute('UPDATE ai_turn_immutable_payloads SET payload_json=? '
                'WHERE turn_id=? AND kind=?', (json.dumps(output, ensure_ascii=False), saved.search_id, _OUTPUT))
    elif change == 'privacy':
        from backend.memory_app.v2.privacy import set_private_project
        set_private_project(saved.records, 'unrelated', True, 0)
    else:
        SourceEgressService(saved.records).set_policy(scope=WorkScope('local-user', 'project-a'),
            source_type='recognition', source_id=saved.insight.id, allowed_purposes=[],
            expected_source_revision=saved.insight.revision, expected_policy_revision=0)
    with pytest.raises((RecognitionError, ModelConfigurationError)):
        validate_answer_authority(saved.query, 'project-a', saved.first_id, saved.parent)
    assert len(saved.search_wire.calls) == 1 and len(saved.model.calls) == before_calls
    assert saved.records.list('workspace_items') == before_items


def test_search_answer_dependency_without_original_proof_is_rejected(do_env):
    saved = search_dependency(do_env)
    before_calls = len(saved.model.calls)
    facts = {**saved.facts, 'search':None}
    with pytest.raises(RecognitionConflict, match='search_proof_missing'):
        _validate_answer_facts(saved.query, facts)
    assert len(saved.search_wire.calls) == 1 and len(saved.model.calls) == before_calls


def test_original_search_guard_is_reconstructed_from_the_closed_answer_capsule(do_env):
    from backend.memory_app.kernel.answer_continuations import PLAN_KIND
    from backend.memory_app.v2.workbench import _restored_answer_plan
    saved = search_dependency(do_env)
    store = saved.query.answer_turns.application.state.ai_turn_store
    plan_ref, binding = store.get_immutable_payload(saved.first_id, PLAN_KIND)
    # 成功首答会清理 prepared 旁路行；只用同一原不可变成果核对守卫重建。
    row = SimpleNamespace(object_id=saved.first_id, payload={
        'plan_ref':plan_ref, 'project_id':'project-a', 'question':binding['question']})
    before_calls = len(saved.model.calls)
    before_events = tuple(store.events_after(saved.search_id))
    before_keys = saved.records.list('v2_memory_turn_keys')
    restored = _restored_answer_plan(saved.query, row)
    assert restored['search']['turn_id'] == saved.search_id
    assert callable(restored['search_guard'])
    saved.query.validate_ask_plan(restored)
    with pytest.raises(RecognitionConflict, match='search_proof_missing'):
        saved.query.validate_ask_plan({key:value for key, value in restored.items() if key != 'search_guard'})
    search_store = MemoryTurn.store_for(saved.records)
    with sqlite3.connect(search_store._path) as connection:
        raw, = connection.execute('SELECT payload_json FROM ai_turn_immutable_payloads '
            'WHERE turn_id=? AND kind=?', (saved.search_id, _OUTPUT)).fetchone()
        changed = json.loads(raw)
        changed['output'][0]['text'] = '已改变的搜索成果'
        connection.execute('UPDATE ai_turn_immutable_payloads SET payload_json=? '
            'WHERE turn_id=? AND kind=?', (json.dumps(changed, ensure_ascii=False), saved.search_id, _OUTPUT))
    with pytest.raises(RecognitionConflict, match='search_output_changed'):
        saved.query.validate_ask_plan(restored)
    assert len(saved.search_wire.calls) == 1 and len(saved.model.calls) == before_calls
    assert tuple(store.events_after(saved.search_id)) == before_events
    assert saved.records.list('v2_memory_turn_keys') == before_keys
