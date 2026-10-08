from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from .ports import ObjectStorePort


class MediaProcessingQueueError(ValueError):
    """Raised when a media Source cannot enter the processing queue contract."""


@dataclass(frozen=True, slots=True)
class MediaProcessingQueueResult:
    status: str
    job_id: str
    source_id: str
    source_type: str
    required_capability: str
    disabled_reason: str | None
    input_refs: tuple[str, ...]
    expected_output_refs: tuple[str, ...]
    activity_refs: tuple[str, ...]
    error: str | None = None
    output_refs: tuple[str, ...] = ()
    output_preview: str | None = None


@dataclass(frozen=True, slots=True)
class MediaOcrAdapterResult:
    text: str
    provider: str
    confidence: float | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)


class MediaOcrAdapterPort(Protocol):
    """Injected OCR adapter; Product Core does not bind to a concrete provider."""

    def extract_text(
        self,
        *,
        source: Mapping[str, object],
        job: Mapping[str, object],
    ) -> MediaOcrAdapterResult:
        """Return OCR text for an explicitly queued image media job."""


@dataclass(frozen=True, slots=True)
class MediaTranscriptionAdapterResult:
    text: str
    provider: str
    language: str | None = None
    confidence: float | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)


class MediaTranscriptionAdapterPort(Protocol):
    """Injected audio transcription adapter; Product Core does not bind to ASR."""

    def transcribe(
        self,
        *,
        source: Mapping[str, object],
        job: Mapping[str, object],
    ) -> MediaTranscriptionAdapterResult:
        """Return transcript text for an explicitly queued audio media job."""


@dataclass(frozen=True, slots=True)
class MediaFrameExtractionAdapterResult:
    frame_refs: tuple[str, ...]
    provider: str
    preview: str
    metadata: Mapping[str, object] = field(default_factory=dict)


class MediaFrameExtractionAdapterPort(Protocol):
    """Injected video frame extraction adapter; Product Core does not bind to video tools."""

    def extract_frames(
        self,
        *,
        source: Mapping[str, object],
        job: Mapping[str, object],
    ) -> MediaFrameExtractionAdapterResult:
        """Return frame refs for an explicitly queued video media job."""


class CreateMediaProcessingQueueJob:
    """Create a traceable media processing job without invoking OCR, ASR, or video tools."""

    _CAPABILITY_BY_SOURCE_TYPE = {
        "image": "ocr",
        "audio": "audio_transcription",
        "video": "video_frame_extraction",
    }
    _OUTPUT_KIND_BY_SOURCE_TYPE = {
        "image": "ocr_text",
        "audio": "transcript",
        "video": "frame_index",
    }
    _REFERENCE_KEY_BY_SOURCE_TYPE = {
        "image": "image_reference",
        "audio": "audio_reference",
        "video": "video_reference",
    }

    def __init__(
        self,
        object_store: ObjectStorePort,
        *,
        namespace_id: str = "default",
        now: str = "2026-07-01T17:10:00+08:00",
        enabled_capabilities: Sequence[str] = (),
    ) -> None:
        self._object_store = object_store
        self._namespace_id = namespace_id
        self._now = now
        self._enabled_capabilities = frozenset(enabled_capabilities)

    def execute(self, *, source_id: str) -> MediaProcessingQueueResult:
        clean_source_id = _required_input(source_id, "source_id")
        source = self._object_store.read("sources", clean_source_id)
        if source is None:
            raise MediaProcessingQueueError("source not found")
        source_type = _required_source_type(source)
        if source_type not in self._CAPABILITY_BY_SOURCE_TYPE:
            raise MediaProcessingQueueError("source type does not support media processing queue")
        required_capability = self._CAPABILITY_BY_SOURCE_TYPE[source_type]
        job_id = f"media-job-{clean_source_id}"
        expected_output_refs = (
            self._expected_output_ref(clean_source_id, self._OUTPUT_KIND_BY_SOURCE_TYPE[source_type]),
        )
        input_refs = self._input_refs(source, source_type)
        if required_capability in self._enabled_capabilities:
            status = "queued"
            disabled_reason = None
            event_type = "media_processing_queued"
        else:
            status = "skipped"
            disabled_reason = f"{required_capability}_disabled_until_adapter_enabled"
            event_type = "media_processing_skipped"

        job_record = {
            "schema_version": "1.0.0",
            "id": job_id,
            "source_id": clean_source_id,
            "source_type": source_type,
            "required_capability": required_capability,
            "projection_source": "effect_tree",
            "execution_state_owner": "core_effect_log",
            "status": status,
            "disabled_reason": disabled_reason,
            "input_refs": list(input_refs),
            "expected_output_refs": list(expected_output_refs),
            "adapter_contract": {
                "capability": required_capability,
                "provider": "not_bound",
                "binary_content_read": False,
                "remote_processing": False,
                "memory_publication": "not_started",
            },
            "error": None,
            "activity_refs": [],
            "created_at": self._now,
            "updated_at": self._now,
        }
        self._object_store.write("media_processing_jobs", job_id, job_record, expected_revision=None)
        activity_ref = self._write_event(
            job_id,
            clean_source_id,
            event_type=event_type,
            status=status,
            details={
                "required_capability": required_capability,
                "disabled_reason": disabled_reason,
                "expected_output_refs": list(expected_output_refs),
            },
        )
        job_record["activity_refs"] = [activity_ref]
        self._object_store.write("media_processing_jobs", job_id, job_record, expected_revision=None)
        self._update_source_media_processing(
            source,
            job_record,
            activity_refs=(activity_ref,),
        )
        return _result_from_record(job_record)

    def _input_refs(self, source: Mapping[str, object], source_type: str) -> tuple[str, ...]:
        refs = [_required_str(source, "storage_uri")]
        metadata = source.get("metadata")
        if isinstance(metadata, Mapping):
            reference_key = self._REFERENCE_KEY_BY_SOURCE_TYPE[source_type]
            reference = _optional_str(metadata.get(reference_key))
            if reference is not None:
                refs.append(f"crp-ref://{self._namespace_id}/assets/{reference}")
        return tuple(refs)

    def _expected_output_ref(self, source_id: str, output_kind: str) -> str:
        return f"crp://{self._namespace_id}/media-processing/{source_id}/{output_kind}.json"

    def _write_event(
        self,
        job_id: str,
        source_id: str,
        *,
        event_type: str,
        status: str,
        details: Mapping[str, object],
    ) -> str:
        event_id = f"event-{event_type.replace('_', '-')}-{source_id}"
        event_ref = self._event_uri(event_id)
        self._object_store.write(
            "activity_events",
            event_id,
            {
                "schema_version": "1.0.0",
                "id": event_id,
                "type": event_type,
                "source_id": source_id,
                "job_id": job_id,
                "status": status,
                "contentRead": False,
                "memoryPublication": "not_started",
                "details": dict(details),
                "created_at": self._now,
                "ref": event_ref,
            },
            expected_revision=None,
        )
        return event_ref

    def _event_uri(self, event_id: str) -> str:
        return f"crp://{self._namespace_id}/activity/{event_id}.json"

    def _update_source_media_processing(
        self,
        source: Mapping[str, object],
        job_record: Mapping[str, object],
        *,
        activity_refs: tuple[str, ...],
    ) -> None:
        _update_source_media_processing(
            self._object_store,
            source,
            job_record,
            namespace_id=self._namespace_id,
            activity_refs=activity_refs,
            updated_at=self._now,
        )


class UpdateMediaProcessingJobStatus:
    """Update the media processing job state without running a concrete provider."""

    _VALID_STATUSES = frozenset({"queued", "running", "failed", "completed", "skipped"})

    def __init__(
        self,
        object_store: ObjectStorePort,
        *,
        namespace_id: str = "default",
        now: str = "2026-07-01T17:10:00+08:00",
    ) -> None:
        self._object_store = object_store
        self._namespace_id = namespace_id
        self._now = now

    def execute(
        self,
        *,
        job_id: str,
        status: str,
        error: str | None = None,
        output_refs: Sequence[str] = (),
        output_preview: str | None = None,
    ) -> MediaProcessingQueueResult:
        clean_job_id = _required_input(job_id, "job_id")
        clean_status = _required_input(status, "status")
        if clean_status not in self._VALID_STATUSES:
            raise MediaProcessingQueueError("media processing status is invalid")
        if clean_status == "failed" and not _optional_str(error):
            raise MediaProcessingQueueError("failed media processing job requires error")
        record = self._object_store.read("media_processing_jobs", clean_job_id)
        if record is None:
            raise MediaProcessingQueueError("media processing job not found")
        source_id = _required_str(record, "source_id")
        source = self._object_store.read("sources", source_id)
        if source is None:
            raise MediaProcessingQueueError("source not found")
        updated = dict(record)
        updated["status"] = clean_status
        updated["error"] = _optional_str(error) if clean_status == "failed" else None
        if clean_status == "completed":
            updated["output_refs"] = list(_string_sequence(output_refs))
            updated["output_preview"] = _optional_str(output_preview)
        updated["updated_at"] = self._now
        activity_ref = self._write_event(
            clean_job_id,
            source_id,
            event_type=f"media_processing_{clean_status}",
            status=clean_status,
            details={
                "required_capability": _required_str(updated, "required_capability"),
                "error": updated["error"],
                "output_refs": list(_string_sequence(updated.get("output_refs"))),
            },
        )
        activity_refs = _string_sequence(updated.get("activity_refs"))
        updated["activity_refs"] = [*activity_refs, activity_ref]
        self._object_store.write("media_processing_jobs", clean_job_id, updated, expected_revision=None)
        _update_source_media_processing(
            self._object_store,
            source,
            updated,
            namespace_id=self._namespace_id,
            activity_refs=tuple(updated["activity_refs"]),
            updated_at=self._now,
        )
        return _result_from_record(updated)

    def _write_event(
        self,
        job_id: str,
        source_id: str,
        *,
        event_type: str,
        status: str,
        details: Mapping[str, object],
    ) -> str:
        event_id = f"event-{event_type.replace('_', '-')}-{job_id}"
        event_ref = f"crp://{self._namespace_id}/activity/{event_id}.json"
        self._object_store.write(
            "activity_events",
            event_id,
            {
                "schema_version": "1.0.0",
                "id": event_id,
                "type": event_type,
                "source_id": source_id,
                "job_id": job_id,
                "status": status,
                "contentRead": False,
                "memoryPublication": "not_started",
                "details": dict(details),
                "created_at": self._now,
                "ref": event_ref,
            },
            expected_revision=None,
        )
        return event_ref


class RunImageOcrAdapterForMediaJob:
    """Run an explicitly injected OCR adapter for a queued image media job."""

    def __init__(
        self,
        object_store: ObjectStorePort,
        *,
        namespace_id: str = "default",
        now: str = "2026-07-01T17:45:00+08:00",
        preview_chars: int = 240,
    ) -> None:
        if preview_chars <= 0:
            raise ValueError("preview_chars must be positive")
        self._object_store = object_store
        self._namespace_id = namespace_id
        self._now = now
        self._preview_chars = preview_chars

    def execute(
        self,
        *,
        job_id: str,
        adapter: MediaOcrAdapterPort,
    ) -> MediaProcessingQueueResult:
        clean_job_id = _required_input(job_id, "job_id")
        job = self._object_store.read("media_processing_jobs", clean_job_id)
        if job is None:
            raise MediaProcessingQueueError("media processing job not found")
        if _required_str(job, "source_type") != "image":
            raise MediaProcessingQueueError("OCR adapter requires an image media job")
        if _required_str(job, "required_capability") != "ocr":
            raise MediaProcessingQueueError("OCR adapter requires ocr capability")
        if _required_str(job, "status") != "queued":
            raise MediaProcessingQueueError("OCR adapter requires queued media job")
        source_id = _required_str(job, "source_id")
        source = self._object_store.read("sources", source_id)
        if source is None:
            raise MediaProcessingQueueError("source not found")

        return _run_text_output_adapter(
            self._object_store,
            namespace_id=self._namespace_id,
            now=self._now,
            preview_chars=self._preview_chars,
            job=job,
            source=source,
            output_id=f"media-output-ocr-{source_id}",
            output_kind="ocr_text",
            run_adapter=lambda running_job: _text_adapter_payload(
                adapter.extract_text(source=source, job=running_job)
            ),
        )


class RunAudioTranscriptionAdapterForMediaJob:
    """Run an injected transcription adapter for a queued audio media job."""

    def __init__(
        self,
        object_store: ObjectStorePort,
        *,
        namespace_id: str = "default",
        now: str = "2026-07-01T18:15:00+08:00",
        preview_chars: int = 240,
    ) -> None:
        if preview_chars <= 0:
            raise ValueError("preview_chars must be positive")
        self._object_store = object_store
        self._namespace_id = namespace_id
        self._now = now
        self._preview_chars = preview_chars

    def execute(
        self,
        *,
        job_id: str,
        adapter: MediaTranscriptionAdapterPort,
    ) -> MediaProcessingQueueResult:
        clean_job_id = _required_input(job_id, "job_id")
        job = self._object_store.read("media_processing_jobs", clean_job_id)
        if job is None:
            raise MediaProcessingQueueError("media processing job not found")
        if _required_str(job, "source_type") != "audio":
            raise MediaProcessingQueueError("transcription adapter requires an audio media job")
        if _required_str(job, "required_capability") != "audio_transcription":
            raise MediaProcessingQueueError("transcription adapter requires audio_transcription capability")
        if _required_str(job, "status") != "queued":
            raise MediaProcessingQueueError("transcription adapter requires queued media job")
        source_id = _required_str(job, "source_id")
        source = self._object_store.read("sources", source_id)
        if source is None:
            raise MediaProcessingQueueError("source not found")
        return _run_text_output_adapter(
            self._object_store,
            namespace_id=self._namespace_id,
            now=self._now,
            preview_chars=self._preview_chars,
            job=job,
            source=source,
            output_id=f"media-output-transcript-{source_id}",
            output_kind="transcript",
            run_adapter=lambda running_job: _transcription_adapter_payload(
                adapter.transcribe(source=source, job=running_job)
            ),
        )


class RunVideoFrameExtractionAdapterForMediaJob:
    """Run an injected frame extraction adapter for a queued video media job."""

    def __init__(
        self,
        object_store: ObjectStorePort,
        *,
        namespace_id: str = "default",
        now: str = "2026-07-01T18:30:00+08:00",
        preview_chars: int = 240,
    ) -> None:
        if preview_chars <= 0:
            raise ValueError("preview_chars must be positive")
        self._object_store = object_store
        self._namespace_id = namespace_id
        self._now = now
        self._preview_chars = preview_chars

    def execute(
        self,
        *,
        job_id: str,
        adapter: MediaFrameExtractionAdapterPort,
    ) -> MediaProcessingQueueResult:
        clean_job_id = _required_input(job_id, "job_id")
        job = self._object_store.read("media_processing_jobs", clean_job_id)
        if job is None:
            raise MediaProcessingQueueError("media processing job not found")
        if _required_str(job, "source_type") != "video":
            raise MediaProcessingQueueError("frame extraction adapter requires a video media job")
        if _required_str(job, "required_capability") != "video_frame_extraction":
            raise MediaProcessingQueueError("frame extraction adapter requires video_frame_extraction capability")
        if _required_str(job, "status") != "queued":
            raise MediaProcessingQueueError("frame extraction adapter requires queued media job")
        source_id = _required_str(job, "source_id")
        source = self._object_store.read("sources", source_id)
        if source is None:
            raise MediaProcessingQueueError("source not found")
        return _run_frame_output_adapter(
            self._object_store,
            namespace_id=self._namespace_id,
            now=self._now,
            preview_chars=self._preview_chars,
            job=job,
            source=source,
            output_id=f"media-output-frame-index-{source_id}",
            run_adapter=lambda running_job: adapter.extract_frames(source=source, job=running_job),
        )


def serialize_media_processing_queue_result(
    result: MediaProcessingQueueResult,
) -> dict[str, object]:
    return {
        "status": result.status,
        "job_id": result.job_id,
        "source_id": result.source_id,
        "source_type": result.source_type,
        "required_capability": result.required_capability,
        "disabled_reason": result.disabled_reason,
        "input_refs": list(result.input_refs),
        "expected_output_refs": list(result.expected_output_refs),
        "activity_refs": list(result.activity_refs),
        "error": result.error,
        "output_refs": list(result.output_refs),
        "output_preview": result.output_preview,
    }


def _update_source_media_processing(
    object_store: ObjectStorePort,
    source: Mapping[str, object],
    job_record: Mapping[str, object],
    *,
    namespace_id: str,
    activity_refs: tuple[str, ...],
    updated_at: str,
) -> None:
    source_id = _required_str(source, "id")
    metadata = dict(source.get("metadata") if isinstance(source.get("metadata"), Mapping) else {})
    job_id = _required_str(job_record, "id")
    status = _required_str(job_record, "status")
    metadata["media_processing"] = {
        "status": status,
        "job_id": job_id,
        "job_ref": f"crp://{namespace_id}/media-processing-jobs/{job_id}.json",
        "required_capability": _required_str(job_record, "required_capability"),
        "disabled_reason": _optional_str(job_record.get("disabled_reason")),
        "expected_output_refs": list(_string_sequence(job_record.get("expected_output_refs"))),
        "activity_refs": list(activity_refs),
        "error": _optional_str(job_record.get("error")),
        "output_refs": list(_string_sequence(job_record.get("output_refs"))),
        "output_preview": _optional_str(job_record.get("output_preview")),
        "updated_at": updated_at,
    }
    updated_source = dict(source)
    updated_source["metadata"] = metadata
    if status == "queued":
        updated_source["processing_state"] = "queued"
    elif status == "running":
        updated_source["processing_state"] = "processing"
    elif status == "completed":
        updated_source["processing_state"] = "ready"
    elif status == "failed":
        updated_source["processing_state"] = "failed"
    object_store.write("sources", source_id, updated_source, expected_revision=None)


def _result_from_record(record: Mapping[str, object]) -> MediaProcessingQueueResult:
    return MediaProcessingQueueResult(
        status=_required_str(record, "status"),
        job_id=_required_str(record, "id"),
        source_id=_required_str(record, "source_id"),
        source_type=_required_str(record, "source_type"),
        required_capability=_required_str(record, "required_capability"),
        disabled_reason=_optional_str(record.get("disabled_reason")),
        input_refs=_string_sequence(record.get("input_refs")),
        expected_output_refs=_string_sequence(record.get("expected_output_refs")),
        activity_refs=_string_sequence(record.get("activity_refs")),
        error=_optional_str(record.get("error")),
        output_refs=_string_sequence(record.get("output_refs")),
        output_preview=_optional_str(record.get("output_preview")),
    )


def _run_text_output_adapter(
    object_store: ObjectStorePort,
    *,
    namespace_id: str,
    now: str,
    preview_chars: int,
    job: Mapping[str, object],
    source: Mapping[str, object],
    output_id: str,
    output_kind: str,
    run_adapter,
) -> MediaProcessingQueueResult:
    job_id = _required_str(job, "id")
    source_id = _required_str(job, "source_id")
    updater = UpdateMediaProcessingJobStatus(
        object_store,
        namespace_id=namespace_id,
        now=now,
    )
    updater.execute(job_id=job_id, status="running")
    running_job = object_store.read("media_processing_jobs", job_id)
    if running_job is None:
        raise MediaProcessingQueueError("media processing job not found")
    try:
        payload = run_adapter(running_job)
        text = _required_adapter_text(_required_str(payload, "text"))
        preview = _preview(text, preview_chars)
        encoded = text.encode("utf-8")
        output_ref = f"crp://{namespace_id}/media-processing-outputs/{output_id}.json"
        output_record = {
            "schema_version": "1.0.0",
            "id": output_id,
            "job_id": job_id,
            "source_id": source_id,
            "source_type": _required_str(source, "type"),
            "output_kind": output_kind,
            "status": "completed",
            "provider": _required_str(payload, "provider"),
            "language": _optional_str(payload.get("language")),
            "confidence": payload.get("confidence"),
            "char_count": len(text),
            "byte_count": len(encoded),
            "preview": preview,
            "text": text,
            "metadata": dict(payload.get("metadata") if isinstance(payload.get("metadata"), Mapping) else {}),
            "memory_publication": "not_started",
            "created_at": now,
            "ref": output_ref,
        }
        object_store.write(
            "media_processing_outputs",
            output_id,
            output_record,
            expected_revision=None,
        )
        return updater.execute(
            job_id=job_id,
            status="completed",
            output_refs=(output_ref,),
            output_preview=preview,
        )
    except Exception as error:  # noqa: BLE001 - adapter errors must become traceable job failures.
        reason = str(error) or error.__class__.__name__
        return updater.execute(job_id=job_id, status="failed", error=reason)


def _text_adapter_payload(result: MediaOcrAdapterResult) -> Mapping[str, object]:
    return {
        "text": result.text,
        "provider": result.provider,
        "language": None,
        "confidence": result.confidence,
        "metadata": dict(result.metadata),
    }


def _transcription_adapter_payload(result: MediaTranscriptionAdapterResult) -> Mapping[str, object]:
    return {
        "text": result.text,
        "provider": result.provider,
        "language": result.language,
        "confidence": result.confidence,
        "metadata": dict(result.metadata),
    }


def _run_frame_output_adapter(
    object_store: ObjectStorePort,
    *,
    namespace_id: str,
    now: str,
    preview_chars: int,
    job: Mapping[str, object],
    source: Mapping[str, object],
    output_id: str,
    run_adapter,
) -> MediaProcessingQueueResult:
    job_id = _required_str(job, "id")
    source_id = _required_str(job, "source_id")
    updater = UpdateMediaProcessingJobStatus(
        object_store,
        namespace_id=namespace_id,
        now=now,
    )
    updater.execute(job_id=job_id, status="running")
    running_job = object_store.read("media_processing_jobs", job_id)
    if running_job is None:
        raise MediaProcessingQueueError("media processing job not found")
    try:
        adapter_result = run_adapter(running_job)
        frame_refs = _required_frame_refs(adapter_result.frame_refs)
        preview = _preview(_required_adapter_text(adapter_result.preview), preview_chars)
        output_ref = f"crp://{namespace_id}/media-processing-outputs/{output_id}.json"
        output_record = {
            "schema_version": "1.0.0",
            "id": output_id,
            "job_id": job_id,
            "source_id": source_id,
            "source_type": _required_str(source, "type"),
            "output_kind": "frame_index",
            "status": "completed",
            "provider": adapter_result.provider,
            "frame_count": len(frame_refs),
            "frame_refs": list(frame_refs),
            "preview": preview,
            "metadata": dict(adapter_result.metadata),
            "memory_publication": "not_started",
            "created_at": now,
            "ref": output_ref,
        }
        object_store.write(
            "media_processing_outputs",
            output_id,
            output_record,
            expected_revision=None,
        )
        return updater.execute(
            job_id=job_id,
            status="completed",
            output_refs=(output_ref,),
            output_preview=preview,
        )
    except Exception as error:  # noqa: BLE001 - adapter errors must become traceable job failures.
        reason = str(error) or error.__class__.__name__
        return updater.execute(job_id=job_id, status="failed", error=reason)


def _required_adapter_text(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MediaProcessingQueueError("media text adapter returned empty text")
    return value


def _required_frame_refs(value: Sequence[str]) -> tuple[str, ...]:
    refs = _string_sequence(value)
    if not refs:
        raise MediaProcessingQueueError("frame extraction adapter returned no frame refs")
    return refs


def _preview(text: str, limit: int) -> str:
    compact = " ".join(text.strip().split())
    if len(compact) <= limit:
        return compact
    return f"{compact[: limit - 1]}..."


def _required_source_type(source: Mapping[str, object]) -> str:
    value = _required_str(source, "type")
    return value


def _required_input(value: str, field_name: str) -> str:
    if not isinstance(value, str):
        raise MediaProcessingQueueError(f"{field_name} is required")
    clean = value.strip()
    if not clean:
        raise MediaProcessingQueueError(f"{field_name} is required")
    return clean


def _required_str(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise MediaProcessingQueueError(f"{key} is required")
    return value


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _string_sequence(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item)
