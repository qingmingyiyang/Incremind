from __future__ import annotations

import subprocess
from pathlib import Path

from core.product_core import (
    BILIBILI_AUTHORIZED_DOWNLOAD_BLOCKED_OPERATIONS,
    BILIBILI_VIDEO_DOWNLOAD_BLOCKED_OPERATIONS,
    AuthorizedBilibiliDownloader,
    BilibiliVideoLinkResolver,
    GetBilibiliDownloaderSettings,
    LinkedVideoDownloadPlanner,
    SaveBilibiliDownloaderSettings,
    serialize_authorized_bilibili_download_result,
    serialize_bilibili_downloader_settings,
    serialize_linked_video_download_plan,
    serialize_video_link_resolution,
)
from core.storage_provider import JsonObjectStore


def _store(tmp_path: Path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def test_bilibili_video_link_resolver_parses_single_video_without_network_or_cookie() -> None:
    resolution = BilibiliVideoLinkResolver().resolve(
        url="https://www.bilibili.com/video/BV1abcDEF234/?spm_id_from=333.337.search-card.all.click"
    )
    payload = serialize_video_link_resolution(resolution)

    assert payload["status"] == "resolved"
    assert payload["provider"] == "bilibili"
    assert payload["resolution_type"] == "single_video"
    assert payload["series_id"] == "bilibili-BV1abcDEF234"
    assert payload["progression_mode"] == "auto"
    assert payload["progression_reason"] == "deterministic"
    assert payload["requires_user_confirmation"] is False
    assert payload["reads_cookies"] is False
    assert payload["downloads_video"] is False
    assert payload["reads_video_bytes"] is False
    assert payload["starts_media_processing"] is False
    assert payload["memory_publication"] == "not_started"
    assert payload["blocked_operations"] == list(BILIBILI_VIDEO_DOWNLOAD_BLOCKED_OPERATIONS)
    assert payload["videos"] == [
        {
            "provider": "bilibili",
            "video_id": "BV1abcDEF234",
            "bvid": "BV1abcDEF234",
            "page": 1,
            "title": "BV1abcDEF234",
            "source_url": "https://www.bilibili.com/video/BV1abcDEF234/?spm_id_from=333.337.search-card.all.click",
            "duration_seconds": None,
            "cover_url": None,
            "video_reference": "linked-video://bilibili/BV1abcDEF234/p1",
        }
    ]


def test_bilibili_video_link_resolver_normalizes_old_multi_page_entries() -> None:
    resolution = BilibiliVideoLinkResolver().resolve(
        url="https://www.bilibili.com/video/BV1abcDEF234",
        extracted_title="多 P 课程",
        extracted_entries=[
            {
                "id": "BV1abcDEF234",
                "page": 1,
                "title": "第一讲",
                "duration": 600,
                "thumbnail": "https://i0.hdslb.com/example.jpg",
                "webpage_url": "https://www.bilibili.com/video/BV1abcDEF234?p=1",
            },
            {
                "id": "BV1abcDEF234",
                "page": 2,
                "title": "第二讲",
                "duration": 720,
                "webpage_url": "https://www.bilibili.com/video/BV1abcDEF234?p=2",
            },
        ],
    )
    payload = serialize_video_link_resolution(resolution)

    assert payload["resolution_type"] == "multi_page"
    assert payload["series_id"] == "bilibili-BV1abcDEF234-2p"
    assert payload["title"] == "多 P 课程"
    assert [item["video_id"] for item in payload["videos"]] == ["BV1abcDEF234", "BV1abcDEF234_p2"]
    assert [item["video_reference"] for item in payload["videos"]] == [
        "linked-video://bilibili/BV1abcDEF234/p1",
        "linked-video://bilibili/BV1abcDEF234/p2",
    ]


def test_bilibili_video_link_resolver_keeps_collection_boundary_without_download() -> None:
    resolution = BilibiliVideoLinkResolver().resolve(
        url="https://space.bilibili.com/123456/list/98765?bvid=BV1abcDEF234",
        extracted_title="收藏夹课程",
        extracted_entries=[
            {
                "id": "BV1abcDEF234",
                "page": 1,
                "title": "收藏夹第一条",
                "webpage_url": "https://www.bilibili.com/video/BV1abcDEF234",
            }
        ],
    )
    payload = serialize_video_link_resolution(resolution)

    assert payload["resolution_type"] == "collection"
    assert payload["title"] == "收藏夹课程"
    assert payload["downloads_video"] is False
    assert payload["starts_media_processing"] is False
    assert "real_video_download" in payload["blocked_operations"]


def test_linked_video_download_planner_creates_dry_run_without_side_effects() -> None:
    resolution = BilibiliVideoLinkResolver().resolve(
        url="https://www.bilibili.com/video/BV1abcDEF234?p=2"
    )

    plan = LinkedVideoDownloadPlanner().create_dry_run_plan(
        resolution=resolution,
        video_id="BV1abcDEF234_p2",
    )
    payload = serialize_linked_video_download_plan(plan)

    assert payload["status"] == "planned"
    assert payload["mode"] == "dry_run"
    assert payload["provider"] == "bilibili"
    assert payload["video_id"] == "BV1abcDEF234_p2"
    assert payload["page"] == 2
    assert payload["proposed_video_reference"] == "linked-video://bilibili/BV1abcDEF234/p2"
    assert payload["progression_mode"] == "auto"
    assert payload["progression_reason"] == "deterministic"
    assert payload["requires_user_confirmation"] is False
    assert payload["reads_cookies"] is False
    assert payload["downloads_video"] is False
    assert payload["writes_video_file"] is False
    assert payload["starts_audio_extraction"] is False
    assert payload["starts_asr"] is False
    assert payload["starts_summary"] is False
    assert payload["creates_memory_candidate"] is False
    assert payload["publishes_memory"] is False
    assert payload["blocked_operations"] == list(BILIBILI_VIDEO_DOWNLOAD_BLOCKED_OPERATIONS)
    assert payload["next_step"] == "ready_for_authorized_download_confirmation"


def test_bilibili_video_link_resolver_rejects_non_bilibili_and_invalid_page() -> None:
    resolver = BilibiliVideoLinkResolver()

    try:
        resolver.resolve(url="https://example.com/video/BV1abcDEF234")
    except ValueError as error:
        assert str(error) == "video link resolver only supports bilibili links"
    else:
        raise AssertionError("expected non-bilibili rejection")

    try:
        resolver.resolve(url="https://www.bilibili.com/video/BV1abcDEF234?p=0")
    except ValueError as error:
        assert str(error) == "bilibili video page must be a positive integer"
    else:
        raise AssertionError("expected invalid page rejection")


def test_bilibili_downloader_settings_require_explicit_confirmation_and_cookie_config(
    tmp_path: Path,
) -> None:
    object_store = _store(tmp_path)
    writer = SaveBilibiliDownloaderSettings(object_store)
    cookies_file = tmp_path / "bilibili-cookies.txt"
    cookies_file.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")

    default_settings = serialize_bilibili_downloader_settings(
        GetBilibiliDownloaderSettings(object_store).execute()
    )
    assert default_settings["enabled"] is False
    assert default_settings["status"] == "disabled"

    try:
        writer.execute(enabled=True, output_root=str(tmp_path / "downloads"), confirm_enable=False)
    except ValueError as error:
        assert str(error) == "enabling bilibili downloader requires confirm_enable=true"
    else:
        raise AssertionError("expected explicit downloader enable guard")

    try:
        writer.execute(
            enabled=True,
            output_root=str(tmp_path / "downloads"),
            cookie_mode="browser",
            confirm_enable=True,
        )
    except ValueError as error:
        assert str(error) == "browser cookie mode requires cookies_from_browser"
    else:
        raise AssertionError("expected browser cookie config guard")

    settings = writer.execute(
        enabled=True,
        output_root=str(tmp_path / "downloads"),
        cookie_mode="file",
        cookies_file=str(cookies_file),
        confirm_enable=True,
    )
    payload = serialize_bilibili_downloader_settings(settings)

    assert payload["status"] == "ready"
    assert payload["enabled"] is True
    assert payload["cookie_mode"] == "file"
    assert payload["cookies_file"] == str(cookies_file)
    assert payload["remote_processing"] is False
    assert payload["memory_publication"] == "not_started"


def test_authorized_bilibili_downloader_builds_real_yt_dlp_command_with_browser_cookie(
    tmp_path: Path,
) -> None:
    object_store = _store(tmp_path)
    settings = SaveBilibiliDownloaderSettings(object_store).execute(
        enabled=True,
        output_root=str(tmp_path / "downloads"),
        cookie_mode="browser",
        cookies_from_browser="edge",
        confirm_enable=True,
    )
    resolution = BilibiliVideoLinkResolver().resolve(
        url="https://www.bilibili.com/video/BV1abcDEF234?p=2"
    )
    plan = LinkedVideoDownloadPlanner().create_dry_run_plan(
        resolution=resolution,
        video_id="BV1abcDEF234_p2",
    )
    captured_commands: list[tuple[str, ...]] = []

    def fake_runner(command: list[str] | tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        captured_commands.append(tuple(command))
        output_index = tuple(command).index("--output") + 1
        output_template = Path(tuple(command)[output_index].replace("%(ext)s", "mp4"))
        output_template.parent.mkdir(parents=True, exist_ok=True)
        output_template.write_bytes(b"fake downloaded video")
        return subprocess.CompletedProcess(args=list(command), returncode=0, stdout="", stderr="")

    result = AuthorizedBilibiliDownloader(runner=fake_runner).execute(plan=plan, settings=settings)
    payload = serialize_authorized_bilibili_download_result(result)
    command = captured_commands[0]

    assert payload["status"] == "completed"
    assert payload["mode"] == "authorized_real_download"
    assert payload["reads_cookies"] is True
    assert payload["cookie_mode"] == "browser"
    assert payload["downloads_video"] is True
    assert payload["writes_video_file"] is True
    assert payload["starts_audio_extraction"] is False
    assert payload["starts_asr"] is False
    assert payload["starts_summary"] is False
    assert payload["creates_memory_candidate"] is False
    assert payload["publishes_memory"] is False
    assert payload["blocked_operations"] == list(BILIBILI_AUTHORIZED_DOWNLOAD_BLOCKED_OPERATIONS)
    assert "backend.bilibili.authorized_public_download" in command
    assert "--cookies-from-browser" in command
    assert "edge" in command
    assert command[command.index("--cookie-mode") + 1] == "browser"
    assert command[command.index("--url") + 1] == plan.source_url
    assert payload["output_file"].endswith("BV1abcDEF234_p2.mp4")
    assert payload["output_video_reference"].endswith("BV1abcDEF234_p2.mp4")


def test_authorized_bilibili_downloader_records_failure_without_followup_processing(
    tmp_path: Path,
) -> None:
    object_store = _store(tmp_path)
    settings = SaveBilibiliDownloaderSettings(object_store).execute(
        enabled=True,
        output_root=str(tmp_path / "downloads"),
        cookie_mode="none",
        confirm_enable=True,
    )
    resolution = BilibiliVideoLinkResolver().resolve(
        url="https://www.bilibili.com/video/BV1abcDEF234"
    )
    plan = LinkedVideoDownloadPlanner().create_dry_run_plan(
        resolution=resolution,
        video_id="BV1abcDEF234",
    )

    def failing_runner(command: list[str] | tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=list(command), returncode=1, stdout="", stderr="network failed")

    result = AuthorizedBilibiliDownloader(runner=failing_runner).execute(plan=plan, settings=settings)
    payload = serialize_authorized_bilibili_download_result(result)

    assert payload["status"] == "failed"
    assert payload["error"] == "network failed"
    assert payload["downloads_video"] is True
    assert payload["writes_video_file"] is False
    assert payload["starts_audio_extraction"] is False
    assert payload["starts_asr"] is False
    assert payload["starts_summary"] is False
    assert payload["creates_memory_candidate"] is False
    assert payload["publishes_memory"] is False
