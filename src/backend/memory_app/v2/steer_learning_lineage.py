from copy import deepcopy

from backend.recognition import RecognitionConflict
from core.storage_provider.record_lineage import FACTS, WITNESS_COLLECTIONS, RecordLineageError

from .outcome_freshness import _lineage


CHILD_ACTIONS = frozenset({'split-child', 'merge-child', 'supersede-child'})
VERSION_FIELDS = frozenset({'id', 'recognition_id', 'recognition_revision', 'version', 'action', 'snapshot', 'recorded_at'})


def _parents(value):
    if (not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value)
            or len(value) != len(set(value))):
        raise RecognitionConflict('steer_learning_lineage_parents_invalid')
    return value


def recognition_family(reader, scope, recognition, *, max_nodes):
    own_scope = {'user_id': scope.user_id, 'project_id': scope.project_id}
    pending, active, complete, rows = [(recognition.object_id, False)], set(), set(), {}
    while pending:
        identity, leaving = pending.pop()
        if leaving:
            active.remove(identity)
            complete.add(identity)
            continue
        if identity in active:
            raise RecognitionConflict('steer_learning_lineage_cycle')
        if identity in complete:
            continue
        row = reader.read('recognitions', identity)
        if (row is None or row.payload.get('scope') != own_scope
                or 'project_id' in row.payload and row.payload['project_id'] != scope.project_id):
            raise RecognitionConflict('steer_learning_lineage_scope_unavailable')
        if identity == recognition.object_id and row != recognition:
            raise RecognitionConflict('steer_learning_lineage_row_changed')
        if len(rows) >= max_nodes:
            raise RecognitionConflict('steer_learning_lineage_limit')
        parents = list(_parents(row.payload.get('parent_ids', [])))
        first = reader.read('recognition_versions', identity + '~v1')
        if first is not None and str(first.payload.get('action', '')).endswith('-child'):
            snapshot = first.payload.get('snapshot')
            if not isinstance(snapshot, dict):
                raise RecognitionConflict('steer_learning_lineage_version_invalid')
            parents = list(dict.fromkeys([*parents, *_parents(snapshot.get('parent_ids', []))]))
        rows[identity] = row
        active.add(identity)
        pending.append((identity, True))
        pending.extend((parent, False) for parent in reversed(parents))
    return rows


def _version(reader, recognition, number):
    identity = recognition.object_id + '~v' + str(number)
    row = reader.read('recognition_versions', identity)
    if row is None or row.revision != 1 or set(row.payload) != VERSION_FIELDS:
        raise RecognitionConflict('steer_learning_lineage_version_unavailable')
    value, snapshot = row.payload, row.payload['snapshot']
    if (value['id'] != identity or value['recognition_id'] != recognition.object_id
            or value['version'] != number or type(value['recognition_revision']) is not int
            or not 1 <= value['recognition_revision'] <= recognition.revision
            or not isinstance(snapshot, dict) or snapshot.get('id') != recognition.object_id
            or snapshot.get('scope') != recognition.payload.get('scope')
            or snapshot.get('project_id') != recognition.payload.get('project_id')
            or snapshot.get('version') != number):
        raise RecognitionConflict('steer_learning_lineage_version_invalid')
    return row


def _record_versions(reader, scope, row):
    if (row is None or reader.read('recognitions', row.object_id) != row
            or row.payload.get('scope') != {'user_id': scope.user_id, 'project_id': scope.project_id}
            or row.payload.get('project_id') != scope.project_id):
        raise RecognitionConflict('steer_learning_lineage_row_changed')
    identity, number = row.object_id, row.payload.get('version')
    if type(number) is not int or number < 1:
        raise RecognitionConflict('steer_learning_lineage_version_invalid')
    first, current = _version(reader, row, 1), _version(reader, row, number)
    if (current.payload['recognition_revision'] != row.revision or current.payload['snapshot'] != row.payload
            or first.payload['recognition_revision'] != 1 or first.payload['snapshot'].get('state') != 'active'
            or any(first.payload['snapshot'].get(key) != row.payload.get(key)
                for key in ('id', 'scope', 'project_id', 'created_at', 'published_by', 'parent_ids'))):
        raise RecognitionConflict('steer_learning_lineage_birth_changed')
    parents, action = _parents(row.payload.get('parent_ids')), first.payload['action']
    if (parents and (action not in CHILD_ACTIONS
            or action in {'split-child', 'supersede-child'} and len(parents) != 1
            or action == 'merge-child' and len(parents) < 2)
            or not parents and action != 'publish'):
        raise RecognitionConflict('steer_learning_lineage_birth_action_changed')
    try:
        birth = _lineage(reader, 'recognitions', identity)
    except RecordLineageError as error:
        raise RecognitionConflict('steer_learning_lineage_birth_unavailable') from error
    fact = reader.read(FACTS, birth['fact_id'])
    witness = reader.read(WITNESS_COLLECTIONS['recognitions'], identity)
    if (fact is None or fact.payload.get('origin') != 'created' or fact.payload.get('observed_revision') != 1
            or witness is None or witness.payload.get('first_fact_id') != birth['fact_id']):
        raise RecognitionConflict('steer_learning_lineage_birth_unavailable')
    return first, current, birth


def retained_recognition_record(source_owner, reader, scope, recognition_id, *, expected_birth=None, expected_revision=None):
    from ..source_egress import _source_owner_binding
    from ..transaction_records import TransactionRecords
    view = TransactionRecords(reader)
    _source_owner_binding(source_owner, view)
    row = view.read('recognitions', recognition_id)
    first, _, birth = _record_versions(view, scope, row)
    if expected_birth is not None and birth != expected_birth:
        raise RecognitionConflict('steer_learning_retained_birth_changed')
    if expected_revision is not None:
        if type(expected_revision) is not int or not 1 <= expected_revision <= row.revision:
            raise RecognitionConflict('steer_learning_retained_revision_unavailable')
        matches = [version for version in view.list('recognition_versions')
            if version.payload.get('recognition_id') == recognition_id
            and version.payload.get('recognition_revision') == expected_revision]
        if len(matches) != 1:
            raise RecognitionConflict('steer_learning_retained_revision_unavailable')
        historical = _version(view, row, matches[0].payload['version'])
        if any(historical.payload['snapshot'].get(key) != first.payload['snapshot'].get(key)
            for key in ('id', 'scope', 'project_id', 'created_at', 'published_by', 'parent_ids')):
            raise RecognitionConflict('steer_learning_retained_birth_changed')
    return row, deepcopy(birth)


def validate_family(reader, scope, recognition, rows):
    nodes, versions = {}, {}
    for identity, row in rows.items():
        if identity == recognition.object_id and row != recognition:
            raise RecognitionConflict('steer_learning_lineage_row_changed')
        first, current, birth = _record_versions(reader, scope, row)
        parents = _parents(row.payload.get('parent_ids'))
        nodes[identity] = {'id': identity, 'revision': row.revision, 'birth': birth,
            'birth_version': {'id': first.object_id, 'revision': first.revision},
            'parent_ids': list(parents), 'transitions': []}
        versions[identity] = current
    for identity, node in nodes.items():
        first = reader.read('recognition_versions', identity + '~v1')
        operation = first.payload['action'].removesuffix('-child')
        for parent_id in node['parent_ids']:
            parent = rows.get(parent_id)
            if (parent is None or parent.payload.get('state') != 'superseded'
                    or identity not in _parents(parent.payload.get('successor_ids'))
                    or versions[parent_id].payload['action'] != operation):
                raise RecognitionConflict('steer_learning_lineage_transition_changed')
            transition = versions[parent_id]
            node['transitions'].append({'parent_id': parent_id, 'id': transition.object_id, 'revision': transition.revision})
    return [deepcopy(nodes[identity]) for identity in sorted(nodes)]
