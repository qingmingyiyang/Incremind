from __future__ import annotations

import json
import subprocess
from pathlib import Path

from core.ingestion_core import ObjectStoreSourceRegistrar, SourceSubmission
from core.memory_core import ObjectStoreMemoryCandidateRepository
from core.product_core import (
    AuthorizedBilibiliDownloader,
    AuthorizeLocalVideoFileForSource,
    BilibiliVideoLinkResolver,
    CreateMemoryCandidateFromSourceOutput,
    ExtractAudioTrackFromAuthorizedVideoSource,
    LinkedVideoDownloadPlanner,
    SaveAudioAssetTranscriberSettings,
    SaveBilibiliDownloaderSettings,
    SaveTranscriptSummarySettings,
    SaveVideoAudioExtractorSettings,
    SummarizeTranscriptOutput,
    TranscribeGeneratedAudioAsset,
    serialize_authorized_bilibili_download_result,
    serialize_audio_asset_transcription_result,
    serialize_transcript_summary_result,
    serialize_video_audio_extraction_result,
)
from core.storage_provider import JsonObjectStore
from tests.rebuild.memory_candidate_saga_review_testlib import (
    publish_staging_user_confirmed,
    review_candidate_to_staging,
    saga_records,
)


def _store(tmp_path: Path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def _executable(path: Path) -> str:
    path.write_text("", encoding="utf-8")
    return str(path)


def _summary_payload() -> dict[str, object]:
    return {
        "title": "产品记忆工作台视频总结",
        "content_type": "产品说明",
        "thirty_second_summary": "视频说明了个人 AI 记忆工作台如何从素材进入四层记忆。",
        "one_sentence_summary": "视频总结输出可以先进入待审候选，再由用户确认发布 Atom Memory。",
        "core_problem": "如何把视频内容沉淀为可追溯、可审查的个人记忆。",
        "chapters": [
            {
                "id": "chapter-1",
                "title": "视频到记忆",
                "start_seconds": 0,
                "end_seconds": 8,
                "summary": "讲解视频下载、音频抽取、转写、总结和候选生成。",
                "key_points": ["下载视频", "转写总结", "人工发布"],
                "evidence_ids": ["ev-1"],
            }
        ],
        "key_takeaways": ["长期 Memory 发布必须经过用户确认"],
        "detailed_notes": ["summary output 本身不自动写入长期 Memory。"],
        "evidence": [
            {
                "id": "ev-1",
                "statement": "视频链路需要显式确认后发布",
                "quote": "先生成总结，再进入候选。",
                "start_seconds": 0,
                "end_seconds": 4,
                "confidence": "high",
            }
        ],
        "people": [],
        "terms": [{"name": "四层记忆", "description": "Atom、Scenario、Series 和项目记忆边界", "evidence_ids": ["ev-1"]}],
        "examples": [],
        "data_points": [],
        "viewpoints": [],
        "action_items": [],
        "relations": [],
        "open_questions": [],
        "visual_attention": {"importance": "unknown", "reason": "本轮只验证音频总结链路", "signals": []},
    }


def test_video_download_to_summary_candidate_review_and_atom_publication_smoke(tmp_path: Path) -> None:
    object_store = _store(tmp_path)

    downloader_settings = SaveBilibiliDownloaderSettings(object_store).execute(
        enabled=True,
        output_root=str(tmp_path / "downloads"),
        cookie_mode="none",
        confirm_enable=True,
    )
    resolution = BilibiliVideoLinkResolver().resolve(
        url="https://www.bilibili.com/video/BV1abcDEF234?p=1"
    )
    plan = LinkedVideoDownloadPlanner().create_dry_run_plan(
        resolution=resolution,
        video_id="BV1abcDEF234",
    )

    def download_runner(command: list[str] | tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        output_index = tuple(command).index("--output") + 1
        output_path = Path(tuple(command)[output_index].replace("%(ext)s", "mp4"))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"fake downloaded video")
        return subprocess.CompletedProcess(args=list(command), returncode=0, stdout="", stderr="")

    download = AuthorizedBilibiliDownloader(runner=download_runner).execute(
        plan=plan,
        settings=downloader_settings,
    )
    download_payload = serialize_authorized_bilibili_download_result(download)

    assert download_payload["status"] == "completed"
    assert download_payload["downloads_video"] is True
    assert download_payload["starts_audio_extraction"] is False
    assert download_payload["starts_asr"] is False
    assert download_payload["starts_summary"] is False
    assert download_payload["creates_memory_candidate"] is False
    assert download_payload["publishes_memory"] is False

    source = dict(
        ObjectStoreSourceRegistrar(object_store).register(
            SourceSubmission(
                kind="video",
                title="Downloaded Bilibili video",
                display_name="BV1abcDEF234.mp4",
                media_type="video/mp4",
                size_bytes=Path(str(download.output_file)).stat().st_size,
                video_reference="bilibili/BV1abcDEF234/p1",
                duration_ms=None,
            )
        )
    )
    AuthorizeLocalVideoFileForSource(object_store).execute(
        source_id=str(source["id"]),
        file_path=str(download.output_file),
    )

    ffmpeg = _executable(tmp_path / "ffmpeg.exe")
    ffprobe = _executable(tmp_path / "ffprobe.exe")
    audio_settings = SaveVideoAudioExtractorSettings(object_store).execute(
        enabled=True,
        ffmpeg_path=ffmpeg,
        ffprobe_path=ffprobe,
        output_root=str(tmp_path / "audio"),
        confirm_enable=True,
    )

    def audio_runner(command: list[str] | tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        clean = tuple(command)
        if clean[0] == audio_settings.ffprobe_path:
            return subprocess.CompletedProcess(args=list(clean), returncode=0, stdout="8.0\n", stderr="")
        output_path = Path(clean[-1])
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"wav")
        return subprocess.CompletedProcess(args=list(clean), returncode=0, stdout="", stderr="")

    audio = ExtractAudioTrackFromAuthorizedVideoSource(object_store, runner=audio_runner).execute(
        source_id=str(source["id"])
    )
    audio_payload = serialize_video_audio_extraction_result(audio)

    assert audio_payload["status"] == "completed"
    assert audio_payload["starts_asr"] is False
    assert audio_payload["starts_summary"] is False
    assert audio_payload["creates_memory_candidate"] is False
    assert audio_payload["publishes_memory"] is False

    asr_executable = _executable(tmp_path / "asr.exe")
    asr_settings = SaveAudioAssetTranscriberSettings(object_store).execute(
        enabled=True,
        command=(asr_executable, "--json", "{audio_path}"),
        confirm_enable=True,
    )

    def asr_runner(command: list[str] | tuple[str, ...], timeout_seconds: float) -> subprocess.CompletedProcess[str]:
        clean = tuple(command)
        assert timeout_seconds == asr_settings.timeout_seconds
        assert Path(clean[-1]).exists()
        return subprocess.CompletedProcess(
            args=list(clean),
            returncode=0,
            stdout=json.dumps(
                {
                    "language": "zh",
                    "segments": [
                        {"start_seconds": 0.0, "end_seconds": 4.0, "text": "先生成总结，再进入候选。"},
                        {"start_seconds": 4.0, "end_seconds": 8.0, "text": "长期记忆需要用户确认发布。"},
                    ],
                },
                ensure_ascii=False,
            ),
            stderr="",
        )

    transcript = TranscribeGeneratedAudioAsset(object_store, runner=asr_runner).execute(
        audio_asset_id=str(audio.audio_asset_id)
    )
    transcript_payload = serialize_audio_asset_transcription_result(transcript)

    assert transcript_payload["status"] == "completed"
    assert transcript_payload["starts_summary"] is False
    assert transcript_payload["creates_memory_candidate"] is False
    assert transcript_payload["publishes_memory"] is False

    summary_executable = _executable(tmp_path / "summary.exe")
    summary_settings = SaveTranscriptSummarySettings(object_store).execute(
        enabled=True,
        command=(summary_executable, "--json"),
        confirm_enable=True,
    )
    summary_stdin: dict[str, object] = {}

    def summary_runner(
        command: list[str] | tuple[str, ...],
        stdin_text: str,
        timeout_seconds: float,
    ) -> subprocess.CompletedProcess[str]:
        assert tuple(command) == summary_settings.command
        assert timeout_seconds == summary_settings.timeout_seconds
        summary_stdin.update(json.loads(stdin_text))
        return subprocess.CompletedProcess(
            args=list(command),
            returncode=0,
            stdout=json.dumps(_summary_payload(), ensure_ascii=False),
            stderr="",
        )

    summary = SummarizeTranscriptOutput(object_store, runner=summary_runner).execute(
        transcript_output_id=str(transcript.output_id)
    )
    summary_payload = serialize_transcript_summary_result(summary)

    assert summary_payload["status"] == "completed"
    assert summary_payload["creates_memory_candidate"] is False
    assert summary_payload["publishes_memory"] is False
    assert summary_stdin["required_summary_schema"] == "old-replay-summary-payload-v1"
    assert "先生成总结，再进入候选。" in str(summary_stdin["text"])

    candidate_result = CreateMemoryCandidateFromSourceOutput(
        object_store,
        now="2026-07-02T06:05:00+08:00",
    ).execute_from_media_output(
        output_id=str(summary.output_id),
        project_id="project-alpha",
        proposed_content="视频总结确认：长期 Memory 发布必须经过用户确认。",
        target_layer="atom",
        candidate_type="answer_summary",
        created_at="2026-07-02T06:05:00+08:00",
    )
    candidate_repo = ObjectStoreMemoryCandidateRepository(object_store)
    candidate = candidate_repo.get(candidate_result.candidate_id)
    summary_output = object_store.read("media_processing_outputs", str(summary.output_id))

    assert candidate_result.status == "candidate_created"
    assert candidate is not None
    assert candidate["status"] == "pending_review"
    assert candidate["source_refs"][0]["locator"] == "media:summary"
    assert candidate["review"]["auto_promote_allowed"] is False
    assert summary_output is not None
    assert summary_output["memory_publication"] == "candidate_created"
    assert not (tmp_path / ".rebuild-data" / "objects" / "default" / "memory_atoms").exists()

    operation = review_candidate_to_staging(
        object_store,
        tmp_path,
        candidate_result.candidate_id,
        review_reason="用户确认视频 summary output 可作为草稿 Atom。",
        reviewed_at="2026-07-02T06:06:00+08:00",
        tags=("video", "summary", "workflow"),
    )
    records = saga_records(tmp_path)
    staged_atom = records.read("staging_atoms", operation.evidence.draft_id)

    assert operation.state == "finalized"
    assert staged_atom is not None
    assert staged_atom.payload["content"] == "视频总结确认：长期 Memory 发布必须经过用户确认。"
    assert staged_atom.payload["source_refs"][0]["locator"] == "media:summary"
    assert records.list("memory_atoms") == ()
    assert not (tmp_path / ".rebuild-data" / "objects" / "default" / "memory_atoms").exists()

    published = publish_staging_user_confirmed(
        tmp_path,
        layer="atom",
        staged_id=operation.evidence.draft_id,
        published_at="2026-07-02T06:07:00+08:00",
    )
    long_term_atom = records.read("memory_atoms", operation.evidence.draft_id)
    publication = records.read("memory_publications", published.publication_id)

    assert long_term_atom is not None
    assert long_term_atom.payload["trust_status"] == "user_confirmed"
    assert publication is not None
    assert publication.payload["status"] == "published"
    assert publication.payload["source_refs"][0]["locator"] == "media:summary"
    assert records.read("staging_atoms", operation.evidence.draft_id) is None
    assert not (tmp_path / "library").exists()
