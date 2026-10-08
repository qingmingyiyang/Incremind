"""Exercise the edit HTTP owner and real SQLite history; never replace them."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib
import sqlite3
from threading import Barrier

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from backend.memory_app.v2.task_drafts import TaskDrafts
from core.document_engine.ports import DocumentDraft
from tests.memory_app.test_api import FakeModels, _shutdown
from tests.memory_app.v2.test_workbench_do import env as do_env


COLLECTION = 'v2_outcome_corrections'
BASE = '原来的做法\n\n保留的段落'


class Clock:
    def __init__(self):
        self.value = datetime(2026, 10, 6, 8, tzinfo=timezone.utc)

    def __call__(self):
        return self.value.isoformat()

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


@pytest.fixture
def edit_env(tmp_path, monkeypatch):
    monkeypatch.setenv('CHRIPTMAS_APP_ROOT', str(tmp_path))
    module = importlib.import_module('backend.memory_app.app')
    clock = Clock()
    monkeypatch.setattr(module, '_now', clock)  # External wall clock only.
    models = FakeModels()
    app = module.create_app(runtime_root=tmp_path, legacy_app=FastAPI(), model_configuration=models)
    client = TestClient(app, raise_server_exceptions=False)
    records, documents = app.state.recognition_records, app.state.recognition_documents
    # The original TaskDrafts owner writes the actual document and delivery operation.
    output = TaskDrafts(records, documents).create(turn_id='turn-root', project='project-a',
        operation='deliver-turn-root', title='成果', markdown=BASE)
    request = {'turn_id': 'turn-root', 'scope': {'kind': 'project', 'project_id': 'project-a', 'series_id': None},
        'desired_outcome': 'project.task', 'input': {'refs': []},
        'privacy': {'material_refs': [], 'source_snapshots': []}}
    # Persist the existing product publication facts, with no model or fake repository.
    with records.begin() as tx:
        tx.put('v2_task_executions', 'turn-product',
            {'project_id': 'project-a', 'started': True, 'request': request}, expected_revision=0)
        tx.put('v2_turns', 'turn-product', {'project_id': 'project-a', 'intent': 'do', 'receipt': {'do': {
            'state': 'done', 'document_id': output['document_id'], 'kernel_turn_id': 'turn-root', 'title': '成果'}}},
            expected_revision=0)
        tx.commit()
    yield client, records, documents, output['document_id'], clock, models
    _shutdown(client)


def _save(env, text, revision, *, identity=None):
    client, _, _, document_id, _, _ = env
    return client.patch('/api/recognition/documents/' + (identity or document_id), json={
        'project_id': 'project-a', 'expected_revision': revision, 'markdown': text})


def _event(env):
    rows = env[1].list(COLLECTION)
    assert len(rows) == 1
    return rows[0]


@pytest.mark.parametrize('gap,merge,mature', [
    (0, True, False), (600, True, False), (601, False, True),
    (-1, True, False), (None, True, False), (True, True, False),
    (float('nan'), True, False), (float('inf'), True, False), ('601', True, False),
])
def test_policy_window_is_pure_and_conservative(gap, merge, mature):
    from backend.memory_app.v2.policies import get
    result = get('outcome_correction', version='@1')(operation='window', gap_seconds=gap)
    assert result == {'merge': merge, 'mature': mature}
    assert all(type(value) is bool for value in result.values())


def test_policy_caps_are_single_authority_after_active_switch():
    from backend.memory_app.v2.policies import ACTIVE, get
    assert ACTIVE['outcome_correction'] == '@1'
    assert get('outcome_correction', version='@1')(operation='limits') == {
        'edit_side_chars': 600, 'outcome_side_chars': 300}


@pytest.mark.parametrize('existing', [False, True])
def test_writer_selects_default_and_preserves_existing_policy(edit_env, existing):
    from backend.memory_app.v2 import policies
    from backend.memory_app.v2.policies.outcome_correction import v1
    assert policies.version('outcome_correction') == '@1'
    assert policies.get('outcome_correction')(operation='limits')['edit_side_chars'] == 600
    if '@9902' not in policies._REGISTRY['outcome_correction']:
        def evaluation_policy(request=None, *, operation, gap_seconds=None):
            if operation == 'limits':
                return {'edit_side_chars': 3, 'outcome_side_chars': 300}
            return v1(request, operation=operation, gap_seconds=gap_seconds)
        policies.register('outcome_correction', '@9902')(evaluation_policy)
    if existing:
        assert _save(edit_env, '中间做法\n\n保留的段落', 1).status_code == 200
        original = _event(edit_env)
    with policies.override(**policies.parse_overrides(['outcome_correction=@9902'])):
        assert _save(edit_env, '最终做法\n\n保留的段落', 2 if existing else 1).status_code == 200
        event = _event(edit_env)
        assert event.payload['policy_version'] == ('@1' if existing else '@9902')
        assert event.payload['before'] == ('原来的做法' if existing else '原来的')
        assert event.payload['after'] == ('最终做法' if existing else '最终做')
        if existing:
            assert event.object_id == original.object_id and event.payload['from_revision'] == 1
    assert policies.version('outcome_correction') == '@1'


def test_http_root_uses_birth_r1_but_before_current_history(edit_env):
    _, records, documents, identity, _, models = edit_env
    operation = records.read('v2_task_draft_operations', 'deliver-turn-root')
    documents.save_user_edit(identity, markdown='已调整的做法\n\n保留的段落', expected_revision=1)
    response = _save(edit_env, '最终做法\n\n保留的段落', 2)
    assert response.status_code == 200, response.text
    event = _event(edit_env).payload
    assert event['kind'] == 'outcome_edit'
    assert event['turn_id'] == 'turn-product' and event['document_id'] == identity
    assert event['birth_revision'] == 1 and event['from_revision'] == 2 and event['to_revision'] == 3
    assert event['before'] == '已调整的做法' and event['after'] == '最终做法'
    assert event['policy_version'] == '@1' and event['net_change'] is True
    assert documents.markdown(identity, revision=1) == BASE
    assert records.read('v2_task_draft_operations', 'deliver-turn-root') == operation
    assert models.calls == []


def test_continuous_saves_keep_first_before_and_latest_after(edit_env):
    assert _save(edit_env, '中间做法\n\n保留的段落', 1).status_code == 200
    first = _event(edit_env)
    edit_env[4].advance(60)
    assert _save(edit_env, '最终做法\n\n保留的段落', 2).status_code == 200
    last = _event(edit_env)
    assert last.object_id == first.object_id and last.revision == first.revision + 1
    assert last.payload['from_revision'] == 1 and last.payload['to_revision'] == 3
    assert last.payload['before'] == '原来的做法' and last.payload['after'] == '最终做法'
    assert last.payload['created_at'] == first.payload['created_at']
    assert last.payload['last_saved_at'] == edit_env[4]()


def test_noop_save_keeps_last_successful_window_for_later_change(edit_env):
    text = '中间做法\n\n保留的段落'
    assert _save(edit_env, text, 1).status_code == 200
    first = _event(edit_env)
    edit_env[4].advance(590)
    assert _save(edit_env, text, 2).status_code == 200
    noop = _event(edit_env)
    assert noop.object_id == first.object_id and noop.revision == first.revision + 1
    assert noop.payload['from_revision'] == 1 and noop.payload['to_revision'] == 3
    assert noop.payload['last_saved_at'] == edit_env[4]()
    assert noop.payload['before'] == '原来的做法' and noop.payload['after'] == '中间做法'
    edit_env[4].advance(60)
    assert _save(edit_env, '最终做法\n\n保留的段落', 3).status_code == 200
    latest = _event(edit_env)
    assert latest.object_id == first.object_id and latest.revision == first.revision + 2
    assert latest.payload['from_revision'] == 1 and latest.payload['to_revision'] == 4
    assert latest.payload['before'] == '原来的做法' and latest.payload['after'] == '最终做法'
    assert latest.payload['created_at'] == first.payload['created_at']
    assert latest.payload['last_saved_at'] == edit_env[4]()


@pytest.mark.parametrize('seconds', [600, 601, -60, None])
def test_noop_window_boundary_and_clock_do_not_rewrite_mature_fact(edit_env, seconds):
    text = '中间做法\n\n保留的段落'
    assert _save(edit_env, text, 1).status_code == 200
    first = _event(edit_env)
    if seconds is None:
        with edit_env[1].begin() as tx:
            tx.put(COLLECTION, first.object_id, {**first.payload, 'last_saved_at': None},
                expected_revision=first.revision)
            tx.commit()
        first = _event(edit_env)
        edit_env[4].advance(60)
    else:
        edit_env[4].advance(seconds)
    assert _save(edit_env, text, 2).status_code == 200
    assert edit_env[2].read(edit_env[3])['revision'] == 3
    latest = _event(edit_env)
    if seconds == 601:
        assert latest == first
    else:
        assert latest.object_id == first.object_id and latest.revision == first.revision + 1
        assert latest.payload['from_revision'] == 1 and latest.payload['to_revision'] == 3
        assert latest.payload['last_saved_at'] == (edit_env[4]() if seconds == 600 else first.payload['last_saved_at'])
        assert latest.payload['before'] == first.payload['before'] and latest.payload['after'] == first.payload['after']


@pytest.mark.parametrize('seconds,expected', [(600, 1), (601, 2)])
def test_merge_boundary_is_last_successful_save(edit_env, seconds, expected):
    assert _save(edit_env, '中间做法\n\n保留的段落', 1).status_code == 200
    first = _event(edit_env)
    edit_env[4].advance(seconds)
    assert _save(edit_env, '最终做法\n\n保留的段落', 2).status_code == 200
    rows = edit_env[1].list(COLLECTION)
    assert len(rows) == expected
    if expected == 1:
        assert rows[0].object_id == first.object_id and rows[0].payload['from_revision'] == 1
    else:
        assert edit_env[1].read(COLLECTION, first.object_id) == first
        newest = next(row for row in rows if row.object_id != first.object_id)
        assert newest.payload['from_revision'] == 2 and newest.payload['before'] == '中间做法'


def test_clock_backwards_saves_same_window_without_earlier_maturity(edit_env):
    assert _save(edit_env, '中间做法\n\n保留的段落', 1).status_code == 200
    first = _event(edit_env)
    edit_env[4].advance(-60)
    assert _save(edit_env, '最终做法\n\n保留的段落', 2).status_code == 200
    last = _event(edit_env)
    assert last.object_id == first.object_id and last.payload['last_saved_at'] == first.payload['last_saved_at']
    assert last.payload['after'] == '最终做法' and last.payload['to_revision'] == 3
    from backend.memory_app.v2.policies import get
    assert get('outcome_correction', version=last.payload['policy_version'])(operation='window',
        gap_seconds=-60)['mature'] is False


def test_unknown_event_time_is_preserved_without_new_identity(edit_env):
    assert _save(edit_env, '中间做法\n\n保留的段落', 1).status_code == 200
    first = _event(edit_env)
    with edit_env[1].begin() as tx:
        tx.put(COLLECTION, first.object_id, {**first.payload, 'last_saved_at': None}, expected_revision=first.revision)
        tx.commit()
    assert _save(edit_env, '最终做法\n\n保留的段落', 2).status_code == 200
    last = _event(edit_env)
    assert last.object_id == first.object_id and last.payload['last_saved_at'] is None
    assert last.payload['after'] == '最终做法'


def test_unqualified_other_collection_input_id_cannot_close_edit_window(edit_env):
    assert _save(edit_env, '中间做法\n\n保留的段落', 1).status_code == 200
    first = _event(edit_env)
    # Current inputs consume the old correction collection, not this new owner.
    with edit_env[1].begin() as tx:
        tx.put('v2_consolidation_inputs', 'input-consumed', {'project_id': 'project-a',
            'documents': [], 'event_ids': [first.object_id]}, expected_revision=0)
        tx.commit()
    edit_env[4].advance(-60)
    assert _save(edit_env, '最终做法\n\n保留的段落', 2).status_code == 200
    latest = _event(edit_env)
    assert latest.object_id == first.object_id
    assert latest.payload['last_saved_at'] == first.payload['last_saved_at']
    assert latest.payload['after'] == '最终做法' and latest.payload['from_revision'] == 1


def test_return_to_first_before_keeps_history_and_same_identity(edit_env):
    assert _save(edit_env, '中间做法\n\n保留的段落', 1).status_code == 200
    first = _event(edit_env)
    edit_env[4].advance(10)
    assert _save(edit_env, BASE, 2).status_code == 200
    reverted = _event(edit_env)
    assert reverted.object_id == first.object_id and reverted.payload['to_revision'] == 3
    assert reverted.payload['before'] == '' and reverted.payload['after'] == ''
    assert reverted.payload['net_change'] is False
    assert edit_env[2].markdown(edit_env[3], revision=2) == '中间做法\n\n保留的段落'
    edit_env[4].advance(10)
    assert _save(edit_env, '最终做法\n\n保留的段落', 3).status_code == 200
    resumed = _event(edit_env)
    assert resumed.object_id == first.object_id and resumed.payload['from_revision'] == 1
    assert resumed.payload['net_change'] is True and resumed.payload['after'] == '最终做法'


@pytest.mark.parametrize('old,new,before,after', [
    ('共同\n\n旧\n\n尾', '共同\n\n新\n\n尾', '旧', '新'),
    ('共同\n\n尾', '共同\n\n新增\n\n尾', '', '新增'),
    ('共同\n\n删除\n\n尾', '共同\n\n尾', '删除', ''),
    ('共同\r\n\r\n旧\r\n行\r\n\r\n尾', '共同\r\n\r\n新\r\n行\r\n\r\n尾', '旧\r\n行', '新\r\n行'),
    ('共同\n\n```py\na=1\n\nb=2\n```\n\n尾',
     '共同\n\n```py\na=1\n\nb=3\n```\n\n尾', '```py\na=1\n\nb=2\n```', '```py\na=1\n\nb=3\n```'),
    ('共同\n\n' + '甲' * 650, '共同\n\n' + '乙' * 650, '甲' * 600, '乙' * 600),
])
def test_paragraph_diff_uses_real_revisions_and_policy_caps(edit_env, old, new, before, after):
    # Retain the real birth r1; start this correction from an actual later history row.
    edit_env[2].save_user_edit(edit_env[3], markdown=old, expected_revision=1)
    response = _save(edit_env, new, 2)
    assert response.status_code == 200, response.text
    event = _event(edit_env).payload
    assert event['before'] == before and event['after'] == after
    assert event['from_revision'] == 2 and event['to_revision'] == 3


def test_same_markdown_and_verification_do_not_create_events(edit_env):
    assert _save(edit_env, BASE, 1).status_code == 200
    response = edit_env[0].post('/api/v2/library/notes/' + edit_env[3] + '/verify', json={
        'project_id': 'project-a', 'document_revision': 2})
    assert response.status_code == 200, response.text
    assert edit_env[1].list(COLLECTION) == ()
    assert edit_env[2].read(edit_env[3])['revision'] == 2


@pytest.mark.parametrize('kind', ['ordinary', 'worker', 'partial', 'bad_birth'])
def test_non_root_or_unknown_birth_keeps_original_legal_save(edit_env, kind):
    _, records, documents, root, _, _ = edit_env
    identity = root
    with records.begin() as tx:
        if kind == 'partial':
            row = tx.read('v2_turns', 'turn-product')
            payload = deepcopy(row.payload)
            payload['receipt']['do']['state'] = 'partial'
            tx.put(row.collection, row.object_id, payload, expected_revision=row.revision)
        elif kind == 'bad_birth':
            row = tx.read('v2_task_draft_operations', 'deliver-turn-root')
            payload = deepcopy(row.payload)
            payload['inputs']['markdown'] = '不是出生正文'
            tx.put(row.collection, row.object_id, payload, expected_revision=row.revision)
        tx.commit()
    if kind == 'ordinary':
        identity = documents.create(DocumentDraft(title='整理稿', document_type='notes', markdown='普通正文',
            source_refs=({'source_id': 'visible-item', 'locator': 'workspace://visible-item'},),
            project_id='project-a'))['id']
    elif kind == 'worker':
        identity = TaskDrafts(records, documents).create(turn_id='turn-worker', project='project-a',
            operation='worker-output', title='中间稿', markdown='中间正文')['document_id']
    if kind in {'ordinary', 'worker'}:
        # Original publication visibility allows editing; it is not root authority.
        with records.begin() as tx:
            tx.put('workspace_items', 'visible-item', {'project_id': 'project-a',
                'status': 'confirmed', 'document_id': identity}, expected_revision=0)
            tx.commit()
    response = _save(edit_env, '合法本地编辑', 1, identity=identity)
    assert response.status_code == 200, response.text
    assert documents.markdown(identity) == '合法本地编辑'
    assert records.list(COLLECTION) == ()


def test_private_project_edit_is_local_and_legal(edit_env):
    from backend.memory_app.v2.privacy import set_private_project
    set_private_project(edit_env[1], 'project-a', True, 0)
    response = _save(edit_env, '私密的本地改动\n\n保留的段落', 1)
    assert response.status_code == 200, response.text
    assert _event(edit_env).payload['after'] == '私密的本地改动'
    assert edit_env[5].calls == []


def test_stale_cas_keeps_original_conflict_and_zero_fact_writes(edit_env):
    assert _save(edit_env, '第一次改动', 1).status_code == 200
    before = edit_env[1].list_all()
    response = _save(edit_env, '陈旧覆盖', 1)
    assert response.status_code == 409, response.text
    assert edit_env[1].list_all() == before


def test_event_sql_failure_rolls_back_document_history_and_index(edit_env):
    records = edit_env[1]
    before = records.list_all()
    with sqlite3.connect(records.database_path) as connection:
        connection.execute("CREATE TRIGGER fail_correction BEFORE INSERT ON crp_structured_records "
            "WHEN NEW.collection='v2_outcome_corrections' BEGIN SELECT RAISE(ABORT, 'synthetic event failure'); END")
    response = _save(edit_env, '应该整笔回滚', 1)
    assert response.status_code == 500
    assert records.list_all() == before
    assert edit_env[2].markdown(edit_env[3]) == BASE
    assert records.read('document_retrieval_index', edit_env[3]).payload['document_revision'] == 1
    assert records.list(COLLECTION) == ()


def test_concurrent_http_saves_only_one_revision_and_event(edit_env):
    barrier = Barrier(2)
    def save(text):
        barrier.wait(timeout=5)
        return _save(edit_env, text, 1)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(save, text) for text in ('并发一', '并发二')]
        responses = [future.result(timeout=20) for future in futures]
    assert sorted(response.status_code for response in responses) == [200, 409]
    assert _event(edit_env).payload['to_revision'] == 2
    assert edit_env[2].read(edit_env[3])['revision'] == 2


def test_real_completed_do_root_http_edit_uses_public_turn_and_actual_birth(do_env):
    import json
    import time
    from backend.recognition import WorkScope
    from backend.recognition.product_draft_dependencies import product_draft_source

    client, models = do_env
    state = client.app.state
    records, documents = state.recognition_records, state.recognition_documents

    def respond(messages, **kwargs):
        context = json.loads(messages[-1]['content'])
        if 'output' in context:
            return json.dumps({'mode': 'cluster', 'assignments': [{
                'profile_id': 'subagent.worker', 'task': '准备成果', 'goal': '准备成果',
                'deliverable': '整理稿', 'capabilities': ['document.draft.propose'], 'depends_on': []}]})
        if any(cap['capability_id'] == 'agent.list' for cap in context.get('capabilities', [])):
            return json.dumps({'type': 'complete', 'summary': BASE})
        return json.dumps({'type': 'tool', 'capability_id': 'document.draft.propose',
            'arguments': {'title': '组成部分', 'markdown': '组成正文', 'final_for': '整理稿'}})

    models.handler = respond  # External provider only; runtime/owners remain real.
    state.recognition_turn_dispatcher._runtime()
    created = client.post('/api/v2/workbench/turns', json={
        'project_id': 'project-a', 'intent': 'do', 'text': '准备一份成果'})
    assert created.status_code == 200, created.text
    data = created.json()
    public_id = data['turn']['id']
    deadline = time.monotonic() + 90
    while True:
        response = client.get(f"/api/v2/workbench/threads/{data['thread_id']}?project_id=project-a")
        assert response.status_code == 200, response.text
        turn = next(row for row in response.json()['turns'] if row['id'] == public_id)
        receipt = turn['receipt']['do']
        if receipt['state'] in {'done', 'partial', 'failed'} or time.monotonic() >= deadline:
            break
        time.sleep(.1)
    assert receipt['state'] == 'done', receipt
    while state.workbench_tasks and time.monotonic() < deadline:
        time.sleep(.01)
    assert not state.workbench_tasks
    kernel_id, document_id = receipt['kernel_turn_id'], receipt['document_id']
    assert public_id != kernel_id
    execution = records.read('v2_task_executions', public_id)
    assert execution.payload['started'] is True and execution.payload['owner'] is None
    assert execution.payload['request']['turn_id'] == kernel_id
    assert state.ai_runtime.receipt_for(kernel_id).status == 'completed'
    operation = records.read('v2_task_draft_operations', 'deliver-' + kernel_id)
    assert operation.payload['result']['document_id'] == document_id
    assert operation.payload['result']['document_revision'] == 1
    bound = product_draft_source(records, WorkScope('local-user', 'project-a'), document_id, 1)
    assert bound.revisions['task_execution_id'] == public_id
    assert bound.revisions['product_turn_id'] == kernel_id
    assert documents.markdown(document_id, revision=1) == BASE
    runs = state.agent_runtime_composition.store.list_runs(project_id='project-a')
    assert len(runs) == 3 and all(state.ai_runtime.receipt_for(run.turn_id).status == 'completed' for run in runs)
    events = [event for run in runs for event in state.ai_turn_store.events_after(run.turn_id)]
    assert sum(event['type'] == 'model.attempt.dispatched' for event in events) == 3
    assert sum(event['type'] == 'tool.intent.recorded' and event['data'].get('capability_id') == 'document.draft.propose'
               for event in events) == 1
    assert records.list(COLLECTION) == ()
    calls = len(models.calls)
    assert calls == 3
    saved = client.patch('/api/recognition/documents/' + document_id, json={
        'project_id': 'project-a', 'expected_revision': 1, 'markdown': '最终做法\n\n保留的段落'})
    assert saved.status_code == 200, saved.text
    event, = records.list(COLLECTION)
    assert event.payload['turn_id'] == public_id and event.payload['document_id'] == document_id
    assert event.payload['birth_revision'] == 1 and event.payload['from_revision'] == 1
    assert event.payload['to_revision'] == 2
    assert event.payload['before'] == '原来的做法' and event.payload['after'] == '最终做法'
    assert documents.markdown(document_id, revision=1) == BASE
    assert len(models.calls) == calls
    assert records.read('v2_task_draft_operations', 'deliver-' + kernel_id) == operation
