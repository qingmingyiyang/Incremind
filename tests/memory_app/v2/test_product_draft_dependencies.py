"""Corrupt existing SQLite owner facts; never replace the authority under test."""
from copy import deepcopy

import pytest

from backend.memory_app.source_egress import SourceEgressService
from backend.memory_app.document_recognition import ensure_document_experience
from backend.memory_app.v2.task_drafts import TaskDrafts
from backend.recognition import RecognitionConflict, RecognitionService, WorkScope
from backend.recognition.product_draft_dependencies import (
    ProductDraftDependencyError, product_draft_source,
)
from core.document_engine import SQLiteDocumentRepository
from core.storage_provider import SQLiteStructuredRecordStore
from core.ai_kernel import SQLiteAITurnStore
from backend.memory_app.original_sources import source_store


@pytest.fixture
def draft(tmp_path, request):
    records = SQLiteStructuredRecordStore(tmp_path / 'draft.sqlite3')
    documents = SQLiteDocumentRepository(records, namespace_id='recognition')
    service = RecognitionService(records)
    scope = WorkScope('local-user', 'beta')
    experience = service.stage_experience(scope=scope, content='新方法')
    candidate = service.propose(scope=scope, content='新方法', source_experience_ids=[experience])
    recognition = service.publish(scope=scope, candidate_id=candidate.id,
        expected_revision=1, reviewer=scope.user_id)
    material = {'type': 'recognition', 'id': recognition.id, 'revision': 1, 'project_id': 'beta'}
    if getattr(request, 'param', False):
        SourceEgressService(records).set_policy(scope, 'recognition', recognition.id, 1, 0, [])
    snapshot = SourceEgressService(records).snapshot(scope,
        [{'type': 'recognition', 'id': recognition.id, 'revision': 1}])
    output = TaskDrafts(records, documents).create(turn_id='turn-test', project='beta',
        operation='deliver-turn-test', title='整理稿', markdown='新方法成果')
    with records.begin() as tx:
        tx.put('v2_task_executions', 'product-test', {'project_id': 'beta', 'started': True,
            'request': {'turn_id': 'turn-test', 'session_id': 'session-test',
                'operation_id': 'op-test', 'idempotency_key': 'key-test', 'scope': {
                'kind': 'project', 'project_id': 'beta', 'series_id': None},
                'desired_outcome': 'project.task', 'privacy': {
                    'material_refs': [material], 'source_snapshots': [snapshot]},
                'input': {'refs': [{'kind': 'atom', 'object_id': recognition.id,
                    'uri': 'crp://default/recognitions/' + recognition.id}]}}}, expected_revision=0)
        tx.put('v2_turns', 'product-test', {'project_id': 'beta', 'intent': 'do', 'receipt': {
            'do': {'state': 'done', 'document_id': output['document_id'],
                'kernel_turn_id': 'turn-test', 'title': '整理稿'}}}, expected_revision=0)
        tx.commit()
    request = records.read('v2_task_executions', 'product-test').payload['request']
    SQLiteAITurnStore(source_store(records).root / 'ai-turns.sqlite3').claim_turn(request)
    return records, scope, output['document_id']


def test_completed_product_draft_reads_only_existing_owner_facts(draft):
    records, scope, identity = draft
    before = records.list_all()
    bound = product_draft_source(records, scope, identity, 1)
    assert bound.revisions['product_turn_id'] == 'turn-test'
    assert bound.revisions['task_execution_id'] == 'product-test'
    assert len(bound.roots) == 1
    assert records.list_all() == before


@pytest.mark.parametrize('collection,path,value', [
    ('document_revisions', ('revision',), True),
    ('document_markdown', ('revision',), 1.0),
    ('document_revisions', ('source_snapshot',), []),
    ('v2_task_executions', ('started',), 'true'),
    ('v2_task_executions', ('request', 'privacy'), []),
    ('v2_task_executions', ('request', 'input'), []),
    ('v2_turns', ('receipt',), []),
    ('v2_turns', ('receipt', 'do'), []),
])
def test_corrupt_owner_facts_reject_with_domain_error_and_zero_writes(draft, collection, path, value):
    records, scope, identity = draft
    key = identity + '~r1' if collection.startswith('document_') else 'product-test'
    row = records.read(collection, key)
    payload = deepcopy(row.payload)
    target = payload
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value
    with records.begin() as tx:
        tx.put(collection, key, payload, expected_revision=row.revision)
        tx.commit()
    before = records.list_all()
    with pytest.raises(ProductDraftDependencyError):
        product_draft_source(records, scope, identity, 1)
    assert records.list_all() == before


def product_experience(draft):
    records, scope, identity = draft
    service = RecognitionService(records)
    documents = SQLiteDocumentRepository(records, namespace_id='recognition')
    experience, _ = ensure_document_experience(documents, service, scope.project_id, identity)
    row = records.read('recognition_experiences', experience)
    assert row.payload['provenance']['kind'] == 'model_generated_artifact'
    assert row.payload['provenance']['outcome_status'] == 'unknown'
    return SourceEgressService(records), scope, {'type': 'experience', 'id': experience, 'revision': 1}


def test_product_draft_revoke_regrant_invalidates_old_snapshot_but_new_respects_original_ceiling(draft):
    records, scope, _ = draft
    authority, scope, ref = product_experience(draft)
    original = authority.snapshot(scope, [ref])
    authority.require(original, 'generation')
    root = records.read('v2_task_executions', 'product-test').payload['request']['privacy']['source_snapshots'][0]['roots'][0]
    authority.set_policy(scope, root['type'], root['id'], root['revision'], 0, [])
    private = authority.snapshot(scope, [ref])
    with pytest.raises(RecognitionConflict):
        authority.validate_snapshot(scope, original)
    with pytest.raises(RecognitionConflict):
        authority.require(private, 'generation')
    authority.set_policy(scope, root['type'], root['id'], root['revision'], 1,
        ['generation', 'embedding', 'rerank'])
    with pytest.raises(RecognitionConflict):
        authority.validate_snapshot(scope, original)
    fresh = authority.snapshot(scope, [ref])
    authority.validate_snapshot(scope, fresh)
    authority.require(fresh, 'generation')
    dependency = fresh['nodes'][0]['dependency_revisions']
    assert records.read('v2_task_executions', 'product-test').payload['request']['privacy']['source_snapshots'][0]['nodes'][-1]['policy_revision'] == 0
    assert next(node for node in dependency['current_source_graph']['nodes']
        if node['id'] == root['id'])['policy_revision'] == 2


@pytest.mark.parametrize('draft', [True], indirect=True)
def test_product_draft_frozen_private_ceiling_never_expands_after_regrant(draft):
    records, scope, _ = draft
    execution = records.read('v2_task_executions', 'product-test')
    root = execution.payload['request']['privacy']['material_refs'][0]
    authority = SourceEgressService(records)
    authority.set_policy(scope, root['type'], root['id'], root['revision'], 1,
        ['generation', 'embedding', 'rerank'])
    authority, scope, ref = product_experience(draft)
    frozen = authority.snapshot(scope, [ref])
    authority.validate_snapshot(scope, frozen)
    with pytest.raises(RecognitionConflict):
        authority.require(frozen, 'generation')
