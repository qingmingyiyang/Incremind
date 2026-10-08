from tests.memory_app.v2.test_workbench_ask import env, add_document
from tests.memory_app.v2.test_workbench_remember import env as remember_env, post, wait
from tests.memory_app.v2.test_workbench_do import env as do_env


def test_document_read_set_batches_bodies_and_leaves_live_store_fresh(env, monkeypatch):
    from backend.memory_app.v2.request_reads import DocumentReadSet
    identity, _ = add_document(env)
    add_document(env, project='other')
    calls = []
    original = env.records.read_batch
    def read_batch(selections):
        calls.append(selections)
        return original(selections)
    monkeypatch.setattr(env.records, 'read_batch', read_batch)
    prepared = DocumentReadSet.load(env.records, env.documents, 'alpha')
    assert set(prepared.documents) == {identity}
    assert len(calls) == 1
    assert len(calls[0]['document_markdown']) == 1
    before = prepared.markdown[identity]
    env.documents.save_user_edit(identity, expected_revision=2, markdown='changed')
    assert prepared.markdown[identity] == before
    assert env.documents.markdown(identity) == 'changed'


def test_query_assembles_each_body_from_one_prepared_read_set(env, monkeypatch):
    from backend.memory_app.v2.request_reads import DocumentReadSet
    identity, _ = add_document(env)
    prepared = DocumentReadSet.load(env.records, env.documents, 'alpha')
    entries = env.domains.query.query_entries('alpha', prepared=prepared)
    assert any(entry['document_id'] == identity for entry in entries if entry['kind'] == 'document')
    # The supplied set is content only: private state is read live at the next
    # candidate pass, rather than inherited from a prepared authorization.
    from backend.memory_app.v2.privacy import set_private_project
    set_private_project(env.records, 'alpha', True, 0)
    assert env.domains.query.collect_candidates('alpha', 'alpha')['candidates'] == []


def test_library_summaries_build_source_types_once(env, monkeypatch):
    from backend.memory_app.v2.library import LibraryRead
    add_document(env)
    add_document(env)
    original, calls = LibraryRead.source_rows, []
    def source_rows(self, project, documents, **kwargs):
        calls.append(kwargs.get('prepared'))
        return original(self, project, documents, **kwargs)
    monkeypatch.setattr(LibraryRead, 'source_rows', source_rows)
    result = env.http.get('/api/v2/library/summaries', params={'project_id': 'alpha'})
    assert result.status_code == 200, result.text
    assert len(result.json()['items']) == 2
    assert len(calls) == 1 and calls[0] is not None
    assert {row['source_type'] for row in result.json()['items']} == {'text'}


def test_prepared_task_candidates_still_check_current_revision(tmp_path):
    import pytest
    from core.storage_provider import SQLiteStructuredRecordStore
    from backend.memory_app.v2.task_divisions import TaskDivisions
    from backend.recognition import RecognitionConflict
    records = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    divisions = TaskDivisions(records)
    item = {'goal':'整理', 'deliverable':'方案', 'capabilities':['memory.recall'], 'depends_on':[]}
    divisions.complete('old', project='alpha', text='整理研究方案', items=[item], outcome='done')
    prepared = records.read_batch({'v2_task_divisions': None})['v2_task_divisions']
    divisions.adjust('old', project='alpha', items=[{**item, 'goal':'changed'}], expected_revision=1)
    with pytest.raises(RecognitionConflict, match='task_division_reference_changed'):
        divisions.similar('alpha', '整理研究方案', rows=prepared)


def test_remember_prepares_project_thread_and_item_together(remember_env, monkeypatch):
    import asyncio
    with remember_env.records.begin() as tx:
        tx.put('v2_projects', 'alpha', {'name':'Alpha'}, expected_revision=0)
        tx.commit()
    first = post(remember_env)
    assert wait(remember_env, first)['receipt']['remember']['state'] == 'done'
    item = asyncio.run(remember_env.domains.intake.add_text({'project_id':'alpha', 'text':'second'}))
    calls, original = [], remember_env.records.read_batch
    def read_batch(selections):
        calls.append(selections)
        return original(selections)
    monkeypatch.setattr(remember_env.records, 'read_batch', read_batch)
    second = post(remember_env, text='#alpha second', intent='remember',
                  thread_id=first['thread_id'], item_id=item['id'])
    assert wait(remember_env, second)['receipt']['remember']['state'] == 'done'
    assert {'v2_projects':None, 'v2_threads':(first['thread_id'],),
            'workspace_items':(item['id'],)} in calls


def test_task_prepares_only_selected_example_turn_references(do_env, monkeypatch):
    from backend.memory_app.v2.task_do import TaskDo
    client, model = do_env
    records = client.app.state.recognition_service.records
    task = TaskDo(records, model, None, None, None, None)
    division = {'goal':'整理', 'deliverable':'方案', 'capabilities':['memory.recall'], 'depends_on':[]}
    for identity in ('sample-a', 'sample-b'):
        task.divisions.complete(identity, project='alpha', text='整理研究方案', items=[division], outcome='done')
        with records.begin() as tx:
            tx.put('v2_turns', identity, {'thread_id':'thread-'+identity}, expected_revision=0)
            tx.commit()
    calls, original = [], records.read_batch
    def read_batch(selections):
        calls.append(selections)
        return original(selections)
    monkeypatch.setattr(records, 'read_batch', read_batch)
    receipt, state = task.initial('new-turn', 'alpha', '整理研究方案', None)
    assert len(receipt['division_examples']) == 2
    assert len([call for call in calls if 'v2_task_divisions' in call]) == 1
    turns = [call['v2_turns'] for call in calls if 'v2_turns' in call]
    assert len(turns) == 1 and set(turns[0]) == {'sample-a', 'sample-b'}
    assert state['request']['scope']['project_id'] == 'alpha'
