"""Real filed document chunk parents and copied L3 share their retained source."""
from types import SimpleNamespace

import pytest

from backend.memory_app.source_egress import SourceEgressService
from backend.memory_app.v2.cache_maintenance import CacheMaintenance
from backend.memory_app.v2.privacy import set_private_project
from backend.recognition import RecognitionService, WorkScope
from backend.recognition_retrieval import SQLiteEmbeddingCache
from backend.recognition_retrieval.cache_invalidation import chunk_cache_namespace, chunk_cache_id
from tests.memory_app.v2.test_document_filing_sources import filed, runtime
from tests.memory_app.v2.test_cache_sources import recognition, vector_rows


def cached(filed, *, edited=False):
    runtime, original, target, copied = filed
    service = RecognitionService(runtime.records)
    scope = WorkScope('local-user', 'beta')
    if edited:
        from backend.memory_app.document_recognition import ensure_document_experience
        runtime.documents.save_user_edit(target['id'], markdown='修改后的摘要与证据', expected_revision=1)
        copied, revision = ensure_document_experience(runtime.documents, service, 'beta', target['id'])
        assert revision == 2
    dependent = recognition(service, scope, copied)
    independent = service.stage_experience(scope=scope, content='Independent method',
        provenance={'kind': 'user_statement', 'actor': 'local-user'})
    independent = recognition(service, scope, independent)
    entries, _, documents, _, _ = runtime.domains.query.retrieval_index.indexed_entries(
        'beta', selected={'kind': 'document', 'id': target['id']})
    entry = next(value for value in entries if value['id'] == target['id'])
    namespace = chunk_cache_namespace('beta', entry)
    chunks = {(namespace, chunk_cache_id(entry, span['layer'], slot, index))
        for slot, span in enumerate(documents[target['id']]['spans']) for index in range(len(span['chunks']))}
    assert chunks
    affected = chunks | {('beta', dependent.id)}
    untouched = {('beta', independent.id)}
    path = runtime.records.database_path.parent / 'recognition-vectors.sqlite3'
    def fill(pairs):
        cache = SQLiteEmbeddingCache(str(path))
        try:
            for project, identity in pairs:
                cache.write(model_id='synthetic-vector', entry=SimpleNamespace(
                    project_id=project, id=identity, revision=entry['revision'] if (project, identity) in chunks else 1), vector=(1., 0.))
        finally:
            cache.close()
    fill(affected | untouched)
    return runtime, path, affected, untouched, fill


@pytest.mark.parametrize('change', ['source', 'project'])
@pytest.mark.parametrize('edited', [False, True])
def test_private_origin_deletes_filed_document_and_l3_cache_preserving_independent_parent(filed, change, edited):
    runtime, path, affected, untouched, fill = cached(filed, edited=edited)
    before = vector_rows(path)
    assert CacheMaintenance(runtime.domains.query).run() == 0
    assert vector_rows(path) == before
    if change == 'project':
        set_private_project(runtime.records, 'alpha', True, 0)
    else:
        source = runtime.records.list('workspace_items')[0]
        SourceEgressService(runtime.records).set_policy(WorkScope('local-user', 'alpha'),
            'original_item', source.object_id, source.revision, 0, [])
    assert vector_rows(path) == tuple(row for row in before if row[:2] in untouched)
    # Daily fallback uses the same current filing authority after interrupted
    # pre-upgrade writes, without producing an embedding/model request.
    fill(affected)
    calls = runtime.model.calls
    assert CacheMaintenance(runtime.domains.query).run() == len(affected)
    assert vector_rows(path) == tuple(row for row in before if row[:2] in untouched)
    assert runtime.model.calls == calls
