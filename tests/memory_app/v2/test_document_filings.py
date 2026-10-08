"""Explicit moves reuse real admission, document writes and retained origins."""
import pytest

from backend.recognition import RecognitionConflict, RecognitionError
from backend.memory_app.v2.privacy import set_private_project
from tests.memory_app.v2.test_insight_generation import env


def setup_projects(env):
    with env.records.begin() as tx:
        for project in ('alpha', 'beta', 'gamma'):
            tx.put('v2_projects', project, {'name': project, 'scenes': ['阅读'],
                'private': False, 'builtin': None}, expected_revision=0)
        tx.commit()


def filings(env):
    from backend.memory_app.v2.document_filings import DocumentFilings
    return DocumentFilings(env.records, env.documents, env.service, env.model)


def move(env):
    return filings(env).move(env.doc, 'alpha', 'beta', scene='阅读', expected_revision=1)


def test_move_uses_same_markdown_refs_and_originals_and_only_one_aux_call(env, monkeypatch):
    setup_projects(env)
    from backend.memory_app import workspace_links, workspace_audio
    gateway_calls = []
    def forbidden(*args, **kwargs):
        gateway_calls.append((args, kwargs))
        raise AssertionError('a filing must not fetch or transcribe a source')
    monkeypatch.setattr(workspace_links, '_fetch_url', forbidden)
    monkeypatch.setattr(workspace_audio, '_transcribe_output', forbidden)
    from backend.memory_app.v2.insight_generation import generate_insights
    generated = generate_insights(env.model, env.service, env.documents, 'alpha', env.doc)
    active = env.service.publish(scope=env.scope, candidate_id=generated[0]['id'],
        expected_revision=generated[0]['revision'], reviewer='local-user')
    original_document = env.documents.read(env.doc)
    original_markdown = env.documents.markdown(env.doc)
    originals = env.records.list('workspace_items')
    original_recognition = env.records.read('recognitions', active.id)
    before_calls = env.model.calls
    result = move(env)
    target = env.documents.read(result['target_document_id'])
    assert target['project_id'] == 'beta' and target['source_refs'] == original_document['source_refs']
    assert env.documents.markdown(target['id']) == original_markdown
    assert env.documents.read(env.doc) == original_document
    assert env.records.list('workspace_items') == originals
    assert env.records.read('recognitions', active.id) == original_recognition
    assert env.records.read('recognition_candidates', generated[1]['id']).payload['state'] == 'rejected'
    assert env.model.calls == before_calls + 1
    assert gateway_calls == []
    assert env.records.read('v2_document_recall', env.doc).payload == {
        'state': 'forgotten', 'by': 'moved', 'moved_to': target['id']}
    assert env.records.list('v2_place_corrections') and env.records.list('v2_place_examples')
    from backend.memory_app.workspace_query import WorkspaceQuery
    # Use the same real query service installed with this fixture's stores.
    from backend.memory_app.workspace import install_workspace_routes
    from fastapi import FastAPI
    domains = install_workspace_routes(FastAPI(), runtime_root=env.records.database_path.parent,
        records=env.records, documents=env.documents, service=env.service, models=env.model)
    assert target['id'] in [row['entry']['id'] for row in domains.query.prepare_ask('beta', '摘要')['chosen']
        if row['kind'] == 'document']
    assert env.doc not in [row['entry']['id'] for row in domains.query.prepare_ask('alpha', '摘要')['chosen']
        if row['kind'] == 'document']
    before = env.records.list_all()
    assert move(env)['target_document_id'] == target['id']
    assert env.records.list_all() == before and env.model.calls == before_calls + 1


@pytest.mark.parametrize('prior', [None, {'state': 'cooling', 'by': 'user'}])
def test_undo_restores_original_recall_and_retains_forgotten_copy(env, prior):
    setup_projects(env)
    if prior:
        with env.records.begin() as tx:
            tx.put('v2_document_recall', env.doc, prior, expected_revision=0)
            tx.commit()
    result = move(env)
    before_calls = env.model.calls
    undone = filings(env).undo(result['target_document_id'], 'beta', expected_revision=1)
    row = env.records.read('v2_document_recall', env.doc)
    assert (dict(row.payload) if row else None) == prior
    assert env.records.read('v2_document_recall', result['target_document_id']).payload == {
        'state': 'forgotten', 'by': 'moved', 'moved_to': env.doc}
    assert env.documents.read(result['target_document_id']) is not None
    assert undone['state'] == 'undone' and env.model.calls == before_calls


def test_private_source_project_cannot_move_and_writes_nothing(env):
    setup_projects(env)
    set_private_project(env.records, 'alpha', True, 0)
    before = env.records.list_all()
    with pytest.raises(RecognitionError):
        move(env)
    assert env.records.list_all() == before and env.model.calls == 0


def test_stale_move_has_no_write_or_aux_call(env):
    setup_projects(env)
    before = env.records.list_all()
    with pytest.raises(RecognitionConflict):
        filings(env).move(env.doc, 'alpha', 'beta', scene='阅读', expected_revision=2)
    assert env.records.list_all() == before and env.model.calls == 0


def test_move_marker_failure_rolls_back_copy_origin_recall_and_rejections(env, monkeypatch):
    setup_projects(env)
    from core.storage_provider.sqlite_uow import SQLiteStructuredRecordUnitOfWork
    original = SQLiteStructuredRecordUnitOfWork.put
    def unavailable(tx, collection, identity, payload, *, expected_revision):
        if collection == 'v2_document_filings':
            raise OSError('synthetic marker write failure')
        return original(tx, collection, identity, payload, expected_revision=expected_revision)
    monkeypatch.setattr(SQLiteStructuredRecordUnitOfWork, 'put', unavailable)
    before = env.records.list_all()
    with pytest.raises(OSError):
        move(env)
    assert env.records.list_all() == before and env.model.calls == 0


def test_undo_requires_latest_move_and_does_not_overwrite_another_filing(env):
    setup_projects(env)
    first = move(env)
    second = filings(env).move(first['target_document_id'], 'beta', 'gamma', expected_revision=1)
    before = env.records.list_all()
    with pytest.raises(RecognitionConflict):
        filings(env).undo(first['target_document_id'], 'beta', expected_revision=1)
    assert env.records.list_all() == before
    filings(env).undo(second['target_document_id'], 'gamma', expected_revision=1)
    filings(env).undo(first['target_document_id'], 'beta', expected_revision=1)
    assert env.records.read('v2_document_recall', env.doc) is None


def test_a_new_move_after_undo_has_its_own_correction_and_replay_is_idempotent(env):
    setup_projects(env)
    first = move(env)
    filings(env).undo(first['target_document_id'], 'beta', expected_revision=1)
    second = move(env)
    assert second['target_document_id'] != first['target_document_id']
    events = env.records.list('v2_place_corrections')
    assert len(events) == len(env.records.list('v2_place_examples')) == 3
    assert [row.payload['action'] for row in events].count('move') == 2
    assert [row.payload['action'] for row in events].count('undo') == 1
    before = env.records.list_all()
    assert move(env)['target_document_id'] == second['target_document_id']
    assert env.records.list_all() == before


def test_concurrent_move_replay_cannot_finalize_aux_while_first_wire_is_running(env):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    setup_projects(env)
    entered, release = threading.Event(), threading.Event()
    def blocked():
        entered.set()
        assert release.wait(20)
    env.model.after = blocked
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(move, env)
        assert entered.wait(20)
        try:
            replay = move(env)
            aux = env.records.read('v2_document_filing_aux', replay['target_document_id'])
            assert aux.payload['state'] == 'pending'
            assert aux.payload['error'] is None
        finally:
            release.set()
        result = first.result(timeout=20)
    assert env.model.calls == 1 and len(result['insights']) == 2 and result['error'] is None
    assert move(env)['insights'] == result['insights']
    aux = env.records.read('v2_document_filing_aux', result['target_document_id'])
    assert aux.payload['insights'] == result['insights'] and aux.payload['error'] is None
