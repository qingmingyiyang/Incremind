"""Governed anonymous Bilibili audio acquisition for Media Hands.

The model never supplies a CDN URL or destination.  This adapter derives both
from the frozen SourceManifest and an adapter-owned staging root.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Protocol
from urllib.parse import urlencode

from backend.api.bilibili_subtitle_provider import TextNetworkPort
from backend.bilibili.media_selection import (
    BilibiliMediaSelectionError,
    decode_api_data,
    select_dash_audio,
)
from backend.security import DownloadedBinary
from core.job_runner.media_execution_receipt import media_job_uri_segment
from core.source_processing import SourceManifest


_BVID = re.compile(r"^BV[A-Za-z0-9]{10}$")
_PLAYURL_MAX_BYTES = 1024 * 1024
_CANONICAL_AUDIO_MEDIA_TYPE = "audio/mp4"
_BILIBILI_AUDIO_RESPONSE_TYPES = frozenset(
    {_CANONICAL_AUDIO_MEDIA_TYPE, "application/octet-stream"}
)


class BilibiliAudioFetchError(ValueError):
    """Stable media acquisition failure without raw platform response details."""


class BinaryDownloadPort(Protocol):
    def download(
        self,
        url: str,
        *,
        relative_path: str,
        max_response_bytes: int,
        headers: Mapping[str, str] | None = None,
        control_check: Callable[[], None] | None = None,
        timeout_seconds: float | None = None,
    ) -> DownloadedBinary: ...

    def staged_path(self, relative_path: str) -> Path: ...


@dataclass(frozen=True, slots=True)
class BilibiliStagedAudio:
    path: str
    byte_count: int
    media_type: str
    codec: str | None


@dataclass(frozen=True, slots=True)
class BilibiliAnonymousAudioFetcher:
    catalog_network: TextNetworkPort
    binary_network: BinaryDownloadPort

    def fetch(
        self,
        manifest: SourceManifest,
        *,
        job_id: str,
        max_download_bytes: int,
        timeout_seconds: float,
        control_check: Callable[[], None] | None = None,
    ) -> BilibiliStagedAudio:
        bvid, cid = _manifest_identity(manifest)
        if max_download_bytes < 1 or timeout_seconds <= 0:
            raise BilibiliAudioFetchError("audio_download_budget_exhausted")
        if control_check is not None:
            control_check()
        playurl = "https://api.bilibili.com/x/player/playurl?" + urlencode(
            {
                "bvid": bvid,
                "cid": str(cid),
                "fnval": "16",
                "fnver": "0",
                "fourk": "0",
            }
        )
        selection = _select_audio(self.catalog_network.fetch_text(playurl))
        result = self.binary_network.download(
            selection[0],
            relative_path=f"{media_job_uri_segment(job_id)}/source-audio.m4s",
            max_response_bytes=max_download_bytes,
            headers={
                "Referer": f"https://www.bilibili.com/video/{bvid}/",
                "Origin": "https://www.bilibili.com",
            },
            control_check=control_check,
            timeout_seconds=timeout_seconds,
        )
        if result.media_type not in _BILIBILI_AUDIO_RESPONSE_TYPES:
            result.path.unlink(missing_ok=True)
            raise BilibiliAudioFetchError("audio_media_type_invalid")
        return BilibiliStagedAudio(
            path=str(result.path),
            byte_count=result.byte_count,
            media_type=_CANONICAL_AUDIO_MEDIA_TYPE,
            codec=selection[1],
        )

    def restore(
        self, *, job_id: str, namespace_id: str, receipt: Mapping[str, object]
    ) -> BilibiliStagedAudio:
        relative_path = f"{media_job_uri_segment(job_id)}/source-audio.m4s"
        expected_ref = (
            f"crp://{namespace_id}/jobs/{media_job_uri_segment(job_id)}/staging/source-audio"
        )
        consumed = receipt.get("consumed")
        byte_count = consumed.get("download_octets") if isinstance(consumed, Mapping) else None
        if (
            receipt.get("step_name") != "fetch_audio"
            or receipt.get("output_ref") != expected_ref
            or not isinstance(byte_count, int)
            or isinstance(byte_count, bool)
            or byte_count < 1
        ):
            raise BilibiliAudioFetchError("audio_step_receipt_invalid")
        path = self.binary_network.staged_path(relative_path)
        if path.stat().st_size != byte_count:
            raise BilibiliAudioFetchError("audio_staging_receipt_mismatch")
        expected_hash = "sha256:" + hashlib.sha256(json.dumps(
            {
                "output_ref": expected_ref,
                "byte_count": byte_count,
                "media_type": _CANONICAL_AUDIO_MEDIA_TYPE,
            },
            ensure_ascii=True, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        if receipt.get("output_state_hash") != expected_hash:
            raise BilibiliAudioFetchError("audio_staging_receipt_mismatch")
        return BilibiliStagedAudio(
            path=str(path), byte_count=byte_count,
            media_type=_CANONICAL_AUDIO_MEDIA_TYPE, codec=None
        )


def _manifest_identity(manifest: SourceManifest) -> tuple[str, int]:
    if not isinstance(manifest, SourceManifest):
        raise BilibiliAudioFetchError("manifest_invalid")
    if (
        manifest.platform != "bilibili"
        or manifest.content_kind != "video"
        or manifest.permission.decision != "granted"
    ):
        raise BilibiliAudioFetchError("manifest_not_authorized_bilibili_video")
    metadata = dict(manifest.metadata.entries)
    bvid = metadata.get("bvid")
    cid = metadata.get("cid")
    if (
        not isinstance(bvid, str)
        or _BVID.fullmatch(bvid) is None
        or not isinstance(cid, int)
        or isinstance(cid, bool)
        or cid <= 0
    ):
        raise BilibiliAudioFetchError("manifest_identity_invalid")
    return bvid, cid


def _select_audio(raw: str) -> tuple[str, str | None]:
    try:
        return select_dash_audio(
            decode_api_data(raw, operation="playurl", max_bytes=_PLAYURL_MAX_BYTES)
        )
    except BilibiliMediaSelectionError as error:
        reason = str(error)
        if reason == "dash_audio_unavailable":
            raise BilibiliAudioFetchError("anonymous_audio_unavailable") from error
        if reason == "dash_audio_url_rejected":
            raise BilibiliAudioFetchError("anonymous_audio_url_rejected") from error
        if reason == "playurl_api_rejected":
            raise BilibiliAudioFetchError("playurl_unavailable") from error
        raise BilibiliAudioFetchError("playurl_payload_invalid") from error
