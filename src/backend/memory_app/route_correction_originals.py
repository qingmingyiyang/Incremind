from copy import deepcopy
from uuid import uuid4

from backend.recognition import RecognitionConflict, WorkScope
from .route_original_metadata import PREFIX, route_original_id
from core.storage_provider.record_lineage import capture_lineage, verify_lineage, FACTS

ORIGINALS = 'v2_route_correction_originals'
_LINEAGE_COLLECTIONS = {'original_item': 'workspace_items', 'experience': 'recognition_experiences', 'recognition': 'recognitions'}


def capture_member_lineage(records, tx, member):
    from .transaction_records import TransactionRecords
    from .source_egress import SourceEgressService
    row = tx.read('v2_turns', member['turn_id'])
    if row is None or row.revision != member['revision']:
        raise RecognitionConflict('route_original_selection_changed')
    member['lineage'] = capture_lineage(tx, 'v2_turns', row.object_id)
    reader = TransactionRecords(tx)
    snapshots = _source_snapshots(records, reader, row, lambda own, refs: SourceEgressService(records, reader=reader).snapshot(own, refs))
    identities = {}
    def collect(snapshot):
        for node in snapshot['nodes']:
            collection = _LINEAGE_COLLECTIONS.get(node['type'])
            if collection is not None:
                identities[(collection, node['id'])] = capture_lineage(tx, collection, node['id'])
            for nested in node.get('research_style_sources', []):
                collect(nested)
    for snapshot in snapshots:
        collect(snapshot)
    member['material_lineages'] = list(identities.values())


def birth_identity(source):
    value = source.payload
    result = {key: value[key] for key in ('project_id', 'input_kind', 'created_at')}
    if value['input_kind'] == 'text':
        result['input'] = {'source_text': value['source_text']}
    elif value['input_kind'] == 'link':
        result['input'] = {key: value[key] for key in ('url', 'platform', 'content_kind', 'media_request_url') if key in value}
    else:
        raise RecognitionConflict('route_original_input_kind_invalid')
    return result


def _operation(reader, payload):
    from .route_correction_failures import CLOSURES
    if reader.read(CLOSURES, payload['child_id']) is not None:
        raise RecognitionConflict('route_correction_execution_closed')
    operation = reader.read('v2_route_correction_operations', payload['operation_id'])
    if (operation is None or operation.payload.get('status') not in {'running', 'completed', 'partial'}
            or operation.payload.get('source_origin', {}).get('kind') != 'personal'):
        raise RecognitionConflict('route_original_operation_unavailable')
    value = operation.payload
    if value.get('schema_version') not in {2, 3}:
        raise RecognitionConflict('route_original_operation_birth_unavailable')
    index = payload['part_index']
    if (type(index) is not int or not 0 <= index < len(value['plan']['parts'])
            or value['plan']['parts'][index]['intent'] != 'remember'):
        raise RecognitionConflict('route_original_part_changed')
    marker = reader.read('v2_route_corrections', value['source_turn_id'])
    before = reader.read('v2_turns', value['source_turn_id'])
    if (marker is None or marker.payload['operation_id'] != operation.object_id
            or before is None or before.revision != value['source_revision']):
        raise RecognitionConflict('route_original_source_changed')
    return operation


def _source_snapshots(records, reader, row, snapshot_for):
    from .v2.route_source_statements import statement_source
    from .kernel.receipt_projection import frozen_public_turn_requests
    from backend.recognition.external_turn_facts import source_store
    from .transaction_records import TransactionRecords
    value = row.payload
    if value['intent'] == 'remember':
        identity = value['receipt']['remember']['item_id']
        source = reader.read('workspace_items', identity)
        if source is None or source.payload['project_id'] != value['project_id']:
            raise RecognitionConflict('route_original_selected_item_missing')
        return [snapshot_for(WorkScope('local-user', value['project_id']),
            [{'type': 'original_item', 'id': identity, 'revision': source.revision}])]
    if value['intent'] == 'inspiration':
        statement = statement_source(reader, row, source_owner=records)
        return [snapshot_for(WorkScope('local-user', value['project_id']), statement['refs'])]
    entries = frozen_public_turn_requests(records, source_store(records).root.parent, row.object_id,
        reader=reader if isinstance(reader, TransactionRecords) else None)
    if len(entries) != 1 or entries[0]['public_revision'] != row.revision:
        raise RecognitionConflict('route_original_native_source_changed')
    return [snapshot_for(WorkScope(**frozen['scope']), frozen['roots'])
        for frozen in entries[0]['native_request']['privacy']['source_snapshots']]


def _selections(reader, payload, operation):
    value = operation.payload
    expected = value['plan']['parts'][payload['part_index']]['source_members']
    fields = {'turn_id', 'revision', 'project_id', 'namespace', 'kind', 'lineage', 'material_lineages'}
    if (not isinstance(payload['selections'], list) or not payload['selections']
            or any(not isinstance(item, dict) or set(item) != fields for item in payload['selections'])
            or [item['turn_id'] for item in payload['selections']] != [member['turn_id'] for member in expected]):
        raise RecognitionConflict('route_original_selection_changed')
    result = []
    for selection, member in zip(payload['selections'], expected):
        old = reader.read('v2_turns', selection['turn_id'])
        if (old is None or old.revision != member['revision'] or type(selection['revision']) is not int
                or selection['revision'] != old.revision or selection['project_id'] != old.payload['project_id']
                or selection['kind'] != old.payload['intent'] or selection['namespace'] != value['source_origin']['namespace']):
            raise RecognitionConflict('route_original_selection_changed')
        if selection['lineage'] != member['lineage'] or selection['material_lineages'] != member['material_lineages']:
            raise RecognitionConflict('route_original_selection_changed')
        verify_lineage(reader, selection['lineage'])
        for lineage in selection['material_lineages']:
            verify_lineage(reader, lineage)
        result.append(old)
    return result


def _snapshots(records, reader, payload, selected, snapshot_for=None):
    from .source_egress import SourceEgressService
    from .source_snapshot import _closure_identity
    current_snapshot = snapshot_for or (lambda own, refs: SourceEgressService(records, reader=reader if reader is not records else None).snapshot(own, refs))
    current = [snapshot for row in selected for snapshot in _source_snapshots(records, reader, row, current_snapshot)]
    if not isinstance(payload['snapshots'], list) or len(current) != len(payload['snapshots']):
        raise RecognitionConflict('route_original_closure_changed')
    for actual, frozen in zip(current, payload['snapshots']):
        if (actual['scope'] != frozen['scope'] or actual['roots'] != frozen['roots']
                or {_closure_identity(node) for node in actual['nodes']} != {_closure_identity(node) for node in frozen['nodes']}):
            raise RecognitionConflict('route_original_closure_changed')
    return current


def _staged_marker(reader, created):
    route_original_id(created.object_id)
    marker = reader.read(ORIGINALS, created.object_id)
    fields = {'schema_version', 'item_id', 'birth_revision', 'birth', 'birth_lineage', 'operation_id', 'part_index', 'child_id', 'selections', 'snapshots'}
    if (marker is None or marker.revision != 1 or set(marker.payload) != fields
            or marker.payload['schema_version'] != 2 or marker.payload['item_id'] != created.object_id
            or type(marker.payload['birth_revision']) is not int or marker.payload['birth_revision'] != 1
            or marker.payload['birth'] != birth_identity(created)):
        raise RecognitionConflict('route_original_birth_changed')
    if (marker.payload['birth_lineage'].get('collection') != 'workspace_items'
            or marker.payload['birth_lineage'].get('object_id') != created.object_id):
        raise RecognitionConflict('route_original_birth_changed')
    verify_lineage(reader, marker.payload['birth_lineage'])
    fact = reader.read(FACTS, marker.payload['birth_lineage']['fact_id'])
    if fact.payload['origin'] != 'created' or fact.payload['observed_revision'] != 1:
        raise RecognitionConflict('route_original_birth_changed')
    operation = _operation(reader, marker.payload)
    _selections(reader, marker.payload, operation)
    return marker


def validate_staged_binding(reader, created, *, records=None):
    marker = _staged_marker(reader, created)
    operation = _operation(reader, marker.payload)
    _snapshots(records if records is not None else reader, reader, marker.payload,
        _selections(reader, marker.payload, operation))
    return marker


def validate_original_binding(records, scope, source, *, reader=None, snapshot_for=None):
    from .transaction_records import TransactionRecords
    actual = TransactionRecords(reader) if reader is not None else records
    marker = _staged_marker(actual, source)
    payload = marker.payload
    operation = _operation(actual, payload)
    value = operation.payload
    child = actual.read('v2_turns', payload['child_id'])
    if (scope != WorkScope('local-user', source.payload['project_id']) or child is None
            or child.payload.get('parent_turn_id') != value['replacement_turn_id']
            or child.payload.get('project_id') != scope.project_id or child.payload.get('intent') != 'remember'
            or child.payload.get('item_id') != source.object_id):
        raise RecognitionConflict('route_original_child_changed')
    current = _snapshots(records, actual, payload, _selections(actual, payload, operation), snapshot_for)
    return {'snapshots': current, 'projects': sorted({selection['project_id'] for selection in payload['selections']}),
        'metadata': {'route_original_revision': marker.revision, 'route_correction_operation_id': operation.object_id,
            'route_correction_part_index': payload['part_index'], 'route_correction_child_id': payload['child_id'],
            'route_original_birth_revision': payload['birth_revision']}}


class RouteOriginalBinding:
    def __init__(self, records, origins, operation_id, part_index):
        self.records, self.origins = records, origins
        self.operation_id, self.part_index = operation_id, part_index

    def for_child(self, child_id):
        from .v2.route_source_statements import statement_source
        from .source_egress import SourceEgressService
        operation = self.records.read('v2_route_correction_operations', self.operation_id)
        value = operation.payload
        part = value['plan']['parts'][self.part_index]
        selections, snapshots = [], []
        for member in part['source_members']:
            row = self.records.read('v2_turns', member['turn_id'])
            if row is None or row.revision != member['revision']:
                raise RecognitionConflict('route_original_selection_changed')
            origin = self.origins(row.object_id)
            if origin['kind'] != 'personal' or origin['namespace'] != value['source_origin']['namespace']:
                raise RecognitionConflict('route_original_namespace_changed')
            selections.append({'turn_id': row.object_id, 'revision': row.revision, 'project_id': row.payload['project_id'],
                'namespace': origin['namespace'], 'kind': row.payload['intent'],
                'lineage': deepcopy(member['lineage']), 'material_lineages': deepcopy(member['material_lineages'])})
            if row.payload['intent'] == 'remember':
                identity = row.payload['receipt']['remember']['item_id']
                old = self.records.read('workspace_items', identity)
                snapshots.append(SourceEgressService(self.records).snapshot(WorkScope('local-user', row.payload['project_id']),
                    [{'type': 'original_item', 'id': identity, 'revision': old.revision}]))
            elif row.payload['intent'] == 'inspiration':
                snapshots.append(SourceEgressService(self.records).snapshot(WorkScope('local-user', row.payload['project_id']),
                    statement_source(self.records, row)['refs']))
            else:
                native = self.origins._request(row)
                snapshots.extend(deepcopy(native['native_request']['privacy']['source_snapshots']))
        return _CreatedBinding(self.records, self.operation_id, self.part_index, child_id, selections, snapshots)


class _CreatedBinding:
    def __init__(self, records, operation_id, part_index, child_id, selections, snapshots):
        self.records = records
        self.created_item_id = PREFIX + uuid4().hex
        self.operation_id, self.part_index, self.child_id = operation_id, part_index, child_id
        self.selections, self.snapshots = selections, snapshots

    def __call__(self, tx, created):
        payload = {'schema_version': 2, 'item_id': created.object_id, 'birth_revision': created.revision,
            'birth': birth_identity(created), 'operation_id': self.operation_id, 'part_index': self.part_index,
            'birth_lineage': capture_lineage(tx, 'workspace_items', created.object_id),
            'child_id': self.child_id, 'selections': deepcopy(self.selections), 'snapshots': deepcopy(self.snapshots)}
        operation = _operation(tx, payload)
        for selected in payload['selections']:
            row = tx.read('v2_turns', selected['turn_id'])
            if row is None or row.revision != selected['revision']:
                raise RecognitionConflict('route_original_selection_changed')
        target_project = (operation.payload['plan']['parts'][self.part_index]['target_scope']['project_id']
            if operation.payload.get('schema_version') == 3 else operation.payload['project_id'])
        if created.object_id != self.created_item_id or created.payload['project_id'] != target_project:
            raise RecognitionConflict('route_original_destination_changed')
        tx.put(ORIGINALS, created.object_id, payload, expected_revision=0)
        from .transaction_records import TransactionRecords
        validate_staged_binding(TransactionRecords(tx), created, records=self.records)
