"""A retained document copy uses its real origin, never a fabricated intake."""
from uuid import uuid4

import pytest

from backend.memory_app.document_recognition import ensure_document_experience
from backend.memory_app.source_egress import SourceEgressService
from backend.memory_app.transaction_records import TransactionRecords
from backend.memory_app.v2.privacy import freeze_turn_materials
from backend.recognition import RecognitionService, WorkScope
from core.document_engine import DocumentDraft
from tests.memory_app.v2.test_auto_confirm import runtime, item, confirm


@pytest.fixture
def filed(runtime):
    runtime.model.public = lambda: {'generation': {'base_url': 'http://localhost/v1', 'allow_remote': False}}
    source = confirm(runtime, item(runtime)['id'])
    document = runtime.documents.read(source['document_id'])
    markdown = runtime.documents.markdown(document['id'])
    experience, revision = ensure_document_experience(
        runtime.documents, runtime.domains.query.service, 'alpha', document['id'])
    before = runtime.records.read('workspace_items', source['id'])
    with runtime.records.begin() as tx:
        target = runtime.documents.create_or_replay_generated_in_uow(DocumentDraft(
            title=document['title'], document_type='filed-v2-' + uuid4().hex,
            markdown=markdown, source_refs=tuple(document['source_refs']), project_id='beta'), tx)
        service = RecognitionService(TransactionRecords(tx))
        copied = service.stage_experience(scope=WorkScope('local-user', 'beta'), content=markdown,
            copy_from={'project_id': 'alpha', 'experience_id': experience, 'revision': 1})
        tx.put('v2_document_filings', target['id'], {
            'user_id': 'local-user', 'source_project_id': 'alpha',
            'source_document_id': document['id'], 'source_document_revision': revision,
            'target_project_id': 'beta', 'target_document_id': target['id'],
            'target_document_revision': target['revision'], 'target_experience_id': copied,
            'target_experience_revision': 1, 'prior_recall': None,
            'state': 'filed', 'scene': None}, expected_revision=0)
        tx.commit()
    assert runtime.records.read('workspace_items', source['id']) == before
    assert len(runtime.records.list('workspace_items')) == 1
    assert target['source_refs'] == document['source_refs']
    # The copy itself already has valid retained-source authority. The red
    # tests below isolate the document consumers which currently miss it.
    authority = SourceEgressService(runtime.records)
    authority.require(authority.snapshot(WorkScope('local-user', 'beta'), [
        {'type': 'experience', 'id': copied, 'revision': 1}]), 'generation')
    return runtime, document, target, copied


def test_target_extraction_reuses_exact_copied_experience(filed):
    runtime, source, target, copied = filed
    before = runtime.records.list('recognition_experiences')
    result = ensure_document_experience(runtime.documents, runtime.domains.query.service, 'beta', target['id'])
    assert result == (copied, target['revision'])
    assert runtime.records.list('recognition_experiences') == before


def test_target_turn_freeze_uses_existing_origin_authority(filed):
    runtime, source, target, copied = filed
    material = {'type': 'document', 'id': target['id'], 'revision': 1, 'project_id': 'beta'}
    allowed, privacy = freeze_turn_materials(runtime.records, runtime.model, 'beta', [material],
        authority=SourceEgressService(runtime.records))
    assert allowed[0]['payload']['source_refs'] == source['source_refs']
    assert privacy['source_snapshots'][0]['roots'] == [
        {'type': 'experience', 'id': copied, 'revision': 1}]


def test_target_question_can_recall_copy_without_importing_original(filed):
    runtime, source, target, copied = filed
    query = runtime.domains.query
    plan = query.prepare_ask('beta', '事实')
    chosen = [row for row in plan['chosen'] if row['kind'] == 'document']
    assert target['id'] in [row['entry']['id'] for row in chosen]
    assert source['id'] not in [entry['id'] for entry in query.query_entries('beta')]
    assert not any(entry['kind'] == 'source' for entry in query.query_entries('beta'))
    query.validate_ask_plan(plan)


def test_real_filed_user_edit_inherits_source_and_strict_frozen_graph(filed):
    from backend.memory_app.source_graph import SourceGraph, validate_graph
    from backend.memory_app.source_snapshot import _closure_identity
    from backend.memory_app.source_egress import _frozen_packet_authority
    runtime, source, target, copied = filed
    runtime.documents.save_user_edit(target['id'], markdown='用户补充的事实', expected_revision=1)
    experience, revision = ensure_document_experience(runtime.documents, runtime.domains.query.service, 'beta', target['id'])
    scope = WorkScope('local-user', 'beta')
    refs = [{'type': 'experience', 'id': experience, 'revision': 1}]
    assert revision == 2 and experience != copied
    assert runtime.records.read('recognition_experiences', experience).payload['provenance']['kind'] == 'user_statement'
    authority = SourceEgressService(runtime.records)
    snapshot = authority.snapshot(scope, refs)
    authority.require(snapshot, 'generation')
    assert snapshot['nodes'][0]['dependency_revisions']['copied_experience_id'] == copied
    parsed = _frozen_packet_authority(scope, {'source_egress': snapshot}, [('experience', experience, 1)])
    assert {_closure_identity(node) for node in parsed['nodes']} == {_closure_identity(node) for node in snapshot['nodes']}
    graph = SourceGraph()
    graph.snapshot(snapshot)
    validate_graph(graph.result(), 'local-user')
    service = runtime.domains.query.service
    candidate = service.propose(scope=scope, content='修改后的认识', source_experience_ids=[experience])
    assert candidate.state == 'pending'
    original_item = runtime.records.list_matching('workspace_items', project_id='alpha')[0]
    authority.set_policy(WorkScope('local-user', 'alpha'), 'original_item', original_item.object_id,
        original_item.revision, 0, [])
    import pytest
    from backend.recognition import RecognitionError
    with pytest.raises(RecognitionError):
        authority.require(authority.snapshot(scope, refs), 'generation')
    assert service.read_candidate_experiences(scope=scope, experience_ids=[experience])[0].content == '用户补充的事实'


@pytest.mark.parametrize('corruption', ['history_missing', 'parent', 'author', 'body', 'refs', 'scope'])
def test_filed_user_edit_history_corruption_is_not_authority(filed, corruption):
    from backend.memory_app.document_recognition import DocumentRecognitionError
    from backend.recognition import RecognitionConflict
    runtime, source, target, copied = filed
    runtime.documents.save_user_edit(target['id'], markdown='用户补充的事实', expected_revision=1)
    key = target['id'] + '~r2'
    with runtime.records.begin() as tx:
        collection = 'document_markdown' if corruption == 'body' else 'documents' if corruption == 'scope' else 'document_revisions'
        identity = target['id'] if corruption == 'scope' else key
        row = tx.read(collection, identity)
        if corruption == 'history_missing':
            tx.delete(collection, identity, expected_revision=row.revision)
        else:
            payload = dict(row.payload)
            if corruption == 'parent': payload['parent_revision'] = True
            if corruption == 'author': payload['author'] = 'system'
            if corruption == 'body': payload['markdown'] = '裸篡改'
            if corruption == 'refs': payload['source_snapshot'] = {**payload['source_snapshot'], 'source_refs': []}
            if corruption == 'scope': payload['project_id'] = 'alpha'
            tx.put(collection, identity, payload, expected_revision=row.revision)
        tx.commit()
    before = runtime.records.list_all()
    with pytest.raises(DocumentRecognitionError):
        ensure_document_experience(runtime.documents, runtime.domains.query.service, 'beta', target['id'])
    assert runtime.records.list_all() == before
    with pytest.raises(RecognitionConflict):
        freeze_turn_materials(runtime.records, runtime.model, 'beta', [{
            'type': 'document', 'id': target['id'], 'revision': runtime.records.read('documents', target['id']).revision,
            'project_id': 'beta'}], authority=SourceEgressService(runtime.records))


def test_filing_metadata_discovery_does_not_load_any_document_or_experience_body(filed, monkeypatch):
    from backend.shared.document_visibility import LegacyDocumentVisibility
    from core.storage_provider.sqlite_uow import SQLiteStructuredRecordUnitOfWork
    runtime, source, target, copied = filed
    reads = []
    original = SQLiteStructuredRecordUnitOfWork.read

    def observed(tx, collection, identity):
        row = original(tx, collection, identity)
        if collection in {'document_markdown', 'document_revisions', 'recognition_experiences'}:
            reads.append((collection, identity))
        return row

    monkeypatch.setattr(SQLiteStructuredRecordUnitOfWork, 'read', observed)
    metadata = LegacyDocumentVisibility.from_repository(runtime.documents, project_id='beta', metadata_only=True)
    assert metadata.allows(target)
    assert reads == []


def test_valid_filing_does_not_bypass_existing_pending_source_visibility(filed):
    from backend.shared.document_visibility import LegacyDocumentVisibility
    runtime, source, target, copied = filed
    with runtime.records.begin() as tx:
        tx.put('workspace_review_intents', 'review-pending-filing', {
            'source_id': target['source_refs'][0]['source_id'], 'state': 'prepared'}, expected_revision=0)
        tx.commit()
    assert not LegacyDocumentVisibility.from_repository(runtime.documents, project_id='beta').allows(target)


@pytest.mark.parametrize('corruption', ['missing', 'target_scope', 'target_id', 'bool_revision',
    'extra_field', 'target_body', 'target_refs', 'source_history', 'origin_missing'])
def test_invalid_filing_is_not_a_publication_or_model_authority_and_writes_nothing(filed, corruption):
    from backend.memory_app.document_recognition import DocumentRecognitionError
    from backend.memory_app.v2.insight_generation import generate_insights
    from backend.shared.document_visibility import LegacyDocumentVisibility, recognition_document_visible
    from backend.recognition import RecognitionConflict
    runtime, source, target, copied = filed
    with runtime.records.begin() as tx:
        marker = tx.read('v2_document_filings', target['id'])
        if corruption == 'missing':
            tx.delete(marker.collection, marker.object_id, expected_revision=marker.revision)
        elif corruption in {'target_scope', 'target_id', 'bool_revision', 'extra_field'}:
            changed = dict(marker.payload)
            if corruption == 'target_scope':
                changed['target_project_id'] = 'gamma'
            elif corruption == 'target_id':
                changed['target_document_id'] = source['id']
            elif corruption == 'bool_revision':
                changed['target_document_revision'] = True
            else:
                changed['untrusted'] = 'unused'
            tx.put(marker.collection, marker.object_id, changed, expected_revision=marker.revision)
        elif corruption == 'origin_missing':
            origin = tx.read('v2_experience_origins', copied)
            tx.delete(origin.collection, origin.object_id, expected_revision=origin.revision)
        else:
            identity = target['id'] if corruption != 'source_history' else source['id']
            collection = 'document_markdown' if corruption != 'target_refs' else 'document_revisions'
            row = tx.read(collection, identity + '~r1')
            changed = dict(row.payload)
            if corruption == 'target_refs':
                changed['source_snapshot'] = {**changed['source_snapshot'], 'source_refs': [
                    {'source_id': 'other', 'locator': 'workspace://other'}]}
            else:
                changed['markdown'] += '\nforged body'
            tx.put(collection, row.object_id, changed, expected_revision=row.revision)
        tx.commit()
    baseline, calls = runtime.records.list_all(), runtime.model.calls
    with pytest.raises(DocumentRecognitionError):
        generate_insights(runtime.model, runtime.domains.query.service, runtime.documents, 'beta', target['id'])
    assert runtime.records.list_all() == baseline
    assert runtime.model.calls == calls
    assert not LegacyDocumentVisibility.from_repository(runtime.documents, project_id='beta').allows(target)
    assert not recognition_document_visible(runtime.records, WorkScope('local-user', 'beta'), target['id'])
    with pytest.raises(RecognitionConflict):
        freeze_turn_materials(runtime.records, runtime.model, 'beta', [
            {'type': 'document', 'id': target['id'], 'revision': 1, 'project_id': 'beta'}],
            authority=SourceEgressService(runtime.records))
    assert runtime.domains.query.prepare_ask('beta', '事实')['chosen'] == []


@pytest.mark.parametrize('privacy', ['project', 'original'])
def test_original_privacy_keeps_local_copy_and_blocks_external_extraction_and_recall(filed, privacy):
    from backend.memory_app.v2.insight_generation import generate_insights
    from backend.memory_app.v2.privacy import set_private_project
    runtime, source, target, copied = filed
    runtime.model.public = lambda: {'generation': {'base_url': 'https://example.invalid/v1', 'allow_remote': True}}
    if privacy == 'project':
        set_private_project(runtime.records, 'alpha', True, 0)
    else:
        item_id = source['source_refs'][0]['source_id']
        item_row = runtime.records.read('workspace_items', item_id)
        SourceEgressService(runtime.records).set_policy(WorkScope('local-user', 'alpha'),
            'original_item', item_id, item_row.revision, 0, [])
    baseline, calls = runtime.records.list_all(), runtime.model.calls
    assert ensure_document_experience(runtime.documents, runtime.domains.query.service, 'beta', target['id']) == (copied, 1)
    assert generate_insights(runtime.model, runtime.domains.query.service, runtime.documents, 'beta', target['id']) == []
    assert runtime.model.calls == calls
    assert runtime.records.list_all() == baseline
    assert runtime.domains.query.prepare_ask('beta', '事实')['chosen'] == []
