from copy import deepcopy
from datetime import datetime
from urllib.parse import urlsplit

from core.ai_kernel.product_admission import validate_bundle

from .policies import get


KIND = 'project-constraints-v1'
PRODUCERS = {
    'item-organize': ('memory.organize', 'object', 'item'),
    'document-insights': ('memory.propose_insights', 'object', 'document'),
    'inspirations': ('memory.propose_insights', 'scope', 'document'),
    'consolidation': ('memory.consolidate', 'project', 'document'),
    'overview': ('memory.overview', 'scope', 'document'),
    'skill-export': ('memory.skill_export', 'scope', 'recognition'),
}
OBJECTS = {'item': 'workspace_items', 'document': 'documents',
           'recognition': 'recognitions', 'candidate': 'recognition_candidates'}


def constraint_markers(request):
    return [ref for ref in request['input']['refs'] if ref.get('kind') == 'session_event'
            and (urlsplit(ref.get('uri', '')).path == '/project-constraints'
                 or urlsplit(ref.get('uri', '')).path.startswith('/project-constraints/'))]


def validate_automatic_spec(spec, kind):
    if (not isinstance(spec, dict) or set(spec) != {'producer', 'mode', 'scene', 'anchors'}
            or not isinstance(spec['producer'], str) or spec['producer'] not in PRODUCERS
            or not isinstance(spec['anchors'], list)
            or spec['scene'] is not None and (not isinstance(spec['scene'], str) or not spec['scene'])):
        raise ValueError('automatic_constraint_spec_invalid')
    expected_kind, mode, object_type = PRODUCERS[spec['producer']]
    if kind != expected_kind or spec['mode'] != mode:
        raise ValueError('automatic_constraint_producer_invalid')
    seen = set()
    for anchor in spec['anchors']:
        if (not isinstance(anchor, dict) or set(anchor) != {'type', 'id'}
                or anchor['type'] != object_type or not isinstance(anchor['id'], str) or not anchor['id']
                or anchor['id'] in seen):
            raise ValueError('automatic_constraint_anchor_invalid')
        seen.add(anchor['id'])
    if (mode == 'object' and (len(seen) != 1 or spec['scene'] is not None)
            or mode == 'scope' and not seen
            or mode == 'project' and spec['scene'] is not None):
        raise ValueError('automatic_constraint_scene_invalid')
    return deepcopy(spec)


def _positive(value):
    return type(value) is int and value > 0


def _assignment(value, project):
    return value is None or (isinstance(value, dict) and set(value) == {'project_id', 'scene'}
        and value['project_id'] == project and (value['scene'] is None
            or isinstance(value['scene'], str) and bool(value['scene'])))


def _origin(value):
    if (not isinstance(value, dict) or set(value) != {'kind', 'namespace', 'owner_user_id', 'physical_identity'}
            or value['kind'] not in {'personal', 'shared'}
            or any(not isinstance(value[key], str) or not value[key] for key in ('namespace', 'owner_user_id'))):
        raise ValueError('automatic_constraint_origin_invalid')
    physical = value['physical_identity']
    if (not isinstance(physical, dict) or set(physical) != {'database_path', 'database', 'directory'}
            or not isinstance(physical['database_path'], str) or not physical['database_path']
            or any(not isinstance(physical[key], list) or len(physical[key]) != 2
                or any(type(item) is not int or item < 0 for item in physical[key])
                for key in ('database', 'directory'))):
        raise ValueError('automatic_constraint_origin_invalid')


def validate_producer_identity(producer, request, spec):
    if (not isinstance(producer, dict) or set(producer) != {'collection', 'kind', 'project', 'key'}
            or producer['kind'] != request['desired_outcome'] or producer['project'] != request['scope']['project_id']
            or producer['collection'] not in {'v2_memory_turn_keys', 'workspace_organize_steps'}
            or producer['collection'] == 'workspace_organize_steps' and (
                spec['producer'] != 'item-organize' or not isinstance(producer['key'], str)
                or not producer['key'].startswith('organize-' + spec['anchors'][0]['id'] + '-'))):
        raise ValueError('automatic_constraint_producer_invalid')


def validate_automatic_descriptor(value, request):
    fields = {'schema_version', 'spec', 'producer_identity', 'project_revision', 'scene',
              'scene_source', 'scene_owners', 'assignments', 'dependencies', 'resolved_scenes', 'materials', 'source'}
    if (not isinstance(value, dict) or set(value) not in (fields, fields | {'producer_previous'})
            or type(value['schema_version']) is not int
            or value['schema_version'] != (2 if 'producer_previous' in value else 1)
            or not _positive(value['project_revision']) or value['materials'] != request['privacy']['material_refs']):
        raise ValueError('automatic_constraint_descriptor_invalid')
    project = request['scope']['project_id']
    spec = validate_automatic_spec(value['spec'], request['desired_outcome'])
    producer = value['producer_identity']
    validate_producer_identity(producer, request, spec)
    if 'producer_previous' in value:
        previous = value['producer_previous']
        if (producer['collection'] != 'workspace_organize_steps' or not isinstance(previous, dict)
                or set(previous) != {'collection', 'id', 'revision', 'payload'}
                or previous['collection'] != producer['collection'] or previous['id'] != producer['key']
                or not _positive(previous['revision']) or not isinstance(previous['payload'], dict)
                or not isinstance(previous['payload'].get('turn_id'), str) or not previous['payload']['turn_id']
                or previous['payload']['turn_id'] == request['turn_id']):
            raise ValueError('automatic_constraint_previous_producer_invalid')
    if any(not isinstance(value[key], list) for key in
           ('assignments', 'dependencies', 'scene_owners', 'resolved_scenes')):
        raise ValueError('automatic_constraint_scene_proof_invalid')
    assignments = {}
    for row in value['assignments']:
        if (not isinstance(row, dict) or set(row) != {'collection', 'id', 'revision', 'payload'}
                or row['collection'] not in {'v2_scene_assignments_' + kind for kind in OBJECTS}
                or not isinstance(row['id'], str) or not row['id']
                or type(row['revision']) is not int or row['revision'] < 0
                or (row['revision'] == 0) != (row['payload'] is None)
                or not _assignment(row['payload'], project)
                or (row['collection'], row['id']) in assignments):
            raise ValueError('automatic_constraint_scene_proof_invalid')
        assignments[(row['collection'], row['id'])] = row
    dependencies = {}
    for row in value['dependencies']:
        if (not isinstance(row, dict) or set(row) != {'collection', 'id', 'revision'}
                or row['collection'] not in {*OBJECTS.values(), 'recognition_experiences'}
                or not isinstance(row['id'], str) or not row['id'] or not _positive(row['revision'])
                or (row['collection'], row['id']) in dependencies):
            raise ValueError('automatic_constraint_dependency_invalid')
        dependencies[(row['collection'], row['id'])] = row['revision']
    resolved = {}
    for row in value['resolved_scenes']:
        if (not isinstance(row, dict) or set(row) != {'type', 'id', 'assignment'}
                or row['type'] not in OBJECTS or not _assignment(row['assignment'], project)
                or (row['type'], row['id']) in resolved):
            raise ValueError('automatic_constraint_scene_resolution_invalid')
        resolved[(row['type'], row['id'])] = row['assignment']
    anchors = {(row['type'], row['id']) for row in spec['anchors']}
    if (set(resolved) != anchors
            or any(('v2_scene_assignments_' + kind, identity) not in assignments
                   or (OBJECTS[kind], identity) not in dependencies for kind, identity in anchors)):
        raise ValueError('automatic_constraint_anchor_proof_missing')
    for row in value['scene_owners']:
        if (not isinstance(row, dict) or set(row) != {'id', 'revision', 'document_id'}
                or not isinstance(row['id'], str) or not row['id'] or not _positive(row['revision'])
                or ('workspace_items', row['id']) not in dependencies
                or dependencies[('workspace_items', row['id'])] != row['revision']
                or ('document', row['document_id']) not in anchors):
            raise ValueError('automatic_constraint_scene_owner_invalid')
    if spec['mode'] == 'object':
        anchor = spec['anchors'][0]
        source = value['scene_source']
        if not isinstance(source, dict) or set(source) != {'collection', 'id'}:
            raise ValueError('automatic_constraint_scene_source_invalid')
        key = (source['collection'], source['id'])
        direct = ('v2_scene_assignments_' + anchor['type'], anchor['id'])
        if (key not in assignments or key != direct and (anchor['type'] != 'document'
                or assignments[direct]['revision'] != 0 or source['collection'] != 'v2_scene_assignments_item'
                or not any(row['id'] == source['id'] and row['document_id'] == anchor['id']
                           for row in value['scene_owners']))):
            raise ValueError('automatic_constraint_scene_source_invalid')
        assignment = assignments[key]['payload']
        if assignment != resolved[(anchor['type'], anchor['id'])]:
            raise ValueError('automatic_constraint_scene_resolution_invalid')
        scene = assignment['scene'] if assignment is not None else None
    else:
        if value['scene_source'] is not None:
            raise ValueError('automatic_constraint_scene_source_invalid')
        scene = spec['scene']
        if scene is not None and any(row is None or row['scene'] != scene for row in resolved.values()):
            raise ValueError('automatic_constraint_scope_changed')
    if value['scene'] != scene:
        raise ValueError('automatic_constraint_scene_resolution_invalid')
    return spec


def automatic_bundle(request, admitted):
    # 此处仅核验原冻结证明，不读取当前规则或当前日期，供历史来源证明复用。
    validate_bundle(request, admitted)
    items = [item for item in admitted.items if item.kind == KIND]
    if len(items) != 1 or any(item.kind == 'workbench-steer-binding-v1' for item in admitted.items):
        raise ValueError('automatic_constraint_admission_invalid')
    item, project = items[0], request['scope']['project_id']
    payload, scope = item.payload, item.scope
    ref = {'kind': 'session_event', 'object_id': request['turn_id'],
           'uri': f"crp://{scope['namespace_id']}/project-constraints/{request['turn_id']}"}
    if (set(payload) != {'native_turn_id', 'automatic_descriptor', 'bundle'}
            or payload['native_turn_id'] != request['turn_id'] or item.ref != ref
            or constraint_markers(request) != [ref]):
        raise ValueError('automatic_constraint_binding_invalid')
    descriptor, bundle = payload['automatic_descriptor'], payload['bundle']
    validate_automatic_descriptor(descriptor, request)
    if (not isinstance(bundle, dict)
            or set(bundle) != {'version', 'policy_version', 'origin', 'scope', 'scene', 'text', 'items', 'prompt'}
            or bundle['version'] != 'project-constraints@1' or bundle['policy_version'] != '@1'
            or bundle['scope'] != {'user_id': 'local-user', 'project_id': project}
            or bundle['scene'] != descriptor['scene'] or bundle['text'] != '' or not isinstance(bundle['items'], list)):
        raise ValueError('automatic_constraint_bundle_invalid')
    origin = bundle['origin']
    _origin(origin)
    sources = [row for row in admitted.items if row.kind == 'material-origin-v1']
    if origin['owner_user_id'] != scope['owner_user_id']:
        raise ValueError('automatic_constraint_owner_invalid')
    if descriptor['source'] is None:
        if origin['kind'] != 'personal' or origin['namespace'] != scope['namespace_id'] or sources:
            raise ValueError('automatic_constraint_origin_invalid')
    else:
        source = descriptor['source']
        if (origin['kind'] != 'shared' or len(sources) != 1 or not isinstance(source, dict)
                or set(source) != {'namespace', 'lease'} or not isinstance(source['lease'], dict)
                or sources[0].scope != scope
                or sources[0].payload.get('shared') != source
                or sources[0].payload.get('actor_user_id') != scope['owner_user_id']
                or source['lease'].get('project_id') != project
                or source['lease'].get('actor_user_id') != scope['owner_user_id']
                or source['lease'].get('data_namespace') != origin['namespace']):
            raise ValueError('automatic_constraint_origin_invalid')
    policy, seen = get('constraint_scope', version='@1')(), set()
    for row in bundle['items']:
        if (not isinstance(row, dict)
                or set(row) != {'id', 'revision', 'scope_revision', 'content', 'valid_from', 'valid_until', 'activation'}
                or not isinstance(row['id'], str) or not row['id'] or row['id'] in seen
                or not _positive(row['revision']) or type(row['scope_revision']) is not int or row['scope_revision'] < 0
                or not isinstance(row['content'], str) or not row['content'].strip()
                or policy.normalize(**row['activation']) != row['activation']
                or not policy.matches(row['activation'], scene=bundle['scene'], text='',
                    available_scenes=[bundle['scene']] if bundle['scene'] is not None else [])):
            raise ValueError('automatic_constraint_selection_invalid')
        dates = [datetime.fromisoformat(row[key]) if row[key] is not None else None
                 for key in ('valid_from', 'valid_until')]
        if any(value is not None and value.tzinfo is None for value in dates) or all(dates) and dates[0] >= dates[1]:
            raise ValueError('automatic_constraint_dates_invalid')
        seen.add(row['id'])
    if bundle['prompt'] != policy.prompt(bundle['items']):
        raise ValueError('automatic_constraint_prompt_invalid')
    return deepcopy(bundle)


def frozen_messages(bundle, messages):
    copied = deepcopy(messages)
    if bundle is None:
        return copied
    prefix = get('constraint_scope', version=bundle['policy_version'])().instruction_prefix
    existing = [row for row in copied if row.get('role') == 'system'
                and isinstance(row.get('content'), str) and row['content'].startswith(prefix)]
    block = {'role': 'system', 'content': bundle['prompt']}
    if existing:
        if not bundle['prompt'] or existing != [block]:
            raise ValueError('automatic_constraint_messages_changed')
        return copied
    return [block, *copied] if bundle['prompt'] else copied
