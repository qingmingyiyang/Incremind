"""Desktop-only credential capture into the existing SecretStore authority."""

from __future__ import annotations

import re
from collections.abc import Mapping

from fastapi import APIRouter, Request
from fastapi import HTTPException
from fastapi.responses import JSONResponse

from backend.api.container import ApiContainerDep
from backend.api.desktop_session import DESKTOP_SESSION_HEADER, desktop_session_authorized


router = APIRouter(tags=["credential-capture"])
_PATH = "/api/rebuild/security/credentials/capture"
_FIELDS = {"credential_kind", "credential_subject", "value", "command_id"}
_COMMAND = re.compile(r"^cmd-[a-z0-9][a-z0-9._-]{7,122}$")
_SUBJECT = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_PREFIXES = {
    "provider_api_key": "provider",
    "tokenhub_asr_api_key": "asr",
    "qwen_realtime_asr_api_key": "asr",
    "xiaohongshu_cookie": "xiaohongshu",
}


@router.post(_PATH)
async def capture_credential(request: Request, container: ApiContainerDep) -> JSONResponse:
    if not desktop_session_authorized(request.headers.get(DESKTOP_SESSION_HEADER)):
        return _response(403, "credential_capture_desktop_required")
    try:
        payload = await request.json()
    except Exception:
        return _response(400, "credential_capture_invalid")
    if not isinstance(payload, Mapping) or set(payload) != _FIELDS:
        return _response(400, "credential_capture_invalid")
    kind, subject = payload.get("credential_kind"), payload.get("credential_subject")
    value, command_id = payload.get("value"), payload.get("command_id")
    if kind not in _PREFIXES or not isinstance(subject, str) or _SUBJECT.fullmatch(subject) is None:
        return _response(400, "credential_capture_invalid")
    if not isinstance(value, str) or not 1 <= len(value) <= 16 * 1024:
        return _response(400, "credential_capture_invalid")
    if not isinstance(command_id, str) or _COMMAND.fullmatch(command_id) is None:
        return _response(400, "credential_capture_invalid")
    completed = getattr(request.app.state, "credential_capture_commands", None)
    if not isinstance(completed, set):
        completed = set()
        request.app.state.credential_capture_commands = completed
    if command_id in completed:
        return _response(409, "credential_capture_command_replayed")
    secret_ref = f"{_PREFIXES[str(kind)]}:{subject}"
    try:
        if kind == "provider_api_key":
            from backend.api.provider_credentials import store_provider_credential

            generation = store_provider_credential(container, subject, value)
        else:
            container.secret_store.set(secret_ref, value)
            generation = container.secret_store.get_generation(secret_ref)
    except HTTPException:
        return _response(400, "credential_capture_invalid")
    except Exception:
        return _response(503, "credential_capture_unavailable")
    completed.add(command_id)
    return JSONResponse(
        status_code=201,
        content={
            "stored": True,
            "secret_ref": secret_ref,
            "generation": generation,
            "authorization_revision": generation,
        },
        headers={"Cache-Control": "no-store"},
    )


def _response(status: int, code: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"detail": code}, headers={"Cache-Control": "no-store"})
