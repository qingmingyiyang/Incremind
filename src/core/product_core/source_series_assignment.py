from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass

from .ports import ObjectStorePort


class SourceSeriesAssignmentError(ValueError):
    """Raised when a Source series assignment would bypass confirmation."""


@dataclass(frozen=True, slots=True)
class SourceSeriesAssignmentResult:
    status: str
    source_id: str
    series_id: str
    series_name: str
    assignment_id: str
    assignment_ref: str
    series_ref: str
    structure_ref: str | None
    activity_refs: tuple[str, ...]
    memory_publication_state: str
    blocked_operations: tuple[str, ...]


class ConfirmSourceSeriesAssignment:
    """Promote a generated series candidate into a user-confirmed Library series link."""

    _BLOCKED_OPERATIONS = (
        "automatic_series_write",
        "model_provider_execution",
        "memory_candidate_auto_creation",
        "long_term_memory_publication",
    )

    def __init__(
        self,
        object_store: ObjectStorePort,
        *,
        namespace_id: str = "default",
        now: str = "2026-07-02T18:45:00+08:00",
    ) -> None:
        self._object_store = object_store
        self._namespace_id = namespace_id
        self._now = now

    def execute(
        self,
        *,
        source_id: str,
        confirm: bool = False,
        series_name: str | None = None,
        reason: str | None = None,
        confirmed_by: str = "user",
    ) -> SourceSeriesAssignmentResult:
        clean_source_id = source_id.strip()
        if not clean_source_id:
            raise SourceSeriesAssignmentError("source_id is required")
        is_system_confirmation = confirmed_by == "system"
        if not is_system_confirmation and confirm is not True:
            raise SourceSeriesAssignmentError("series assignment requires explicit user confirmation")
        source = self._object_store.read("sources", clean_source_id)
        if source is None:
            raise SourceSeriesAssignmentError("source not found")
        metadata = dict(source.get("metadata") if isinstance(source.get("metadata"), Mapping) else {})
        structure = metadata.get("content_structure")
        if not isinstance(structure, Mapping) or structure.get("status") != "completed":
            raise SourceSeriesAssignmentError("completed content structure is required before series assignment")

        candidate = _clean_series_name(series_name) or _clean_series_name(structure.get("series_candidate"))
        if candidate is None or candidate == "未归类资料":
            raise SourceSeriesAssignmentError("series assignment requires a concrete series name")

        series_id = _series_id(candidate)
        assignment_id = f"series-assignment-{clean_source_id}"
        assignment_ref = f"crp://{self._namespace_id}/source-series-assignments/{assignment_id}.json"
        series_ref = f"crp://{self._namespace_id}/library-series/{series_id}.json"
        structure_ref = _optional_str(structure.get("structure_ref"))
        event_ref = self._write_event(
            source_id=clean_source_id,
            series_id=series_id,
            series_name=candidate,
            assignment_ref=assignment_ref,
            structure_ref=structure_ref,
        )
        assignment_record = {
            "schema_version": "1.0.0",
            "id": assignment_id,
            "source_id": clean_source_id,
            "status": "confirmed",
            "series_id": series_id,
            "series_name": candidate,
            "series_ref": series_ref,
            "structure_ref": structure_ref,
            "confirmed_by": confirmed_by,
            "confirmed_at": self._now,
            "reason": _clean_reason(reason) or (
                "高置信度系列候选由系统自动确认，保留用户后续调整入口。"
                if is_system_confirmation
                else "用户确认资料归入该系列。"
            ),
            "activity_refs": [event_ref],
            "memory_publication": "not_started",
            "blocked_operations": list(self._BLOCKED_OPERATIONS),
            "ref": assignment_ref,
        }
        self._object_store.write(
            "source_series_assignments",
            assignment_id,
            assignment_record,
            expected_revision=None,
        )
        self._upsert_series(series_id=series_id, series_name=candidate, source_id=clean_source_id, series_ref=series_ref)
        metadata["series_assignment"] = {
            "status": "confirmed",
            "assignment_id": assignment_id,
            "assignment_ref": assignment_ref,
            "series_id": series_id,
            "series_name": candidate,
            "series_ref": series_ref,
            "structure_ref": structure_ref,
            "confirmed_by": confirmed_by,
            "confirmed_at": self._now,
            "activity_refs": [event_ref],
            "memory_publication": "not_started",
            "blocked_operations": list(self._BLOCKED_OPERATIONS),
        }
        updated = dict(source)
        updated["metadata"] = metadata
        self._object_store.write("sources", clean_source_id, updated, expected_revision=None)
        return SourceSeriesAssignmentResult(
            status="confirmed",
            source_id=clean_source_id,
            series_id=series_id,
            series_name=candidate,
            assignment_id=assignment_id,
            assignment_ref=assignment_ref,
            series_ref=series_ref,
            structure_ref=structure_ref,
            activity_refs=(event_ref,),
            memory_publication_state="not_published",
            blocked_operations=self._BLOCKED_OPERATIONS,
        )

    def _upsert_series(self, *, series_id: str, series_name: str, source_id: str, series_ref: str) -> None:
        existing = self._object_store.read("library_series", series_id)
        source_ids = []
        if isinstance(existing, Mapping):
            raw_source_ids = existing.get("source_ids")
            if isinstance(raw_source_ids, list):
                source_ids.extend(item for item in raw_source_ids if isinstance(item, str) and item)
        if source_id not in source_ids:
            source_ids.append(source_id)
        payload = {
            "schema_version": "1.0.0",
            "id": series_id,
            "name": series_name,
            "status": "active",
            "source_ids": sorted(source_ids),
            "created_at": _optional_str(existing.get("created_at")) if isinstance(existing, Mapping) else self._now,
            "updated_at": self._now,
            "ref": series_ref,
        }
        self._object_store.write("library_series", series_id, payload, expected_revision=None)

    def _write_event(
        self,
        *,
        source_id: str,
        series_id: str,
        series_name: str,
        assignment_ref: str,
        structure_ref: str | None,
    ) -> str:
        event_id = f"event-series-assigned-{source_id}"
        event_ref = f"crp://{self._namespace_id}/activity/{event_id}.json"
        self._object_store.write(
            "activity_events",
            event_id,
            {
                "schema_version": "1.0.0",
                "id": event_id,
                "type": "source_series_assigned",
                "source_id": source_id,
                "status": "confirmed",
                "contentRead": True,
                "memoryPublication": "not_started",
                "details": {
                    "series_id": series_id,
                    "series_name": series_name,
                    "assignment_ref": assignment_ref,
                    "structure_ref": structure_ref,
                },
                "created_at": self._now,
                "ref": event_ref,
            },
            expected_revision=None,
        )
        return event_ref


def serialize_source_series_assignment_result(result: SourceSeriesAssignmentResult) -> dict[str, object]:
    return {
        "status": result.status,
        "source_id": result.source_id,
        "series_id": result.series_id,
        "series_name": result.series_name,
        "assignment_id": result.assignment_id,
        "assignment_ref": result.assignment_ref,
        "series_ref": result.series_ref,
        "structure_ref": result.structure_ref,
        "activity_refs": list(result.activity_refs),
        "memory_publication_state": result.memory_publication_state,
        "blocked_operations": list(result.blocked_operations),
    }


def _series_id(series_name: str) -> str:
    digest = hashlib.sha256(series_name.lower().encode("utf-8")).hexdigest()[:12]
    return f"series-{digest}"


def _clean_series_name(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.strip().split())
    return cleaned or None


def _clean_reason(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.strip().split())
    return cleaned or None


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
