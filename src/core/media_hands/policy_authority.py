from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
import re
import sqlite3

from core.storage_provider import (
    SQLiteStructuredRecord,
    SQLiteStructuredRecordStore,
    SQLiteUnitOfWorkConflict,
    SQLiteUnitOfWorkError,
)

from .policy_source import (
    MediaHandsPolicySourceError,
    default_personal_workbench_policy_snapshot,
    load_media_hands_policy,
    validate_media_hands_policy_snapshot,
)


_COMMAND_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{7,127}$")
_SCOPE = "personal-workbench"
_REVISIONS = "media_hands_policy_revisions"
_HEADS = "media_hands_policy_heads"
_COMMANDS = "media_hands_policy_commands"


class MediaHandsPolicyAuthorityError(ValueError):
    pass


class MediaHandsPolicyAuthorityConflict(MediaHandsPolicyAuthorityError):
    pass


@dataclass(frozen=True, slots=True)
class MediaHandsPolicyRevision:
    revision: int
    public_ref: str
    snapshot: Mapping[str, object]
    command_id: str
    actor: str
    created_at: str


class MediaHandsPolicyAuthority:
    """Workench-global immutable Media Hands policy revisions and CAS head."""

    def __init__(self, records: SQLiteStructuredRecordStore) -> None:
        self._records = records

    def current(self) -> MediaHandsPolicyRevision | None:
        try:
            with self._records.begin() as unit:
                head = unit.read(_HEADS, _SCOPE)
                if head is None:
                    unit.rollback()
                    return None
                revision = self._head_revision(head)
                revision_id = head.payload.get("revision_id")
                record = (
                    unit.read(_REVISIONS, revision_id)
                    if isinstance(revision_id, str)
                    else None
                )
                result = self._resolve_head(head, record, revision=revision)
                unit.rollback()
                return result
        except MediaHandsPolicyAuthorityError:
            raise
        except (
            MediaHandsPolicySourceError,
            SQLiteUnitOfWorkError,
            sqlite3.DatabaseError,
        ) as error:
            raise MediaHandsPolicyAuthorityError(
                "media policy authority read failed"
            ) from error

    def load_current_snapshot(self) -> dict[str, object]:
        current = self.current()
        if current is None:
            return default_personal_workbench_policy_snapshot()
        return dict(current.snapshot)

    def assert_current_for_admission(
        self,
        connection: sqlite3.Connection,
        expected_policy_revision: str,
    ) -> None:
        """Fence a new Job against the policy head in its admission transaction."""

        head = self._read_record(connection, _HEADS, _SCOPE)
        if head is None:
            raise MediaHandsPolicyAuthorityError("media hands policy is disabled")
        revision = self._head_revision(head)
        revision_id = head.payload.get("revision_id")
        record = (
            self._read_record(connection, _REVISIONS, revision_id)
            if isinstance(revision_id, str)
            else None
        )
        resolved = self._resolve_head(head, record, revision=revision)
        current = load_media_hands_policy(resolved.snapshot)
        if current.revision != expected_policy_revision:
            raise MediaHandsPolicyAuthorityError(
                "media hands policy revision changed before admission"
            )

    def publish(
        self,
        snapshot: Mapping[str, object],
        *,
        expected_revision: int,
        command_id: str,
        actor: str,
        created_at: str,
    ) -> MediaHandsPolicyRevision:
        if (
            not isinstance(expected_revision, int)
            or isinstance(expected_revision, bool)
            or expected_revision < 0
            or not isinstance(command_id, str)
            or _COMMAND_ID.fullmatch(command_id) is None
            or not isinstance(actor, str)
            or not actor
            or not isinstance(created_at, str)
            or not created_at
        ):
            raise MediaHandsPolicyAuthorityError("media policy command is invalid")
        normalized = validate_media_hands_policy_snapshot(snapshot)
        try:
            with self._records.begin() as unit:
                replay = unit.read(_COMMANDS, command_id)
                if replay is not None:
                    revision_id = replay.payload.get("revision_id")
                    revision_record = (
                        unit.read(_REVISIONS, revision_id)
                        if isinstance(revision_id, str)
                        else None
                    )
                    result = self._revision_from_command(replay, revision_record)
                    if (
                        result.snapshot != normalized
                        or result.actor != actor
                        or expected_revision != result.revision - 1
                    ):
                        raise MediaHandsPolicyAuthorityConflict(
                            "media policy command id conflicts with immutable input"
                        )
                    unit.rollback()
                    return result
                head = unit.read(_HEADS, _SCOPE)
                current_revision = self._head_revision(head)
                if current_revision != expected_revision:
                    raise MediaHandsPolicyAuthorityConflict(
                        f"expected policy revision {expected_revision}, found {current_revision}"
                    )
                revision = current_revision + 1
                expected_name = f"personal-workbench-r{revision}"
                if normalized.get("revision") != expected_name:
                    raise MediaHandsPolicyAuthorityError(
                        f"media policy snapshot revision must be {expected_name}"
                    )
                revision_id = f"{_SCOPE}~r{revision}"
                public_ref = f"crp://media-hands/policies/{_SCOPE}/r{revision}"
                revision_payload = {
                    "scope": _SCOPE,
                    "revision": revision,
                    "public_ref": public_ref,
                    "snapshot": normalized,
                    "command_id": command_id,
                    "actor": actor,
                    "created_at": created_at,
                }
                unit.put(_REVISIONS, revision_id, revision_payload, expected_revision=0)
                unit.put(
                    _HEADS,
                    _SCOPE,
                    {"revision": revision, "revision_id": revision_id, "public_ref": public_ref},
                    expected_revision=head.revision if head is not None else 0,
                )
                unit.put(
                    _COMMANDS,
                    command_id,
                    {"revision_id": revision_id, **revision_payload},
                    expected_revision=0,
                )
                unit.commit()
        except MediaHandsPolicyAuthorityError:
            raise
        except (SQLiteUnitOfWorkConflict, SQLiteUnitOfWorkError) as exc:
            raise MediaHandsPolicyAuthorityConflict("media policy publication conflicted") from exc
        record = self._records.read(_REVISIONS, revision_id)
        if record is None:
            raise MediaHandsPolicyAuthorityError("media policy revision disappeared after commit")
        return self._revision_from_record(record)

    def _resolve_head(
        self,
        head: SQLiteStructuredRecord,
        record: SQLiteStructuredRecord | None,
        *,
        revision: int,
    ) -> MediaHandsPolicyRevision:
        revision_id = head.payload.get("revision_id")
        public_ref = head.payload.get("public_ref")
        if (
            not isinstance(revision_id, str)
            or revision_id != f"{_SCOPE}~r{revision}"
            or public_ref != f"crp://media-hands/policies/{_SCOPE}/r{revision}"
        ):
            raise MediaHandsPolicyAuthorityError("media policy head identity drifted")
        if record is None:
            raise MediaHandsPolicyAuthorityError("media policy head revision is missing")
        result = self._revision_from_record(record)
        if result.public_ref != public_ref:
            raise MediaHandsPolicyAuthorityError("media policy head public ref drifted")
        return result

    @staticmethod
    def _read_record(
        connection: sqlite3.Connection,
        collection: str,
        object_id: str,
    ) -> SQLiteStructuredRecord | None:
        try:
            row = connection.execute(
                """
                SELECT collection, object_id, payload_json, revision
                FROM crp_structured_records
                WHERE collection = ? AND object_id = ?
                """,
                (collection, object_id),
            ).fetchone()
        except sqlite3.DatabaseError as error:
            raise MediaHandsPolicyAuthorityError(
                "media policy authority read failed"
            ) from error
        if row is None:
            return None
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, UnicodeError, json.JSONDecodeError) as error:
            raise MediaHandsPolicyAuthorityError(
                "media policy record payload is invalid"
            ) from error
        if not isinstance(payload, dict):
            raise MediaHandsPolicyAuthorityError("media policy record payload is invalid")
        return SQLiteStructuredRecord(
            collection=str(row["collection"]),
            object_id=str(row["object_id"]),
            payload=payload,
            revision=int(row["revision"]),
        )

    @staticmethod
    def _head_revision(head: SQLiteStructuredRecord | None) -> int:
        if head is None:
            return 0
        revision = head.payload.get("revision")
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
            raise MediaHandsPolicyAuthorityError("media policy head revision is invalid")
        if head.revision != revision:
            raise MediaHandsPolicyAuthorityError("media policy head CAS revision drifted")
        return revision

    def _revision_from_command(
        self,
        command: SQLiteStructuredRecord,
        record: SQLiteStructuredRecord | None,
    ) -> MediaHandsPolicyRevision:
        revision_id = command.payload.get("revision_id")
        if not isinstance(revision_id, str):
            raise MediaHandsPolicyAuthorityError("media policy command revision is invalid")
        if record is None or record.object_id != revision_id or dict(record.payload) != {
            key: value for key, value in command.payload.items() if key != "revision_id"
        }:
            raise MediaHandsPolicyAuthorityError("media policy command evidence drifted")
        return self._revision_from_record(record)

    @staticmethod
    def _revision_from_record(record: SQLiteStructuredRecord) -> MediaHandsPolicyRevision:
        value = record.payload
        if set(value) != {
            "scope", "revision", "public_ref", "snapshot", "command_id", "actor", "created_at"
        }:
            raise MediaHandsPolicyAuthorityError("media policy revision fields drifted")
        revision = value.get("revision")
        snapshot = value.get("snapshot")
        if (
            value.get("scope") != _SCOPE
            or record.revision != 1
            or not isinstance(revision, int)
            or isinstance(revision, bool)
            or revision < 1
            or record.object_id != f"{_SCOPE}~r{revision}"
            or value.get("public_ref") != f"crp://media-hands/policies/{_SCOPE}/r{revision}"
            or not isinstance(snapshot, Mapping)
        ):
            raise MediaHandsPolicyAuthorityError("media policy revision identity drifted")
        normalized = validate_media_hands_policy_snapshot(snapshot)
        if normalized.get("revision") != f"personal-workbench-r{revision}":
            raise MediaHandsPolicyAuthorityError("media policy snapshot revision drifted")
        command_id = value.get("command_id")
        actor = value.get("actor")
        created_at = value.get("created_at")
        if not all(isinstance(item, str) and item for item in (command_id, actor, created_at)):
            raise MediaHandsPolicyAuthorityError("media policy revision audit fields drifted")
        return MediaHandsPolicyRevision(
            revision, str(value["public_ref"]), normalized,
            str(command_id), str(actor), str(created_at),
        )
