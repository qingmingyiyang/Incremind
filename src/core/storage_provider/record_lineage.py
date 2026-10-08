"""Body-free record incarnations inside the existing structured-record UOW.

Creation facts are append-only. Current heads rotate only when the original
record is actually inserted, not when its payload or revision is updated.
Legacy records can gain a current observation anchor; that is not a claim
about their historical creation. Domain owners still decide revision and
qualification rules.
"""
from collections.abc import Mapping
import re
from types import MappingProxyType
from uuid import uuid4


FACTS = 'record_lineage_facts'
HEAD_COLLECTIONS = MappingProxyType({
    'workspace_items': 'record_lineage_workspace_items',
    'recognition_experiences': 'record_lineage_experiences',
    'recognitions': 'record_lineage_recognitions',
    'documents': 'record_lineage_documents',
    'document_revisions': 'record_lineage_document_revisions',
    'document_markdown': 'record_lineage_document_markdown',
    'workspace_confirmation_operations': 'record_lineage_confirmations',
    'workspace_review_intents': 'record_lineage_review_intents',
    'v2_experience_origins': 'record_lineage_experience_origins',
    'recognition_tasks': 'record_lineage_recognition_tasks',
    'recognition_context_packets': 'record_lineage_context_packets',
    'v2_task_draft_operations': 'record_lineage_draft_operations',
    'v2_task_executions': 'record_lineage_task_executions',
    'v2_turns': 'record_lineage_product_turns',
    'v2_external_input_dependencies': 'record_lineage_external_inputs',
    'v2_research_source_reads': 'record_lineage_research_reads',
    'v2_external_agent_deliveries': 'record_lineage_external_deliveries',
})
WITNESS_COLLECTIONS = MappingProxyType({
    'workspace_items': 'record_lineage_first_workspace_items',
    'recognition_experiences': 'record_lineage_first_experiences',
    'recognitions': 'record_lineage_first_recognitions',
    'documents': 'record_lineage_first_documents',
    'document_revisions': 'record_lineage_first_document_revisions',
    'document_markdown': 'record_lineage_first_document_markdown',
    'workspace_confirmation_operations': 'record_lineage_first_confirmations',
    'workspace_review_intents': 'record_lineage_first_review_intents',
    'v2_experience_origins': 'record_lineage_first_experience_origins',
    'recognition_tasks': 'record_lineage_first_recognition_tasks',
    'recognition_context_packets': 'record_lineage_first_context_packets',
    'v2_task_draft_operations': 'record_lineage_first_draft_operations',
    'v2_task_executions': 'record_lineage_first_task_executions',
    'v2_turns': 'record_lineage_first_product_turns',
    'v2_external_input_dependencies': 'record_lineage_first_external_inputs',
    'v2_research_source_reads': 'record_lineage_first_research_reads',
    'v2_external_agent_deliveries': 'record_lineage_first_external_deliveries',
})
_SEGMENT = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$')
_FACT_ID = re.compile(r'^[0-9a-f]{32}$')
_IDENTITY = {'collection', 'object_id', 'fact_id'}
_FACT = {'schema_version', 'collection', 'object_id', 'origin', 'observed_revision'}


class RecordLineageError(ValueError):
    """A record has no intact current incarnation authority."""


def _invalid():
    raise RecordLineageError('record_lineage_invalid')


def _key(collection, object_id):
    if (not isinstance(collection, str) or collection not in HEAD_COLLECTIONS
            or not isinstance(object_id, str) or not _SEGMENT.fullmatch(object_id)):
        _invalid()


def _fact_id(value):
    if not isinstance(value, str) or not _FACT_ID.fullmatch(value):
        _invalid()


def _fact(reader, collection, object_id, fact_id):
    _fact_id(fact_id)
    fact = reader.read(FACTS, fact_id)
    if fact is None or type(fact.revision) is not int or fact.revision != 1:
        _invalid()
    value = fact.payload
    if (not isinstance(value, Mapping) or set(value) != _FACT
            or type(value['schema_version']) is not int or value['schema_version'] != 1
            or value['collection'] != collection or value['object_id'] != object_id
            or not isinstance(value['origin'], str) or value['origin'] not in {'created', 'current_anchor'}
            or type(value['observed_revision']) is not int or value['observed_revision'] < 1):
        _invalid()
    return fact


def _head(reader, collection, object_id):
    head = reader.read(HEAD_COLLECTIONS[collection], object_id)
    witness = reader.read(WITNESS_COLLECTIONS[collection], object_id)
    if head is None and witness is None:
        return None
    # A permanent first witness distinguishes a new slot from a damaged head
    # using the original (collection, object_id) primary key, never a scan of
    # the accumulated UUID facts. Existing incomplete metadata is not adopted.
    if head is None or witness is None:
        _invalid()
    value = head.payload
    if (type(head.revision) is not int or head.revision < 1
            or not isinstance(value, Mapping) or set(value) != {'schema_version', 'fact_id'}
            or type(value['schema_version']) is not int or value['schema_version'] != 1):
        _invalid()
    value = witness.payload
    if (type(witness.revision) is not int or witness.revision != 1
            or not isinstance(value, Mapping) or set(value) != {'schema_version', 'first_fact_id'}
            or type(value['schema_version']) is not int or value['schema_version'] != 1):
        _invalid()
    _fact(reader, collection, object_id, value['first_fact_id'])
    if head.payload['fact_id'] != value['first_fact_id']:
        _fact(reader, collection, object_id, head.payload['fact_id'])
    return head


def _append(tx, collection, object_id, observed_revision, origin, head):
    identity = {'collection': collection, 'object_id': object_id, 'fact_id': uuid4().hex}
    tx._put_sidecar(FACTS, identity['fact_id'], {'schema_version': 1,
        'collection': collection, 'object_id': object_id, 'origin': origin,
        'observed_revision': observed_revision}, expected_revision=0)
    if head is None:
        tx._put_sidecar(WITNESS_COLLECTIONS[collection], object_id,
            {'schema_version': 1, 'first_fact_id': identity['fact_id']}, expected_revision=0)
    tx._put_sidecar(HEAD_COLLECTIONS[collection], object_id,
        {'schema_version': 1, 'fact_id': identity['fact_id']},
        expected_revision=head.revision if head is not None else 0)
    return identity


def record_created(tx, record):
    """Called solely after a real original INSERT in SQLite UOW.put."""
    if record.collection not in HEAD_COLLECTIONS:
        return
    head = _head(tx, record.collection, record.object_id)
    _append(tx, record.collection, record.object_id, record.revision, 'created', head)


def capture_lineage(tx, collection, object_id):
    """Capture an intact head or adopt one existing record in this writer TX.

    A current_anchor records the revision observed now. It is never presented
    as a historical birth, and no existing broken proof is repaired here.
    """
    try:
        _key(collection, object_id)
        record = tx.read(collection, object_id)
        if record is None or type(record.revision) is not int or record.revision < 1:
            _invalid()
        head = _head(tx, collection, object_id)
        if head is not None:
            return {'collection': collection, 'object_id': object_id, 'fact_id': head.payload['fact_id']}
        return _append(tx, collection, object_id, record.revision, 'current_anchor', None)
    except Exception:
        tx.rollback()
        raise


def verify_lineage(reader, identity):
    """Read-only continuity check; leave revision eligibility to the owner."""
    if not isinstance(identity, Mapping) or set(identity) != _IDENTITY:
        _invalid()
    collection, object_id = identity['collection'], identity['object_id']
    _key(collection, object_id)
    _fact_id(identity['fact_id'])
    record = reader.read(collection, object_id)
    if record is None or type(record.revision) is not int or record.revision < 1:
        _invalid()
    head = _head(reader, collection, object_id)
    if head is None or head.payload['fact_id'] != identity['fact_id']:
        _invalid()
