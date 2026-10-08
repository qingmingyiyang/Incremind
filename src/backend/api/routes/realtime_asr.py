from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from websockets.exceptions import InvalidStatus
from backend.api.container import ApiContainerDep
from backend.api.qwen_realtime_asr_secure_connector import QwenRealtimeSecureConnector
from backend.api.realtime_asr_session_service import (
    RealtimeClientDisconnected,
    RealtimeSessionService,
    RealtimeSessionTimeout,
)
from backend.api.desktop_session import (
    DESKTOP_SESSION_HEADER,
    desktop_session,
    desktop_session_authorized,
)
from backend.api.qwen_realtime_asr import build_run_task, parse_qwen_event, qwen_realtime_egress_manifest
from backend.api.realtime_asr_ticket import (
    REALTIME_ASR_TICKET_TTL_SECONDS,
    RealtimeAsrTicketAuthority,
)
from backend.api.rebuild_storage_runtime import build_rebuild_object_store
from backend.security.provider_egress import ProviderEgressError, ProviderEgressPolicyStore
from backend.security.device_identity import server_mode, server_identity, server_authorized, ServerTicketIdentity
from core.aggregate_repository_factory import AggregateRepositoryFactory
from core.product_core.realtime_asr_lexicon import (
    MAX_SESSION_TERMS,
    RealtimeAsrLexicon,
    RealtimeAsrLexiconError,
)
from core.product_core.realtime_asr_provider_settings import (
    QWEN_REALTIME_ASR_ENDPOINT,
    QWEN_REALTIME_ASR_SECRET_REF,
    GetRealtimeAsrProviderSettings,
    RealtimeAsrProviderSettingsError,
    SaveRealtimeAsrProviderSettings,
)
from core.storage_provider import JsonObjectStore


router = APIRouter(tags=["realtime-asr"])
_SETTINGS_PATH = "/api/rebuild/settings/realtime-asr-provider"
_LEXICON_PATH = "/api/rebuild/settings/realtime-asr-lexicon"
_WEBSOCKET_PATH = "/api/rebuild/workbench/realtime-asr/ws"
_WEBSOCKET_TICKET_PATH = "/api/rebuild/workbench/realtime-asr/ticket"
_TICKET_AUTHORITY = RealtimeAsrTicketAuthority()


@router.get(_SETTINGS_PATH)
async def get_realtime_asr_settings(container: ApiContainerDep) -> JSONResponse:
    return _response(200, _public_settings(container))


@router.put(_SETTINGS_PATH)
async def put_realtime_asr_settings(
    request: Request, container: ApiContainerDep
) -> JSONResponse:
    body = await _json_body(request)
    if not isinstance(body, Mapping) or not set(body).issubset(
        {"enabled", "confirm_enable", "endpoint", "region", "workspace_id"}
    ) or set(body).isdisjoint({"enabled", "confirm_enable"}):
        return _response(400, {"detail": "realtime_asr_settings_invalid"})
    store, _storage = build_rebuild_object_store(Path(container.root_dir))
    try:
        SaveRealtimeAsrProviderSettings(
            store, now=_now()
        ).execute(
            enabled=body.get("enabled") is True,
            confirm_enable=body.get("confirm_enable") is True,
            endpoint=str(body.get("endpoint") or QWEN_REALTIME_ASR_ENDPOINT),
            region=body.get("region") if isinstance(body.get("region"), str) else None,
            workspace_id=body.get("workspace_id") if isinstance(body.get("workspace_id"), str) else None,
        )
    except RealtimeAsrProviderSettingsError as error:
        return _response(409, {"detail": str(error)})
    return _response(200, _public_settings(container))


@router.post(f"{_SETTINGS_PATH}/egress-consent")
async def grant_realtime_asr_consent(
    request: Request, container: ApiContainerDep
) -> JSONResponse:
    body = await _json_body(request)
    root = Path(container.root_dir)
    settings = _settings(root)
    manifest = qwen_realtime_egress_manifest(root, endpoint=settings.endpoint)
    if not isinstance(body, Mapping) or set(body) != {"manifest_id", "confirm"}:
        return _response(400, {"detail": "realtime_asr_consent_invalid"})
    try:
        ProviderEgressPolicyStore(root).grant(
            manifest,
            manifest_id=str(body.get("manifest_id") or ""),
            confirm=body.get("confirm") is True,
        )
    except ProviderEgressError as error:
        return _response(409, {"detail": str(error)})
    return _response(200, _public_settings(container))


@router.delete(f"{_SETTINGS_PATH}/egress-consent")
async def revoke_realtime_asr_consent(container: ApiContainerDep) -> JSONResponse:
    ProviderEgressPolicyStore(Path(container.root_dir)).revoke("qwen-realtime-asr")
    return _response(200, _public_settings(container))


@router.get(_LEXICON_PATH)
async def list_realtime_asr_lexicon(container: ApiContainerDep) -> JSONResponse:
    lexicon = _lexicon(Path(container.root_dir))
    selection = lexicon.select_for_session()
    return _response(
        200,
        {
            "revision": selection.revision,
            "terms": [_term_public(item) for item in lexicon.list_terms()],
            "pending_candidates": [
                _candidate_public(item)
                for item in lexicon.list_candidates(status="pending_review")
            ],
            "session_limit": MAX_SESSION_TERMS,
            "automatic_maintenance": "pending_review_only",
        },
    )


@router.post(f"{_LEXICON_PATH}/terms")
async def upsert_realtime_asr_term(
    request: Request, container: ApiContainerDep
) -> JSONResponse:
    body = await _json_body(request)
    if not isinstance(body, Mapping) or not set(body).issubset(
        {"term", "weight", "is_super"}
    ) or "term" not in body:
        return _response(400, {"detail": "realtime_asr_term_invalid"})
    try:
        item = _lexicon(Path(container.root_dir)).manual_upsert(
            body.get("term"),
            weight=body.get("weight", 4),
            is_super=body.get("is_super") is True,
        )
    except RealtimeAsrLexiconError as error:
        return _response(409, {"detail": str(error)})
    return _response(201, {"term": _term_public(item)})


@router.post(f"{_LEXICON_PATH}/terms/{{term_id}}/retire")
async def retire_realtime_asr_term(
    term_id: str, container: ApiContainerDep
) -> JSONResponse:
    lexicon = _lexicon(Path(container.root_dir))
    term = next((item for item in lexicon.list_terms() if item.term_id == term_id), None)
    if term is None:
        return _response(404, {"detail": "realtime_asr_term_not_found"})
    try:
        retired = lexicon.retire(term.term)
    except RealtimeAsrLexiconError as error:
        return _response(409, {"detail": str(error)})
    return _response(200, {"term": _term_public(retired)})


@router.post(f"{_LEXICON_PATH}/candidates")
async def propose_realtime_asr_candidate(
    request: Request, container: ApiContainerDep
) -> JSONResponse:
    body = await _json_body(request)
    allowed = {"term", "suggested_weight", "source_kind", "source_ref", "confirm_source"}
    if not isinstance(body, Mapping) or not set(body).issubset(allowed):
        return _response(400, {"detail": "realtime_asr_candidate_invalid"})
    if body.get("source_kind") not in {"explicit_correction", "confirmed_project_metadata"}:
        return _response(409, {"detail": "realtime_asr_candidate_source_not_confirmed"})
    if body.get("confirm_source") is not True:
        return _response(409, {"detail": "realtime_asr_candidate_source_not_confirmed"})
    source_ref = f"{body.get('source_kind')}:{str(body.get('source_ref') or 'workbench')[:400]}"
    try:
        candidate = _lexicon(Path(container.root_dir)).propose_candidate(
            body.get("term"),
            suggested_weight=body.get("suggested_weight", 4),
            source_ref=source_ref,
        )
    except RealtimeAsrLexiconError as error:
        return _response(409, {"detail": str(error)})
    return _response(
        201,
        {
            "candidate": _candidate_public(candidate),
            "activation": "pending_review",
        },
    )


@router.post(f"{_LEXICON_PATH}/candidates/{{candidate_id}}/review")
async def review_realtime_asr_candidate(
    candidate_id: str, request: Request, container: ApiContainerDep
) -> JSONResponse:
    body = await _json_body(request)
    if not isinstance(body, Mapping) or body.get("action") not in {"accept", "reject"}:
        return _response(400, {"detail": "realtime_asr_candidate_review_invalid"})
    lexicon = _lexicon(Path(container.root_dir))
    try:
        if body.get("action") == "reject":
            candidate = lexicon.reject_candidate(candidate_id)
            return _response(200, {"candidate": _candidate_public(candidate)})
        term = lexicon.accept_candidate(
            candidate_id,
            weight=body.get("weight"),
            is_super=body.get("is_super") is True,
        )
    except RealtimeAsrLexiconError as error:
        return _response(409, {"detail": str(error)})
    return _response(200, {"term": _term_public(term)})


@router.post(_WEBSOCKET_TICKET_PATH)
async def issue_realtime_asr_ticket(
    request: Request, container: ApiContainerDep
) -> JSONResponse:
    if server_mode(request):
        identity = server_identity(request)
        if identity is None:
            return _response(401, {"detail": "device_unauthorized"})
        from backend.security.user_context import USER_ACCESS
        access = USER_ACCESS.get()
        target = access.target_user_id if access is not None else identity.user_id
        ticket = _TICKET_AUTHORITY.issue(subject=f"device:{identity.user_id}:{identity.device_id}:{target}")
        return _response(201, {"ticket": ticket, "expires_in_seconds": REALTIME_ASR_TICKET_TTL_SECONDS,
            "single_use": True, "transport": "subprotocol", "protocol": "chriptmas-asr"})
    try:
        session = desktop_session()
    except RuntimeError:
        return _response(503, {"detail": "desktop_session_config_invalid"})
    if session is not None and not desktop_session_authorized(
        request.headers.get(DESKTOP_SESSION_HEADER)
    ):
        return _response(403, {"detail": "desktop_session_unauthorized"})
    subject = session.instance_id if session is not None else "development"
    ticket = _TICKET_AUTHORITY.issue(subject=subject)
    return _response(
        201,
        {
            "ticket": ticket,
            "expires_in_seconds": REALTIME_ASR_TICKET_TTL_SECONDS,
            "single_use": True,
        },
    )


@router.websocket(_WEBSOCKET_PATH)
async def realtime_asr_websocket(websocket: WebSocket) -> None:
    if not _websocket_authorized(websocket):
        await websocket.accept()
        await websocket.send_json({"type": "error", "code": "desktop_session_unauthorized"})
        await websocket.close(code=4403)
        return
    await websocket.accept(subprotocol="chriptmas-asr" if server_mode(websocket)
        and "chriptmas-asr" in websocket.scope.get("subprotocols", []) else None)
    root = Path(websocket.app.state.container.root_dir)
    container = websocket.app.state.container
    settings = _settings(root)
    manifest = qwen_realtime_egress_manifest(root, endpoint=settings.endpoint)
    policy = ProviderEgressPolicyStore(root)
    if not settings.enabled:
        await _fail_socket(websocket, "realtime_asr_disabled", 4403)
        return
    if not container.secret_store.has_secret(QWEN_REALTIME_ASR_SECRET_REF):
        await _fail_socket(websocket, "realtime_asr_api_key_required", 4403)
        return
    if not policy.is_consented(manifest):
        await _fail_socket(websocket, "realtime_asr_consent_required", 4403)
        return
    lexicon = _lexicon(root)
    _propose_confirmed_project_name(
        root,
        str(websocket.query_params.get("project_id") or "default")[:128],
        lexicon,
    )
    selection = lexicon.select_for_session()
    task_id = str(uuid4())
    snapshot_id = f"realtime-asr-session-{uuid4().hex}"
    store, _storage = build_rebuild_object_store(root)
    snapshot = {
        "schema_version": "1.0.0",
        "id": snapshot_id,
        "status": "opening",
        "provider_id": settings.provider_id,
        "model": settings.model,
        "settings_revision": settings.settings_revision,
        "secret_generation": container.secret_store.get_generation(QWEN_REALTIME_ASR_SECRET_REF),
        "egress_manifest_id": manifest.manifest_id,
        "lexicon_revision": selection.revision,
        "selected_term_ids": [item.term_id for item in selection.terms],
        "started_at": _now(),
        "audio_bytes_sent": 0,
    }
    session_service = RealtimeSessionService(store)
    session_service.start(snapshot)
    connector = getattr(websocket.app.state, "qwen_realtime_connector", None)
    vocabulary = {item.term: item.weight for item in selection.terms}
    vocabulary_bytes = len(json.dumps(vocabulary, ensure_ascii=False).encode("utf-8"))
    try:
        lease = policy.authorize(
            manifest,
            purpose="realtime_transcription",
            payload_categories=("microphone_pcm_audio", "session_vocabulary"),
            payload_bytes=settings.max_audio_bytes + vocabulary_bytes,
        )
    except ProviderEgressError as error:
        session_service.settle(snapshot, status="blocked", error_code=str(error))
        await _fail_socket(websocket, "realtime_asr_consent_required", 4403)
        return
    try:
        secure_connector = QwenRealtimeSecureConnector(
            container.secret_store, boundary_revision=manifest.manifest_id, wire_factory=connector,
        )
        upstream_connection = secure_connector.connect(settings.endpoint)
        async with upstream_connection as upstream:
            session_service.update(snapshot, secret_generation=upstream_connection.secret_generation)
            await upstream.send(json.dumps(build_run_task(
                task_id=task_id, settings=settings, vocabulary=vocabulary
            ), ensure_ascii=False))
            try:
                started = parse_qwen_event(json.loads(await asyncio.wait_for(upstream.recv(), 20)))
            except asyncio.TimeoutError as error:
                raise RealtimeAsrSessionError("handshake", "start_timeout", True, "retry") from error
            if started.kind != "started":
                raise RealtimeAsrSessionError("handshake", "protocol_mismatch", False, "check_settings")
            session_service.update(snapshot, status="streaming")
            await websocket.send_json({
                "type": "ready",
                "session_id": snapshot_id,
                "lexicon_revision": selection.revision,
                "hotword_count": len(selection.terms),
                "sample_rate": settings.sample_rate,
                "max_session_seconds": settings.max_session_seconds,
            })
            outcome, bytes_sent = await session_service.relay_until_terminal(
                websocket, upstream, task_id=task_id,
                max_audio_bytes=settings.max_audio_bytes,
                secret_generation=upstream_connection.secret_generation, container=container,
                policy=policy, manifest=manifest, vocabulary_bytes=vocabulary_bytes,
                timeout_seconds=settings.max_session_seconds + 30,
            )
        lease.finish("completed" if outcome == "finished" else "cancelled")
        session_service.settle(snapshot, status=outcome, error_code="")
    except (RealtimeClientDisconnected, WebSocketDisconnect):
        lease.finish("cancelled", error_code="client_disconnected")
        session_service.settle(snapshot, status="unknown", error_code="client_disconnected")
    except RealtimeSessionTimeout:
        lease.finish("failed", error_code="stream_timeout")
        session_service.settle(snapshot, status="failed", error_code="stream_timeout")
        await _safe_socket_error(websocket, "stream_timeout", stage="stream", retryable=True, action="retry")
    except RealtimeAsrSessionError as error:
        lease.finish("failed", error_code=error.code)
        session_service.settle(snapshot, status="failed", error_code=error.code)
        await _safe_socket_error(websocket, error.code, stage=error.stage, retryable=error.retryable, action=error.action)
    except InvalidStatus as error:
        status_code = getattr(getattr(error, "response", None), "status_code", 0)
        code = "upstream_auth_failed" if status_code in {401, 403} else "workspace_or_model_denied" if status_code == 404 else "transport_unreachable"
        lease.finish("failed", error_code=code)
        session_service.settle(snapshot, status="failed", error_code=code)
        await _safe_socket_error(websocket, code, stage="handshake", retryable=code == "transport_unreachable", action="check_settings")
    except (OSError, ConnectionError):
        lease.finish("failed", error_code="transport_unreachable")
        session_service.settle(snapshot, status="failed", error_code="transport_unreachable")
        await _safe_socket_error(websocket, "transport_unreachable", stage="handshake", retryable=True, action="retry")
    except Exception:
        lease.finish("failed", error_code="provider_unavailable")
        session_service.settle(snapshot, status="failed", error_code="provider_unavailable")
        await _safe_socket_error(
            websocket, "provider_unavailable", stage="stream", retryable=True, action="retry"
        )

def _public_settings(container: object) -> dict[str, object]:
    root = Path(getattr(container, "root_dir"))
    settings = _settings(root)
    manifest = qwen_realtime_egress_manifest(root, endpoint=settings.endpoint)
    has_key = container.secret_store.has_secret(QWEN_REALTIME_ASR_SECRET_REF)
    consented = ProviderEgressPolicyStore(root).is_consented(manifest)
    status = "disabled" if not settings.enabled else "needs_api_key" if not has_key else "needs_consent" if not consented else "ready"
    return {
        "status": status,
        "enabled": settings.enabled,
        "provider_id": settings.provider_id,
        "provider_name": settings.provider_name,
        "model": settings.model,
        "endpoint": settings.endpoint,
        "region": settings.region,
        "workspace_id": settings.workspace_id,
        "sample_rate": settings.sample_rate,
        "max_session_seconds": settings.max_session_seconds,
        "max_audio_bytes": settings.max_audio_bytes,
        "remote_processing": True,
        "settings_revision": settings.settings_revision,
        "has_api_key": has_key,
        "secret_generation": container.secret_store.get_generation(QWEN_REALTIME_ASR_SECRET_REF),
        "egress_manifest": manifest.public_dict(consented=consented),
        "hotword_mode": "local_reviewed_immediate_vocabulary",
        "automatic_maintenance": "pending_review_only",
    }


def _settings(root: Path):
    store, _storage = build_rebuild_object_store(root)
    return GetRealtimeAsrProviderSettings(store).execute()


def _lexicon(root: Path) -> RealtimeAsrLexicon:
    store, _storage = build_rebuild_object_store(root)
    return RealtimeAsrLexicon(store)


def _propose_confirmed_project_name(
    root: Path, project_id: str, lexicon: RealtimeAsrLexicon
) -> None:
    """Turn confirmed project metadata into a review candidate, never an active term."""
    try:
        _store, settings = build_rebuild_object_store(root)
        json_store = JsonObjectStore(
            root / ".rebuild-data",
            legacy_root=root / "library",
            namespace_id=settings.namespace_id,
        )
        skill = AggregateRepositoryFactory(
            runtime_root=root,
            namespace_id=settings.namespace_id,
            json_store=json_store,
        ).project_skill_repository().load(project_id)
        if (
            not isinstance(skill, Mapping)
            or skill.get("status") != "active"
            or skill.get("trust_status") != "user_confirmed"
            or not isinstance(skill.get("name"), str)
        ):
            return
        name = str(skill["name"]).strip()
        normalized = name.casefold()
        known = {item.term.casefold() for item in lexicon.list_terms()}
        candidates = {
            item.term.casefold()
            for status in ("pending_review", "accepted", "rejected")
            for item in lexicon.list_candidates(status=status)
        }
        if normalized in known or normalized in candidates:
            return
        lexicon.propose_candidate(
            name,
            suggested_weight=4,
            source_ref=f"confirmed_project_metadata:{project_id}:r{skill.get('revision', 0)}",
        )
    except Exception:
        # Automatic maintenance is proposal-only and must never block recording.
        return


def _websocket_authorized(websocket: WebSocket) -> bool:
    if server_mode(websocket):
        return server_authorized(websocket)
    try:
        session = desktop_session()
    except RuntimeError:
        return False
    if session is None:
        if not os.environ.get("CHRIPTMAS_WORKER_SECRET"):
            return True
        return _TICKET_AUTHORITY.consume(
            websocket.query_params.get("ticket"), subject="development"
        )
    if datetime.fromisoformat(session.expires_at).astimezone(timezone.utc) <= datetime.now(timezone.utc):
        return False
    if desktop_session_authorized(websocket.headers.get(DESKTOP_SESSION_HEADER)):
        return True
    return _TICKET_AUTHORITY.consume(
        websocket.query_params.get("ticket"), subject=session.instance_id
    )


def consume_server_ticket(token, registry, *, users=None):
    subject = _TICKET_AUTHORITY.consume_subject(token)
    if subject is None:
        return None
    parts = subject.split(":")
    if len(parts) not in {3, 4} or parts[0] != "device":
        return None
    caller = registry.identity_for_device(parts[2], parts[1])
    if caller is None:
        return None
    target = parts[3] if len(parts) == 4 else caller.user_id
    if users is not None:
        from backend.security.user_context import UserError, authorize_user
        try:
            access = authorize_user(users, caller, target)
            if users.get(access.target_user_id)['disabled_at'] is not None:
                return None
        except UserError:
            return None
    elif target != caller.user_id:
        return None
    return ServerTicketIdentity(caller, target)


async def _json_body(request: Request) -> object:
    try:
        return await request.json()
    except Exception:
        return None


async def _fail_socket(websocket: WebSocket, code: str, close_code: int) -> None:
    await websocket.send_json(_socket_error(code))
    await websocket.close(code=close_code)


async def _safe_socket_error(websocket: WebSocket, code: str, *, stage: str = "stream", retryable: bool = False, action: str = "check_settings") -> None:
    try:
        await websocket.send_json(_socket_error(code, stage=stage, retryable=retryable, action=action))
        await websocket.close(code=1011)
    except Exception:
        pass


def _socket_error(code: str, *, stage: str = "admission", retryable: bool = False, action: str = "check_settings") -> dict[str, object]:
    return {
        "type": "error", "stage": stage, "code": code, "retryable": retryable,
        "action": action, "trace_id": f"realtime-asr-{uuid4().hex}",
    }


class RealtimeAsrSessionError(RuntimeError):
    def __init__(self, stage: str, code: str, retryable: bool, action: str) -> None:
        self.stage, self.code, self.retryable, self.action = stage, code, retryable, action


def _update_snapshot(store: object, current: dict[str, object], **changes: object) -> None:
    current.update(changes)
    current["updated_at"] = _now()
    store.write("realtime_asr_session_snapshots", str(current["id"]), dict(current), expected_revision=None)


def _term_public(item: object) -> dict[str, object]:
    return {
        "term_id": item.term_id,
        "term": item.term,
        "weight": item.weight,
        "status": item.status,
        "origin": item.origin,
        "revision": item.revision,
    }


def _candidate_public(item: object) -> dict[str, object]:
    return {
        "candidate_id": item.candidate_id,
        "term": item.term,
        "suggested_weight": item.suggested_weight,
        "status": item.status,
        "revision": item.revision,
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _response(status: int, body: Mapping[str, object]) -> JSONResponse:
    return JSONResponse(status_code=status, content=dict(body), headers={"Cache-Control": "no-store"})
