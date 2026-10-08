from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from core.document_engine import DocumentDraft, ObjectStoreDocumentRepository
from core.project_skill_core import ObjectStoreProjectSkillRepository, ProjectSkillUpdate
from core.storage_provider import JsonObjectStore


ROOT = Path(__file__).resolve().parents[2]
PROJECT_SKILL_FIXTURE = (
    ROOT
    / "core-contracts"
    / "rebuild"
    / "fixtures"
    / "project_skill"
    / "valid-active-skill.json"
)


class InjectedWriteFailure(RuntimeError):
    pass


class FailOnWriteObjectStore:
    """Delegates reads while failing one deterministic write call."""

    def __init__(self, delegate: JsonObjectStore, *, fail_on_write: int) -> None:
        self.delegate = delegate
        self.fail_on_write = fail_on_write
        self.write_count = 0

    def read(self, collection: str, object_id: str) -> Mapping[str, object] | None:
        return self.delegate.read(collection, object_id)

    def list(self, collection: str) -> Sequence[Mapping[str, object]]:
        return self.delegate.list(collection)

    def delete(self, collection: str, object_id: str) -> bool:
        return self.delegate.delete(collection, object_id)

    def write(
        self,
        collection: str,
        object_id: str,
        payload: Mapping[str, object],
        expected_revision: int | None,
    ) -> int:
        self.write_count += 1
        if self.write_count == self.fail_on_write:
            raise InjectedWriteFailure(f"injected failure at {collection}/{object_id}")
        return self.delegate.write(collection, object_id, payload, expected_revision)


def _store(tmp_path: Path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def _draft(markdown: str = "Initial source-backed draft.") -> DocumentDraft:
    return DocumentDraft(
        title="Atomic document",
        document_type="project_doc",
        markdown=markdown,
        source_refs=(
            {
                "source_id": "source-atomic-001",
                "locator": "char:0-28",
                "quote": "Initial source-backed draft.",
            },
        ),
        project_id="project-alpha",
    )


def _project_skill_update() -> ProjectSkillUpdate:
    structured = json.loads(PROJECT_SKILL_FIXTURE.read_text(encoding="utf-8"))
    return ProjectSkillUpdate(
        project_id=str(structured["project_id"]),
        markdown="# Alpha 项目 Skill\n\n沿用旧结构。",
        structured=structured,
        expected_revision=0,
        reason="user confirmed project skill",
    )


@pytest.mark.parametrize(
    ("fail_on_write", "revision_persisted"),
    ((2, False), (3, True)),
)
def test_document_create_exposes_incomplete_revision_when_child_write_fails(
    tmp_path: Path,
    fail_on_write: int,
    revision_persisted: bool,
) -> None:
    store = _store(tmp_path)
    repository = ObjectStoreDocumentRepository(
        FailOnWriteObjectStore(store, fail_on_write=fail_on_write)
    )

    with pytest.raises(InjectedWriteFailure):
        repository.create(_draft())

    documents = store.list("documents")
    assert len(documents) == 1
    document_id = str(documents[0]["id"])
    assert documents[0]["revision"] == 1
    assert (store.read("document_revisions", f"{document_id}~r1") is not None) is revision_persisted
    assert store.read("document_markdown", f"{document_id}~r1") is None


@pytest.mark.parametrize(
    ("fail_on_write", "revision_persisted"),
    ((2, False), (3, True)),
)
def test_document_update_can_advance_root_without_complete_revision(
    tmp_path: Path,
    fail_on_write: int,
    revision_persisted: bool,
) -> None:
    store = _store(tmp_path)
    healthy = ObjectStoreDocumentRepository(store)
    created = healthy.create(_draft())
    document_id = str(created["id"])
    failing = ObjectStoreDocumentRepository(
        FailOnWriteObjectStore(store, fail_on_write=fail_on_write)
    )

    with pytest.raises(InjectedWriteFailure):
        failing.save_user_edit(
            document_id,
            markdown="User edit that reaches only the root object.",
            expected_revision=1,
        )

    current = store.read("documents", document_id)
    assert current is not None
    assert current["revision"] == 2
    assert (store.read("document_revisions", f"{document_id}~r2") is not None) is revision_persisted
    assert store.read("document_markdown", f"{document_id}~r2") is None
    assert healthy.markdown(document_id, revision=1) == "Initial source-backed draft."
    assert healthy.markdown(document_id) is None


@pytest.mark.parametrize(
    ("fail_on_write", "markdown_persisted", "json_persisted", "revision_persisted"),
    (
        (2, False, False, False),
        (3, True, False, False),
        (4, True, True, False),
        (5, True, True, True),
    ),
)
def test_project_skill_save_can_orphan_partial_revision_before_index_commit(
    tmp_path: Path,
    fail_on_write: int,
    markdown_persisted: bool,
    json_persisted: bool,
    revision_persisted: bool,
) -> None:
    store = _store(tmp_path)
    repository = ObjectStoreProjectSkillRepository(
        FailOnWriteObjectStore(store, fail_on_write=fail_on_write)
    )

    with pytest.raises(InjectedWriteFailure):
        repository.save(_project_skill_update())

    skills = store.list("project_skills")
    assert len(skills) == 1
    skill_id = str(skills[0]["id"])
    project_id = str(skills[0]["project_id"])
    assert skills[0]["revision"] == 1
    assert (store.read("project_skill_markdown", f"{skill_id}~r1") is not None) is markdown_persisted
    assert (store.read("project_skill_json", f"{skill_id}~r1") is not None) is json_persisted
    assert (store.read("project_skill_revisions", f"{skill_id}~r1") is not None) is revision_persisted
    assert store.read("project_skill_index", project_id) is None
    assert ObjectStoreProjectSkillRepository(store).load(project_id) is None
