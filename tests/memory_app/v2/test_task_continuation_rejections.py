"""Real closed planner pauses confer no authority after current facts drift."""
import json
import sqlite3
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

from backend.memory_app.kernel.task_continuations import COLLECTION
from backend.memory_app.workspace_contracts import _now
from backend.recognition import WorkScope
from core.ai_kernel import validate_turn_action
from tests.memory_app.v2.test_profile import publish
from tests.memory_app.v2.test_workbench_do import env


def closed_main(env, *, profile=False):
    client, models = env
    runtime, store = client.app.state.ai_runtime, client.app.state.ai_turn_store
    service = client.app.state.recognition_service
    item = publish(SimpleNamespace(records=service.records, service=service),
                   'I prefer verifiable synthetic conclusions.') if profile else None
    calls, closed = [], []

    def provider(**request):
        context = json.loads(request['messages'][-1]['content'])
        steward = 'output' in context
        # An explorer also has agent.list; use the actual frozen role packet.
        main_role = client.app.state.agent_runtime_composition.profiles.get('main.orchestrator').organization_role
        role = ('steward' if steward else 'main'
                if context['role']['organization_role'] == main_role else 'worker')
        calls.append((role, request))
        text = json.dumps({'mode': 'main_only', 'assignments': []} if steward else
                          {'type': 'complete', 'summary': '已闭合的任务段落。\n\n未完成尾部' if role == 'main'
                           else '已完成的工作单元。'},
                          ensure_ascii=False)
        if not request.get('stream'):
            return {'choices': [{'message': {'content': text}, 'finish_reason': 'stop'}],
                    'usage': {'prompt_tokens': 6, 'completion_tokens': 4}}

        def stream():
            try:
                assert not steward
                yield {'choices': [{'delta': {'content': text[:-2] if role == 'main' else text},
                                    'finish_reason': None}],
                       'usage': {'prompt_tokens': 6, 'completion_tokens': 4}}
                if role == 'main':
                    raise ConnectionError('synthetic closed task for rejection matrix')
                yield {'choices': [{'delta': {}, 'finish_reason': 'stop'}],
                       'usage': {'prompt_tokens': 6, 'completion_tokens': 4}}
            finally:
                closed.append(role)
        return stream()

    models._completion_fn = provider
    response = client.post('/api/v2/workbench/turns', json={
        'project_id': 'project-a', 'intent': 'do', 'text': '核对关闭后的任务权限'})
    assert response.status_code == 200, response.text
    product = response.json()['turn']['id']
    saved = service.records.read('v2_task_executions', product)
    identity = saved.payload['request']['turn_id']
    deadline = time.monotonic() + 35
    while (not store.events_after(identity)
           or identity in client.app.state.ai_turn_runner.active_turn_ids
           or service.records.read('v2_task_executions', product).payload['owner'] is not None):
        assert time.monotonic() < deadline
        time.sleep(.05)
    assert runtime.receipt_for(identity).status == 'waiting_approval'
    binding = runtime.task_continuations.paused(identity, 'project-a')
    assert binding is not None
    assert binding[1]['partial'] == '已闭合的任务段落。\n\n'
    rows = tuple(store.events_after(identity))
    attempts = [store.get(row['data']['receipt_ref']) for row in rows
                if row['type'] == 'model.attempt.terminal']
    assert len(attempts) == 1 and attempts[0]['status'] == 'failed_transport'
    assert runtime._effect_runner.log.get(attempts[0]['attempt_id']).state.value == 'UNKNOWN'
    assert closed.count('main') == 1 and [role for role, _ in calls].count('main') == 1
    assert not service.records.list('v2_task_draft_operations')
    if item is not None:
        frozen = service.records.read('v2_task_profiles', identity).payload['profile']
        assert [entry['id'] for entry in frozen['items']] == [item.id]
        assert any(item.content in message['content'] for _, call in calls for message in call['messages'])
    return SimpleNamespace(client=client, models=models, runtime=runtime, store=store,
        service=service, records=service.records, identity=identity, product=product,
        binding=binding, calls=calls, closed=closed, attempt=attempts[0], item=item)


def action(state, kind):
    rows = tuple(state.store.events_after(state.identity))
    return validate_turn_action({'schema_version': '1.0.0', 'action_id': 'action-' + uuid4().hex,
        'turn_id': state.identity, 'type': kind,
        'target_event_id': rows[-1]['event_id'] if kind == 'approve' else None,
        'reason': 'synthetic explicit authority negative', 'actor': 'user',
        'expected_sequence': len(rows), 'idempotency_key': 'negative-' + kind, 'created_at': _now()})


@pytest.mark.parametrize('change', ['model', 'remote_disabled', 'private_project', 'missing_descriptor',
    'bad_capsule_ref', 'bad_event', 'wrong_lease', 'unknown_tool_effect', 'cancelled',
    'profile_revoke', 'profile_forget'])
def test_closed_task_rejects_current_drift_or_corrupt_authority_without_action_or_wire(env, change):
    state = closed_main(env, profile=change.startswith('profile_'))
    row, capsule = state.binding
    if change in {'model', 'remote_disabled'}:
        state.models.update('generation', {'expected_revision': state.models.public()['generation']['revision'],
            **({'model': 'another-synthetic-model'} if change == 'model' else {'allow_remote': False})})
    elif change == 'private_project':
        from backend.memory_app.v2.privacy import set_private_project
        set_private_project(state.records, 'project-a', True, 0)
    elif change in {'missing_descriptor', 'bad_capsule_ref', 'bad_event'}:
        with state.records.begin() as tx:
            if change == 'missing_descriptor':
                tx.delete(COLLECTION, state.identity, expected_revision=row.revision)
            else:
                values = dict(row.payload)
                if change == 'bad_capsule_ref':
                    values['capsule_ref'] = state.store.get_or_create_immutable_payload(
                        state.identity, 'unbound-negative-capsule', capsule)
                else:
                    values['event_id'] = next(event['event_id'] for event in state.store.events_after(state.identity)
                                              if event['type'] == 'model.attempt.terminal')
                tx.put(COLLECTION, state.identity, values, expected_revision=row.revision)
            tx.commit()
    elif change == 'wrong_lease':
        # Immutable read caching retains the original trusted capsule. Alter
        # the exact live reservation metadata checked by the read-only guard.
        with sqlite3.connect(state.store._path) as connection:
            changed = connection.execute(
                'UPDATE ai_model_attempt_reservations SET lease_generation=? WHERE turn_id=? AND attempt_id=?',
                (capsule['lease']['generation'] + 1, state.identity, state.attempt['attempt_id']))
            assert changed.rowcount == 1
        assert state.store.get(row.payload['capsule_ref']) == capsule
    elif change == 'unknown_tool_effect':
        invocation = capsule['completed_tools'][0]
        log = state.runtime._effect_runner.log
        assert log.get(invocation).state.value == 'SETTLED_OK'
        with sqlite3.connect(log.database) as connection:
            connection.execute('UPDATE effect SET state=? WHERE operation_id=?', ('UNKNOWN', invocation))
    elif change == 'cancelled':
        receipt = state.client.app.state.ai_turn_runner.apply_action_and_wait(action(state, 'cancel'))
        assert receipt.status == 'cancelled'
    elif change == 'profile_revoke':
        state.service.revoke(scope=WorkScope('local-user', 'me'), recognition_id=state.item.id,
            expected_revision=state.item.revision, reason='synthetic profile revoked after close')
    else:
        from backend.memory_app.recall_preferences import set_preference
        set_preference(state.records, WorkScope('local-user', 'me'), state.item.id,
            recognition_revision=state.item.revision, preference_revision=0, state='forgotten')
    before = tuple(state.store.events_after(state.identity))
    descriptor = state.records.read(COLLECTION, state.identity)
    tracked = {name: state.records.list(name) for name in
               ('v2_turns', 'v2_task_executions', 'v2_task_draft_operations', 'v2_usage_insight', 'v2_usage_document')}
    call_count = len(state.calls)
    closed_before = tuple(state.closed)
    response = state.client.post(f'/api/v2/workbench/turns/{state.product}/continue',
        json={'project_id': 'project-a'}, headers={'Idempotency-Key': 'rejected-task-' + change})
    assert response.status_code == 409, response.text
    assert tuple(state.store.events_after(state.identity)) == before
    assert state.records.read(COLLECTION, state.identity) == descriptor
    assert {name: state.records.list(name) for name in tracked} == tracked
    assert state.store.get_action('rejected-task-' + change) is None
    assert state.store.get(state.binding[1]['model_receipt_ref'])['status'] == 'failed'
    assert state.store.get(next(event['data']['receipt_ref'] for event in before
        if event['type'] == 'model.attempt.terminal')) == state.attempt
    assert state.runtime._effect_runner.log.get(state.attempt['attempt_id']).state.value == 'UNKNOWN'
    assert len(state.calls) == call_count and tuple(state.closed) == closed_before
    assert state.closed.count('main') == 1


def test_internal_task_pause_rejects_generic_approve_without_granting_or_dispatching(env):
    state = closed_main(env)
    before = tuple(state.store.events_after(state.identity))
    with pytest.raises(ValueError):
        state.runtime.apply_action(action(state, 'approve'))
    assert tuple(state.store.events_after(state.identity)) == before
    assert state.store.get_action('negative-approve') is None
    assert state.runtime.receipt_for(state.identity).status == 'waiting_approval'
    assert state.runtime.task_continuations.paused(state.identity, 'project-a') == state.binding
    assert [role for role, _ in state.calls].count('main') == 1 and state.closed == ['main']
    assert not state.records.list('v2_task_draft_operations')
