"""Editable and versioned document ports and runtime repository."""

from .ports import DocumentDraft, DocumentRepositoryPort
from .migration_inventory import (
    DocumentInventoryIssue,
    DocumentMigrationInventory,
    DocumentMigrationInventoryError,
    plan_document_migration_dry_run,
    scan_document_migration_inventory,
)
from .migration_executor import (
    DocumentMigrationExecutionError,
    DocumentMigrationExecutionResult,
    execute_document_fixture_migration,
)
from .runtime import (
    DocumentExpectedRevisionError,
    DocumentRepositoryError,
    ObjectStoreDocumentRepository,
)
from .sqlite_runtime import SQLiteDocumentRepository
from .text import document_block_text

__all__ = [
    "DocumentDraft",
    "DocumentExpectedRevisionError",
    "DocumentInventoryIssue",
    "DocumentMigrationExecutionError",
    "DocumentMigrationExecutionResult",
    "DocumentMigrationInventory",
    "DocumentMigrationInventoryError",
    "DocumentRepositoryError",
    "DocumentRepositoryPort",
    "ObjectStoreDocumentRepository",
    "SQLiteDocumentRepository",
    "document_block_text",
    "execute_document_fixture_migration",
    "plan_document_migration_dry_run",
    "scan_document_migration_inventory",
]
