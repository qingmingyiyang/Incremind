"""Four-layer memory domain ports."""

from .ports import MemoryReaderPort, MemoryWriterPort
from .manual_publication_contract import (
    ManualPublicationContractError,
    STAGING_PUBLICATION_CONTEXT_COLLECTION,
    build_context as build_manual_publication_context,
    build_record as build_manual_publication_record,
    build_replacement_record as build_manual_publication_replacement_record,
    context_id as manual_publication_context_id,
    validate_context as validate_manual_publication_context,
)
from .publication_fixture_migration import (
    MemoryPublicationFixtureInventory,
    MemoryPublicationFixtureMigrationError,
    MemoryPublicationFixtureMigrationIssue,
    MemoryPublicationFixtureMigrationResult,
    execute_memory_publication_fixture_migration,
    plan_memory_publication_fixture_migration_dry_run,
    scan_memory_publication_fixture_inventory,
)
from .publication_fixture_compatibility import (
    MemoryPublicationFixtureCompatibilityError,
    MemoryPublicationFixtureCompatibilityReport,
    compare_memory_publication_fixture,
)
from .runtime import (
    InMemoryMemoryStore,
    MemoryCandidateRepositoryError,
    ObjectStoreMemoryCandidateRepository,
    ObjectStoreMemoryStore,
    SQLiteMemoryReader,
    memory_candidate_id,
)
from .shared_trust_audit_activation import (
    SharedTrustAuditActivation,
    SharedTrustAuditActivationError,
    require_shared_trust_audit_activation,
    shared_trust_audit_activation_id,
    shared_trust_audit_activation_payload,
)

__all__ = [
    "InMemoryMemoryStore",
    "MemoryCandidateRepositoryError",
    "MemoryReaderPort",
    "MemoryWriterPort",
    "ManualPublicationContractError",
    "MemoryPublicationFixtureInventory",
    "MemoryPublicationFixtureCompatibilityError",
    "MemoryPublicationFixtureCompatibilityReport",
    "MemoryPublicationFixtureMigrationError",
    "MemoryPublicationFixtureMigrationIssue",
    "MemoryPublicationFixtureMigrationResult",
    "ObjectStoreMemoryCandidateRepository",
    "ObjectStoreMemoryStore",
    "SQLiteMemoryReader",
    "memory_candidate_id",
    "STAGING_PUBLICATION_CONTEXT_COLLECTION",
    "build_manual_publication_context",
    "build_manual_publication_record",
    "build_manual_publication_replacement_record",
    "compare_memory_publication_fixture",
    "execute_memory_publication_fixture_migration",
    "manual_publication_context_id",
    "plan_memory_publication_fixture_migration_dry_run",
    "scan_memory_publication_fixture_inventory",
    "validate_manual_publication_context",
    "SharedTrustAuditActivation",
    "SharedTrustAuditActivationError",
    "require_shared_trust_audit_activation",
    "shared_trust_audit_activation_id",
    "shared_trust_audit_activation_payload",
]
