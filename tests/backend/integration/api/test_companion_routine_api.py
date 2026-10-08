from types import SimpleNamespace

from fastapi.testclient import TestClient

from backend.api.app import create_app


def client_for(tmp_path) -> TestClient:
    return TestClient(create_app(SimpleNamespace(root_dir=tmp_path / "vault")))


def test_routine_settings_default_roundtrip_and_conflict(tmp_path) -> None:
    client = client_for(tmp_path)
    initial = client.get("/api/rebuild/companion/settings")
    assert initial.status_code == 200
    assert initial.json()["settings"] == {"enabled": True, "sleep_start": "23:00", "wake_time": "07:00"}
    saved = client.put("/api/rebuild/companion/settings", json={
        "expected_revision": 0,
        "settings": {"enabled": True, "sleep_start": "22:30", "wake_time": "06:30"},
    })
    assert saved.status_code == 200
    assert saved.json()["revision"] == 1
    conflict = client.put("/api/rebuild/companion/settings", json={
        "expected_revision": 0,
        "settings": {"enabled": False, "sleep_start": "23:00", "wake_time": "07:00"},
    })
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "settings_conflict"


def test_routine_api_rejects_unknown_fields_equal_times_and_bad_revisions(tmp_path) -> None:
    client = client_for(tmp_path)
    assert client.put("/api/rebuild/companion/settings", json={"expected_revision": 0, "settings": {}, "path": "C:/x"}).status_code == 400
    assert client.put("/api/rebuild/companion/settings", json={
        "expected_revision": 0,
        "settings": {"enabled": True, "sleep_start": "07:00", "wake_time": "07:00"},
    }).status_code == 400
    assert client.put("/api/rebuild/companion/settings", json={
        "expected_revision": True,
        "settings": {"enabled": True, "sleep_start": "23:00", "wake_time": "07:00"},
    }).status_code == 400


def test_morning_claim_is_idempotent_without_accepting_a_client_day(tmp_path) -> None:
    client = client_for(tmp_path)
    first = client.post("/api/rebuild/companion/routine/morning-claim")
    second = client.post("/api/rebuild/companion/routine/morning-claim")
    assert first.status_code == 200 and first.json()["claimed"] is True
    assert second.status_code == 200 and second.json()["claimed"] is False
    assert first.json()["local_day"] == second.json()["local_day"]
    assert client.post("/api/rebuild/companion/routine/morning-claim", json={"local_day": "2099-01-01"}).json()["local_day"] != "2099-01-01"
