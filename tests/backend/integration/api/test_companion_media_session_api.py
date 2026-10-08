from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from backend.api.app import create_app
from core.companion_core import CompanionRepository


def client(tmp_path) -> TestClient:
    return TestClient(create_app(SimpleNamespace(root_dir=tmp_path)))


def test_media_session_defaults_closed_and_settings_are_cas_persistent(tmp_path) -> None:
    api = client(tmp_path)
    initial = api.get("/api/rebuild/companion/media-session")
    saved = api.put("/api/rebuild/companion/media-session/settings", json={
        "enabled": True, "model_commentary_enabled": False, "expected_revision": 0,
    })
    conflict = api.put("/api/rebuild/companion/media-session/settings", json={
        "enabled": False, "model_commentary_enabled": False, "expected_revision": 0,
    })
    restarted = client(tmp_path).get("/api/rebuild/companion/media-session")

    assert initial.status_code == 200
    assert initial.headers["cache-control"] == "no-store"
    assert initial.json()["config"] == {"enabled": False, "model_commentary_enabled": False}
    assert saved.status_code == 200 and saved.json()["revision"] == 1
    assert conflict.status_code == 409
    assert restarted.json()["config"]["enabled"] is True


def test_media_observation_returns_bounded_projection_and_persists_no_metadata(tmp_path) -> None:
    api = client(tmp_path)
    api.put("/api/rebuild/companion/media-session/settings", json={
        "enabled": True, "model_commentary_enabled": False, "expected_revision": 0,
    })
    title = "CP_E03_API_TITLE_CANARY"
    artist = "CP_E03_API_ARTIST_CANARY"
    response = api.post("/api/rebuild/companion/media-session/observe", json={
        "observation_id": "media:abcdef12-3456",
        "title": title,
        "artist": artist,
        "playback_status": "playing",
        "quiet": False,
    })
    duplicate = api.post("/api/rebuild/companion/media-session/observe", json={
        "observation_id": "media:abcdef12-3457",
        "title": title,
        "artist": artist,
        "playback_status": "playing",
        "quiet": False,
    })

    assert response.status_code == 200
    assert set(response.json()) == {"result"}
    result = response.json()["result"]
    assert set(result) == {"status", "title", "artist", "playback_status", "commentary", "commentary_source", "reason"}
    assert result["commentary_source"] == "local"
    assert duplicate.json()["result"]["reason"] == "unchanged"
    database = CompanionRepository.at_data_root(tmp_path).database_path.read_bytes()
    assert title.encode() not in database and artist.encode() not in database


def test_media_api_rejects_extra_fields_invalid_egress_and_disabled_observation(tmp_path) -> None:
    api = client(tmp_path)
    extra = api.put("/api/rebuild/companion/media-session/settings", json={
        "enabled": True, "model_commentary_enabled": False, "expected_revision": 0, "url": "https://bad.invalid",
    })
    invalid_egress = api.put("/api/rebuild/companion/media-session/settings", json={
        "enabled": False, "model_commentary_enabled": True, "expected_revision": 0,
    })
    disabled = api.post("/api/rebuild/companion/media-session/observe", json={
        "observation_id": "media:abcdef12-3456", "title": "测试", "artist": "",
        "playback_status": "playing", "quiet": False,
    })
    invalid = api.post("/api/rebuild/companion/media-session/observe", json={
        "observation_id": "media:abcdef12-3456", "title": "测试", "artist": "",
        "playback_status": "playing", "quiet": False, "source": "private-player",
    })

    assert extra.status_code == 400
    assert invalid_egress.status_code == 400
    assert disabled.status_code == 200 and disabled.json()["result"]["reason"] == "disabled"
    assert invalid.status_code == 400
    assert "private-player" not in invalid.text
