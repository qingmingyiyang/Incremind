from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3

import pytest

from core.ai_kernel import (
    AgentAdapterProfile,
    ContextCompaction,
    ContextEntry,
    ContextManifest,
    ExternalAgentAuthoritySnapshot,
    ExternalAgentAdmissionSnapshot,
    ExternalAgentContextBridge,
    ExternalAgentContextConflict,
    ExternalAgentCursorGap,
    ExternalAgentContextError,
    SQLiteAITurnStore,
    context_manifest_to_payload,
)


NOW = datetime(2026, 8, 27, 8, 0, tzinfo=timezone.utc)


def test_bridge_uses_existing_turn_context_and_persists_project_cursor(tmp_path: Path) -> None:
    store, bridge, payload_ref = _bridge(tmp_path)
    context_map = _start(bridge)

    assert context_map["context_refs"] == [{
        "context_ref": payload_ref,
        "kind": "application_skill",
        "revision": "skill-r4",
        "maximum_bytes": len("bounded project background".encode("utf-8")),
    }]
    assert "capability-manifest" not in json.dumps(context_map)
    resolved = bridge.resolve_context(
        operation_id="resolve-initial",
        session_id=context_map["session_id"],
        context_refs=[payload_ref],
        expected_context_manifest_revision="context-manifest-turn-a",
        purpose="project_assistance",
    )
    assert resolved["slices"][0]["content"]["markdown"] == "bounded project background"

    _append_change(store, 3,
        project_id="project-a", change_type="project_skill.published",
        object_ref="crp://skills/project-a/skill-a", object_revision="skill-r5",
        occurred_at=NOW.isoformat(),
    )
    restarted = SQLiteAITurnStore(tmp_path / "turns.sqlite3")
    resumed = ExternalAgentContextBridge(
        store=restarted,
        adapters=[_adapter()],
        authority=lambda _: _authority(),
        clock=lambda: NOW,
    )
    feed = resumed.get_changes(
        operation_id="changes-after-restart",
        session_id=context_map["session_id"],
        after_cursor=context_map["event_cursor"],
        purpose="project_assistance",
    )
    assert [item["change_type"] for item in feed["changes"]] == ["project_skill.published"]
    assert "summary" not in json.dumps(feed)


def test_two_adapters_receive_the_same_authority_revision_projection(tmp_path: Path) -> None:
    _, bridge, _ = _bridge(tmp_path)
    codex = _start(bridge)
    claude = _start(
        bridge, adapter_id="claude", template_revision="claude-template-r1",
    )
    assert codex["context_manifest_revision"] == claude["context_manifest_revision"]
    assert codex["context_refs"] == claude["context_refs"]
    assert codex["project_profile_revision"] == claude["project_profile_revision"]
    assert codex["boundary_profile_revision"] == claude["boundary_profile_revision"]


def test_compaction_exposes_only_terminal_output_and_survives_store_restart(tmp_path: Path) -> None:
    store, bridge, raw_ref, compact_ref = _compacted_bridge(tmp_path)
    context_map = _start(bridge)

    assert context_map["context_manifest_revision"] == "context-manifest-turn-a-compacted"
    assert [item["context_ref"] for item in context_map["context_refs"]] == [compact_ref]
    assert "cookie" not in json.dumps(context_map).lower()
    assert "C:\\" not in json.dumps(context_map)
    with pytest.raises(ExternalAgentContextError, match="target was not found"):
        bridge.resolve_context(
            operation_id="resolve-compaction-raw", session_id=context_map["session_id"],
            context_refs=[raw_ref],
            expected_context_manifest_revision="context-manifest-turn-a-compacted",
            purpose="project_assistance",
        )

    resolved = bridge.resolve_context(
        operation_id="resolve-compaction-output", session_id=context_map["session_id"],
        context_refs=[compact_ref],
        expected_context_manifest_revision="context-manifest-turn-a-compacted",
        purpose="project_assistance",
    )
    assert resolved["slices"][0]["content"]["markdown"] == "sanitized compact summary"
    assert "cookie" not in json.dumps(resolved).lower()
    assert "C:\\" not in json.dumps(resolved)

    restarted = ExternalAgentContextBridge(
        store=SQLiteAITurnStore(tmp_path / "turns.sqlite3"), adapters=[_adapter()],
        authority=lambda _: _authority(), clock=lambda: NOW,
    )
    restarted_map = _start(restarted, operation_id="start-codex-compacted-restart")
    assert restarted_map["context_manifest_revision"] == context_map["context_manifest_revision"]
    assert [item["context_ref"] for item in restarted_map["context_refs"]] == [compact_ref]
    replay = restarted.resolve_context(
        operation_id="resolve-compaction-output-restart", session_id=restarted_map["session_id"],
        context_refs=[compact_ref],
        expected_context_manifest_revision="context-manifest-turn-a-compacted",
        purpose="project_assistance",
    )
    assert replay["slices"] == resolved["slices"]
    with pytest.raises(ExternalAgentContextError, match="target was not found"):
        restarted.resolve_context(
            operation_id="resolve-compaction-raw-restart", session_id=restarted_map["session_id"],
            context_refs=[raw_ref],
            expected_context_manifest_revision="context-manifest-turn-a-compacted",
            purpose="project_assistance",
        )


def test_start_session_is_receipted_idempotent_and_revalidates_authority(tmp_path: Path) -> None:
    store, bridge, payload_ref = _bridge(tmp_path)
    started = _start(bridge, operation_id="start-replay")
    replay = _start(bridge, operation_id="start-replay")
    assert replay == started
    bridge.resolve_context(
        operation_id="resolve-after-start", session_id=started["session_id"],
        context_refs=[payload_ref],
        expected_context_manifest_revision="context-manifest-turn-a",
        purpose="project_assistance",
    )
    assert _start(bridge, operation_id="start-replay") == started
    connection = store._connect()  # noqa: SLF001 - durable receipt inspection
    try:
        receipt = json.loads(connection.execute(
            "SELECT receipt_json FROM ai_external_agent_read_receipts WHERE operation_id=?",
            ("start-replay",),
        ).fetchone()[0])
    finally:
        connection.close()
    assert receipt["operation"] == "start_session"
    assert "context_refs" not in json.dumps(receipt)
    drifted = ExternalAgentContextBridge(
        store=store, adapters=[_adapter()],
        authority=lambda _: replace(_authority(), boundary_profile_revision=8), clock=lambda: NOW,
    )
    with pytest.raises(ExternalAgentContextConflict, match="authority revision"):
        _start(drifted, operation_id="start-replay")


def test_bridge_rejects_adapter_authority_ref_and_expiry_drift(tmp_path: Path) -> None:
    _, bridge, payload_ref = _bridge(tmp_path)
    with pytest.raises(ExternalAgentContextError, match="adapter revision"):
        _start(bridge, adapter_revision=2)
    context_map = _start(bridge)
    with pytest.raises(ExternalAgentContextError, match="not found"):
        bridge.resolve_context(
            operation_id="resolve-ref-rejected",
            session_id=context_map["session_id"],
            context_refs=["crp://session/turn-a/project-skill/guessed"],
            expected_context_manifest_revision="context-manifest-turn-a",
            purpose="project_assistance",
        )
    with pytest.raises(ExternalAgentContextConflict, match="manifest revision"):
        bridge.resolve_context(
            operation_id="resolve-manifest-drift",
            session_id=context_map["session_id"],
            context_refs=[payload_ref],
            expected_context_manifest_revision="stale",
            purpose="project_assistance",
        )

    drifted = ExternalAgentContextBridge(
        store=bridge._store,  # noqa: SLF001 - controlled contract fixture
        adapters=[_adapter()],
        authority=lambda _: replace(_authority(), boundary_profile_revision=8),
        clock=lambda: NOW,
    )
    with pytest.raises(ExternalAgentContextConflict, match="authority revision"):
        drifted.get_changes(
            operation_id="changes-drifted",
            session_id=context_map["session_id"],
            after_cursor=context_map["event_cursor"],
            purpose="project_assistance",
        )
    expired = ExternalAgentContextBridge(
        store=bridge._store,  # noqa: SLF001 - controlled contract fixture
        adapters=[_adapter()],
        authority=lambda _: _authority(),
        clock=lambda: NOW + timedelta(minutes=16),
    )
    with pytest.raises(ExternalAgentContextError, match="expired"):
        expired.resolve_context(
            operation_id="resolve-expired",
            session_id=context_map["session_id"],
            context_refs=[payload_ref],
            expected_context_manifest_revision="context-manifest-turn-a",
            purpose="project_assistance",
        )


def test_bridge_rejects_context_without_explicit_project_ownership(tmp_path: Path) -> None:
    _, bridge, _ = _bridge(tmp_path, source_project_id=None)
    with pytest.raises(ExternalAgentContextError, match="crossed project scope"):
        _start(bridge)


@pytest.mark.parametrize("kind", ("project_skill", "memory_r1", "memory_r2", "memory_r3"))
def test_bridge_resolves_only_frozen_same_project_published_memory_payloads(
    tmp_path: Path, kind: str,
) -> None:
    _, bridge, payload_ref = _bridge(tmp_path, published_kind=kind)
    context_map = _start(bridge)

    assert context_map["context_refs"] == [{
        "context_ref": payload_ref,
        "kind": kind,
        "revision": f"{kind}-r4",
        "maximum_bytes": len("published project memory".encode("utf-8")),
        "source_ref": f"crp://memory/project-a/{kind}-a",
        "provenance_refs": ["crp://session/turn-a/published-project-memory-snapshot-v1"],
    }]
    resolved = bridge.resolve_context(
        operation_id=f"resolve-{kind}", session_id=context_map["session_id"],
        context_refs=[payload_ref],
        expected_context_manifest_revision="context-manifest-turn-a",
        purpose="project_assistance",
    )
    assert resolved["slices"] == [{
        "context_ref": payload_ref,
        "kind": kind,
        "revision": f"{kind}-r4",
        "content_bytes": len("published project memory".encode("utf-8")),
        "content": {
            "kind": kind,
            "object_id": f"{kind}-a",
            "revision": f"{kind}-r4",
            "trust_status": "trusted",
            "markdown": "published project memory",
        },
    }]


def test_existing_bridge_session_receives_metadata_only_memory_invalidation(tmp_path: Path) -> None:
    store, bridge, _payload_ref = _bridge(tmp_path, published_kind="memory_r1")
    context_map = _start(bridge)
    invalidation = _append_change(
        store, 3,
        project_id="project-a", change_type="memory.invalidated",
        object_ref="crp://memory/project-a/memory_r1-a", object_revision="r5",
        occurred_at=NOW.isoformat(),
    )

    feed = bridge.get_changes(
        operation_id="memory-invalidation", session_id=context_map["session_id"],
        after_cursor=context_map["event_cursor"], purpose="project_assistance",
    )

    assert feed["changes"] == [invalidation["change"]]
    assert "markdown" not in json.dumps(feed)


def test_bridge_rejects_published_memory_payload_or_source_scope_drift(tmp_path: Path) -> None:
    store, bridge, payload_ref = _bridge(tmp_path, published_kind="memory_r1")
    connection = store._connect()  # noqa: SLF001 - deliberate immutable payload corruption fixture
    try:
        connection.execute(
            "UPDATE ai_turn_payloads SET payload_json=? WHERE payload_ref=?",
            (json.dumps({
                "schema_version": "1.0.0", "kind": "memory_r1", "project_id": "project-a",
                "object_id": "memory-r1-a", "revision": "memory_r1-r4", "trust_status": "trusted",
                "markdown": "published project memory",
            }), payload_ref),
        )
        connection.commit()
    finally:
        connection.close()
    context_map = _start(bridge)
    with pytest.raises(ExternalAgentContextConflict, match="source identity"):
        bridge.resolve_context(
            operation_id="resolve-memory-foreign-ref", session_id=context_map["session_id"],
            context_refs=[payload_ref], expected_context_manifest_revision="context-manifest-turn-a",
            purpose="project_assistance",
        )
    assert store.get_external_agent_session(context_map["session_id"])["resolved_bytes"] == 0

    _, foreign_bridge, _ = _bridge(
        tmp_path / "foreign-source", published_kind="memory_r1",
        source_ref="crp://memory/project-other/memory-r1-a",
    )
    with pytest.raises(ExternalAgentContextError, match="source ref crossed"):
        _start(foreign_bridge)


@pytest.mark.parametrize("payload_patch, error", (
    ({"unexpected": "field"}, "shape is invalid"),
    ({"markdown": "Cookie=session-canary"}, "unsafe content"),
))
def test_bridge_rejects_published_memory_shape_and_sensitive_body(
    tmp_path: Path, payload_patch: dict[str, str], error: str,
) -> None:
    store, bridge, payload_ref = _bridge(tmp_path, published_kind="memory_r2")
    corrupted = {
        "schema_version": "1.0.0", "kind": "memory_r2", "project_id": "project-a",
        "object_id": "memory_r2-a", "revision": "memory_r2-r4", "trust_status": "trusted",
        "markdown": "published project memory", **payload_patch,
    }
    connection = store._connect()  # noqa: SLF001 - deliberate immutable payload corruption fixture
    try:
        connection.execute(
            "UPDATE ai_turn_payloads SET payload_json=? WHERE payload_ref=?",
            (json.dumps(corrupted), payload_ref),
        )
        connection.commit()
    finally:
        connection.close()
    context_map = _start(bridge)
    with pytest.raises(ExternalAgentContextError, match=error):
        bridge.resolve_context(
            operation_id=f"resolve-memory-r2-{error[:5]}", session_id=context_map["session_id"],
            context_refs=[payload_ref], expected_context_manifest_revision="context-manifest-turn-a",
            purpose="project_assistance",
        )


def test_bridge_budget_is_cumulative_atomic_and_no_partial_body_is_returned(tmp_path: Path) -> None:
    store, bridge, payload_ref = _bridge(tmp_path, requested_bytes=1024)
    context_map = _start(bridge, requested_context_bytes=1024)
    first = bridge.resolve_context(
        operation_id="resolve-budget-0",
        session_id=context_map["session_id"], context_refs=[payload_ref],
        expected_context_manifest_revision="context-manifest-turn-a",
        purpose="project_assistance",
    )
    size = first["resolved_bytes"]
    for index in range(1024 // size - 1):
        bridge.resolve_context(
            operation_id=f"resolve-budget-{index + 1}",
            session_id=context_map["session_id"], context_refs=[payload_ref],
            expected_context_manifest_revision="context-manifest-turn-a",
            purpose="project_assistance",
        )
    before = store.get_external_agent_session(context_map["session_id"])["resolved_bytes"]
    with pytest.raises(ValueError, match="budget exceeded"):
        bridge.resolve_context(
            operation_id="resolve-budget-exceeded",
            session_id=context_map["session_id"], context_refs=[payload_ref],
            expected_context_manifest_revision="context-manifest-turn-a",
            purpose="project_assistance",
        )
    assert store.get_external_agent_session(context_map["session_id"])["resolved_bytes"] == before


@pytest.mark.parametrize("unsafe", [
    "api_key=secret-canary",
    "Cookie=session-canary",
    "Authorization: Bearer secret-canary",
    r"open C:\Users\person\private.txt",
    "open C:/Users/person/private.txt",
    "file:///C:/Users/person/private.txt",
    "open /home/person/private.txt",
    "client_secret=secret-canary",
    "session_token=secret-canary",
])
def test_bridge_rejects_sensitive_and_absolute_path_content(tmp_path: Path, unsafe: str) -> None:
    store, bridge, payload_ref = _bridge(tmp_path)
    connection = store._connect()  # noqa: SLF001 - deliberate corruption fixture
    try:
        connection.execute(
            "UPDATE ai_turn_payloads SET payload_json=? WHERE payload_ref=?",
            (json.dumps({
                "schema_version": "1.0.0", "skill_id": "skill-a",
                "skill_fingerprint": "fingerprint-a", "markdown": unsafe,
            }), payload_ref),
        )
        connection.commit()
    finally:
        connection.close()
    context_map = _start(bridge)
    with pytest.raises(ExternalAgentContextError, match="unsafe content"):
        bridge.resolve_context(
            operation_id="resolve-unsafe",
            session_id=context_map["session_id"], context_refs=[payload_ref],
            expected_context_manifest_revision="context-manifest-turn-a",
            purpose="project_assistance",
        )
    assert store.get_external_agent_session(context_map["session_id"])["resolved_bytes"] == 0


def test_project_feed_is_atomic_bounded_and_project_scoped(tmp_path: Path) -> None:
    store, bridge, _ = _bridge(tmp_path)
    _append_change(store, 3,
        project_id="project-a", change_type="context.invalidated",
        object_ref="crp://context/project-a/old", object_revision="context-r0",
        occurred_at=NOW.isoformat(),
    )
    context_map = _start(bridge)
    _claim_other_project(store)
    _append_change(store, 2,
        project_id="project-other", change_type="memory.published",
        object_ref="crp://memory/project-other/memory-a", object_revision="memory-r1",
        occurred_at=NOW.isoformat(),
    )
    first = _append_change(store, 4,
        project_id="project-a", change_type="project_skill.published",
        object_ref="crp://skills/project-a/skill-a", object_revision="skill-r5",
        occurred_at=NOW.isoformat(),
    )
    second = _append_change(store, 5,
        project_id="project-a", change_type="document.published",
        object_ref="crp://documents/project-a/doc-a", object_revision="doc-r2",
        occurred_at=NOW.isoformat(),
    )
    with pytest.raises(ValueError, match="ahead"):
        store.project_events_after("project-a", 999)
    feed = bridge.get_changes(
        operation_id="changes-first",
        session_id=context_map["session_id"], after_cursor=context_map["event_cursor"],
        purpose="project_assistance", limit=1,
    )
    assert len(feed["changes"]) == 1
    assert feed["next_cursor"] == first["change"]["cursor"]
    assert feed["head_cursor"] == second["change"]["cursor"]
    assert feed["has_more"] is True
    resumed = bridge.get_changes(
        operation_id="changes-second",
        session_id=context_map["session_id"], after_cursor=feed["next_cursor"],
        purpose="project_assistance", limit=1,
    )
    assert resumed["next_cursor"] == second["change"]["cursor"]
    assert resumed["has_more"] is False
    assert "project-other" not in json.dumps(feed)
    with pytest.raises(ExternalAgentContextError, match="predates"):
        bridge.get_changes(
            operation_id="changes-predates",
            session_id=context_map["session_id"], after_cursor=0,
            purpose="project_assistance",
        )


def test_retained_project_feed_rejects_stale_cursor_and_rebases_from_a_new_context_map(
    tmp_path: Path,
) -> None:
    store, bridge, _ = _bridge(tmp_path)
    stale_map = _start(bridge)
    first = store.ingest_external_agent_publication_change({
        "publication_identity": "retention-one", "project_id": "project-a",
        "change_type": "memory.published", "object_ref": "crp://memory/project-a/one",
        "object_revision": "memory-r1", "occurred_at": NOW.isoformat(),
    })
    second = store.ingest_external_agent_publication_change({
        "publication_identity": "retention-two", "project_id": "project-a",
        "change_type": "project_skill.published", "object_ref": "crp://skills/project-a/two",
        "object_revision": "skill-r2", "occurred_at": NOW.isoformat(),
    })
    assert store.prune_project_events_through("project-a", first["change"]["cursor"]) == {
        "retained_after_cursor": first["change"]["cursor"],
        "earliest_available_cursor": second["change"]["cursor"],
        "head_cursor": second["change"]["cursor"],
    }
    with pytest.raises(ExternalAgentCursorGap) as gap:
        bridge.get_changes(
            operation_id="changes-retention-gap", session_id=stale_map["session_id"],
            after_cursor=stale_map["event_cursor"], purpose="project_assistance",
        )
    assert gap.value.public_payload() == {
        "code": "cursor_retention_gap", "project_id": "project-a",
        "after_cursor": 0, "retained_after_cursor": first["change"]["cursor"],
        "earliest_available_cursor": second["change"]["cursor"],
        "head_cursor": second["change"]["cursor"], "rebase_required": True,
        "rebase_action": "start_session",
    }
    stale_session = store.get_external_agent_session(stale_map["session_id"])
    assert stale_session is not None
    assert stale_session["delivered_cursor"] == stale_map["event_cursor"]
    assert stale_session["acknowledged_cursor"] == stale_map["event_cursor"]
    restarted = ExternalAgentContextBridge(
        store=SQLiteAITurnStore(tmp_path / "turns.sqlite3"), adapters=[_adapter()],
        authority=lambda _: _authority(), clock=lambda: NOW,
    )
    with pytest.raises(ExternalAgentCursorGap, match="rebase is required"):
        restarted.get_changes(
            operation_id="changes-retention-gap-restarted", session_id=stale_map["session_id"],
            after_cursor=stale_map["event_cursor"], purpose="project_assistance",
        )
    rebased_map = _start(restarted, operation_id="start-retention-rebase")
    assert rebased_map["event_cursor"] == second["change"]["cursor"]
    assert rebased_map["retained_after_cursor"] == first["change"]["cursor"]
    assert rebased_map["earliest_available_cursor"] == second["change"]["cursor"]
    third = store.ingest_external_agent_publication_change({
        "publication_identity": "retention-three", "project_id": "project-a",
        "change_type": "memory.published", "object_ref": "crp://memory/project-a/three",
        "object_revision": "memory-r3", "occurred_at": NOW.isoformat(),
    })
    resumed = restarted.get_changes(
        operation_id="changes-after-retention-rebase", session_id=rebased_map["session_id"],
        after_cursor=rebased_map["event_cursor"], purpose="project_assistance",
    )
    assert resumed["changes"] == [{
        key: value for key, value in third["change"].items() if key != "project_id"
    }]
    assert resumed["retained_after_cursor"] == first["change"]["cursor"]
    assert resumed["earliest_available_cursor"] == second["change"]["cursor"]


def test_read_operations_are_idempotent_receipted_and_privacy_safe(tmp_path: Path) -> None:
    store, bridge, payload_ref = _bridge(tmp_path)
    context_map = _start(bridge)
    first = bridge.resolve_context(
        operation_id="resolve-replay", session_id=context_map["session_id"],
        context_refs=[payload_ref], expected_context_manifest_revision="context-manifest-turn-a",
        purpose="project_assistance",
    )
    replay = bridge.resolve_context(
        operation_id="resolve-replay", session_id=context_map["session_id"],
        context_refs=[payload_ref], expected_context_manifest_revision="context-manifest-turn-a",
        purpose="project_assistance",
    )
    assert replay == first
    assert store.get_external_agent_session(context_map["session_id"])["resolved_bytes"] == first["resolved_bytes"]
    connection = store._connect()  # noqa: SLF001 - durable receipt inspection
    try:
        receipt = json.loads(connection.execute(
            "SELECT receipt_json FROM ai_external_agent_read_receipts WHERE operation_id=?",
            ("resolve-replay",),
        ).fetchone()[0])
    finally:
        connection.close()
    assert receipt["operation"] == "resolve_context"
    assert receipt["context_bytes"] == len("bounded project background".encode("utf-8"))
    serialized = json.dumps(receipt)
    assert "markdown" not in serialized
    assert "bounded project background" not in serialized
    assert "crp://" not in serialized


def test_delivery_acknowledgement_is_monotonic_and_replays_across_restart(tmp_path: Path) -> None:
    store, bridge, _ = _bridge(tmp_path)
    context_map = _start(bridge)
    first = _append_change(store, 3, project_id="project-a", change_type="memory.published",
        object_ref="crp://memory/project-a/one", object_revision="memory-r1", occurred_at=NOW.isoformat())
    _append_change(store, 4, project_id="project-a", change_type="document.published",
        object_ref="crp://documents/project-a/two", object_revision="document-r2", occurred_at=NOW.isoformat())
    delivered = bridge.get_changes(
        operation_id="changes-deliver-one", session_id=context_map["session_id"],
        after_cursor=context_map["event_cursor"], purpose="project_assistance", limit=1,
    )
    assert delivered["next_cursor"] == first["change"]["cursor"]
    _append_change(store, 5, project_id="project-a", change_type="project_skill.published",
        object_ref="crp://skills/project-a/three", object_revision="skill-r3", occurred_at=NOW.isoformat())
    replay = bridge.get_changes(
        operation_id="changes-deliver-one", session_id=context_map["session_id"],
        after_cursor=context_map["event_cursor"], purpose="project_assistance", limit=1,
    )
    assert replay == delivered
    acknowledged = bridge.acknowledge_changes(
        operation_id="ack-one", session_id=context_map["session_id"],
        acknowledged_cursor=delivered["next_cursor"], purpose="project_assistance",
    )
    assert acknowledged["acknowledged_cursor"] == delivered["next_cursor"]
    restarted = ExternalAgentContextBridge(
        store=SQLiteAITurnStore(tmp_path / "turns.sqlite3"), adapters=[_adapter()],
        authority=lambda _: _authority(), clock=lambda: NOW,
    )
    assert restarted.acknowledge_changes(
        operation_id="ack-one", session_id=context_map["session_id"],
        acknowledged_cursor=delivered["next_cursor"], purpose="project_assistance",
    ) == acknowledged
    with pytest.raises(ExternalAgentContextConflict, match="acknowledgement cursor"):
        restarted.acknowledge_changes(
            operation_id="ack-backward", session_id=context_map["session_id"],
            acknowledged_cursor=context_map["event_cursor"], purpose="project_assistance",
        )
    with pytest.raises(ExternalAgentContextConflict, match="acknowledgement cursor"):
        restarted.acknowledge_changes(
            operation_id="ack-ahead", session_id=context_map["session_id"],
            acknowledged_cursor=delivered["next_cursor"] + 1, purpose="project_assistance",
        )
    with pytest.raises(ExternalAgentContextError, match="exceeds delivered"):
        restarted.get_changes(
            operation_id="changes-skip-undelivered", session_id=context_map["session_id"],
            after_cursor=delivered["next_cursor"] + 1, purpose="project_assistance",
        )


def test_legacy_external_session_table_gains_delivery_cursors(tmp_path: Path) -> None:
    database = tmp_path / "legacy-sessions.sqlite3"
    connection = sqlite3.connect(database)
    try:
        connection.executescript("""
            CREATE TABLE ai_turn_schema(version INTEGER NOT NULL);
            INSERT INTO ai_turn_schema(version) VALUES(1);
            CREATE TABLE ai_turns(turn_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                operation_id TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE, request_json TEXT NOT NULL);
            CREATE TABLE ai_turn_events(turn_id TEXT NOT NULL REFERENCES ai_turns(turn_id),
                sequence INTEGER NOT NULL, event_id TEXT NOT NULL UNIQUE, event_json TEXT NOT NULL,
                PRIMARY KEY(turn_id, sequence));
            CREATE TABLE ai_project_event_feed(project_id TEXT NOT NULL, cursor INTEGER NOT NULL,
                change_type TEXT NOT NULL, object_ref TEXT NOT NULL, object_revision TEXT NOT NULL,
                occurred_at TEXT NOT NULL, PRIMARY KEY(project_id,cursor));
            CREATE TABLE ai_external_agent_sessions(session_id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
                turn_id TEXT NOT NULL, expires_at TEXT NOT NULL, resolved_bytes INTEGER NOT NULL,
                session_json TEXT NOT NULL);
        """)
        connection.execute(
            "INSERT INTO ai_external_agent_sessions VALUES(?,?,?,?,?,?)",
            ("agent-session-legacy", "project-a", "turn-a", NOW.isoformat(), 0, json.dumps({
                "event_cursor": 7,
            })),
        )
        connection.execute(
            "INSERT INTO ai_external_agent_sessions VALUES(?,?,?,?,?,?)",
            ("agent-session-corrupt", "project-a", "turn-a", NOW.isoformat(), 0, "not-json"),
        )
        connection.commit()
    finally:
        connection.close()
    SQLiteAITurnStore(database)
    connection = sqlite3.connect(database)
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(ai_external_agent_sessions)")}
        assert {"delivered_cursor", "acknowledged_cursor"}.issubset(columns)
        assert connection.execute(
            "SELECT delivered_cursor,acknowledged_cursor FROM ai_external_agent_sessions "
            "WHERE session_id='agent-session-legacy'"
        ).fetchone() == (7, 7)
        assert connection.execute(
            "SELECT delivered_cursor,acknowledged_cursor FROM ai_external_agent_sessions "
            "WHERE session_id='agent-session-corrupt'"
        ).fetchone() == (0, 0)
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ai_external_agent_read_receipts'"
        ).fetchone() is not None
    finally:
        connection.close()


def test_project_feed_failure_rolls_back_the_turn_event(tmp_path: Path) -> None:
    store, _, _ = _bridge(tmp_path)
    connection = store._connect()  # noqa: SLF001 - deliberate failure injection
    try:
        connection.execute(
            "CREATE TRIGGER reject_project_feed BEFORE INSERT ON ai_project_event_feed "
            "BEGIN SELECT RAISE(ABORT, 'feed rejected'); END"
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(Exception, match="identity conflict"):
        _append_change(store, 3,
            project_id="project-a", change_type="memory.published",
            object_ref="crp://memory/project-a/memory-a", object_revision="memory-r1",
            occurred_at=NOW.isoformat(),
        )
    assert store.project_event_cursor("project-a") == 0
    assert len(store.events_after("turn-a")) == 2


@pytest.mark.parametrize("unsafe_ref", (
    "crp://memory/project-a/item?token=abc",
    "crp://memory/project-a/C:\\private\\cookie.txt",
    "crp://memory/project-other/item",
    "crp://documents/project-a/item",
))
def test_project_feed_rejects_unsafe_or_foreign_refs(
    tmp_path: Path, unsafe_ref: str,
) -> None:
    store, _, _ = _bridge(tmp_path)
    with pytest.raises(ValueError, match="ref is invalid"):
        _append_change(
            store, 3, project_id="project-a", change_type="memory.published",
            object_ref=unsafe_ref, object_revision="memory-r1",
            occurred_at=NOW.isoformat(),
        )
    assert len(store.events_after("turn-a")) == 2


def test_existing_v1_database_migrates_preview_feed_to_project_local_cursors(tmp_path: Path) -> None:
    database = tmp_path / "turns.sqlite3"
    connection = sqlite3.connect(database)
    try:
        connection.executescript("""
            CREATE TABLE ai_turn_schema(version INTEGER NOT NULL);
            INSERT INTO ai_turn_schema(version) VALUES(1);
            CREATE TABLE ai_turns(turn_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                operation_id TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE,
                request_json TEXT NOT NULL);
            CREATE TABLE ai_turn_events(turn_id TEXT NOT NULL REFERENCES ai_turns(turn_id),
                sequence INTEGER NOT NULL, event_id TEXT NOT NULL UNIQUE,
                event_json TEXT NOT NULL, PRIMARY KEY(turn_id, sequence));
            CREATE TABLE ai_project_event_feed(cursor INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT NOT NULL, change_type TEXT NOT NULL, object_ref TEXT NOT NULL,
                object_revision TEXT NOT NULL, occurred_at TEXT NOT NULL);
        """)
        request = _request()
        connection.execute(
            "INSERT INTO ai_turns VALUES(?,?,?,?,?)",
            ("turn-a", "session-a", "operation-a", "turn-key-a", json.dumps(request)),
        )
        connection.execute(
            "INSERT INTO ai_turn_events VALUES(?,?,?,?)",
            ("turn-a", 1, "event-old-1", json.dumps(_event(1, "turn.accepted"))),
        )
        connection.executemany(
            "INSERT INTO ai_project_event_feed(cursor,project_id,change_type,object_ref,object_revision,occurred_at) "
            "VALUES(?,?,?,?,?,?)",
            (
                (3, "project-a", "memory.published", "crp://memory/project-a/one", "memory-r1", NOW.isoformat()),
                (5, "project-other", "memory.published", "crp://memory/project-other/one", "memory-r1", NOW.isoformat()),
                (8, "project-a", "memory.published", "crp://memory/project-a/two", "memory-r2", NOW.isoformat()),
            ),
        )
        connection.commit()
    finally:
        connection.close()
    store = SQLiteAITurnStore(database)
    assert store.project_event_cursor("project-a") == 2
    assert [item["cursor"] for item in store.project_events_after("project-a")] == [1, 2]
    assert store.project_event_cursor("project-other") == 1
    change = store.append_external_agent_change(
        _event(2, "external_agent.change.projected"), expected_sequence=1,
        project_id="project-a", change_type="context.invalidated",
        object_ref="crp://context/project-a/current", object_revision="context-r1",
        occurred_at=NOW.isoformat(),
    )
    assert change["change"]["cursor"] == 3


def _bridge(
    tmp_path: Path, *, requested_bytes: int = 4096,
    source_project_id: str | None = "project-a",
    published_kind: str | None = None,
    source_ref: str | None = None,
):
    store = SQLiteAITurnStore(tmp_path / "turns.sqlite3")
    request = _request(max_context_bytes=requested_bytes)
    store.claim_turn(request)
    store.append(_event(1, "turn.accepted"), expected_sequence=0)
    entry_kind = published_kind or "application_skill"
    revision = f"{entry_kind}-r4" if published_kind else "skill-r4"
    source_ref = source_ref or (
        f"crp://memory/project-a/{entry_kind}-a"
        if published_kind else "crp://skills/project-a/skill-a"
    )
    content = (
        {
            "schema_version": "1.0.0", "kind": entry_kind, "project_id": "project-a",
            "object_id": f"{entry_kind}-a", "revision": revision, "trust_status": "trusted",
            "markdown": "published project memory",
        }
        if published_kind else {
            "schema_version": "1.0.0", "skill_id": "skill-a",
            "skill_fingerprint": "fingerprint-a", "markdown": "bounded project background",
        }
    )
    payload_ref = store.put("turn-a", "application-skill-instructions-skill-a", content)
    capability_ref = store.put("turn-a", "capability-manifest", {"capability_ids": []})
    manifest = ContextManifest(
        manifest_id="context-manifest-turn-a",
        turn_id="turn-a",
        resolver_id="fixture-resolver",
        project_id="project-a",
        series_id=None,
        project_profile_id="profile-project-a",
        project_profile_revision=4,
        boundary_profile_id="boundary-project-a",
        boundary_profile_revision=7,
        capability_manifest_ref=capability_ref,
        entries=(ContextEntry(
            entry_id="skill-entry",
            kind=entry_kind,
            source_ref=source_ref,
            payload_ref=payload_ref,
            source_project_id=source_project_id,
            revision_identity=revision,
            content_fingerprint=None,
            provenance_refs=(
                "crp://session/turn-a/published-project-memory-snapshot-v1",
            ) if published_kind else ("crp://documents/project-a/doc-a",),
            disclosure="model",
            selection_reason="project_scope",
            content_bytes=len(content["markdown"].encode("utf-8")),
        ),),
        compactions=(),
        excluded_reason_counts=(),
        max_context_bytes=requested_bytes,
        selected_context_bytes=len(content["markdown"].encode("utf-8")),
    )
    manifest_ref = store.put("turn-a", "context-manifest", context_manifest_to_payload(manifest))
    store.append(_event(2, "context.resolved", payload_ref=manifest_ref), expected_sequence=1)
    bridge = ExternalAgentContextBridge(
        store=store,
        adapters=[_adapter(), AgentAdapterProfile(
            adapter_id="claude", revision=1, template_revision="claude-template-r1",
            maximum_context_bytes=8192, supported_purposes=("project_assistance",),
        )],
        authority=lambda _: _authority(),
        clock=lambda: NOW,
    )
    return store, bridge, payload_ref


def _compacted_bridge(tmp_path: Path):
    store = SQLiteAITurnStore(tmp_path / "turns.sqlite3")
    request = _request(max_context_bytes=4096)
    store.claim_turn(request)
    store.append(_event(1, "turn.accepted"), expected_sequence=0)
    raw_markdown = "raw Cookie=must-never-leak C:\\private\\token.txt"
    compact_markdown = "sanitized compact summary"
    raw_ref = store.put("turn-a", "memory-r1-raw", {
        "schema_version": "1.0.0", "kind": "memory_r1", "project_id": "project-a",
        "object_id": "raw-a", "revision": "memory-r1-raw", "trust_status": "trusted",
        "markdown": raw_markdown,
    })
    compact_ref = store.put("turn-a", "memory-r1-compact", {
        "schema_version": "1.0.0", "kind": "memory_r1", "project_id": "project-a",
        "object_id": "compact-a", "revision": "memory-r1-compact", "trust_status": "trusted",
        "markdown": compact_markdown,
    })
    capability_ref = store.put("turn-a", "capability-manifest", {"capability_ids": []})
    raw_entry = ContextEntry(
        entry_id="memory-r1-raw", kind="memory_r1",
        source_ref="crp://memory/project-a/raw-a", payload_ref=raw_ref,
        source_project_id="project-a", revision_identity="memory-r1-raw",
        content_fingerprint=None, provenance_refs=("crp://sources/project-a/raw-a",),
        disclosure="model", selection_reason="long_session_source",
        content_bytes=len(raw_markdown.encode("utf-8")),
    )
    compact_entry = ContextEntry(
        entry_id="memory-r1-compact", kind="memory_r1",
        source_ref="crp://memory/project-a/compact-a", payload_ref=compact_ref,
        source_project_id="project-a", revision_identity="memory-r1-compact",
        content_fingerprint=None, provenance_refs=("crp://sources/project-a/raw-a",),
        disclosure="model", selection_reason="long_session_compaction",
        content_bytes=len(compact_markdown.encode("utf-8")),
    )
    manifest = ContextManifest(
        manifest_id="context-manifest-turn-a-compacted", turn_id="turn-a",
        resolver_id="fixture-compaction-resolver", project_id="project-a", series_id=None,
        project_profile_id="profile-project-a", project_profile_revision=4,
        boundary_profile_id="boundary-project-a", boundary_profile_revision=7,
        capability_manifest_ref=capability_ref, entries=(raw_entry, compact_entry),
        compactions=(ContextCompaction(
            compaction_id="compact-memory-r1", strategy="deterministic_excerpt",
            source_entry_ids=(raw_entry.entry_id,), output_entry_id=compact_entry.entry_id,
            input_bytes=raw_entry.content_bytes, output_bytes=compact_entry.content_bytes,
        ),),
        excluded_reason_counts=(), max_context_bytes=4096,
        selected_context_bytes=raw_entry.content_bytes + compact_entry.content_bytes,
    )
    manifest_ref = store.put("turn-a", "context-manifest", context_manifest_to_payload(manifest))
    store.append(_event(2, "context.resolved", payload_ref=manifest_ref), expected_sequence=1)
    bridge = ExternalAgentContextBridge(
        store=store, adapters=[_adapter()], authority=lambda _: _authority(), clock=lambda: NOW,
    )
    return store, bridge, raw_ref, compact_ref


def _start(bridge: ExternalAgentContextBridge, **overrides):
    values = {
        "adapter_id": "codex",
        "adapter_revision": 1,
        "template_revision": "template-r1",
        "turn_id": "turn-a",
        "project_id": "project-a",
        "purpose": "project_assistance",
        "requested_context_bytes": 4096,
        "admission": ExternalAgentAdmissionSnapshot(
            admission_id="start-context-map",
            project_id="project-a",
            adapter_id="codex",
            outcome="allow",
            policy_revision=7,
            reason_codes=("fixture_local_read_allowed",),
        ),
    }
    values.update(overrides)
    if "operation_id" in overrides and "admission" not in overrides:
        values["admission"] = replace(
            values["admission"], admission_id=str(values["operation_id"]),
        )
    if "adapter_id" in overrides and "admission" not in overrides:
        values["admission"] = replace(
            values["admission"], adapter_id=str(values["adapter_id"]),
        )
    values.setdefault("operation_id", f"start-{values['adapter_id']}")
    return bridge.start_session(**values)


def _adapter() -> AgentAdapterProfile:
    return AgentAdapterProfile(
        adapter_id="codex", revision=1, template_revision="template-r1",
        maximum_context_bytes=8192, supported_purposes=("project_assistance",),
    )


def _authority() -> ExternalAgentAuthoritySnapshot:
    return ExternalAgentAuthoritySnapshot(
        project_id="project-a", project_profile_id="profile-project-a",
        project_profile_revision=4, boundary_profile_id="boundary-project-a",
        boundary_profile_revision=7,
    )


def _request(*, max_context_bytes: int = 4096) -> dict[str, object]:
    return {
        "turn_id": "turn-a", "session_id": "session-a", "operation_id": "operation-a",
        "idempotency_key": "turn-key-a", "scope": {"project_id": "project-a", "series_id": None},
        "context_policy": {"max_context_bytes": max_context_bytes},
    }


def _event(sequence: int, event_type: str, *, payload_ref: str | None = None) -> dict[str, object]:
    return {
        "schema_version": "1.0.0", "event_id": f"event-{sequence}-fixture",
        "turn_id": "turn-a", "session_id": "session-a", "sequence": sequence,
        "type": event_type, "actor": "ai-kernel",
        "correlation": {"step_id": None, "tool_call_id": None, "model_request_id": None, "operation_id": "operation-a"},
        "data": {"status": "running", "summary": "private body", "capability_id": None,
                 "payload_ref": payload_ref, "receipt_ref": None, "evidence_refs": [],
                 "error_code": None, "retryable": False},
        "occurred_at": NOW.isoformat(),
    }


def _append_change(
    store: SQLiteAITurnStore, sequence: int, *, project_id: str,
    change_type: str, object_ref: str, object_revision: str, occurred_at: str,
) -> dict[str, object]:
    event = _event(sequence, "external_agent.change.projected")
    if project_id == "project-other":
        event.update({
            "turn_id": "turn-other", "session_id": "session-other",
            "event_id": f"event-other-{sequence}",
        })
        event["correlation"] = {
            **event["correlation"], "operation_id": "operation-other",
        }
    return store.append_external_agent_change(
        event, expected_sequence=sequence - 1, project_id=project_id,
        change_type=change_type, object_ref=object_ref,
        object_revision=object_revision, occurred_at=occurred_at,
    )


def _claim_other_project(store: SQLiteAITurnStore) -> None:
    request = {
        "turn_id": "turn-other", "session_id": "session-other", "operation_id": "operation-other",
        "idempotency_key": "turn-key-other", "scope": {"project_id": "project-other"},
    }
    store.claim_turn(request)
    event = _event(1, "turn.accepted")
    event.update({"turn_id": "turn-other", "session_id": "session-other", "event_id": "event-other-1"})
    event["correlation"] = {**event["correlation"], "operation_id": "operation-other"}
    store.append(event, expected_sequence=0)
