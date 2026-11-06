from collections import Counter
from contextlib import closing
import sqlite3
from types import SimpleNamespace
from urllib.parse import urlsplit

from backend.recognition import RecognitionError
from backend.recognition_retrieval import RecognitionRetrievalError, SQLiteEmbeddingCache
from backend.recognition_retrieval.cache_invalidation import chunk_cache_namespace
from core.storage_provider import SQLiteUnitOfWorkConflict

from ..source_egress import SourceEgressService
from .material_analysis import PUBLICATIONS
from .policies.topic_grouping import frequent_labels
from .privacy import is_private_project, privacy_revision


COLLECTION = 'v2_topic_groups'


def summary_cache_entry(project, entry):
    return SimpleNamespace(project_id=chunk_cache_namespace(project, entry),
        id='topic-summary-v1:' + entry['id'], revision=entry['revision'])


def _tags(publication):
    return publication.payload.get('analysis', {}).get('plan', {}).get('topic_groups', ())


def known_labels(reader, project):
    identities = {row.object_id for row in reader.list_matching('documents', project_id=project)
                  if row.payload.get('status') != 'archived'}
    names, counts = {}, Counter()
    for row in reader.list_matching(PUBLICATIONS, project_id=project):
        if row.payload.get('document_id') not in identities:
            continue
        for value in _tags(row):
            name = names.setdefault(value.casefold(), value)
            counts[name] += 1
    return frequent_labels(counts)


def can_use_labels(models, auxiliary_binding):
    if auxiliary_binding is not None:
        models = models.for_auxiliary(auxiliary_binding)
    generation = models.public()['generation']
    host = urlsplit(generation.get('base_url') or '').hostname
    return host in {'localhost', '127.0.0.1', '::1'} and not generation.get('subscription_binding')


def _document_tags(reader, project, entry):
    if not entry.get('item_id'):
        return ()
    row = reader.read(PUBLICATIONS, 'confirm-' + entry['item_id'])
    if (row is None or row.payload.get('project_id') != project
            or row.payload.get('document_id') != entry['id']):
        return ()
    return _tags(row)


def matching_documents(query, project, entries, *, model_id, question_vector, policy, scene=None):
    if not entries or not question_vector or query.origin_admission is not None:
        return frozenset()
    records = query.records
    path = records.database_path.parent / 'recognition-vectors.sqlite3'
    if not path.is_file() or is_private_project(records, project):
        return frozenset()
    privacy = privacy_revision(records)
    authority = SourceEgressService(records)
    eligible = []
    for entry, scope, snapshot in entries:
        try:
            authority.validate_snapshot(scope, snapshot)
            authority.require(snapshot, 'generation')
        except RecognitionError:
            continue
        eligible.append(entry)
    try:
        with closing(SQLiteEmbeddingCache(str(path), read_only=True)) as cache:
            vectors = cache.read_many(model_id=model_id,
                entries=[summary_cache_entry(project, entry) for entry in eligible])
    except (sqlite3.Error, RecognitionRetrievalError):
        return frozenset()
    if not vectors or privacy_revision(records) != privacy:
        return frozenset()
    rows = []
    basis = []
    for entry in sorted(eligible, key=lambda value: value['id']):
        tags = list(_document_tags(records, project, entry))
        vector = vectors.get(summary_cache_entry(project, entry).id)
        rows.append({'id': entry['id'], 'tags': tags, 'vector': vector})
        basis.append([entry['id'], entry['revision'], tags, vector is not None])
    previous = records.read(COLLECTION, project) if scene is None else None
    signature = {'model_id': model_id, 'policy': '@6', 'privacy_revision': privacy, 'members': basis}
    if previous is not None and previous.payload.get('signature') == signature:
        groups = previous.payload['groups']
        return policy.match(groups, question_vector) if privacy_revision(records) == privacy else frozenset()
    groups = policy.build(rows)
    if scene is None and not query.retrieval_index.read_only:
        try:
            with records.begin() as tx:
                if privacy_revision(tx) != privacy or tx.read(COLLECTION, project) != previous:
                    return frozenset()
                tx.put(COLLECTION, project, {'signature': signature, 'groups': groups},
                    expected_revision=previous.revision if previous else 0)
                tx.commit()
        except SQLiteUnitOfWorkConflict:
            return frozenset()
    return policy.match(groups, question_vector) if privacy_revision(records) == privacy else frozenset()
