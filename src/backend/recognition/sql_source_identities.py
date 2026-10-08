"""Observe selected SQL evidence through its existing domain readers.

This reader has no qualification, permission, write or commit authority. Each
number gets its own instance, so recursive domain memoization cannot omit the
identities of an independently frozen proof. List results are never evidence
until their original owner point-reads the selected record.
"""
from collections.abc import Mapping
from contextlib import contextmanager
from copy import deepcopy
import re

from core.storage_provider.record_lineage import HEAD_COLLECTIONS, capture_lineage
from .external_evidence_json import encoded


KIND = 'external-context-sql-identities-v1'
# Positive rows selected by the existing source/material owners. Settings,
# admission, usage, indexes and negative migration scans are not identities.
AUTHORITY_COLLECTIONS = frozenset({
    'workspace_items', 'recognition_experiences', 'recognitions', 'documents',
    'document_revisions', 'document_markdown', 'workspace_confirmation_operations',
    'workspace_review_intents', 'v2_experience_origins', 'recognition_tasks',
    'recognition_context_packets', 'v2_task_draft_operations', 'v2_task_executions',
    'v2_turns', 'v2_external_input_dependencies', 'v2_research_source_reads',
    'v2_external_agent_deliveries',
})
_COMPANION = {'schema_version', 'owner_id', 'turn_id', 'immutable_ref', 'entries'}
_PROOF = {'material', 'snapshot', 'usage_kind'}
_MATERIAL = {'type', 'id', 'revision', 'project_id'}
_SNAPSHOT = {'schema_version', 'scope', 'privacy_revision', 'roots', 'nodes'}
_IDENTITY = {'collection', 'object_id', 'fact_id'}
_SEGMENT = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$')
_FACT = re.compile(r'^[0-9a-f]{32}$')
_NUMBER = re.compile(r'^[MP][1-9][0-9]*$')
_KINDS = {'original_item': None, 'original_source': None, 'document': 'document', 'recognition': 'insight'}


class SQLSourceIdentityError(ValueError):
    """A fixed error without source content or paths."""


def _invalid():
    raise SQLSourceIdentityError('external_context_evidence_invalid')


class CollectingReader:
    def __init__(self, reader):
        self._reader = reader
        self._selected = set()

    @property
    def connection(self):
        return self._reader.connection

    @property
    def database_path(self):
        return next(row[2] for row in self.connection.execute('PRAGMA database_list') if row[1] == 'main')

    def read(self, collection, object_id):
        row = self._reader.read(collection, object_id)
        if row is not None and collection in AUTHORITY_COLLECTIONS:
            self._selected.add((collection, object_id))
        return row

    def list(self, collection):
        return self._reader.list(collection)

    @contextmanager
    def begin(self):
        yield self

    def selected_keys(self):
        return tuple(sorted(self._selected))


def capture_identities(tx, reader):
    """Anchor the successful selected reads in their original writer TX."""
    if not isinstance(reader, CollectingReader) or reader._reader is not tx:
        _invalid()
    return [capture_lineage(tx, collection, identity) for collection, identity in reader.selected_keys()]


def _proof(proof, owner):
    # Domain readers already validate the complete snapshot and its metadata.
    # This checks the persisted envelope; it grants no source permissions.
    if not isinstance(proof, Mapping) or set(proof) != _PROOF:
        _invalid()
    material, snapshot = proof['material'], proof['snapshot']
    if (not isinstance(material, Mapping) or set(material) != _MATERIAL
            or not isinstance(material['type'], str) or material['type'] not in _KINDS
            or proof['usage_kind'] != _KINDS[material['type']]
            or type(material['revision']) is not int or material['revision'] < 1
            or any(not isinstance(material[key], str) or not _SEGMENT.fullmatch(material[key]) for key in ('id', 'project_id'))
            or not isinstance(snapshot, Mapping) or set(snapshot) != _SNAPSHOT
            or type(snapshot['schema_version']) is not int or snapshot['schema_version'] != 1
            or snapshot['scope'] != {'user_id': owner, 'project_id': material['project_id']}
            or type(snapshot['privacy_revision']) is not int or snapshot['privacy_revision'] < 0
            or not isinstance(snapshot['roots'], list) or not snapshot['roots']
            or not isinstance(snapshot['nodes'], list) or not snapshot['nodes']):
        _invalid()


def validate_companion(value, *, owner_id, turn_id, immutable_ref, mapping):
    """Strictly bind the identity envelope to the original saved archive."""
    if (not isinstance(value, Mapping) or set(value) != _COMPANION
            or type(value['schema_version']) is not int or value['schema_version'] != 1
            or value['owner_id'] != owner_id or value['turn_id'] != turn_id
            or value['immutable_ref'] != immutable_ref
            or any(not isinstance(item, str) or not item for item in (owner_id, turn_id, immutable_ref))
            or not isinstance(mapping, Mapping) or not isinstance(value['entries'], Mapping)
            or set(value['entries']) != set(mapping)):
        _invalid()
    for number, entry in value['entries'].items():
        if (not isinstance(number, str) or not _NUMBER.fullmatch(number)
                or not isinstance(entry, Mapping) or set(entry) != {'proof', 'sql_identities'}):
            _invalid()
        _proof(entry['proof'], owner_id)
        if encoded(entry['proof']) != encoded(mapping[number]) or not isinstance(entry['sql_identities'], list):
            _invalid()
        seen = set()
        for identity in entry['sql_identities']:
            if (not isinstance(identity, Mapping) or set(identity) != _IDENTITY
                    or not isinstance(identity['collection'], str)
                    or identity['collection'] not in AUTHORITY_COLLECTIONS or identity['collection'] not in HEAD_COLLECTIONS
                    or not isinstance(identity['object_id'], str) or not _SEGMENT.fullmatch(identity['object_id'])
                    or not isinstance(identity['fact_id'], str) or not _FACT.fullmatch(identity['fact_id'])):
                _invalid()
            key = (identity['collection'], identity['object_id'])
            if key in seen:
                _invalid()
            seen.add(key)


def make_companion(*, owner_id, turn_id, immutable_ref, mapping, entries):
    value = {'schema_version': 1, 'owner_id': owner_id, 'turn_id': turn_id,
        'immutable_ref': immutable_ref, 'entries': deepcopy(entries)}
    validate_companion(value, owner_id=owner_id, turn_id=turn_id, immutable_ref=immutable_ref, mapping=mapping)
    return value
