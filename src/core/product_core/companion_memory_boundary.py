"""Public Product Core boundary for Companion memory and recall integration.

Companion Core is intentionally restricted to Product Core.  This module keeps
the concrete memory and search implementations behind that allowed composition
boundary without changing their runtime contracts.
"""

from core.memory_core import MemoryCandidateRepositoryError, memory_candidate_id
from core.search_and_recall import (
    IndexSourceRecord,
    LibrarySearchError,
    LibrarySearchService,
    RecallIndexEntry,
    RecallQuery,
    SqliteFts5ActiveIndex,
    SqliteFts5DryRunIndex,
    create_index_rebuild_request,
    create_sqlite_fts5_manifest,
    evaluate_index_freshness,
    select_default_recall_backend_policy,
    sqlite_fts5_manifest_payload,
)


__all__ = [
    "IndexSourceRecord",
    "LibrarySearchError",
    "LibrarySearchService",
    "MemoryCandidateRepositoryError",
    "RecallIndexEntry",
    "RecallQuery",
    "SqliteFts5ActiveIndex",
    "SqliteFts5DryRunIndex",
    "create_index_rebuild_request",
    "create_sqlite_fts5_manifest",
    "evaluate_index_freshness",
    "memory_candidate_id",
    "select_default_recall_backend_policy",
    "sqlite_fts5_manifest_payload",
]
