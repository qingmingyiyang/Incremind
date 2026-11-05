from backend.recognition import RecognitionConflict, WorkScope


def dependency_parent(records, parent_id, child_id, *, source_owner=None):
    parent = records.read('v2_turns', parent_id)
    child = records.read('v2_turns', child_id)
    if parent is None or child is None:
        raise RecognitionConflict('part_dependency_parent_changed')
    if child.payload.get('parent_turn_id') == parent_id:
        if child_id not in parent.payload.get('part_turn_ids', []):
            raise RecognitionConflict('part_dependency_parent_changed')
        from .route_correction_scopes import correction_parent_id, planned_child_scope_binding
        if correction_parent_id(parent_id):
            proof = planned_child_scope_binding(records, parent_id, child_id)
            if (proof['target']['project_id'] != child.payload['project_id']
                    or proof['target']['thread_id'] != child.payload['thread_id']):
                raise RecognitionConflict('part_dependency_parent_changed')
        elif parent.payload['project_id'] != child.payload['project_id']:
            from .v2.route_source_statements import statement_source
            statement_source(records, child, source_owner=source_owner)
        return parent_id
    reuse = records.read('v2_route_correction_reuses', parent_id)
    if reuse is None or reuse.revision != 1 or reuse.payload['project_id'] != parent.payload['project_id']:
        raise RecognitionConflict('part_dependency_reuse_missing')
    binding = reuse.payload['parts'].get(child_id)
    operation = records.read('v2_route_correction_operations', reuse.payload['operation_id'])
    source = records.read('v2_turns', reuse.payload['source_turn_id'])
    if (binding is None or child.revision != binding['revision'] or binding['turn_id'] != child_id
            or child.payload.get('parent_turn_id') != binding['parent_turn_id']
            or operation is None or operation.payload.get('replacement_turn_id') != parent_id
            or operation.payload['source_turn_id'] != reuse.payload['source_turn_id']
            or source is None or source.revision != reuse.payload['source_revision']
            or source.payload['project_id'] != parent.payload['project_id']):
        raise RecognitionConflict('part_dependency_reuse_changed')
    original = next((member for row in operation.payload['originals'] for member in row['source_members'] if member['turn_id'] == child_id), None)
    selected = any(member['turn_id'] == child_id for part in operation.payload['plan']['parts'] for member in part['source_members'])
    if original is None or not selected or any(original[key] != binding[key] for key in binding):
        raise RecognitionConflict('part_dependency_reuse_changed')
    if operation.payload.get('schema_version') in {2, 3}:
        from core.storage_provider.record_lineage import verify_lineage
        verify_lineage(records, binding['lineage'])
        for lineage in binding['material_lineages']:
            verify_lineage(records, lineage)
    if source.payload['intent'] == 'multi':
        return dependency_parent(records, source.object_id, child_id, source_owner=source_owner)
    if source.object_id != child_id:
        raise RecognitionConflict('part_dependency_reuse_changed')
    original_parent = binding['parent_turn_id']
    return dependency_parent(records, original_parent, child_id, source_owner=source_owner) if original_parent is not None else None


def correction_native_entries(records, runtime_root, parent_id, native_reader, *, reader=None):
    source = reader if reader is not None else records
    parent = source.read('v2_turns', parent_id)
    if parent is None or parent.payload.get('intent') != 'multi':
        raise RecognitionConflict('part_dependency_parent_changed')
    parts, children = parent.payload['receipt']['parts'], parent.payload['part_turn_ids']
    if len(parts) != len(children) or len(children) != len(set(children)):
        raise RecognitionConflict('part_dependency_parent_changed')
    result = []
    for index, (part, child_id) in enumerate(zip(parts, children)):
        members = part.get('source_turn_ids', [child_id])
        for member_id in members:
            original_parent = dependency_parent(source, parent_id, member_id, source_owner=records)
            for entry in native_reader(records, runtime_root, member_id, reader=reader):
                if entry['parent_id'] != original_parent:
                    raise RecognitionConflict('part_dependency_reuse_changed')
                result.append({'requested_parent_id': parent_id, 'requested_part': index, 'entry': entry})
    return tuple(result)


def explicit_source_ref(turn_id):
    return {'kind': 'session_event', 'object_id': turn_id, 'uri': 'crp://default/v2_turns/' + turn_id}


def validate_explicit_source_binding(records, context, source, statement_reader, *, source_owner=None):
    from .source_graph import SourceGraph, validate_graph
    fields = {'kind', 'turn_id', 'revision', 'project_id', 'namespace', 'ref', 'snapshot', 'text'}
    if (not isinstance(source, dict) or source.get('kind') not in {'inspiration', 'remember'}
            or set(source) != fields | ({'content'} if source['kind'] == 'remember' else set())):
        raise RecognitionConflict('part_explicit_source_invalid')
    identity = source['turn_id']
    dependency_parent(records, context['parent'], identity, source_owner=source_owner)
    row = records.read('v2_turns', identity)
    reuse = records.read('v2_route_correction_reuses', context['parent'])
    operation = records.read('v2_route_correction_operations', reuse.payload['operation_id']) if reuse else None
    from .route_correction_scopes import correction_parent_id, planned_child_scope_binding
    if correction_parent_id(context['parent']):
        recipient = context.get('recipient')
        if not isinstance(recipient, str):
            raise RecognitionConflict('part_explicit_source_recipient_changed')
        admitted = planned_child_scope_binding(records, context['parent'], recipient)
        if operation is None or admitted['operation_id'] != operation.object_id:
            raise RecognitionConflict('part_explicit_source_recipient_changed')
        part = operation.payload['plan']['parts'][admitted['part_index']]
        allowed = {member['turn_id'] for member in part['source_members']}
        for index in part['depends_on']:
            dependency = operation.payload['plan']['parts'][index]
            allowed.update(member['turn_id'] for member in dependency['source_members'])
            allowed.add(operation.payload['children'][index])
        if identity not in allowed or admitted['target']['project_id'] != context.get('project_id'):
            raise RecognitionConflict('part_explicit_source_recipient_changed')
    if source['kind'] == 'remember':
        from .v2.route_source_statements import remembered_source
        statement = remembered_source(records, row)
        reference = statement['ref']
        if statement['content'] != source['content']:
            raise RecognitionConflict('part_explicit_source_changed')
    else:
        statement = statement_reader(records, row, source_owner=source_owner)
        reference = explicit_source_ref(identity)
    if (row is None or type(source['revision']) is not int or row.revision != source['revision'] or row.payload['project_id'] != source['project_id']
            or row.payload['intent'] != source['kind'] or source['ref'] != reference or source['ref'] not in context['refs']
            or operation is None or operation.payload['source_origin']['kind'] != 'personal'
            or operation.payload['source_origin']['namespace'] != source['namespace']):
        raise RecognitionConflict('part_explicit_source_changed')
    snapshot = source['snapshot']
    if (statement['text'] != source['text'] or not isinstance(snapshot, dict)
            or set(snapshot) != {'schema_version', 'scope', 'privacy_revision', 'roots', 'nodes'}
            or type(snapshot['schema_version']) is not int or snapshot['schema_version'] != 1
            or snapshot['scope'] != {'user_id': 'local-user', 'project_id': source['project_id']}
            or snapshot['roots'] != statement['refs']
            or type(snapshot['privacy_revision']) is not int or snapshot['privacy_revision'] < 0):
        raise RecognitionConflict('part_explicit_source_changed')
    graph = SourceGraph()
    graph.snapshot(snapshot)
    validate_graph(graph.result(), 'local-user')
    return snapshot


def validate_explicit_source(records, context, source, statement_reader, *, reader=None):
    from .source_egress import SourceEgressService
    actual = reader if reader is not None else records
    validate_explicit_source_binding(actual, context, source, statement_reader, source_owner=records)
    authority = SourceEgressService(records, reader=reader)
    authority.validate_snapshot(WorkScope('local-user', source['project_id']), source['snapshot'])
    authority.require(source['snapshot'], 'generation')
    return source['snapshot']
