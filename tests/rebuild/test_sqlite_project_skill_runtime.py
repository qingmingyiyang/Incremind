from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from core.project_skill_core import (
    ProjectSkillExpectedRevisionError,
    ProjectSkillRepositoryError,
    ProjectSkillUpdate,
    SQLiteProjectSkillRepository,
)
from core.storage_provider import SQLiteStructuredRecordStore


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = (
    ROOT
    / "core-contracts"
    / "rebuild"
    / "fixtures"
    / "project_skill"
    / "valid-active-skill.json"
)


def _records(tmp_path: Path) -> SQLiteStructuredRecordStore:
    return SQLiteStructuredRecordStore(tmp_path / "project-skills.sqlite3")


def _repository(tmp_path: Path) -> SQLiteProjectSkillRepository:
    return SQLiteProjectSkillRepository(_records(tmp_path))


def _structured() -> dict[str, object]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _update(
    structured: dict[str, object],
    *,
    markdown: str = "# Alpha 项目 Skill\n\n沿用旧结构。",
    expected_revision: int = 0,
    reason: str = "user confirmed project skill",
) -> ProjectSkillUpdate:
    return ProjectSkillUpdate(
        project_id=str(structured["project_id"]),
        markdown=markdown,
        structured=structured,
        expected_revision=expected_revision,
        reason=reason,
    )


def _seed(
    records: SQLiteStructuredRecordStore,
    collection: str,
    object_id: str,
    payload: dict[str, object],
) -> None:
    with records.begin() as uow:
        uow.put(collection, object_id, payload, expected_revision=0)
        uow.commit()


def test_sqlite_project_skill_saves_and_reopens_five_synchronized_objects(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)

    saved = repository.save(_update(_structured()))
    project_id = str(saved["project_id"])
    skill_id = str(saved["id"])
    reloaded = _repository(tmp_path)
    records = _records(tmp_path)

    assert reloaded.load(project_id) == saved
    assert reloaded.markdown(project_id) == "# Alpha 项目 Skill\n\n沿用旧结构。"
    assert reloaded.structured(project_id) == saved
    assert [item["revision"] for item in reloaded.revisions(project_id)] == [1]
    assert records.read("project_skills", skill_id).revision == 1
    assert records.read("project_skill_markdown", f"{skill_id}~r1").revision == 1
    assert records.read("project_skill_json", f"{skill_id}~r1").revision == 1
    assert records.read("project_skill_revisions", f"{skill_id}~r1").revision == 1
    assert records.read("project_skill_index", project_id).revision == 1


def test_sqlite_project_skill_reads_current_skills_by_identity_and_project(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    saved = repository.save(_update(_structured()))

    assert repository.get(str(saved["id"])) == saved
    assert repository.get("missing-skill") is None
    assert repository.list_by_project(str(saved["project_id"])) == (saved,)


def test_sqlite_project_skill_keeps_history_and_rejects_stale_revision(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    first = repository.save(_update(_structured()))
    updated = copy.deepcopy(first)
    updated["style_preferences"]["voice"] = "直接、具体、保留来源"
    second = repository.save(
        _update(
            updated,
            markdown="# Alpha 项目 Skill\n\n沿用旧结构，并保留来源。",
            expected_revision=1,
            reason="update style preference",
        )
    )
    project_id = str(second["project_id"])

    assert second["revision"] == 2
    assert repository.markdown(project_id, revision=1) == "# Alpha 项目 Skill\n\n沿用旧结构。"
    assert repository.markdown(project_id, revision=2) == "# Alpha 项目 Skill\n\n沿用旧结构，并保留来源。"
    assert repository.structured(project_id, revision=1) == first
    assert [item["revision"] for item in repository.revisions(project_id)] == [1, 2]
    with pytest.raises(ProjectSkillExpectedRevisionError):
        repository.save(_update(updated, expected_revision=1))


def test_sqlite_project_skill_direct_rollback_is_one_atomic_new_revision(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    first = repository.save(_update(_structured()))
    edited = copy.deepcopy(first)
    edited["style_preferences"]["voice"] = "SQLite用户编辑"
    repository.save(ProjectSkillUpdate(
        project_id=str(first["project_id"]),
        markdown="# Alpha 项目 Skill\n\nSQLite用户编辑。",
        structured=edited,
        expected_revision=1,
        reason="用户直接修改SQLite项目规则",
        transition_kind="user_edit",
        actor="user",
        confirmation_kind="direct_user_save",
    ))

    restored = repository.rollback(
        str(first["project_id"]),
        target_revision=1,
        expected_revision=2,
        reason="用户恢复SQLite第一版",
    )

    assert restored["revision"] == 3
    assert restored["style_preferences"] == first["style_preferences"]
    assert [item["transition_kind"] for item in repository.revisions(str(first["project_id"]))] == [
        "revision_saved", "user_edit", "user_rollback",
    ]
    assert _records(tmp_path).read("project_skills", str(first["id"])).revision == 3
    with pytest.raises(ProjectSkillExpectedRevisionError):
        repository.rollback(str(first["project_id"]), target_revision=1, expected_revision=2, reason="重复请求")
    assert repository.load(str(first["project_id"]))["revision"] == 3


def test_sqlite_project_skill_preserves_active_publish_gate(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    stale = _structured()
    stale["required_context"][0]["stale"] = True

    with pytest.raises(ProjectSkillRepositoryError, match="stale required context"):
        repository.save(_update(stale))

    assert _records(tmp_path).list("project_skills") == ()


def test_sqlite_project_skill_rolls_back_current_when_markdown_conflicts(
    tmp_path: Path,
) -> None:
    records = _records(tmp_path)
    structured = _structured()
    skill_id = str(structured["id"])
    project_id = str(structured["project_id"])
    _seed(
        records,
        "project_skill_markdown",
        f"{skill_id}~r1",
        {"id": f"{skill_id}~r1", "seeded_conflict": True},
    )
    repository = SQLiteProjectSkillRepository(records)

    with pytest.raises(ProjectSkillRepositoryError, match="persistence conflict"):
        repository.save(_update(structured))

    assert records.read("project_skills", skill_id) is None
    assert records.read("project_skill_index", project_id) is None
    assert records.read("project_skill_markdown", f"{skill_id}~r1").payload["seeded_conflict"] is True


def test_sqlite_project_skill_rolls_back_first_four_writes_when_index_conflicts(
    tmp_path: Path,
) -> None:
    records = _records(tmp_path)
    structured = _structured()
    skill_id = str(structured["id"])
    project_id = str(structured["project_id"])
    _seed(
        records,
        "project_skill_index",
        project_id,
        {"project_id": project_id, "skill_id": "skill-missing", "seeded_conflict": True},
    )
    repository = SQLiteProjectSkillRepository(records)

    with pytest.raises(ProjectSkillRepositoryError, match="persistence conflict"):
        repository.save(_update(structured))

    assert records.read("project_skills", skill_id) is None
    assert records.read("project_skill_markdown", f"{skill_id}~r1") is None
    assert records.read("project_skill_json", f"{skill_id}~r1") is None
    assert records.read("project_skill_revisions", f"{skill_id}~r1") is None
    assert records.read("project_skill_index", project_id).payload["seeded_conflict"] is True
