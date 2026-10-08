"""Actual organization planners preserve governed, closed model interruptions."""
import json
import sqlite3
import time
import pytest
from pydantic import BaseModel

from backend.api.agent_organization_planner import MainCoordinationPlanner, StewardPlanningPlanner
from backend.memory_app.kernel.policy_runtime import retry_policy_for
from backend.memory_app.kernel.product_routing import ProductGenerationRouting
from backend.memory_app.model_config import ModelConfiguration
from backend.security.secrets import InMemorySecretStore
from backend.shared.llm.model_transport import ModelInterrupted, is_provider_closed_witness
from core.ai_kernel import SQLiteAITurnStore, SynchronousAIRuntime, ScopedCapabilityRegistry
from core.ai_kernel.model_planner import ModelGatewayAgentPlanner
from core.ai_kernel.turn_kinds import freeze_turn_request
from core.storage_provider import SQLiteStructuredRecordStore
from tests.memory_app.v2.test_workbench_do import env


class SummaryWire(BaseModel):
    answer: str


@pytest.mark.parametrize('role', ['main', 'steward'])
def test_organization_planner_propagates_real_closed_body_interruption(tmp_path, role):
    records = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    calls, closed, partials, decisions, interruptions = [], [], [], [], []

    def provider(**request):
        calls.append(request)
        def stream():
            try:
                yield {'choices': [{'delta': {'content': '{"answer":"已完成段落。\\n\\n未完成'},
                                     'finish_reason': None}],
                       'usage': {'prompt_tokens': 5, 'completion_tokens': 3}}
                raise ConnectionError('synthetic organization wire interruption')
            finally:
                closed.append(True)
        return stream()

    models = ModelConfiguration(records, tmp_path, InMemorySecretStore(), completion_fn=provider)
    models.update('generation', {'base_url': 'https://api.deepseek.com', 'model': 'deepseek-flash',
        'api_key': 'synthetic-only', 'allow_remote': True, 'expected_revision': 0})
    store = SQLiteAITurnStore(tmp_path / '.rebuild-data/ai-turns.sqlite3')
    identity = 'task-interruption-' + role
    request = freeze_turn_request('project.task', turn_id=identity, session_id='task-session',
        operation_id=identity, idempotency_key=identity, project_id='alpha',
        created_at='2026-10-05T00:00:00Z', text='合并已经完成的成果', capabilities=[],
        privacy={'mode': 'remote_allowed', 'allow_remote': True, 'pii': 'possible',
            'consent_refs': ['crp://default/model-settings/generation'], 'retention': 'session'})
    class Gateway:
        def invoke(self, model_request):
            route = ProductGenerationRouting(models, store).acquire(request=request, project_id='alpha',
                project_profile_id='alpha', project_profile_revision=1,
                boundary_profile_id='test', boundary_profile_revision=1, capability_ids=[])
            models.complete_governed([{'role': 'user', 'content': model_request.input}],
                routing_snapshot=route.generation_binding(), execution_control=model_request.execution_control,
                metadata_sink=model_request.metadata_sink, wire_attempt_sink=model_request.execution_control,
                response_model=SummaryWire, on_delta=partials.append,
                retry_policy=retry_policy_for(request))
            raise AssertionError('disconnected provider cannot produce a completed decision')

    remote = ModelGatewayAgentPlanner(Gateway())
    organization = (MainCoordinationPlanner(remote=remote) if role == 'main' else
        StewardPlanningPlanner(remote=remote, proposal_validator=lambda value: value == {'mode': 'main_only'},
            proposal_builder=lambda *_: {'mode': 'main_only'}))

    class Boundary:
        def plan(self, value, events, capabilities, payloads, execution_control):
            try:
                decision = (organization._synthesize(value, events, capabilities, payloads,
                    execution_control, (), {}) if role == 'main' else
                    organization.plan(value, events, capabilities, payloads, execution_control))
            except ModelInterrupted as error:
                interruptions.append(error)
                raise
            decisions.append(decision)
            return decision

    runtime = SynchronousAIRuntime(planner=Boundary(), registry=ScopedCapabilityRegistry(),
        state=store, events=store, payloads=store)
    receipt = runtime.submit_turn(request)
    assert len(interruptions) == 1, decisions
    assert interruptions[0].partial == '已完成段落。\n\n'
    assert is_provider_closed_witness(interruptions[0].close_witness)
    assert decisions == []
    assert receipt.status == 'failed'  # Generic core still owns its original terminal semantics.
    rows = store.events_after(identity)
    dispatched = [row for row in rows if row['type'] == 'model.attempt.dispatched']
    terminals = [row for row in rows if row['type'] == 'model.attempt.terminal']
    assert len(dispatched) == len(terminals) == len(calls) == 1
    assert closed == [True]
    assert store.get(terminals[0]['data']['receipt_ref'])['status'] == 'failed_transport'
    assert not any(row['type'] in {'tool.intent.recorded', 'tool.started', 'turn.completed'} for row in rows)


@pytest.mark.parametrize('corrupt', [None, 'unknown_tool_effect', 'wrong_agent_owner'])
def test_product_task_summary_stream_closes_without_dispatching_partial_decision(env, monkeypatch, corrupt):
    client, models = env
    calls, closed = [], []
    observed = []
    from backend.memory_app.kernel.task_continuations import TaskContinuations
    original_pause = TaskContinuations.pause
    def observe(self, runtime, identity, control, error):
        if corrupt == 'wrong_agent_owner' and hasattr(error, 'control'):
            error.control.agents = object()
        flags = {'error': type(error).__name__, 'has_lease': runtime._run_lease_context.get() is not None,
            'same_control': getattr(error, 'control', None) is control,
            'same_request': getattr(error, 'request', None) == self.store.get_request(identity),
            'closed': is_provider_closed_witness(getattr(error, 'close_witness', None)),
            'terminal': control.model_terminal if control else None}
        observed.append(flags)
        try:
            flags['paused'] = original_pause(self, runtime, identity, control, error)
            return flags['paused']
        except Exception as failure:
            flags['failure'] = type(failure).__name__
            raise
    monkeypatch.setattr(TaskContinuations, 'pause', observe)
    def provider(**request):
        context = json.loads(request['messages'][-1]['content'])
        calls.append(request)
        steward = 'output' in context
        output = {'mode': 'main_only', 'assignments': []} if steward else {
            'type': 'complete', 'summary': '完整成果段落。\n\n后续成果'}
        text = json.dumps(output, ensure_ascii=False)
        if request.get('stream') is not True:
            return {'choices': [{'message': {'content': text}, 'finish_reason': 'stop'}],
                    'usage': {'prompt_tokens': 6, 'completion_tokens': 4}}
        def stream():
            try:
                if steward:
                    yield {'choices': [{'delta': {'content': text}, 'finish_reason': None}]}
                    yield {'choices': [{'delta': {}, 'finish_reason': 'stop'}],
                           'usage': {'prompt_tokens': 6, 'completion_tokens': 4}}
                else:
                    yield {'choices': [{'delta': {'content': text[:-2]}, 'finish_reason': None}],
                           'usage': {'prompt_tokens': 6, 'completion_tokens': 4}}
                    if corrupt == 'unknown_tool_effect':
                        runtime = client.app.state.ai_runtime
                        main = next(run for run in client.app.state.agent_runtime_composition.store.list_runs(
                            project_id='project-a') if run.role == 'main')
                        intent = next(row for row in runtime.events_after(main.turn_id)
                                      if row['type'] == 'tool.intent.recorded')
                        identity = intent['correlation']['tool_call_id']
                        log = runtime._effect_runner.log
                        assert log.get(identity).state.value == 'SETTLED_OK'
                        with sqlite3.connect(log.database) as connection:
                            connection.execute('UPDATE effect SET state=? WHERE operation_id=?', ('UNKNOWN', identity))
                    raise ConnectionError('synthetic product task summary disconnect')
            finally:
                closed.append('steward' if steward else 'main')
        return stream()
    models._completion_fn = provider
    result = client.post('/api/v2/workbench/turns', json={
        'project_id': 'project-a', 'intent': 'do', 'text': '完成一份文字汇总'})
    assert result.status_code == 200, result.text
    thread = result.json()['thread_id']
    deadline = time.monotonic() + 35
    while True:
        turn = client.get(f'/api/v2/workbench/threads/{thread}?project_id=project-a').json()['turns'][0]
        if turn['receipt']['do']['state'] in {'done', 'failed', 'partial'} or time.monotonic() >= deadline:
            break
        time.sleep(.05)
    summary = [call for call in calls if 'output' not in json.loads(call['messages'][-1]['content'])]
    assert len(summary) == 1 and summary[0].get('stream') is True
    assert closed == ['main']  # Steward's existing non-summary proposal stays unchanged.
    store = client.app.state.ai_turn_store
    main = next(run for run in client.app.state.agent_runtime_composition.store.list_runs(
        project_id='project-a') if run.role == 'main')
    rows = store.events_after(main.turn_id)
    terminal = [store.get(row['data']['receipt_ref']) for row in rows if row['type'] == 'model.attempt.terminal']
    assert len(terminal) == 1 and terminal[0]['status'] == 'failed_transport'
    if corrupt is not None:
        assert client.app.state.ai_runtime.receipt_for(main.turn_id).status == 'failed', observed
        assert not any(row['type'] == 'model.result.discarded' for row in rows)
        assert client.app.state.recognition_service.records.read('v2_task_continuations', main.turn_id) is None
        return
    assert client.app.state.ai_runtime.receipt_for(main.turn_id).status == 'waiting_approval', observed
    assert rows[-1]['type'] == 'model.result.discarded'
    assert rows[-1]['correlation']['model_request_id'] == terminal[0]['model_request_id']
    assert not any(row['type'] in {'approval.required', 'turn.failed'} for row in rows)
    assert not any(row['type'] == 'turn.completed' for row in rows)
    assert not client.app.state.recognition_service.records.list('v2_task_draft_operations')
    saved = client.app.state.recognition_service.records.read('v2_task_continuations', main.turn_id)
    capsule = store.get(saved.payload['capsule_ref'])
    assert set(rows[-1]['data']['evidence_refs']) == {saved.payload['capsule_ref']}
    assert capsule['request'] == store.get_request(main.turn_id)
    assert capsule['partial'] == '完整成果段落。\n\n'
    assert capsule['attempts'][0]['attempt_id'] == terminal[0]['attempt_id']
    assert capsule['lease']['owner_id'] and capsule['lease']['generation'] > 0
    assert client.app.state.ai_runtime._effect_runner.log.get(terminal[0]['attempt_id']).state.value == 'UNKNOWN'
    before = len(calls)
    replay = client.app.state.ai_turn_runner.accept_and_submit(store.get_request(main.turn_id))
    assert replay.status == 'waiting_approval'
    assert len(calls) == before


def test_task_explicit_continue_uses_same_turn_new_model_attempt_without_redoing_tools(env):
    client, models = env
    calls, closed = [], []
    def provider(**request):
        context = json.loads(request['messages'][next(index for index in range(len(request['messages']) - 1, -1, -1)
            if request['messages'][index]['role'] == 'user' and request['messages'][index]['content'].startswith('{'))]['content'])
        steward = 'output' in context
        calls.append(('steward' if steward else 'main', request))
        text = json.dumps({'mode': 'main_only', 'assignments': []} if steward else
            {'type': 'complete', 'summary': '后续完成成果。'}, ensure_ascii=False)
        if not request.get('stream'):
            return {'choices': [{'message': {'content': text}, 'finish_reason': 'stop'}],
                    'usage': {'prompt_tokens': 6, 'completion_tokens': 4}}
        def stream():
            try:
                if sum(role == 'main' for role, _ in calls) == 1:
                    yield {'choices': [{'delta': {'content': '{"type":"complete","summary":"完整成果段落。\\n\\n未完成'},
                                        'finish_reason': None}], 'usage': {'prompt_tokens': 6, 'completion_tokens': 4}}
                    raise ConnectionError('synthetic summary body disconnect')
                yield {'choices': [{'delta': {'content': text}, 'finish_reason': None}]}
                yield {'choices': [{'delta': {}, 'finish_reason': 'stop'}],
                       'usage': {'prompt_tokens': 8, 'completion_tokens': 5}}
            finally:
                closed.append(True)
        return stream()
    models._completion_fn = provider
    response = client.post('/api/v2/workbench/turns', json={
        'project_id': 'project-a', 'intent': 'do', 'text': '完成一份文字汇总'})
    assert response.status_code == 200, response.text
    turn_id, thread = response.json()['turn']['id'], response.json()['thread_id']
    records, store = client.app.state.recognition_service.records, client.app.state.ai_turn_store
    saved = records.read('v2_task_executions', turn_id)
    identity = saved.payload['request']['turn_id']
    deadline = time.monotonic() + 25
    while (not store.events_after(identity)
           or client.app.state.ai_runtime.receipt_for(identity).status != 'waiting_approval'):
        assert time.monotonic() < deadline
        time.sleep(.05)
    turn = client.get(f'/api/v2/workbench/threads/{thread}?project_id=project-a').json()['turns'][0]
    assert turn['receipt']['do']['interruption'] == 'connection'
    assert turn['receipt']['do']['partial'] == '完整成果段落。\n\n'
    original_request = store.get_request(identity)
    original_rows = store.events_after(identity)
    tools = [row for row in original_rows if row['type'] == 'tool.intent.recorded']
    failed_receipt = next(store.get(row['data']['receipt_ref']) for row in original_rows if row['type'] == 'model.attempt.terminal')
    resumed = client.post(f'/api/v2/workbench/turns/{turn_id}/continue', json={'project_id': 'project-a'},
        headers={'Idempotency-Key': 'resume-task-once'})
    assert resumed.status_code == 200, resumed.text
    final = resumed.json()['receipt']['do']
    assert final['state'] == 'done' and final['kernel_turn_id'] == identity
    assert 'interruption' not in final and 'partial' not in final
    assert store.get_request(identity) == original_request
    rows = store.events_after(identity)
    assert [row for row in rows if row['type'] == 'tool.intent.recorded'] == tools
    attempts = [store.get(row['data']['receipt_ref']) for row in rows if row['type'] == 'model.attempt.terminal']
    assert len(attempts) == 2 and len({item['model_request_id'] for item in attempts}) == 2
    assert attempts[0] == failed_receipt and attempts[0]['status'] == 'failed_transport'
    assert attempts[1]['status'] == 'succeeded'
    assert client.app.state.ai_runtime._effect_runner.log.get(attempts[0]['attempt_id']).state.value == 'UNKNOWN'
    assert closed == [True, True]
    assert [role for role, _ in calls].count('main') == 2
    assert records.read('v2_task_executions', turn_id).payload['started'] is True
    repository = client.app.state.recognition_documents
    assert repository.markdown(final['document_id']) == '完整成果段落。\n\n后续完成成果。'
    replay = client.post(f'/api/v2/workbench/turns/{turn_id}/continue', json={'project_id': 'project-a'},
        headers={'Idempotency-Key': 'resume-task-once'})
    assert replay.status_code == 200 and replay.json() == resumed.json()
    assert [role for role, _ in calls].count('main') == 2


@pytest.mark.parametrize('end', ['complete', 'exhausted'])
def test_real_pure_text_worker_continues_at_most_three_times(env, end):
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
                'profile_id': 'subagent.worker', 'task': '写一段纯文字成果', 'goal': '文字目标',
                'deliverable': '文字成果', 'capabilities': ['memory.recall'], 'depends_on': []}]}, ensure_ascii=False)
            return {'choices': [{'message': {'content': text}, 'finish_reason': 'stop'}],
                    'usage': {'prompt_tokens': 5, 'completion_tokens': 3}}
        def stream():
            try:
                number = sum(owner == 'worker' for owner, _ in calls)
                if role == 'worker' and (end == 'exhausted' or number < 4):
                    yield {'choices': [{'delta': {'content': '{"type":"complete","summary":"已经完成段落。\\n\\n半截'},
                                        'finish_reason': None}], 'usage': {'prompt_tokens': 5, 'completion_tokens': 3}}
                    raise ConnectionError('synthetic pure text worker disconnect')
                text = json.dumps({'type': 'complete', 'summary': '最后完整成果。'}, ensure_ascii=False)
                yield {'choices': [{'delta': {'content': text}, 'finish_reason': None}]}
                yield {'choices': [{'delta': {}, 'finish_reason': 'stop'}],
                       'usage': {'prompt_tokens': 6, 'completion_tokens': 4}}
            finally:
                closed.append(role)
        return stream()
    models._completion_fn = provider
    response = client.post('/api/v2/workbench/turns', json={
        'project_id': 'project-a', 'intent': 'do', 'text': '分工完成纯文字成果并汇总'})
    assert response.status_code == 200, response.text
    store = client.app.state.ai_turn_store
    composition = client.app.state.agent_runtime_composition
    deadline = time.monotonic() + 40
    worker = None
    while time.monotonic() < deadline:
        worker = next((run for run in composition.store.list_runs(project_id='project-a')
                       if run.profile_id == 'subagent.worker'), None)
        if worker and store.events_after(worker.turn_id):
            status = client.app.state.ai_runtime.receipt_for(worker.turn_id).status
            if (status in {'waiting_approval', 'failed', 'completed'}
                    and worker.turn_id not in client.app.state.ai_turn_runner.active_turn_ids):
                break
        time.sleep(.05)
    assert worker is not None
    wire_calls = [request for role, request in calls if role == 'worker']
    assert len(wire_calls) == 4, {'status': status, 'wires': len(wire_calls)}
    rows = store.events_after(worker.turn_id)
    attempts = [store.get(row['data']['receipt_ref']) for row in rows if row['type'] == 'model.attempt.terminal']
    assert len(attempts) == 4 and len({item['model_request_id'] for item in attempts}) == 4
    assert all(item['status'] == 'failed_transport' for item in attempts[:3])
    assert status == ('completed' if end == 'complete' else 'failed')
    assert attempts[-1]['status'] == ('succeeded' if end == 'complete' else 'failed_transport')
    assert closed.count('worker') == 4
    assert not any(row['type'] in {'tool.intent.recorded', 'approval.required'} for row in rows)
    assert all(client.app.state.ai_runtime._effect_runner.log.get(item['attempt_id']).state.value == 'UNKNOWN'
               for item in attempts if item['status'] == 'failed_transport')
    assert sum(item['usage']['input_tokens'] for item in attempts) >= 20
