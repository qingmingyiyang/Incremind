"""Phase 3: Media/document auto-workflow integration into OrchestrateWorkbenchAutoIntake.

These tests verify that the unified auto-intake orchestrator triggers the
corresponding media auto-workflow (document text extraction, image OCR, audio
auto workflow, video auto workflow) after capturing the source, when the
optional callable is provided. They also verify that:

- When the callable is None, behavior stays backward-compatible (the legacy
  needs_extractor / needs_asr / needs_video_workflow status and await_* next_step
  are preserved).
- On a blocked or failed workflow the original needs_* status is kept so the
  UI can surface the blocked reason instead of crashing.
- On a successful workflow the item is upgraded to ``completed`` with a
  ``media_auto_workflow`` trace in ``auto_organization``.
- No secret material (sk-*, password=, cookie:, authorization:) or local
  absolute paths leak into the serialized response.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from core.ingestion_core import ObjectStoreSourceRegistrar
from core.job_runner import ObjectStoreJobRepository
from core.product_core import (
    OrchestrateWorkbenchAutoIntake,
    serialize_workbench_auto_intake_result,
)
from core.product_core.audio_auto_workflow import AudioAutoWorkflowResult, AudioAutoWorkflowStep
from core.product_core.media_processing_queue import MediaProcessingQueueResult
from core.product_core.source_content_read import SourceContentReadResult
from core.product_core.video_auto_workflow import VideoAutoWorkflowResult, VideoAutoWorkflowStep
from core.storage_provider import JsonObjectStore


def _store(tmp_path: Path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def _make_orchestrator(
    store: JsonObjectStore,
    *,
    run_document_text_extractor=None,
    run_image_ocr=None,
    run_audio_auto_workflow=None,
    run_video_auto_workflow=None,
    prepare_file_source=None,
    prepare_video_source=None,
) -> OrchestrateWorkbenchAutoIntake:
    return OrchestrateWorkbenchAutoIntake(
        object_store=store,
        source_registrar=ObjectStoreSourceRegistrar(store, namespace_id="default"),
        job_repository=ObjectStoreJobRepository(store),
        fetch_url=lambda url: "<html><body><p>个人 AI 记忆工作台</p></body></html>",
        namespace_id="default",
        run_document_text_extractor=run_document_text_extractor,
        run_image_ocr=run_image_ocr,
        run_audio_auto_workflow=run_audio_auto_workflow,
        run_video_auto_workflow=run_video_auto_workflow,
        prepare_file_source=prepare_file_source,
        prepare_video_source=prepare_video_source,
    )


def _completed_read_result(source_id: str) -> SourceContentReadResult:
    return SourceContentReadResult(
        status="completed",
        source_id=source_id,
        media_type="application/pdf",
        content_read=True,
        char_count=128,
        byte_count=256,
        preview="extracted text preview",
        read_ref=f"crp://default/source_content_reads/content-read-{source_id}.json",
        error=None,
        activity_refs=(f"activity-read-{source_id}",),
    )


def _completed_ocr_result(source_id: str) -> MediaProcessingQueueResult:
    return MediaProcessingQueueResult(
        status="completed",
        job_id=f"media-job-ocr-{source_id}",
        source_id=source_id,
        source_type="image",
        required_capability="ocr",
        disabled_reason=None,
        input_refs=(f"image-ref-{source_id}",),
        expected_output_refs=(f"media-output-ocr-{source_id}",),
        activity_refs=(f"activity-ocr-{source_id}",),
        error=None,
        output_refs=(f"media-output-ocr-{source_id}",),
        output_preview="ocr extracted text",
    )


def _completed_audio_result(source_id: str) -> AudioAutoWorkflowResult:
    step = AudioAutoWorkflowStep(
        name="transcribe_audio",
        status="completed",
        reason=None,
        job_id=f"job-transcribe-{source_id}",
        output_id=f"media-output-transcript-{source_id}",
        audio_asset_id=f"audio-asset-{source_id}",
    )
    return AudioAutoWorkflowResult(
        status="completed",
        workflow_id=f"audio-auto-workflow-{source_id}",
        source_id=source_id,
        project_id="default",
        steps=(step,),
        audio_asset_id=f"audio-asset-{source_id}",
        transcript_output_id=f"media-output-transcript-{source_id}",
        transcriber_status="enabled",
        transcriber_model_profile="local-whisper",
        transcriber_model_name="whisper-base",
        audio_asset_status="available",
        readiness_reason=None,
        next_step="transcript_ready",
        memory_publication="not_started",
        blocked_operations=(),
        error=None,
    )


def _completed_video_result(source_id: str) -> VideoAutoWorkflowResult:
    steps = (
        VideoAutoWorkflowStep(name="extract_audio", status="completed", reason=None, output_id=f"audio-asset-{source_id}"),
        VideoAutoWorkflowStep(name="transcribe_audio", status="completed", reason=None, output_id=f"transcript-{source_id}"),
        VideoAutoWorkflowStep(name="summarize_transcript", status="completed", reason=None, output_id=f"summary-{source_id}"),
        VideoAutoWorkflowStep(name="create_memory_candidate", status="completed", reason=None, output_id=f"candidate-{source_id}"),
        VideoAutoWorkflowStep(name="publish_memory", status="skipped", reason="auto_publication_disabled"),
    )
    return VideoAutoWorkflowResult(
        status="completed",
        workflow_id=f"video-auto-workflow-{source_id}",
        source_id=source_id,
        project_id="default",
        steps=steps,
        audio_asset_id=f"audio-asset-{source_id}",
        transcript_output_id=f"transcript-{source_id}",
        summary_output_id=f"summary-{source_id}",
        memory_candidate_id=f"candidate-{source_id}",
        summary_provider_status="enabled",
        summary_provider_name="local-summary",
        summary_readiness_reason=None,
        summary_next_step=None,
        auto_publication_status="skipped",
        publication_id=None,
        published_ref=None,
        rollback_ref=None,
        memory_publication="skipped",
        blocked_operations=(),
        error=None,
    )


# ---------------------------------------------------------------------------
# Document text extraction (file / pdf / document)
# ---------------------------------------------------------------------------


def test_pdf_intake_triggers_document_text_extractor_and_completes(tmp_path: Path) -> None:
    store = _store(tmp_path)
    calls: list[str] = []

    def run_extractor(source_id: str) -> SourceContentReadResult:
        calls.append(source_id)
        return _completed_read_result(source_id)

    orchestrator = _make_orchestrator(store, run_document_text_extractor=run_extractor)

    result = orchestrator.execute(
        media_type="application/pdf",
        file_name="report.pdf",
        add_to_knowledge_base=True,
    )

    assert result.status == "accepted"
    assert len(result.items) == 1
    item = result.items[0]
    assert len(calls) == 1, "document text extractor must be triggered once"
    assert calls[0] == item.source_id
    assert item.status == "completed", "successful extractor must upgrade item to completed"
    assert item.content_read_status == "completed"
    assert item.next_step == "library_overview_refresh"
    media_workflow = item.auto_organization["media_auto_workflow"]
    assert media_workflow["workflow_kind"] == "document_text_extraction"
    assert media_workflow["status"] == "completed"
    assert media_workflow["triggered"] is True
    assert media_workflow["read_ref"].endswith(f"content-read-{item.source_id}.json")
    assert media_workflow["char_count"] == 128


def test_pdf_intake_without_extractor_keeps_legacy_needs_extractor(tmp_path: Path) -> None:
    """When run_document_text_extractor is None, behavior must stay backward-compatible."""
    store = _store(tmp_path)
    orchestrator = _make_orchestrator(store, run_document_text_extractor=None)

    result = orchestrator.execute(
        media_type="application/pdf",
        file_name="report.pdf",
        add_to_knowledge_base=True,
    )

    assert result.status == "accepted"
    item = result.items[0]
    assert item.status == "needs_extractor", "legacy status must be preserved when no callable provided"
    assert item.next_step == "await_document_extractor"
    assert "media_auto_workflow" not in item.auto_organization, "no trace when no callable provided"


def test_uploaded_file_is_prepared_before_document_text_extraction(tmp_path: Path) -> None:
    store = _store(tmp_path)
    asset_ref = "crp-ref-default-assets-originals-file-fixture"
    store.write(
        "workbench_original_assets",
        "original-file-fixture",
        {
            "id": "original-file-fixture",
            "asset_ref": asset_ref,
            "sha256": "a" * 64,
            "byte_count": 1,
        },
        expected_revision=None,
    )
    events: list[tuple[str, str, str | None]] = []

    def prepare(source_id: str, original_asset_ref: str) -> None:
        events.append(("prepare", source_id, original_asset_ref))

    def run_extractor(source_id: str) -> SourceContentReadResult:
        events.append(("run", source_id, None))
        return _completed_read_result(source_id)

    orchestrator = _make_orchestrator(
        store,
        prepare_file_source=prepare,
        run_document_text_extractor=run_extractor,
    )
    result = orchestrator.execute(
        media_type="text/plain",
        file_name="notes.txt",
        original_asset_ref=asset_ref,
    )

    item = result.items[0]
    assert events == [
        ("prepare", item.source_id, asset_ref),
        ("run", item.source_id, None),
    ]
    assert item.status == "completed"


def test_uploaded_file_preparation_failure_stops_extractor_with_diagnostic_trace(tmp_path: Path) -> None:
    store = _store(tmp_path)
    asset_ref = "crp-ref-default-assets-originals-file-fixture"
    store.write(
        "workbench_original_assets",
        "original-file-fixture",
        {
            "id": "original-file-fixture",
            "asset_ref": asset_ref,
            "sha256": "a" * 64,
            "byte_count": 1,
        },
        expected_revision=None,
    )
    runs: list[str] = []

    def prepare(_source_id: str, _asset_ref: str) -> None:
        raise ValueError("managed original asset is unavailable: original_file_hash_drift")

    def run_extractor(source_id: str) -> SourceContentReadResult:
        runs.append(source_id)
        return _completed_read_result(source_id)

    orchestrator = _make_orchestrator(
        store,
        prepare_file_source=prepare,
        run_document_text_extractor=run_extractor,
    )
    result = orchestrator.execute(
        media_type="text/markdown",
        file_name="notes.md",
        original_asset_ref=asset_ref,
    )

    item = result.items[0]
    assert runs == []
    assert item.status == "needs_extractor"
    trace = item.auto_organization["media_auto_workflow"]
    assert trace["status"] == "failed"
    assert trace["blocked_reason"] == "file_source_preparation_failed"
    assert trace["error"] == "managed original asset is unavailable: original_file_hash_drift"


# ---------------------------------------------------------------------------
# Image OCR
# ---------------------------------------------------------------------------


def test_image_intake_triggers_ocr_and_completes(tmp_path: Path) -> None:
    store = _store(tmp_path)
    calls: list[str] = []

    def run_ocr(source_id: str) -> MediaProcessingQueueResult:
        calls.append(source_id)
        return _completed_ocr_result(source_id)

    orchestrator = _make_orchestrator(store, run_image_ocr=run_ocr)

    result = orchestrator.execute(
        media_type="image/png",
        file_name="screenshot.png",
        add_to_knowledge_base=True,
    )

    assert result.status == "accepted"
    item = result.items[0]
    assert len(calls) == 1
    assert calls[0] == item.source_id
    assert item.status == "completed"
    assert item.next_step == "library_overview_refresh"
    media_workflow = item.auto_organization["media_auto_workflow"]
    assert media_workflow["workflow_kind"] == "image_ocr"
    assert media_workflow["status"] == "completed"
    assert media_workflow["workflow_id"] == f"media-job-ocr-{item.source_id}"


# ---------------------------------------------------------------------------
# Audio auto workflow
# ---------------------------------------------------------------------------


def test_audio_intake_triggers_audio_auto_workflow_and_completes(tmp_path: Path) -> None:
    store = _store(tmp_path)
    calls: list[tuple[str, str | None]] = []

    def run_audio(source_id: str, audio_asset_id) -> AudioAutoWorkflowResult:
        calls.append((source_id, audio_asset_id))
        return _completed_audio_result(source_id)

    orchestrator = _make_orchestrator(store, run_audio_auto_workflow=run_audio)

    result = orchestrator.execute(
        media_type="audio/mpeg",
        file_name="meeting.mp3",
        add_to_knowledge_base=True,
    )

    assert result.status == "accepted"
    item = result.items[0]
    assert len(calls) == 1, "audio auto workflow must be triggered once"
    assert calls[0][0] == item.source_id
    # audio_asset_id is captured from the capture result and passed through
    assert calls[0][1] == item.auto_organization.get("asset_id")
    assert item.status == "completed"
    assert item.next_step == "library_overview_refresh"
    media_workflow = item.auto_organization["media_auto_workflow"]
    assert media_workflow["workflow_kind"] == "audio_auto_workflow"
    assert media_workflow["status"] == "completed"
    assert media_workflow["workflow_id"] == f"audio-auto-workflow-{item.source_id}"
    assert media_workflow["steps"][0]["name"] == "transcribe_audio"
    assert media_workflow["steps"][0]["status"] == "completed"


def test_audio_intake_blocked_workflow_keeps_needs_asr_status(tmp_path: Path) -> None:
    """When the audio auto workflow returns a blocked status, the orchestrator
    must keep the original needs_asr status so the UI can surface the blocked
    reason instead of marking the item completed.
    """
    store = _store(tmp_path)
    blocked_step = AudioAutoWorkflowStep(
        name="transcribe_audio",
        status="blocked",
        reason="local ASR provider disabled",
        job_id=None,
        output_id=None,
        audio_asset_id="audio-asset-x",
    )
    blocked_result = AudioAutoWorkflowResult(
        status="blocked",
        workflow_id="audio-auto-workflow-x",
        source_id="placeholder",
        project_id="default",
        steps=(blocked_step,),
        audio_asset_id="audio-asset-x",
        transcript_output_id=None,
        transcriber_status="disabled",
        transcriber_model_profile="",
        transcriber_model_name="",
        audio_asset_status="available",
        readiness_reason="local ASR provider disabled",
        next_step="enable_local_asr_provider",
        memory_publication="not_started",
        blocked_operations=("audio_asset_transcription",),
        error="local ASR provider disabled",
    )

    def run_audio(source_id: str, audio_asset_id) -> AudioAutoWorkflowResult:
        return blocked_result

    orchestrator = _make_orchestrator(store, run_audio_auto_workflow=run_audio)

    result = orchestrator.execute(
        media_type="audio/mpeg",
        file_name="meeting.mp3",
        add_to_knowledge_base=True,
    )

    assert result.status == "accepted"
    item = result.items[0]
    assert item.status == "needs_asr", "blocked workflow must keep needs_asr status"
    assert item.next_step == "await_asr_provider", "blocked workflow must keep await_asr_provider next_step"
    media_workflow = item.auto_organization["media_auto_workflow"]
    assert media_workflow["status"] == "blocked"
    assert media_workflow["blocked_operations"] == ["audio_asset_transcription"]
    assert media_workflow["next_step"] == "enable_local_asr_provider"
    assert media_workflow["error"] == "local ASR provider disabled"


# ---------------------------------------------------------------------------
# Video auto workflow
# ---------------------------------------------------------------------------


def test_video_intake_triggers_video_auto_workflow_and_completes(tmp_path: Path) -> None:
    store = _store(tmp_path)
    calls: list[str] = []

    def run_video(source_id: str) -> VideoAutoWorkflowResult:
        calls.append(source_id)
        return _completed_video_result(source_id)

    orchestrator = _make_orchestrator(store, run_video_auto_workflow=run_video)

    result = orchestrator.execute(
        media_type="video/mp4",
        file_name="clip.mp4",
        add_to_knowledge_base=True,
    )

    assert result.status == "accepted"
    item = result.items[0]
    assert len(calls) == 1
    assert calls[0] == item.source_id
    assert item.status == "completed"
    assert item.next_step == "library_overview_refresh"
    media_workflow = item.auto_organization["media_auto_workflow"]
    assert media_workflow["workflow_kind"] == "video_auto_workflow"
    assert media_workflow["status"] == "completed"
    step_names = [step["name"] for step in media_workflow["steps"]]
    assert step_names == [
        "extract_audio",
        "transcribe_audio",
        "summarize_transcript",
        "create_memory_candidate",
        "publish_memory",
    ]
    assert media_workflow["steps"][4]["status"] == "skipped"


def test_uploaded_video_is_prepared_before_auto_workflow_runs(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write(
        "workbench_original_assets",
        "original-video-fixture",
        {
            "id": "original-video-fixture",
            "asset_ref": "crp-ref-default-assets-originals-fixture",
            "sha256": "a" * 64,
            "byte_count": 1,
        },
        expected_revision=None,
    )
    events: list[tuple[str, str, str | None]] = []

    def prepare(source_id: str, asset_ref: str) -> None:
        events.append(("prepare", source_id, asset_ref))

    def run_video(source_id: str) -> VideoAutoWorkflowResult:
        events.append(("run", source_id, None))
        return _completed_video_result(source_id)

    orchestrator = _make_orchestrator(
        store,
        prepare_video_source=prepare,
        run_video_auto_workflow=run_video,
    )
    result = orchestrator.execute(
        media_type="video/mp4",
        file_name="long-session.mp4",
        original_asset_ref="crp-ref-default-assets-originals-fixture",
    )

    item = result.items[0]
    assert events == [
        ("prepare", item.source_id, "crp-ref-default-assets-originals-fixture"),
        ("run", item.source_id, None),
    ]


def test_uploaded_video_preparation_failure_stops_workflow_with_diagnostic_trace(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write(
        "workbench_original_assets",
        "original-video-fixture",
        {
            "id": "original-video-fixture",
            "asset_ref": "crp-ref-default-assets-originals-fixture",
            "sha256": "a" * 64,
            "byte_count": 1,
        },
        expected_revision=None,
    )
    runs: list[str] = []

    def prepare(_source_id: str, _asset_ref: str) -> None:
        raise ValueError("stored original video escapes the active Vault")

    def run_video(source_id: str) -> VideoAutoWorkflowResult:
        runs.append(source_id)
        return _completed_video_result(source_id)

    orchestrator = _make_orchestrator(
        store,
        prepare_video_source=prepare,
        run_video_auto_workflow=run_video,
    )
    result = orchestrator.execute(
        media_type="video/mp4",
        file_name="long-session.mp4",
        original_asset_ref="crp-ref-default-assets-originals-fixture",
    )

    item = result.items[0]
    assert runs == []
    assert item.status == "needs_video_workflow"
    trace = item.auto_organization["media_auto_workflow"]
    assert trace["status"] == "failed"
    assert trace["blocked_reason"] == "video_source_preparation_failed"
    assert trace["error"] == "stored original video escapes the active Vault"


def test_video_intake_workflow_exception_degrades_to_needs_video_workflow(tmp_path: Path) -> None:
    """When the video auto workflow callable raises, the orchestrator must not
    crash; it must keep the original needs_video_workflow status and record the
    failure reason in the media_auto_workflow trace.
    """
    store = _store(tmp_path)

    def run_video(source_id: str) -> VideoAutoWorkflowResult:
        raise RuntimeError("video audio extraction failed")

    orchestrator = _make_orchestrator(store, run_video_auto_workflow=run_video)

    result = orchestrator.execute(
        media_type="video/mp4",
        file_name="clip.mp4",
        add_to_knowledge_base=True,
    )

    assert result.status == "accepted"
    item = result.items[0]
    assert item.status == "needs_video_workflow", "exception must degrade to needs_video_workflow"
    assert item.next_step == "await_video_auto_workflow"
    media_workflow = item.auto_organization["media_auto_workflow"]
    assert media_workflow["status"] == "failed"
    assert media_workflow["blocked_reason"] == "media_workflow_exception"
    assert "video audio extraction failed" in str(media_workflow["error"])


# ---------------------------------------------------------------------------
# Privacy boundary
# ---------------------------------------------------------------------------


def test_media_workflow_response_contains_no_secret_or_local_path(tmp_path: Path) -> None:
    """The serialized response after triggering a video auto workflow must not
    leak API keys, cookies, authorization headers, passwords or local absolute
    paths even when the workflow ran successfully.
    """
    store = _store(tmp_path)

    def run_video(source_id: str) -> VideoAutoWorkflowResult:
        return _completed_video_result(source_id)

    orchestrator = _make_orchestrator(store, run_video_auto_workflow=run_video)

    result = orchestrator.execute(
        media_type="video/mp4",
        file_name="clip.mp4",
        add_to_knowledge_base=True,
    )

    payload = serialize_workbench_auto_intake_result(result)
    encoded = str(payload)
    assert not re.search(r"sk-[A-Za-z0-9_-]{8,}", encoded), "no API key material in media workflow response"
    assert not re.search(r"[A-Za-z]:\\\\", encoded), "no Windows absolute path in media workflow response"
    assert "password=" not in encoded.lower()
    assert "cookie:" not in encoded.lower()
    assert "authorization:" not in encoded.lower()
    # media_auto_workflow trace must be present
    items = payload.get("items", [])
    assert len(items) == 1
    auto_org = items[0].get("auto_organization", {})
    assert "media_auto_workflow" in auto_org
    assert auto_org["media_auto_workflow"]["workflow_kind"] == "video_auto_workflow"


def test_media_workflow_trace_in_response_has_no_local_path(tmp_path: Path) -> None:
    """Even if the underlying workflow result carried a local path in some
    field, the orchestrator's _summarize_media_workflow_result only copies safe
    fields (status, workflow_id, steps name+status+reason, next_step,
    blocked_operations, error) so no local path reaches the response.
    """
    store = _store(tmp_path)

    @dataclass(frozen=True, slots=True)
    class _LeakyStep:
        name: str
        status: str
        reason: str | None = None
        # The summarizer must NOT copy arbitrary fields like local_path
        local_path: str | None = None

    @dataclass(frozen=True, slots=True)
    class _LeakyResult:
        status: str
        workflow_id: str
        source_id: str
        project_id: str
        steps: tuple
        audio_asset_id: str | None
        transcript_output_id: str | None
        summary_output_id: str | None
        memory_candidate_id: str | None
        summary_provider_status: str
        summary_provider_name: str | None
        summary_readiness_reason: str | None
        summary_next_step: str | None
        auto_publication_status: str | None
        publication_id: str | None
        published_ref: str | None
        rollback_ref: str | None
        memory_publication: str
        blocked_operations: tuple
        error: str | None

    def run_video(source_id: str):
        leaky_step = _LeakyStep(
            name="extract_audio",
            status="completed",
            reason=None,
            local_path="C:\\Users\\secret\\Videos\\clip.mp4",
        )
        return _LeakyResult(
            status="completed",
            workflow_id=f"video-auto-workflow-{source_id}",
            source_id=source_id,
            project_id="default",
            steps=(leaky_step,),
            audio_asset_id=f"audio-asset-{source_id}",
            transcript_output_id=f"transcript-{source_id}",
            summary_output_id=f"summary-{source_id}",
            memory_candidate_id=f"candidate-{source_id}",
            summary_provider_status="enabled",
            summary_provider_name="local-summary",
            summary_readiness_reason=None,
            summary_next_step=None,
            auto_publication_status="skipped",
            publication_id=None,
            published_ref=None,
            rollback_ref=None,
            memory_publication="skipped",
            blocked_operations=(),
            error=None,
        )

    orchestrator = _make_orchestrator(store, run_video_auto_workflow=run_video)

    result = orchestrator.execute(
        media_type="video/mp4",
        file_name="clip.mp4",
        add_to_knowledge_base=True,
    )

    payload = serialize_workbench_auto_intake_result(result)
    encoded = str(payload)
    # The local path "C:\Users\secret\Videos\clip.mp4" must NOT appear. We
    # check for the actual path pattern rather than the word "secret" because
    # the privacy_boundary documentation legitimately contains "secrets".
    assert "C:\\\\Users" not in encoded, "local path from leaky step must not propagate to response"
    assert "Videos\\clip.mp4" not in encoded, "local path filename must not leak"
    # The step trace should only have name/status/reason — local_path field
    # from the leaky step must be dropped by the summarizer.
    step_trace = payload["items"][0]["auto_organization"]["media_auto_workflow"]["steps"][0]
    assert set(step_trace.keys()) == {"name", "status", "reason"}, "only safe fields must be copied"
    assert step_trace["name"] == "extract_audio"
    assert step_trace["status"] == "completed"
