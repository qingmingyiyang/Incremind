"""沿原公开终态与出生事实绑定成果版本。"""
from copy import deepcopy
from collections.abc import Mapping
import json

from backend.recognition import RecognitionConflict, WorkScope
from backend.recognition.product_draft_dependencies import product_draft_source, ProductDraftDependencyError

COLLECTION = 'v2_outcome_lineage'
COMPOSITION = 'product-outcome-composition-v1'
_FIELDS = {'project_id', 'scene', 'root_id', 'previous_id', 'version', 'turn_id', 'task_text'}
_SELECTION_FIELDS = {'mode', 'project_id', 'scene', 'document_id', 'document_revision',
                     'lineage_revision', 'lineage', 'owner', 'root_id', 'previous_id'}


def _birth(reader, project, document_id, public_turn_id):
    # 当前来源引用可以由用户修改，出生身份只沿原公开轮和交付操作回读保留修订。
    try:
        execution = reader.read('v2_task_executions', public_turn_id)
        request = execution.payload.get('request') if execution else None
        kernel = request.get('turn_id') if isinstance(request, Mapping) else None
        if not isinstance(kernel, str) or not kernel:
            return None
        operation = reader.read('v2_task_draft_operations', 'deliver-' + kernel)
        result = operation.payload.get('result') if operation else None
        if (not isinstance(result, Mapping) or result.get('document_id') != document_id
                or type(result.get('document_revision')) is not int or result['document_revision'] < 1):
            return None
        return product_draft_source(reader, WorkScope('local-user', project), document_id,
                                    result['document_revision'], turn_id=kernel)
    except (ProductDraftDependencyError, KeyError, TypeError, ValueError, AttributeError):
        return None


def _shape(row, project):
    if row is None or row.revision != 1 or set(row.payload) != _FIELDS:
        return False
    data = row.payload
    return (data['project_id'] == project and type(data['version']) is int and data['version'] > 0
            and all(isinstance(data[key], str) and data[key] for key in ('root_id', 'turn_id', 'task_text'))
            and (data['scene'] is None or isinstance(data['scene'], str) and bool(data['scene']))
            and (data['previous_id'] is None or isinstance(data['previous_id'], str) and bool(data['previous_id'])))


def _composition_matches(reader, state, document_id):
    """以原完成事务保存的不可变结果，核对公开稿和回退事实。"""
    data = state.get('outcome_composition')
    if data is None:
        return None
    request = state['request']
    turn = reader.read('v2_turns', state['outcome_composition_turn'])
    operation = reader.read('v2_task_draft_operations', 'deliver-' + request['turn_id'])
    receipt = turn.payload.get('receipt', {}).get('do', {}) if turn else {}
    if (not isinstance(data, dict) or set(data) != {'turn_id', 'model_request_id', 'input', 'markdown', 'changes', 'fallback_new'}
            or not isinstance(data['model_request_id'], str) or not data['model_request_id']
            or data['turn_id'] != request['turn_id'] or data['input'] != request['input']
            or operation is None or operation.payload['result']['document_id'] != document_id
            or data['markdown'] != operation.payload['inputs']['markdown']
            or type(data['fallback_new']) is not bool or not isinstance(data['changes'], list)
            or receipt.get('fallback_new') != data['fallback_new'] or receipt.get('changes') != data['changes']):
        raise RecognitionConflict('outcome_composition_changed')
    return data


def _trusted(reader, rows, identity, project, memo, visiting):
    if identity in memo:
        return memo[identity]
    row = rows.get(identity)
    if identity in visiting or not _shape(row, project):
        return None
    visiting.add(identity)
    try:
        data = row.payload
        bound = _birth(reader, project, identity, data['turn_id'])
        if bound is None or bound.revisions['task_execution_id'] != data['turn_id']:
            return None
        execution = reader.read('v2_task_executions', data['turn_id'])
        state = execution.payload
        if state.get('task_text') != data['task_text'] or state.get('scene') != data['scene']:
            return None
        selected = state.get('outcome_selection')
        composed = _composition_matches(reader, state, identity)
        if selected is None:
            if (data['root_id'] != identity or data['previous_id'] is not None or data['version'] != 1):
                return None
        else:
            if not isinstance(selected, dict) or set(selected) != _SELECTION_FIELDS:
                return None
            frozen = json.loads(state['request']['input']['text'])
            if (frozen.get('outcome_selection') != selected or frozen.get('task') != data['task_text']
                    or selected['project_id'] != project or selected['scene'] != data['scene']):
                return None
            source = _trusted(reader, rows, selected['document_id'], project, memo, visiting)
            if (source is None or selected['lineage_revision'] != source.revision
                    or selected['lineage'] != source.payload
                    or selected['mode'] not in ('continue', 'redo')
                    or selected['previous_id'] != (source.object_id if selected['mode'] == 'continue'
                                                  else source.payload['previous_id'])):
                return None
            if composed is not None and composed['fallback_new']:
                if (data['root_id'] != identity or data['previous_id'] is not None or data['version'] != 1
                        or composed['changes'] != []):
                    return None
            elif (selected['root_id'] != data['root_id'] or selected['previous_id'] != data['previous_id']
                    or source.payload['root_id'] != data['root_id'] or source.payload['version'] >= data['version']):
                return None
        memo[identity] = row
        return row
    except (KeyError, TypeError, ValueError, AttributeError, RecognitionConflict):
        return None
    finally:
        visiting.remove(identity)


def qualified_lineages(reader, project):
    rows = {row.object_id: row for row in reader.list(COLLECTION)}
    memo = {}
    for identity in rows:
        _trusted(reader, rows, identity, project, memo, set())
    return memo


def select_outcome(reader, *, project, scene, document_id, mode='continue'):
    """冻结真实成果选择，当前稿与出生稿分别由原权威核验。"""
    if mode not in ('continue', 'redo'):
        raise RecognitionConflict('outcome_selection_invalid')
    row = qualified_lineages(reader, project).get(document_id)
    if row is None or mode == 'redo' and row.payload['scene'] != scene:
        raise RecognitionConflict('outcome_unavailable')
    document = reader.read('documents', document_id)
    recall = reader.read('v2_document_recall', document_id)
    if (document is None or document.payload.get('status') == 'archived'
            or recall is not None and recall.payload.get('state') == 'forgotten'):
        raise RecognitionConflict('outcome_unavailable')
    bound = _birth(reader, project, document_id, row.payload['turn_id'])
    return deepcopy({'mode': mode, 'project_id': project, 'scene': scene, 'document_id': document_id,
        'document_revision': document.revision, 'lineage_revision': row.revision, 'lineage': row.payload,
        'owner': dict(bound.revisions), 'root_id': row.payload['root_id'],
        'previous_id': document_id if mode == 'continue' else row.payload['previous_id']})


def validate_selection(reader, *, project, scene, selection):
    if (not isinstance(selection, dict) or set(selection) != _SELECTION_FIELDS
            or selection['project_id'] != project or selection['scene'] != scene):
        raise RecognitionConflict('outcome_selection_invalid')
    fresh = select_outcome(reader, project=project, scene=scene,
                           document_id=selection['document_id'], mode=selection['mode'])
    if fresh != selection:
        raise RecognitionConflict('outcome_selection_changed')
    return deepcopy(fresh)


def prepare_continuation(reader, documents, *, project, scene, selection, policy_version='@1'):
    """分别冻结当前正文和真实出生稿，材料仍由原领域权威解析。"""
    from backend.recognition import RecognitionService
    from ..document_recognition import ensure_document_experience
    from ..original_sources import document_roots
    from .policies import get
    selected = validate_selection(reader, project=project, scene=scene, selection=selection)
    get('continuation', version=policy_version)
    identity, birth = selected['document_id'], selected['owner']['document_revision']
    document = reader.read('documents', identity)
    markdown = reader.read('document_markdown', f'{identity}~r{document.revision}')
    born = reader.read('document_markdown', f'{identity}~r{birth}')
    if markdown is None or born is None:
        raise RecognitionConflict('outcome_document_unavailable')
    experience, _ = ensure_document_experience(documents, RecognitionService(reader), project,
        identity, retained_revision=birth)
    row = reader.read('recognition_experiences', experience)
    material = [{'type': 'experience', 'id': experience, 'revision': row.revision, 'project_id': project}]
    birth_ref = {'source_id': selected['owner']['product_turn_id'],
                 'locator': 'task://' + selected['owner']['product_turn_id']}
    refs = document.payload.get('source_refs')
    if not isinstance(refs, list):
        raise RecognitionConflict('outcome_document_sources_invalid')
    extra = [ref for ref in refs if ref != birth_ref]
    for kind, source, revision in document_roots(reader, WorkScope('local-user', project), extra):
        material.append({'type': kind, 'id': source, 'revision': revision, 'project_id': project})
    return {'continuation_policy': policy_version, 'document_id': identity,
        'document_revision': document.revision, 'markdown_record_revision': markdown.revision,
        'current_markdown': markdown.payload['markdown'], 'birth_ai_markdown': born.payload['markdown'],
        'current_source_refs': deepcopy(refs), 'materials': material}


def validate_continuation(reader, request):
    """当前稿的资格在原外发读事务中重新核验，不由出生事实授予。"""
    value = json.loads(request['input']['text'])
    data = value.get('outcome_input')
    if data is None:
        return None
    from .policies import get
    if not isinstance(data, dict) or not isinstance(data.get('continuation_policy'), str):
        raise RecognitionConflict('outcome_policy_unavailable')
    get('continuation', version=data['continuation_policy'])
    if data.get('document_id') is None:
        if set(data) != {'continuation_policy', 'document_id'} or value.get('outcome_selection') is not None:
            raise RecognitionConflict('outcome_input_invalid')
        return deepcopy(data)
    if not isinstance(data, dict) or set(data) != {'continuation_policy', 'document_id',
            'document_revision', 'markdown_record_revision', 'current_markdown',
            'birth_ai_markdown', 'current_source_refs', 'materials'}:
        raise RecognitionConflict('outcome_input_invalid')
    selected = value.get('outcome_selection')
    if not isinstance(selected, dict):
        raise RecognitionConflict('outcome_selection_invalid')
    validate_selection(reader, project=request['scope']['project_id'], scene=selected['scene'], selection=selected)
    identity, revision = data['document_id'], data['document_revision']
    document = reader.read('documents', identity)
    markdown = reader.read('document_markdown', f'{identity}~r{revision}')
    born = reader.read('document_markdown', f"{identity}~r{selected['owner']['document_revision']}")
    if (identity != selected['document_id'] or revision != selected['document_revision']
            or document is None or document.revision != revision or markdown is None or born is None
            or document.payload.get('source_refs') != data['current_source_refs']
            or markdown.revision != data['markdown_record_revision']
            or markdown.payload.get('markdown') != data['current_markdown']
            or born.payload.get('markdown') != data['birth_ai_markdown']
            or any(material not in request['privacy']['material_refs'] for material in data['materials'])
            or request['privacy']['allow_remote'] is not True):
        raise RecognitionConflict('outcome_input_changed')
    return deepcopy(data)


def completed_composition(store, request, summary, *, events=()):
    """只把本轮完成正文与同一不可变输入绑定的结果交给原交付者。"""
    try:
        frozen = json.loads(request['input']['text'])
    except (ValueError, TypeError):
        return None
    if not isinstance(frozen, dict):
        return None
    if frozen.get('outcome_input') is None or frozen['outcome_input'].get('document_id') is None:
        return None
    saved = store.get_immutable_payload(request['turn_id'], COMPOSITION)
    if saved is None:
        raise RecognitionConflict('outcome_composition_unavailable')
    data = saved[1]
    if (not isinstance(data, dict) or set(data) != {'turn_id', 'model_request_id', 'input', 'markdown', 'changes', 'fallback_new'}
            or not any(event.get('type') == 'model.completed'
                and event.get('correlation', {}).get('model_request_id') == data['model_request_id'] for event in events)
            or data['turn_id'] != request['turn_id'] or data['input'] != request['input']
            or not isinstance(data['markdown'], str) or data['markdown'].strip() != summary
            or type(data['fallback_new']) is not bool or not isinstance(data['changes'], list)):
        raise RecognitionConflict('outcome_composition_changed')
    from .policies import get
    get('continuation', version=frozen['outcome_input']['continuation_policy'])
    return deepcopy(data)


def record_outcome(reader, *, project, document_id, turn_id, scene, task_text, selection=None):
    """在原公开完成事务写入之后登记成果链。"""
    bound = _birth(reader, project, document_id, turn_id)
    existing = reader.read(COLLECTION, document_id)
    if bound is None and selection is None and existing is None:
        # 原调用方的未知出生保持原交付语义，只有真实成果才登记新链。
        return None
    if bound is None or bound.revisions['task_execution_id'] != turn_id:
        raise RecognitionConflict('outcome_birth_invalid')
    rows = qualified_lineages(reader, project)
    if existing is not None:
        if rows.get(document_id) != existing:
            raise RecognitionConflict('outcome_lineage_conflicted')
        return existing
    if selection is None:
        root, previous, version = document_id, None, 1
    else:
        selected = validate_selection(reader, project=project, scene=scene, selection=selection)
        root, previous = selected['root_id'], selected['previous_id']
        # 同根编号由公开完成事务串行分配，重做与并发分支也不会重复编号。
        version = max(row.payload['version'] for row in rows.values() if row.payload['root_id'] == root) + 1
    payload = {'project_id': project, 'scene': scene, 'root_id': root, 'previous_id': previous,
               'version': version, 'turn_id': turn_id, 'task_text': task_text}
    saved = reader.put(COLLECTION, document_id, payload, expected_revision=0)
    if qualified_lineages(reader, project).get(document_id) != saved:
        raise RecognitionConflict('outcome_lineage_binding_invalid')
    return saved


def hidden_outcome_ids(reader, project):
    rows = qualified_lineages(reader, project)
    latest = {}
    for row in rows.values():
        root = row.payload['root_id']
        latest[root] = max(latest.get(root, 0), row.payload['version'])
    # 归档和遗忘影响当前资格，保留的出生事实仍阻止旧版自动回落。
    return {row.object_id for row in rows.values()
            if row.payload['version'] < latest[row.payload['root_id']]}


def candidates(reader, *, project, document_id=None):
    """自动选择只投影可信最新版，明确目标仍经同一资格核验。"""
    rows, hidden = qualified_lineages(reader, project), hidden_outcome_ids(reader, project)
    result = []
    for row in sorted(rows.values(), key=lambda row: row.object_id):
        if (document_id is None and row.object_id in hidden
                or document_id is not None and row.object_id != document_id):
            continue
        try:
            select_outcome(reader, project=project, scene=row.payload['scene'], document_id=row.object_id)
        except RecognitionConflict:
            continue
        document = reader.read('documents', row.object_id)
        result.append({'document_id': row.object_id, 'title': document.payload['title'],
            'version': row.payload['version'], 'project_id': project,
            'scene': row.payload['scene'], 'task_text': row.payload['task_text']})
    return result


def versions(reader, *, project, document_id, documents):
    rows = qualified_lineages(reader, project)
    chosen = rows.get(document_id)
    if chosen is None:
        raise RecognitionConflict('outcome_unavailable')
    root = chosen.payload['root_id']
    items = []
    for row in sorted(rows.values(), key=lambda item: item.payload['version']):
        if row.payload['root_id'] == root:
            turn = reader.read('v2_turns', row.payload['turn_id'])
            execution = reader.read('v2_task_executions', row.payload['turn_id'])
            composed = _composition_matches(reader, execution.payload, row.object_id)
            items.append({'document_id': row.object_id, 'version': row.payload['version'],
                          'created_at': turn.payload['updated_at'],
                          'changes': deepcopy(composed['changes']) if composed is not None else []})
    previous = None
    execution = reader.read('v2_task_executions', chosen.payload['turn_id'])
    composed = _composition_matches(reader, execution.payload, document_id)
    if composed is not None and not composed['fallback_new']:
        frozen = json.loads(composed['input']['text']).get('outcome_input')
        selected = execution.payload.get('outcome_selection')
        if (not isinstance(selected, dict) or not isinstance(frozen, dict)
                or frozen.get('document_id') != selected.get('document_id')
                or type(frozen.get('document_revision')) is not int or frozen['document_revision'] < 1
                or not isinstance(frozen.get('current_markdown'), str)):
            raise RecognitionConflict('outcome_previous_unavailable')
        # 对照只读交付时冻结的历史，不以当前稿替代丢失的修订。
        markdown = documents.markdown(frozen['document_id'], revision=frozen['document_revision'])
        if markdown is not None:
            if markdown != frozen['current_markdown']:
                raise RecognitionConflict('outcome_previous_changed')
            previous = {'document_id': frozen['document_id'], 'revision': frozen['document_revision'],
                        'markdown': markdown}
    return {'items': items, 'previous': previous}
