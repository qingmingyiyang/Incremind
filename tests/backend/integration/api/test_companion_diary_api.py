from __future__ import annotations

from datetime import datetime, timezone
import json
from types import SimpleNamespace

from fastapi.testclient import TestClient

from backend.api.app import create_app
from core.companion_core import CompanionRepository


def test_diary_preview_confirm_fallback_edit_delete_and_restart(tmp_path) -> None:
    repository = CompanionRepository.at_data_root(tmp_path)
    repository.initialize()
    with repository._transaction() as connection:
        connection.execute(
            "INSERT INTO companion_interaction_events(event_id,kind,value_json,occurred_at,expires_at) VALUES(?,?,?,?,NULL)",
            ("diary:focus:api", "focus", json.dumps({"minutes": 30}), datetime.now(timezone.utc).isoformat()),
        )
    api = TestClient(create_app(SimpleNamespace(root_dir=tmp_path)))
    preview = api.get("/api/rebuild/companion/diary/preview", params={"timezone_offset_minutes": 480})
    assert preview.status_code == 200
    assert preview.headers["cache-control"] == "no-store"
    assert preview.json()["summary"]["event_count"] == 1
    assert "chat_content" in preview.json()["egress"]["excluded"]
    body = {
        "request_id": "diary:api:one",
        "timezone_offset_minutes": 480,
        "preview_fingerprint": preview.json()["fingerprint"],
        "confirm_egress": True,
    }
    generated = api.post("/api/rebuild/companion/diary/generate", json=body)
    assert generated.status_code == 201
    assert generated.json()["source"] == "local"
    diary = generated.json()["diary"]
    edited = api.post(f"/api/rebuild/companion/diary/{diary['diary_id']}/edit", json={"content": "我修改了今天的观察日记。", "expected_revision": 1})
    assert edited.status_code == 201
    assert edited.json()["diary"]["revision"] == 2
    deleted = api.delete("/api/rebuild/companion/diary/events/diary:focus:api")
    assert deleted.status_code == 200
    restarted = TestClient(create_app(SimpleNamespace(root_dir=tmp_path)))
    history = restarted.get("/api/rebuild/companion/diary")
    assert len(history.json()["items"]) == 2
    assert all(item["source_status"] == "missing" for item in history.json()["items"])
    assert restarted.get("/api/rebuild/companion/diary/preview", params={"timezone_offset_minutes": 480}).json()["summary"]["event_count"] == 0


def test_diary_api_rejects_unconfirmed_stale_and_unsupported_payloads(tmp_path) -> None:
    api = TestClient(create_app(SimpleNamespace(root_dir=tmp_path)))
    preview = api.get("/api/rebuild/companion/diary/preview", params={"timezone_offset_minutes": 0}).json()
    base = {"request_id": "diary:api:bad", "timezone_offset_minutes": 0, "preview_fingerprint": preview["fingerprint"]}
    assert api.post("/api/rebuild/companion/diary/generate", json={**base, "confirm_egress": False}).status_code == 400
    assert api.post("/api/rebuild/companion/diary/generate", json={**base, "confirm_egress": True, "prompt": "override"}).status_code == 400
    assert api.get("/api/rebuild/companion/diary/preview", params={"timezone_offset_minutes": 999}).status_code == 400
