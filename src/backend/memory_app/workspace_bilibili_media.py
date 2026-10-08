"""Extract reviewable Bilibili video speech for the workspace intake.

This adapter never publishes a document. Page metadata is context only: a
successful result always contains subtitles or transcribed speech.
"""

from __future__ import annotations

from collections.abc import Mapping
from collections.abc import Callable
from pathlib import Path
import tempfile
from uuid import uuid4
from urllib.parse import urlencode, urlsplit, urlunsplit

from backend.api.bilibili_audio_fetcher import BilibiliAudioFetchError, _select_audio
from backend.api.bilibili_platform_provider import (
    BilibiliMetadataProviderError,
    _canonical_source,
    _decode_view_payload,
    _normalized_metadata,
    _select_page,
)
from backend.api.bilibili_subtitle_provider import BilibiliOfficialSubtitleProvider
from backend.security.network_adapter import (
    NetworkBoundaryError,
    SafeBinaryDownloadAdapter,
    SafeTextNetworkAdapter,
)
from core.source_processing import SourceManifestCodec

from .workspace_media_url import resolve_media_url


_MAX_SOURCE_CHARS = 60_000
_MAX_AUDIO_BYTES = 64 * 1024 * 1024


def read_bilibili_media(
    url: str, runtime_root: Path, *, project_id: str = "default",
    item_id: str | None = None, run_id: str | None = None,
    validate_remote: Callable[[], None] | None = None,
    with_sections: bool = False,
) -> dict[str, object]:
    """Read a video link into a bounded, timestamped source for model review."""
    platform, resolved = resolve_media_url(url)
    if platform != "bilibili":
        raise ValueError("unsupported_media_url")
    parsed = urlsplit(resolved)
    # The old canonical parser accepts desktop URLs. Mobile links have the
    # same video identity; all tracking query keys except p are discarded.
    if (parsed.hostname or "").lower().rstrip(".") == "m.bilibili.com":
        resolved = urlunsplit((parsed.scheme, "www.bilibili.com", parsed.path, parsed.query, ""))
    try:
        bvid, page, canonical_url = _canonical_source(resolved)
    except BilibiliMetadataProviderError as error:
        raise ValueError("unsupported_media_url") from error

    network = SafeTextNetworkAdapter(
        allowed_hosts=("api.bilibili.com", "aisubtitle.hdslb.com"),
        max_redirects=0,
        max_response_bytes=4 * 1024 * 1024,
        timeout_seconds=20.0,
    )
    try:
        raw = network.fetch_text("https://api.bilibili.com/x/web-interface/view?" + urlencode({"bvid": bvid}))
        data = _decode_view_payload(raw, bvid=bvid)
        metadata = _normalized_metadata(data, _select_page(data, page), bvid=bvid, page=page)
    except (NetworkBoundaryError, BilibiliMetadataProviderError, ValueError) as error:
        raise ValueError("bilibili_metadata_unavailable") from error

    manifest = _subtitle_manifest(canonical_url, metadata)
    subtitle = BilibiliOfficialSubtitleProvider(network).resolve(manifest)
    if subtitle.available:
        assert subtitle.transcript is not None
        segments = subtitle.transcript["segments"]
        method = "official_subtitle"
    else:
        from .workspace_audio import _cloud_asr_selected

        selected_cloud = _cloud_asr_selected(runtime_root)
        segments = _transcribe_audio(
            network, metadata, runtime_root, project_id,
            item_id or "platform-bili-" + uuid4().hex,
            run_id or "workspace-run-" + uuid4().hex,
            "tokenhub-asr" if selected_cloud else "local-faster-whisper",
            validate_remote,
        )
        method = "hy_asr" if selected_cloud else "local_asr"

    source_text = _source_text(metadata, segments, method=method, canonical_url=canonical_url)
    sections = None
    if type(data.get('aid')) is int and data['aid'] > 0:
        captured = _append_comments(source_text, network, data['aid'], with_sections=with_sections)
        if with_sections:
            source_text, sections = captured['source_text'], captured['sections']
        else:
            source_text = captured
    result = {
        "source_text": source_text,
        "title": str(metadata["title"]),
        "canonical_url": canonical_url,
        "acquisition_method": method,
        "content_kind": "video",
    }
    if with_sections:
        result['_source_sections'] = sections
    return result


def _append_comments(source_text, network, aid, *, with_sections=False):
    try:
        from .v2.bilibili_comments import append_bilibili_comments, build_bilibili_comment_source
        if with_sections:
            return build_bilibili_comment_source(source_text, network, aid, maximum=_MAX_SOURCE_CHARS)
        return append_bilibili_comments(source_text, network, aid, maximum=_MAX_SOURCE_CHARS)
    except Exception:
        # An unavailable extractor must not invalidate successfully read speech.
        return {'source_text': source_text, 'sections': None} if with_sections else source_text


def _subtitle_manifest(canonical_url: str, metadata: Mapping[str, object]):
    bvid = str(metadata["bvid"])
    page = int(metadata["page"])
    source_id = f"bili-{bvid}-p{page}"
    source_ref = f"crp://workspace/sources/{source_id}"
    # Internal parser carrier only. The returned workspace result does not
    # expose this manifest or claim that an evidence record was persisted.
    parser_ref = f"crp://workspace/evidence/{source_id}-parser"
    return SourceManifestCodec.decode({
        "schema_version": "1.0.0",
        "source_id": source_id,
        "source_ref": source_ref,
        "platform": "bilibili",
        "input_identity": canonical_url,
        "resolver_revision": "bilibili-view-api-v1",
        "normalizer_revision": "bilibili-manifest-v1",
        "content_kind": "video",
        "body": None,
        "metadata": dict(metadata),
        "permission": {"decision": "unknown", "evidence_refs": [parser_ref]},
        "provenance_refs": [parser_ref],
        "assets": [{
            "asset_id": f"video-{bvid}-p{page}", "ordinal": 0, "kind": "video",
            "media_type": None, "role": "primary", "locator": canonical_url,
            "source_ref": f"{source_ref}/assets/video-{bvid}-p{page}",
            "relations": [], "evidence_refs": [parser_ref],
        }],
    })


def _transcribe_audio(
    network: SafeTextNetworkAdapter, metadata: Mapping[str, object], runtime_root: Path,
    project_id: str, item_id: str, run_id: str, expected_provider: str,
    validate_remote: Callable[[], None] | None = None,
):
    endpoint = "https://api.bilibili.com/x/player/playurl?" + urlencode({
        "bvid": metadata["bvid"], "cid": str(metadata["cid"]),
        "fnval": "16", "fnver": "0", "fourk": "0",
    })
    try:
        audio_url, _codec = _select_audio(network.fetch_text(endpoint))
    except (NetworkBoundaryError, BilibiliAudioFetchError, ValueError) as error:
        raise ValueError("bilibili_video_unavailable") from error

    staging_parent = runtime_root / "workspace" / "media-staging"
    staging_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="bili-", dir=staging_parent) as temporary:
        staging = Path(temporary)
        binary = SafeBinaryDownloadAdapter(
            staging, allowed_host_suffixes=("bilivideo.com",),
            max_redirects=0, timeout_seconds=30.0,
        )
        try:
            downloaded = binary.download(
                audio_url, relative_path="audio.m4s", max_response_bytes=_MAX_AUDIO_BYTES,
                headers={
                    "Referer": f"https://www.bilibili.com/video/{metadata['bvid']}/",
                    "Origin": "https://www.bilibili.com",
                    "User-Agent": "Mozilla/5.0",
                }, timeout_seconds=60.0,
            )
            # Bilibili serves DASH audio-only MP4 with either audio/mp4 or video/mp4.
            if downloaded.media_type not in {"audio/mp4", "video/mp4", "application/octet-stream"}:
                raise ValueError("bilibili_video_unavailable")
            from .workspace_audio import _transcribe_output

            output = _transcribe_output(
                downloaded.path, runtime_root, project_id, item_id, run_id,
                source_type="video", expected_provider=expected_provider,
                validate_remote=validate_remote,
            )
            segments = output.get("segments")
            if not isinstance(segments, list):
                raise ValueError("bilibili_video_transcription_failed")
        except ValueError as error:
            if str(error) == "bilibili_video_unavailable":
                raise
            if str(error) == "remote_processing_target_changed":
                raise
            if str(error) == "asr_unavailable":
                raise ValueError("bilibili_asr_unavailable") from error
            raise ValueError("bilibili_video_transcription_failed") from error
        except Exception as error:
            raise ValueError("bilibili_video_transcription_failed") from error
    if not segments:
        raise ValueError("bilibili_video_transcription_failed")
    return segments


def _source_text(metadata: Mapping[str, object], segments, *, method: str, canonical_url: str) -> str:
    lines = [
        "来源：B站视频",
        f"视频链接：{canonical_url}",
        f"标题（页面元数据）：{metadata['title']}",
    ]
    if metadata.get("series_title") and metadata["series_title"] != metadata["title"]:
        lines.append(f"合集标题（页面元数据）：{metadata['series_title']}")
    if metadata.get("uploader"):
        lines.append(f"UP主（页面元数据）：{metadata['uploader']}")
    if metadata.get("published_at"):
        lines.append(f"发布日期（页面元数据）：{metadata['published_at']}")
    if metadata.get("description"):
        lines.append(f"简介（页面元数据，未验证为视频内容）：{metadata['description']}")
    method_label = {
        "official_subtitle": "官方字幕", "hy_asr": "Hy-ASR 语音转写", "local_asr": "本地语音转写",
    }[method]
    lines.append("\n视频语音内容（" + method_label + "）：")
    speech_count = 0
    for item in segments:
        start = item["start_seconds"] if isinstance(item, Mapping) else item.start_seconds
        text = item["text"] if isinstance(item, Mapping) else item.text
        if isinstance(text, str) and text.strip():
            lines.append(f"[{_timestamp(float(start))}] {text.strip()}")
            speech_count += 1
    if speech_count == 0:
        raise ValueError("bilibili_video_transcription_failed")
    source_text = "\n".join(lines)
    if len(source_text) > _MAX_SOURCE_CHARS:
        raise ValueError("source_text_too_large")
    return source_text


def _timestamp(seconds: float) -> str:
    whole = max(0, int(seconds))
    hours, remainder = divmod(whole, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"
