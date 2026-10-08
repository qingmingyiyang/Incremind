from tests.memory_app.v2.test_workbench_do import env
from tests.memory_app.v2.test_divided_do import _real_kernel_drafts
from tests.memory_app.v2.test_workbench_do import env as do_env
from tests.memory_app.v2.test_outcome_redos import scenario, completed, redo, wait_product, NEW
from tests.memory_app.v2.test_outcome_redos import blocked_new_main, redo_events
from tests.memory_app.v2.test_outcome_redos import legacy_outcome_defaults
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
import time
from uuid import uuid4

import pytest

from backend.memory_app.v2 import _LazyOrganization
from backend.memory_app.v2.outcomes import select_outcome, qualified_lineages, hidden_outcome_ids
from backend.memory_app.v2.outcomes import validate_selection
from backend.memory_app.v2.task_do import TaskDo, TASK_EXECUTIONS
from backend.memory_app.v2.task_drafts import TaskDrafts
from backend.memory_app.workspace_contracts import _now
from backend.recognition import RecognitionConflict
from core.document_engine import DocumentDraft
from core.storage_provider import SQLiteStructuredRecordStore
from backend.memory_app.v2.outcomes import record_outcome
from copy import deepcopy


def test_real_coordinator_records_only_the_delivered_first_outcome(env):
    _real_kernel_drafts(env)
    client, model = env
    records = client.app.state.recognition_service.records
    turns = [row for row in records.list('v2_turns')
             if row.payload.get('project_id') == 'project-a'
             and row.payload.get('intent') == 'do'
             and row.payload.get('receipt', {}).get('do', {}).get('state') == 'done']
    assert len(turns) == 1
    turn = turns[0]
    document_id = turn.payload['receipt']['do']['document_id']
    lineage = records.read('v2_outcome_lineage', document_id)
    assert lineage is not None, 'a delivered root outcome must have an authoritative lineage'
    assert lineage.payload == {
        'project_id': 'project-a', 'scene': None, 'root_id': document_id,
        'previous_id': None, 'version': 1, 'turn_id': turn.object_id,
        'task_text': '分别准备三部分方案并汇总',
    }
    assert [row.object_id for row in records.list('v2_outcome_lineage')] == [document_id]


def test_real_first_version_redo_retains_root_and_shared_none_previous(scenario, legacy_outcome_defaults):
    values = scenario
    original, old = completed(values)
    root = old['receipt']['do']['document_id']
    values.summary = NEW
    response = redo(values, original)
    assert response.status_code == 200, response.text
    delivered = wait_product(values, response.json())
    assert delivered['receipt']['do']['state'] == 'done'
    document = delivered['receipt']['do']['document_id']
    lineage = values.records.read('v2_outcome_lineage', document)
    assert lineage.payload == {
        'project_id': 'project-a', 'scene': None, 'root_id': root,
        'previous_id': None, 'version': 2, 'turn_id': response.json()['turn']['id'],
        'task_text': '准备一份成果',
    }
    execution = values.records.read('v2_task_executions', response.json()['turn']['id'])
    frozen = execution.payload['outcome_selection']
    assert frozen['document_id'] == root and frozen['root_id'] == root
    assert frozen['mode'] == 'redo' and frozen['previous_id'] is None
    assert json.loads(execution.payload['request']['input']['text'])['outcome_selection'] == frozen
    versions = values.client.get('/api/v2/library/outcomes/' + root + '/versions',
                                 params={'project_id': 'project-a'})
    assert versions.status_code == 200, versions.text
    assert [(row['document_id'], row['version']) for row in versions.json()['items']] == [(root, 1), (document, 2)]


def _internal_choice(values, selected):
    organization = _LazyOrganization(values.client.app)
    organization.method_query = values.state.workspace_domains.query
    service = TaskDo(values.records, values.models, TaskDrafts(values.records, values.documents),
                     organization, organization.read, organization.topology)
    turn_id = 'turn-' + uuid4().hex
    receipt, state = service.initial(turn_id, 'project-a', '继续补充成果', selected['scene'], continuation=selected)
    source = values.records.read('v2_turns', selected['lineage']['turn_id'])
    now = _now()
    with values.records.begin() as tx:
        tx.put('v2_turns', turn_id, {'project_id': 'project-a', 'intent': 'do',
            'thread_id': source.payload['thread_id'], 'user_text': '继续补充成果',
            'created_at': now, 'updated_at': now, 'receipt': {'do': receipt}, 'item_id': None,
            'instance': values.state.workbench_instance, 'run_id': uuid4().hex}, expected_revision=0)
        tx.put(TASK_EXECUTIONS, turn_id, state, expected_revision=0)
        tx.commit()
    return service, turn_id


def _complete_internal(values, service, turn_id):
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        asyncio.run(service.advance(turn_id))
        receipt = values.records.read('v2_turns', turn_id).payload['receipt']['do']
        if receipt['state'] in {'done', 'partial', 'failed'}:
            assert receipt['state'] == 'done', receipt
            return receipt['document_id']
        time.sleep(.02)
    raise AssertionError('真实内部协调器没有在原90秒夹具窗口完成')


def _settled(values):
    deadline = time.monotonic() + 90
    while values.state.workbench_tasks and time.monotonic() < deadline:
        time.sleep(.02)
    assert not values.state.workbench_tasks


def test_real_two_frozen_choices_share_previous_and_have_serial_completed_versions(scenario, monkeypatch):
    values = scenario
    from core.ai_kernel.runtime import SynchronousAIRuntime
    import traceback
    failures = []
    original_fail = SynchronousAIRuntime._fail

    def observe_fail(owner, identity, error):
        # 仅观察原失败收口，继续委托真实内核，不改变协调器的结果或预算。
        failures.append({'turn_id': identity, 'type': type(error).__name__, 'message': str(error),
            'frames': [(frame.filename, frame.name, frame.lineno)
                       for frame in traceback.extract_tb(error.__traceback__)[-3:]]})
        return original_fail(owner, identity, error)

    monkeypatch.setattr(SynchronousAIRuntime, '_fail', observe_fail)
    try:
        _, first = completed(values, summary='# 并发成果\n\n' + values.summary)
    except AssertionError:
        print(json.dumps({'owner_failures': failures, 'transport_calls': len(values.models.calls)}, ensure_ascii=False))
        raise
    root = first['receipt']['do']['document_id']
    selected = select_outcome(values.records, project='project-a', scene=None, document_id=root)
    values.summary = NEW
    original_response = values.models.handler

    def patch_response(messages, **options):
        context = json.loads(messages[-1]['content'])
        if any(cap['capability_id'] == 'agent.list' for cap in context.get('capabilities', [])):
            # 只校准本节点的外模型协议；真实 Main 应用两份非空补丁，原 owner 和并发断言保持。
            return json.dumps({'type': 'complete', 'patches': [
                {'kind': 'update', 'path': ['并发成果'], 'body': values.summary}]}, ensure_ascii=False)
        return original_response(messages, **options)

    values.models.handler = patch_response
    calls = [_internal_choice(values, selected), _internal_choice(values, selected)]
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(_complete_internal, values, service, identity) for service, identity in calls]
        documents = [future.result(timeout=100) for future in futures]
    rows = [values.records.read('v2_outcome_lineage', identity) for identity in documents]
    assert len(set(documents)) == 2
    assert {row.payload['version'] for row in rows} == {2, 3}
    assert {row.payload['root_id'] for row in rows} == {root}
    assert {row.payload['previous_id'] for row in rows} == {root}
    assert len(qualified_lineages(values.records, 'project-a')) == 3
    assert hidden_outcome_ids(values.records, 'project-a') == {root, next(row.object_id for row in rows if row.payload['version'] == 2)}
    for service, identity in calls:
        frozen = values.records.read(TASK_EXECUTIONS, identity).payload
        assert frozen['outcome_selection'] == selected
        assert json.loads(frozen['request']['input']['text'])['outcome_selection'] == selected
        assert values.state.ai_runtime.receipt_for(frozen['request']['turn_id']).status == 'completed'
        assert frozen['request']['turn_id'] != identity


def test_real_latest_outcome_is_recalled_and_old_versions_are_excluded(scenario, legacy_outcome_defaults):
    values = scenario
    original, first = completed(values)
    root = first['receipt']['do']['document_id']
    root_document = values.documents.read(root)
    root_body = values.documents.markdown(root, revision=1)
    values.summary = NEW
    response = redo(values, original)
    assert response.status_code == 200, response.text
    delivered = wait_product(values, response.json())['receipt']['do']
    assert delivered['state'] == 'done', delivered
    latest = delivered['document_id']
    assert latest != root
    assert hidden_outcome_ids(values.records, 'project-a') == {root}
    # 直接使用原召回管线，最新版仍须通过原有范围、来源和资格过滤。
    collected = values.state.workspace_domains.query._collect_candidates('project-a', '准备一份成果')
    identities = {row['id'] for row in collected['candidates']}
    assert latest in identities
    assert root not in identities
    notes = values.client.get('/api/v2/library/notes', params={'project_id': 'project-a'})
    assert notes.status_code == 200, notes.text
    note_ids = {row['document_id'] for row in notes.json()['items']}
    assert latest in note_ids and root not in note_ids
    assert values.documents.read(root) == root_document
    assert values.documents.markdown(root, revision=1) == root_body


def test_real_latest_archive_keeps_old_hidden_but_direct_drill_and_versions_remain(scenario, legacy_outcome_defaults):
    values = scenario
    old, first = completed(values)
    root = first['receipt']['do']['document_id']
    values.summary = NEW
    response = redo(values, old)
    assert response.status_code == 200, response.text
    latest = wait_product(values, response.json())['receipt']['do']['document_id']
    notes = values.client.get('/api/v2/library/notes', params={'project_id': 'project-a'})
    assert notes.status_code == 200
    assert root not in {row['document_id'] for row in notes.json()['items']}
    assert latest in {row['document_id'] for row in notes.json()['items']}
    drill = values.client.get('/api/v2/library/drill', params={'project_id': 'project-a', 'from': 'note', 'id': root})
    assert drill.status_code == 200, drill.text
    assert drill.json()['note']['markdown'] == values.documents.markdown(root, revision=1)
    values.documents.archive(latest, expected_revision=1)
    assert latest in qualified_lineages(values.records, 'project-a')
    assert hidden_outcome_ids(values.records, 'project-a') == {root}
    summaries = values.client.get('/api/v2/library/summaries', params={'project_id': 'project-a'})
    assert summaries.status_code == 200
    assert not {root, latest} & {row['document_id'] for row in summaries.json()['items']}
    candidates = values.state.workspace_domains.query._collect_candidates('project-a', '准备一份成果')
    assert not {root, latest} & {row['id'] for row in candidates['candidates']}
    versions = values.client.get('/api/v2/library/outcomes/' + root + '/versions', params={'project_id': 'project-a'})
    assert versions.status_code == 200
    assert [row['version'] for row in versions.json()['items']] == [1, 2]
    assert values.client.get('/api/v2/library/outcomes/' + root + '/versions', params={'project_id': 'other-project'}).status_code == 404


def test_real_source_revision_drift_cannot_publish_done_or_leave_owned_pending(scenario, legacy_outcome_defaults):
    values = scenario
    old, first = completed(values)
    root = first['receipt']['do']['document_id']
    with blocked_new_main(values):
        response = redo(values, old)
        assert response.status_code == 200, response.text
        assert values.entered.wait(timeout=15)
        selected = values.records.read(TASK_EXECUTIONS, response.json()['turn']['id']).payload['outcome_selection']
        assert selected['document_revision'] == 1
        values.documents.save_user_edit(root, markdown='真实用户修改', expected_revision=1)
    _settled(values)
    turn = values.records.read('v2_turns', response.json()['turn']['id'])
    state = values.records.read(TASK_EXECUTIONS, turn.object_id)
    assert turn.payload['receipt']['do']['state'] == 'failed'
    assert turn.payload['receipt']['do']['document_id'] is None
    assert state.payload['owner'] is None and state.payload['failures'] == 3
    assert [row.object_id for row in values.records.list('v2_outcome_lineage')] == [root]
    pending, = redo_events(values)
    assert 'completed_at' not in pending.payload
    assert len(values.state.agent_runtime_composition.store.list_runs(project_id='project-a')) == 6


def test_real_lineage_insert_failure_rolls_back_public_completion_and_redo_after(scenario, legacy_outcome_defaults):
    values = scenario
    old, first = completed(values)
    root = first['receipt']['do']['document_id']
    with blocked_new_main(values):
        response = redo(values, old)
        assert response.status_code == 200, response.text
        pending, = redo_events(values)
        assert values.entered.wait(timeout=15)
        with sqlite3.connect(values.records.database_path) as connection:
            connection.execute("CREATE TRIGGER reject_lineage BEFORE INSERT ON crp_structured_records "
                "WHEN NEW.collection='v2_outcome_lineage' BEGIN SELECT RAISE(ABORT, 'synthetic lineage failure'); END")
    _settled(values)
    turn = values.records.read('v2_turns', response.json()['turn']['id'])
    state = values.records.read(TASK_EXECUTIONS, turn.object_id)
    assert values.state.ai_runtime.receipt_for(state.payload['request']['turn_id']).status == 'completed'
    assert turn.payload['receipt']['do']['state'] == 'running'
    assert turn.payload['receipt']['do']['document_id'] is None
    assert values.records.read('v2_outcome_corrections', pending.object_id) == pending
    assert [row.object_id for row in values.records.list('v2_outcome_lineage')] == [root]


def test_historical_root_and_ordinary_document_remain_visible_without_trusted_lineage(scenario):
    values = scenario
    _, first = completed(values)
    root = first['receipt']['do']['document_id']
    original = values.documents.read(root)
    ordinary = values.documents.create(DocumentDraft(title='普通整理稿', document_type='notes',
        markdown='准备一份成果的普通正文', project_id='project-a',
        source_refs=tuple(original['source_refs'])))['id']
    with values.records.begin() as tx:
        row = tx.read('v2_outcome_lineage', root)
        tx.delete(row.collection, row.object_id, expected_revision=row.revision)
        tx.put('v2_outcome_lineage', ordinary, {'project_id': 'project-a', 'scene': None,
            'root_id': root, 'previous_id': root, 'version': 999, 'turn_id': first['id'],
            'task_text': '未经出生核实的旁路记录'}, expected_revision=0)
        tx.commit()
    assert qualified_lineages(values.records, 'project-a') == {}
    rows = values.client.get('/api/v2/library/notes', params={'project_id': 'project-a'})
    assert rows.status_code == 200
    assert {root, ordinary} <= {row['document_id'] for row in rows.json()['items']}
    assert values.documents.read(root) == original
    assert values.client.get('/api/v2/library/outcomes/' + root + '/versions', params={'project_id': 'project-a'}).status_code == 404
    collected = values.state.workspace_domains.query._collect_candidates('project-a', '准备一份成果 普通正文')
    assert {root, ordinary} <= {row['id'] for row in collected['candidates']}


def test_real_selection_rechecks_scope_owner_and_current_revision_before_new_wire(scenario):
    values = scenario
    _, first = completed(values)
    root = first['receipt']['do']['document_id']
    selected = select_outcome(values.records, project='project-a', scene=None, document_id=root)
    calls = len(values.models.calls)
    with pytest.raises(RecognitionConflict):
        select_outcome(values.records, project='other-project', scene=None, document_id=root)
    with pytest.raises(RecognitionConflict):
        select_outcome(values.records, project='project-a', scene='其他场景', document_id=root, mode='redo')
    tampered = deepcopy(selected)
    tampered['owner']['document_revision'] = 99
    with pytest.raises(RecognitionConflict):
        validate_selection(values.records, project='project-a', scene=None, selection=tampered)
    values.documents.save_user_edit(root, markdown='真实用户后续编辑', expected_revision=1)
    with pytest.raises(RecognitionConflict):
        _internal_choice(values, selected)
    fresh = select_outcome(values.records, project='project-a', scene=None, document_id=root)
    assert fresh['document_revision'] == 2 and fresh['owner']['document_revision'] == 1
    assert len(values.models.calls) == calls
    assert len(values.records.list(TASK_EXECUTIONS)) == 1


def test_unknown_birth_with_existing_lineage_cannot_use_legacy_completion(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / 'unknown-owner.sqlite3')
    payload = {'project_id': 'project-a', 'scene': None, 'root_id': 'unknown-document',
        'previous_id': None, 'version': 1, 'turn_id': 'unknown-turn', 'task_text': '未知历史事实'}
    with records.begin() as tx:
        row = tx.put('v2_outcome_lineage', 'unknown-document', payload, expected_revision=0)
        tx.commit()
    with records.begin() as tx:
        with pytest.raises(RecognitionConflict):
            record_outcome(tx, project='project-a', document_id='unknown-document',
                           turn_id='unknown-turn', scene=None, task_text='未知历史事实')
        tx.rollback()
    assert records.read('v2_outcome_lineage', 'unknown-document') == row


def test_real_latest_source_reference_edit_retains_birth_versions_and_no_old_fallback(scenario, legacy_outcome_defaults):
    values = scenario
    old, first = completed(values)
    root = first['receipt']['do']['document_id']
    original_document = values.documents.read(root)
    original_body = values.documents.markdown(root)
    values.summary = NEW
    response = redo(values, old)
    assert response.status_code == 200, response.text
    latest = wait_product(values, response.json())['receipt']['do']['document_id']
    item = asyncio.run(values.state.workspace_domains.intake.add_text({
        'project_id': 'project-a', 'text': '用户真实补充的来源'}))
    values.documents.save_user_edit(latest, markdown=NEW + '\n\n用户添加的新来源说明',
        source_refs=({'source_id': item['id'], 'locator': 'workspace://' + item['id']},), expected_revision=1)
    assert values.documents.read(latest)['revision'] == 2
    assert values.documents.revision(latest, 1)['source_snapshot']['source_refs'][0]['locator'].startswith('task://')
    rows = values.client.get('/api/v2/library/notes', params={'project_id': 'project-a'})
    assert rows.status_code == 200
    assert root not in {row['document_id'] for row in rows.json()['items']}
    versions = values.client.get('/api/v2/library/outcomes/' + root + '/versions', params={'project_id': 'project-a'})
    assert versions.status_code == 200
    assert [(row['document_id'], row['version']) for row in versions.json()['items']] == [(root, 1), (latest, 2)]
    assert hidden_outcome_ids(values.records, 'project-a') == {root}
    collected = values.state.workspace_domains.query._collect_candidates('project-a', '准备一份成果')
    assert root not in {row['id'] for row in collected['candidates']}
    assert values.documents.read(root) == original_document
    assert values.documents.markdown(root, revision=1) == original_body
    assert values.documents.markdown(latest, revision=1) == NEW
