from contextlib import nullcontext
from copy import deepcopy

from backend.recognition import RecognitionConflict
from backend.recognition.external_turn_facts import completed_answer_facts
from core.ai_kernel import validate_turn_request
from core.ai_kernel.product_admission import MANIFEST_KIND, admission_identity
from ..answer_effective import NativeAnswerSnapshot
from ..answer_proof import read_answer_input
from ..transaction_records import TransactionRecords
from .material_origin_admission import read_origin_item
from .material_origin_readers import qualified_record_read
from .material_origins import ORIGIN_KIND
from .shared_auxiliary import _qualified_commit


def history_dependency(query, row):
    actual = read_model_history_answer(query, row.object_id, row.revision)
    if actual['public'] != row:
        raise RecognitionConflict('shared_history_public_changed')
    return {'turns': [{'id': row.object_id, 'revision': row.revision}], 'chosen': [], 'snapshots': [],
        'preferences': {}, 'multipart': [], 'clarifications': []}


def validate_model_history(query, project, history, target):
    from ..kernel.answer_turns import ACTIVE_ANSWER
    from .material_origin_context import freeze_personal_context, personal_context_materials, context_sql_identities
    from .material_origin_privacy import freeze_origin_materials
    from .privacy import privacy_revision
    if (project != query.origin_admission.lease.project_id
            or history.get('privacy_revision') != privacy_revision(query.execution_owner.records)):
        raise RecognitionConflict('shared_history_authority_changed')
    active = ACTIVE_ANSWER.get()
    if active is None:
        raise RecognitionConflict('shared_history_execution_unavailable')
    request = active[1].get_request(active[0]['turn_id'])
    origins = query.origin_admission.restore(request)
    context = freeze_personal_context(query, request, {'history': history['text']}, history)
    materials = personal_context_materials(query, request, context)
    allowed, privacy, selection = freeze_origin_materials(origins, query.models, project, materials,
        local_only=target['execution_location'] == 'local')
    if (len(allowed) != len(materials) or selection['origin_materials'] != materials
            or target['execution_location'] == 'remote' and not privacy['allow_remote']):
        raise RecognitionConflict('shared_history_source_egress_changed')
    context_sql_identities(query, request, context, [])
    query.validate_retrieval_origin()


def historical_origin(query, snapshot):
    admission = query.origin_admission
    request = snapshot.get_request(snapshot.turn_id)
    bundle = snapshot.restore_admission(request)
    selected = [] if bundle is None else [item for item in bundle.items if item.kind == ORIGIN_KIND]
    if len(selected) != 1 or not isinstance(selected[0].payload, dict):
        raise RecognitionConflict('shared_history_origin_unavailable')
    historical = selected[0].payload.get('execution')
    if not isinstance(historical, dict):
        raise RecognitionConflict('shared_history_origin_changed')
    lease, auxiliary = read_origin_item(request, selected[0], actor_user_id=selected[0].payload.get('actor_user_id'),
        personal=historical)
    # 这里只核历史完成事实的逻辑归属；当前来源许可由新轮实际 lease 提供。
    expected = {key: admission.personal[key] for key in ('owner_user_id', 'namespace_id', 'namespace')}
    if (any(historical.get(key) != value for key, value in expected.items())
            or lease.namespace != query.source_namespace or lease.project_id != admission.lease.project_id
            or request['scope']['project_id'] != admission.lease.project_id or auxiliary is not None):
        raise RecognitionConflict('shared_history_scope_changed')
    return selected[0], lease


def historical_model_inputs(query, snapshot):
    origin, lease = historical_origin(query, snapshot)
    request = snapshot.get_request(snapshot.turn_id)
    result = []
    for event in snapshot.events_after(snapshot.turn_id):
        if event['type'] != 'model.requested':
            continue
        model_id = event['correlation']['model_request_id']
        purpose = event['data'].get('model_call_purpose')
        binding = snapshot.get_immutable_payload(snapshot.turn_id, 'answer-invocation-binding-' + model_id)
        gap = False
        if binding is None:
            binding = snapshot.get_immutable_payload(snapshot.turn_id, 'answer-shared-invocation-binding-' + model_id)
        if binding is None:
            matches = [value for kind, value in snapshot.value['immutable'].items()
                if kind.startswith('answer-gap-binding-') and value[1].get('model_request_id') == model_id]
            if len(matches) == 1:
                binding = matches[0]
                gap = True
        if binding is None or purpose not in {'primary', 'aux'}:
            raise RecognitionConflict('shared_history_model_binding_unavailable')
        bound = binding[1]
        fields = ({'version', 'turn_id', 'invocation_key', 'purpose', 'input_ref', 'route_ref', 'route_revision',
            'model_request_id'} if gap else {'turn_id', 'project_id', 'model_request_id', 'invocation_key',
            'logical_invocation', 'tool_call_id', 'input_ref', 'run_owner_id', 'run_generation', 'request_identity'})
        if (set(bound) not in (fields, fields | {'phase', 'phase_logical'})
                or bound.get('turn_id') != snapshot.turn_id or bound.get('model_request_id') != model_id
                or not isinstance(bound.get('input_ref'), str)
                or 'request_identity' in bound and bound['request_identity'] != admission_identity(request)
                or 'tool_call_id' in bound and bound['tool_call_id'] != event['correlation']['tool_call_id']
                or not gap and (bound['project_id'] != request['scope']['project_id']
                    or not isinstance(bound['run_owner_id'], str) or not bound['run_owner_id']
                    or type(bound['run_generation']) is not int or bound['run_generation'] < 1)):
            raise RecognitionConflict('shared_history_model_binding_changed')
        matches = [(kind, value) for kind, value in snapshot.value['immutable'].items()
            if value[0] == bound['input_ref']]
        if len(matches) != 1 or not matches[0][0].startswith(('answer-model-input-', 'answer-gap-input-')):
            raise RecognitionConflict('shared_history_model_input_unavailable')
        kind, (reference, value) = matches[0]
        if (not isinstance(bound['invocation_key'], str) or not bound['invocation_key']
                or kind != ('answer-gap-input-' + bound['invocation_key'] + '-v1' if gap
                    else 'answer-model-input-' + bound['invocation_key'])):
            raise RecognitionConflict('shared_history_model_input_changed')
        input_fields = ({'messages', 'purpose', 'response_schema', 'material_proof'}
            | ({'phase', 'phase_logical'} if 'phase' in value else set()))
        if kind.startswith('answer-gap-input-'):
            input_fields = {'messages', 'material_proof', 'policy_versions', 'purpose', 'response_schema',
                'max_tokens', 'route_ref', 'route_revision'}
        proof = value.get('material_proof')
        if (set(value) != input_fields or value.get('purpose') != purpose
                or not isinstance(proof, dict) or set(proof) != {'privacy', 'selection'}):
            raise RecognitionConflict('shared_history_material_proof_unavailable')
        logical = bound.get('phase_logical', bound.get('logical_invocation', bound['invocation_key']))
        if 'phase' in value:
            if (purpose != 'primary' or bound.get('phase') not in {'single', 'first', 'final-review'}
                    or bound['phase'] != value['phase'] or logical != value['phase_logical']):
                raise RecognitionConflict('shared_history_model_binding_changed')
        elif not gap:
            from ..answer_legacy import read_legacy_input
            legacy = read_legacy_input(snapshot, snapshot.turn_id, bound['logical_invocation'])
            if legacy is not None:
                logical = legacy['intent'][1]['logical_invocation']
        selection = proof['selection']
        primary_fields = {'origin_ref', 'origin_materials', 'search_fragments', 'personal_context',
            'schema_version', 'sql_identities'}
        rag_fields = primary_fields | {'privacy', 'candidates', 'skills', 'history', 'profile', 'tools', 'search'}
        if (not isinstance(selection, dict) or selection.get('origin_ref') != origin.ref
                or set(selection) not in (primary_fields, rag_fields)
                or type(selection.get('schema_version')) is not int or selection['schema_version'] != 2
                or not isinstance(selection.get('origin_materials'), list)
                or not isinstance(selection.get('sql_identities'), list)):
            raise RecognitionConflict('shared_history_material_proof_changed')
        if set(selection) == rag_fields and selection['privacy'] != proof['privacy']:
            raise RecognitionConflict('shared_history_material_proof_changed')
        validate_turn_request({**request, 'privacy': proof['privacy']})
        from .material_origin_sql import validate_sql_shape
        validate_sql_shape(selection['sql_identities'])
        for material in selection['origin_materials']:
            if (not isinstance(material, dict)
                    or set(material) != {'type', 'id', 'revision', 'project_id', 'namespace'}
                    or (material['project_id'], material['namespace']) not in {
                        (lease.project_id, lease.namespace), ('me', query.origin_admission.personal['namespace'])}):
                raise RecognitionConflict('shared_history_material_scope_changed')
        descriptors = proof['privacy']['material_refs']
        if (len(descriptors) != len(selection['origin_materials'])
                or any({key: material[key] for key in ('type', 'id', 'revision', 'project_id')} != descriptor
                    for material, descriptor in zip(selection['origin_materials'], descriptors))):
            raise RecognitionConflict('shared_history_material_proof_changed')
        result.append({'kind': kind, 'input_ref': reference, 'binding_ref': binding[0],
            'model_request_id': model_id, 'payload': deepcopy(value), 'logical': logical,
            'invocation_key': bound['invocation_key']})
    return result


def read_model_history_answer(query, public_id, expected_revision, *, reader=None):
    admission = query.origin_admission
    admission.validate_execution_owner()
    query.validate_retrieval_origin()
    with _qualified_commit(admission) as qualification:
        context = (qualified_record_read(admission.destination.records, validate=qualification._validate)
            if reader is None else nullcontext(reader))
        with context as transaction:
            view = transaction if isinstance(transaction, TransactionRecords) else TransactionRecords(transaction)
            qualification._actor_reader(view)
            public = view.read('v2_turns', public_id)
            if (public is None or public.revision != expected_revision
                    or public.payload.get('project_id') != admission.lease.project_id
                    or public.payload.get('intent') != 'ask'):
                raise RecognitionConflict('shared_history_public_changed')
            try:
                facts = completed_answer_facts(view, project_id=admission.lease.project_id, turn_id=public_id)
            except (ValueError, KeyError, TypeError) as error:
                raise RecognitionConflict('shared_history_completion_unavailable') from error
            value = facts.get('native_snapshot')
            if value is None:
                raise RecognitionConflict('shared_history_native_snapshot_unavailable')
            snapshot = NativeAnswerSnapshot(value)
            origin, lease = historical_origin(query, snapshot)
            model_inputs = historical_model_inputs(query, snapshot)
            if facts['request'] != snapshot.get_request(snapshot.turn_id) or snapshot.turn_id != public_id:
                raise RecognitionConflict('shared_history_native_changed')
            try:
                selected = read_answer_input(snapshot, snapshot.turn_id)
            except (ValueError, KeyError, TypeError) as error:
                raise RecognitionConflict('shared_history_input_unavailable') from error
            if selected is None and (facts['answer'].get('no_match') is not True
                    or facts['answer'].get('citations') != [] or facts['result'].get('chosen') != []
                    or any(event['type'] == 'model.requested' and event['data'].get('model_call_purpose') == 'primary'
                        for event in snapshot.events_after(snapshot.turn_id))):
                raise RecognitionConflict('shared_history_input_unavailable')
            manifest = snapshot.get_immutable_payload(snapshot.turn_id, MANIFEST_KIND)
            identity = snapshot.get_immutable_payload(snapshot.turn_id, 'workbench-steer-binding-v1')
            if manifest is None or identity is None:
                raise RecognitionConflict('shared_history_identity_unavailable')
            binding = identity[1]
            if (binding.get('owner_user_id') != admission.destination.owner_user_id
                    or binding.get('namespace_id') != admission.destination.namespace
                    or binding.get('target_turn_id') != public_id or binding.get('turn_id') != snapshot.turn_id
                    or binding.get('thread_id') != public.payload.get('thread_id')
                    or binding.get('project_id') != admission.lease.project_id):
                raise RecognitionConflict('shared_history_identity_changed')
            qualification._actor_reader(view)
            return {'facts': facts, 'snapshot': snapshot, 'origin': origin, 'lease': lease,
                'input': selected, 'public': deepcopy(public), 'model_inputs': model_inputs}
