from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

from core.effect_log import (
    EffectRunner,
    EffectState,
    shared_effect_runner,
)
from core.task_reference_contract import (
    task_updated_utc_key,
    workbench_transform_job_id_from_reference,
    workbench_transform_task_reference,
    workbench_transform_task_tie_key,
)

from .media_execution_evidence import (
    MediaOperationExecutionEvidence,
    media_operation_execution_evidence_from_payload,
    media_operation_execution_evidence_to_payload,
    validate_media_operation_execution_evidence,
)
from .media_execution_receipt import (
    MediaOperationExecutionReceipt,
    media_operation_execution_receipt_from_payload,
)
from .media_recipe_step_evidence import (
    MediaRecipeStepEvidence,
    media_recipe_step_evidence_from_payload,
    media_recipe_step_evidence_to_payload,
)
from .media_recipe_step_receipt import (
    MediaRecipeStepReceipt,
    media_recipe_step_receipt_from_payload,
    media_recipe_step_receipt_to_payload,
)
from .job_projection import (
    JobEffectProjectionAuthority,
    JobProjectionBuilder,
    initialize_job_projection_schema,
    job_attempt_operation_id,
    job_fact_payload,
    job_root_operation_id,
    rebuild_task_candidate_projection_in_connection,
    task_candidate_projection_is_ready_in_connection,
)
from .legacy_history import (
    LegacyJobHistoryProjection,
    LegacyJobHistorySnapshot,
    initialize_legacy_job_history_schema,
    legacy_job_history_display_payload,
)



class SQLiteJobLeaseConflict(ValueError):
    pass


class SQLiteMediaAdmissionConflict(ValueError):
    """A media-hands identity or lane admission cannot be accepted."""

    pass


class SQLiteMediaConcurrencyConflict(SQLiteMediaAdmissionConflict):
    """A valid Media Hands Job must wait for execution lane capacity."""

    pass


class SQLiteLegacyMediaExecutionBlocked(RuntimeError):
    """A legacy Media Job tried to enter the retired execution authority."""


def reject_legacy_media_execution() -> NoReturn:
    raise SQLiteLegacyMediaExecutionBlocked(
        "legacy Media Job execution is read-only; use media_hands_job_execution/effect-v2"
    )


@dataclass(frozen=True, slots=True)
class SQLiteJobRecord:
    payload: Mapping[str, object]
    revision: int


class SQLiteJobStore:
    """Explicit-path, not-yet-composed Job store with token fencing."""

    def __init__(self, database_path: Path, *, effect_runner: EffectRunner | None = None) -> None:
        self._path = database_path.expanduser().resolve(strict=False)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._effect_runner = effect_runner or shared_effect_runner(
            self._path, owner_role="job-effect-runner",
        )
        self._job_effect_authority = JobEffectProjectionAuthority()
        self._projection_builder = JobProjectionBuilder()
        self._legacy_history = LegacyJobHistoryProjection()

    @property
    def database_path(self) -> Path:
        """Expose the canonical authority identity without exposing connections."""

        return self._path

    def create(self, job: Mapping[str, object]) -> SQLiteJobRecord:
        return self.save(job, expected_revision=0)

    def create_media_admitted(
        self,
        job: Mapping[str, object],
        *,
        policy_admission_fence: Callable[[sqlite3.Connection, str], None] | None = None,
    ) -> tuple[SQLiteJobRecord, bool]:
        """Reject new legacy Media Jobs after the Effect-v2 cutover.

        The signature remains during the frozen-history compatibility window
        only so old callers fail before any Job, Effect, evidence or lease write.
        """
        reject_legacy_media_execution()

    def acquire_media_execution(self, job_id: str, *, worker_id: str, lease_token: str, now: str, expires_at: str) -> SQLiteJobRecord:
        reject_legacy_media_execution()

    def consume_media_budget(self, job_id: str, *, lease_token: str, now: str, consumed: Mapping[str, int]) -> SQLiteJobRecord:
        reject_legacy_media_execution()

    def complete_media_operation(
        self,
        job_id: str,
        *,
        lease_token: str,
        now: str,
        step_name: str,
        published_outputs: tuple[Mapping[str, object], ...],
        consumed: Mapping[str, int],
    ) -> SQLiteJobRecord:
        """Atomically publish the final media receipt and settle its execution lease."""
        reject_legacy_media_execution()

    def finalize_media_execution_with_receipt(
        self,
        job_id: str,
        *,
        lease_token: str,
        now: str,
        execution_id: str,
        receipt_ref: str,
        published_outputs: tuple[Mapping[str, object], ...],
        checkpoint: Mapping[str, object],
        consumed: Mapping[str, int],
        log_refs: tuple[str, ...],
    ) -> dict[str, object]:
        """Persist the verified receipt and completed evidence atomically.

        The receipt is a recovery projection only. It never owns the output
        objects referenced by a Provider and it deliberately contains no
        secret, media body, URL, or local path.
        """
        reject_legacy_media_execution()

    def get_media_execution_receipt(
        self, job_id: str, *, execution_id: str
    ) -> dict[str, object] | None:
        """Load one receipt only when it remains bound to the execution identity."""
        if not self._path.exists():
            return None
        connection = self._connect()
        try:
            job = self._require_locked(connection, job_id)
            row = connection.execute(
                "SELECT payload_json FROM media_execution_receipts WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None:
                return None
            receipt = _media_execution_receipt_record(
                row["payload_json"], budget=_media_payload(job.payload)["budget"]
            )
            if receipt.execution_id != _media_execution_execution_id(execution_id):
                raise ValueError("media execution receipt execution identity conflicts")
            evidence_row = connection.execute(
                "SELECT payload_json FROM media_execution_evidence WHERE job_id = ?", (job_id,)
            ).fetchone()
            if evidence_row is None:
                raise ValueError("media execution receipt has no execution evidence")
            evidence = _project_media_execution_evidence(
                connection, _media_execution_evidence_record(evidence_row["payload_json"]),
            )
            expected_evidence = _media_execution_evidence_for_job(
                job.payload,
                execution_id=evidence.execution_id,
                provider_id=evidence.provider_id,
                provider_revision=evidence.provider_revision,
            )
            if (
                evidence.state != "completed"
                or evidence.receipt_ref != receipt.receipt_ref
                or _media_execution_identity(evidence)
                != _media_execution_identity(expected_evidence)
                or not _media_execution_receipt_matches_evidence(receipt, evidence)
                or _canonical_payload(receipt.permission_snapshot)
                != _canonical_payload(_media_payload(job.payload)["permission_snapshot"])
            ):
                raise ValueError("media execution receipt is not bound to completed evidence")
            return _media_execution_receipt_public(receipt)
        finally:
            connection.close()

    def assert_media_execution_active(
        self, job_id: str, *, lease_token: str, now: str
    ) -> None:
        """Read-only local fencing used by cooperative Provider checkpoints."""

        reject_legacy_media_execution()

    def reserve_media_execution_evidence(
        self,
        job_id: str,
        *,
        lease_token: str,
        now: str,
        execution_id: str,
        provider_id: str,
        provider_revision: str,
    ) -> tuple[dict[str, object], bool]:
        """Reserve the single external effect for a leased media Job.

        ``created`` is true exactly once. Callers may invoke a provider only
        after that outcome; a replay returns the durable evidence instead.
        """
        reject_legacy_media_execution()

    def get_media_execution_evidence(self, job_id: str) -> dict[str, object] | None:
        if not self._path.exists():
            return None
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT payload_json FROM media_execution_evidence WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None:
                return None
            evidence = _project_media_execution_evidence(
                connection, _media_execution_evidence_record(row["payload_json"]),
            )
            return _media_execution_evidence_public(evidence)
        finally:
            connection.close()

    def reserve_media_recipe_step(
        self,
        job_id: str,
        *,
        lease_token: str,
        now: str,
        execution_id: str,
        provider_id: str,
        provider_revision: str,
        step_name: str,
        input_state_hash: str,
    ) -> tuple[dict[str, object], bool]:
        """Atomically reserve one internal recipe step under the existing effect.

        This is not a second Job authority.  The parent Media execution must
        already be reserved and the same local Job lease must still be active.
        """

        reject_legacy_media_execution()

    def get_media_recipe_step(
        self, job_id: str, *, execution_id: str, step_name: str
    ) -> dict[str, object] | None:
        if not self._path.exists():
            return None
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT payload_json FROM media_recipe_step_evidence WHERE job_id = ? AND step_name = ?",
                (job_id, step_name),
            ).fetchone()
            if row is None:
                return None
            evidence = media_recipe_step_evidence_from_payload(json.loads(row["payload_json"]))
            if evidence.execution_id != _media_execution_execution_id(execution_id):
                raise ValueError("media recipe step execution identity conflicts")
            _require_parent_media_execution(connection, evidence)
            evidence = _project_media_recipe_step_evidence(connection, evidence)
            return media_recipe_step_evidence_to_payload(evidence)
        finally:
            connection.close()

    def mark_media_recipe_step_unknown(
        self, job_id: str, *, lease_token: str, now: str, execution_id: str, step_name: str
    ) -> dict[str, object]:
        reject_legacy_media_execution()

    def complete_media_recipe_step(
        self,
        job_id: str,
        *,
        lease_token: str,
        now: str,
        execution_id: str,
        step_name: str,
        receipt: MediaRecipeStepReceipt,
    ) -> dict[str, object]:
        reject_legacy_media_execution()

    def get_media_recipe_step_receipt(
        self, job_id: str, *, execution_id: str, step_name: str
    ) -> dict[str, object] | None:
        if not self._path.exists():
            return None
        connection = self._connect()
        try:
            evidence_row = connection.execute(
                "SELECT payload_json FROM media_recipe_step_evidence WHERE job_id = ? AND step_name = ?",
                (job_id, step_name),
            ).fetchone()
            receipt_row = connection.execute(
                "SELECT payload_json FROM media_recipe_step_receipts WHERE job_id = ? AND step_name = ?",
                (job_id, step_name),
            ).fetchone()
            if evidence_row is None or receipt_row is None:
                return None
            evidence = media_recipe_step_evidence_from_payload(json.loads(evidence_row["payload_json"]))
            evidence = _project_media_recipe_step_evidence(connection, evidence)
            receipt = media_recipe_step_receipt_from_payload(json.loads(receipt_row["payload_json"]))
            if (
                evidence.state != "completed"
                or evidence.execution_id != _media_execution_execution_id(execution_id)
                or evidence.receipt_ref != receipt.receipt_ref
                or _media_recipe_step_receipt_identity(receipt) != _media_recipe_step_identity(evidence)
            ):
                raise ValueError("media recipe step receipt is not bound to completed evidence")
            _require_parent_media_execution(connection, evidence)
            return media_recipe_step_receipt_to_payload(receipt)
        finally:
            connection.close()

    def media_recipe_resume_ready(self, job_id: str, *, execution_id: str) -> bool:
        """Return whether a crashed parent may safely re-enter its durable recipe."""

        if not self._path.exists():
            return False
        connection = self._connect()
        try:
            parent_row = connection.execute(
                "SELECT payload_json FROM media_execution_evidence WHERE job_id = ?", (job_id,)
            ).fetchone()
            if parent_row is None:
                return False
            parent = _media_execution_evidence_record(parent_row["payload_json"])
            parent = _project_media_execution_evidence(connection, parent)
            if (
                parent.execution_id != _media_execution_execution_id(execution_id)
                or parent.state != "started"
            ):
                return False
            rows = connection.execute(
                "SELECT step_name, payload_json FROM media_recipe_step_evidence WHERE job_id = ? ORDER BY step_name",
                (job_id,),
            ).fetchall()
            if not rows:
                return False
            for row in rows:
                evidence = media_recipe_step_evidence_from_payload(json.loads(row["payload_json"]))
                evidence = _project_media_recipe_step_evidence(connection, evidence)
                if evidence.state != "completed" or evidence.execution_id != parent.execution_id:
                    return False
                receipt_row = connection.execute(
                    "SELECT payload_json FROM media_recipe_step_receipts WHERE job_id = ? AND step_name = ?",
                    (job_id, evidence.step_name),
                ).fetchone()
                if receipt_row is None:
                    return False
                receipt = media_recipe_step_receipt_from_payload(json.loads(receipt_row["payload_json"]))
                if (
                    evidence.receipt_ref != receipt.receipt_ref
                    or _media_recipe_step_receipt_identity(receipt)
                    != _media_recipe_step_identity(evidence)
                ):
                    return False
            return True
        finally:
            connection.close()

    def mark_media_execution_unknown(
        self, job_id: str, *, lease_token: str, now: str, execution_id: str
    ) -> dict[str, object]:
        reject_legacy_media_execution()

    def finalize_media_execution_evidence(
        self, job_id: str, *, lease_token: str, now: str, execution_id: str, receipt_ref: str
    ) -> dict[str, object]:
        reject_legacy_media_execution()

    def bind(self, connection: sqlite3.Connection) -> SQLiteJobStoreTransaction:
        """Enlist Job persistence in an already-open SQLite transaction.

        The caller owns begin/commit/rollback. This seam exists for aggregate
        UoWs and never starts a second transaction or commits the connection.
        """

        if not isinstance(connection, sqlite3.Connection):
            raise TypeError("connection must be a SQLite connection")
        _initialize_job_schema(connection)
        return SQLiteJobStoreTransaction(self, connection)

    def read(self, job_id: str) -> SQLiteJobRecord | None:
        if not self._path.exists():
            return None
        connection = self._connect_readonly()
        try:
            self._legacy_history.validate_job_in_connection(connection, job_id=job_id)
            if _table_exists(connection, "job_effect_fact"):
                try:
                    record = self._projection_builder.derive_in_connection(
                        connection, job_id=job_id,
                    )
                except KeyError:
                    pass
                else:
                    return SQLiteJobRecord(record.payload, record.revision)
            projection_only = _read_projection_only_in_connection(connection, job_id)
            if projection_only is not None:
                return projection_only
            try:
                history = self._legacy_history.read_in_connection(
                    connection, job_id=job_id,
                )
            except KeyError:
                return None
            return SQLiteJobRecord(
                legacy_job_history_display_payload(history),
                history.legacy_revision,
            )
        finally:
            connection.close()

    def all(self) -> tuple[SQLiteJobRecord, ...]:
        if not self._path.exists():
            return ()
        connection = self._connect_readonly()
        try:
            self._legacy_history.validate_inventory_in_connection(connection)
            projected = tuple(
                SQLiteJobRecord(record.payload, record.revision)
                for record in (
                    self._projection_builder.derive_all_in_connection(connection)
                    if _table_exists(connection, "job_effect_fact")
                    else ()
                )
            )
            projected_ids = {str(record.payload.get("id")) for record in projected}
            projection_only = _all_projection_only_in_connection(connection)
            projected_ids.update(str(record.payload.get("id")) for record in projection_only)
            history = tuple(
                SQLiteJobRecord(
                    legacy_job_history_display_payload(record),
                    record.legacy_revision,
                )
                for record in self._legacy_history.all_in_connection(connection)
                if record.job_id not in projected_ids
            )
            return projected + projection_only + history
        finally:
            connection.close()

    def list_effect_jobs_page(
        self,
        *,
        effect_kind: str,
        contract_version: str,
        project_id: str,
        after: tuple[str, str] | None = None,
        limit: int,
    ) -> tuple[Mapping[str, object], ...]:
        """Read one sorted Effect-v2 Job candidate page without ``all()``.

        The SQL query only inspects immutable fact metadata.  Each returned
        candidate is still derived through the existing single-Job projection,
        so the method never caches lifecycle or delivery conclusions.
        """
        if not effect_kind or not contract_version or not isinstance(project_id, str) or not project_id:
            raise ValueError("effect Job page arguments are invalid")
        if limit < 1 or limit > 256:
            raise ValueError("effect Job page limit is invalid")
        if after is not None and (len(after) != 2 or not all(isinstance(value, str) for value in after)):
            raise ValueError("effect Job page cursor is invalid")
        if not self._path.exists():
            return ()
        connection = self._connect_readonly()
        try:
            if not _table_exists(connection, "job_effect_fact") or not _table_exists(connection, "effect"):
                return ()
            indexed = self._list_indexed_effect_jobs_page(
                connection,
                effect_kind=effect_kind,
                contract_version=contract_version,
                project_id=project_id,
                after=after,
                limit=limit,
            )
            if indexed is not None:
                return indexed
            connection.create_function("task_updated_sort_key", 1, task_updated_utc_key)
            connection.create_function(
                "task_reference_for_job", 1,
                lambda job_id: workbench_transform_task_reference(project_id=project_id, job_id=str(job_id)),
            )
            where = ""
            parameters: list[object] = [effect_kind, contract_version]
            if after is not None:
                where = (
                    "AND (task_updated_sort_key(json_extract(f.payload_json,'$.updated_at')) < ? "
                    "OR (task_updated_sort_key(json_extract(f.payload_json,'$.updated_at')) = ? "
                    "AND task_reference_for_job(f.job_id) < ?))"
                )
                parameters.extend((after[0], after[0], after[1]))
            parameters.append(limit)
            rows = connection.execute(
                f"""
                SELECT f.job_id
                  FROM job_effect_fact AS f
                  JOIN effect AS e ON e.operation_id=f.effect_operation_id
                 WHERE f.sequence=(SELECT MAX(current.sequence) FROM job_effect_fact AS current
                                    WHERE current.job_id=f.job_id)
                   AND e.kind=? AND e.contract_version=?
                   {where}
                 ORDER BY task_updated_sort_key(json_extract(f.payload_json,'$.updated_at')) DESC,
                          task_reference_for_job(f.job_id) DESC
                 LIMIT ?
                """,
                tuple(parameters),
            ).fetchall()
            return tuple(
                dict(self._projection_builder.derive_in_connection(connection, job_id=str(row["job_id"])).payload)
                for row in rows
            )
        finally:
            connection.close()

    def _list_indexed_effect_jobs_page(
        self,
        connection: sqlite3.Connection,
        *,
        effect_kind: str,
        contract_version: str,
        project_id: str,
        after: tuple[str, str] | None,
        limit: int,
    ) -> tuple[Mapping[str, object], ...] | None:
        """Use the disposable index only when its API-reference contract fits."""
        if (
            effect_kind != "workbench_content_transform"
            or contract_version != "effect-v2"
            or not _table_exists(connection, "job_task_candidate_projection")
            or not _table_exists(connection, "job_task_candidate_projection_meta")
            or not task_candidate_projection_is_ready_in_connection(connection)
        ):
            return None
        where = ""
        parameters: list[object] = [effect_kind, contract_version]
        if after is not None:
            cursor_job_id = workbench_transform_job_id_from_reference(after[1])
            if cursor_job_id is None:
                return None
            if after[1] != workbench_transform_task_reference(
                project_id=project_id, job_id=cursor_job_id,
            ):
                return None
            where = "AND (updated_at_key,tie_key) < (?,?)"
            parameters.extend((after[0], workbench_transform_task_tie_key(cursor_job_id)))
        parameters.append(limit)
        rows = connection.execute(
            f"""
            SELECT job_id FROM job_task_candidate_projection
             WHERE effect_kind=? AND contract_version=? {where}
             ORDER BY updated_at_key DESC,tie_key DESC
             LIMIT ?
            """,
            tuple(parameters),
        ).fetchall()
        return tuple(
            dict(self._projection_builder.derive_in_connection(connection, job_id=str(row["job_id"])).payload)
            for row in rows
        )

    def import_legacy_history(
        self,
        snapshots: tuple[LegacyJobHistorySnapshot, ...],
        *,
        migration_id: str,
        source_kind: str,
        imported_at: str,
    ) -> tuple[SQLiteJobRecord, ...]:
        """Atomically freeze legacy rows without creating executable Effects."""

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._legacy_history.validate_inventory_in_connection(connection)
            imported = self._import_legacy_history_in_connection(
                connection,
                snapshots,
                migration_id=migration_id,
                source_kind=source_kind,
                imported_at=imported_at,
            )
            connection.execute("COMMIT")
            return imported
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def import_legacy_job_store_history(
        self, *, migration_id: str, imported_at: str,
    ) -> tuple[SQLiteJobRecord, ...]:
        """Freeze retired v1 rows without recreating their storage authority.

        ``job_store`` is no longer created or written by current code.  An
        existing table is an immutable compatibility input: exact current
        projection-only duplicates and the previously adopted legacy Effect
        shape remain in place but are not imported into a second authority.
        Other rows are frozen as read-only history, subject to the existing
        fail-closed collision fence.
        """

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._legacy_history.validate_inventory_in_connection(connection)
            if not _table_exists(connection, "job_store"):
                connection.execute("COMMIT")
                return ()
            rows = connection.execute(
                "SELECT job_id,payload_json,revision FROM job_store ORDER BY job_id"
            ).fetchall()
            snapshots: list[LegacyJobHistorySnapshot] = []
            for row in rows:
                payload = json.loads(str(row["payload_json"]))
                if not isinstance(payload, dict):
                    raise ValueError("legacy Job projection payload is invalid")
                job_id = str(row["job_id"])
                revision = int(row["revision"])
                if _is_retired_compatibility_projection(
                    connection,
                    job_id=job_id,
                    payload=payload,
                    revision=revision,
                ) or _is_adopted_legacy_effect_projection(
                    connection,
                    job_id=job_id,
                    payload=payload,
                    revision=revision,
                ):
                    continue
                snapshots.append(LegacyJobHistorySnapshot(
                    source_ref=job_id,
                    job_id=job_id,
                    payload=payload,
                    legacy_revision=revision,
                ))
            imported = self._import_legacy_history_in_connection(
                connection,
                tuple(snapshots),
                migration_id=migration_id,
                source_kind="sqlite-job-store-v1",
                imported_at=imported_at,
            )
            connection.execute("COMMIT")
            return imported
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def _import_legacy_history_in_connection(
        self,
        connection: sqlite3.Connection,
        snapshots: tuple[LegacyJobHistorySnapshot, ...],
        *,
        migration_id: str,
        source_kind: str,
        imported_at: str,
    ) -> tuple[SQLiteJobRecord, ...]:
        imported: list[SQLiteJobRecord] = []
        seen_jobs: set[str] = set()
        seen_sources: set[str] = set()
        for snapshot in snapshots:
            if snapshot.job_id in seen_jobs or snapshot.source_ref in seen_sources:
                raise ValueError("legacy Job history import inventory is duplicated")
            seen_jobs.add(snapshot.job_id)
            seen_sources.add(snapshot.source_ref)
            record = self._legacy_history.import_in_connection(
                connection,
                migration_id=migration_id,
                source_kind=source_kind,
                source_ref=snapshot.source_ref,
                job_id=snapshot.job_id,
                payload=snapshot.payload,
                revision=snapshot.legacy_revision,
                imported_at=imported_at,
            )
            imported.append(SQLiteJobRecord(
                legacy_job_history_display_payload(record),
                record.legacy_revision,
            ))
        return tuple(imported)

    def rebuild_projection(self, job_id: str, *, rebuilt_at: str) -> SQLiteJobRecord:
        """Reconstruct one Effect cache without altering legacy history inputs."""

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._legacy_history.validate_job_in_connection(connection, job_id=job_id)
            projection_only = _read_projection_only_in_connection(connection, job_id)
            if projection_only is not None:
                connection.execute("COMMIT")
                return projection_only
            connection.execute("DELETE FROM job_projection WHERE job_id=?", (job_id,))
            connection.execute("DELETE FROM job_task_candidate_projection WHERE job_id=?", (job_id,))
            try:
                projected = self._projection_builder.rebuild_in_connection(
                    connection,
                    job_id=job_id,
                    rebuilt_at=rebuilt_at,
                )
            except KeyError:
                history = self._legacy_history.read_in_connection(
                    connection, job_id=job_id,
                )
                connection.execute("COMMIT")
                return SQLiteJobRecord(
                    legacy_job_history_display_payload(history),
                    history.legacy_revision,
                )
            connection.execute("COMMIT")
            return SQLiteJobRecord(projected.payload, projected.revision)
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def rebuild_all_projections(self, *, rebuilt_at: str) -> tuple[SQLiteJobRecord, ...]:
        """Rebuild every Job cache from Effect nodes and immutable Job facts."""

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._legacy_history.validate_inventory_in_connection(connection)
            connection.execute(
                "DELETE FROM job_projection WHERE job_id IN "
                "(SELECT DISTINCT job_id FROM job_effect_fact)"
            )
            connection.execute("DELETE FROM job_task_candidate_projection")
            projected = self._projection_builder.rebuild_all_in_connection(
                connection,
                rebuilt_at=rebuilt_at,
            )
            connection.execute(
                "INSERT INTO job_task_candidate_projection_meta(key,value) VALUES('coverage','ready-v1') "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
            )
            projected_ids = {str(record.payload["id"]) for record in projected}
            projection_only = _all_projection_only_in_connection(connection)
            projected_ids.update(str(record.payload.get("id")) for record in projection_only)
            history = tuple(
                record
                for record in self._legacy_history.all_in_connection(connection)
                if record.job_id not in projected_ids
            )
            connection.execute("COMMIT")
            return (
                tuple(SQLiteJobRecord(record.payload, record.revision) for record in projected)
                + projection_only
                + tuple(
                    SQLiteJobRecord(
                        legacy_job_history_display_payload(record),
                        record.legacy_revision,
                    )
                    for record in history
                )
            )
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def rebuild_task_candidate_projection(self) -> None:
        """Explicitly backfill the disposable candidate index in one transaction."""
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._legacy_history.validate_inventory_in_connection(connection)
            rebuild_task_candidate_projection_in_connection(connection)
            connection.execute("COMMIT")
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def save(self, job: Mapping[str, object], *, expected_revision: int) -> SQLiteJobRecord:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            saved = self._save_locked(connection, job, expected_revision=expected_revision)
            connection.execute("COMMIT")
            return saved
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def request_cancel(self, job_id: str, *, request_id: str, now: str) -> SQLiteJobRecord:
        if not request_id:
            raise SQLiteJobLeaseConflict("cancel request_id is required")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            current = self._require_locked(connection, job_id)
            payload = dict(current.payload)
            status = payload.get("status")
            existing = payload.get("cancel_request")
            if status == "cancelled" and isinstance(existing, Mapping) and existing.get("request_id") == request_id:
                connection.execute("COMMIT")
                return current
            if status in {"completed", "failed", "cancelled"}:
                raise SQLiteJobLeaseConflict(f"job status {status!r} cannot be cancelled")
            if isinstance(existing, Mapping):
                if existing.get("request_id") != request_id:
                    raise SQLiteJobLeaseConflict("job already has a different cancel request")
                connection.execute("COMMIT")
                return current
            payload["cancel_request"] = {
                "request_id": request_id,
                "status": "requested",
                "requested_at": now,
                "applied_at": None,
            }
            lease = payload.get("lease")
            active_lease = isinstance(lease, Mapping) and _iso_after(lease.get("expires_at"), now)
            if status != "running" or not active_lease:
                payload["status"] = "cancelled"
                payload["lease"] = None
                payload["cancel_request"] = {**payload["cancel_request"], "status": "applied", "applied_at": now}
                payload["events"] = list(payload.get("events", [])) + [{"type": "cancelled", "at": now}]
            payload["updated_at"] = now
            saved = self._save_locked(connection, payload, expected_revision=current.revision)
            connection.execute("COMMIT")
            return saved
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def retry(self, job_id: str, *, now: str) -> SQLiteJobRecord:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            current = self._require_locked(connection, job_id)
            payload = dict(current.payload)
            if payload.get("status") not in {"failed", "waiting_user", "cancelled"} or payload.get("lease") is not None:
                raise SQLiteJobLeaseConflict("job is not retryable without an active lease")
            for step in payload.get("steps", []):
                if step.get("status") in {"failed", "waiting_user", "cancelled"}:
                    step["status"] = "pending"
            payload["status"] = "pending"
            payload.pop("cancel_request", None)
            payload["attempt"] = int(payload.get("attempt", 0)) + 1
            payload["updated_at"] = now
            saved = self._save_locked(connection, payload, expected_revision=current.revision)
            connection.execute("COMMIT")
            return saved
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def _require(self, job_id: str) -> SQLiteJobRecord:
        if not self._path.exists():
            raise SQLiteJobLeaseConflict("job was not found")
        connection = self._connect_readonly()
        try:
            self._assert_writable_job_identity_in_connection(connection, job_id)
            try:
                record = self._projection_builder.derive_in_connection(
                    connection, job_id=job_id,
                )
            except (KeyError, sqlite3.OperationalError):
                record = _read_projection_only_in_connection(connection, job_id)
                if record is None:
                    raise SQLiteJobLeaseConflict("job was not found")
            return SQLiteJobRecord(record.payload, record.revision)
        finally:
            connection.close()

    def _save_locked(self, connection: sqlite3.Connection, job: Mapping[str, object], *, expected_revision: int) -> SQLiteJobRecord:
        job_id = _job_id(job)
        self._reject_legacy_media_job_write_in_connection(
            connection, job_id=job_id, candidate=job,
        )
        self._assert_writable_job_identity_in_connection(connection, job_id)
        job = _normalize_projection_only_job(job)
        row = connection.execute("SELECT revision FROM job_projection WHERE job_id = ?", (job_id,)).fetchone()
        actual = int(row[0]) if row else 0
        if actual != expected_revision:
            raise SQLiteJobLeaseConflict(f"stale job revision: expected {expected_revision}, found {actual}")
        projected = self._job_effect_authority.record_snapshot_in_connection(
            connection,
            job,
            minimum_sequence=actual + 1,
        )
        return SQLiteJobRecord(projected.payload, projected.revision)

    @staticmethod
    def _reject_legacy_media_job_write_in_connection(
        connection: sqlite3.Connection,
        *,
        job_id: str,
        candidate: Mapping[str, object],
    ) -> None:
        if candidate.get("job_type") == "media_hands":
            reject_legacy_media_execution()
        rows = (
            connection.execute(
                "SELECT payload_json FROM job_projection WHERE job_id=?",
                (job_id,),
            ).fetchone(),
            connection.execute(
                "SELECT payload_json FROM job_effect_fact WHERE job_id=? "
                "ORDER BY sequence DESC LIMIT 1",
                (job_id,),
            ).fetchone(),
        )
        for row in rows:
            if row is None:
                continue
            payload = json.loads(str(row[0]))
            if isinstance(payload, Mapping) and payload.get("job_type") == "media_hands":
                reject_legacy_media_execution()

    def _assert_writable_job_identity_in_connection(
        self, connection: sqlite3.Connection, job_id: str,
    ) -> None:
        if self._legacy_history.exists_in_connection(connection, job_id=job_id):
            raise SQLiteJobLeaseConflict(f"legacy Job is read-only: {job_id}")

    def _require_locked(
        self, connection: sqlite3.Connection, job_id: str,
    ) -> SQLiteJobRecord:
        self._assert_writable_job_identity_in_connection(connection, job_id)
        try:
            record = JobProjectionBuilder.derive_in_connection(connection, job_id=job_id)
        except KeyError:
            raise SQLiteJobLeaseConflict("job was not found")
        return SQLiteJobRecord(record.payload, record.revision)

    def _connect(self) -> sqlite3.Connection:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self._path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        try:
            connection.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower():
                connection.close()
                raise
        connection.execute("PRAGMA synchronous=FULL")
        _initialize_job_schema(connection)
        connection.commit()
        return connection

    def _connect_readonly(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            f"{self._path.as_uri()}?mode=ro",
            uri=True,
            timeout=30,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA query_only=ON")
        return connection


class SQLiteJobStoreTransaction:
    """Connection-bound Job mutation participant for an aggregate UoW."""

    def __init__(self, store: SQLiteJobStore, connection: sqlite3.Connection) -> None:
        self._store = store
        self._connection = connection

    def save(self, job: Mapping[str, object], *, expected_revision: int) -> SQLiteJobRecord:
        return self._store._save_locked(self._connection, job, expected_revision=expected_revision)

    def create(self, job: Mapping[str, object]) -> SQLiteJobRecord:
        return self.save(job, expected_revision=0)


def _is_adopted_legacy_effect_projection(
    connection: sqlite3.Connection,
    *,
    job_id: str,
    payload: Mapping[str, object],
    revision: int,
) -> bool:
    """Prove one rollback source row is already owned by the old Effect cutover."""

    if (
        payload.get("id") != job_id
        or payload.get("execution_version") == "effect-v2"
        or not isinstance(revision, int)
        or isinstance(revision, bool)
        or revision <= 0
    ):
        return False
    attempt = payload.get("attempt")
    if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 0:
        return False

    inventory = connection.execute(
        "SELECT COUNT(*),MIN(sequence),MAX(sequence),COUNT(DISTINCT sequence) "
        "FROM job_effect_fact WHERE job_id=?",
        (job_id,),
    ).fetchone()
    if inventory is None or tuple(int(value or 0) for value in inventory) != (
        revision, 1, revision, revision,
    ):
        return False

    latest = connection.execute(
        "SELECT effect_operation_id,payload_json FROM job_effect_fact "
        "WHERE job_id=? ORDER BY sequence DESC LIMIT 1",
        (job_id,),
    ).fetchone()
    expected_attempt_operation = job_attempt_operation_id(job_id, attempt)
    if latest is None or str(latest["effect_operation_id"]) != expected_attempt_operation:
        return False
    try:
        latest_payload = json.loads(str(latest["payload_json"]))
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    if latest_payload != job_fact_payload(payload):
        return False

    disconnected = connection.execute(
        "SELECT 1 FROM job_effect_fact AS fact "
        "LEFT JOIN effect AS effect ON effect.operation_id=fact.effect_operation_id "
        "WHERE fact.job_id=? AND (effect.operation_id IS NULL OR effect.root_id<>? "
        "OR effect.contract_version<>'legacy-v1') LIMIT 1",
        (job_id, job_id),
    ).fetchone()
    if disconnected is not None:
        return False

    required_nodes = (
        ("root", "root", 0, job_root_operation_id(job_id), "job_root"),
        ("attempt", "attempt", attempt, expected_attempt_operation, "job_attempt"),
    )
    for node_kind, node_key, node_attempt, operation_id, effect_kind in required_nodes:
        node = connection.execute(
            "SELECT effect.root_id,effect.kind,effect.contract_version "
            "FROM job_effect_node AS node "
            "JOIN effect AS effect ON effect.operation_id=node.effect_operation_id "
            "WHERE node.job_id=? AND node.node_kind=? AND node.node_key=? "
            "AND node.attempt=? AND node.effect_operation_id=?",
            (job_id, node_kind, node_key, node_attempt, operation_id),
        ).fetchone()
        if node is None or tuple(str(value) for value in node) != (
            job_id, effect_kind, "legacy-v1",
        ):
            return False

    disconnected_node = connection.execute(
        "SELECT 1 FROM job_effect_node AS node "
        "LEFT JOIN effect AS effect ON effect.operation_id=node.effect_operation_id "
        "WHERE node.job_id=? AND (effect.operation_id IS NULL OR effect.root_id<>? "
        "OR effect.contract_version<>'legacy-v1') LIMIT 1",
        (job_id, job_id),
    ).fetchone()
    return disconnected_node is None


def _is_retired_compatibility_projection(
    connection: sqlite3.Connection,
    *,
    job_id: str,
    payload: Mapping[str, object],
    revision: int,
) -> bool:
    """Recognize one field-exact duplicate of the current cache.

    A historical ``job_store`` row is not proof of execution authority.  It is
    safe to leave it untouched only when the current ``job_projection`` already
    holds exactly the same, strictly valid projection-only record.  Normalizing
    first would accidentally accept stale or malformed historical payloads, so
    the source must already equal its legal canonical form.
    """

    if (
        not isinstance(revision, int)
        or isinstance(revision, bool)
        or revision <= 0
        or payload.get("id") != job_id
    ):
        return False
    try:
        canonical = _normalize_projection_only_job(payload)
    except SQLiteJobLeaseConflict:
        return False
    if canonical != dict(payload):
        return False
    projection = _read_projection_only_in_connection(connection, job_id)
    return projection is not None and (
        projection.revision == revision
        and dict(projection.payload) == dict(payload)
    )


def _initialize_job_schema(connection: sqlite3.Connection) -> None:
    initialize_job_projection_schema(connection)
    initialize_legacy_job_history_schema(connection)
    connection.execute(
        "CREATE TABLE IF NOT EXISTS media_execution_evidence (job_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS media_execution_receipts (job_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS media_recipe_step_evidence (job_id TEXT NOT NULL, step_name TEXT NOT NULL, payload_json TEXT NOT NULL, PRIMARY KEY (job_id, step_name))"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS media_recipe_step_receipts (job_id TEXT NOT NULL, step_name TEXT NOT NULL, payload_json TEXT NOT NULL, PRIMARY KEY (job_id, step_name))"
    )


_PROJECTION_ONLY_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
_WORKBENCH_COORDINATION_STATUSES = frozenset({"pending", "waiting_user"})
_WORKBENCH_COORDINATION_MARKER = "workbench_auto_intake_coordination"


def _normalize_projection_only_job(job: Mapping[str, object]) -> dict[str, object]:
    """Accept only the two known non-executable compatibility projections.

    Capture has already completed synchronously.  Auto-intake can also persist
    a bounded coordination result, but it cannot use this writer to invent a
    running executable Job.  Every other Job must enter through v2 admission.
    """

    normalized = dict(job)
    job_type = normalized.get("job_type")
    status = normalized.get("status")
    execution_version = normalized.get("execution_version")
    if job_type not in {"capture", "workbench_auto_intake"}:
        raise SQLiteJobLeaseConflict(
            "direct Job snapshots are disabled; executable Jobs require effect-v2 admission"
        )
    if execution_version not in {None, "projection-only"}:
        raise SQLiteJobLeaseConflict("projection-only Job execution_version conflicts")

    if status in _PROJECTION_ONLY_TERMINAL_STATUSES:
        normalized.pop("projection_role", None)
    elif (
        job_type == "workbench_auto_intake"
        and status in _WORKBENCH_COORDINATION_STATUSES
    ):
        normalized["projection_role"] = _WORKBENCH_COORDINATION_MARKER
    else:
        raise SQLiteJobLeaseConflict(
            "projection-only Job must be terminal or explicit workbench_auto_intake coordination"
        )
    normalized["execution_version"] = "projection-only"
    return normalized


def _read_projection_only_in_connection(
    connection: sqlite3.Connection, job_id: str,
) -> SQLiteJobRecord | None:
    if not _table_exists(connection, "job_projection"):
        return None
    row = connection.execute(
        "SELECT payload_json,revision FROM job_projection WHERE job_id=?", (job_id,),
    ).fetchone()
    if row is None:
        return None
    payload = json.loads(str(row["payload_json"]))
    if not isinstance(payload, Mapping) or payload.get("execution_version") != "projection-only":
        return None
    return SQLiteJobRecord(dict(payload), int(row["revision"]))


def _all_projection_only_in_connection(
    connection: sqlite3.Connection,
) -> tuple[SQLiteJobRecord, ...]:
    if not _table_exists(connection, "job_projection"):
        return ()
    rows = connection.execute(
        "SELECT payload_json,revision FROM job_projection ORDER BY job_id"
    ).fetchall()
    records: list[SQLiteJobRecord] = []
    for row in rows:
        payload = json.loads(str(row["payload_json"]))
        if isinstance(payload, Mapping) and payload.get("execution_version") == "projection-only":
            records.append(SQLiteJobRecord(dict(payload), int(row["revision"])))
    return tuple(records)


def _table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table_name,),
    ).fetchone() is not None


def _job_id(job: Mapping[str, object]) -> str:
    value = job.get("id")
    if not isinstance(value, str) or not value:
        raise ValueError("job requires id")
    return value


def _record(row: sqlite3.Row) -> SQLiteJobRecord:
    payload = json.loads(row["payload_json"])
    if not isinstance(payload, dict):
        raise ValueError("stored job payload is invalid")
    return SQLiteJobRecord(dict(payload), int(row["revision"]))


def _canonical_payload(payload: Mapping[str, object]) -> str:
    return json.dumps(dict(payload), sort_keys=True, separators=(",", ":"))



def _media_payload(payload: Mapping[str, object]) -> Mapping[str, object]:
    media = payload.get("media_hands")
    if not isinstance(media, Mapping): raise SQLiteMediaAdmissionConflict("media job payload is unavailable")
    policy = media.get("policy")
    if not isinstance(policy, Mapping) or not isinstance(policy.get("lane_max_concurrency"), Mapping): raise SQLiteMediaAdmissionConflict("media concurrency snapshot is unavailable")
    return media


def _settle_media_projection(payload: dict[str, object], *, status: str) -> None:
    media = payload.get("media_hands")
    if not isinstance(media, Mapping):
        return
    updated = dict(media)
    updated["concurrency"] = {"state": "idle", "acquired_at": None}
    updated["admission_state"] = status if status in {"completed", "failed", "cancelled"} else "queued"
    payload["media_hands"] = updated


def _media_effect_operation_id(execution_id: str) -> str:
    return f"media-effect-{execution_id}"


def _media_recipe_step_effect_operation_id(execution_id: str, step_name: str) -> str:
    return f"media-recipe-effect-{execution_id}:{step_name}"


def _media_execution_evidence_for_job(
    job: Mapping[str, object], *, execution_id: str, provider_id: str, provider_revision: str
) -> MediaOperationExecutionEvidence:
    media = _media_payload(job)
    manifest = media.get("manifest")
    if not isinstance(manifest, Mapping):
        raise ValueError("media execution evidence manifest is unavailable")
    return validate_media_operation_execution_evidence(MediaOperationExecutionEvidence(
        job_id=job.get("id") if isinstance(job.get("id"), str) else "",
        source_id=job.get("source_id") if isinstance(job.get("source_id"), str) else "",
        operation=media.get("operation") if isinstance(media.get("operation"), str) else "",
        manifest_ref=manifest.get("ref") if isinstance(manifest.get("ref"), str) else "",
        manifest_revision=manifest.get("revision") if isinstance(manifest.get("revision"), str) else "",
        provider_id=provider_id,
        provider_revision=provider_revision,
        execution_id=execution_id,
        state="started",
    ))


def _media_execution_evidence_record(value: object) -> MediaOperationExecutionEvidence:
    try:
        evidence = json.loads(value) if isinstance(value, str) else None
    except json.JSONDecodeError as exc:
        raise ValueError("stored media execution evidence is invalid") from exc
    if not isinstance(evidence, Mapping):
        raise ValueError("stored media execution evidence is invalid")
    return media_operation_execution_evidence_from_payload(evidence)


def _media_execution_evidence_payload(
    evidence: MediaOperationExecutionEvidence,
) -> dict[str, object]:
    return media_operation_execution_evidence_to_payload(evidence)


def _project_media_execution_evidence(
    connection: sqlite3.Connection,
    evidence: MediaOperationExecutionEvidence,
) -> MediaOperationExecutionEvidence:
    row = connection.execute(
        "SELECT state,result_ref FROM effect WHERE operation_id=?",
        (_media_effect_operation_id(evidence.execution_id),),
    ).fetchone()
    if row is None:
        return evidence
    state = EffectState(str(row[0]))
    projected = {
        EffectState.PLANNED: "started",
        EffectState.INFLIGHT: "started",
        EffectState.UNKNOWN: "unknown_effect",
        EffectState.SETTLED_OK: "completed",
    }.get(state)
    if projected is None:
        raise ValueError("media execution Effect state cannot be projected")
    return _media_execution_evidence_with_state(
        evidence,
        projected,
        str(row[1]) if projected == "completed" and row[1] is not None else None,
    )


def _project_media_recipe_step_evidence(
    connection: sqlite3.Connection,
    evidence: MediaRecipeStepEvidence,
) -> MediaRecipeStepEvidence:
    row = connection.execute(
        "SELECT state,result_ref FROM effect WHERE operation_id=?",
        (_media_recipe_step_effect_operation_id(evidence.execution_id, evidence.step_name),),
    ).fetchone()
    if row is None:
        return evidence
    state = EffectState(str(row[0]))
    projected = {
        EffectState.PLANNED: "started",
        EffectState.INFLIGHT: "started",
        EffectState.UNKNOWN: "unknown_effect",
        EffectState.SETTLED_OK: "completed",
    }.get(state)
    if projected is None:
        raise ValueError("media recipe step Effect state cannot be projected")
    return MediaRecipeStepEvidence(
        job_id=evidence.job_id,
        execution_id=evidence.execution_id,
        provider_id=evidence.provider_id,
        provider_revision=evidence.provider_revision,
        step_name=evidence.step_name,
        input_state_hash=evidence.input_state_hash,
        state=projected,  # type: ignore[arg-type]
        receipt_ref=(
            str(row[1]) if projected == "completed" and row[1] is not None else None
        ),
    )


def _media_execution_evidence_with_state(
    evidence: MediaOperationExecutionEvidence, state: str, receipt_ref: str | None,
) -> MediaOperationExecutionEvidence:
    return validate_media_operation_execution_evidence(MediaOperationExecutionEvidence(
        job_id=evidence.job_id,
        source_id=evidence.source_id,
        operation=evidence.operation,
        manifest_ref=evidence.manifest_ref,
        manifest_revision=evidence.manifest_revision,
        provider_id=evidence.provider_id,
        provider_revision=evidence.provider_revision,
        execution_id=evidence.execution_id,
        state=state,  # type: ignore[arg-type]
        receipt_ref=receipt_ref,
    ))


def _media_execution_identity(
    evidence: MediaOperationExecutionEvidence,
) -> tuple[str, str, str, str, str, str, str, str]:
    return (
        evidence.job_id, evidence.source_id, evidence.operation, evidence.manifest_ref,
        evidence.manifest_revision, evidence.provider_id, evidence.provider_revision,
        evidence.execution_id,
    )


def _media_recipe_step_identity(
    evidence: MediaRecipeStepEvidence,
) -> tuple[str, str, str, str, str, str]:
    return (
        evidence.job_id,
        evidence.execution_id,
        evidence.provider_id,
        evidence.provider_revision,
        evidence.step_name,
        evidence.input_state_hash,
    )


def _media_recipe_step_receipt_identity(
    receipt: MediaRecipeStepReceipt,
) -> tuple[str, str, str, str, str, str]:
    return (
        receipt.job_id,
        receipt.execution_id,
        receipt.provider_id,
        receipt.provider_revision,
        receipt.step_name,
        receipt.input_state_hash,
    )


def _require_parent_media_execution(
    connection: sqlite3.Connection, step: MediaRecipeStepEvidence
) -> None:
    row = connection.execute(
        "SELECT payload_json FROM media_execution_evidence WHERE job_id = ?", (step.job_id,)
    ).fetchone()
    if row is None:
        raise ValueError("media recipe step has no parent execution evidence")
    parent = _project_media_execution_evidence(
        connection, _media_execution_evidence_record(row["payload_json"]),
    )
    lifecycle_bound = parent.state in {"started", "unknown_effect"} or (
        parent.state == "completed" and step.state == "completed"
    )
    if (
        parent.execution_id != step.execution_id
        or parent.provider_id != step.provider_id
        or parent.provider_revision != step.provider_revision
        or not lifecycle_bound
    ):
        raise ValueError("media recipe step is not bound to its active parent execution")


def _media_execution_execution_id(value: object) -> str:
    return validate_media_operation_execution_evidence(MediaOperationExecutionEvidence(
        job_id="media-execution-placeholder",
        source_id="media-execution-placeholder",
        operation="placeholder",
        manifest_ref="crp://jobs/source-manifests/media-execution-placeholder",
        manifest_revision="media-execution-placeholder",
        provider_id="media-execution-placeholder",
        provider_revision="media-execution-placeholder",
        execution_id=value if isinstance(value, str) else "",
        state="started",
    )).execution_id


def _media_execution_evidence_public(
    evidence: MediaOperationExecutionEvidence,
) -> dict[str, object]:
    payload = _media_execution_evidence_payload(evidence)
    payload.pop("schema_version")
    payload.pop("kind")
    return payload


def _media_execution_receipt_record(
    value: object, *, budget: Mapping[str, int]
) -> MediaOperationExecutionReceipt:
    try:
        payload = json.loads(value) if isinstance(value, str) else None
    except json.JSONDecodeError as exc:
        raise ValueError("stored media execution receipt is invalid") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("stored media execution receipt is invalid")
    return media_operation_execution_receipt_from_payload(payload, budget=budget)


def _media_execution_receipt_identity(
    receipt: MediaOperationExecutionReceipt,
) -> tuple[object, ...]:
    return (
        receipt.job_id, receipt.source_id, receipt.operation, receipt.manifest_ref,
        receipt.manifest_revision, receipt.provider_id, receipt.provider_revision,
        receipt.execution_id, receipt.receipt_ref, _canonical_payload(receipt.permission_snapshot),
        None if receipt.credential_use is None else _canonical_payload(receipt.credential_use),
    )


def _media_execution_receipt_matches_evidence(
    receipt: MediaOperationExecutionReceipt, evidence: MediaOperationExecutionEvidence,
) -> bool:
    return (
        receipt.job_id, receipt.source_id, receipt.operation, receipt.manifest_ref,
        receipt.manifest_revision, receipt.provider_id, receipt.provider_revision,
        receipt.execution_id,
    ) == _media_execution_identity(evidence)


def _media_execution_receipt_public(
    receipt: MediaOperationExecutionReceipt,
) -> dict[str, object]:
    # Validation already happened at write/read time. This representation stays
    # confined to references and resource counters, never output content.
    return {
        "job_id": receipt.job_id,
        "source_id": receipt.source_id,
        "operation": receipt.operation,
        "manifest_ref": receipt.manifest_ref,
        "manifest_revision": receipt.manifest_revision,
        "provider_id": receipt.provider_id,
        "provider_revision": receipt.provider_revision,
        "execution_id": receipt.execution_id,
        "permission_snapshot": dict(receipt.permission_snapshot),
        "receipt_ref": receipt.receipt_ref,
        "published_outputs": [dict(value) for value in receipt.published_outputs],
        "checkpoint": dict(receipt.checkpoint),
        "consumed": dict(receipt.consumed),
        "log_refs": list(receipt.log_refs),
        "credential_use": None if receipt.credential_use is None else dict(receipt.credential_use),
    }
