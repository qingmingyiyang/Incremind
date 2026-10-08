"""Final-draft declarations use the real organization and governed transport."""
import json
import time

import pytest

from tests.memory_app.v2.test_workbench_do import env


@pytest.mark.parametrize('declaration, expected_calls, same_title', [
    ('deliverable', 1, False),
    (None, 2, False),
    (None, 2, True),
    ('another deliverable', 2, False),
])
def test_final_draft_ends_only_for_an_explicit_frozen_deliverable(env, declaration, expected_calls, same_title):
    client, model = env
    contexts = []
    outputs = [('worker-one', '行程建议', '九月出行安排'),
               ('worker-two', '检查结论', '需要核对的事项')]
    if same_title:
        outputs = [(task, deliverable, deliverable) for task, deliverable, _ in outputs]

    def respond(messages, **kwargs):
        context = json.loads(messages[-1]['content'])
        contexts.append(context)
        if 'output' in context:
            return json.dumps({'mode': 'cluster', 'assignments': [
                {'profile_id': 'subagent.worker', 'task': task, 'goal': task,
                 'deliverable': deliverable, 'capabilities': ['document.draft.propose'],
                 'depends_on': []} for task, deliverable, _ in outputs]})
        if any(cap['capability_id'] == 'agent.list' for cap in context.get('capabilities', [])):
            return json.dumps({'type': 'complete', 'summary': '两部分合成成果'})
        task, deliverable, title = next(row for row in outputs if row[0] in context['input']['text'])
        summary = title + '\n' + '保持相同的草稿正文'
        if any(event['type'] in {'tool.completed', 'tool.failed'} for event in context['events']):
            return json.dumps({'type': 'complete', 'summary': summary})
        args = {'title': title, 'markdown': '保持相同的草稿正文'}
        if declaration is not None:
            args['final_for'] = deliverable if declaration == 'deliverable' else declaration
        return json.dumps({'type': 'tool', 'capability_id': 'document.draft.propose', 'arguments': args})

    model.handler = respond
    client.app.state.recognition_turn_dispatcher._runtime()
    response = client.post('/api/v2/workbench/turns', json={
        'project_id': 'project-a', 'intent': 'do', 'text': '分别准备两部分并汇总'})
    assert response.status_code == 200, response.text
    data = response.json()
    deadline = time.monotonic() + 90
    while True:
        receipt = client.get(f"/api/v2/workbench/threads/{data['thread_id']}?project_id=project-a").json()['turns'][0]['receipt']['do']
        if receipt['state'] in {'done', 'partial', 'failed'} or time.monotonic() >= deadline:
            break
        time.sleep(.1)
    assert receipt['state'] == 'done', receipt
    composition = client.app.state.agent_runtime_composition
    store = client.app.state.ai_turn_store
    workers = [run for run in composition.store.list_runs(project_id='project-a')
               if run.profile_id == 'subagent.worker']
    assert len(workers) == 2
    records = client.app.state.recognition_service.records
    operations = records.list('v2_task_draft_operations')
    for worker in workers:
        text = composition.request_loader(worker.turn_id)['input']['text']
        _, deliverable, title = next(row for row in outputs if row[0] in text)
        events = store.events_after(worker.turn_id)
        assert sum(event['type'] == 'model.attempt.dispatched' for event in events) == expected_calls
        assert sum(row[0] in context.get('input', {}).get('text', '') for context in contexts for row in outputs
                   if row[0] in text and 'output' not in context) == expected_calls
        operation = next(row for row in operations if row.payload['inputs']['turn_id'] == worker.turn_id)
        assert operation.payload['inputs']['title'] == title
        assert operation.payload['inputs']['markdown'] == '保持相同的草稿正文'
        completed = [event for event in events if event['type'] == 'tool.completed'
                     and event['data']['capability_id'] == 'document.draft.propose']
        assert len(completed) == 1
        result = store.get(completed[0]['data']['payload_ref'])
        assert result.get('terminate', False) is (declaration == 'deliverable')
        if declaration == 'deliverable':
            assert result['final_for'] == deliverable
            assert result['title'] == title
        terminal = next(event for event in reversed(events) if event['type'] == 'turn.completed')
        assert terminal['data']['summary'] == title + '\n保持相同的草稿正文'
    main_context = next(context for context in reversed(contexts)
                        if any(cap['capability_id'] == 'agent.list' for cap in context.get('capabilities', [])))
    topology = next(event['resolved_payload'] for event in reversed(main_context['events'])
                    if event['type'] == 'tool.completed' and event['data']['capability_id'] == 'agent.list')
    conclusions = [child['conclusion'] for fan_in in topology['fan_ins']
                   for child in fan_in['result']['children'] if child['child_run_id'] in {run.run_id for run in workers}]
    assert sorted(conclusions) == sorted(title + '\n保持相同的草稿正文' for _, _, title in outputs)
    assert all(len(value) <= 2000 for value in conclusions)
    wires = [store.get(event['data']['receipt_ref'])
             for run in composition.store.list_runs(project_id='project-a')
             for event in store.events_after(run.turn_id)
             if event['type'] == 'model.attempt.terminal']
    assert len(wires) == len(model.calls) == 2 + 2 * expected_calls
    assert all(wire['status'] == 'succeeded' for wire in wires)
    expected_usage = {key: sum(wire['usage'][key] for wire in wires)
                      for key in ('input_tokens', 'output_tokens', 'total_tokens')}
    assert expected_usage == {'input_tokens': 4 * len(wires),
                              'output_tokens': 2 * len(wires), 'total_tokens': 6 * len(wires)}
    assert receipt['model_usage'] == expected_usage
    from backend.memory_app.v2.settings import _receipts
    egress = [row for row in _receipts(records, 50, runtime_root=model.root) if row['purpose'] == '干活']
    assert len(egress) == 1
    assert egress[0]['usage'] == {'input': expected_usage['input_tokens'],
                                 'output': expected_usage['output_tokens']}
