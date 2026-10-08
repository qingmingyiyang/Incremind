from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from backend.api.app import create_app
from core.aggregate_repository_factory import (
    AUTHORITY_DATABASE_NAME,
    TARGET_IDENTITY,
)
from core.memory_core import (
    ObjectStoreMemoryCandidateRepository,
    shared_trust_audit_activation_id,
    shared_trust_audit_activation_payload,
)
from core.storage_provider import (
    AggregateAuthorityEvidence,
    JsonObjectStore,
    SQLiteAggregateAuthorityStore,
    SQLiteStructuredRecordStore,
)


def _client(tmp_path) -> TestClient:
    return TestClient(create_app(SimpleNamespace(root_dir=tmp_path)))


def _activate_memory_sqlite_authority(tmp_path) -> None:
    records = SQLiteStructuredRecordStore(
        tmp_path / ".rebuild-data" / "structured-records.sqlite3"
    )
    authority = SQLiteAggregateAuthorityStore(
        tmp_path / ".rebuild-data" / AUTHORITY_DATABASE_NAME
    )
    members = (
        "memory_atoms",
        "memory_publications",
        "memory_scenarios",
        "memory_series_memory",
        "memory_transitions",
        "project_skills",
    )
    evidence = AggregateAuthorityEvidence(
        "four-layer-review-v1",
        "a" * 64,
        "b" * 64,
        TARGET_IDENTITY,
    )
    with records.begin() as transaction:
        for member in members:
            transaction.put(
                "aggregate_authority_targets",
                f"default~{member}",
                {
                    "namespace_id": "default",
                    "aggregate": member,
                    "migration_id": evidence.migration_id,
                    "source_fingerprint": evidence.source_fingerprint,
                    "target_fingerprint": evidence.target_fingerprint,
                    "target_identity": evidence.target_identity,
                },
                expected_revision=0,
            )
        transaction.put(
            "aggregate_authority_compound_activations",
            shared_trust_audit_activation_id("default"),
            shared_trust_audit_activation_payload(
                namespace_id="default",
                target_identity=TARGET_IDENTITY,
                activation_id=evidence.migration_id,
                member_migrations={
                    member: evidence.migration_id for member in members
                },
                source_fingerprint=evidence.source_fingerprint,
                target_fingerprint=evidence.target_fingerprint,
                activated_at="2026-07-27T00:00:00+00:00",
            ),
            expected_revision=0,
        )
        transaction.commit()
    for member in members:
        initial = authority.create_json_active(
            namespace_id="default",
            aggregate=member,
            reason="four-layer review test initial",
        )
        staged = authority.transition(
            namespace_id="default",
            aggregate=member,
            expected_revision=initial.revision,
            to_state="sqlite_staged",
            evidence=evidence,
            reason="four-layer review test staged",
        )
        authority.transition(
            namespace_id="default",
            aggregate=member,
            expected_revision=staged.revision,
            to_state="sqlite_active",
            evidence=evidence,
            reason="four-layer review test active",
        )


def _store(tmp_path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def _seed_candidate(store: JsonObjectStore, *, candidate_id: str = "candidate-four-layer-1") -> str:
    candidate = {
        "schema_version": "1.0.0",
        "id": candidate_id,
        "project_id": "default",
        "target_layer": "atom",
        "candidate_type": "answer_summary",
        "status": "pending_review",
        "proposed_content": "个人 AI 记忆工作台的资料库需要长期记忆。",
        "source_refs": [
            {
                "source_id": "source-four-layer-1",
                "locator": "char:0-40",
                "quote": "个人 AI 记忆工作台",
            }
        ],
        "evidence_refs": [
            {
                "locator": "source-content-reads/content-read-source-four-layer-1.json",
                "quote": "资料库需要长期记忆",
            }
        ],
        "provenance": {
            "model_result_id": None,
            "model_request_id": None,
            "recall_result_id": None,
            "document_id": None,
            "document_revision": None,
            "source_content_read_id": "content-read-source-four-layer-1",
            "input_refs": [
                {
                    "kind": "source",
                    "object_id": "source-four-layer-1",
                    "uri": "crp://default/sources/source-four-layer-1.json",
                },
                {
                    "kind": "source_content_read",
                    "object_id": "content-read-source-four-layer-1",
                    "uri": "crp://default/source-content-reads/content-read-source-four-layer-1.json",
                },
            ],
        },
        "review": {
            "requires_user_confirmation": True,
            "auto_promote_allowed": False,
            "reviewed_by": None,
            "reviewed_at": None,
        },
        "review_prompt": "确认是否提升为 staging atom",
        "created_at": "2026-07-04T00:00:00+08:00",
        "updated_at": "2026-07-04T00:00:00+08:00",
    }
    ObjectStoreMemoryCandidateRepository(store).save(candidate)
    return candidate_id


def test_auto_memory_publication_settings_get_returns_default(tmp_path) -> None:
    client = _client(tmp_path)

    response = client.get("/api/rebuild/settings/auto-memory-publication")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] in {"ready", "disabled"}
    assert payload["enabled"] is False
    assert payload["allowed_layers"] == ["atom", "scenario", "series_memory", "project_skill"]
    assert payload["explicit_enable_required"] is True
    assert payload["rollback_required"] is True


def test_auto_memory_publication_settings_put_quarantines_atom_only_configuration(tmp_path) -> None:
    client = _client(tmp_path)

    response = client.put(
        "/api/rebuild/settings/auto-memory-publication",
        json={
            "enabled": True,
            "confirm_enable": True,
            "allowed_layers": ["atom"],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["enabled"] is True
    assert payload["allowed_layers"] == ["atom"]
    assert payload["status"] == "quarantined"
    assert payload["quarantined"] is True


def test_auto_memory_publication_settings_rejects_invalid_layer(tmp_path) -> None:
    client = _client(tmp_path)

    response = client.put(
        "/api/rebuild/settings/auto-memory-publication",
        json={
            "enabled": True,
            "confirm_enable": True,
            "allowed_layers": ["atom", "invalid_layer"],
        },
    )

    assert response.status_code == 400
    payload = response.json()
    assert "allowed_layers" in payload["reason"].lower() or "layer" in payload["reason"].lower()


def test_memory_candidate_review_get_returns_pending_review(tmp_path) -> None:
    client = _client(tmp_path)
    store = _store(tmp_path)
    candidate_id = _seed_candidate(store)

    response = client.get(f"/api/rebuild/memory-candidates/{candidate_id}/review")

    assert response.status_code == 200
    payload = response.json()
    assert payload["candidate_status"] in {"pending_review", "ready", "promoted"}
    assert payload["memory_publication_state"] == "not_published"
    assert payload["target_layer"] == "atom"


def test_memory_candidate_review_post_promotes_to_atom_staging(tmp_path) -> None:
    _activate_memory_sqlite_authority(tmp_path)
    client = _client(tmp_path)
    store = _store(tmp_path)
    candidate_id = _seed_candidate(store)

    response = client.post(
        f"/api/rebuild/memory-candidates/{candidate_id}/review",
        json={
            "action": "promote_to_atom",
            "reason": "四层记忆测试提升到 staging atom",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] in {"promoted", "ready"}
    assert payload["memory_publication_state"] in {"not_published", "staging_atom_created_not_published"}


def test_memory_candidate_auto_publication_respects_disabled_settings(tmp_path) -> None:
    client = _client(tmp_path)
    store = _store(tmp_path)
    candidate_id = _seed_candidate(store)

    response = client.post(
        f"/api/rebuild/memory-candidates/{candidate_id}/auto-publication",
        json={"reason": "尝试在未开启自动发布时触发"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "skipped"
    assert payload["memory_publication_state"] == "not_published"
    assert "disabled" in payload["skipped_reason"]


def test_memory_candidate_auto_publication_rejects_provider_command(tmp_path) -> None:
    client = _client(tmp_path)
    store = _store(tmp_path)
    candidate_id = _seed_candidate(store)

    response = client.post(
        f"/api/rebuild/memory-candidates/{candidate_id}/auto-publication",
        json={"command": "provider_execute"},
    )

    assert response.status_code == 400
    payload = response.json()
    assert "does not accept provider command" in payload["reason"]


def test_staging_atom_publication_writes_long_term_memory(tmp_path) -> None:
    _activate_memory_sqlite_authority(tmp_path)
    client = _client(tmp_path)
    store = _store(tmp_path)
    candidate_id = _seed_candidate(store)

    promote_response = client.post(
        f"/api/rebuild/memory-candidates/{candidate_id}/review",
        json={
            "action": "promote_to_atom",
            "reason": "提升到 staging 以便发布",
        },
    )
    staging_object_id = promote_response.json().get("promoted_object_id")

    response = client.post(
        f"/api/rebuild/staging-atoms/{staging_object_id}/publication",
        json={"confirm": True, "reason": "四层记忆测试二次确认发布 atom"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["memory_publication_state"] in {"published", "published_with_rollback_ref"}
    assert payload["publication_id"]
    assert payload["rollback_ref"]
    encoded = str(payload).lower()
    assert "sk-" not in encoded


def test_staging_scenario_publication_rejects_raw_staging_without_manual_context(tmp_path) -> None:
    _activate_memory_sqlite_authority(tmp_path)
    client = _client(tmp_path)
    records = SQLiteStructuredRecordStore(
        tmp_path / ".rebuild-data" / "structured-records.sqlite3"
    )

    with records.begin() as transaction:
        transaction.put(
            "staging_scenarios",
            "staging-scenario-four-layer-1",
            {
                "schema_version": "1.0.0",
                "id": "staging-scenario-four-layer-1",
                "layer": "scenario",
                "title": "四层记忆测试场景",
                "summary": "验证 scenario 层 publication。",
                "scenario_id": "scenario-four-layer-1",
                "source_refs": [
                    {
                        "source_id": "source-four-layer-1",
                        "locator": "char:0-40",
                        "quote": "个人 AI 记忆工作台",
                    }
                ],
                "trust_status": "system_generated",
                "revision": 1,
                "status": "staged",
                "created_at": "2026-07-04T00:00:00+08:00",
            },
            expected_revision=0,
        )
        transaction.commit()

    response = client.post(
        "/api/rebuild/staging-scenarios/staging-scenario-four-layer-1/publication",
        json={"confirm": True, "reason": "四层记忆测试发布 scenario"},
    )

    assert response.status_code == 409
    payload = response.json()
    assert "canonical manual publication context" in payload["reason"]
    assert records.read("memory_scenarios", "staging-scenario-four-layer-1") is None


def test_memory_publication_rollback_marks_publication_rolled_back(tmp_path) -> None:
    _activate_memory_sqlite_authority(tmp_path)
    client = _client(tmp_path)
    store = _store(tmp_path)
    candidate_id = _seed_candidate(store)

    promote_response = client.post(
        f"/api/rebuild/memory-candidates/{candidate_id}/review",
        json={"action": "promote_to_atom", "reason": "提升后发布再回滚"},
    )
    staging_object_id = promote_response.json().get("promoted_object_id")
    publish_response = client.post(
        f"/api/rebuild/staging-atoms/{staging_object_id}/publication",
        json={"confirm": True, "reason": "发布后回滚"},
    )
    publication_id = publish_response.json()["publication_id"]

    response = client.post(
        f"/api/rebuild/memory-publications/{publication_id}/rollback",
        json={"confirm": True, "reason": "四层记忆测试回滚"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["memory_publication_state"] in {
        "rolled_back",
        "rolled_back_with_evidence",
        "rolled_back_not_published",
    }
    records = SQLiteStructuredRecordStore(
        tmp_path / ".rebuild-data" / "structured-records.sqlite3"
    )
    publication = records.read("memory_publications", publication_id)
    assert publication is not None
    assert publication.payload.get("status") == "rolled_back"


def test_staging_publication_rejects_missing_confirm(tmp_path) -> None:
    _activate_memory_sqlite_authority(tmp_path)
    client = _client(tmp_path)
    store = _store(tmp_path)
    store.write(
        "staging_atoms",
        "staging-atom-no-confirm",
        {
            "schema_version": "1.0.0",
            "id": "staging-atom-no-confirm",
            "layer": "atom",
            "title": "未确认发布测试",
            "summary": "测试缺少 confirm 字段。",
            "source_refs": ["source-1"],
            "status": "staged",
            "created_at": "2026-07-04T00:00:00+08:00",
        },
        expected_revision=None,
    )

    response = client.post(
        "/api/rebuild/staging-atoms/staging-atom-no-confirm/publication",
        json={"reason": "缺少 confirm"},
    )

    assert response.status_code == 400
