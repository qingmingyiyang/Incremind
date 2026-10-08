from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from core.document_engine import (
    DocumentDraft,
    DocumentMigrationInventoryError,
    ObjectStoreDocumentRepository,
    plan_document_migration_dry_run,
    scan_document_migration_inventory,
)
from core.storage_provider import (
    JsonObjectStore,
    MigrationLedgerConflict,
    SQLiteMigrationLedger,
)


def _roots(tmp_path: Path) -> tuple[Path, Path]:
    return tmp_path / ".rebuild-data", tmp_path / "library"


def _store(tmp_path: Path) -> JsonObjectStore:
    rebuild_root, library_root = _roots(tmp_path)
    return JsonObjectStore(rebuild_root, legacy_root=library_root)


def _source_ref() -> dict[str, object]:
    return {
        "source_id": "source-document-inventory-001",
        "locator": "char:0-36",
        "quote": "Document inventory keeps no content.",
    }


def _healthy_fixture(tmp_path: Path) -> tuple[JsonObjectStore, str]:
    store = _store(tmp_path)
    repository = ObjectStoreDocumentRepository(store)
    document = repository.create(
        DocumentDraft(
            title="Private inventory title",
            document_type="project_doc",
            markdown="Document inventory keeps no content.",
            source_refs=(_source_ref(),),
            project_id="project-alpha",
        )
    )
    document_id = str(document["id"])
    repository.save_user_edit(
        document_id,
        markdown="User-edited content must not enter inventory output.",
        expected_revision=1,
    )
    return store, document_id


def _scan(tmp_path: Path):
    rebuild_root, _library_root = _roots(tmp_path)
    return scan_document_migration_inventory(rebuild_root, namespace_id="default")


def _root_for_short_object_fixture(tmp_path: Path, collection: str, object_id: str) -> Path:
    short_stem = "~h-" + hashlib.sha256(object_id.encode("utf-8")).hexdigest()
    for depth in range(121):
        root = tmp_path / ("d" * depth) / ".rebuild-data"
        legacy_meta = root / "objects" / "default" / collection / f"{object_id}.meta.json"
        short_meta = root / "objects" / "default" / collection / f"{short_stem}.meta.json"
        if len(str(legacy_meta)) > 259 and len(str(short_meta)) <= 259:
            return root
    raise AssertionError("could not construct Document short filename fixture")


def test_document_inventory_is_path_free_deterministic_and_ledger_idempotent(
    tmp_path: Path,
) -> None:
    _healthy_fixture(tmp_path)

    first = _scan(tmp_path)
    repeated = _scan(tmp_path)
    ledger = SQLiteMigrationLedger(tmp_path / "ledger.sqlite3")
    planned = plan_document_migration_dry_run(
        ledger=ledger,
        migration_id="json-to-sqlite-documents-v1",
        target_schema_version=2,
        inventory=first,
        rollback_pointer="snapshot:documents-v1",
    )

    assert first == repeated
    assert first.issues == ()
    assert first.inventory.object_count == 5
    assert [item.collection for item in first.inventory.collections] == [
        "document_markdown",
        "document_revisions",
        "documents",
    ]
    assert [item.object_count for item in first.inventory.collections] == [2, 2, 1]
    assert str(tmp_path) not in repr(first)
    assert "Private inventory title" not in repr(first)
    assert "User-edited content" not in repr(first)
    assert planned == plan_document_migration_dry_run(
        ledger=ledger,
        migration_id="json-to-sqlite-documents-v1",
        target_schema_version=2,
        inventory=repeated,
        rollback_pointer="snapshot:documents-v1",
    )


def test_document_inventory_recovers_logical_ids_from_short_object_names(tmp_path: Path) -> None:
    document_id = "document-" + "x" * 115
    revision_id = f"{document_id}~r1"
    rebuild_root = _root_for_short_object_fixture(tmp_path, "document_revisions", revision_id)
    store = JsonObjectStore(rebuild_root, legacy_root=tmp_path / "library")
    markdown = "short filename inventory must retain the document identity"
    content_hash = "sha256:" + hashlib.sha256(markdown.encode("utf-8")).hexdigest()
    store.write(
        "documents",
        document_id,
        {"id": document_id, "revision": 1, "content_hash": content_hash},
        expected_revision=0,
    )
    store.write(
        "document_revisions",
        revision_id,
        {
            "document_id": document_id,
            "revision": 1,
            "parent_revision": None,
            "new_content_hash": content_hash,
        },
        expected_revision=0,
    )
    store.write(
        "document_markdown",
        revision_id,
        {
            "document_id": document_id,
            "revision": 1,
            "markdown": markdown,
            "content_hash": content_hash,
        },
        expected_revision=0,
    )

    inventory = scan_document_migration_inventory(rebuild_root, namespace_id="default")

    assert inventory.issues == ()
    assert inventory.inventory.object_count == 3
    short_stem = "~h-" + hashlib.sha256(revision_id.encode("utf-8")).hexdigest()
    assert (rebuild_root / "objects" / "default" / "document_revisions" / f"{short_stem}.json").exists()


@pytest.mark.parametrize(
    ("fixture_name", "expected_code"),
    (
        ("missing-markdown", "markdown_missing"),
        ("missing-revision", "revision_missing"),
        ("orphan-revision", "orphan_revision"),
        ("hash-mismatch", "markdown_content_hash_mismatch"),
    ),
)
def test_document_inventory_reports_partial_and_orphan_revisions(
    tmp_path: Path,
    fixture_name: str,
    expected_code: str,
) -> None:
    case_root = tmp_path / fixture_name
    store, document_id = _healthy_fixture(case_root)
    if fixture_name == "missing-markdown":
        store.delete("document_markdown", f"{document_id}~r2")
    elif fixture_name == "missing-revision":
        store.delete("document_revisions", f"{document_id}~r1")
    elif fixture_name == "orphan-revision":
        store.write(
            "document_revisions",
            "document-missing~r1",
            {
                "id": "document-revision-document-missing-r1",
                "document_id": "document-missing",
                "revision": 1,
                "parent_revision": None,
                "new_content_hash": "sha256:" + "0" * 64,
            },
            expected_revision=0,
        )
    else:
        markdown = store.read("document_markdown", f"{document_id}~r2")
        assert markdown is not None
        markdown["markdown"] = "changed without matching content hash"
        store.write(
            "document_markdown",
            f"{document_id}~r2",
            markdown,
            expected_revision=1,
        )

    inventory = _scan(case_root)

    assert expected_code in {issue.code for issue in inventory.issues}
    assert all(not hasattr(issue, "markdown") for issue in inventory.issues)


def test_inconsistent_inventory_cannot_enter_ledger_dry_run(tmp_path: Path) -> None:
    store, document_id = _healthy_fixture(tmp_path)
    store.delete("document_markdown", f"{document_id}~r2")
    inventory = _scan(tmp_path)
    ledger = SQLiteMigrationLedger(tmp_path / "ledger.sqlite3")

    with pytest.raises(DocumentMigrationInventoryError, match="markdown_missing"):
        plan_document_migration_dry_run(
            ledger=ledger,
            migration_id="json-to-sqlite-documents-v1",
            target_schema_version=2,
            inventory=inventory,
            rollback_pointer="snapshot:documents-v1",
        )

    assert ledger.list_records() == ()


def test_ledger_rejects_changed_document_inventory_for_same_migration_id(
    tmp_path: Path,
) -> None:
    store, document_id = _healthy_fixture(tmp_path)
    ledger = SQLiteMigrationLedger(tmp_path / "ledger.sqlite3")
    first = _scan(tmp_path)
    plan_document_migration_dry_run(
        ledger=ledger,
        migration_id="json-to-sqlite-documents-v1",
        target_schema_version=2,
        inventory=first,
        rollback_pointer="snapshot:documents-v1",
    )
    document = store.read("documents", document_id)
    assert document is not None
    document["title"] = "Changed after dry-run"
    store.write("documents", document_id, document, expected_revision=2)
    changed = _scan(tmp_path)

    assert changed.issues == ()
    with pytest.raises(MigrationLedgerConflict, match="input fingerprint"):
        plan_document_migration_dry_run(
            ledger=ledger,
            migration_id="json-to-sqlite-documents-v1",
            target_schema_version=2,
            inventory=changed,
            rollback_pointer="snapshot:documents-v1",
        )
