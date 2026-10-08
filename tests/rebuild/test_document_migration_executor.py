from __future__ import annotations

from pathlib import Path

import pytest

from core.document_engine import (
    DocumentDraft,
    DocumentMigrationExecutionError,
    ObjectStoreDocumentRepository,
    SQLiteDocumentRepository,
    execute_document_fixture_migration,
    plan_document_migration_dry_run,
    scan_document_migration_inventory,
)
from core.storage_provider import (
    JsonObjectStore,
    SQLiteMigrationLedger,
    SQLiteStructuredRecordStore,
    SQLiteStructuredRecordUnitOfWork,
)


def _fixture(tmp_path: Path):
    rebuild_root = tmp_path / "legacy" / ".rebuild-data"
    library_root = tmp_path / "legacy" / "library"
    store = JsonObjectStore(rebuild_root, legacy_root=library_root)
    repository = ObjectStoreDocumentRepository(store)
    source_ref = {
        "source_id": "source-document-executor-001",
        "locator": "char:0-34",
        "quote": "Document executor fixture source.",
    }
    document = repository.create(
        DocumentDraft(
            title="Document executor fixture",
            document_type="project_doc",
            markdown="Document executor fixture source.",
            source_refs=(source_ref,),
            project_id="project-alpha",
        )
    )
    document_id = str(document["id"])
    repository.save_user_edit(
        document_id,
        markdown="User-confirmed migrated revision.",
        expected_revision=1,
    )
    inventory = scan_document_migration_inventory(
        rebuild_root,
        namespace_id="default",
    )
    ledger = SQLiteMigrationLedger(tmp_path / "ledger.sqlite3")
    dry_run = plan_document_migration_dry_run(
        ledger=ledger,
        migration_id="fixture-documents-v1",
        target_schema_version=2,
        inventory=inventory,
        rollback_pointer="snapshot:fixture-documents-v1",
    )
    return rebuild_root, store, repository, document_id, inventory, ledger, dry_run


def test_executor_copies_all_document_objects_and_reopens_history(tmp_path: Path) -> None:
    rebuild_root, _store, source, document_id, inventory, ledger, dry_run = _fixture(tmp_path)
    target = tmp_path / "target" / "documents.sqlite3"

    result = execute_document_fixture_migration(
        object_store_root=rebuild_root,
        target_database_path=target,
        ledger=ledger,
        dry_run=dry_run,
    )

    migrated = SQLiteDocumentRepository(SQLiteStructuredRecordStore(target))
    assert result.document_count == 1
    assert result.object_count == 5
    assert result.input_fingerprint == inventory.inventory.fingerprint
    assert migrated.read(document_id) == source.read(document_id)
    assert migrated.revisions(document_id) == source.revisions(document_id)
    assert migrated.markdown(document_id, revision=1) == source.markdown(document_id, revision=1)
    assert migrated.markdown(document_id, revision=2) == source.markdown(document_id, revision=2)
    assert scan_document_migration_inventory(rebuild_root, namespace_id="default") == inventory


def test_executor_rejects_input_drift_before_creating_target(tmp_path: Path) -> None:
    rebuild_root, store, _source, document_id, _inventory, ledger, dry_run = _fixture(tmp_path)
    document = store.read("documents", document_id)
    assert document is not None
    document["title"] = "changed after dry-run"
    store.write("documents", document_id, document, expected_revision=2)
    target = tmp_path / "target" / "documents.sqlite3"

    with pytest.raises(DocumentMigrationExecutionError, match="fingerprint changed"):
        execute_document_fixture_migration(
            object_store_root=rebuild_root,
            target_database_path=target,
            ledger=ledger,
            dry_run=dry_run,
        )

    assert target.exists() is False


def test_executor_rejects_existing_target_without_modifying_it(tmp_path: Path) -> None:
    rebuild_root, _store, _source, _document_id, _inventory, ledger, dry_run = _fixture(tmp_path)
    target = tmp_path / "target" / "documents.sqlite3"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"keep-existing-target")

    with pytest.raises(DocumentMigrationExecutionError, match="already exist"):
        execute_document_fixture_migration(
            object_store_root=rebuild_root,
            target_database_path=target,
            ledger=ledger,
            dry_run=dry_run,
        )

    assert target.read_bytes() == b"keep-existing-target"


def test_executor_rejects_target_inside_source_root(tmp_path: Path) -> None:
    rebuild_root, _store, _source, _document_id, _inventory, ledger, dry_run = _fixture(tmp_path)
    target = rebuild_root / "nested-target.sqlite3"

    with pytest.raises(DocumentMigrationExecutionError, match="inside source root"):
        execute_document_fixture_migration(
            object_store_root=rebuild_root,
            target_database_path=target,
            ledger=ledger,
            dry_run=dry_run,
        )

    assert target.exists() is False


def test_executor_rolls_back_and_removes_new_target_on_mid_copy_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rebuild_root, _store, _source, _document_id, inventory, ledger, dry_run = _fixture(tmp_path)
    target = tmp_path / "target" / "documents.sqlite3"
    original_put = SQLiteStructuredRecordUnitOfWork.put
    put_count = 0

    def _fail_second_put(self, *args, **kwargs):
        nonlocal put_count
        put_count += 1
        if put_count == 2:
            raise RuntimeError("injected second record failure")
        return original_put(self, *args, **kwargs)

    monkeypatch.setattr(SQLiteStructuredRecordUnitOfWork, "put", _fail_second_put)

    with pytest.raises(DocumentMigrationExecutionError, match="injected second record failure"):
        execute_document_fixture_migration(
            object_store_root=rebuild_root,
            target_database_path=target,
            ledger=ledger,
            dry_run=dry_run,
        )

    assert target.exists() is False
    assert Path(f"{target}-wal").exists() is False
    assert Path(f"{target}-shm").exists() is False
    assert scan_document_migration_inventory(rebuild_root, namespace_id="default") == inventory
