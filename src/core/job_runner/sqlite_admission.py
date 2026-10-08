"""SQLite command boundary for atomically admitting v2 Job execution.

The legacy Job store remains a compatibility reader during migration.  New
execution admission is deliberately composed here instead of calling a Store
``save`` method: a caller supplies durable Gate evidence, this command owns one
SQLite transaction, and the projection is created only after Core has accepted
the Effect.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from pathlib import Path

from core.effect_log import EffectIntent, EffectLog

from .admission import JobAdmissionAuthorization
from .execution_admission import JobExecutionAdmission, JobExecutionAdmissionAuthority
from .job_projection import JobProjectionBuilder, initialize_job_projection_schema


JobAdmissionPreflight = Callable[[sqlite3.Connection, bool], None]


class SQLiteJobAdmissionCommand:
    """Own the atomic Gate → Effect → Job projection admission transaction."""

    def __init__(self, database_path: str | Path) -> None:
        self._path = Path(database_path).expanduser().resolve(strict=False)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._effects = EffectLog(self._path)
        self._authority = JobExecutionAdmissionAuthority(
            self._effects, JobProjectionBuilder(),
        )

    @property
    def database_path(self) -> Path:
        return self._path

    def admit(
        self,
        *,
        payload: Mapping[str, object],
        authorization: JobAdmissionAuthorization,
        intent: EffectIntent,
        preflight: JobAdmissionPreflight | None = None,
    ) -> JobExecutionAdmission:
        """Admit one execution or verify an exact replay.

        ``preflight`` runs under the same ``BEGIN IMMEDIATE`` transaction.  Its
        boolean argument reports whether immutable Job facts already exist, so
        domain capacity checks can skip exact replays while revision fences are
        still revalidated.  It may only validate domain facts; it cannot create
        a Gate decision or command Effect lifecycle.
        """

        if preflight is not None and not callable(preflight):
            raise TypeError("Job admission preflight must be callable")
        connection = sqlite3.connect(self._path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA foreign_keys=ON")
        initialize_job_projection_schema(connection)
        try:
            connection.execute("BEGIN IMMEDIATE")
            replay = connection.execute(
                "SELECT 1 FROM job_effect_fact "
                "WHERE job_id=? AND effect_operation_id=? LIMIT 1",
                (authorization.job_id, intent.operation_id),
            ).fetchone() is not None
            if preflight is not None:
                preflight(connection, replay)
            admitted = self._authority.admit_in_connection(
                connection,
                payload=payload,
                authorization=authorization,
                intent=intent,
            )
            if replay == admitted.created:
                raise RuntimeError("Job admission replay classification drifted")
            connection.commit()
            return admitted
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()
