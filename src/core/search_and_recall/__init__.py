"""Trust-aware cross-layer recall ports."""

from .backend_policy import (
    RecallBackendCandidate,
    RecallBackendDecision,
    RecallBackendSelection,
    SelectRecallBackendPolicy,
    default_recall_backend_candidates,
    select_default_recall_backend_policy,
    sqlite_fts5_candidate,
)
from .index_lifecycle import (
    IndexFreshness,
    IndexLifecyclePolicyError,
    IndexRebuildRequest,
    IndexSourceRecord,
    build_recall_authority_ledger,
    build_source_ledger_from_object_store,
    create_index_rebuild_request,
    evaluate_index_freshness,
    manifest_with_source_fingerprint,
    source_ledger_fingerprint,
)
from .object_store_entries import (
    build_recall_entries_from_authorities,
    build_recall_entries_from_object_store,
)
from .ports import RecallHit, RecallIndexEntry, RecallPort, RecallQuery
from .runtime import (
    InMemoryRecallIndexAdapter,
    ObjectStoreRecallIndex,
    ObjectStoreRecallRepository,
    RecallRepositoryError,
)
from .sqlite_fts5 import (
    SqliteFts5DryRunError,
    SqliteFts5DryRunIndex,
    SqliteFts5DryRunResult,
    sqlite_fts5_verification_query,
)
from .sqlite_activation import (
    ObjectStoreSqliteFts5ActivationRepository,
    SqliteFts5ActivationError,
    SqliteFts5ActivationResult,
)
from .sqlite_manifest import (
    ObjectStoreSqliteFts5ManifestRepository,
    SqliteFts5Manifest,
    SqliteFts5ManifestError,
    create_sqlite_fts5_manifest,
    sqlite_fts5_manifest_payload,
)
from .sqlite_vec_manifest import (
    SqliteVecInterface,
    SqliteVecManifest,
    SqliteVecManifestError,
    create_sqlite_vec_manifest,
    sqlite_vec_manifest_payload,
)
from .active_search import (
    LibrarySearchError,
    LibrarySearchHit,
    LibrarySearchResult,
    LibrarySearchService,
    SqliteFts5ActiveIndex,
)

__all__ = [
    "InMemoryRecallIndexAdapter",
    "LibrarySearchError",
    "LibrarySearchHit",
    "LibrarySearchResult",
    "LibrarySearchService",
    "ObjectStoreRecallIndex",
    "ObjectStoreRecallRepository",
    "ObjectStoreSqliteFts5ActivationRepository",
    "ObjectStoreSqliteFts5ManifestRepository",
    "RecallBackendCandidate",
    "RecallBackendDecision",
    "RecallBackendSelection",
    "RecallHit",
    "RecallIndexEntry",
    "RecallPort",
    "RecallQuery",
    "RecallRepositoryError",
    "SelectRecallBackendPolicy",
    "SqliteFts5Manifest",
    "SqliteFts5ManifestError",
    "SqliteFts5DryRunError",
    "SqliteFts5DryRunIndex",
    "SqliteFts5DryRunResult",
    "sqlite_fts5_verification_query",
    "SqliteFts5ActivationError",
    "SqliteFts5ActivationResult",
    "SqliteFts5ActiveIndex",
    "SqliteVecInterface",
    "SqliteVecManifest",
    "SqliteVecManifestError",
    "IndexFreshness",
    "IndexLifecyclePolicyError",
    "IndexRebuildRequest",
    "IndexSourceRecord",
    "build_recall_authority_ledger",
    "build_recall_entries_from_authorities",
    "build_source_ledger_from_object_store",
    "build_recall_entries_from_object_store",
    "create_index_rebuild_request",
    "create_sqlite_fts5_manifest",
    "create_sqlite_vec_manifest",
    "default_recall_backend_candidates",
    "evaluate_index_freshness",
    "manifest_with_source_fingerprint",
    "select_default_recall_backend_policy",
    "source_ledger_fingerprint",
    "sqlite_fts5_candidate",
    "sqlite_fts5_manifest_payload",
    "sqlite_vec_manifest_payload",
]
