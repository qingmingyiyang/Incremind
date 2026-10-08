"""Read a public Xiaohongshu video into Workspace source text.

This is an extraction boundary only. It does not create a source manifest,
publish a document, or retain signed media locators after the call.
"""

from __future__ import annotations

from pathlib import Path
from collections.abc import Callable
from dataclasses import dataclass
import re
import tempfile
from uuid import uuid4
from urllib.parse import parse_qsl, urlencode, urlsplit

from backend.api.xiaohongshu_platform_provider import (
    XiaohongshuMetadataProviderError,
    _asset_locator,
    _extract_note,
    _normalize_note,
)
from backend.security.network_adapter import (
    NetworkBoundaryError,
    SafeBinaryDownloadAdapter,
    SafeTextNetworkAdapter,
)
from .workspace_media_url import resolve_media_url


_NOTE_PATH = re.compile(r"/(?:explore|discovery/item)/([A-Za-z0-9]{24})/?\Z")
_MAX_VIDEO_BYTES = 64 * 1024 * 1024
_MAX_AUDIO_MS = 15 * 60 * 1000
_MAX_SOURCE_CHARS = 60_000


@dataclass(frozen=True)
class _PublicNote:
    canonical_url: str
    title: str
    description: str
    content_kind: str
    note: object


def read_xiaohongshu_media(
    url: str, runtime_root: Path, *, project_id: str = "default",
    item_id: str | None = None, run_id: str | None = None,
    validate_remote: Callable[[], None] | None = None,
) -> dict[str, str]:
    """Return caption and actual video speech, or fail with a stable code.

    Tracking parameters are used only for the ephemeral page fetch. They are
    removed from the canonical URL returned to the Workspace record.
    """
    return _read_captured_note(_capture_public_note(url), runtime_root,
        project_id=project_id, item_id=item_id, run_id=run_id,
        validate_remote=validate_remote)


def _capture_public_note(url):
    """Read the existing safe public page once; retain media locators in memory."""
    platform, resolved_url = resolve_media_url(url)
    if platform != "xiaohongshu":
        raise ValueError("invalid_source")
    note_id, canonical_url, request_url = _source_url(resolved_url)
    try:
        page = SafeTextNetworkAdapter(
            allowed_hosts=("xiaohongshu.com", "www.xiaohongshu.com"),
            max_redirects=0,
            max_response_bytes=1024 * 1024,
            timeout_seconds=20,
        ).fetch_text(request_url)
        note = _extract_note(page, note_id=note_id)
        metadata, _assets, content_kind, body = _normalize_note(note, note_id=note_id)
    except NetworkBoundaryError as error:
        raise ValueError("xiaohongshu_page_unavailable") from error
    except XiaohongshuMetadataProviderError as error:
        raise ValueError("xiaohongshu_metadata_unavailable") from error
    return _PublicNote(canonical_url, str(metadata['title']),
        str(body['text']) if body else '', content_kind, note)


def _read_captured_note(capture, runtime_root, *, project_id='default',
                        item_id=None, run_id=None, validate_remote=None, allow_nonvideo=False):
    canonical_url, title, description = capture.canonical_url, capture.title, capture.description
    content_kind, note = capture.content_kind, capture.note
    if content_kind != "video":
        if not allow_nonvideo:
            raise ValueError("xiaohongshu_video_required")
        parts = [f'标题：{title}', f'来源：{canonical_url}']
        if description:
            parts.append(f'笔记描述：{description}')
        return {'source_text': '\n\n'.join(parts), 'title': title,
            'canonical_url': canonical_url, 'content_kind': content_kind,
            'acquisition_method': 'public_html_note'}

    locator = _asset_locator(note.get("video"), kind="video")
    if not locator:
        raise ValueError("xiaohongshu_video_unavailable")
    _validate_video_locator(locator)
    from .workspace_audio import _cloud_asr_selected

    selected_cloud = _cloud_asr_selected(runtime_root)
    speech = _download_and_transcribe(
        locator, canonical_url, runtime_root, title,
        project_id, item_id or "platform-xhs-" + uuid4().hex,
        run_id or "workspace-run-" + uuid4().hex,
        "tokenhub-asr" if selected_cloud else "local-faster-whisper",
        validate_remote,
    )
    if not speech:
        raise ValueError("xiaohongshu_video_speech_unavailable")
    parts = [f"标题：{title}", f"来源：{canonical_url}"]
    if description:
        parts.append(f"笔记描述：{description}")
    parts.append(f"视频语音转写：\n{speech}")
    source_text = "\n\n".join(parts)
    if len(source_text) > _MAX_SOURCE_CHARS:
        raise ValueError("source_text_too_large")
    return {
        "source_text": source_text,
        "title": title,
        "canonical_url": canonical_url,
        "acquisition_method": (
            "public_html_video_hy_asr" if selected_cloud else "public_html_video_local_asr"
        ),
        "content_kind": "video",
    }


def _source_url(url: str) -> tuple[str, str, str]:
    if not isinstance(url, str) or len(url) > 2048:
        raise ValueError("invalid_source")
    try:
        parsed = urlsplit(url.strip())
        host = (parsed.hostname or "").encode("idna").decode("ascii").lower().rstrip(".")
        port = parsed.port
    except (ValueError, UnicodeError) as error:
        raise ValueError("invalid_source") from error
    if (
        parsed.scheme != "https"
        or host not in {"xiaohongshu.com", "www.xiaohongshu.com"}
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("invalid_source")
    match = _NOTE_PATH.fullmatch(parsed.path)
    if match is None:
        raise ValueError("invalid_source")
    note_id = match.group(1)
    canonical = f"https://www.xiaohongshu.com/explore/{note_id}"
    try:
        params = parse_qsl(parsed.query, keep_blank_values=False, strict_parsing=False, max_num_fields=16)
    except ValueError as error:
        raise ValueError("invalid_source") from error
    selected: dict[str, str] = {}
    for key, value in params:
        if key == "xsec_token" and 1 <= len(value) <= 512 and re.fullmatch(r"[A-Za-z0-9._~=/+-]+", value):
            selected[key] = value
        elif key == "xsec_source" and 1 <= len(value) <= 80 and re.fullmatch(r"[A-Za-z0-9_-]+", value):
            selected[key] = value
    query = urlencode(selected)
    return note_id, canonical, canonical + ("?" + query if query else "")


def _download_and_transcribe(
    locator: str, canonical_url: str, runtime_root: Path, title: str,
    project_id: str, item_id: str, run_id: str, expected_provider: str,
    validate_remote: Callable[[], None] | None = None,
) -> str:
    # The existing binary adapter vets public IPs on each hop, restricts hosts,
    # and streams into a size-bounded temporary file. No CDN URL is persisted.
    temporary_parent = Path(runtime_root) / "workspace" / "media-tmp"
    temporary_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="xhs-", dir=temporary_parent) as directory:
        stage_root = Path(directory)
        try:
            downloaded = SafeBinaryDownloadAdapter(
                stage_root,
                allowed_host_suffixes=("xhscdn.com",),
                max_redirects=1,
                timeout_seconds=30,
            ).download(
                locator,
                relative_path="video.mp4",
                max_response_bytes=_MAX_VIDEO_BYTES,
                headers={"Referer": canonical_url, "Origin": "https://www.xiaohongshu.com"},
                timeout_seconds=30,
            )
        except NetworkBoundaryError as error:
            raise ValueError("xiaohongshu_video_unavailable") from error
        if downloaded.media_type != "video/mp4":
            raise ValueError("xiaohongshu_video_unavailable")

        try:
            from .workspace_audio import _transcribe_output

            output = _transcribe_output(
                downloaded.path, runtime_root, project_id, item_id, run_id,
                source_type="video", max_duration_seconds=_MAX_AUDIO_MS / 1000,
                expected_provider=expected_provider, validate_remote=validate_remote,
            )
        except ValueError as error:
            if str(error) == "asr_unavailable":
                raise ValueError("xiaohongshu_asr_unavailable") from error
            if str(error) == "remote_processing_target_changed":
                raise
            raise ValueError("xiaohongshu_video_transcription_failed") from error
        except Exception as error:
            raise ValueError("xiaohongshu_video_transcription_failed") from error
        segments = output.get("segments")
        if not isinstance(segments, list):
            raise ValueError("xiaohongshu_video_transcription_failed")
        lines = [str(segment.get("text", "")).strip() for segment in segments if isinstance(segment, dict)]
        return "\n".join(line for line in lines if line)


def _validate_video_locator(locator: str) -> None:
    try:
        parsed = urlsplit(locator)
        host = (parsed.hostname or "").encode("idna").decode("ascii").lower().rstrip(".")
        port = parsed.port
    except (TypeError, ValueError, UnicodeError) as error:
        raise ValueError("xiaohongshu_video_unavailable") from error
    if (
        parsed.scheme != "https"
        or not host.endswith(".xhscdn.com")
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError("xiaohongshu_video_unavailable")
