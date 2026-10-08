from __future__ import annotations

import pytest

from backend.recognition import RecognitionConflict, RecognitionService, WorkScope
from core.storage_provider import SQLiteStructuredRecordStore


@pytest.fixture
def service(tmp_path):
    return RecognitionService(SQLiteStructuredRecordStore(tmp_path / "recognitions.sqlite3"))


@pytest.fixture
def scope():
    return WorkScope("user-1", "project-1")


def test_pending_candidate_edit_publishes_its_final_content_and_conditions(service, scope):
    experience_id = service.stage_experience(scope=scope, content="先在网页中验证工作流")
    candidate = service.propose(
        scope=scope,
        content="初始候选",
        source_experience_ids=[experience_id],
        conditions=["仅用于原型阶段"],
    )

    edited = service.edit_candidate(
        scope=scope,
        candidate_id=candidate.id,
        expected_revision=candidate.revision,
        content="网页优先，桌面壳后置",
        conditions=["先完成网页闭环", "桌面打包留待稳定版本"],
        editor="reviewer-1",
    )
    stored = service.records.read("recognition_candidates", candidate.id)
    recognition = service.publish(
        scope=scope,
        candidate_id=edited.id,
        expected_revision=edited.revision,
        reviewer="reviewer-1",
    )

    assert edited.revision == candidate.revision + 1
    assert edited.content == "网页优先，桌面壳后置"
    assert edited.conditions == ("先完成网页闭环", "桌面打包留待稳定版本")
    assert stored is not None
    assert stored.payload["initial_content"] == "初始候选"
    assert stored.payload["initial_conditions"] == ["仅用于原型阶段"]
    assert stored.payload["updated_by"] == "reviewer-1"
    assert recognition.content == edited.content
    assert recognition.conditions == edited.conditions


def test_candidate_edit_rejects_stale_revision_and_other_scope(service, scope):
    experience_id = service.stage_experience(scope=scope, content="需要人工审核")
    candidate = service.propose(scope=scope, content="候选", source_experience_ids=[experience_id])
    edited = service.edit_candidate(
        scope=scope,
        candidate_id=candidate.id,
        expected_revision=candidate.revision,
        content="编辑后候选",
    )

    with pytest.raises(RecognitionConflict, match="revision"):
        service.edit_candidate(
            scope=scope,
            candidate_id=candidate.id,
            expected_revision=candidate.revision,
            content="陈旧写入",
        )
    with pytest.raises(RecognitionConflict, match="scope"):
        service.edit_candidate(
            scope=WorkScope("user-1", "project-2"),
            candidate_id=candidate.id,
            expected_revision=edited.revision,
            content="跨项目写入",
        )


def test_candidate_edit_rejects_a_candidate_invalidated_by_source_change(service, scope):
    experience_id = service.stage_experience(scope=scope, content="来源经历")
    candidate = service.propose(scope=scope, content="依赖来源的候选", source_experience_ids=[experience_id])
    experience = service.list_experiences(scope=scope)[0]

    service.revoke_experience(scope=scope, experience_id=experience.id, expected_revision=experience.revision)
    invalidated = service.list_candidates(scope=scope, include_inactive=True)[0]

    assert invalidated.state == "invalidated"
    with pytest.raises(RecognitionConflict, match="pending"):
        service.edit_candidate(
            scope=scope,
            candidate_id=candidate.id,
            expected_revision=invalidated.revision,
            content="不能用编辑洗白失效来源",
        )


def test_candidate_edit_noop_keeps_revision_and_initial_snapshot_absent(service, scope):
    experience_id = service.stage_experience(scope=scope, content="来源经历")
    candidate = service.propose(
        scope=scope,
        content="不改内容",
        source_experience_ids=[experience_id],
        conditions=["条件"],
    )

    unchanged = service.edit_candidate(
        scope=scope,
        candidate_id=candidate.id,
        expected_revision=candidate.revision,
        content="不改内容",
        conditions=["条件"],
    )
    stored = service.records.read("recognition_candidates", candidate.id)

    assert unchanged == candidate
    assert stored is not None
    assert "initial_content" not in stored.payload
    assert "updated_at" not in stored.payload
