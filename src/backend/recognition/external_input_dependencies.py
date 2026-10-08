"""Retained numbered input identity, separate from today's external admission."""
from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import re

from core.storage_provider.record_lineage import verify_lineage
from .external_turn_facts import delivery_facts, encoded, source_store, source_is_committed, IDENTITIES
from .sql_source_identities import KIND as SQL_IDENTITIES

COLLECTION = 'v2_external_input_dependencies'
EXTERNAL_ID = re.compile(r'^experience-external-v1-[0-9a-f]{32}$')
OWNER_FIELDS = frozenset({'external_input_revision', 'external_experience_id'})
FIELDS = OWNER_FIELDS | {'current_source_graph'}
_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$')
_REF = {'turn_id', 'id', 'immutable_ref', 'identities_ref', 'identities_kind', 'outcome_ref', 'completed_sequence'}
_MARKER = {'schema_version', 'owner_id', 'client', 'scope', 'experience_id', 'experience_revision',
    'ownexperience_identity', 'references'}
_COMPANION = {'schema_version', 'owner_id', 'turn_id', 'immutable_ref', 'entries'}
_REPLAY = ContextVar('external_input_replay', default=None)


class ExternalInputDependencyError(ValueError):
    pass


def _invalid():
    raise ExternalInputDependencyError('external_context_evidence_invalid')


@contextmanager
def _replaying(scope=None, experience_id=None, *, trail=(), budget=None):
    parent = _REPLAY.get()
    nodes = budget if budget is not None else (parent['budget'] if parent is not None else set())
    if parent is not None:
        nodes.update(parent['budget'])
    markers = () if parent is None else parent['markers']
    path = trail or (() if parent is None else parent['trail'])
    if scope is not None:
        key = ('material', scope.user_id, scope.project_id, 'experience', experience_id)
        if key in markers or key not in nodes and len(nodes) >= 256:
            _invalid()
        markers = (*markers, key)
        nodes.add(key)
        if key not in path:
            path = (*path, key)
    token = _REPLAY.set({'markers': markers, 'trail': path, 'budget': nodes})
    try:
        yield
    finally:
        if parent is not None:
            parent['budget'].update(nodes)
        _REPLAY.reset(token)


def metadata(value):
    if (not isinstance(value, Mapping) or set(value) != OWNER_FIELDS
            or type(value['external_input_revision']) is not int or value['external_input_revision'] != 1
            or not isinstance(value['external_experience_id'], str)
            or not EXTERNAL_ID.fullmatch(value['external_experience_id'])):
        _invalid()


def json_proof(proof, owner):
    """Only an existing JSON incarnation is birth authority at this checkpoint."""
    if not isinstance(proof, Mapping) or set(proof) != {'material', 'snapshot', 'usage_kind'}:
        _invalid()
    material, snapshot = proof['material'], proof['snapshot']
    if (not isinstance(material, Mapping) or set(material) != {'type', 'id', 'revision', 'project_id'}
            or material['type'] != 'original_source' or proof['usage_kind'] is not None
            or type(material['revision']) is not int or material['revision'] < 1
            or any(not isinstance(material[key], str) or not _ID.fullmatch(material[key]) for key in ('id', 'project_id'))
            or not isinstance(snapshot, Mapping)
            or set(snapshot) != {'schema_version', 'scope', 'privacy_revision', 'roots', 'nodes'}
            or type(snapshot['schema_version']) is not int or snapshot['schema_version'] != 1
            or snapshot['scope'] != {'user_id': owner, 'project_id': material['project_id']}
            or type(snapshot['privacy_revision']) is not int or snapshot['privacy_revision'] < 0
            or snapshot['roots'] != [{'type': 'original_source', 'id': material['id'], 'revision': material['revision']}]
            or type(snapshot['roots'][0]['revision']) is not int
            or not isinstance(snapshot['nodes'], list) or not 1 <= len(snapshot['nodes']) <= 256):
        _invalid()
    seen = set()
    for node in snapshot['nodes']:
        if (not isinstance(node, Mapping) or set(node) != {'type', 'id', 'source_revision',
                'policy_revision', 'effective_purposes', 'incarnation'}
                or node['type'] != 'original_source' or not isinstance(node['id'], str) or not _ID.fullmatch(node['id'])
                or type(node['source_revision']) is not int or node['source_revision'] < 1
                or type(node['policy_revision']) is not int or node['policy_revision'] < 0
                or node['effective_purposes'] != ['embedding', 'generation', 'rerank']
                or not isinstance(node['incarnation'], str) or not re.fullmatch('[0-9a-f]{32}', node['incarnation'])
                or node['id'] in seen):
            _invalid()
        seen.add(node['id'])
    if not any(node['id'] == material['id'] and node['source_revision'] == material['revision'] for node in snapshot['nodes']):
        _invalid()
    return snapshot


def _originals_committed(reader, snapshot):
    # Explicit historical JSON proofs allow only flat original_source nodes.
    store = source_store(reader)
    project = snapshot['scope']['project_id']
    for node in snapshot['nodes']:
        with store.locked('sources', node['id']):
            body = store.read('sources', node['id'])
            if (body is None or body.get('id') != node['id'] or body.get('project_id') != project
                    or store.revision('sources', node['id']) != node['source_revision']
                    or store.incarnation('sources', node['id']) != node['incarnation']
                    or not source_is_committed(store, node['id'], project,
                        node['source_revision'], node['incarnation'])):
                _invalid()


def _reference(reader, scope, client, reference, *, _sql_validator=None):
    if (not isinstance(reference, Mapping) or set(reference) not in ({'turn_id', 'id'}, _REF)
            or any(not isinstance(reference[key], str) or not reference[key] for key in ('turn_id', 'id'))
            or (set(reference) == _REF and reference['identities_kind'] not in {IDENTITIES, SQL_IDENTITIES})):
        _invalid()
    kind = reference['identities_kind'] if set(reference) == _REF else SQL_IDENTITIES
    ref, facts, identities_ref, identities, outcome_ref, sequence = delivery_facts(reader, reference['turn_id'], reference['id'],
        identities_kind=kind)
    if (facts['owner_id'] != scope.user_id or facts['client'] != client
            or facts['scope'] != {'user_id': scope.user_id, 'project_id': scope.project_id}
            or not isinstance(identities, Mapping) or set(identities) != _COMPANION
            or type(identities['schema_version']) is not int or identities['schema_version'] != 1
            or identities['owner_id'] != scope.user_id or identities['turn_id'] != reference['turn_id']
            or identities['immutable_ref'] != ref or not isinstance(identities['entries'], Mapping)
            or reference['id'] not in identities['entries'] or facts['proof'] is None):
        _invalid()
    entry = identities['entries'][reference['id']]
    proof = entry['proof'] if kind == SQL_IDENTITIES else entry
    if encoded(proof) != encoded(facts['proof']):
        _invalid()
    delivery = reader.read('v2_external_agent_deliveries', reference['turn_id'])
    if (delivery is None or delivery.revision != 1 or set(delivery.payload) != {'owner_id', 'turn_id', 'immutable_ref', 'outcome_ref', 'at'}
            or delivery.payload['owner_id'] != scope.user_id or delivery.payload['turn_id'] != reference['turn_id']
            or delivery.payload['immutable_ref'] != ref or delivery.payload['outcome_ref'] != outcome_ref):
        _invalid()
    expected = {'turn_id': reference['turn_id'], 'id': reference['id'], 'immutable_ref': ref,
        'identities_ref': identities_ref, 'identities_kind': kind,
        'outcome_ref': outcome_ref, 'completed_sequence': sequence}
    if set(reference) == _REF and (type(reference['completed_sequence']) is not int or encoded(reference) != encoded(expected)):
        _invalid()
    if kind == SQL_IDENTITIES:
        if not callable(_sql_validator):
            _invalid()
        state = _REPLAY.get()
        entry_bytes, snapshot_bytes = encoded(entry), encoded(proof['snapshot'])
        snapshot = _sql_validator(reader, scope, entry, trail=state['trail'], budget=state['budget'])
        if encoded(entry) != entry_bytes or encoded(snapshot) != snapshot_bytes:
            _invalid()
    else:
        snapshot = json_proof(proof, scope.user_id)
        _originals_committed(reader, snapshot)
    return expected, snapshot


def freeze_references(reader, scope, client, references, *, _sql_validator=None):
    if (client not in {'claude', 'codex'} or not isinstance(references, list) or not 1 <= len(references) <= 256):
        _invalid()
    try:
        with _replaying():
            rows = [_reference(reader, scope, client, ref, _sql_validator=_sql_validator) for ref in references]
    except ExternalInputDependencyError:
        raise
    except Exception:
        _invalid()
    identities = [(row[0]['turn_id'], row[0]['id']) for row in rows]
    if len(identities) != len(set(identities)):
        _invalid()
    nodes = {(row[1]['scope']['project_id'], node['type'], node['id'])
        for row in rows for node in row[1]['nodes']}
    if len(nodes) > 256:
        _invalid()
    return [row[0] for row in rows], tuple(row[1] for row in rows)


@dataclass(frozen=True)
class ExternalInputDependencies:
    revisions: Mapping
    snapshots: tuple


def _read_external_input_dependencies(reader, scope, experience, *, _sql_validator=None):
    marker = reader.read(COLLECTION, experience.object_id)
    if marker is None:
        if EXTERNAL_ID.fullmatch(experience.object_id):
            _invalid()
        return None
    value, provenance = marker.payload, experience.payload.get('provenance', {})
    if (marker.revision != 1 or not isinstance(value, Mapping) or set(value) != _MARKER
            or type(value['schema_version']) is not int or value['schema_version'] != 2
            or value['owner_id'] != scope.user_id or value['scope'] != {'user_id': scope.user_id, 'project_id': scope.project_id}
            or not EXTERNAL_ID.fullmatch(experience.object_id) or value['experience_id'] != experience.object_id
            or type(value['experience_revision']) is not int or value['experience_revision'] != experience.revision
            or experience.payload.get('id') != experience.object_id or experience.payload.get('scope') != value['scope']
            or not isinstance(provenance, Mapping) or provenance.get('kind') != 'user_statement'
            or provenance.get('actor') != value['client']):
        _invalid()
    identity = value['ownexperience_identity']
    if (not isinstance(identity, Mapping) or identity.get('collection') != 'recognition_experiences'
            or identity.get('object_id') != experience.object_id):
        _invalid()
    try:
        verify_lineage(reader, identity)
    except Exception:
        _invalid()
    refs, snapshots = freeze_references(reader, scope, value['client'], value['references'], _sql_validator=_sql_validator)
    if encoded(refs) != encoded(value['references']):
        _invalid()
    return ExternalInputDependencies({'external_input_revision': marker.revision,
        'external_experience_id': experience.object_id}, snapshots)


def read_external_input_dependencies(reader, scope, experience, *, _trail=(), _budget=None, _sql_validator=None):
    with _replaying(scope, experience.object_id, trail=_trail, budget=_budget):
        return _read_external_input_dependencies(reader, scope, experience, _sql_validator=_sql_validator)
