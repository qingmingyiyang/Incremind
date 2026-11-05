from copy import deepcopy

from backend.recognition import RecognitionConflict
from core.ai_kernel.product_admission import bundle_from_payload, bundle_to_payload, seal_admission, validate_bundle
from ..kernel.turn_requests import freeze_product_turn
from ..privacy_policy import global_egress_allowed
from .intent import parse_scope_tag
from .material_origin_admission import BoundMaterialOrigins
from .material_origin_privacy import validate_origin_privacy, freeze_origin_materials
from .privacy import egress_allowed
from .route_example_origins import RouteExampleOrigins
from .route_examples import RouteExamples
from .shared_auxiliary import _qualified_commit
from .shared_rendered_preparation import _query_current


class SharedRouteSource:
    def __init__(self, query):
        _query_current(query)
        self.query, self.admission = query, query.origin_admission
        self.records, self.models = query.execution_owner.records, query.models

    def project(self, text, project):
        _query_current(self.query)
        if project != self.admission.lease.project_id or parse_scope_tag(text)[0]:
            raise RecognitionConflict('shared_route_scope_changed')
        return project

    def identity(self, project, request_key):
        self.project('', project)
        return {'project_id': project, 'request_key': request_key,
            'source_namespace': self.query.source_namespace}

    def allowed(self):
        _query_current(self.query)
        return (global_egress_allowed(self.models, 'generation')
            and egress_allowed(self.query.records, self.models, self.admission.lease.project_id, 'generation'))

    def examples(self):
        original = self.admission.application.state.workbench_route_origins
        if original.shared_origins_for is None:
            raise RecognitionConflict('shared_route_examples_unavailable')
        origins = RouteExampleOrigins(self.admission.application, native_reader=original.native_reader,
            shared_origins_for=original.shared_origins_for,
            current_source=lambda project: self.query.material_owner
                if project == self.admission.lease.project_id else None)
        def source_allowed(row):
            origin = row.payload.get('origin', {})
            return (row.payload.get('project_id') == self.admission.lease.project_id
                and origin.get('kind') == 'shared' and origin.get('namespace') == self.query.source_namespace
                and not any(part['intent'] == 'do' for part in row.payload['parts'])
                and self.allowed())
        return RouteExamples(self.records, self.models, origin_for=origins, source_allowed=source_allowed)

    def scope(self):
        return _qualified_commit(self.admission)

    def freeze(self, text, **identity):
        def materials(records, models, project, selected, *, local_only, purpose):
            if records is not self.records or models is not self.models or selected or purpose != 'generation':
                raise RecognitionConflict('shared_route_owner_changed')
            return [], self.query.freeze_answer_privacy(project, local_only=local_only)
        def validate(records, models, request, *, purpose):
            origins = BoundMaterialOrigins(self.admission, self.admission.lease, {})
            validate_origin_privacy(origins, models, request['scope']['project_id'], request['privacy'], purpose=purpose)
        with self.scope() as qualification:
            request = freeze_product_turn('workbench.route', records=self.records, models=self.models,
                project_id=self.admission.lease.project_id, load_text=lambda item: '',
                freeze_materials=materials, validate_request=validate, instruction=text, text=text, **identity)
            additions = self.admission.prepare(request)
            bundle = seal_admission(request, additions, self.admission._store())
            qualification._validate()
        return request, bundle_to_payload(bundle)

    def validate(self, request, frozen):
        _query_current(self.query)
        if self.admission._store().get_request(request['turn_id']) is not None:
            origins = self.admission.restore(request)
            if bundle_to_payload(self.admission._store().restore_admission(request)) != frozen:
                raise RecognitionConflict('shared_route_admission_changed')
        else:
            bundle = bundle_from_payload(deepcopy(frozen))
            validate_bundle(request, bundle)
            origins = self.admission._bind_bundle(request, bundle)
        validate_origin_privacy(origins, self.models, request['scope']['project_id'], request['privacy'])


def shared_routes(routes, query):
    from .route import RouteService
    source = SharedRouteSource(query)
    if routes.records is not source.records or routes.models is not source.models:
        raise RecognitionConflict('shared_route_owner_changed')
    return RouteService(source.records, source.models, examples=source.examples(), shared_source=source)


def validate_route_example_egress(query, row):
    from .shared_history import read_model_history_answer
    from .model_material_coverage import validate_primary_material_proof
    if not SharedRouteSource(query).allowed():
        raise RecognitionConflict('route_example_shared_egress_revoked')
    actual = read_model_history_answer(query, row.object_id, row.revision)
    request, materials = actual['facts']['request'], []
    for item in actual['model_inputs']:
        payload = item['payload']
        if payload['purpose'] != 'primary':
            raise RecognitionConflict('route_example_shared_material_proof_unavailable')
        validate_primary_material_proof(query, query.models, payload['material_proof'],
            native_request=request, messages=payload['messages'], logical_invocation=item['logical'],
            input_ref=item['input_ref'], invocation_key=item['invocation_key'])
        for material in payload['material_proof']['selection']['origin_materials']:
            if material not in materials:
                materials.append(deepcopy(material))
    origins = query.origin_admission.restore(request)
    allowed, privacy, selection = freeze_origin_materials(origins, query.models, request['scope']['project_id'],
        materials, local_only=False)
    if len(allowed) != len(materials) or selection['origin_materials'] != materials or not privacy['allow_remote']:
        raise RecognitionConflict('route_example_shared_egress_revoked')
