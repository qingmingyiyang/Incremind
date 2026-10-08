"""Shared pure Bilibili response and CDN validation rules.

Network execution, authorization, staging and persistence deliberately remain
with their callers.  API payload and CDN URL validation are shared by the
legacy authorized download and governed Media Hands adapters.  DASH audio
selection belongs only to the governed audio-output contract; the legacy
full-media ``durl`` contract intentionally keeps a different selector.
"""

from __future__ import annotations

from collections.abc import Mapping
import json
from urllib.parse import urlsplit


class BilibiliMediaSelectionError(ValueError):
    pass


def require_api_data(payload: object, *, operation: str) -> Mapping[str, object]:
    if not isinstance(payload, Mapping) or payload.get("code") != 0:
        raise BilibiliMediaSelectionError(f"{operation}_api_rejected")
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise BilibiliMediaSelectionError(f"{operation}_data_invalid")
    return data


def decode_api_data(raw: str, *, operation: str, max_bytes: int) -> Mapping[str, object]:
    if not isinstance(raw, str) or max_bytes < 1:
        raise BilibiliMediaSelectionError(f"{operation}_payload_invalid")
    try:
        if len(raw.encode("utf-8", errors="strict")) > max_bytes:
            raise BilibiliMediaSelectionError(f"{operation}_payload_invalid")
        payload = json.loads(raw)
    except (TypeError, UnicodeEncodeError, json.JSONDecodeError) as error:
        raise BilibiliMediaSelectionError(f"{operation}_payload_invalid") from error
    return require_api_data(payload, operation=operation)


def require_bilibili_cdn_url(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise BilibiliMediaSelectionError("media_url_invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port
        host = (parsed.hostname or "").encode("idna").decode("ascii").lower().rstrip(".")
    except (ValueError, UnicodeError) as error:
        raise BilibiliMediaSelectionError("media_url_invalid") from error
    if (
        parsed.scheme != "https"
        or not (host == "bilivideo.com" or host.endswith(".bilivideo.com"))
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or not parsed.path
        or parsed.fragment
    ):
        raise BilibiliMediaSelectionError("media_url_invalid")
    return value


def select_dash_audio(data: Mapping[str, object]) -> tuple[str, str | None]:
    dash = data.get("dash")
    values = dash.get("audio") if isinstance(dash, Mapping) else None
    if not isinstance(values, list) or not values:
        raise BilibiliMediaSelectionError("dash_audio_unavailable")
    candidates: list[tuple[int, str, str | None]] = []
    for item in values:
        if not isinstance(item, Mapping):
            continue
        try:
            url = require_bilibili_cdn_url(item.get("baseUrl") or item.get("base_url"))
        except BilibiliMediaSelectionError:
            continue
        bandwidth = item.get("bandwidth")
        codec = item.get("codecs")
        score = bandwidth if isinstance(bandwidth, int) and not isinstance(bandwidth, bool) else 0
        candidates.append((score, url, codec if isinstance(codec, str) and codec else None))
    if not candidates:
        raise BilibiliMediaSelectionError("dash_audio_url_rejected")
    _score, url, codec = max(candidates, key=lambda value: (value[0], value[1]))
    return url, codec
