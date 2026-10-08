from __future__ import annotations

import base64
from dataclasses import asdict

import pytest

from backend.security.file_grant import (
    DesktopFileGrant,
    DesktopFileGrantError,
    sign_desktop_file_grant,
    verify_desktop_file_grant,
)


SECRET = "s" * 43
INSTANCE = "instance-test"
NOW = 1_720_000_000_000


def _headers(**changes: object) -> dict[str, str]:
    grant = DesktopFileGrant(
        grant_id="file-grant-" + "a" * 43,
        session_instance_id=INSTANCE,
        display_name="two-hour-video.mp4",
        media_type="video/mp4",
        source_kind="video",
        size_bytes=8 * 1024 * 1024,
        sha256="b" * 64,
        expires_at_ms=NOW + 60_000,
    )
    values = {**asdict(grant), **changes}
    current = DesktopFileGrant(**values)
    return {
        "x-chriptmas-file-grant": current.grant_id,
        "x-chriptmas-file-session": current.session_instance_id,
        "x-chriptmas-file-name": base64.urlsafe_b64encode(current.display_name.encode()).decode().rstrip("="),
        "x-chriptmas-file-media-type": current.media_type,
        "x-chriptmas-file-source-kind": current.source_kind,
        "x-chriptmas-file-size": str(current.size_bytes),
        "x-chriptmas-file-sha256": current.sha256,
        "x-chriptmas-file-expires": str(current.expires_at_ms),
        "x-chriptmas-file-signature": sign_desktop_file_grant(current, session_secret=SECRET),
    }


def test_current_session_grant_verifies_without_a_path() -> None:
    verified = verify_desktop_file_grant(
        _headers(), session_secret=SECRET, session_instance_id=INSTANCE, now_ms=NOW
    )
    assert verified.display_name == "two-hour-video.mp4"
    assert verified.size_bytes == 8 * 1024 * 1024
    assert all("path" not in key for key in _headers())


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"session_instance_id": "other"}, "session_mismatch"),
        ({"expires_at_ms": NOW}, "expired"),
        ({"expires_at_ms": NOW + 301_000}, "ttl_invalid"),
        ({"size_bytes": 17 * 1024 * 1024 * 1024}, "size_invalid"),
    ],
)
def test_grant_rejects_scope_drift(change: dict[str, object], message: str) -> None:
    with pytest.raises(DesktopFileGrantError, match=message):
        verify_desktop_file_grant(
            _headers(**change), session_secret=SECRET, session_instance_id=INSTANCE, now_ms=NOW
        )


def test_grant_rejects_tampered_metadata() -> None:
    headers = _headers()
    headers["x-chriptmas-file-size"] = "1"
    with pytest.raises(DesktopFileGrantError, match="signature_invalid"):
        verify_desktop_file_grant(
            headers, session_secret=SECRET, session_instance_id=INSTANCE, now_ms=NOW
        )
