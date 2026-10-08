from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from backend.api.app import create_app
from fastapi.testclient import TestClient

from core.aggregate_repository_factory import AUTHORITY_DATABASE_NAME
from core.memory_core import ObjectStoreMemoryCandidateRepository
from core.shared_trust_audit_activation_preflight import (
    activate_verified_shared_trust_audit,
)
from core.shared_trust_audit_activation_service import SharedTrustAuditActivationSagaService
from core.shared_trust_audit_fixture_migration import (
    execute_shared_trust_audit_fixture_migration,
)
from core.storage_provider import (
    JsonObjectStore,
    SQLiteAggregateAuthorityStore,
    SQLiteSharedTrustAuditActivationSagaStore,
    SQLiteStructuredRecordStore,
)
from tests.rebuild.test_shared_trust_audit_fixture_migration import _fixture


def _candidate(
    *,
    candidate_id: str = "candidate-default-project-skill-update",
    content: str = "默认项目下一阶段优先完成可恢复的本地问答与证据召回。",
    source_id: str = "source-default-project-plan",
    document_id: str = "document-default-project-plan",
    created_at: str = "2026-07-13T09:00:00+08:00",
) -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "id": candidate_id,
        "project_id": "default",
        "target_layer": "project_skill",
        "candidate_type": "answer_summary",
        "status": "pending_review",
        "proposed_content": content,
        "source_refs": [
            {
                "source_id": source_id,
                "locator": "char:0-64",
                "quote": content,
            }
        ],
        "provenance": {
            "model_result_id": None,
            "model_request_id": None,
            "recall_result_id": None,
            "document_id": document_id,
            "document_revision": 1,
            "source_content_read_id": None,
            "input_refs": [
                {
                    "kind": "document",
                    "object_id": document_id,
                    "uri": f"crp://default/documents/{document_id}.json",
                }
            ],
        },
        "review": {
            "requires_user_confirmation": True,
            "auto_promote_allowed": False,
            "reason": "等待用户确认。",
            "reviewed_by": None,
            "reviewed_at": None,
        },
        "created_at": created_at,
        "updated_at": created_at,
    }


def test_real_composite_activation_enables_project_skill_publication_and_recall(
    tmp_path: Path,
) -> None:
    root, store, _inventory, ledger, dry_run, *_ = _fixture(
        tmp_path,
        project_id="default",
    )
    target = root / "structured-records.sqlite3"
    migration = execute_shared_trust_audit_fixture_migration(
        object_store_root=root,
        target_database_path=target,
        ledger=ledger,
        dry_run=dry_run,
    )
    records = SQLiteStructuredRecordStore(target)
    authority = SQLiteAggregateAuthorityStore(root / AUTHORITY_DATABASE_NAME)
    service = SharedTrustAuditActivationSagaService(
        operations=SQLiteSharedTrustAuditActivationSagaStore(records),
        records=records,
        authority=authority,
    )
    activation = activate_verified_shared_trust_audit(
        object_store_root=root,
        target_database_path=target,
        authority=authority,
        namespace_id="default",
        activation_id="project-skill-direct-question-e2e-v1",
        migration=migration,
        service=service,
        now="2026-07-13T09:05:00+08:00",
    )
    json_skill_before = dict(store.read("project_skills", "skill-default"))
    json_publications_before = store.list("memory_publications")
    ObjectStoreMemoryCandidateRepository(store).save(_candidate())

    with TestClient(create_app(SimpleNamespace(root_dir=tmp_path / "source"))) as client:
        reviewed = client.post(
            "/api/rebuild/memory-candidates/candidate-default-project-skill-update/review",
            json={
                "action": "promote_to_project_skill",
                "reason": "用户确认默认项目 Skill 草案。",
            },
        )
        draft_id = reviewed.json().get("promoted_object_id")
        published = client.post(
            f"/api/rebuild/staging-project-skills/{draft_id}/publication",
            json={"confirm": True, "reason": "用户二次确认发布默认项目 Skill。"},
        )
        replayed = client.post(
            f"/api/rebuild/staging-project-skills/{draft_id}/publication",
            json={"confirm": True, "reason": "用户二次确认发布默认项目 Skill。"},
        )
        outline = [
            {
                "section_id": "user-confirmed-evidence",
                "title": "用户确认的证据结构",
                "kind": "sources",
                "required": True,
            }
        ]
        user_edited = client.put(
            "/api/rebuild/projects/default/skill/outline",
            json={
                "outline": outline,
                "expected_revision": published.json()["project_skill_revision"],
                "reason": "用户确认后固定后续输出证据结构。",
            },
        )
        second_candidate = _candidate(
            candidate_id="candidate-default-project-skill-update-2",
            content="新增资料要求后续项目建议固定包含风险、证据和下一步。",
            source_id="source-default-project-follow-up",
            document_id="document-default-project-follow-up",
            created_at="2026-07-13T10:00:00+08:00",
        )
        ObjectStoreMemoryCandidateRepository(store).save(second_candidate)
        reviewed_again = client.post(
            "/api/rebuild/memory-candidates/candidate-default-project-skill-update-2/review",
            json={
                "action": "promote_to_project_skill",
                "reason": "用户确认把新增资料加入同一项目 Skill。",
            },
        )
        second_draft_id = reviewed_again.json().get("promoted_object_id")
        published_again = client.post(
            f"/api/rebuild/staging-project-skills/{second_draft_id}/publication",
            json={"confirm": True, "reason": "用户二次确认发布连续更新。"},
        )
        replayed_again = client.post(
            f"/api/rebuild/staging-project-skills/{second_draft_id}/publication",
            json={"confirm": True, "reason": "用户二次确认发布连续更新。"},
        )
        direct_question = client.post(
            "/api/rebuild/workbench/direct-question",
            json={"question": "默认项目后续建议需要包含哪些内容？"},
        )
        published_current_skill = records.read("project_skills", "skill-default").payload
        rolled_back = client.post(
            "/api/rebuild/projects/default/skill/rollback",
            json={
                "confirm": True,
                "target_revision": 4,
                "expected_revision": 5,
                "reason": "用户确认恢复AI连续更新前的人工结构。",
            },
        )
        direct_question_after_rollback = client.post(
            "/api/rebuild/workbench/direct-question",
            json={"question": "恢复后默认项目应采用什么证据结构？"},
        )

    assert activation.operation.state == "finalized"
    assert reviewed.status_code == 200
    assert published.status_code == 200, published.text
    assert published.json()["status"] == "published"
    assert published.json()["project_skill_revision"] == 3
    assert replayed.status_code == 409
    assert user_edited.status_code == 200, user_edited.text
    assert user_edited.json()["revision"] == 4
    assert user_edited.json()["outline"] == outline
    assert reviewed_again.status_code == 200, reviewed_again.text
    assert published_again.status_code == 200, published_again.text
    assert published_again.json()["project_skill_revision"] == 5
    assert replayed_again.status_code == 409
    body = direct_question.json()
    assert direct_question.status_code == 200
    assert body["recall_status"] == "recalled"
    assert body["evidence_count"] >= 1
    assert "l3_project_skill" in {item["layer"] for item in body["evidence_refs"]}
    assert published_current_skill["revision"] == 5
    assert published_current_skill["outline"] == outline
    assert "风险、证据和下一步" in published_current_skill["purpose"]
    assert "source-default-project-follow-up" in str(published_current_skill["source_refs"])
    assert rolled_back.status_code == 200, rolled_back.text
    assert rolled_back.json()["revision"] == 6
    assert rolled_back.json()["outline"] == outline
    assert direct_question_after_rollback.status_code == 200
    assert direct_question_after_rollback.json()["recall_status"] == "recalled"
    assert "l3_project_skill" in {
        item["layer"] for item in direct_question_after_rollback.json()["evidence_refs"]
    }
    current_skill = records.read("project_skills", "skill-default").payload
    assert current_skill["revision"] == 6
    assert current_skill["outline"] == outline
    assert tuple(record.object_id for record in records.list("project_skills")) == (
        "skill-default",
    )
    assert len(records.list("project_skill_revisions")) == 6
    assert "风险、证据和下一步" in body["answer"]["text"]
    publications = [
        record.payload
        for record in records.list("memory_publications")
        if record.payload.get("object_type") == "project_skill"
    ]
    assert len(publications) == 3
    assert sum(item["status"] == "published" for item in publications) == 1
    assert sum(item["status"] == "superseded" for item in publications) == 2
    assert dict(store.read("project_skills", "skill-default")) == json_skill_before
    assert store.list("memory_publications") == json_publications_before
