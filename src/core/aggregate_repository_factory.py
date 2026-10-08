from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .document_engine import ObjectStoreDocumentRepository, SQLiteDocumentRepository
from .memory_core import (
    SharedTrustAuditActivationError,
    require_shared_trust_audit_activation,
)
from .project_skill_core import (
    ObjectStoreProjectSkillRepository,
    SQLiteProjectSkillRepository,
)
from .storage_provider import (
    JsonObjectStore,
    SQLiteAggregateAuthorityStore,
    SQLiteStructuredRecordStore,
)


TARGET_IDENTITY = "sqlite:structured-records-v1"
JSON_DOCUMENT_AUTHORITY_IDENTITY = "json:object-store-v1"
JSON_PROJECT_SKILL_AUTHORITY_IDENTITY = "json:object-store-v1"
JSON_MEMORY_PUBLICATION_AUTHORITY_IDENTITY = "json:object-store-v1"
JSON_SOURCE_ASSET_AUTHORITY_IDENTITY = "json:object-store-v1"
AUTHORITY_DATABASE_NAME = "aggregate-authority.sqlite3"
STRUCTURED_DATABASE_NAME = "structured-records.sqlite3"
_TARGET_MARKER_COLLECTION = "aggregate_authority_targets"
_MEMORY_PUBLICATION_MEMBERS = (
    "memory_atoms",
    "memory_publications",
    "memory_scenarios",
    "memory_series_memory",
    "memory_transitions",
    "project_skills",
)
SOURCE_ASSET_AUTHORITY_MEMBERS = (
    "asset_blobs",
    "original_assets",
    "source_asset_links",
)


class AggregateRepositoryFactoryError(RuntimeError):
    """Raised when recorded authority cannot be resolved without data ambiguity."""


@dataclass(frozen=True, slots=True)
class DocumentRepositoryResolution:
    repository: ObjectStoreDocumentRepository | SQLiteDocumentRepository
    authority_identity: str


@dataclass(frozen=True, slots=True)
class ProjectSkillRepositoryResolution:
    repository: ObjectStoreProjectSkillRepository | SQLiteProjectSkillRepository
    authority_identity: str


@dataclass(frozen=True, slots=True)
class MemoryPublicationAuthorityResolution:
    """One authority for the generic Memory projection and shared audit.

    ``records`` is intentionally ``None`` for JSON.  A later reader/writer
    cutover must make an explicit choice from this result; it must not infer a
    mixed source from an individual aggregate marker.
    """

    records: SQLiteStructuredRecordStore | None
    authority_identity: str


@dataclass(frozen=True, slots=True)
class SourceAssetAuthorityResolution:
    """Resolve the Source/Asset/Blob compound without mixed authority."""

    records: SQLiteStructuredRecordStore | None
    authority_identity: str


@dataclass(frozen=True, slots=True)
class AggregateRepositoryFactory:
    runtime_root: Path
    namespace_id: str
    json_store: JsonObjectStore
    authority_store_factory: Callable[[Path], SQLiteAggregateAuthorityStore] = field(default=SQLiteAggregateAuthorityStore, kw_only=True, repr=False, compare=False)
    record_store_factory: Callable[[Path], SQLiteStructuredRecordStore] = field(default=SQLiteStructuredRecordStore, kw_only=True, repr=False, compare=False)

    def document_repository(
        self,
    ) -> ObjectStoreDocumentRepository | SQLiteDocumentRepository:
        return self.document_repository_resolution().repository

    def document_repository_resolution(self) -> DocumentRepositoryResolution:
        authority = self._authority("documents")
        if authority is None or authority.state in {"json_active", "sqlite_staged"}:
            return DocumentRepositoryResolution(
                repository=ObjectStoreDocumentRepository(
                    self.json_store,
                    namespace_id=self.namespace_id,
                ),
                authority_identity=JSON_DOCUMENT_AUTHORITY_IDENTITY,
            )
        records = self._active_records("documents", authority)
        return DocumentRepositoryResolution(
            repository=SQLiteDocumentRepository(records, namespace_id=self.namespace_id),
            authority_identity=TARGET_IDENTITY,
        )

    def project_skill_repository(
        self,
    ) -> ObjectStoreProjectSkillRepository | SQLiteProjectSkillRepository:
        return self.project_skill_repository_resolution().repository

    def project_skill_repository_resolution(self) -> ProjectSkillRepositoryResolution:
        authority = self._authority("project_skills")
        if authority is None or authority.state in {"json_active", "sqlite_staged"}:
            return ProjectSkillRepositoryResolution(
                repository=ObjectStoreProjectSkillRepository(
                    self.json_store,
                    namespace_id=self.namespace_id,
                ),
                authority_identity=JSON_PROJECT_SKILL_AUTHORITY_IDENTITY,
            )
        records = self._active_records("project_skills", authority)
        return ProjectSkillRepositoryResolution(
            repository=SQLiteProjectSkillRepository(records, namespace_id=self.namespace_id),
            authority_identity=TARGET_IDENTITY,
        )

    def memory_publication_authority_resolution(self) -> MemoryPublicationAuthorityResolution:
        """Resolve generic Memory only when its Trust Audit is compound-safe.

        Generic Atom/Scenario/Series projections and their publication audit
        cannot independently leave JSON.  Project Skill participates because
        it shares the audit collections.  Until every member is active with
        the same evidence and a matching attestation, callers receive JSON or
        a fail-closed error, never a partial SQLite selection.
        """

        authorities = tuple(
            (member, self._authority(member))
            for member in _MEMORY_PUBLICATION_MEMBERS
        )
        active = tuple(
            (member, authority)
            for member, authority in authorities
            if authority is not None and authority.state == "sqlite_active"
        )
        if not active:
            return MemoryPublicationAuthorityResolution(
                records=None,
                authority_identity=JSON_MEMORY_PUBLICATION_AUTHORITY_IDENTITY,
            )
        if len(active) != len(_MEMORY_PUBLICATION_MEMBERS):
            raise AggregateRepositoryFactoryError(
                "Memory Publication authority is partially SQLite active"
            )

        evidence = active[0][1].evidence
        if evidence is None:  # pragma: no cover - _active_records defends this too
            raise AggregateRepositoryFactoryError(
                "Memory Publication active authority lacks evidence"
            )
        if any(authority.evidence != evidence for _member, authority in active):
            raise AggregateRepositoryFactoryError(
                "Memory Publication authority evidence does not match across members"
            )

        records: SQLiteStructuredRecordStore | None = None
        for member, authority in active:
            candidate = self._active_records(member, authority)
            if records is None:
                records = candidate
        if records is None:  # pragma: no cover - active length is checked above
            raise AggregateRepositoryFactoryError("Memory Publication active target is missing")
        try:
            activation = require_shared_trust_audit_activation(
                records,
                namespace_id=self.namespace_id,
                target_identity=TARGET_IDENTITY,
            )
        except SharedTrustAuditActivationError as error:
            raise AggregateRepositoryFactoryError(
                f"Memory Publication compound activation is invalid: {error}"
            ) from error
        if (
            activation.source_fingerprint != evidence.source_fingerprint
            or activation.target_fingerprint != evidence.target_fingerprint
            or any(
                activation.member_migrations[member] != evidence.migration_id
                for member in _MEMORY_PUBLICATION_MEMBERS
            )
        ):
            raise AggregateRepositoryFactoryError(
                "Memory Publication compound activation evidence does not match authority"
            )
        return MemoryPublicationAuthorityResolution(
            records=records,
            authority_identity=TARGET_IDENTITY,
        )

    def source_asset_authority_resolution(self) -> SourceAssetAuthorityResolution:
        """Select SQLite only when every Source Asset member proves cutover.

        Staged records remain migration evidence while JSON stays active.
        Once any member becomes active, incomplete or divergent activation is
        an error rather than a reason to silently mix SQLite and JSON.
        """

        authorities = tuple(
            (member, self._authority(member))
            for member in SOURCE_ASSET_AUTHORITY_MEMBERS
        )
        active = tuple(
            (member, authority)
            for member, authority in authorities
            if authority is not None and authority.state == "sqlite_active"
        )
        if not active:
            return SourceAssetAuthorityResolution(
                records=None,
                authority_identity=JSON_SOURCE_ASSET_AUTHORITY_IDENTITY,
            )
        if len(active) != len(SOURCE_ASSET_AUTHORITY_MEMBERS):
            raise AggregateRepositoryFactoryError(
                "Source Asset authority is partially SQLite active"
            )

        evidence = active[0][1].evidence
        if evidence is None:  # pragma: no cover - _active_records also defends this
            raise AggregateRepositoryFactoryError(
                "Source Asset active authority lacks evidence"
            )
        if any(authority.evidence != evidence for _member, authority in active):
            raise AggregateRepositoryFactoryError(
                "Source Asset authority evidence does not match across members"
            )

        records: SQLiteStructuredRecordStore | None = None
        for member, authority in active:
            candidate = self._active_records(member, authority)
            if records is None:
                records = candidate
        if records is None:  # pragma: no cover - active length is checked above
            raise AggregateRepositoryFactoryError("Source Asset active target is missing")
        return SourceAssetAuthorityResolution(
            records=records,
            authority_identity=TARGET_IDENTITY,
        )

    def _authority(self, aggregate: str):
        authority_path = self._rebuild_root / AUTHORITY_DATABASE_NAME
        if not authority_path.exists():
            return None
        authority = self.authority_store_factory(authority_path).get(
            self.namespace_id,
            aggregate,
        )
        if authority is not None and authority.state == "rollback_required":
            raise AggregateRepositoryFactoryError(
                f"{aggregate} authority requires verified rollback"
            )
        return authority

    def _active_records(self, aggregate: str, authority) -> SQLiteStructuredRecordStore:
        if authority.state != "sqlite_active" or authority.evidence is None:
            raise AggregateRepositoryFactoryError(
                f"{aggregate} authority state is not resolvable"
            )
        if authority.evidence.target_identity != TARGET_IDENTITY:
            raise AggregateRepositoryFactoryError(
                f"{aggregate} target identity does not match runtime contract"
            )
        target_path = self._rebuild_root / STRUCTURED_DATABASE_NAME
        if not target_path.exists():
            raise AggregateRepositoryFactoryError(
                f"{aggregate} active SQLite target is missing"
            )
        records = self.record_store_factory(target_path)
        marker_id = f"{self.namespace_id}~{aggregate}"
        marker = records.read(_TARGET_MARKER_COLLECTION, marker_id)
        if marker is None:
            raise AggregateRepositoryFactoryError(
                f"{aggregate} active SQLite target marker is missing"
            )
        payload = marker.payload
        common_match = (
            payload.get("namespace_id") != self.namespace_id
            or payload.get("aggregate") != aggregate
            or payload.get("target_identity") != authority.evidence.target_identity
            or payload.get("migration_id") != authority.evidence.migration_id
        )
        if authority.evidence.verification_method == "exact_records":
            evidence_mismatch = (
                aggregate != "documents"
                or payload.get("verification_method") != "exact_records"
                or payload.get("verified_record_count") != authority.evidence.verified_record_count
                or payload.get("source_fingerprint") is not None
                or payload.get("target_fingerprint") is not None
            )
        else:
            evidence_mismatch = (
                payload.get("verification_method", "fingerprint") != "fingerprint"
                or payload.get("verified_record_count") is not None
                or payload.get("source_fingerprint") != authority.evidence.source_fingerprint
                or payload.get("target_fingerprint") != authority.evidence.target_fingerprint
            )
        if common_match or evidence_mismatch:
            raise AggregateRepositoryFactoryError(
                f"{aggregate} active SQLite target marker does not match authority evidence"
            )
        return records

    @property
    def _rebuild_root(self) -> Path:
        return self.runtime_root.expanduser().resolve(strict=False) / ".rebuild-data"
