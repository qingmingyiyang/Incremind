from __future__ import annotations

from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3

import pytest

from core.media_hands import (
    MediaHandsPolicyAuthority,
    MediaHandsPolicyAuthorityConflict,
    MediaHandsPolicyAuthorityError,
    default_personal_workbench_policy_snapshot,
    load_media_hands_policy,
)
from core.storage_provider import SQLiteStructuredRecordStore


def _snapshot(revision: int, *, enabled: bool = True) -> dict[str, object]:
    value = default_personal_workbench_policy_snapshot()
    value["enabled"] = enabled
    value["revision"] = f"personal-workbench-r{revision}"
    return value


def test_missing_policy_head_loads_disabled_default(tmp_path) -> None:
    authority = MediaHandsPolicyAuthority(
        SQLiteStructuredRecordStore(tmp_path / "jobs.sqlite3")
    )

    assert authority.current() is None
    snapshot = authority.load_current_snapshot()
    assert snapshot["enabled"] is False
    with pytest.raises(Exception, match="disabled"):
        load_media_hands_policy(snapshot)


def test_enabled_policy_revision_survives_new_authority_instance_and_replays_command(
    tmp_path,
) -> None:
    database = tmp_path / "jobs.sqlite3"
    first = MediaHandsPolicyAuthority(SQLiteStructuredRecordStore(database))
    snapshot = _snapshot(1)
    created = first.publish(
        snapshot,
        expected_revision=0,
        command_id="enable-media-policy-0001",
        actor="local-user",
        created_at="2026-08-25T22:30:00Z",
    )
    replayed = first.publish(
        deepcopy(snapshot),
        expected_revision=0,
        command_id="enable-media-policy-0001",
        actor="local-user",
        created_at="2026-08-25T22:31:00Z",
    )
    restarted = MediaHandsPolicyAuthority(SQLiteStructuredRecordStore(database)).current()

    assert created == replayed == restarted
    assert restarted is not None and restarted.revision == 1
    assert restarted.public_ref == "crp://media-hands/policies/personal-workbench/r1"
    assert load_media_hands_policy(restarted.snapshot).revision == "personal-workbench-r1"


def test_policy_publish_requires_cas_and_strict_command_replay(tmp_path) -> None:
    authority = MediaHandsPolicyAuthority(
        SQLiteStructuredRecordStore(tmp_path / "jobs.sqlite3")
    )
    authority.publish(
        _snapshot(1), expected_revision=0, command_id="media-policy-command-0001",
        actor="local-user", created_at="2026-08-25T22:30:00Z",
    )

    with pytest.raises(MediaHandsPolicyAuthorityConflict, match="expected policy revision"):
        authority.publish(
            _snapshot(2), expected_revision=0, command_id="media-policy-command-0002",
            actor="local-user", created_at="2026-08-25T22:31:00Z",
        )
    changed = _snapshot(1)
    changed["enabled"] = False
    with pytest.raises(MediaHandsPolicyAuthorityConflict, match="command id conflicts"):
        authority.publish(
            changed, expected_revision=0, command_id="media-policy-command-0001",
            actor="local-user", created_at="2026-08-25T22:30:00Z",
        )
    records = SQLiteStructuredRecordStore(tmp_path / "jobs.sqlite3")
    assert len(records.list("media_hands_policy_revisions")) == 1
    assert len(records.list("media_hands_policy_commands")) == 1
    assert authority.current().revision == 1


def test_concurrent_first_publish_commits_exactly_one_revision_command_and_head(
    tmp_path,
) -> None:
    database = tmp_path / "jobs.sqlite3"
    SQLiteStructuredRecordStore(database).journal_mode()

    def publish(index: int) -> str:
        authority = MediaHandsPolicyAuthority(SQLiteStructuredRecordStore(database))
        try:
            authority.publish(
                _snapshot(1), expected_revision=0,
                command_id=f"concurrent-media-policy-{index:04d}",
                actor="local-user", created_at=f"2026-08-25T22:3{index}:00Z",
            )
            return "created"
        except MediaHandsPolicyAuthorityConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(publish, (1, 2)))

    records = SQLiteStructuredRecordStore(database)
    assert sorted(results) == ["conflict", "created"]
    assert len(records.list("media_hands_policy_revisions")) == 1
    assert len(records.list("media_hands_policy_heads")) == 1
    assert len(records.list("media_hands_policy_commands")) == 1


def test_disabled_revision_is_persisted_but_not_runtime_ready(tmp_path) -> None:
    authority = MediaHandsPolicyAuthority(
        SQLiteStructuredRecordStore(tmp_path / "jobs.sqlite3")
    )
    authority.publish(
        _snapshot(1), expected_revision=0, command_id="media-policy-enable-0001",
        actor="local-user", created_at="2026-08-25T22:30:00Z",
    )
    disabled = authority.publish(
        _snapshot(2, enabled=False), expected_revision=1,
        command_id="media-policy-disable-0002", actor="local-user",
        created_at="2026-08-25T22:31:00Z",
    )

    assert disabled.revision == 2
    assert authority.load_current_snapshot()["enabled"] is False
    with pytest.raises(Exception, match="disabled"):
        load_media_hands_policy(disabled.snapshot)


def test_missing_revision_behind_head_fails_closed(tmp_path) -> None:
    records = SQLiteStructuredRecordStore(tmp_path / "jobs.sqlite3")
    authority = MediaHandsPolicyAuthority(records)
    authority.publish(
        _snapshot(1), expected_revision=0, command_id="media-policy-drift-0001",
        actor="local-user", created_at="2026-08-25T22:30:00Z",
    )
    with records.begin() as unit:
        unit.delete(
            "media_hands_policy_revisions",
            "personal-workbench~r1",
            expected_revision=1,
        )
        unit.commit()

    with pytest.raises(MediaHandsPolicyAuthorityError, match="missing"):
        authority.current()


def test_overwritten_immutable_revision_fails_closed(tmp_path) -> None:
    records = SQLiteStructuredRecordStore(tmp_path / "jobs.sqlite3")
    authority = MediaHandsPolicyAuthority(records)
    authority.publish(
        _snapshot(1), expected_revision=0, command_id="media-policy-overwrite-0001",
        actor="local-user", created_at="2026-08-25T22:30:00Z",
    )
    record = records.read("media_hands_policy_revisions", "personal-workbench~r1")
    assert record is not None
    with records.begin() as unit:
        unit.put(
            record.collection, record.object_id, record.payload,
            expected_revision=record.revision,
        )
        unit.commit()

    with pytest.raises(MediaHandsPolicyAuthorityError, match="identity drifted"):
        authority.current()


def test_corrupt_snapshot_schema_is_mapped_to_authority_failure(tmp_path) -> None:
    database = tmp_path / "jobs.sqlite3"
    authority = MediaHandsPolicyAuthority(SQLiteStructuredRecordStore(database))
    authority.publish(
        _snapshot(1), expected_revision=0, command_id="media-policy-corrupt-0001",
        actor="local-user", created_at="2026-08-25T22:30:00Z",
    )
    connection = sqlite3.connect(database)
    try:
        row = connection.execute(
            "SELECT payload_json FROM crp_structured_records WHERE collection = ? AND object_id = ?",
            ("media_hands_policy_revisions", "personal-workbench~r1"),
        ).fetchone()
        payload = json.loads(row[0])
        payload["snapshot"]["unexpected"] = True
        connection.execute(
            "UPDATE crp_structured_records SET payload_json = ? WHERE collection = ? AND object_id = ?",
            (json.dumps(payload), "media_hands_policy_revisions", "personal-workbench~r1"),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(MediaHandsPolicyAuthorityError, match="read failed"):
        authority.current()
