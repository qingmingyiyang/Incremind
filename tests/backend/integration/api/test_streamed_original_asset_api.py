from __future__ import annotations

import hashlib
import base64
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.api.desktop_session import DESKTOP_SESSION_HEADER
from backend.security.file_grant import DesktopFileGrant, sign_desktop_file_grant
from core.storage_provider import JsonObjectStore


SECRET = "s" * 43
INSTANCE = "instance-stream-test"


def _configure_desktop(monkeypatch) -> None:
    monkeypatch.setenv("CHRIPTMAS_DESKTOP_SESSION_MODE", "desktop_production")
    monkeypatch.setenv("CHRIPTMAS_DESKTOP_SESSION_SECRET", SECRET)
    monkeypatch.setenv("CHRIPTMAS_DESKTOP_INSTANCE_ID", INSTANCE)
    monkeypatch.setenv("CHRIPTMAS_DESKTOP_NONCE", "n" * 43)
    monkeypatch.setenv("CHRIPTMAS_DESKTOP_PROTOCOL_VERSION", "desktop-loopback/1")
    monkeypatch.setenv("CHRIPTMAS_DESKTOP_SESSION_EXPIRES_AT", (datetime.now(UTC) + timedelta(hours=1)).isoformat())
    monkeypatch.setenv("CHRIPTMAS_DESKTOP_ALLOWED_ORIGIN", "http://127.0.0.1:8317")


def _headers(content: bytes, *, grant_id: str = "file-grant-" + "a" * 43, sha256: str | None = None) -> dict[str, str]:
    grant = DesktopFileGrant(
        grant_id=grant_id,
        session_instance_id=INSTANCE,
        display_name="fixture-video.mp4",
        media_type="video/mp4",
        source_kind="video",
        size_bytes=len(content),
        sha256=sha256 or hashlib.sha256(content).hexdigest(),
        expires_at_ms=int((datetime.now(UTC) + timedelta(minutes=1)).timestamp() * 1000),
    )
    return {
        DESKTOP_SESSION_HEADER: SECRET,
        "X-Chriptmas-File-Grant": grant.grant_id,
        "X-Chriptmas-File-Session": grant.session_instance_id,
        "X-Chriptmas-File-Name": base64.urlsafe_b64encode(grant.display_name.encode()).decode().rstrip("="),
        "X-Chriptmas-File-Media-Type": grant.media_type,
        "X-Chriptmas-File-Source-Kind": grant.source_kind,
        "X-Chriptmas-File-Size": str(grant.size_bytes),
        "X-Chriptmas-File-Sha256": grant.sha256,
        "X-Chriptmas-File-Expires": str(grant.expires_at_ms),
        "X-Chriptmas-File-Signature": sign_desktop_file_grant(grant, session_secret=SECRET),
        "Content-Type": "application/octet-stream",
    }


def test_streamed_asset_is_hashed_stored_and_idempotent(tmp_path, monkeypatch) -> None:
    _configure_desktop(monkeypatch)
    content = b"streamed-original" * 64 * 1024
    headers = _headers(content)
    with TestClient(create_app(SimpleNamespace(root_dir=tmp_path))) as client:
        first = client.post("/api/rebuild/workbench/original-asset-stream", headers=headers, content=content)
        second = client.post("/api/rebuild/workbench/original-asset-stream", headers=headers, content=content)

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["asset_id"] == second.json()["asset_id"]
    assert first.json()["sha256"] == hashlib.sha256(content).hexdigest()
    store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    assert len(store.list("workbench_original_assets")) == 1
    assert store.revision("workbench_original_assets", first.json()["asset_id"]) == 1
    stored = tmp_path / "library" / first.json()["vault_ref"]
    assert stored.read_bytes() == content


def test_streamed_asset_rejects_hash_drift_and_removes_partial_file(tmp_path, monkeypatch) -> None:
    _configure_desktop(monkeypatch)
    content = b"expected-content"
    with TestClient(create_app(SimpleNamespace(root_dir=tmp_path))) as client:
        response = client.post(
            "/api/rebuild/workbench/original-asset-stream",
            headers=_headers(content, sha256="0" * 64),
            content=content,
        )

    assert response.status_code == 400
    assert "sha256" in response.json()["detail"]
    assert not list((tmp_path / "library" / "assets" / "originals" / ".incoming").glob("*.part"))
    store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    assert store.list("workbench_original_assets") == ()


def test_streamed_asset_rejects_low_disk_before_creating_partial(tmp_path, monkeypatch) -> None:
    _configure_desktop(monkeypatch)
    content = b"long-media-budget-fixture"

    def reject_budget(_root, *, incoming_bytes):
        assert incoming_bytes == len(content)
        from backend.security import StreamStorageBudgetError

        raise StreamStorageBudgetError("stream_storage_budget_insufficient")

    monkeypatch.setattr(
        "backend.api.routes.workbench_original_asset.require_stream_storage_budget",
        reject_budget,
    )
    with TestClient(create_app(SimpleNamespace(root_dir=tmp_path))) as client:
        response = client.post(
            "/api/rebuild/workbench/original-asset-stream",
            headers=_headers(content),
            content=content,
        )

    assert response.status_code == 507
    assert response.json() == {
        "detail": "stream_storage_budget_insufficient",
        "actionable": True,
    }
    incoming = tmp_path / "library" / "assets" / "originals" / ".incoming"
    assert not incoming.exists() or list(incoming.iterdir()) == []
