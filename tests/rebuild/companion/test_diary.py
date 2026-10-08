from __future__ import annotations

from datetime import datetime, timezone

import pytest

from core.companion_core import (
    CompanionConflict,
    CompanionDiaryService,
    CompanionModelRouter,
    CompanionRepository,
    project_diary_food_names,
)


NOW = datetime(2026, 7, 22, 1, 0, tzinfo=timezone.utc)


class Provider:
    def __init__(self, text: str = "今天我认真看着御主完成了自己的安排。" * 15) -> None:
        self.text = text
        self.requests = []

    def generate(self, request):
        self.requests.append(request)
        return {"text": self.text, "usage": {"input_tokens": 10, "output_tokens": 20, "cost_usd": 0.0}}


def service(tmp_path, *, provider=None, consented=True, enabled=True):
    repo = CompanionRepository(tmp_path / "companion.sqlite3", now=lambda: NOW)
    router = CompanionModelRouter(
        provider=provider,
        provider_capabilities=("text_generation",),
        egress_consented=consented,
        enabled_routes={"companion.diary": enabled},
    )
    return repo, CompanionDiaryService(
        repo,
        model_router=router,
        character_prompt_loader=lambda: ("你是一位细心的桌面伙伴。", 4),
        food_names={"cookie": "黄油曲奇"},
        model_version="test-model-v2" if provider is not None else "local-template-v1",
        now=lambda: NOW,
    )


def test_food_name_projection_keeps_valid_food_and_skips_malformed_entries() -> None:
    assert project_diary_food_names({"items": [
        {"id": "food:apple", "kind": "food", "name": "  红苹果  "},
        42,
        {"id": "outfit:red", "kind": "outfit", "name": "红围巾"},
        {"id": "food:bad", "kind": "food", "name": "bad\x00name"},
        {"id": "outfit:wrong-kind", "kind": "food", "name": "错误 ID"},
    ]}) == {"food:apple": "红苹果"}


def test_food_name_projection_drops_conflicting_duplicate_ids() -> None:
    assert project_diary_food_names({"items": [
        {"id": "food:apple", "kind": "food", "name": "红苹果"},
        {"id": "food:apple", "kind": "food", "name": "青苹果"},
        {"id": "food:cookie", "kind": "food", "name": "曲奇"},
        {"id": "food:cookie", "kind": "food", "name": "曲奇"},
    ]}) == {"food:cookie": "曲奇"}


@pytest.mark.parametrize("catalog", (None, [], {}, {"items": {}}, {"items": "not-a-list"}))
def test_food_name_projection_recovers_from_an_unusable_catalog(catalog) -> None:
    assert project_diary_food_names(catalog) == {}


def insert_event(repo, event_id, kind, value, occurred_at):
    import json
    repo.initialize()
    with repo._transaction() as connection:
        connection.execute(
            "INSERT INTO companion_interaction_events(event_id,kind,value_json,occurred_at,expires_at) VALUES(?,?,?,?,NULL)",
            (event_id, kind, json.dumps(value, ensure_ascii=False), occurred_at),
        )


def test_preview_uses_three_local_days_and_allow_list(tmp_path):
    repo, diary = service(tmp_path)
    insert_event(repo, "diary:focus:a", "focus", {"minutes": 25}, "2026-07-19T15:59:59+00:00")
    insert_event(repo, "diary:feeding:b", "feeding", {"food_id": "cookie", "food_name": "黄油曲奇"}, "2026-07-19T16:00:00+00:00")
    insert_event(repo, "diary:mood:c", "mood", {"bucket": "happy"}, "2026-07-22T01:00:00+00:00")
    result = diary.preview(timezone_offset_minutes=480)
    assert result["summary"]["window"] == {"start": "2026-07-20", "end": "2026-07-22"}
    assert result["summary"]["event_count"] == 2
    assert result["summary"]["days"][0]["foods"] == [{"id": "cookie", "name": "黄油曲奇"}]
    assert set(result["egress"]["excluded"]) >= {"chat_content", "window_title", "clipboard", "local_path"}
    assert "25" not in str(result["summary"])


def test_generate_requires_current_fingerprint_and_explicit_confirmation(tmp_path):
    provider = Provider()
    repo, diary = service(tmp_path, provider=provider)
    insert_event(repo, "diary:focus:a", "focus", {"minutes": 50}, "2026-07-21T02:00:00+00:00")
    preview = diary.preview(timezone_offset_minutes=480)
    with pytest.raises(Exception, match="explicit egress"):
        diary.generate(request_id="diary:req:one", timezone_offset_minutes=480, preview_fingerprint=preview["fingerprint"], confirm_egress=False)
    insert_event(repo, "diary:mood:b", "mood", {"bucket": "normal"}, "2026-07-21T03:00:00+00:00")
    with pytest.raises(CompanionConflict, match="preview changed"):
        diary.generate(request_id="diary:req:one", timezone_offset_minutes=480, preview_fingerprint=preview["fingerprint"], confirm_egress=True)
    current = diary.preview(timezone_offset_minutes=480)
    result = diary.generate(request_id="diary:req:one", timezone_offset_minutes=480, preview_fingerprint=current["fingerprint"], confirm_egress=True)
    assert result["source"] == "provider"
    assert result["diary"]["provider_trace"]["prompt_revision"] == 4
    assert result["diary"]["provider_trace"]["model_version"] == "test-model-v2"
    assert "structured_three_day_summary" in provider.requests[0]["messages"][-1]["content"]
    assert "window_title" not in provider.requests[0]["messages"][-1]["content"]


def test_local_fallback_history_edit_and_deleted_source_status(tmp_path):
    repo, diary = service(tmp_path, provider=None)
    insert_event(repo, "diary:reminder:a", "reminder_completed", {"count": 1}, "2026-07-22T00:00:00+00:00")
    preview = diary.preview(timezone_offset_minutes=480)
    generated = diary.generate(request_id="diary:req:fallback", timezone_offset_minutes=480, preview_fingerprint=preview["fingerprint"], confirm_egress=True)
    assert generated["source"] == "local"
    assert "完成了 1 项日程" in generated["diary"]["content"]
    edited = diary.edit(diary_id=generated["diary"]["diary_id"], content="这是我亲手修改后的日记。", expected_revision=1)
    assert edited["edited"] is True and edited["revision"] == 2
    assert len(diary.list(local_date="2026-07-22")["items"]) == 2
    assert diary.delete_event("diary:reminder:a")["deleted"] is True
    items = diary.list(local_date="2026-07-22")["items"]
    assert all(item["source_status"] == "missing" for item in items)
    assert diary.preview(timezone_offset_minutes=480)["summary"]["event_count"] == 0


def test_invalid_stored_fields_fail_closed(tmp_path):
    repo, diary = service(tmp_path)
    insert_event(repo, "diary:focus:bad", "focus", {"minutes": 10, "window_title": "secret"}, "2026-07-22T00:00:00+00:00")
    with pytest.raises(Exception, match="fields"):
        diary.preview(timezone_offset_minutes=480)


def test_empty_fallback_and_generation_request_replay(tmp_path):
    _repo, diary = service(tmp_path, provider=None)
    preview = diary.preview(timezone_offset_minutes=0)
    first = diary.generate(request_id="diary:req:empty", timezone_offset_minutes=0, preview_fingerprint=preview["fingerprint"], confirm_egress=True)
    replay = diary.generate(request_id="diary:req:empty", timezone_offset_minutes=0, preview_fingerprint=preview["fingerprint"], confirm_egress=True)
    assert first["diary"]["source_event_ids"] == []
    assert replay["replayed"] is True
    assert replay["diary"]["diary_id"] == first["diary"]["diary_id"]
    assert len(diary.list()["items"]) == 1


def test_cancelled_generation_does_not_persist_diary(tmp_path):
    _repo, diary = service(tmp_path, provider=Provider())
    preview = diary.preview(timezone_offset_minutes=0)
    with pytest.raises(CompanionConflict, match="cancelled"):
        diary.generate(request_id="diary:req:cancel", timezone_offset_minutes=0, preview_fingerprint=preview["fingerprint"], confirm_egress=True, cancelled=lambda: True)
    assert diary.list()["items"] == []


def test_preview_projects_completed_authority_rows_without_sensitive_bodies(tmp_path):
    repo, diary = service(tmp_path)
    repo.initialize()
    stamp = "2026-07-22T00:00:00+00:00"
    import json
    with repo._transaction() as connection:
        connection.execute("INSERT INTO companion_focus_sessions(session_id,status,target_seconds,elapsed_seconds,supervision_enabled,work_processes_json,distracting_processes_json,warning_count,last_warning_at,reward_state,revision,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", ("focus_source", "completed", 1800, 1800, 0, "[]", "[]", 0, None, "rewarded", 2, stamp, stamp))
        connection.execute("INSERT INTO companion_commerce_receipts(receipt_id,idempotency_key,operation,item_id,catalog_version,quantity_after,inventory_revision,price,state_action_id,result_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", ("commerce_source", "feed-source", "feed", "cookie", 1, 0, 2, 0, None, "{}", stamp))
        connection.execute("INSERT INTO companion_reminders(reminder_id,schedule_json,advance_minutes,next_fire_at,ack_state,revision,updated_at) VALUES(?,?,?,?,?,?,?)", ("reminder_source", json.dumps({"title": "private title"}), 0, None, "completed", 2, stamp))
        connection.execute("INSERT INTO companion_reminder_occurrences(occurrence_id,reminder_id,scheduled_for,fire_at,phase,state,snooze_until,revision,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)", ("occurrence_source", "reminder_source", stamp, stamp, "due", "completed", None, 2, stamp, stamp))
        connection.execute("INSERT INTO companion_random_events(event_id,prompt_ref,options_json,selected_option,result_json,state,revision,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)", ("event_source", "coin-on-path", "{}", "return", json.dumps({"changes": {"affinity": 1, "mood": 1, "coins": 0}}), "settled", 2, stamp, stamp))
    result = diary.preview(timezone_offset_minutes=480)
    kinds = {event["kind"] for event in result["events"]}
    assert {"focus", "feeding", "reminder_completed", "random_event"} <= kinds
    assert result["summary"]["days"][-1]["focus_minutes"] == 30
    assert result["summary"]["days"][-1]["foods"] == [{"id": "cookie", "name": "黄油曲奇"}]
    assert "private title" not in str(result)
