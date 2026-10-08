from __future__ import annotations

import pytest

from backend.recognition import RecognitionConflict, RecognitionError, RecognitionService, WorkScope
from core.storage_provider import SQLiteStructuredRecordStore


@pytest.fixture
def service(tmp_path):
    return RecognitionService(SQLiteStructuredRecordStore(tmp_path / "recognitions.sqlite3"))


@pytest.fixture
def scope():
    return WorkScope("user-1", "project-1")


def _published(service, scope, content, *, experience_id=None, conditions=()):
    experience_id = experience_id or service.stage_experience(scope=scope, content=f"证据：{content}")
    candidate = service.propose(
        scope=scope,
        content=content,
        source_experience_ids=[experience_id],
        conditions=conditions,
    )
    return service.publish(
        scope=scope,
        candidate_id=candidate.id,
        expected_revision=candidate.revision,
        reviewer="user-1",
    )


def test_merge_can_select_exact_sources_and_replace_conditions(service, scope):
    first = _published(service, scope, "SQLite 适合本地", conditions=["单用户"])
    second = _published(service, scope, "网页适合验证", conditions=["网页阶段"])
    replacement_experience = service.stage_experience(scope=scope, content="新的验证证据")
    supporting = _published(service, scope, "部署仍待验证")

    merged = service.merge(
        scope=scope,
        recognition_ids=[first.id, second.id],
        expected_revisions={first.id: first.revision, second.id: second.revision},
        content="当前采用 SQLite 网页架构",
        source_experience_ids=[replacement_experience],
        source_recognition_ids=[supporting.id],
        conditions=["仅在本地"],
        replacement_conditions=["迁移前重新验收"],
    )

    assert merged.source_experience_ids == (replacement_experience,)
    assert merged.source_experience_revisions == {replacement_experience: 1}
    assert merged.source_recognition_ids == (supporting.id,)
    assert merged.source_recognition_revisions == {supporting.id: supporting.revision}
    assert merged.conditions == ("迁移前重新验收",)


def test_merge_without_selective_parameters_keeps_the_legacy_unions(service, scope):
    first = _published(service, scope, "SQLite 适合本地", conditions=["单用户"])
    second = _published(service, scope, "网页适合验证", conditions=["网页阶段"])

    merged = service.merge(
        scope=scope,
        recognition_ids=[first.id, second.id],
        expected_revisions={first.id: first.revision, second.id: second.revision},
        content="当前采用 SQLite 网页架构",
        conditions=["桌面壳尚未验证"],
    )

    assert merged.source_experience_ids == (*first.source_experience_ids, *second.source_experience_ids)
    assert merged.source_recognition_ids == ()
    assert merged.conditions == ("单用户", "网页阶段", "桌面壳尚未验证")


def test_merge_rejects_a_parent_as_an_explicit_new_source_without_partial_write(service, scope):
    first = _published(service, scope, "SQLite 适合本地")
    second = _published(service, scope, "网页适合验证")

    with pytest.raises(RecognitionError, match="parent recognitions"):
        service.merge(
            scope=scope,
            recognition_ids=[first.id, second.id],
            expected_revisions={first.id: first.revision, second.id: second.revision},
            content="不应发布",
            source_recognition_ids=[first.id],
        )

    assert service.get_recognition(scope=scope, recognition_id=first.id).state == "active"
    assert service.get_recognition(scope=scope, recognition_id=second.id).state == "active"


def test_merge_rejects_invalid_parent_provenance_even_when_explicit_sources_exclude_it(service, scope):
    first = _published(service, scope, "SQLite 适合本地")
    second = _published(service, scope, "网页适合验证")
    invalidated_experience = first.source_experience_ids[0]
    experience = next(item for item in service.list_experiences(scope=scope) if item.id == invalidated_experience)
    service.revoke_experience(scope=scope, experience_id=experience.id, expected_revision=experience.revision)
    stale_first = service.get_recognition(scope=scope, recognition_id=first.id)

    with pytest.raises(RecognitionConflict, match="no longer active"):
        service.merge(
            scope=scope,
            recognition_ids=[first.id, second.id],
            expected_revisions={first.id: stale_first.revision, second.id: second.revision},
            content="不应发布",
            source_experience_ids=[],
            source_recognition_ids=[],
            replacement_conditions=[],
        )

    assert service.get_recognition(scope=scope, recognition_id=first.id).state == "stale"
    assert service.get_recognition(scope=scope, recognition_id=second.id).state == "active"


def test_merge_validates_explicit_source_liveness_and_scope_before_parent_changes(service, scope):
    first = _published(service, scope, "SQLite 适合本地")
    second = _published(service, scope, "网页适合验证")
    revoked = service.stage_experience(scope=scope, content="已撤销的证据")
    revoked_record = next(item for item in service.list_experiences(scope=scope) if item.id == revoked)
    service.revoke_experience(scope=scope, experience_id=revoked, expected_revision=revoked_record.revision)

    with pytest.raises(RecognitionConflict, match="revoked"):
        service.merge(
            scope=scope,
            recognition_ids=[first.id, second.id],
            expected_revisions={first.id: first.revision, second.id: second.revision},
            content="不应发布",
            source_experience_ids=[revoked],
        )

    foreign_scope = WorkScope("user-1", "project-2")
    foreign = _published(service, foreign_scope, "其他项目的认识")
    with pytest.raises(RecognitionConflict, match="scope"):
        service.merge(
            scope=scope,
            recognition_ids=[first.id, second.id],
            expected_revisions={first.id: first.revision, second.id: second.revision},
            content="不应发布",
            source_recognition_ids=[foreign.id],
        )

    assert service.get_recognition(scope=scope, recognition_id=first.id).state == "active"
    assert service.get_recognition(scope=scope, recognition_id=second.id).state == "active"
