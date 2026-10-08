"""Secret and path canary Gate for the governed external-Agent Bridge.

This is deliberately an HTTP-level proof: template generation supplies the
frozen adapter revision, one adapter starts/resolves/proposes, a restarted
application serves the durable change to another adapter, and every public
projection plus the relevant SQLite records remains free of secret/path
canaries.  It is not a substitute for a real global client installation.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

from fastapi.testclient import TestClient

import backend.api.app as api_app
from backend.api.routes import ai as ai_routes
from backend.api.external_agent_context_runtime import (
    generate_external_agent_client_templates,
)
from core.ai_kernel import ContextEntry, ContextManifest, SQLiteAITurnStore, context_manifest_to_payload
from core.ai_kernel.external_agent_client_adapters import preview_install


PROJECT_ID = "project-secret-canary"
NOW = datetime(2026, 8, 29, 9, 0, tzinfo=timezone.utc).isoformat()
COOKIE_CANARY = "Cookie: session=canary-cookie-7Kp9Q4"
TOKEN_CANARY = "access_token=canary-token-2wV8mR"
PATH_CANARY = r"C:\\Users\\canary\\private\\source.txt"
CANARIES = (COOKIE_CANARY, TOKEN_CANARY, PATH_CANARY)


def test_external_agent_secret_canary_survives_template_http_restart_and_second_adapter(
    tmp_path: Path, monkeypatch,
) -> None:
    """Unsafe client input is rejected before durable write and never leaks."""
    monkeypatch.setattr(ai_routes, "desktop_session", lambda: object())
    monkeypatch.setattr(ai_routes, "desktop_session_authorized", lambda _value: True)
    context_ref = _seed_turn(tmp_path, turn_id="turn-secret-canary")
    templates = generate_external_agent_client_templates(target_ids={
        "codex": "codex-instructions", "claude": "claude-instructions",
        "workbuddy": "workbuddy-instructions",
    })
    installed_views = {
        adapter_id: preview_install(template, "# user-owned instructions\n")
        for adapter_id, template in templates.items()
    }
    _assert_safe(*[template.content for template in templates.values()])
    _assert_safe(*[json.dumps(view.receipt, ensure_ascii=False) for view in installed_views.values()])

    observed: list[str] = [
        *(template.content for template in templates.values()),
        *(json.dumps(view.receipt, ensure_ascii=False) for view in installed_views.values()),
    ]
    with TestClient(api_app.create_app(SimpleNamespace(root_dir=tmp_path))) as client:
        codex = _start(
            client, turn_id="turn-secret-canary", adapter_id="codex",
            template_revision=templates["codex"].template_revision,
        )
        observed.append(json.dumps(codex, ensure_ascii=False))
        resolved = _resolve(client, codex, context_ref, operation_id="secret-canary-codex-resolve")
        observed.append(json.dumps(resolved, ensure_ascii=False))
        # Claude's map is frozen before the publication event.  Its cursor is
        # therefore a durable proof that the restart does not replace the
        # second adapter with a newly-started, already-caught-up client.
        claude = _start(
            client, turn_id="turn-secret-canary", adapter_id="claude",
            template_revision=templates["claude"].template_revision,
        )
        observed.append(json.dumps(claude, ensure_ascii=False))

        for suffix, unsafe_value in (
            ("cookie", COOKIE_CANARY), ("token", TOKEN_CANARY), ("path", PATH_CANARY),
        ):
            rejected = client.post(
                f"/api/ai/external-agents/context-sessions/{codex['session_id']}/memory-proposals",
                json=_proposal_body(
                    session=codex, context_ref=context_ref,
                    operation_id=f"secret-canary-reject-{suffix}", unsafe_value=unsafe_value,
                ),
            )
            assert rejected.status_code == 400
            observed.append(rejected.text)

        proposal = client.post(
            f"/api/ai/external-agents/context-sessions/{codex['session_id']}/memory-proposals",
            json=_proposal_body(
                session=codex, context_ref=context_ref,
                operation_id="secret-canary-safe-proposal",
            ),
        )
        assert proposal.status_code == 201, proposal.text
        observed.append(proposal.text)

        empty = _changes(
            client, codex, after_cursor=codex["event_cursor"],
            operation_id="secret-canary-codex-changes",
        )
        observed.append(json.dumps(empty, ensure_ascii=False))
        acknowledgement = _acknowledge(
            client, codex, cursor=empty["next_cursor"], operation_id="secret-canary-codex-ack",
        )
        observed.append(json.dumps(acknowledgement, ensure_ascii=False))

    # The second adapter resumes only after process reconstruction and consumes
    # the same durable project event through its own frozen template identity.
    restarted = api_app.create_app(SimpleNamespace(root_dir=tmp_path))
    with TestClient(restarted) as client:
        changes = _changes(
            client, claude, after_cursor=claude["event_cursor"],
            operation_id="secret-canary-claude-changes",
        )
        assert [item["change_type"] for item in changes["changes"]] == ["memory.proposed"]
        observed.append(json.dumps(changes, ensure_ascii=False))
        acknowledgement = _acknowledge(
            client, claude, cursor=changes["next_cursor"], operation_id="secret-canary-claude-ack",
        )
        observed.append(json.dumps(acknowledgement, ensure_ascii=False))

    _assert_safe(*observed)
    _assert_safe(*_sqlite_text_values(tmp_path / ".rebuild-data" / "ai-turns.sqlite3"))
    _assert_safe(*_sqlite_text_values(tmp_path / ".rebuild-data" / "structured-records.sqlite3"))

    # Invalid proposals are normalized and rejected before the prepared-operation
    # reservation, so no delayed recovery path can revive their unsafe bodies.
    proposal_rows = _sqlite_rows(
        tmp_path / ".rebuild-data" / "ai-turns.sqlite3",
        "SELECT operation_id FROM ai_external_agent_proposal_operations ORDER BY operation_id",
    )
    assert proposal_rows == [("secret-canary-safe-proposal",)]


def _start(
    client: TestClient, *, turn_id: str, adapter_id: str, template_revision: str,
) -> dict[str, object]:
    response = client.post("/api/ai/external-agents/context-sessions", json={
        "operation_id": f"secret-canary-{adapter_id}-start", "confirm": True,
        "adapter_id": adapter_id, "adapter_revision": 1,
        "template_revision": template_revision, "turn_id": turn_id,
        "project_id": PROJECT_ID, "purpose": "project_assistance",
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
            "operation_id": operation_id, "context_refs": [context_ref],
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
            "operation_id": operation_id, "after_cursor": after_cursor,
            "purpose": "project_assistance",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _acknowledge(
    client: TestClient, session: dict[str, object], *, cursor: int, operation_id: str,
) -> dict[str, object]:
    response = client.post(
        f"/api/ai/external-agents/context-sessions/{session['session_id']}/acknowledgements",
        json={"operation_id": operation_id, "acknowledged_cursor": cursor, "purpose": "project_assistance"},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _proposal_body(
    *, session: dict[str, object], context_ref: str, operation_id: str, unsafe_value: str | None = None,
) -> dict[str, object]:
    proposed_content = "经过审核后才能让其他 Agent 读取的项目结论。"
    if unsafe_value is not None:
        proposed_content = unsafe_value
    return {
        "operation_id": operation_id,
        "expected_context_manifest_revision": session["context_manifest_revision"],
        "purpose": "project_assistance", "confirm": True,
        "proposal": {
            "proposal_type": "memory_candidate_proposal",
            "summary": "外部 Agent 提交项目记忆候选。",
            "source_refs": [context_ref], "evidence_refs": [context_ref],
            "suggested_changes": {
                "target_layer": "scenario", "candidate_type": "external_agent_memory",
                "proposed_content": proposed_content,
            },
            "requires_user_review": True,
        },
    }


def _seed_turn(tmp_path: Path, *, turn_id: str) -> str:
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
        "schema_version": "1.0.0", "skill_id": "secret-canary-project-skill",
        "skill_fingerprint": "secret-canary-project-skill-r1",
        "markdown": "仅含已授权且可公开的项目背景。",
    }
    payload_ref = store.put(turn_id, "application-skill-instructions-secret-canary", content)
    capability_ref = store.put(turn_id, "capability-manifest", {"capability_ids": []})
    manifest = ContextManifest(
        manifest_id=f"context-manifest-{turn_id}", turn_id=turn_id,
        resolver_id="external-agent-secret-canary-fixture", project_id=PROJECT_ID,
        series_id=None, project_profile_id=f"project-capability-{PROJECT_ID}",
        project_profile_revision=1, boundary_profile_id=f"project-boundary-{PROJECT_ID}",
        boundary_profile_revision=1, capability_manifest_ref=capability_ref,
        entries=(ContextEntry(
            entry_id="secret-canary-project-skill-entry", kind="application_skill",
            source_ref=f"crp://skills/{PROJECT_ID}/secret-canary-project-skill",
            payload_ref=payload_ref, source_project_id=PROJECT_ID,
            revision_identity="secret-canary-project-skill-r1", content_fingerprint=None,
            provenance_refs=(), disclosure="model", selection_reason="project_scope",
            content_bytes=len(content["markdown"].encode("utf-8")),
        ),),
        compactions=(), excluded_reason_counts=(), max_context_bytes=4096,
        selected_context_bytes=len(content["markdown"].encode("utf-8")),
    )
    manifest_ref = store.put(turn_id, "context-manifest", context_manifest_to_payload(manifest))
    store.append(_event(turn_id, 2, "context.resolved", payload_ref=manifest_ref), expected_sequence=1)
    return payload_ref


def _event(turn_id: str, sequence: int, event_type: str, *, payload_ref: str | None = None) -> dict[str, object]:
    return {
        "schema_version": "1.0.0", "event_id": f"event-{turn_id}-{sequence}",
        "turn_id": turn_id, "session_id": f"session-{turn_id}", "sequence": sequence,
        "type": event_type, "actor": "ai-kernel",
        "correlation": {"step_id": None, "tool_call_id": None, "model_request_id": None, "operation_id": f"operation-{turn_id}"},
        "data": {"status": "running", "summary": "private", "capability_id": None, "payload_ref": payload_ref, "receipt_ref": None, "evidence_refs": [], "error_code": None, "retryable": False},
        "occurred_at": NOW,
    }


def _sqlite_text_values(database: Path) -> tuple[str, ...]:
    connection = sqlite3.connect(database)
    try:
        tables = [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'",
        )]
        values: list[str] = []
        for table in tables:
            for row in connection.execute(f'SELECT * FROM "{table}"'):
                values.extend(value for value in row if isinstance(value, str))
        return tuple(values)
    finally:
        connection.close()


def _sqlite_rows(database: Path, query: str) -> list[tuple[object, ...]]:
    connection = sqlite3.connect(database)
    try:
        return connection.execute(query).fetchall()
    finally:
        connection.close()


def _assert_safe(*values: str) -> None:
    observed = "\n".join(values)
    for canary in CANARIES:
        assert canary not in observed
