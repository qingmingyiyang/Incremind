"""HTTP input validation and public workspace item projection."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from fastapi import HTTPException, Request



_COLLECTION = "workspace_items"


_MAX_FILE = 12 * 1024 * 1024


_MAX_TEXT = 60000


_PROJECT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$")


async def _json(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "invalid_json") from None
    if not isinstance(body, dict):
        raise HTTPException(400, "invalid_json")
    return body


def _project(value: object) -> str:
    if not isinstance(value, str) or not _PROJECT.fullmatch(value):
        raise HTTPException(400, "invalid_project_id")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > _MAX_TEXT:
        raise HTTPException(400, "invalid_" + label)
    return value.strip()


def _optional_title(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise HTTPException(400, "invalid_title")
    return value.strip()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _public(item: dict) -> dict:
    return {key: value for key, value in item.items()
            if key not in {"original_path", "processing_pid", "media_request_url",
                           "processing_consent", "processing_run_id", "processing_instance_id",
                           "processing_heartbeat_at", "processing_lease_expires_at",
                           "audio_transcription"}}


_ASK_PART_KEYS = ("insight", "persona", "summary", "note", "source", "instruction", "question", "history")

def _empty_ask_context():
    return {"window": None, "reserve": None,
            "parts": [{"key": key, "count": 0, "tokens": 0} for key in _ASK_PART_KEYS], "entries": []}
