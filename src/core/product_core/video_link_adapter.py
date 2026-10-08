from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .ports import ObjectStorePort
from .workflow_progression import WorkflowDecisionBoundary, decide_workflow_progression


BILIBILI_VIDEO_DOWNLOAD_BLOCKED_OPERATIONS = (
    "cookie_read",
    "real_video_download",
    "restricted_content_download",
    "video_byte_read",
    "audio_track_extraction",
    "asr_transcription",
    "summary_generation",
    "memory_candidate",
    "memory_publication",
)
BILIBILI_AUTHORIZED_DOWNLOAD_BLOCKED_OPERATIONS = (
    "restricted_content_download",
    "audio_track_extraction",
    "asr_transcription",
    "summary_generation",
    "memory_candidate",
    "memory_publication",
)
BILIBILI_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)
BILIBILI_DEFAULT_FORMAT = "bv*[height<=1080]+ba/b[height<=1080]/bv*+ba/b"

_BVID_RE = re.compile(r"\b(BV[0-9A-Za-z]{10,})\b")
_AVID_RE = re.compile(r"\bav(\d+)\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class LinkedVideoItem:
    provider: str
    video_id: str
    bvid: str
    page: int
    title: str
    source_url: str
    duration_seconds: int | None
    cover_url: str | None
    video_reference: str


@dataclass(frozen=True, slots=True)
class VideoLinkResolution:
    status: str
    provider: str
    resolution_type: str
    source_url: str
    series_id: str
    title: str
    videos: tuple[LinkedVideoItem, ...]
    progression_mode: str
    progression_reason: str
    requires_user_confirmation: bool
    reads_cookies: bool
    downloads_video: bool
    reads_video_bytes: bool
    starts_media_processing: bool
    memory_publication: str
    blocked_operations: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LinkedVideoDownloadPlan:
    status: str
    mode: str
    provider: str
    series_id: str
    video_id: str
    bvid: str
    page: int
    source_url: str
    proposed_video_reference: str
    progression_mode: str
    progression_reason: str
    requires_user_confirmation: bool
    reads_cookies: bool
    downloads_video: bool
    writes_video_file: bool
    starts_audio_extraction: bool
    starts_asr: bool
    starts_summary: bool
    creates_memory_candidate: bool
    publishes_memory: bool
    blocked_operations: tuple[str, ...]
    next_step: str


@dataclass(frozen=True, slots=True)
class BilibiliDownloaderSettings:
    status: str
    enabled: bool
    provider_name: str
    output_root: str
    cookie_mode: str
    cookies_from_browser: str
    cookies_file: str | None
    allow_restricted_content: bool
    explicit_enable_required: bool
    remote_processing: bool
    memory_publication: str


@dataclass(frozen=True, slots=True)
class AuthorizedBilibiliDownloadResult:
    status: str
    provider: str
    mode: str
    series_id: str
    video_id: str
    bvid: str
    page: int
    command: tuple[str, ...]
    output_file: str | None
    output_video_reference: str | None
    reads_cookies: bool
    cookie_mode: str
    downloads_video: bool
    writes_video_file: bool
    starts_audio_extraction: bool
    starts_asr: bool
    starts_summary: bool
    creates_memory_candidate: bool
    publishes_memory: bool
    blocked_operations: tuple[str, ...]
    error: str | None


class BilibiliVideoLinkResolver:
    """Normalize Bilibili link metadata without network, cookies or media download."""

    def resolve(
        self,
        *,
        url: str,
        extracted_entries: Sequence[Mapping[str, object]] | None = None,
        extracted_title: str | None = None,
    ) -> VideoLinkResolution:
        clean_url = _required_http_url(url)
        _require_bilibili_host(clean_url)
        videos = (
            tuple(_item_from_entry(entry, fallback_url=clean_url) for entry in extracted_entries)
            if extracted_entries
            else (_item_from_url(clean_url),)
        )
        if not videos:
            raise ValueError("bilibili link resolver requires at least one video")
        series_key = _series_key(videos)
        resolution_type = "single_video"
        if len(videos) > 1:
            resolution_type = "multi_page"
        elif _looks_like_collection_url(clean_url):
            resolution_type = "collection"
        title = (extracted_title or "").strip() or videos[0].title
        progression = decide_workflow_progression(WorkflowDecisionBoundary.DETERMINISTIC)
        return VideoLinkResolution(
            status="resolved",
            provider="bilibili",
            resolution_type=resolution_type,
            source_url=clean_url,
            series_id=f"bilibili-{series_key}",
            title=title,
            videos=videos,
            progression_mode=progression.mode.value,
            progression_reason=progression.reason.value,
            requires_user_confirmation=progression.requires_user_confirmation,
            reads_cookies=False,
            downloads_video=False,
            reads_video_bytes=False,
            starts_media_processing=False,
            memory_publication="not_started",
            blocked_operations=BILIBILI_VIDEO_DOWNLOAD_BLOCKED_OPERATIONS,
        )


class LinkedVideoDownloadPlanner:
    """Create dry-run download plans before any downloader implementation is enabled."""

    def create_dry_run_plan(
        self,
        *,
        resolution: VideoLinkResolution,
        video_id: str,
    ) -> LinkedVideoDownloadPlan:
        video = _select_video(resolution, video_id)
        progression = decide_workflow_progression(WorkflowDecisionBoundary.DETERMINISTIC)
        return LinkedVideoDownloadPlan(
            status="planned",
            mode="dry_run",
            provider=resolution.provider,
            series_id=resolution.series_id,
            video_id=video.video_id,
            bvid=video.bvid,
            page=video.page,
            source_url=video.source_url,
            proposed_video_reference=video.video_reference,
            progression_mode=progression.mode.value,
            progression_reason=progression.reason.value,
            requires_user_confirmation=progression.requires_user_confirmation,
            reads_cookies=False,
            downloads_video=False,
            writes_video_file=False,
            starts_audio_extraction=False,
            starts_asr=False,
            starts_summary=False,
            creates_memory_candidate=False,
            publishes_memory=False,
            blocked_operations=BILIBILI_VIDEO_DOWNLOAD_BLOCKED_OPERATIONS,
            next_step="ready_for_authorized_download_confirmation",
        )


class GetBilibiliDownloaderSettings:
    _COLLECTION = "bilibili_downloader_settings"
    _SETTINGS_ID = "default"

    def __init__(self, object_store: ObjectStorePort) -> None:
        self._object_store = object_store

    def execute(self) -> BilibiliDownloaderSettings:
        record = self._object_store.read(self._COLLECTION, self._SETTINGS_ID)
        if record is None:
            return _downloader_settings_from_record(_default_downloader_settings_record())
        return _downloader_settings_from_record(record)


class SaveBilibiliDownloaderSettings:
    _COLLECTION = "bilibili_downloader_settings"
    _SETTINGS_ID = "default"

    def __init__(self, object_store: ObjectStorePort, *, now: str = "2026-07-02T01:20:00+08:00") -> None:
        self._object_store = object_store
        self._now = now

    def execute(
        self,
        *,
        enabled: bool,
        output_root: str,
        cookie_mode: str = "none",
        cookies_from_browser: str = "",
        cookies_file: str | None = None,
        allow_restricted_content: bool = False,
        provider_name: str = "yt-dlp-bilibili",
        confirm_enable: bool = False,
    ) -> BilibiliDownloaderSettings:
        clean_provider = _required_text(provider_name, "provider_name")
        clean_output_root = _required_text(output_root, "output_root")
        clean_cookie_mode = _cookie_mode(cookie_mode)
        clean_browser = _optional_str(cookies_from_browser) or ""
        clean_cookie_file = _optional_str(cookies_file)
        if enabled and confirm_enable is not True:
            raise ValueError("enabling bilibili downloader requires confirm_enable=true")
        if enabled and clean_cookie_mode == "browser" and not clean_browser:
            raise ValueError("browser cookie mode requires cookies_from_browser")
        if enabled and clean_cookie_mode == "file":
            if clean_cookie_file is None:
                raise ValueError("file cookie mode requires cookies_file")
            if not Path(clean_cookie_file).expanduser().resolve(strict=False).exists():
                raise ValueError("cookies_file does not exist")
        record = {
            "schema_version": "1.0.0",
            "id": self._SETTINGS_ID,
            "enabled": bool(enabled),
            "provider_name": clean_provider,
            "output_root": clean_output_root,
            "cookie_mode": clean_cookie_mode,
            "cookies_from_browser": clean_browser if clean_cookie_mode == "browser" else "",
            "cookies_file": clean_cookie_file if clean_cookie_mode == "file" else None,
            "allow_restricted_content": bool(allow_restricted_content),
            "remote_processing": False,
            "memory_publication": "not_started",
            "updated_at": self._now,
        }
        self._object_store.write(self._COLLECTION, self._SETTINGS_ID, record, expected_revision=None)
        return _downloader_settings_from_record(record)


class AuthorizedBilibiliDownloader:
    """Run yt-dlp only after downloader settings and a dry-run plan are explicitly authorized."""

    def __init__(
        self,
        *,
        runner: Callable[[Sequence[str]], subprocess.CompletedProcess[str]] | None = None,
    ) -> None:
        self._runner = runner or _run_subprocess

    def execute(
        self,
        *,
        plan: LinkedVideoDownloadPlan,
        settings: BilibiliDownloaderSettings,
    ) -> AuthorizedBilibiliDownloadResult:
        if settings.enabled is not True:
            raise ValueError("bilibili downloader is disabled")
        if plan.mode != "dry_run":
            raise ValueError("authorized downloader requires a dry-run plan")
        output_dir = Path(settings.output_root).expanduser().resolve(strict=False) / plan.series_id
        output_dir.mkdir(parents=True, exist_ok=True)
        output_template = str(output_dir / _output_template_stem(plan))
        command = _build_bilibili_download_command(
            url=plan.source_url,
            output_template=f"{output_template}.%(ext)s",
            settings=settings,
        )
        completed = self._runner(command)
        if completed.returncode != 0:
            return _download_result(
                plan=plan,
                settings=settings,
                command=command,
                status="failed",
                output_file=None,
                error=(completed.stderr or completed.stdout or f"yt-dlp exited {completed.returncode}").strip(),
            )
        output_file = _find_download_output(output_dir, _output_template_stem(plan))
        if output_file is None:
            return _download_result(
                plan=plan,
                settings=settings,
                command=command,
                status="failed",
                output_file=None,
                error="yt-dlp completed but no output file was found",
            )
        return _download_result(
            plan=plan,
            settings=settings,
            command=command,
            status="completed",
            output_file=output_file,
            error=None,
        )


def serialize_video_link_resolution(resolution: VideoLinkResolution) -> dict[str, object]:
    return {
        "status": resolution.status,
        "provider": resolution.provider,
        "resolution_type": resolution.resolution_type,
        "source_url": resolution.source_url,
        "series_id": resolution.series_id,
        "title": resolution.title,
        "videos": [
            {
                "provider": item.provider,
                "video_id": item.video_id,
                "bvid": item.bvid,
                "page": item.page,
                "title": item.title,
                "source_url": item.source_url,
                "duration_seconds": item.duration_seconds,
                "cover_url": item.cover_url,
                "video_reference": item.video_reference,
            }
            for item in resolution.videos
        ],
        "progression_mode": resolution.progression_mode,
        "progression_reason": resolution.progression_reason,
        "requires_user_confirmation": resolution.requires_user_confirmation,
        "reads_cookies": resolution.reads_cookies,
        "downloads_video": resolution.downloads_video,
        "reads_video_bytes": resolution.reads_video_bytes,
        "starts_media_processing": resolution.starts_media_processing,
        "memory_publication": resolution.memory_publication,
        "blocked_operations": list(resolution.blocked_operations),
    }


def serialize_linked_video_download_plan(plan: LinkedVideoDownloadPlan) -> dict[str, object]:
    return {
        "status": plan.status,
        "mode": plan.mode,
        "provider": plan.provider,
        "series_id": plan.series_id,
        "video_id": plan.video_id,
        "bvid": plan.bvid,
        "page": plan.page,
        "source_url": plan.source_url,
        "proposed_video_reference": plan.proposed_video_reference,
        "progression_mode": plan.progression_mode,
        "progression_reason": plan.progression_reason,
        "requires_user_confirmation": plan.requires_user_confirmation,
        "reads_cookies": plan.reads_cookies,
        "downloads_video": plan.downloads_video,
        "writes_video_file": plan.writes_video_file,
        "starts_audio_extraction": plan.starts_audio_extraction,
        "starts_asr": plan.starts_asr,
        "starts_summary": plan.starts_summary,
        "creates_memory_candidate": plan.creates_memory_candidate,
        "publishes_memory": plan.publishes_memory,
        "blocked_operations": list(plan.blocked_operations),
        "next_step": plan.next_step,
    }


def serialize_bilibili_downloader_settings(settings: BilibiliDownloaderSettings) -> dict[str, object]:
    return {
        "status": settings.status,
        "enabled": settings.enabled,
        "provider_name": settings.provider_name,
        "output_root": settings.output_root,
        "cookie_mode": settings.cookie_mode,
        "cookies_from_browser": settings.cookies_from_browser,
        "cookies_file": settings.cookies_file,
        "allow_restricted_content": settings.allow_restricted_content,
        "explicit_enable_required": settings.explicit_enable_required,
        "remote_processing": settings.remote_processing,
        "memory_publication": settings.memory_publication,
    }


def serialize_authorized_bilibili_download_result(result: AuthorizedBilibiliDownloadResult) -> dict[str, object]:
    return {
        "status": result.status,
        "provider": result.provider,
        "mode": result.mode,
        "series_id": result.series_id,
        "video_id": result.video_id,
        "bvid": result.bvid,
        "page": result.page,
        "command": list(result.command),
        "output_file": result.output_file,
        "output_video_reference": result.output_video_reference,
        "reads_cookies": result.reads_cookies,
        "cookie_mode": result.cookie_mode,
        "downloads_video": result.downloads_video,
        "writes_video_file": result.writes_video_file,
        "starts_audio_extraction": result.starts_audio_extraction,
        "starts_asr": result.starts_asr,
        "starts_summary": result.starts_summary,
        "creates_memory_candidate": result.creates_memory_candidate,
        "publishes_memory": result.publishes_memory,
        "blocked_operations": list(result.blocked_operations),
        "error": result.error,
    }


def _build_bilibili_download_command(
    *,
    url: str,
    output_template: str,
    settings: BilibiliDownloaderSettings,
) -> tuple[str, ...]:
    command = [
        sys.executable,
        "-m",
        "backend.bilibili.authorized_public_download",
        "--url",
        url,
        "--output",
        output_template,
        "--format",
        BILIBILI_DEFAULT_FORMAT,
        "--user-agent",
        BILIBILI_USER_AGENT,
        "--cookie-mode",
        settings.cookie_mode,
    ]
    if settings.cookie_mode == "file" and settings.cookies_file:
        command.extend(("--cookies-file", settings.cookies_file))
    if settings.cookie_mode == "browser" and settings.cookies_from_browser:
        command.extend(("--cookies-from-browser", settings.cookies_from_browser))
    return tuple(command)


def _item_from_url(url: str) -> LinkedVideoItem:
    bvid = _extract_bvid(url)
    page = _page_from_url(url)
    title = bvid if page == 1 else f"{bvid} P{page}"
    return _linked_video_item(
        bvid=bvid,
        page=page,
        title=title,
        source_url=url,
        duration_seconds=None,
        cover_url=None,
    )


def _item_from_entry(entry: Mapping[str, object], *, fallback_url: str) -> LinkedVideoItem:
    source_url = _optional_str(entry.get("webpage_url")) or _optional_str(entry.get("url")) or fallback_url
    bvid = _optional_str(entry.get("bvid")) or _extract_bvid(_optional_str(entry.get("id")) or source_url)
    page = _positive_int(entry.get("page")) or _page_from_url(source_url)
    title = _optional_str(entry.get("title")) or (bvid if page == 1 else f"{bvid} P{page}")
    return _linked_video_item(
        bvid=bvid,
        page=page,
        title=title,
        source_url=source_url,
        duration_seconds=_positive_int(entry.get("duration")),
        cover_url=_optional_str(entry.get("thumbnail")),
    )


def _linked_video_item(
    *,
    bvid: str,
    page: int,
    title: str,
    source_url: str,
    duration_seconds: int | None,
    cover_url: str | None,
) -> LinkedVideoItem:
    video_id = bvid if page == 1 else f"{bvid}_p{page}"
    return LinkedVideoItem(
        provider="bilibili",
        video_id=video_id,
        bvid=bvid,
        page=page,
        title=title,
        source_url=source_url,
        duration_seconds=duration_seconds,
        cover_url=cover_url,
        video_reference=f"linked-video://bilibili/{bvid}/p{page}",
    )


def _select_video(resolution: VideoLinkResolution, video_id: str) -> LinkedVideoItem:
    clean_video_id = video_id.strip()
    for video in resolution.videos:
        if video.video_id == clean_video_id:
            return video
    raise ValueError("download plan video_id not found in link resolution")


def _required_http_url(url: str) -> str:
    clean = url.strip()
    parsed = urlparse(clean)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("video link resolver requires http or https url")
    return clean


def _require_bilibili_host(url: str) -> None:
    host = urlparse(url).netloc.lower()
    if not (host == "bilibili.com" or host.endswith(".bilibili.com")):
        raise ValueError("video link resolver only supports bilibili links")


def _extract_bvid(value: str) -> str:
    match = _BVID_RE.search(value)
    if match is not None:
        return match.group(1)
    av_match = _AVID_RE.search(value)
    if av_match is not None:
        return f"av{av_match.group(1)}"
    raise ValueError("bilibili link resolver requires BV or av id")


def _page_from_url(url: str) -> int:
    page_values = parse_qs(urlparse(url).query).get("p")
    if not page_values:
        return 1
    page = _positive_int(page_values[0])
    if page is None:
        raise ValueError("bilibili video page must be a positive integer")
    return page


def _series_key(videos: Sequence[LinkedVideoItem]) -> str:
    first = videos[0]
    return first.bvid if len(videos) == 1 else f"{first.bvid}-{len(videos)}p"


def _looks_like_collection_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    return "/list/" in path or "/medialist/" in path or "/channel/collectiondetail" in path


def _optional_str(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _positive_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    if isinstance(value, str) and value.strip().isdigit():
        parsed = int(value.strip())
        return parsed if parsed > 0 else None
    return None


def _downloader_settings_from_record(record: Mapping[str, object]) -> BilibiliDownloaderSettings:
    enabled = record.get("enabled") is True
    output_root = _optional_str(record.get("output_root")) or "work/video-downloads"
    cookie_mode = _cookie_mode(_optional_str(record.get("cookie_mode")) or "none")
    cookies_from_browser = _optional_str(record.get("cookies_from_browser")) or ""
    cookies_file = _optional_str(record.get("cookies_file"))
    status = "ready" if enabled else "disabled"
    return BilibiliDownloaderSettings(
        status=status,
        enabled=enabled,
        provider_name=_optional_str(record.get("provider_name")) or "yt-dlp-bilibili",
        output_root=output_root,
        cookie_mode=cookie_mode,
        cookies_from_browser=cookies_from_browser if cookie_mode == "browser" else "",
        cookies_file=cookies_file if cookie_mode == "file" else None,
        allow_restricted_content=record.get("allow_restricted_content") is True,
        explicit_enable_required=True,
        remote_processing=False,
        memory_publication="not_started",
    )


def _default_downloader_settings_record() -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "id": "default",
        "enabled": False,
        "provider_name": "yt-dlp-bilibili",
        "output_root": "work/video-downloads",
        "cookie_mode": "none",
        "cookies_from_browser": "",
        "cookies_file": None,
        "allow_restricted_content": False,
        "remote_processing": False,
        "memory_publication": "not_started",
    }


def _download_result(
    *,
    plan: LinkedVideoDownloadPlan,
    settings: BilibiliDownloaderSettings,
    command: Sequence[str],
    status: str,
    output_file: Path | None,
    error: str | None,
) -> AuthorizedBilibiliDownloadResult:
    return AuthorizedBilibiliDownloadResult(
        status=status,
        provider=plan.provider,
        mode="authorized_real_download",
        series_id=plan.series_id,
        video_id=plan.video_id,
        bvid=plan.bvid,
        page=plan.page,
        command=tuple(command),
        output_file=str(output_file) if output_file is not None else None,
        output_video_reference=(
            f"downloaded-video://bilibili/{plan.series_id}/{output_file.name}" if output_file is not None else None
        ),
        reads_cookies=settings.cookie_mode in {"browser", "file"},
        cookie_mode=settings.cookie_mode,
        downloads_video=True,
        writes_video_file=output_file is not None,
        starts_audio_extraction=False,
        starts_asr=False,
        starts_summary=False,
        creates_memory_candidate=False,
        publishes_memory=False,
        blocked_operations=BILIBILI_AUTHORIZED_DOWNLOAD_BLOCKED_OPERATIONS,
        error=error,
    )


def _run_subprocess(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        check=False,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )


def _output_template_stem(plan: LinkedVideoDownloadPlan) -> str:
    return plan.bvid if plan.page == 1 else f"{plan.bvid}_p{plan.page}"


def _find_download_output(output_dir: Path, stem: str) -> Path | None:
    candidates = sorted(path for path in output_dir.glob(f"{stem}.*") if path.is_file())
    return candidates[0] if candidates else None


def _cookie_mode(value: str) -> str:
    clean = value.strip().lower()
    if clean not in {"none", "browser", "file"}:
        raise ValueError("cookie_mode must be none, browser or file")
    return clean


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} is required")
    return value.strip()
