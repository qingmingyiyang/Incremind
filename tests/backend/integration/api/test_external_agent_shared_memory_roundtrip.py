from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient

import backend.api.app as api_app
from backend.api.routes import ai as ai_routes
from backend.api.published_project_memory_snapshot import PublishedProjectMemorySnapshotAuthority
from backend.api.rebuild_storage_runtime import build_rebuild_object_store
from core.aggregate_repository_factory import AggregateRepositoryFactory, STRUCTURED_DATABASE_NAME
from core.ai_kernel import (
    ContextEntry,
    ContextManifest,
    SQLiteAITurnStore,
    context_manifest_to_payload,
)
from core.storage_provider import JsonObjectStore, SQLiteStructuredRecordStore
from core.storage_provider.external_agent_publication_change import publication_outbox_collection


PROJECT_ID = "project-external-memory"
NOW = datetime(2026, 8, 29, 8, 0, tzinfo=timezone.utc).isoformat()


def test_external_agents_share_reviewed_published_memory_across_restart(
    tmp_path, monkeypatch,
) -> None:
    """A proposes only; user publication makes the same immutable revision readable later."""
    monkeypatch.setattr(ai_routes, "desktop_session", lambda: object())
    monkeypatch.setattr(ai_routes, "desktop_session_authorized", lambda _value: True)
    initial_ref = _seed_turn(tmp_path, turn_id="turn-external-a")

    first_application = api_app.create_app(SimpleNamespace(root_dir=tmp_path))
    with TestClient(first_application) as client:
        agent_a = _start(client, turn_id="turn-external-a", adapter_id="claude")
        _resolve(client, agent_a, initial_ref, operation_id="external-a-resolve")
        agent_b = _start(client, turn_id="turn-external-a", adapter_id="codex")
        proposal = client.post(
            f"/api/ai/external-agents/context-sessions/{agent_a['session_id']}/memory-proposals",
            json={
                "operation_id": "external-a-propose",
                "expected_context_manifest_revision": agent_a["context_manifest_revision"],
                "purpose": "project_assistance",
                "confirm": True,
                "proposal": {
                    "proposal_type": "memory_candidate_proposal",
                    "summary": "把已核验的外部 Agent 协作结论沉淀为项目场景。",
                    "source_refs": [initial_ref],
                    "evidence_refs": [initial_ref],
                    "suggested_changes": {
                        "target_layer": "scenario",
                        "candidate_type": "external_agent_memory",
                        "proposed_content": "项目协作采用候选审核后发布，再由其他 Agent 按需读取。",
                    },
                    "requires_user_review": True,
                },
            },
        )
        assert proposal.status_code == 201, proposal.text
        proposal_body = proposal.json()
        candidate_id = proposal_body["memory_candidate_id"]
        assert proposal_body["memory_publication_state"] == "not_published"

        proposed_changes = _changes(client, agent_b, after_cursor=agent_b["event_cursor"], operation_id="external-b-proposed")
        assert [item["change_type"] for item in proposed_changes["changes"]] == ["memory.proposed"]
        assert proposed_changes["changes"][0]["object_ref"].startswith(f"crp://proposals/{PROJECT_ID}/")
        assert SQLiteStructuredRecordStore(
            tmp_path / ".rebuild-data" / STRUCTURED_DATABASE_NAME,
        ).list("memory_scenarios") == ()

        reviewed = client.post(
            f"/api/rebuild/memory-candidates/{candidate_id}/review",
            json={
                "action": "promote_to_scenario",
                "reason": "用户确认外部协作结论。",
                "series_id": PROJECT_ID,
                "atom_ids": [],
            },
        )
        assert reviewed.status_code == 200, reviewed.text
        staged_id = reviewed.json()["promoted_object_id"]
        published = client.post(
            f"/api/rebuild/staging-scenarios/{staged_id}/publication",
            json={"confirm": True, "reason": "用户发布已审核外部协作结论。"},
        )
        assert published.status_code == 200, published.text
        published_body = published.json()
        assert published_body["memory_publication_state"] == "published_with_rollback_ref"
        pending_outbox = SQLiteStructuredRecordStore(
            tmp_path / ".rebuild-data" / STRUCTURED_DATABASE_NAME,
        ).read(
            publication_outbox_collection(PROJECT_ID), published_body["publication_id"],
        )
        assert pending_outbox is not None and pending_outbox.payload["state"] == "pending"
        assert _changes(client, agent_b, after_cursor=proposed_changes["next_cursor"], operation_id="external-b-before-restart")["changes"] == []

    # A new process drains the durable outbox. It must not re-run the proposal
    # or alter its pre-publication candidate state.
    restarted_application = api_app.create_app(SimpleNamespace(root_dir=tmp_path))
    with TestClient(restarted_application) as client:
        published_changes = _changes(
            client, agent_b, after_cursor=proposed_changes["next_cursor"], operation_id="external-b-published",
        )
        assert [item["change_type"] for item in published_changes["changes"]] == ["memory.published"]
        change = published_changes["changes"][0]
        assert change["object_ref"] == f"crp://memory/{PROJECT_ID}/{staged_id}"
        assert change["object_revision"] == "r1"

        _seed_published_memory_turn(
            tmp_path, turn_id="turn-external-after-publication", project_id=PROJECT_ID,
        )
        agent_after_publication = _start(
            client, turn_id="turn-external-after-publication", adapter_id="codex",
        )
        assert len(agent_after_publication["context_refs"]) == 1
        resolved = _resolve(
            client,
            agent_after_publication,
            agent_after_publication["context_refs"][0]["context_ref"],
            operation_id="external-b-resolve-published",
        )
        slice_ = resolved["slices"][0]
        assert slice_["kind"] == "memory_r2"
        assert slice_["revision"] == "1"
        assert slice_["content"]["object_id"] == staged_id
        assert slice_["content"]["markdown"] == "项目协作采用候选审核后发布，再由其他 Agent 按需读取。"


def _start(client: TestClient, *, turn_id: str, adapter_id: str) -> dict[str, object]:
    response = client.post("/api/ai/external-agents/context-sessions", json={
        "operation_id": f"{turn_id}-{adapter_id}-start",
        "confirm": True,
        "adapter_id": adapter_id,
        "adapter_revision": 1,
        "template_revision": (
            "claude-project-map-v1" if adapter_id == "claude" else "openai-skill-map-v1"
        ),
        "turn_id": turn_id,
        "project_id": PROJECT_ID,
        "purpose": "project_assistance",
        "requested_context_bytes": 4096,
    })
    assert response.status_code == 201, response.text
    return response.json()


def _resolve(
    client: TestClient, session: dict[str, object], context_ref: str, *, operation_id: str,
) -> dict[str, object]:
    response = client.post(
        f"/api/ai/external-agents/context-sessions/{session['session_id']}/resolve",
        json={
            "operation_id": operation_id,
            "context_refs": [context_ref],
            "expected_context_manifest_revision": session["context_manifest_revision"],
            "purpose": "project_assistance",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _changes(
    client: TestClient, session: dict[str, object], *, after_cursor: int, operation_id: str,
) -> dict[str, object]:
    response = client.get(
        f"/api/ai/external-agents/context-sessions/{session['session_id']}/changes",
        params={
            "operation_id": operation_id,
            "after_cursor": after_cursor,
            "purpose": "project_assistance",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _seed_turn(tmp_path, *, turn_id: str) -> str:
    store = SQLiteAITurnStore(tmp_path / ".rebuild-data" / "ai-turns.sqlite3")
    request = {
        "turn_id": turn_id, "session_id": f"session-{turn_id}",
        "operation_id": f"operation-{turn_id}", "idempotency_key": f"key-{turn_id}",
        "scope": {"project_id": PROJECT_ID, "series_id": None},
        "context_policy": {"max_context_bytes": 4096},
    }
    store.claim_turn(request)
    store.append(_event(turn_id, 1, "turn.accepted"), expected_sequence=0)
    content = {
        "schema_version": "1.0.0", "skill_id": "external-project-skill",
        "skill_fingerprint": "external-project-skill-r1",
        "markdown": "已授权的项目背景。",
    }
    payload_ref = store.put(turn_id, "application-skill-instructions-external", content)
    capability_ref = store.put(turn_id, "capability-manifest", {"capability_ids": []})
    manifest = ContextManifest(
        manifest_id=f"context-manifest-{turn_id}", turn_id=turn_id,
        resolver_id="external-agent-roundtrip-fixture", project_id=PROJECT_ID,
        series_id=None, project_profile_id=f"project-capability-{PROJECT_ID}",
        project_profile_revision=1, boundary_profile_id=f"project-boundary-{PROJECT_ID}",
        boundary_profile_revision=1, capability_manifest_ref=capability_ref,
        entries=(ContextEntry(
            entry_id="external-project-skill-entry", kind="application_skill",
            source_ref="crp://skills/project-external-memory/external-project-skill",
            payload_ref=payload_ref, source_project_id=PROJECT_ID,
            revision_identity="external-project-skill-r1", content_fingerprint=None,
            provenance_refs=(), disclosure="model", selection_reason="project_scope",
            content_bytes=len(content["markdown"].encode("utf-8")),
        ),),
        compactions=(), excluded_reason_counts=(), max_context_bytes=4096,
        selected_context_bytes=len(content["markdown"].encode("utf-8")),
    )
    manifest_ref = store.put(turn_id, "context-manifest", context_manifest_to_payload(manifest))
    store.append(_event(turn_id, 2, "context.resolved", payload_ref=manifest_ref), expected_sequence=1)
    return payload_ref


def _seed_published_memory_turn(tmp_path, *, turn_id: str, project_id: str) -> None:
    store = SQLiteAITurnStore(tmp_path / ".rebuild-data" / "ai-turns.sqlite3")
    request = {
        "turn_id": turn_id, "session_id": f"session-{turn_id}",
        "operation_id": f"operation-{turn_id}", "idempotency_key": f"key-{turn_id}",
        "scope": {"project_id": project_id, "series_id": None},
        "context_policy": {"max_context_bytes": 4096},
    }
    store.claim_turn(request)
    store.append(_event(turn_id, 1, "turn.accepted"), expected_sequence=0)
    capability_ref = store.put(turn_id, "capability-manifest", {"capability_ids": []})
    _runtime_store, settings = build_rebuild_object_store(tmp_path)
    json_store = JsonObjectStore(
        tmp_path / ".rebuild-data", legacy_root=tmp_path / "library",
        namespace_id=settings.namespace_id,
    )
    snapshot = PublishedProjectMemorySnapshotAuthority(
        factory=AggregateRepositoryFactory(
            runtime_root=tmp_path, namespace_id=settings.namespace_id, json_store=json_store,
        ),
        payloads=store,
    ).acquire(
        request, project_id=project_id, profile_id=f"project-capability-{project_id}",
        profile_revision=1,
    )
    selected = snapshot.payload["selected"]
    assert len(selected) == 1
    item = selected[0]
    manifest = ContextManifest(
        manifest_id=f"context-manifest-{turn_id}", turn_id=turn_id,
        resolver_id="published-project-memory-snapshot-v1", project_id=project_id,
        series_id=None, project_profile_id=f"project-capability-{project_id}",
        project_profile_revision=1, boundary_profile_id=f"project-boundary-{project_id}",
        boundary_profile_revision=1, capability_manifest_ref=capability_ref,
        entries=(ContextEntry(
            entry_id="published-project-memory-entry", kind=str(item["manifest_kind"]),
            source_ref=f"crp://memory/{project_id}/{item['object_id']}",
            payload_ref=str(item["payload_ref"]), source_project_id=project_id,
            revision_identity=str(item["revision"]), content_fingerprint=None,
            provenance_refs=(snapshot.payload_ref,), disclosure="model",
            selection_reason="published_project_memory_selected_within_remaining_budget",
            content_bytes=int(item["context_bytes"]),
        ),),
        compactions=(), excluded_reason_counts=(), max_context_bytes=4096,
        selected_context_bytes=int(item["context_bytes"]),
    )
    manifest_ref = store.put(turn_id, "context-manifest", context_manifest_to_payload(manifest))
    store.append(_event(turn_id, 2, "context.resolved", payload_ref=manifest_ref), expected_sequence=1)


def _event(turn_id: str, sequence: int, event_type: str, *, payload_ref: str | None = None) -> dict[str, object]:
    return {
        "schema_version": "1.0.0", "event_id": f"event-{turn_id}-{sequence}",
        "turn_id": turn_id, "session_id": f"session-{turn_id}", "sequence": sequence,
        "type": event_type, "actor": "ai-kernel",
        "correlation": {"step_id": None, "tool_call_id": None, "model_request_id": None, "operation_id": f"operation-{turn_id}"},
        "data": {"status": "running", "summary": "private", "capability_id": None, "payload_ref": payload_ref, "receipt_ref": None, "evidence_refs": [], "error_code": None, "retryable": False},
        "occurred_at": NOW,
    }
