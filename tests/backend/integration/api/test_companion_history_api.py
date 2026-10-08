from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient

from backend.api.app import create_app
from core.companion_core import CompanionHardForgetError, CompanionHistoryService, CompanionRepository
from core.fresh_vault_shared_trust_audit_bootstrap import (
    bootstrap_fresh_vault_shared_trust_audit,
)
from core.storage_provider import (
    AggregateAuthorityEvidence,
    JsonObjectStore,
    SQLiteAggregateAuthorityStore,
    SQLiteStructuredRecordStore,
)
from core.aggregate_repository_factory import AUTHORITY_DATABASE_NAME, STRUCTURED_DATABASE_NAME, TARGET_IDENTITY


def client(tmp_path) -> TestClient:
    return TestClient(create_app(SimpleNamespace(root_dir=tmp_path)))


def create_chat(api: TestClient, request_id: str = "request:history") -> dict:
    response = api.post("/api/rebuild/companion/chat", json={"request_id": request_id, "text": "历史 CANARY-唯一内容"})
    assert response.status_code == 201
    return response.json()


def activate_one_memory_publication_member(tmp_path) -> None:
    authority = SQLiteAggregateAuthorityStore(tmp_path / ".rebuild-data" / AUTHORITY_DATABASE_NAME)
    evidence = AggregateAuthorityEvidence(
        migration_id="companion-partial-v1",
        source_fingerprint="a" * 64,
        target_fingerprint="b" * 64,
        target_identity=TARGET_IDENTITY,
    )
    initial = authority.create_json_active(namespace_id="default", aggregate="memory_atoms", reason="fixture")
    staged = authority.transition(
        namespace_id="default", aggregate="memory_atoms", expected_revision=initial.revision,
        to_state="sqlite_staged", reason="fixture", evidence=evidence,
    )
    authority.transition(
        namespace_id="default", aggregate="memory_atoms", expected_revision=staged.revision,
        to_state="sqlite_active", reason="fixture", evidence=evidence,
    )


def test_global_history_has_bounded_projection_and_stable_cursor(tmp_path) -> None:
    api = client(tmp_path)
    created = create_chat(api)

    first = api.get("/api/rebuild/companion/history", params={"limit": 1})
    second = api.get("/api/rebuild/companion/history", params={"limit": 1, "cursor": first.json()["next_cursor"]})

    assert first.status_code == 200 and second.status_code == 200
    assert first.headers["cache-control"] == "no-store"
    combined = first.json()["items"] + second.json()["items"]
    assert {item["message_id"] for item in combined} == {
        created["user_message"]["message_id"], created["assistant_message"]["message_id"],
    }
    assert all(set(item) == {
        "message_id", "session_id", "role", "status", "preview", "created_at", "memory_linked", "dependency_count", "memory_candidate",
    } for item in combined)
    assert first.json()["scope"] == "local_companion_data_only"


def test_history_filters_interleaved_projects_before_pagination(tmp_path) -> None:
    api = client(tmp_path)
    alpha = api.post("/api/rebuild/companion/chat", json={
        "request_id": "request:history-alpha", "text": "Alpha 历史", "project_id": "project-alpha",
    })
    beta = api.post("/api/rebuild/companion/chat", json={
        "request_id": "request:history-beta", "text": "Beta 历史", "project_id": "project-beta",
    })
    assert alpha.status_code == 201 and beta.status_code == 201

    first = api.get("/api/rebuild/companion/history", params={"limit": 1, "project_id": "project-alpha"})
    second = api.get("/api/rebuild/companion/history", params={
        "limit": 1, "project_id": "project-alpha", "cursor": first.json()["next_cursor"],
    })

    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["project_id"] == second.json()["project_id"] == "project-alpha"
    combined = first.json()["items"] + second.json()["items"]
    assert {item["message_id"] for item in combined} == {
        alpha.json()["user_message"]["message_id"], alpha.json()["assistant_message"]["message_id"],
    }
    assert first.json()["next_cursor"] is not None
    assert second.json()["next_cursor"] is None


def test_partial_memory_publication_authority_keeps_history_and_candidates_available(tmp_path) -> None:
    activate_one_memory_publication_member(tmp_path)
    api = client(tmp_path)

    empty = api.get("/api/rebuild/companion/history")
    created = create_chat(api, "request:partial-authority")
    proposed = api.post(
        f"/api/rebuild/companion/messages/{created['user_message']['message_id']}/memory-candidate",
        json={},
    )
    history = api.get("/api/rebuild/companion/history")

    assert empty.status_code == 200 and empty.json()["items"] == []
    assert proposed.status_code == 201
    assert history.status_code == 200
    row = next(item for item in history.json()["items"] if item["message_id"] == created["user_message"]["message_id"])
    assert row["memory_candidate"]["status"] == "pending_review"


def test_partial_memory_publication_authority_fails_closed_for_published_dependency_delete(tmp_path) -> None:
    api = client(tmp_path)
    created = create_chat(api, "request:partial-authority-delete")
    message_id = created["user_message"]["message_id"]
    CompanionRepository.at_data_root(tmp_path).register_message_dependency(
        message_id=message_id,
        dependent_kind="published_memory",
        dependent_id="publication:ambiguous",
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    activate_one_memory_publication_member(tmp_path)

    deleted = api.delete(f"/api/rebuild/companion/messages/{message_id}")
    history = api.get("/api/rebuild/companion/history")

    assert deleted.status_code == 503
    assert deleted.json()["error"]["code"] == "forget_incomplete"
    assert deleted.json()["receipt"]["failed_step"] == "erase:published_memory"
    assert history.status_code == 200
    assert any(item["message_id"] == message_id for item in history.json()["items"])


def test_history_message_can_be_proposed_once_for_manual_memory_review(tmp_path) -> None:
    api = client(tmp_path)
    created = create_chat(api, "request:memory-candidate")
    message_id = created["user_message"]["message_id"]

    first = api.post(f"/api/rebuild/companion/messages/{message_id}/memory-candidate", json={})
    replay = api.post(f"/api/rebuild/companion/messages/{message_id}/memory-candidate", json={})
    history = api.get("/api/rebuild/companion/history").json()["items"]

    assert first.status_code == 201 and replay.status_code == 200
    assert first.json()["candidate"]["status"] == "pending_review"
    assert first.json()["memory_publication_state"] == "candidate_pending_review"
    assert replay.json()["candidate"]["candidate_id"] == first.json()["candidate"]["candidate_id"]
    assert replay.json()["candidate"]["replayed"] is True
    row = next(item for item in history if item["message_id"] == message_id)
    assert row["memory_candidate"]["status"] == "pending_review"
    assert row["dependency_count"] == 1


def test_project_scoped_chat_proposes_candidate_in_same_project_after_restart(tmp_path) -> None:
    created = client(tmp_path).post(
        "/api/rebuild/companion/chat",
        json={
            "request_id": "request:project-candidate",
            "text": "项目 Alpha 已确定下一步。",
            "project_id": "project-alpha",
        },
    )
    assert created.status_code == 201
    message = created.json()["user_message"]
    assert message["project_id"] == "project-alpha"

    proposed = client(tmp_path).post(
        f"/api/rebuild/companion/messages/{message['message_id']}/memory-candidate",
        json={},
    )
    assert proposed.status_code == 201
    candidate_id = proposed.json()["candidate"]["candidate_id"]
    store = JsonObjectStore(
        tmp_path / ".rebuild-data", legacy_root=tmp_path / "library", namespace_id="default",
    )
    candidate = store.read("memory_candidates", candidate_id)
    assert candidate is not None and candidate["project_id"] == "project-alpha"
    history = client(tmp_path).get("/api/rebuild/companion/history")
    row = next(item for item in history.json()["items"] if item["message_id"] == message["message_id"])
    assert row["memory_candidate"]["candidate_id"] == candidate_id
    assert row["memory_candidate"]["status"] == "pending_review"


def test_deleting_message_removes_its_unpublished_memory_candidate(tmp_path) -> None:
    api = client(tmp_path)
    created = create_chat(api, "request:memory-candidate-delete")
    message_id = created["user_message"]["message_id"]
    proposed = api.post(f"/api/rebuild/companion/messages/{message_id}/memory-candidate", json={})
    candidate_id = proposed.json()["candidate"]["candidate_id"]

    deleted = api.delete(f"/api/rebuild/companion/messages/{message_id}")

    assert deleted.status_code == 200
    assert deleted.json()["receipt"]["affected"]["candidate"] == 1
    restarted = client(tmp_path)
    assert restarted.get("/api/rebuild/companion/history").status_code == 200
    store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library", namespace_id="default")
    assert store.read("memory_candidates", candidate_id) is None


def test_deleting_source_message_rolls_back_published_memory_before_forgetting(tmp_path) -> None:
    bootstrap_fresh_vault_shared_trust_audit(tmp_path)
    api = client(tmp_path)
    created = create_chat(api, "request:published-memory-delete")
    message_id = created["user_message"]["message_id"]
    candidate_id = api.post(
        f"/api/rebuild/companion/messages/{message_id}/memory-candidate", json={},
    ).json()["candidate"]["candidate_id"]
    reviewed = api.post(
        f"/api/rebuild/memory-candidates/{candidate_id}/review",
        json={"action": "promote_to_atom", "reason": "用户确认这条陪伴事实值得长期记忆。"},
    )
    assert reviewed.status_code == 200
    atom_id = reviewed.json()["promoted_object_id"]
    published = api.post(
        f"/api/rebuild/staging-atoms/{atom_id}/publication",
        json={"confirm": True, "reason": "用户确认发布陪伴长期记忆。"},
    )
    assert published.status_code == 200
    publication_id = published.json()["publication_id"]

    deleted = api.delete(f"/api/rebuild/companion/messages/{message_id}")

    assert deleted.status_code == 200
    assert deleted.json()["receipt"]["affected"] == {
        "candidate": 1, "message": 1, "published_memory": 1,
    }
    records = SQLiteStructuredRecordStore(tmp_path / ".rebuild-data" / STRUCTURED_DATABASE_NAME)
    publication = records.read("memory_publications", publication_id)
    assert publication is not None
    assert publication.payload["status"] == "rolled_back"
    assert records.read("memory_atoms", atom_id) is None


def test_json_published_dependency_forget_fails_closed_without_sqlite_authority(
    tmp_path,
) -> None:
    api = client(tmp_path)
    created = create_chat(api, "request:json-publication-delete")
    message_id = created["user_message"]["message_id"]
    publication_id = "memory-publication-json-legacy"
    repository = CompanionRepository.at_data_root(tmp_path)
    repository.register_message_dependency(
        message_id=message_id,
        dependent_kind="published_memory",
        dependent_id=publication_id,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    store = JsonObjectStore(
        tmp_path / ".rebuild-data",
        legacy_root=tmp_path / "library",
        namespace_id="default",
    )
    store.write(
        "memory_atoms",
        "atom-json-legacy",
        {"id": "atom-json-legacy", "content": "legacy published evidence"},
        expected_revision=0,
    )
    store.write(
        "memory_publications",
        publication_id,
        {
            "id": publication_id,
            "publication_id": publication_id,
            "layer": "atom",
            "published_object_id": "atom-json-legacy",
            "status": "published",
        },
        expected_revision=0,
    )

    deleted = api.delete(f"/api/rebuild/companion/messages/{message_id}")

    assert deleted.status_code == 503
    assert deleted.json()["receipt"]["failed_step"] == "erase:published_memory"
    assert store.read("memory_atoms", "atom-json-legacy") is not None
    assert store.read("memory_publications", publication_id)["status"] == "published"
    assert any(
        item["message_id"] == message_id
        for item in api.get("/api/rebuild/companion/history").json()["items"]
    )


def test_delete_physically_forgets_one_message_and_is_restart_idempotent(tmp_path) -> None:
    api = client(tmp_path)
    created = create_chat(api, "request:delete")
    message_id = created["user_message"]["message_id"]

    deleted = api.delete(f"/api/rebuild/companion/messages/{message_id}")
    restarted = client(tmp_path)
    replay = restarted.delete(f"/api/rebuild/companion/messages/{message_id}")
    history = restarted.get("/api/rebuild/companion/history")
    session = restarted.get(
        "/api/rebuild/companion/chat/messages", params={"session_id": created["session"]["session_id"]},
    )
    forgotten_replay = restarted.post("/api/rebuild/companion/chat", json={
        "request_id": "request:delete",
        "session_id": created["session"]["session_id"],
        "text": "历史 CANARY-唯一内容",
    })

    assert deleted.status_code == 200
    assert deleted.json()["receipt"]["status"] == "completed"
    assert replay.status_code == 200 and replay.json()["receipt"]["replayed"] is True
    assert all(item["message_id"] != message_id and "CANARY-唯一内容" not in item["preview"] for item in history.json()["items"])
    assert all(item["message_id"] != message_id for item in session.json()["items"])
    assert session.json()["session"]["context_epoch"] == created["session"]["context_epoch"] + 1
    assert forgotten_replay.status_code == 409


def test_dependency_failure_returns_retryable_receipt_without_deleting_message(tmp_path) -> None:
    api = client(tmp_path)
    created = create_chat(api, "request:dependency")
    message_id = created["user_message"]["message_id"]
    repo = CompanionRepository.at_data_root(tmp_path)
    repo.register_message_dependency(
        message_id=message_id,
        dependent_kind="published_memory",
        dependent_id="memory:must-withdraw",
        created_at=datetime.now(timezone.utc).isoformat(),
    )

    failed = api.delete(f"/api/rebuild/companion/messages/{message_id}")
    history = api.get("/api/rebuild/companion/history")

    assert failed.status_code == 503
    assert failed.json()["error"]["code"] == "forget_incomplete"
    assert failed.json()["receipt"]["failed_step"] == "erase:published_memory"
    assert "CANARY" not in str(failed.json()["receipt"])
    target = next(item for item in history.json()["items"] if item["message_id"] == message_id)
    assert target["memory_linked"] is True and "CANARY" in target["preview"]


def test_runtime_dependency_adapter_withdraws_then_completes_delete(tmp_path) -> None:
    withdrawn: list[str] = []
    api = TestClient(create_app(SimpleNamespace(
        root_dir=tmp_path,
        companion_forget_dependency_erasers={"published_memory": withdrawn.append},
    )))
    created = create_chat(api, "request:withdraw")
    message_id = created["assistant_message"]["message_id"]
    CompanionRepository.at_data_root(tmp_path).register_message_dependency(
        message_id=message_id,
        dependent_kind="published_memory",
        dependent_id="memory:withdraw-me",
        created_at=datetime.now(timezone.utc).isoformat(),
    )

    response = api.delete(f"/api/rebuild/companion/messages/{message_id}")

    assert response.status_code == 200
    assert withdrawn == ["memory:withdraw-me"]
    assert response.json()["receipt"]["affected"]["published_memory"] == 1


def test_history_and_delete_reject_invalid_or_missing_targets(tmp_path) -> None:
    api = client(tmp_path)
    assert api.get("/api/rebuild/companion/history", params={"limit": 101}).status_code in {400, 422}
    assert api.get("/api/rebuild/companion/history", params={"cursor": "not-a-cursor"}).status_code == 400
    assert api.delete("/api/rebuild/companion/messages/not%20safe").status_code == 400
    assert api.delete("/api/rebuild/companion/messages/message:missing").status_code == 404


def test_history_exposes_body_free_recovery_when_secure_purge_needs_retry(tmp_path) -> None:
    api = client(tmp_path)
    created = create_chat(api, "request:purge-api")

    class FailPurge(CompanionRepository):
        def _purge_deleted_pages(self) -> None:
            raise sqlite3.OperationalError("busy")

    repository = FailPurge(CompanionRepository.at_data_root(tmp_path).database_path)
    try:
        CompanionHistoryService(repository).forget(created["user_message"]["message_id"])
    except CompanionHardForgetError:
        pass

    history = api.get("/api/rebuild/companion/history").json()
    recovery = history["recovery_items"][0]
    assert recovery["message_id"] == created["user_message"]["message_id"]
    assert recovery["failed_step"] == "purge:sqlite"
    assert "CANARY" not in str(recovery)
