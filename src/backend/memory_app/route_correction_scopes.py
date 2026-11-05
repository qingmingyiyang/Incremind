from pathlib import Path
from copy import deepcopy
from uuid import UUID

from backend.recognition import RecognitionConflict, WorkScope

PARENT_PREFIX = 'turn-route-'
TARGET_FIELDS = frozenset({'project_id', 'scene', 'namespace', 'thread_id'})
PLANNED_FIELDS = frozenset({'part_index', 'target', 'selected_source_ids'})


def correction_parent_id(identity):
    if not isinstance(identity, str) or not identity.startswith(PARENT_PREFIX):
        return False
    suffix = identity[len(PARENT_PREFIX):]
    try:
        parsed = UUID(suffix)
    except ValueError as error:
        raise RecognitionConflict('route_correction_parent_invalid') from error
    if parsed.hex != suffix:
        raise RecognitionConflict('route_correction_parent_invalid')
    return True


def _owner(records, reader):
    from backend.security.audited_records import AuditedRecordStore
    from backend.security.user_context import USER_ACCESS
    from backend.recognition.external_turn_facts import source_store
    from core.storage_provider.sqlite_uow import SQLiteStructuredRecordStore
    from .transaction_records import TransactionRecords
    access = USER_ACCESS.get()
    if (not isinstance(records, (SQLiteStructuredRecordStore, AuditedRecordStore))
            or access is not None and (not isinstance(records, AuditedRecordStore)
                or records.user_id != access.target_user_id)):
        raise RecognitionConflict('route_correction_actual_owner_required')
    if reader is not None and (not isinstance(reader, TransactionRecords) or not reader.connection.in_transaction
            or Path(reader.database_path).resolve() != Path(records.database_path).resolve()):
        raise RecognitionConflict('route_correction_reader_changed')
    namespace = getattr(records, 'namespace', None) or source_store(records).namespace_id
    return 'personal/' + (getattr(records, 'user_id', None) or 'local-user') + '/' + namespace


def source_scope(records, row, *, namespace, reader=None):
    from .kernel.receipt_projection import frozen_public_turn_requests
    from .kernel.route_source_snapshot import native_source_snapshot
    from .kernel.answer_continuations import PREPARATION_KIND, decode
    from .v2.projects import scene_of
    from .v2.insights import resolve_insight, _scene_assignment
    from backend.recognition.external_turn_facts import source_store
    actual = reader if reader is not None else records
    if namespace != _owner(records, reader):
        raise RecognitionConflict('route_correction_namespace_changed')
    value = row.payload
    project, thread_id = value['project_id'], value['thread_id']
    thread = actual.read('v2_threads', thread_id)
    if thread is None or thread.payload.get('project_id') != project:
        raise RecognitionConflict('route_correction_thread_changed')
    if value['intent'] == 'remember':
        item_id = value['receipt']['remember']['item_id']
        item = actual.read('workspace_items', item_id)
        if item is None or item.payload.get('project_id') != project:
            raise RecognitionConflict('route_correction_source_changed')
        assignment = scene_of(actual, 'item', item_id)
        scene = assignment['scene'] if assignment is not None else None
        if assignment is not None and assignment['project_id'] != project:
            raise RecognitionConflict('route_correction_scene_changed')
    elif value['intent'] == 'inspiration':
        scope = WorkScope('local-user', project)
        insight = resolve_insight(actual, scope, value['receipt']['inspiration']['insight']['id'])
        if insight is None:
            raise RecognitionConflict('route_correction_source_changed')
        kind = 'candidate' if insight.collection == 'recognition_candidates' else 'recognition'
        assignment = _scene_assignment(actual, scope, insight, kind)
        if assignment is not None and assignment['project_id'] != project:
            raise RecognitionConflict('route_correction_scene_changed')
        scene = assignment['scene'] if assignment is not None else None
    else:
        entries = frozen_public_turn_requests(records, source_store(records).root.parent, row.object_id, reader=reader)
        if len(entries) != 1 or entries[0]['public_revision'] != row.revision:
            raise RecognitionConflict('route_correction_native_changed')
        entry = entries[0]
        if entry['qualified']:
            if 'scene' not in entry['identity']:
                raise RecognitionConflict('route_correction_scene_unavailable')
            scene = entry['identity']['scene']
        elif value['intent'] == 'ask':
            snapshot = native_source_snapshot(records, row.object_id, reader=reader)
            if snapshot['request'] != entry['native_request']:
                raise RecognitionConflict('route_correction_native_changed')
            saved = snapshot['immutable'].get(PREPARATION_KIND)
            if saved is None:
                raise RecognitionConflict('route_correction_scene_unavailable')
            preparation = saved[1]
            parameters = decode(preparation['parameters'])
            if (preparation['request'] != entry['native_request'] or preparation['turn_id'] != row.object_id
                    or preparation['project_id'] != project or preparation['question'] != entry['native_request']['input']['text']
                    or parameters['project'] != project or parameters['thread_id'] != thread_id
                    or parameters['turn_id'] != row.object_id or 'scene' not in parameters):
                raise RecognitionConflict('route_correction_scene_changed')
            scene = parameters['scene']
        else:
            execution = actual.read('v2_task_executions', row.object_id)
            if execution is None or 'scene' not in execution.payload:
                raise RecognitionConflict('route_correction_scene_unavailable')
            scene = execution.payload['scene']
    return {'project_id': project, 'scene': scene, 'namespace': namespace, 'thread_id': thread_id}


def planned_child_scope_binding(reader, parent_id, child_id):
    from core.storage_provider.record_lineage import verify_lineage
    actual = reader
    if not correction_parent_id(parent_id):
        return None
    parent = actual.read('v2_turns', parent_id)
    marker = actual.read('v2_route_correction_reuses', parent_id)
    if parent is None or marker is None or marker.revision != 1:
        raise RecognitionConflict('route_correction_scope_missing')
    operation = actual.read('v2_route_correction_operations', marker.payload['operation_id'])
    if (operation is None or operation.payload.get('schema_version') != 3
            or operation.payload.get('replacement_turn_id') != parent_id
            or operation.payload.get('planned_scopes') != marker.payload.get('planned_scopes')):
        raise RecognitionConflict('route_correction_scope_changed')
    value = operation.payload
    if (value.get('status') not in {'running', 'completed', 'partial', 'unknown'}
            or marker.payload.get('source_turn_id') != value['source_turn_id']
            or marker.payload.get('source_revision') != value['source_revision']
            or marker.payload.get('project_id') != value['project_id']
            or not isinstance(marker.payload.get('planned_scopes'), dict)):
        raise RecognitionConflict('route_correction_scope_changed')
    source = actual.read('v2_turns', value['source_turn_id'])
    scope = marker.payload['planned_scopes'].get(child_id)
    if (not isinstance(scope, dict) or set(scope) != PLANNED_FIELDS
            or type(scope['part_index']) is not int or not 0 <= scope['part_index'] < len(value['plan']['parts'])
            or not isinstance(scope['target'], dict) or set(scope['target']) != TARGET_FIELDS
            or not isinstance(scope['selected_source_ids'], list) or not scope['selected_source_ids']
            or len(scope['selected_source_ids']) != len(set(scope['selected_source_ids']))):
        raise RecognitionConflict('route_correction_scope_changed')
    if (source is None or source.revision != value['source_revision']
            or parent.payload['project_id'] != source.payload['project_id']
            or parent.payload['thread_id'] != source.payload['thread_id']
            or value['children'][scope['part_index']] != child_id
            or parent.payload['part_turn_ids'][scope['part_index']] != child_id):
        raise RecognitionConflict('route_correction_scope_changed')
    part = value['plan']['parts'][scope['part_index']]
    if scope['target'] != part['target_scope'] or scope['selected_source_ids'] != [member['turn_id'] for member in part['source_members']]:
        raise RecognitionConflict('route_correction_scope_changed')
    child = actual.read('v2_turns', child_id)
    if child is not None and (child.payload.get('parent_turn_id') != parent_id
            or child.payload.get('project_id') != scope['target']['project_id']
            or child.payload.get('thread_id') != scope['target']['thread_id']
            or child.payload.get('intent') != part['intent']):
        raise RecognitionConflict('route_correction_scope_changed')
    selected = []
    for member in part['source_members']:
        original = actual.read('v2_turns', member['turn_id'])
        if original is None or original.revision != member['revision']:
            raise RecognitionConflict('route_correction_source_changed')
        verify_lineage(actual, member['lineage'])
        for lineage in member['material_lineages']:
            verify_lineage(actual, lineage)
        selected.append(original)
    if (scope['target'] != value['originals'][part['target_source']]['scope']
            or not any(item.payload['project_id'] == scope['target']['project_id']
                and item.payload['thread_id'] == scope['target']['thread_id'] for item in selected)):
        raise RecognitionConflict('route_correction_target_changed')
    return {'schema_version': 1, 'operation_id': operation.object_id, 'marker_revision': marker.revision,
        'parent_id': parent_id, 'parent_project_id': parent.payload['project_id'],
        'parent_thread_id': parent.payload['thread_id'], 'child_id': child_id, **deepcopy(scope),
        'required_ref': {'kind': 'session_event', 'object_id': parent_id,
            'uri': 'crp://default/v2_route_correction_reuses/' + parent_id}}


def planned_child_scope(records, parent_id, child_id, *, reader=None):
    actual = reader if reader is not None else records
    proof = planned_child_scope_binding(actual, parent_id, child_id)
    if proof is None:
        return None
    from .route_correction_failures import CLOSURES
    if actual.read(CLOSURES, child_id) is not None:
        raise RecognitionConflict('route_correction_execution_closed')
    namespace = _owner(records, reader)
    operation = actual.read('v2_route_correction_operations', proof['operation_id'])
    if operation.payload['source_origin']['namespace'] != namespace:
        raise RecognitionConflict('route_correction_namespace_changed')
    part = operation.payload['plan']['parts'][proof['part_index']]
    selected = []
    for member in part['source_members']:
        current = source_scope(records, actual.read('v2_turns', member['turn_id']), namespace=namespace, reader=reader)
        if current != member.get('scope'):
            raise RecognitionConflict('route_correction_source_scope_changed')
        selected.append(current)
    if proof['target'] not in selected:
        raise RecognitionConflict('route_correction_target_changed')
    return proof
