from __future__ import annotations

import hashlib
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.security.secrets import InMemorySecretStore
from core.product_core.team_memory_candidate_import import (
    CreateTeamMemoryImportDraft,
    ObjectStoreTeamMemoryImportDraftRepository,
)
from core.product_core.team_memory_source_authority_saga import (
    CommitTeamMemoryStagingToSource,
    ObjectStoreTeamSourceAuthority,
)
from core.product_core.team_memory_source_recovery import (
    ForgetTeamCreatedSource,
)
from core.product_core.team_memory_source_staging import (
    PrepareTeamMemoryLocalSource,
    TeamMemorySourceStagingRepository,
)
from core.storage_provider import JsonObjectStore
from core.storage_provider import SQLiteStructuredRecordStore


def _client(tmp_path) -> tuple[TestClient, InMemorySecretStore]:
    secrets = InMemorySecretStore()
    return TestClient(create_app(SimpleNamespace(root_dir=tmp_path, secret_store=secrets))), secrets


def _staged_team_source(
    tmp_path,
    *,
    asset_type: str = "chat_memory",
    asset_id: str = "asset-api",
    content: str = "API 团队资料正文 CANARY_TEAM_SOURCE_BODY",
    asset_version: int = 1,
    project_id: str = "project-api",
):
    store = JsonObjectStore(
        tmp_path / ".rebuild-data",
        legacy_root=tmp_path / "library",
    )
    drafts = ObjectStoreTeamMemoryImportDraftRepository(store)
    draft = CreateTeamMemoryImportDraft(drafts=drafts).execute(
        profile={
            "enabled": True,
            "service_id": "service-1",
            "team_id": "team-1",
            "agent_id": "agent-1",
            "user_id": "user-1",
        },
        asset={
            "asset_id": asset_id,
            "team_id": "team-1",
            "asset_type": asset_type,
            "name": "API 团队资料",
            "visibility": "restricted",
            "status": "approved",
            "version": asset_version,
        },
        access_evidence={
            "service_id": "service-1",
            "team_id": "team-1",
            "agent_id": "agent-1",
            "user_id": "user-1",
            "asset_id": asset_id,
            "asset_version": asset_version,
            "action": "read",
            "inventory_fingerprint": "a" * 64,
            "verified_at": "2026-07-27T08:00:00+08:00",
        },
        project_id=project_id,
        target_layer="project_skill" if asset_type == "skill" else "series_memory",
        candidate_type="other" if asset_type == "skill" else "answer_summary",
        content=content,
        content_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        consent_id="consent-api-0001",
        confirmed=True,
        created_at="2026-07-27T08:01:00+08:00",
    )
    staging = TeamMemorySourceStagingRepository(store)
    preparer = PrepareTeamMemoryLocalSource(
        drafts=drafts,
        staging=staging,
    )
    preview = preparer.preview(draft.draft_id)
    staged = preparer.stage(
        draft.draft_id,
        preview_id=preview.preview_id,
        expected_draft_revision=preview.draft_revision,
        confirmed=True,
        staged_at="2026-07-27T08:02:00+08:00",
    )
    return store, drafts, staging, staged, content


def test_team_memory_profile_and_secrets_api_are_default_off_cas_and_redacted(tmp_path) -> None:
    client, secrets = _client(tmp_path)
    initial = client.get("/api/rebuild/team-memory/profile")
    assert initial.status_code == 200
    assert initial.json()["enabled"] is False
    assert initial.json()["revision"] == 0
    assert initial.json()["sync_available"] is False

    secret_response = client.put(
        "/api/rebuild/team-memory/secrets",
        json={"service_api_key": "service-canary", "user_key": "user-canary"},
    )
    assert secret_response.status_code == 200
    assert secret_response.json() == {"has_service_api_key": True, "has_user_key": True}
    assert secrets.get_snapshot("team-memory:service-api-key").value == "service-canary"

    saved = client.put(
        "/api/rebuild/team-memory/profile",
        json={
            "enabled": True,
            "endpoint": "https://memory.example.test",
            "service_id": "service-1",
            "team_id": "team-1",
            "agent_id": "agent-1",
            "user_id": "user-1",
            "expected_revision": 0,
            "confirm_enable": True,
        },
    )
    assert saved.status_code == 200
    assert saved.json()["revision"] == 1
    assert "service-canary" not in saved.text
    assert "user-canary" not in saved.text
    stale = client.put(
        "/api/rebuild/team-memory/profile",
        json={
            "enabled": True,
            "endpoint": "https://memory.example.test",
            "service_id": "service-1",
            "team_id": "team-1",
            "agent_id": "agent-1",
            "user_id": "user-1",
            "expected_revision": 0,
            "confirm_enable": True,
        },
    )
    assert stale.status_code == 409


def test_team_memory_preflight_requires_explicit_consent_and_accepts_no_endpoint_override(tmp_path) -> None:
    client, _secrets = _client(tmp_path)
    denied = client.post("/api/rebuild/team-memory/preflight", json={"consented": False})
    assert denied.status_code == 400
    override = client.post(
        "/api/rebuild/team-memory/preflight",
        json={"consented": True, "endpoint": "https://attacker.invalid"},
    )
    assert override.status_code == 422


def test_team_memory_asset_inventory_requires_consent_and_accepts_no_scope_override(tmp_path) -> None:
    client, _secrets = _client(tmp_path)
    denied = client.post("/api/rebuild/team-memory/asset-inventory", json={"consented": False})
    assert denied.status_code == 400
    override = client.post(
        "/api/rebuild/team-memory/asset-inventory",
        json={"consented": True, "team_id": "attacker-team"},
    )
    assert override.status_code == 422


def test_team_memory_disconnect_is_confirmed_cas_redacted_and_restart_persistent(tmp_path) -> None:
    client, secrets = _client(tmp_path)
    assert client.put(
        "/api/rebuild/team-memory/secrets",
        json={"service_api_key": "service-canary", "user_key": "user-canary"},
    ).status_code == 200
    saved = client.put(
        "/api/rebuild/team-memory/profile",
        json={
            "enabled": True,
            "endpoint": "https://memory.example.test",
            "service_id": "service-1",
            "team_id": "team-1",
            "agent_id": "agent-1",
            "user_id": "user-1",
            "expected_revision": 0,
            "confirm_enable": True,
        },
    )
    assert saved.status_code == 200
    assert client.post(
        "/api/rebuild/team-memory/disconnect",
        json={"expected_revision": 1, "confirmed": False},
    ).status_code == 400
    assert client.post(
        "/api/rebuild/team-memory/disconnect",
        json={"expected_revision": 0, "confirmed": True},
    ).status_code == 409
    disconnected = client.post(
        "/api/rebuild/team-memory/disconnect",
        json={"expected_revision": 1, "confirmed": True},
    )
    assert disconnected.status_code == 200
    assert disconnected.json() == {
        "enabled": False,
        "endpoint": "",
        "service_id": "",
        "team_id": "",
        "agent_id": "",
        "user_id": "",
        "revision": 2,
        "schema_version": "1.0.0",
        "updated_at": disconnected.json()["updated_at"],
        "has_service_api_key": False,
        "has_user_key": False,
        "sync_available": False,
        "import_available": False,
        "export_available": False,
    }
    assert "service-canary" not in disconnected.text
    assert "user-canary" not in disconnected.text
    assert secrets.get_snapshot("team-memory:service-api-key").value == ""
    assert secrets.get_snapshot("team-memory:user-key").value == ""
    restarted = TestClient(create_app(SimpleNamespace(root_dir=tmp_path, secret_store=secrets)))
    assert restarted.get("/api/rebuild/team-memory/profile").json() == disconnected.json()


def test_team_memory_fresh_api_preflight_is_read_only_redacted_and_restart_persistent(tmp_path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok", "version": "2.0.0-beta.1", "uptime": 2, "stores": {}})
        return httpx.Response(200, json={"code": 0, "data": {"valid": True, "user": {"user_id": "user-1"}}})

    secrets = InMemorySecretStore()
    transport_client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
    container = SimpleNamespace(
        root_dir=tmp_path,
        secret_store=secrets,
        team_memory_preflight_client=transport_client,
        team_memory_preflight_resolver=lambda _host, _port: ("93.184.216.34",),
    )
    client = TestClient(create_app(container))
    assert client.put(
        "/api/rebuild/team-memory/secrets",
        json={"service_api_key": "service-canary", "user_key": "user-canary"},
    ).status_code == 200
    assert client.put(
        "/api/rebuild/team-memory/profile",
        json={
            "enabled": True,
            "endpoint": "https://memory.example.test",
            "service_id": "service-1",
            "team_id": "team-1",
            "agent_id": "agent-1",
            "user_id": "user-1",
            "expected_revision": 0,
            "confirm_enable": True,
        },
    ).status_code == 200

    preflight = client.post("/api/rebuild/team-memory/preflight", json={"consented": True})
    assert preflight.status_code == 200
    assert preflight.json() == {
        "status": "compatible_read_only_preflight",
        "endpoint_origin": "https://memory.example.test",
        "server_health": "ok",
        "server_version": "2.0.0-beta.1",
        "resolved_user_id": "user-1",
        "capabilities": [
            {"capability": "health", "state": "verified"},
            {"capability": "auth_verify", "state": "verified"},
            {"capability": "asset_acl", "state": "advertised_unverified"},
            {"capability": "skill_expected_version", "state": "advertised_unverified"},
            {"capability": "knowledge_tools", "state": "advertised_unverified"},
            {"capability": "asset_sync", "state": "disabled"},
        ],
        "sync_available": False,
        "import_available": False,
        "export_available": False,
    }
    assert requests[1].headers["authorization"] == "Bearer service-canary"
    assert requests[1].content == b'{"user_key":"user-canary"}'

    profile_file = tmp_path / "library" / "global" / "team-memory" / "profile.json"
    persisted = profile_file.read_text(encoding="utf-8")
    assert "service-canary" not in persisted
    assert "user-canary" not in persisted
    restarted = TestClient(create_app(SimpleNamespace(root_dir=tmp_path, secret_store=secrets)))
    restarted_profile = restarted.get("/api/rebuild/team-memory/profile")
    assert restarted_profile.status_code == 200
    assert restarted_profile.json()["enabled"] is True
    assert restarted_profile.json()["revision"] == 1
    assert restarted_profile.json()["has_service_api_key"] is True


def test_team_memory_asset_inventory_is_acl_scoped_redacted_and_zero_persistence(tmp_path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = __import__("json").loads(request.content)
        asset_type = body["asset_type"]
        return httpx.Response(200, json={
            "code": 0,
            "data": {
                "items": [{
                    "asset_id": f"{asset_type}-asset",
                    "team_id": "team-1",
                    "asset_type": asset_type,
                    "name": f"Remote {asset_type}",
                    "description": "REMOTE_BODY_CANARY",
                    "owner_user_id": "owner-private",
                    "source_ref": "REMOTE_SOURCE_CANARY",
                    "content_ref": "REMOTE_CONTENT_CANARY",
                    "metadata_json": "{\"private\":true}",
                    "visibility": "restricted",
                    "status": "approved",
                    "version": 2,
                }],
                "total": 1,
            },
        })

    secrets = InMemorySecretStore()
    transport_client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
    container = SimpleNamespace(
        root_dir=tmp_path,
        secret_store=secrets,
        team_memory_asset_client=transport_client,
        team_memory_asset_resolver=lambda _host, _port: ("93.184.216.34",),
    )
    client = TestClient(create_app(container))
    assert client.put(
        "/api/rebuild/team-memory/secrets",
        json={"service_api_key": "service-canary", "user_key": "user-canary"},
    ).status_code == 200
    assert client.put(
        "/api/rebuild/team-memory/profile",
        json={
            "enabled": True,
            "endpoint": "https://memory.example.test",
            "service_id": "service-1",
            "team_id": "team-1",
            "agent_id": "agent-1",
            "user_id": "user-1",
            "expected_revision": 0,
            "confirm_enable": True,
        },
    ).status_code == 200
    before = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }

    response = client.post("/api/rebuild/team-memory/asset-inventory", json={"consented": True})
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "read_only_asset_inventory"
    assert payload["counts"] == {"chat_memory": 1, "skill": 1, "llm_wiki": 1, "code_graph": 1}
    assert [item["asset_type"] for item in payload["items"]] == ["chat_memory", "skill", "llm_wiki", "code_graph"]
    assert payload["persisted"] is False
    assert payload["content_fetched"] is False
    assert payload["sync_available"] is False
    assert payload["import_available"] is False
    assert payload["export_available"] is False
    serialized = response.text
    for forbidden in ("service-canary", "user-canary", "REMOTE_BODY_CANARY", "REMOTE_SOURCE_CANARY", "REMOTE_CONTENT_CANARY", "owner-private", "metadata_json"):
        assert forbidden not in serialized
    after = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert after == before
    assert len(requests) == 4
    assert all(request.headers["x-tdai-user-key"] == "user-canary" for request in requests)
    assert all(__import__("json").loads(request.content)["action"] == "read" for request in requests)


def test_team_source_recovery_api_diagnoses_and_abandons_conflict_without_body(
    tmp_path,
) -> None:
    store, drafts, staging, staged, content = _staged_team_source(tmp_path)
    with pytest.raises(RuntimeError, match="claimed"):
        CommitTeamMemoryStagingToSource(
            drafts=drafts,
            staging=staging,
            sources=ObjectStoreTeamSourceAuthority(store),
            after_claimed=lambda: (_ for _ in ()).throw(
                RuntimeError("claimed")
            ),
        ).execute(
            staged.staging_id,
            expected_staging_revision=1,
            confirmed=True,
            completed_at="2026-07-27T08:03:00+08:00",
        )
    proposal = dict(staging.get(staged.staging_id)["proposed_source"])
    conflicting = {**proposal, "title": "existing unrelated Source"}
    store.write(
        "sources",
        str(proposal["id"]),
        conflicting,
        expected_revision=0,
    )
    client, _secrets = _client(tmp_path)

    diagnosis = client.get(
        f"/api/rebuild/team-memory/source-staging/{staged.staging_id}/recovery"
    )
    assert diagnosis.status_code == 200
    assert diagnosis.json()["status"] == "source_identity_conflict"
    assert diagnosis.json()["content_included"] is False
    assert content not in diagnosis.text

    denied = client.post(
        f"/api/rebuild/team-memory/source-staging/{staged.staging_id}/abandon",
        json={
            "expected_staging_revision": 2,
            "reason": "保留既有 Source。",
            "confirmed": False,
            "abandoned_at": "2026-07-27T08:04:00+08:00",
        },
    )
    assert denied.status_code == 400
    abandoned = client.post(
        f"/api/rebuild/team-memory/source-staging/{staged.staging_id}/abandon",
        json={
            "expected_staging_revision": 2,
            "reason": "保留既有 Source。",
            "confirmed": True,
            "abandoned_at": "2026-07-27T08:04:00+08:00",
        },
    )
    assert abandoned.status_code == 200
    assert abandoned.json()["status"] == "abandoned"
    assert abandoned.json()["replayed"] is False
    assert content not in abandoned.text
    assert store.read("sources", str(proposal["id"])) == conflicting

    replay = client.post(
        f"/api/rebuild/team-memory/source-staging/{staged.staging_id}/abandon",
        json={
            "expected_staging_revision": 2,
            "reason": "保留既有 Source。",
            "confirmed": True,
            "abandoned_at": "2026-07-27T08:05:00+08:00",
        },
    )
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True


def test_team_source_staging_list_is_content_free_and_resume_uses_claim_identity(
    tmp_path,
) -> None:
    store, drafts, staging, staged, content = _staged_team_source(tmp_path)
    with pytest.raises(RuntimeError, match="claimed"):
        CommitTeamMemoryStagingToSource(
            drafts=drafts,
            staging=staging,
            sources=ObjectStoreTeamSourceAuthority(store),
            after_claimed=lambda: (_ for _ in ()).throw(
                RuntimeError("claimed")
            ),
        ).execute(
            staged.staging_id,
            expected_staging_revision=1,
            confirmed=True,
            completed_at="2026-07-27T08:03:00+08:00",
        )
    client, _secrets = _client(tmp_path)
    listed = client.get("/api/rebuild/team-memory/source-staging")
    assert listed.status_code == 200
    assert listed.json()["total"] == 1
    item = listed.json()["items"][0]
    assert item["staging_revision"] == 2
    assert item["claimed_staging_revision"] == 1
    assert item["recovery_status"] == "resume_source_missing"
    assert item["content_included"] is False
    assert content not in listed.text

    denied = client.post(
        f"/api/rebuild/team-memory/source-staging/{staged.staging_id}/resume",
        json={
            "claimed_staging_revision": 1,
            "confirmed": False,
            "completed_at": "2026-07-27T08:04:00+08:00",
        },
    )
    assert denied.status_code == 400
    resumed = client.post(
        f"/api/rebuild/team-memory/source-staging/{staged.staging_id}/resume",
        json={
            "claimed_staging_revision": 1,
            "confirmed": True,
            "completed_at": "2026-07-27T08:04:00+08:00",
        },
    )
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "completed"
    assert resumed.json()["content_included"] is False
    assert content not in resumed.text
    assert store.read("sources", resumed.json()["source_id"]) is not None


def test_team_source_hard_forget_api_is_confirmed_cas_private_and_restart_safe(
    tmp_path,
) -> None:
    store, drafts, staging, staged, content = _staged_team_source(tmp_path)
    completed = CommitTeamMemoryStagingToSource(
        drafts=drafts,
        staging=staging,
        sources=ObjectStoreTeamSourceAuthority(store),
    ).execute(
        staged.staging_id,
        expected_staging_revision=1,
        confirmed=True,
        completed_at="2026-07-27T08:03:00+08:00",
    )
    client, secrets = _client(tmp_path)
    endpoint = (
        f"/api/rebuild/team-memory/source-staging/"
        f"{staged.staging_id}/hard-forget"
    )
    denied = client.post(
        endpoint,
        json={
            "expected_staging_revision": 3,
            "reason": "用户撤回。",
            "confirmed": False,
            "forgotten_at": "2026-07-27T08:04:00+08:00",
        },
    )
    assert denied.status_code == 400
    forgotten = client.post(
        endpoint,
        json={
            "expected_staging_revision": 3,
            "reason": "用户撤回。",
            "confirmed": True,
            "forgotten_at": "2026-07-27T08:04:00+08:00",
        },
    )
    assert forgotten.status_code == 200
    assert forgotten.json()["status"] == "forgotten"
    assert forgotten.json()["replayed"] is False
    assert forgotten.json()["content_included"] is False
    assert content not in forgotten.text
    assert store.read_including_deleted("sources", completed.source_id) is None

    restarted = TestClient(
        create_app(SimpleNamespace(root_dir=tmp_path, secret_store=secrets))
    )
    replay = restarted.post(
        endpoint,
        json={
            "expected_staging_revision": 3,
            "reason": "用户撤回。",
            "confirmed": True,
            "forgotten_at": "2026-07-27T08:05:00+08:00",
        },
    )
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    persisted = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (tmp_path / ".rebuild-data").rglob("*.json")
    )
    assert content not in persisted


def test_team_source_hard_forget_api_fails_closed_on_dependency_and_extra_fields(
    tmp_path,
) -> None:
    store, drafts, staging, staged, _content = _staged_team_source(tmp_path)
    completed = CommitTeamMemoryStagingToSource(
        drafts=drafts,
        staging=staging,
        sources=ObjectStoreTeamSourceAuthority(store),
    ).execute(
        staged.staging_id,
        expected_staging_revision=1,
        confirmed=True,
        completed_at="2026-07-27T08:03:00+08:00",
    )
    store.write(
        "memory_atoms",
        "atom-api-dependent",
        {
            "id": "atom-api-dependent",
            "source_refs": [
                {
                    "source_id": completed.source_id,
                    "locator": "source:content",
                }
            ],
        },
        expected_revision=0,
    )
    client, _secrets = _client(tmp_path)
    endpoint = (
        f"/api/rebuild/team-memory/source-staging/"
        f"{staged.staging_id}/hard-forget"
    )
    blocked = client.post(
        endpoint,
        json={
            "expected_staging_revision": 3,
            "reason": "不能绕过下游。",
            "confirmed": True,
            "forgotten_at": "2026-07-27T08:04:00+08:00",
        },
    )
    assert blocked.status_code == 409
    assert "memory_atoms/atom-api-dependent" in blocked.json()["detail"]
    assert store.read("sources", completed.source_id) is not None
    assert staging.get(staged.staging_id)["status"] == "completed"

    override = client.post(
        endpoint,
        json={
            "expected_staging_revision": 3,
            "reason": "不能绕过下游。",
            "confirmed": True,
            "forgotten_at": "2026-07-27T08:04:00+08:00",
            "cascade": True,
        },
    )
    assert override.status_code == 422


@pytest.mark.parametrize("action", ["reject", "withdraw"])
def test_team_source_candidate_api_preview_stage_dispose_restart_and_forget(
    tmp_path,
    action: str,
) -> None:
    store, drafts, staging, staged, content = _staged_team_source(tmp_path)
    completed = CommitTeamMemoryStagingToSource(
        drafts=drafts,
        staging=staging,
        sources=ObjectStoreTeamSourceAuthority(store),
    ).execute(
        staged.staging_id,
        expected_staging_revision=1,
        confirmed=True,
        completed_at="2026-07-27T10:00:00+08:00",
    )
    client, secrets = _client(tmp_path)
    preview_endpoint = (
        f"/api/rebuild/team-memory/source-staging/"
        f"{staged.staging_id}/candidate-preview"
    )
    preview_response = client.get(preview_endpoint)
    assert preview_response.status_code == 200
    preview = preview_response.json()
    assert preview["proposed_content"] == content
    assert preview["content_included"] is True
    assert preview["target_layer"] == "series_memory"
    assert preview["source_revision"] == completed.source_revision
    assert preview["safety"]["publication_created"] is False

    denied = client.post(
        f"/api/rebuild/team-memory/source-staging/{staged.staging_id}/candidate",
        json={
            "preview_id": preview["preview_id"],
            "expected_staging_revision": preview["staging_revision"],
            "expected_source_revision": preview["source_revision"],
            "confirmed": False,
            "created_at": "2026-07-27T10:01:00+08:00",
        },
    )
    assert denied.status_code == 400
    staged_response = client.post(
        f"/api/rebuild/team-memory/source-staging/{staged.staging_id}/candidate",
        json={
            "preview_id": preview["preview_id"],
            "expected_staging_revision": preview["staging_revision"],
            "expected_source_revision": preview["source_revision"],
            "confirmed": True,
            "created_at": "2026-07-27T10:01:00+08:00",
        },
    )
    assert staged_response.status_code == 200
    candidate_id = staged_response.json()["candidate_id"]
    assert staged_response.json()["publication_created"] is False
    listed = client.get("/api/rebuild/team-memory/source-staging")
    item = listed.json()["items"][0]
    assert item["candidate_id"] == candidate_id
    assert item["candidate_status"] == "pending_review"
    assert item["candidate_revision"] == 1
    assert item["candidate_content_erased"] is False
    assert content not in listed.text

    disposition_endpoint = (
        f"/api/rebuild/team-memory/candidates/{candidate_id}/disposition"
    )
    disposition = client.post(
        disposition_endpoint,
        json={
            "expected_candidate_revision": 1,
            "action": action,
            "reason": "用户不保留本地候选。",
            "confirmed": True,
            "reviewed_at": "2026-07-27T10:02:00+08:00",
        },
    )
    assert disposition.status_code == 200
    assert disposition.json()["content_erased"] is True
    assert content not in disposition.text

    restarted = TestClient(
        create_app(SimpleNamespace(root_dir=tmp_path, secret_store=secrets))
    )
    replay = restarted.post(
        disposition_endpoint,
        json={
            "expected_candidate_revision": 1,
            "action": action,
            "reason": "用户不保留本地候选。",
            "confirmed": True,
            "reviewed_at": "2026-07-27T10:03:00+08:00",
        },
    )
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    relisted = restarted.get("/api/rebuild/team-memory/source-staging")
    terminal_item = relisted.json()["items"][0]
    assert terminal_item["candidate_status"] == (
        "rejected" if action == "reject" else "withdrawn"
    )
    assert terminal_item["candidate_content_erased"] is True
    assert content not in relisted.text

    forgotten = restarted.post(
        f"/api/rebuild/team-memory/source-staging/{staged.staging_id}/hard-forget",
        json={
            "expected_staging_revision": preview["staging_revision"],
            "reason": "用户要求删除本地副本。",
            "confirmed": True,
            "forgotten_at": "2026-07-27T10:04:00+08:00",
        },
    )
    assert forgotten.status_code == 200
    assert forgotten.json()["status"] == "forgotten"
    persisted = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (tmp_path / ".rebuild-data").rglob("*.json")
    )
    assert content not in persisted


@pytest.mark.parametrize("target_layer", ["atom", "scenario"])
def test_team_source_extraction_api_previews_and_stages_one_review_item(
    tmp_path,
    target_layer: str,
) -> None:
    store, drafts, staging, staged, content = _staged_team_source(tmp_path)
    completed = CommitTeamMemoryStagingToSource(
        drafts=drafts,
        staging=staging,
        sources=ObjectStoreTeamSourceAuthority(store),
    ).execute(
        staged.staging_id,
        expected_staging_revision=1,
        confirmed=True,
        completed_at="2026-07-27T10:00:00+08:00",
    )
    client, secrets = _client(tmp_path)
    endpoint = (
        f"/api/rebuild/team-memory/source-staging/{staged.staging_id}"
    )
    preview_response = client.get(f"{endpoint}/extraction-preview")
    assert preview_response.status_code == 200
    preview = preview_response.json()
    assert preview["source_revision"] == completed.source_revision
    assert preview["source_content_sha256"] == hashlib.sha256(
        content.encode("utf-8")
    ).hexdigest()
    assert preview["safety"]["provider_called"] is False
    assert preview["safety"]["publication_created"] is False
    assert str(tmp_path) not in preview_response.text
    assert store.list("memory_candidates") == ()
    selected = next(
        item for item in preview["items"] if item["target_layer"] == target_layer
    )
    assert selected["source_quote"] == content[
        selected["start_char"] : selected["end_char"]
    ]

    denied = client.post(
        f"{endpoint}/extraction-candidate",
        json={
            "preview_id": preview["preview_id"],
            "item_id": selected["item_id"],
            "target_layer": target_layer,
            "expected_staging_revision": preview["staging_revision"],
            "expected_source_revision": preview["source_revision"],
            "confirmed": False,
            "created_at": "2026-07-27T10:01:00+08:00",
        },
    )
    assert denied.status_code == 400
    payload = {
        "preview_id": preview["preview_id"],
        "item_id": selected["item_id"],
        "target_layer": target_layer,
        "expected_staging_revision": preview["staging_revision"],
        "expected_source_revision": preview["source_revision"],
        "confirmed": True,
        "created_at": "2026-07-27T10:01:00+08:00",
    }
    created = client.post(f"{endpoint}/extraction-candidate", json=payload)
    replayed = client.post(f"{endpoint}/extraction-candidate", json=payload)
    assert created.status_code == replayed.status_code == 200
    assert created.json()["target_layer"] == target_layer
    assert created.json()["publication_created"] is False
    assert replayed.json()["candidate_id"] == created.json()["candidate_id"]
    assert replayed.json()["replayed"] is True

    restarted = TestClient(
        create_app(SimpleNamespace(root_dir=tmp_path, secret_store=secrets))
    )
    listed = restarted.get("/api/rebuild/team-memory/source-staging")
    assert listed.status_code == 200
    listed_item = listed.json()["items"][0]
    assert listed_item["has_active_source_candidates"] is True
    assert {
        value["candidate_id"] for value in listed_item["source_candidates"]
    } == {created.json()["candidate_id"]}
    assert content not in listed.text
    assert store.list("memory_publications") == ()

    candidate_id = created.json()["candidate_id"]
    candidate_summary = next(
        value
        for value in listed_item["source_candidates"]
        if value["candidate_id"] == candidate_id
    )
    disposed = restarted.post(
        f"/api/rebuild/team-memory/candidates/{candidate_id}/disposition",
        json={
            "expected_candidate_revision": candidate_summary["candidate_revision"],
            "action": "withdraw",
            "reason": "用户撤回结构化提取候选。",
            "confirmed": True,
            "reviewed_at": "2026-07-27T10:02:00+08:00",
        },
    )
    assert disposed.status_code == 200
    erased_candidate = store.read("memory_candidates", candidate_id)
    assert erased_candidate is not None
    assert erased_candidate["proposed_content"] == "[content erased]"
    assert all("quote" not in ref for ref in erased_candidate["source_refs"])

    forgotten = restarted.post(
        f"{endpoint}/hard-forget",
        json={
            "expected_staging_revision": preview["staging_revision"],
            "reason": "用户删除已撤回提取项的本地来源。",
            "confirmed": True,
            "forgotten_at": "2026-07-27T10:03:00+08:00",
        },
    )
    assert forgotten.status_code == 200
    persisted = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (tmp_path / ".rebuild-data").rglob("*.json")
    )
    assert content not in persisted


def test_team_skill_source_candidate_enters_existing_durable_skill_draft_chain(
    tmp_path,
) -> None:
    store, drafts, staging, staged, _content = _staged_team_source(
        tmp_path,
        asset_type="skill",
    )
    CommitTeamMemoryStagingToSource(
        drafts=drafts,
        staging=staging,
        sources=ObjectStoreTeamSourceAuthority(store),
    ).execute(
        staged.staging_id,
        expected_staging_revision=1,
        confirmed=True,
        completed_at="2026-07-27T10:00:00+08:00",
    )
    client, _secrets = _client(tmp_path)
    preview = client.get(
        f"/api/rebuild/team-memory/source-staging/{staged.staging_id}/candidate-preview"
    ).json()
    assert preview["target_layer"] == "project_skill"
    created = client.post(
        f"/api/rebuild/team-memory/source-staging/{staged.staging_id}/candidate",
        json={
            "preview_id": preview["preview_id"],
            "expected_staging_revision": preview["staging_revision"],
            "expected_source_revision": preview["source_revision"],
            "confirmed": True,
            "created_at": "2026-07-27T10:01:00+08:00",
        },
    )
    assert created.status_code == 200
    candidate_id = created.json()["candidate_id"]

    reviewed = client.post(
        f"/api/rebuild/memory-candidates/{candidate_id}/review",
        json={
            "action": "promote_to_project_skill",
            "reason": "用户确认进入 Project Skill 待发布区。",
        },
    )
    assert reviewed.status_code == 200
    body = reviewed.json()
    assert body["status"] == "promoted"
    assert body["promoted_layer"] == "project_skill"
    assert body["memory_publication_state"] == "staging_project_skill_created_not_published"
    assert store.list("project_skills") == ()
    assert store.list("memory_publications") == ()


def test_team_sources_publish_restart_recall_and_rollback_without_old_current(
    tmp_path,
) -> None:
    series_canary = "TEAM_SERIES_CURRENT_CANARY：发布检查先核对 Source revision。"
    skill_canary = "TEAM_SKILL_CURRENT_CANARY：回答必须先列已确认来源，再给结论。"
    bootstrap_secrets = InMemorySecretStore()
    with TestClient(
        create_app(
            SimpleNamespace(
                root_dir=tmp_path,
                secret_store=bootstrap_secrets,
            )
        )
    ) as bootstrap_client:
        assert bootstrap_client.get("/api/rebuild/team-memory/profile").status_code == 200

    def publish_team_asset(
        *,
        asset_type: str,
        asset_id: str,
        content: str,
        review_action: str,
        publication_prefix: str,
    ):
        store, drafts, staging, staged, _ = _staged_team_source(
            tmp_path,
            asset_type=asset_type,
            asset_id=asset_id,
            content=content,
        )
        completed = CommitTeamMemoryStagingToSource(
            drafts=drafts,
            staging=staging,
            sources=ObjectStoreTeamSourceAuthority(store),
        ).execute(
            staged.staging_id,
            expected_staging_revision=1,
            confirmed=True,
            completed_at="2026-07-27T11:00:00+08:00",
        )
        client, _ = _client(tmp_path)
        preview = client.get(
            f"/api/rebuild/team-memory/source-staging/{staged.staging_id}/candidate-preview"
        ).json()
        created = client.post(
            f"/api/rebuild/team-memory/source-staging/{staged.staging_id}/candidate",
            json={
                "preview_id": preview["preview_id"],
                "expected_staging_revision": preview["staging_revision"],
                "expected_source_revision": preview["source_revision"],
                "confirmed": True,
                "created_at": "2026-07-27T11:01:00+08:00",
            },
        )
        candidate_id = created.json()["candidate_id"]
        reviewed = client.post(
            f"/api/rebuild/memory-candidates/{candidate_id}/review",
            json={
                "action": review_action,
                "reason": "用户确认进入待发布区。",
            },
        )
        assert reviewed.status_code == 200, reviewed.text
        draft_id = reviewed.json()["promoted_object_id"]
        records = SQLiteStructuredRecordStore(
            tmp_path / ".rebuild-data" / "structured-records.sqlite3"
        )
        current_collection = (
            "project_skills"
            if asset_type == "skill"
            else "memory_series_memory"
        )
        assert records.list(current_collection) == ()
        published = client.post(
            f"/api/rebuild/{publication_prefix}/{draft_id}/publication",
            json={
                "confirm": True,
                "reason": "用户二次确认发布 Team 资产。",
            },
        )
        assert published.status_code == 200, published.text
        publication_replay = client.post(
            f"/api/rebuild/{publication_prefix}/{draft_id}/publication",
            json={
                "confirm": True,
                "reason": "用户二次确认发布 Team 资产。",
            },
        )
        if asset_type == "skill":
            assert publication_replay.status_code == 409
        else:
            assert publication_replay.status_code == 200
            assert (
                publication_replay.json()["publication_id"]
                == published.json()["publication_id"]
            )
        return {
            "store": store,
            "completed": completed,
            "candidate_id": candidate_id,
            "draft_id": draft_id,
            "published": published.json(),
            "publication_replay": publication_replay,
        }

    series = publish_team_asset(
        asset_type="chat_memory",
        asset_id="asset-series",
        content=series_canary,
        review_action="promote_to_series_memory",
        publication_prefix="staging-series-memory",
    )
    skill = publish_team_asset(
        asset_type="skill",
        asset_id="asset-skill",
        content=skill_canary,
        review_action="promote_to_project_skill",
        publication_prefix="staging-project-skills",
    )
    records = SQLiteStructuredRecordStore(
        tmp_path / ".rebuild-data" / "structured-records.sqlite3"
    )
    series_current = records.read("memory_series_memory", series["draft_id"])
    skill_current = records.read("project_skills", "skill-project-api")
    assert series_current is not None
    assert series_canary in str(series_current.payload)
    assert skill_current is not None
    assert skill_canary in str(skill_current.payload)
    assert series["completed"].source_id in str(series_current.payload)
    assert skill["completed"].source_id in str(skill_current.payload)
    series_candidate = series["store"].read(
        "memory_candidates",
        series["candidate_id"],
    )
    skill_candidate = skill["store"].read(
        "memory_candidates",
        skill["candidate_id"],
    )
    for candidate, completed, original_content in (
        (series_candidate, series["completed"], series_canary),
        (skill_candidate, skill["completed"], skill_canary),
    ):
        assert candidate["provenance"]["source_id"] == completed.source_id
        assert candidate["provenance"]["source_revision"] == completed.source_revision
        assert candidate["provenance"]["source_content_sha256"] == hashlib.sha256(
            original_content.encode("utf-8")
        ).hexdigest()
    assert series["candidate_id"] in str(
        records.read(
            "memory_publications",
            series["published"]["publication_id"],
        ).payload
    )
    assert skill["candidate_id"] in str(
        records.read(
            "memory_publications",
            skill["published"]["publication_id"],
        ).payload
    )

    restarted, _secrets = _client(tmp_path)
    first_answer = restarted.post(
        "/api/rebuild/workbench/direct-question",
        json={
            "project_id": "project-api",
            "question": "发布检查和回答规则是什么？",
        },
    )
    second_answer = restarted.post(
        "/api/rebuild/workbench/direct-question",
        json={
            "project_id": "project-api",
            "question": "发布检查和回答规则是什么？",
        },
    )
    assert first_answer.status_code == second_answer.status_code == 200
    recalled = second_answer.json()
    assert recalled["recall_status"] == "recalled"
    recalled_text = str(recalled)
    assert series["completed"].source_id in recalled_text
    assert skill["completed"].source_id in recalled_text

    series_rollback = restarted.post(
        f"/api/rebuild/memory-publications/{series['published']['publication_id']}/rollback",
        json={
            "confirm": True,
            "reason": "用户确认撤回 Team Series。",
        },
    )
    skill_rollback = restarted.post(
        f"/api/rebuild/memory-publications/{skill['published']['publication_id']}/rollback",
        json={
            "confirm": True,
            "reason": "用户确认撤回 Team Skill。",
            "expected_publication_revision": 1,
            "expected_project_skill_revision": 1,
        },
    )
    assert series_rollback.status_code == skill_rollback.status_code == 200
    assert series_rollback.json()["status"] == "rolled_back"
    assert skill_rollback.json()["status"] == "rolled_back"
    assert records.read("memory_series_memory", series["draft_id"]) is None
    rolled_back_skill = records.read("project_skills", "skill-project-api")
    assert rolled_back_skill is not None
    assert rolled_back_skill.payload["status"] == "rolled_back"

    after_restart, _ = _client(tmp_path)
    after = after_restart.post(
        "/api/rebuild/workbench/direct-question",
        json={
            "project_id": "project-api",
            "question": "撤回后还能使用旧的 Team 规则吗？",
        },
    )
    assert after.status_code == 200
    assert series_canary not in after.text
    assert skill_canary not in after.text
    all_http = "\n".join(
        (
            first_answer.text,
            second_answer.text,
            series_rollback.text,
            skill_rollback.text,
            after.text,
        )
    )
    assert str(tmp_path) not in all_http
    assert "service-canary" not in all_http
    assert "user-canary" not in all_http


def test_team_l1_l2_extractions_publish_recall_and_rollback_with_exact_source(
    tmp_path,
) -> None:
    atom_canary = "TEAM_L1_ATOM_CANARY：启动窗口目标为三秒。"
    unselected_canary = "TEAM_UNSELECTED_PARAGRAPH_CANARY：这段不得进入已选 Atom。"
    scenario_canary = "TEAM_L2_SCENARIO_CANARY：故障时保留来源并显示恢复入口。"
    bootstrap_secrets = InMemorySecretStore()
    with TestClient(
        create_app(
            SimpleNamespace(
                root_dir=tmp_path,
                secret_store=bootstrap_secrets,
            )
        )
    ) as bootstrap_client:
        assert bootstrap_client.get("/api/rebuild/team-memory/profile").status_code == 200

    def publish_extraction(
        *,
        asset_id: str,
        content: str,
        target_layer: str,
        series_id: str | None = None,
        atom_ids: list[str] | None = None,
        project_id: str = "project-api",
        rejected_atom_id: str = "atom-missing-from-current-project",
    ) -> dict[str, object]:
        store, drafts, staging, staged, _ = _staged_team_source(
            tmp_path,
            asset_id=asset_id,
            content=content,
            project_id=project_id,
        )
        completed = CommitTeamMemoryStagingToSource(
            drafts=drafts,
            staging=staging,
            sources=ObjectStoreTeamSourceAuthority(store),
        ).execute(
            staged.staging_id,
            expected_staging_revision=1,
            confirmed=True,
            completed_at="2026-07-27T12:00:00+08:00",
        )
        client, _ = _client(tmp_path)
        endpoint = f"/api/rebuild/team-memory/source-staging/{staged.staging_id}"
        candidates_before_preview = store.list("memory_candidates")
        preview_response = client.get(f"{endpoint}/extraction-preview")
        assert preview_response.status_code == 200
        preview = preview_response.json()
        selected = next(
            item
            for item in preview["items"]
            if item["target_layer"] == target_layer
            and (
                atom_canary in item["proposed_content"]
                or scenario_canary in item["proposed_content"]
            )
        )
        assert store.list("memory_candidates") == candidates_before_preview
        created = client.post(
            f"{endpoint}/extraction-candidate",
            json={
                "preview_id": preview["preview_id"],
                "item_id": selected["item_id"],
                "target_layer": target_layer,
                "expected_staging_revision": preview["staging_revision"],
                "expected_source_revision": preview["source_revision"],
                "confirmed": True,
                "created_at": "2026-07-27T12:01:00+08:00",
            },
        )
        assert created.status_code == 200, created.text
        candidate_id = created.json()["candidate_id"]
        candidate = store.read("memory_candidates", candidate_id)
        assert candidate is not None
        assert candidate["status"] == "pending_review"
        assert candidate["provenance"]["source_id"] == completed.source_id
        assert candidate["provenance"]["source_revision"] == completed.source_revision
        assert candidate["provenance"]["source_content_sha256"] == hashlib.sha256(
            content.encode("utf-8")
        ).hexdigest()
        assert candidate["source_refs"][0]["locator"] == selected["source_locator"]
        if target_layer == "atom":
            assert unselected_canary not in candidate["proposed_content"]
            assert unselected_canary not in candidate["source_refs"][0]["quote"]

        review_payload: dict[str, object] = {
            "action": (
                "promote_to_atom"
                if target_layer == "atom"
                else "promote_to_scenario"
            ),
            "reason": f"用户确认 {target_layer} 进入待发布区。",
        }
        if series_id is not None:
            review_payload["series_id"] = series_id
        if atom_ids is not None:
            review_payload["atom_ids"] = atom_ids
        if target_layer == "scenario":
            invalid_review = client.post(
                f"/api/rebuild/memory-candidates/{candidate_id}/review",
                json={
                    **review_payload,
                    "atom_ids": [rejected_atom_id],
                },
            )
            assert invalid_review.status_code == 400, invalid_review.text
            assert store.read("memory_candidates", candidate_id)["status"] == "pending_review"
        reviewed = client.post(
            f"/api/rebuild/memory-candidates/{candidate_id}/review",
            json=review_payload,
        )
        assert reviewed.status_code == 200, reviewed.text
        staged_id = reviewed.json()["promoted_object_id"]
        records = SQLiteStructuredRecordStore(
            tmp_path / ".rebuild-data" / "structured-records.sqlite3"
        )
        current_collection = (
            "memory_atoms" if target_layer == "atom" else "memory_scenarios"
        )
        assert records.read(current_collection, staged_id) is None
        publication = client.post(
            f"/api/rebuild/staging-{target_layer}s/{staged_id}/publication",
            json={
                "confirm": True,
                "reason": f"用户二次确认发布 Team {target_layer}。",
            },
        )
        assert publication.status_code == 200, publication.text
        current = records.read(current_collection, staged_id)
        assert current is not None
        assert completed.source_id in str(current.payload)
        assert candidate_id in str(
            records.read(
                "memory_publications",
                publication.json()["publication_id"],
            ).payload
        )
        return {
            "candidate": candidate,
            "completed": completed,
            "staged_id": staged_id,
            "publication": publication.json(),
        }

    atom = publish_extraction(
        asset_id="asset-l1",
        content=f"{atom_canary}\n\n{unselected_canary}",
        target_layer="atom",
    )
    foreign_atom = publish_extraction(
        asset_id="asset-l1-foreign-project",
        content=atom_canary,
        target_layer="atom",
        project_id="project-other",
    )
    hierarchy_client, _ = _client(tmp_path)
    atom_options = hierarchy_client.get(
        "/api/rebuild/projects/project-api/memory-hierarchy-options"
    )
    assert atom_options.status_code == 200, atom_options.text
    assert atom_options.json()["content_included"] is False
    assert atom_options.json()["network_called"] is False
    assert [item["id"] for item in atom_options.json()["atoms"]] == [atom["staged_id"]]
    assert foreign_atom["staged_id"] not in atom_options.text
    assert atom_canary in atom_options.json()["atoms"][0]["preview"]
    assert str(tmp_path) not in atom_options.text
    assert unselected_canary not in atom_options.text
    scenario = publish_extraction(
        asset_id="asset-l2",
        content=(
            "# 故障恢复\n\n"
            f"{scenario_canary}\n\n"
            "- 步骤一：检查本地状态\n"
            "- 步骤二：按原 Source revision 恢复"
        ),
        target_layer="scenario",
        series_id="project-api",
        atom_ids=[atom["staged_id"]],
        rejected_atom_id=foreign_atom["staged_id"],
    )

    scenario_store = JsonObjectStore(
        tmp_path / ".rebuild-data",
        legacy_root=tmp_path / "library",
    )
    scenario_staging = TeamMemorySourceStagingRepository(scenario_store)
    scenario_staging_id = next(
        str(value["id"])
        for value in scenario_store.list(scenario_staging.collection)
        if value.get("proposed_source", {}).get("id") == scenario["completed"].source_id
    )
    series_preview = hierarchy_client.get(
        f"/api/rebuild/team-memory/source-staging/{scenario_staging_id}/candidate-preview"
    ).json()
    series_created = hierarchy_client.post(
        f"/api/rebuild/team-memory/source-staging/{scenario_staging_id}/candidate",
        json={
            "preview_id": series_preview["preview_id"],
            "expected_staging_revision": series_preview["staging_revision"],
            "expected_source_revision": series_preview["source_revision"],
            "confirmed": True,
            "created_at": "2026-07-27T12:02:00+08:00",
        },
    )
    assert series_created.status_code == 200, series_created.text
    scenario_options = hierarchy_client.get(
        "/api/rebuild/projects/project-api/memory-hierarchy-options"
    )
    assert scenario_options.status_code == 200, scenario_options.text
    assert [item["id"] for item in scenario_options.json()["scenarios"]] == [
        scenario["staged_id"]
    ]
    invalid_series_review = hierarchy_client.post(
        f"/api/rebuild/memory-candidates/{series_created.json()['candidate_id']}/review",
        json={
            "action": "promote_to_series_memory",
            "reason": "不得引用其他项目的 Scenario。",
            "series_id": "project-api",
            "scenario_ids": ["scenario-from-another-project"],
        },
    )
    assert invalid_series_review.status_code == 400, invalid_series_review.text
    series_reviewed = hierarchy_client.post(
        f"/api/rebuild/memory-candidates/{series_created.json()['candidate_id']}/review",
        json={
            "action": "promote_to_series_memory",
            "reason": "用户确认建立 Team L1/L2 的系列入口。",
            "series_id": "project-api",
            "scenario_ids": [scenario["staged_id"]],
        },
    )
    assert series_reviewed.status_code == 200, series_reviewed.text
    series_staged_id = series_reviewed.json()["promoted_object_id"]
    series_published = hierarchy_client.post(
        f"/api/rebuild/staging-series-memory/{series_staged_id}/publication",
        json={
            "confirm": True,
            "reason": "用户二次确认发布 Team 系列入口。",
        },
    )
    assert series_published.status_code == 200, series_published.text

    skill_store, skill_drafts, skill_staging, skill_staged, _ = _staged_team_source(
        tmp_path,
        asset_type="skill",
        asset_id="asset-l1-l2-router-skill",
        content="TEAM_LAYER_ROUTER_SKILL：先读系列，再按需下钻 Scenario 与 Atom。",
    )
    skill_completed = CommitTeamMemoryStagingToSource(
        drafts=skill_drafts,
        staging=skill_staging,
        sources=ObjectStoreTeamSourceAuthority(skill_store),
    ).execute(
        skill_staged.staging_id,
        expected_staging_revision=1,
        confirmed=True,
        completed_at="2026-07-27T12:03:00+08:00",
    )
    skill_endpoint = (
        f"/api/rebuild/team-memory/source-staging/{skill_staged.staging_id}"
    )
    skill_preview = hierarchy_client.get(
        f"{skill_endpoint}/candidate-preview"
    ).json()
    skill_created = hierarchy_client.post(
        f"{skill_endpoint}/candidate",
        json={
            "preview_id": skill_preview["preview_id"],
            "expected_staging_revision": skill_preview["staging_revision"],
            "expected_source_revision": skill_preview["source_revision"],
            "confirmed": True,
            "created_at": "2026-07-27T12:04:00+08:00",
        },
    )
    assert skill_created.status_code == 200, skill_created.text
    skill_reviewed = hierarchy_client.post(
        f"/api/rebuild/memory-candidates/{skill_created.json()['candidate_id']}/review",
        json={
            "action": "promote_to_project_skill",
            "reason": "用户确认 Team 分层读取 Skill 进入待发布区。",
        },
    )
    assert skill_reviewed.status_code == 200, skill_reviewed.text
    skill_published = hierarchy_client.post(
        f"/api/rebuild/staging-project-skills/{skill_reviewed.json()['promoted_object_id']}/publication",
        json={
            "confirm": True,
            "reason": "用户二次确认启用 Team 分层读取 Skill。",
        },
    )
    assert skill_published.status_code == 200, skill_published.text
    assert skill_completed.source_id in str(
        SQLiteStructuredRecordStore(
            tmp_path / ".rebuild-data" / "structured-records.sqlite3"
        ).read("project_skills", "skill-project-api").payload
    )

    restarted, _ = _client(tmp_path)
    recalled = restarted.post(
        "/api/rebuild/workbench/direct-question",
        json={
            "project_id": "project-api",
            "question": "启动窗口目标和故障恢复规则分别是什么？",
        },
    )
    assert recalled.status_code == 200, recalled.text
    assert recalled.json()["recall_status"] == "recalled", recalled.text
    assert atom_canary in recalled.text
    assert scenario_canary in recalled.text
    assert atom["completed"].source_id in recalled.text
    assert scenario["completed"].source_id in recalled.text
    recalled_layers = [
        value["layer"]
        for value in recalled.json()["evidence_items"]
        if value.get("layer") in {"l3_series_memory", "l2_scenario", "l1_atom"}
    ]
    assert recalled_layers == ["l3_series_memory", "l2_scenario", "l1_atom"]

    for value in (
        atom,
        scenario,
        {"publication": series_published.json()},
    ):
        rolled_back = restarted.post(
            f"/api/rebuild/memory-publications/{value['publication']['publication_id']}/rollback",
            json={
                "confirm": True,
                "reason": "用户确认撤回 Team L1/L2 publication。",
            },
        )
        assert rolled_back.status_code == 200, rolled_back.text
        assert rolled_back.json()["status"] == "rolled_back"

    after_restart, _ = _client(tmp_path)
    after = after_restart.post(
        "/api/rebuild/workbench/direct-question",
        json={
            "project_id": "project-api",
            "question": "撤回后的启动指标和恢复规则还有效吗？",
        },
    )
    assert after.status_code == 200
    assert atom_canary not in after.text
    assert scenario_canary not in after.text
    all_http = "\n".join((recalled.text, after.text))
    assert str(tmp_path) not in all_http
    assert "service-canary" not in all_http
    assert "user-canary" not in all_http


def test_team_source_list_and_api_resume_interrupted_hard_forget(
    tmp_path,
) -> None:
    store, drafts, staging, staged, _content = _staged_team_source(tmp_path)
    CommitTeamMemoryStagingToSource(
        drafts=drafts,
        staging=staging,
        sources=ObjectStoreTeamSourceAuthority(store),
    ).execute(
        staged.staging_id,
        expected_staging_revision=1,
        confirmed=True,
        completed_at="2026-07-27T08:03:00+08:00",
    )
    with pytest.raises(RuntimeError, match="after claim"):
        ForgetTeamCreatedSource(
            object_store=store,
            staging=staging,
            after_claimed=lambda: (_ for _ in ()).throw(
                RuntimeError("after claim")
            ),
        ).execute(
            staged.staging_id,
            expected_staging_revision=3,
            confirmed=True,
            reason="原始硬遗忘原因。",
            forgotten_at="2026-07-27T08:04:00+08:00",
        )
    client, _secrets = _client(tmp_path)
    listed = client.get("/api/rebuild/team-memory/source-staging")
    assert listed.status_code == 200
    item = listed.json()["items"][0]
    assert item["status"] == "forgetting"
    assert item["staging_revision"] == 4
    assert item["forget_requested_staging_revision"] == 3
    assert "原始硬遗忘原因" not in listed.text

    resumed = client.post(
        f"/api/rebuild/team-memory/source-staging/{staged.staging_id}/hard-forget",
        json={
            "expected_staging_revision": 3,
            "reason": "UI 不持有原始原因。",
            "confirmed": True,
            "forgotten_at": "2026-07-27T08:05:00+08:00",
        },
    )
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "forgotten"
