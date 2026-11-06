from copy import deepcopy
import json

from backend.recognition import RecognitionConflict, WorkScope
from core.ai_kernel.product_admission import admission_identity
from core.ai_kernel.sqlite_store import SQLiteAITurnStore

from ..transaction_records import TransactionRecords


PREFIX = 'outcome-input-preparation-v3-'
BASIS = 'outcome-input-basis-v3-'
ANSWER_BINDING = 'product-answer-source-input-'


def _material(kind, identity, revision, project, *, role, proof_ref, snapshot=None, namespace=None):
    if (kind not in {'recognition', 'experience', 'document', 'original_item', 'original_source'}
            or not isinstance(identity, str) or not identity or type(revision) is not int or revision < 1
            or not isinstance(project, str) or not project):
        raise RecognitionConflict('outcome_context_material_invalid')
    result = {'type': kind, 'id': identity, 'revision': revision, 'project_id': project,
        'role': role, 'proof_ref': proof_ref, 'snapshot': deepcopy(snapshot)}
    if namespace is not None:
        if not isinstance(namespace, str) or not namespace:
            raise RecognitionConflict('outcome_context_namespace_invalid')
        result['namespace'] = namespace
    return result


def _selected(candidate, project, proof_ref, *, role='evidence'):
    entry = candidate.get('entry', {})
    kind = candidate.get('kind')
    identity = candidate.get('id')
    own = candidate.get('project_id', entry.get('project_id', project))
    if kind == 'source':
        kind = 'original_item' if entry.get('item_id') else 'original_source'
        identity = entry.get('item_id') or entry.get('source_id') or identity
    if candidate.get('inspiration'):
        kind = 'experience'
    if kind == 'document':
        identity = entry.get('document_id') or identity
    revision = entry.get('item_revision') if kind == 'original_item' else entry.get('revision')
    return _material(kind, identity, revision, own, role=role, proof_ref=proof_ref,
        snapshot=candidate.get('snapshot'), namespace=candidate.get('namespace'))


def _answer_context(native, request):
    from ..kernel.answer_continuations import decode
    saved = native.get_immutable_payload(request['turn_id'], 'answer-continuation-plan-v1')
    if saved is None:
        raise RecognitionConflict('outcome_answer_context_unavailable')
    capsule = saved[1]
    if (capsule.get('request') != request or capsule.get('turn_id') != request['turn_id']
            or capsule.get('project_id') != request['scope']['project_id']
            or capsule.get('question') != request['input']['text']):
        raise RecognitionConflict('outcome_answer_context_changed')
    decoded = decode(capsule['capsule'])
    plan = decoded.get('plan') if isinstance(decoded, dict) else None
    project = request['scope']['project_id']
    if (not isinstance(plan, dict) or plan.get('project_id') != project
            or plan.get('scope') != WorkScope('local-user', project)
            or plan.get('question') != request['input']['text']
            or plan.get('policy_versions') != request['policy_versions']):
        raise RecognitionConflict('outcome_answer_context_changed')
    materials = [_selected(item, project, saved[0], role='method' if item.get('supplemented') else 'evidence')
        for item in plan.get('chosen', []) if item.get('kind') != 'search']
    unavailable = [{'id': item.get('id'), 'proof_ref': saved[0], 'reason': 'outcome_search_source_requires_birth_proof'}
        for item in plan.get('chosen', []) if item.get('kind') == 'search']
    materials.extend(_material('recognition', item['id'], item['revision'], 'me',
        role='profile', proof_ref=saved[0], snapshot=item.get('snapshot'))
        for item in plan.get('profile', {}).get('items', []))
    return {'turn_id': request['turn_id'], 'plan_ref': saved[0], 'input_ref': None,
        'materials': materials, 'recognitions': [item for item in materials if item['type'] == 'recognition'],
        'proof_refs': [saved[0]], 'unverifiable': unavailable}


def answer_input_context(service, native, request, input_kind, messages, reader):
    from .outcome_source_reads import SourceReadRecords
    if not isinstance(reader, SourceReadRecords):
        return _answer_context(native, request)
    from .model_material_coverage import (
        build_primary_material_proof, frozen_primary_plan, validate_primary_material_proof_shape,
    )
    reader.validate(service)
    actual = native.get_immutable_payload(request['turn_id'], input_kind)
    if (actual is None or not input_kind.startswith('answer-model-input-')
            or actual[1].get('messages') != messages or actual[1].get('purpose') != 'primary'):
        raise RecognitionConflict('outcome_shared_answer_input_unavailable')
    proof = actual[1].get('material_proof')
    validate_primary_material_proof_shape(proof, native_request=request)
    invocation = input_kind.removeprefix('answer-model-input-')
    logical = actual[1].get('phase_logical', invocation)
    query = reader.query
    frozen, _, _ = frozen_primary_plan(query, request, logical)
    rebuilt = build_primary_material_proof(query, request, logical, messages,
        plan=frozen, invocation_key=invocation)
    if rebuilt != proof:
        raise RecognitionConflict('outcome_shared_answer_material_changed')
    reader.qualification.validate_sql(proof['selection']['sql_identities'], actor_reader=reader._plain)
    material_roles = {}
    for candidate in frozen['chosen']:
        if candidate['kind'] == 'search':
            raise RecognitionConflict('outcome_search_source_requires_birth_proof')
        selected = _selected(candidate, frozen['project_id'], actual[0],
            role='method' if candidate.get('supplemented') else 'evidence')
        key = tuple(selected[field] for field in ('type', 'id', 'revision', 'project_id'))
        namespace = candidate.get('namespace', query.source_namespace)
        material_roles[(*key, namespace)] = selected['role']
    personal = query.origin_admission.personal['namespace']
    for item in frozen['profile'].get('items', []):
        material_roles[('recognition', item['id'], item['revision'], 'me', personal)] = 'profile'
    materials = []
    for item in proof['selection']['origin_materials']:
        fields = ('type', 'id', 'revision', 'project_id', 'namespace')
        if not isinstance(item, dict) or set(item) != set(fields):
            raise RecognitionConflict('outcome_material_namespace_unavailable')
        key = tuple(item[field] for field in fields)
        materials.append(_material(item['type'], item['id'], item['revision'], item['project_id'],
            namespace=item['namespace'], role=material_roles.get(key, 'history'), proof_ref=actual[0]))
    return {'turn_id': request['turn_id'], 'plan_ref': None, 'input_ref': None,
        'materials': materials, 'recognitions': [item for item in materials if item['type'] == 'recognition'],
        'proof_refs': [actual[0]], 'unverifiable': []}


def _contains(messages, text):
    return bool(text) and any(item.get('content') == text for item in messages)


def _dependency_visible(messages, source, text):
    if _contains(messages, text):
        return True
    for message in messages:
        try:
            frame = json.loads(message.get('content', ''))
        except (ValueError, TypeError):
            continue
        if not isinstance(frame, dict) or frame.get('input') != source['input'] or frame.get('scope') != source['scope']:
            continue
        try:
            product = json.loads(frame['input']['text'])
        except (ValueError, TypeError):
            continue
        if isinstance(product, dict) and product.get('dependency_context') == text and text:
            return True
    return False


def _record_proof(row):
    return {'collection': row.collection, 'object_id': row.object_id, 'revision': row.revision}


def _task_context(service, native, request, source, messages, reader):
    from ..research_packets import authority_stores
    from ..research_reads import product_read_sources, read_dependencies
    from ..source_evidence_refs import READS
    from .policies import get
    from .outcome_source_reads import SourceReadRecords
    shared = isinstance(reader, SourceReadRecords)
    task_materials = None
    if shared:
        from .shared_task_materials import read_task_material_item
        task_materials = read_task_material_item(reader.query, source, reader=reader)
    project = source['scope']['project_id']
    materials, proofs = [], []
    try:
        product = json.loads(source['input']['text'])
    except (ValueError, TypeError):
        product = None
    basis = (product.get('outcome_input') or {}).get('update_basis') if isinstance(product, dict) else None
    update = (service.validate_update_basis(project=project, document_id=basis['document_id'], basis=basis, reader=reader)
        if isinstance(basis, dict) else None)
    update_visible = False
    update_policy = get('outcome_update', version=basis['update_policy']) if update is not None else None
    history_message = update_policy('messages', update)[1] if update is not None else None
    profile = reader.read('v2_task_profiles', source['turn_id'])
    if profile is not None:
        value = profile.payload
        if (profile.revision != 1 or value.get('project_id') != project
                or value.get('input_refs') != source['input']['refs']):
            raise RecognitionConflict('outcome_task_profile_changed')
        block = value.get('profile', {})
        if _contains(messages, block.get('text')):
            proof = _record_proof(profile)
            proofs.append(proof)
            materials.extend(_material('recognition', item['id'], item['revision'], 'me',
                role='profile', proof_ref=proof, snapshot=item.get('snapshot'),
                namespace=reader.query.origin_admission.personal['namespace'] if shared else None)
                for item in block.get('items', []))
    style = reader.read('v2_task_styles', source['turn_id'])
    if style is not None:
        value = style.payload
        if (style.revision != 1 or value.get('project_id') != project or value.get('input_refs') != source['input']['refs']):
            raise RecognitionConflict('outcome_task_style_changed')
        block = value.get('style', {})
        if _contains(messages, block.get('text')):
            proof = _record_proof(style)
            proofs.append(proof)
            materials.extend(_material('recognition', item['id'], item['revision'], item['project_id'],
                role='style', proof_ref=proof, snapshot=item.get('snapshot'), namespace=item.get('namespace'))
                for item in block.get('items', []))
    methods = reader.read('v2_task_methods', source['turn_id'])
    if methods is not None:
        value = methods.payload
        if (methods.revision != 1 or value.get('project_id') != project
                or value.get('input_refs') != source['input']['refs']
                or value.get('policy_versions') != source['policy_versions']):
            raise RecognitionConflict('outcome_task_methods_changed')
        if _contains(messages, value.get('text')):
            proof = _record_proof(methods)
            proofs.append(proof)
            materials.extend(_selected(item, project, proof, role='method') for item in value.get('methods', []))
            materials.extend(_selected(item, project, proof, role='inspiration') for item in value.get('inspirations', []))
        pack = value.get('situation_pack') or {}
        if _contains(messages, pack.get('text')):
            proof = _record_proof(methods)
            if proof not in proofs:
                proofs.append(proof)
            materials.extend(_selected(item, project, proof, role='method') for item in pack['members'])
    context = reader.read('v2_part_contexts', source['turn_id'])
    if context is not None and _dependency_visible(messages, source, context.payload.get('context', {}).get('text')):
        from .part_context import validate_bound_context
        validate_bound_context(reader, service.application.state.recognition_models, source,
            query=reader.query if shared else service.application.state.workspace_domains.query)
        proof = _record_proof(context)
        proofs.append(proof)
        if shared:
            bound = context.payload['context']
            if bound.get('scope_kind') != 'shared' or not isinstance(bound.get('shared_answers'), list):
                raise RecognitionConflict('outcome_task_dependency_namespace_unavailable')
            for item in bound['shared_materials']:
                if item not in task_materials['selection']['origin_materials']:
                    raise RecognitionConflict('outcome_task_dependency_admission_changed')
                materials.append(_material(item['type'], item['id'], item['revision'], item['project_id'],
                    role='dependency', proof_ref=proof, namespace=item['namespace']))
        for original in context.payload['context'].get('originals', []):
            row = reader.read('workspace_items', original['id'])
            if row is None:
                raise RecognitionConflict('outcome_task_original_unavailable')
            materials.append(_material('original_item', row.object_id, row.revision,
                row.payload['project_id'], role='dependency', proof_ref=proof,
                namespace=original.get('namespace') if shared else None))
        for explicit in context.payload['context'].get('explicit_sources', []):
            for root in explicit.get('snapshot', {}).get('roots', []):
                materials.append(_material(root['type'], root['id'], root['revision'], explicit['project_id'],
                    role='dependency', proof_ref=proof, snapshot=explicit['snapshot'],
                    namespace=explicit.get('namespace') if shared else None))
    visible = set()
    actual_events = {event['event_id']: event for event in native.events_after(request['turn_id'])}
    for message in messages:
        text = message.get('content')
        if not isinstance(text, str):
            continue
        if update is not None and message == history_message:
            update_visible = True
        try:
            body = json.loads(text)
        except (ValueError, TypeError):
            continue
        if not isinstance(body, dict) or not isinstance(body.get('events'), list):
            continue
        visible_input = body.get('input')
        if update is not None and request['turn_id'] == source['turn_id']:
            original_frame = {**deepcopy(body), 'input': deepcopy(request['input'])}
            projected = update_policy('planner_input', {'text': json.dumps(original_frame, ensure_ascii=False),
                'original_input': request['input'], 'history': update})
            if text != projected:
                raise RecognitionConflict('outcome_update_rendered_input_changed')
            expected_input = json.loads(projected)['input']
            update_visible = True
        else:
            expected_input = request['input']
        if (body.get('scope') != request['scope'] or body.get('goal') != request['desired_outcome']
                or not isinstance(visible_input, dict) or set(visible_input) != set(request['input'])
                or any(visible_input[key] != value for key, value in expected_input.items() if key != 'text')
                or not isinstance(visible_input.get('text'), str)):
            raise RecognitionConflict('outcome_planner_context_changed')
        visible_payloads = set()
        for event in body['events']:
            if not isinstance(event, dict):
                continue
            actual = actual_events.get(event.get('event_id'))
            matched = actual is not None and {key: item for key, item in event.items() if key != 'resolved_payload'} == actual
            reference = actual['data'].get('payload_ref') if matched else None
            if reference and 'resolved_payload' in event:
                expected_payload = native.get(reference)
                if update is not None:
                    expected_payload = update_policy('payload', {'value': expected_payload,
                        'original_input': source['input'], 'history': update})
                if expected_payload == event['resolved_payload']:
                    visible_payloads.add(reference)
            if event.get('type') != 'tool.outcome.recorded':
                continue
            if not matched:
                raise RecognitionConflict('outcome_planner_event_changed')
            outcome = native.get(reference) if reference else None
            if isinstance(outcome, dict) and outcome.get('capability_id') not in READS:
                continue
            if reference not in visible_payloads:
                raise RecognitionConflict('outcome_planner_result_changed')
            if isinstance(outcome, dict) and outcome.get('status') == 'completed':
                visible.add(outcome.get('invocation_id'))
    if update_visible:
        proof = {'collection': 'v2_outcome_update_preparations', 'object_id': basis['preparation_id'], 'revision': 1}
        proofs.append(proof)
        for item in update['current_sources']:
            frozen = item['frozen']
            materials.append(_material(frozen['source_type'], frozen['id'], frozen['revision'], frozen['project_id'],
                role='update', proof_ref=proof, snapshot=item['snapshot'],
                namespace=item['origin']['namespace'] if shared else None))
    if visible:
        view = TransactionRecords(reader)
        turns, agents = authority_stores(view)
        if not shared:
            product_read_sources(view, turns, agents, source, project)
        for proof in read_dependencies(view, turns, agents, source['turn_id'], project):
            if proof['id'] not in visible:
                continue
            reference = {'collection': 'v2_research_source_reads', 'object_id': proof['id'], 'revision': proof['revision']}
            proofs.append(reference)
            if shared:
                reads = getattr(service.application.state, 'product_shared_task_reads', None)
                if reads is None:
                    raise RecognitionConflict('shared_task_reads_unavailable')
                qualified = reads.qualified_proof(proof, reader=reader, root_request=source)
                reference['shared_read'] = deepcopy(qualified['entry'])
                for item in qualified['materials']:
                    materials.append(_material(item['type'], item['id'], item['revision'], item['project_id'],
                        role='read', proof_ref=reference, namespace=item['namespace']))
                continue
            for item in (proof.get('v2_recall') or {}).get('materials', []):
                kind = {'source': 'original_source'}.get(item['kind'], item['kind'])
                materials.append(_material(kind, item['id'], item['revision'], item['project_id'],
                    role='read', proof_ref=reference, snapshot=item.get('snapshot')))
            for item in proof.get('documents', []):
                materials.append(_material('document', item['id'], item['revision'], proof['project_id'],
                    role='read', proof_ref=reference))
            snapshot = proof.get('source_egress')
            if snapshot:
                for root in snapshot['roots']:
                    materials.append(_material(root['type'], root['id'], root['revision'], snapshot['scope']['project_id'],
                        role='read', proof_ref=reference, snapshot=snapshot))
    for material in materials:
        if shared and not isinstance(material.get('namespace'), str):
            raise RecognitionConflict('outcome_task_material_namespace_unavailable')
        if material['role'] in {'profile', 'method', 'inspiration', 'style'}:
            descriptor = {key: material[key] for key in ('type', 'id', 'revision', 'project_id')}
            if (descriptor not in source['privacy']['material_refs']
                    or material['snapshot'] not in source['privacy']['source_snapshots']):
                raise RecognitionConflict('outcome_task_source_binding_changed')
            if shared and {key: material[key] for key in ('type', 'id', 'revision', 'project_id', 'namespace')} not in task_materials['selection']['origin_materials']:
                raise RecognitionConflict('outcome_task_source_binding_changed')
    result = {'turn_id': request['turn_id'], 'plan_ref': None, 'input_ref': None, 'materials': materials,
        'recognitions': [item for item in materials if item['type'] == 'recognition'], 'proof_refs': proofs}
    if shared:
        from .shared_task_materials import KIND
        reference = native.get_immutable_payload(source['turn_id'], KIND)[0]
        result['privacy_materials'] = [_material(item['type'], item['id'], item['revision'], item['project_id'],
            role='initial_admission', proof_ref=reference, namespace=item['namespace'], snapshot=snapshot)
            for item, snapshot in zip(task_materials['selection']['origin_materials'],
                task_materials['privacy']['source_snapshots'], strict=True)]
        from .shared_task_redo import inherited_sources, KIND as REDO_KIND
        inherited = inherited_sources(reader.query, source, reader=reader)
        if inherited:
            result['inherited_sources'] = inherited
            proofs.append({'shared_redo': {'turn_id': source['turn_id'],
                'ref': native.get_immutable_payload(source['turn_id'], REDO_KIND)[0]}})
    return result


def prepare(service, *, request, input_kind, messages, reader=None, source_request=None,
            source_request_ref=None, ancestry_root=None):
    native = service._native()
    source = source_request or request
    if (native.get_request(request['turn_id']) != request or native.get_request(source['turn_id']) != source
            or not isinstance(messages, list) or not messages or any(not isinstance(item, dict) for item in messages)):
        raise RecognitionConflict('outcome_input_owner_changed')
    if source != request:
        from .task_model_inputs import task_profile_chain, ROOT_KIND
        composition = service.application.state.agent_runtime_composition
        chain = task_profile_chain(native, composition.store, request)
        if (chain[-1] != source or ancestry_root != source['turn_id']
                or native.get_immutable_payload(source['turn_id'], ROOT_KIND) != (source_request_ref, {'request': source})):
            raise RecognitionConflict('outcome_input_ancestry_changed')
    with service._read_scope(reader, request=source) as view:
        context = (answer_input_context(service, native, request, input_kind, messages, view)
            if request['desired_outcome'] == 'project.answer'
            else _task_context(service, native, request, source, messages, view))
        sources = []
        for material in [*context['materials'], *context.get('privacy_materials', [])]:
            owner = service._material_owner(material, view)
            sources.extend(service._freeze_material(owner, material, reader=view))
        sources.extend(deepcopy(context.get('inherited_sources', [])))
        payload = {'version': 3, 'turn_id': request['turn_id'], 'request_identity': admission_identity(request),
            'owner': deepcopy(service.owner), 'scope_origin': service._origin(service._source_owner(source['scope']['project_id'])),
            'input_kind': input_kind, 'messages': deepcopy(messages), 'source_request': deepcopy(source),
            'source_request_ref': source_request_ref, 'ancestry_root': ancestry_root, 'context': context, 'sources': sources}
        service._validate()
        return native.get_or_create_immutable_payload(request['turn_id'], PREFIX + input_kind, payload)


def read(native, request, reference, *, input_kind, input_ref=None):
    saved = native.get_immutable_payload(request['turn_id'], PREFIX + input_kind)
    if saved is None or saved[0] != reference:
        raise RecognitionConflict('outcome_input_preparation_unavailable')
    value = saved[1]
    if (value.get('version') != 3 or value.get('turn_id') != request['turn_id']
            or value.get('request_identity') != admission_identity(request) or value.get('input_kind') != input_kind
            or native.get_request(request['turn_id']) != request):
        raise RecognitionConflict('outcome_input_preparation_changed')
    if input_ref is not None:
        original = native.get_immutable_payload(request['turn_id'], input_kind)
        if original is None or original[0] != input_ref or original[1].get('messages') != value['messages']:
            raise RecognitionConflict('outcome_final_input_changed')
        if request['desired_outcome'] == 'project.answer':
            binding = native.get_immutable_payload(request['turn_id'], ANSWER_BINDING + input_kind)
            if (binding is None or set(binding[1]) != {'turn_id', 'input_kind', 'input_ref', 'preparation_ref', 'model_request_id'}
                    or binding[1]['turn_id'] != request['turn_id'] or binding[1]['input_kind'] != input_kind
                    or binding[1]['input_ref'] != input_ref or binding[1]['preparation_ref'] != reference
                    or not isinstance(binding[1]['model_request_id'], str) or not binding[1]['model_request_id']):
                raise RecognitionConflict('outcome_answer_source_binding_changed')
        elif original[1].get('outcome_source_preparation_ref') != reference:
            raise RecognitionConflict('outcome_final_input_changed')
        if input_kind.startswith('product-task-model-input-v1-') and (
                original[1].get('context_sources') != value['context']['materials']
                or original[1].get('context_proof_refs') != value['context']['proof_refs']):
            raise RecognitionConflict('outcome_final_context_changed')
    return deepcopy(value)


def read_model_context(native, request, *, input_ref=None, input_kind=None):
    if not isinstance(native, SQLiteAITurnStore):
        from ..answer_effective import NativeAnswerSnapshot
        if type(native) is not NativeAnswerSnapshot:
            raise RecognitionConflict('outcome_context_native_owner_required')
    if native.get_request(request['turn_id']) != request:
        raise RecognitionConflict('outcome_context_native_owner_changed')
    if input_ref is None:
        events = tuple(native.events_after(request['turn_id']))
        empty = {'turn_id': request['turn_id'], 'plan_ref': None, 'input_ref': None,
            'materials': [], 'recognitions': [], 'proof_refs': [], 'context_state': 'not_yet_consumed',
            'observed_sequence': events[-1]['sequence'] if events else 0}
        if request['desired_outcome'] != 'project.answer':
            if any(event['type'] == 'model.attempt.dispatched' for event in events):
                raise RecognitionConflict('outcome_context_input_required')
            return empty
        if native.get_immutable_payload(request['turn_id'], 'answer-continuation-plan-v1') is None:
            if any(event['type'] == 'model.requested' and event['data'].get('model_call_purpose') == 'primary'
                    for event in events):
                raise RecognitionConflict('outcome_answer_context_unavailable')
            return empty
        return _answer_context(native, request)
    original = native.get(input_ref)
    if request['desired_outcome'] == 'project.answer':
        if not isinstance(input_kind, str):
            raise RecognitionConflict('outcome_answer_input_kind_required')
        binding = native.get_immutable_payload(request['turn_id'], ANSWER_BINDING + input_kind)
        reference = binding[1].get('preparation_ref') if binding is not None else None
    else:
        reference = original.get('outcome_source_preparation_ref') if isinstance(original, dict) else None
    if not isinstance(reference, str):
        raise RecognitionConflict('outcome_context_preparation_required')
    prepared = native.get(reference)
    if not isinstance(prepared, dict) or not isinstance(prepared.get('input_kind'), str):
        raise RecognitionConflict('outcome_context_preparation_changed')
    value = read(native, request, reference, input_kind=prepared['input_kind'], input_ref=input_ref)
    return {**value['context'], 'input_ref': input_ref, 'input_kind': prepared['input_kind'],
        'preparation_ref': reference, 'source_proofs': deepcopy(value['sources'])}
