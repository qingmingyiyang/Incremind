from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .library_item_deletion import source_is_deleted


class LibraryOverviewReaderPort(Protocol):
    """Read-only Library collections used by the Product Core overview."""

    def sources(self) -> Sequence[Mapping[str, object]]:
        """Return captured Source objects."""

    def documents(self) -> Sequence[Mapping[str, object]]:
        """Return editable Document objects."""

    def memory_candidates(self) -> Sequence[Mapping[str, object]]:
        """Return reviewable Memory Candidate objects."""

    def external_agent_review_drafts(self) -> Sequence[Mapping[str, object]]:
        """Return external Agent proposals that require local user review."""

    def memory_objects(self) -> Sequence[Mapping[str, object]]:
        """Return published Memory objects across supported layers."""


@runtime_checkable
class LibraryOverviewJobReaderPort(Protocol):
    """Optional Job identity capability for authority-aware Library readers."""

    def capture_job_id(self, source_id: str) -> str | None:
        """Return the authority-verified capture Job for a Source, when present."""

    def user_job_id(self, source_id: str) -> str | None:
        """Return the authority-verified Job surfaced to the user for a Source."""


class LibraryOverviewError(ValueError):
    """Raised when Library overview filters are invalid."""


@dataclass(frozen=True, slots=True)
class LibraryOverviewItem:
    item_id: str
    item_type: str
    title: str
    project_id: str | None
    status: str
    trust_status: str | None
    source_refs: tuple[str, ...]
    trace_refs: tuple[str, ...]
    blocked_operations: tuple[str, ...]
    source_revision: int | None = None
    capture_job_id: str | None = None
    user_job_id: str | None = None
    target_layer: str | None = None
    candidate_revision: int | None = None
    import_batch_id: str | None = None
    expert_proposal_id: str | None = None
    source_media_type: str | None = None
    source_content_kind: str | None = None
    source_platform: str | None = None
    source_workspace_item_id: str | None = None
    source_original_url: str | None = None
    content_read_status: str | None = None
    content_read: bool = False
    content_char_count: int | None = None
    content_preview: str | None = None
    content_read_ref: str | None = None
    content_read_id: str | None = None
    content_read_error: str | None = None
    source_output_memory_candidate_id: str | None = None
    source_output_memory_publication: str | None = None
    media_processing_status: str | None = None
    media_required_capability: str | None = None
    media_disabled_reason: str | None = None
    media_job_ref: str | None = None
    media_expected_output_refs: tuple[str, ...] = ()
    media_processing_error: str | None = None
    media_output_refs: tuple[str, ...] = ()
    media_output_ids: tuple[str, ...] = ()
    media_output_preview: str | None = None
    video_audio_asset_id: str | None = None
    video_audio_asset_ref: str | None = None
    video_transcript_output_id: str | None = None
    video_summary_output_id: str | None = None
    video_auto_workflow_status: str | None = None
    video_auto_workflow_id: str | None = None
    video_auto_workflow_steps: tuple[Mapping[str, object], ...] = ()
    video_auto_workflow_error: str | None = None
    video_auto_workflow_summary_provider_status: str | None = None
    video_auto_workflow_summary_provider_name: str | None = None
    video_auto_workflow_summary_readiness_reason: str | None = None
    video_auto_workflow_summary_next_step: str | None = None
    audio_auto_workflow_status: str | None = None
    audio_auto_workflow_id: str | None = None
    audio_auto_workflow_steps: tuple[Mapping[str, object], ...] = ()
    audio_auto_workflow_error: str | None = None
    audio_transcript_output_id: str | None = None
    audio_auto_workflow_transcriber_status: str | None = None
    audio_auto_workflow_transcriber_model_profile: str | None = None
    audio_auto_workflow_transcriber_model_name: str | None = None
    audio_auto_workflow_asset_status: str | None = None
    audio_auto_workflow_readiness_reason: str | None = None
    audio_auto_workflow_next_step: str | None = None
    memory_publication_id: str | None = None
    memory_published_ref: str | None = None
    memory_rollback_ref: str | None = None
    collection_item_count: int | None = None
    collection_urls: tuple[str, ...] = ()
    collection_child_source_ids: tuple[str, ...] = ()
    content_tags: tuple[str, ...] = ()
    manual_tags: tuple[str, ...] = ()
    paragraph_tags: tuple[Mapping[str, object], ...] = ()
    structured_summary: str | None = None
    structured_key_points: tuple[str, ...] = ()
    structured_body: str | None = None
    series_candidate: str | None = None
    series_confidence: float | None = None
    series_reason: str | None = None
    content_structure_status: str | None = None
    content_structure_ref: str | None = None
    organization_prompt_refs: tuple[Mapping[str, object], ...] = ()
    template_prompt_refs: tuple[Mapping[str, object], ...] = ()
    series_assignment_status: str | None = None
    series_assignment_ref: str | None = None
    series_id: str | None = None
    series_name: str | None = None
    series_ref: str | None = None
    inspiration_status: str | None = None
    inspiration_id: str | None = None
    inspiration_ref: str | None = None
    inspiration_series_id: str | None = None
    inspiration_series_name: str | None = None
    inspiration_themes: tuple[str, ...] = ()
    inspiration_summary: str | None = None
    inspiration_activity_refs: tuple[str, ...] = ()
    inspiration_memory_publication: str | None = None
    document_revision: int | None = None
    document_type: str | None = None
    external_agent_proposal_id: str | None = None
    external_agent_proposal_type: str | None = None
    external_agent_draft_type: str | None = None
    external_agent_target_id: str | None = None
    external_agent_review_state: str | None = None
    external_agent_application_state: str | None = None
    external_agent_writes_long_term_memory: bool | None = None
    external_agent_writes_staging_memory: bool | None = None
    external_agent_memory_publication_state: str | None = None
    external_agent_applied_target_kind: str | None = None
    external_agent_applied_target_id: str | None = None
    external_agent_applied_revision: int | None = None
    external_agent_applied_ref: str | None = None


@dataclass(frozen=True, slots=True)
class LibraryOverview:
    status: str
    scope: str
    project_id: str | None
    items: tuple[LibraryOverviewItem, ...]
    counts: Mapping[str, int]
    blocked_operations: tuple[str, ...]
    next_step_boundary: str


class GetLibraryOverview:
    """Builds a unified Library overview without reading source contents."""

    _BLOCKED_OPERATIONS = (
        "source_content_read",
        "parser_execution",
        "media_processing_provider_execution",
        "model_provider_execution",
        "qa_answer_generation",
        "memory_publication",
        "document_overwrite",
        "legacy_library_write",
    )

    def __init__(self, reader: LibraryOverviewReaderPort, *, namespace_id: str = "default") -> None:
        self._reader = reader
        self._namespace_id = namespace_id

    def execute(self, *, project_id: str | None = None) -> LibraryOverview:
        normalized_project = _normalize_project_id(project_id)
        items = tuple(
            sorted(
                (
                    *self._source_items(self._reader.sources(), project_id=normalized_project),
                    *self._document_items(self._reader.documents(), project_id=normalized_project),
                    *self._candidate_items(self._reader.memory_candidates(), project_id=normalized_project),
                    *self._external_agent_review_draft_items(
                        self._reader.external_agent_review_drafts(),
                        project_id=normalized_project,
                    ),
                    *self._memory_items(self._reader.memory_objects(), project_id=normalized_project),
                ),
                key=lambda item: (item.item_type, item.title, item.item_id),
            )
        )
        counts = _counts(items)
        return LibraryOverview(
            status="ready",
            scope="project" if normalized_project else "all",
            project_id=normalized_project,
            items=items,
            counts=counts,
            blocked_operations=self._BLOCKED_OPERATIONS,
            next_step_boundary=(
                "library_project_filter_ready_without_content_read"
                if normalized_project
                else "library_all_items_ready_without_content_read"
            ),
        )

    def _source_items(
        self,
        sources: Sequence[Mapping[str, object]],
        *,
        project_id: str | None,
    ) -> tuple[LibraryOverviewItem, ...]:
        items: list[LibraryOverviewItem] = []
        job_reader = (
            self._reader
            if isinstance(self._reader, LibraryOverviewJobReaderPort)
            else None
        )
        for source in sources:
            if source_is_deleted(source):
                continue
            source_project = _optional_str(source.get("project_id"))
            if project_id is not None and source_project not in {None, project_id}:
                continue
            source_id = _required_str(source, "id")
            source_metadata = source.get("metadata")
            if not isinstance(source_metadata, Mapping):
                source_metadata = {}
            content_read = _source_content_read(source)
            content_structure = _source_content_structure(source)
            template_outputs = _source_template_outputs(source)
            series_assignment = _source_series_assignment(source)
            inspiration = _source_inspiration(source)
            media_processing = _source_media_processing(source)
            video_auto_workflow = _source_video_auto_workflow(source)
            audio_auto_workflow = _source_audio_auto_workflow(source)
            collection = _source_collection(source)
            trace_refs = [f"crp://{self._namespace_id}/sources/{source_id}"]
            trace_refs.extend(
                f"crp://{self._namespace_id}/sources/{child_source_id}"
                for child_source_id in collection["child_source_ids"]
            )
            trace_refs.extend(_source_file_authorization_refs(source))
            if content_read["read_ref"]:
                trace_refs.append(content_read["read_ref"])
            trace_refs.extend(content_read["activity_refs"])
            if content_structure["structure_ref"]:
                trace_refs.append(content_structure["structure_ref"])
            trace_refs.extend(content_structure["activity_refs"])
            trace_refs.extend(template_outputs["output_refs"])
            if series_assignment["assignment_ref"]:
                trace_refs.append(series_assignment["assignment_ref"])
            if series_assignment["series_ref"]:
                trace_refs.append(series_assignment["series_ref"])
            trace_refs.extend(series_assignment["activity_refs"])
            if inspiration["inspiration_ref"]:
                trace_refs.append(inspiration["inspiration_ref"])
            trace_refs.extend(inspiration["activity_refs"])
            if media_processing["job_ref"]:
                trace_refs.append(media_processing["job_ref"])
            trace_refs.extend(media_processing["expected_output_refs"])
            trace_refs.extend(media_processing["output_refs"])
            trace_refs.extend(media_processing["activity_refs"])
            if video_auto_workflow["workflow_ref"]:
                trace_refs.append(video_auto_workflow["workflow_ref"])
            if audio_auto_workflow["workflow_ref"]:
                trace_refs.append(audio_auto_workflow["workflow_ref"])
            blocked_operations = ["parser_execution", "memory_publication"]
            if not content_read["content_read"] and collection["item_count"] is None:
                blocked_operations.insert(0, "source_content_read")
            if media_processing["status"] in {"queued", "running", "failed", "skipped"}:
                blocked_operations.append("media_processing_provider_execution")
            items.append(
                LibraryOverviewItem(
                    item_id=source_id,
                    item_type="source",
                    title=_title(source, fallback=source_id),
                    project_id=source_project,
                    status=_optional_str(source.get("processing_state")) or "captured",
                    trust_status=_optional_str(source.get("trust_status")),
                    source_media_type=_optional_str(source.get("media_type")),
                    source_content_kind=_optional_str(source_metadata.get("content_kind")),
                    source_platform=_optional_str(source_metadata.get("platform")),
                    source_workspace_item_id=_optional_str(source.get("workspace_item_id")),
                    source_original_url=_optional_str(source.get("original_url")),
                    source_refs=_source_refs(source_id, content_read),
                    trace_refs=tuple(trace_refs),
                    blocked_operations=tuple(blocked_operations),
                    source_revision=_optional_int(source.get("revision")),
                    capture_job_id=(
                        job_reader.capture_job_id(source_id) if job_reader is not None else None
                    ),
                    user_job_id=(
                        job_reader.user_job_id(source_id) if job_reader is not None else None
                    ),
                    content_read_status=content_read["status"],
                    content_read=content_read["content_read"],
                    content_char_count=content_read["char_count"],
                    content_preview=content_read["preview"],
                    content_read_ref=content_read["read_ref"],
                    content_read_id=content_read["read_id"],
                    content_read_error=content_read["error"],
                    source_output_memory_candidate_id=(
                        media_processing["memory_candidate_id"] or content_read["memory_candidate_id"]
                    ),
                    source_output_memory_publication=(
                        media_processing["memory_publication"] or content_read["memory_publication"]
                    ),
                    media_processing_status=media_processing["status"],
                    media_required_capability=media_processing["required_capability"],
                    media_disabled_reason=media_processing["disabled_reason"],
                    media_job_ref=media_processing["job_ref"],
                    media_expected_output_refs=media_processing["expected_output_refs"],
                    media_processing_error=media_processing["error"],
                    media_output_refs=media_processing["output_refs"],
                    media_output_ids=media_processing["output_ids"],
                    media_output_preview=media_processing["output_preview"],
                    video_audio_asset_id=media_processing["audio_asset_id"],
                    video_audio_asset_ref=media_processing["audio_asset_ref"],
                    video_transcript_output_id=media_processing["transcript_output_id"],
                    video_summary_output_id=media_processing["summary_output_id"],
                    video_auto_workflow_status=video_auto_workflow["status"],
                    video_auto_workflow_id=video_auto_workflow["workflow_id"],
                    video_auto_workflow_steps=video_auto_workflow["steps"],
                    video_auto_workflow_error=video_auto_workflow["error"],
                    video_auto_workflow_summary_provider_status=video_auto_workflow["summary_provider_status"],
                    video_auto_workflow_summary_provider_name=video_auto_workflow["summary_provider_name"],
                    video_auto_workflow_summary_readiness_reason=video_auto_workflow["summary_readiness_reason"],
                    video_auto_workflow_summary_next_step=video_auto_workflow["summary_next_step"],
                    audio_auto_workflow_status=audio_auto_workflow["status"],
                    audio_auto_workflow_id=audio_auto_workflow["workflow_id"],
                    audio_auto_workflow_steps=audio_auto_workflow["steps"],
                    audio_auto_workflow_error=audio_auto_workflow["error"],
                    audio_transcript_output_id=media_processing["transcript_output_id"],
                    audio_auto_workflow_transcriber_status=audio_auto_workflow["transcriber_status"],
                    audio_auto_workflow_transcriber_model_profile=audio_auto_workflow["transcriber_model_profile"],
                    audio_auto_workflow_transcriber_model_name=audio_auto_workflow["transcriber_model_name"],
                    audio_auto_workflow_asset_status=audio_auto_workflow["audio_asset_status"],
                    audio_auto_workflow_readiness_reason=audio_auto_workflow["readiness_reason"],
                    audio_auto_workflow_next_step=audio_auto_workflow["next_step"],
                    memory_publication_id=video_auto_workflow["publication_id"],
                    memory_published_ref=video_auto_workflow["published_ref"],
                    memory_rollback_ref=video_auto_workflow["rollback_ref"],
                    collection_item_count=collection["item_count"],
                    collection_urls=collection["urls"],
                    collection_child_source_ids=collection["child_source_ids"],
                    content_tags=tuple(dict.fromkeys((*content_structure["tags"], *_source_manual_tags(source)))),
                    manual_tags=_source_manual_tags(source),
                    paragraph_tags=content_structure["paragraph_tags"],
                    structured_summary=content_structure["summary"],
                    structured_key_points=content_structure["key_points"],
                    structured_body=content_structure["structured_body"],
                    series_candidate=content_structure["series_candidate"],
                    series_confidence=content_structure["series_confidence"],
                    series_reason=content_structure["series_reason"],
                    content_structure_status=content_structure["status"],
                    content_structure_ref=content_structure["structure_ref"],
                    organization_prompt_refs=content_structure["organization_prompt_refs"],
                    template_prompt_refs=template_outputs["prompt_refs"],
                    series_assignment_status=series_assignment["status"],
                    series_assignment_ref=series_assignment["assignment_ref"],
                    series_id=series_assignment["series_id"],
                    series_name=series_assignment["series_name"],
                    series_ref=series_assignment["series_ref"],
                    inspiration_status=inspiration["status"],
                    inspiration_id=inspiration["inspiration_id"],
                    inspiration_ref=inspiration["inspiration_ref"],
                    inspiration_series_id=inspiration["series_id"],
                    inspiration_series_name=inspiration["series_name"],
                    inspiration_themes=inspiration["themes"],
                    inspiration_summary=inspiration["summary"],
                    inspiration_activity_refs=inspiration["activity_refs"],
                    inspiration_memory_publication=inspiration["memory_publication"],
                )
            )
        return tuple(items)

    def _document_items(
        self,
        documents: Sequence[Mapping[str, object]],
        *,
        project_id: str | None,
    ) -> tuple[LibraryOverviewItem, ...]:
        items: list[LibraryOverviewItem] = []
        for document in documents:
            document_project = _optional_str(document.get("project_id"))
            if project_id is not None and document_project != project_id:
                continue
            document_id = _required_str(document, "id")
            items.append(
                LibraryOverviewItem(
                    item_id=document_id,
                    item_type="document",
                    title=_title(document, fallback=document_id),
                    project_id=document_project,
                    status=_optional_str(document.get("status")) or "draft",
                    trust_status=None,
                    source_refs=_display_source_refs(document.get("source_refs")),
                    trace_refs=(f"crp://{self._namespace_id}/documents/{document_id}.json",),
                    blocked_operations=("document_overwrite", "source_content_read", "memory_publication"),
                    document_revision=_optional_int(document.get("revision")),
                    document_type=_optional_str(document.get("type")),
                )
            )
        return tuple(items)

    def _candidate_items(
        self,
        candidates: Sequence[Mapping[str, object]],
        *,
        project_id: str | None,
    ) -> tuple[LibraryOverviewItem, ...]:
        items: list[LibraryOverviewItem] = []
        for candidate in candidates:
            candidate_project = _optional_str(candidate.get("project_id"))
            if project_id is not None and candidate_project != project_id:
                continue
            candidate_id = _required_str(candidate, "id")
            provenance = candidate.get("provenance")
            proposal_id = (
                provenance.get("external_agent_proposal_id")
                if isinstance(provenance, Mapping)
                else None
            )
            items.append(
                LibraryOverviewItem(
                    item_id=candidate_id,
                    item_type="memory_candidate",
                    title=_candidate_title(candidate, fallback=candidate_id),
                    project_id=candidate_project,
                    status=_optional_str(candidate.get("status")) or "pending_review",
                    trust_status=None,
                    target_layer=_optional_str(candidate.get("target_layer")) or "atom",
                    candidate_revision=_optional_int(candidate.get("candidate_revision")),
                    import_batch_id=_optional_str(candidate.get("import_batch_id")),
                    expert_proposal_id=(
                        proposal_id
                        if isinstance(proposal_id, str) and proposal_id.startswith("expert-")
                        else None
                    ),
                    source_refs=_display_source_refs(candidate.get("source_refs")),
                    trace_refs=_candidate_trace_refs(
                        candidate,
                        candidate_ref=f"crp://{self._namespace_id}/memory-candidates/{candidate_id}.json",
                    ),
                    blocked_operations=("auto_promote_memory", "memory_publication", "source_content_read"),
                )
            )
        return tuple(items)

    def _external_agent_review_draft_items(
        self,
        drafts: Sequence[Mapping[str, object]],
        *,
        project_id: str | None,
    ) -> tuple[LibraryOverviewItem, ...]:
        items: list[LibraryOverviewItem] = []
        for draft in drafts:
            draft_project = _optional_str(draft.get("project_id"))
            if project_id is not None and draft_project != project_id:
                continue
            draft_id = _required_str(draft, "id")
            review = draft.get("review") if isinstance(draft.get("review"), Mapping) else {}
            application = draft.get("application") if isinstance(draft.get("application"), Mapping) else {}
            applied_summary = _external_agent_applied_summary(application, namespace_id=self._namespace_id)
            ref = _optional_str(draft.get("ref")) or (
                f"crp://{self._namespace_id}/external-agent-review-drafts/{draft_id}.json"
            )
            trace_refs = [ref]
            proposal_id = _optional_str(draft.get("proposal_id"))
            if proposal_id is not None:
                trace_refs.append(f"crp://{self._namespace_id}/external-agent-proposals/{proposal_id}.json")
            trace_refs.extend(_external_agent_evidence_refs(draft.get("evidence_refs")))
            items.append(
                LibraryOverviewItem(
                    item_id=draft_id,
                    item_type="external_agent_review_draft",
                    title=_external_agent_draft_title(draft, fallback=draft_id),
                    project_id=draft_project,
                    status=_optional_str(draft.get("status")) or "pending_review",
                    trust_status=None,
                    source_refs=_display_source_refs(draft.get("source_refs")),
                    trace_refs=tuple(dict.fromkeys(trace_refs)),
                    blocked_operations=(
                        "apply_without_user_confirmation",
                        "automatic_memory_publication",
                        "staging_memory_write",
                        "direct_long_term_memory_write",
                        "source_content_read",
                    ),
                    external_agent_proposal_id=proposal_id,
                    external_agent_proposal_type=_optional_str(draft.get("proposal_type")),
                    external_agent_draft_type=_optional_str(draft.get("draft_type")),
                    external_agent_target_id=_optional_str(draft.get("target_id")),
                    external_agent_review_state=_optional_str(review.get("state")),
                    external_agent_application_state=_optional_str(application.get("state")),
                    external_agent_writes_long_term_memory=(
                        application.get("writes_long_term_memory")
                        if isinstance(application.get("writes_long_term_memory"), bool)
                        else None
                    ),
                    external_agent_writes_staging_memory=(
                        application.get("writes_staging_memory")
                        if isinstance(application.get("writes_staging_memory"), bool)
                        else None
                    ),
                    external_agent_memory_publication_state="not_published",
                    external_agent_applied_target_kind=applied_summary.get("kind"),
                    external_agent_applied_target_id=applied_summary.get("id"),
                    external_agent_applied_revision=applied_summary.get("revision"),
                    external_agent_applied_ref=applied_summary.get("ref"),
                )
            )
        return tuple(items)

    def _memory_items(
        self,
        memory_objects: Sequence[Mapping[str, object]],
        *,
        project_id: str | None,
    ) -> tuple[LibraryOverviewItem, ...]:
        items: list[LibraryOverviewItem] = []
        for memory in memory_objects:
            memory_project = _memory_project_id(memory)
            if project_id is not None and memory_project != project_id:
                continue
            memory_id = _required_str(memory, "id")
            layer = _memory_layer(memory)
            items.append(
                LibraryOverviewItem(
                    item_id=memory_id,
                    item_type=layer,
                    title=_memory_title(memory, fallback=memory_id),
                    project_id=memory_project,
                    status="published",
                    trust_status=_optional_str(memory.get("trust_status")),
                    source_refs=_display_source_refs(memory.get("source_refs")),
                    trace_refs=(f"crp://{self._namespace_id}/memory/{layer}/{memory_id}.json",),
                    blocked_operations=("memory_mutation", "source_content_read"),
                )
            )
        return tuple(items)


def serialize_library_overview(overview: LibraryOverview) -> dict[str, object]:
    return {
        "status": overview.status,
        "scope": overview.scope,
        "project_id": overview.project_id,
        "items": [serialize_library_overview_item(item) for item in overview.items],
        "counts": dict(overview.counts),
        "blocked_operations": list(overview.blocked_operations),
        "next_step_boundary": overview.next_step_boundary,
    }


def serialize_library_overview_item(item: LibraryOverviewItem) -> dict[str, object]:
    return {
        "item_id": item.item_id,
        "item_type": item.item_type,
        "title": item.title,
        "project_id": item.project_id,
        "status": item.status,
        "trust_status": item.trust_status,
        "target_layer": item.target_layer,
        "candidate_revision": item.candidate_revision,
        "import_batch_id": item.import_batch_id,
        "expert_proposal_id": item.expert_proposal_id,
        "source_refs": list(item.source_refs),
        "trace_refs": list(item.trace_refs),
        "blocked_operations": list(item.blocked_operations),
        "source_revision": item.source_revision,
        "capture_job_id": item.capture_job_id,
        "user_job_id": item.user_job_id,
        "source_media_type": item.source_media_type,
        "source_content_kind": item.source_content_kind,
        "source_platform": item.source_platform,
        "source_workspace_item_id": item.source_workspace_item_id,
        "source_original_url": item.source_original_url,
        "content_read_status": item.content_read_status,
        "content_read": item.content_read,
        "content_char_count": item.content_char_count,
        "content_preview": item.content_preview,
        "content_read_ref": item.content_read_ref,
        "content_read_id": item.content_read_id,
        "content_read_error": item.content_read_error,
        "source_output_memory_candidate_id": item.source_output_memory_candidate_id,
        "source_output_memory_publication": item.source_output_memory_publication,
        "media_processing_status": item.media_processing_status,
        "media_required_capability": item.media_required_capability,
        "media_disabled_reason": item.media_disabled_reason,
        "media_job_ref": item.media_job_ref,
        "media_expected_output_refs": list(item.media_expected_output_refs),
        "media_processing_error": item.media_processing_error,
        "media_output_refs": list(item.media_output_refs),
        "media_output_ids": list(item.media_output_ids),
        "media_output_preview": item.media_output_preview,
        "video_audio_asset_id": item.video_audio_asset_id,
        "video_audio_asset_ref": item.video_audio_asset_ref,
        "video_transcript_output_id": item.video_transcript_output_id,
        "video_summary_output_id": item.video_summary_output_id,
        "video_auto_workflow_status": item.video_auto_workflow_status,
        "video_auto_workflow_id": item.video_auto_workflow_id,
        "video_auto_workflow_steps": list(item.video_auto_workflow_steps),
        "video_auto_workflow_error": item.video_auto_workflow_error,
        "video_auto_workflow_summary_provider_status": item.video_auto_workflow_summary_provider_status,
        "video_auto_workflow_summary_provider_name": item.video_auto_workflow_summary_provider_name,
        "video_auto_workflow_summary_readiness_reason": item.video_auto_workflow_summary_readiness_reason,
        "video_auto_workflow_summary_next_step": item.video_auto_workflow_summary_next_step,
        "audio_auto_workflow_status": item.audio_auto_workflow_status,
        "audio_auto_workflow_id": item.audio_auto_workflow_id,
        "audio_auto_workflow_steps": list(item.audio_auto_workflow_steps),
        "audio_auto_workflow_error": item.audio_auto_workflow_error,
        "audio_transcript_output_id": item.audio_transcript_output_id,
        "audio_auto_workflow_transcriber_status": item.audio_auto_workflow_transcriber_status,
        "audio_auto_workflow_transcriber_model_profile": item.audio_auto_workflow_transcriber_model_profile,
        "audio_auto_workflow_transcriber_model_name": item.audio_auto_workflow_transcriber_model_name,
        "audio_auto_workflow_asset_status": item.audio_auto_workflow_asset_status,
        "audio_auto_workflow_readiness_reason": item.audio_auto_workflow_readiness_reason,
        "audio_auto_workflow_next_step": item.audio_auto_workflow_next_step,
        "memory_publication_id": item.memory_publication_id,
        "memory_published_ref": item.memory_published_ref,
        "memory_rollback_ref": item.memory_rollback_ref,
        "collection_item_count": item.collection_item_count,
        "collection_urls": list(item.collection_urls),
        "collection_child_source_ids": list(item.collection_child_source_ids),
        "content_tags": list(item.content_tags),
        "manual_tags": list(item.manual_tags),
        "paragraph_tags": list(item.paragraph_tags),
        "structured_summary": item.structured_summary,
        "structured_key_points": list(item.structured_key_points),
        "structured_body": item.structured_body,
        "series_candidate": item.series_candidate,
        "series_confidence": item.series_confidence,
        "series_reason": item.series_reason,
        "content_structure_status": item.content_structure_status,
        "content_structure_ref": item.content_structure_ref,
        "organization_prompt_refs": list(item.organization_prompt_refs),
        "template_prompt_refs": list(item.template_prompt_refs),
        "series_assignment_status": item.series_assignment_status,
        "series_assignment_ref": item.series_assignment_ref,
        "series_id": item.series_id,
        "series_name": item.series_name,
        "series_ref": item.series_ref,
        "inspiration_status": item.inspiration_status,
        "inspiration_id": item.inspiration_id,
        "inspiration_ref": item.inspiration_ref,
        "inspiration_series_id": item.inspiration_series_id,
        "inspiration_series_name": item.inspiration_series_name,
        "inspiration_themes": list(item.inspiration_themes),
        "inspiration_summary": item.inspiration_summary,
        "inspiration_activity_refs": list(item.inspiration_activity_refs),
        "inspiration_memory_publication": item.inspiration_memory_publication,
        "document_revision": item.document_revision,
        "document_type": item.document_type,
        "external_agent_proposal_id": item.external_agent_proposal_id,
        "external_agent_proposal_type": item.external_agent_proposal_type,
        "external_agent_draft_type": item.external_agent_draft_type,
        "external_agent_target_id": item.external_agent_target_id,
        "external_agent_review_state": item.external_agent_review_state,
        "external_agent_application_state": item.external_agent_application_state,
        "external_agent_writes_long_term_memory": item.external_agent_writes_long_term_memory,
        "external_agent_writes_staging_memory": item.external_agent_writes_staging_memory,
        "external_agent_memory_publication_state": item.external_agent_memory_publication_state,
        "external_agent_applied_target_kind": item.external_agent_applied_target_kind,
        "external_agent_applied_target_id": item.external_agent_applied_target_id,
        "external_agent_applied_revision": item.external_agent_applied_revision,
        "external_agent_applied_ref": item.external_agent_applied_ref,
    }


def _normalize_project_id(project_id: str | None) -> str | None:
    if project_id is None:
        return None
    normalized = project_id.strip()
    if not normalized:
        raise LibraryOverviewError("project_id cannot be empty")
    return normalized


def _counts(items: Sequence[LibraryOverviewItem]) -> dict[str, int]:
    counts: dict[str, int] = {"total": len(items)}
    for item in items:
        counts[item.item_type] = counts.get(item.item_type, 0) + 1
    return counts


def _display_source_refs(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    refs: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping):
            continue
        source_id = item.get("source_id")
        locator = item.get("locator")
        if not isinstance(source_id, str) or not source_id:
            continue
        if not isinstance(locator, str) or not locator:
            continue
        ref = f"{source_id}#{locator}"
        if ref in seen:
            continue
        seen.add(ref)
        refs.append(ref)
    return tuple(refs)


def _source_refs(source_id: str, content_read: Mapping[str, object]) -> tuple[str, ...]:
    refs = [f"{source_id}#source:metadata"]
    if content_read["content_read"]:
        refs.append(f"{source_id}#source:content")
    return tuple(refs)


def _source_content_read(source: Mapping[str, object]) -> dict[str, object]:
    metadata = source.get("metadata")
    if not isinstance(metadata, Mapping):
        return _empty_content_read()
    content_read = metadata.get("content_read")
    if not isinstance(content_read, Mapping):
        return _empty_content_read()
    status = _optional_str(content_read.get("status"))
    read_ref = _optional_str(content_read.get("read_ref"))
    read_id = _content_read_id_from_ref(read_ref)
    error = _optional_str(content_read.get("error"))
    preview = _optional_str(content_read.get("preview"))
    char_count = _optional_int(content_read.get("char_count"))
    activity_refs = _string_sequence(content_read.get("activity_refs"))
    return {
        "status": status,
        "content_read": bool(content_read.get("content_read") is True and status == "completed"),
        "char_count": char_count,
        "preview": preview,
        "read_ref": read_ref,
        "read_id": read_id,
        "error": error,
        "activity_refs": activity_refs,
        "memory_publication": _optional_str(content_read.get("memory_publication")),
        "memory_candidate_id": _optional_str(content_read.get("memory_candidate_id")),
    }


def _source_content_structure(source: Mapping[str, object]) -> dict[str, object]:
    metadata = source.get("metadata")
    if not isinstance(metadata, Mapping):
        return _empty_content_structure()
    structure = metadata.get("content_structure")
    if not isinstance(structure, Mapping):
        return _empty_content_structure()
    return {
        "status": _optional_str(structure.get("status")) or "not_started",
        "structure_ref": _optional_str(structure.get("structure_ref")),
        "tags": _string_sequence(structure.get("tags")),
        "paragraph_tags": _paragraph_tag_sequence(structure.get("paragraph_tags")),
        "summary": _optional_str(structure.get("summary")),
        "key_points": _string_sequence(structure.get("key_points")),
        "structured_body": _optional_str(structure.get("structured_body")),
        "series_candidate": _optional_str(structure.get("series_candidate")),
        "series_confidence": _optional_float(structure.get("series_confidence")),
        "series_reason": _optional_str(structure.get("series_reason")),
        "organization_prompt_refs": _prompt_ref_sequence(structure.get("organization_prompt_refs")),
        "activity_refs": _string_sequence(structure.get("activity_refs")),
    }


def _source_manual_tags(source: Mapping[str, object]) -> tuple[str, ...]:
    metadata = source.get("metadata")
    values = metadata.get("manual_tags") if isinstance(metadata, Mapping) else ()
    return tuple(str(value).strip() for value in (values or ()) if str(value).strip())


def _source_series_assignment(source: Mapping[str, object]) -> dict[str, object]:
    metadata = source.get("metadata")
    if not isinstance(metadata, Mapping):
        return _empty_series_assignment()
    assignment = metadata.get("series_assignment")
    if not isinstance(assignment, Mapping):
        return _empty_series_assignment()
    return {
        "status": _optional_str(assignment.get("status")) or "not_started",
        "assignment_ref": _optional_str(assignment.get("assignment_ref")),
        "series_id": _optional_str(assignment.get("series_id")),
        "series_name": _optional_str(assignment.get("series_name")),
        "series_ref": _optional_str(assignment.get("series_ref")),
        "activity_refs": _string_sequence(assignment.get("activity_refs")),
    }


def _source_template_outputs(source: Mapping[str, object]) -> dict[str, object]:
    metadata = source.get("metadata")
    if not isinstance(metadata, Mapping):
        return {"prompt_refs": (), "output_refs": ()}
    prompt_refs: list[Mapping[str, object]] = []
    output_refs: list[str] = []
    seen_prompts: set[str] = set()
    seen_outputs: set[str] = set()
    for collection_name in ("template_outputs", "media_template_outputs"):
        outputs = metadata.get(collection_name)
        if not isinstance(outputs, Sequence) or isinstance(outputs, (str, bytes)):
            continue
        for output in outputs:
            if not isinstance(output, Mapping):
                continue
            output_ref = _optional_str(output.get("output_ref"))
            if output_ref is not None and output_ref not in seen_outputs:
                seen_outputs.add(output_ref)
                output_refs.append(output_ref)
            for prompt_ref in _prompt_ref_sequence(output.get("template_prompt_refs")):
                prompt_id = _optional_str(prompt_ref.get("id"))
                if prompt_id is None or prompt_id in seen_prompts:
                    continue
                seen_prompts.add(prompt_id)
                prompt_refs.append(prompt_ref)
    return {"prompt_refs": tuple(prompt_refs), "output_refs": tuple(output_refs)}


def _source_inspiration(source: Mapping[str, object]) -> dict[str, object]:
    metadata = source.get("metadata")
    if not isinstance(metadata, Mapping):
        return _empty_inspiration()
    inspiration = metadata.get("inspiration")
    if not isinstance(inspiration, Mapping):
        return _empty_inspiration()
    return {
        "status": _optional_str(inspiration.get("status")) or "not_started",
        "inspiration_id": _optional_str(inspiration.get("inspiration_id")),
        "inspiration_ref": _optional_str(inspiration.get("inspiration_ref")),
        "series_id": _optional_str(inspiration.get("series_id")),
        "series_name": _optional_str(inspiration.get("series_name")),
        "themes": _string_sequence(inspiration.get("themes")),
        "summary": _optional_str(inspiration.get("summary")),
        "activity_refs": _string_sequence(inspiration.get("activity_refs")),
        "memory_publication": _optional_str(inspiration.get("memory_publication")),
    }


def _source_file_authorization_refs(source: Mapping[str, object]) -> tuple[str, ...]:
    metadata = source.get("metadata")
    if not isinstance(metadata, Mapping):
        return ()
    authorization_refs: list[str] = []
    for key in (
        "file_authorization",
        "document_authorization",
        "image_authorization",
        "audio_authorization",
        "video_authorization",
    ):
        authorization = metadata.get(key)
        if not isinstance(authorization, Mapping):
            continue
        authorization_ref = _optional_str(authorization.get("authorization_ref"))
        if authorization_ref is not None:
            authorization_refs.append(authorization_ref)
    return tuple(authorization_refs)


def _source_media_processing(source: Mapping[str, object]) -> dict[str, object]:
    metadata = source.get("metadata")
    if not isinstance(metadata, Mapping):
        return _empty_media_processing()
    media_processing = metadata.get("media_processing")
    if not isinstance(media_processing, Mapping):
        audio_transcription = _media_processing_from_audio_transcription(metadata)
        if audio_transcription["status"] != "not_started":
            return audio_transcription
        return _media_processing_from_audio_track_extraction(metadata)
    return {
        "status": _optional_str(media_processing.get("status")) or "not_started",
        "required_capability": _optional_str(media_processing.get("required_capability")),
        "disabled_reason": _optional_str(media_processing.get("disabled_reason")),
        "job_ref": _optional_str(media_processing.get("job_ref")),
        "expected_output_refs": _string_sequence(media_processing.get("expected_output_refs")),
        "activity_refs": _string_sequence(media_processing.get("activity_refs")),
        "error": _optional_str(media_processing.get("error")),
        "output_refs": _string_sequence(media_processing.get("output_refs")),
        "output_ids": _media_output_ids_from_refs(_string_sequence(media_processing.get("output_refs"))),
        "output_preview": _optional_str(media_processing.get("output_preview")),
        "memory_publication": _optional_str(media_processing.get("memory_publication")),
        "memory_candidate_id": _optional_str(media_processing.get("memory_candidate_id")),
        "audio_asset_id": _optional_str(media_processing.get("audio_asset_id")),
        "audio_asset_ref": _optional_str(media_processing.get("audio_asset_ref")),
        "transcript_output_id": _optional_str(media_processing.get("transcript_output_id")),
        "summary_output_id": _optional_str(media_processing.get("summary_output_id")),
    }


def _media_processing_from_audio_track_extraction(metadata: Mapping[str, object]) -> dict[str, object]:
    extraction = metadata.get("audio_track_extraction")
    if not isinstance(extraction, Mapping):
        return _empty_media_processing()
    status = _optional_str(extraction.get("summary_state")) or _optional_str(extraction.get("asr_state"))
    output_ref = (
        _optional_str(extraction.get("summary_output_ref"))
        or _optional_str(extraction.get("transcript_output_ref"))
        or _optional_str(extraction.get("output_ref"))
    )
    output_refs = (output_ref,) if output_ref else ()
    return {
        "status": "completed" if status == "completed" else (_optional_str(extraction.get("status")) or "not_started"),
        "required_capability": (
            "transcript_summary"
            if _optional_str(extraction.get("transcript_output_id")) and status != "completed"
            else "video_audio_extraction"
        ),
        "disabled_reason": None,
        "job_ref": None,
        "expected_output_refs": (),
        "activity_refs": (),
        "error": None,
        "output_refs": output_refs,
        "output_ids": _media_output_ids_from_refs(output_refs),
        "output_preview": (
            _optional_str(extraction.get("summary_preview"))
            or _optional_str(extraction.get("transcript_preview"))
            or _optional_str(extraction.get("output_preview"))
        ),
        "memory_publication": _optional_str(extraction.get("memory_publication")),
        "memory_candidate_id": _optional_str(extraction.get("memory_candidate_id")),
        "audio_asset_id": _optional_str(extraction.get("audio_asset_id")),
        "audio_asset_ref": _optional_str(extraction.get("audio_asset_ref")),
        "transcript_output_id": _optional_str(extraction.get("transcript_output_id")),
        "summary_output_id": _optional_str(extraction.get("summary_output_id")),
    }


def _media_processing_from_audio_transcription(metadata: Mapping[str, object]) -> dict[str, object]:
    transcription = metadata.get("audio_transcription")
    if not isinstance(transcription, Mapping):
        return _empty_media_processing()
    output_ref = _optional_str(transcription.get("transcript_output_ref"))
    output_refs = (output_ref,) if output_ref else ()
    status = _optional_str(transcription.get("asr_state")) or _optional_str(transcription.get("status"))
    return {
        "status": "completed" if status == "completed" else (status or "not_started"),
        "required_capability": "audio_transcription",
        "disabled_reason": None,
        "job_ref": None,
        "expected_output_refs": (),
        "activity_refs": (),
        "error": _optional_str(transcription.get("error")),
        "output_refs": output_refs,
        "output_ids": _media_output_ids_from_refs(output_refs),
        "output_preview": _optional_str(transcription.get("transcript_preview")),
        "memory_publication": _optional_str(transcription.get("memory_publication")),
        "memory_candidate_id": _optional_str(transcription.get("memory_candidate_id")),
        "audio_asset_id": _optional_str(transcription.get("audio_asset_id")),
        "audio_asset_ref": _optional_str(transcription.get("audio_asset_ref")),
        "transcript_output_id": _optional_str(transcription.get("transcript_output_id")),
        "summary_output_id": None,
    }


def _source_video_auto_workflow(source: Mapping[str, object]) -> dict[str, object]:
    metadata = source.get("metadata")
    if not isinstance(metadata, Mapping):
        return _empty_video_auto_workflow()
    workflow = metadata.get("video_auto_workflow")
    if not isinstance(workflow, Mapping):
        return _empty_video_auto_workflow()
    return {
        "status": _optional_str(workflow.get("status")) or "not_started",
        "workflow_id": _optional_str(workflow.get("workflow_id")),
        "workflow_ref": _optional_str(workflow.get("workflow_ref")),
        "steps": _workflow_steps(workflow.get("steps")),
        "error": _optional_str(workflow.get("error")),
        "summary_provider_status": _optional_str(workflow.get("summary_provider_status")),
        "summary_provider_name": _optional_str(workflow.get("summary_provider_name")),
        "summary_readiness_reason": _optional_str(workflow.get("summary_readiness_reason")),
        "summary_next_step": _optional_str(workflow.get("summary_next_step")),
        "publication_id": _optional_str(workflow.get("publication_id")),
        "published_ref": _optional_str(workflow.get("published_ref")),
        "rollback_ref": _optional_str(workflow.get("rollback_ref")),
    }


def _source_audio_auto_workflow(source: Mapping[str, object]) -> dict[str, object]:
    metadata = source.get("metadata")
    if not isinstance(metadata, Mapping):
        return _empty_audio_auto_workflow()
    workflow = metadata.get("audio_auto_workflow")
    if not isinstance(workflow, Mapping):
        return _empty_audio_auto_workflow()
    return {
        "status": _optional_str(workflow.get("status")) or "not_started",
        "workflow_id": _optional_str(workflow.get("workflow_id")),
        "workflow_ref": _optional_str(workflow.get("workflow_ref")),
        "steps": _workflow_steps(workflow.get("steps")),
        "error": _optional_str(workflow.get("error")),
        "transcriber_status": _optional_str(workflow.get("transcriber_status")),
        "transcriber_model_profile": _optional_str(workflow.get("transcriber_model_profile")),
        "transcriber_model_name": _optional_str(workflow.get("transcriber_model_name")),
        "audio_asset_status": _optional_str(workflow.get("audio_asset_status")),
        "readiness_reason": _optional_str(workflow.get("readiness_reason")),
        "next_step": _optional_str(workflow.get("next_step")),
    }


def _workflow_steps(value: object) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    steps: list[Mapping[str, object]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        steps.append(
            {
                "name": _optional_str(item.get("name")),
                "status": _optional_str(item.get("status")),
                "reason": _optional_str(item.get("reason")),
                "job_id": _optional_str(item.get("job_id")),
                "output_id": _optional_str(item.get("output_id")),
                "audio_asset_id": _optional_str(item.get("audio_asset_id")),
                "candidate_id": _optional_str(item.get("candidate_id")),
            }
        )
    return tuple(steps)


def _empty_content_read() -> dict[str, object]:
    return {
        "status": "not_started",
        "content_read": False,
        "char_count": None,
        "preview": None,
        "read_ref": None,
        "read_id": None,
        "error": None,
        "activity_refs": (),
        "memory_publication": None,
        "memory_candidate_id": None,
    }


def _empty_content_structure() -> dict[str, object]:
    return {
        "status": "not_started",
        "structure_ref": None,
        "tags": (),
        "paragraph_tags": (),
        "summary": None,
        "key_points": (),
        "structured_body": None,
        "series_candidate": None,
        "series_confidence": None,
        "series_reason": None,
        "organization_prompt_refs": (),
        "activity_refs": (),
    }


def _empty_series_assignment() -> dict[str, object]:
    return {
        "status": "not_started",
        "assignment_ref": None,
        "series_id": None,
        "series_name": None,
        "series_ref": None,
        "activity_refs": (),
    }


def _empty_inspiration() -> dict[str, object]:
    return {
        "status": "not_started",
        "inspiration_id": None,
        "inspiration_ref": None,
        "series_id": None,
        "series_name": None,
        "themes": (),
        "summary": None,
        "activity_refs": (),
        "memory_publication": None,
    }


def _empty_media_processing() -> dict[str, object]:
    return {
        "status": "not_started",
        "required_capability": None,
        "disabled_reason": None,
        "job_ref": None,
        "expected_output_refs": (),
        "activity_refs": (),
        "error": None,
        "output_refs": (),
        "output_ids": (),
        "output_preview": None,
        "memory_publication": None,
        "memory_candidate_id": None,
        "audio_asset_id": None,
        "audio_asset_ref": None,
        "transcript_output_id": None,
        "summary_output_id": None,
    }


def _empty_video_auto_workflow() -> dict[str, object]:
    return {
        "status": "not_started",
        "workflow_id": None,
        "workflow_ref": None,
        "steps": (),
        "error": None,
        "summary_provider_status": None,
        "summary_provider_name": None,
        "summary_readiness_reason": None,
        "summary_next_step": None,
        "publication_id": None,
        "published_ref": None,
        "rollback_ref": None,
    }


def _empty_audio_auto_workflow() -> dict[str, object]:
    return {
        "status": "not_started",
        "workflow_id": None,
        "workflow_ref": None,
        "steps": (),
        "error": None,
        "transcriber_status": None,
        "transcriber_model_profile": None,
        "transcriber_model_name": None,
        "audio_asset_status": None,
        "readiness_reason": None,
        "next_step": None,
    }


def _source_collection(source: Mapping[str, object]) -> dict[str, object]:
    metadata = source.get("metadata")
    if source.get("type") != "collection" or not isinstance(metadata, Mapping):
        return {"item_count": None, "urls": (), "child_source_ids": ()}
    urls_value = metadata.get("urls")
    if not isinstance(urls_value, Sequence) or isinstance(urls_value, (str, bytes)):
        urls: tuple[str, ...] = ()
    else:
        urls = tuple(item for item in urls_value if isinstance(item, str) and item.strip())
    item_count_value = metadata.get("item_count")
    item_count = item_count_value if isinstance(item_count_value, int) else len(urls)
    child_source_ids = tuple(_link_source_id_for_url(url) for url in urls)
    return {"item_count": item_count, "urls": urls, "child_source_ids": child_source_ids}


def _link_source_id_for_url(url: str) -> str:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return f"source-link-{digest[:12]}"


def _content_read_id_from_ref(read_ref: str | None) -> str | None:
    if read_ref is None:
        return None
    marker = "/source-content-reads/"
    if marker not in read_ref:
        return None
    tail = read_ref.rsplit(marker, 1)[-1]
    if tail.endswith(".json"):
        tail = tail[:-5]
    return tail or None


def _media_output_ids_from_refs(output_refs: Sequence[str]) -> tuple[str, ...]:
    ids: list[str] = []
    for ref in output_refs:
        marker = "/media-processing-outputs/"
        if marker not in ref:
            continue
        tail = ref.rsplit(marker, 1)[-1]
        if tail.endswith(".json"):
            tail = tail[:-5]
        if tail:
            ids.append(tail)
    return tuple(ids)


def _candidate_title(candidate: Mapping[str, object], *, fallback: str) -> str:
    proposed = candidate.get("proposed_content")
    if isinstance(proposed, str) and proposed.strip():
        return proposed.strip().splitlines()[0][:80]
    return fallback


def _external_agent_draft_title(draft: Mapping[str, object], *, fallback: str) -> str:
    summary = draft.get("summary")
    if isinstance(summary, str) and summary.strip():
        return summary.strip().splitlines()[0][:80]
    proposed = draft.get("proposed_content")
    if isinstance(proposed, str) and proposed.strip():
        return proposed.strip().splitlines()[0][:80]
    return fallback


def _external_agent_evidence_refs(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    refs: list[str] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        locator = item.get("locator")
        if isinstance(locator, str) and locator.startswith("crp://"):
            refs.append(locator)
    return tuple(refs)


def _external_agent_applied_summary(
    application: Mapping[str, object],
    *,
    namespace_id: str,
) -> dict[str, object | None]:
    document_id = _optional_str(application.get("applied_document_id"))
    document_revision = _optional_int(application.get("applied_document_revision"))
    if document_id is not None:
        return {
            "kind": "document",
            "id": document_id,
            "revision": document_revision,
            "ref": f"crp://{namespace_id}/documents/{document_id}.json",
        }
    project_skill_id = _optional_str(application.get("applied_project_skill_id"))
    project_skill_revision = _optional_int(application.get("applied_project_skill_revision"))
    if project_skill_id is not None:
        return {
            "kind": "project_skill",
            "id": project_skill_id,
            "revision": project_skill_revision,
            "ref": f"crp://{namespace_id}/project-skills/{project_skill_id}.json",
        }
    series_memory_id = _optional_str(application.get("applied_series_memory_id"))
    series_memory_revision = _optional_int(application.get("applied_series_memory_revision"))
    if series_memory_id is not None:
        return {
            "kind": "series_memory",
            "id": series_memory_id,
            "revision": series_memory_revision,
            "ref": f"crp://{namespace_id}/memory/series/{series_memory_id}.json",
        }
    return {"kind": None, "id": None, "revision": None, "ref": None}


def _candidate_trace_refs(candidate: Mapping[str, object], *, candidate_ref: str) -> tuple[str, ...]:
    refs = [candidate_ref]
    provenance = candidate.get("provenance")
    if isinstance(provenance, Mapping):
        for item in provenance.get("input_refs", []):
            if not isinstance(item, Mapping):
                continue
            uri = item.get("uri")
            if isinstance(uri, str) and uri.startswith("crp://") and uri not in refs:
                refs.append(uri)
    return tuple(refs)


def _memory_title(memory: Mapping[str, object], *, fallback: str) -> str:
    for key in ("title", "summary", "content", "overview"):
        value = memory.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().splitlines()[0][:80]
    return fallback


def _title(item: Mapping[str, object], *, fallback: str) -> str:
    title = item.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()
    return fallback


def _memory_layer(memory: Mapping[str, object]) -> str:
    layer = memory.get("layer")
    if isinstance(layer, str) and layer in {"atom", "scenario", "series_memory", "project_skill"}:
        return layer
    if "skill_name" in memory or "procedure" in memory:
        return "project_skill"
    if "atom_ids" in memory:
        return "scenario"
    if "overview" in memory or "project_ids" in memory:
        return "series_memory"
    return "atom"


def _memory_project_id(memory: Mapping[str, object]) -> str | None:
    project_id = _optional_str(memory.get("project_id"))
    if project_id is not None:
        return project_id
    project_ids = memory.get("project_ids")
    if isinstance(project_ids, Sequence) and not isinstance(project_ids, (str, bytes)):
        for item in project_ids:
            if isinstance(item, str) and item:
                return item
    return None


def _required_str(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise LibraryOverviewError(f"{key} is required")
    return value


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _optional_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _string_sequence(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item)


def _paragraph_tag_sequence(value: object) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    items: list[Mapping[str, object]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        paragraph_id = _optional_str(item.get("paragraph_id"))
        text_preview = _optional_str(item.get("text_preview"))
        if paragraph_id is None or text_preview is None:
            continue
        items.append(
            {
                "paragraph_id": paragraph_id,
                "text_preview": text_preview,
                "tags": list(_string_sequence(item.get("tags"))),
                "confidence": _optional_float(item.get("confidence")),
            }
        )
    return tuple(items)


def _prompt_ref_sequence(value: object) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    refs: list[Mapping[str, object]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping):
            continue
        prompt_id = _optional_str(item.get("id"))
        if prompt_id is None or prompt_id in seen:
            continue
        seen.add(prompt_id)
        ref: dict[str, object] = {"id": prompt_id}
        revision = _optional_int(item.get("revision"))
        if revision is not None:
            ref["revision"] = revision
        source = _optional_str(item.get("source"))
        if source is not None:
            ref["source"] = source
        stage_id = _optional_str(item.get("stage_id"))
        if stage_id is not None:
            ref["stage_id"] = stage_id
        model_profile_id = _optional_str(item.get("model_profile_id"))
        if model_profile_id is not None:
            ref["model_profile_id"] = model_profile_id
        refs.append(ref)
    return tuple(refs)
