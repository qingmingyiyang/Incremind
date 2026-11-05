"""Bind product privacy to the kernel's auxiliary execution adapter."""
from ..kernel.memory_turn import MemoryTurn as KernelMemoryTurn, embedding_request as kernel_embedding_request
from .privacy import egress_allowed
from .turn_requests import freeze_product_turn, validate_frozen_inputs


class MemoryTurn(KernelMemoryTurn):
    def __init__(self, records, models, *, freeze_request=None, automatic=None, admission=None, **kwargs):
        if admission is None:
            admission = automatic_admission(records, models, kind=kwargs['kind'], project=kwargs['project'],
                automatic=automatic, purpose=kwargs.get('purpose', 'generation'))
        super().__init__(records, models, freeze_request=freeze_request or freeze_product_turn,
            validate_request=validate_frozen_inputs, remote_allowed=egress_allowed, admission=admission, **kwargs)


def automatic_admission(records, models, *, kind, project, automatic, purpose='generation'):
    from .automatic_constraint_input import PRODUCERS
    from .automatic_constraints import AutomaticConstraintAdmission
    owner = getattr(models, 'product_automatic_constraints', None)
    if owner is None:
        return None
    if purpose != 'generation' or kind not in {row[0] for row in PRODUCERS.values()}:
        if automatic is not None:
            raise ValueError('automatic_constraint_producer_invalid')
        return None
    if not isinstance(owner, AutomaticConstraintAdmission) or automatic is None:
        raise ValueError('automatic_constraint_producer_missing')
    return owner.bind(records, models, kind=kind, project=project, spec=automatic)


def validate_cached_generation(records, models, *, kind, project, key, automatic):
    binding = automatic_admission(records, models, kind=kind, project=project, automatic=automatic)
    if binding is None:
        return None
    row = next((row for row in records.list('v2_memory_turn_keys')
        if row.payload.get('identity') == {'kind': kind, 'project': project, 'key': key}), None)
    if row is None:
        return None
    return binding.validate_current(row.payload['request'], KernelMemoryTurn.store_for(records))


def embedding_request(records, models, project, materials, key, validate, transport, request, *, purpose='embedding',
                      origin_admission=None, rendered_input=None):
    factory = MemoryTurn
    prepare_original_wire = None
    if origin_admission is not None:
        from .shared_auxiliary import shared_auxiliary_factory
        from .material_origin_admission import BoundMaterialOrigins
        from backend.recognition import RecognitionConflict
        from .shared_rendered_preparation import PreparedRenderedWireInput
        if (not isinstance(rendered_input, PreparedRenderedWireInput)
                or rendered_input.query.origin_admission is not origin_admission
                or rendered_input.public_payload != request['payload']):
            raise RecognitionConflict('shared_rendered_input_preparation_missing')
        origins = BoundMaterialOrigins(origin_admission, origin_admission.lease, {})
        qualified = []
        for material in materials:
            owners = [owner for owner in (origins.shared, origins.profile) if owner.project_id == material['project_id']]
            if len(owners) != 1:
                raise RecognitionConflict('material_origin_scope_changed')
            namespace = material.get('namespace', owners[0].namespace)
            origins.owner(material['project_id'], namespace)
            qualified.append({**material, 'namespace': namespace})
        materials = qualified
        shared_factory = shared_auxiliary_factory(origin_admission)
        def factory(owner_records, owner_models, **options):
            return shared_factory(owner_records, owner_models, rendered_input=rendered_input, **options)
        def prepare_original_wire(turn, payload):
            if turn.origin_admission is not origin_admission or turn.rendered_payload != payload:
                raise RecognitionConflict('shared_rendered_input_changed')
            def current():
                turn.validate()
                if turn.rendered_payload != payload:
                    raise RecognitionConflict('shared_rendered_input_changed')
            return current
    elif rendered_input is not None:
        raise ValueError('shared_rendered_input_personal_owner_invalid')
    return kernel_embedding_request(records, models, project, materials, key, validate,
        transport, request, turn_factory=factory, purpose=purpose, prepare_original_wire=prepare_original_wire)
