from copy import deepcopy

from backend.recognition import RecognitionConflict, WorkScope
from ..kernel.answer_continuations import decode
from .material_input_binding import prepared_gap_input, prepared_gap_proof
from .material_origin_context import freeze_personal_context, personal_context_materials, context_sql_identities
from .material_origin_sql import freeze_sql_identities
from .shared_auxiliary import _qualified_commit


PRIMARY_FIELDS = {'origin_ref', 'origin_materials', 'search_fragments', 'personal_context',
                  'schema_version', 'sql_identities'}
RAG_FIELDS = PRIMARY_FIELDS | {'privacy', 'candidates', 'skills', 'history', 'profile', 'tools', 'search'}


def _history(plan, value):
    if value == {} and plan.get('history', '') == '' and plan.get('history_count', 0) == 0:
        return {'text': '', 'turns': []}
    return value


def _qualified(query, values):
    result = []
    namespaces = {query.origin_admission.lease.project_id: query.source_namespace,
                  'me': query.origin_admission.personal['namespace']}
    for value in values:
        if (not isinstance(value, dict) or set(value) != {'type', 'id', 'revision', 'project_id', 'namespace'}
                or value['namespace'] != namespaces.get(value['project_id'])):
            raise RecognitionConflict('rag_material_origin_changed')
        if value not in result:
            result.append(deepcopy(value))
    return result


def _materials(query, request, descriptors, context):
    values = _qualified(query, descriptors)
    for material in personal_context_materials(query, request, context):
        if material not in values:
            values.append(material)
    return _qualified(query, values)


def _plan_materials(query, request, plan, descriptors, context):
    from .agentic_rag import _candidates, _descriptors
    selected = _descriptors(_candidates(plan), source_namespace=query.source_namespace)
    selected.extend({'type': 'recognition', 'id': item['id'], 'revision': item['revision'],
        'project_id': 'me', 'namespace': query.origin_admission.personal['namespace']}
        for item in plan.get('profile', {}).get('items', []))
    materials = _materials(query, request, selected, context)
    if any(value not in materials for value in _qualified(query, descriptors)):
        raise RecognitionConflict('rag_material_origin_changed')
    return materials


def freeze_gap_materials(query, request, plan, models, descriptors, *, local_only):
    from .search import shared_search_fragments
    with _qualified_commit(query.origin_admission):
        context = freeze_personal_context(query, request, plan, _history(plan, plan['_rag']['history']))
        materials = _plan_materials(query, request, plan, descriptors, context)
        allowed, privacy, origin = query.freeze_materials(models, plan['project_id'], materials,
            local_only=local_only, native_request=request)
        if allowed != materials or origin['origin_materials'] != materials:
            raise RecognitionConflict('rag_egress_unavailable')
        origins = query.origin_admission.restore(request)
        with prepared_gap_input(query, request, materials, plan=plan) as prepared:
            identities = freeze_sql_identities(origins, materials, query=query, prepared_input=prepared)
        identities = context_sql_identities(query, request, context, identities)
        from .agentic_rag import _candidates
        return allowed, privacy, {**origin, 'schema_version': 2, 'sql_identities': identities,
            'personal_context': context, 'search_fragments': shared_search_fragments(query, {**plan, 'chosen': _candidates(plan)})}


def validate_gap_material_proof(query, project, target, proof, *, request):
    from .agentic_rag import _descriptors, tool_availability
    from .search import shared_search_fragments
    if (not isinstance(proof, dict) or set(proof) != RAG_FIELDS
            or type(proof['schema_version']) is not int or proof['schema_version'] != 2
            or proof['origin_ref'] != query.origin_admission._reference(request)
            or proof['tools'] != tool_availability(query, project)):
        raise RecognitionConflict('rag_material_origin_changed')
    candidates, profile, history = decode(proof['candidates']), decode(proof['profile']), decode(proof['history'])
    plan = {'project_id': project, 'chosen': candidates, 'profile': profile, 'target': target,
            'scope': WorkScope('local-user', project),
            'history': history.get('text', ''), 'history_count': len(history.get('turns', []))}
    with _qualified_commit(query.origin_admission):
        context = freeze_personal_context(query, request, plan, _history(plan, history))
        descriptors = _descriptors(candidates, source_namespace=query.source_namespace)
        descriptors.extend({'type': 'recognition', 'id': item['id'], 'revision': item['revision'],
            'project_id': 'me', 'namespace': query.origin_admission.personal['namespace']}
            for item in profile.get('items', []))
        materials = _materials(query, request, descriptors, context)
        if materials != proof['origin_materials'] or context != proof['personal_context']:
            raise RecognitionConflict('rag_material_origin_changed')
        allowed, privacy, _ = query.freeze_materials(query.models, project, materials,
            local_only=not proof['privacy']['allow_remote'], native_request=request)
        if allowed != materials or privacy != proof['privacy']:
            raise RecognitionConflict('rag_egress_unavailable')
        origins = query.origin_admission.restore(request)
        with prepared_gap_proof(query, request, materials, proof=proof) as prepared:
            identities = freeze_sql_identities(origins, materials, query=query, prepared_input=prepared)
        if context_sql_identities(query, request, context, identities) != proof['sql_identities']:
            raise RecognitionConflict('rag_material_sql_changed')
        if shared_search_fragments(query, plan, binding=proof['search']) != proof['search_fragments']:
            raise RecognitionConflict('rag_search_source_changed')


def freeze_derived_gap(query, request, plan, purpose, descriptors):
    with _qualified_commit(query.origin_admission):
        context = freeze_personal_context(query, request, plan, _history(plan, plan['_rag']['history']))
        materials = _plan_materials(query, request, plan, descriptors, context)
        allowed, privacy, _ = query.freeze_materials(query.models, plan['project_id'], materials,
            purpose=purpose, native_request=request)
        if allowed != materials or not privacy['allow_remote']:
            raise RecognitionConflict('rag_derived_egress_blocked')
        return privacy
