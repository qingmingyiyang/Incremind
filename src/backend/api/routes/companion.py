from __future__ import annotations

import os
import asyncio
import hashlib
import hmac
import re
from datetime import datetime
from pathlib import Path
from threading import Event
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.background import BackgroundTask

from backend.api.ai_runtime import get_or_build_ai_runtime
from backend.api.task_completion_learning import process_completed_companion_episode
from backend.api.companion_chat_ai_runtime import (
    COMPANION_CHAT_CONTEXT_CAPABILITY,
    COMPANION_CHAT_MESSAGE_WRITE_CAPABILITY,
    COMPANION_CHAT_OUTCOME,
)
from backend.api.companion_vision_ai_runtime import (
    COMPANION_VISION_ANALYZE_CAPABILITY,
    COMPANION_VISION_CONTEXT_CAPABILITY,
    COMPANION_VISION_OUTCOME,
)
from backend.api.container import ApiContainerDep
from core.companion_core import (
    CompanionConflict,
    CompanionHardForgetError,
    CompanionHistoryService,
    CompanionMemoryBridgeError,
    CompanionManualService,
    CompanionNotesService,
    CompanionRepository,
    CompanionRepositoryError,
    CompanionRoutineService,
    CompanionReminderService,
    CompanionStateReducer,
    CompanionCommerceService,
    CompanionAppearanceService,
    CompanionBackupService,
    build_companion_clock,
    load_catalog,
    load_appearance_catalog,
    CompanionFocusService,
    CompanionAmbientService,
    CompanionSystemSensorService,
    CompanionVisionGrantStore,
    CompanionVoiceGrantStore,
    MAX_VISION_BYTES,
    MAX_VOICE_AUDIO_BYTES,
    sample_foreground_process,
)
from backend.api.desktop_session import desktop_session
from backend.companion_runtime import build_companion_memory_bridge
from backend.companion_runtime import build_companion_ambient_service, build_companion_diary_service, build_companion_media_service, build_companion_voice_transcription_service
from backend.companion_scheduler_runtime import CompanionSchedulerRuntime, serialize_companion_event
from backend.companion_weather_runtime import CompanionWeatherRuntime


router = APIRouter(prefix="/api/rebuild/companion", tags=["rebuild-companion"])
_NO_STORE = {"Content-Type": "application/json", "Cache-Control": "no-store"}
_CHAT_ID = re.compile(r"^[a-z0-9][a-z0-9:_-]{0,127}$")
_MEMORY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:_-]{0,191}$")
_PROJECT_ID_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_CHAT_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MEMORY_TOPIC_PREFIX = "请根据我已经确认发布的长期记忆，围绕《"
_MEMORY_TOPIC_SUFFIX = "》说明这条记忆的意义、与当前工作的联系，并给出两个可继续追问的问题。请区分有证据的事实与推断。"
_MEMORY_TOPIC_PROMPT = re.compile(rf"^{re.escape(_MEMORY_TOPIC_PREFIX)}(.{{1,120}}){re.escape(_MEMORY_TOPIC_SUFFIX)}$")


@router.get("/settings")
async def get_companion_settings(container: ApiContainerDep) -> JSONResponse:
    try:
        snapshot = _routine_service(container).get()
    except CompanionRepositoryError as exc:
        return _error(503, "settings_unavailable", str(exc))
    return JSONResponse(
        content={"revision": snapshot.revision, "settings": snapshot.as_dict(), "updated_at": snapshot.updated_at},
        headers=_NO_STORE,
    )


@router.get("/state")
async def get_companion_state(container: ApiContainerDep) -> JSONResponse:
    try:
        service = _state_service(container)
        snapshot = service.snapshot()
        ledger = service.wallet(limit=20)
    except CompanionRepositoryError as exc:
        return _error(503, "companion_state_unavailable", str(exc))
    return JSONResponse(content={
        "state": _serialize_state(snapshot),
        "wallet": [{
            "transaction_id": item.transaction_id, "reason": item.reason, "delta": item.delta,
            "balance_after": item.balance_after, "created_at": item.created_at,
        } for item in ledger],
        "daily_check_in_claimed": service.daily_check_in_claimed(),
        "rules": {"version": service.rules.version, "affinity_thresholds": list(service.rules.affinity_thresholds)},
    }, headers=_NO_STORE)


@router.post("/state/daily-check-in")
async def check_in_companion_state(request: Request, container: ApiContainerDep) -> JSONResponse:
    try:
        payload: Any = await request.json()
    except Exception:
        return _error(400, "invalid_json", "invalid JSON body")
    if payload != {}:
        return _error(400, "invalid_state_action", "daily check-in body must be empty")
    try:
        result = _state_service(container).daily_check_in()
    except CompanionConflict as exc:
        return _error(409, "state_action_conflict", str(exc))
    except CompanionRepositoryError as exc:
        return _error(503, "companion_state_unavailable", str(exc))
    return JSONResponse(content={"result": _serialize_state_action(result)}, headers=_NO_STORE)


@router.post("/data/backup")
async def create_companion_backup(request: Request, container: ApiContainerDep) -> JSONResponse:
    payload = await _strict_json_object(request, {"path"})
    if payload is None or not _main_path_authorized(request, "backup", payload):
        return _error(403, "backup_authorization_required", "desktop main authorization required")
    target = _safe_native_path(payload.get("path"))
    if target is None:
        return _error(400, "backup_path_invalid", "backup path is invalid")
    try:
        receipt = CompanionBackupService(
            CompanionRepository.at_data_root(getattr(container, "root_dir"))
        ).create_backup(target)
    except CompanionRepositoryError as exc:
        return _error(409, "backup_rejected", str(exc))
    return JSONResponse(
        status_code=201,
        content={
            "status": "created",
            "fingerprint": receipt.fingerprint,
            "size_bytes": receipt.size_bytes,
            "database_schema_version": receipt.database_schema_version,
            "created_at": receipt.created_at,
        },
        headers=_NO_STORE,
    )


@router.post("/data/restore/preflight")
async def preflight_companion_restore(request: Request, container: ApiContainerDep) -> JSONResponse:
    payload = await _strict_json_object(request, {"path"})
    if payload is None or not _main_path_authorized(request, "restore-preflight", payload):
        return _error(403, "restore_authorization_required", "desktop main authorization required")
    source = _safe_native_path(payload.get("path"))
    if source is None:
        return _error(400, "restore_path_invalid", "restore path is invalid")
    try:
        result = CompanionBackupService(
            CompanionRepository.at_data_root(getattr(container, "root_dir"))
        ).preflight_restore(source)
    except CompanionRepositoryError as exc:
        return _error(409, "restore_preflight_rejected", str(exc))
    return JSONResponse(
        content={
            "status": "ready",
            "fingerprint": result.fingerprint,
            "size_bytes": result.size_bytes,
            "source_schema_version": result.source_schema_version,
            "target_schema_version": result.target_schema_version,
            "requires_migration": result.requires_migration,
        },
        headers=_NO_STORE,
    )


@router.post("/data/restore")
async def restore_companion_backup(request: Request, container: ApiContainerDep) -> JSONResponse:
    payload = await _strict_json_object(request, {"expected_fingerprint", "path"})
    if payload is None or not _main_path_authorized(request, "restore", payload):
        return _error(403, "restore_authorization_required", "desktop main authorization required")
    source = _safe_native_path(payload.get("path"))
    fingerprint = payload.get("expected_fingerprint")
    if source is None or not isinstance(fingerprint, str) or _SHA256.fullmatch(fingerprint) is None:
        return _error(400, "restore_request_invalid", "restore request is invalid")
    try:
        receipt = CompanionBackupService(
            CompanionRepository.at_data_root(getattr(container, "root_dir"))
        ).restore_backup(
            source,
            expected_fingerprint=fingerprint,
            rollback_directory=Path(getattr(container, "root_dir"))
            / ".rebuild-data"
            / "companion"
            / "restore-points",
        )
    except CompanionRepositoryError as exc:
        return _error(409, "restore_rejected", str(exc))
    rollback = receipt.rollback_backup
    return JSONResponse(
        content={
            "status": "restored",
            "restored_fingerprint": receipt.restored_fingerprint,
            "restored_schema_version": receipt.restored_schema_version,
            "completed_at": receipt.completed_at,
            "rollback_created": rollback is not None,
            "rollback_fingerprint": rollback.fingerprint if rollback is not None else None,
        },
        headers=_NO_STORE,
    )


@router.get("/focus")
async def get_companion_focus(request: Request, container: ApiContainerDep) -> JSONResponse:
    try: item = _focus_service(request, container).current()
    except CompanionRepositoryError as exc: return _error(503, "focus_unavailable", str(exc))
    return JSONResponse(content={"session": _serialize_focus(item) if item else None}, headers=_NO_STORE)


@router.get("/ambient")
async def get_companion_ambient(container: ApiContainerDep) -> JSONResponse:
    try:
        result = _ambient_service(container).status()
    except CompanionRepositoryError as exc:
        return _error(503, "ambient_unavailable", str(exc))
    return JSONResponse(content=result, headers=_NO_STORE)


@router.get("/sensors")
async def get_companion_sensors(request: Request, container: ApiContainerDep) -> JSONResponse:
    try:
        result = _sensor_service(request, container).status()
    except CompanionRepositoryError as exc:
        return _error(503, "sensor_unavailable", str(exc))
    return JSONResponse(content=result, headers=_NO_STORE)


@router.get("/weather")
async def get_companion_weather(request: Request) -> JSONResponse:
    runtime = _weather_runtime(request)
    if runtime is None:
        return _error(503, "weather_unavailable", "weather service is unavailable")
    try:
        return JSONResponse(content=runtime.status(), headers=_NO_STORE)
    except CompanionRepositoryError:
        return _error(503, "weather_unavailable", "weather service is unavailable")


@router.put("/weather/settings")
async def configure_companion_weather(request: Request) -> JSONResponse:
    try:
        payload: Any = await request.json()
    except Exception:
        return _error(400, "invalid_json", "invalid JSON body")
    expected = {"enabled", "location_name", "latitude", "longitude", "noncommercial_acknowledged", "expected_revision"}
    if not isinstance(payload, dict) or set(payload) != expected:
        return _error(400, "invalid_weather_settings", "weather settings contain unsupported fields")
    runtime = _weather_runtime(request)
    if runtime is None:
        return _error(503, "weather_unavailable", "weather service is unavailable")
    try:
        runtime.service.configure(**payload)
        runtime.wake()
        result = runtime.status()
    except CompanionConflict:
        return _error(409, "weather_settings_conflict", "weather settings changed; reload and try again")
    except CompanionRepositoryError:
        return _error(400, "invalid_weather_settings", "weather settings are invalid")
    return JSONResponse(content=result, headers=_NO_STORE)


@router.post("/weather/refresh")
async def refresh_companion_weather(request: Request) -> JSONResponse:
    try:
        payload: Any = await request.json()
    except Exception:
        return _error(400, "invalid_json", "invalid JSON body")
    if payload != {}:
        return _error(400, "invalid_weather_refresh", "weather refresh body must be empty")
    runtime = _weather_runtime(request)
    if runtime is None:
        return _error(503, "weather_unavailable", "weather service is unavailable")
    runtime.wake(manual=True)
    return JSONResponse(status_code=202, content={"status": "scheduled", **runtime.status()}, headers=_NO_STORE)


@router.get("/media-session")
async def get_companion_media_session(container: ApiContainerDep) -> JSONResponse:
    try:
        result = build_companion_media_service(container).status()
    except CompanionRepositoryError:
        return _error(503, "media_session_unavailable", "media session service is unavailable")
    return JSONResponse(content=result, headers=_NO_STORE)


@router.put("/media-session/settings")
async def configure_companion_media_session(request: Request, container: ApiContainerDep) -> JSONResponse:
    try:
        payload: Any = await request.json()
    except Exception:
        return _error(400, "invalid_json", "invalid JSON body")
    if not isinstance(payload, dict) or set(payload) != {"enabled", "model_commentary_enabled", "expected_revision"}:
        return _error(400, "invalid_media_session_settings", "media session settings contain unsupported fields")
    try:
        config = build_companion_media_service(container).configure(**payload)
    except CompanionConflict:
        return _error(409, "media_session_settings_conflict", "media session settings changed; reload and try again")
    except CompanionRepositoryError:
        return _error(400, "invalid_media_session_settings", "media session settings are invalid")
    return JSONResponse(content={"config": config.as_dict(), "revision": config.revision, "updated_at": config.updated_at}, headers=_NO_STORE)


@router.post("/media-session/observe")
async def observe_companion_media_session(request: Request, container: ApiContainerDep) -> JSONResponse:
    try:
        payload: Any = await request.json()
    except Exception:
        return _error(400, "invalid_json", "invalid JSON body")
    if not isinstance(payload, dict) or set(payload) != {"observation_id", "title", "artist", "playback_status", "quiet"}:
        return _error(400, "invalid_media_session_observation", "media session observation contains unsupported fields")
    try:
        result = await asyncio.to_thread(build_companion_media_service(container).observe, **payload)
    except CompanionConflict:
        return _error(409, "media_session_observation_conflict", "media session state changed; retry the next sample")
    except CompanionRepositoryError:
        return _error(400, "invalid_media_session_observation", "media session observation is invalid")
    return JSONResponse(content={"result": result}, headers=_NO_STORE)


@router.put("/sensors/settings")
async def configure_companion_sensors(request: Request, container: ApiContainerDep) -> JSONResponse:
    try:
        payload: Any = await request.json()
    except Exception:
        return _error(400, "invalid_json", "invalid JSON body")
    expected = {"enabled", "network_enabled", "health_origin", "game_enabled", "game_processes", "game_behavior", "expected_revision"}
    if not isinstance(payload, dict) or set(payload) != expected:
        return _error(400, "invalid_sensor_settings", "sensor settings contain unsupported fields")
    try:
        config = _sensor_service(request, container).configure(**payload)
    except CompanionConflict as exc:
        return _error(409, "sensor_settings_conflict", str(exc))
    except CompanionRepositoryError as exc:
        return _error(400, "invalid_sensor_settings", str(exc))
    return JSONResponse(content={"config": config.as_dict(), "revision": config.revision, "updated_at": config.updated_at}, headers=_NO_STORE)


@router.post("/sensors/sample")
async def sample_companion_sensors(request: Request, container: ApiContainerDep) -> JSONResponse:
    try:
        payload: Any = await request.json()
    except Exception:
        return _error(400, "invalid_json", "invalid JSON body")
    if not isinstance(payload, dict) or set(payload) != {"network_state", "latency_ms"}:
        return _error(400, "invalid_sensor_sample", "sensor sample contains unsupported fields")
    try:
        result = await asyncio.to_thread(_sensor_service(request, container).sample, network_state=payload["network_state"], latency_ms=payload["latency_ms"])
    except CompanionRepositoryError as exc:
        return _error(400, "invalid_sensor_sample", str(exc))
    return JSONResponse(content=result, headers=_NO_STORE)


@router.put("/ambient/settings")
async def configure_companion_ambient(request: Request, container: ApiContainerDep) -> JSONResponse:
    try: payload: Any = await request.json()
    except Exception: return _error(400, "invalid_json", "invalid JSON body")
    if not isinstance(payload, dict) or set(payload) != {"enabled", "interval_minutes", "idle_enabled", "idle_minutes", "expected_revision"}:
        return _error(400, "invalid_ambient_settings", "ambient settings contain unsupported fields")
    try: result = _ambient_service(container).configure(**payload)
    except CompanionConflict as exc: return _error(409, "ambient_settings_conflict", str(exc))
    except CompanionRepositoryError as exc: return _error(400, "invalid_ambient_settings", str(exc))
    return JSONResponse(content=result, headers=_NO_STORE)


@router.post("/ambient/idle")
async def companion_idle_message(request: Request, container: ApiContainerDep) -> JSONResponse:
    try: payload: Any = await request.json()
    except Exception: return _error(400, "invalid_json", "invalid JSON body")
    if not isinstance(payload, dict) or set(payload) != {"idle_seconds", "quiet", "game", "sleeping"}:
        return _error(400, "invalid_ambient_idle", "ambient idle body contains unsupported fields")
    try: result = _ambient_service(container).idle_message(**payload)
    except CompanionRepositoryError as exc: return _error(400, "invalid_ambient_idle", str(exc))
    return JSONResponse(content=result, headers=_NO_STORE)


@router.post("/ambient/offer")
async def offer_companion_ambient(request: Request, container: ApiContainerDep) -> JSONResponse:
    try: payload: Any = await request.json()
    except Exception: return _error(400, "invalid_json", "invalid JSON body")
    expected = {"require_due", "quiet", "game", "sleeping"}
    if not isinstance(payload, dict) or set(payload) != expected:
        return _error(400, "invalid_ambient_trigger", "ambient trigger contains unsupported fields")
    try: result = _ambient_service(container).offer(**payload)
    except CompanionRepositoryError as exc: return _error(400, "invalid_ambient_trigger", str(exc))
    return JSONResponse(status_code=201 if result.get("event") and not result.get("replayed") else 200, content=result, headers=_NO_STORE)


@router.post("/ambient/events/{event_id}/choose")
async def choose_companion_ambient(event_id: str, request: Request, container: ApiContainerDep) -> JSONResponse:
    try: payload: Any = await request.json()
    except Exception: return _error(400, "invalid_json", "invalid JSON body")
    if not isinstance(payload, dict) or set(payload) != {"option_id", "expected_revision"}:
        return _error(400, "invalid_random_event_choice", "random event choice contains unsupported fields")
    try: result = _ambient_service(container).choose(event_id, payload["option_id"], payload["expected_revision"])
    except CompanionConflict as exc: return _error(409, "random_event_conflict", str(exc))
    except CompanionRepositoryError as exc: return _error(400, "invalid_random_event_choice", str(exc))
    return JSONResponse(content=result, headers=_NO_STORE)


@router.get("/diary/preview")
async def preview_companion_diary(timezone_offset_minutes: int, container: ApiContainerDep) -> JSONResponse:
    try:
        result = _diary_service(container).preview(timezone_offset_minutes=timezone_offset_minutes)
    except CompanionRepositoryError as exc:
        return _error(400, "invalid_diary_preview", str(exc))
    return JSONResponse(content=result, headers=_NO_STORE)


@router.get("/diary")
async def list_companion_diaries(container: ApiContainerDep, local_date: str | None = None) -> JSONResponse:
    try:
        result = _diary_service(container).list(local_date=local_date)
    except CompanionRepositoryError as exc:
        return _error(400, "invalid_diary_query", str(exc))
    return JSONResponse(content=result, headers=_NO_STORE)


@router.post("/diary/generate")
async def generate_companion_diary(request: Request, container: ApiContainerDep) -> JSONResponse:
    try:
        payload: Any = await request.json()
    except Exception:
        return _error(400, "invalid_json", "invalid JSON body")
    expected = {"request_id", "timezone_offset_minutes", "preview_fingerprint", "confirm_egress"}
    if not isinstance(payload, dict) or set(payload) != expected:
        return _error(400, "invalid_diary_generation", "diary generation body contains unsupported fields")
    cancelled = Event()
    monitor = asyncio.create_task(_watch_disconnect(request, cancelled))
    try:
        result = await asyncio.to_thread(
            _diary_service(container).generate,
            request_id=payload["request_id"],
            timezone_offset_minutes=payload["timezone_offset_minutes"],
            preview_fingerprint=payload["preview_fingerprint"],
            confirm_egress=payload["confirm_egress"],
            cancelled=cancelled.is_set,
        )
    except CompanionConflict as exc:
        return _error(409, "diary_generation_conflict", str(exc))
    except CompanionRepositoryError as exc:
        return _error(400, "invalid_diary_generation", str(exc))
    finally:
        monitor.cancel()
    return JSONResponse(status_code=201, content=result, headers=_NO_STORE)


@router.post("/diary/{diary_id}/edit")
async def edit_companion_diary(diary_id: str, request: Request, container: ApiContainerDep) -> JSONResponse:
    try:
        payload: Any = await request.json()
    except Exception:
        return _error(400, "invalid_json", "invalid JSON body")
    if not isinstance(payload, dict) or set(payload) != {"content", "expected_revision"}:
        return _error(400, "invalid_diary_edit", "diary edit body contains unsupported fields")
    try:
        result = _diary_service(container).edit(diary_id=diary_id, content=payload["content"], expected_revision=payload["expected_revision"])
    except CompanionConflict as exc:
        return _error(409, "diary_edit_conflict", str(exc))
    except CompanionRepositoryError as exc:
        return _error(400, "invalid_diary_edit", str(exc))
    return JSONResponse(status_code=201, content={"diary": result}, headers=_NO_STORE)


@router.delete("/diary/events/{event_id}")
async def delete_companion_diary_event(event_id: str, container: ApiContainerDep) -> JSONResponse:
    try:
        result = _diary_service(container).delete_event(event_id)
    except CompanionRepositoryError as exc:
        return _error(400, "invalid_diary_event", str(exc))
    if not result["deleted"]:
        return _error(404, "diary_event_not_found", "diary source event was not found")
    return JSONResponse(content=result, headers=_NO_STORE)


@router.post("/focus/start")
async def start_companion_focus(request: Request, container: ApiContainerDep) -> JSONResponse:
    try: payload: Any = await request.json()
    except Exception: return _error(400, "invalid_json", "invalid JSON body")
    if not isinstance(payload, dict) or set(payload) != {"duration_minutes","supervision_enabled","work_processes","distracting_processes"}: return _error(400,"invalid_focus","focus body contains unsupported fields")
    try: item=_focus_service(request,container).start(duration_minutes=payload["duration_minutes"],supervision_enabled=payload["supervision_enabled"],work_processes=payload["work_processes"],distracting_processes=payload["distracting_processes"])
    except CompanionConflict as exc: return _error(409,"focus_conflict",str(exc))
    except CompanionRepositoryError as exc: return _error(400,"invalid_focus",str(exc))
    return JSONResponse(status_code=201,content={"session":_serialize_focus(item)},headers=_NO_STORE)


@router.post("/focus/{session_id}/action")
async def act_on_companion_focus(session_id: str, request: Request, container: ApiContainerDep) -> JSONResponse:
    try: payload: Any=await request.json()
    except Exception: return _error(400,"invalid_json","invalid JSON body")
    if not isinstance(payload,dict) or set(payload)!={"action","expected_revision"}: return _error(400,"invalid_focus_action","focus action body contains unsupported fields")
    try: item=_focus_service(request,container).act(session_id,payload["action"],payload["expected_revision"])
    except CompanionConflict as exc: return _error(409,"focus_conflict",str(exc))
    except CompanionRepositoryError as exc: return _error(400,"invalid_focus_action",str(exc))
    return JSONResponse(content={"session":_serialize_focus(item)},headers=_NO_STORE)


@router.post("/focus/observe")
async def observe_companion_focus(request: Request, container: ApiContainerDep) -> JSONResponse:
    try: payload: Any=await request.json()
    except Exception: return _error(400,"invalid_json","invalid JSON body")
    if not isinstance(payload,dict) or set(payload)!={"locked","sleeping","game_quiet"}: return _error(400,"invalid_focus_observation","focus observation body contains unsupported fields")
    try: item=_focus_service(request,container).observe(process_name=sample_foreground_process(),locked=payload["locked"],sleeping=payload["sleeping"],game_quiet=payload["game_quiet"])
    except CompanionRepositoryError as exc: return _error(404 if "not found" in str(exc) else 400,"focus_missing" if "not found" in str(exc) else "invalid_focus_observation",str(exc))
    return JSONResponse(content={"session":_serialize_focus(item)},headers=_NO_STORE)


@router.put("/settings")
async def save_companion_settings(request: Request, container: ApiContainerDep) -> JSONResponse:
    try:
        payload: Any = await request.json()
    except Exception:
        return _error(400, "invalid_json", "invalid JSON body")
    if not isinstance(payload, dict) or set(payload) != {"expected_revision", "settings"}:
        return _error(400, "invalid_settings", "settings body contains unsupported fields")
    try:
        snapshot = _routine_service(container).save(
            expected_revision=payload["expected_revision"], settings=payload["settings"]
        )
    except CompanionConflict as exc:
        return _error(409, "settings_conflict", str(exc))
    except CompanionRepositoryError as exc:
        return _error(400, "invalid_settings", str(exc))
    return JSONResponse(
        content={"revision": snapshot.revision, "settings": snapshot.as_dict(), "updated_at": snapshot.updated_at},
        headers=_NO_STORE,
    )


@router.post("/routine/morning-claim")
async def claim_companion_morning(container: ApiContainerDep) -> JSONResponse:
    routine = _routine_service(container)
    local_day = routine.local_day()
    try:
        claimed = routine.claim_morning(local_day)
    except CompanionRepositoryError as exc:
        return _error(503, "morning_claim_unavailable", str(exc))
    return JSONResponse(content={"claimed": claimed, "local_day": local_day}, headers=_NO_STORE)


@router.get("/reminders")
async def list_companion_reminders(container: ApiContainerDep) -> JSONResponse:
    try:
        items = _reminder_service(container).list()
    except CompanionRepositoryError as exc:
        return _error(503, "reminders_unavailable", str(exc))
    return JSONResponse(content={"items": [_serialize_reminder(item) for item in items]}, headers=_NO_STORE)


@router.post("/reminders")
async def create_companion_reminder(request: Request, container: ApiContainerDep) -> JSONResponse:
    try:
        payload: Any = await request.json()
    except Exception:
        return _error(400, "invalid_json", "invalid JSON body")
    expected = {"title", "scheduled_at", "timezone", "advance_minutes", "recurrence", "repeat_count"}
    if not isinstance(payload, dict) or set(payload) != expected:
        return _error(400, "invalid_reminder", "reminder body contains unsupported fields")
    try:
        item = _reminder_service(container).create(
            title=payload["title"], scheduled_at=payload["scheduled_at"], timezone_name=payload["timezone"],
            advance_minutes=payload["advance_minutes"], recurrence=payload["recurrence"], repeat_count=payload["repeat_count"],
        )
    except CompanionRepositoryError as exc:
        return _error(400, "invalid_reminder", str(exc))
    return JSONResponse(status_code=201, content={"reminder": _serialize_reminder(item)}, headers=_NO_STORE)


@router.post("/reminders/{reminder_id}/cancel")
async def cancel_companion_reminder(reminder_id: str, request: Request, container: ApiContainerDep) -> JSONResponse:
    try:
        payload: Any = await request.json()
    except Exception:
        return _error(400, "invalid_json", "invalid JSON body")
    if not isinstance(payload, dict) or set(payload) != {"expected_revision"}:
        return _error(400, "invalid_reminder", "cancel body contains unsupported fields")
    try:
        item = _reminder_service(container).cancel(reminder_id=reminder_id, expected_revision=payload["expected_revision"])
    except CompanionConflict as exc:
        return _error(409, "reminder_conflict", str(exc))
    except CompanionRepositoryError as exc:
        return _error(404 if "not found" in str(exc) else 400, "reminder_missing" if "not found" in str(exc) else "invalid_reminder", str(exc))
    return JSONResponse(content={"reminder": _serialize_reminder(item)}, headers=_NO_STORE)


@router.get("/events/next")
async def next_companion_event(request: Request) -> JSONResponse:
    runtime = getattr(request.app.state, "companion_scheduler_runtime", None)
    if not isinstance(runtime, CompanionSchedulerRuntime):
        return _error(503, "companion_scheduler_unavailable", "companion scheduler is unavailable")
    event = runtime.next_event()
    return JSONResponse(content={"event": serialize_companion_event(event) if event is not None else None}, headers=_NO_STORE)


@router.post("/events/{event_id}/action")
async def act_on_companion_event(event_id: str, request: Request, container: ApiContainerDep) -> JSONResponse:
    if not _CHAT_ID.fullmatch(event_id):
        return _error(400, "invalid_companion_event", "companion event id is invalid")
    try:
        payload: Any = await request.json()
    except Exception:
        return _error(400, "invalid_json", "invalid JSON body")
    if not isinstance(payload, dict) or set(payload) != {"action"}:
        return _error(400, "invalid_companion_action", "action body contains unsupported fields")
    runtime = getattr(request.app.state, "companion_scheduler_runtime", None)
    if not isinstance(runtime, CompanionSchedulerRuntime):
        return _error(503, "companion_scheduler_unavailable", "companion scheduler is unavailable")
    try:
        result = runtime.act(event_id, payload["action"])
    except CompanionConflict as exc:
        return _error(409, "companion_event_conflict", str(exc))
    except CompanionRepositoryError as exc:
        return _error(400, "invalid_companion_action", str(exc))
    if payload["action"] == "complete" and result.get("status") in {"completed", "already_performed"}:
        try:
            reward = _state_service(container).apply(
                command="reminder_complete",
                idempotency_key=f"reminder:{hashlib.sha256(event_id.encode('utf-8')).hexdigest()[:32]}:complete",
                subject_id=event_id,
            )
            result = {**result, "reward": _serialize_state_action(reward)}
        except CompanionConflict as exc:
            result = {**result, "reward": {"status": "limited", "reason": str(exc)}}
        except CompanionRepositoryError:
            result = {**result, "reward": {"status": "unavailable"}}
    return JSONResponse(content=result, headers=_NO_STORE)


@router.get("/manual")
async def get_companion_manual(container: ApiContainerDep) -> JSONResponse:
    try:
        service = _manual_service(container)
        authority = service.authority()
        markdown = service.read_markdown()
    except CompanionRepositoryError as exc:
        return _error(404, "manual_unavailable", str(exc))
    return JSONResponse(
        content={"markdown": markdown, "mode": authority.mode, "can_open_editor": _native_manual_available(container)},
        headers=_NO_STORE,
    )


@router.get("/notes")
async def list_companion_notes(container: ApiContainerDep) -> JSONResponse:
    try:
        items = _notes_service(container).list()
    except CompanionRepositoryError as exc:
        return _error(503, "notes_unavailable", str(exc))
    return JSONResponse(content={"items": [_serialize_note(item) for item in items]}, headers=_NO_STORE)


@router.post("/notes")
async def create_companion_note(request: Request, container: ApiContainerDep) -> JSONResponse:
    try:
        payload: Any = await request.json()
    except Exception:
        return _error(400, "invalid_json", "invalid JSON body")
    if not isinstance(payload, dict) or set(payload) != {"content"}:
        return _error(400, "invalid_note", "note body must contain only content")
    try:
        note = _notes_service(container).append(payload["content"])
    except CompanionConflict as exc:
        return _error(409, "note_conflict", str(exc))
    except CompanionRepositoryError as exc:
        return _error(400, "invalid_note", str(exc))
    return JSONResponse(status_code=201, content={"note": _serialize_note(note)}, headers=_NO_STORE)


@router.post("/interactions")
async def record_companion_interaction(request: Request, container: ApiContainerDep) -> JSONResponse:
    try:
        payload: Any = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"detail": "invalid JSON body"}, headers=_NO_STORE)
    if not isinstance(payload, dict) or set(payload) != {"event_id", "kind"}:
        return JSONResponse(
            status_code=400,
            content={"detail": "interaction body must contain only event_id and kind"},
            headers=_NO_STORE,
        )
    repository = CompanionRepository.at_data_root(container.root_dir)
    try:
        event = repository.record_interaction(event_id=payload["event_id"], kind=payload["kind"])
    except CompanionConflict as exc:
        return JSONResponse(status_code=409, content={"detail": str(exc)}, headers=_NO_STORE)
    except CompanionRepositoryError as exc:
        return JSONResponse(status_code=400, content={"detail": str(exc)}, headers=_NO_STORE)
    state_action: dict[str, object] | None = None
    try:
        reduction = _state_service(container).apply(
            command=event.kind,
            idempotency_key=f"interaction:{hashlib.sha256(event.event_id.encode('utf-8')).hexdigest()[:32]}",
            subject_id=event.event_id,
        )
        state_action = _serialize_state_action(reduction)
    except CompanionConflict as exc:
        state_action = {"status": "limited", "reason": str(exc)}
    except CompanionRepositoryError:
        state_action = {"status": "unavailable"}
    return JSONResponse(
        status_code=200 if event.replayed else 201,
        content={
            "event_id": event.event_id,
            "kind": event.kind,
            "occurred_at": event.occurred_at,
            "replayed": event.replayed,
            "state_action": state_action,
        },
        headers=_NO_STORE,
    )


@router.post("/chat")
async def send_companion_chat(request: Request, container: ApiContainerDep) -> JSONResponse:
    try:
        payload: Any = await request.json()
    except Exception:
        return _error(400, "invalid_json", "invalid JSON body")
    allowed_fields = {"request_id", "session_id", "memory_id", "project_id", "text"}
    if (
        not isinstance(payload, dict)
        or not {"request_id", "text"}.issubset(payload)
        or not set(payload).issubset(allowed_fields)
    ):
        return _error(400, "invalid_chat", "chat body contains unsupported fields")
    if not isinstance(payload.get("request_id"), str) or not isinstance(payload.get("text"), str):
        return _error(400, "invalid_chat", "chat request id and text must be strings")
    if _CHAT_ID.fullmatch(payload["request_id"]) is None or not payload["text"].strip() or len(payload["text"].strip()) > 4_000 or _CHAT_CONTROL.search(payload["text"]):
        return _error(400, "invalid_chat", "chat request id or text is invalid")
    session_id = payload.get("session_id")
    if session_id is not None and (not isinstance(session_id, str) or _CHAT_ID.fullmatch(session_id) is None):
        return _error(400, "invalid_chat", "chat session id must be a string or null")
    memory_id = payload.get("memory_id")
    if memory_id is not None and (not isinstance(memory_id, str) or _MEMORY_ID.fullmatch(memory_id) is None):
        return _error(400, "invalid_chat", "chat memory id must be a valid opaque id")
    if memory_id is not None and _MEMORY_TOPIC_PROMPT.fullmatch(payload["text"]) is None:
        return _error(400, "invalid_chat", "chat memory anchor requires the controlled topic prompt")
    project_id = payload.get("project_id")
    if project_id is not None:
        if not isinstance(project_id, str):
            return _error(400, "invalid_chat", "chat project id must be a string")
        project_id = project_id.strip()
        if not project_id or len(project_id) > 191 or _PROJECT_ID_CONTROL.search(project_id):
            return _error(400, "invalid_chat", "chat project id is invalid")
    if session_id is not None:
        try:
            repository = CompanionRepository.at_data_root(container.root_dir)
            repository.initialize()
            existing_session = repository.get_session(session_id)
        except CompanionRepositoryError:
            return _error(503, "chat_unavailable", "companion chat is temporarily unavailable")
        if existing_session is not None:
            if project_id is not None and project_id != existing_session.project_id:
                return _error(409, "chat_conflict", "chat session belongs to a different project")
            project_id = existing_session.project_id
    effective_project_id = project_id or "default"
    try:
        replay_repository = CompanionRepository.at_data_root(container.root_dir)
        replay_repository.initialize()
        prior_user = replay_repository.get_message_by_request(
            request_id=payload["request_id"], role="user",
        )
        prior_assistant = replay_repository.get_message_by_request(
            request_id=payload["request_id"], role="assistant",
        )
    except CompanionRepositoryError:
        return _error(503, "chat_unavailable", "companion chat is temporarily unavailable")
    if prior_user is not None or prior_assistant is not None:
        if (
            prior_user is None or prior_assistant is None
            or prior_user.content != payload["text"]
            or prior_user.project_id != effective_project_id
            or prior_assistant.project_id != effective_project_id
            or (session_id is not None and session_id != prior_user.session_id)
        ):
            return _error(409, "chat_conflict", "chat request payload changed")
        # The optional legacy session field is a route locator, rather than a
        # command input.  Preserve its historical replay equivalence by not
        # adding it to the already accepted Turn payload.
        session_id = None
    runtime = get_or_build_ai_runtime(request, container)
    metadata = getattr(runtime, "composition_metadata", {})
    remote_usable = isinstance(metadata, dict) and metadata.get("companion_chat_remote_usable") is True
    turn_request = _legacy_chat_turn_request(
        project_id=effective_project_id,
        request_id=payload["request_id"],
        text=payload["text"],
        session_id=session_id,
        memory_id=memory_id,
        remote_usable=remote_usable,
    )
    cancelled = Event()
    monitor = asyncio.create_task(_watch_disconnect(request, cancelled))
    try:
        waiting = await asyncio.to_thread(runtime.submit_turn, turn_request)
        if waiting.status == "waiting_approval":
            if cancelled.is_set():
                await asyncio.to_thread(runtime.apply_action, _legacy_chat_cancel_action(waiting, turn_request))
                return _error(408, "chat_cancelled", "chat request was cancelled")
            events = tuple(runtime.events_after(waiting.turn_id))
            approval = next(event for event in reversed(events) if event.get("type") == "approval.required")
            completed = await asyncio.to_thread(
                runtime.apply_action,
                _legacy_chat_approval_action(waiting, approval, turn_request),
            )
        else:
            completed = waiting
        if completed.status != "completed":
            return _legacy_chat_turn_failure(runtime, completed.turn_id)
        presentation = runtime.presentation_for(completed.turn_id)
        if not isinstance(presentation, dict):
            return _error(409, "chat_conflict", "companion chat turn did not produce a presentation")
        repository = CompanionRepository.at_data_root(container.root_dir)
        repository.initialize()
        operation_id = str(turn_request["operation_id"])
        user_message = repository.get_message_by_request(request_id=operation_id, role="user")
        assistant_message = repository.get_message_by_request(request_id=operation_id, role="assistant")
        if user_message is None or assistant_message is None:
            return _error(503, "chat_unavailable", "companion chat messages are unavailable")
        result_session = repository.get_session(user_message.session_id)
        if result_session is None:
            return _error(503, "chat_unavailable", "companion chat session is unavailable")
    except CompanionRepositoryError:
        return _error(503, "chat_unavailable", "companion chat is temporarily unavailable")
    except (KeyError, StopIteration, TypeError, ValueError) as exc:
        return _legacy_chat_adapter_error(str(exc))
    finally:
        cancelled.set()
        monitor.cancel()
    try:
        _state_service(container).apply(
            command="chat",
            idempotency_key=f"interaction:{hashlib.sha256(('chat:' + payload['request_id']).encode('utf-8')).hexdigest()[:32]}",
            subject_id=f"chat:{hashlib.sha256(payload['request_id'].encode('utf-8')).hexdigest()[:32]}",
        )
    except (CompanionConflict, CompanionRepositoryError):
        pass
    return JSONResponse(
        status_code=200 if completed.replayed else 201,
        content={
            "session": {
                "session_id": result_session.session_id,
                "context_epoch": result_session.context_epoch,
                "project_id": result_session.project_id,
            },
            "user_message": _serialize_chat_message(user_message),
            "assistant_message": _serialize_chat_message(assistant_message),
            "source": "remote" if assistant_message.provider_mode == "remote" else "local",
            "reason": None if assistant_message.provider_mode == "remote" else "route_disabled",
            "trace": {
                "memory_recall": _legacy_chat_memory_trace(
                    container, repository, effective_project_id, payload["text"], memory_id,
                ),
                "conversation_recall": presentation.get("conversation_recall", {
                    "status": "empty", "episode_count": 0, "episodes": [],
                }),
            },
            "replayed": completed.replayed,
        },
        headers=_NO_STORE,
        background=BackgroundTask(
            process_completed_companion_episode,
            container,
            user_message=user_message,
            assistant_message=assistant_message,
        ),
    )


@router.post("/vision/grants")
async def create_companion_vision_grant(request: Request, container: ApiContainerDep) -> JSONResponse:
    from backend.api.desktop_session import desktop_session
    from backend.security import DesktopFileGrantError, verify_desktop_file_grant

    session = desktop_session()
    if session is None:
        return _error(403, "vision_desktop_required", "desktop vision grant required")
    try:
        incoming = verify_desktop_file_grant(
            {key.lower(): value for key, value in request.headers.items()},
            session_secret=session.secret, session_instance_id=session.instance_id,
        )
    except DesktopFileGrantError:
        return _error(403, "vision_grant_invalid", "vision upload grant is invalid")
    if incoming.source_kind != "image" or incoming.media_type not in {"image/jpeg", "image/png"} or not 1 <= incoming.size_bytes <= MAX_VISION_BYTES:
        return _error(413, "vision_image_invalid", "vision image type or size is invalid")
    received = bytearray()
    try:
        async for chunk in request.stream():
            if len(received) + len(chunk) > incoming.size_bytes or len(received) + len(chunk) > MAX_VISION_BYTES:
                return _error(413, "vision_image_too_large", "vision image exceeds its grant")
            received.extend(chunk)
        if len(received) != incoming.size_bytes:
            return _error(400, "vision_image_incomplete", "vision image size does not match its grant")
        grant = _vision_store(request, container).issue(media_type=incoming.media_type, data=bytes(received), expected_sha256=incoming.sha256)
    except (CompanionRepositoryError, OSError):
        return _error(400, "vision_image_invalid", "vision image failed integrity validation")
    return JSONResponse(status_code=201, content={"grant": grant.public(), "expires_in_seconds": 120}, headers=_NO_STORE)


@router.delete("/vision/grants/{grant_id}")
async def revoke_companion_vision_grant(grant_id: str, request: Request, container: ApiContainerDep) -> JSONResponse:
    return JSONResponse(content={"revoked": _vision_store(request, container).revoke(grant_id)}, headers=_NO_STORE)


@router.post("/vision/analyze")
async def analyze_companion_screen(request: Request, container: ApiContainerDep) -> JSONResponse:
    try:
        payload: Any = await request.json()
    except Exception:
        return _error(400, "invalid_json", "invalid JSON body")
    if not isinstance(payload, dict) or set(payload) - {"request_id", "grant_id", "question", "confirm_egress", "project_id"}:
        return _error(400, "invalid_vision", "vision body contains unsupported fields")
    if payload.get("confirm_egress") is not True:
        return _error(400, "invalid_vision", "vision analysis requires explicit confirmation")
    project_id = payload.get("project_id", "default")
    if not isinstance(project_id, str) or not project_id.strip() or len(project_id.strip()) > 191 or _PROJECT_ID_CONTROL.search(project_id):
        return _error(400, "invalid_vision", "vision project id is invalid")
    runtime = get_or_build_ai_runtime(request, container)
    metadata = getattr(runtime, "composition_metadata", {})
    remote_usable = isinstance(metadata, dict) and metadata.get("companion_vision_remote_usable") is True
    try:
        turn_request = _legacy_vision_turn_request(
            project_id=project_id.strip(), request_id=payload.get("request_id"),
            grant_id=payload.get("grant_id"), question=payload.get("question"), remote_usable=remote_usable,
        )
    except ValueError as exc:
        return _error(400, "invalid_vision", str(exc))
    cancelled = Event()
    monitor = asyncio.create_task(_watch_disconnect(request, cancelled))
    try:
        waiting = await asyncio.to_thread(runtime.submit_turn, turn_request)
        if waiting.status == "waiting_approval":
            if cancelled.is_set():
                await asyncio.to_thread(runtime.apply_action, _legacy_vision_cancel_action(waiting, turn_request))
                return _error(408, "vision_cancelled", "vision request was cancelled")
            events = tuple(runtime.events_after(waiting.turn_id))
            approval = next(event for event in reversed(events) if event.get("type") == "approval.required")
            completed = await asyncio.to_thread(runtime.apply_action, _legacy_vision_approval_action(waiting, approval, turn_request))
        else:
            completed = waiting
        if completed.status != "completed":
            return _legacy_vision_turn_failure(runtime, completed.turn_id)
        presentation = runtime.presentation_for(completed.turn_id)
        if not isinstance(presentation, dict):
            return _error(409, "vision_grant_unavailable", "companion vision turn did not produce a presentation")
        result = _legacy_vision_result(presentation)
    except (KeyError, StopIteration, TypeError, ValueError) as exc:
        return _legacy_vision_adapter_error(str(exc))
    finally:
        cancelled.set(); monitor.cancel()
    return JSONResponse(content={"result": result}, headers=_NO_STORE)


@router.post("/voice/grants")
async def create_companion_voice_grant(request: Request, container: ApiContainerDep) -> JSONResponse:
    from backend.api.desktop_session import desktop_session
    from backend.security import DesktopFileGrantError, verify_desktop_file_grant

    session = desktop_session()
    if session is None:
        return _error(403, "voice_desktop_required", "desktop voice grant required")
    try:
        incoming = verify_desktop_file_grant(
            {key.lower(): value for key, value in request.headers.items()},
            session_secret=session.secret, session_instance_id=session.instance_id,
        )
    except DesktopFileGrantError:
        return _error(403, "voice_grant_invalid", "voice upload grant is invalid")
    if incoming.source_kind != "audio" or incoming.media_type not in {"audio/webm", "audio/ogg", "audio/wav"} or not 1 <= incoming.size_bytes <= MAX_VOICE_AUDIO_BYTES:
        return _error(413, "voice_audio_invalid", "voice audio type or size is invalid")
    store = _voice_store(request, container)
    staging = store.create_staging()
    received = 0
    digest = hashlib.sha256()
    try:
        with staging.open("wb") as output:
            async for chunk in request.stream():
                if received + len(chunk) > incoming.size_bytes or received + len(chunk) > MAX_VOICE_AUDIO_BYTES:
                    return _error(413, "voice_audio_too_large", "voice audio exceeds its grant")
                output.write(chunk)
                digest.update(chunk)
                received += len(chunk)
            output.flush(); os.fsync(output.fileno())
        if received != incoming.size_bytes or digest.hexdigest() != incoming.sha256:
            return _error(400, "voice_audio_incomplete", "voice audio does not match its grant")
        grant = store.commit_staging(path=staging, media_type=incoming.media_type, expected_size=received, expected_sha256=incoming.sha256)
    except (CompanionRepositoryError, OSError):
        return _error(400, "voice_audio_invalid", "voice audio failed integrity validation")
    finally:
        store.discard_staging(staging)
    return JSONResponse(status_code=201, content={"grant": grant.public(), "expires_in_seconds": 120}, headers=_NO_STORE)


@router.delete("/voice/grants/{grant_id}")
async def revoke_companion_voice_grant(grant_id: str, request: Request, container: ApiContainerDep) -> JSONResponse:
    return JSONResponse(content={"revoked": _voice_store(request, container).revoke(grant_id)}, headers=_NO_STORE)


@router.post("/voice/transcribe")
async def transcribe_companion_voice(request: Request, container: ApiContainerDep) -> JSONResponse:
    try:
        payload: Any = await request.json()
    except Exception:
        return _error(400, "invalid_json", "invalid JSON body")
    if not isinstance(payload, dict) or set(payload) != {"request_id", "grant_id"}:
        return _error(400, "invalid_voice_transcription", "voice transcription body contains unsupported fields")
    cancelled = Event()
    monitor = asyncio.create_task(_watch_disconnect(request, cancelled))
    try:
        result = await asyncio.to_thread(
            build_companion_voice_transcription_service(container, _voice_store(request, container)).transcribe,
            request_id=payload.get("request_id"), grant_id=payload.get("grant_id"), cancelled=cancelled.is_set,
        )
    except CompanionConflict as exc:
        return _error(409, "voice_transcription_cancelled", str(exc))
    except CompanionRepositoryError as exc:
        return _error(503, "voice_transcription_unavailable", str(exc))
    finally:
        cancelled.set(); monitor.cancel()
    return JSONResponse(content={"result": result}, headers=_NO_STORE)


@router.get("/chat/messages")
async def list_companion_chat_messages(
    container: ApiContainerDep,
    session_id: str,
    limit: int = 50,
) -> JSONResponse:
    repository = CompanionRepository.at_data_root(container.root_dir)
    if limit < 1 or limit > 100:
        return _error(400, "invalid_chat_history", "chat history limit must be between 1 and 100")
    try:
        repository.initialize()
        session = repository.get_session(session_id)
        if session is None:
            return _error(404, "chat_session_missing", "chat session was not found")
        page = repository.list_messages(limit=limit, session_id=session_id)
    except CompanionRepositoryError as exc:
        return _error(400, "invalid_chat_history", str(exc))
    return JSONResponse(
        content={
            "session": {
                "session_id": session.session_id,
                "context_epoch": session.context_epoch,
                "project_id": session.project_id,
            },
            # Turn-backed writes deliberately share a stable request timestamp;
            # preserve the legacy conversational order when the pair ties.
            "items": [_serialize_chat_message(message) for message in sorted(
                reversed(page.items),
                key=lambda item: (item.created_at, 0 if item.role == "user" else 1),
            )],
            "next_cursor": page.next_cursor,
        },
        headers=_NO_STORE,
    )


@router.get("/history")
async def list_companion_history(
    container: ApiContainerDep,
    limit: int = 50,
    cursor: str | None = None,
    project_id: str | None = None,
) -> JSONResponse:
    if limit < 1 or limit > 100:
        return _error(400, "invalid_history", "history limit must be between 1 and 100")
    repository = CompanionRepository.at_data_root(container.root_dir)
    try:
        page = repository.list_history(limit=limit, before=cursor, project_id=project_id)
        recoveries = repository.list_forget_recoveries(project_id=project_id)
        memory_bridge = build_companion_memory_bridge(container, repository=repository)
    except CompanionRepositoryError as exc:
        return _error(400, "invalid_history", str(exc))
    return JSONResponse(
        content={
            "items": [
                {
                    "message_id": item.message.message_id,
                    "session_id": item.message.session_id,
                    "role": item.message.role,
                    "status": item.message.status,
                    "preview": item.message.content[:160],
                    "created_at": item.message.created_at,
                    "memory_linked": "published_memory" in item.dependency_kinds,
                    "dependency_count": item.dependency_count,
                    "memory_candidate": memory_bridge.candidate_status(item.message.message_id),
                }
                for item in page.items
            ],
            "next_cursor": page.next_cursor,
            "recovery_items": [
                {
                    "message_id": receipt.message_id,
                    "session_id": receipt.session_id,
                    "status": receipt.status,
                    "failed_step": receipt.failed_step,
                    "attempts": receipt.attempts,
                    "updated_at": receipt.updated_at,
                }
                for receipt in recoveries
            ],
            "scope": "local_companion_data_only",
            "project_id": project_id,
        },
        headers=_NO_STORE,
    )


@router.post("/messages/{message_id}/memory-candidate")
async def propose_companion_memory_candidate(
    message_id: str, container: ApiContainerDep,
) -> JSONResponse:
    if _CHAT_ID.fullmatch(message_id) is None:
        return _error(400, "invalid_message_id", "message id is invalid")
    try:
        repository = CompanionRepository.at_data_root(container.root_dir)
        repository.initialize()
        message = repository.get_message(message_id)
        if message is None:
            return _error(404, "message_missing", "companion message was not found")
        result = build_companion_memory_bridge(
            container, repository=repository, project_id=message.project_id,
        ).propose(message_id)
    except CompanionMemoryBridgeError as exc:
        code = "message_missing" if "not found" in str(exc) else "memory_candidate_rejected"
        return _error(404 if code == "message_missing" else 409, code, str(exc))
    except (CompanionConflict, CompanionRepositoryError) as exc:
        return _error(409, "memory_candidate_conflict", str(exc))
    return JSONResponse(
        status_code=200 if result.replayed else 201,
        content={
            "candidate": {
                "candidate_id": result.candidate_id,
                "message_id": result.message_id,
                "status": result.status,
                "target_layer": result.target_layer,
                "replayed": result.replayed,
            },
            "memory_publication_state": "candidate_pending_review",
            "review_route": "#view=rebuild-library-overview",
        },
        headers=_NO_STORE,
    )


@router.delete("/messages/{message_id}")
async def forget_companion_message(message_id: str, container: ApiContainerDep) -> JSONResponse:
    if _CHAT_ID.fullmatch(message_id) is None:
        return _error(400, "invalid_message_id", "message id is invalid")
    try:
        build_companion_memory_bridge(container).sync_published_dependencies(message_id)
        receipt = _history_service(container).forget(message_id)
    except CompanionHardForgetError as exc:
        return JSONResponse(
            status_code=503,
            content={
                "error": {"code": "forget_incomplete", "message": "local deletion is incomplete and can be retried"},
                "receipt": _serialize_forget_receipt(exc.receipt) if exc.receipt is not None else None,
            },
            headers=_NO_STORE,
        )
    except CompanionRepositoryError as exc:
        code = "message_missing" if "not found" in str(exc) else "forget_unavailable"
        return _error(404 if code == "message_missing" else 503, code, str(exc))
    return JSONResponse(
        status_code=200,
        content={"receipt": _serialize_forget_receipt(receipt), "scope": "local_companion_data_only"},
        headers=_NO_STORE,
    )


def _manual_service(container: object) -> CompanionManualService:
    mode = _runtime_value(container, "companion_mode", "CHRIPTMAS_COMPANION_MODE")
    user_data = _runtime_path(container, "companion_user_data_root", "CHRIPTMAS_COMPANION_USER_DATA_ROOT")
    repository = _runtime_path(container, "companion_repository_root", "CHRIPTMAS_COMPANION_REPOSITORY_ROOT")
    resources = _runtime_path(container, "companion_resources_root", "CHRIPTMAS_COMPANION_RESOURCES_ROOT")
    if mode not in {"development", "packaged"}:
        raise CompanionRepositoryError("companion manual mode is unavailable")
    return CompanionManualService(
        development=mode == "development",
        repository_root=repository,
        resources_path=resources,
        user_data_root=user_data,
    )


def _notes_service(container: object) -> CompanionNotesService:
    return CompanionNotesService(
        _runtime_path(container, "companion_user_data_root", "CHRIPTMAS_COMPANION_USER_DATA_ROOT")
    )


def _history_service(container: object) -> CompanionHistoryService:
    configured = getattr(container, "companion_forget_dependency_erasers", None)
    erasers: dict[str, Any] = {}
    if configured is not None:
        if not isinstance(configured, dict) or any(not isinstance(key, str) or not callable(value) for key, value in configured.items()):
            raise CompanionRepositoryError("companion forget dependency adapters are invalid")
        erasers = dict(configured)
    memory_bridge = build_companion_memory_bridge(container)
    erasers.setdefault("candidate", memory_bridge.erase_candidate)
    erasers.setdefault("published_memory", memory_bridge.withdraw_published)
    return CompanionHistoryService(
        CompanionRepository.at_data_root(getattr(container, "root_dir")),
        dependency_erasers=erasers,
    )


def _routine_service(container: object) -> CompanionRoutineService:
    clock = build_companion_clock()
    return CompanionRoutineService(CompanionRepository.at_data_root(getattr(container, "root_dir")), now=clock.now_utc)


def _reminder_service(container: object) -> CompanionReminderService:
    return CompanionReminderService(CompanionRepository.at_data_root(getattr(container, "root_dir")))


def _state_service(container: object) -> CompanionStateReducer:
    mode = _runtime_value(container, "companion_mode", "CHRIPTMAS_COMPANION_MODE")
    if mode == "packaged":
        rules_path = _runtime_path(container, "companion_resources_root", "CHRIPTMAS_COMPANION_RESOURCES_ROOT") / "companion-config" / "economy-rules.json"
    elif mode == "development":
        rules_path = _runtime_path(container, "companion_repository_root", "CHRIPTMAS_COMPANION_REPOSITORY_ROOT") / "config" / "companion" / "economy-rules.json"
    else:
        raise CompanionRepositoryError("companion state mode is unavailable")
    clock = build_companion_clock()
    return CompanionStateReducer(
        CompanionRepository.at_data_root(getattr(container, "root_dir")), rules_path=rules_path,
        now=clock.now_utc, local_day=clock.local_day,
    )


def _commerce_service(container: object) -> CompanionCommerceService:
    mode = _runtime_value(container, "companion_mode", "CHRIPTMAS_COMPANION_MODE")
    if mode == "packaged":
        config_root = _runtime_path(container, "companion_resources_root", "CHRIPTMAS_COMPANION_RESOURCES_ROOT") / "companion-config"
        image_root = _runtime_path(container, "companion_resources_root", "CHRIPTMAS_COMPANION_RESOURCES_ROOT") / "companion-assets"
    elif mode == "development":
        repository_root = _runtime_path(container, "companion_repository_root", "CHRIPTMAS_COMPANION_REPOSITORY_ROOT")
        config_root = repository_root / "config" / "companion"
        image_root = repository_root / "src" / "frontend" / "public" / "mascots"
    else:
        raise CompanionRepositoryError("companion commerce mode is unavailable")
    catalog = load_catalog(config_root / "items.json", config_root / "shop.json", image_root=image_root)
    reducer = _state_service(container)
    return CompanionCommerceService(CompanionRepository.at_data_root(getattr(container, "root_dir")), catalog=catalog, rules=reducer.rules)


def _appearance_service(container: object) -> CompanionAppearanceService:
    mode = _runtime_value(container, "companion_mode", "CHRIPTMAS_COMPANION_MODE")
    if mode == "packaged":
        config_root = _runtime_path(container, "companion_resources_root", "CHRIPTMAS_COMPANION_RESOURCES_ROOT") / "companion-config"
        image_root = _runtime_path(container, "companion_resources_root", "CHRIPTMAS_COMPANION_RESOURCES_ROOT") / "companion-assets"
    elif mode == "development":
        repository_root = _runtime_path(container, "companion_repository_root", "CHRIPTMAS_COMPANION_REPOSITORY_ROOT")
        config_root = repository_root / "config" / "companion"
        image_root = repository_root / "src" / "frontend" / "public" / "mascots"
    else:
        raise CompanionRepositoryError("companion appearance mode is unavailable")
    commerce = load_catalog(config_root / "items.json", config_root / "shop.json", image_root=image_root)
    appearance = load_appearance_catalog(config_root / "appearance.json", image_root=image_root)
    return CompanionAppearanceService(
        CompanionRepository.at_data_root(getattr(container, "root_dir")),
        catalog=appearance,
        item_ids=set(commerce.items),
    )


def _focus_service(request: Request, container: object) -> CompanionFocusService:
    service = getattr(request.app.state, "companion_focus_service", None)
    if isinstance(service, CompanionFocusService): return service
    service = CompanionFocusService(CompanionRepository.at_data_root(getattr(container,"root_dir")),reducer=_state_service(container))
    request.app.state.companion_focus_service = service
    return service


def _ambient_service(container: object) -> CompanionAmbientService:
    reducer = _state_service(container)
    return build_companion_ambient_service(container, rules=reducer.rules)


def _diary_service(container: object):
    return build_companion_diary_service(container)


def _vision_store(request: Request, container: object) -> CompanionVisionGrantStore:
    from backend.api.desktop_session import desktop_session

    current = desktop_session()
    if current is None:
        raise CompanionRepositoryError("desktop vision session is unavailable")
    service = getattr(request.app.state, "companion_vision_grant_store", None)
    if isinstance(service, CompanionVisionGrantStore) and service.session_id == current.instance_id:
        return service
    if isinstance(service, CompanionVisionGrantStore):
        service.dispose()
    service = CompanionVisionGrantStore(session_id=current.instance_id)
    request.app.state.companion_vision_grant_store = service
    return service


def _voice_store(request: Request, container: object) -> CompanionVoiceGrantStore:
    from backend.api.desktop_session import desktop_session

    current = desktop_session()
    if current is None:
        raise CompanionRepositoryError("desktop voice session is unavailable")
    service = getattr(request.app.state, "companion_voice_grant_store", None)
    if isinstance(service, CompanionVoiceGrantStore) and service.session_id == current.instance_id:
        return service
    if isinstance(service, CompanionVoiceGrantStore):
        service.dispose()
    service = CompanionVoiceGrantStore(session_id=current.instance_id)
    request.app.state.companion_voice_grant_store = service
    return service


def _sensor_service(request: Request, container: object) -> CompanionSystemSensorService:
    service = getattr(request.app.state, "companion_system_sensor_service", None)
    if isinstance(service, CompanionSystemSensorService):
        return service
    service = CompanionSystemSensorService(CompanionRepository.at_data_root(getattr(container, "root_dir")))
    request.app.state.companion_system_sensor_service = service
    return service


def _weather_runtime(request: Request) -> CompanionWeatherRuntime | None:
    runtime = getattr(request.app.state, "companion_weather_runtime", None)
    return runtime if isinstance(runtime, CompanionWeatherRuntime) else None


def _runtime_value(container: object, attribute: str, environment: str) -> str:
    value = getattr(container, attribute, None)
    if value is None:
        value = os.environ.get(environment)
    if not isinstance(value, str) or not value or "\x00" in value:
        raise CompanionRepositoryError("companion runtime path is unavailable")
    return value


def _native_manual_available(container: object) -> bool:
    configured = getattr(container, "companion_native_manual_open", None)
    if configured is not None:
        return configured is True
    return os.environ.get("CHRIPTMAS_DESKTOP_SESSION_MODE") == "desktop_production"


def _runtime_path(container: object, attribute: str, environment: str) -> Path:
    value = _runtime_value(container, attribute, environment)
    path = Path(value)
    if not path.is_absolute():
        raise CompanionRepositoryError("companion runtime path must be absolute")
    return path


def _serialize_note(note: object) -> dict[str, str]:
    return {
        "note_id": str(getattr(note, "note_id")),
        "content": str(getattr(note, "content")),
        "created_at": str(getattr(note, "created_at")),
    }


def _serialize_chat_message(message: object) -> dict[str, object]:
    return {
        "message_id": str(getattr(message, "message_id")),
        "request_id": str(getattr(message, "request_id")),
        "role": str(getattr(message, "role")),
        "status": str(getattr(message, "status")),
        "content": str(getattr(message, "content")),
        "created_at": str(getattr(message, "created_at")),
        "provider_mode": str(getattr(message, "provider_mode")),
        "context_epoch": int(getattr(message, "context_epoch")),
        "project_id": str(getattr(message, "project_id")),
        "memory_review": dict(getattr(message, "memory_review", {})),
    }


def _serialize_forget_receipt(receipt: object) -> dict[str, object]:
    return {
        "receipt_id": str(getattr(receipt, "receipt_id")),
        "message_id": str(getattr(receipt, "message_id")),
        "session_id": str(getattr(receipt, "session_id")),
        "status": str(getattr(receipt, "status")),
        "failed_step": getattr(receipt, "failed_step"),
        "affected": dict(getattr(receipt, "affected")),
        "attempts": int(getattr(receipt, "attempts")),
        "completed_at": getattr(receipt, "completed_at"),
        "replayed": bool(getattr(receipt, "replayed")),
    }


def _serialize_reminder(item: object) -> dict[str, object]:
    schedule = dict(getattr(item, "schedule"))
    return {
        "reminder_id": str(getattr(item, "reminder_id")),
        "title": str(schedule.get("title", "")),
        "scheduled_at": str(schedule.get("scheduled_at", "")),
        "timezone": str(schedule.get("timezone", "")),
        "recurrence": str(schedule.get("recurrence", "")),
        "repeat_count": int(schedule.get("repeat_count", 1)),
        "advance_minutes": int(getattr(item, "advance_minutes")),
        "next_fire_at": getattr(item, "next_fire_at"),
        "state": str(getattr(item, "ack_state")),
        "revision": int(getattr(item, "revision")),
    }


def _serialize_state(item: object) -> dict[str, object]:
    return {
        "affinity": item.affinity, "affinity_level": item.affinity_level,
        "mood_score": item.mood_score, "mood": item.mood, "coins": item.coins,
        "outfit_id": item.outfit_id, "background_id": item.background_id,
        "revision": item.revision, "updated_at": item.updated_at,
    }


def _serialize_state_action(item: object) -> dict[str, object]:
    return {
        "action_id": item.action_id, "command": item.command, "local_day": item.local_day,
        "rule_version": item.rule_version, "state": _serialize_state(item.snapshot),
        "changes": {"affinity": item.affinity_delta, "mood": item.mood_delta, "coins": item.coin_delta},
        "unlocks": list(item.unlocks), "created_at": item.created_at, "replayed": item.replayed,
    }


def _serialize_focus(item: object) -> dict[str, object]:
    return {"session_id":item.session_id,"status":item.status,"target_seconds":item.target_seconds,"elapsed_seconds":item.elapsed_seconds,"remaining_seconds":max(0,item.target_seconds-item.elapsed_seconds),"supervision_enabled":item.supervision_enabled,"work_processes":list(item.work_processes),"distracting_processes":list(item.distracting_processes),"warning_count":item.warning_count,"last_warning_at":item.last_warning_at,"reward_state":item.reward_state,"revision":item.revision,"updated_at":item.updated_at,"classification":item.classification,"should_warn":item.should_warn}


async def _watch_disconnect(request: Request, cancelled: Event) -> None:
    try:
        while not cancelled.is_set():
            if await request.is_disconnected():
                cancelled.set()
                return
            await asyncio.sleep(0.05)
    except asyncio.CancelledError:
        return


async def _strict_json_object(request: Request, fields: set[str]) -> dict[str, object] | None:
    try:
        payload: Any = await request.json()
    except Exception:
        return None
    if not isinstance(payload, dict) or set(payload) != fields:
        return None
    return payload


def _safe_native_path(value: object) -> Path | None:
    if not isinstance(value, str) or not value or "\0" in value or len(value) > 4096:
        return None
    try:
        return Path(value).expanduser().absolute()
    except (OSError, ValueError):
        return None


def _main_path_authorized(request: Request, action: str, payload: dict[str, object]) -> bool:
    session = desktop_session()
    path_value = payload.get("path")
    fingerprint = payload.get("expected_fingerprint", "")
    if session is None or not isinstance(path_value, str) or not isinstance(fingerprint, str):
        return False
    message = f"companion-data:{action}:{path_value}:{fingerprint}"
    expected = hmac.new(session.secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()
    signature = request.headers.get("x-chriptmas-main-signature", "")
    return bool(signature) and hmac.compare_digest(signature, expected)


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message[:240]}},
        headers=_NO_STORE,
    )


def _legacy_chat_turn_request(
    *,
    project_id: str,
    request_id: str,
    text: str,
    session_id: str | None,
    memory_id: str | None,
    remote_usable: bool,
) -> dict[str, object]:
    """Map one legacy chat command to its single durable AI Turn identity."""
    digest = _legacy_chat_digest(project_id, request_id)
    refs: list[dict[str, str]] = []
    if session_id is not None:
        refs.append({
            "kind": "companion_session", "object_id": session_id,
            "uri": f"crp://default/companion/sessions/{session_id}",
        })
    if memory_id is not None:
        refs.append({
            "kind": "atom", "object_id": memory_id,
            "uri": f"crp://default/memory/{memory_id}",
        })
    return {
        "schema_version": "1.0.0",
        "turn_id": f"turn-{digest}",
        "session_id": f"session-companion-{digest}",
        # The domain repository exposes request_id in the legacy DTO.  Keep the
        # original opaque identity while Turn identity remains project-scoped.
        "operation_id": request_id,
        "idempotency_key": f"companion-chat-{digest}",
        "scope": {"kind": "project", "project_id": project_id, "series_id": None},
        "input": {"kind": "text", "text": text, "refs": refs},
        "desired_outcome": COMPANION_CHAT_OUTCOME,
        "privacy": {
            "mode": "remote_allowed" if remote_usable else "local_only",
            "allow_remote": remote_usable, "pii": "possible",
            "consent_refs": ["crp://default/consent/provider-egress-policy"] if remote_usable else [],
            "retention": "local_durable",
        },
        "capability_policy": {
            "allowed": [COMPANION_CHAT_CONTEXT_CAPABILITY, COMPANION_CHAT_MESSAGE_WRITE_CAPABILITY],
            "denied": [], "require_approval": [COMPANION_CHAT_MESSAGE_WRITE_CAPABILITY],
        },
        "context_policy": {
            "include_project_skill": True, "include_memory": True,
            "include_session_history": True, "max_context_bytes": 262144,
        },
        "approval_policy": {"mode": "explicit", "auto_approve_read_only": True},
        # The compatibility request identity must replay byte-for-byte; wall
        # clock time is not part of a legacy chat command.
        "created_at": "2026-08-23T00:00:00+00:00",
    }


def _legacy_chat_approval_action(waiting: object, approval: dict[str, object], turn_request: dict[str, object]) -> dict[str, object]:
    digest = str(turn_request["turn_id"])[5:]
    return {
        "schema_version": "1.0.0", "action_id": f"action-{digest}",
        "turn_id": str(getattr(waiting, "turn_id")), "type": "approve",
        "target_event_id": approval["event_id"],
        "reason": "legacy Companion Chat submit mapped to AI Turn approval",
        "actor": "user", "expected_sequence": int(getattr(waiting, "current_sequence")),
        "idempotency_key": f"approve-companion-chat-{digest}",
        "created_at": "2026-08-23T00:00:00+00:00",
    }


def _legacy_chat_cancel_action(waiting: object, turn_request: dict[str, object]) -> dict[str, object]:
    digest = str(turn_request["turn_id"])[5:]
    return {
        "schema_version": "1.0.0", "action_id": f"action-{digest}",
        "turn_id": str(getattr(waiting, "turn_id")), "type": "cancel",
        "target_event_id": None, "reason": "legacy Companion Chat request disconnected before approval",
        "actor": "user", "expected_sequence": int(getattr(waiting, "current_sequence")),
        "idempotency_key": f"cancel-companion-chat-{digest}",
        "created_at": "2026-08-23T00:00:00+00:00",
    }


def _legacy_chat_turn_failure(runtime: object, turn_id: str) -> JSONResponse:
    events = tuple(runtime.events_after(turn_id))
    latest = events[-1] if events else {}
    data = latest.get("data") if isinstance(latest, dict) else None
    code = data.get("error_code") if isinstance(data, dict) else None
    if code == "ai.stale_baseline":
        return _error(409, "chat_conflict", "companion chat baseline changed")
    return _error(400, "invalid_chat", "companion chat turn failed")


def _legacy_chat_adapter_error(message: str) -> JSONResponse:
    normalized = message.casefold()
    if "identity conflict" in normalized or "baseline is stale" in normalized or "approval" in normalized:
        return _error(409, "chat_conflict", message)
    return _error(400, "invalid_chat", message)


def _legacy_vision_turn_request(
    *,
    project_id: str,
    request_id: object,
    grant_id: object,
    question: object,
    remote_usable: bool,
) -> dict[str, object]:
    """Map one confirmed legacy screen request to a durable Vision Turn."""
    if not isinstance(request_id, str) or not request_id.strip():
        raise ValueError("vision request id is invalid")
    if not isinstance(grant_id, str) or not grant_id.strip():
        raise ValueError("vision grant id is invalid")
    if not isinstance(question, str) or not question.strip():
        raise ValueError("vision question is invalid")
    request_id, grant_id, question = request_id.strip(), grant_id.strip(), question.strip()
    digest = _legacy_vision_digest(project_id, request_id)
    return {
        "schema_version": "1.0.0",
        "turn_id": f"turn-{digest}",
        "session_id": f"session-companion-vision-{digest}",
        "operation_id": request_id,
        "idempotency_key": f"companion-vision-{digest}",
        "scope": {"kind": "project", "project_id": project_id, "series_id": None},
        "input": {
            "kind": "text", "text": question,
            "refs": [{
                "kind": "companion_vision_grant", "object_id": grant_id,
                "uri": f"crp://default/companion/vision/grants/{grant_id}",
            }],
        },
        "desired_outcome": COMPANION_VISION_OUTCOME,
        "privacy": {
            "mode": "remote_allowed" if remote_usable else "local_only",
            "allow_remote": remote_usable, "pii": "possible",
            "consent_refs": ["crp://default/consent/provider-egress-policy"] if remote_usable else [],
            "retention": "local_durable",
        },
        "capability_policy": {
            "allowed": [COMPANION_VISION_CONTEXT_CAPABILITY, COMPANION_VISION_ANALYZE_CAPABILITY],
            "denied": [], "require_approval": [COMPANION_VISION_ANALYZE_CAPABILITY],
        },
        "context_policy": {
            "include_project_skill": False, "include_memory": False,
            "include_session_history": False, "max_context_bytes": 4096,
        },
        "approval_policy": {"mode": "explicit", "auto_approve_read_only": True},
        "created_at": "2026-08-23T00:00:00+00:00",
    }


def _legacy_vision_approval_action(waiting: object, approval: dict[str, object], turn_request: dict[str, object]) -> dict[str, object]:
    digest = str(turn_request["turn_id"])[5:]
    return {
        "schema_version": "1.0.0", "action_id": f"action-{digest}",
        "turn_id": str(getattr(waiting, "turn_id")), "type": "approve",
        "target_event_id": approval["event_id"],
        "reason": "legacy Companion Vision confirmation mapped to AI Turn approval",
        "actor": "user", "expected_sequence": int(getattr(waiting, "current_sequence")),
        "idempotency_key": f"approve-companion-vision-{digest}",
        "created_at": "2026-08-23T00:00:00+00:00",
    }


def _legacy_vision_cancel_action(waiting: object, turn_request: dict[str, object]) -> dict[str, object]:
    digest = str(turn_request["turn_id"])[5:]
    return {
        "schema_version": "1.0.0", "action_id": f"action-{digest}",
        "turn_id": str(getattr(waiting, "turn_id")), "type": "cancel", "target_event_id": None,
        "reason": "legacy Companion Vision request disconnected before approval",
        "actor": "user", "expected_sequence": int(getattr(waiting, "current_sequence")),
        "idempotency_key": f"cancel-companion-vision-{digest}",
        "created_at": "2026-08-23T00:00:00+00:00",
    }


def _legacy_vision_result(presentation: dict[str, object]) -> dict[str, object]:
    request_id = presentation.get("request_id")
    text = presentation.get("text")
    provider_called = presentation.get("provider_call_performed") is True
    if not isinstance(request_id, str) or not isinstance(text, str):
        raise ValueError("companion vision presentation is invalid")
    local = not provider_called
    return {
        "request_id": request_id,
        "status": "fallback" if local else "completed",
        "source": "local" if local else "provider",
        "reason": "fallback_not_configured" if local else None,
        "text": text,
        "trace": {
            "route_key": "companion.vision",
            "usage": {},
            "provider_id": presentation.get("provider_id") if isinstance(presentation.get("provider_id"), str) else "",
            "model_name": presentation.get("model_name") if isinstance(presentation.get("model_name"), str) else "",
            "replayed": presentation.get("replayed") is True,
        },
    }


def _legacy_vision_turn_failure(runtime: object, turn_id: str) -> JSONResponse:
    events = tuple(runtime.events_after(turn_id))
    latest = events[-1] if events else {}
    data = latest.get("data") if isinstance(latest, dict) else None
    code = data.get("error_code") if isinstance(data, dict) else None
    if code in {"ai.stale_baseline", "ai.execution_failed"}:
        return _error(409, "vision_grant_unavailable", "companion vision grant changed")
    return _error(400, "invalid_vision", "companion vision turn failed")


def _legacy_vision_adapter_error(message: str) -> JSONResponse:
    normalized = message.casefold()
    if "identity conflict" in normalized or "approval" in normalized or "grant" in normalized:
        return _error(409, "vision_grant_unavailable", message)
    return _error(400, "invalid_vision", message)


def _legacy_vision_digest(project_id: str, request_id: str) -> str:
    return hashlib.sha256(f"{project_id}\0{request_id}".encode("utf-8")).hexdigest()[:32]


def _legacy_chat_memory_trace(
    container: object,
    repository: CompanionRepository,
    project_id: str,
    text: str,
    memory_id: str | None,
) -> dict[str, object]:
    review_scope: str | None = None
    query = text
    topic = _MEMORY_TOPIC_PROMPT.fullmatch(text)
    if topic is not None:
        review_scope, query = "memory_topic_discussion", topic.group(1).strip()
    elif text == "请根据我已经确认发布的长期记忆，回顾今天值得注意的变化。请区分有证据的事实与暂无依据的推断。":
        review_scope = "today_memory_review"
    elif text == "请根据我已经确认发布的长期记忆，梳理最近七天的重要变化、仍未解决的问题和下一步。请区分有证据的事实与暂无依据的推断。":
        review_scope = "weekly_memory_review"
    try:
        bridge = build_companion_memory_bridge(container, repository=repository, project_id=project_id)
        recall = bridge.recall(query, review_scope=review_scope, target_memory_id=memory_id)
        trace: dict[str, object] = {
            "status": recall.status, "backend": recall.backend,
            "selected": [dict(item) for item in recall.selected],
            "dropped_reasons": list(recall.dropped_reasons),
        }
        if recall.review_scope is not None:
            trace["review_scope"] = recall.review_scope
        return trace
    except Exception:
        trace = {
            "status": "degraded", "backend": "unavailable", "selected": [],
            "dropped_reasons": ["index_unavailable"],
        }
        if review_scope is not None:
            trace["review_scope"] = review_scope
        return trace


def _legacy_chat_digest(project_id: str, request_id: str) -> str:
    return hashlib.sha256(f"{project_id}\0{request_id}".encode("utf-8")).hexdigest()[:32]


@router.get("/commerce")
async def get_companion_commerce(container: ApiContainerDep) -> JSONResponse:
    try:
        return JSONResponse(content=_commerce_service(container).snapshot(), headers=_NO_STORE)
    except CompanionRepositoryError as exc:
        return _error(503, "commerce_unavailable", str(exc))


@router.post("/commerce/purchase")
async def purchase_companion_item(request: Request, container: ApiContainerDep) -> JSONResponse:
    try: payload: Any = await request.json()
    except Exception: return _error(400, "invalid_json", "invalid JSON body")
    if not isinstance(payload, dict) or set(payload) != {"offer_id", "idempotency_key"}:
        return _error(400, "invalid_purchase", "purchase body contains unsupported fields")
    try: result = _commerce_service(container).purchase(payload["offer_id"], payload["idempotency_key"])
    except CompanionConflict as exc: return _error(409, "purchase_conflict", str(exc))
    except CompanionRepositoryError as exc: return _error(400, "invalid_purchase", str(exc))
    return JSONResponse(status_code=200 if result["replayed"] else 201, content={"result": result}, headers=_NO_STORE)


@router.post("/commerce/feed")
async def feed_companion_item(request: Request, container: ApiContainerDep) -> JSONResponse:
    try: payload: Any = await request.json()
    except Exception: return _error(400, "invalid_json", "invalid JSON body")
    if not isinstance(payload, dict) or set(payload) != {"item_id", "idempotency_key"}:
        return _error(400, "invalid_feed", "feed body contains unsupported fields")
    try: result = _commerce_service(container).feed(payload["item_id"], payload["idempotency_key"])
    except CompanionConflict as exc: return _error(409, "feed_conflict", str(exc))
    except CompanionRepositoryError as exc: return _error(400, "invalid_feed", str(exc))
    return JSONResponse(status_code=200 if result["replayed"] else 201, content={"result": result}, headers=_NO_STORE)


@router.get("/appearance")
async def get_companion_appearance(container: ApiContainerDep) -> JSONResponse:
    try:
        _state_service(container).reconcile_daily_mood()
        return JSONResponse(content=_appearance_service(container).snapshot(), headers=_NO_STORE)
    except CompanionRepositoryError as exc:
        return _error(503, "appearance_unavailable", str(exc))


@router.post("/appearance/equip")
async def equip_companion_appearance(request: Request, container: ApiContainerDep) -> JSONResponse:
    try: payload: Any = await request.json()
    except Exception: return _error(400, "invalid_json", "invalid JSON body")
    if not isinstance(payload, dict) or set(payload) != {"slot", "selection_id", "idempotency_key"}:
        return _error(400, "invalid_appearance", "appearance body contains unsupported fields")
    try: result = _appearance_service(container).equip(slot=payload["slot"], selection_id=payload["selection_id"], idempotency_key=payload["idempotency_key"])
    except CompanionConflict as exc: return _error(409, "appearance_conflict", str(exc))
    except CompanionRepositoryError as exc: return _error(400, "invalid_appearance", str(exc))
    return JSONResponse(status_code=200 if result["replayed"] else 201, content={"result": result}, headers=_NO_STORE)


@router.post("/appearance/stories/{chapter_id}/seen")
async def mark_companion_story_seen(chapter_id: str, request: Request, container: ApiContainerDep) -> JSONResponse:
    try: payload: Any = await request.json()
    except Exception: return _error(400, "invalid_json", "invalid JSON body")
    if payload != {}:
        return _error(400, "invalid_story", "story body contains unsupported fields")
    try: result = _appearance_service(container).mark_story_seen(chapter_id)
    except CompanionConflict as exc: return _error(409, "story_locked", str(exc))
    except CompanionRepositoryError as exc: return _error(400, "invalid_story", str(exc))
    return JSONResponse(content={"result": result}, headers=_NO_STORE)
