from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .workflow_progression import WorkflowDecisionBoundary, WorkflowProgressionMode, decide_workflow_progression


class WorkflowHandlerCategory(StrEnum):
    MODEL = "model"
    TOOL = "tool"
    MEDIA = "media"
    MCP = "mcp"
    PLUGIN = "plugin"
    MEMORY_DOCUMENT = "memory_document"
    CAPABILITY_LIFECYCLE = "capability_lifecycle"


@dataclass(frozen=True, slots=True)
class WorkflowHandlerGovernance:
    category: WorkflowHandlerCategory
    boundary: WorkflowDecisionBoundary

    @property
    def mode(self) -> WorkflowProgressionMode:
        return decide_workflow_progression(self.boundary).mode


_G = WorkflowHandlerGovernance
WORKFLOW_HANDLER_GOVERNANCE = {
    "model_call": _G(WorkflowHandlerCategory.MODEL, WorkflowDecisionBoundary.DETERMINISTIC),
    "tool_call_pure": _G(WorkflowHandlerCategory.TOOL, WorkflowDecisionBoundary.DETERMINISTIC),
    "tool_call_idempotent": _G(WorkflowHandlerCategory.TOOL, WorkflowDecisionBoundary.DETERMINISTIC),
    "tool_call_queryable": _G(WorkflowHandlerCategory.TOOL, WorkflowDecisionBoundary.DETERMINISTIC),
    "tool_call_at_most_once": _G(WorkflowHandlerCategory.TOOL, WorkflowDecisionBoundary.HIGH_RISK),
    "tool_call_needs_reauth": _G(WorkflowHandlerCategory.TOOL, WorkflowDecisionBoundary.PERMISSION_EXPANSION),
    "mcp_call": _G(WorkflowHandlerCategory.MCP, WorkflowDecisionBoundary.PERMISSION_EXPANSION),
    "memory_propose": _G(WorkflowHandlerCategory.MEMORY_DOCUMENT, WorkflowDecisionBoundary.DETERMINISTIC),
    "audio_auto_transcribe": _G(WorkflowHandlerCategory.MEDIA, WorkflowDecisionBoundary.DETERMINISTIC),
    "video_auto_extract_audio": _G(WorkflowHandlerCategory.MEDIA, WorkflowDecisionBoundary.DETERMINISTIC),
    "video_auto_transcribe_audio": _G(WorkflowHandlerCategory.MEDIA, WorkflowDecisionBoundary.DETERMINISTIC),
    "video_auto_summarize_transcript": _G(WorkflowHandlerCategory.MEDIA, WorkflowDecisionBoundary.DETERMINISTIC),
    "video_auto_create_memory_candidate": _G(WorkflowHandlerCategory.MEMORY_DOCUMENT, WorkflowDecisionBoundary.DETERMINISTIC),
    "video_auto_publish_memory": _G(WorkflowHandlerCategory.MEMORY_DOCUMENT, WorkflowDecisionBoundary.FORMAL_MEMORY_PUBLICATION),
    "workbench_auto_extract_audio": _G(WorkflowHandlerCategory.MEDIA, WorkflowDecisionBoundary.DETERMINISTIC),
    "workbench_auto_transcribe_audio": _G(WorkflowHandlerCategory.MEDIA, WorkflowDecisionBoundary.DETERMINISTIC),
    "workbench_auto_summarize_transcript": _G(WorkflowHandlerCategory.MEDIA, WorkflowDecisionBoundary.DETERMINISTIC),
    "workbench_auto_create_memory_candidate": _G(WorkflowHandlerCategory.MEMORY_DOCUMENT, WorkflowDecisionBoundary.DETERMINISTIC),
    "workbench_auto_publish_memory": _G(WorkflowHandlerCategory.MEMORY_DOCUMENT, WorkflowDecisionBoundary.FORMAL_MEMORY_PUBLICATION),
    "long_audio_split": _G(WorkflowHandlerCategory.MEDIA, WorkflowDecisionBoundary.DETERMINISTIC),
    "workbench_auto_fetch_url": _G(WorkflowHandlerCategory.MEDIA, WorkflowDecisionBoundary.EXTERNAL_DOWNLOAD_WRITES_FILE),
    "workbench_auto_document_extract": _G(WorkflowHandlerCategory.MEMORY_DOCUMENT, WorkflowDecisionBoundary.DETERMINISTIC),
    "workbench_auto_image_ocr": _G(WorkflowHandlerCategory.MEDIA, WorkflowDecisionBoundary.DETERMINISTIC),
    "workbench_auto_prepare_file": _G(WorkflowHandlerCategory.MEDIA, WorkflowDecisionBoundary.DETERMINISTIC),
    "workbench_auto_prepare_video": _G(WorkflowHandlerCategory.MEDIA, WorkflowDecisionBoundary.DETERMINISTIC),
    "bilibili_authorized_download": _G(WorkflowHandlerCategory.MEDIA, WorkflowDecisionBoundary.EXTERNAL_DOWNLOAD_WRITES_FILE),
    "provider_model_discovery": _G(WorkflowHandlerCategory.MODEL, WorkflowDecisionBoundary.REVERSIBLE_VISIBLE_CHANGE),
    "index_rebuild": _G(WorkflowHandlerCategory.TOOL, WorkflowDecisionBoundary.DETERMINISTIC),
    "job_execution": _G(WorkflowHandlerCategory.TOOL, WorkflowDecisionBoundary.DETERMINISTIC),
    "media_hands_job_execution": _G(WorkflowHandlerCategory.MEDIA, WorkflowDecisionBoundary.EXTERNAL_DOWNLOAD_WRITES_FILE),
    "workbench_content_transform": _G(WorkflowHandlerCategory.MEDIA, WorkflowDecisionBoundary.DETERMINISTIC),
    "bilibili_media_postprocess": _G(WorkflowHandlerCategory.MEDIA, WorkflowDecisionBoundary.DETERMINISTIC),
    "bilibili_favorite_batch_admission": _G(WorkflowHandlerCategory.MEDIA, WorkflowDecisionBoundary.DETERMINISTIC),
    "external_extension_source_resolve": _G(WorkflowHandlerCategory.MCP, WorkflowDecisionBoundary.DETERMINISTIC),
    "external_extension_acquire_intake": _G(WorkflowHandlerCategory.MCP, WorkflowDecisionBoundary.PERMISSION_EXPANSION),
    "external_extension_activation": _G(WorkflowHandlerCategory.MCP, WorkflowDecisionBoundary.PERMISSION_EXPANSION),
    "external_extension_disable": _G(WorkflowHandlerCategory.MCP, WorkflowDecisionBoundary.REVERSIBLE_VISIBLE_CHANGE),
    "external_extension_health": _G(WorkflowHandlerCategory.MCP, WorkflowDecisionBoundary.DETERMINISTIC),
    "external_extension_rollback": _G(WorkflowHandlerCategory.MCP, WorkflowDecisionBoundary.REVERSIBLE_VISIBLE_CHANGE),
    "external_extension_uninstall": _G(WorkflowHandlerCategory.MCP, WorkflowDecisionBoundary.IRREVERSIBLE_CHANGE),
    "plugin_hands_execution": _G(WorkflowHandlerCategory.PLUGIN, WorkflowDecisionBoundary.HIGH_RISK),
    "plugin_hands_upgrade": _G(WorkflowHandlerCategory.PLUGIN, WorkflowDecisionBoundary.PERMISSION_EXPANSION),
    "plugin_hands_workspace_cleanup": _G(WorkflowHandlerCategory.PLUGIN, WorkflowDecisionBoundary.DETERMINISTIC),
    "document_delivery": _G(WorkflowHandlerCategory.MEMORY_DOCUMENT, WorkflowDecisionBoundary.REVERSIBLE_VISIBLE_CHANGE),
    "external_agent_publication": _G(WorkflowHandlerCategory.MEMORY_DOCUMENT, WorkflowDecisionBoundary.IRREVERSIBLE_CHANGE),
    "external_document_apply": _G(WorkflowHandlerCategory.MEMORY_DOCUMENT, WorkflowDecisionBoundary.IRREVERSIBLE_CHANGE),
    "external_series_candidate": _G(WorkflowHandlerCategory.MEMORY_DOCUMENT, WorkflowDecisionBoundary.DETERMINISTIC),
    "formal_memory_publication": _G(WorkflowHandlerCategory.MEMORY_DOCUMENT, WorkflowDecisionBoundary.FORMAL_MEMORY_PUBLICATION),
    "memory_candidate_from_source_output": _G(WorkflowHandlerCategory.MEMORY_DOCUMENT, WorkflowDecisionBoundary.DETERMINISTIC),
    "memory_projection_rebuild": _G(WorkflowHandlerCategory.MEMORY_DOCUMENT, WorkflowDecisionBoundary.DETERMINISTIC),
    "memory_review_staging": _G(WorkflowHandlerCategory.MEMORY_DOCUMENT, WorkflowDecisionBoundary.DETERMINISTIC),
    "original_asset_retention_purge": _G(WorkflowHandlerCategory.MEMORY_DOCUMENT, WorkflowDecisionBoundary.IRREVERSIBLE_CHANGE),
    "source_retention_purge": _G(WorkflowHandlerCategory.MEMORY_DOCUMENT, WorkflowDecisionBoundary.IRREVERSIBLE_CHANGE),
    "team_memory_source_forget": _G(WorkflowHandlerCategory.MEMORY_DOCUMENT, WorkflowDecisionBoundary.HARD_REDACT),
    "external_project_skill_apply": _G(WorkflowHandlerCategory.CAPABILITY_LIFECYCLE, WorkflowDecisionBoundary.PERMISSION_EXPANSION),
    "project_skill_review_staging": _G(WorkflowHandlerCategory.CAPABILITY_LIFECYCLE, WorkflowDecisionBoundary.DETERMINISTIC),
    "shared_trust_activation": _G(WorkflowHandlerCategory.CAPABILITY_LIFECYCLE, WorkflowDecisionBoundary.PERMISSION_EXPANSION),
    "ppt_master_self_install": _G(WorkflowHandlerCategory.CAPABILITY_LIFECYCLE, WorkflowDecisionBoundary.PERMISSION_EXPANSION),
    "ppt_master_self_install_rollback": _G(WorkflowHandlerCategory.CAPABILITY_LIFECYCLE, WorkflowDecisionBoundary.REVERSIBLE_VISIBLE_CHANGE),
    "presentation_pptx_fixed_generate": _G(WorkflowHandlerCategory.CAPABILITY_LIFECYCLE, WorkflowDecisionBoundary.REVERSIBLE_VISIBLE_CHANGE),
}

PPT_MASTER_WORKFLOW_HANDLER_KINDS = frozenset({
    "ppt_master_self_install", "ppt_master_self_install_rollback",
    "presentation_pptx_fixed_generate",
})
AI_WORKFLOW_EFFECT_KINDS = frozenset({
    "model_call", "tool_call_pure", "tool_call_idempotent", "tool_call_queryable",
    "tool_call_at_most_once", "tool_call_needs_reauth", "mcp_call", "memory_propose",
})
INLINE_WORKFLOW_EFFECT_KINDS = frozenset({
    "audio_auto_transcribe", "video_auto_extract_audio", "video_auto_transcribe_audio",
    "video_auto_summarize_transcript", "video_auto_create_memory_candidate",
    "video_auto_publish_memory", "workbench_auto_extract_audio",
    "workbench_auto_transcribe_audio", "workbench_auto_summarize_transcript",
    "workbench_auto_create_memory_candidate", "workbench_auto_publish_memory",
    "long_audio_split", "workbench_auto_fetch_url", "workbench_auto_document_extract",
    "workbench_auto_image_ocr", "workbench_auto_prepare_file",
    "workbench_auto_prepare_video", "bilibili_authorized_download",
    "provider_model_discovery",
})
MAIN_WORKFLOW_HANDLER_KINDS = (
    frozenset(WORKFLOW_HANDLER_GOVERNANCE)
    - PPT_MASTER_WORKFLOW_HANDLER_KINDS
    - INLINE_WORKFLOW_EFFECT_KINDS
    - AI_WORKFLOW_EFFECT_KINDS
    | {"provider_model_discovery"}
)


def validate_workflow_handler_governance(
    handler_kinds: tuple[str, ...], *, expected_kinds: frozenset[str] | None = None,
    allow_missing: bool = False,
) -> None:
    actual = set(handler_kinds)
    governed = set(expected_kinds or WORKFLOW_HANDLER_GOVERNANCE)
    missing = sorted(actual - governed)
    stale = [] if allow_missing else sorted(governed - actual)
    if missing or stale:
        raise RuntimeError(f"workflow handler governance drifted: missing={missing}, stale={stale}")


def workflow_handler_governance_projection(kind: str) -> dict[str, str]:
    try:
        entry = WORKFLOW_HANDLER_GOVERNANCE[kind]
    except KeyError as error:
        raise RuntimeError(f"workflow handler is not governed: {kind}") from error
    return {
        "category": entry.category.value,
        "boundary": entry.boundary.value,
        "default_mode": entry.mode.value,
    }
