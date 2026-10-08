"""Real product redo requests and completion transactions with original owners."""
import asyncio
from contextlib import contextmanager
import json
import sqlite3
from threading import Event
import time
from types import SimpleNamespace

import pytest

from backend.memory_app.v2.task_divisions import TaskDivisions
from backend.memory_app.v2.task_do import TaskDo
from backend.memory_app.v2.task_drafts import TaskDrafts
from backend.recognition import WorkScope
from backend.recognition.product_draft_dependencies import product_draft_source
from tests.memory_app.v2.test_workbench_do import env as do_env
from tests.memory_app.v2.test_outcome_divisions import division_env


COLLECTION = 'v2_outcome_corrections'
OLD = '最初完成的正文\n\n' + '旧' * 340
EDITED = '最后保存的正文\n\n' + '改' * 340
NEW = '新完成的正文\n\n' + '新' * 340


pytestmark = pytest.mark.usefixtures('legacy_outcome_defaults')


@pytest.fixture
def legacy_outcome_defaults(monkeypatch):
    """历史普通汇总协议仍校验原调用数，新默认补丁另有真实流程控制。"""
    from backend.memory_app.v2 import policies
    monkeypatch.delitem(policies.ACTIVE, 'continuation', raising=False)
    monkeypatch.delitem(policies.ACTIVE, 'style', raising=False)


@pytest.fixture
def scenario(do_env):
    client, models = do_env
    state = client.app.state
    # Preserve the original lifespan portal; only the external ASGI error mode changes.
    client._transport.raise_server_exceptions = False
    entered, release = Event(), Event()
    values = SimpleNamespace(client=client, models=models, state=state,
        records=state.recognition_records, documents=state.recognition_documents,
        summary=OLD, block=False, entered=entered, release=release)

    def respond(messages, **kwargs):
        context = json.loads(messages[-1]['content'])
        if 'output' in context:
            return json.dumps({'mode': 'cluster', 'assignments': [{
                'profile_id': 'subagent.worker', 'task': '准备成果', 'goal': '准备成果',
                'deliverable': '整理稿', 'capabilities': ['document.draft.propose'], 'depends_on': []}]})
        if any(cap['capability_id'] == 'agent.list' for cap in context.get('capabilities', [])):
            if values.block:
                entered.set()
                if not release.wait(timeout=25):
                    raise RuntimeError('synthetic provider barrier timeout')
            return json.dumps({'type': 'complete', 'summary': values.summary})
        return json.dumps({'type': 'tool', 'capability_id': 'document.draft.propose',
            'arguments': {'title': '组成部分', 'markdown': '组成正文', 'final_for': '整理稿'}})

    models.handler = respond  # Only the external provider protocol; all owners stay real.
    state.recognition_turn_dispatcher._runtime()
    yield values
    release.set()


def wait_product(env, data, *, terminal=True):
    identity = data['turn']['id']
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        response = env.client.get('/api/v2/workbench/threads/' + data['thread_id'] + '?project_id=project-a')
        assert response.status_code == 200, response.text
        turn = next(row for row in response.json()['turns'] if row['id'] == identity)
        if (not terminal or turn['receipt']['do']['state'] in {'done', 'partial', 'failed'}):
            if not terminal or not env.state.workbench_tasks:
                return turn
        time.sleep(.02)
    raise AssertionError('original product owner did not complete within its existing 90s fixture bound')


def completed(env, *, summary=OLD):
    env.summary = summary
    response = env.client.post('/api/v2/workbench/turns', json={
        'project_id': 'project-a', 'intent': 'do', 'text': '准备一份成果'})
    assert response.status_code == 200, response.text
    data = response.json()
    turn = wait_product(env, data)
    if summary:
        assert turn['receipt']['do']['state'] == 'done'
        document = turn['receipt']['do']['document_id']
        bound = product_draft_source(env.records, WorkScope('local-user', 'project-a'), document, 1)
        assert bound.revisions['task_execution_id'] == data['turn']['id']
        assert bound.revisions['product_turn_id'] != data['turn']['id']
        assert env.documents.markdown(document, revision=1) == summary
    else:
        assert turn['receipt']['do']['state'] == 'failed' and turn['receipt']['do']['document_id'] is None
    return data, turn


def cancel_active_main(env, data):
    assert env.entered.wait(timeout=15)
    kernel_id = data['turn']['receipt']['do']['kernel_turn_id']
    assert env.state.ai_turn_runner.request_turn_cancel(kernel_id, reason='synthetic product cancellation') is True
    env.release.set()
    turn = wait_product(env, data)
    assert env.state.ai_runtime.receipt_for(kernel_id).status == 'cancelled'
    assert turn['receipt']['do']['state'] == 'failed' and turn['receipt']['do']['document_id'] is None
    return turn


def redo(env, old, *, project='project-a', revision=1):
    return env.client.post('/api/v2/workbench/turns/' + old['turn']['id'] + '/redo',
        json={'project_id': project, 'expected_revision': revision})


def redo_events(env):
    return tuple(row for row in env.records.list(COLLECTION) if row.payload['kind'] == 'outcome_redo')


@contextmanager
def blocked_new_main(env):
    env.summary, env.block = NEW, True
    try:
        yield
    finally:
        env.release.set()


def test_real_completed_do_redo_freezes_current_history_then_completes_after(scenario):
    from backend.memory_app.v2.learning_events import events as learning_events, checkpoint

    env = scenario
    old, old_turn = completed(env)
    document_id = old_turn['receipt']['do']['document_id']
    saved = env.documents.save_user_edit(document_id, markdown=EDITED, title='最后保存的标题', expected_revision=1)
    assert saved['revision'] == 2
    old_row = env.records.read('v2_turns', old['turn']['id'])
    old_execution = env.records.read('v2_task_executions', old['turn']['id'])
    original_operation = env.records.read('v2_task_draft_operations', 'deliver-' + old_turn['receipt']['do']['kernel_turn_id'])
    with blocked_new_main(env):
        response = redo(env, old)
        assert response.status_code == 200, response.text
        new = response.json()
        assert new['turn']['id'] != old['turn']['id'] and new['thread_id'] == old['thread_id']
        pending, = redo_events(env)
        assert pending.payload['project_id'] == 'project-a'
        assert pending.payload['turn_id'] == old['turn']['id'] and pending.payload['new_turn_id'] == new['turn']['id']
        assert pending.payload['document_id'] == document_id
        assert pending.payload['birth_revision'] == 1 and pending.payload['from_revision'] == 2
        assert pending.payload['before_title'] == '最后保存的标题'
        assert pending.payload['before'] == EDITED[:300] and len(pending.payload['before']) == 300
        assert pending.payload['policy_version'] == '@1'
        assert 'after' not in pending.payload and 'after_title' not in pending.payload
        assert env.records.read('v2_turns', new['turn']['id']).payload['receipt']['do']['state'] == 'running'
        assert env.entered.wait(timeout=15)
        point = 'outcome:' + pending.object_id
        assert learning_events(env.records) == {}
        pending_states = checkpoint(env.records, 0)
        assert pending_states.get('project-a', {}).get('score', 0) == 0
        assert point not in pending_states.get('project-a', {}).get('seen_event_ids', [])
        assert env.records.read(COLLECTION, pending.object_id) == pending
    new_turn = wait_product(env, new)
    assert new_turn['receipt']['do']['state'] == 'done'
    event, = redo_events(env)
    assert event.object_id == pending.object_id and event.revision == 2
    assert {key: event.payload[key] for key in pending.payload} == pending.payload
    new_document = new_turn['receipt']['do']['document_id']
    assert event.payload['new_document_id'] == new_document
    assert event.payload['new_birth_revision'] == event.payload['to_revision'] == 1
    assert event.payload['after_title'] == env.documents.read(new_document)['title']
    assert event.payload['after'] == NEW[:300] and len(event.payload['after']) == 300
    assert env.documents.markdown(new_document, revision=1) == NEW
    facts = env.records.list(COLLECTION)
    assert learning_events(env.records) == {'project-a': {point}}
    scored = checkpoint(env.records, 0)
    assert scored['project-a']['score'] == 1 and scored['project-a']['seen_event_ids'] == [point]
    score_row = env.records.read('v2_learning_accumulation', 'project-a')
    assert checkpoint(env.records, 0) == scored
    assert env.records.read('v2_learning_accumulation', 'project-a') == score_row
    assert env.records.list(COLLECTION) == facts
    assert env.records.read('v2_turns', old['turn']['id']) == old_row
    assert env.records.read('v2_task_executions', old['turn']['id']) == old_execution
    assert env.records.read('v2_task_draft_operations', original_operation.object_id) == original_operation
    assert env.documents.markdown(document_id, revision=1) == OLD and env.documents.markdown(document_id, revision=2) == EDITED
    runs = env.state.agent_runtime_composition.store.list_runs(project_id='project-a')
    assert len(runs) == 6 and all(env.state.ai_runtime.receipt_for(run.turn_id).status == 'completed' for run in runs)
    events = [event for run in runs for event in env.state.ai_turn_store.events_after(run.turn_id)]
    assert sum(event['type'] == 'model.attempt.dispatched' for event in events) == 5  # Redo reuses explicit divisions, no new steward model.
    assert sum(event['type'] == 'tool.intent.recorded' and event['data'].get('capability_id') == 'document.draft.propose' for event in events) == 2
    assert len(env.models.calls) == 5
    execution = env.records.read('v2_task_executions', new['turn']['id'])
    assert execution.payload['started'] is True and execution.payload['owner'] is None
    service = TaskDo(env.records, env.models, TaskDrafts(env.records, env.documents), None, None, None)
    before = env.records.list_all()
    asyncio.run(service.advance(new['turn']['id']))
    assert env.records.list_all() == before


def test_sql_event_insert_failure_rolls_back_new_turn_and_execution(scenario):
    env = scenario
    old, _ = completed(env)
    before_turns = env.records.list('v2_turns')
    before_exec = env.records.list('v2_task_executions')
    before_threads = env.records.list('v2_threads')
    calls = len(env.models.calls)
    with sqlite3.connect(env.records.database_path) as connection:
        connection.execute("CREATE TRIGGER reject_redo_insert BEFORE INSERT ON crp_structured_records "
            "WHEN NEW.collection='v2_outcome_corrections' AND json_extract(NEW.payload_json,'$.kind')='outcome_redo' "
            "BEGIN SELECT RAISE(ABORT, 'synthetic redo insert failure'); END")
    response = redo(env, old)
    assert response.status_code == 500, response.text
    assert env.records.list('v2_turns') == before_turns
    assert env.records.list('v2_task_executions') == before_exec
    assert env.records.list('v2_threads') == before_threads
    assert redo_events(env) == () and len(env.models.calls) == calls


def test_failed_old_product_retry_remains_legal_and_has_no_redo_fact(scenario):
    env = scenario
    with blocked_new_main(env):
        response = env.client.post('/api/v2/workbench/turns', json={
            'project_id': 'project-a', 'intent': 'do', 'text': '准备一份成果'})
        assert response.status_code == 200, response.text
        old = response.json()
        cancel_active_main(env, old)
    env.block, env.summary = False, NEW
    response = redo(env, old)
    assert response.status_code == 200, response.text
    assert wait_product(env, response.json())['receipt']['do']['state'] == 'done'
    assert redo_events(env) == ()


@pytest.mark.parametrize('failure,status', [('cross_project', 404), ('stale', 409), ('deleted', 404)])
def test_original_redo_identity_and_sample_guards_still_reject(division_env, failure, status):
    env = division_env
    old = {'turn': {'id': env.identity}}
    if failure == 'deleted':
        TaskDivisions(env.records).remove(old['turn']['id'], project='project-a', expected_revision=1)
    before = env.records.list_all()
    calls = len(env.models.calls)
    response = redo(env, old, project='project-other' if failure == 'cross_project' else 'project-a',
        revision=2 if failure == 'stale' else 1)
    assert response.status_code == status, response.text
    assert env.records.list_all() == before and len(env.models.calls) == calls


def test_unknown_old_birth_is_not_adopted_as_a_redo_fact(scenario):
    env = scenario
    old, turn = completed(env)
    operation_id = 'deliver-' + turn['receipt']['do']['kernel_turn_id']
    with env.records.begin() as tx:
        # 历史成果没有版本链；保留旧未知出生的重做语义。
        lineage = tx.read('v2_outcome_lineage', turn['receipt']['do']['document_id'])
        assert lineage is not None
        tx.delete(lineage.collection, lineage.object_id, expected_revision=lineage.revision)
        operation = tx.read('v2_task_draft_operations', operation_id)
        tx.put('v2_task_draft_operations', operation_id, {**operation.payload,
            'result': {**operation.payload['result'], 'document_revision': True}}, expected_revision=operation.revision)
        tx.commit()
    env.summary = NEW
    response = redo(env, old)
    assert response.status_code == 200, response.text
    assert wait_product(env, response.json())['receipt']['do']['state'] == 'done'
    assert redo_events(env) == ()


def test_damaged_known_lineage_birth_cannot_fall_back_to_legacy_redo(scenario):
    env = scenario
    old, turn = completed(env)
    document = turn['receipt']['do']['document_id']
    assert env.records.read('v2_outcome_lineage', document) is not None
    operation_id = 'deliver-' + turn['receipt']['do']['kernel_turn_id']
    with env.records.begin() as tx:
        operation = tx.read('v2_task_draft_operations', operation_id)
        tx.put(operation.collection, operation.object_id, {**operation.payload,
            'result': {**operation.payload['result'], 'document_revision': True}}, expected_revision=operation.revision)
        tx.commit()
    before, calls = env.records.list_all(), len(env.models.calls)
    response = redo(env, old)
    assert response.status_code == 409 and response.json()['detail'] == 'outcome_selection_changed'
    assert len(env.models.calls) == calls and env.records.list_all() == before
    assert redo_events(env) == ()


def test_failed_new_product_keeps_pending_before_without_false_after(scenario):
    env = scenario
    old, _ = completed(env)
    with blocked_new_main(env):
        response = redo(env, old)
        assert response.status_code == 200, response.text
        new = response.json()
        turn = cancel_active_main(env, new)
    event, = redo_events(env)
    assert event.revision == 1 and event.payload['before'] == OLD[:300]
    assert event.payload['new_turn_id'] == new['turn']['id']
    assert not {'after', 'after_title', 'completed_at', 'new_document_id', 'to_revision'} & event.payload.keys()
    assert env.state.ai_runtime.receipt_for(turn['receipt']['do']['kernel_turn_id']).status == 'cancelled'


def test_sql_after_failure_rolls_back_original_final_publication(scenario):
    env = scenario
    old, _ = completed(env)
    with blocked_new_main(env):
        response = redo(env, old)
        assert response.status_code == 200, response.text
        new = response.json()
        pending, = redo_events(env)
        assert env.entered.wait(timeout=15)
        with sqlite3.connect(env.records.database_path) as connection:
            connection.execute("CREATE TRIGGER reject_redo_after BEFORE UPDATE ON crp_structured_records "
                "WHEN NEW.collection='v2_outcome_corrections' AND json_extract(NEW.payload_json,'$.kind')='outcome_redo' "
                "BEGIN SELECT RAISE(ABORT, 'synthetic redo after failure'); END")
    deadline = time.monotonic() + 90
    while env.state.workbench_tasks and time.monotonic() < deadline:
        time.sleep(.02)
    assert not env.state.workbench_tasks
    turn = env.records.read('v2_turns', new['turn']['id'])
    execution = env.records.read('v2_task_executions', new['turn']['id'])
    kernel_id = execution.payload['request']['turn_id']
    assert env.state.ai_runtime.receipt_for(kernel_id).status == 'completed'
    assert turn.payload['receipt']['do']['state'] == 'running' and turn.payload['receipt']['do']['document_id'] is None
    assert execution.payload['started'] is True and execution.payload['owner'] is not None
    assert env.records.read(COLLECTION, pending.object_id) == pending
    operation = env.records.read('v2_task_draft_operations', 'deliver-' + kernel_id)
    assert operation is not None and env.documents.markdown(operation.payload['result']['document_id'], revision=1) == NEW
    runs = env.state.agent_runtime_composition.store.list_runs(project_id='project-a')
    assert len(runs) == 6 and all(env.state.ai_runtime.receipt_for(run.turn_id).status == 'completed' for run in runs)
    assert len(env.models.calls) == 5


def test_missing_pending_policy_cannot_reinterpret_using_current_default(scenario):
    env = scenario
    old, _ = completed(env)
    with blocked_new_main(env):
        response = redo(env, old)
        assert response.status_code == 200, response.text
        new = response.json()
        assert env.entered.wait(timeout=15)
        with env.records.begin() as tx:
            pending, = redo_events(env)
            payload = {key: value for key, value in pending.payload.items() if key != 'policy_version'}
            damaged = tx.put(COLLECTION, pending.object_id, payload, expected_revision=pending.revision)
            tx.commit()
    assert wait_product(env, new)['receipt']['do']['state'] == 'done'
    assert env.records.read(COLLECTION, damaged.object_id) == damaged
    assert 'after' not in damaged.payload
