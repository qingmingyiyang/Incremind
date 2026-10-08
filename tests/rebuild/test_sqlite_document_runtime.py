from __future__ import annotations

from pathlib import Path

import pytest

from core.document_engine import (
    DocumentDraft,
    DocumentExpectedRevisionError,
    DocumentRepositoryError,
    SQLiteDocumentRepository,
)
from core.storage_provider import SQLiteStructuredRecordStore


def _records(tmp_path: Path) -> SQLiteStructuredRecordStore:
    return SQLiteStructuredRecordStore(tmp_path / "document-runtime.sqlite3")


def _repository(tmp_path: Path) -> SQLiteDocumentRepository:
    return SQLiteDocumentRepository(_records(tmp_path))


def _source_ref() -> dict[str, object]:
    return {
        "source_id": "source-sqlite-document-001",
        "locator": "char:0-40",
        "quote": "SQLite Document revision is atomic.",
    }


def _draft() -> DocumentDraft:
    return DocumentDraft(
        title="SQLite document",
        document_type="project_doc",
        markdown="SQLite Document revision is atomic.",
        source_refs=(_source_ref(),),
        project_id="project-alpha",
    )


def _seed(
    records: SQLiteStructuredRecordStore,
    collection: str,
    object_id: str,
) -> None:
    with records.begin() as uow:
        uow.put(
            collection,
            object_id,
            {"id": object_id, "seeded_conflict": True},
            expected_revision=0,
        )
        uow.commit()


def test_sqlite_document_repository_persists_and_reopens_complete_revision(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)

    document = repository.create(_draft())
    document_id = str(document["id"])
    reloaded = _repository(tmp_path)

    assert reloaded.read(document_id) == document
    assert reloaded.markdown(document_id) == "SQLite Document revision is atomic."
    assert reloaded.revision(document_id, 1)["source_snapshot"] == document["source_snapshot"]
    assert [item["revision"] for item in reloaded.revisions(document_id)] == [1]
    assert _records(tmp_path).read("documents", document_id).revision == 1
    assert _records(tmp_path).read("document_revisions", f"{document_id}~r1").revision == 1
    assert _records(tmp_path).read("document_markdown", f"{document_id}~r1").revision == 1


def test_sqlite_generated_document_replays_through_same_transactional_authority(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)

    first = repository.create_or_replay_generated(_draft())
    replayed = _repository(tmp_path).create_or_replay_generated(_draft())

    assert replayed == first
    assert _repository(tmp_path).revisions(str(first["id"])) == (
        _repository(tmp_path).revision(str(first["id"]), 1),
    )


def test_sqlite_document_keeps_user_edit_history_and_ai_conflict_semantics(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    document = repository.create(_draft())
    document_id = str(document["id"])
    edited = repository.save_user_edit(
        document_id,
        markdown="用户确认：保留这段判断。",
        expected_revision=1,
    )
    user_block = edited["blocks"][0]

    conflicted = repository.apply_ai_patch(
        document_id,
        expected_revision=2,
        blocks=[
            {
                "id": user_block["id"],
                "block_type": user_block["block_type"],
                "origin": "ai",
                "content": "AI 尝试覆盖用户判断。",
                "source_refs": [_source_ref()],
                "edited_by_user": False,
                "lock_policy": "source_required",
            }
        ],
    )

    assert conflicted["revision"] == 3
    assert conflicted["status"] == "conflicted"
    assert conflicted["blocks"][0]["content"] == "用户确认：保留这段判断。"
    assert repository.markdown(document_id, revision=1) == "SQLite Document revision is atomic."
    assert repository.markdown(document_id) == "用户确认：保留这段判断。"
    assert [item["revision"] for item in repository.revisions(document_id)] == [1, 2, 3]

    with pytest.raises(DocumentExpectedRevisionError):
        repository.save_user_edit(
            document_id,
            markdown="stale edit",
            expected_revision=1,
        )


def test_sqlite_document_create_rolls_back_root_when_revision_conflicts(
    tmp_path: Path,
) -> None:
    probe = SQLiteDocumentRepository(_records(tmp_path / "probe"))
    document_id = str(probe.create(_draft())["id"])
    records = _records(tmp_path)
    _seed(records, "document_revisions", f"{document_id}~r1")
    repository = SQLiteDocumentRepository(records)

    with pytest.raises(DocumentRepositoryError, match="persistence conflict"):
        repository.create(_draft())

    assert records.read("documents", document_id) is None
    assert records.read("document_revisions", f"{document_id}~r1").payload["seeded_conflict"] is True
    assert records.read("document_markdown", f"{document_id}~r1") is None


def test_sqlite_document_update_rolls_back_root_and_revision_when_markdown_conflicts(
    tmp_path: Path,
) -> None:
    records = _records(tmp_path)
    repository = SQLiteDocumentRepository(records)
    document = repository.create(_draft())
    document_id = str(document["id"])
    _seed(records, "document_markdown", f"{document_id}~r2")

    with pytest.raises(DocumentRepositoryError, match="persistence conflict"):
        repository.save_user_edit(
            document_id,
            markdown="This revision must roll back.",
            expected_revision=1,
        )

    current = repository.read(document_id)
    assert current is not None
    assert current["revision"] == 1
    assert repository.revision(document_id, 2) is None
    assert repository.markdown(document_id) == "SQLite Document revision is atomic."
    assert records.read("document_markdown", f"{document_id}~r2").payload["seeded_conflict"] is True


def test_sqlite_document_archive_restore_is_revisioned_and_restart_durable(
    tmp_path: Path,
) -> None:
    records = _records(tmp_path)
    repository = SQLiteDocumentRepository(records)
    created = repository.create(_draft())
    document_id = str(created["id"])

    archived = repository.archive(document_id, expected_revision=1)

    assert archived["revision"] == 2
    assert archived["status"] == "archived"
    assert archived["archived_from_status"] == "draft"
    assert repository.list() == ()
    assert [item["id"] for item in repository.list(include_archived=True)] == [document_id]
    assert repository.markdown(document_id, revision=1) == "SQLite Document revision is atomic."
    assert repository.markdown(document_id, revision=2) == "SQLite Document revision is atomic."
    assert repository.revision(document_id, 2)["operation"] == "archive"

    reopened = SQLiteDocumentRepository(_records(tmp_path))
    assert reopened.read(document_id)["status"] == "archived"
    restored = reopened.restore(document_id, expected_revision=2)

    assert restored["revision"] == 3
    assert restored["status"] == "draft"
    assert "archived_from_status" not in restored
    assert reopened.revision(document_id, 3)["operation"] == "restore"
    assert reopened.markdown(document_id, revision=3) == "SQLite Document revision is atomic."
    assert [item["revision"] for item in reopened.revisions(document_id)] == [1, 2, 3]
    assert _records(tmp_path).read("documents", document_id).revision == 3


def test_sqlite_document_lifecycle_rejects_stale_duplicate_and_archived_edits(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    document_id = str(repository.create(_draft())["id"])
    repository.archive(document_id, expected_revision=1)

    with pytest.raises(DocumentExpectedRevisionError):
        repository.restore(document_id, expected_revision=1)
    with pytest.raises(DocumentRepositoryError, match="already archived"):
        repository.archive(document_id, expected_revision=2)
    with pytest.raises(DocumentRepositoryError, match="restored before editing"):
        repository.save_user_edit(document_id, markdown="unsafe", expected_revision=2)

    repository.restore(document_id, expected_revision=2)
    with pytest.raises(DocumentRepositoryError, match="not archived"):
        repository.restore(document_id, expected_revision=3)

    assert [item["revision"] for item in repository.revisions(document_id)] == [1, 2, 3]
