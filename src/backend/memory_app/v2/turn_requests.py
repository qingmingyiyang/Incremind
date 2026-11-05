"""Product-side freezing and fresh input checks for the existing Turn kernel."""
from backend.recognition import RecognitionConflict, WorkScope
from ..kernel.turn_requests import freeze_product_turn as assemble_product_turn
from .privacy import freeze_turn_materials, privacy_revision, egress_allowed, resolve_turn_material
from ..source_egress import SourceEgressService
from ..transaction_records import TransactionRecords


_INSTRUCTIONS = {
    "memory.organize": "整理以下原件。",
    "memory.propose_insights": "从以下整理稿提出待确认的认识。",
    "memory.consolidate": "从以下认识和使用记录归纳待确认的规律。",
    "memory.link_suggest": "为以下认识提出相关连接建议。",
    "memory.overview": "仅根据已授权摘要生成范围概览。",
    "memory.place": "根据整理稿的标题和摘要判断它属于哪个项目。",
}


def freeze_product_turn(kind, *, records, models, project_id, materials=(), load_text,
                        text="", local_only=False, model_purpose="generation",
                        load_verified_feedback=None, **identities):
    """Domain callers identify material and render the resolved authoritative payload.

    This function neither writes user activity nor submits a model call. Consumers
    must call validate_frozen_inputs at the actual dispatch boundary as well.
    """
    def freeze_materials(records, models, project, materials, **options):
        return freeze_turn_materials(records, models, project, materials,
            authority=SourceEgressService(records), **options)

    return assemble_product_turn(kind, records=records, models=models, project_id=project_id,
        materials=materials, load_text=load_text, text=text, local_only=local_only,
        model_purpose=model_purpose, freeze_materials=freeze_materials,
        load_verified_feedback=load_verified_feedback,
        validate_request=validate_frozen_inputs, instruction=_INSTRUCTIONS.get(kind, text), **identities)


def validate_frozen_inputs(records, models, request, *, purpose="generation", query=None, source_owner=None):
    privacy = request["privacy"]
    project = request["scope"]["project_id"]
    if privacy_revision(records) != privacy["privacy_revision"]:
        raise RecognitionConflict("turn privacy revision conflicted")
    if privacy["allow_remote"] and not egress_allowed(records, models, project, purpose):
        raise RecognitionConflict("turn remote authorization was revoked")
    if isinstance(records, TransactionRecords) and query is None and source_owner is None:
        raise RecognitionConflict('turn source actual owner unavailable')
    owner = source_owner if source_owner is not None else query.records if isinstance(records, TransactionRecords) else records
    authority = SourceEgressService(owner, reader=records if isinstance(records, TransactionRecords) else None)
    for material in privacy["material_refs"]:
        if material["project_id"] not in {project, "me"}:
            raise RecognitionConflict("turn material scope conflicted")
        resolve_turn_material(records, WorkScope("local-user", material["project_id"]), material)
    for snapshot in privacy["source_snapshots"]:
        scope = WorkScope(**snapshot["scope"])
        if scope.project_id not in {project, "me"}:
            raise RecognitionConflict("turn material scope conflicted")
        authority.validate_snapshot(scope, snapshot)
        if privacy["allow_remote"]:
            authority.require(snapshot, purpose)
    auxiliary = records.read('v2_steer_learning_inputs', request['turn_id'])
    key = records.read('v2_memory_turn_keys', request['turn_id'])
    identity = key.payload.get('identity', {}).get('key') if key is not None else None
    if auxiliary is not None or isinstance(identity, list) and identity and identity[0] == 'steer-learning-v1':
        learning = getattr(models, 'product_steer_learning', None)
        if learning is None or learning.records is not owner:
            raise RecognitionConflict('steer_learning_auxiliary_actual_owner_unavailable')
        if auxiliary is not None or learning.freshness._native().get_request(request['turn_id']) is not None:
            with learning.freshness._read_scope(None if records is learning.records else records) as current:
                if learning.sources.validate_auxiliary_request(current, request,
                        local_only=not privacy['allow_remote']) is None:
                    raise RecognitionConflict('steer_learning_auxiliary_input_unavailable')
    from .part_context import validate_bound_context
    validate_bound_context(records, models, request, query)
