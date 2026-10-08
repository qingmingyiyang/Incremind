"""Product-side freezing and fresh input checks for the existing Turn kernel."""
from backend.recognition import RecognitionConflict, WorkScope
from ..kernel.turn_requests import freeze_product_turn as assemble_product_turn
from .privacy import freeze_turn_materials, privacy_revision, egress_allowed, resolve_turn_material
from ..source_egress import SourceEgressService


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


def validate_frozen_inputs(records, models, request, *, purpose="generation", query=None):
    privacy = request["privacy"]
    project = request["scope"]["project_id"]
    if privacy_revision(records) != privacy["privacy_revision"]:
        raise RecognitionConflict("turn privacy revision conflicted")
    if privacy["allow_remote"] and not egress_allowed(records, models, project, purpose):
        raise RecognitionConflict("turn remote authorization was revoked")
    authority = SourceEgressService(records)
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
    from .part_context import validate_bound_context
    validate_bound_context(records, models, request, query)
