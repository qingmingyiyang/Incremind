"""Automatic pure-text continuation cannot acquire a tool decision."""
import json
import time
import pytest

from tests.memory_app.v2.test_workbench_do import env


def test_real_worker_automatic_text_continuation_rejects_a_valid_allowed_tool(env):
    _automatic_worker(env)


@pytest.mark.parametrize('raw', [
    '{"type":"complete","type":"complete","summary":"重复类型"}',
    '{"type":"complete","summary":"原文","summary":"重复摘要"}',
    '{"type":"complete","summary":"文字","unknown":"未知字段"}',
    '{"type":"complete","summary":"文字","capability_id":"document.draft.propose","arguments":{}}',
])
def test_real_worker_automatic_continuation_validates_entire_raw_before_decision(env, raw):
    _automatic_worker(env, raw)


def _automatic_worker(env, raw=None):
    client, models = env
    calls, closed = [], []
    def provider(**request):
        context = json.loads(next(message['content'] for message in reversed(request['messages'])
            if message['role'] == 'user' and message['content'].startswith('{')))
        role = 'steward' if 'output' in context else 'main' if any(
            item['capability_id'] == 'agent.list' for item in context['capabilities']) else 'worker'
        calls.append((role, request))
        if role == 'steward':
            text = json.dumps({'mode': 'cluster', 'assignments': [{
                'profile_id': 'subagent.worker', 'task': '写出文字成果', 'goal': '文字目标',
                'deliverable': '文字成果', 'capabilities': ['document.draft.propose'], 'depends_on': []}]},
                ensure_ascii=False)
            return {'choices': [{'message': {'content': text}, 'finish_reason': 'stop'}],
                    'usage': {'prompt_tokens': 5, 'completion_tokens': 3}}
        def stream():
            try:
                number = sum(owner == 'worker' for owner, _ in calls)
                if role == 'main' or number == 1:
                    yield {'choices': [{'delta': {'content': '{"type":"complete","summary":"已经完成文字段落。\\n\\n半截'},
                                        'finish_reason': None}], 'usage': {'prompt_tokens': 5, 'completion_tokens': 3}}
                    raise ConnectionError('synthetic closed pure-text interruption')
                output = ({'type': 'tool', 'capability_id': 'document.draft.propose',
                           'arguments': {'title': '不应自动生成', 'markdown': '不应自动保存的草稿'}}
                          if number == 2 else {'type': 'complete', 'summary': '工具之后的完整结果。'})
                yield {'choices': [{'delta': {'content': raw if number == 2 and raw is not None
                                             else json.dumps(output, ensure_ascii=False)},
                                    'finish_reason': None}]}
                yield {'choices': [{'delta': {}, 'finish_reason': 'stop'}],
                       'usage': {'prompt_tokens': 7, 'completion_tokens': 4}}
            finally:
                closed.append(role)
        return stream()
    models._completion_fn = provider
    response = client.post('/api/v2/workbench/turns', json={
        'project_id': 'project-a', 'intent': 'do', 'text': '分工完成文字成果'})
    assert response.status_code == 200, response.text
    store = client.app.state.ai_turn_store
    composition = client.app.state.agent_runtime_composition
    worker = None
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        worker = next((run for run in composition.store.list_runs(project_id='project-a')
                       if run.profile_id == 'subagent.worker'), None)
        if worker and store.events_after(worker.turn_id):
            status = client.app.state.ai_runtime.receipt_for(worker.turn_id).status
            if status in {'failed', 'completed'} and worker.turn_id not in client.app.state.ai_turn_runner.active_turn_ids:
                break
        time.sleep(.05)
    assert worker is not None
    rows = store.events_after(worker.turn_id)
    attempts = [store.get(row['data']['receipt_ref']) for row in rows if row['type'] == 'model.attempt.terminal']
    tool_intents = [row for row in rows if row['type'] == 'tool.intent.recorded']
    drafts = client.app.state.recognition_service.records.list('v2_task_draft_operations')
    assert tool_intents == [], {'capabilities': [row['data']['capability_id'] for row in tool_intents],
                               'worker_wires': sum(role == 'worker' for role, _ in calls), 'drafts': len(drafts)}
    assert drafts == ()
    assert status == 'failed'
    assert len(attempts) == 2 and len({item['model_request_id'] for item in attempts}) == 2
    assert attempts[0]['status'] == 'failed_transport'
    assert client.app.state.ai_runtime._effect_runner.log.get(attempts[0]['attempt_id']).state.value == 'UNKNOWN'
    assert sum(role == 'worker' for role, _ in calls) == closed.count('worker') == 2
    assert sum(item['usage']['input_tokens'] for item in attempts) == 12
    assert sum(item['usage']['output_tokens'] for item in attempts) == 7
