from types import SimpleNamespace
from pathlib import Path

from fastapi.testclient import TestClient

from backend.api.app import create_app
from core.companion_core import CompanionRepository


ROOT = Path(__file__).parents[4]


def test_companion_petting_interaction_roundtrip_is_idempotent(tmp_path) -> None:
    client = TestClient(create_app(SimpleNamespace(
        root_dir=tmp_path, companion_mode="development", companion_repository_root=str(ROOT),
    )))
    payload = {"event_id": "gesture:petting:api-one", "kind": "petting"}

    created = client.post("/api/rebuild/companion/interactions", json=payload)
    replay = client.post("/api/rebuild/companion/interactions", json=payload)

    assert created.status_code == 201
    assert created.headers["cache-control"] == "no-store"
    assert created.json()["replayed"] is False
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert created.json()["state_action"]["changes"]["affinity"] == 2
    assert replay.json()["state_action"]["replayed"] is True
    assert CompanionRepository.at_data_root(tmp_path).get_state_snapshot().affinity == 2
    assert CompanionRepository.at_data_root(tmp_path).wallet_integrity().snapshot_balance == 0


def test_companion_interaction_api_rejects_extra_fields_and_unknown_kinds(tmp_path) -> None:
    client = TestClient(create_app(SimpleNamespace(root_dir=tmp_path)))
    assert client.post(
        "/api/rebuild/companion/interactions",
        json={"event_id": "gesture:petting:bad", "kind": "petting", "affinity": 100},
    ).status_code == 400
    assert client.post(
        "/api/rebuild/companion/interactions",
        json={"event_id": "gesture:feeding:bad", "kind": "feeding"},
    ).status_code == 400
    assert client.post(
        "/api/rebuild/companion/interactions",
        content="not-json",
        headers={"Content-Type": "application/json"},
    ).status_code == 400
