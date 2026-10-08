from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.composition import ObjectStoreLibraryOverviewReader
from core.ingestion_core import ObjectStoreSourceRegistrar, SourceSubmission
from core.product_core import (
    CreateMediaProcessingQueueJob,
    GetLibraryOverview,
    MediaFrameExtractionAdapterResult,
    MediaOcrAdapterResult,
    MediaProcessingQueueError,
    MediaTranscriptionAdapterResult,
    RunAudioTranscriptionAdapterForMediaJob,
    RunImageOcrAdapterForMediaJob,
    RunVideoFrameExtractionAdapterForMediaJob,
    ServeMediaProcessingQueueEndpoint,
    UpdateMediaProcessingJobStatus,
    serialize_library_overview,
    serialize_media_processing_queue_result,
)
from core.storage_provider import JsonObjectStore
from tools.validate_rebuild_contracts import validate_contract_instance


ROOT = Path(__file__).resolve().parents[2]
CONTRACT_ROOT = ROOT / "core-contracts" / "rebuild"


class StubOcrAdapter:
    def __init__(self, text: str = "白板内容：下一轮需要推进 OCR adapter。") -> None:
        self._text = text

    def extract_text(self, *, source: dict[str, object], job: dict[str, object]) -> MediaOcrAdapterResult:
        return MediaOcrAdapterResult(
            text=self._text,
            provider="stub-local-ocr",
            confidence=0.91,
            metadata={"source_title": source["title"], "job_status": job["status"]},
        )


class FailingOcrAdapter:
    def extract_text(self, *, source: dict[str, object], job: dict[str, object]) -> MediaOcrAdapterResult:
        raise RuntimeError("ocr adapter failed to read reference")


class StubTranscriptionAdapter:
    def __init__(self, text: str = "会议转写：先确认产品设计文档，再推进音频 adapter。") -> None:
        self._text = text

    def transcribe(
        self,
        *,
        source: dict[str, object],
        job: dict[str, object],
    ) -> MediaTranscriptionAdapterResult:
        return MediaTranscriptionAdapterResult(
            text=self._text,
            provider="stub-local-asr",
            language="zh-CN",
            confidence=0.88,
            metadata={"source_title": source["title"], "job_status": job["status"]},
        )


class FailingTranscriptionAdapter:
    def transcribe(
        self,
        *,
        source: dict[str, object],
        job: dict[str, object],
    ) -> MediaTranscriptionAdapterResult:
        raise RuntimeError("transcription adapter failed to read reference")


class StubFrameExtractionAdapter:
    def extract_frames(
        self,
        *,
        source: dict[str, object],
        job: dict[str, object],
    ) -> MediaFrameExtractionAdapterResult:
        return MediaFrameExtractionAdapterResult(
            frame_refs=(
                "crp-ref://default/assets/platform-video-ref-screen/frames/frame-0001.jpg",
                "crp-ref://default/assets/platform-video-ref-screen/frames/frame-0002.jpg",
            ),
            provider="stub-local-frame-extractor",
            preview="提取 2 个关键帧：开场画面、白板讲解画面。",
            metadata={"source_title": source["title"], "job_status": job["status"]},
        )


class FailingFrameExtractionAdapter:
    def extract_frames(
        self,
        *,
        source: dict[str, object],
        job: dict[str, object],
    ) -> MediaFrameExtractionAdapterResult:
        raise RuntimeError("frame extraction adapter failed to read reference")


def _store(tmp_path: Path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def _registrar(object_store: JsonObjectStore) -> ObjectStoreSourceRegistrar:
    return ObjectStoreSourceRegistrar(object_store)


def _image_source(object_store: JsonObjectStore) -> dict[str, object]:
    return dict(
        _registrar(object_store).register(
            SourceSubmission(
                kind="image",
                title="Whiteboard photo",
                display_name="whiteboard.png",
                media_type="image/png",
                size_bytes=4096,
                image_reference="platform-image-ref-whiteboard",
                width_px=1200,
                height_px=800,
            )
        )
    )


def _audio_source(object_store: JsonObjectStore) -> dict[str, object]:
    return dict(
        _registrar(object_store).register(
            SourceSubmission(
                kind="audio",
                title="Meeting audio",
                display_name="meeting.m4a",
                media_type="audio/mp4",
                size_bytes=8192,
                audio_reference="platform-audio-ref-meeting",
                duration_ms=120000,
            )
        )
    )


def _video_source(object_store: JsonObjectStore) -> dict[str, object]:
    return dict(
        _registrar(object_store).register(
            SourceSubmission(
                kind="video",
                title="Screen recording",
                display_name="screen.mp4",
                media_type="video/mp4",
                size_bytes=16384,
                video_reference="platform-video-ref-screen",
                duration_ms=180000,
                width_px=1920,
                height_px=1080,
            )
        )
    )


def test_image_media_processing_queue_records_skipped_ocr_and_overview(
    tmp_path: Path,
) -> None:
    object_store = _store(tmp_path)
    source = _image_source(object_store)

    result = CreateMediaProcessingQueueJob(object_store).execute(source_id=str(source["id"]))
    payload = serialize_media_processing_queue_result(result)

    job = object_store.read("media_processing_jobs", result.job_id)
    updated_source = object_store.read("sources", str(source["id"]))
    event_types = [event["type"] for event in object_store.list("activity_events")]
    overview = serialize_library_overview(
        GetLibraryOverview(ObjectStoreLibraryOverviewReader(object_store)).execute()
    )
    source_item = next(item for item in overview["items"] if item["item_id"] == source["id"])

    assert payload["status"] == "skipped"
    assert payload["required_capability"] == "ocr"
    assert payload["disabled_reason"] == "ocr_disabled_until_adapter_enabled"
    assert payload["expected_output_refs"] == [
        f"crp://default/media-processing/{source['id']}/ocr_text.json"
    ]
    assert job is not None
    assert job["adapter_contract"]["provider"] == "not_bound"
    assert job["adapter_contract"]["binary_content_read"] is False
    assert job["adapter_contract"]["remote_processing"] is False
    assert updated_source is not None
    assert updated_source["processing_state"] == "captured"
    assert updated_source["metadata"]["media_processing"]["status"] == "skipped"
    assert updated_source["metadata"]["media_processing"]["job_id"] == result.job_id
    assert "media_processing_skipped" in event_types
    assert source_item["media_processing_status"] == "skipped"
    assert source_item["media_required_capability"] == "ocr"
    assert source_item["media_disabled_reason"] == "ocr_disabled_until_adapter_enabled"
    assert source_item["media_expected_output_refs"] == payload["expected_output_refs"]
    assert "media_processing_provider_execution" in source_item["blocked_operations"]
    assert result.activity_refs[0] in source_item["trace_refs"]
    assert f"crp://default/media-processing-jobs/{result.job_id}.json" in source_item["trace_refs"]
    assert validate_contract_instance(
        "source.schema.json",
        json.loads((CONTRACT_ROOT / "source.schema.json").read_text(encoding="utf-8")),
        updated_source,
    ) == []


def test_audio_media_processing_queue_can_enter_queued_state_when_capability_enabled(
    tmp_path: Path,
) -> None:
    object_store = _store(tmp_path)
    source = _audio_source(object_store)

    result = CreateMediaProcessingQueueJob(
        object_store,
        enabled_capabilities=("audio_transcription",),
    ).execute(source_id=str(source["id"]))
    updated_source = object_store.read("sources", str(source["id"]))

    assert result.status == "queued"
    assert result.required_capability == "audio_transcription"
    assert result.disabled_reason is None
    assert result.input_refs == (
        source["storage_uri"],
        "crp-ref://default/assets/platform-audio-ref-meeting",
    )
    assert updated_source is not None
    assert updated_source["processing_state"] == "queued"
    assert updated_source["metadata"]["media_processing"]["status"] == "queued"


def test_media_processing_job_status_transitions_update_source_and_activity(
    tmp_path: Path,
) -> None:
    object_store = _store(tmp_path)
    source = _video_source(object_store)
    queued = CreateMediaProcessingQueueJob(
        object_store,
        enabled_capabilities=("video_frame_extraction",),
    ).execute(source_id=str(source["id"]))
    updater = UpdateMediaProcessingJobStatus(object_store)

    running = updater.execute(job_id=queued.job_id, status="running")
    failed = updater.execute(job_id=queued.job_id, status="failed", error="adapter contract timeout")
    completed = updater.execute(job_id=queued.job_id, status="completed")
    updated_source = object_store.read("sources", str(source["id"]))
    event_types = [event["type"] for event in object_store.list("activity_events")]

    assert running.status == "running"
    assert failed.status == "failed"
    assert failed.error == "adapter contract timeout"
    assert completed.status == "completed"
    assert completed.error is None
    assert updated_source is not None
    assert updated_source["processing_state"] == "ready"
    assert updated_source["metadata"]["media_processing"]["status"] == "completed"
    assert "media_processing_running" in event_types
    assert "media_processing_failed" in event_types
    assert "media_processing_completed" in event_types
    assert len(completed.activity_refs) == 4


def test_image_ocr_adapter_writes_output_and_library_preview(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    source = _image_source(object_store)
    queued = CreateMediaProcessingQueueJob(
        object_store,
        enabled_capabilities=("ocr",),
    ).execute(source_id=str(source["id"]))

    result = RunImageOcrAdapterForMediaJob(object_store).execute(
        job_id=queued.job_id,
        adapter=StubOcrAdapter(),
    )
    output = object_store.read("media_processing_outputs", f"media-output-ocr-{source['id']}")
    updated_source = object_store.read("sources", str(source["id"]))
    event_types = [event["type"] for event in object_store.list("activity_events")]
    overview = serialize_library_overview(
        GetLibraryOverview(ObjectStoreLibraryOverviewReader(object_store)).execute()
    )
    source_item = next(item for item in overview["items"] if item["item_id"] == source["id"])

    assert result.status == "completed"
    assert result.output_preview == "白板内容：下一轮需要推进 OCR adapter。"
    assert result.output_refs == (
        f"crp://default/media-processing-outputs/media-output-ocr-{source['id']}.json",
    )
    assert output is not None
    assert output["output_kind"] == "ocr_text"
    assert output["provider"] == "stub-local-ocr"
    assert output["memory_publication"] == "not_started"
    assert output["text"] == "白板内容：下一轮需要推进 OCR adapter。"
    assert updated_source is not None
    assert updated_source["processing_state"] == "ready"
    assert updated_source["metadata"]["media_processing"]["status"] == "completed"
    assert updated_source["metadata"]["media_processing"]["output_refs"] == list(result.output_refs)
    assert "media_processing_running" in event_types
    assert "media_processing_completed" in event_types
    assert source_item["media_processing_status"] == "completed"
    assert source_item["media_output_refs"] == list(result.output_refs)
    assert source_item["media_output_preview"] == result.output_preview
    assert result.output_refs[0] in source_item["trace_refs"]
    assert "media_processing_provider_execution" not in source_item["blocked_operations"]


def test_image_ocr_adapter_failure_updates_job_and_source_error(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    source = _image_source(object_store)
    queued = CreateMediaProcessingQueueJob(
        object_store,
        enabled_capabilities=("ocr",),
    ).execute(source_id=str(source["id"]))

    result = RunImageOcrAdapterForMediaJob(object_store).execute(
        job_id=queued.job_id,
        adapter=FailingOcrAdapter(),
    )
    updated_source = object_store.read("sources", str(source["id"]))
    output = object_store.read("media_processing_outputs", f"media-output-ocr-{source['id']}")

    assert result.status == "failed"
    assert result.error == "ocr adapter failed to read reference"
    assert result.output_refs == ()
    assert output is None
    assert updated_source is not None
    assert updated_source["processing_state"] == "failed"
    assert updated_source["metadata"]["media_processing"]["status"] == "failed"
    assert updated_source["metadata"]["media_processing"]["error"] == result.error


def test_image_ocr_adapter_rejects_non_queued_or_non_image_jobs(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    skipped_source = _image_source(object_store)
    skipped = CreateMediaProcessingQueueJob(object_store).execute(source_id=str(skipped_source["id"]))
    audio_source = _audio_source(object_store)
    audio = CreateMediaProcessingQueueJob(
        object_store,
        enabled_capabilities=("audio_transcription",),
    ).execute(source_id=str(audio_source["id"]))

    with pytest.raises(MediaProcessingQueueError, match="queued"):
        RunImageOcrAdapterForMediaJob(object_store).execute(
            job_id=skipped.job_id,
            adapter=StubOcrAdapter(),
        )
    with pytest.raises(MediaProcessingQueueError, match="image media job"):
        RunImageOcrAdapterForMediaJob(object_store).execute(
            job_id=audio.job_id,
            adapter=StubOcrAdapter(),
        )


def test_audio_transcription_adapter_writes_output_and_library_preview(
    tmp_path: Path,
) -> None:
    object_store = _store(tmp_path)
    source = _audio_source(object_store)
    queued = CreateMediaProcessingQueueJob(
        object_store,
        enabled_capabilities=("audio_transcription",),
    ).execute(source_id=str(source["id"]))

    result = RunAudioTranscriptionAdapterForMediaJob(object_store).execute(
        job_id=queued.job_id,
        adapter=StubTranscriptionAdapter(),
    )
    output = object_store.read("media_processing_outputs", f"media-output-transcript-{source['id']}")
    updated_source = object_store.read("sources", str(source["id"]))
    event_types = [event["type"] for event in object_store.list("activity_events")]
    overview = serialize_library_overview(
        GetLibraryOverview(ObjectStoreLibraryOverviewReader(object_store)).execute()
    )
    source_item = next(item for item in overview["items"] if item["item_id"] == source["id"])

    assert result.status == "completed"
    assert result.output_preview == "会议转写：先确认产品设计文档，再推进音频 adapter。"
    assert result.output_refs == (
        f"crp://default/media-processing-outputs/media-output-transcript-{source['id']}.json",
    )
    assert output is not None
    assert output["output_kind"] == "transcript"
    assert output["provider"] == "stub-local-asr"
    assert output["language"] == "zh-CN"
    assert output["memory_publication"] == "not_started"
    assert output["text"] == "会议转写：先确认产品设计文档，再推进音频 adapter。"
    assert updated_source is not None
    assert updated_source["processing_state"] == "ready"
    assert updated_source["metadata"]["media_processing"]["status"] == "completed"
    assert updated_source["metadata"]["media_processing"]["output_refs"] == list(result.output_refs)
    assert "media_processing_running" in event_types
    assert "media_processing_completed" in event_types
    assert source_item["media_processing_status"] == "completed"
    assert source_item["media_output_refs"] == list(result.output_refs)
    assert source_item["media_output_preview"] == result.output_preview
    assert result.output_refs[0] in source_item["trace_refs"]
    assert "media_processing_provider_execution" not in source_item["blocked_operations"]


def test_audio_transcription_adapter_failure_updates_job_and_source_error(
    tmp_path: Path,
) -> None:
    object_store = _store(tmp_path)
    source = _audio_source(object_store)
    queued = CreateMediaProcessingQueueJob(
        object_store,
        enabled_capabilities=("audio_transcription",),
    ).execute(source_id=str(source["id"]))

    result = RunAudioTranscriptionAdapterForMediaJob(object_store).execute(
        job_id=queued.job_id,
        adapter=FailingTranscriptionAdapter(),
    )
    updated_source = object_store.read("sources", str(source["id"]))
    output = object_store.read("media_processing_outputs", f"media-output-transcript-{source['id']}")

    assert result.status == "failed"
    assert result.error == "transcription adapter failed to read reference"
    assert result.output_refs == ()
    assert output is None
    assert updated_source is not None
    assert updated_source["processing_state"] == "failed"
    assert updated_source["metadata"]["media_processing"]["status"] == "failed"
    assert updated_source["metadata"]["media_processing"]["error"] == result.error


def test_audio_transcription_adapter_rejects_non_queued_or_non_audio_jobs(
    tmp_path: Path,
) -> None:
    object_store = _store(tmp_path)
    skipped_source = _audio_source(object_store)
    skipped = CreateMediaProcessingQueueJob(object_store).execute(source_id=str(skipped_source["id"]))
    image_source = _image_source(object_store)
    image = CreateMediaProcessingQueueJob(
        object_store,
        enabled_capabilities=("ocr",),
    ).execute(source_id=str(image_source["id"]))

    with pytest.raises(MediaProcessingQueueError, match="queued"):
        RunAudioTranscriptionAdapterForMediaJob(object_store).execute(
            job_id=skipped.job_id,
            adapter=StubTranscriptionAdapter(),
        )
    with pytest.raises(MediaProcessingQueueError, match="audio media job"):
        RunAudioTranscriptionAdapterForMediaJob(object_store).execute(
            job_id=image.job_id,
            adapter=StubTranscriptionAdapter(),
        )


def test_video_frame_extraction_adapter_writes_output_and_library_preview(
    tmp_path: Path,
) -> None:
    object_store = _store(tmp_path)
    source = _video_source(object_store)
    queued = CreateMediaProcessingQueueJob(
        object_store,
        enabled_capabilities=("video_frame_extraction",),
    ).execute(source_id=str(source["id"]))

    result = RunVideoFrameExtractionAdapterForMediaJob(object_store).execute(
        job_id=queued.job_id,
        adapter=StubFrameExtractionAdapter(),
    )
    output = object_store.read("media_processing_outputs", f"media-output-frame-index-{source['id']}")
    updated_source = object_store.read("sources", str(source["id"]))
    event_types = [event["type"] for event in object_store.list("activity_events")]
    overview = serialize_library_overview(
        GetLibraryOverview(ObjectStoreLibraryOverviewReader(object_store)).execute()
    )
    source_item = next(item for item in overview["items"] if item["item_id"] == source["id"])

    assert result.status == "completed"
    assert result.output_preview == "提取 2 个关键帧：开场画面、白板讲解画面。"
    assert result.output_refs == (
        f"crp://default/media-processing-outputs/media-output-frame-index-{source['id']}.json",
    )
    assert output is not None
    assert output["output_kind"] == "frame_index"
    assert output["provider"] == "stub-local-frame-extractor"
    assert output["frame_count"] == 2
    assert output["frame_refs"] == [
        "crp-ref://default/assets/platform-video-ref-screen/frames/frame-0001.jpg",
        "crp-ref://default/assets/platform-video-ref-screen/frames/frame-0002.jpg",
    ]
    assert output["memory_publication"] == "not_started"
    assert updated_source is not None
    assert updated_source["processing_state"] == "ready"
    assert updated_source["metadata"]["media_processing"]["status"] == "completed"
    assert updated_source["metadata"]["media_processing"]["output_refs"] == list(result.output_refs)
    assert "media_processing_running" in event_types
    assert "media_processing_completed" in event_types
    assert source_item["media_processing_status"] == "completed"
    assert source_item["media_output_refs"] == list(result.output_refs)
    assert source_item["media_output_preview"] == result.output_preview
    assert result.output_refs[0] in source_item["trace_refs"]
    assert "media_processing_provider_execution" not in source_item["blocked_operations"]


def test_video_frame_extraction_adapter_failure_updates_job_and_source_error(
    tmp_path: Path,
) -> None:
    object_store = _store(tmp_path)
    source = _video_source(object_store)
    queued = CreateMediaProcessingQueueJob(
        object_store,
        enabled_capabilities=("video_frame_extraction",),
    ).execute(source_id=str(source["id"]))

    result = RunVideoFrameExtractionAdapterForMediaJob(object_store).execute(
        job_id=queued.job_id,
        adapter=FailingFrameExtractionAdapter(),
    )
    updated_source = object_store.read("sources", str(source["id"]))
    output = object_store.read("media_processing_outputs", f"media-output-frame-index-{source['id']}")

    assert result.status == "failed"
    assert result.error == "frame extraction adapter failed to read reference"
    assert result.output_refs == ()
    assert output is None
    assert updated_source is not None
    assert updated_source["processing_state"] == "failed"
    assert updated_source["metadata"]["media_processing"]["status"] == "failed"
    assert updated_source["metadata"]["media_processing"]["error"] == result.error


def test_video_frame_extraction_adapter_rejects_non_queued_or_non_video_jobs(
    tmp_path: Path,
) -> None:
    object_store = _store(tmp_path)
    skipped_source = _video_source(object_store)
    skipped = CreateMediaProcessingQueueJob(object_store).execute(source_id=str(skipped_source["id"]))
    image_source = _image_source(object_store)
    image = CreateMediaProcessingQueueJob(
        object_store,
        enabled_capabilities=("ocr",),
    ).execute(source_id=str(image_source["id"]))

    with pytest.raises(MediaProcessingQueueError, match="queued"):
        RunVideoFrameExtractionAdapterForMediaJob(object_store).execute(
            job_id=skipped.job_id,
            adapter=StubFrameExtractionAdapter(),
        )
    with pytest.raises(MediaProcessingQueueError, match="video media job"):
        RunVideoFrameExtractionAdapterForMediaJob(object_store).execute(
            job_id=image.job_id,
            adapter=StubFrameExtractionAdapter(),
        )


def test_media_processing_queue_rejects_non_media_source_and_missing_failure_error(
    tmp_path: Path,
) -> None:
    object_store = _store(tmp_path)
    text_source = _registrar(object_store).register(
        SourceSubmission(kind="text", title="Text", content="No media processing.")
    )

    with pytest.raises(MediaProcessingQueueError, match="source type does not support"):
        CreateMediaProcessingQueueJob(object_store).execute(source_id=str(text_source["id"]))

    audio_source = _audio_source(object_store)
    queued = CreateMediaProcessingQueueJob(
        object_store,
        enabled_capabilities=("audio_transcription",),
    ).execute(source_id=str(audio_source["id"]))

    with pytest.raises(MediaProcessingQueueError, match="requires error"):
        UpdateMediaProcessingJobStatus(object_store).execute(job_id=queued.job_id, status="failed")


def test_media_processing_queue_endpoint_serves_narrow_post(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    source = _audio_source(object_store)
    endpoint = ServeMediaProcessingQueueEndpoint()

    def queue_media_processing(
        *,
        source_id: str,
        enabled_capabilities: tuple[str, ...],
    ):
        return CreateMediaProcessingQueueJob(
            object_store,
            enabled_capabilities=enabled_capabilities,
        ).execute(source_id=source_id)

    response = endpoint.execute(
        method="POST",
        path=f"/api/rebuild/sources/{source['id']}/media-processing",
        body={"enabled_capabilities": ["audio_transcription"]},
        queue_media_processing=queue_media_processing,
    )

    assert response.status_code == 201
    assert response.headers["Content-Type"] == "application/json"
    assert response.headers["Cache-Control"] == "no-store"
    assert response.body["status"] == "queued"
    assert response.body["source_id"] == source["id"]
    assert response.body["required_capability"] == "audio_transcription"


def test_media_processing_queue_endpoint_rejects_wrong_method_path_and_body(
    tmp_path: Path,
) -> None:
    object_store = _store(tmp_path)
    source = _image_source(object_store)
    endpoint = ServeMediaProcessingQueueEndpoint()

    def queue_media_processing(
        *,
        source_id: str,
        enabled_capabilities: tuple[str, ...],
    ):
        return CreateMediaProcessingQueueJob(
            object_store,
            enabled_capabilities=enabled_capabilities,
        ).execute(source_id=source_id)

    wrong_method = endpoint.execute(
        method="GET",
        path=f"/api/rebuild/sources/{source['id']}/media-processing",
        body=None,
        queue_media_processing=queue_media_processing,
    )
    wrong_path = endpoint.execute(
        method="POST",
        path=f"/api/rebuild/sources/{source['id']}/other",
        body=None,
        queue_media_processing=queue_media_processing,
    )
    wrong_body = endpoint.execute(
        method="POST",
        path=f"/api/rebuild/sources/{source['id']}/media-processing",
        body={"enabled_capabilities": "ocr"},
        queue_media_processing=queue_media_processing,
    )

    assert wrong_method.status_code == 405
    assert wrong_method.headers["Allow"] == "POST"
    assert wrong_path.status_code == 404
    assert wrong_body.status_code == 400
    assert wrong_body.body["reason"] == "enabled_capabilities must be a list"
