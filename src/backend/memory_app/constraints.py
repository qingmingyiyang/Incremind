"""Explicit user-configured project constraints.

Constraints are a small, separate authority from recognitions.  They are never
derived from model output or memory text and are scoped only by the supplied
local user and project identity.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timezone
import re

from backend.recognition import RecognitionConflict, WorkScope
from core.storage_provider import SQLiteStructuredRecord, SQLiteStructuredRecordStore


_CONSTRAINTS = "project_constraints"
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$")
_MAX_CONTENT = 100_000


class ProjectConstraintError(RecognitionConflict):
    """A project constraint request cannot be applied safely."""


class ProjectConstraintService:
    """Persist only constraints a user explicitly configured for one project."""

    def __init__(
        self,
        records: SQLiteStructuredRecordStore,
        *,
        now: Callable[[], datetime | str] | None = None,
    ) -> None:
        self._records = records
        self._now = now or _utc_now

    def list(self, scope: WorkScope) -> tuple[dict[str, object], ...]:
        now = _as_utc(self._now(), "now")
        return tuple(
            _payload(record, now)
            for record in self._records.list(_CONSTRAINTS)
            if _same_scope(record.payload, scope)
        )

    def active(self, scope: WorkScope) -> tuple[dict[str, object], ...]:
        now = _as_utc(self._now(), "now")
        return tuple(
            item
            for record in self._records.list(_CONSTRAINTS)
            if _same_scope(record.payload, scope)
            for item in (_payload(record, now),)
            if item["effective"]
        )

    def upsert(
        self,
        scope: WorkScope,
        constraint_id: str,
        expected_revision: int,
        content: str,
        *,
        enabled: bool = True,
        valid_from: str | None = None,
        valid_until: str | None = None,
    ) -> dict[str, object]:
        constraint_id = _id(constraint_id, "constraint_id")
        expected_revision = _expected_revision(expected_revision)
        content = _content(content)
        if not isinstance(enabled, bool):
            raise ProjectConstraintError("enabled is invalid")
        start = _optional_time(valid_from, "valid_from")
        end = _optional_time(valid_until, "valid_until")
        if start is not None and end is not None and start >= end:
            raise ProjectConstraintError("valid_from must be before valid_until")
        current_time = _as_utc(self._now(), "now")

        with self._records.begin() as tx:
            existing = tx.read(_CONSTRAINTS, constraint_id)
            if existing is not None and not _same_scope(existing.payload, scope):
                raise ProjectConstraintError("constraint is unavailable in this project")
            if existing is not None and existing.revision != expected_revision:
                raise ProjectConstraintError(
                    f"expected revision {expected_revision}, found {existing.revision}"
                )
            if existing is None and expected_revision != 0:
                raise ProjectConstraintError(f"expected revision {expected_revision}, found 0")

            created_at = existing.payload.get("created_at") if existing is not None else _time_text(current_time)
            payload = {
                "id": constraint_id,
                "scope": {"user_id": scope.user_id, "project_id": scope.project_id},
                "project_id": scope.project_id,
                "content": content,
                "enabled": enabled,
                "valid_from": _time_text(start) if start is not None else None,
                "valid_until": _time_text(end) if end is not None else None,
                "created_at": created_at,
                "updated_at": _time_text(current_time),
            }
            if existing is not None and _same_constraint(existing.payload, payload):
                tx.rollback()
                return _payload(existing, current_time)
            stored = tx.put(_CONSTRAINTS, constraint_id, payload, expected_revision=expected_revision)
            tx.commit()
        return _payload(stored, current_time)

    def validate_snapshot(
        self, scope: WorkScope, constraints: Iterable[Mapping[str, object]] | None = None
    ) -> tuple[dict[str, object], ...]:
        """Reject a preview whose active constraint ID/revision set changed."""
        supplied = _snapshot(constraints)
        current = self.active(scope)
        expected = {(item["id"], item["revision"]) for item in current}
        if supplied != expected:
            raise ProjectConstraintError("project constraints changed; preview again")
        return current


def _payload(record: SQLiteStructuredRecord, now: datetime) -> dict[str, object]:
    payload = record.payload
    try:
        constraint_id = _id(payload.get("id"), "stored constraint id")
        scope = payload.get("scope")
        if not isinstance(scope, Mapping):
            raise ProjectConstraintError("stored constraint scope is invalid")
        user_id = _id(scope.get("user_id"), "stored user_id")
        project_id = scope.get("project_id")
        if project_id is not None:
            project_id = _id(project_id, "stored project_id")
        if payload.get("project_id") != project_id:
            raise ProjectConstraintError("stored constraint scope is invalid")
        content = _content(payload.get("content"))
        enabled = payload.get("enabled")
        if not isinstance(enabled, bool):
            raise ProjectConstraintError("stored constraint enabled is invalid")
        start = _optional_time(payload.get("valid_from"), "stored valid_from")
        end = _optional_time(payload.get("valid_until"), "stored valid_until")
        if start is not None and end is not None and start >= end:
            raise ProjectConstraintError("stored constraint time range is invalid")
        created_at = _time_text(_as_utc(payload.get("created_at"), "stored created_at"))
        updated_at = _time_text(_as_utc(payload.get("updated_at"), "stored updated_at"))
    except ProjectConstraintError:
        raise
    result = {
        "id": constraint_id, "revision": record.revision, "project_id": project_id,
        "user_id": user_id, "content": content, "enabled": enabled,
        "valid_from": _time_text(start) if start is not None else None,
        "valid_until": _time_text(end) if end is not None else None,
        "created_at": created_at, "updated_at": updated_at,
    }
    result["effective"] = _active_payload(result, now)
    return result


def _active_payload(payload: Mapping[str, object], now: datetime) -> bool:
    if payload.get("enabled") is not True:
        return False
    start = _optional_time(payload.get("valid_from"), "valid_from")
    end = _optional_time(payload.get("valid_until"), "valid_until")
    return (start is None or start <= now) and (end is None or now < end)


def _same_scope(payload: Mapping[str, object], scope: WorkScope) -> bool:
    value = payload.get("scope")
    return isinstance(value, Mapping) and value.get("user_id") == scope.user_id and value.get("project_id") == scope.project_id


def _same_constraint(existing: Mapping[str, object], requested: Mapping[str, object]) -> bool:
    return all(existing.get(field) == requested.get(field) for field in (
        "id", "scope", "project_id", "content", "enabled", "valid_from", "valid_until", "created_at"
    ))


def _snapshot(constraints: Iterable[Mapping[str, object]] | None) -> set[tuple[str, int]]:
    if constraints is None:
        constraints = ()
    if isinstance(constraints, (str, bytes)):
        raise ProjectConstraintError("constraints snapshot is invalid")
    result: set[tuple[str, int]] = set()
    try:
        for item in constraints:
            if not isinstance(item, Mapping):
                raise ProjectConstraintError("constraints snapshot is invalid")
            pair = (_id(item.get("id"), "constraint id"), _revision(item.get("revision")))
            if pair in result or any(old_id == pair[0] for old_id, _ in result):
                raise ProjectConstraintError("constraints snapshot has duplicate ids")
            result.add(pair)
    except TypeError as exc:
        raise ProjectConstraintError("constraints snapshot is invalid") from exc
    return result


def _id(value: object, label: str) -> str:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        raise ProjectConstraintError(f"{label} is invalid")
    return value


def _content(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > _MAX_CONTENT:
        raise ProjectConstraintError("content is invalid")
    return value.strip()


def _expected_revision(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ProjectConstraintError("expected_revision is invalid")
    return value


def _revision(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ProjectConstraintError("revision is invalid")
    return value


def _optional_time(value: object, label: str) -> datetime | None:
    return None if value is None else _as_utc(value, label)


def _as_utc(value: object, label: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ProjectConstraintError(f"{label} is invalid") from exc
    else:
        raise ProjectConstraintError(f"{label} is invalid")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProjectConstraintError(f"{label} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _time_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
