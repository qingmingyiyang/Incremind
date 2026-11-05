from copy import deepcopy
import json

from core.ai_kernel.agent_contracts import (
    agent_budget_reservation_from_payload, agent_child_link_from_payload, agent_child_link_to_payload,
    agent_run_from_payload,
)
from core.ai_kernel.contracts import validate_turn_request
from core.ai_kernel.product_admission import ADMISSION_KINDS, MANIFEST_KIND
from core.ai_kernel.product_snapshot import admission_base_refs, restore_admission_snapshot
from core.storage_provider.record_lineage import FACTS, verify_lineage


KINDS = {'document': 'documents', 'workspace_item': 'workspace_items'}
PREFIX = 'v2_material_logical_scope_'
ORIGIN_KIND = 'material-origin-v1'


def _invalid():
    from .service import RecognitionConflict
    raise RecognitionConflict('material_logical_scope_unavailable')


def native_material_origin(records, turn_id):
    request, _, origin = native_material_scope(records, turn_id)
    return request, origin


def _native_material_request(connection, turns, turn_id):
    row = connection.execute('SELECT request_json FROM ai_turns WHERE turn_id=?', (turn_id,)).fetchone()
    if row is None:
        if any(connection.execute('SELECT 1 FROM ' + table + ' WHERE turn_id=? LIMIT 1', (turn_id,)).fetchone()
               for table in ('ai_turn_immutable_payloads', 'ai_turn_events', 'ai_turn_run_leases')):
            _invalid()
        return None, [], []
    request = validate_turn_request(json.loads(row[0]))
    if request['turn_id'] != turn_id or turns.get_request(turn_id) != request:
        _invalid()
    immutable = {}
    for kind in (MANIFEST_KIND, *sorted(ADMISSION_KINDS)):
        item = connection.execute('SELECT payload_ref,payload_json FROM ai_turn_immutable_payloads '
            'WHERE turn_id=? AND kind=?', (turn_id, kind)).fetchone()
        if item is not None:
            immutable[kind] = (item[0], json.loads(item[1]))
    admission_base_refs(request, immutable)
    bundle = restore_admission_snapshot(request, immutable) if MANIFEST_KIND in immutable else None
    selected = [] if bundle is None else [item for item in bundle.items if item.kind == ORIGIN_KIND]
    markers = [ref for ref in request['input']['refs'] if ref['uri'].startswith('crp://material-origin/')]
    return request, selected, markers


def _accepted_material_turn(connection, request):
    row = connection.execute('SELECT event_json FROM ai_turn_events WHERE turn_id=? AND sequence=1',
        (request['turn_id'],)).fetchone()
    event = json.loads(row[0]) if row is not None else {}
    if (event.get('type') != 'turn.accepted' or event.get('sequence') != 1
            or event.get('turn_id') != request['turn_id'] or event.get('session_id') != request['session_id']
            or event.get('correlation', {}).get('operation_id') != request['operation_id']):
        _invalid()


def _material_run(connection, turn_id):
    row = connection.execute('SELECT run_id,project_id,parent_run_id,payload_json FROM ai_agent_runs WHERE turn_id=?',
        (turn_id,)).fetchone()
    if row is None:
        return None
    run = agent_run_from_payload(json.loads(row[3]))
    if tuple(row)[:3] != (run.run_id, run.project_id, run.parent_run_id) or run.turn_id != turn_id:
        _invalid()
    return run


def _finalized_material_child(connection, request, run, parent_id):
    binding = request.get('agent_binding', {})
    fields = ('run_id', 'role', 'profile_id', 'profile_revision', 'model_tier', 'parent_run_id', 'depth',
        'cancel_epoch', 'budget_snapshot_ref')
    if (run.role != 'subagent' or run.parent_run_id != parent_id
            or any(binding.get(key) != getattr(run, key) for key in fields)
            or run.project_id != request['scope']['project_id'] or request['desired_outcome'] != 'project.task'):
        _invalid()
    link_row = connection.execute('SELECT parent_run_id,child_run_id,project_id,status,payload_json '
        'FROM ai_agent_child_links WHERE link_id=?', (binding.get('link_id'),)).fetchone()
    budget_row = connection.execute('SELECT parent_run_id,child_run_id,project_id,status,payload_json '
        'FROM ai_agent_budget_reservations WHERE reservation_id=?', (binding.get('reservation_id'),)).fetchone()
    if link_row is None or budget_row is None:
        _invalid()
    link = agent_child_link_from_payload(json.loads(link_row[4]))
    budget = agent_budget_reservation_from_payload(json.loads(budget_row[4]))
    target = (parent_id, run.run_id, run.project_id)
    if (tuple(link_row)[:4] != (*target, link.status) or tuple(budget_row)[:4] != (*target, budget.status)
            or (link.parent_run_id, link.child_run_id, link.parent_project_id) != target
            or (budget.parent_run_id, budget.child_run_id, budget.project_id) != target
            or link.child_project_id != run.project_id or link.link_id != binding.get('link_id')
            or budget.reservation_id != binding.get('reservation_id') or budget.status not in {'reserved', 'settled'}
            or link.status == 'reserved' or link.spawn_operation_id != request['operation_id']
            or binding.get('spawn_operation_id') != link.spawn_operation_id or budget.operation_id != link.spawn_operation_id
            or link.parent_cancel_epoch != run.cancel_epoch or budget.parent_cancel_epoch != run.cancel_epoch
            or link.child_depth != run.depth or link.delegated_capability_ids != run.capability_ids
            or link.delegated_budget != run.budget_limit or budget.reserved_budget != run.budget_limit):
        _invalid()
    # 原正常登记和冷启动续接都保留同一份正式的子轮归属事实。
    facts = list(connection.execute('SELECT kind,request_json,result_json FROM ai_agent_operations '
        'WHERE operation_id IN (?,?)', (link.spawn_operation_id + '.finalize', 'recovery-finalize-' + request['turn_id'])))
    if not facts or any(kind != 'finalize_spawn' or json.loads(arguments) != {'link_id': link.link_id,
            'cancel_epoch': link.parent_cancel_epoch} or json.loads(result) != {
                **agent_child_link_to_payload(link), 'status': 'spawned'} for kind, arguments, result in facts):
        _invalid()
    _accepted_material_turn(connection, request)


def _inherited_material_scope(connection, turns, request, markers):
    chain, seen = [], set()
    current, run = request, _material_run(connection, request['turn_id'])
    if run is None or run.parent_run_id is None:
        if markers:
            _invalid()
        return request, None
    while run.parent_run_id is not None:
        parent_id = run.parent_run_id
        if parent_id in seen or len(seen) >= 8:
            _invalid()
        seen.add(parent_id)
        row = connection.execute('SELECT turn_id FROM ai_agent_runs WHERE run_id=?', (parent_id,)).fetchone()
        if row is None:
            _invalid()
        parent, selected, parent_markers = _native_material_request(connection, turns, row[0])
        parent_run = _material_run(connection, row[0])
        if (parent is None or parent_run is None or parent_run.run_id != parent_id
                or parent_run.project_id != current['scope']['project_id'] or parent_run.depth + 1 != run.depth
                or parent['scope'] != current['scope'] or parent['privacy'] != current['privacy']
                or any(ref not in parent['input']['refs'] for ref in current['input']['refs'])):
            _invalid()
        chain.append((current, run, parent_id))
        current, run = parent, parent_run
    if not markers and not selected and not parent_markers:
        return request, None
    if (len(selected) != 1 or markers != [selected[0].ref] or parent_markers != markers
            or current['desired_outcome'] != 'project.task' or run.role != 'main'
            or current.get('agent_binding', {}).get('role') != 'main'
            or current['agent_binding'].get('run_id') != run.run_id or run.depth != 0):
        _invalid()
    for child, child_run, parent_id in chain:
        _finalized_material_child(connection, child, child_run, parent_id)
    _accepted_material_turn(connection, current)
    return current, selected[0]


def native_material_scope(records, turn_id):
    from .external_turn_facts import _readonly, source_store
    from .kernel_read_facts import authority_facts
    database = source_store(records).root / 'ai-turns.sqlite3'
    if not database.is_file():
        return None, None, None
    turns, _ = authority_facts(database)
    with _readonly(database) as connection:
        request, selected, markers = _native_material_request(connection, turns, turn_id)
        if request is None:
            return None, None, None
        authority = request
        if selected:
            if len(selected) != 1 or not markers:
                _invalid()
            item = selected[0]
        elif markers or request.get('agent_binding', {}).get('parent_run_id'):
            authority, item = _inherited_material_scope(connection, turns, request, markers)
        else:
            item = None
    if item is None:
        return request, authority, None
    origin = {'kind': item.kind, 'ref': deepcopy(item.ref), 'scope': deepcopy(item.scope),
        'payload': deepcopy(item.payload)}
    _origin_scope(authority, origin)
    return request, authority, origin


def _origin_scope(request, origin):
    if (not isinstance(origin, dict) or set(origin) != {'kind', 'ref', 'scope', 'payload'}
            or origin['kind'] != ORIGIN_KIND or request['input']['refs'].count(origin['ref']) != 1):
        _invalid()
    value, declared = origin['payload'], origin['scope']
    if (not isinstance(value, dict) or value.get('schema_version') != 1
            or not isinstance(value.get('execution'), dict) or not isinstance(value.get('shared'), dict)):
        _invalid()
    execution, shared = value['execution'], value['shared']
    if (set(execution) != {'owner_user_id', 'namespace_id', 'namespace', 'root_identity'}
            or any(not isinstance(execution[key], str) or not execution[key]
                for key in ('owner_user_id', 'namespace_id', 'namespace'))
            or execution['namespace'] != '/'.join(('personal', execution['owner_user_id'], execution['namespace_id']))
            or declared != {'turn_id': request['turn_id'], 'project_id': request['scope']['project_id'],
                'owner_user_id': execution['owner_user_id'], 'namespace_id': execution['namespace_id'], 'origin_kind': 'personal'}
            or not isinstance(shared.get('namespace'), str) or not shared['namespace']
            or not isinstance(shared.get('lease'), dict)
            or shared['lease'].get('project_id') != request['scope']['project_id']
            or shared['lease'].get('actor_user_id') != value.get('actor_user_id')):
        _invalid()
    return execution, shared


def _birth(reader, descriptor):
    kind, identity = descriptor['material_type'], descriptor['identity']
    if kind not in KINDS:
        _invalid()
    birth = descriptor['birth']
    if (not isinstance(birth, dict) or birth.get('collection') != KINDS[kind]
            or birth.get('object_id') != identity or type(birth.get('revision')) is not int
            or birth['revision'] != 1):
        _invalid()
    verify_lineage(reader, birth['lineage'])
    fact = reader.read(FACTS, birth['lineage']['fact_id'])
    row = reader.read(KINDS[kind], identity)
    if (fact.payload['origin'] != 'created' or fact.payload['observed_revision'] != 1
            or row is None or row.payload.get('project_id') != descriptor['project_id']):
        _invalid()
    if kind == 'document':
        revision = reader.read('document_revisions', identity + '~r1')
        markdown = reader.read('document_markdown', identity + '~r1')
        refs = revision.payload.get('source_snapshot', {}).get('source_refs') if revision else None
        if (revision is None or markdown is None or revision.revision != birth['revision_record_revision']
                or markdown.revision != birth['markdown_record_revision']
                or revision.payload.get('document_id') != identity or revision.payload.get('revision') != 1
                or markdown.payload.get('document_id') != identity or markdown.payload.get('revision') != 1
                or refs != birth['source_refs'] or row.payload.get('type') != birth['document_type']):
            _invalid()
        if 'source_item_id' in birth:
            source = _confirmed_source_scope(reader, birth['source_id'], descriptor['project_id'],
                descriptor['execution']['namespace_id'])
            operation = reader.read('workspace_confirmation_operations', birth['confirmation_operation_id'])
            if (source is None or operation is None or operation.revision != birth['confirmation_revision']
                    or operation.payload.get('document_id') != identity
                    or operation.payload.get('workspace_item_id') != birth['source_item_id']
                    or source['origin'] != descriptor['origin']
                    or source['birth']['parent_lineage'] != birth['parent_lineage']
                    or revision.payload['source_snapshot']['source_refs'] != operation.payload.get('source_refs')
                    or markdown.payload.get('markdown') != operation.payload.get('markdown')):
                _invalid()
        elif 'source_document_id' in birth:
            operation = reader.read('v2_outcome_restorations', refs[0]['source_id'])
            parent = read_material_logical_scope(reader, material_type='document', identity=birth['source_document_id'],
                project_id=descriptor['project_id'], namespace_id=descriptor['execution']['namespace_id'])
            if (operation is None or operation.revision != birth['restoration_revision']
                    or operation.payload.get('document_id') != identity
                    or operation.payload.get('source_document_id') != birth['source_document_id']
                    or operation.payload.get('project_id') != descriptor['project_id']
                    or parent is None or parent['origin'] != descriptor['origin']
                    or parent['native_turn_id'] != descriptor['native_turn_id']
                    or refs[0]['locator'] != 'outcome://' + operation.object_id):
                _invalid()
        elif refs != [{'source_id': descriptor['native_turn_id'], 'locator': birth['source_locator']}]:
            _invalid()
    elif row.payload.get('origin') != 'search' or row.payload.get('search_turn_id') != descriptor['native_turn_id']:
        _invalid()
    return row


def _unmarked_origin(reader, material_type, identity, seen=()):
    if identity in seen:
        _invalid()
    if material_type == 'workspace_item':
        item = reader.read('workspace_items', identity)
        return item.payload.get('search_turn_id') if item is not None and item.payload.get('origin') == 'search' else None
    if material_type == 'document':
        revision = reader.read('document_revisions', identity + '~r1')
        refs = revision.payload.get('source_snapshot', {}).get('source_refs') if revision else None
        if isinstance(refs, list) and len(refs) == 1 and isinstance(refs[0], dict):
            turn = refs[0].get('source_id')
            if isinstance(turn, str) and refs[0].get('locator') in {'task://' + turn, 'conversation://' + turn}:
                return turn
            if isinstance(turn, str) and refs[0].get('locator') == 'outcome://' + turn:
                marker = reader.read('v2_outcome_restorations', turn)
                if marker is None or marker.payload.get('document_id') != identity:
                    _invalid()
                return _unmarked_origin(reader, 'document', marker.payload['source_document_id'], (*seen, identity))
        document = reader.read('documents', identity)
        if document is None or not str(document.payload.get('type', '')).startswith('agent-result'):
            return None
        operations = [row for row in reader.list('v2_task_draft_operations')
            if row.payload.get('result', {}).get('document_id') == identity]
        identities = {row.payload.get('inputs', {}).get('turn_id') for row in operations}
        if len(identities) > 1:
            _invalid()
        return next(iter(identities)) if identities else None
    _invalid()


def _confirmed_source_scope(reader, identity, project_id, namespace_id):
    from backend.memory_app.original_sources import alias
    from .external_turn_facts import source_store
    from .service import WorkScope
    store = source_store(reader)
    with store.locked('sources', identity):
        body = store.read('sources', identity)
        if body is None or body.get('project_id', 'default') != project_id:
            _invalid()
        marker = reader.read(PREFIX + 'original_source', identity)
        if body.get('identity_method') != 'workspace_confirmation':
            if marker is not None:
                _invalid()
            return None
        parent = body.get('workspace_item_id')
        value = (read_material_logical_scope(reader, material_type='workspace_item', identity=parent,
            project_id=project_id, namespace_id=namespace_id) if isinstance(parent, str) else None)
        if value is None:
            if marker is not None:
                _invalid()
            return None
        if alias(reader, WorkScope('local-user', project_id), identity, body) != ('original_item', parent):
            _invalid()
        result = _marker_value(marker, 'original_source', identity, project_id)
        birth = result['birth']
        if (result['origin'] != value['origin'] or result['execution'] != value['execution']
                or result['native_turn_id'] != value['native_turn_id']
                or birth != {'collection': 'sources', 'object_id': identity, 'revision': 1,
                    'incarnation': store.incarnation('sources', identity),
                    'source_item_id': parent, 'parent_lineage': value['birth']['lineage'],
                    'confirmation_operation_id': body['confirmation_operation_id']}
                or store.revision('sources', identity) != 1):
            _invalid()
        return deepcopy(result)


def _confirmed_document_scope(reader, identity, project_id, namespace_id):
    operations = [row for row in reader.list_matching('workspace_confirmation_operations',
        project_id=project_id, document_id=identity, state='committed')]
    if not operations:
        return None
    if len(operations) != 1:
        _invalid()
    operation = operations[0]
    value = _confirmed_source_scope(reader, operation.payload['source_id'], project_id, namespace_id)
    if value is None:
        return None
    # 共享父原件已经有真实出生，整理稿必须具有原确认事务写下的旁路。
    _invalid()


def _marker_value(marker, material_type, identity, project_id):
    fields = {'schema_version', 'material_type', 'identity', 'project_id', 'scope_kind',
        'execution', 'origin', 'birth', 'native_turn_id'}
    value = marker.payload if marker is not None else {}
    if (marker is None or marker.revision != 1 or set(value) != fields or value['schema_version'] != 1
            or value['material_type'] != material_type or value['identity'] != identity
            or value['project_id'] != project_id or value['scope_kind'] != 'shared'):
        _invalid()
    return value


def read_material_logical_scope(reader, *, material_type, identity, project_id, revision=None, namespace_id=None):
    if material_type == 'original_item':
        material_type = 'workspace_item'
    if material_type == 'original_source':
        value = _confirmed_source_scope(reader, identity, project_id, namespace_id)
        if revision is not None:
            from .external_turn_facts import source_store
            if source_store(reader).revision('sources', identity) != revision:
                _invalid()
        return value
    if material_type not in KINDS:
        _invalid()
    marker = reader.read(PREFIX + material_type, identity)
    if marker is None:
        if material_type == 'document':
            _confirmed_document_scope(reader, identity, project_id, namespace_id)
        turn = _unmarked_origin(reader, material_type, identity)
        if isinstance(turn, str) and native_material_origin(reader, turn)[1] is not None:
            _invalid()
        return None
    value = _marker_value(marker, material_type, identity, project_id)
    row = _birth(reader, value)
    actual_revision = row.payload.get('revision') if material_type == 'document' else row.revision
    if revision is not None and (type(revision) is not int or revision != actual_revision):
        _invalid()
    request, authority, origin = native_material_scope(reader, value['native_turn_id'])
    if (request is None or origin != value['origin'] or request['scope']['project_id'] != project_id
            or material_type == 'workspace_item' and request['desired_outcome'] != 'web.search'
            or material_type == 'document' and not {'source_document_id', 'source_item_id'}.intersection(value['birth'])
            and request['desired_outcome'] not in {'project.task', 'project.answer'}):
        _invalid()
    execution, _ = _origin_scope(authority, origin)
    if (execution != value['execution'] or namespace_id is not None and execution['namespace_id'] != namespace_id
            or getattr(reader, 'user_id', execution['owner_user_id']) != execution['owner_user_id']):
        _invalid()
    return deepcopy(value)


def require_material_logical_scope(reader, *, material_type, identity, project_id, revision=None,
        namespace_id=None, source_request=None, scope_kind='shared'):
    if scope_kind not in {'personal', 'shared'}:
        _invalid()
    value = read_material_logical_scope(reader, material_type=material_type, identity=identity,
        project_id=project_id, revision=revision, namespace_id=namespace_id)
    if scope_kind == 'personal':
        if value is not None:
            _invalid()
        return None
    if value is None:
        _invalid()
    if source_request is not None:
        actual, authority, origin = native_material_scope(reader, source_request['turn_id'])
        if actual != source_request or origin is None:
            _invalid()
        execution, shared = _origin_scope(authority, origin)
        if (execution != value['execution']
                or shared['namespace'] != value['origin']['payload']['shared']['namespace']):
            _invalid()
    return value


def material_scope_visible(reader, *, material_type, identity, project_id, namespace_id=None,
        logical_scope_kind='personal'):
    from .service import RecognitionConflict
    if logical_scope_kind not in {'personal', 'shared'}:
        _invalid()
    try:
        value = read_material_logical_scope(reader, material_type=material_type, identity=identity,
            project_id=project_id, namespace_id=namespace_id)
    except RecognitionConflict:
        return False
    return (value['scope_kind'] if value is not None else 'personal') == logical_scope_kind


def excluded_documents(reader, *, project_id=None, namespace_id=None, logical_scope_kind='personal', document_ids=None):
    from .service import RecognitionConflict
    if logical_scope_kind not in {'personal', 'shared'}:
        _invalid()
    identities = document_ids if document_ids is not None else {row.object_id for row in reader.list('documents')
        if project_id is None or row.payload.get('project_id') == project_id}
    result = set()
    for identity in identities:
        document = reader.read('documents', identity)
        if document is None or project_id is not None and document.payload.get('project_id') != project_id:
            continue
        try:
            value = read_material_logical_scope(reader, material_type='document', identity=identity,
                project_id=document.payload['project_id'], namespace_id=namespace_id)
        except RecognitionConflict:
            result.add(identity)
            continue
        if (value['scope_kind'] if value is not None else 'personal') != logical_scope_kind:
            result.add(identity)
    return frozenset(result)
