from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from core.ingestion_core import ObjectStoreSourceRegistrar, SourceSubmission
from .ports import ObjectStorePort

from .source_file_authorization import AuthorizeLocalVideoFileForSource, SourceFileAuthorizationResult
from .video_link_adapter import AuthorizedBilibiliDownloadResult


class DownloadedVideoSourceRegistrationError(ValueError):
    """Raised when a downloaded video cannot become an authorized OS video Source."""


@dataclass(frozen=True, slots=True)
class DownloadedVideoSourceRegistrationResult:
    status: str
    source_id: str
    source_ref: str
    source_title: str
    media_type: str
    size_bytes: int
    video_reference: str
    authorization_id: str
    authorization_ref: str
    starts_audio_extraction: bool
    starts_asr: bool
    starts_summary: bool
    creates_memory_candidate: bool
    publishes_memory: bool


class RegisterDownloadedBilibiliVideoSource:
    """Create and authorize a local video Source from a completed Bilibili download."""

    def __init__(
        self,
        object_store: ObjectStorePort,
        *,
        namespace_id: str = "default",
    ) -> None:
        self._object_store = object_store
        self._namespace_id = namespace_id

    def execute(
        self,
        *,
        download: AuthorizedBilibiliDownloadResult,
        title: str | None = None,
    ) -> DownloadedVideoSourceRegistrationResult:
        if download.status != "completed":
            raise DownloadedVideoSourceRegistrationError("download must be completed")
        if not download.output_file:
            raise DownloadedVideoSourceRegistrationError("download output_file is required")
        output_path = Path(download.output_file).expanduser().resolve(strict=False)
        if not output_path.exists():
            raise DownloadedVideoSourceRegistrationError("download output file does not exist")
        if not output_path.is_file():
            raise DownloadedVideoSourceRegistrationError("download output path is not a file")
        video_reference = _video_reference(download)
        media_type = _video_media_type(output_path)
        source = ObjectStoreSourceRegistrar(self._object_store, namespace_id=self._namespace_id).register(
            SourceSubmission(
                kind="video",
                title=_source_title(download, title=title),
                display_name=output_path.name,
                media_type=media_type,
                size_bytes=output_path.stat().st_size,
                video_reference=video_reference,
            )
        )
        authorization = AuthorizeLocalVideoFileForSource(
            self._object_store,
            namespace_id=self._namespace_id,
        ).execute(source_id=str(source["id"]), file_path=str(output_path))
        return _result_from_source(source=source, authorization=authorization, video_reference=video_reference)


def serialize_downloaded_video_source_registration(
    result: DownloadedVideoSourceRegistrationResult,
) -> dict[str, object]:
    return {
        "status": result.status,
        "source_id": result.source_id,
        "source_ref": result.source_ref,
        "source_title": result.source_title,
        "media_type": result.media_type,
        "size_bytes": result.size_bytes,
        "video_reference": result.video_reference,
        "authorization_id": result.authorization_id,
        "authorization_ref": result.authorization_ref,
        "starts_audio_extraction": result.starts_audio_extraction,
        "starts_asr": result.starts_asr,
        "starts_summary": result.starts_summary,
        "creates_memory_candidate": result.creates_memory_candidate,
        "publishes_memory": result.publishes_memory,
    }


def _result_from_source(
    *,
    source: dict[str, object],
    authorization: SourceFileAuthorizationResult,
    video_reference: str,
) -> DownloadedVideoSourceRegistrationResult:
    return DownloadedVideoSourceRegistrationResult(
        status="authorized_source_created",
        source_id=str(source["id"]),
        source_ref=str(source["storage_uri"]),
        source_title=str(source["title"]),
        media_type=str(source["media_type"]),
        size_bytes=int(source["size_bytes"]),
        video_reference=video_reference,
        authorization_id=authorization.authorization_id,
        authorization_ref=authorization.authorization_ref,
        starts_audio_extraction=False,
        starts_asr=False,
        starts_summary=False,
        creates_memory_candidate=False,
        publishes_memory=False,
    )


def _source_title(download: AuthorizedBilibiliDownloadResult, *, title: str | None) -> str:
    clean_title = (title or "").strip()
    if clean_title:
        return clean_title
    return f"Bilibili {download.bvid} p{download.page}"


def _video_reference(download: AuthorizedBilibiliDownloadResult) -> str:
    return f"bilibili/{download.bvid}/p{download.page}"


def _video_media_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".webm":
        return "video/webm"
    if suffix == ".mkv":
        return "video/x-matroska"
    if suffix == ".mov":
        return "video/quicktime"
    return "video/mp4"
