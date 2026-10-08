from __future__ import annotations

from pathlib import Path

import pytest

from core.project_skill_core import (
    ProjectSkillUpdate,
    ProjectSkillPublicationCompositeError,
    ProjectSkillPublicationTarget,
    SQLiteProjectSkillRepository,
    SQLiteProjectSkillPublicationCompositeUnitOfWork,
    build_project_skill_publication_draft,
)
from core.storage_provider import SQLiteStructuredRecordStore
from core.storage_provider.external_agent_publication_change import (
    publication_outbox_collection,
)


def _draft(
    *,
    expected_revision: int = 0,
    content: str = "沿用既有结构并保留来源。",
    current_payload: dict[str, object] | None = None,
) -> dict[str, object]:
    target = (
        ProjectSkillPublicationTarget.for_create("project-alpha")
        if expected_revision == 0
        else ProjectSkillPublicationTarget.for_update(
            project_id="project-alpha",
            skill_id="skill-project-alpha",
            expected_revision=expected_revision,
        )
    )
    return build_project_skill_publication_draft(
        target=target,
        source_candidate_id=f"candidate-alpha-{expected_revision + 1:03d}",
        proposed_content=content,
        source_refs=({"source_id": "source-alpha-001", "locator": "char:0-64"},),
        evidence_refs=({"source_id": "source-alpha-001", "locator": "char:0-64"},),
        reviewed_by="user",
        reviewed_at="2026-07-12T12:00:00+08:00",
        review_reason="用户确认发布该项目技能。",
        current_payload=current_payload,
    )


def _stage(records: SQLiteStructuredRecordStore, draft: dict[str, object]) -> None:
    with records.begin() as transaction:
        transaction.put("staging_project_skills", str(draft["id"]), draft, expected_revision=0)
        transaction.commit()


def test_publish_moves_draft_writes_five_object_aggregate_and_audit_in_one_commit(tmp_path: Path) -> None:
    database = tmp_path / "structured-records.sqlite3"
    records = SQLiteStructuredRecordStore(database)
    draft = _draft()
    _stage(records, draft)

    with SQLiteProjectSkillPublicationCompositeUnitOfWork(database, now="2026-07-12T12:05:00+08:00").begin() as transaction:
        result = transaction.publish(draft=draft)
        committed = transaction.commit()

    assert result.replayed is False
    assert committed.replayed is False
    assert records.read("staging_project_skills", str(draft["id"])) is None
    skill = records.read("project_skills", "skill-project-alpha")
    assert skill is not None
    assert skill.payload["status"] == "active"
    assert skill.payload["revision"] == 1
    assert records.read("project_skill_index", "project-alpha") is not None
    assert records.read("project_skill_markdown", "skill-project-alpha~r1") is not None
    assert records.read("project_skill_json", "skill-project-alpha~r1") is not None
    revision = records.read("project_skill_revisions", "skill-project-alpha~r1")
    assert revision is not None
    assert revision.payload["transition_kind"] == "ai_publication"
    assert revision.payload["confirmation_kind"] == "review_and_second_confirmation"
    assert records.read("memory_transitions", result.transition_id) is not None
    assert records.read("memory_publications", result.publication_id) is not None
    outbox = records.read(
        publication_outbox_collection("project-alpha"), result.publication_id,
    )
    assert outbox is not None
    assert outbox.payload == {
        "schema": "external-agent-publication-outbox-v1",
        "state": "pending",
        "event": {
            "publication_identity": result.publication_id,
            "project_id": "project-alpha",
            "change_type": "project_skill.published",
            "object_ref": "crp://skills/project-alpha/skill-project-alpha",
            "object_revision": "project-skill-r1",
            "occurred_at": "2026-07-12T12:05:00+08:00",
        },
        "attempt_count": 0,
        "last_attempt_at": None,
    }


def test_rollback_appends_rolled_back_revision_and_preserves_history(tmp_path: Path) -> None:
    database = tmp_path / "structured-records.sqlite3"
    records = SQLiteStructuredRecordStore(database)
    draft = _draft()
    _stage(records, draft)
    composite = SQLiteProjectSkillPublicationCompositeUnitOfWork(database, now="2026-07-12T12:05:00+08:00")
    with composite.begin() as transaction:
        published = transaction.publish(draft=draft)
        transaction.commit()

    with composite.begin() as transaction:
        rolled_back = transaction.rollback(
            publication_id=published.publication_id,
            expected_publication_revision=1,
            expected_project_skill_revision=1,
            reason="用户撤回该项目技能。",
        )
        transaction.commit()

    current = records.read("project_skills", "skill-project-alpha")
    assert current is not None
    assert current.payload["status"] == "rolled_back"
    assert current.payload["revision"] == 2
    assert records.read("project_skill_markdown", "skill-project-alpha~r1") is not None
    assert records.read("project_skill_json", "skill-project-alpha~r1") is not None
    assert records.read("project_skill_revisions", "skill-project-alpha~r1") is not None
    rollback_revision = records.read("project_skill_revisions", "skill-project-alpha~r2")
    assert rollback_revision is not None
    assert rollback_revision.payload["transition_kind"] == "ai_publication_rollback"
    publication = records.read("memory_publications", published.publication_id)
    assert publication is not None
    assert publication.payload["status"] == "rolled_back"
    assert records.read("memory_transitions", rolled_back.transition_id) is not None
    outbox = records.read(
        publication_outbox_collection("project-alpha"), published.publication_id,
    )
    assert outbox is not None
    assert outbox.payload["event"]["change_type"] == "project_skill.published"
    assert outbox.payload["event"]["object_revision"] == "project-skill-r1"
    invalidation = records.read(
        publication_outbox_collection("project-alpha"), rolled_back.transition_id,
    )
    assert invalidation is not None
    assert invalidation.payload["event"] == {
        "publication_identity": rolled_back.transition_id,
        "project_id": "project-alpha",
        "change_type": "project_skill.invalidated",
        "object_ref": "crp://skills/project-alpha/skill-project-alpha",
        "object_revision": "project-skill-r2",
        "occurred_at": "2026-07-12T12:05:00+08:00",
    }
    assert len(records.list(publication_outbox_collection("project-alpha"))) == 2


def test_update_publish_keeps_stable_skill_identity_and_appends_history(tmp_path: Path) -> None:
    database = tmp_path / "structured-records.sqlite3"
    records = SQLiteStructuredRecordStore(database)
    first = _draft()
    _stage(records, first)
    composite = SQLiteProjectSkillPublicationCompositeUnitOfWork(database, now="2026-07-12T12:05:00+08:00")
    with composite.begin() as transaction:
        transaction.publish(draft=first)
        transaction.commit()
    repository = SQLiteProjectSkillRepository(records)
    current = dict(repository.load("project-alpha"))
    current["outline"] = [
        {
            "section_id": "manual-summary",
            "title": "人工摘要",
            "kind": "summary",
            "required": True,
        }
    ]
    manual = repository.save(
        ProjectSkillUpdate(
            project_id="project-alpha",
            markdown=str(repository.markdown("project-alpha")),
            structured=current,
            expected_revision=1,
            reason="用户确认保存项目级章节结构",
            transition_kind="user_edit",
            actor="user",
            confirmation_kind="direct_user_save",
        )
    )
    update = _draft(
        expected_revision=2,
        content="保留原结构，并追加新的证据要求。",
        current_payload=dict(manual),
    )
    _stage(records, update)
    with composite.begin() as transaction:
        result = transaction.publish(draft=update)
        transaction.commit()

    current = records.read("project_skills", "skill-project-alpha")
    assert current is not None
    assert current.payload["id"] == "skill-project-alpha"
    assert current.payload["revision"] == 3
    assert current.payload["status"] == "active"
    assert current.payload["outline"] == [
        {
            "section_id": "manual-summary",
            "title": "人工摘要",
            "kind": "summary",
            "required": True,
        }
    ]
    assert records.read("project_skill_markdown", "skill-project-alpha~r1") is not None
    assert records.read("project_skill_markdown", "skill-project-alpha~r2") is not None
    assert records.read("project_skill_markdown", "skill-project-alpha~r3") is not None
    assert records.read("memory_publications", result.publication_id).payload["published_revision"] == 3
    outbox = records.read(
        publication_outbox_collection("project-alpha"), result.publication_id,
    )
    assert outbox is not None
    assert outbox.payload["event"] == {
        "publication_identity": result.publication_id,
        "project_id": "project-alpha",
        "change_type": "project_skill.published",
        "object_ref": "crp://skills/project-alpha/skill-project-alpha",
        "object_revision": "project-skill-r3",
        "occurred_at": "2026-07-12T12:05:00+08:00",
    }


def test_uncommitted_project_skill_publish_does_not_claim_external_publication(tmp_path: Path) -> None:
    database = tmp_path / "structured-records.sqlite3"
    records = SQLiteStructuredRecordStore(database)
    draft = _draft()
    _stage(records, draft)

    with SQLiteProjectSkillPublicationCompositeUnitOfWork(database).begin() as transaction:
        transaction.publish(draft=draft)

    assert records.read("project_skills", "skill-project-alpha") is None
    assert records.read(
        publication_outbox_collection("project-alpha"),
        f"memory-publication-project-skill-{draft['id']}",
    ) is None


def test_migrated_domain_revision_uses_sqlite_record_revision_for_cas(tmp_path: Path) -> None:
    source = SQLiteStructuredRecordStore(tmp_path / "source.sqlite3")
    first = _draft()
    _stage(source, first)
    source_composite = SQLiteProjectSkillPublicationCompositeUnitOfWork(tmp_path / "source.sqlite3")
    with source_composite.begin() as transaction:
        transaction.publish(draft=first)
        transaction.commit()
    second = _draft(
        expected_revision=1,
        content="Second.",
        current_payload=dict(source.read("project_skills", "skill-project-alpha").payload),
    )
    _stage(source, second)
    with source_composite.begin() as transaction:
        transaction.publish(draft=second)
        transaction.commit()
    database = tmp_path / "migrated.sqlite3"
    records = SQLiteStructuredRecordStore(database)
    with records.begin() as transaction:
        for collection in (
            "project_skill_index",
            "project_skill_json",
            "project_skill_markdown",
            "project_skill_revisions",
            "project_skills",
        ):
            for record in source.list(collection):
                transaction.put(
                    collection,
                    record.object_id,
                    record.payload,
                    expected_revision=0,
                )
        transaction.commit()
    assert records.read("project_skills", "skill-project-alpha").revision == 1
    assert records.read("project_skills", "skill-project-alpha").payload["revision"] == 2
    draft = _draft(
        expected_revision=2,
        content="迁移后追加第三版。",
        current_payload=dict(records.read("project_skills", "skill-project-alpha").payload),
    )
    _stage(records, draft)

    with SQLiteProjectSkillPublicationCompositeUnitOfWork(database).begin() as transaction:
        result = transaction.publish(draft=draft)
        transaction.commit()

    assert result.project_skill_revision == 3
    current = records.read("project_skills", "skill-project-alpha")
    assert current is not None
    assert current.revision == 2
    assert current.payload["revision"] == 3


def test_publish_conflict_and_drift_fail_closed_without_partial_aggregate(tmp_path: Path) -> None:
    database = tmp_path / "structured-records.sqlite3"
    records = SQLiteStructuredRecordStore(database)
    draft = _draft()
    _stage(records, draft)
    tampered = dict(draft)
    tampered["draft_digest"] = "0" * 64

    with pytest.raises(ProjectSkillPublicationCompositeError, match="digest"):
        with SQLiteProjectSkillPublicationCompositeUnitOfWork(database).begin() as transaction:
            transaction.publish(draft=tampered)
    assert records.read("project_skills", "skill-project-alpha") is None

    publication_id = f"memory-publication-project-skill-{draft['id']}"
    with records.begin() as transaction:
        transaction.put("memory_publications", publication_id, {"id": publication_id, "status": "other"}, expected_revision=0)
        transaction.commit()
    with pytest.raises(ProjectSkillPublicationCompositeError, match="append-only"):
        with SQLiteProjectSkillPublicationCompositeUnitOfWork(database).begin() as transaction:
            transaction.publish(draft=draft)
    assert records.read("project_skills", "skill-project-alpha") is None
    assert records.read("staging_project_skills", str(draft["id"])) is not None


def test_publish_and_rollback_strict_replay_do_not_advance_revision(tmp_path: Path) -> None:
    database = tmp_path / "structured-records.sqlite3"
    records = SQLiteStructuredRecordStore(database)
    draft = _draft()
    _stage(records, draft)
    composite = SQLiteProjectSkillPublicationCompositeUnitOfWork(database, now="2026-07-12T12:05:00+08:00")
    with composite.begin() as transaction:
        published = transaction.publish(draft=draft)
        transaction.commit()
    with composite.begin() as transaction:
        replay = transaction.publish(draft=draft)
        transaction.commit()
    assert replay.replayed is True
    assert records.read("project_skills", "skill-project-alpha").payload["revision"] == 1

    with composite.begin() as transaction:
        rolled_back = transaction.rollback(
            publication_id=published.publication_id,
            expected_publication_revision=1,
            expected_project_skill_revision=1,
            reason="用户撤回该项目技能。",
        )
        transaction.commit()
    with composite.begin() as transaction:
        replay_rollback = transaction.rollback(
            publication_id=published.publication_id,
            expected_publication_revision=2,
            expected_project_skill_revision=1,
            reason="用户撤回该项目技能。",
        )
        transaction.commit()
    assert rolled_back.replayed is False
    assert replay_rollback.replayed is True
    assert records.read("project_skills", "skill-project-alpha").payload["revision"] == 2
    assert len(records.list(publication_outbox_collection("project-alpha"))) == 2
    assert records.read(
        publication_outbox_collection("project-alpha"), rolled_back.transition_id,
    ).payload["event"]["publication_identity"] == rolled_back.transition_id
