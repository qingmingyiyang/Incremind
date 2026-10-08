from __future__ import annotations

import json
import subprocess
from pathlib import Path

from core.ingestion_core import ObjectStoreSourceRegistrar, SourceSubmission
from core.product_core import (
    GetTranscriptSummarySettings,
    SaveTranscriptSummarySettings,
    SummarizeTranscriptOutput,
    TranscriptSummaryError,
    serialize_transcript_summary_result,
    serialize_transcript_summary_settings,
)
from core.product_core.transcript_summary_adapter import BUILTIN_LOCAL_SUMMARY_COMMAND
from backend.video_summary.infrastructure.local_semantic_summary import build_semantic_extractive_summary
from core.storage_provider import JsonObjectStore


def _store(tmp_path: Path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def _video_source(object_store: JsonObjectStore) -> dict[str, object]:
    return dict(
        ObjectStoreSourceRegistrar(object_store).register(
            SourceSubmission(
                kind="video",
                title="Video transcript",
                display_name="video.mp4",
                media_type="video/mp4",
                size_bytes=2048,
                video_reference="bilibili/BV1xx411c7mD/p1",
                duration_ms=3000,
            )
        )
    )


def _transcript_output(object_store: JsonObjectStore, source_id: str) -> str:
    output_id = f"media-output-transcript-{source_id}"
    job_id = f"media-job-transcript-audio-track-{source_id}"
    object_store.write(
        "media_processing_jobs",
        job_id,
        {
            "schema_version": "1.0.0",
            "id": job_id,
            "source_id": source_id,
            "source_type": "video",
            "required_capability": "audio_asset_transcription",
            "status": "completed",
            "disabled_reason": None,
            "input_refs": [],
            "expected_output_refs": [],
            "adapter_contract": {},
            "error": None,
            "activity_refs": [],
            "output_refs": [f"crp://default/media-processing-outputs/{output_id}.json"],
            "created_at": "2026-07-02T04:00:00+08:00",
            "updated_at": "2026-07-02T04:00:00+08:00",
        },
        expected_revision=None,
    )
    object_store.write(
        "media_processing_outputs",
        output_id,
        {
            "schema_version": "1.0.0",
            "id": output_id,
            "job_id": job_id,
            "source_id": source_id,
            "source_type": "video",
            "output_kind": "transcript",
            "status": "completed",
            "provider": "local-faster-whisper",
            "language": "zh",
            "segment_count": 2,
            "char_count": 12,
            "byte_count": 36,
            "preview": "第一段 第二段",
            "text": "第一段转写。\n第二段转写。",
            "segments": [
                {"start_seconds": 0.0, "end_seconds": 1.5, "text": "第一段转写。"},
                {"start_seconds": 1.5, "end_seconds": 3.0, "text": "第二段转写。"},
            ],
            "metadata": {"remote_processing": False, "memory_publication": "not_started"},
            "memory_publication": "not_started",
            "created_at": "2026-07-02T04:00:00+08:00",
            "ref": f"crp://default/media-processing-outputs/{output_id}.json",
        },
        expected_revision=None,
    )
    return output_id


def _summary_payload() -> dict[str, object]:
    return {
        "title": "视频总结",
        "content_type": "教程",
        "thirty_second_summary": "这个视频说明了如何把转写整理成结构化总结。",
        "one_sentence_summary": "转写可以进入结构化总结输出。",
        "core_problem": "如何从转写生成可回看的总结。",
        "chapters": [
            {
                "id": "chapter-1",
                "title": "转写到总结",
                "start_seconds": 0,
                "end_seconds": 3,
                "summary": "说明转写可进入总结。",
                "key_points": ["读取 transcript", "输出 summary"],
                "evidence_ids": ["ev-1"],
            }
        ],
        "key_takeaways": ["总结输出保持待审边界"],
        "detailed_notes": ["本轮只生成输出，不写 Memory。"],
        "evidence": [
            {
                "id": "ev-1",
                "statement": "转写进入总结输出",
                "quote": "第一段转写。",
                "start_seconds": 0,
                "end_seconds": 1.5,
                "confidence": "high",
            }
        ],
        "people": [],
        "terms": [{"name": "Transcript", "description": "转写文本", "evidence_ids": ["ev-1"]}],
        "examples": [],
        "data_points": [],
        "viewpoints": [],
        "action_items": [],
        "relations": [],
        "open_questions": [],
        "visual_attention": {"importance": "unknown", "reason": "未读取画面", "signals": []},
    }


def test_transcript_summary_settings_are_default_off(tmp_path: Path) -> None:
    payload = serialize_transcript_summary_settings(GetTranscriptSummarySettings(_store(tmp_path)).execute())

    assert payload["status"] == "disabled"
    assert payload["enabled"] is False
    assert payload["remote_processing"] is False
    assert payload["memory_publication"] == "not_started"
    assert payload["command"] == [BUILTIN_LOCAL_SUMMARY_COMMAND]
    assert payload["summary_method"] == "semantic_extractive"


def test_transcript_summary_settings_require_confirmation_and_executable(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    executable = tmp_path / "summary.exe"
    executable.write_text("", encoding="utf-8")
    writer = SaveTranscriptSummarySettings(object_store)

    try:
        writer.execute(enabled=True, command=(str(executable),), confirm_enable=False)
    except TranscriptSummaryError as error:
        assert str(error) == "enabling transcript summary provider requires confirm_enable=true"
    else:
        raise AssertionError("expected explicit enable guard")

    settings = writer.execute(
        enabled=True,
        command=(str(executable),),
        provider_name="local-summary",
        confirm_enable=True,
    )

    assert settings.status == "ready"
    assert settings.enabled is True
    assert settings.command == (str(executable),)


def test_summarize_transcript_output_writes_structured_summary_without_memory_publication(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    source = _video_source(object_store)
    transcript_output_id = _transcript_output(object_store, str(source["id"]))
    executable = tmp_path / "summary.exe"
    executable.write_text("", encoding="utf-8")
    settings = SaveTranscriptSummarySettings(object_store).execute(
        enabled=True,
        command=(str(executable), "--json"),
        confirm_enable=True,
    )
    captured: dict[str, object] = {}

    def runner(command: list[str] | tuple[str, ...], stdin_text: str, timeout_seconds: float) -> subprocess.CompletedProcess[str]:
        captured["command"] = tuple(command)
        captured["stdin"] = json.loads(stdin_text)
        captured["timeout"] = timeout_seconds
        return subprocess.CompletedProcess(
            args=list(command),
            returncode=0,
            stdout=json.dumps(_summary_payload(), ensure_ascii=False),
            stderr="",
        )

    result = SummarizeTranscriptOutput(object_store, runner=runner).execute(transcript_output_id=transcript_output_id)
    payload = serialize_transcript_summary_result(result)
    output = object_store.read("media_processing_outputs", str(payload["output_id"]))
    job = object_store.read("media_processing_jobs", str(payload["job_id"]))
    updated_source = object_store.read("sources", str(source["id"]))

    assert payload["status"] == "completed"
    assert payload["title"] == "视频总结"
    assert payload["chapter_count"] == 1
    assert payload["evidence_count"] == 1
    assert payload["creates_memory_candidate"] is False
    assert payload["publishes_memory"] is False
    assert captured["command"] == settings.command
    assert captured["timeout"] == settings.timeout_seconds
    assert captured["stdin"]["text"] == "第一段转写。\n第二段转写。"
    assert captured["stdin"]["required_summary_schema"] == "old-replay-summary-payload-v1"
    assert output is not None
    assert output["output_kind"] == "summary"
    assert output["summary_data"]["core_problem"] == "如何从转写生成可回看的总结。"
    assert output["markdown"].startswith("# 视频总结")
    assert output["metadata"]["transcript_output_id"] == transcript_output_id
    assert output["metadata"]["auto_memory_candidate"] is False
    assert output["memory_publication"] == "not_started"
    assert job is not None
    assert job["status"] == "completed"
    assert job["required_capability"] == "transcript_summary"
    assert updated_source is not None
    extraction = updated_source["metadata"]["audio_track_extraction"]
    assert extraction["summary_state"] == "completed"
    assert extraction["memory_publication"] == "not_started"


def test_summarize_transcript_output_records_failed_job_without_output(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    source = _video_source(object_store)
    transcript_output_id = _transcript_output(object_store, str(source["id"]))
    executable = tmp_path / "summary.exe"
    executable.write_text("", encoding="utf-8")
    SaveTranscriptSummarySettings(object_store).execute(
        enabled=True,
        command=(str(executable),),
        confirm_enable=True,
    )

    def runner(command: list[str] | tuple[str, ...], stdin_text: str, timeout_seconds: float) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=list(command), returncode=1, stdout="", stderr="summary failed")

    result = SummarizeTranscriptOutput(object_store, runner=runner).execute(transcript_output_id=transcript_output_id)
    job = object_store.read("media_processing_jobs", result.job_id)
    output = object_store.read("media_processing_outputs", result.output_id)

    assert result.status == "failed"
    assert result.error == "summary failed"
    assert result.creates_memory_candidate is False
    assert result.publishes_memory is False
    assert job is not None
    assert job["status"] == "failed"
    assert output is None


def test_builtin_summary_reports_missing_model_without_running_provider(tmp_path: Path, monkeypatch) -> None:
    object_store = _store(tmp_path)
    source = _video_source(object_store)
    transcript_output_id = _transcript_output(object_store, str(source["id"]))
    monkeypatch.setenv("CHRIPTMAS_APP_ROOT", str(tmp_path))
    settings = SaveTranscriptSummarySettings(object_store).execute(
        enabled=True,
        command=(),
        confirm_enable=True,
    )

    assert settings.command == (BUILTIN_LOCAL_SUMMARY_COMMAND,)
    assert settings.status == "model_missing"
    try:
        SummarizeTranscriptOutput(object_store).execute(transcript_output_id=transcript_output_id)
    except TranscriptSummaryError as error:
        assert str(error) == "local semantic summary model is missing"
    else:
        raise AssertionError("expected missing model guard")


def test_builtin_summary_creates_four_review_candidates_and_replays_without_duplicates(
    tmp_path: Path, monkeypatch,
) -> None:
    object_store = _store(tmp_path)
    source = _video_source(object_store)
    transcript_output_id = _transcript_output(object_store, str(source["id"]))
    model_dir = tmp_path / "data" / "models" / "fastembed" / "fast-bge-small-zh-v1.5"
    model_dir.mkdir(parents=True)
    for name in ("config.json", "model_optimized.onnx", "tokenizer.json", "tokenizer_config.json"):
        (model_dir / name).write_bytes(b"model")
    monkeypatch.setenv("CHRIPTMAS_APP_ROOT", str(tmp_path))
    SaveTranscriptSummarySettings(object_store).execute(enabled=True, command=(), confirm_enable=True)
    calls = 0

    def runner(command, stdin_text, timeout_seconds):
        nonlocal calls
        calls += 1
        prompt = json.loads(stdin_text)
        result = build_semantic_extractive_summary(
            prompt,
            embed=lambda texts: [[float(index + 1), 1.0] for index, _text in enumerate(texts)],
        )
        return subprocess.CompletedProcess(command, 0, json.dumps(result, ensure_ascii=False), "")

    first = SummarizeTranscriptOutput(object_store, runner=runner).execute(
        transcript_output_id=transcript_output_id
    )
    second = SummarizeTranscriptOutput(
        object_store,
        runner=lambda *_args: (_ for _ in ()).throw(AssertionError("replay ran provider")),
    ).execute(transcript_output_id=transcript_output_id)

    assert first.status == "completed"
    assert first.creates_memory_candidate is True
    assert len(first.candidate_ids) == 4
    assert second.candidate_ids == first.candidate_ids
    assert calls == 1
    candidates = [object_store.read("memory_candidates", candidate_id) for candidate_id in first.candidate_ids]
    assert all(candidate is not None and candidate["status"] == "pending_review" for candidate in candidates)
    assert all(candidate["review"]["requires_user_confirmation"] is True for candidate in candidates)
    assert object_store.list("memory_atoms") == ()
    assert object_store.list("memory_scenarios") == ()
    assert object_store.list("memory_series_memory") == ()

    (model_dir / "config.json").write_bytes(b"changed-model-config")
    try:
        SummarizeTranscriptOutput(object_store, runner=runner).execute(
            transcript_output_id=transcript_output_id
        )
    except TranscriptSummaryError as error:
        assert str(error) == "transcript or local summary model changed after summary completion"
    else:
        raise AssertionError("expected summary model drift guard")
