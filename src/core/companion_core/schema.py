from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass

from .errors import CompanionIntegrityError, CompanionSchemaTooNew


SCHEMA_VERSION = 11


@dataclass(frozen=True, slots=True)
class CompanionMigration:
    version: int
    migration_id: str
    statements: tuple[str, ...]

    @property
    def checksum(self) -> str:
        payload = "\n-- statement --\n".join(statement.strip() for statement in self.statements)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


MIGRATION_1 = CompanionMigration(
    version=1,
    migration_id="001_companion_authority",
    statements=(
        """
        CREATE TABLE companion_settings (
            id TEXT PRIMARY KEY,
            revision INTEGER NOT NULL CHECK (revision > 0),
            payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE companion_master_profile (
            id TEXT PRIMARY KEY,
            revision INTEGER NOT NULL CHECK (revision > 0),
            nickname TEXT NOT NULL CHECK (length(nickname) <= 120),
            birthday TEXT,
            oc_address TEXT NOT NULL CHECK (length(oc_address) <= 120),
            relationship TEXT NOT NULL CHECK (length(relationship) <= 1000),
            extra_json TEXT NOT NULL CHECK (json_valid(extra_json)),
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE companion_prompt_binding (
            id TEXT PRIMARY KEY CHECK (id = 'active'),
            active_prompt_id TEXT NOT NULL,
            activation_revision INTEGER NOT NULL CHECK (activation_revision > 0),
            unit_revision INTEGER NOT NULL CHECK (unit_revision > 0),
            revision INTEGER NOT NULL CHECK (revision > 0),
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE companion_sessions (
            session_id TEXT PRIMARY KEY,
            context_epoch INTEGER NOT NULL CHECK (context_epoch > 0),
            prompt_revision INTEGER NOT NULL CHECK (prompt_revision > 0),
            profile_revision INTEGER NOT NULL CHECK (profile_revision > 0),
            started_at TEXT NOT NULL,
            closed_at TEXT,
            revision INTEGER NOT NULL CHECK (revision > 0)
        )
        """,
        """
        CREATE TABLE companion_messages (
            message_id TEXT PRIMARY KEY,
            request_id TEXT NOT NULL,
            session_id TEXT NOT NULL REFERENCES companion_sessions(session_id) ON DELETE CASCADE,
            context_epoch INTEGER NOT NULL CHECK (context_epoch > 0),
            role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system_event')),
            status TEXT NOT NULL CHECK (status IN ('completed', 'failed', 'cancelled')),
            content TEXT NOT NULL,
            created_at TEXT NOT NULL,
            provider_mode TEXT NOT NULL CHECK (provider_mode IN ('local', 'remote', 'none')),
            revision INTEGER NOT NULL CHECK (revision > 0),
            UNIQUE (request_id, role)
        )
        """,
        "CREATE INDEX companion_messages_timeline ON companion_messages(created_at DESC, message_id DESC)",
        "CREATE INDEX companion_messages_session_epoch ON companion_messages(session_id, context_epoch, created_at, message_id)",
        """
        CREATE TABLE companion_message_dependencies (
            message_id TEXT NOT NULL REFERENCES companion_messages(message_id) ON DELETE CASCADE,
            dependent_kind TEXT NOT NULL CHECK (dependent_kind IN ('context', 'cache', 'fts', 'vector', 'candidate', 'summary', 'published_memory')),
            dependent_id TEXT NOT NULL,
            state TEXT NOT NULL CHECK (state IN ('active', 'deleted', 'withdrawn', 'failed')),
            created_at TEXT NOT NULL,
            PRIMARY KEY (message_id, dependent_kind, dependent_id)
        )
        """,
        """
        CREATE TABLE companion_state (
            id TEXT PRIMARY KEY CHECK (id = 'current'),
            affinity INTEGER NOT NULL CHECK (affinity BETWEEN 0 AND 100),
            affinity_level INTEGER NOT NULL CHECK (affinity_level BETWEEN 0 AND 4),
            mood_score INTEGER NOT NULL CHECK (mood_score BETWEEN -100 AND 100),
            mood TEXT NOT NULL CHECK (mood IN ('happy', 'normal', 'sad')),
            coins INTEGER NOT NULL CHECK (coins >= 0),
            outfit_id TEXT NOT NULL,
            background_id TEXT NOT NULL,
            revision INTEGER NOT NULL CHECK (revision > 0),
            updated_at TEXT NOT NULL
        )
        """,
        """
        INSERT INTO companion_state (
            id, affinity, affinity_level, mood_score, mood, coins,
            outfit_id, background_id, revision, updated_at
        ) VALUES (
            'current', 0, 0, 0, 'normal', 0,
            'default', 'default', 1, '1970-01-01T00:00:00+00:00'
        )
        """,
        """
        CREATE TABLE companion_wallet_ledger (
            transaction_id TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL UNIQUE,
            reason TEXT NOT NULL CHECK (length(reason) BETWEEN 1 AND 160),
            delta INTEGER NOT NULL CHECK (delta != 0),
            balance_after INTEGER NOT NULL CHECK (balance_after >= 0),
            created_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX companion_wallet_created ON companion_wallet_ledger(created_at, transaction_id)",
        """
        CREATE TABLE companion_inventory (
            item_id TEXT PRIMARY KEY,
            quantity INTEGER NOT NULL CHECK (quantity >= 0),
            revision INTEGER NOT NULL CHECK (revision > 0),
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE companion_reminders (
            reminder_id TEXT PRIMARY KEY,
            schedule_json TEXT NOT NULL CHECK (json_valid(schedule_json)),
            advance_minutes INTEGER NOT NULL CHECK (advance_minutes BETWEEN 0 AND 10080),
            next_fire_at TEXT,
            ack_state TEXT NOT NULL CHECK (ack_state IN ('pending', 'acknowledged', 'snoozed', 'completed', 'cancelled')),
            revision INTEGER NOT NULL CHECK (revision > 0),
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE companion_interaction_events (
            event_id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            value_json TEXT NOT NULL CHECK (json_valid(value_json)),
            occurred_at TEXT NOT NULL,
            expires_at TEXT
        )
        """,
        "CREATE INDEX companion_interactions_time ON companion_interaction_events(occurred_at, event_id)",
        """
        CREATE TABLE companion_random_events (
            event_id TEXT PRIMARY KEY,
            prompt_ref TEXT NOT NULL,
            options_json TEXT NOT NULL CHECK (json_valid(options_json)),
            selected_option TEXT,
            result_json TEXT CHECK (result_json IS NULL OR json_valid(result_json)),
            state TEXT NOT NULL CHECK (state IN ('offered', 'selected', 'settled', 'expired', 'cancelled')),
            revision INTEGER NOT NULL CHECK (revision > 0),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE companion_diaries (
            diary_id TEXT PRIMARY KEY,
            local_date TEXT NOT NULL,
            content TEXT NOT NULL,
            source_event_ids_json TEXT NOT NULL CHECK (json_valid(source_event_ids_json)),
            provider_trace_json TEXT NOT NULL CHECK (json_valid(provider_trace_json)),
            revision INTEGER NOT NULL CHECK (revision > 0),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE companion_provider_jobs (
            job_id TEXT PRIMARY KEY,
            kind TEXT NOT NULL CHECK (kind IN ('chat', 'event', 'diary', 'vision', 'voice', 'tts', 'weather')),
            status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'completed', 'failed', 'cancelled', 'timed_out')),
            artifact_token TEXT,
            error_code TEXT,
            idempotency_key TEXT NOT NULL UNIQUE,
            revision INTEGER NOT NULL CHECK (revision > 0),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
    ),
)

MIGRATION_2 = CompanionMigration(
    version=2,
    migration_id="002_companion_hard_forget_receipts",
    statements=(
        """
        CREATE TABLE companion_forget_receipts (
            receipt_id TEXT PRIMARY KEY,
            message_id TEXT NOT NULL UNIQUE,
            session_id TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('pending', 'failed', 'completed')),
            failed_step TEXT,
            affected_json TEXT NOT NULL CHECK (json_valid(affected_json)),
            attempts INTEGER NOT NULL CHECK (attempts > 0),
            started_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT
        )
        """,
        "CREATE INDEX companion_forget_status ON companion_forget_receipts(status, updated_at, receipt_id)",
    ),
)

MIGRATION_3 = CompanionMigration(
    version=3,
    migration_id="003_companion_reminder_occurrences",
    statements=(
        """
        CREATE TABLE companion_reminder_occurrences (
            occurrence_id TEXT PRIMARY KEY,
            reminder_id TEXT NOT NULL REFERENCES companion_reminders(reminder_id) ON DELETE CASCADE,
            scheduled_for TEXT NOT NULL,
            fire_at TEXT NOT NULL,
            phase TEXT NOT NULL CHECK (phase IN ('advance', 'due')),
            state TEXT NOT NULL CHECK (state IN ('pending', 'presented', 'acknowledged', 'snoozed', 'completed', 'expired', 'cancelled')),
            snooze_until TEXT,
            revision INTEGER NOT NULL CHECK (revision > 0),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (reminder_id, scheduled_for, phase)
        )
        """,
        "CREATE INDEX companion_reminder_occurrences_due ON companion_reminder_occurrences(state, fire_at, occurrence_id)",
        "CREATE INDEX companion_reminder_occurrences_reminder ON companion_reminder_occurrences(reminder_id, scheduled_for, phase)",
    ),
)

MIGRATION_4 = CompanionMigration(
    version=4,
    migration_id="004_companion_state_actions",
    statements=(
        """
        CREATE TABLE companion_state_actions (
            action_id TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL UNIQUE,
            command TEXT NOT NULL,
            subject_id TEXT,
            local_day TEXT NOT NULL,
            rule_version INTEGER NOT NULL CHECK (rule_version > 0),
            affinity_before INTEGER NOT NULL,
            affinity_after INTEGER NOT NULL,
            affinity_level_after INTEGER NOT NULL,
            mood_before INTEGER NOT NULL,
            mood_after INTEGER NOT NULL,
            mood_label_after TEXT NOT NULL,
            coins_before INTEGER NOT NULL,
            coins_after INTEGER NOT NULL,
            outfit_id TEXT NOT NULL,
            background_id TEXT NOT NULL,
            state_revision INTEGER NOT NULL CHECK (state_revision > 0),
            unlocks_json TEXT NOT NULL CHECK (json_valid(unlocks_json)),
            created_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX companion_state_actions_daily ON companion_state_actions(command, local_day, created_at, action_id)",
        """
        CREATE TABLE companion_unlock_events (
            unlock_id TEXT PRIMARY KEY,
            affinity_threshold INTEGER NOT NULL UNIQUE CHECK (affinity_threshold BETWEEN 1 AND 100),
            rule_version INTEGER NOT NULL CHECK (rule_version > 0),
            created_at TEXT NOT NULL
        )
        """,
    ),
)

MIGRATION_5 = CompanionMigration(
    version=5,
    migration_id="005_companion_commerce_receipts",
    statements=(
        """
        CREATE TABLE companion_commerce_receipts (
            receipt_id TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL UNIQUE,
            operation TEXT NOT NULL CHECK (operation IN ('purchase', 'feed')),
            item_id TEXT NOT NULL,
            catalog_version INTEGER NOT NULL CHECK (catalog_version > 0),
            quantity_after INTEGER NOT NULL CHECK (quantity_after >= 0),
            inventory_revision INTEGER NOT NULL CHECK (inventory_revision > 0),
            price INTEGER NOT NULL CHECK (price >= 0),
            state_action_id TEXT,
            result_json TEXT NOT NULL CHECK (json_valid(result_json)),
            created_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX companion_commerce_receipts_item ON companion_commerce_receipts(item_id, created_at)",
    ),
)

MIGRATION_6 = CompanionMigration(
    version=6,
    migration_id="006_companion_focus_sessions",
    statements=(
        """
        CREATE TABLE companion_focus_sessions (
            session_id TEXT PRIMARY KEY,
            status TEXT NOT NULL CHECK (status IN ('running','paused','cancelled','completed')),
            target_seconds INTEGER NOT NULL CHECK (target_seconds BETWEEN 300 AND 14400),
            elapsed_seconds REAL NOT NULL CHECK (elapsed_seconds >= 0),
            supervision_enabled INTEGER NOT NULL CHECK (supervision_enabled IN (0,1)),
            work_processes_json TEXT NOT NULL CHECK (json_valid(work_processes_json)),
            distracting_processes_json TEXT NOT NULL CHECK (json_valid(distracting_processes_json)),
            warning_count INTEGER NOT NULL DEFAULT 0 CHECK (warning_count >= 0),
            last_warning_at TEXT,
            reward_state TEXT NOT NULL CHECK (reward_state IN ('none','pending','rewarded','limited')),
            revision INTEGER NOT NULL CHECK (revision > 0),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX companion_focus_sessions_status ON companion_focus_sessions(status, updated_at)",
    ),
)

MIGRATION_7 = CompanionMigration(
    version=7,
    migration_id="007_companion_appearance_story",
    statements=(
        """
        CREATE TABLE companion_appearance_receipts (
            receipt_id TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL UNIQUE,
            slot TEXT NOT NULL CHECK (slot IN ('outfit','background')),
            selection_id TEXT NOT NULL,
            state_revision INTEGER NOT NULL CHECK (state_revision > 0),
            result_json TEXT NOT NULL CHECK (json_valid(result_json)),
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE companion_story_progress (
            chapter_id TEXT PRIMARY KEY,
            affinity_threshold INTEGER NOT NULL CHECK (affinity_threshold BETWEEN 0 AND 100),
            unlocked_at TEXT NOT NULL,
            seen_at TEXT,
            revision INTEGER NOT NULL CHECK (revision > 0)
        )
        """,
    ),
)


MIGRATION_8 = CompanionMigration(
    version=8,
    migration_id="008_companion_message_memory_review",
    statements=(
        """
        ALTER TABLE companion_messages
        ADD COLUMN memory_review_json TEXT NOT NULL DEFAULT '{}'
        CHECK (json_valid(memory_review_json))
        """,
    ),
)


MIGRATION_9 = CompanionMigration(
    version=9,
    migration_id="009_companion_message_project_scope",
    statements=(
        """
        ALTER TABLE companion_messages
        ADD COLUMN project_id TEXT NOT NULL DEFAULT 'default'
        CHECK (length(project_id) BETWEEN 1 AND 191)
        """,
        "CREATE INDEX companion_messages_project_context ON companion_messages(project_id, session_id, context_epoch, created_at, message_id)",
    ),
)


MIGRATION_10 = CompanionMigration(
    version=10,
    migration_id="010_companion_session_project_scope",
    statements=(
        """
        ALTER TABLE companion_sessions
        ADD COLUMN project_id TEXT NOT NULL DEFAULT 'default'
        CHECK (length(project_id) BETWEEN 1 AND 191)
        """,
    ),
)


MIGRATION_11 = CompanionMigration(
    version=11,
    migration_id="011_conversation_episode_projection",
    statements=(
        """
        CREATE TABLE companion_conversation_episodes (
            episode_id TEXT PRIMARY KEY,
            agent_id TEXT NOT NULL CHECK (agent_id = 'companion.chat'),
            project_id TEXT NOT NULL CHECK (length(project_id) BETWEEN 1 AND 191),
            session_id TEXT NOT NULL REFERENCES companion_sessions(session_id) ON DELETE CASCADE,
            context_epoch INTEGER NOT NULL CHECK (context_epoch > 0),
            request_id TEXT NOT NULL,
            user_message_id TEXT NOT NULL REFERENCES companion_messages(message_id) ON DELETE CASCADE,
            user_message_revision INTEGER NOT NULL CHECK (user_message_revision > 0),
            assistant_message_id TEXT NOT NULL REFERENCES companion_messages(message_id) ON DELETE CASCADE,
            assistant_message_revision INTEGER NOT NULL CHECK (assistant_message_revision > 0),
            summary TEXT NOT NULL CHECK (length(summary) BETWEEN 1 AND 2400),
            occurred_at TEXT NOT NULL,
            UNIQUE(agent_id, assistant_message_id)
        )
        """,
        "CREATE INDEX companion_conversation_episodes_scope ON companion_conversation_episodes(project_id, agent_id, occurred_at DESC, episode_id DESC)",
    ),
)


MIGRATIONS = (
    MIGRATION_1, MIGRATION_2, MIGRATION_3, MIGRATION_4,
    MIGRATION_5, MIGRATION_6, MIGRATION_7, MIGRATION_8,
    MIGRATION_9,
    MIGRATION_10,
    MIGRATION_11,
)


def migrate(connection: sqlite3.Connection, *, applied_at: str) -> tuple[int, ...]:
    _validate_migration_inventory()
    connection.execute("BEGIN IMMEDIATE")
    applied: list[int] = []
    try:
        current = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if current > SCHEMA_VERSION:
            raise CompanionSchemaTooNew(
                f"companion database schema {current} is newer than supported {SCHEMA_VERSION}"
            )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS companion_migrations (
                version INTEGER PRIMARY KEY CHECK (version > 0),
                migration_id TEXT NOT NULL UNIQUE,
                checksum TEXT NOT NULL CHECK (length(checksum) = 64),
                applied_at TEXT NOT NULL
            )
            """
        )
        recorded_rows = connection.execute(
            "SELECT version, migration_id, checksum FROM companion_migrations ORDER BY version"
        ).fetchall()
        recorded = {int(row[0]): (str(row[1]), str(row[2])) for row in recorded_rows}
        if any(version > SCHEMA_VERSION for version in recorded):
            raise CompanionSchemaTooNew("companion migration ledger contains a newer schema")
        expected_recorded_versions = set(range(1, current + 1))
        if set(recorded) != expected_recorded_versions:
            raise CompanionIntegrityError(
                "companion migration ledger and user_version do not describe the same history"
            )

        for migration in MIGRATIONS:
            existing = recorded.get(migration.version)
            expected = (migration.migration_id, migration.checksum)
            if existing is not None and existing != expected:
                raise CompanionIntegrityError(
                    f"companion migration {migration.version} checksum or id does not match"
                )
            if migration.version <= current:
                if existing is None:
                    raise CompanionIntegrityError(
                        f"companion migration ledger is missing version {migration.version}"
                    )
                continue
            for statement in migration.statements:
                connection.execute(statement)
            connection.execute(
                """
                INSERT INTO companion_migrations (version, migration_id, checksum, applied_at)
                VALUES (?, ?, ?, ?)
                """,
                (migration.version, migration.migration_id, migration.checksum, applied_at),
            )
            connection.execute(f"PRAGMA user_version={migration.version}")
            current = migration.version
            applied.append(migration.version)
        connection.execute("COMMIT")
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    return tuple(applied)


def _validate_migration_inventory() -> None:
    versions = tuple(migration.version for migration in MIGRATIONS)
    if versions != tuple(range(1, SCHEMA_VERSION + 1)):
        raise CompanionIntegrityError("companion migration inventory must be contiguous")
    ids = tuple(migration.migration_id for migration in MIGRATIONS)
    if len(ids) != len(set(ids)):
        raise CompanionIntegrityError("companion migration ids must be unique")
