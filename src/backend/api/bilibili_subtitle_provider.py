"""Pure, bounded Bilibili official-subtitle resolution for Media Hands.

This module deliberately has no storage, runtime composition, Cookie or media
download responsibility.  It consumes the already frozen source manifest and
turns the official Bilibili subtitle response into a deterministic transcript
candidate.  A caller can use an unavailable outcome to select its governed ASR
fallback without guessing whether an official subtitle was present.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import html
import json
import math
import re
from typing import Protocol
from urllib.parse import urlencode, urlsplit

from backend.security.network_adapter import NetworkBoundaryError
from backend.video_intake.structured import build_chunks
from core.source_processing import SourceManifest


_BVID = re.compile(r"^BV[A-Za-z0-9]{10}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_MARKUP = re.compile(r"<[^>]*>")
_CATALOG_MAX_BYTES = 512 * 1024
_SUBTITLE_MAX_BYTES = 4 * 1024 * 1024
_MAX_CUES = 20_000
_MAX_CUE_TEXT = 4_000
_MAX_TRANSCRIPT_TEXT = 1_000_000
_CATALOG_HOST = "api.bilibili.com"
_SUBTITLE_HOST = "aisubtitle.hdslb.com"


class BilibiliSubtitleProviderError(ValueError):
    """Stable local contract failure; raw network/API details never escape."""


class TextNetworkPort(Protocol):
    """A bounded, policy-owned text transport.

    The provider owns URL construction and validation only.  The injected
    transport must own DNS/public-address checks, TLS peer pinning, redirects,
    response limits and timeout enforcement.
    """

    def fetch_text(self, url: str) -> str: ...


@dataclass(frozen=True, slots=True)
class BilibiliSubtitleOutcome:
    """A transcript candidate or a stable reason to run the ASR fallback."""

    transcript: Mapping[str, object] | None
    chunks: tuple[Mapping[str, object], ...]
    unavailable_reason: str | None

    @property
    def available(self) -> bool:
        return self.transcript is not None


@dataclass(frozen=True, slots=True)
class BilibiliOfficialSubtitleProvider:
    """Fetch one official subtitle for a frozen Bilibili video manifest."""

    network: TextNetworkPort

    def resolve(self, manifest: SourceManifest) -> BilibiliSubtitleOutcome:
        bvid, cid, title, duration = _manifest_identity(manifest)
        catalog_url = "https://api.bilibili.com/x/player/v2?" + urlencode(
            {"bvid": bvid, "cid": str(cid)}
        )
        try:
            catalog = _decode_catalog(self.network.fetch_text(catalog_url))
        except (NetworkBoundaryError, BilibiliSubtitleProviderError, ValueError):
            return _unavailable("official_subtitle_catalog_unavailable")

        selected = _select_subtitle(catalog)
        if selected is None:
            return _unavailable("official_subtitle_unavailable")
        subtitle_url = _strict_subtitle_url(selected)
        if subtitle_url is None:
            return _unavailable("official_subtitle_url_rejected")
        try:
            payload = _decode_subtitle(self.network.fetch_text(subtitle_url))
            transcript = _normalized_transcript(
                payload, title=title, duration=duration, language=_language(selected)
            )
        except (NetworkBoundaryError, BilibiliSubtitleProviderError, ValueError):
            return _unavailable("official_subtitle_invalid")
        chunks = tuple(build_chunks(transcript, source_type="official_subtitle"))
        if not chunks:
            return _unavailable("official_subtitle_invalid")
        return BilibiliSubtitleOutcome(transcript, chunks, None)


def _manifest_identity(manifest: SourceManifest) -> tuple[str, int, str, float]:
    if not isinstance(manifest, SourceManifest):
        raise BilibiliSubtitleProviderError("manifest_invalid")
    if manifest.platform != "bilibili" or manifest.content_kind != "video":
        raise BilibiliSubtitleProviderError("manifest_not_bilibili_video")
    metadata = dict(manifest.metadata.entries)
    bvid = metadata.get("bvid")
    cid = metadata.get("cid")
    if not isinstance(bvid, str) or _BVID.fullmatch(bvid) is None:
        raise BilibiliSubtitleProviderError("manifest_identity_invalid")
    if not isinstance(cid, int) or isinstance(cid, bool) or cid <= 0:
        raise BilibiliSubtitleProviderError("manifest_identity_invalid")
    title = _clean_text(metadata.get("title"), limit=300) or bvid
    duration = _finite_number(metadata.get("duration_seconds"), minimum=0.0)
    return bvid, cid, title, duration


def _decode_catalog(raw: str) -> tuple[Mapping[str, object], ...]:
    payload = _json_object(raw, limit=_CATALOG_MAX_BYTES)
    if payload.get("code") != 0 or not isinstance(payload.get("data"), Mapping):
        raise BilibiliSubtitleProviderError("catalog_invalid")
    subtitle = payload["data"].get("subtitle")
    if not isinstance(subtitle, Mapping):
        return ()
    values = subtitle.get("subtitles")
    if not isinstance(values, list):
        return ()
    return tuple(item for item in values if isinstance(item, Mapping))


def _select_subtitle(catalog: tuple[Mapping[str, object], ...]) -> Mapping[str, object] | None:
    candidates: list[tuple[tuple[int, str, str], Mapping[str, object]]] = []
    for item in catalog:
        url = item.get("subtitle_url")
        language = item.get("lan")
        if not isinstance(url, str) or not isinstance(language, str):
            continue
        normalized = language.strip().lower().replace("_", "-")
        candidates.append(((_language_priority(normalized), normalized, url), item))
    return min(candidates, default=((), None), key=lambda item: item[0])[1]


def _language_priority(language: str) -> int:
    priorities = ("zh-hans", "zh-cn", "zh", "zh-hant", "en")
    try:
        return priorities.index(language)
    except ValueError:
        return len(priorities)


def _strict_subtitle_url(item: Mapping[str, object]) -> str | None:
    value = item.get("subtitle_url")
    if not isinstance(value, str) or not value or len(value) > 4_096:
        return None
    if value.startswith("//"):
        value = f"https:{value}"
    try:
        parsed = urlsplit(value)
        port = parsed.port
        host = (parsed.hostname or "").encode("idna").decode("ascii").lower().rstrip(".")
    except (ValueError, UnicodeError):
        return None
    if (
        parsed.scheme != "https"
        or host != _SUBTITLE_HOST
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or not parsed.path.startswith("/")
        or parsed.fragment
    ):
        return None
    return value


def _decode_subtitle(raw: str) -> Mapping[str, object]:
    payload = _json_object(raw, limit=_SUBTITLE_MAX_BYTES)
    body = payload.get("body")
    if not isinstance(body, list) or not body or len(body) > _MAX_CUES:
        raise BilibiliSubtitleProviderError("subtitle_invalid")
    return payload


def _normalized_transcript(
    payload: Mapping[str, object], *, title: str, duration: float, language: str
) -> dict[str, object]:
    body = payload["body"]
    assert isinstance(body, list)  # validated by _decode_subtitle
    segments: list[dict[str, object]] = []
    total_text = 0
    previous_start = -1.0
    for cue in body:
        if not isinstance(cue, Mapping):
            raise BilibiliSubtitleProviderError("subtitle_invalid")
        start = _finite_number(cue.get("from"), minimum=0.0)
        end = _finite_number(cue.get("to"), minimum=0.0)
        text = _clean_text(cue.get("content"), limit=_MAX_CUE_TEXT)
        if end <= start or start < previous_start or not text:
            raise BilibiliSubtitleProviderError("subtitle_invalid")
        total_text += len(text)
        if total_text > _MAX_TRANSCRIPT_TEXT:
            raise BilibiliSubtitleProviderError("subtitle_invalid")
        segments.append({"start_seconds": start, "end_seconds": end, "text": text})
        previous_start = start
    if not segments:
        raise BilibiliSubtitleProviderError("subtitle_invalid")
    return {
        "title": title,
        "language": language,
        "duration_seconds": duration,
        "source": "official_subtitle",
        "segments": segments,
    }


def _json_object(raw: str, *, limit: int) -> Mapping[str, object]:
    if not isinstance(raw, str) or _utf8_size(raw) > limit:
        raise BilibiliSubtitleProviderError("payload_invalid")
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as error:
        raise BilibiliSubtitleProviderError("payload_invalid") from error
    if not isinstance(value, Mapping):
        raise BilibiliSubtitleProviderError("payload_invalid")
    return value


def _utf8_size(value: str) -> int:
    try:
        return len(value.encode("utf-8", errors="strict"))
    except UnicodeEncodeError as error:
        raise BilibiliSubtitleProviderError("payload_invalid") from error


def _finite_number(value: object, *, minimum: float) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise BilibiliSubtitleProviderError("subtitle_invalid")
    result = float(value)
    if not math.isfinite(result) or result < minimum:
        raise BilibiliSubtitleProviderError("subtitle_invalid")
    return result


def _clean_text(value: object, *, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    value = html.unescape(value)
    value = _MARKUP.sub(" ", value)
    value = _CONTROL.sub(" ", value)
    return " ".join(value.split())[:limit].strip()


def _language(item: Mapping[str, object]) -> str:
    value = item.get("lan")
    assert isinstance(value, str)
    return value.strip().lower().replace("_", "-")


def _unavailable(reason: str) -> BilibiliSubtitleOutcome:
    return BilibiliSubtitleOutcome(None, (), reason)
