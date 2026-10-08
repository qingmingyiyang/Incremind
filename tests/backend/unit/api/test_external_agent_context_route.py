from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.routes import ai as ai_routes
from backend.api.routes.ai import router
from backend.security.project_boundary_profiles import ProjectBoundaryProfileStore
from core.ai_kernel import (
    ContextEntry,
    ContextManifest,
    SQLiteAITurnStore,
    context_manifest_to_payload,
)


NOW = datetime(2026, 8, 28, 8, 0, tzinfo=timezone.utc).isoformat()


def _client(tmp_path) -> TestClient:
    application = FastAPI()
    application.state.container = SimpleNamespace(root_dir=tmp_path)
    application.include_router(router)
    return TestClient(application)


def test_external_agent_route_reads_existing_context_and_recovers_cursor(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setattr(ai_routes, "desktop_session", lambda: object())
    monkeypatch.setattr(ai_routes, "desktop_session_authorized", lambda _value: True)
    payload_ref = _seed_turn(tmp_path)
    client = _client(tmp_path)
    started = client.post("/api/ai/external-agents/context-sessions", json={
        "operation_id": "route-start-1",
        "confirm": True,
        "adapter_id": "codex",
        "adapter_revision": 1,
        "template_revision": "openai-skill-map-v1",
        "turn_id": "turn-route-a",
        "project_id": "project-route-a",
        "purpose": "project_assistance",
        "requested_context_bytes": 4096,
    })
    assert started.status_code == 201
    context_map = started.json()
    assert context_map["context_refs"][0]["context_ref"] == payload_ref

    resolved = client.post(
        f"/api/ai/external-agents/context-sessions/{context_map['session_id']}/resolve",
        json={
            "operation_id": "route-resolve-1",
            "context_refs": [payload_ref],
            "expected_context_manifest_revision": "context-manifest-turn-route-a",
            "purpose": "project_assistance",
        },
    )
    assert resolved.status_code == 200
    assert resolved.json()["slices"][0]["content"]["markdown"] == "route project background"

    changes = client.get(
        f"/api/ai/external-agents/context-sessions/{context_map['session_id']}/changes",
        params={
            "operation_id": "route-changes-1",
            "after_cursor": context_map["event_cursor"],
            "purpose": "project_assistance",
        },
    )
    assert changes.status_code == 200
    assert changes.json()["changes"] == []
    acknowledged = client.post(
        f"/api/ai/external-agents/context-sessions/{context_map['session_id']}/acknowledgements",
        json={
            "operation_id": "route-ack-1",
            "acknowledged_cursor": changes.json()["next_cursor"],
            "purpose": "project_assistance",
        },
    )
    assert acknowledged.status_code == 200
    assert acknowledged.json()["acknowledged_cursor"] == changes.json()["next_cursor"]
    assert "route project background" not in changes.text + acknowledged.text


def test_external_agent_change_route_returns_structured_retention_gap_and_allows_rebase(
    tmp_path, monkeypatch,
) -> None:
    _seed_turn(tmp_path)
    monkeypatch.setattr(ai_routes, "desktop_session", lambda: object())
    monkeypatch.setattr(ai_routes, "desktop_session_authorized", lambda _value: True)
    client = _client(tmp_path)
    start_body = {
        "operation_id": "route-retention-start", "confirm": True,
        "adapter_id": "codex", "adapter_revision": 1,
        "template_revision": "openai-skill-map-v1", "turn_id": "turn-route-a",
        "project_id": "project-route-a", "purpose": "project_assistance",
        "requested_context_bytes": 4096,
    }
    stale_map = client.post("/api/ai/external-agents/context-sessions", json=start_body).json()
    store = SQLiteAITurnStore(tmp_path / ".rebuild-data" / "ai-turns.sqlite3")
    first = store.ingest_external_agent_publication_change({
        "publication_identity": "route-retention-one", "project_id": "project-route-a",
        "change_type": "memory.published", "object_ref": "crp://memory/project-route-a/one",
        "object_revision": "memory-r1", "occurred_at": NOW,
    })
    second = store.ingest_external_agent_publication_change({
        "publication_identity": "route-retention-two", "project_id": "project-route-a",
        "change_type": "project_skill.published", "object_ref": "crp://skills/project-route-a/two",
        "object_revision": "skill-r2", "occurred_at": NOW,
    })
    store.prune_project_events_through("project-route-a", first["change"]["cursor"])
    gap = client.get(
        f"/api/ai/external-agents/context-sessions/{stale_map['session_id']}/changes",
        params={"operation_id": "route-retention-gap", "after_cursor": stale_map["event_cursor"],
                "purpose": "project_assistance"},
    )
    assert gap.status_code == 409
    assert gap.json() == {
        "detail": "External Agent context cursor retention gap",
        "reason": "project event cursor falls before retained authority; rebase is required",
        "code": "cursor_retention_gap", "project_id": "project-route-a",
        "after_cursor": 0, "retained_after_cursor": first["change"]["cursor"],
        "earliest_available_cursor": second["change"]["cursor"],
        "head_cursor": second["change"]["cursor"], "rebase_required": True,
        "rebase_action": "start_session",
    }
    rebased = client.post("/api/ai/external-agents/context-sessions", json={
        **start_body, "operation_id": "route-retention-rebase",
    })
    assert rebased.status_code == 201
    assert rebased.json()["event_cursor"] == second["change"]["cursor"]
    assert rebased.json()["retained_after_cursor"] == first["change"]["cursor"]


def test_external_agent_route_is_strict_local_and_does_not_build_ai_runtime(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setattr(ai_routes, "desktop_session", lambda: object())
    monkeypatch.setattr(ai_routes, "desktop_session_authorized", lambda _value: False)
    monkeypatch.setattr(
        ai_routes,
        "get_or_build_ai_runtime",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("AI Runtime must not start")),
    )
    client = _client(tmp_path)
    body = {
        "operation_id": "route-start-auth",
        "confirm": True,
        "adapter_id": "codex", "adapter_revision": 1,
        "template_revision": "openai-skill-map-v1", "turn_id": "turn-route-a",
        "project_id": "project-route-a", "purpose": "project_assistance",
        "requested_context_bytes": 4096,
    }
    assert client.post("/api/ai/external-agents/context-sessions", json=body).status_code == 403
    assert not (tmp_path / ".rebuild-data" / "ai-turns.sqlite3").exists()

    monkeypatch.setattr(ai_routes, "desktop_session", lambda: object())
    monkeypatch.setattr(ai_routes, "desktop_session_authorized", lambda _value: True)
    assert client.post(
        "/api/ai/external-agents/context-sessions", json={**body, "unexpected": True},
    ).status_code == 400
    assert client.get(
        "/api/ai/external-agents/context-sessions/agent-session-missing/changes",
        params={"operation_id": "bad", "after_cursor": "x", "purpose": "project_assistance"},
    ).status_code == 400

    unavailable = FastAPI()
    unavailable.state.container = SimpleNamespace()
    unavailable.include_router(router)
    assert TestClient(unavailable).post(
        "/api/ai/external-agents/context-sessions", json=body,
    ).status_code == 503


def test_external_agent_start_requires_confirmation_and_boundary_allow(
    tmp_path, monkeypatch,
) -> None:
    _seed_turn(tmp_path)
    monkeypatch.setattr(ai_routes, "desktop_session", lambda: object())
    monkeypatch.setattr(ai_routes, "desktop_session_authorized", lambda _value: True)
    client = _client(tmp_path)
    body = {
        "operation_id": "route-start-boundary", "confirm": True,
        "adapter_id": "codex", "adapter_revision": 1,
        "template_revision": "openai-skill-map-v1", "turn_id": "turn-route-a",
        "project_id": "project-route-a", "purpose": "project_assistance",
        "requested_context_bytes": 4096,
    }
    assert client.post(
        "/api/ai/external-agents/context-sessions", json={**body, "confirm": False},
    ).status_code == 400
    ProjectBoundaryProfileStore(tmp_path).update(
        "project-route-a", mode="guarded", remote_default="review",
        denied_effects=("read",), expected_revision=0,
    )
    denied = client.post("/api/ai/external-agents/context-sessions", json=body)
    assert denied.status_code == 400
    assert "not allowed" in denied.text


def test_external_agent_session_submits_idempotent_pending_memory_proposal(
    tmp_path, monkeypatch,
) -> None:
    payload_ref = _seed_turn(tmp_path)
    monkeypatch.setattr(ai_routes, "desktop_session", lambda: object())
    monkeypatch.setattr(ai_routes, "desktop_session_authorized", lambda _value: True)
    client = _client(tmp_path)
    started = client.post("/api/ai/external-agents/context-sessions", json={
        "operation_id": "route-start-proposal", "confirm": True,
        "adapter_id": "claude", "adapter_revision": 1,
        "template_revision": "claude-project-map-v1", "turn_id": "turn-route-a",
        "project_id": "project-route-a", "purpose": "project_assistance",
        "requested_context_bytes": 4096,
    })
    assert started.status_code == 201
    context_map = started.json()
    resolved = client.post(
        f"/api/ai/external-agents/context-sessions/{context_map['session_id']}/resolve",
        json={
            "operation_id": "route-resolve-proposal", "context_refs": [payload_ref],
            "expected_context_manifest_revision": context_map["context_manifest_revision"],
            "purpose": "project_assistance",
        },
    )
    assert resolved.status_code == 200
    body = {
        "operation_id": "route-proposal-1",
        "expected_context_manifest_revision": context_map["context_manifest_revision"],
        "purpose": "project_assistance", "confirm": True,
        "proposal": {
            "proposal_type": "memory_candidate_proposal",
            "summary": "建议把已授权的项目背景作为后续复盘候选。",
            "source_refs": [payload_ref], "evidence_refs": [payload_ref],
            "suggested_changes": {
                "target_layer": "atom", "candidate_type": "external_agent_memory",
                "proposed_content": "项目复盘应按需读取已授权的项目背景。",
            },
            "requires_user_review": True,
        },
    }
    url = f"/api/ai/external-agents/context-sessions/{context_map['session_id']}/memory-proposals"

    first = client.post(url, json=body)
    replay = client.post(url, json=body)

    assert first.status_code == 201
    assert replay.status_code == 201
    assert first.json()["status"] == "pending_review"
    assert first.json()["memory_publication_state"] == "not_published"
    assert first.json()["proposal_id"] == replay.json()["proposal_id"]
    assert replay.json()["replayed"] is True
    changes = client.get(
        f"/api/ai/external-agents/context-sessions/{context_map['session_id']}/changes",
        params={
            "operation_id": "route-changes-proposal-1",
            "after_cursor": context_map["event_cursor"], "purpose": "project_assistance",
        },
    )
    assert changes.status_code == 200
    assert [item["change_type"] for item in changes.json()["changes"]] == ["memory.proposed"]
    assert "建议把" not in changes.text

    from core.storage_provider import JsonObjectStore
    store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    assert len(store.list("external_agent_proposals")) == 1
    assert len(store.list("memory_candidates")) == 1
    assert store.list("memory_atoms") == ()
    assert store.list("memory_publications") == ()

    drift = client.post(url, json={
        **body,
        "proposal": {**body["proposal"], "summary": "漂移后的不同提案"},
    })
    assert drift.status_code == 409
    assert len(store.list("external_agent_proposals")) == 1


def test_external_agent_memory_proposal_rejects_unscoped_ref_and_missing_confirmation(
    tmp_path, monkeypatch,
) -> None:
    _seed_turn(tmp_path)
    monkeypatch.setattr(ai_routes, "desktop_session", lambda: object())
    monkeypatch.setattr(ai_routes, "desktop_session_authorized", lambda _value: True)
    client = _client(tmp_path)
    context_map = client.post("/api/ai/external-agents/context-sessions", json={
        "operation_id": "route-start-proposal-deny", "confirm": True,
        "adapter_id": "codex", "adapter_revision": 1,
        "template_revision": "openai-skill-map-v1", "turn_id": "turn-route-a",
        "project_id": "project-route-a", "purpose": "project_assistance",
        "requested_context_bytes": 4096,
    }).json()
    url = f"/api/ai/external-agents/context-sessions/{context_map['session_id']}/memory-proposals"
    body = {
        "operation_id": "route-proposal-deny",
        "expected_context_manifest_revision": context_map["context_manifest_revision"],
        "purpose": "project_assistance", "confirm": True,
        "proposal": {
            "proposal_type": "memory_candidate_proposal", "summary": "bad",
            "source_refs": ["crp://other/project/secret"],
            "evidence_refs": ["crp://other/project/secret"],
            "suggested_changes": {"proposed_content": "bad"},
            "requires_user_review": True,
        },
    }
    assert client.post(url, json={**body, "confirm": False}).status_code == 400
    rejected = client.post(url, json=body)
    assert rejected.status_code == 400
    assert "session-authorized" in rejected.text


def test_external_agent_memory_proposal_recovers_after_object_write_before_receipt(
    tmp_path, monkeypatch,
) -> None:
    payload_ref = _seed_turn(tmp_path)
    monkeypatch.setattr(ai_routes, "desktop_session", lambda: object())
    monkeypatch.setattr(ai_routes, "desktop_session_authorized", lambda _value: True)
    client = _client(tmp_path)
    context_map = client.post("/api/ai/external-agents/context-sessions", json={
        "operation_id": "route-start-crash", "confirm": True,
        "adapter_id": "codex", "adapter_revision": 1,
        "template_revision": "openai-skill-map-v1", "turn_id": "turn-route-a",
        "project_id": "project-route-a", "purpose": "project_assistance",
        "requested_context_bytes": 4096,
    }).json()
    client.post(
        f"/api/ai/external-agents/context-sessions/{context_map['session_id']}/resolve",
        json={
            "operation_id": "route-resolve-crash", "context_refs": [payload_ref],
            "expected_context_manifest_revision": context_map["context_manifest_revision"],
            "purpose": "project_assistance",
        },
    )
    body = {
        "operation_id": "route-proposal-crash",
        "expected_context_manifest_revision": context_map["context_manifest_revision"],
        "purpose": "project_assistance", "confirm": True,
        "proposal": {
            "proposal_type": "memory_candidate_proposal", "summary": "recoverable",
            "source_refs": [payload_ref], "evidence_refs": [payload_ref],
            "suggested_changes": {"target_layer": "atom", "proposed_content": "recoverable"},
            "requires_user_review": True,
        },
    }
    url = f"/api/ai/external-agents/context-sessions/{context_map['session_id']}/memory-proposals"
    original = SQLiteAITurnStore.finalize_external_agent_proposal_operation
    calls = 0

    def crash_once(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("crash after proposal write")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(SQLiteAITurnStore, "finalize_external_agent_proposal_operation", crash_once)
    assert client.post(url, json=body).status_code == 503
    recovered = client.post(url, json=body)
    assert recovered.status_code == 201
    assert recovered.json()["status"] == "pending_review"

    from core.storage_provider import JsonObjectStore
    store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    assert len(store.list("external_agent_proposals")) == 1
    assert len(store.list("memory_candidates")) == 1


def test_external_agent_memory_proposal_requires_write_boundary_allow(
    tmp_path, monkeypatch,
) -> None:
    payload_ref = _seed_turn(tmp_path)
    ProjectBoundaryProfileStore(tmp_path).update(
        "project-route-a", mode="guarded", remote_default="review",
        denied_effects=("write",), expected_revision=0,
    )
    monkeypatch.setattr(ai_routes, "desktop_session", lambda: object())
    monkeypatch.setattr(ai_routes, "desktop_session_authorized", lambda _value: True)
    client = _client(tmp_path)
    context_map = client.post("/api/ai/external-agents/context-sessions", json={
        "operation_id": "route-start-write-deny", "confirm": True,
        "adapter_id": "codex", "adapter_revision": 1,
        "template_revision": "openai-skill-map-v1", "turn_id": "turn-route-a",
        "project_id": "project-route-a", "purpose": "project_assistance",
        "requested_context_bytes": 4096,
    }).json()
    client.post(
        f"/api/ai/external-agents/context-sessions/{context_map['session_id']}/resolve",
        json={
            "operation_id": "route-resolve-write-deny", "context_refs": [payload_ref],
            "expected_context_manifest_revision": context_map["context_manifest_revision"],
            "purpose": "project_assistance",
        },
    )
    denied = client.post(
        f"/api/ai/external-agents/context-sessions/{context_map['session_id']}/memory-proposals",
        json={
            "operation_id": "route-proposal-write-deny", "confirm": True,
            "expected_context_manifest_revision": context_map["context_manifest_revision"],
            "purpose": "project_assistance",
            "proposal": {
                "proposal_type": "memory_candidate_proposal", "summary": "denied",
                "source_refs": [payload_ref], "evidence_refs": [payload_ref],
                "suggested_changes": {"proposed_content": "denied"},
                "requires_user_review": True,
            },
        },
    )
    assert denied.status_code == 400
    assert "not allowed" in denied.text


def _seed_turn(tmp_path) -> str:
    store = SQLiteAITurnStore(tmp_path / ".rebuild-data" / "ai-turns.sqlite3")
    request = {
        "turn_id": "turn-route-a", "session_id": "session-route-a",
        "operation_id": "operation-route-a", "idempotency_key": "turn-route-key-a",
        "scope": {"project_id": "project-route-a", "series_id": None},
        "context_policy": {"max_context_bytes": 4096},
    }
    store.claim_turn(request)
    store.append(_event(1, "turn.accepted"), expected_sequence=0)
    content = {
        "schema_version": "1.0.0", "skill_id": "skill-route-a",
        "skill_fingerprint": "fingerprint-route-a", "markdown": "route project background",
    }
    payload_ref = store.put("turn-route-a", "application-skill-instructions-skill-route-a", content)
    capability_ref = store.put("turn-route-a", "capability-manifest", {"capability_ids": []})
    manifest = ContextManifest(
        manifest_id="context-manifest-turn-route-a",
        turn_id="turn-route-a",
        resolver_id="route-fixture-resolver",
        project_id="project-route-a",
        series_id=None,
        project_profile_id="project-capability-project-route-a",
        project_profile_revision=1,
        boundary_profile_id="project-boundary-project-route-a",
        boundary_profile_revision=1,
        capability_manifest_ref=capability_ref,
        entries=(ContextEntry(
            entry_id="skill-route-entry", kind="application_skill",
            source_ref="crp://skills/project-route-a/skill-route-a",
            payload_ref=payload_ref, source_project_id="project-route-a",
            revision_identity="skill-route-r1", content_fingerprint=None,
            provenance_refs=(), disclosure="model", selection_reason="project_scope",
            content_bytes=len(content["markdown"].encode("utf-8")),
        ),),
        compactions=(), excluded_reason_counts=(), max_context_bytes=4096,
        selected_context_bytes=len(content["markdown"].encode("utf-8")),
    )
    manifest_ref = store.put(
        "turn-route-a", "context-manifest", context_manifest_to_payload(manifest),
    )
    store.append(_event(2, "context.resolved", payload_ref=manifest_ref), expected_sequence=1)
    return payload_ref


def _event(sequence: int, event_type: str, *, payload_ref: str | None = None) -> dict[str, object]:
    return {
        "schema_version": "1.0.0", "event_id": f"event-route-{sequence}",
        "turn_id": "turn-route-a", "session_id": "session-route-a", "sequence": sequence,
        "type": event_type, "actor": "ai-kernel",
        "correlation": {
            "step_id": None, "tool_call_id": None, "model_request_id": None,
            "operation_id": "operation-route-a",
        },
        "data": {
            "status": "running", "summary": "private", "capability_id": None,
            "payload_ref": payload_ref, "receipt_ref": None, "evidence_refs": [],
            "error_code": None, "retryable": False,
        },
        "occurred_at": NOW,
    }
