"""Fade generated single-document impressions without rewriting their evidence."""
from uuid import uuid4

from core.storage_provider import SQLiteUnitOfWorkConflict
from .usage import timestamp, utc_now


COLLECTION = 'v2_candidate_fade'
PATTERNS = 'v2_insight_patterns'


def pattern_sources(reader, scope):
    """Source candidate identities retained by reviewable pattern metadata."""
    sources = set()
    for metadata in reader.list(PATTERNS):
        pattern = reader.read('recognition_candidates', metadata.object_id)
        if (pattern and pattern.payload.get('scope') == scope
                and pattern.payload.get('state') in {'pending', 'published'}):
            sources.update(identity for identity in metadata.payload.get('source_candidate_ids', [])
                           if isinstance(identity, str))
    return sources


def single_document(reader, candidate):
    payload = candidate.payload
    scope = payload.get('scope', {})
    experiences = payload.get('source_experience_ids', [])
    if (not payload.get('generation') or len(experiences) != 1
            or payload.get('source_recognition_ids')):
        return False
    experience = reader.read('recognition_experiences', experiences[0])
    if experience is None or experience.payload.get('scope') != scope:
        return False
    provenance = experience.payload.get('provenance', {})
    if provenance.get('kind') != 'workspace_confirmed_document':
        return False
    documents = {ref.get('id') for ref in provenance.get('source_refs', [])
                 if ref.get('type') == 'document' and isinstance(ref.get('id'), str)}
    if len(documents) != 1:
        return False
    document = reader.read('documents', next(iter(documents)))
    return document is not None and document.payload.get('project_id') == scope.get('project_id')


class CandidateFade:
    def __init__(self, records, *, now=utc_now):
        self.records, self.now = records, now

    def run(self):
        now, changed = self.now(), 0
        for candidate in self.records.list('recognition_candidates'):
            payload = candidate.payload
            scope = payload.get('scope', {})
            if (payload.get('state') != 'pending' or scope.get('project_id') == 'me'
                    or (now - timestamp(payload.get('created_at'), now)).total_seconds() < 30 * 86400
                    or not single_document(self.records, candidate)):
                continue
            try:
                with self.records.begin() as tx:
                    if (tx.read('recognition_candidates', candidate.object_id) != candidate
                            or tx.read(COLLECTION, candidate.object_id) is not None
                            or not single_document(tx, candidate)
                            or candidate.object_id in pattern_sources(tx, scope)):
                        continue
                    tx.put(COLLECTION, candidate.object_id, {'faded_at': now.isoformat()}, expected_revision=0)
                    tx.put('v2_activity', 'activity-' + uuid4().hex, {
                        'kind': 'fade', 'by': 'auto', 'project_id': scope['project_id'],
                        'object_kind': 'candidate', 'object_id': candidate.object_id,
                        'created_at': now.isoformat(),
                    }, expected_revision=0)
                    tx.commit()
                    changed += 1
            except SQLiteUnitOfWorkConflict:
                continue
        return changed
