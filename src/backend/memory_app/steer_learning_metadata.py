import json
from collections.abc import Mapping

from backend.recognition import RecognitionConflict
from backend.recognition.external_evidence_json import encoded


STEER_LEARNING_OWNER_FIELDS = frozenset({'steer_learning_identity'})
STEER_LEARNING_LINEAGE_FIELDS = frozenset({'steer_learning_lineage_identity'})
STEER_LEARNING_FIELD_SETS = (STEER_LEARNING_OWNER_FIELDS, STEER_LEARNING_LINEAGE_FIELDS)
_IDENTITY_FIELDS = frozenset({'owner', 'candidate_id', 'candidate_birth', 'recognition_birth',
    'auxiliary_turn_id', 'generation_ref', 'proposal_ref', 'candidate_origin_ref', 'publication',
    'feedback_ids', 'input_ref', 'model_input_refs', 'publication_ref'})


def validate_steer_learning_metadata(value, identifier, revision, *, recognition_id,
                                     recognition_revision=None, max_nodes=None):
    if isinstance(value, Mapping) and set(value) == STEER_LEARNING_LINEAGE_FIELDS:
        return _lineage_metadata(value, identifier, revision, recognition_id=recognition_id,
            recognition_revision=recognition_revision, max_nodes=max_nodes)
    if not isinstance(value, Mapping) or set(value) != STEER_LEARNING_OWNER_FIELDS:
        raise RecognitionConflict('steer_learning_graph_owner_invalid')
    raw = value['steer_learning_identity']
    if not isinstance(raw, str):
        raise RecognitionConflict('steer_learning_graph_owner_invalid')
    try:
        identity = json.loads(raw)
        if not isinstance(identity, dict) or set(identity) != _IDENTITY_FIELDS or encoded(identity) != raw:
            raise ValueError('noncanonical learning identity')
    except (TypeError, ValueError) as error:
        raise RecognitionConflict('steer_learning_graph_owner_invalid') from error
    owner = identity['owner']
    if (not isinstance(owner, dict) or set(owner) != {'owner_user_id', 'namespace_id', 'origin_kind'}
            or owner['origin_kind'] != 'personal'):
        raise RecognitionConflict('steer_learning_graph_owner_invalid')
    identifier(owner['owner_user_id'])
    identifier(owner['namespace_id'])
    identifier(identity['candidate_id'])
    identifier(identity['auxiliary_turn_id'])
    if identity['candidate_id'] != 'steer-' + identity['auxiliary_turn_id']:
        raise RecognitionConflict('steer_learning_graph_candidate_invalid')
    for field, collection, object_id in (
            ('candidate_birth', 'recognition_candidates', identity['candidate_id']),
            ('recognition_birth', 'recognitions', recognition_id)):
        birth = identity[field]
        if (not isinstance(birth, dict) or set(birth) != {'collection', 'object_id', 'fact_id'}
                or birth['collection'] != collection or birth['object_id'] != object_id):
            raise RecognitionConflict('steer_learning_graph_birth_invalid')
        identifier(birth['object_id'])
        identifier(birth['fact_id'])
    publication = identity['publication']
    if not isinstance(publication, dict) or set(publication) != {'id', 'revision'}:
        raise RecognitionConflict('steer_learning_graph_publication_invalid')
    identifier(publication['id'])
    revision(publication['revision'])
    input_ref = identity['input_ref']
    if (not isinstance(input_ref, dict) or set(input_ref) != {'collection', 'id', 'revision'}
            or input_ref['collection'] != 'v2_steer_learning_inputs' or input_ref['id'] != identity['auxiliary_turn_id']
            or type(input_ref['revision']) is not int or input_ref['revision'] != 1):
        raise RecognitionConflict('steer_learning_graph_input_invalid')
    for field in ('generation_ref', 'proposal_ref', 'candidate_origin_ref', 'publication_ref'):
        if not isinstance(identity[field], str) or not identity[field]:
            raise RecognitionConflict('steer_learning_graph_input_invalid')
    for field in ('feedback_ids', 'model_input_refs'):
        items = identity[field]
        if (not isinstance(items, list) or not items or any(not isinstance(item, str) or not item for item in items)
                or items != sorted(set(items))):
            raise RecognitionConflict('steer_learning_graph_input_invalid')
    for feedback in identity['feedback_ids']:
        identifier(feedback)
    return identity


def _birth(value, identity, identifier):
    if (not isinstance(value, dict) or set(value) != {'collection', 'object_id', 'fact_id'}
            or value['collection'] != 'recognitions' or value['object_id'] != identity):
        raise RecognitionConflict('steer_learning_lineage_graph_birth_invalid')
    identifier(value['object_id'])
    identifier(value['fact_id'])


def _lineage_metadata(value, identifier, revision, *, recognition_id, recognition_revision, max_nodes):
    raw = value['steer_learning_lineage_identity']
    if not isinstance(raw, str) or type(max_nodes) is not int or max_nodes < 1:
        raise RecognitionConflict('steer_learning_lineage_graph_invalid')
    try:
        identity = json.loads(raw)
        if (not isinstance(identity, dict) or set(identity) != {'owner', 'recognition_id',
                'recognition_revision', 'recognition_birth', 'nodes', 'roots'} or encoded(identity) != raw):
            raise ValueError('noncanonical learning lineage')
    except (TypeError, ValueError) as error:
        raise RecognitionConflict('steer_learning_lineage_graph_invalid') from error
    if identity['recognition_id'] != recognition_id:
        raise RecognitionConflict('steer_learning_lineage_graph_identity_invalid')
    revision(identity['recognition_revision'])
    if recognition_revision is not None and identity['recognition_revision'] != recognition_revision:
        raise RecognitionConflict('steer_learning_lineage_graph_revision_invalid')
    _birth(identity['recognition_birth'], recognition_id, identifier)
    nodes, roots = identity['nodes'], identity['roots']
    if (not isinstance(nodes, list) or not nodes or len(nodes) > max_nodes
            or not isinstance(roots, list) or not roots or len(roots) > max_nodes):
        raise RecognitionConflict('steer_learning_lineage_graph_limit')
    indexed = {}
    for node in nodes:
        if (not isinstance(node, dict) or set(node) != {'id', 'revision', 'birth',
                'birth_version', 'parent_ids', 'transitions'}):
            raise RecognitionConflict('steer_learning_lineage_graph_node_invalid')
        identifier(node['id']); revision(node['revision'])
        _birth(node['birth'], node['id'], identifier)
        birth_version = node['birth_version']
        if (not isinstance(birth_version, dict) or type(birth_version.get('revision')) is not int
                or birth_version != {'id': node['id'] + '~v1', 'revision': 1}):
            raise RecognitionConflict('steer_learning_lineage_graph_version_invalid')
        parents, transitions = node['parent_ids'], node['transitions']
        if (not isinstance(parents, list) or not isinstance(transitions, list)
                or any(not isinstance(item, str) for item in parents) or len(set(parents)) != len(parents)
                or len(transitions) != len(parents)):
            raise RecognitionConflict('steer_learning_lineage_graph_parents_invalid')
        for parent, transition in zip(parents, transitions, strict=True):
            identifier(parent)
            if (not isinstance(transition, dict) or set(transition) != {'parent_id', 'id', 'revision'}
                    or transition['parent_id'] != parent or type(transition['revision']) is not int
                    or transition['revision'] != 1):
                raise RecognitionConflict('steer_learning_lineage_graph_transition_invalid')
            identifier(transition['id'])
        if node['id'] in indexed:
            raise RecognitionConflict('steer_learning_lineage_graph_duplicate')
        indexed[node['id']] = node
    own = indexed.get(recognition_id)
    if (list(indexed) != sorted(indexed) or own is None or not own['parent_ids']
            or own['revision'] != identity['recognition_revision'] or own['birth'] != identity['recognition_birth']):
        raise RecognitionConflict('steer_learning_lineage_graph_identity_invalid')
    seen, active, pending = set(), set(), [(recognition_id, False)]
    while pending:
        current, leaving = pending.pop()
        if leaving:
            active.remove(current)
            seen.add(current)
            continue
        if current in active or current not in indexed:
            raise RecognitionConflict('steer_learning_lineage_graph_cycle')
        if current in seen:
            continue
        active.add(current)
        pending.append((current, True))
        pending.extend((parent, False) for parent in reversed(indexed[current]['parent_ids']))
    if seen != set(indexed):
        raise RecognitionConflict('steer_learning_lineage_graph_unreachable')
    root_ids = []
    for root in roots:
        if not isinstance(root, dict) or set(root) != {'id', 'revision', 'birth', 'origin_identity'}:
            raise RecognitionConflict('steer_learning_lineage_graph_root_invalid')
        identifier(root['id']); revision(root['revision'])
        node = indexed.get(root['id'])
        if (node is None or node['parent_ids'] or root['revision'] != node['revision']
                or root['birth'] != node['birth']):
            raise RecognitionConflict('steer_learning_lineage_graph_root_invalid')
        original = validate_steer_learning_metadata({'steer_learning_identity': root['origin_identity']},
            identifier, revision, recognition_id=root['id'])
        if original['owner'] != identity['owner'] or original['recognition_birth'] != root['birth']:
            raise RecognitionConflict('steer_learning_lineage_graph_owner_invalid')
        root_ids.append(root['id'])
    if root_ids != sorted(set(root_ids)):
        raise RecognitionConflict('steer_learning_lineage_graph_root_invalid')
    return identity
