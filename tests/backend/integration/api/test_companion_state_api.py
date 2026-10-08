from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from backend.api.app import create_app


ROOT = Path(__file__).parents[4]


def client_for(tmp_path) -> TestClient:
    return TestClient(create_app(SimpleNamespace(
        root_dir=tmp_path / "vault", companion_mode="development",
        companion_repository_root=str(ROOT),
    )))


def test_state_api_projects_bounded_state_and_idempotent_daily_check_in(tmp_path) -> None:
    client = client_for(tmp_path)
    initial = client.get("/api/rebuild/companion/state")
    assert initial.status_code == 200
    assert initial.json()["state"] == {
        "affinity": 0, "affinity_level": 0, "mood_score": 0, "mood": "normal", "coins": 0,
        "outfit_id": "default", "background_id": "default", "revision": 1,
        "updated_at": "1970-01-01T00:00:00+00:00",
    }
    assert initial.json()["wallet"] == []
    assert initial.json()["rules"] == {"version": 1, "affinity_thresholds": [0, 25, 50, 75, 100]}

    first = client.post("/api/rebuild/companion/state/daily-check-in", json={})
    replay = client.post("/api/rebuild/companion/state/daily-check-in", json={})
    assert first.status_code == replay.status_code == 200
    assert first.json()["result"]["changes"] == {"affinity": 0, "mood": 2, "coins": 10}
    assert first.json()["result"]["state"]["coins"] == 10
    assert replay.json()["result"]["replayed"] is True
    ledger = client.get("/api/rebuild/companion/state").json()["wallet"]
    assert len(ledger) == 1 and ledger[0]["reason"] == "每日签到" and ledger[0]["delta"] == 10


def test_state_api_rejects_client_delta_and_unknown_body_fields(tmp_path) -> None:
    client = client_for(tmp_path)
    response = client.post("/api/rebuild/companion/state/daily-check-in", json={"coins": 9999})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_state_action"
    assert client.get("/api/rebuild/companion/state").json()["state"]["coins"] == 0
