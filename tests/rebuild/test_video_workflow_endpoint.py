from __future__ import annotations

from core.product_core import (
    AudioAssetTranscriptionResult,
    AuthorizedBilibiliDownloadResult,
    DownloadedVideoSourceRegistrationResult,
    BilibiliVideoLinkResolver,
    BilibiliDownloaderSettings,
    LinkedVideoDownloadPlanner,
    ServeAudioAssetTranscriptionEndpoint,
    ServeAuthorizedBilibiliDownloadEndpoint,
    ServeBilibiliVideoDownloadPlanEndpoint,
    ServeTranscriptSummaryEndpoint,
    ServeVideoAudioExtractionEndpoint,
    serialize_linked_video_download_plan,
    TranscriptSummaryResult,
    VideoAudioExtractionResult,
)


def test_bilibili_video_download_plan_endpoint_creates_dry_run_plan_without_download() -> None:
    endpoint = ServeBilibiliVideoDownloadPlanEndpoint()

    response = endpoint.execute(
        method="POST",
        path="/api/rebuild/video-links/bilibili/download-plan",
        body={"url": "https://www.bilibili.com/video/BV1abcDEF234?p=2"},
        resolve_link=BilibiliVideoLinkResolver().resolve,
        create_plan=LinkedVideoDownloadPlanner().create_dry_run_plan,
    )

    assert response.status_code == 200
    assert response.body["status"] == "planned"
    assert response.body["video_id"] == "BV1abcDEF234_p2"
    assert response.body["downloads_video"] is False
    assert response.body["starts_audio_extraction"] is False
    assert response.body["starts_asr"] is False
    assert response.body["starts_summary"] is False
    assert response.body["creates_memory_candidate"] is False
    assert response.body["publishes_memory"] is False


def test_authorized_bilibili_download_endpoint_requires_confirmation() -> None:
    response = ServeAuthorizedBilibiliDownloadEndpoint().execute(
        method="POST",
        path="/api/rebuild/video-links/bilibili/authorized-download",
        body={"plan": _download_plan_payload(), "settings": _downloader_settings_payload()},
        download_video=lambda **_: _download_result(),
        trusted_settings=_trusted_downloader_settings(),
    )

    assert response.status_code == 400
    assert response.body["reason"] == "confirm_download=true is required"
    assert response.body["progression_mode"] == "ask"
    assert response.body["progression_reason"] == "external_download_writes_file"


def test_authorized_bilibili_download_endpoint_rejects_ui_supplied_command() -> None:
    response = ServeAuthorizedBilibiliDownloadEndpoint().execute(
        method="POST",
        path="/api/rebuild/video-links/bilibili/authorized-download",
        body={
            "command": ["yt-dlp"],
            "confirm_download": True,
            "plan": _download_plan_payload(),
            "settings": _downloader_settings_payload(),
        },
        download_video=lambda **_: _download_result(),
        trusted_settings=_trusted_downloader_settings(),
    )

    assert response.status_code == 400
    assert response.body["reason"] == "Bilibili authorized download endpoint does not accept provider command"


def test_authorized_bilibili_download_endpoint_runs_from_accepted_dry_run_plan() -> None:
    captured: dict[str, object] = {}

    def download_video(**kwargs: object) -> AuthorizedBilibiliDownloadResult:
        captured.update(kwargs)
        return _download_result()

    response = ServeAuthorizedBilibiliDownloadEndpoint().execute(
        method="POST",
        path="/api/rebuild/video-links/bilibili/authorized-download",
        body={
            "confirm_download": True,
            "plan": _download_plan_payload(),
            "settings": _downloader_settings_payload(),
        },
        download_video=download_video,
        trusted_settings=_trusted_downloader_settings(),
    )

    assert response.status_code == 200
    assert captured["plan"].mode == "dry_run"
    assert captured["settings"].enabled is True
    assert response.body["status"] == "completed"
    assert response.body["downloads_video"] is True
    assert response.body["starts_audio_extraction"] is False
    assert response.body["starts_asr"] is False
    assert response.body["starts_summary"] is False
    assert response.body["creates_memory_candidate"] is False
    assert response.body["publishes_memory"] is False


def test_authorized_bilibili_download_endpoint_can_register_downloaded_video_source() -> None:
    response = ServeAuthorizedBilibiliDownloadEndpoint().execute(
        method="POST",
        path="/api/rebuild/video-links/bilibili/authorized-download",
        body={
            "confirm_download": True,
            "title": "Downloaded OS video",
            "plan": _download_plan_payload(),
            "settings": _downloader_settings_payload(),
        },
        download_video=lambda **_: _download_result(),
        trusted_settings=_trusted_downloader_settings(),
        register_downloaded_video=lambda **_: _registration_result(),
    )

    assert response.status_code == 200
    assert response.body["source_id"] == "source-video-downloaded-001"
    assert response.body["authorization_ref"] == "crp://default/authorized-video/authorized-video-source-video-downloaded-001.json"
    assert response.body["source_registration"]["starts_audio_extraction"] is False
    assert response.body["source_registration"]["publishes_memory"] is False


def test_authorized_bilibili_download_rejects_forged_host_and_output_settings() -> None:
    handler_called = False

    def download_video(**_: object) -> AuthorizedBilibiliDownloadResult:
        nonlocal handler_called
        handler_called = True
        return _download_result()

    forged_plan = _download_plan_payload()
    forged_plan["source_url"] = "https://example.com/video/BV1abcDEF234"
    forged_settings = _downloader_settings_payload()
    forged_settings["output_root"] = "C:\\forged-output"
    response = ServeAuthorizedBilibiliDownloadEndpoint().execute(
        method="POST",
        path="/api/rebuild/video-links/bilibili/authorized-download",
        body={
            "confirm_download": True,
            "plan": forged_plan,
            "settings": forged_settings,
        },
        download_video=download_video,
        trusted_settings=_trusted_downloader_settings(),
    )

    assert response.status_code == 400
    assert handler_called is False


def test_video_audio_extraction_endpoint_rejects_ui_supplied_command() -> None:
    response = ServeVideoAudioExtractionEndpoint().execute(
        method="POST",
        path="/api/rebuild/sources/source-video-001/audio-track",
        body={"command": ["ffmpeg"]},
        extract_audio=lambda **_: _audio_result(),
    )

    assert response.status_code == 400
    assert response.body["reason"] == "video audio extraction endpoint does not accept provider command"


def test_video_audio_extraction_endpoint_runs_by_source_id() -> None:
    captured: dict[str, object] = {}

    def extract_audio(**kwargs: object) -> VideoAudioExtractionResult:
        captured.update(kwargs)
        return _audio_result()

    response = ServeVideoAudioExtractionEndpoint().execute(
        method="POST",
        path="/api/rebuild/sources/source%20video%20001/audio-track",
        body={},
        extract_audio=extract_audio,
    )

    assert response.status_code == 200
    assert captured == {"source_id": "source video 001"}
    assert response.body["status"] == "completed"
    assert response.body["audio_asset_id"] == "audio-track-source-video-001"
    assert response.body["starts_asr"] is False
    assert response.body["publishes_memory"] is False


def test_audio_asset_transcription_endpoint_runs_by_audio_asset_id_without_command() -> None:
    captured: dict[str, object] = {}

    def transcribe_audio(**kwargs: object) -> AudioAssetTranscriptionResult:
        captured.update(kwargs)
        return _transcript_result()

    response = ServeAudioAssetTranscriptionEndpoint().execute(
        method="POST",
        path="/api/rebuild/audio-assets/audio%20track%20001/transcription",
        body={},
        transcribe_audio=transcribe_audio,
    )

    assert response.status_code == 200
    assert captured == {"audio_asset_id": "audio track 001"}
    assert response.body["status"] == "completed"
    assert response.body["output_id"] == "media-output-transcript-source-video-001"
    assert response.body["starts_summary"] is False
    assert response.body["publishes_memory"] is False


def test_transcript_summary_endpoint_runs_by_output_id_without_command() -> None:
    captured: dict[str, object] = {}

    def summarize_transcript(**kwargs: object) -> TranscriptSummaryResult:
        captured.update(kwargs)
        return _summary_result()

    response = ServeTranscriptSummaryEndpoint().execute(
        method="POST",
        path="/api/rebuild/media-processing-outputs/media-output-transcript-001/summary",
        body={},
        summarize_transcript=summarize_transcript,
    )

    assert response.status_code == 200
    assert captured == {"transcript_output_id": "media-output-transcript-001"}
    assert response.body["status"] == "completed"
    assert response.body["output_id"] == "media-output-summary-source-video-001"
    assert response.body["creates_memory_candidate"] is False
    assert response.body["publishes_memory"] is False


def _audio_result() -> VideoAudioExtractionResult:
    return VideoAudioExtractionResult(
        status="completed",
        job_id="media-job-audio-track-source-video-001",
        output_id="media-output-audio-track-source-video-001",
        source_id="source-video-001",
        provider="local-ffmpeg",
        audio_asset_id="audio-track-source-video-001",
        audio_asset_ref="crp-ref://default/assets/audio-track-source-video-001",
        duration_seconds=8.0,
        sample_rate_hz=16000,
        channels=1,
        output_preview="已抽取 8 秒音频。",
        starts_asr=False,
        starts_summary=False,
        creates_memory_candidate=False,
        publishes_memory=False,
        error=None,
    )


def _download_plan_payload() -> dict[str, object]:
    resolution = BilibiliVideoLinkResolver().resolve(
        url="https://www.bilibili.com/video/BV1abcDEF234",
    )
    plan = LinkedVideoDownloadPlanner().create_dry_run_plan(
        resolution=resolution,
        video_id="BV1abcDEF234",
    )
    return serialize_linked_video_download_plan(plan)


def _downloader_settings_payload() -> dict[str, object]:
    return {
        "enabled": True,
        "provider_name": "yt-dlp-bilibili",
        "output_root": "local-video-output",
        "cookie_mode": "none",
        "cookies_from_browser": "",
        "cookies_file": None,
        "allow_restricted_content": False,
    }


def _trusted_downloader_settings() -> BilibiliDownloaderSettings:
    payload = _downloader_settings_payload()
    return BilibiliDownloaderSettings(
        status="ready",
        enabled=True,
        provider_name=str(payload["provider_name"]),
        output_root=str(payload["output_root"]),
        cookie_mode=str(payload["cookie_mode"]),
        cookies_from_browser=str(payload["cookies_from_browser"]),
        cookies_file=None,
        allow_restricted_content=False,
        explicit_enable_required=True,
        remote_processing=False,
        memory_publication="not_started",
    )


def _download_result() -> AuthorizedBilibiliDownloadResult:
    return AuthorizedBilibiliDownloadResult(
        status="completed",
        provider="yt-dlp-bilibili",
        mode="authorized_download",
        series_id="bilibili-BV1abcDEF234",
        video_id="BV1abcDEF234_p1",
        bvid="BV1abcDEF234",
        page=1,
        command=("python", "-m", "yt_dlp"),
        output_file="local-video-output/bilibili-BV1abcDEF234/BV1abcDEF234_p1.mp4",
        output_video_reference="authorized-video://bilibili/BV1abcDEF234/p1",
        reads_cookies=False,
        cookie_mode="none",
        downloads_video=True,
        writes_video_file=True,
        starts_audio_extraction=False,
        starts_asr=False,
        starts_summary=False,
        creates_memory_candidate=False,
        publishes_memory=False,
        blocked_operations=("audio_track_extraction", "memory_publication"),
        error=None,
    )


def _registration_result() -> DownloadedVideoSourceRegistrationResult:
    return DownloadedVideoSourceRegistrationResult(
        status="authorized_source_created",
        source_id="source-video-downloaded-001",
        source_ref="crp://default/sources/source-video-downloaded-001",
        source_title="Downloaded OS video",
        media_type="video/mp4",
        size_bytes=1024,
        video_reference="authorized-video://bilibili/BV1abcDEF234/p1",
        authorization_id="authorized-video-source-video-downloaded-001",
        authorization_ref="crp://default/authorized-video/authorized-video-source-video-downloaded-001.json",
        starts_audio_extraction=False,
        starts_asr=False,
        starts_summary=False,
        creates_memory_candidate=False,
        publishes_memory=False,
    )


def _transcript_result() -> AudioAssetTranscriptionResult:
    return AudioAssetTranscriptionResult(
        status="completed",
        job_id="media-job-transcript-source-video-001",
        output_id="media-output-transcript-source-video-001",
        source_id="source-video-001",
        audio_asset_id="audio-track-source-video-001",
        provider="local-asr",
        language="zh",
        segment_count=2,
        char_count=18,
        output_preview="视频转写输出。",
        starts_summary=False,
        creates_memory_candidate=False,
        publishes_memory=False,
        error=None,
    )


def _summary_result() -> TranscriptSummaryResult:
    return TranscriptSummaryResult(
        status="completed",
        job_id="media-job-summary-source-video-001",
        output_id="media-output-summary-source-video-001",
        source_id="source-video-001",
        transcript_output_id="media-output-transcript-001",
        provider="local-summary",
        title="视频总结",
        chapter_count=1,
        evidence_count=1,
        output_preview="视频总结输出。",
        creates_memory_candidate=False,
        publishes_memory=False,
        error=None,
    )
