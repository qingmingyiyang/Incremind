from __future__ import annotations

from pathlib import Path

from core.product_core import (
    AuthorizedBilibiliDownloadResult,
    DownloadedVideoSourceRegistrationError,
    RegisterDownloadedBilibiliVideoSource,
    serialize_downloaded_video_source_registration,
)
from core.storage_provider import JsonObjectStore


def _store(tmp_path: Path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def test_register_downloaded_bilibili_video_source_creates_authorized_video_source(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    video_file = tmp_path / "downloads" / "BV1abcDEF234_p1.mp4"
    video_file.parent.mkdir()
    video_file.write_bytes(b"fake video")

    result = RegisterDownloadedBilibiliVideoSource(object_store).execute(
        download=_download_result(video_file),
        title="Downloaded OS video",
    )
    payload = serialize_downloaded_video_source_registration(result)
    source = object_store.read("sources", result.source_id)
    authorization = object_store.read("authorized_file_refs", result.authorization_id)

    assert payload["status"] == "authorized_source_created"
    assert payload["source_id"].startswith("source-video-")
    assert payload["authorization_ref"].startswith("crp://default/authorized-video/")
    assert payload["starts_audio_extraction"] is False
    assert payload["starts_asr"] is False
    assert payload["starts_summary"] is False
    assert payload["creates_memory_candidate"] is False
    assert payload["publishes_memory"] is False
    assert source is not None
    assert source["type"] == "video"
    assert source["title"] == "Downloaded OS video"
    assert source["media_type"] == "video/mp4"
    assert source["metadata"]["video_reference"] == "bilibili/BV1abcDEF234/p1"
    assert source["metadata"]["video_authorization"]["path_stored_in_source"] is False
    assert str(video_file) not in str(source)
    assert authorization is not None
    assert authorization["path"] == str(video_file.resolve(strict=False))
    assert authorization["path_scope"] == "local_user_authorized_video"


def test_register_downloaded_bilibili_video_source_requires_completed_download(tmp_path: Path) -> None:
    video_file = tmp_path / "missing.mp4"
    download = _download_result(video_file, status="failed")

    try:
        RegisterDownloadedBilibiliVideoSource(_store(tmp_path)).execute(download=download)
    except DownloadedVideoSourceRegistrationError as error:
        assert str(error) == "download must be completed"
    else:
        raise AssertionError("expected completed download guard")


def test_register_downloaded_bilibili_video_source_requires_existing_output_file(tmp_path: Path) -> None:
    download = _download_result(tmp_path / "missing.mp4")

    try:
        RegisterDownloadedBilibiliVideoSource(_store(tmp_path)).execute(download=download)
    except DownloadedVideoSourceRegistrationError as error:
        assert str(error) == "download output file does not exist"
    else:
        raise AssertionError("expected output file guard")


def _download_result(path: Path, *, status: str = "completed") -> AuthorizedBilibiliDownloadResult:
    return AuthorizedBilibiliDownloadResult(
        status=status,
        provider="yt-dlp-bilibili",
        mode="authorized_download",
        series_id="bilibili-BV1abcDEF234",
        video_id="BV1abcDEF234_p1",
        bvid="BV1abcDEF234",
        page=1,
        command=("python", "-m", "yt_dlp"),
        output_file=str(path),
        output_video_reference="authorized-video://bilibili/BV1abcDEF234/p1",
        reads_cookies=False,
        cookie_mode="none",
        downloads_video=status == "completed",
        writes_video_file=status == "completed",
        starts_audio_extraction=False,
        starts_asr=False,
        starts_summary=False,
        creates_memory_candidate=False,
        publishes_memory=False,
        blocked_operations=("audio_track_extraction", "memory_publication"),
        error=None if status == "completed" else "download failed",
    )
