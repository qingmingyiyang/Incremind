"""Effect-first coordination commands for Job query projections."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from core.effect_log import Effect, EffectLog

from .job_projection import JobProjectionBuilder, JobProjectionRecord
from .legacy_history import LegacyJobHistoryProjection


@dataclass(frozen=True, slots=True)
class JobCancellationResult:
    effect: Effect
    record: JobProjectionRecord


class SQLiteJobEffectCommandAuthority:
    """Write Core coordination facts without restoring Job execution authority."""

    def __init__(self, database_path: str | Path) -> None:
        self._path = Path(database_path).expanduser().resolve(strict=False)
        self._effects = EffectLog(self._path)
        self._projection = JobProjectionBuilder()
        self._history = LegacyJobHistoryProjection()

    def request_cancellation(
        self,
        *,
        job_id: str,
        request_ref: str,
        requested_at: int,
    ) -> JobCancellationResult:
        if not isinstance(job_id, str) or not job_id.strip():
            raise ValueError("job_id must be non-empty")
        connection = sqlite3.connect(self._path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            connection.execute("BEGIN IMMEDIATE")
            if self._history.validate_job_in_connection(connection, job_id=job_id):
                raise ValueError("legacy Job history is read-only")
            row = connection.execute(
                "SELECT n.effect_operation_id FROM job_effect_node n "
                "WHERE n.job_id=? AND n.node_kind='attempt' "
                "ORDER BY n.attempt DESC LIMIT 1",
                (job_id,),
            ).fetchone()
            if row is None:
                raise KeyError(job_id)
            effect = self._effects.request_cancellation_in_connection(
                connection,
                str(row["effect_operation_id"]),
                request_ref=request_ref,
                now=requested_at,
            )
            record = self._projection.derive_in_connection(connection, job_id=job_id)
            connection.commit()
            return JobCancellationResult(effect=effect, record=record)
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()
