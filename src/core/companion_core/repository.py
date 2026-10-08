from __future__ import annotations

import base64
import hashlib
import json
import re
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .errors import CompanionConflict, CompanionIntegrityError, CompanionRepositoryError
from .models import (
    CompanionInteractionEvent,
    CompanionForgetReceipt,
    CompanionHistoryItem,
    CompanionHistoryPage,
    CompanionInventoryItem,
    CompanionMasterProfile,
    CompanionMessage,
    CompanionMessageDependency,
    CompanionMessagePage,
    ConversationEpisodeProjection,
    CompanionProfileMutation,
    CompanionPromptBinding,
    CompanionPromptBindingMutation,
    CompanionSchemaStatus,
    CompanionSetting,
    CompanionReminder,
    CompanionReminderOccurrence,
    CompanionSession,
    CompanionStateActionResult,
    CompanionStateSnapshot,
    CompanionWalletIntegrity,
    CompanionWalletEntry,
    CompanionWalletMutation,
)
from .schema import SCHEMA_VERSION, migrate
from .conversation_recall import CONVERSATION_AGENT_ID, episode_id_for, summarize_conversation_pair


_SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9:_-]{0,127}$")
_MESSAGE_ROLES = {"user", "assistant", "system_event"}
_MESSAGE_STATUSES = {"completed", "failed", "cancelled"}
_PROVIDER_MODES = {"local", "remote", "none"}
_INTERACTION_KINDS = {"petting", "chat"}
_DEPENDENT_KINDS = {"context", "cache", "fts", "vector", "candidate", "summary", "published_memory"}
_DEPENDENT_STATES = {"active", "deleted", "withdrawn", "failed"}
_BUSY_TIMEOUT_MS = 5_000
_MEMORY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:_-]{0,191}$")
_PROJECT_ID_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class CompanionRepository:
    """Single SQLite authority for mutable Companion state.

    All SQL remains inside this domain adapter. Public mutations enforce CAS or
    idempotency before one short `BEGIN IMMEDIATE` transaction is committed.
    """

    def __init__(
        self,
        database_path: Path,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.database_path = database_path.expanduser().absolute()
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._maintenance_lock = threading.RLock()
        self._initialized = False

    @classmethod
    def at_data_root(
        cls,
        data_root: Path,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> CompanionRepository:
        root = data_root.expanduser().absolute()
        return cls(root / ".rebuild-data" / "companion" / "companion.sqlite3", now=now)

    def initialize(self) -> CompanionSchemaStatus:
        with self._maintenance_lock:
            connection = self._open_connection()
            try:
                applied = migrate(connection, applied_at=self._now_utc())
                status = self._schema_status(connection, applied=applied)
                integrity = connection.execute("PRAGMA integrity_check").fetchone()
                if integrity is None or str(integrity[0]).lower() != "ok":
                    raise CompanionIntegrityError("companion database integrity check failed")
                self._initialized = True
                return status
            finally:
                connection.close()

    def schema_status(self) -> CompanionSchemaStatus:
        self._ensure_initialized()
        connection = self._open_connection()
        try:
            return self._schema_status(connection, applied=())
        finally:
            connection.close()

    def get_setting(self, setting_id: str) -> CompanionSetting | None:
        _require_id("setting_id", setting_id)
        self._ensure_initialized()
        connection = self._open_connection()
        try:
            row = connection.execute("SELECT * FROM companion_settings WHERE id = ?", (setting_id,)).fetchone()
            return _setting_from_row(row) if row is not None else None
        finally:
            connection.close()

    def save_setting(self, *, setting_id: str, expected_revision: int, payload: dict[str, object], updated_at: str) -> CompanionSetting:
        _require_id("setting_id", setting_id)
        _require_non_negative("expected_revision", expected_revision)
        _require_utc("updated_at", updated_at)
        if not isinstance(payload, dict):
            raise CompanionRepositoryError("setting payload is invalid")
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > 8_192:
            raise CompanionRepositoryError("setting payload is too large")
        with self._transaction() as connection:
            row = connection.execute("SELECT revision FROM companion_settings WHERE id = ?", (setting_id,)).fetchone()
            actual_revision = int(row["revision"]) if row is not None else 0
            if actual_revision != expected_revision:
                raise CompanionConflict(f"setting expected revision {expected_revision}, found {actual_revision}")
            revision = actual_revision + 1
            connection.execute(
                """INSERT INTO companion_settings (id, revision, payload_json, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET revision = excluded.revision,
                    payload_json = excluded.payload_json, updated_at = excluded.updated_at""",
                (setting_id, revision, encoded, updated_at),
            )
            saved = connection.execute("SELECT * FROM companion_settings WHERE id = ?", (setting_id,)).fetchone()
            if saved is None:
                raise CompanionIntegrityError("saved setting is missing")
            return _setting_from_row(saved)

    def claim_daily_setting(self, *, setting_id: str, local_day: str, updated_at: str) -> bool:
        _require_id("setting_id", setting_id)
        _require_utc("updated_at", updated_at)
        if not isinstance(local_day, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", local_day) is None:
            raise CompanionRepositoryError("local day is invalid")
        try:
            date.fromisoformat(local_day)
        except ValueError as exc:
            raise CompanionRepositoryError("local day is invalid") from exc
        with self._transaction() as connection:
            row = connection.execute("SELECT revision, payload_json FROM companion_settings WHERE id = ?", (setting_id,)).fetchone()
            revision = int(row["revision"]) if row is not None else 0
            if row is not None:
                try:
                    stored = json.loads(str(row["payload_json"]))
                except json.JSONDecodeError as exc:
                    raise CompanionIntegrityError("stored setting payload is invalid") from exc
                if stored == {"local_day": local_day}:
                    return False
            connection.execute(
                """INSERT INTO companion_settings (id, revision, payload_json, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET revision = excluded.revision,
                    payload_json = excluded.payload_json, updated_at = excluded.updated_at""",
                (setting_id, revision + 1, json.dumps({"local_day": local_day}, separators=(",", ":")), updated_at),
            )
            return True

    def create_reminder(
        self,
        *,
        reminder_id: str,
        schedule: dict[str, object],
        advance_minutes: int,
        next_fire_at: str,
        occurrences: tuple[dict[str, str], ...],
        updated_at: str,
    ) -> CompanionReminder:
        _require_id("reminder_id", reminder_id)
        _require_utc("next_fire_at", next_fire_at)
        _require_utc("updated_at", updated_at)
        if advance_minutes not in {0, 5} or not isinstance(schedule, dict) or not occurrences:
            raise CompanionRepositoryError("reminder definition is invalid")
        encoded = json.dumps(schedule, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > 4_096:
            raise CompanionRepositoryError("reminder schedule is too large")
        with self._transaction() as connection:
            if connection.execute("SELECT 1 FROM companion_reminders WHERE reminder_id = ?", (reminder_id,)).fetchone():
                raise CompanionConflict("reminder id already exists")
            connection.execute(
                """INSERT INTO companion_reminders
                (reminder_id, schedule_json, advance_minutes, next_fire_at, ack_state, revision, updated_at)
                VALUES (?, ?, ?, ?, 'pending', 1, ?)""",
                (reminder_id, encoded, advance_minutes, next_fire_at, updated_at),
            )
            for occurrence in occurrences:
                if set(occurrence) != {"occurrence_id", "scheduled_for", "fire_at", "phase"}:
                    raise CompanionRepositoryError("reminder occurrence is invalid")
                _require_id("occurrence_id", occurrence["occurrence_id"])
                _require_utc("scheduled_for", occurrence["scheduled_for"])
                _require_utc("fire_at", occurrence["fire_at"])
                if occurrence["phase"] not in {"advance", "due"}:
                    raise CompanionRepositoryError("reminder occurrence phase is invalid")
                connection.execute(
                    """INSERT INTO companion_reminder_occurrences
                    (occurrence_id, reminder_id, scheduled_for, fire_at, phase, state, snooze_until, revision, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, 'pending', NULL, 1, ?, ?)""",
                    (occurrence["occurrence_id"], reminder_id, occurrence["scheduled_for"], occurrence["fire_at"], occurrence["phase"], updated_at, updated_at),
                )
            row = connection.execute("SELECT * FROM companion_reminders WHERE reminder_id = ?", (reminder_id,)).fetchone()
            return _reminder_from_row(row)

    def list_reminders(self, *, include_cancelled: bool = False) -> tuple[CompanionReminder, ...]:
        self._ensure_initialized()
        connection = self._open_connection()
        try:
            where = "" if include_cancelled else "WHERE ack_state != 'cancelled'"
            rows = connection.execute(f"SELECT * FROM companion_reminders {where} ORDER BY next_fire_at, reminder_id").fetchall()
            return tuple(_reminder_from_row(row) for row in rows)
        finally:
            connection.close()

    def list_reminder_occurrences(self, reminder_id: str) -> tuple[CompanionReminderOccurrence, ...]:
        _require_id("reminder_id", reminder_id)
        self._ensure_initialized()
        connection = self._open_connection()
        try:
            rows = connection.execute(
                "SELECT * FROM companion_reminder_occurrences WHERE reminder_id = ? ORDER BY fire_at, phase",
                (reminder_id,),
            ).fetchall()
            return tuple(_reminder_occurrence_from_row(row) for row in rows)
        finally:
            connection.close()

    def cancel_reminder(self, *, reminder_id: str, expected_revision: int, updated_at: str) -> CompanionReminder:
        _require_id("reminder_id", reminder_id)
        _require_positive("expected_revision", expected_revision)
        _require_utc("updated_at", updated_at)
        with self._transaction() as connection:
            row = connection.execute("SELECT * FROM companion_reminders WHERE reminder_id = ?", (reminder_id,)).fetchone()
            if row is None:
                raise CompanionRepositoryError("reminder was not found")
            if int(row["revision"]) != expected_revision:
                raise CompanionConflict(f"reminder expected revision {expected_revision}, found {int(row['revision'])}")
            if str(row["ack_state"]) != "cancelled":
                connection.execute(
                    "UPDATE companion_reminders SET ack_state = 'cancelled', next_fire_at = NULL, revision = revision + 1, updated_at = ? WHERE reminder_id = ?",
                    (updated_at, reminder_id),
                )
                connection.execute(
                    "UPDATE companion_reminder_occurrences SET state = 'cancelled', revision = revision + 1, updated_at = ? WHERE reminder_id = ? AND state IN ('pending', 'snoozed')",
                    (updated_at, reminder_id),
                )
            saved = connection.execute("SELECT * FROM companion_reminders WHERE reminder_id = ?", (reminder_id,)).fetchone()
            return _reminder_from_row(saved)

    def list_due_reminder_occurrences(
        self, *, now: str, oldest_meaningful: str
    ) -> tuple[tuple[CompanionReminderOccurrence, CompanionReminder], ...]:
        _require_utc("now", now)
        _require_utc("oldest_meaningful", oldest_meaningful)
        with self._transaction() as connection:
            connection.execute(
                """UPDATE companion_reminder_occurrences
                SET state = 'expired', revision = revision + 1, updated_at = ?
                WHERE state IN ('pending', 'presented', 'snoozed')
                  AND COALESCE(snooze_until, fire_at) < ?""",
                (now, oldest_meaningful),
            )
            rows = connection.execute(
                """SELECT o.*, r.schedule_json, r.advance_minutes, r.next_fire_at,
                          r.ack_state, r.revision AS reminder_revision, r.updated_at AS reminder_updated_at
                FROM companion_reminder_occurrences o
                JOIN companion_reminders r ON r.reminder_id = o.reminder_id
                WHERE r.ack_state != 'cancelled'
                  AND o.state IN ('pending', 'presented', 'snoozed')
                  AND COALESCE(o.snooze_until, o.fire_at) <= ?
                ORDER BY COALESCE(o.snooze_until, o.fire_at), o.phase, o.occurrence_id""",
                (now,),
            ).fetchall()
            return tuple((_reminder_occurrence_from_row(row), _joined_reminder_from_row(row)) for row in rows)

    def present_reminder_occurrence(
        self, *, occurrence_id: str, expected_revision: int, requires_ack: bool, updated_at: str
    ) -> CompanionReminderOccurrence:
        _require_id("occurrence_id", occurrence_id)
        _require_positive("expected_revision", expected_revision)
        _require_utc("updated_at", updated_at)
        with self._transaction() as connection:
            row = connection.execute("SELECT * FROM companion_reminder_occurrences WHERE occurrence_id = ?", (occurrence_id,)).fetchone()
            if row is None:
                raise CompanionRepositoryError("reminder occurrence was not found")
            if int(row["revision"]) != expected_revision:
                if str(row["state"]) in ({"presented"} if requires_ack else {"acknowledged"}):
                    return _reminder_occurrence_from_row(row)
                raise CompanionConflict("reminder occurrence revision conflict")
            target = "presented" if requires_ack else "acknowledged"
            if str(row["state"]) not in {"pending", "snoozed", "presented"}:
                raise CompanionConflict("reminder occurrence is no longer presentable")
            if str(row["state"]) != target:
                connection.execute(
                    "UPDATE companion_reminder_occurrences SET state = ?, snooze_until = NULL, revision = revision + 1, updated_at = ? WHERE occurrence_id = ?",
                    (target, updated_at, occurrence_id),
                )
            saved = connection.execute("SELECT * FROM companion_reminder_occurrences WHERE occurrence_id = ?", (occurrence_id,)).fetchone()
            self._refresh_reminder_state(connection, str(saved["reminder_id"]), updated_at)
            return _reminder_occurrence_from_row(saved)

    def act_on_reminder_occurrence(
        self, *, occurrence_id: str, action: str, expected_revision: int, now: str
    ) -> CompanionReminderOccurrence:
        _require_id("occurrence_id", occurrence_id)
        _require_positive("expected_revision", expected_revision)
        _require_utc("now", now)
        targets = {"acknowledge": "acknowledged", "complete": "completed", "snooze_5m": "snoozed"}
        if action not in targets:
            raise CompanionRepositoryError("reminder action is invalid")
        with self._transaction() as connection:
            row = connection.execute("SELECT * FROM companion_reminder_occurrences WHERE occurrence_id = ?", (occurrence_id,)).fetchone()
            if row is None:
                raise CompanionRepositoryError("reminder occurrence was not found")
            target = targets[action]
            if int(row["revision"]) != expected_revision:
                if str(row["state"]) == target:
                    return _reminder_occurrence_from_row(row)
                raise CompanionConflict("reminder occurrence revision conflict")
            if str(row["state"]) != "presented":
                if str(row["state"]) == target:
                    return _reminder_occurrence_from_row(row)
                raise CompanionConflict("reminder occurrence is not awaiting action")
            snooze_until = None
            if action == "snooze_5m":
                snooze_until = (datetime.fromisoformat(now.replace("Z", "+00:00")) + timedelta(minutes=5)).isoformat()
            connection.execute(
                "UPDATE companion_reminder_occurrences SET state = ?, snooze_until = ?, revision = revision + 1, updated_at = ? WHERE occurrence_id = ?",
                (target, snooze_until, now, occurrence_id),
            )
            saved = connection.execute("SELECT * FROM companion_reminder_occurrences WHERE occurrence_id = ?", (occurrence_id,)).fetchone()
            self._refresh_reminder_state(connection, str(saved["reminder_id"]), now)
            return _reminder_occurrence_from_row(saved)

    @staticmethod
    def _refresh_reminder_state(connection: sqlite3.Connection, reminder_id: str, updated_at: str) -> None:
        active = connection.execute(
            """SELECT MIN(COALESCE(snooze_until, fire_at)) FROM companion_reminder_occurrences
            WHERE reminder_id = ? AND state IN ('pending', 'presented', 'snoozed')""",
            (reminder_id,),
        ).fetchone()[0]
        if active is not None:
            connection.execute(
                "UPDATE companion_reminders SET next_fire_at = ?, ack_state = 'pending', revision = revision + 1, updated_at = ? WHERE reminder_id = ?",
                (str(active), updated_at, reminder_id),
            )
            return
        completed = connection.execute(
            "SELECT COUNT(*) FROM companion_reminder_occurrences WHERE reminder_id = ? AND state = 'completed'",
            (reminder_id,),
        ).fetchone()[0]
        state = "completed" if int(completed) > 0 else "acknowledged"
        connection.execute(
            "UPDATE companion_reminders SET next_fire_at = NULL, ack_state = ?, revision = revision + 1, updated_at = ? WHERE reminder_id = ?",
            (state, updated_at, reminder_id),
        )

    def create_session(
        self,
        *,
        session_id: str,
        context_epoch: int,
        prompt_revision: int,
        profile_revision: int,
        started_at: str,
        project_id: str = "default",
    ) -> CompanionSession:
        _require_id("session_id", session_id)
        _require_positive("context_epoch", context_epoch)
        _require_positive("prompt_revision", prompt_revision)
        _require_positive("profile_revision", profile_revision)
        project_id = _require_project_id(project_id)
        _require_utc("started_at", started_at)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM companion_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            expected = (context_epoch, project_id, prompt_revision, profile_revision, started_at)
            if row is not None:
                actual = (
                    int(row["context_epoch"]),
                    str(row["project_id"]),
                    int(row["prompt_revision"]),
                    int(row["profile_revision"]),
                    str(row["started_at"]),
                )
                if actual != expected:
                    raise CompanionConflict("session id was reused with different input")
                return _session_from_row(row)
            connection.execute(
                """
                INSERT INTO companion_sessions (
                    session_id, context_epoch, prompt_revision, profile_revision,
                    started_at, closed_at, revision, project_id
                ) VALUES (?, ?, ?, ?, ?, NULL, 1, ?)
                """,
                (session_id, context_epoch, prompt_revision, profile_revision, started_at, project_id),
            )
            created = connection.execute(
                "SELECT * FROM companion_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if created is None:
                raise CompanionIntegrityError("created session is missing")
            return _session_from_row(created)

    def get_session(self, session_id: str) -> CompanionSession | None:
        _require_id("session_id", session_id)
        self._ensure_initialized()
        connection = self._open_connection()
        try:
            row = connection.execute(
                "SELECT * FROM companion_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            return _session_from_row(row) if row is not None else None
        finally:
            connection.close()

    def synchronize_session_authorities(
        self,
        *,
        session_id: str,
        prompt_revision: int,
        profile_revision: int,
    ) -> CompanionSession:
        _require_id("session_id", session_id)
        _require_positive("prompt_revision", prompt_revision)
        _require_positive("profile_revision", profile_revision)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM companion_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None or row["closed_at"] is not None:
                raise CompanionRepositoryError("chat session is unavailable")
            if int(row["prompt_revision"]) != prompt_revision or int(row["profile_revision"]) != profile_revision:
                connection.execute(
                    """
                    UPDATE companion_sessions
                    SET context_epoch = context_epoch + 1,
                        prompt_revision = ?, profile_revision = ?, revision = revision + 1
                    WHERE session_id = ?
                    """,
                    (prompt_revision, profile_revision, session_id),
                )
                row = connection.execute(
                    "SELECT * FROM companion_sessions WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
            return _session_from_row(row)

    def get_master_profile(self) -> CompanionMasterProfile | None:
        self._ensure_initialized()
        connection = self._open_connection()
        try:
            row = connection.execute(
                "SELECT * FROM companion_master_profile WHERE id = 'current'"
            ).fetchone()
            return _profile_from_row(row) if row is not None else None
        finally:
            connection.close()

    def save_master_profile(
        self,
        *,
        expected_revision: int,
        nickname: str,
        birthday: str | None,
        oc_address: str,
        relationship: str,
        custom_notes: str,
        updated_at: str,
    ) -> CompanionProfileMutation:
        _require_non_negative("expected_revision", expected_revision)
        _require_utc("updated_at", updated_at)
        if not isinstance(nickname, str) or len(nickname) > 120:
            raise CompanionRepositoryError("profile nickname is invalid")
        if birthday is not None and (not isinstance(birthday, str) or len(birthday) > 5):
            raise CompanionRepositoryError("profile birthday is invalid")
        if not isinstance(oc_address, str) or len(oc_address) > 120:
            raise CompanionRepositoryError("profile OC address is invalid")
        if not isinstance(relationship, str) or len(relationship) > 1_000:
            raise CompanionRepositoryError("profile relationship is invalid")
        if not isinstance(custom_notes, str) or len(custom_notes) > 2_000:
            raise CompanionRepositoryError("profile custom notes are invalid")
        extra_json = json.dumps(
            {"custom_notes": custom_notes},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT revision FROM companion_master_profile WHERE id = 'current'"
            ).fetchone()
            actual_revision = int(row["revision"]) if row is not None else 0
            if actual_revision != expected_revision:
                raise CompanionConflict(
                    f"profile expected revision {expected_revision}, found {actual_revision}"
                )
            revision = actual_revision + 1
            connection.execute(
                """
                INSERT INTO companion_master_profile (
                    id, revision, nickname, birthday, oc_address,
                    relationship, extra_json, updated_at
                ) VALUES ('current', ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    revision = excluded.revision,
                    nickname = excluded.nickname,
                    birthday = excluded.birthday,
                    oc_address = excluded.oc_address,
                    relationship = excluded.relationship,
                    extra_json = excluded.extra_json,
                    updated_at = excluded.updated_at
                """,
                (revision, nickname, birthday, oc_address, relationship, extra_json, updated_at),
            )
            rebased = connection.execute(
                """
                UPDATE companion_sessions
                SET context_epoch = context_epoch + 1,
                    profile_revision = ?,
                    revision = revision + 1
                WHERE closed_at IS NULL
                """,
                (revision,),
            ).rowcount
            saved = connection.execute(
                "SELECT * FROM companion_master_profile WHERE id = 'current'"
            ).fetchone()
            if saved is None:
                raise CompanionIntegrityError("saved profile is missing")
            return CompanionProfileMutation(_profile_from_row(saved), int(rebased))

    def get_prompt_binding(self) -> CompanionPromptBinding | None:
        self._ensure_initialized()
        connection = self._open_connection()
        try:
            row = connection.execute(
                "SELECT * FROM companion_prompt_binding WHERE id = 'active'"
            ).fetchone()
            return _prompt_binding_from_row(row) if row is not None else None
        finally:
            connection.close()

    def apply_prompt_binding(
        self,
        *,
        expected_revision: int,
        active_prompt_id: str,
        activation_revision: int,
        unit_revision: int,
        updated_at: str,
    ) -> CompanionPromptBindingMutation:
        _require_non_negative("expected_revision", expected_revision)
        _require_id("active_prompt_id", active_prompt_id)
        _require_positive("activation_revision", activation_revision)
        _require_positive("unit_revision", unit_revision)
        _require_utc("updated_at", updated_at)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT revision FROM companion_prompt_binding WHERE id = 'active'"
            ).fetchone()
            actual_revision = int(row["revision"]) if row is not None else 0
            if actual_revision != expected_revision:
                raise CompanionConflict(
                    f"prompt binding expected revision {expected_revision}, found {actual_revision}"
                )
            revision = actual_revision + 1
            connection.execute(
                """
                INSERT INTO companion_prompt_binding (
                    id, active_prompt_id, activation_revision,
                    unit_revision, revision, updated_at
                ) VALUES ('active', ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    active_prompt_id = excluded.active_prompt_id,
                    activation_revision = excluded.activation_revision,
                    unit_revision = excluded.unit_revision,
                    revision = excluded.revision,
                    updated_at = excluded.updated_at
                """,
                (active_prompt_id, activation_revision, unit_revision, revision, updated_at),
            )
            rebased = connection.execute(
                """
                UPDATE companion_sessions
                SET context_epoch = context_epoch + 1,
                    prompt_revision = ?,
                    revision = revision + 1
                WHERE closed_at IS NULL
                """,
                (activation_revision,),
            ).rowcount
            saved = connection.execute(
                "SELECT * FROM companion_prompt_binding WHERE id = 'active'"
            ).fetchone()
            if saved is None:
                raise CompanionIntegrityError("saved prompt binding is missing")
            return CompanionPromptBindingMutation(_prompt_binding_from_row(saved), int(rebased))

    def append_message(
        self,
        *,
        message_id: str,
        request_id: str,
        session_id: str,
        context_epoch: int,
        role: str,
        status: str,
        content: str,
        created_at: str,
        provider_mode: str,
        project_id: str = "default",
        memory_review: Mapping[str, object] | None = None,
    ) -> CompanionMessage:
        for label, value in (("message_id", message_id), ("request_id", request_id), ("session_id", session_id)):
            _require_id(label, value)
        _require_positive("context_epoch", context_epoch)
        project_id = _require_project_id(project_id)
        _require_utc("created_at", created_at)
        if role not in _MESSAGE_ROLES:
            raise CompanionRepositoryError("message role is invalid")
        if status not in _MESSAGE_STATUSES:
            raise CompanionRepositoryError("message status is invalid")
        if provider_mode not in _PROVIDER_MODES:
            raise CompanionRepositoryError("message provider mode is invalid")
        if status == "completed" and not content:
            raise CompanionRepositoryError("completed message requires content")
        if status != "completed" and content:
            raise CompanionRepositoryError("non-completed message must not persist content")
        if len(content) > 32_000:
            raise CompanionRepositoryError("message content is too long")
        memory_review_json = _memory_review_json(memory_review)

        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM companion_messages WHERE request_id = ? AND role = ?",
                (request_id, role),
            ).fetchone()
            expected = (
                message_id,
                session_id,
                context_epoch,
                status,
                content,
                created_at,
                provider_mode,
                project_id,
                memory_review_json,
            )
            if existing is not None:
                actual = (
                    str(existing["message_id"]),
                    str(existing["session_id"]),
                    int(existing["context_epoch"]),
                    str(existing["status"]),
                    str(existing["content"]),
                    str(existing["created_at"]),
                    str(existing["provider_mode"]),
                    str(existing["project_id"]),
                    str(existing["memory_review_json"]),
                )
                if actual != expected:
                    raise CompanionConflict("message request was replayed with different input")
                if role == "assistant" and status == "completed":
                    _upsert_conversation_episode(connection, assistant_message_id=message_id)
                return _message_from_row(existing)
            try:
                connection.execute(
                    """
                    INSERT INTO companion_messages (
                        message_id, request_id, session_id, context_epoch, role,
                        status, content, created_at, provider_mode, revision,
                        project_id, memory_review_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                    """,
                    (
                        message_id,
                        request_id,
                        session_id,
                        context_epoch,
                        role,
                        status,
                        content,
                        created_at,
                        provider_mode,
                        project_id,
                        memory_review_json,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise CompanionConflict("message violates identity or session constraints") from exc
            created = connection.execute(
                "SELECT * FROM companion_messages WHERE message_id = ?",
                (message_id,),
            ).fetchone()
            if created is None:
                raise CompanionIntegrityError("created message is missing")
            if role == "assistant" and status == "completed":
                _upsert_conversation_episode(connection, assistant_message_id=message_id)
            return _message_from_row(created)

    def get_message_by_request(self, *, request_id: str, role: str) -> CompanionMessage | None:
        _require_id("request_id", request_id)
        if role not in _MESSAGE_ROLES:
            raise CompanionRepositoryError("message role is invalid")
        self._ensure_initialized()
        connection = self._open_connection()
        try:
            row = connection.execute(
                "SELECT * FROM companion_messages WHERE request_id = ? AND role = ?",
                (request_id, role),
            ).fetchone()
            return _message_from_row(row) if row is not None else None
        finally:
            connection.close()

    def get_message(self, message_id: str) -> CompanionMessage | None:
        _require_id("message_id", message_id)
        self._ensure_initialized()
        connection = self._open_connection()
        try:
            row = connection.execute(
                "SELECT * FROM companion_messages WHERE message_id = ?",
                (message_id,),
            ).fetchone()
            return _message_from_row(row) if row is not None else None
        finally:
            connection.close()

    def resolve_message(
        self,
        *,
        message_id: str,
        expected_revision: int,
        status: str,
        content: str,
        provider_mode: str,
        memory_review: Mapping[str, object] | None = None,
    ) -> CompanionMessage:
        _require_id("message_id", message_id)
        _require_positive("expected_revision", expected_revision)
        if status not in {"completed", "cancelled"}:
            raise CompanionRepositoryError("resolved message status is invalid")
        if provider_mode not in {"local", "remote"}:
            raise CompanionRepositoryError("resolved message provider mode is invalid")
        if status == "completed" and (not isinstance(content, str) or not content or len(content) > 32_000):
            raise CompanionRepositoryError("resolved message content is invalid")
        if status == "cancelled" and content:
            raise CompanionRepositoryError("cancelled message must not persist content")
        memory_review_json = _memory_review_json(memory_review)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM companion_messages WHERE message_id = ?",
                (message_id,),
            ).fetchone()
            if row is None:
                raise CompanionRepositoryError("message is missing")
            if int(row["revision"]) != expected_revision:
                raise CompanionConflict("message revision conflict")
            if str(row["status"]) == "completed":
                if (
                    str(row["content"]) == content
                    and str(row["provider_mode"]) == provider_mode
                    and str(row["memory_review_json"]) == memory_review_json
                ):
                    return _message_from_row(row)
                raise CompanionConflict("completed message cannot be replaced")
            connection.execute(
                """
                UPDATE companion_messages
                SET status = ?, content = ?, provider_mode = ?, memory_review_json = ?, revision = revision + 1
                WHERE message_id = ? AND revision = ?
                """,
                (status, content, provider_mode, memory_review_json, message_id, expected_revision),
            )
            updated = connection.execute(
                "SELECT * FROM companion_messages WHERE message_id = ?",
                (message_id,),
            ).fetchone()
            if updated is None:
                raise CompanionIntegrityError("resolved message is missing")
            if str(updated["role"]) == "assistant" and status == "completed":
                _upsert_conversation_episode(connection, assistant_message_id=message_id)
            return _message_from_row(updated)

    def list_context_messages(
        self,
        *,
        session_id: str,
        context_epoch: int,
        project_id: str | None = None,
        limit: int = 24,
    ) -> tuple[CompanionMessage, ...]:
        _require_id("session_id", session_id)
        _require_positive("context_epoch", context_epoch)
        if project_id is not None:
            project_id = _require_project_id(project_id)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 24:
            raise CompanionRepositoryError("context message limit must be between 1 and 24")
        self._ensure_initialized()
        connection = self._open_connection()
        try:
            project_clause = " AND project_id = ?" if project_id is not None else ""
            parameters: tuple[object, ...] = (
                (session_id, context_epoch, project_id, limit)
                if project_id is not None else (session_id, context_epoch, limit)
            )
            rows = connection.execute(
                f"""
                SELECT * FROM (
                    SELECT * FROM companion_messages
                    WHERE session_id = ? AND context_epoch = ?
                      {project_clause}
                      AND role IN ('user', 'assistant') AND status = 'completed'
                    ORDER BY created_at DESC, message_id DESC
                    LIMIT ?
                ) ORDER BY created_at ASC, message_id ASC
                """,
                parameters,
            ).fetchall()
            return tuple(_message_from_row(row) for row in rows)
        finally:
            connection.close()

    def list_messages(
        self,
        *,
        limit: int = 50,
        before: str | None = None,
        session_id: str | None = None,
        project_id: str | None = None,
    ) -> CompanionMessagePage:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise CompanionRepositoryError("message page limit must be between 1 and 100")
        if session_id is not None:
            _require_id("session_id", session_id)
        if project_id is not None:
            project_id = _require_project_id(project_id)
        cursor = _decode_cursor(before) if before is not None else None
        self._ensure_initialized()
        connection = self._open_connection()
        try:
            clauses: list[str] = []
            parameters: list[object] = []
            if session_id is not None:
                clauses.append("session_id = ?")
                parameters.append(session_id)
            if project_id is not None:
                clauses.append("project_id = ?")
                parameters.append(project_id)
            if cursor is not None:
                clauses.append("(created_at < ? OR (created_at = ? AND message_id < ?))")
                parameters.extend((cursor[0], cursor[0], cursor[1]))
            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            rows = connection.execute(
                f"""
                SELECT * FROM companion_messages
                {where}
                ORDER BY created_at DESC, message_id DESC
                LIMIT ?
                """,
                (*parameters, limit + 1),
            ).fetchall()
            has_more = len(rows) > limit
            visible = rows[:limit]
            items = tuple(_message_from_row(row) for row in visible)
            next_cursor = None
            if has_more and visible:
                tail = visible[-1]
                next_cursor = _encode_cursor(str(tail["created_at"]), str(tail["message_id"]))
            return CompanionMessagePage(items=items, next_cursor=next_cursor)
        finally:
            connection.close()

    def rebuild_conversation_episodes(self, *, project_id: str) -> int:
        """Recreate the disposable recall projection from completed chat facts."""

        project_id = _require_project_id(project_id)
        with self._transaction() as connection:
            connection.execute(
                "DELETE FROM companion_conversation_episodes WHERE project_id = ? AND agent_id = ?",
                (project_id, CONVERSATION_AGENT_ID),
            )
            rows = connection.execute(
                """
                SELECT
                    user.message_id AS user_message_id,
                    user.revision AS user_message_revision,
                    user.content AS user_content,
                    assistant.message_id AS assistant_message_id,
                    assistant.revision AS assistant_message_revision,
                    assistant.content AS assistant_content,
                    assistant.session_id AS session_id,
                    assistant.context_epoch AS context_epoch,
                    assistant.request_id AS request_id,
                    assistant.created_at AS occurred_at
                FROM companion_messages AS user
                JOIN companion_messages AS assistant
                  ON assistant.request_id = user.request_id
                 AND assistant.session_id = user.session_id
                 AND assistant.context_epoch = user.context_epoch
                 AND assistant.project_id = user.project_id
                JOIN companion_sessions AS session
                  ON session.session_id = assistant.session_id
                 AND session.project_id = assistant.project_id
                WHERE user.project_id = ?
                  AND user.role = 'user' AND user.status = 'completed'
                  AND assistant.role = 'assistant' AND assistant.status = 'completed'
                ORDER BY assistant.created_at ASC, assistant.message_id ASC
                """,
                (project_id,),
            ).fetchall()
            for row in rows:
                user_message_id = str(row["user_message_id"])
                assistant_message_id = str(row["assistant_message_id"])
                connection.execute(
                    """
                    INSERT INTO companion_conversation_episodes (
                        episode_id, agent_id, project_id, session_id, context_epoch, request_id,
                        user_message_id, user_message_revision, assistant_message_id,
                        assistant_message_revision, summary, occurred_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        episode_id_for(user_message_id, assistant_message_id), CONVERSATION_AGENT_ID,
                        project_id, str(row["session_id"]), int(row["context_epoch"]), str(row["request_id"]),
                        user_message_id, int(row["user_message_revision"]), assistant_message_id,
                        int(row["assistant_message_revision"]),
                        summarize_conversation_pair(str(row["user_content"]), str(row["assistant_content"])),
                        str(row["occurred_at"]),
                    ),
                )
            return len(rows)

    def list_conversation_episodes(
        self,
        *,
        project_id: str,
        current_session_id: str,
        limit: int,
    ) -> tuple[ConversationEpisodeProjection, ...]:
        project_id = _require_project_id(project_id)
        _require_id("current_session_id", current_session_id)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 12:
            raise CompanionRepositoryError("conversation episode limit must be between 1 and 12")
        self._ensure_initialized()
        connection = self._open_connection()
        try:
            rows = connection.execute(
                """
                SELECT episode.*
                FROM companion_conversation_episodes AS episode
                JOIN companion_messages AS user ON user.message_id = episode.user_message_id
                JOIN companion_messages AS assistant ON assistant.message_id = episode.assistant_message_id
                JOIN companion_sessions AS session ON session.session_id = episode.session_id
                WHERE episode.project_id = ?
                  AND episode.agent_id = ?
                  AND episode.session_id != ?
                  AND user.revision = episode.user_message_revision
                  AND assistant.revision = episode.assistant_message_revision
                  AND user.project_id = episode.project_id
                  AND assistant.project_id = episode.project_id
                  AND session.project_id = episode.project_id
                  AND user.session_id = episode.session_id
                  AND assistant.session_id = episode.session_id
                  AND user.context_epoch = episode.context_epoch
                  AND assistant.context_epoch = episode.context_epoch
                  AND user.request_id = episode.request_id
                  AND assistant.request_id = episode.request_id
                  AND user.role = 'user' AND user.status = 'completed'
                  AND assistant.role = 'assistant' AND assistant.status = 'completed'
                ORDER BY episode.occurred_at DESC, episode.episode_id DESC
                LIMIT ?
                """,
                (project_id, CONVERSATION_AGENT_ID, current_session_id, limit),
            ).fetchall()
            return tuple(_conversation_episode_from_row(row) for row in rows)
        finally:
            connection.close()

    def get_conversation_episode(
        self, *, project_id: str, session_id: str, episode_id: str,
    ) -> ConversationEpisodeProjection | None:
        """Read one still-valid private episode by its complete scope.

        Unlike recall listing, this exact lookup may address the current
        session.  It exists for post-terminal governance consumers and keeps
        the same message revision, role, status, project and context fences.
        """

        project_id = _require_project_id(project_id)
        _require_id("session_id", session_id)
        _require_id("episode_id", episode_id)
        self._ensure_initialized()
        connection = self._open_connection()
        try:
            row = connection.execute(
                """
                SELECT episode.*
                FROM companion_conversation_episodes AS episode
                JOIN companion_messages AS user ON user.message_id = episode.user_message_id
                JOIN companion_messages AS assistant ON assistant.message_id = episode.assistant_message_id
                JOIN companion_sessions AS session ON session.session_id = episode.session_id
                WHERE episode.episode_id = ?
                  AND episode.project_id = ?
                  AND episode.session_id = ?
                  AND episode.agent_id = ?
                  AND user.revision = episode.user_message_revision
                  AND assistant.revision = episode.assistant_message_revision
                  AND user.project_id = episode.project_id
                  AND assistant.project_id = episode.project_id
                  AND session.project_id = episode.project_id
                  AND user.session_id = episode.session_id
                  AND assistant.session_id = episode.session_id
                  AND user.context_epoch = episode.context_epoch
                  AND assistant.context_epoch = episode.context_epoch
                  AND user.request_id = episode.request_id
                  AND assistant.request_id = episode.request_id
                  AND user.role = 'user' AND user.status = 'completed'
                  AND assistant.role = 'assistant' AND assistant.status = 'completed'
                """,
                (episode_id, project_id, session_id, CONVERSATION_AGENT_ID),
            ).fetchone()
            return _conversation_episode_from_row(row) if row is not None else None
        finally:
            connection.close()

    def list_history(
        self, *, limit: int = 50, before: str | None = None, project_id: str | None = None,
    ) -> CompanionHistoryPage:
        page = self.list_messages(limit=limit, before=before, project_id=project_id)
        if not page.items:
            return CompanionHistoryPage((), page.next_cursor)
        connection = self._open_connection()
        try:
            placeholders = ",".join("?" for _ in page.items)
            rows = connection.execute(
                f"""
                SELECT message_id, dependent_kind, COUNT(*) AS item_count
                FROM companion_message_dependencies
                WHERE message_id IN ({placeholders}) AND state IN ('active', 'failed')
                GROUP BY message_id, dependent_kind
                """,
                tuple(message.message_id for message in page.items),
            ).fetchall()
            grouped: dict[str, dict[str, int]] = {}
            for row in rows:
                grouped.setdefault(str(row["message_id"]), {})[str(row["dependent_kind"])] = int(row["item_count"])
            items = tuple(
                CompanionHistoryItem(
                    message=message,
                    dependency_count=sum(grouped.get(message.message_id, {}).values()),
                    dependency_kinds=tuple(sorted(grouped.get(message.message_id, {}))),
                )
                for message in page.items
            )
            return CompanionHistoryPage(items, page.next_cursor)
        finally:
            connection.close()

    def register_message_dependency(
        self, *, message_id: str, dependent_kind: str, dependent_id: str, created_at: str,
    ) -> CompanionMessageDependency:
        _require_id("message_id", message_id)
        _require_id("dependent_id", dependent_id)
        if dependent_kind not in _DEPENDENT_KINDS:
            raise CompanionRepositoryError("message dependency kind is invalid")
        _require_utc("created_at", created_at)
        with self._transaction() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO companion_message_dependencies (
                        message_id, dependent_kind, dependent_id, state, created_at
                    ) VALUES (?, ?, ?, 'active', ?)
                    """,
                    (message_id, dependent_kind, dependent_id, created_at),
                )
            except sqlite3.IntegrityError as exc:
                raise CompanionConflict("message dependency identity conflicts with stored data") from exc
            row = connection.execute(
                "SELECT * FROM companion_message_dependencies WHERE message_id = ? AND dependent_kind = ? AND dependent_id = ?",
                (message_id, dependent_kind, dependent_id),
            ).fetchone()
            return _dependency_from_row(row)

    def begin_forget(self, message_id: str) -> tuple[CompanionForgetReceipt, tuple[CompanionMessageDependency, ...]]:
        _require_id("message_id", message_id)
        now = self._now_utc()
        with self._transaction() as connection:
            receipt_row = connection.execute(
                "SELECT * FROM companion_forget_receipts WHERE message_id = ?", (message_id,),
            ).fetchone()
            if receipt_row is not None and str(receipt_row["status"]) == "completed":
                return _forget_receipt_from_row(receipt_row, replayed=True), ()
            message = connection.execute(
                "SELECT session_id FROM companion_messages WHERE message_id = ?", (message_id,),
            ).fetchone()
            purge_resume = (
                message is None
                and receipt_row is not None
                and str(receipt_row["failed_step"] or "") == "purge:sqlite"
            )
            if message is None and not purge_resume:
                raise CompanionRepositoryError("companion message was not found")
            if receipt_row is None:
                receipt_id = f"forget:{hashlib.sha256(message_id.encode('utf-8')).hexdigest()}"
                connection.execute(
                    """
                    INSERT INTO companion_forget_receipts (
                        receipt_id, message_id, session_id, status, failed_step,
                        affected_json, attempts, started_at, updated_at, completed_at
                    ) VALUES (?, ?, ?, 'pending', NULL, '{}', 1, ?, ?, NULL)
                    """,
                    (receipt_id, message_id, str(message["session_id"]), now, now),
                )
            else:
                connection.execute(
                    """
                    UPDATE companion_forget_receipts
                    SET status = 'pending',
                        failed_step = CASE WHEN failed_step = 'purge:sqlite' THEN failed_step ELSE NULL END,
                        attempts = attempts + 1, updated_at = ?
                    WHERE message_id = ?
                    """,
                    (now, message_id),
                )
            receipt_row = connection.execute(
                "SELECT * FROM companion_forget_receipts WHERE message_id = ?", (message_id,),
            ).fetchone()
            dependencies = connection.execute(
                "SELECT * FROM companion_message_dependencies WHERE message_id = ? ORDER BY dependent_kind, dependent_id",
                (message_id,),
            ).fetchall()
            return _forget_receipt_from_row(receipt_row), tuple(_dependency_from_row(row) for row in dependencies)

    def get_forget_receipt(self, message_id: str) -> CompanionForgetReceipt | None:
        _require_id("message_id", message_id)
        self._ensure_initialized()
        connection = self._open_connection()
        try:
            row = connection.execute(
                "SELECT * FROM companion_forget_receipts WHERE message_id = ?", (message_id,),
            ).fetchone()
            return _forget_receipt_from_row(row) if row is not None else None
        finally:
            connection.close()

    def list_forget_recoveries(
        self, *, limit: int = 100, project_id: str | None = None,
    ) -> tuple[CompanionForgetReceipt, ...]:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise CompanionRepositoryError("forget recovery limit must be between 1 and 100")
        if project_id is not None:
            project_id = _require_project_id(project_id)
        self._ensure_initialized()
        connection = self._open_connection()
        try:
            project_clause = " AND session.project_id = ?" if project_id is not None else ""
            parameters: tuple[object, ...] = (project_id, limit) if project_id is not None else (limit,)
            rows = connection.execute(
                f"""
                SELECT receipt.* FROM companion_forget_receipts AS receipt
                LEFT JOIN companion_messages AS message ON message.message_id = receipt.message_id
                JOIN companion_sessions AS session ON session.session_id = receipt.session_id
                WHERE receipt.status = 'failed' AND message.message_id IS NULL
                {project_clause}
                ORDER BY receipt.updated_at DESC, receipt.receipt_id DESC LIMIT ?
                """,
                parameters,
            ).fetchall()
            return tuple(_forget_receipt_from_row(row) for row in rows)
        finally:
            connection.close()

    def mark_forget_dependency(self, dependency: CompanionMessageDependency, *, state: str) -> None:
        if state not in {"deleted", "withdrawn", "failed"}:
            raise CompanionRepositoryError("forget dependency terminal state is invalid")
        with self._transaction() as connection:
            updated = connection.execute(
                """
                UPDATE companion_message_dependencies SET state = ?
                WHERE message_id = ? AND dependent_kind = ? AND dependent_id = ?
                """,
                (state, dependency.message_id, dependency.dependent_kind, dependency.dependent_id),
            ).rowcount
            if updated != 1:
                raise CompanionRepositoryError("forget dependency was not found")

    def fail_forget(self, message_id: str, *, failed_step: str, affected: dict[str, int]) -> CompanionForgetReceipt:
        _require_id("message_id", message_id)
        _validate_affected(affected)
        if not isinstance(failed_step, str) or not failed_step or len(failed_step) > 160:
            raise CompanionRepositoryError("forget failed step is invalid")
        now = self._now_utc()
        with self._transaction() as connection:
            connection.execute(
                """
                UPDATE companion_forget_receipts
                SET status = 'failed', failed_step = ?, affected_json = ?, updated_at = ?
                WHERE message_id = ? AND status = 'pending'
                """,
                (failed_step, json.dumps(affected, sort_keys=True, separators=(",", ":")), now, message_id),
            )
            row = connection.execute(
                "SELECT * FROM companion_forget_receipts WHERE message_id = ?", (message_id,),
            ).fetchone()
            if row is None:
                raise CompanionRepositoryError("forget receipt was not found")
            return _forget_receipt_from_row(row)

    def finalize_forget(self, message_id: str, *, affected: dict[str, int]) -> CompanionForgetReceipt:
        _require_id("message_id", message_id)
        _validate_affected(affected)
        now = self._now_utc()
        with self._transaction() as connection:
            receipt = connection.execute(
                "SELECT * FROM companion_forget_receipts WHERE message_id = ?", (message_id,),
            ).fetchone()
            if receipt is None:
                raise CompanionRepositoryError("forget receipt was not found")
            if str(receipt["status"]) == "completed":
                return _forget_receipt_from_row(receipt, replayed=True)
            message = connection.execute(
                "SELECT request_id, session_id FROM companion_messages WHERE message_id = ?", (message_id,),
            ).fetchone()
            if message is not None:
                remaining = int(connection.execute(
                    "SELECT COUNT(*) FROM companion_message_dependencies WHERE message_id = ? AND state NOT IN ('deleted', 'withdrawn')",
                    (message_id,),
                ).fetchone()[0])
                if remaining:
                    raise CompanionRepositoryError("forget dependencies are incomplete")
                interaction_id = "chat:" + hashlib.sha256(str(message["request_id"]).encode("utf-8")).hexdigest()[:32]
                connection.execute("DELETE FROM companion_interaction_events WHERE event_id = ?", (interaction_id,))
                connection.execute("DELETE FROM companion_messages WHERE message_id = ?", (message_id,))
                connection.execute(
                    "UPDATE companion_sessions SET context_epoch = context_epoch + 1, revision = revision + 1 WHERE session_id = ?",
                    (str(message["session_id"]),),
                )
            elif str(receipt["failed_step"] or "") != "purge:sqlite":
                raise CompanionRepositoryError("forget target disappeared before commit")
            connection.execute(
                """
                UPDATE companion_forget_receipts
                SET status = 'pending', failed_step = 'purge:sqlite', affected_json = ?, updated_at = ?, completed_at = NULL
                WHERE message_id = ?
                """,
                (json.dumps(affected, sort_keys=True, separators=(",", ":")), now, message_id),
            )
        try:
            self._purge_deleted_pages()
        except (CompanionRepositoryError, OSError, sqlite3.Error) as exc:
            self.fail_forget(message_id, failed_step="purge:sqlite", affected=affected)
            raise CompanionRepositoryError("forget secure purge failed") from exc
        completed_at = self._now_utc()
        with self._transaction() as connection:
            connection.execute(
                """
                UPDATE companion_forget_receipts
                SET status = 'completed', failed_step = NULL, updated_at = ?, completed_at = ?
                WHERE message_id = ? AND status = 'pending' AND failed_step = 'purge:sqlite'
                """,
                (completed_at, completed_at, message_id),
            )
            row = connection.execute(
                "SELECT * FROM companion_forget_receipts WHERE message_id = ?", (message_id,),
            ).fetchone()
            if row is None or str(row["status"]) != "completed":
                raise CompanionRepositoryError("forget receipt completion failed")
            return _forget_receipt_from_row(row)

    def _purge_deleted_pages(self) -> None:
        connection = self._open_connection()
        try:
            result = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if result is None or int(result[0]) != 0:
                raise CompanionRepositoryError("companion WAL checkpoint is busy")
        finally:
            connection.close()

    def record_wallet_transaction(
        self,
        *,
        transaction_id: str,
        idempotency_key: str,
        reason: str,
        delta: int,
        created_at: str,
    ) -> CompanionWalletMutation:
        _require_id("transaction_id", transaction_id)
        _require_id("idempotency_key", idempotency_key)
        _require_utc("created_at", created_at)
        if not isinstance(delta, int) or isinstance(delta, bool) or delta == 0:
            raise CompanionRepositoryError("wallet delta must be a non-zero integer")
        if not 1 <= len(reason) <= 160:
            raise CompanionRepositoryError("wallet reason length is invalid")

        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM companion_wallet_ledger WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                actual = (
                    str(existing["transaction_id"]),
                    str(existing["reason"]),
                    int(existing["delta"]),
                    str(existing["created_at"]),
                )
                if actual != (transaction_id, reason, delta, created_at):
                    raise CompanionConflict("wallet idempotency key was reused with different input")
                return _wallet_from_row(existing, replayed=True)

            state = connection.execute(
                "SELECT coins, revision FROM companion_state WHERE id = 'current'"
            ).fetchone()
            if state is None:
                raise CompanionIntegrityError("companion state row is missing")
            snapshot_balance = int(state["coins"])
            ledger_balance = int(
                connection.execute(
                    "SELECT COALESCE(SUM(delta), 0) FROM companion_wallet_ledger"
                ).fetchone()[0]
            )
            if snapshot_balance != ledger_balance:
                raise CompanionIntegrityError("wallet snapshot does not match ledger")
            balance_after = snapshot_balance + delta
            if balance_after < 0:
                raise CompanionConflict("wallet balance cannot become negative")
            try:
                connection.execute(
                    """
                    INSERT INTO companion_wallet_ledger (
                        transaction_id, idempotency_key, reason, delta, balance_after, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (transaction_id, idempotency_key, reason, delta, balance_after, created_at),
                )
                self._after_wallet_ledger_insert(connection)
                connection.execute(
                    """
                    UPDATE companion_state
                    SET coins = ?, revision = revision + 1, updated_at = ?
                    WHERE id = 'current'
                    """,
                    (balance_after, created_at),
                )
            except sqlite3.IntegrityError as exc:
                raise CompanionConflict("wallet transaction identity conflicts with stored data") from exc
            return CompanionWalletMutation(
                transaction_id=transaction_id,
                idempotency_key=idempotency_key,
                reason=reason,
                delta=delta,
                balance_after=balance_after,
                created_at=created_at,
                replayed=False,
            )

    def wallet_integrity(self) -> CompanionWalletIntegrity:
        self._ensure_initialized()
        connection = self._open_connection()
        try:
            state = connection.execute(
                "SELECT coins FROM companion_state WHERE id = 'current'"
            ).fetchone()
            if state is None:
                raise CompanionIntegrityError("companion state row is missing")
            snapshot = int(state[0])
            ledger = int(
                connection.execute(
                    "SELECT COALESCE(SUM(delta), 0) FROM companion_wallet_ledger"
                ).fetchone()[0]
            )
            return CompanionWalletIntegrity(snapshot, ledger, snapshot == ledger)
        finally:
            connection.close()

    def list_wallet_entries(self, *, limit: int = 20) -> tuple[CompanionWalletEntry, ...]:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise CompanionRepositoryError("wallet entry limit is invalid")
        self._ensure_initialized()
        connection = self._open_connection()
        try:
            rows = connection.execute(
                "SELECT * FROM companion_wallet_ledger ORDER BY created_at DESC, transaction_id DESC LIMIT ?", (limit,)
            ).fetchall()
            return tuple(CompanionWalletEntry(
                transaction_id=str(row["transaction_id"]), reason=str(row["reason"]), delta=int(row["delta"]),
                balance_after=int(row["balance_after"]), created_at=str(row["created_at"]),
            ) for row in rows)
        finally:
            connection.close()

    def get_state_snapshot(self) -> CompanionStateSnapshot:
        self._ensure_initialized()
        connection = self._open_connection()
        try:
            row = connection.execute("SELECT * FROM companion_state WHERE id = 'current'").fetchone()
            if row is None:
                raise CompanionIntegrityError("companion state row is missing")
            return _state_snapshot_from_row(row)
        finally:
            connection.close()

    def has_state_action(self, *, command: str, local_day: str) -> bool:
        _require_id("command", command)
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", local_day):
            raise CompanionRepositoryError("state action local day is invalid")
        self._ensure_initialized()
        connection = self._open_connection()
        try:
            return connection.execute(
                "SELECT 1 FROM companion_state_actions WHERE command = ? AND local_day = ? LIMIT 1", (command, local_day)
            ).fetchone() is not None
        finally:
            connection.close()

    def apply_state_action(
        self, *, action_id: str, idempotency_key: str, command: str, subject_id: str | None,
        local_day: str, rule_version: int, affinity_delta: int, mood_delta: int, coin_delta: int,
        daily_limit: int, cooldown_seconds: int, affinity_thresholds: tuple[int, ...],
        sad_max: int, happy_min: int, wallet_reason: str, created_at: str,
    ) -> CompanionStateActionResult:
        for label, value in (("action_id", action_id), ("idempotency_key", idempotency_key), ("command", command)):
            _require_id(label, value)
        if subject_id is not None:
            _require_id("subject_id", subject_id)
        _require_utc("created_at", created_at)
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", local_day):
            raise CompanionRepositoryError("state action local day is invalid")
        if not isinstance(rule_version, int) or rule_version < 1:
            raise CompanionRepositoryError("state action rule version is invalid")
        if not isinstance(daily_limit, int) or daily_limit < 1 or not isinstance(cooldown_seconds, int) or cooldown_seconds < 0:
            raise CompanionRepositoryError("state action limits are invalid")
        if not affinity_thresholds or affinity_thresholds[0] != 0 or affinity_thresholds[-1] != 100:
            raise CompanionRepositoryError("affinity thresholds are invalid")
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM companion_state_actions WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
            if existing is not None:
                if str(existing["command"]) != command or existing["subject_id"] != subject_id:
                    raise CompanionConflict("state action idempotency key was reused with different input")
                return _state_action_from_row(existing, replayed=True)
            latest_day = connection.execute("SELECT MAX(local_day) FROM companion_state_actions").fetchone()[0]
            if latest_day is not None and str(latest_day) > local_day:
                raise CompanionConflict("state action local date moved backwards")
            daily_count = int(connection.execute(
                "SELECT COUNT(*) FROM companion_state_actions WHERE command = ? AND local_day = ?", (command, local_day)
            ).fetchone()[0])
            if daily_count >= daily_limit:
                raise CompanionConflict("state action daily limit reached")
            latest = connection.execute(
                "SELECT created_at FROM companion_state_actions WHERE command = ? ORDER BY created_at DESC LIMIT 1", (command,)
            ).fetchone()
            created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            if latest is not None:
                previous = datetime.fromisoformat(str(latest[0]).replace("Z", "+00:00"))
                if created < previous:
                    raise CompanionConflict("state action clock moved backwards")
                if (created - previous).total_seconds() < cooldown_seconds:
                    raise CompanionConflict("state action is cooling down")
            row = connection.execute("SELECT * FROM companion_state WHERE id = 'current'").fetchone()
            if row is None:
                raise CompanionIntegrityError("companion state row is missing")
            before = _state_snapshot_from_row(row)
            ledger_balance = int(connection.execute("SELECT COALESCE(SUM(delta), 0) FROM companion_wallet_ledger").fetchone()[0])
            if before.coins != ledger_balance:
                raise CompanionIntegrityError("wallet snapshot does not match ledger")
            affinity = min(100, max(0, before.affinity + affinity_delta))
            mood_score = min(100, max(-100, before.mood_score + mood_delta))
            coins = before.coins + coin_delta
            if coins < 0:
                raise CompanionConflict("wallet balance cannot become negative")
            level = max(index for index, threshold in enumerate(affinity_thresholds) if affinity >= threshold)
            mood = "sad" if mood_score <= sad_max else "happy" if mood_score >= happy_min else "normal"
            unlocks: list[int] = []
            for threshold in affinity_thresholds[1:]:
                if before.affinity < threshold <= affinity:
                    cursor = connection.execute(
                        "INSERT OR IGNORE INTO companion_unlock_events (unlock_id, affinity_threshold, rule_version, created_at) VALUES (?, ?, ?, ?)",
                        (f"affinity:{threshold}", threshold, rule_version, created_at),
                    )
                    if cursor.rowcount == 1:
                        unlocks.append(threshold)
            state_revision = before.revision + 1
            connection.execute(
                "UPDATE companion_state SET affinity = ?, affinity_level = ?, mood_score = ?, mood = ?, coins = ?, revision = ?, updated_at = ? WHERE id = 'current'",
                (affinity, level, mood_score, mood, coins, state_revision, created_at),
            )
            if coin_delta:
                connection.execute(
                    "INSERT INTO companion_wallet_ledger (transaction_id, idempotency_key, reason, delta, balance_after, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (f"wallet:{action_id}", f"state:{idempotency_key}", wallet_reason, coin_delta, coins, created_at),
                )
            connection.execute(
                """INSERT INTO companion_state_actions (
                    action_id, idempotency_key, command, subject_id, local_day, rule_version,
                    affinity_before, affinity_after, affinity_level_after, mood_before, mood_after, mood_label_after, coins_before, coins_after,
                    outfit_id, background_id, state_revision, unlocks_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (action_id, idempotency_key, command, subject_id, local_day, rule_version,
                 before.affinity, affinity, level, before.mood_score, mood_score, mood, before.coins, coins,
                 before.outfit_id, before.background_id, state_revision, json.dumps(unlocks), created_at),
            )
            saved = connection.execute("SELECT * FROM companion_state_actions WHERE action_id = ?", (action_id,)).fetchone()
            return _state_action_from_row(saved, replayed=False)

    def reconcile_daily_mood(
        self, *, local_day: str, rule_version: int, step: int, max_days: int,
        sad_max: int, happy_min: int, created_at: str,
    ) -> CompanionStateActionResult:
        """Move persistent mood toward neutral once per observed local calendar day.

        The latest append-only action is the durable cursor. Cursor discovery,
        state mutation, and the new receipt share one immediate transaction so
        concurrent processes cannot apply the same day twice.
        """
        _require_utc("created_at", created_at)
        if not isinstance(local_day, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", local_day) is None:
            raise CompanionRepositoryError("daily mood local day is invalid")
        try:
            today = date.fromisoformat(local_day)
        except ValueError as exc:
            raise CompanionRepositoryError("daily mood local day is invalid") from exc
        if not isinstance(rule_version, int) or isinstance(rule_version, bool) or rule_version < 1:
            raise CompanionRepositoryError("daily mood rule version is invalid")
        if not isinstance(step, int) or isinstance(step, bool) or not 1 <= step <= 10:
            raise CompanionRepositoryError("daily mood step is invalid")
        if not isinstance(max_days, int) or isinstance(max_days, bool) or not 1 <= max_days <= 365:
            raise CompanionRepositoryError("daily mood catch-up limit is invalid")

        command = "daily_mood_decay"
        idempotency_key = f"daily_mood_decay:{local_day}"
        digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:32]
        action_id = f"act_{digest}"
        with self._transaction() as connection:
            latest_day = connection.execute(
                "SELECT MAX(local_day) FROM companion_state_actions"
            ).fetchone()[0]
            latest_created = connection.execute(
                "SELECT created_at FROM companion_state_actions ORDER BY created_at DESC, action_id DESC LIMIT 1"
            ).fetchone()
            if latest_day is not None and str(latest_day) > local_day:
                raise CompanionConflict("daily mood local date moved backwards")
            if latest_created is not None:
                created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                previous_created = datetime.fromisoformat(str(latest_created["created_at"]).replace("Z", "+00:00"))
                if created < previous_created:
                    raise CompanionConflict("daily mood clock moved backwards")
            existing = connection.execute(
                "SELECT * FROM companion_state_actions WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
            if existing is not None:
                if str(existing["command"]) != command or existing["subject_id"] is not None:
                    raise CompanionConflict("daily mood idempotency key was reused with different input")
                return _state_action_from_row(existing, replayed=True)

            latest = connection.execute(
                "SELECT local_day FROM companion_state_actions WHERE command = ? ORDER BY local_day DESC LIMIT 1",
                (command,),
            ).fetchone()
            elapsed_days = 0
            if latest is not None:
                try:
                    previous_day = date.fromisoformat(str(latest["local_day"]))
                except ValueError as exc:
                    raise CompanionIntegrityError("stored daily mood cursor is invalid") from exc
                if today < previous_day:
                    raise CompanionConflict("daily mood local date moved backwards")
                elapsed_days = min((today - previous_day).days, max_days)

            row = connection.execute("SELECT * FROM companion_state WHERE id = 'current'").fetchone()
            if row is None:
                raise CompanionIntegrityError("companion state row is missing")
            before = _state_snapshot_from_row(row)
            ledger_balance = int(connection.execute(
                "SELECT COALESCE(SUM(delta), 0) FROM companion_wallet_ledger"
            ).fetchone()[0])
            if before.coins != ledger_balance:
                raise CompanionIntegrityError("wallet snapshot does not match ledger")

            magnitude = min(abs(before.mood_score), elapsed_days * step)
            mood_delta = -magnitude if before.mood_score > 0 else magnitude if before.mood_score < 0 else 0
            mood_score = before.mood_score + mood_delta
            mood = "sad" if mood_score <= sad_max else "happy" if mood_score >= happy_min else "normal"
            state_revision = before.revision + 1 if mood_delta else before.revision
            if mood_delta:
                connection.execute(
                    "UPDATE companion_state SET mood_score=?, mood=?, revision=?, updated_at=? WHERE id='current'",
                    (mood_score, mood, state_revision, created_at),
                )
            connection.execute(
                """INSERT INTO companion_state_actions (
                    action_id, idempotency_key, command, subject_id, local_day, rule_version,
                    affinity_before, affinity_after, affinity_level_after, mood_before, mood_after,
                    mood_label_after, coins_before, coins_after, outfit_id, background_id,
                    state_revision, unlocks_json, created_at
                ) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '[]', ?)""",
                (action_id, idempotency_key, command, local_day, rule_version,
                 before.affinity, before.affinity, before.affinity_level, before.mood_score, mood_score,
                 mood, before.coins, before.coins, before.outfit_id, before.background_id,
                 state_revision, created_at),
            )
            saved = connection.execute(
                "SELECT * FROM companion_state_actions WHERE action_id = ?", (action_id,)
            ).fetchone()
            if saved is None:
                raise CompanionIntegrityError("daily mood receipt is missing")
            return _state_action_from_row(saved, replayed=False)

    def record_interaction(self, *, event_id: str, kind: str) -> CompanionInteractionEvent:
        """Persist one fixed-kind interaction without mutating affinity or wallet state."""
        _require_id("event_id", event_id)
        if kind not in _INTERACTION_KINDS:
            raise CompanionRepositoryError("interaction kind is invalid")
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM companion_interaction_events WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            if existing is not None:
                if str(existing["kind"]) != kind or str(existing["value_json"]) != "{}" or existing["expires_at"] is not None:
                    raise CompanionConflict("interaction event id was reused with different input")
                return _interaction_from_row(existing, replayed=True)
            occurred_at = self._now_utc()
            connection.execute(
                """
                INSERT INTO companion_interaction_events (
                    event_id, kind, value_json, occurred_at, expires_at
                ) VALUES (?, ?, '{}', ?, NULL)
                """,
                (event_id, kind, occurred_at),
            )
            created = connection.execute(
                "SELECT * FROM companion_interaction_events WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            if created is None:
                raise CompanionIntegrityError("created interaction event is missing")
            return _interaction_from_row(created, replayed=False)

    def set_inventory_quantity(
        self,
        *,
        item_id: str,
        quantity: int,
        expected_revision: int,
        updated_at: str,
    ) -> CompanionInventoryItem:
        _require_id("item_id", item_id)
        _require_non_negative("quantity", quantity)
        _require_non_negative("expected_revision", expected_revision)
        _require_utc("updated_at", updated_at)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM companion_inventory WHERE item_id = ?",
                (item_id,),
            ).fetchone()
            actual_revision = int(row["revision"]) if row is not None else 0
            if actual_revision != expected_revision:
                raise CompanionConflict(
                    f"inventory expected revision {expected_revision}, found {actual_revision}"
                )
            revision = actual_revision + 1
            connection.execute(
                """
                INSERT INTO companion_inventory (item_id, quantity, revision, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(item_id) DO UPDATE SET
                    quantity = excluded.quantity,
                    revision = excluded.revision,
                    updated_at = excluded.updated_at
                """,
                (item_id, quantity, revision, updated_at),
            )
            return CompanionInventoryItem(item_id, quantity, revision, updated_at)

    def get_inventory_item(self, item_id: str) -> CompanionInventoryItem | None:
        _require_id("item_id", item_id)
        self._ensure_initialized()
        connection = self._open_connection()
        try:
            row = connection.execute(
                "SELECT * FROM companion_inventory WHERE item_id = ?",
                (item_id,),
            ).fetchone()
            if row is None:
                return None
            return CompanionInventoryItem(
                item_id=str(row["item_id"]),
                quantity=int(row["quantity"]),
                revision=int(row["revision"]),
                updated_at=str(row["updated_at"]),
            )
        finally:
            connection.close()

    def _after_wallet_ledger_insert(self, connection: sqlite3.Connection) -> None:
        """Internal crash-simulation seam; production implementation is a no-op."""

    def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        self.initialize()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._maintenance_lock:
            self._ensure_initialized()
            connection = self._open_connection()
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.execute("COMMIT")
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
            finally:
                connection.close()

    def _open_connection(self) -> sqlite3.Connection:
        _require_database_path(self.database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self.database_path,
            timeout=_BUSY_TIMEOUT_MS / 1000,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        try:
            connection.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
            connection.execute("PRAGMA secure_delete=ON")
            mode = None
            for attempt in range(6):
                try:
                    mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()
                    break
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc).lower() or attempt == 5:
                        raise
                    time.sleep(0.05 * (attempt + 1))
            if mode is None or str(mode[0]).lower() != "wal":
                raise CompanionRepositoryError("companion database requires WAL mode")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA synchronous=FULL")
            return connection
        except Exception:
            connection.close()
            raise

    def _schema_status(
        self,
        connection: sqlite3.Connection,
        *,
        applied: tuple[int, ...],
    ) -> CompanionSchemaStatus:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version != SCHEMA_VERSION:
            raise CompanionIntegrityError("companion database schema version is incomplete")
        journal = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        foreign_keys = int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) == 1
        synchronous_value = int(connection.execute("PRAGMA synchronous").fetchone()[0])
        synchronous = {0: "off", 1: "normal", 2: "full", 3: "extra"}.get(synchronous_value, "unknown")
        busy_timeout = int(connection.execute("PRAGMA busy_timeout").fetchone()[0])
        return CompanionSchemaStatus(
            schema_version=version,
            applied_migrations=applied,
            journal_mode=journal,
            foreign_keys_enabled=foreign_keys,
            synchronous_mode=synchronous,
            busy_timeout_ms=busy_timeout,
        )

    def _now_utc(self) -> str:
        value = self._now()
        if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
            raise CompanionRepositoryError("repository clock must return UTC time")
        return value.isoformat()


def _session_from_row(row: sqlite3.Row) -> CompanionSession:
    return CompanionSession(
        session_id=str(row["session_id"]),
        context_epoch=int(row["context_epoch"]),
        project_id=str(row["project_id"]),
        prompt_revision=int(row["prompt_revision"]),
        profile_revision=int(row["profile_revision"]),
        started_at=str(row["started_at"]),
        closed_at=str(row["closed_at"]) if row["closed_at"] is not None else None,
        revision=int(row["revision"]),
    )


def _profile_from_row(row: sqlite3.Row) -> CompanionMasterProfile:
    try:
        extra = json.loads(str(row["extra_json"]))
    except (TypeError, json.JSONDecodeError) as exc:
        raise CompanionIntegrityError("stored profile extra data is invalid") from exc
    if not isinstance(extra, dict) or set(extra) != {"custom_notes"} or not isinstance(extra["custom_notes"], str):
        raise CompanionIntegrityError("stored profile extra data is invalid")
    return CompanionMasterProfile(
        profile_id=str(row["id"]),
        nickname=str(row["nickname"]),
        birthday=str(row["birthday"]) if row["birthday"] is not None else None,
        oc_address=str(row["oc_address"]),
        relationship=str(row["relationship"]),
        custom_notes=extra["custom_notes"],
        revision=int(row["revision"]),
        updated_at=str(row["updated_at"]),
    )


def _prompt_binding_from_row(row: sqlite3.Row) -> CompanionPromptBinding:
    return CompanionPromptBinding(
        active_prompt_id=str(row["active_prompt_id"]),
        activation_revision=int(row["activation_revision"]),
        unit_revision=int(row["unit_revision"]),
        revision=int(row["revision"]),
        updated_at=str(row["updated_at"]),
    )


def _message_from_row(row: sqlite3.Row) -> CompanionMessage:
    try:
        memory_review = json.loads(str(row["memory_review_json"]))
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise CompanionIntegrityError("stored message memory review is invalid") from exc
    if not isinstance(memory_review, dict):
        raise CompanionIntegrityError("stored message memory review is invalid")
    try:
        _memory_review_json(memory_review)
    except CompanionRepositoryError as exc:
        raise CompanionIntegrityError("stored message memory review is invalid") from exc
    return CompanionMessage(
        message_id=str(row["message_id"]),
        request_id=str(row["request_id"]),
        session_id=str(row["session_id"]),
        context_epoch=int(row["context_epoch"]),
        project_id=str(row["project_id"]),
        role=str(row["role"]),
        status=str(row["status"]),
        content=str(row["content"]),
        created_at=str(row["created_at"]),
        provider_mode=str(row["provider_mode"]),
        revision=int(row["revision"]),
        memory_review=memory_review,
    )


def _upsert_conversation_episode(connection: sqlite3.Connection, *, assistant_message_id: str) -> bool:
    """Write-side projection maintenance for a completed assistant reply.

    The projection is still disposable: rebuild_conversation_episodes is its
    recovery path, while this keeps normal reads fresh without a rebuild.
    """

    row = connection.execute(
        """
        SELECT
            user.message_id AS user_message_id,
            user.revision AS user_message_revision,
            user.content AS user_content,
            assistant.message_id AS assistant_message_id,
            assistant.revision AS assistant_message_revision,
            assistant.content AS assistant_content,
            assistant.project_id AS project_id,
            assistant.session_id AS session_id,
            assistant.context_epoch AS context_epoch,
            assistant.request_id AS request_id,
            assistant.created_at AS occurred_at
        FROM companion_messages AS assistant
        JOIN companion_messages AS user
          ON user.request_id = assistant.request_id
         AND user.session_id = assistant.session_id
         AND user.context_epoch = assistant.context_epoch
         AND user.project_id = assistant.project_id
        JOIN companion_sessions AS session
          ON session.session_id = assistant.session_id
         AND session.project_id = assistant.project_id
        WHERE assistant.message_id = ?
          AND assistant.role = 'assistant' AND assistant.status = 'completed'
          AND user.role = 'user' AND user.status = 'completed'
        """,
        (assistant_message_id,),
    ).fetchone()
    if row is None:
        return False
    user_message_id = str(row["user_message_id"])
    connection.execute(
        """
        INSERT INTO companion_conversation_episodes (
            episode_id, agent_id, project_id, session_id, context_epoch, request_id,
            user_message_id, user_message_revision, assistant_message_id,
            assistant_message_revision, summary, occurred_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(agent_id, assistant_message_id) DO UPDATE SET
            episode_id = excluded.episode_id,
            project_id = excluded.project_id,
            session_id = excluded.session_id,
            context_epoch = excluded.context_epoch,
            request_id = excluded.request_id,
            user_message_id = excluded.user_message_id,
            user_message_revision = excluded.user_message_revision,
            assistant_message_revision = excluded.assistant_message_revision,
            summary = excluded.summary,
            occurred_at = excluded.occurred_at
        """,
        (
            episode_id_for(user_message_id, str(row["assistant_message_id"])), CONVERSATION_AGENT_ID,
            str(row["project_id"]), str(row["session_id"]), int(row["context_epoch"]), str(row["request_id"]),
            user_message_id, int(row["user_message_revision"]), str(row["assistant_message_id"]),
            int(row["assistant_message_revision"]),
            summarize_conversation_pair(str(row["user_content"]), str(row["assistant_content"])),
            str(row["occurred_at"]),
        ),
    )
    return True


def _conversation_episode_from_row(row: sqlite3.Row) -> ConversationEpisodeProjection:
    return ConversationEpisodeProjection(
        episode_id=str(row["episode_id"]),
        agent_id=str(row["agent_id"]),
        project_id=str(row["project_id"]),
        session_id=str(row["session_id"]),
        context_epoch=int(row["context_epoch"]),
        request_id=str(row["request_id"]),
        user_message_id=str(row["user_message_id"]),
        user_message_revision=int(row["user_message_revision"]),
        assistant_message_id=str(row["assistant_message_id"]),
        assistant_message_revision=int(row["assistant_message_revision"]),
        summary=str(row["summary"]),
        occurred_at=str(row["occurred_at"]),
    )


def _memory_review_json(value: Mapping[str, object] | None) -> str:
    if value is None or not value:
        return "{}"
    if set(value) != {"review_scope", "status", "matched_count", "memory_ids", "generated"}:
        raise CompanionRepositoryError("message memory review fields are invalid")
    scope = value.get("review_scope")
    status = value.get("status")
    matched_count = value.get("matched_count")
    memory_ids = value.get("memory_ids")
    generated = value.get("generated")
    if scope not in {
        "conversation_context", "today_memory_review", "weekly_memory_review", "memory_topic_discussion",
    }:
        raise CompanionRepositoryError("message memory review scope is invalid")
    if status not in {"recalled", "empty", "degraded", "disabled"}:
        raise CompanionRepositoryError("message memory review status is invalid")
    if not isinstance(matched_count, int) or isinstance(matched_count, bool) or not 0 <= matched_count <= 4:
        raise CompanionRepositoryError("message memory review count is invalid")
    if not isinstance(generated, bool) or not isinstance(memory_ids, (list, tuple)):
        raise CompanionRepositoryError("message memory review evidence is invalid")
    ids = [item for item in memory_ids if isinstance(item, str)]
    if (
        len(ids) != len(memory_ids)
        or len(ids) > 4
        or len(ids) != len(set(ids))
        or any(_MEMORY_ID.fullmatch(item) is None for item in ids)
        or (not generated and ids)
        or (generated and len(ids) != matched_count)
    ):
        raise CompanionRepositoryError("message memory review evidence is invalid")
    return json.dumps(
        {
            "generated": generated,
            "matched_count": matched_count,
            "memory_ids": ids,
            "review_scope": scope,
            "status": status,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _wallet_from_row(row: sqlite3.Row, *, replayed: bool) -> CompanionWalletMutation:
    return CompanionWalletMutation(
        transaction_id=str(row["transaction_id"]),
        idempotency_key=str(row["idempotency_key"]),
        reason=str(row["reason"]),
        delta=int(row["delta"]),
        balance_after=int(row["balance_after"]),
        created_at=str(row["created_at"]),
        replayed=replayed,
    )


def _state_snapshot_from_row(row: sqlite3.Row) -> CompanionStateSnapshot:
    return CompanionStateSnapshot(
        affinity=int(row["affinity"]), affinity_level=int(row["affinity_level"]),
        mood_score=int(row["mood_score"]), mood=str(row["mood"]), coins=int(row["coins"]),
        outfit_id=str(row["outfit_id"]), background_id=str(row["background_id"]),
        revision=int(row["revision"]), updated_at=str(row["updated_at"]),
    )


def _state_action_from_row(row: sqlite3.Row, *, replayed: bool) -> CompanionStateActionResult:
    try:
        unlocks = tuple(int(value) for value in json.loads(str(row["unlocks_json"])))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CompanionIntegrityError("stored state action unlocks are invalid") from exc
    snapshot = CompanionStateSnapshot(
        affinity=int(row["affinity_after"]),
        affinity_level=int(row["affinity_level_after"]),
        mood_score=int(row["mood_after"]),
        mood=str(row["mood_label_after"]),
        coins=int(row["coins_after"]),
        outfit_id=str(row["outfit_id"]), background_id=str(row["background_id"]),
        revision=int(row["state_revision"]), updated_at=str(row["created_at"]),
    )
    return CompanionStateActionResult(
        action_id=str(row["action_id"]), idempotency_key=str(row["idempotency_key"]),
        command=str(row["command"]), subject_id=str(row["subject_id"]) if row["subject_id"] is not None else None,
        local_day=str(row["local_day"]), rule_version=int(row["rule_version"]), snapshot=snapshot,
        affinity_delta=int(row["affinity_after"]) - int(row["affinity_before"]),
        mood_delta=int(row["mood_after"]) - int(row["mood_before"]),
        coin_delta=int(row["coins_after"]) - int(row["coins_before"]),
        unlocks=() if replayed else unlocks, created_at=str(row["created_at"]), replayed=replayed,
    )


def _interaction_from_row(row: sqlite3.Row, *, replayed: bool) -> CompanionInteractionEvent:
    return CompanionInteractionEvent(
        event_id=str(row["event_id"]),
        kind=str(row["kind"]),
        occurred_at=str(row["occurred_at"]),
        replayed=replayed,
    )


def _dependency_from_row(row: sqlite3.Row) -> CompanionMessageDependency:
    return CompanionMessageDependency(
        message_id=str(row["message_id"]),
        dependent_kind=str(row["dependent_kind"]),
        dependent_id=str(row["dependent_id"]),
        state=str(row["state"]),
        created_at=str(row["created_at"]),
    )


def _forget_receipt_from_row(row: sqlite3.Row, *, replayed: bool = False) -> CompanionForgetReceipt:
    try:
        affected = json.loads(str(row["affected_json"]))
    except (TypeError, json.JSONDecodeError) as exc:
        raise CompanionIntegrityError("stored forget receipt counts are invalid") from exc
    _validate_affected(affected)
    return CompanionForgetReceipt(
        receipt_id=str(row["receipt_id"]),
        message_id=str(row["message_id"]),
        session_id=str(row["session_id"]),
        status=str(row["status"]),
        failed_step=str(row["failed_step"]) if row["failed_step"] is not None else None,
        affected=affected,
        attempts=int(row["attempts"]),
        started_at=str(row["started_at"]),
        updated_at=str(row["updated_at"]),
        completed_at=str(row["completed_at"]) if row["completed_at"] is not None else None,
        replayed=replayed,
    )


def _setting_from_row(row: sqlite3.Row) -> CompanionSetting:
    try:
        payload = json.loads(str(row["payload_json"]))
    except (TypeError, json.JSONDecodeError) as exc:
        raise CompanionIntegrityError("stored setting payload is invalid") from exc
    if not isinstance(payload, dict):
        raise CompanionIntegrityError("stored setting payload is invalid")
    return CompanionSetting(
        setting_id=str(row["id"]), payload=payload, revision=int(row["revision"]), updated_at=str(row["updated_at"])
    )


def _reminder_from_row(row: sqlite3.Row) -> CompanionReminder:
    try:
        schedule = json.loads(str(row["schedule_json"]))
    except (TypeError, json.JSONDecodeError) as exc:
        raise CompanionIntegrityError("stored reminder schedule is invalid") from exc
    if not isinstance(schedule, dict):
        raise CompanionIntegrityError("stored reminder schedule is invalid")
    return CompanionReminder(
        reminder_id=str(row["reminder_id"]), schedule=schedule, advance_minutes=int(row["advance_minutes"]),
        next_fire_at=str(row["next_fire_at"]) if row["next_fire_at"] is not None else None,
        ack_state=str(row["ack_state"]), revision=int(row["revision"]), updated_at=str(row["updated_at"]),
    )


def _reminder_occurrence_from_row(row: sqlite3.Row) -> CompanionReminderOccurrence:
    return CompanionReminderOccurrence(
        occurrence_id=str(row["occurrence_id"]), reminder_id=str(row["reminder_id"]),
        scheduled_for=str(row["scheduled_for"]), fire_at=str(row["fire_at"]), phase=str(row["phase"]),
        state=str(row["state"]), snooze_until=str(row["snooze_until"]) if row["snooze_until"] is not None else None,
        revision=int(row["revision"]), created_at=str(row["created_at"]), updated_at=str(row["updated_at"]),
    )


def _joined_reminder_from_row(row: sqlite3.Row) -> CompanionReminder:
    try:
        schedule = json.loads(str(row["schedule_json"]))
    except (TypeError, json.JSONDecodeError) as exc:
        raise CompanionIntegrityError("stored reminder schedule is invalid") from exc
    return CompanionReminder(
        reminder_id=str(row["reminder_id"]), schedule=schedule, advance_minutes=int(row["advance_minutes"]),
        next_fire_at=str(row["next_fire_at"]) if row["next_fire_at"] is not None else None,
        ack_state=str(row["ack_state"]), revision=int(row["reminder_revision"]), updated_at=str(row["reminder_updated_at"]),
    )


def _validate_affected(value: object) -> None:
    if not isinstance(value, dict) or any(
        not isinstance(key, str) or not key or not isinstance(count, int) or isinstance(count, bool) or count < 0
        for key, count in value.items()
    ):
        raise CompanionRepositoryError("forget affected counts are invalid")


def _encode_cursor(created_at: str, message_id: str) -> str:
    payload = json.dumps(
        {"v": 1, "created_at": created_at, "message_id": message_id},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_cursor(value: str) -> tuple[str, str]:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise CompanionRepositoryError("message cursor is invalid")
    try:
        padded = value + "=" * (-len(value) % 4)
        payload = json.loads(base64.b64decode(padded, altchars=b"-_", validate=True))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CompanionRepositoryError("message cursor is invalid") from exc
    if not isinstance(payload, dict) or set(payload) != {"v", "created_at", "message_id"}:
        raise CompanionRepositoryError("message cursor is invalid")
    if payload["v"] != 1 or not isinstance(payload["created_at"], str) or not isinstance(payload["message_id"], str):
        raise CompanionRepositoryError("message cursor is invalid")
    _require_utc("cursor.created_at", payload["created_at"])
    _require_id("cursor.message_id", payload["message_id"])
    return payload["created_at"], payload["message_id"]


def _require_id(label: str, value: object) -> None:
    if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
        raise CompanionRepositoryError(f"{label} is invalid")


def _require_project_id(value: object) -> str:
    if not isinstance(value, str):
        raise CompanionRepositoryError("message project id is invalid")
    project_id = value.strip()
    if not project_id or len(project_id) > 191 or _PROJECT_ID_CONTROL.search(project_id):
        raise CompanionRepositoryError("message project id is invalid")
    return project_id


def _require_positive(label: str, value: object) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise CompanionRepositoryError(f"{label} must be a positive integer")


def _require_non_negative(label: str, value: object) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise CompanionRepositoryError(f"{label} must be a non-negative integer")


def _require_utc(label: str, value: object) -> None:
    if not isinstance(value, str):
        raise CompanionRepositoryError(f"{label} must be UTC ISO 8601")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CompanionRepositoryError(f"{label} must be UTC ISO 8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise CompanionRepositoryError(f"{label} must be UTC ISO 8601")


def _require_database_path(path: Path) -> None:
    if path.is_symlink() or (path.exists() and path.is_dir()):
        raise CompanionRepositoryError("companion database path must be a regular file")
    for parent in path.parents:
        if parent.exists() and parent.is_symlink():
            raise CompanionRepositoryError("companion database path cannot traverse a symlink")
