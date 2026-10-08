"""Product request assembly with caller-owned material and privacy adapters."""
from backend.recognition import RecognitionConflict
from core.ai_kernel import validate_turn_request
from core.ai_kernel.turn_kinds import freeze_turn_request as freeze_kernel_request
from ..v2.policies.pipelines import versions_for_turn


def freeze_turn_request(kind, **values):
    """Extend new product requests; the core builder and old payloads stay intact."""
    request = freeze_kernel_request(kind, **values)
    request['policy_versions'] = versions_for_turn(kind)
    return validate_turn_request(request)


def freeze_product_turn(kind, *, records, models, project_id, materials=(), load_text,
                        freeze_materials, validate_request, instruction,
                        text='', local_only=False, model_purpose='generation',
                        load_verified_feedback=None, **identities):
    """The product supplies authority callbacks; the kernel only assembles facts."""
    if kind.startswith('memory.') and text:
        raise RecognitionConflict('auxiliary instructions cannot carry unfiltered material')
    if load_verified_feedback is not None and kind != 'memory.consolidate':
        raise RecognitionConflict('verified feedback is only available for consolidation')
    selected, privacy = freeze_materials(records, models, project_id, materials,
        local_only=local_only, purpose=model_purpose)
    pieces = [instruction]
    pieces.extend(load_text(item) for item in selected)
    if load_verified_feedback is not None:
        feedback = load_verified_feedback()
        if not isinstance(feedback, str):
            raise RecognitionConflict('verified consolidation feedback must be text')
        pieces.append(feedback)
    request = freeze_turn_request(kind, project_id=project_id,
        text='\n\n'.join(piece for piece in pieces if piece),
        refs=[item['ref'] for item in selected], privacy=privacy, **identities)
    validate_request(records, models, request, purpose=model_purpose)
    return request
