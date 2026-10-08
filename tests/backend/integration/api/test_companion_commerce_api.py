from pathlib import Path
from types import SimpleNamespace
from fastapi.testclient import TestClient
from backend.api.app import create_app
from core.companion_core import CompanionRepository

ROOT = Path(__file__).parents[4]

def client_for(tmp_path) -> TestClient:
    return TestClient(create_app(SimpleNamespace(root_dir=tmp_path / "vault", companion_mode="development", companion_repository_root=str(ROOT))))

def test_commerce_api_purchase_feed_and_replay(tmp_path) -> None:
    client = client_for(tmp_path)
    catalog = client.get("/api/rebuild/companion/commerce")
    assert catalog.status_code == 200 and len(catalog.json()["items"]) == 5
    client.post("/api/rebuild/companion/state/daily-check-in", json={})
    body = {"offer_id": "offer:cookie", "idempotency_key": "purchase:api"}
    bought = client.post("/api/rebuild/companion/commerce/purchase", json=body)
    replay = client.post("/api/rebuild/companion/commerce/purchase", json=body)
    assert bought.status_code == 201 and bought.json()["result"]["coins_after"] == 6
    assert replay.status_code == 200 and replay.json()["result"]["replayed"] is True
    fed = client.post("/api/rebuild/companion/commerce/feed", json={"item_id": "food:cookie", "idempotency_key": "feed:api"})
    assert fed.status_code == 201 and fed.json()["result"]["quantity_after"] == 0
    assert client.get("/api/rebuild/companion/state").json()["state"]["affinity"] == 2

def test_commerce_api_rejects_unknown_fields_without_mutation(tmp_path) -> None:
    client = client_for(tmp_path)
    response = client.post("/api/rebuild/companion/commerce/purchase", json={"offer_id": "offer:cookie", "idempotency_key": "x", "coins": 999})
    assert response.status_code == 400
    assert client.get("/api/rebuild/companion/commerce").json()["coins"] == 0

def test_appearance_api_reconciles_equips_marks_story_and_rejects_extra_fields(tmp_path) -> None:
    client = client_for(tmp_path)
    initial = client.get("/api/rebuild/companion/appearance")
    assert initial.status_code == 200 and initial.json()["growth_stage"] == "new"
    repository = CompanionRepository.at_data_root(tmp_path / "vault")
    assert repository.has_state_action(
        command="daily_mood_decay", local_day=repository._now().astimezone().date().isoformat()
    ) is True
    with repository._transaction() as connection:
        connection.execute("UPDATE companion_state SET affinity=50,affinity_level=2,revision=revision+1 WHERE id='current'")
    reconciled = client.get("/api/rebuild/companion/appearance").json()
    assert reconciled["growth_stage"] == "partner"
    assert next(item for item in reconciled["outfits"] if item["id"] == "gold-star")["owned"] is True
    body = {"slot": "outfit", "selection_id": "gold-star", "idempotency_key": "equip:api"}
    equipped = client.post("/api/rebuild/companion/appearance/equip", json=body)
    replay = client.post("/api/rebuild/companion/appearance/equip", json=body)
    assert equipped.status_code == 201 and equipped.json()["result"]["outfit_id"] == "gold-star"
    assert replay.status_code == 200 and replay.json()["result"]["replayed"] is True
    assert client.post("/api/rebuild/companion/appearance/equip", json={**body, "path": "C:/secret"}).status_code == 400
    seen = client.post("/api/rebuild/companion/appearance/stories/chapter:first-trust/seen", json={})
    assert seen.status_code == 200 and seen.json()["result"]["seen"] is True
