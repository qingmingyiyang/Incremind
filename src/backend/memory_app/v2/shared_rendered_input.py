from copy import deepcopy
import json

from backend.recognition import RecognitionConflict


_CAPSULE = {'schema_version', 'turn_id', 'purpose', 'wire_payload', 'items'}
_ITEM = {'role', 'position', 'qualified_material', 'renderer', 'rendered_text', 'rendering'}
_MATERIAL = {'type', 'id', 'revision', 'project_id', 'namespace'}
_RENDERERS = {'recognition-content-v1', 'recognition-condition-v1', 'contextual-chunk-v1',
              'rerank-candidate-v1', 'task-division-text-v1'}


def validate_rendered_capsule_shape(capsule, *, request, reference, auxiliary):
    if not isinstance(auxiliary, dict) or not isinstance(auxiliary.get('selection'), dict):
        raise RecognitionConflict('shared_rendered_input_changed')
    selection = auxiliary['selection']
    if (set(auxiliary) != {'purpose', 'materials', 'selection', 'rendered_input'}
            or set(selection) != {'schema_version', 'origin_ref', 'origin_materials', 'sql_identities', 'rendered_input_ref'}
            or type(selection['schema_version']) is not int or selection['schema_version'] != 3
            or not isinstance(auxiliary['materials'], list) or selection['origin_materials'] != auxiliary['materials']
            or selection['origin_ref'] != reference or auxiliary['rendered_input'] != capsule
            or not isinstance(reference, dict) or set(reference) != {'kind', 'object_id', 'uri'}
            or reference['kind'] != 'session_event' or reference['object_id'] != request['turn_id']
            or not isinstance(reference['uri'], str) or not reference['uri']
            or not isinstance(capsule, dict) or set(capsule) != _CAPSULE
            or type(capsule['schema_version']) is not int or capsule['schema_version'] != 1
            or capsule['turn_id'] != request['turn_id'] or capsule['purpose'] != auxiliary['purpose']
            or capsule['purpose'] not in {'embedding', 'rerank'}
            or selection['rendered_input_ref'] != reference
            or not isinstance(capsule['wire_payload'], dict) or not isinstance(capsule['items'], list)):
        raise RecognitionConflict('shared_rendered_input_changed')
    from .material_origin_sql import validate_sql_shape
    validate_sql_shape(selection['sql_identities'])
    try:
        json.dumps(capsule, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise RecognitionConflict('shared_rendered_input_changed') from error
    payload, purpose = capsule['wire_payload'], capsule['purpose']
    if not isinstance(payload.get('model'), str) or not payload['model']:
        raise RecognitionConflict('shared_rendered_input_changed')
    if purpose == 'embedding':
        if (set(payload) not in ({'model', 'input'}, {'model', 'input', 'input_type'})
                or 'input_type' in payload and payload['input_type'] not in {'query', 'document'}
                or not isinstance(payload['input'], list) or not payload['input']
                or any(not isinstance(text, str) or not text for text in payload['input'])):
            raise RecognitionConflict('shared_rendered_input_changed')
        actual = list(enumerate(payload['input']))
    else:
        if (set(payload) not in ({'model', 'query', 'documents'},
                    {'model', 'query', 'documents', 'max_tokens_per_doc'})
                or not isinstance(payload['query'], str) or not payload['query']
                or not isinstance(payload['documents'], list) or not payload['documents']
                or any(not isinstance(text, str) or not text for text in payload['documents'])
                or 'max_tokens_per_doc' in payload and (type(payload['max_tokens_per_doc']) is not int
                    or payload['max_tokens_per_doc'] < 1)):
            raise RecognitionConflict('shared_rendered_input_changed')
        actual = [(-1, payload['query']), *enumerate(payload['documents'])]
    if len(actual) != len(capsule['items']):
        raise RecognitionConflict('shared_rendered_input_changed')
    questions = 0
    for (position, text), item in zip(actual, capsule['items']):
        if (not isinstance(item, dict) or set(item) != _ITEM or type(item['position']) is not int
                or item['position'] != position or item['rendered_text'] != text):
            raise RecognitionConflict('shared_rendered_input_changed')
        if item['role'] == 'question':
            questions += 1
            if (questions > 1 or position != (-1 if purpose == 'rerank' else 0)
                    or item['qualified_material'] is not None or item['rendering'] is not None
                    or item['renderer'] != 'user-question-v1'):
                raise RecognitionConflict('shared_rendered_input_changed')
        elif item['role'] == 'material':
            material = item['qualified_material']
            task_division = item['renderer'] == 'task-division-text-v1'
            if (position < 0 or not isinstance(material, dict) or set(material) != _MATERIAL
                    or material not in auxiliary['materials']
                    or material.get('type') not in ({'recognition', 'document', 'original_source', 'original_item', 'experience'}
                        if task_division else {'recognition', 'document', 'original_source', 'original_item'})
                    or type(material['revision']) is not int or material['revision'] < 1
                    or any(not isinstance(material[key], str) or not material[key]
                           for key in ('type', 'id', 'project_id', 'namespace'))
                    or item['renderer'] not in _RENDERERS or not isinstance(item['rendering'], dict)):
                raise RecognitionConflict('shared_rendered_input_changed')
            rendering = item['rendering']
            condition = item['renderer'] == 'recognition-condition-v1'
            fields = ({'candidate', 'summary'} if item['renderer'] == 'contextual-chunk-v1' else
                {'candidate', 'condition_index'} if condition else {'candidate'})
            if (set(rendering) != fields or not isinstance(rendering['candidate'], (dict, list))
                    or 'summary' in rendering and not isinstance(rendering['summary'], str)
                    or condition and (material['type'] != 'recognition' or purpose != 'embedding'
                        or type(rendering['condition_index']) is not int or rendering['condition_index'] < 0)
                    or purpose == 'rerank' and item['renderer'] != 'rerank-candidate-v1'
                    or purpose == 'embedding' and item['renderer'] == 'rerank-candidate-v1'
                    or task_division and purpose != 'embedding'):
                raise RecognitionConflict('shared_rendered_input_changed')
            if task_division:
                from .shared_task_division_vectors import validate_task_division_rendering
                validate_task_division_rendering(item)
                if any(source not in auxiliary['materials'] for source in rendering['candidate']['materials']):
                    raise RecognitionConflict('shared_rendered_input_changed')
        else:
            raise RecognitionConflict('shared_rendered_input_changed')
    if purpose == 'rerank' and questions != 1:
        raise RecognitionConflict('shared_rendered_input_changed')


def read_rendered_capsule(request, auxiliary, *, reference):
    if not isinstance(auxiliary, dict):
        raise RecognitionConflict('shared_rendered_input_changed')
    capsule = auxiliary.get('rendered_input')
    validate_rendered_capsule_shape(capsule, request=request, reference=reference, auxiliary=auxiliary)
    return deepcopy(capsule)
