from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlsplit

from .memory_publication import (
    MemoryPublicationError,
    MemoryPublicationResult,
    MemoryRollbackResult,
    serialize_memory_rollback_result,
    serialize_memory_publication_result,
)


@dataclass(frozen=True, slots=True)
class MemoryPublicationEndpointResponse:
    status_code: int
    body: Mapping[str, Any]
    headers: Mapping[str, str]


class ServeMemoryPublicationEndpoint:
    """Serve the narrow staging Atom publication endpoint."""

    def execute(
        self,
        *,
        method: str,
        path: str,
        body: Mapping[str, object] | None,
        publish_atom: Callable[..., MemoryPublicationResult],
        publish_memory: Callable[..., MemoryPublicationResult] | None = None,
        rollback_publication: Callable[..., MemoryRollbackResult] | None = None,
    ) -> MemoryPublicationEndpointResponse:
        publication_id = _publication_id_from_rollback_path(path)
        if publication_id is not None:
            return self._serve_rollback(
                method=method,
                publication_id=publication_id,
                body=body,
                rollback_publication=rollback_publication,
            )
        publication_target = _publication_target_from_path(path)
        if publication_target is None:
            return self._json_response(404, {"detail": "memory publication endpoint not found"})
        if method.upper() != "POST":
            return self._json_response(
                405,
                {"detail": "memory publication endpoint only supports POST"},
                extra_headers={"Allow": "POST"},
            )
        try:
            if body is None:
                raise MemoryPublicationError("memory publication body is required")
            if publication_target["layer"] == "atom":
                result = publish_atom(
                    atom_id=publication_target["object_id"],
                    confirm=body.get("confirm") is True,
                    reason=_required_body_str(body, "reason"),
                    published_by="user",
                )
            else:
                if publish_memory is None:
                    raise MemoryPublicationError("layered memory publication is not configured")
                result = publish_memory(
                    object_id=publication_target["object_id"],
                    layer=publication_target["layer"],
                    confirm=body.get("confirm") is True,
                    reason=_required_body_str(body, "reason"),
                    published_by="user",
                )
        except MemoryPublicationError as error:
            status_code = 404 if str(error).startswith("staging ") and str(error).endswith(" not found") else 400
            return self._json_response(
                status_code,
                {
                    "detail": "memory publication rejected",
                    "reason": str(error),
                    "actionable": True,
                },
            )
        return self._json_response(200, serialize_memory_publication_result(result))

    def _serve_rollback(
        self,
        *,
        method: str,
        publication_id: str,
        body: Mapping[str, object] | None,
        rollback_publication: Callable[..., MemoryRollbackResult] | None,
    ) -> MemoryPublicationEndpointResponse:
        if method.upper() != "POST":
            return self._json_response(
                405,
                {"detail": "memory rollback endpoint only supports POST"},
                extra_headers={"Allow": "POST"},
            )
        if rollback_publication is None:
            return self._json_response(
                400,
                {
                    "detail": "memory rollback rejected",
                    "reason": "memory rollback is not configured",
                    "actionable": True,
                },
            )
        try:
            if body is None:
                raise MemoryPublicationError("memory rollback body is required")
            result = rollback_publication(
                publication_id=publication_id,
                confirm=body.get("confirm") is True,
                reason=_required_body_str(body, "reason"),
                rolled_back_by="user",
            )
        except MemoryPublicationError as error:
            status_code = 404 if str(error) == "memory publication not found" or (
                str(error).startswith("published ") and str(error).endswith(" not found")
            ) else 400
            return self._json_response(
                status_code,
                {
                    "detail": "memory rollback rejected",
                    "reason": str(error),
                    "actionable": True,
                },
            )
        return self._json_response(200, serialize_memory_rollback_result(result))

    def _json_response(
        self,
        status_code: int,
        body: Mapping[str, Any],
        *,
        extra_headers: Mapping[str, str] | None = None,
    ) -> MemoryPublicationEndpointResponse:
        headers = {"Content-Type": "application/json", "Cache-Control": "no-store"}
        if extra_headers:
            headers.update(extra_headers)
        return MemoryPublicationEndpointResponse(status_code=status_code, body=body, headers=headers)


def _publication_target_from_path(path: str) -> dict[str, str] | None:
    parsed = urlsplit(path)
    suffix = "/publication"
    if not parsed.path.endswith(suffix):
        return None
    for prefix, layer in (
        ("/api/rebuild/staging-atoms/", "atom"),
        ("/api/rebuild/staging-scenarios/", "scenario"),
        ("/api/rebuild/staging-series-memory/", "series_memory"),
        ("/api/rebuild/staging-project-skills/", "project_skill"),
    ):
        if parsed.path.startswith(prefix):
            object_id = unquote(parsed.path[len(prefix) : -len(suffix)]).strip()
            return {"layer": layer, "object_id": object_id} if object_id else None
    return None


def _publication_id_from_rollback_path(path: str) -> str | None:
    parsed = urlsplit(path)
    prefix = "/api/rebuild/memory-publications/"
    suffix = "/rollback"
    if not parsed.path.startswith(prefix) or not parsed.path.endswith(suffix):
        return None
    publication_id = unquote(parsed.path[len(prefix) : -len(suffix)]).strip()
    return publication_id or None


def _required_body_str(body: Mapping[str, object], key: str) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value.strip():
        raise MemoryPublicationError(f"{key} must be a non-empty string")
    return value.strip()
