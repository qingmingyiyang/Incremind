from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .legacy_history import LegacyJobHistorySnapshot, legacy_job_readonly_payload
from .sqlite_store import SQLiteJobStore


class JobAuthorityConflict(ValueError):
    pass


@dataclass(slots=True)
class RoutedJobRepository:
    """Effect-backed Job Projection repository with recoverable legacy import."""

    legacy: object
    sqlite: SQLiteJobStore
    sqlite_job_types: frozenset[str]

    def get(self, job_id: str) -> Mapping[str, object] | None:
        sqlite_record = self.sqlite.read(job_id)
        if sqlite_record is not None:
            return dict(sqlite_record.payload)
        legacy = self._legacy_job_by_logical_id(job_id)
        if legacy is None:
            return None
        return legacy_job_readonly_payload(
            legacy, history_source="legacy-object-store-live-readonly",
        )

    def save(self, job: Mapping[str, object]) -> None:
        job_id = _required_job_id(job)
        sqlite_record = self.sqlite.read(job_id)
        if sqlite_record is not None:
            if sqlite_record.payload.get("execution_version") == "legacy-v1-readonly":
                raise JobAuthorityConflict(f"legacy Job is read-only: {job_id}")
            self.sqlite.save(job, expected_revision=sqlite_record.revision)
            return
        if self._legacy_job_by_logical_id(job_id) is not None:
            raise JobAuthorityConflict(f"legacy Job is read-only: {job_id}")
        self.sqlite.create(job)

    def is_sqlite_authority(self, job_id: str) -> bool:
        record = self.sqlite.read(job_id)
        return (
            record is not None
            and record.payload.get("execution_version") != "legacy-v1-readonly"
        )

    def request_cancel(self, job_id: str, *, request_id: str, now: str) -> Mapping[str, object]:
        sqlite_record = self.sqlite.read(job_id)
        if sqlite_record is not None:
            if sqlite_record.payload.get("execution_version") == "legacy-v1-readonly":
                raise JobAuthorityConflict(f"legacy Job is read-only: {job_id}")
            return dict(self.sqlite.request_cancel(job_id, request_id=request_id, now=now).payload)
        if self._legacy_job_by_logical_id(job_id) is not None:
            raise JobAuthorityConflict(f"legacy Job is read-only: {job_id}")
        raise JobAuthorityConflict(f"job was not found: {job_id}")

    def all(self) -> tuple[Mapping[str, object], ...]:
        sqlite_records = tuple(dict(record.payload) for record in self.sqlite.all())
        known = {str(record.get("id")) for record in sqlite_records}
        legacy_records = tuple(
            legacy_job_readonly_payload(
                normalized,
                history_source="legacy-object-store-live-readonly",
            )
            for normalized in (
                _normalized_legacy_job(job) for job in self.legacy.all()
            )
            if str(normalized["id"]) not in known
        )
        return sqlite_records + legacy_records

    def list_jobs(
        self,
        *,
        job_type: str | None = None,
        status: str | None = None,
        source_id: str | None = None,
        limit: int | None = None,
    ) -> tuple[Mapping[str, object], ...]:
        filtered = [
            job
            for job in self.all()
            if (job_type is None or job.get("job_type") == job_type)
            and (status is None or job.get("status") == status)
            and (source_id is None or job.get("source_id") == source_id)
        ]
        return tuple(filtered[:limit] if limit is not None and limit >= 0 else filtered)

    def list_effect_jobs_page(
        self,
        *,
        effect_kind: str,
        contract_version: str,
        project_id: str,
        after: tuple[str, str] | None = None,
        limit: int,
    ) -> tuple[Mapping[str, object], ...]:
        """Expose bounded current Effect candidates without legacy fallback."""
        return self.sqlite.list_effect_jobs_page(
            effect_kind=effect_kind,
            contract_version=contract_version,
            project_id=project_id,
            after=after,
            limit=limit,
        )

    def import_legacy_history(
        self, *, migration_id: str, imported_at: str,
    ) -> tuple[str, ...]:
        """Explicitly freeze ObjectStore Jobs without deleting or executing them."""

        listed = getattr(self.legacy, "all_with_storage_ids", None)
        entries = (
            tuple(listed())
            if callable(listed)
            else tuple((_legacy_storage_id(job), job) for job in self.legacy.all())
        )
        snapshots: list[LegacyJobHistorySnapshot] = []
        for storage_id, job in entries:
            normalized = _normalized_legacy_job(job)
            job_id = str(normalized["id"])
            existing = self.sqlite.read(job_id)
            if existing is not None and existing.payload.get("execution_version") != "legacy-v1-readonly":
                raise JobAuthorityConflict(
                    f"legacy Job conflicts with executable Effect projection: {job_id}"
                )
            snapshots.append(LegacyJobHistorySnapshot(
                source_ref=str(storage_id),
                job_id=job_id,
                payload=normalized,
                legacy_revision=1,
            ))
        try:
            imported = self.sqlite.import_legacy_history(
                tuple(snapshots),
                migration_id=migration_id,
                source_kind="object-store-job-v1",
                imported_at=imported_at,
            )
        except ValueError as exc:
            raise JobAuthorityConflict(str(exc)) from exc
        return tuple(str(record.payload["id"]) for record in imported)

    def rollback_to_legacy(self) -> tuple[str, ...]:
        """Refuse to restore legacy execution authority during rollback."""

        raise JobAuthorityConflict(
            "rollback may switch read models but cannot restore legacy Job execution authority"
        )

    def _legacy_job_by_logical_id(self, job_id: str) -> dict[str, object] | None:
        match: dict[str, object] | None = None
        for job in self.legacy.all():
            normalized = _normalized_legacy_job(job)
            if normalized["id"] != job_id:
                continue
            if match is not None:
                raise JobAuthorityConflict(f"legacy job identity is duplicated: {job_id}")
            match = normalized
        return match


def _required_job_id(job: Mapping[str, object]) -> str:
    value = job.get("id")
    if not isinstance(value, str) or not value:
        raise ValueError("job requires id")
    return value


def _legacy_storage_id(job: Mapping[str, object]) -> str:
    value = job.get("id")
    if not isinstance(value, str) or not value:
        value = job.get("job_id")
    if not isinstance(value, str) or not value:
        raise JobAuthorityConflict("legacy Job storage identity is invalid")
    return value


def _normalized_legacy_job(job: Mapping[str, object]) -> dict[str, object]:
    """Expose pre-canonical JSON jobs through the current read contract."""

    value = job.get("id")
    if not isinstance(value, str) or not value:
        value = job.get("job_id")
    if not isinstance(value, str) or not value:
        raise JobAuthorityConflict("legacy job identity is invalid")
    normalized = dict(job)
    normalized["id"] = value
    return normalized
