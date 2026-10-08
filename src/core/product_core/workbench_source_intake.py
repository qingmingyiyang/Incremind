from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from core.ingestion_core import SourceRegistrarPort, SourceSubmission
from core.job_runner import JobRepositoryPort


WorkbenchTextIntakeStatus = Literal["captured"]


@dataclass(frozen=True, slots=True)
class WorkbenchMinimalLibraryBridgeItem:
    item_id: str
    item_kind: str
    title: str
    source_id: str
    source_uri: str
    media_type: str
    processing_state: str
    capture_job_id: str
    capture_job_status: str
    evidence_refs: tuple[str, ...]
    selectable_evidence_refs: tuple[str, ...]
    selection_id: str
    selection_state: str
    selection_persistence_ref: str
    memory_publication_state: str
    boundary: str


@dataclass(frozen=True, slots=True)
class WorkbenchTextSourceIntakeResult:
    status: WorkbenchTextIntakeStatus
    source_id: str
    source_uri: str
    source_title: str
    source_type: str
    capture_mode: str
    media_type: str
    size_bytes: int
    processing_state: str
    content_hash: str
    job_id: str
    job_type: str
    job_status: str
    job_progress_percent: int
    job_progress_message: str
    step_names: tuple[str, ...]
    published_output_kinds: tuple[str, ...]
    trace_refs: tuple[str, ...]
    next_step: str
    library_bridge_item: WorkbenchMinimalLibraryBridgeItem
    intake_intent: str
    intake_intent_label: str
    intake_route: str
    intake_feedback: str
    structured_output_plan: tuple[str, ...]
    memory_layer_update_plan: tuple[str, ...]
    suggested_next_actions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WorkbenchLinkSourceIntakeResult:
    status: WorkbenchTextIntakeStatus
    source_id: str
    source_uri: str
    source_title: str
    source_type: str
    capture_mode: str
    media_type: str
    size_bytes: int
    processing_state: str
    content_hash: str
    original_url: str
    remote_fetch_state: str
    source_display_kind: str
    capture_boundary: str
    remote_fetch_boundary: str
    library_selection_scope: str
    job_id: str
    job_type: str
    job_status: str
    job_progress_percent: int
    job_progress_message: str
    step_names: tuple[str, ...]
    published_output_kinds: tuple[str, ...]
    trace_refs: tuple[str, ...]
    next_step: str
    library_bridge_item: WorkbenchMinimalLibraryBridgeItem


@dataclass(frozen=True, slots=True)
class WorkbenchBookmarkCollectionIntakeResult:
    status: WorkbenchTextIntakeStatus
    source_id: str
    source_uri: str
    source_title: str
    source_type: str
    capture_mode: str
    media_type: str
    size_bytes: int
    processing_state: str
    content_hash: str
    collection_item_count: int
    child_source_ids: tuple[str, ...]
    child_source_uris: tuple[str, ...]
    original_urls: tuple[str, ...]
    remote_fetch_state: str
    source_display_kind: str
    capture_boundary: str
    remote_fetch_boundary: str
    library_selection_scope: str
    job_id: str
    job_type: str
    job_status: str
    job_progress_percent: int
    job_progress_message: str
    step_names: tuple[str, ...]
    published_output_kinds: tuple[str, ...]
    trace_refs: tuple[str, ...]
    next_step: str
    library_bridge_item: WorkbenchMinimalLibraryBridgeItem


@dataclass(frozen=True, slots=True)
class WorkbenchFileSourceIntakeResult:
    status: WorkbenchTextIntakeStatus
    source_id: str
    source_uri: str
    source_title: str
    source_type: str
    capture_mode: str
    media_type: str
    size_bytes: int
    processing_state: str
    content_hash: str
    file_display_name: str
    file_reference: str
    file_content_policy: str
    path_policy: str
    source_display_kind: str
    capture_boundary: str
    parser_state: str
    asset_id: str
    asset_uri: str
    asset_record_ref: str
    asset_uri_role: str
    asset_record_ref_role: str
    job_asset_output_ref: str
    asset_storage_mode: str
    asset_availability: str
    asset_availability_reason: str
    asset_handoff_state: str
    library_selection_scope: str
    library_selection_copy: str
    no_content_read_boundary: str
    parser_boundary: str
    job_id: str
    job_type: str
    job_status: str
    job_progress_percent: int
    job_progress_message: str
    step_names: tuple[str, ...]
    published_output_kinds: tuple[str, ...]
    trace_refs: tuple[str, ...]
    next_step: str
    library_bridge_item: WorkbenchMinimalLibraryBridgeItem
    asset_record: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class WorkbenchImageSourceIntakeResult:
    status: WorkbenchTextIntakeStatus
    source_id: str
    source_uri: str
    source_title: str
    source_type: str
    capture_mode: str
    media_type: str
    size_bytes: int
    processing_state: str
    content_hash: str
    image_display_name: str
    image_reference: str
    image_reference_role: str
    width_px: int | None
    height_px: int | None
    binary_content_policy: str
    path_policy: str
    source_display_kind: str
    capture_boundary: str
    preview_policy: str
    preview_boundary: str
    thumbnail_state: str
    ocr_state: str
    extractor_state: str
    asset_id: str
    asset_uri: str
    asset_record_ref: str
    asset_uri_role: str
    asset_record_ref_role: str
    job_asset_output_ref: str
    asset_storage_mode: str
    asset_availability: str
    asset_availability_reason: str
    asset_handoff_state: str
    asset_handoff_copy: str
    library_selection_scope: str
    library_selection_copy: str
    library_selection_boundary: str
    no_binary_read_boundary: str
    ocr_boundary: str
    extractor_boundary: str
    derived_output_boundary: str
    job_id: str
    job_type: str
    job_status: str
    job_progress_percent: int
    job_progress_message: str
    step_names: tuple[str, ...]
    published_output_kinds: tuple[str, ...]
    trace_refs: tuple[str, ...]
    next_step: str
    library_bridge_item: WorkbenchMinimalLibraryBridgeItem
    asset_record: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class WorkbenchAudioSourceIntakeResult:
    status: WorkbenchTextIntakeStatus
    source_id: str
    source_uri: str
    source_title: str
    source_type: str
    capture_mode: str
    media_type: str
    size_bytes: int
    processing_state: str
    content_hash: str
    source_uri_role: str
    audio_display_name: str
    audio_reference: str
    audio_reference_role: str
    duration_ms: int | None
    media_content_policy: str
    path_policy: str
    source_display_kind: str
    capture_boundary: str
    transcription_state: str
    transcription_policy: str
    waveform_state: str
    waveform_policy: str
    remote_processing_state: str
    remote_processing_policy: str
    asset_id: str
    asset_uri: str
    asset_record_ref: str
    asset_uri_role: str
    asset_record_ref_role: str
    job_source_output_ref: str
    job_source_output_ref_role: str
    job_asset_output_ref: str
    job_asset_output_ref_role: str
    asset_storage_mode: str
    asset_availability: str
    asset_availability_reason: str
    asset_handoff_state: str
    asset_handoff_copy: str
    library_selection_scope: str
    library_selection_copy: str
    library_selection_boundary: str
    library_selection_role_summary: str
    library_selection_excluded_outputs: tuple[str, ...]
    memory_selection_policy: str
    no_media_read_boundary: str
    derived_output_state: str
    derived_output_boundary: str
    parser_readiness_state: str
    parser_readiness_boundary: str
    parser_readiness_copy: str
    parser_required_evidence_refs: tuple[str, ...]
    parser_blocked_operations: tuple[str, ...]
    trace_display_sections: tuple[str, ...]
    job_id: str
    job_type: str
    job_status: str
    job_progress_percent: int
    job_progress_message: str
    step_names: tuple[str, ...]
    published_output_kinds: tuple[str, ...]
    trace_refs: tuple[str, ...]
    next_step: str
    library_bridge_item: WorkbenchMinimalLibraryBridgeItem
    asset_record: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class WorkbenchVideoSourceIntakeResult:
    status: WorkbenchTextIntakeStatus
    source_id: str
    source_uri: str
    source_title: str
    source_type: str
    capture_mode: str
    media_type: str
    size_bytes: int
    processing_state: str
    content_hash: str
    source_uri_role: str
    video_display_name: str
    video_reference: str
    video_reference_role: str
    duration_ms: int | None
    width_px: int | None
    height_px: int | None
    media_content_policy: str
    path_policy: str
    source_display_kind: str
    capture_boundary: str
    frame_extraction_state: str
    frame_extraction_policy: str
    audio_track_extraction_state: str
    audio_track_extraction_policy: str
    thumbnail_state: str
    thumbnail_policy: str
    remote_processing_state: str
    remote_processing_policy: str
    asset_id: str
    asset_uri: str
    asset_record_ref: str
    asset_uri_role: str
    asset_record_ref_role: str
    job_source_output_ref: str
    job_source_output_ref_role: str
    job_asset_output_ref: str
    job_asset_output_ref_role: str
    asset_storage_mode: str
    asset_availability: str
    asset_availability_reason: str
    asset_handoff_state: str
    asset_handoff_copy: str
    library_selection_scope: str
    library_selection_copy: str
    library_selection_boundary: str
    library_selection_role_summary: str
    library_selection_excluded_outputs: tuple[str, ...]
    memory_selection_policy: str
    no_media_read_boundary: str
    derived_output_state: str
    derived_output_boundary: str
    parser_readiness_state: str
    parser_readiness_boundary: str
    parser_readiness_copy: str
    parser_required_evidence_refs: tuple[str, ...]
    parser_blocked_operations: tuple[str, ...]
    trace_display_sections: tuple[str, ...]
    trace_role_summary: str
    job_output_role_summary: str
    library_selection_evidence_roles: tuple[str, ...]
    job_id: str
    job_type: str
    job_status: str
    job_progress_percent: int
    job_progress_message: str
    step_names: tuple[str, ...]
    published_output_kinds: tuple[str, ...]
    trace_refs: tuple[str, ...]
    next_step: str
    library_bridge_item: WorkbenchMinimalLibraryBridgeItem
    asset_record: Mapping[str, object]


class CaptureWorkbenchTextSource:
    """Capture a minimal workbench text input as traceable Source and Job evidence."""

    def __init__(
        self,
        *,
        source_registrar: SourceRegistrarPort,
        job_repository: JobRepositoryPort,
        namespace_id: str = "default",
        now: str = "2026-06-30T10:00:00+08:00",
    ) -> None:
        self._source_registrar = source_registrar
        self._job_repository = job_repository
        self._namespace_id = namespace_id
        self._now = now

    def execute(
        self,
        *,
        title: str,
        content: str,
    ) -> WorkbenchTextSourceIntakeResult:
        clean_title = title.strip() or "Untitled workbench text"
        clean_content = content.strip()
        if not clean_content:
            raise ValueError("workbench text intake requires content")

        source = self._source_registrar.register(
            SourceSubmission(kind="text", title=clean_title, content=clean_content)
        )
        source_id = _required_str(source, "id")
        source_uri = _required_str(source, "storage_uri")
        source_title = _required_str(source, "title")
        source_type = _required_str(source, "type")
        capture_mode = _required_str(source, "capture_mode")
        media_type = _required_str(source, "media_type")
        processing_state = _required_str(source, "processing_state")
        content_hash = _required_str(source, "content_hash")
        size_bytes = _required_int(source, "size_bytes")
        job_id = f"job-capture-{source_id}"
        job = self._capture_job(
            job_id=job_id,
            source_id=source_id,
            source_uri=source_uri,
        )
        self._job_repository.save(job)

        trace_refs = (source_uri, self._log_uri(job_id, "persist_source"))
        intent = _classify_direct_text_intent(clean_content)
        library_bridge_item = WorkbenchMinimalLibraryBridgeItem(
            item_id=f"library-source-{source_id}",
            item_kind="source_preview",
            title=source_title,
            source_id=source_id,
            source_uri=source_uri,
            media_type=media_type,
            processing_state=processing_state,
            capture_job_id=job_id,
            capture_job_status="completed",
            evidence_refs=trace_refs,
            selectable_evidence_refs=trace_refs,
            selection_id=f"library-selection-{source_id}",
            selection_state="available",
            selection_persistence_ref=self._selection_uri(source_id),
            memory_publication_state="not_started",
            boundary="source_job_selection_only",
        )

        return WorkbenchTextSourceIntakeResult(
            status="captured",
            source_id=source_id,
            source_uri=source_uri,
            source_title=source_title,
            source_type=source_type,
            capture_mode=capture_mode,
            media_type=media_type,
            size_bytes=size_bytes,
            processing_state=processing_state,
            content_hash=content_hash,
            job_id=job_id,
            job_type="capture",
            job_status="completed",
            job_progress_percent=100,
            job_progress_message="captured workbench text source",
            step_names=("persist_source",),
            published_output_kinds=("source",),
            trace_refs=trace_refs,
            next_step="minimal_library_view_bridge_ready",
            library_bridge_item=library_bridge_item,
            intake_intent=intent["intent"],
            intake_intent_label=intent["label"],
            intake_route=intent["route"],
            intake_feedback=intent["feedback"],
            structured_output_plan=tuple(intent["structured_output_plan"]),
            memory_layer_update_plan=tuple(intent["memory_layer_update_plan"]),
            suggested_next_actions=tuple(intent["suggested_next_actions"]),
        )

    def _capture_job(self, *, job_id: str, source_id: str, source_uri: str) -> dict[str, object]:
        log_ref = self._log_uri(job_id, "persist_source")
        return {
            "schema_version": "1.0.0",
            "id": job_id,
            "source_id": source_id,
            "job_type": "capture",
            "idempotency_key": f"workbench-capture-{source_id}",
            "status": "completed",
            "attempt": 1,
            "max_attempts": 1,
            "lease": None,
            "progress": {
                "current": 1,
                "total": 1,
                "percent": 100,
                "message": "captured workbench text source",
            },
            "steps": [
                {
                    "name": "persist_source",
                    "status": "completed",
                    "attempt": 1,
                    "started_at": self._now,
                    "completed_at": self._now,
                    "progress": 100,
                    "input_refs": [source_uri],
                    "staged_output_refs": [],
                    "log_refs": [log_ref],
                    "error": None,
                }
            ],
            "error": None,
            "checkpoint": None,
            "staged_outputs": [],
            "published_outputs": [
                {
                    "kind": "source",
                    "uri": source_uri,
                    "object_id": source_id,
                    "published": True,
                }
            ],
            "log_refs": [log_ref],
            "created_at": self._now,
            "updated_at": self._now,
        }

    def _log_uri(self, job_id: str, name: str) -> str:
        return f"crp://{self._namespace_id}/logs/jobs/{job_id}/{name}.jsonl"

    def _selection_uri(self, source_id: str) -> str:
        return f"crp://{self._namespace_id}/library/selections/{source_id}.json"


class CaptureWorkbenchLinkSource:
    """Capture a user-provided URL as Source metadata without fetching remote content."""

    def __init__(
        self,
        *,
        source_registrar: SourceRegistrarPort,
        job_repository: JobRepositoryPort,
        namespace_id: str = "default",
        now: str = "2026-07-01T10:00:00+08:00",
    ) -> None:
        self._source_registrar = source_registrar
        self._job_repository = job_repository
        self._namespace_id = namespace_id
        self._now = now

    def execute(
        self,
        *,
        title: str,
        url: str,
    ) -> WorkbenchLinkSourceIntakeResult:
        clean_title = title.strip()
        clean_url = url.strip()
        if not clean_url:
            raise ValueError("workbench link intake requires url")

        source = self._source_registrar.register(
            SourceSubmission(kind="link", title=clean_title, original_url=clean_url)
        )
        source_id = _required_str(source, "id")
        source_uri = _required_str(source, "storage_uri")
        source_title = _required_str(source, "title")
        source_type = _required_str(source, "type")
        capture_mode = _required_str(source, "capture_mode")
        media_type = _required_str(source, "media_type")
        processing_state = _required_str(source, "processing_state")
        content_hash = _required_str(source, "content_hash")
        original_url = _required_str(source, "original_url")
        size_bytes = _required_int(source, "size_bytes")
        job_id = f"job-capture-{source_id}"
        job = self._capture_job(
            job_id=job_id,
            source_id=source_id,
            source_uri=source_uri,
            original_url=original_url,
        )
        self._job_repository.save(job)

        trace_refs = (source_uri, self._log_uri(job_id, "persist_source"))
        library_bridge_item = WorkbenchMinimalLibraryBridgeItem(
            item_id=f"library-source-{source_id}",
            item_kind="source_preview",
            title=source_title,
            source_id=source_id,
            source_uri=source_uri,
            media_type=media_type,
            processing_state=processing_state,
            capture_job_id=job_id,
            capture_job_status="completed",
            evidence_refs=trace_refs,
            selectable_evidence_refs=trace_refs,
            selection_id=f"library-selection-{source_id}",
            selection_state="available",
            selection_persistence_ref=self._selection_uri(source_id),
            memory_publication_state="not_started",
            boundary="source_job_selection_only",
        )

        return WorkbenchLinkSourceIntakeResult(
            status="captured",
            source_id=source_id,
            source_uri=source_uri,
            source_title=source_title,
            source_type=source_type,
            capture_mode=capture_mode,
            media_type=media_type,
            size_bytes=size_bytes,
            processing_state=processing_state,
            content_hash=content_hash,
            original_url=original_url,
            remote_fetch_state="not_performed",
            source_display_kind="url_reference",
            capture_boundary="url_metadata_only",
            remote_fetch_boundary="remote_fetch_not_performed",
            library_selection_scope="source_job_url_reference_only",
            job_id=job_id,
            job_type="capture",
            job_status="completed",
            job_progress_percent=100,
            job_progress_message="captured workbench link source without remote fetch",
            step_names=("persist_source",),
            published_output_kinds=("source",),
            trace_refs=trace_refs,
            next_step="minimal_library_view_bridge_ready",
            library_bridge_item=library_bridge_item,
        )

    def _capture_job(
        self,
        *,
        job_id: str,
        source_id: str,
        source_uri: str,
        original_url: str,
    ) -> dict[str, object]:
        log_ref = self._log_uri(job_id, "persist_source")
        return {
            "schema_version": "1.0.0",
            "id": job_id,
            "source_id": source_id,
            "job_type": "capture",
            "idempotency_key": f"workbench-link-capture-{source_id}",
            "status": "completed",
            "attempt": 1,
            "max_attempts": 1,
            "lease": None,
            "progress": {
                "current": 1,
                "total": 1,
                "percent": 100,
                "message": "captured workbench link source without remote fetch",
            },
            "steps": [
                {
                    "name": "persist_source",
                    "status": "completed",
                    "attempt": 1,
                    "started_at": self._now,
                    "completed_at": self._now,
                    "progress": 100,
                    "input_refs": [source_uri],
                    "staged_output_refs": [],
                    "log_refs": [log_ref],
                    "error": None,
                }
            ],
            "error": None,
            "checkpoint": None,
            "staged_outputs": [],
            "published_outputs": [
                {
                    "kind": "source",
                    "uri": source_uri,
                    "object_id": source_id,
                    "published": True,
                }
            ],
            "log_refs": [log_ref],
            "created_at": self._now,
            "updated_at": self._now,
        }

    def _log_uri(self, job_id: str, name: str) -> str:
        return f"crp://{self._namespace_id}/logs/jobs/{job_id}/{name}.jsonl"

    def _selection_uri(self, source_id: str) -> str:
        return f"crp://{self._namespace_id}/library/selections/{source_id}.json"


class CaptureWorkbenchBookmarkCollection:
    """Capture a bookmark collection as one container Source plus link child Sources."""

    def __init__(
        self,
        *,
        source_registrar: SourceRegistrarPort,
        job_repository: JobRepositoryPort,
        namespace_id: str = "default",
        now: str = "2026-07-01T10:00:00+08:00",
    ) -> None:
        self._source_registrar = source_registrar
        self._job_repository = job_repository
        self._namespace_id = namespace_id
        self._now = now

    def execute(
        self,
        *,
        title: str,
        urls: tuple[str, ...],
    ) -> WorkbenchBookmarkCollectionIntakeResult:
        clean_title = title.strip() or "收藏夹"
        clean_urls = _clean_url_list(urls)
        child_sources = tuple(
            self._source_registrar.register(
                SourceSubmission(kind="link", title=f"{clean_title} · {index}", original_url=url)
            )
            for index, url in enumerate(clean_urls, start=1)
        )
        collection_source = self._source_registrar.register(
            SourceSubmission(kind="collection", title=clean_title, collection_urls=clean_urls)
        )
        source_id = _required_str(collection_source, "id")
        source_uri = _required_str(collection_source, "storage_uri")
        source_title = _required_str(collection_source, "title")
        source_type = _required_str(collection_source, "type")
        capture_mode = _required_str(collection_source, "capture_mode")
        media_type = _required_str(collection_source, "media_type")
        processing_state = _required_str(collection_source, "processing_state")
        content_hash = _required_str(collection_source, "content_hash")
        size_bytes = _required_int(collection_source, "size_bytes")
        child_source_ids = tuple(_required_str(source, "id") for source in child_sources)
        child_source_uris = tuple(_required_str(source, "storage_uri") for source in child_sources)
        job_id = f"job-capture-{source_id}"
        job = self._capture_job(
            job_id=job_id,
            source_id=source_id,
            source_uri=source_uri,
            child_source_ids=child_source_ids,
            child_source_uris=child_source_uris,
        )
        self._job_repository.save(job)

        trace_refs = (source_uri, *child_source_uris, self._log_uri(job_id, "persist_collection"))
        library_bridge_item = WorkbenchMinimalLibraryBridgeItem(
            item_id=f"library-source-{source_id}",
            item_kind="source_preview",
            title=source_title,
            source_id=source_id,
            source_uri=source_uri,
            media_type=media_type,
            processing_state=processing_state,
            capture_job_id=job_id,
            capture_job_status="completed",
            evidence_refs=trace_refs,
            selectable_evidence_refs=trace_refs,
            selection_id=f"library-selection-{source_id}",
            selection_state="available",
            selection_persistence_ref=self._selection_uri(source_id),
            memory_publication_state="not_started",
            boundary="collection_source_and_child_link_sources_only",
        )
        return WorkbenchBookmarkCollectionIntakeResult(
            status="captured",
            source_id=source_id,
            source_uri=source_uri,
            source_title=source_title,
            source_type=source_type,
            capture_mode=capture_mode,
            media_type=media_type,
            size_bytes=size_bytes,
            processing_state=processing_state,
            content_hash=content_hash,
            collection_item_count=len(clean_urls),
            child_source_ids=child_source_ids,
            child_source_uris=child_source_uris,
            original_urls=clean_urls,
            remote_fetch_state="not_performed",
            source_display_kind="bookmark_collection",
            capture_boundary="collection_metadata_and_child_links_only",
            remote_fetch_boundary="remote_fetch_not_performed_for_collection_items",
            library_selection_scope="collection_source_with_child_link_sources",
            job_id=job_id,
            job_type="capture",
            job_status="completed",
            job_progress_percent=100,
            job_progress_message="captured bookmark collection source and child link sources without remote fetch",
            step_names=("persist_child_link_sources", "persist_collection_source"),
            published_output_kinds=("source", "link_sources"),
            trace_refs=trace_refs,
            next_step="bookmark_collection_bridge_ready",
            library_bridge_item=library_bridge_item,
        )

    def _capture_job(
        self,
        *,
        job_id: str,
        source_id: str,
        source_uri: str,
        child_source_ids: tuple[str, ...],
        child_source_uris: tuple[str, ...],
    ) -> dict[str, object]:
        log_ref = self._log_uri(job_id, "persist_collection")
        return {
            "schema_version": "1.0.0",
            "id": job_id,
            "source_id": source_id,
            "job_type": "capture",
            "idempotency_key": f"workbench-bookmark-collection-capture-{source_id}",
            "status": "completed",
            "attempt": 1,
            "progress_percent": 100,
            "progress_message": "captured bookmark collection source and child link sources without remote fetch",
            "input_refs": [source_uri],
            "steps": [
                {
                    "name": "persist_child_link_sources",
                    "status": "completed",
                    "output_refs": list(child_source_uris),
                    "published": True,
                },
                {
                    "name": "persist_collection_source",
                    "status": "completed",
                    "output_refs": [source_uri],
                    "published": True,
                },
            ],
            "outputs": [
                {
                    "kind": "source",
                    "ref": source_uri,
                    "published": True,
                },
                {
                    "kind": "link_sources",
                    "refs": list(child_source_uris),
                    "source_ids": list(child_source_ids),
                    "published": True,
                },
            ],
            "log_refs": [log_ref],
            "created_at": self._now,
            "updated_at": self._now,
        }

    def _log_uri(self, job_id: str, name: str) -> str:
        return f"crp://{self._namespace_id}/logs/jobs/{job_id}/{name}.jsonl"

    def _selection_uri(self, source_id: str) -> str:
        return f"crp://{self._namespace_id}/library/selections/{source_id}.json"


class CaptureWorkbenchFileSource:
    """Capture platform-neutral local file metadata as Source and Asset reference evidence."""

    def __init__(
        self,
        *,
        source_registrar: SourceRegistrarPort,
        job_repository: JobRepositoryPort,
        namespace_id: str = "default",
        now: str = "2026-07-01T11:00:00+08:00",
    ) -> None:
        self._source_registrar = source_registrar
        self._job_repository = job_repository
        self._namespace_id = namespace_id
        self._now = now

    def execute(
        self,
        *,
        title: str,
        display_name: str,
        media_type: str,
        size_bytes: int,
        file_reference: str,
    ) -> WorkbenchFileSourceIntakeResult:
        source = self._source_registrar.register(
            SourceSubmission(
                kind="file",
                title=title.strip(),
                display_name=display_name,
                media_type=media_type,
                size_bytes=size_bytes,
                file_reference=file_reference,
            )
        )
        source_id = _required_str(source, "id")
        source_uri = _required_str(source, "storage_uri")
        source_title = _required_str(source, "title")
        source_type = _required_str(source, "type")
        capture_mode = _required_str(source, "capture_mode")
        captured_media_type = _required_str(source, "media_type")
        processing_state = _required_str(source, "processing_state")
        content_hash = _required_str(source, "content_hash")
        captured_size_bytes = _required_int(source, "size_bytes")
        metadata = _required_mapping(source, "metadata")
        file_display_name = _required_str(metadata, "display_name")
        captured_file_reference = _required_str(metadata, "file_reference")

        asset = self._asset_reference(
            source_id=source_id,
            content_hash=content_hash,
            size_bytes=captured_size_bytes,
            media_type=captured_media_type,
            display_name=file_display_name,
            file_reference=captured_file_reference,
        )
        asset_id = _required_str(asset, "id")
        asset_uri = _required_str(asset, "uri")
        asset_record_ref = self._asset_record_ref(asset_id)
        job_id = f"job-capture-{source_id}"
        job = self._capture_job(
            job_id=job_id,
            source_id=source_id,
            source_uri=source_uri,
            asset_id=asset_id,
            asset_record_ref=asset_record_ref,
        )
        self._job_repository.save(job)

        trace_refs = (
            source_uri,
            asset_uri,
            self._log_uri(job_id, "persist_source"),
            self._log_uri(job_id, "persist_asset_reference"),
        )
        library_bridge_item = WorkbenchMinimalLibraryBridgeItem(
            item_id=f"library-source-{source_id}",
            item_kind="source_preview",
            title=source_title,
            source_id=source_id,
            source_uri=source_uri,
            media_type=captured_media_type,
            processing_state=processing_state,
            capture_job_id=job_id,
            capture_job_status="completed",
            evidence_refs=trace_refs,
            selectable_evidence_refs=trace_refs,
            selection_id=f"library-selection-{source_id}",
            selection_state="available",
            selection_persistence_ref=self._selection_uri(source_id),
            memory_publication_state="not_started",
            boundary="source_job_asset_reference_selection_only",
        )

        return WorkbenchFileSourceIntakeResult(
            status="captured",
            source_id=source_id,
            source_uri=source_uri,
            source_title=source_title,
            source_type=source_type,
            capture_mode=capture_mode,
            media_type=captured_media_type,
            size_bytes=captured_size_bytes,
            processing_state=processing_state,
            content_hash=content_hash,
            file_display_name=file_display_name,
            file_reference=captured_file_reference,
            file_content_policy="metadata_only_no_content_read",
            path_policy="no_os_absolute_path_in_product_core",
            source_display_kind="file_reference",
            capture_boundary="file_metadata_only",
            parser_state="not_started",
            asset_id=asset_id,
            asset_uri=asset_uri,
            asset_record_ref=asset_record_ref,
            asset_uri_role="original_file_reference_uri",
            asset_record_ref_role="published_asset_record_ref",
            job_asset_output_ref=asset_record_ref,
            asset_storage_mode=_required_str(asset, "storage_mode"),
            asset_availability=_required_str(asset, "availability"),
            asset_availability_reason=_required_str(asset, "availability_reason"),
            asset_handoff_state="reference_record_created",
            library_selection_scope="source_job_asset_file_reference_only",
            library_selection_copy=(
                "select Source, Asset reference and capture Job evidence; no file content or parser output"
            ),
            no_content_read_boundary="file_bytes_not_read_or_copied",
            parser_boundary="parser_not_started_until_asset_verification",
            job_id=job_id,
            job_type="capture",
            job_status="completed",
            job_progress_percent=100,
            job_progress_message="captured workbench file metadata and asset reference without content read",
            step_names=("persist_source", "persist_asset_reference"),
            published_output_kinds=("source", "asset"),
            trace_refs=trace_refs,
            next_step="file_trace_display_hardening_ready",
            library_bridge_item=library_bridge_item,
            asset_record=dict(asset),
        )

    def _asset_reference(
        self,
        *,
        source_id: str,
        content_hash: str,
        size_bytes: int,
        media_type: str,
        display_name: str,
        file_reference: str,
    ) -> dict[str, object]:
        asset_id = source_id.replace("source-", "asset-", 1)
        return {
            "schema_version": "1.0.0",
            "id": asset_id,
            "source_id": source_id,
            "uri": f"crp-ref://{self._namespace_id}/assets/{asset_id}",
            "content_hash": content_hash,
            "size_bytes": size_bytes,
            "media_type": media_type,
            "storage_mode": "reference",
            "availability": "unknown",
            "availability_reason": "metadata_only_reference_not_verified",
            "created_at": self._now,
            "verified_at": None,
            "metadata": {
                "display_name": display_name,
                "file_reference": file_reference,
                "content_hash_basis": "file_reference_metadata_not_content",
                "file_content_read": False,
                "parser": "not_started",
            },
        }

    def _capture_job(
        self,
        *,
        job_id: str,
        source_id: str,
        source_uri: str,
        asset_id: str,
        asset_record_ref: str,
    ) -> dict[str, object]:
        source_log_ref = self._log_uri(job_id, "persist_source")
        asset_log_ref = self._log_uri(job_id, "persist_asset_reference")
        return {
            "schema_version": "1.0.0",
            "id": job_id,
            "source_id": source_id,
            "job_type": "capture",
            "idempotency_key": f"workbench-file-capture-{source_id}",
            "status": "completed",
            "attempt": 1,
            "max_attempts": 1,
            "lease": None,
            "progress": {
                "current": 2,
                "total": 2,
                "percent": 100,
                "message": "captured workbench file metadata and asset reference without content read",
            },
            "steps": [
                {
                    "name": "persist_source",
                    "status": "completed",
                    "attempt": 1,
                    "started_at": self._now,
                    "completed_at": self._now,
                    "progress": 100,
                    "input_refs": [source_uri],
                    "staged_output_refs": [],
                    "log_refs": [source_log_ref],
                    "error": None,
                },
                {
                    "name": "persist_asset_reference",
                    "status": "completed",
                    "attempt": 1,
                    "started_at": self._now,
                    "completed_at": self._now,
                    "progress": 100,
                    "input_refs": [source_uri],
                    "staged_output_refs": [],
                    "log_refs": [asset_log_ref],
                    "error": None,
                },
            ],
            "error": None,
            "checkpoint": None,
            "staged_outputs": [],
            "published_outputs": [
                {
                    "kind": "source",
                    "uri": source_uri,
                    "object_id": source_id,
                    "published": True,
                },
                {
                    "kind": "asset",
                    "uri": asset_record_ref,
                    "object_id": asset_id,
                    "published": True,
                },
            ],
            "log_refs": [source_log_ref, asset_log_ref],
            "created_at": self._now,
            "updated_at": self._now,
        }

    def _log_uri(self, job_id: str, name: str) -> str:
        return f"crp://{self._namespace_id}/logs/jobs/{job_id}/{name}.jsonl"

    def _selection_uri(self, source_id: str) -> str:
        return f"crp://{self._namespace_id}/library/selections/{source_id}.json"

    def _asset_record_ref(self, asset_id: str) -> str:
        return f"crp://{self._namespace_id}/assets/{asset_id}"


class CaptureWorkbenchImageSource:
    """Capture platform-neutral image metadata as Source and Asset reference evidence."""

    def __init__(
        self,
        *,
        source_registrar: SourceRegistrarPort,
        job_repository: JobRepositoryPort,
        namespace_id: str = "default",
        now: str = "2026-07-01T12:00:00+08:00",
    ) -> None:
        self._source_registrar = source_registrar
        self._job_repository = job_repository
        self._namespace_id = namespace_id
        self._now = now

    def execute(
        self,
        *,
        title: str,
        display_name: str,
        media_type: str,
        size_bytes: int,
        image_reference: str,
        width_px: int | None = None,
        height_px: int | None = None,
    ) -> WorkbenchImageSourceIntakeResult:
        source = self._source_registrar.register(
            SourceSubmission(
                kind="image",
                title=title.strip(),
                display_name=display_name,
                media_type=media_type,
                size_bytes=size_bytes,
                image_reference=image_reference,
                width_px=width_px,
                height_px=height_px,
            )
        )
        source_id = _required_str(source, "id")
        source_uri = _required_str(source, "storage_uri")
        source_title = _required_str(source, "title")
        source_type = _required_str(source, "type")
        capture_mode = _required_str(source, "capture_mode")
        captured_media_type = _required_str(source, "media_type")
        processing_state = _required_str(source, "processing_state")
        content_hash = _required_str(source, "content_hash")
        captured_size_bytes = _required_int(source, "size_bytes")
        metadata = _required_mapping(source, "metadata")
        image_display_name = _required_str(metadata, "display_name")
        captured_image_reference = _required_str(metadata, "image_reference")
        captured_width_px = _optional_metadata_int(metadata, "width_px")
        captured_height_px = _optional_metadata_int(metadata, "height_px")

        asset = self._asset_reference(
            source_id=source_id,
            content_hash=content_hash,
            size_bytes=captured_size_bytes,
            media_type=captured_media_type,
            display_name=image_display_name,
            image_reference=captured_image_reference,
            width_px=captured_width_px,
            height_px=captured_height_px,
        )
        asset_id = _required_str(asset, "id")
        asset_uri = _required_str(asset, "uri")
        asset_record_ref = self._asset_record_ref(asset_id)
        job_id = f"job-capture-{source_id}"
        job = self._capture_job(
            job_id=job_id,
            source_id=source_id,
            source_uri=source_uri,
            asset_id=asset_id,
            asset_record_ref=asset_record_ref,
        )
        self._job_repository.save(job)

        trace_refs = (
            source_uri,
            asset_uri,
            self._log_uri(job_id, "persist_source"),
            self._log_uri(job_id, "persist_image_asset_reference"),
        )
        library_bridge_item = WorkbenchMinimalLibraryBridgeItem(
            item_id=f"library-source-{source_id}",
            item_kind="source_preview",
            title=source_title,
            source_id=source_id,
            source_uri=source_uri,
            media_type=captured_media_type,
            processing_state=processing_state,
            capture_job_id=job_id,
            capture_job_status="completed",
            evidence_refs=trace_refs,
            selectable_evidence_refs=trace_refs,
            selection_id=f"library-selection-{source_id}",
            selection_state="available",
            selection_persistence_ref=self._selection_uri(source_id),
            memory_publication_state="not_started",
            boundary="source_job_image_asset_reference_selection_only",
        )

        return WorkbenchImageSourceIntakeResult(
            status="captured",
            source_id=source_id,
            source_uri=source_uri,
            source_title=source_title,
            source_type=source_type,
            capture_mode=capture_mode,
            media_type=captured_media_type,
            size_bytes=captured_size_bytes,
            processing_state=processing_state,
            content_hash=content_hash,
            image_display_name=image_display_name,
            image_reference=captured_image_reference,
            image_reference_role="platform_neutral_original_image_reference",
            width_px=captured_width_px,
            height_px=captured_height_px,
            binary_content_policy="metadata_only_no_binary_read",
            path_policy="no_os_absolute_path_in_product_core",
            source_display_kind="image_reference",
            capture_boundary="image_metadata_only",
            preview_policy="preview_metadata_only_no_thumbnail_generation",
            preview_boundary="thumbnail_not_generated_preview_metadata_only",
            thumbnail_state="not_generated",
            ocr_state="disabled",
            extractor_state="disabled",
            asset_id=asset_id,
            asset_uri=asset_uri,
            asset_record_ref=asset_record_ref,
            asset_uri_role="original_image_reference_uri",
            asset_record_ref_role="published_asset_record_ref",
            job_asset_output_ref=asset_record_ref,
            asset_storage_mode=_required_str(asset, "storage_mode"),
            asset_availability=_required_str(asset, "availability"),
            asset_availability_reason=_required_str(asset, "availability_reason"),
            asset_handoff_state="image_reference_record_created",
            asset_handoff_copy="image Asset reference created before OCR or visual extractor",
            library_selection_scope="source_job_asset_image_reference_only",
            library_selection_copy=(
                "select Source, image Asset reference and capture Job evidence only; no image bytes, thumbnail, OCR, visual extractor output or Memory"
            ),
            library_selection_boundary="source_job_image_asset_reference_only_no_derived_outputs",
            no_binary_read_boundary="image_bytes_not_read_or_copied",
            ocr_boundary="ocr_disabled_until_explicit_image_extractor_slice",
            extractor_boundary="extractor_disabled_until_asset_verification",
            derived_output_boundary="no_thumbnail_ocr_visual_features_or_memory_published",
            job_id=job_id,
            job_type="capture",
            job_status="completed",
            job_progress_percent=100,
            job_progress_message="captured workbench image metadata and asset reference without binary read",
            step_names=("persist_source", "persist_image_asset_reference"),
            published_output_kinds=("source", "asset"),
            trace_refs=trace_refs,
            next_step="audio_video_source_intake_planning_ready",
            library_bridge_item=library_bridge_item,
            asset_record=dict(asset),
        )

    def _asset_reference(
        self,
        *,
        source_id: str,
        content_hash: str,
        size_bytes: int,
        media_type: str,
        display_name: str,
        image_reference: str,
        width_px: int | None,
        height_px: int | None,
    ) -> dict[str, object]:
        asset_id = source_id.replace("source-", "asset-", 1)
        return {
            "schema_version": "1.0.0",
            "id": asset_id,
            "source_id": source_id,
            "uri": f"crp-ref://{self._namespace_id}/assets/{asset_id}",
            "content_hash": content_hash,
            "size_bytes": size_bytes,
            "media_type": media_type,
            "storage_mode": "reference",
            "availability": "unknown",
            "availability_reason": "metadata_only_image_reference_not_verified",
            "created_at": self._now,
            "verified_at": None,
            "metadata": {
                "display_name": display_name,
                "image_reference": image_reference,
                "content_hash_basis": "image_reference_metadata_not_binary",
                "image_bytes_read": False,
                "thumbnail_generated": False,
                "ocr": "disabled",
                "extractor": "disabled",
                "width_px": width_px,
                "height_px": height_px,
            },
        }

    def _capture_job(
        self,
        *,
        job_id: str,
        source_id: str,
        source_uri: str,
        asset_id: str,
        asset_record_ref: str,
    ) -> dict[str, object]:
        source_log_ref = self._log_uri(job_id, "persist_source")
        asset_log_ref = self._log_uri(job_id, "persist_image_asset_reference")
        return {
            "schema_version": "1.0.0",
            "id": job_id,
            "source_id": source_id,
            "job_type": "capture",
            "idempotency_key": f"workbench-image-capture-{source_id}",
            "status": "completed",
            "attempt": 1,
            "max_attempts": 1,
            "lease": None,
            "progress": {
                "current": 2,
                "total": 2,
                "percent": 100,
                "message": "captured workbench image metadata and asset reference without binary read",
            },
            "steps": [
                {
                    "name": "persist_source",
                    "status": "completed",
                    "attempt": 1,
                    "started_at": self._now,
                    "completed_at": self._now,
                    "progress": 100,
                    "input_refs": [source_uri],
                    "staged_output_refs": [],
                    "log_refs": [source_log_ref],
                    "error": None,
                },
                {
                    "name": "persist_image_asset_reference",
                    "status": "completed",
                    "attempt": 1,
                    "started_at": self._now,
                    "completed_at": self._now,
                    "progress": 100,
                    "input_refs": [source_uri],
                    "staged_output_refs": [],
                    "log_refs": [asset_log_ref],
                    "error": None,
                },
            ],
            "error": None,
            "checkpoint": None,
            "staged_outputs": [],
            "published_outputs": [
                {
                    "kind": "source",
                    "uri": source_uri,
                    "object_id": source_id,
                    "published": True,
                },
                {
                    "kind": "asset",
                    "uri": asset_record_ref,
                    "object_id": asset_id,
                    "published": True,
                },
            ],
            "log_refs": [source_log_ref, asset_log_ref],
            "created_at": self._now,
            "updated_at": self._now,
        }

    def _log_uri(self, job_id: str, name: str) -> str:
        return f"crp://{self._namespace_id}/logs/jobs/{job_id}/{name}.jsonl"

    def _selection_uri(self, source_id: str) -> str:
        return f"crp://{self._namespace_id}/library/selections/{source_id}.json"

    def _asset_record_ref(self, asset_id: str) -> str:
        return f"crp://{self._namespace_id}/assets/{asset_id}"


class CaptureWorkbenchAudioSource:
    """Capture platform-neutral audio metadata as Source and Asset reference evidence."""

    def __init__(
        self,
        *,
        source_registrar: SourceRegistrarPort,
        job_repository: JobRepositoryPort,
        namespace_id: str = "default",
        now: str = "2026-07-01T12:00:00+08:00",
    ) -> None:
        self._source_registrar = source_registrar
        self._job_repository = job_repository
        self._namespace_id = namespace_id
        self._now = now

    def execute(
        self,
        *,
        title: str,
        display_name: str,
        media_type: str,
        size_bytes: int,
        audio_reference: str,
        duration_ms: int | None = None,
    ) -> WorkbenchAudioSourceIntakeResult:
        source = self._source_registrar.register(
            SourceSubmission(
                kind="audio",
                title=title.strip(),
                display_name=display_name,
                media_type=media_type,
                size_bytes=size_bytes,
                audio_reference=audio_reference,
                duration_ms=duration_ms,
            )
        )
        source_id = _required_str(source, "id")
        source_uri = _required_str(source, "storage_uri")
        source_title = _required_str(source, "title")
        source_type = _required_str(source, "type")
        capture_mode = _required_str(source, "capture_mode")
        captured_media_type = _required_str(source, "media_type")
        captured_size_bytes = _required_int(source, "size_bytes")
        processing_state = "captured"
        content_hash = _required_str(source, "content_hash")
        metadata = _required_mapping(source, "metadata")
        audio_display_name = _required_str(metadata, "display_name")
        captured_audio_reference = _required_str(metadata, "audio_reference")
        captured_duration_ms = _optional_metadata_int(metadata, "duration_ms")

        asset = self._asset_reference(
            source_id=source_id,
            content_hash=content_hash,
            size_bytes=captured_size_bytes,
            media_type=captured_media_type,
            display_name=audio_display_name,
            audio_reference=captured_audio_reference,
            duration_ms=captured_duration_ms,
        )
        asset_id = _required_str(asset, "id")
        asset_uri = _required_str(asset, "uri")
        asset_record_ref = self._asset_record_ref(asset_id)
        job_id = f"job-capture-{source_id}"
        job = self._capture_job(
            job_id=job_id,
            source_id=source_id,
            source_uri=source_uri,
            asset_id=asset_id,
            asset_record_ref=asset_record_ref,
        )
        self._job_repository.save(job)

        trace_refs = (
            source_uri,
            asset_uri,
            self._log_uri(job_id, "persist_source"),
            self._log_uri(job_id, "persist_audio_asset_reference"),
        )
        library_bridge_item = WorkbenchMinimalLibraryBridgeItem(
            item_id=f"library-source-{source_id}",
            item_kind="source_preview",
            title=source_title,
            source_id=source_id,
            source_uri=source_uri,
            media_type=captured_media_type,
            processing_state=processing_state,
            capture_job_id=job_id,
            capture_job_status="completed",
            evidence_refs=trace_refs,
            selectable_evidence_refs=trace_refs,
            selection_id=f"library-selection-{source_id}",
            selection_state="available",
            selection_persistence_ref=self._selection_uri(source_id),
            memory_publication_state="not_started",
            boundary="source_job_audio_asset_reference_selection_only",
        )

        return WorkbenchAudioSourceIntakeResult(
            status="captured",
            source_id=source_id,
            source_uri=source_uri,
            source_title=source_title,
            source_type=source_type,
            capture_mode=capture_mode,
            media_type=captured_media_type,
            size_bytes=captured_size_bytes,
            processing_state=processing_state,
            content_hash=content_hash,
            source_uri_role="captured_audio_source_record_uri",
            audio_display_name=audio_display_name,
            audio_reference=captured_audio_reference,
            audio_reference_role="platform_neutral_original_audio_reference",
            duration_ms=captured_duration_ms,
            media_content_policy="metadata_only_no_media_read",
            path_policy="no_os_absolute_path_in_product_core",
            source_display_kind="audio_reference",
            capture_boundary="audio_metadata_only",
            transcription_state="disabled",
            transcription_policy="transcription_disabled_until_explicit_audio_slice",
            waveform_state="not_generated",
            waveform_policy="waveform_generation_disabled_until_media_processing_slice",
            remote_processing_state="not_performed",
            remote_processing_policy="remote_media_processing_not_performed",
            asset_id=asset_id,
            asset_uri=asset_uri,
            asset_record_ref=asset_record_ref,
            asset_uri_role="original_audio_reference_uri",
            asset_record_ref_role="published_asset_record_ref",
            job_source_output_ref=source_uri,
            job_source_output_ref_role="job_published_source_output_ref",
            job_asset_output_ref=asset_record_ref,
            job_asset_output_ref_role="job_published_audio_asset_output_ref",
            asset_storage_mode=_required_str(asset, "storage_mode"),
            asset_availability=_required_str(asset, "availability"),
            asset_availability_reason=_required_str(asset, "availability_reason"),
            asset_handoff_state="audio_reference_record_created",
            asset_handoff_copy="audio Asset reference created before transcription or waveform generation",
            library_selection_scope="source_job_asset_audio_reference_only",
            library_selection_copy=(
                "select Source, audio Asset reference and capture Job evidence only; no audio bytes, transcript, waveform, remote processing output or Memory"
            ),
            library_selection_boundary="source_job_audio_asset_reference_only_no_derived_outputs",
            library_selection_role_summary=(
                "selected evidence contains captured Source URI, original audio Asset reference URI, published Asset record ref and capture Job logs only"
            ),
            library_selection_excluded_outputs=(
                "audio_bytes",
                "transcript",
                "waveform",
                "remote_media_output",
                "memory_candidate",
                "memory_publication",
            ),
            memory_selection_policy="library_selection_cannot_publish_or_imply_memory",
            no_media_read_boundary="audio_bytes_not_read_or_copied",
            derived_output_state="not_created",
            derived_output_boundary="no_transcript_waveform_remote_processing_or_memory_published",
            parser_readiness_state="metadata_ready_parser_not_started",
            parser_readiness_boundary=(
                "audio_parser_not_started_until_asset_verification_and_explicit_parser_slice"
            ),
            parser_readiness_copy=(
                "audio parser readiness can be reviewed from Source, Asset reference and capture Job evidence only; "
                "no audio bytes, transcript, waveform, remote output or Memory are available to parser"
            ),
            parser_required_evidence_refs=trace_refs,
            parser_blocked_operations=(
                "audio_byte_read",
                "transcription",
                "waveform_generation",
                "remote_media_processing",
                "memory_candidate",
                "memory_publication",
            ),
            trace_display_sections=(
                "source_record",
                "original_audio_asset_reference",
                "published_asset_record",
                "capture_job_outputs",
                "excluded_derived_outputs",
                "library_selection_boundary",
                "parser_readiness_boundary",
            ),
            job_id=job_id,
            job_type="capture",
            job_status="completed",
            job_progress_percent=100,
            job_progress_message="captured workbench audio metadata and asset reference without media read",
            step_names=("persist_source", "persist_audio_asset_reference"),
            published_output_kinds=("source", "asset"),
            trace_refs=trace_refs,
            next_step="audio_parser_boundary_readiness_ready",
            library_bridge_item=library_bridge_item,
            asset_record=dict(asset),
        )

    def _asset_reference(
        self,
        *,
        source_id: str,
        content_hash: str,
        size_bytes: int,
        media_type: str,
        display_name: str,
        audio_reference: str,
        duration_ms: int | None,
    ) -> dict[str, object]:
        asset_id = source_id.replace("source-", "asset-", 1)
        return {
            "schema_version": "1.0.0",
            "id": asset_id,
            "source_id": source_id,
            "uri": f"crp-ref://{self._namespace_id}/assets/{asset_id}",
            "content_hash": content_hash,
            "size_bytes": size_bytes,
            "media_type": media_type,
            "storage_mode": "reference",
            "availability": "unknown",
            "availability_reason": "metadata_only_audio_reference_not_verified",
            "created_at": self._now,
            "verified_at": None,
            "metadata": {
                "display_name": display_name,
                "audio_reference": audio_reference,
                "content_hash_basis": "audio_reference_metadata_not_binary",
                "audio_bytes_read": False,
                "transcription": "disabled",
                "waveform_generated": False,
                "remote_processing": "not_performed",
                "duration_ms": duration_ms,
            },
        }

    def _capture_job(
        self,
        *,
        job_id: str,
        source_id: str,
        source_uri: str,
        asset_id: str,
        asset_record_ref: str,
    ) -> dict[str, object]:
        source_log_ref = self._log_uri(job_id, "persist_source")
        asset_log_ref = self._log_uri(job_id, "persist_audio_asset_reference")
        return {
            "schema_version": "1.0.0",
            "id": job_id,
            "source_id": source_id,
            "job_type": "capture",
            "idempotency_key": f"workbench-audio-capture-{source_id}",
            "status": "completed",
            "attempt": 1,
            "max_attempts": 1,
            "lease": None,
            "progress": {
                "current": 2,
                "total": 2,
                "percent": 100,
                "message": "captured workbench audio metadata and asset reference without media read",
            },
            "steps": [
                {
                    "name": "persist_source",
                    "status": "completed",
                    "attempt": 1,
                    "started_at": self._now,
                    "completed_at": self._now,
                    "progress": 100,
                    "input_refs": [source_uri],
                    "staged_output_refs": [],
                    "log_refs": [source_log_ref],
                    "error": None,
                },
                {
                    "name": "persist_audio_asset_reference",
                    "status": "completed",
                    "attempt": 1,
                    "started_at": self._now,
                    "completed_at": self._now,
                    "progress": 100,
                    "input_refs": [source_uri],
                    "staged_output_refs": [],
                    "log_refs": [asset_log_ref],
                    "error": None,
                },
            ],
            "error": None,
            "checkpoint": None,
            "staged_outputs": [],
            "published_outputs": [
                {
                    "kind": "source",
                    "uri": source_uri,
                    "object_id": source_id,
                    "published": True,
                },
                {
                    "kind": "asset",
                    "uri": asset_record_ref,
                    "object_id": asset_id,
                    "published": True,
                },
            ],
            "log_refs": [source_log_ref, asset_log_ref],
            "created_at": self._now,
            "updated_at": self._now,
        }

    def _log_uri(self, job_id: str, name: str) -> str:
        return f"crp://{self._namespace_id}/logs/jobs/{job_id}/{name}.jsonl"

    def _selection_uri(self, source_id: str) -> str:
        return f"crp://{self._namespace_id}/library/selections/{source_id}.json"

    def _asset_record_ref(self, asset_id: str) -> str:
        return f"crp://{self._namespace_id}/assets/{asset_id}"


class CaptureWorkbenchVideoSource:
    """Capture platform-neutral video metadata as Source and Asset reference evidence."""

    def __init__(
        self,
        *,
        source_registrar: SourceRegistrarPort,
        job_repository: JobRepositoryPort,
        namespace_id: str = "default",
        now: str = "2026-07-01T12:30:00+08:00",
    ) -> None:
        self._source_registrar = source_registrar
        self._job_repository = job_repository
        self._namespace_id = namespace_id
        self._now = now

    def execute(
        self,
        *,
        title: str,
        display_name: str,
        media_type: str,
        size_bytes: int,
        video_reference: str,
        duration_ms: int | None = None,
        width_px: int | None = None,
        height_px: int | None = None,
    ) -> WorkbenchVideoSourceIntakeResult:
        source = self._source_registrar.register(
            SourceSubmission(
                kind="video",
                title=title.strip(),
                display_name=display_name,
                media_type=media_type,
                size_bytes=size_bytes,
                video_reference=video_reference,
                duration_ms=duration_ms,
                width_px=width_px,
                height_px=height_px,
            )
        )
        source_id = _required_str(source, "id")
        source_uri = _required_str(source, "storage_uri")
        source_title = _required_str(source, "title")
        source_type = _required_str(source, "type")
        capture_mode = _required_str(source, "capture_mode")
        captured_media_type = _required_str(source, "media_type")
        captured_size_bytes = _required_int(source, "size_bytes")
        processing_state = "captured"
        content_hash = _required_str(source, "content_hash")
        metadata = _required_mapping(source, "metadata")
        video_display_name = _required_str(metadata, "display_name")
        captured_video_reference = _required_str(metadata, "video_reference")
        captured_duration_ms = _optional_metadata_int(metadata, "duration_ms")
        captured_width_px = _optional_metadata_int(metadata, "width_px")
        captured_height_px = _optional_metadata_int(metadata, "height_px")

        asset = self._asset_reference(
            source_id=source_id,
            content_hash=content_hash,
            size_bytes=captured_size_bytes,
            media_type=captured_media_type,
            display_name=video_display_name,
            video_reference=captured_video_reference,
            duration_ms=captured_duration_ms,
            width_px=captured_width_px,
            height_px=captured_height_px,
        )
        asset_id = _required_str(asset, "id")
        asset_uri = _required_str(asset, "uri")
        asset_record_ref = self._asset_record_ref(asset_id)
        job_id = f"job-capture-{source_id}"
        job = self._capture_job(
            job_id=job_id,
            source_id=source_id,
            source_uri=source_uri,
            asset_id=asset_id,
            asset_record_ref=asset_record_ref,
        )
        self._job_repository.save(job)

        trace_refs = (
            source_uri,
            asset_uri,
            self._log_uri(job_id, "persist_source"),
            self._log_uri(job_id, "persist_video_asset_reference"),
        )
        library_bridge_item = WorkbenchMinimalLibraryBridgeItem(
            item_id=f"library-source-{source_id}",
            item_kind="source_preview",
            title=source_title,
            source_id=source_id,
            source_uri=source_uri,
            media_type=captured_media_type,
            processing_state=processing_state,
            capture_job_id=job_id,
            capture_job_status="completed",
            evidence_refs=trace_refs,
            selectable_evidence_refs=trace_refs,
            selection_id=f"library-selection-{source_id}",
            selection_state="available",
            selection_persistence_ref=self._selection_uri(source_id),
            memory_publication_state="not_started",
            boundary="source_job_video_asset_reference_selection_only",
        )

        return WorkbenchVideoSourceIntakeResult(
            status="captured",
            source_id=source_id,
            source_uri=source_uri,
            source_title=source_title,
            source_type=source_type,
            capture_mode=capture_mode,
            media_type=captured_media_type,
            size_bytes=captured_size_bytes,
            processing_state=processing_state,
            content_hash=content_hash,
            source_uri_role="captured_video_source_record_uri",
            video_display_name=video_display_name,
            video_reference=captured_video_reference,
            video_reference_role="platform_neutral_original_video_reference",
            duration_ms=captured_duration_ms,
            width_px=captured_width_px,
            height_px=captured_height_px,
            media_content_policy="metadata_only_no_media_read",
            path_policy="no_os_absolute_path_in_product_core",
            source_display_kind="video_reference",
            capture_boundary="video_metadata_only",
            frame_extraction_state="disabled",
            frame_extraction_policy="frame_extraction_disabled_until_explicit_video_slice",
            audio_track_extraction_state="disabled",
            audio_track_extraction_policy="audio_track_extraction_disabled_until_explicit_video_slice",
            thumbnail_state="not_generated",
            thumbnail_policy="thumbnail_generation_disabled_until_media_processing_slice",
            remote_processing_state="not_performed",
            remote_processing_policy="remote_media_processing_not_performed",
            asset_id=asset_id,
            asset_uri=asset_uri,
            asset_record_ref=asset_record_ref,
            asset_uri_role="original_video_reference_uri",
            asset_record_ref_role="published_asset_record_ref",
            job_source_output_ref=source_uri,
            job_source_output_ref_role="job_published_source_output_ref",
            job_asset_output_ref=asset_record_ref,
            job_asset_output_ref_role="job_published_video_asset_output_ref",
            asset_storage_mode=_required_str(asset, "storage_mode"),
            asset_availability=_required_str(asset, "availability"),
            asset_availability_reason=_required_str(asset, "availability_reason"),
            asset_handoff_state="video_reference_record_created",
            asset_handoff_copy="video Asset reference created before frame extraction, audio-track extraction or thumbnail generation",
            library_selection_scope="source_job_asset_video_reference_only",
            library_selection_copy=(
                "select Source, video Asset reference and capture Job evidence only; no video bytes, frames, audio track, thumbnail, remote processing output or Memory"
            ),
            library_selection_boundary="source_job_video_asset_reference_only_no_derived_outputs",
            library_selection_role_summary=(
                "selected evidence contains captured Source URI, original video Asset reference URI, published Asset record ref and capture Job logs only"
            ),
            library_selection_excluded_outputs=(
                "video_bytes",
                "extracted_frames",
                "audio_track",
                "thumbnail",
                "remote_media_output",
                "memory_candidate",
                "memory_publication",
            ),
            memory_selection_policy="library_selection_cannot_publish_or_imply_memory",
            no_media_read_boundary="video_bytes_not_read_or_copied",
            derived_output_state="not_created",
            derived_output_boundary="no_frames_audio_track_thumbnail_remote_processing_or_memory_published",
            parser_readiness_state="metadata_ready_parser_not_started",
            parser_readiness_boundary=(
                "video_parser_not_started_until_asset_verification_and_explicit_parser_slice"
            ),
            parser_readiness_copy=(
                "video parser readiness can be reviewed from Source, Asset reference and capture Job evidence only; "
                "no video bytes, extracted frames, audio track, thumbnail, remote output or Memory are available to parser"
            ),
            parser_required_evidence_refs=trace_refs,
            parser_blocked_operations=(
                "video_byte_read",
                "frame_extraction",
                "audio_track_extraction",
                "thumbnail_generation",
                "remote_media_processing",
                "memory_candidate",
                "memory_publication",
            ),
            trace_display_sections=(
                "source_record",
                "original_video_asset_reference",
                "published_asset_record",
                "capture_job_outputs",
                "excluded_derived_outputs",
                "library_selection_boundary",
                "parser_readiness_boundary",
            ),
            trace_role_summary=(
                "video trace separates captured Source URI, platform-neutral original video Asset reference URI, published Asset record ref, Job Source output and Job Asset output"
            ),
            job_output_role_summary=(
                "capture Job publishes the Source record and the video Asset record ref only; it does not publish video bytes, extracted frames, audio track, thumbnail, remote media output or Memory"
            ),
            library_selection_evidence_roles=(
                "captured_source_uri",
                "original_video_asset_reference_uri",
                "published_asset_record_ref",
                "job_persist_source_log",
                "job_persist_video_asset_reference_log",
            ),
            job_id=job_id,
            job_type="capture",
            job_status="completed",
            job_progress_percent=100,
            job_progress_message="captured workbench video metadata and asset reference without media read",
            step_names=("persist_source", "persist_video_asset_reference"),
            published_output_kinds=("source", "asset"),
            trace_refs=trace_refs,
            next_step="video_parser_boundary_readiness_ready",
            library_bridge_item=library_bridge_item,
            asset_record=dict(asset),
        )

    def _asset_reference(
        self,
        *,
        source_id: str,
        content_hash: str,
        size_bytes: int,
        media_type: str,
        display_name: str,
        video_reference: str,
        duration_ms: int | None,
        width_px: int | None,
        height_px: int | None,
    ) -> dict[str, object]:
        asset_id = source_id.replace("source-", "asset-", 1)
        return {
            "schema_version": "1.0.0",
            "id": asset_id,
            "source_id": source_id,
            "uri": f"crp-ref://{self._namespace_id}/assets/{asset_id}",
            "content_hash": content_hash,
            "size_bytes": size_bytes,
            "media_type": media_type,
            "storage_mode": "reference",
            "availability": "unknown",
            "availability_reason": "metadata_only_video_reference_not_verified",
            "created_at": self._now,
            "verified_at": None,
            "metadata": {
                "display_name": display_name,
                "video_reference": video_reference,
                "content_hash_basis": "video_reference_metadata_not_binary",
                "video_bytes_read": False,
                "frame_extraction": "disabled",
                "audio_track_extraction": "disabled",
                "thumbnail_generated": False,
                "remote_processing": "not_performed",
                "duration_ms": duration_ms,
                "width_px": width_px,
                "height_px": height_px,
            },
        }

    def _capture_job(
        self,
        *,
        job_id: str,
        source_id: str,
        source_uri: str,
        asset_id: str,
        asset_record_ref: str,
    ) -> dict[str, object]:
        source_log_ref = self._log_uri(job_id, "persist_source")
        asset_log_ref = self._log_uri(job_id, "persist_video_asset_reference")
        return {
            "schema_version": "1.0.0",
            "id": job_id,
            "source_id": source_id,
            "job_type": "capture",
            "idempotency_key": f"workbench-video-capture-{source_id}",
            "status": "completed",
            "attempt": 1,
            "max_attempts": 1,
            "lease": None,
            "progress": {
                "current": 2,
                "total": 2,
                "percent": 100,
                "message": "captured workbench video metadata and asset reference without media read",
            },
            "steps": [
                {
                    "name": "persist_source",
                    "status": "completed",
                    "attempt": 1,
                    "started_at": self._now,
                    "completed_at": self._now,
                    "progress": 100,
                    "input_refs": [source_uri],
                    "staged_output_refs": [],
                    "log_refs": [source_log_ref],
                    "error": None,
                },
                {
                    "name": "persist_video_asset_reference",
                    "status": "completed",
                    "attempt": 1,
                    "started_at": self._now,
                    "completed_at": self._now,
                    "progress": 100,
                    "input_refs": [source_uri],
                    "staged_output_refs": [],
                    "log_refs": [asset_log_ref],
                    "error": None,
                },
            ],
            "error": None,
            "checkpoint": None,
            "staged_outputs": [],
            "published_outputs": [
                {"kind": "source", "uri": source_uri, "object_id": source_id, "published": True},
                {"kind": "asset", "uri": asset_record_ref, "object_id": asset_id, "published": True},
            ],
            "log_refs": [source_log_ref, asset_log_ref],
            "created_at": self._now,
            "updated_at": self._now,
        }

    def _log_uri(self, job_id: str, name: str) -> str:
        return f"crp://{self._namespace_id}/logs/jobs/{job_id}/{name}.jsonl"

    def _selection_uri(self, source_id: str) -> str:
        return f"crp://{self._namespace_id}/library/selections/{source_id}.json"

    def _asset_record_ref(self, asset_id: str) -> str:
        return f"crp://{self._namespace_id}/assets/{asset_id}"


def serialize_workbench_text_source_intake(
    result: WorkbenchTextSourceIntakeResult,
) -> dict[str, object]:
    return {
        "status": result.status,
        "source_id": result.source_id,
        "source_uri": result.source_uri,
        "source_title": result.source_title,
        "source_type": result.source_type,
        "capture_mode": result.capture_mode,
        "media_type": result.media_type,
        "size_bytes": result.size_bytes,
        "processing_state": result.processing_state,
        "content_hash": result.content_hash,
        "job_id": result.job_id,
        "job_type": result.job_type,
        "job_status": result.job_status,
        "job_progress_percent": result.job_progress_percent,
        "job_progress_message": result.job_progress_message,
        "step_names": list(result.step_names),
        "published_output_kinds": list(result.published_output_kinds),
        "trace_refs": list(result.trace_refs),
        "next_step": result.next_step,
        "library_bridge_item": serialize_workbench_minimal_library_bridge_item(
            result.library_bridge_item
        ),
        "intake_intent": result.intake_intent,
        "intake_intent_label": result.intake_intent_label,
        "intake_route": result.intake_route,
        "intake_feedback": result.intake_feedback,
        "structured_output_plan": list(result.structured_output_plan),
        "memory_layer_update_plan": list(result.memory_layer_update_plan),
        "suggested_next_actions": list(result.suggested_next_actions),
    }


def serialize_workbench_link_source_intake(
    result: WorkbenchLinkSourceIntakeResult,
) -> dict[str, object]:
    return {
        "status": result.status,
        "source_id": result.source_id,
        "source_uri": result.source_uri,
        "source_title": result.source_title,
        "source_type": result.source_type,
        "capture_mode": result.capture_mode,
        "media_type": result.media_type,
        "size_bytes": result.size_bytes,
        "processing_state": result.processing_state,
        "content_hash": result.content_hash,
        "original_url": result.original_url,
        "remote_fetch_state": result.remote_fetch_state,
        "source_display_kind": result.source_display_kind,
        "capture_boundary": result.capture_boundary,
        "remote_fetch_boundary": result.remote_fetch_boundary,
        "library_selection_scope": result.library_selection_scope,
        "job_id": result.job_id,
        "job_type": result.job_type,
        "job_status": result.job_status,
        "job_progress_percent": result.job_progress_percent,
        "job_progress_message": result.job_progress_message,
        "step_names": list(result.step_names),
        "published_output_kinds": list(result.published_output_kinds),
        "trace_refs": list(result.trace_refs),
        "next_step": result.next_step,
        "library_bridge_item": serialize_workbench_minimal_library_bridge_item(
            result.library_bridge_item
        ),
    }


def serialize_workbench_bookmark_collection_intake(
    result: WorkbenchBookmarkCollectionIntakeResult,
) -> dict[str, object]:
    return {
        "status": result.status,
        "source_id": result.source_id,
        "source_uri": result.source_uri,
        "source_title": result.source_title,
        "source_type": result.source_type,
        "capture_mode": result.capture_mode,
        "media_type": result.media_type,
        "size_bytes": result.size_bytes,
        "processing_state": result.processing_state,
        "content_hash": result.content_hash,
        "collection_item_count": result.collection_item_count,
        "child_source_ids": list(result.child_source_ids),
        "child_source_uris": list(result.child_source_uris),
        "original_urls": list(result.original_urls),
        "remote_fetch_state": result.remote_fetch_state,
        "source_display_kind": result.source_display_kind,
        "capture_boundary": result.capture_boundary,
        "remote_fetch_boundary": result.remote_fetch_boundary,
        "library_selection_scope": result.library_selection_scope,
        "job_id": result.job_id,
        "job_type": result.job_type,
        "job_status": result.job_status,
        "job_progress_percent": result.job_progress_percent,
        "job_progress_message": result.job_progress_message,
        "step_names": list(result.step_names),
        "published_output_kinds": list(result.published_output_kinds),
        "trace_refs": list(result.trace_refs),
        "next_step": result.next_step,
        "library_bridge_item": serialize_workbench_minimal_library_bridge_item(
            result.library_bridge_item
        ),
    }


def serialize_workbench_file_source_intake(
    result: WorkbenchFileSourceIntakeResult,
) -> dict[str, object]:
    return {
        "status": result.status,
        "source_id": result.source_id,
        "source_uri": result.source_uri,
        "source_title": result.source_title,
        "source_type": result.source_type,
        "capture_mode": result.capture_mode,
        "media_type": result.media_type,
        "size_bytes": result.size_bytes,
        "processing_state": result.processing_state,
        "content_hash": result.content_hash,
        "file_display_name": result.file_display_name,
        "file_reference": result.file_reference,
        "file_content_policy": result.file_content_policy,
        "path_policy": result.path_policy,
        "source_display_kind": result.source_display_kind,
        "capture_boundary": result.capture_boundary,
        "parser_state": result.parser_state,
        "asset_id": result.asset_id,
        "asset_uri": result.asset_uri,
        "asset_record_ref": result.asset_record_ref,
        "asset_uri_role": result.asset_uri_role,
        "asset_record_ref_role": result.asset_record_ref_role,
        "job_asset_output_ref": result.job_asset_output_ref,
        "asset_storage_mode": result.asset_storage_mode,
        "asset_availability": result.asset_availability,
        "asset_availability_reason": result.asset_availability_reason,
        "asset_handoff_state": result.asset_handoff_state,
        "library_selection_scope": result.library_selection_scope,
        "library_selection_copy": result.library_selection_copy,
        "no_content_read_boundary": result.no_content_read_boundary,
        "parser_boundary": result.parser_boundary,
        "job_id": result.job_id,
        "job_type": result.job_type,
        "job_status": result.job_status,
        "job_progress_percent": result.job_progress_percent,
        "job_progress_message": result.job_progress_message,
        "step_names": list(result.step_names),
        "published_output_kinds": list(result.published_output_kinds),
        "trace_refs": list(result.trace_refs),
        "next_step": result.next_step,
        "library_bridge_item": serialize_workbench_minimal_library_bridge_item(
            result.library_bridge_item
        ),
        "asset_record": dict(result.asset_record),
    }


def serialize_workbench_image_source_intake(
    result: WorkbenchImageSourceIntakeResult,
) -> dict[str, object]:
    return {
        "status": result.status,
        "source_id": result.source_id,
        "source_uri": result.source_uri,
        "source_title": result.source_title,
        "source_type": result.source_type,
        "capture_mode": result.capture_mode,
        "media_type": result.media_type,
        "size_bytes": result.size_bytes,
        "processing_state": result.processing_state,
        "content_hash": result.content_hash,
        "image_display_name": result.image_display_name,
        "image_reference": result.image_reference,
        "image_reference_role": result.image_reference_role,
        "width_px": result.width_px,
        "height_px": result.height_px,
        "binary_content_policy": result.binary_content_policy,
        "path_policy": result.path_policy,
        "source_display_kind": result.source_display_kind,
        "capture_boundary": result.capture_boundary,
        "preview_policy": result.preview_policy,
        "preview_boundary": result.preview_boundary,
        "thumbnail_state": result.thumbnail_state,
        "ocr_state": result.ocr_state,
        "extractor_state": result.extractor_state,
        "asset_id": result.asset_id,
        "asset_uri": result.asset_uri,
        "asset_record_ref": result.asset_record_ref,
        "asset_uri_role": result.asset_uri_role,
        "asset_record_ref_role": result.asset_record_ref_role,
        "job_asset_output_ref": result.job_asset_output_ref,
        "asset_storage_mode": result.asset_storage_mode,
        "asset_availability": result.asset_availability,
        "asset_availability_reason": result.asset_availability_reason,
        "asset_handoff_state": result.asset_handoff_state,
        "asset_handoff_copy": result.asset_handoff_copy,
        "library_selection_scope": result.library_selection_scope,
        "library_selection_copy": result.library_selection_copy,
        "library_selection_boundary": result.library_selection_boundary,
        "no_binary_read_boundary": result.no_binary_read_boundary,
        "ocr_boundary": result.ocr_boundary,
        "extractor_boundary": result.extractor_boundary,
        "derived_output_boundary": result.derived_output_boundary,
        "job_id": result.job_id,
        "job_type": result.job_type,
        "job_status": result.job_status,
        "job_progress_percent": result.job_progress_percent,
        "job_progress_message": result.job_progress_message,
        "step_names": list(result.step_names),
        "published_output_kinds": list(result.published_output_kinds),
        "trace_refs": list(result.trace_refs),
        "next_step": result.next_step,
        "library_bridge_item": serialize_workbench_minimal_library_bridge_item(
            result.library_bridge_item
        ),
        "asset_record": dict(result.asset_record),
    }


def serialize_workbench_audio_source_intake(
    result: WorkbenchAudioSourceIntakeResult,
) -> dict[str, object]:
    return {
        "status": result.status,
        "source_id": result.source_id,
        "source_uri": result.source_uri,
        "source_title": result.source_title,
        "source_type": result.source_type,
        "capture_mode": result.capture_mode,
        "media_type": result.media_type,
        "size_bytes": result.size_bytes,
        "processing_state": result.processing_state,
        "content_hash": result.content_hash,
        "source_uri_role": result.source_uri_role,
        "audio_display_name": result.audio_display_name,
        "audio_reference": result.audio_reference,
        "audio_reference_role": result.audio_reference_role,
        "duration_ms": result.duration_ms,
        "media_content_policy": result.media_content_policy,
        "path_policy": result.path_policy,
        "source_display_kind": result.source_display_kind,
        "capture_boundary": result.capture_boundary,
        "transcription_state": result.transcription_state,
        "transcription_policy": result.transcription_policy,
        "waveform_state": result.waveform_state,
        "waveform_policy": result.waveform_policy,
        "remote_processing_state": result.remote_processing_state,
        "remote_processing_policy": result.remote_processing_policy,
        "asset_id": result.asset_id,
        "asset_uri": result.asset_uri,
        "asset_record_ref": result.asset_record_ref,
        "asset_uri_role": result.asset_uri_role,
        "asset_record_ref_role": result.asset_record_ref_role,
        "job_source_output_ref": result.job_source_output_ref,
        "job_source_output_ref_role": result.job_source_output_ref_role,
        "job_asset_output_ref": result.job_asset_output_ref,
        "job_asset_output_ref_role": result.job_asset_output_ref_role,
        "asset_storage_mode": result.asset_storage_mode,
        "asset_availability": result.asset_availability,
        "asset_availability_reason": result.asset_availability_reason,
        "asset_handoff_state": result.asset_handoff_state,
        "asset_handoff_copy": result.asset_handoff_copy,
        "library_selection_scope": result.library_selection_scope,
        "library_selection_copy": result.library_selection_copy,
        "library_selection_boundary": result.library_selection_boundary,
        "library_selection_role_summary": result.library_selection_role_summary,
        "library_selection_excluded_outputs": list(result.library_selection_excluded_outputs),
        "memory_selection_policy": result.memory_selection_policy,
        "no_media_read_boundary": result.no_media_read_boundary,
        "derived_output_state": result.derived_output_state,
        "derived_output_boundary": result.derived_output_boundary,
        "parser_readiness_state": result.parser_readiness_state,
        "parser_readiness_boundary": result.parser_readiness_boundary,
        "parser_readiness_copy": result.parser_readiness_copy,
        "parser_required_evidence_refs": list(result.parser_required_evidence_refs),
        "parser_blocked_operations": list(result.parser_blocked_operations),
        "trace_display_sections": list(result.trace_display_sections),
        "job_id": result.job_id,
        "job_type": result.job_type,
        "job_status": result.job_status,
        "job_progress_percent": result.job_progress_percent,
        "job_progress_message": result.job_progress_message,
        "step_names": list(result.step_names),
        "published_output_kinds": list(result.published_output_kinds),
        "trace_refs": list(result.trace_refs),
        "next_step": result.next_step,
        "library_bridge_item": serialize_workbench_minimal_library_bridge_item(
            result.library_bridge_item
        ),
        "asset_record": dict(result.asset_record),
    }


def serialize_workbench_video_source_intake(
    result: WorkbenchVideoSourceIntakeResult,
) -> dict[str, object]:
    return {
        "status": result.status,
        "source_id": result.source_id,
        "source_uri": result.source_uri,
        "source_title": result.source_title,
        "source_type": result.source_type,
        "capture_mode": result.capture_mode,
        "media_type": result.media_type,
        "size_bytes": result.size_bytes,
        "processing_state": result.processing_state,
        "content_hash": result.content_hash,
        "source_uri_role": result.source_uri_role,
        "video_display_name": result.video_display_name,
        "video_reference": result.video_reference,
        "video_reference_role": result.video_reference_role,
        "duration_ms": result.duration_ms,
        "width_px": result.width_px,
        "height_px": result.height_px,
        "media_content_policy": result.media_content_policy,
        "path_policy": result.path_policy,
        "source_display_kind": result.source_display_kind,
        "capture_boundary": result.capture_boundary,
        "frame_extraction_state": result.frame_extraction_state,
        "frame_extraction_policy": result.frame_extraction_policy,
        "audio_track_extraction_state": result.audio_track_extraction_state,
        "audio_track_extraction_policy": result.audio_track_extraction_policy,
        "thumbnail_state": result.thumbnail_state,
        "thumbnail_policy": result.thumbnail_policy,
        "remote_processing_state": result.remote_processing_state,
        "remote_processing_policy": result.remote_processing_policy,
        "asset_id": result.asset_id,
        "asset_uri": result.asset_uri,
        "asset_record_ref": result.asset_record_ref,
        "asset_uri_role": result.asset_uri_role,
        "asset_record_ref_role": result.asset_record_ref_role,
        "job_source_output_ref": result.job_source_output_ref,
        "job_source_output_ref_role": result.job_source_output_ref_role,
        "job_asset_output_ref": result.job_asset_output_ref,
        "job_asset_output_ref_role": result.job_asset_output_ref_role,
        "asset_storage_mode": result.asset_storage_mode,
        "asset_availability": result.asset_availability,
        "asset_availability_reason": result.asset_availability_reason,
        "asset_handoff_state": result.asset_handoff_state,
        "asset_handoff_copy": result.asset_handoff_copy,
        "library_selection_scope": result.library_selection_scope,
        "library_selection_copy": result.library_selection_copy,
        "library_selection_boundary": result.library_selection_boundary,
        "library_selection_role_summary": result.library_selection_role_summary,
        "library_selection_excluded_outputs": list(result.library_selection_excluded_outputs),
        "memory_selection_policy": result.memory_selection_policy,
        "no_media_read_boundary": result.no_media_read_boundary,
        "derived_output_state": result.derived_output_state,
        "derived_output_boundary": result.derived_output_boundary,
        "parser_readiness_state": result.parser_readiness_state,
        "parser_readiness_boundary": result.parser_readiness_boundary,
        "parser_readiness_copy": result.parser_readiness_copy,
        "parser_required_evidence_refs": list(result.parser_required_evidence_refs),
        "parser_blocked_operations": list(result.parser_blocked_operations),
        "trace_display_sections": list(result.trace_display_sections),
        "trace_role_summary": result.trace_role_summary,
        "job_output_role_summary": result.job_output_role_summary,
        "library_selection_evidence_roles": list(result.library_selection_evidence_roles),
        "job_id": result.job_id,
        "job_type": result.job_type,
        "job_status": result.job_status,
        "job_progress_percent": result.job_progress_percent,
        "job_progress_message": result.job_progress_message,
        "step_names": list(result.step_names),
        "published_output_kinds": list(result.published_output_kinds),
        "trace_refs": list(result.trace_refs),
        "next_step": result.next_step,
        "library_bridge_item": serialize_workbench_minimal_library_bridge_item(
            result.library_bridge_item
        ),
        "asset_record": dict(result.asset_record),
    }


def serialize_workbench_minimal_library_bridge_item(
    item: WorkbenchMinimalLibraryBridgeItem,
) -> dict[str, object]:
    return {
        "item_id": item.item_id,
        "item_kind": item.item_kind,
        "title": item.title,
        "source_id": item.source_id,
        "source_uri": item.source_uri,
        "media_type": item.media_type,
        "processing_state": item.processing_state,
        "capture_job_id": item.capture_job_id,
        "capture_job_status": item.capture_job_status,
        "evidence_refs": list(item.evidence_refs),
        "selectable_evidence_refs": list(item.selectable_evidence_refs),
        "selection_id": item.selection_id,
        "selection_state": item.selection_state,
        "selection_persistence_ref": item.selection_persistence_ref,
        "memory_publication_state": item.memory_publication_state,
        "boundary": item.boundary,
    }


def _required_str(source: Mapping[str, object], key: str) -> str:
    value = source.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"source requires {key}")
    return value


def _required_int(source: Mapping[str, object], key: str) -> int:
    value = source.get(key)
    if not isinstance(value, int):
        raise ValueError(f"source requires {key}")
    return value


def _clean_url_list(urls: tuple[str, ...]) -> tuple[str, ...]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for url in urls:
        if not isinstance(url, str):
            raise ValueError("bookmark collection urls must be strings")
        clean_url = url.strip()
        if not clean_url:
            continue
        if clean_url not in seen:
            cleaned.append(clean_url)
            seen.add(clean_url)
    if not cleaned:
        raise ValueError("bookmark collection intake requires at least one url")
    return tuple(cleaned)


def _required_mapping(source: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = source.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"source requires {key}")
    return value


def _classify_direct_text_intent(content: str) -> dict[str, object]:
    text = content.strip().lower()
    if _contains_any(text, ("复盘", "回顾", "总结", "反思", "review")):
        return _direct_text_intent(
            intent="review",
            label="复盘",
            route="review_material",
            feedback="已识别为复盘材料，后续适合抽取事件、判断、反复模式和下一步。",
            structured_output_plan=("事件摘要", "判断依据", "反复模式", "下一步入口"),
            memory_layer_update_plan=("atom", "scenario", "series_overview"),
            suggested_next_actions=("生成复盘摘要", "创建待审场景记忆", "关联到项目或系列"),
        )
    if _contains_any(text, ("下一步", "推进", "任务", "计划", "实现", "修复", "完成", "todo", "deadline")):
        return _direct_text_intent(
            intent="project_progress",
            label="项目推进",
            route="project_progress_material",
            feedback="已识别为项目推进材料，后续适合拆出任务、阻塞、责任对象和项目技能更新点。",
            structured_output_plan=("任务摘要", "当前状态", "阻塞事项", "验收标准"),
            memory_layer_update_plan=("scenario", "project_skill"),
            suggested_next_actions=("生成项目推进条目", "创建项目技能候选", "关联到当前项目"),
        )
    if _contains_any(text, ("?", "？", "为什么", "怎么", "如何", "能不能", "是否", "什么", "哪", "吗")):
        return _direct_text_intent(
            intent="question",
            label="问答",
            route="qa_material",
            feedback="已识别为问答材料，后续适合进入召回、回答生成和回答文档草稿。",
            structured_output_plan=("问题", "上下文", "待召回证据", "回答草稿"),
            memory_layer_update_plan=("atom", "scenario"),
            suggested_next_actions=("准备问答证据", "生成本地回答", "生成回答文档或待审记忆候选"),
        )
    if _contains_any(text, ("知识", "资料", "概念", "方法", "学习", "补充", "原则", "定义")):
        return _direct_text_intent(
            intent="knowledge_supplement",
            label="知识补充",
            route="knowledge_material",
            feedback="已识别为知识补充，后续适合抽取摘要、标签、关键字段和系列归类。",
            structured_output_plan=("摘要", "详细摘要", "标签", "关键字段"),
            memory_layer_update_plan=("atom", "series_overview"),
            suggested_next_actions=("生成结构化正文", "创建待审原子记忆", "自动建议系列"),
        )
    return _direct_text_intent(
        intent="inspiration",
        label="灵感",
        route="inspiration_material",
        feedback="已识别为灵感或想法，后续适合保留原文、提炼可行动假设和可能关联的系列。",
        structured_output_plan=("原始想法", "可行动假设", "关联标签", "待确认问题"),
        memory_layer_update_plan=("atom", "scenario"),
        suggested_next_actions=("生成灵感卡片", "创建待审原子记忆", "等待用户确认系列"),
    )


def _direct_text_intent(
    *,
    intent: str,
    label: str,
    route: str,
    feedback: str,
    structured_output_plan: tuple[str, ...],
    memory_layer_update_plan: tuple[str, ...],
    suggested_next_actions: tuple[str, ...],
) -> dict[str, object]:
    return {
        "intent": intent,
        "label": label,
        "route": route,
        "feedback": feedback,
        "structured_output_plan": structured_output_plan,
        "memory_layer_update_plan": memory_layer_update_plan,
        "suggested_next_actions": suggested_next_actions,
    }


def _contains_any(text: str, tokens: tuple[str, ...]) -> bool:
    return any(token in text for token in tokens)


def _optional_metadata_int(source: Mapping[str, object], key: str) -> int | None:
    value = source.get(key)
    if value is None:
        return None
    if not isinstance(value, int):
        raise ValueError(f"source requires {key}")
    return value
