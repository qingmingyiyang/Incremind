from types import SimpleNamespace
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from backend.api.app import create_app
from core.companion_core.scheduler import CompanionEvent, EventPriority


def test_reminder_api_create_list_cancel_and_conflict(tmp_path) -> None:
    client = TestClient(create_app(SimpleNamespace(root_dir=tmp_path / "vault")))
    assert client.get("/api/rebuild/companion/reminders").json() == {"items": []}
    body = {
        "title": "明天复盘", "scheduled_at": "2099-07-21T02:00:00.000Z", "timezone": "Asia/Shanghai",
        "advance_minutes": 5, "recurrence": "once", "repeat_count": 1,
    }
    created = client.post("/api/rebuild/companion/reminders", json=body)
    assert created.status_code == 201
    item = created.json()["reminder"]
    assert item["title"] == "明天复盘" and item["state"] == "pending" and item["revision"] == 1
    assert client.get("/api/rebuild/companion/reminders").json()["items"] == [item]
    conflict = client.post(f"/api/rebuild/companion/reminders/{item['reminder_id']}/cancel", json={"expected_revision": 2})
    assert conflict.status_code == 409
    cancelled = client.post(f"/api/rebuild/companion/reminders/{item['reminder_id']}/cancel", json={"expected_revision": 1})
    assert cancelled.status_code == 200 and cancelled.json()["reminder"]["state"] == "cancelled"
    assert client.get("/api/rebuild/companion/reminders").json() == {"items": []}


def test_reminder_api_rejects_unknown_fields_bad_timezone_past_and_unbounded_repeat(tmp_path) -> None:
    client = TestClient(create_app(SimpleNamespace(root_dir=tmp_path / "vault")))
    base = {
        "title": "事项", "scheduled_at": "2099-07-21T02:00:00.000Z", "timezone": "UTC",
        "advance_minutes": 5, "recurrence": "once", "repeat_count": 1,
    }
    for patch in [
        {"path": "C:/secret"}, {"timezone": "Mars/Base"}, {"scheduled_at": "2000-01-01T00:00:00Z"},
        {"recurrence": "daily", "repeat_count": 366},
    ]:
        payload = {**base, **patch}
        response = client.post("/api/rebuild/companion/reminders", json=payload)
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_reminder"


def test_event_drain_has_bounded_schema_and_action_allowlist(tmp_path) -> None:
    app = create_app(SimpleNamespace(root_dir=tmp_path / "vault"))
    runtime = app.state.companion_scheduler_runtime
    now = datetime.now(timezone.utc)
    runtime.scheduler.store.enqueue(CompanionEvent(
        event_id="hour:test", kind="hourly_chime", priority=EventPriority.AMBIENT,
        created_at=now, expires_at=now + timedelta(minutes=2), dedupe_key="hour:test",
        visual_state="speaking", text="整点了", sound_key="hourly-default",
    ))
    client = TestClient(app)
    response = client.get("/api/rebuild/companion/events/next")
    assert response.status_code == 200
    assert response.json() == {"event": {
        "event_id": "hour:test", "kind": "hourly_chime", "priority": "ambient",
        "visual_state": "speaking", "text": "整点了", "actions": [],
        "requires_ack": False, "sound_key": "hourly-default",
    }}
    assert client.get("/api/rebuild/companion/events/next").json() == {"event": None}
    rejected = client.post("/api/rebuild/companion/events/hour:test/action", json={"action": "launch_program"})
    assert rejected.status_code == 400
