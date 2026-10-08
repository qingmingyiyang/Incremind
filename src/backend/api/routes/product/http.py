"""Http ownership for the product API."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse


def _no_store_headers() -> dict[str, str]:
    return {"Content-Type": "application/json", "Cache-Control": "no-store"}


async def _json_body(request: Request) -> Mapping[str, object] | None:
    if request.method.upper() == "GET":
        return None
    try:
        payload: Any = await request.json()
    except Exception:
        return None
    return payload if isinstance(payload, Mapping) else None


def _optional_body_str(body: Mapping[str, object] | None, key: str) -> str | None:
    if not isinstance(body, Mapping):
        return None
    value = body.get(key)
    if not isinstance(value, str):
        return None
    clean = value.strip()
    return clean or None


def _clean_str(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    clean = value.strip()
    return clean or None


def _optional_body_text(body: Mapping[str, object] | None, key: str) -> str | None:
    if not isinstance(body, Mapping):
        return None
    value = body.get(key)
    return value if isinstance(value, str) else None


def _optional_body_int(body: Mapping[str, object] | None, key: str) -> int | None:
    if not isinstance(body, Mapping):
        return None
    value = body.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    return value


def _required_body_int(body: Mapping[str, object] | None, key: str) -> int:
    value = _optional_body_int(body, key)
    if value is None or value < 0:
        raise ValueError(f"{key} must be a non-negative integer")
    return value


def _required_body_bool(body: Mapping[str, object] | None, key: str) -> bool:
    if not isinstance(body, Mapping):
        raise ValueError(f"{key} is required")
    value = body.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be boolean")
    return value


def _optional_body_bool(body: Mapping[str, object] | None, key: str) -> bool | None:
    if not isinstance(body, Mapping):
        return None
    value = body.get(key)
    return value if isinstance(value, bool) else None


def _optional_body_str_list(body: Mapping[str, object] | None, key: str) -> tuple[str, ...]:
    if not isinstance(body, Mapping):
        return ()
    value = body.get(key)
    if not isinstance(value, list):
        return ()
    return tuple(item.strip() for item in value if isinstance(item, str) and item.strip())


def _list_body(
    body: Mapping[str, object] | None,
    key: str,
    *,
    default: list[object] | None = None,
) -> list[object]:
    if not isinstance(body, Mapping):
        raise ValueError("request body must be an object")
    value = body.get(key, default)
    if not isinstance(value, list):
        raise ValueError(f"{key} must be a list")
    return value


def _mapping_body(body: Mapping[str, object] | None, key: str) -> Mapping[str, object]:
    if not isinstance(body, Mapping):
        raise ValueError("request body must be an object")
    value = body.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"{key} must be an object")
    return value


def _optional_query_str(request: Request, key: str) -> str | None:
    return _clean_str(request.query_params.get(key))


def _optional_query_int(request: Request, key: str) -> int | None:
    value = request.query_params.get(key)
    if value is None:
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed >= 1 else None


def _json_response(
    status_code: int,
    body: Mapping[str, Any],
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        content=body,
        status_code=status_code,
        headers={key: value for key, value in (headers or {"Cache-Control": "no-store"}).items() if key.lower() != "content-type"},
    )
