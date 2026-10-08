"""Real write-through projections and first-query freshness in synthetic stores."""
from backend.memory_app.v2.privacy import set_private_project
from backend.memory_app.source_egress import SourceEgressService
from backend.recognition import WorkScope
from core.document_engine.runtime import _revision_object_id
import pytest
from core.storage_provider import SQLiteUnitOfWorkError
from tests.memory_app.v2.test_workbench_ask import env, add_document


INDEX = 'document_retrieval_index'


def test_document_creation_and_edit_publish_index_in_the_same_transaction(env):
    identity, _ = add_document(env, summary='needle summary', body='needle body')
    document = env.documents.read(identity)
    index = env.records.read(INDEX, identity)
    assert index is not None
    assert index.payload['document_revision'] == document['revision'] == 2
    assert any('needle body' in chunk['text'] for span in index.payload['spans'] for chunk in span['chunks'])
    assert index.payload['projection_version']
    changed = env.documents.save_user_edit(identity, expected_revision=2,
        markdown='# Synthetic\n\n## 摘要\nreplacement\n\n## 正文\nreplacement')
    index = env.records.read(INDEX, identity)
    assert index.payload['document_revision'] == changed['revision'] == 3
    assert all('needle' not in chunk['text'] for span in index.payload['spans'] for chunk in span['chunks'])
    assert any('replacement' in chunk['text'] for span in index.payload['spans'] for chunk in span['chunks'])


def test_question_reads_only_matching_document_bodies(env, monkeypatch):
    hit, _ = add_document(env, summary='zygomorphic', body='zygomorphic', original='zygomorphic')
    miss, _ = add_document(env, summary='orchard', body='orchard', original='orchard')
    reads = []
    original = env.records.read_batch
    original_read = env.records.read
    def observed(selections):
        reads.extend(selections.get('document_markdown', ()))
        return original(selections)
    monkeypatch.setattr(env.records, 'read_batch', observed)
    def observed_read(collection, identity):
        if collection == 'document_markdown':
            reads.append(identity)
        return original_read(collection, identity)
    monkeypatch.setattr(env.records, 'read', observed_read)
    rows = env.domains.query.collect_candidates('alpha', 'zygomorphic')['candidates']
    assert any(row['entry']['id'] == hit for row in rows)
    assert not any(row['entry']['id'] == miss for row in rows)
    assert set(reads) == {_revision_object_id(hit, 2)}


def test_index_is_precomputed_spans_and_query_does_not_rebuild_all_entries(env, monkeypatch):
    hit, _ = add_document(env, summary='zygomorphic', body='zygomorphic', original='zygomorphic')
    add_document(env, summary='orchard', body='orchard', original='orchard')
    index = env.records.read(INDEX, hit).payload
    assert not {'markdown', 'content', 'search_text'} & index.keys()
    assert index['entry']['id'] == hit
    query, selections = env.domains.query, []
    original = query.query_entries
    def observed(project, **options):
        selections.append(options.get('selected'))
        return original(project, **options)
    monkeypatch.setattr(query, 'query_entries', observed)
    assert query.collect_candidates('alpha', 'zygomorphic')['candidates']
    assert selections and all(values is not None for values in selections)
    assert {entry['id'] for values in selections for entry in values} == {hit}


def test_cached_vector_only_second_question_reads_only_matching_document_body(env, monkeypatch):
    from backend.memory_app.retrieval_models import ConfiguredTransport
    from tests.memory_app.v2.test_insight_links import VectorModel
    hit, _ = add_document(env, summary='orchardonly', body='orchardbody', original='orchardsource')
    misses = {add_document(env, summary=text, body=text, original=text)[0] for text in ('stones', 'metals')}
    query = env.domains.query
    query.models = VectorModel()
    wires = []
    def wire(transport, *, endpoint, payload):
        transport._check_current()
        wires.append(payload['input'])
        return {'data':[{'index':i, 'embedding':[1.0 if text == 'botany?' or 'orchard' in text else -1.0, 0.0]}
                        for i, text in enumerate(payload['input'])], 'usage':{'prompt_tokens':5}}
    monkeypatch.setattr(ConfiguredTransport, 'post_json', wire)
    first = query.collect_candidates('alpha', 'botany?')['candidates']
    assert {row['entry']['id'] for row in first} == {hit}
    assert {row['layer'] for row in first} == {'L0', 'L1'}
    assert all('botany' not in window.text for row in first for window in row['windows'])
    first_wire_count, reads = len(wires), []
    original_read, original_batch = env.records.read, env.records.read_batch
    def observed(collection, identity):
        if collection == 'document_markdown':
            reads.append(identity)
        return original_read(collection, identity)
    def observed_batch(selections):
        reads.extend(selections.get('document_markdown', ()))
        return original_batch(selections)
    monkeypatch.setattr(env.records, 'read', observed)
    monkeypatch.setattr(env.records, 'read_batch', observed_batch)
    second = query.collect_candidates('alpha', 'botany?')['candidates']
    assert {row['entry']['id'] for row in second} == {hit}
    assert all(inputs == ['botany?'] for inputs in wires[first_wire_count:])
    assert set(reads) == {_revision_object_id(hit, 2)}
    assert not {_revision_object_id(identity, 2) for identity in misses} & set(reads)


def test_vector_guard_reads_only_its_current_parent_projections(env, monkeypatch):
    from core.storage_provider import SQLiteStructuredRecordStore
    from core.storage_provider.source_retrieval_index import ORIGINALS, COLLECTION as SOURCES
    hit, item = add_document(env, summary='needle', body='needle', original='needle')
    add_document(env, summary='other', body='other', original='other')
    query = env.domains.query
    entry = query.query_entries('alpha', selected=[{'kind':'document', 'id':hit}])[0]
    visited = set()
    original_read, original_list = SQLiteStructuredRecordStore.read, SQLiteStructuredRecordStore.list_matching
    def observed_read(store, collection, identity):
        if collection in {INDEX, ORIGINALS, SOURCES}:
            visited.add((collection, identity))
        return original_read(store, collection, identity)
    def observed_list(store, collection, **fields):
        values = original_list(store, collection, **fields)
        if collection in {INDEX, ORIGINALS, SOURCES}:
            visited.update((collection, row.object_id) for row in values)
        return values
    monkeypatch.setattr(SQLiteStructuredRecordStore, 'read', observed_read)
    monkeypatch.setattr(SQLiteStructuredRecordStore, 'list_matching', observed_list)
    material = query.retrieval_index.vector_material('alpha', entry, 'L1')
    assert material is not None and material[0]['id'] == hit
    assert visited == {(INDEX, hit), (ORIGINALS, item)}


@pytest.mark.parametrize('selected_source', [False, True])
def test_publication_discovery_does_not_read_unrelated_fact_table_bodies(env, monkeypatch, selected_source):
    from core.storage_provider import SQLiteStructuredRecordStore
    hit, _ = add_document(env, summary='needle', body='needle', original='needle')
    miss, item = add_document(env, summary='other', body='other', original='other')
    query = env.domains.query
    query.source_store.write('sources', 'standalone', {'id':'standalone', 'project_id':'alpha',
        'title':'Synthetic', 'metadata':{'content':'source needle'}}, expected_revision=0)
    with env.records.begin() as tx:
        tx.put('v2_turns', 'publication-turn', {'project_id':'alpha', 'receipt':{'do':{
            'state':'done', 'document_id':miss, 'kernel_turn_id':'publication-kernel'}}}, expected_revision=0)
        tx.put('v2_task_draft_operations', 'deliver-publication-kernel', {
            'inputs':{'project_id':'alpha', 'turn_id':'publication-kernel', 'markdown':'unrelated delivery body'},
            'result':{'document_id':miss}}, expected_revision=0)
        tx.commit()
    body_reads = []
    def observe(row):
        if row is not None:
            value = row.payload
            if (row.collection == 'workspace_items' and row.object_id == item
                    and {'source_text', 'draft'} & value.keys()):
                body_reads.append((row.collection, row.object_id))
            if row.collection == 'documents' and row.object_id == miss and 'blocks' in value:
                body_reads.append((row.collection, row.object_id))
            if row.collection == 'v2_task_draft_operations' and 'markdown' in value.get('inputs', {}):
                body_reads.append((row.collection, row.object_id))
        return row
    original_read, original_list = SQLiteStructuredRecordStore.read, SQLiteStructuredRecordStore.list_matching
    def observed_read(store, collection, identity):
        return observe(original_read(store, collection, identity))
    def observed_list(store, collection, **fields):
        return tuple(observe(row) for row in original_list(store, collection, **fields))
    monkeypatch.setattr(SQLiteStructuredRecordStore, 'read', observed_read)
    monkeypatch.setattr(SQLiteStructuredRecordStore, 'list_matching', observed_list)
    original_projected, original_single = SQLiteStructuredRecordStore.list_projected, SQLiteStructuredRecordStore.read_projected
    def observed_projected(store, collection, **options):
        return tuple(observe(row) for row in original_projected(store, collection, **options))
    def observed_single(store, collection, identity, **options):
        return observe(original_single(store, collection, identity, **options))
    monkeypatch.setattr(SQLiteStructuredRecordStore, 'list_projected', observed_projected)
    monkeypatch.setattr(SQLiteStructuredRecordStore, 'read_projected', observed_single)
    if selected_source:
        entries = query.query_entries('alpha', selected=[{'kind':'source', 'id':'standalone'}])
        assert {entry['id'] for entry in entries} == {'standalone'}
    else:
        entries = query.retrieval_index.indexed_entries('alpha')[0]
        assert {hit, miss, 'standalone'} <= {entry['id'] for entry in entries}
    assert body_reads == []


def test_publication_metadata_projection_preserves_delivery_and_current_state(tmp_path):
    from backend.shared.document_visibility import LegacyDocumentVisibility
    from backend.memory_app.v2.task_drafts import TaskDrafts
    from core.document_engine import SQLiteDocumentRepository
    from core.storage_provider import SQLiteStructuredRecordStore
    records = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    documents = SQLiteDocumentRepository(records)
    output = TaskDrafts(records, documents).create(turn_id='turn-kernel-one', project='alpha',
        operation='deliver-turn-kernel-one', title='Synthetic', markdown='private delivery body')
    identity = output['document_id']
    with records.begin() as tx:
        tx.put('v2_turns', 'product-one', {'project_id':'alpha', 'receipt':{'do':{
            'state':'partial', 'document_id':identity, 'kernel_turn_id':'turn-kernel-one'}}}, expected_revision=0)
        tx.put('workspace_review_intents', 'review-one', {'source_id':'pending-source', 'state':'prepared'}, expected_revision=0)
        tx.commit()
    def compare():
        whole = LegacyDocumentVisibility.from_repository(documents, project_id='alpha')
        projected = LegacyDocumentVisibility.from_repository(documents, project_id='alpha', metadata_only=True)
        assert projected == whole
        selected = LegacyDocumentVisibility.from_repository(documents, project_id='alpha',
            document_ids={identity}, metadata_only=True)
        assert selected == LegacyDocumentVisibility.from_repository(documents, project_id='alpha', document_ids={identity})
        assert projected.pending_sources == frozenset({'pending-source'})
        return projected.allows(documents.read(identity))
    assert compare()
    with records.begin() as tx:
        current = tx.read('v2_task_draft_operations', 'deliver-turn-kernel-one')
        changed = {**current.payload, 'inputs':{**current.payload['inputs'], 'turn_id':'different-kernel'}}
        tx.put(current.collection, current.object_id, changed, expected_revision=current.revision)
        tx.commit()
    assert not compare()


def test_fixed_nested_metadata_reader_returns_no_body_and_rejects_body_fields(tmp_path):
    from core.storage_provider import SQLiteStructuredRecordStore
    records = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    with records.begin() as tx:
        tx.put('v2_task_draft_operations', 'deliver-kernel-one', {
            'inputs':{'project_id':'alpha', 'turn_id':'kernel-one', 'markdown':'body'},
            'result':{'document_id':'document-one'}}, expected_revision=0)
        tx.commit()
    row = records.read_projected('v2_task_draft_operations', 'deliver-kernel-one',
        fields=('inputs.project_id', 'inputs.turn_id', 'result.document_id'))
    assert row.revision == 1
    assert row.payload == {'inputs':{'project_id':'alpha', 'turn_id':'kernel-one'},
                           'result':{'document_id':'document-one'}}
    assert records.read_projected('v2_task_draft_operations', 'absent', fields=('inputs.turn_id',)) is None
    for field in ('inputs.markdown', 'inputs', 'receipt.do', 'blocks', "inputs.turn_id') FROM crp_structured_records --"):
        with pytest.raises(SQLiteUnitOfWorkError, match='unsupported projected metadata field'):
            records.read_projected('v2_task_draft_operations', 'deliver-kernel-one', fields=(field,))


def test_first_query_after_document_edit_uses_new_text_and_offsets(env):
    identity, _ = add_document(env, summary='oldneedle', body='oldneedle', original='plain original')
    query = env.domains.query
    assert any(row['entry']['id'] == identity for row in query.collect_candidates('alpha', 'oldneedle')['candidates'])
    env.documents.save_user_edit(identity, expected_revision=2,
        markdown='# Synthetic\n\n## 摘要\nnewneedle\n\n## 正文\nnewneedle')
    assert not any(row['entry']['id'] == identity for row in query.collect_candidates('alpha', 'oldneedle')['candidates'])
    rows = query.collect_candidates('alpha', 'newneedle')['candidates']
    assert any(row['entry']['id'] == identity for row in rows)
    current = env.documents.markdown(identity)
    for row in rows:
        if row['entry']['id'] == identity and row['layer'] in {'L1', 'L2'}:
            assert row['entry']['revision'] == 3
            assert all(current[window.start:window.end] == window.text for window in row['windows'])


def test_stale_index_is_discarded_before_using_its_text(env):
    identity, _ = add_document(env, summary='oldneedle', body='oldneedle', original='plain original')
    previous = env.records.read(INDEX, identity)
    assert previous is not None
    env.documents.save_user_edit(identity, expected_revision=2, markdown='# New\nnewneedle')
    with env.records.begin() as tx:
        current = tx.read(INDEX, identity)
        tx.put(INDEX, identity, previous.payload, expected_revision=current.revision)
        tx.commit()
    rows = env.domains.query.collect_candidates('alpha', 'oldneedle')['candidates']
    assert not any(row['entry']['id'] == identity for row in rows)
    env.domains.query.retrieval_index.wait_for_repairs()
    assert env.records.read(INDEX, identity).payload['document_revision'] == 3
    assert any(row['entry']['id'] == identity for row in
        env.domains.query.collect_candidates('alpha', 'newneedle')['candidates'])


def test_projection_failure_rolls_back_document_and_index_together(env, monkeypatch):
    from core.document_engine import sqlite_runtime
    identity, _ = add_document(env)
    previous = env.records.read(INDEX, identity)
    markdown = env.documents.markdown(identity)
    def fail(tx, document, body):
        raise RuntimeError('synthetic projection failure')
    monkeypatch.setattr(sqlite_runtime, 'project_document', fail)
    with pytest.raises(RuntimeError, match='synthetic projection failure'):
        env.documents.save_user_edit(identity, expected_revision=2, markdown='replacement')
    assert env.documents.read(identity)['revision'] == 2
    assert env.documents.markdown(identity) == markdown
    assert env.records.read(INDEX, identity) == previous


def test_confirmed_original_projection_binds_final_item_revision(env):
    from core.storage_provider.source_retrieval_index import ORIGINALS
    _, identity = add_document(env, original='original needle')
    item = env.records.read('workspace_items', identity)
    projected = env.records.read(ORIGINALS, identity)
    assert projected is not None
    assert projected.payload['original_revision'] == item.revision
    assert projected.payload['chunks'][0]['text'] == 'original needle'


def test_processing_text_projection_commits_with_the_new_item_revision(env):
    from core.storage_provider.source_retrieval_index import ORIGINALS
    import asyncio
    item = asyncio.run(env.domains.intake.add_text({'project_id':'alpha', 'text':'before'}))
    lease = env.domains.intake.items.processing_lease
    lease.claim(item['id'], 'alpha', item['revision'], 'synthetic-run', None, {})
    lease.apply(item['id'], 'alpha', 'synthetic-run', {'source_text':'after', 'status':'ready'})
    current = env.records.read('workspace_items', item['id'])
    projected = env.records.read(ORIGINALS, item['id'])
    assert projected is not None
    assert projected.payload['original_revision'] == current.revision
    assert projected.payload['chunks'][0]['text'] == 'after'


def test_nondefault_confirmation_preserves_original_only_hit_on_first_question(tmp_path):
    import asyncio
    from threading import RLock
    from types import SimpleNamespace
    from core.document_engine import SQLiteDocumentRepository
    from core.storage_provider import JsonObjectStore, SQLiteStructuredRecordStore
    from core.storage_provider.source_retrieval_index import ORIGINALS
    from backend.recognition import RecognitionService
    from backend.memory_app.processing_lease import ProcessingLease
    from backend.memory_app.workspace_items import WorkspaceItems
    from backend.memory_app.workspace_review import WorkspaceReview
    from backend.memory_app.workspace_query import WorkspaceQuery
    records = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    documents = SQLiteDocumentRepository(records, namespace_id='recognition')
    service = RecognitionService(records)
    items = WorkspaceItems(records, ProcessingLease(records, 'workspace_items', 'synthetic'), RLock())
    query = WorkspaceQuery(records, documents, JsonObjectStore(tmp_path / '.rebuild-data', namespace_id='recognition'),
                           SimpleNamespace(), service)
    item = items.create('alpha', 'text', 'Synthetic', 'zygomorphic')
    ready = items.update(item['id'], 'alpha', {'staged'}, status='ready', draft={
        'title':'Synthetic', 'summary':'plain', 'facts':[], 'topics':[], 'todos':[],
        'uncertainties':[], 'people':[], 'dates':[], 'suggestions':[]})
    review = WorkspaceReview(items, documents, service, None, None, tmp_path)
    saved = asyncio.run(review.confirm(item['id'], {'project_id':'alpha', 'expected_revision':ready['revision']}))
    current = records.read('workspace_items', item['id'])
    assert records.read(ORIGINALS, item['id']).payload['original_revision'] == current.revision
    assert query.source_store.list('sources') == ()
    selected = query.collect_candidates('alpha', 'zygomorphic')['candidates']
    assert {row['id'] for row in selected} == {item['id']}
    assert {row['layer'] for row in selected} == {'L0'}
    assert all(row['entry']['revision'] == documents.read(saved['document_id'])['revision'] for row in selected)


@pytest.mark.parametrize('body', [
    'plain. ' * 200 + 'Straße needle. ',
    '# Header\r\n\r\n' + ('汉字。\r\n\r\n' * 170) + 'needle。',
    '```\n## 摘要\nneedle\n```\n' + ('other. ' * 230),
])
def test_indexed_long_chunks_keep_exact_lexical_vector_windows(body):
    from backend.memory_app.v2.contextual_chunks import select_contextual_windows, select_indexed_contextual_windows
    from core.search_and_recall.evidence_windows import split_evidence_chunks
    chunks = split_evidence_chunks(body)
    for question, vectors in (('needle', {}), ('STRASSE', {}), ('synonym', {len(chunks)-1:0.9})):
        expected = select_contextual_windows(body, question, chunks=chunks, vector_scores=vectors,
                                            title='Synthetic', summary='context', max_chars=10800)
        selected = select_indexed_contextual_windows(len(body), question, chunks=chunks, vector_scores=vectors,
                                                     title='Synthetic', summary='context', max_chars=10800)
        assert selected == expected
        assert all(body[window.start:window.end] == window.text for window in selected.windows)


def test_metadata_projection_does_not_admit_body_fields_or_free_sql(env):
    identity, _ = add_document(env)
    rows = env.records.list_projected('documents', fields=('id','revision'), project_id='alpha')
    assert len(rows) == 1 and rows[0].payload == {'id':identity, 'revision':2}
    with pytest.raises(SQLiteUnitOfWorkError):
        env.records.list_projected('documents', fields=('blocks',))
    with pytest.raises(SQLiteUnitOfWorkError):
        env.records.list_projected('documents', fields=('id',), arbitrary_sql='unsafe')


def test_first_query_reads_private_and_forgotten_state_live(env):
    identity, item = add_document(env, summary='needle', body='needle', original='needle')
    query = env.domains.query
    def selected():
        return [row for row in query.collect_candidates('alpha', 'needle')['candidates']
                if row['entry']['id'] == identity]
    assert selected()
    set_private_project(env.records, 'alpha', True, 0)
    assert not selected()
    set_private_project(env.records, 'alpha', False, 1)
    assert selected()
    authority = SourceEgressService(env.records)
    source = env.records.read('workspace_items', item)
    authority.set_policy(WorkScope('local-user', 'alpha'), 'original_item', item,
        source.revision, 0, [])
    assert not selected()
    authority.set_policy(WorkScope('local-user', 'alpha'), 'original_item', item,
        source.revision, 1, ['generation', 'embedding', 'rerank'])
    assert selected()
    with env.records.begin() as tx:
        tx.put('v2_document_recall', identity,
            {'project_id':'alpha', 'state':'forgotten', 'by':'user'}, expected_revision=0)
        tx.commit()
    assert not selected()
    with env.records.begin() as tx:
        tx.put('v2_document_recall', identity,
            {'project_id':'alpha', 'state':'normal', 'by':'user'}, expected_revision=1)
        tx.commit()
    assert selected()
