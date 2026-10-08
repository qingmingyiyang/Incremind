from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import re
from typing import Protocol

from .errors import CompanionConflict, CompanionRepositoryError
from .model_routes import CompanionModelRouter, compose_companion_prompt
from .repository import CompanionRepository


_ID = re.compile(r"^[a-z0-9][a-z0-9:_-]{0,127}$")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_KINDS = {"focus", "feeding", "mood", "reminder_completed", "random_event"}


class CharacterPromptLoader(Protocol):
    def __call__(self) -> tuple[str, int]: ...


def project_diary_food_names(catalog: object) -> dict[str, str]:
    if not isinstance(catalog, Mapping):
        return {}
    items = catalog.get("items")
    if not isinstance(items, list):
        return {}
    projected: dict[str, str] = {}
    conflicted: set[str] = set()
    for item in items:
        if not isinstance(item, Mapping) or item.get("kind") != "food":
            continue
        item_id = item.get("id")
        name = item.get("name")
        if not isinstance(item_id, str) or not item_id.startswith("food:") or _ID.fullmatch(item_id) is None:
            continue
        if not isinstance(name, str):
            continue
        normalized_name = name.strip()
        if not normalized_name or len(normalized_name) > 80 or _CONTROL.search(normalized_name):
            continue
        if item_id in conflicted:
            continue
        previous = projected.get(item_id)
        if previous is not None and previous != normalized_name:
            projected.pop(item_id, None)
            conflicted.add(item_id)
            continue
        projected[item_id] = normalized_name
    return projected


class CompanionDiaryService:
    """Build a consent-visible diary from a three-local-day, allow-listed projection."""

    def __init__(
        self,
        repository: CompanionRepository,
        *,
        model_router: CompanionModelRouter,
        character_prompt_loader: CharacterPromptLoader,
        food_names: Mapping[str, str] | None = None,
        model_version: str = "local",
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.repository = repository
        self.model_router = model_router
        self.character_prompt_loader = character_prompt_loader
        self.food_names = {str(key): str(value) for key, value in (food_names or {}).items()}
        self.model_version = model_version if isinstance(model_version, str) and model_version else "local"
        self.now = now or (lambda: datetime.now(timezone.utc))

    def preview(self, *, timezone_offset_minutes: object) -> dict[str, object]:
        offset = _offset(timezone_offset_minutes)
        now = _utc(self.now())
        self.repository.initialize()
        self._refresh_projection(now=now)
        local_now = now.astimezone(timezone(timedelta(minutes=offset)))
        start_day = local_now.date() - timedelta(days=2)
        end_day = local_now.date()
        start_utc = datetime.combine(start_day, datetime.min.time(), tzinfo=local_now.tzinfo).astimezone(timezone.utc)
        end_utc = datetime.combine(end_day + timedelta(days=1), datetime.min.time(), tzinfo=local_now.tzinfo).astimezone(timezone.utc)
        connection = self.repository._open_connection()
        try:
            rows = connection.execute(
                "SELECT event_id,kind,value_json,occurred_at FROM companion_interaction_events "
                "WHERE occurred_at>=? AND occurred_at<? AND kind IN ('focus','feeding','mood','reminder_completed','random_event') "
                "ORDER BY occurred_at,event_id",
                (start_utc.isoformat(), end_utc.isoformat()),
            ).fetchall()
        finally:
            connection.close()
        events: list[dict[str, object]] = []
        days = {str(start_day + timedelta(days=index)): _empty_day(str(start_day + timedelta(days=index))) for index in range(3)}
        for row in rows:
            value = _safe_value(str(row["kind"]), row["value_json"])
            occurred = datetime.fromisoformat(str(row["occurred_at"]).replace("Z", "+00:00"))
            day = occurred.astimezone(local_now.tzinfo).date().isoformat()
            event = {"event_id": str(row["event_id"]), "kind": str(row["kind"]), "occurred_at": occurred.isoformat(), "local_day": day, "value": value}
            events.append(event)
            _accumulate(days[day], event)
        day_list = [days[key] for key in sorted(days)]
        summary = {
            "timezone_offset_minutes": offset,
            "window": {"start": start_day.isoformat(), "end": end_day.isoformat()},
            "days": day_list,
            "event_count": len(events),
        }
        fingerprint = hashlib.sha256(_json({"summary": summary, "source_event_ids": [item["event_id"] for item in events]}).encode()).hexdigest()
        return {
            "summary": summary,
            "events": events,
            "source_event_ids": [item["event_id"] for item in events],
            "fingerprint": fingerprint,
            "egress": {
                "requires_explicit_confirmation": True,
                "fields": ["focus_minutes", "food_id", "food_name", "mood_bucket", "reminder_completed_count", "random_event_result"],
                "excluded": ["chat_content", "window_title", "process_name", "command_line", "clipboard", "local_path"],
            },
        }

    def generate(
        self,
        *,
        request_id: object,
        timezone_offset_minutes: object,
        preview_fingerprint: object,
        confirm_egress: object,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, object]:
        if not isinstance(request_id, str) or _ID.fullmatch(request_id) is None:
            raise CompanionRepositoryError("diary request id is invalid")
        if not isinstance(preview_fingerprint, str) or not re.fullmatch(r"[a-f0-9]{64}", preview_fingerprint):
            raise CompanionRepositoryError("diary preview fingerprint is invalid")
        if confirm_egress is not True:
            raise CompanionRepositoryError("diary generation requires explicit egress confirmation")
        preview = self.preview(timezone_offset_minutes=timezone_offset_minutes)
        if preview["fingerprint"] != preview_fingerprint:
            raise CompanionConflict("diary preview changed; review the current summary before generating")
        replay = self._generated_for_request(request_id)
        if replay is not None:
            if replay["provider_trace"].get("summary_fingerprint") != preview_fingerprint:
                raise CompanionConflict("diary request id was reused with a different preview")
            return {"diary": replay, "source": replay["provider_trace"].get("source", "local"), "reason": "replayed", "replayed": True}
        offset = int(preview["summary"]["timezone_offset_minutes"])
        local_date = _utc(self.now()).astimezone(timezone(timedelta(minutes=offset))).date().isoformat()
        prompt_text, prompt_revision = self.character_prompt_loader()
        profile = self.repository.get_master_profile()
        prompt = compose_companion_prompt(
            route_key="companion.diary",
            master_profile={} if profile is None else {
                "nickname": profile.nickname,
                "birthday": profile.birthday,
                "oc_address": profile.oc_address,
                "relationship": profile.relationship,
                "custom_notes": profile.custom_notes,
            },
            character_prompt=prompt_text,
            modifiers={"requested_voice": "first_person", "local_date": local_date, "target_hanzi": "300-600"},
            published_context=(),
            short_term_messages=(),
            user_payload={"structured_three_day_summary": preview["summary"]},
            context_epoch=1,
        )
        outcome = self.model_router.execute(route_key="companion.diary", prompt=prompt, request_id=request_id, cancelled=cancelled)
        if outcome.get("reason") == "cancelled":
            raise CompanionConflict("diary generation was cancelled")
        source = "provider" if outcome.get("source") == "provider" else "local"
        content = str(outcome.get("text") or "").strip() if source == "provider" else _local_diary(local_date, preview["summary"])
        if not content or len(content) > 6_000 or _CONTROL.search(content):
            source = "local"
            content = _local_diary(local_date, preview["summary"])
        trace = dict(outcome.get("trace") or {})
        safe_trace = {
            "source": source,
            "route_key": "companion.diary",
            "prompt_revision": prompt_revision,
            "model_version": self.model_version if source == "provider" else "local-template-v1",
            "summary_fingerprint": preview_fingerprint,
            "attempt_count": int(trace.get("attempt_count", 0)),
            "usage": trace.get("usage", {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}),
            "edited": False,
            "generation_request_id": request_id,
        }
        diary = self._insert_revision(local_date=local_date, content=content, source_event_ids=preview["source_event_ids"], trace=safe_trace)
        return {"diary": diary, "source": source, "reason": outcome.get("reason"), "replayed": False}

    def list(self, *, local_date: object | None = None) -> dict[str, object]:
        if local_date is not None and (not isinstance(local_date, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", local_date)):
            raise CompanionRepositoryError("diary local date is invalid")
        self.repository.initialize()
        connection = self.repository._open_connection()
        try:
            rows = connection.execute(
                "SELECT * FROM companion_diaries " + ("WHERE local_date=? " if local_date else "") + "ORDER BY local_date DESC,revision DESC",
                (local_date,) if local_date else (),
            ).fetchall()
            existing = {str(row[0]) for row in connection.execute("SELECT event_id FROM companion_interaction_events").fetchall()}
        finally:
            connection.close()
        return {"items": [_diary(row, existing) for row in rows]}

    def edit(self, *, diary_id: object, content: object, expected_revision: object) -> dict[str, object]:
        if not isinstance(diary_id, str) or _ID.fullmatch(diary_id) is None or not isinstance(expected_revision, int) or isinstance(expected_revision, bool):
            raise CompanionRepositoryError("diary edit request is invalid")
        if not isinstance(content, str) or not content.strip() or len(content.strip()) > 6_000 or _CONTROL.search(content):
            raise CompanionRepositoryError("diary content is invalid")
        self.repository.initialize()
        with self.repository._transaction() as connection:
            row = connection.execute("SELECT * FROM companion_diaries WHERE diary_id=?", (diary_id,)).fetchone()
            if row is None:
                raise CompanionRepositoryError("diary was not found")
            latest = int(connection.execute("SELECT MAX(revision) FROM companion_diaries WHERE local_date=?", (row["local_date"],)).fetchone()[0])
            if int(row["revision"]) != expected_revision or latest != expected_revision:
                raise CompanionConflict("diary revision conflict")
            trace = json.loads(row["provider_trace_json"])
            trace.update({"source": "local_edit", "edited": True, "edited_from": diary_id})
            trace.pop("generation_request_id", None)
            created = self._insert_revision_in(connection, local_date=str(row["local_date"]), content=content.strip(), source_event_ids=json.loads(row["source_event_ids_json"]), trace=trace, revision=latest + 1)
            existing = {str(item[0]) for item in connection.execute("SELECT event_id FROM companion_interaction_events").fetchall()}
            return _diary(created, existing)

    def delete_event(self, event_id: object) -> dict[str, object]:
        if not isinstance(event_id, str) or _ID.fullmatch(event_id) is None:
            raise CompanionRepositoryError("diary source event id is invalid")
        self.repository.initialize()
        with self.repository._transaction() as connection:
            cursor = connection.execute("DELETE FROM companion_interaction_events WHERE event_id=?", (event_id,))
            if cursor.rowcount == 1:
                tombstone_id = "diary_deleted:" + hashlib.sha256(event_id.encode()).hexdigest()[:32]
                stamp = _utc(self.now()).isoformat()
                connection.execute(
                    "INSERT OR IGNORE INTO companion_settings(id,revision,payload_json,updated_at) VALUES(?,1,?,?)",
                    (tombstone_id, _json({"event_id": event_id}), stamp),
                )
        return {"event_id": event_id, "deleted": cursor.rowcount == 1}

    def _refresh_projection(self, *, now: datetime) -> None:
        cutoff = now - timedelta(days=5)
        with self.repository._transaction() as connection:
            connection.execute("DELETE FROM companion_interaction_events WHERE expires_at IS NOT NULL AND expires_at<=?", (now.isoformat(),))
            rows = connection.execute("SELECT session_id,target_seconds,updated_at FROM companion_focus_sessions WHERE status='completed' AND updated_at>=?", (cutoff.isoformat(),)).fetchall()
            for row in rows:
                self._project(connection, "focus", str(row["session_id"]), {"minutes": max(1, round(float(row["target_seconds"]) / 60))}, str(row["updated_at"]))
            rows = connection.execute("SELECT receipt_id,item_id,created_at FROM companion_commerce_receipts WHERE operation='feed' AND created_at>=?", (cutoff.isoformat(),)).fetchall()
            for row in rows:
                item_id = str(row["item_id"])
                self._project(connection, "feeding", str(row["receipt_id"]), {"food_id": item_id, "food_name": self.food_names.get(item_id, item_id)}, str(row["created_at"]))
            rows = connection.execute("SELECT occurrence_id,updated_at FROM companion_reminder_occurrences WHERE state='completed' AND updated_at>=?", (cutoff.isoformat(),)).fetchall()
            for row in rows:
                self._project(connection, "reminder_completed", str(row["occurrence_id"]), {"count": 1}, str(row["updated_at"]))
            rows = connection.execute("SELECT event_id,prompt_ref,selected_option,result_json,updated_at FROM companion_random_events WHERE state='settled' AND updated_at>=?", (cutoff.isoformat(),)).fetchall()
            for row in rows:
                result = json.loads(row["result_json"] or "{}")
                changes = result.get("changes") if isinstance(result, dict) else {}
                score = sum(int(changes.get(key, 0)) for key in ("affinity", "mood", "coins")) if isinstance(changes, dict) else 0
                self._project(connection, "random_event", str(row["event_id"]), {"template_id": str(row["prompt_ref"]), "option_id": str(row["selected_option"]), "result": "positive" if score > 0 else "negative" if score < 0 else "neutral"}, str(row["updated_at"]))
            rows = connection.execute("SELECT action_id,mood_label_after,created_at FROM companion_state_actions WHERE created_at>=? AND command IN ('focus_complete','feed','random_event','chat_affect')", (cutoff.isoformat(),)).fetchall()
            for row in rows:
                self._project(connection, "mood", str(row["action_id"]), {"bucket": str(row["mood_label_after"])}, str(row["created_at"]))

    def _project(self, connection, kind: str, source_id: str, value: Mapping[str, object], occurred_at: str) -> None:
        event_id = f"diary:{kind}:{hashlib.sha256(source_id.encode()).hexdigest()[:24]}"
        tombstone_id = "diary_deleted:" + hashlib.sha256(event_id.encode()).hexdigest()[:32]
        if connection.execute("SELECT 1 FROM companion_settings WHERE id=?", (tombstone_id,)).fetchone() is not None:
            return
        expires_at = (datetime.fromisoformat(occurred_at.replace("Z", "+00:00")) + timedelta(days=5)).isoformat()
        connection.execute("INSERT OR IGNORE INTO companion_interaction_events(event_id,kind,value_json,occurred_at,expires_at) VALUES(?,?,?,?,?)", (event_id, kind, _json(value), occurred_at, expires_at))

    def _insert_revision(self, *, local_date: str, content: str, source_event_ids: list[str], trace: Mapping[str, object]) -> dict[str, object]:
        with self.repository._transaction() as connection:
            revision = int(connection.execute("SELECT COALESCE(MAX(revision),0)+1 FROM companion_diaries WHERE local_date=?", (local_date,)).fetchone()[0])
            row = self._insert_revision_in(connection, local_date=local_date, content=content, source_event_ids=source_event_ids, trace=trace, revision=revision)
            existing = {str(item[0]) for item in connection.execute("SELECT event_id FROM companion_interaction_events").fetchall()}
            return _diary(row, existing)

    def _generated_for_request(self, request_id: str) -> dict[str, object] | None:
        connection = self.repository._open_connection()
        try:
            row = connection.execute(
                "SELECT * FROM companion_diaries WHERE json_extract(provider_trace_json,'$.generation_request_id')=? ORDER BY created_at,diary_id LIMIT 1",
                (request_id,),
            ).fetchone()
            if row is None:
                return None
            existing = {str(item[0]) for item in connection.execute("SELECT event_id FROM companion_interaction_events").fetchall()}
            return _diary(row, existing)
        finally:
            connection.close()

    def _insert_revision_in(self, connection, *, local_date: str, content: str, source_event_ids: list[str], trace: Mapping[str, object], revision: int):
        stamp = _utc(self.now()).isoformat()
        diary_id = f"diary:{local_date.replace('-', '')}:{revision}"
        connection.execute("INSERT INTO companion_diaries(diary_id,local_date,content,source_event_ids_json,provider_trace_json,revision,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)", (diary_id, local_date, content, _json(source_event_ids), _json(trace), revision, stamp, stamp))
        return connection.execute("SELECT * FROM companion_diaries WHERE diary_id=?", (diary_id,)).fetchone()


def _safe_value(kind: str, raw: object) -> dict[str, object]:
    if kind not in _KINDS:
        raise CompanionRepositoryError("stored diary event kind is invalid")
    try:
        value = json.loads(str(raw))
    except json.JSONDecodeError as exc:
        raise CompanionRepositoryError("stored diary event value is invalid") from exc
    if not isinstance(value, dict):
        raise CompanionRepositoryError("stored diary event value is invalid")
    allowed = {"focus": {"minutes"}, "feeding": {"food_id", "food_name"}, "mood": {"bucket"}, "reminder_completed": {"count"}, "random_event": {"template_id", "option_id", "result"}}[kind]
    if set(value) != allowed:
        raise CompanionRepositoryError("stored diary event fields are invalid")
    return value


def _empty_day(day: str) -> dict[str, object]:
    return {"local_date": day, "focus_minutes": 0, "foods": [], "mood_buckets": [], "reminders_completed": 0, "random_events": []}


def _accumulate(day: dict[str, object], event: Mapping[str, object]) -> None:
    kind, value = event["kind"], event["value"]
    if kind == "focus": day["focus_minutes"] += int(value["minutes"])
    elif kind == "feeding": day["foods"].append({"id": value["food_id"], "name": value["food_name"]})
    elif kind == "mood": day["mood_buckets"].append(value["bucket"])
    elif kind == "reminder_completed": day["reminders_completed"] += int(value["count"])
    elif kind == "random_event": day["random_events"].append(value)


def _local_diary(local_date: str, summary: Mapping[str, object]) -> str:
    days = summary.get("days", [])
    total_focus = sum(int(day.get("focus_minutes", 0)) for day in days)
    foods = [food.get("name") for day in days for food in day.get("foods", [])]
    reminders = sum(int(day.get("reminders_completed", 0)) for day in days)
    events = sum(len(day.get("random_events", [])) for day in days)
    parts = [f"{local_date}，这是我这三天的观察日记。"]
    parts.append(f"御主一共专注了 {total_focus} 分钟，完成了 {reminders} 项日程。")
    parts.append(f"我还记得吃过{'、'.join(foods) if foods else '暂时没有记录的食物'}，一起经历了 {events} 个小剧场。")
    parts.append("这些记录只来自本地的结构化事件，没有读取聊天正文、窗口标题或剪贴板。今天也要按自己的节奏好好生活，我会继续在桌面边上陪着你。")
    return "".join(parts)


def _diary(row, existing: set[str]) -> dict[str, object]:
    source_ids = json.loads(row["source_event_ids_json"])
    trace = json.loads(row["provider_trace_json"])
    missing = [item for item in source_ids if item not in existing]
    return {"diary_id": str(row["diary_id"]), "local_date": str(row["local_date"]), "content": str(row["content"]), "source_event_ids": source_ids, "missing_source_event_ids": missing, "source_status": "missing" if missing else "complete", "provider_trace": trace, "edited": trace.get("edited") is True, "revision": int(row["revision"]), "created_at": str(row["created_at"]), "updated_at": str(row["updated_at"])}


def _offset(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not -840 <= value <= 840:
        raise CompanionRepositoryError("diary timezone offset is invalid")
    return value


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise CompanionRepositoryError("diary clock must be timezone aware")
    return value.astimezone(timezone.utc)


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
