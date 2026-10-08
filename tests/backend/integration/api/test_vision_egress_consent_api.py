from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.security.secrets import InMemorySecretStore


def _client(tmp_path) -> TestClient:
    container = SimpleNamespace(root_dir=tmp_path, secret_store=InMemorySecretStore())
    return TestClient(create_app(container))


def test_vision_egress_consent_is_explicit_and_invalidated_by_endpoint_drift(tmp_path) -> None:
    client = _client(tmp_path)
    saved = client.put(
        "/api/intake/vision-settings",
        json={
            "mode": "real",
            "provider": "openai_compatible",
            "base_url": "https://vision.example/v1",
            "model": "vision-test",
            "api_key": "test-only-key",
            "max_frames": 12,
            "timeout_seconds": 60,
        },
    )
    assert saved.status_code == 200
    manifest = saved.json()["egress_manifest"]
    assert manifest["external"] is True
    assert manifest["consented"] is False

    granted = client.post(
        "/api/intake/vision-settings/egress-consent",
        json={"manifest_id": manifest["manifest_id"], "confirm": True},
    )
    assert granted.status_code == 200
    assert granted.json()["egress_manifest"]["consented"] is True

    drifted = client.put(
        "/api/intake/vision-settings",
        json={
            "mode": "real",
            "provider": "openai_compatible",
            "base_url": "https://other-vision.example/v1",
            "model": "vision-test",
            "api_key": None,
            "max_frames": 12,
            "timeout_seconds": 60,
        },
    )
    assert drifted.status_code == 200
    assert drifted.json()["egress_manifest"]["consented"] is False


def test_vision_egress_consent_rejects_stale_manifest_and_can_be_revoked(tmp_path) -> None:
    client = _client(tmp_path)
    saved = client.put(
        "/api/intake/vision-settings",
        json={
            "mode": "real",
            "provider": "openai_compatible",
            "base_url": "https://vision.example/v1",
            "model": "vision-test",
            "api_key": "test-only-key",
            "max_frames": 12,
            "timeout_seconds": 60,
        },
    )
    current = saved.json()["egress_manifest"]

    stale = client.post(
        "/api/intake/vision-settings/egress-consent",
        json={"manifest_id": "stale-manifest", "confirm": True},
    )
    assert stale.status_code == 409

    granted = client.post(
        "/api/intake/vision-settings/egress-consent",
        json={"manifest_id": current["manifest_id"], "confirm": True},
    )
    assert granted.status_code == 200
    assert granted.json()["egress_manifest"]["consented"] is True
    revoked = client.delete("/api/intake/vision-settings/egress-consent")
    assert revoked.status_code == 200
    assert revoked.json()["egress_manifest"]["consented"] is False
