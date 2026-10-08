from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
from dataclasses import dataclass


FILE_GRANT_REVISION = "desktop-file-grant-v1"
FILE_GRANT_MAX_BYTES = 16 * 1024 * 1024 * 1024
FILE_GRANT_MAX_TTL_SECONDS = 5 * 60
_GRANT_ID = re.compile(r"^file-grant-[A-Za-z0-9_-]{32,128}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class DesktopFileGrantError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class DesktopFileGrant:
    grant_id: str
    session_instance_id: str
    display_name: str
    media_type: str
    source_kind: str
    size_bytes: int
    sha256: str
    expires_at_ms: int


def verify_desktop_file_grant(
    headers: dict[str, str],
    *,
    session_secret: str,
    session_instance_id: str,
    now_ms: int | None = None,
) -> DesktopFileGrant:
    grant = DesktopFileGrant(
        grant_id=_header(headers, "x-chriptmas-file-grant"),
        session_instance_id=_header(headers, "x-chriptmas-file-session"),
        display_name=_decode_display_name(_header(headers, "x-chriptmas-file-name")),
        media_type=_header(headers, "x-chriptmas-file-media-type"),
        source_kind=_header(headers, "x-chriptmas-file-source-kind"),
        size_bytes=_integer_header(headers, "x-chriptmas-file-size"),
        sha256=_header(headers, "x-chriptmas-file-sha256"),
        expires_at_ms=_integer_header(headers, "x-chriptmas-file-expires"),
    )
    signature = _header(headers, "x-chriptmas-file-signature")
    _validate(grant, session_instance_id=session_instance_id, now_ms=now_ms)
    expected = sign_desktop_file_grant(grant, session_secret=session_secret)
    if not hmac.compare_digest(signature, expected):
        raise DesktopFileGrantError("file_grant_signature_invalid")
    return grant


def sign_desktop_file_grant(grant: DesktopFileGrant, *, session_secret: str) -> str:
    if len(session_secret) < 43:
        raise DesktopFileGrantError("file_grant_session_invalid")
    payload = json.dumps(
        {
            "revision": FILE_GRANT_REVISION,
            "grant_id": grant.grant_id,
            "session_instance_id": grant.session_instance_id,
            "display_name": grant.display_name,
            "media_type": grant.media_type,
            "source_kind": grant.source_kind,
            "size_bytes": grant.size_bytes,
            "sha256": grant.sha256,
            "expires_at_ms": grant.expires_at_ms,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hmac.new(session_secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def _validate(grant: DesktopFileGrant, *, session_instance_id: str, now_ms: int | None) -> None:
    current = int(time.time() * 1000) if now_ms is None else now_ms
    if not _GRANT_ID.fullmatch(grant.grant_id):
        raise DesktopFileGrantError("file_grant_id_invalid")
    if not session_instance_id or grant.session_instance_id != session_instance_id:
        raise DesktopFileGrantError("file_grant_session_mismatch")
    if not grant.display_name or len(grant.display_name.encode("utf-8")) > 720:
        raise DesktopFileGrantError("file_grant_name_invalid")
    if not re.fullmatch(r"[a-z0-9.+-]+/[a-z0-9.+-]+", grant.media_type):
        raise DesktopFileGrantError("file_grant_media_type_invalid")
    if grant.source_kind not in {"file", "image", "audio", "video"}:
        raise DesktopFileGrantError("file_grant_source_kind_invalid")
    if grant.size_bytes < 0 or grant.size_bytes > FILE_GRANT_MAX_BYTES:
        raise DesktopFileGrantError("file_grant_size_invalid")
    if not _SHA256.fullmatch(grant.sha256):
        raise DesktopFileGrantError("file_grant_sha256_invalid")
    if grant.expires_at_ms <= current:
        raise DesktopFileGrantError("file_grant_expired")
    if grant.expires_at_ms > current + FILE_GRANT_MAX_TTL_SECONDS * 1000:
        raise DesktopFileGrantError("file_grant_ttl_invalid")


def _header(headers: dict[str, str], name: str) -> str:
    value = headers.get(name, "")
    if not isinstance(value, str) or not value.strip():
        raise DesktopFileGrantError(f"{name}_required")
    return value.strip()


def _integer_header(headers: dict[str, str], name: str) -> int:
    try:
        return int(_header(headers, name))
    except ValueError as error:
        raise DesktopFileGrantError(f"{name}_invalid") from error


def _decode_display_name(value: str) -> str:
    try:
        padding = "=" * (-len(value) % 4)
        decoded = base64.urlsafe_b64decode(value + padding).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as error:
        raise DesktopFileGrantError("file_grant_name_invalid") from error
    if not decoded or "/" in decoded or "\\" in decoded or "\0" in decoded:
        raise DesktopFileGrantError("file_grant_name_invalid")
    return decoded
