"""Revision-bound, rebuildable text projection of a Document, without authority."""
from .markdown_sections import summary_of
from core.search_and_recall.evidence_windows import split_evidence_chunks
from core.search_and_recall.vector_cache_invalidation import VectorCacheInvalidator, vector_cache_path

COLLECTION = 'document_retrieval_index'
PROJECTION_VERSION = 'document-search@1'


def project_document(tx, document, markdown):
    identity = document['id']
    summary, start, end = summary_of(markdown)
    spans = [{'layer':'L2', 'start':start, 'end':end, 'chunks':[]}] if summary else []
    for left, right in ((0, start), (end, len(markdown))):
        if right > left:
            spans.append({'layer':'L1', 'start':left, 'end':right,
                'chunks':[{'start':w.start, 'end':w.end, 'text':w.text}
                          for w in split_evidence_chunks(markdown[left:right])]})
    payload = {'document_id': identity, 'project_id': document['project_id'],
               'document_revision': document['revision'],
               'projection_version': PROJECTION_VERSION,
               'entry': {'kind':'document', 'id':identity, 'document_id':identity,
                         'title':str(document.get('title') or identity), 'revision':document['revision']},
               'summary':summary, 'spans':spans}
    current = tx.read(COLLECTION, identity)
    if current is not None and dict(current.payload) == payload:
        return current
    invalidator = VectorCacheInvalidator(vector_cache_path(tx))
    projects = {document['project_id']}
    if current is not None:
        projects.add(current.payload['project_id'])
    for project in sorted(projects):
        invalidator.material(project, 'document', identity)
    return tx.put(COLLECTION, identity, payload,
                  expected_revision=current.revision if current else 0)
