from contextlib import nullcontext
from copy import deepcopy

from backend.recognition import RecognitionConflict, WorkScope
from core.ai_kernel.product_admission import ADMISSION_KINDS, MANIFEST_KIND, ProductAdmissionItem, validate_bundle
from core.ai_kernel.product_snapshot import admission_base_refs
from core.document_engine import SQLiteDocumentRepository

from ..original_sources import resolve_turn_material
from ..transaction_records import TransactionRecords
from .material_origin_admission import BoundMaterialOrigins, origin_reference, personal_origin
from .material_origin_privacy import freeze_origin_materials, validate_origin_privacy
from .material_origin_readers import qualified_record_read
from .material_origin_sql import freeze_sql_identities
from .shared_auxiliary import _qualified_commit
from .shared_rendered_preparation import _query_current


KIND = 'shared-task-materials-v1'
MATERIAL_FIELDS = frozenset({'type', 'id', 'revision', 'project_id', 'namespace'})
PROOF_FIELDS = frozenset({'schema_version', 'privacy', 'selection', 'bodies'})


def _origins(query, request):
    _query_current(query)
    admission = query.origin_admission
    if (request.get('desired_outcome') != 'project.task'
            or request.get('scope', {}).get('project_id') != admission.lease.project_id
            or query.models is not admission.destination.workspace.query.models):
        raise RecognitionConflict('shared_task_material_owner_changed')
    stored = admission._store().get_request(request['turn_id'])
    if stored is not None:
        if stored != request:
            raise RecognitionConflict('shared_task_material_request_changed')
        return admission.restore(request)
    return BoundMaterialOrigins(admission, admission.lease, admission._reference(request))


def _actor_material(query, request, material, reader):
    from backend.recognition.material_logical_scope import require_material_logical_scope
    admission = query.origin_admission
    stored = admission._store().get_request(request['turn_id']) if request is not None else None
    selected = material
    if material['type'] == 'experience':
        from backend.recognition.product_draft_dependencies import read_product_draft_dependencies
        row = reader.read('recognition_experiences', material['id'])
        if row is None or row.revision != material['revision']:
            raise RecognitionConflict('shared_task_actor_material_scope_changed')
        draft = read_product_draft_dependencies(reader, WorkScope('local-user', material['project_id']), row.payload)
        refs = row.payload.get('provenance', {}).get('source_refs', [])
        documents = [ref for ref in refs if ref.get('type') == 'document']
        if draft is None or len(documents) != 1:
            raise RecognitionConflict('shared_task_actor_material_scope_changed')
        selected = {**material, 'type': 'document', 'id': documents[0]['id'], 'revision': documents[0]['revision']}
    actual = require_material_logical_scope(reader, material_type={
        'original_item': 'workspace_item'}.get(selected['type'], selected['type']),
        identity=selected['id'], project_id=selected['project_id'], revision=selected['revision'],
        namespace_id=admission.destination.namespace, source_request=request if stored is not None else None,
        scope_kind='shared')
    if (actual['execution'] != personal_origin(admission.destination)
            or actual['origin']['payload']['shared']['namespace'] != query.source_namespace):
        raise RecognitionConflict('shared_task_actor_material_scope_changed')


def _capture(query, origins, request, materials, *, local_only, reader=None):
    if type(local_only) is not bool or not isinstance(materials, (tuple, list)):
        raise RecognitionConflict('shared_task_materials_invalid')
    values = deepcopy(list(materials))
    if any(not isinstance(item, dict) or set(item) != MATERIAL_FIELDS for item in values):
        raise RecognitionConflict('shared_task_material_namespace_required')
    from .material_origin_readers import qualified_actor_reader
    qualified_actor_reader(origins, reader)
    bodies = []
    with _qualified_commit(origins.admission) as qualification:
        context = (qualified_record_read(origins.admission.destination.records, validate=qualification._validate)
            if reader is None else nullcontext(reader))
        with context as transaction:
            actor_reader = TransactionRecords(transaction)
            qualification._actor_reader(actor_reader)
            allowed, privacy, selection = freeze_origin_materials(origins, query.models, origins.lease.project_id,
                values, local_only=local_only, reader=actor_reader)
            if len(allowed) != len(values) or selection['origin_materials'] != values:
                raise RecognitionConflict('shared_task_material_not_qualified')
            for material in values:
                owner = origins.owner(material['project_id'], material['namespace'])
                source_reader = TransactionRecords(qualification.reader_for(origins, owner, actor_reader=actor_reader))
                if owner is origins.actor:
                    _actor_material(query, request, material, actor_reader)
                descriptor = {key: material[key] for key in ('type', 'id', 'revision', 'project_id')}
                resolved, _ = resolve_turn_material(source_reader, WorkScope('local-user', owner.project_id), descriptor)
                markdown = (SQLiteDocumentRepository(source_reader, namespace_id=owner.documents.namespace_id).markdown(
                    material['id'], revision=material['revision']) if material['type'] == 'document' else None)
                from ..source_evidence_refs import qualified_material_ref
                bodies.append({'material': material, 'payload': deepcopy(resolved['payload']),
                    'markdown': markdown, 'ref': deepcopy(resolved['ref']),
                    'qualified_ref': {**deepcopy(resolved['ref']), 'uri': qualified_material_ref(material)}})
            selection = {**selection, 'schema_version': 2,
                'sql_identities': freeze_sql_identities(origins, values, actor_reader=actor_reader)}
            qualification.validate_sql(selection['sql_identities'], actor_reader=actor_reader)
            origins.validate_current()
    return {'schema_version': 1, 'privacy': privacy, 'selection': selection, 'bodies': bodies}


def prepare_task_materials(query, *, project, turn_id, materials, local_only):
    _query_current(query)
    admission = query.origin_admission
    if (project != admission.lease.project_id or not isinstance(turn_id, str) or not turn_id
            or admission._store().get_request(turn_id) is not None):
        raise RecognitionConflict('shared_task_material_preparation_changed')
    reference = origin_reference(admission.actor.user_id, admission.destination.namespace, turn_id)
    origins = BoundMaterialOrigins(admission, admission.lease, reference)
    return _capture(query, origins, None, materials, local_only=local_only)


def freeze_task_request(query, *, materials, **values):
    from ..kernel.turn_requests import freeze_product_turn
    proof = prepare_task_materials(query, project=values['project_id'], turn_id=values['turn_id'],
        materials=materials, local_only=values.get('local_only', False))

    def freeze(records, models, project, requested, **options):
        if (records is not query.receipt_records or models is not query.models
                or project != query.origin_admission.lease.project_id or list(requested) != list(materials)
                or options.get('purpose') != 'generation'):
            raise RecognitionConflict('shared_task_material_preparation_changed')
        return [{'ref': deepcopy(row['qualified_ref']), 'payload': deepcopy(row['payload']),
            'namespace': row['material']['namespace']} for row in proof['bodies']], deepcopy(proof['privacy'])

    def validate(records, models, request, *, purpose):
        if (purpose != 'generation' or request['privacy'] != proof['privacy']
                or request['input']['refs'] != [row['qualified_ref'] for row in proof['bodies']]):
            raise RecognitionConflict('shared_task_material_preparation_changed')
        validate_task_materials(query, request, proof)

    request = freeze_product_turn('project.task', materials=materials, freeze_materials=freeze,
        validate_request=validate, instruction=values['text'], **values)
    return request, proof


def capture_task_materials(query, request, materials, *, local_only, reader=None):
    return _capture(query, _origins(query, request), request, materials, local_only=local_only, reader=reader)


def validate_task_materials(query, request, proof, *, reader=None):
    if (not isinstance(proof, dict) or set(proof) != PROOF_FIELDS
            or type(proof.get('schema_version')) is not int or proof['schema_version'] != 1
            or not isinstance(proof.get('selection'), dict)
            or not isinstance(proof.get('privacy'), dict)):
        raise RecognitionConflict('shared_task_material_proof_changed')
    origins = _origins(query, request)
    validate_origin_privacy(origins, query.models, origins.lease.project_id,
        proof['privacy'], selection=proof['selection'], reader=reader)
    fresh = capture_task_materials(query, request, proof['selection']['origin_materials'],
        local_only=not proof['privacy']['allow_remote'], reader=reader)
    if fresh != proof:
        raise RecognitionConflict('shared_task_material_body_changed')
    return proof


def validate_historical_task_materials(query, request, origin, proof, *, reader):
    # 已接纳材料的正文和出生证明沿当前资格读取，原轮设备不参与新轮授权。
    admission = query.origin_admission
    origins = BoundMaterialOrigins(admission, admission.lease, origin.ref)
    selection = proof['selection']
    fresh = _capture(query, origins, request, selection['origin_materials'], local_only=True, reader=reader)
    if (fresh['selection']['sql_identities'] != selection.get('sql_identities')
            or fresh['bodies'] != proof['bodies']
            or fresh['privacy']['source_snapshots'] != proof['privacy']['source_snapshots']):
        raise RecognitionConflict('shared_task_redo_initial_material_changed')


def task_material_item(query, request, proof):
    validate_task_materials(query, request, proof)
    if request['privacy'] != proof['privacy']:
        raise RecognitionConflict('shared_task_admission_privacy_changed')
    admission = query.origin_admission
    native = admission._store()
    reference = {'kind': 'session_event', 'object_id': request['turn_id'],
        'uri': native.immutable_payload_reference(request['turn_id'], KIND)}
    payload = {'schema_version': 1, 'turn_id': request['turn_id'],
        'execution': deepcopy(admission.personal), 'origin_ref': admission._reference(request),
        'input': deepcopy(request['input']), 'proof': deepcopy(proof)}
    return ProductAdmissionItem(KIND, reference, admission._scope(request), payload)


def read_prepared_task_material_item(query, request, bundle, *, reader=None):
    validate_bundle(request, bundle)
    if query.origin_admission._store().get_request(request['turn_id']) is not None:
        return read_task_material_item(query, request, reader=reader)
    items = [item for item in bundle.items if item.kind == KIND]
    tail = [item.ref for item in bundle.items] + [{'kind': 'session_event', 'object_id': request['turn_id'],
        'uri': query.origin_admission._store().immutable_payload_reference(request['turn_id'], MANIFEST_KIND)}]
    if len(items) != 1 or request['input']['refs'][-len(tail):] != tail:
        raise RecognitionConflict('shared_task_admission_changed')
    item, payload = items[0], items[0].payload
    expected_input = {**deepcopy(request['input']), 'refs': request['input']['refs'][:-len(tail)]}
    if (set(payload) != {'schema_version', 'turn_id', 'execution', 'origin_ref', 'input', 'proof'}
            or payload['schema_version'] != 1 or payload['turn_id'] != request['turn_id']
            or payload['execution'] != query.origin_admission.personal
            or payload['origin_ref'] != query.origin_admission._reference(request)
            or payload['input'] != expected_input or item.scope != query.origin_admission._scope(request)
            or request['privacy'] != payload['proof']['privacy']):
        raise RecognitionConflict('shared_task_admission_changed')
    proof = validate_task_materials(query, request, payload['proof'], reader=reader)
    from .shared_task_redo import read_item
    read_item(query, request, bundle=bundle, reader=reader)
    return proof


def read_task_material_item(query, request, *, reader=None):
    origins = _origins(query, request)
    native = origins.admission._store()
    if native.get_request(request['turn_id']) != request:
        raise RecognitionConflict('shared_task_admission_unavailable')
    bundle = native.restore_admission(request)
    items = [] if bundle is None else [item for item in bundle.items if item.kind == KIND]
    if len(items) != 1:
        raise RecognitionConflict('shared_task_admission_unavailable')
    item, payload = items[0], items[0].payload
    expected = {'schema_version', 'turn_id', 'execution', 'origin_ref', 'input', 'proof'}
    immutable = {kind: native.get_immutable_payload(request['turn_id'], kind)
        for kind in (MANIFEST_KIND, *sorted(ADMISSION_KINDS))}
    immutable = {kind: value for kind, value in immutable.items() if value is not None}
    original_input = {**deepcopy(request['input']), 'refs': admission_base_refs(request, immutable)}
    if (not isinstance(payload, dict) or set(payload) != expected
            or type(payload['schema_version']) is not int or payload['schema_version'] != 1
            or payload['turn_id'] != request['turn_id'] or payload['execution'] != origins.admission.personal
            or payload['origin_ref'] != origins.reference or payload['input'] != original_input
            or item.scope != origins.admission._scope(request)
            or immutable[KIND][1] != payload):
        raise RecognitionConflict('shared_task_admission_changed')
    validate_task_materials(query, request, payload['proof'], reader=reader)
    if payload['proof']['privacy'] != request['privacy']:
        raise RecognitionConflict('shared_task_admission_privacy_changed')
    from .shared_task_redo import read_item
    read_item(query, request, bundle=bundle, reader=reader)
    return payload['proof']
