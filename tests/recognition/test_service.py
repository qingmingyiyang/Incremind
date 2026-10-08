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


def _published(service, scope, content="网页优先"):
    experience_id = service.stage_experience(scope=scope, content="Electron 构建耗时很长")
    candidate = service.propose(scope=scope, content=content, source_experience_ids=[experience_id])
    return service.publish(scope=scope, candidate_id=candidate.id, expected_revision=candidate.revision, reviewer="user-1")


def test_selected_experience_requires_manual_review_before_publication(service, scope):
    experience_id = service.stage_experience(scope=scope, content="先以网页验证核心流程")
    candidate = service.propose(scope=scope, content="桌面壳后置", source_experience_ids=[experience_id])

    assert candidate.state == "pending"
    assert service.list_recognitions(scope=scope) == ()

    recognition = service.publish(
        scope=scope, candidate_id=candidate.id, expected_revision=candidate.revision, reviewer="user-1"
    )

    assert recognition.content == "桌面壳后置"
    assert recognition.project_id == "project-1"
    assert service.list_candidates(scope=scope) == ()


def test_candidate_cannot_revive_after_its_experience_is_revoked(service, scope):
    experience_id = service.stage_experience(scope=scope, content="旧构建过程需要封存")
    candidate = service.propose(scope=scope, content="保留网页入口", source_experience_ids=[experience_id])
    experience = service.list_experiences(scope=scope)[0]

    service.revoke_experience(scope=scope, experience_id=experience_id, expected_revision=experience.revision)

    assert service.list_candidates(scope=scope) == ()
    invalidated = next(item for item in service.list_candidates(scope=scope, include_inactive=True) if item.id == candidate.id)
    assert invalidated.state == "invalidated"
    with pytest.raises(RecognitionConflict, match="pending"):
        service.publish(scope=scope, candidate_id=candidate.id, expected_revision=invalidated.revision, reviewer="user-1")


def test_scope_isolation_hides_records_from_other_project(service, scope):
    recognition = _published(service, scope)

    other_scope = WorkScope("user-1", "project-2")

    assert service.get_recognition(scope=other_scope, recognition_id=recognition.id) is None
    assert service.list_recognitions(scope=other_scope) == ()
    with pytest.raises(RecognitionConflict, match="scope"):
        service.revise(scope=other_scope, recognition_id=recognition.id, expected_revision=recognition.revision, content="错误写入")


def test_retrieval_projection_is_current_scoped_and_has_provenance(service, scope):
    recognition = _published(service, scope)

    entries = service.retrieval_entries(scope=scope)

    assert entries == (
        {
            "id": recognition.id,
            "revision": recognition.revision,
            "current_revision": recognition.revision,
            "project_id": "project-1",
            "content": "网页优先",
            "status": "active",
            "recorded_state": "active",
            "evidence_eligible": True,
            "evidence_reason": None,
            "authorized": True,
            "source_refs": [{"type": "experience", "id": recognition.source_experience_ids[0], "revision": 1}],
            "source_experience_ids": [recognition.source_experience_ids[0]],
            "source_recognition_ids": [],
            "conditions": [],
            "source_evidence": [dict(item) for item in recognition.source_evidence],
            "source_evidence_complete": True,
            "source_evidence_reason": None,
        },
    )


def test_stale_version_cannot_overwrite_current_recognition(service, scope):
    recognition = _published(service, scope)
    current = service.revise(
        scope=scope, recognition_id=recognition.id, expected_revision=recognition.revision, content="网页先行，桌面壳后置"
    )

    with pytest.raises(RecognitionConflict, match="revision"):
        service.revise(scope=scope, recognition_id=recognition.id, expected_revision=recognition.revision, content="旧编辑")

    assert current.content == "网页先行，桌面壳后置"


def test_split_is_atomic_and_invalidates_fixed_question(service, scope):
    parent = _published(service, scope, content="SQLite 本地单用户，网页优先")
    service.upsert_question(
        scope=scope, question_id="architecture", question="当前技术架构如何选择？",
        content="当前先采用网页和 SQLite。", recognition_ids=[parent.id],
        source_revisions={parent.id: parent.revision}, expected_revision=0,
    )

    children = service.split(
        scope=scope, recognition_id=parent.id, expected_revision=parent.revision,
        parts=["SQLite 适合本地单用户", "网页先验证核心流程"], new_ids=["recognition-db", "recognition-web"],
    )

    assert [child.content for child in children] == ["SQLite 适合本地单用户", "网页先验证核心流程"]
    assert service.get_recognition(scope=scope, recognition_id=parent.id).state == "superseded"
    assert service.list_questions(scope=scope)[0].state == "stale"
    assert {item.id for item in service.list_recognitions(scope=scope)} == {"recognition-db", "recognition-web"}


def test_merge_rolls_back_every_parent_when_child_id_conflicts(service, scope):
    first = _published(service, scope, content="SQLite 是当前权威存储")
    second = _published(service, scope, content="网页是当前产品入口")
    _published(service, scope, content="保留的占用项")

    with pytest.raises(RecognitionConflict, match="already exists"):
        service.merge(
            scope=scope, recognition_ids=[first.id, second.id],
            expected_revisions={first.id: first.revision, second.id: second.revision},
            content="当前采用 SQLite 网页应用", recognition_id=first.id,
        )

    assert service.get_recognition(scope=scope, recognition_id=first.id).state == "active"
    assert service.get_recognition(scope=scope, recognition_id=second.id).state == "active"


def test_markdown_preview_and_commit_use_exported_revision(service, scope):
    recognition = _published(service, scope)
    exported = service.export_markdown(scope=scope, recognition_id=recognition.id)
    changed = exported.replace("网页优先", "网页先验证，再考虑桌面壳")

    preview = service.markdown_preview(scope=scope, markdown=changed)
    committed = service.markdown_commit(scope=scope, markdown=changed)

    assert preview.changed is True
    assert committed.content == "网页先验证，再考虑桌面壳"
    with pytest.raises(RecognitionConflict, match="revision"):
        service.markdown_preview(scope=scope, markdown=changed)


def test_markdown_export_is_readable_and_condition_edits_invalidate_dependents(service, scope):
    source = _published(service, scope)
    derived = service.propose(
        scope=scope, content="因此桌面壳后置", source_experience_ids=[], source_recognition_ids=[source.id]
    )
    service.upsert_question(
        scope=scope, question_id="architecture", question="当前架构是什么？", content="网页优先。",
        recognition_ids=[source.id], source_revisions={source.id: source.revision}, expected_revision=0,
    )
    exported = service.export_markdown(scope=scope, recognition_id=source.id)
    edited = exported.replace("## Conditions\n\n", "## Conditions\n- 仅适用于本地单用户\n")

    preview = service.markdown_preview(scope=scope, markdown=edited)
    committed = service.markdown_commit(scope=scope, markdown=edited)

    assert "## Project\nproject-1" in exported
    assert f"## Recognition ID\n{source.id}" in exported
    assert f"## Base revision\n{source.revision}" in exported
    assert f"- `experience:{source.source_experience_ids[0]}` (revision: 1)" in exported
    assert "## Content\n\n网页优先" in exported
    assert preview.conditions == ("仅适用于本地单用户",)
    assert committed.conditions == ("仅适用于本地单用户",)
    invalidated = next(item for item in service.list_candidates(scope=scope, include_inactive=True) if item.id == derived.id)
    assert invalidated.state == "invalidated"
    assert service.list_questions(scope=scope)[0].state == "stale"


@pytest.mark.parametrize(
    ("replacement", "message"),
    [
        (("## Project\nproject-1", "## Project\nproject-2"), "project identity"),
        (("## Recognition ID\n", "## Recognition ID\nrecognition-forged"), "recognition identity"),
        (("(revision: 1)", "(revision: 99)"), "source references"),
    ],
)
def test_markdown_rejects_forged_visible_identity_and_sources(service, scope, replacement, message):
    recognition = _published(service, scope)
    exported = service.export_markdown(scope=scope, recognition_id=recognition.id)
    if replacement[0] == "## Recognition ID\n":
        edited = exported.replace(replacement[0], replacement[1], 1)
    else:
        edited = exported.replace(*replacement, 1)

    with pytest.raises(RecognitionConflict, match=message):
        service.markdown_preview(scope=scope, markdown=edited)


def test_markdown_scope_and_legacy_export_are_checked_without_losing_existing_conditions(service, scope):
    recognition = _published(service, scope)
    exported = service.export_markdown(scope=scope, recognition_id=recognition.id)
    legacy = exported.split("\n", 1)[0] + "\n\n# Recognition\n\n旧格式正文\n"

    with pytest.raises(RecognitionConflict, match="scope"):
        service.markdown_preview(scope=WorkScope("user-1", "other-project"), markdown=exported)

    committed = service.markdown_commit(scope=scope, markdown=legacy)

    assert committed.content == "旧格式正文"
    assert committed.conditions == ()


def test_rejected_candidate_never_publishes(service, scope):
    experience_id = service.stage_experience(scope=scope, content="需要审阅")
    candidate = service.propose(scope=scope, content="未经确认的候选", source_experience_ids=[experience_id])
    rejected = service.reject_candidate(scope=scope, candidate_id=candidate.id, expected_revision=candidate.revision, reviewer="user-1")

    assert rejected.state == "rejected"
    with pytest.raises(RecognitionConflict, match="pending"):
        service.publish(scope=scope, candidate_id=candidate.id, expected_revision=rejected.revision, reviewer="user-1")


def test_fixed_question_only_becomes_current_with_matching_dependency_and_question_versions(service, scope):
    recognition = _published(service, scope)
    question = service.upsert_question(
        scope=scope, question_id="architecture", question="当前架构是什么？", content="网页优先。",
        recognition_ids=[recognition.id], source_revisions={recognition.id: recognition.revision}, expected_revision=0,
    )
    changed = service.revise(
        scope=scope, recognition_id=recognition.id, expected_revision=recognition.revision, content="网页优先且保持 SQLite。"
    )

    with pytest.raises(RecognitionConflict, match="dependencies changed"):
        service.upsert_question(
            scope=scope, question_id=question.id, question=question.question, content="旧综合正文",
            recognition_ids=[recognition.id], source_revisions={recognition.id: recognition.revision}, expected_revision=question.revision,
        )
    with pytest.raises(RecognitionConflict, match="question revision"):
        service.upsert_question(
            scope=scope, question_id=question.id, question=question.question, content="新综合正文",
            recognition_ids=[changed.id], source_revisions={changed.id: changed.revision}, expected_revision=0,
        )


def test_revising_source_invalidates_old_candidate_before_it_can_publish(service, scope):
    source = _published(service, scope, content="网页优先")
    candidate = service.propose(
        scope=scope, content="因此桌面壳后置", source_experience_ids=[], source_recognition_ids=[source.id]
    )
    service.revise(scope=scope, recognition_id=source.id, expected_revision=source.revision, content="网页优先，但桌面壳待评估")

    invalidated = next(item for item in service.list_candidates(scope=scope, include_inactive=True) if item.id == candidate.id)
    assert invalidated.id == candidate.id
    assert invalidated.state == "invalidated"
    with pytest.raises(RecognitionConflict, match="pending"):
        service.publish(scope=scope, candidate_id=candidate.id, expected_revision=invalidated.revision, reviewer="user-1")


def test_revoked_experience_stales_transitive_recognitions_and_fixed_question(service, scope):
    experience_id = service.stage_experience(scope=scope, content="网页链已经可用")
    first_candidate = service.propose(scope=scope, content="网页优先", source_experience_ids=[experience_id])
    first = service.publish(scope=scope, candidate_id=first_candidate.id, expected_revision=first_candidate.revision, reviewer="user-1")
    second_candidate = service.propose(scope=scope, content="桌面壳后置", source_experience_ids=[], source_recognition_ids=[first.id])
    second = service.publish(scope=scope, candidate_id=second_candidate.id, expected_revision=second_candidate.revision, reviewer="user-1")
    service.upsert_question(
        scope=scope, question_id="delivery", question="如何交付？", content="网页先行。",
        recognition_ids=[second.id], source_revisions={second.id: second.revision}, expected_revision=0,
    )
    experience = service.list_experiences(scope=scope)[0]

    service.revoke_experience(scope=scope, experience_id=experience.id, expected_revision=experience.revision)

    assert service.get_recognition(scope=scope, recognition_id=first.id).state == "stale"
    assert service.get_recognition(scope=scope, recognition_id=second.id).state == "stale"
    assert service.list_questions(scope=scope)[0].state == "stale"
    assert service.retrieval_entries(scope=scope) == ()


def test_split_part_can_keep_only_its_relevant_source_and_conditions(service, scope):
    first_experience = service.stage_experience(scope=scope, content="SQLite 适合单用户")
    second_experience = service.stage_experience(scope=scope, content="网页适合快速验证")
    candidate = service.propose(
        scope=scope, content="当前采用 SQLite 网页架构", source_experience_ids=[first_experience, second_experience]
    )
    parent = service.publish(scope=scope, candidate_id=candidate.id, expected_revision=candidate.revision, reviewer="user-1")

    children = service.split(
        scope=scope, recognition_id=parent.id, expected_revision=parent.revision,
        parts=[
            {"content": "SQLite 适合单用户", "source_experience_ids": [first_experience], "source_recognition_ids": [], "conditions": ["本地单用户"]},
            {"content": "网页适合快速验证", "source_experience_ids": [second_experience], "source_recognition_ids": [], "conditions": ["桌面版本尚未验证"]},
        ],
        new_ids=["recognition-sqlite", "recognition-web"],
    )

    assert children[0].source_experience_ids == (first_experience,)
    assert children[0].source_experience_revisions[first_experience] == 1
    assert "source_experience_revisions" in service.export_markdown(scope=scope, recognition_id=children[0].id)


def test_merge_keeps_parent_and_user_conditions_in_public_projection(service, scope):
    source = _published(service, scope)
    children = service.split(
        scope=scope, recognition_id=source.id, expected_revision=source.revision,
        parts=[
            {"content": "SQLite 适合本地", "source_experience_ids": list(source.source_experience_ids), "source_recognition_ids": [], "conditions": ["单用户"]},
            {"content": "网页适合验证", "source_experience_ids": list(source.source_experience_ids), "source_recognition_ids": [], "conditions": ["网页阶段"]},
        ], new_ids=["recognition-local", "recognition-web"],
    )

    merged = service.merge(
        scope=scope, recognition_ids=[item.id for item in children],
        expected_revisions={item.id: item.revision for item in children}, content="本地网页架构",
        conditions=["尚未验证桌面壳"],
    )

    assert merged.conditions == ("单用户", "网页阶段", "尚未验证桌面壳")
    projection = merged.retrieval_projection()
    assert projection["source_experience_ids"] == list(merged.source_experience_ids)
    assert projection["source_recognition_ids"] == []
    assert projection["conditions"] == ["单用户", "网页阶段", "尚未验证桌面壳"]
