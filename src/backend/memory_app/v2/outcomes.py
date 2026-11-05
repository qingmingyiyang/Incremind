"""沿原公开终态与出生事实绑定成果版本。"""
from copy import deepcopy
from collections.abc import Mapping
import json
from uuid import uuid4

from backend.recognition import RecognitionConflict, WorkScope
from backend.recognition.product_draft_dependencies import (
    product_draft_source, ProductDraftDependencyError, RESTORATIONS, ANSWER_OUTCOMES,
)

COLLECTION = 'v2_outcome_lineage'
COMPOSITION = 'product-outcome-composition-v1'
_FIELDS = {'project_id', 'scene', 'root_id', 'previous_id', 'version', 'turn_id', 'task_text'}
_SELECTION_FIELDS = {'mode', 'project_id', 'scene', 'document_id', 'document_revision',
                     'lineage_revision', 'lineage', 'owner', 'root_id', 'previous_id'}


def _birth(reader, project, document_id, public_turn_id):
    # 当前来源引用可以由用户修改，出生身份只沿原公开轮和交付操作回读保留修订。
    try:
        document = reader.read('documents', document_id)
        if document is not None and str(document.payload.get('type', '')).startswith(('agent-result-restore-', 'agent-result-answer-')):
            bound = product_draft_source(reader, WorkScope('local-user', project), document_id, 1)
            return bound if bound is not None and bound.public_turn_id == public_turn_id else None
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
        if bound is None or bound.public_turn_id != data['turn_id']:
            return None
        if bound.restoration is not None:
            restored = bound.restoration
            source = _trusted(reader, rows, restored['source_document_id'], project, memo, visiting)
            previous = _trusted(reader, rows, restored['previous_id'], project, memo, visiting)
            if (source is None or previous is None or source.payload != restored['source_lineage']
                    or any(data[key] != restored[key] for key in _FIELDS)
                    or source.payload['root_id'] != data['root_id'] or previous.payload['root_id'] != data['root_id']
                    or previous.payload['version'] >= data['version'] or source.payload['version'] >= data['version']):
                return None
            memo[identity] = row
            return row
        if bound.answer_birth is not None:
            saved = bound.answer_birth
            if (data['root_id'] != identity or data['previous_id'] is not None or data['version'] != 1
                    or data['task_text'] != saved['question'] or data['scene'] != saved['scene']):
                return None
            memo[identity] = row
            return row
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


def qualified_update_input(freshness, reader, *, project, document_id, basis):
    if freshness is None:
        raise RecognitionConflict('outcome_update_authority_unavailable')
    with freshness._read_scope(None if reader is freshness.records else reader) as current:
        return freshness.validate_update_basis(project=project, document_id=document_id, basis=basis, reader=current)


def validate_update_evidence(freshness, native_request, evidence_refs, *, reader=None):
    if (freshness is None or not isinstance(evidence_refs, list)
            or any(not isinstance(reference, str) for reference in evidence_refs)):
        raise RecognitionConflict('outcome_update_evidence_invalid')
    with freshness._read_scope(reader) as current:
        context = freshness.completion_context(native_request, reader=current)
        qualified = freshness.qualify_learning_context(context, reader=current, local_only=True)
        if not qualified['complete']:
            raise RecognitionConflict('outcome_update_context_unverifiable')
        fields = ('type', 'id', 'revision', 'project_id')
        current_materials = {tuple(item['material'][key] for key in fields)
            for item in qualified['current_sources'] if not item['privacy_only']}
        allowed = {alias for source in context['source_proofs'] if source.get('verified')
            and tuple(source['material'][key] for key in fields) in current_materials for alias in source['aliases']}
        if any(reference not in allowed for reference in evidence_refs):
            raise RecognitionConflict('outcome_update_evidence_not_consumed')
        return frozenset(allowed)


def update_materials(qualified):
    materials = []
    def retain(value, namespace):
        item = {key: value[key] for key in ('type', 'id', 'revision', 'project_id')}
        if qualified.get('namespaced_materials'):
            item['namespace'] = namespace
        if item not in materials:
            materials.append(item)
    for source in qualified['current_sources']:
        retain(source['material'], source['origin']['namespace'])
    for source in qualified['privacy_ceiling']['sources']:
        if source['source']['type'] != 'document':
            retain(source['source'], source['origin']['namespace'])
        origins = source.get('history_origins', [source['origin']] * len(source['history_current']))
        if not isinstance(origins, list) or len(origins) != len(source['history_current']):
            raise RecognitionConflict('outcome_update_history_namespace_changed')
        snapshots = [(source['current'], source['origin']), *zip(source['history_current'], origins, strict=True)]
        for snapshot, origin in snapshots:
            for root in snapshot['roots']:
                retain({**root, 'project_id': snapshot['scope']['project_id']}, origin['namespace'])
    return materials


def prepare_continuation(reader, documents, *, project, scene, selection, policy_version='@1',
                         update_basis=None, freshness=None):
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
    if update_basis is not None:
        qualified = qualified_update_input(freshness, reader, project=project, document_id=identity, basis=update_basis)
        get('outcome_update', version=update_basis['update_policy'])
        materials = update_materials(qualified)
        return {'continuation_policy': policy_version, 'document_id': identity,
            'document_revision': document.revision, 'markdown_record_revision': markdown.revision,
            'current_markdown': markdown.payload['markdown'], 'birth_ai_markdown': born.payload['markdown'],
            'current_source_refs': deepcopy(document.payload['source_refs']), 'materials': materials,
            'update_policy': update_basis['update_policy'], 'update_basis': deepcopy(update_basis)}
    experience, _ = ensure_document_experience(documents, RecognitionService(reader), project,
        identity, retained_revision=birth)
    row = reader.read('recognition_experiences', experience)
    material = [{'type': 'experience', 'id': experience, 'revision': row.revision, 'project_id': project}]
    operation = selected['owner'].get('restoration_id')
    birth_ref = ({'source_id': operation, 'locator': 'outcome://' + operation} if operation else
        {'source_id': selected['owner']['answer_turn_id'], 'locator': 'conversation://' + selected['owner']['answer_turn_id']}
        if 'answer_turn_id' in selected['owner'] else
        {'source_id': selected['owner']['product_turn_id'], 'locator': 'task://' + selected['owner']['product_turn_id']})
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


def validate_continuation(reader, request, *, freshness=None):
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
    update = {'update_policy', 'update_basis'} if 'update_basis' in data else set()
    if not isinstance(data, dict) or set(data) != {'continuation_policy', 'document_id',
            'document_revision', 'markdown_record_revision', 'current_markdown',
            'birth_ai_markdown', 'current_source_refs', 'materials'} | update:
        raise RecognitionConflict('outcome_input_invalid')
    selected = value.get('outcome_selection')
    if not isinstance(selected, dict):
        raise RecognitionConflict('outcome_selection_invalid')
    validate_selection(reader, project=request['scope']['project_id'], scene=selected['scene'], selection=selected)
    if update:
        if not isinstance(data['update_basis'], dict) or data['update_policy'] != data['update_basis'].get('update_policy'):
            raise RecognitionConflict('outcome_update_policy_changed')
        get('outcome_update', version=data['update_policy'])
        qualified = qualified_update_input(freshness, reader, project=request['scope']['project_id'],
            document_id=data['document_id'], basis=data['update_basis'])
        if (qualified['current_markdown'] != data['current_markdown']
                or qualified['current_source_refs'] != data['current_source_refs']
                or request['privacy']['allow_remote'] and 'generation' not in qualified['privacy_ceiling']['effective_purposes']):
            raise RecognitionConflict('outcome_update_source_changed')
        expected = update_materials(qualified)
        if expected != data['materials']:
            raise RecognitionConflict('outcome_update_materials_changed')
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
            or any({key: material[key] for key in ('type', 'id', 'revision', 'project_id')}
                not in request['privacy']['material_refs'] for material in data['materials'])
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
    if bound is None or bound.public_turn_id != turn_id:
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
        if reader.read(COLLECTION, document_id) is None:
            # 旧成果只沿真实出生事实读取，不补写版本或授予续写资格。
            for execution in reader.list('v2_task_executions'):
                if (execution.payload.get('project_id') == project
                        and _birth(reader, project, document_id, execution.object_id) is not None):
                    return {'items': [], 'previous': None}
        raise RecognitionConflict('outcome_unavailable')
    root = chosen.payload['root_id']
    latest_row = max((row for row in rows.values() if row.payload['root_id'] == root),
                     key=lambda row: row.payload['version'])
    latest_document = documents.read(latest_row.object_id)
    if latest_document is None:
        raise RecognitionConflict('outcome_unavailable')
    items = []
    for row in sorted(rows.values(), key=lambda item: item.payload['version']):
        if row.payload['root_id'] == root:
            bound = _birth(reader, project, row.object_id, row.payload['turn_id'])
            document = documents.read(row.object_id)
            if bound is None or document is None:
                raise RecognitionConflict('outcome_unavailable')
            if bound.restoration is not None:
                items.append({'document_id': row.object_id, 'version': row.payload['version'],
                    'revision': document['revision'], 'created_at': bound.restoration['created_at'],
                    'changes': deepcopy(bound.restoration['changes'])})
                continue
            if bound.answer_birth is not None:
                items.append({'document_id': row.object_id, 'version': row.payload['version'],
                    'revision': document['revision'], 'created_at': bound.answer_birth['created_at'], 'changes': []})
                continue
            turn = reader.read('v2_turns', row.payload['turn_id'])
            execution = reader.read('v2_task_executions', row.payload['turn_id'])
            composed = _composition_matches(reader, execution.payload, row.object_id)
            items.append({'document_id': row.object_id, 'version': row.payload['version'],
                          'revision': document['revision'],
                          'created_at': turn.payload['updated_at'],
                          'changes': deepcopy(composed['changes']) if composed is not None else []})
    previous = None
    chosen_birth = _birth(reader, project, document_id, chosen.payload['turn_id'])
    if chosen_birth is not None and chosen_birth.answer_birth is not None:
        return {'items': items, 'previous': None,
                'latest': {'document_id': latest_row.object_id, 'revision': latest_document['revision']}}
    if chosen_birth is not None and chosen_birth.restoration is not None:
        restored = chosen_birth.restoration
        markdown = documents.markdown(restored['previous_id'], revision=restored['previous_revision'])
        if markdown is None:
            raise RecognitionConflict('outcome_previous_unavailable')
        return {'items': items, 'previous': {'document_id': restored['previous_id'],
            'revision': restored['previous_revision'], 'markdown': markdown},
            'latest': {'document_id': latest_row.object_id, 'revision': latest_document['revision']}}
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
    return {'items': items, 'previous': previous,
            'latest': {'document_id': latest_row.object_id, 'revision': latest_document['revision']}}


def restore_outcome_in_uow(tx, documents, drafts, *, project, source_document_id, source_revision,
                           expected_latest_id, expected_latest_revision, now):
    from core.document_engine import SQLiteDocumentRepository
    from ..transaction_records import TransactionRecords
    from .task_drafts import TaskDrafts
    from .outcome_patches import compare_outlines
    from .citation_refs import COLLECTION as CITATIONS, safe_record_restored_citations
    documents = SQLiteDocumentRepository(TransactionRecords(tx), namespace_id=documents.namespace_id, now=now)
    if any(type(value) is not int or value < 1 for value in (source_revision, expected_latest_revision)):
        raise RecognitionConflict('outcome_revision_conflict')
    rows = qualified_lineages(tx, project)
    source = rows.get(source_document_id)
    if source is None:
        raise RecognitionConflict('outcome_unavailable')
    latest = max((row for row in rows.values() if row.payload['root_id'] == source.payload['root_id']),
                 key=lambda row: row.payload['version'])
    source_document, head = documents.read(source_document_id), documents.read(latest.object_id)
    if (source_document is None or head is None or latest.object_id != expected_latest_id
            or head['revision'] != expected_latest_revision or source_document['revision'] != source_revision):
        raise RecognitionConflict('outcome_revision_conflict')
    select_outcome(tx, project=project, scene=latest.payload['scene'], document_id=latest.object_id)
    source_record = documents.revision(source_document_id, source_revision)
    markdown = documents.markdown(source_document_id, revision=source_revision)
    previous = documents.markdown(latest.object_id, revision=expected_latest_revision)
    if (source_record is None or source_record.get('document_id') != source_document_id
            or source_record.get('revision') != source_revision or not isinstance(markdown, str)
            or not markdown.strip() or not isinstance(previous, str)):
        raise RecognitionConflict('outcome_source_revision_unavailable')
    bound = _birth(tx, project, source_document_id, source.payload['turn_id'])
    if bound is None:
        raise RecognitionConflict('outcome_birth_invalid')
    refs = (deepcopy(bound.restoration['cited_refs']) if bound.restoration is not None else
        deepcopy(bound.answer_birth['cited_refs']) if bound.answer_birth is not None else [
        {'kind': row.payload['target_kind'], 'id': row.payload['target_id'], 'revision': row.payload['target_revision']}
        for row in tx.list(CITATIONS) if row.payload.get('project_id') == project
        and row.payload.get('turn_id') == source.payload['turn_id'] and row.payload.get('use') == 'outcome'])
    operation = 'restore-' + uuid4().hex
    enlisted = SQLiteDocumentRepository(TransactionRecords(tx), namespace_id=drafts.documents.namespace_id, now=now)
    document = TaskDrafts(TransactionRecords(tx), enlisted).create_restored_in_uow(tx,
        project=project, operation=operation, title=source_document['title'], markdown=markdown)
    changes = compare_outlines(previous, markdown)
    lineage = {'project_id': project, 'scene': latest.payload['scene'], 'root_id': source.payload['root_id'],
        'previous_id': latest.object_id, 'version': latest.payload['version'] + 1,
        'turn_id': source.payload['turn_id'], 'task_text': latest.payload['task_text']}
    tx.put(RESTORATIONS, operation, {**lineage, 'operation_id': operation, 'document_id': document['id'],
        'source_document_id': source_document_id, 'source_revision': source_revision,
        'source_birth_revision': bound.revisions['document_revision'], 'source_lineage': deepcopy(source.payload),
        'previous_revision': expected_latest_revision,
        'source_refs': deepcopy(source_record['source_snapshot']['source_refs']),
        'changes': changes, 'created_at': now, 'cited_refs': refs}, expected_revision=0)
    from .material_logical_scope import inherit_material_scope_in_uow
    inherit_material_scope_in_uow(tx, drafts.records, identity=document['id'], project_id=project,
        namespace_id=drafts.documents.namespace_id, source_document_id=source_document_id)
    tx.put(COLLECTION, document['id'], lineage, expected_revision=0)
    if qualified_lineages(tx, project).get(document['id']) is None:
        raise RecognitionConflict('outcome_lineage_binding_invalid')
    from ..document_recognition import ensure_document_experience
    from ..source_egress import recognition_service, validate_product_draft_source
    service = recognition_service(TransactionRecords(tx), product_draft_validator=validate_product_draft_source,
        source_owner=drafts.records)
    ensure_document_experience(enlisted, service, project, document['id'], retained_revision=1)
    if lineage['scene'] is not None:
        from .projects import assign_scene
        assign_scene(TransactionRecords(tx), 'document', document['id'], project, lineage['scene'])
    safe_record_restored_citations(tx, project=project, turn_id=lineage['turn_id'], refs=refs, now=now)
    return {'document_id': document['id'], 'title': document['title'], 'revision': document['revision'],
            'version': lineage['version'], 'changes': changes}


def save_answer_outcome_in_uow(tx, documents, drafts, *, project, turn_id, now):
    from core.document_engine import SQLiteDocumentRepository
    from ..transaction_records import TransactionRecords
    from ..answer_proof import completed_answer_evidence
    from ..document_recognition import ensure_document_experience
    from ..source_egress import recognition_service
    from .task_drafts import TaskDrafts
    from .citation_refs import safe_record_restored_citations
    existing = tx.read(ANSWER_OUTCOMES, turn_id)
    if existing is not None:
        bound = _birth(tx, project, existing.payload.get('document_id'), turn_id)
        if existing.payload.get('project_id') != project or bound is None or bound.answer_birth is None:
            raise RecognitionConflict('saved_answer_birth_changed')
        return {'document_id': existing.payload['document_id']}
    evidence = completed_answer_evidence(tx, WorkScope('local-user', project), turn_id)
    enlisted = SQLiteDocumentRepository(TransactionRecords(tx), namespace_id=documents.namespace_id, now=now)
    document = TaskDrafts(TransactionRecords(tx), enlisted).create_answer_in_uow(tx, project=project,
        turn_id=turn_id, title=evidence['question'][:40], markdown=evidence['answer']['answer'])
    publication = {key: deepcopy(evidence['answer'][key]) for key in ('unverified_claims', 'unverified_claim_spans')
                   if key in evidence['answer']}
    scene = evidence.get('scene')
    tx.put(ANSWER_OUTCOMES, turn_id, {'project_id': project, 'turn_id': turn_id,
        'turn_revision': evidence['public_revision'], 'document_id': document['id'],
        'result_ref': evidence['result_ref'], 'question': evidence['question'], 'scene': scene,
        'created_at': now, 'cited_refs': evidence['cited_refs'], 'publication': publication}, expected_revision=0)
    tx.put(COLLECTION, document['id'], {'project_id': project, 'scene': scene,
        'root_id': document['id'], 'previous_id': None, 'version': 1,
        'turn_id': turn_id, 'task_text': evidence['question']}, expected_revision=0)
    if qualified_lineages(tx, project).get(document['id']) is None:
        raise RecognitionConflict('saved_answer_lineage_invalid')
    ensure_document_experience(enlisted, recognition_service(TransactionRecords(tx), source_owner=drafts.records),
        project, document['id'], retained_revision=1)
    if scene is not None:
        from .projects import assign_scene
        assign_scene(TransactionRecords(tx), 'document', document['id'], project, scene)
    refs = [ref for ref in evidence['cited_refs'] if ref['project_id'] == project
            and ref['kind'] in {'insight', 'document', 'inspiration'}]
    safe_record_restored_citations(tx, project=project, turn_id=turn_id, refs=refs, now=now)
    return {'document_id': document['id']}


def conversation_origin(reader, project, document_id):
    document = reader.read('documents', document_id)
    if document is None or not str(document.payload.get('type', '')).startswith(('agent-result-answer-', 'agent-result-restore-')):
        return None
    try:
        bound = product_draft_source(reader, WorkScope('local-user', project), document_id, 1)
    except ProductDraftDependencyError:
        return None
    if bound is None or bound.answer_origin is None:
        return None
    return {'kind': 'conversation', 'turn_id': bound.public_turn_id,
            'source_refs': deepcopy(bound.answer_origin['cited_refs']),
            'publication': deepcopy(bound.answer_origin['publication']), 'document_revision': 1}
