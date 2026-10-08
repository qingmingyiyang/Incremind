from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from core.search_and_recall import (
    IndexSourceRecord,
    LibrarySearchError,
    LibrarySearchService,
    ObjectStoreRecallIndex,
    ObjectStoreSqliteFts5ActivationRepository,
    RecallIndexEntry,
    SqliteFts5DryRunIndex,
    create_index_rebuild_request,
    create_sqlite_fts5_manifest,
    evaluate_index_freshness,
    select_default_recall_backend_policy,
    sqlite_fts5_manifest_payload,
)
from core.storage_provider import JsonObjectStore


def _store(tmp_path: Path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def _sources() -> tuple[IndexSourceRecord, ...]:
    return (
        IndexSourceRecord(
            source_id="source-alpha",
            revision=1,
            content_hash="sha256-alpha",
            updated_at="2026-07-01T02:00:00+08:00",
        ),
        IndexSourceRecord(
            source_id="source-beta",
            revision=2,
            content_hash="sha256-beta",
            updated_at="2026-07-01T02:01:00+08:00",
        ),
    )


def _candidate_manifest() -> dict[str, object]:
    sources = _sources()
    request = create_index_rebuild_request(
        freshness=evaluate_index_freshness(None, sources),
        backend_selection=select_default_recall_backend_policy(),
        sources=sources,
        requested_at="2026-07-01T02:02:00+08:00",
    )
    return sqlite_fts5_manifest_payload(
        create_sqlite_fts5_manifest(
            rebuild_request=request,
            backend_selection=select_default_recall_backend_policy(),
            created_at="2026-07-01T02:03:00+08:00",
        )
    )


def _entries() -> tuple[RecallIndexEntry, ...]:
    return (
        RecallIndexEntry(
            object_id="skill-alpha",
            project_id="project-alpha",
            layer="l3_project_skill",
            content="project recall evidence for sqlite fts5 active search",
            source_refs=("source-alpha#char:0-25",),
            trust_status="user_confirmed",
            base_score=0.8,
        ),
        RecallIndexEntry(
            object_id="atom-alpha",
            project_id="project-alpha",
            layer="l1_atom",
            content="sqlite fts5 active search evidence atom",
            source_refs=("source-alpha#char:26-70",),
            trust_status="system_generated",
            base_score=0.6,
        ),
        RecallIndexEntry(
            object_id="foreign-beta",
            project_id="project-beta",
            layer="l1_atom",
            content="project recall evidence for sqlite fts5 active search",
            source_refs=("source-beta#char:0-30",),
            trust_status="trusted",
            base_score=1.0,
        ),
    )


def _verified_job(manifest_id: str, database_uri: str) -> dict[str, object]:
    return {
        "id": "rebuild-job-active-search-001",
        "job_type": "rebuild_index",
        "status": "completed",
        "published_outputs": [
            {
                "published": True,
                "kind": "other",
                "object_id": f"verified-{manifest_id}",
                "uri": f"crp://default/recall/index-verifications/verified-{manifest_id}.json",
            }
        ],
        "worker_verification": {
            "status": "ready",
            "backend_kind": "sqlite_fts5",
            "database_uri": database_uri,
            "manifest_id": manifest_id,
            "entry_count": 3,
            "hit_count": 2,
            "hit_object_ids": ["skill-alpha", "atom-alpha"],
            "vector_enabled": False,
            "source_refs": [
                "source-alpha#rev:1",
                "source-beta#rev:2",
            ],
        },
    }


def _build_active_fts5_database(tmp_path: Path) -> Path:
    """Build a real SQLite FTS5 database using the dry-run index, return its path."""
    database_path = tmp_path / ".sqlite-active" / "recall_fts5.db"
    result = SqliteFts5DryRunIndex(database_path).rebuild_and_query(
        _entries(),
        manifest=_candidate_manifest(),
        query=__import__("core.search_and_recall", fromlist=["RecallQuery"]).RecallQuery(
            text="project recall evidence sqlite",
            project_id="project-alpha",
            layers=("l3_project_skill", "l1_atom"),
            allowed_trust_statuses=("user_confirmed", "system_generated", "trusted"),
            limit=5,
        ),
    )
    assert result.status == "ready"
    assert database_path.exists()
    return database_path


def test_activation_stores_database_uri_in_active_manifest(tmp_path: Path) -> None:
    store = _store(tmp_path)
    candidate = _candidate_manifest()
    database_path = _build_active_fts5_database(tmp_path)
    database_uri = database_path.resolve().as_uri()
    repo = ObjectStoreSqliteFts5ActivationRepository(store)
    result = repo.activate(
        candidate_manifest=candidate,
        verified_job=_verified_job(candidate["id"], database_uri),
        activated_by="phase5-test",
        database_uri=database_uri,
    )
    assert result.active_manifest.get("database_uri") == database_uri


def test_activation_rejects_non_file_database_uri(tmp_path: Path) -> None:
    store = _store(tmp_path)
    candidate = _candidate_manifest()
    repo = ObjectStoreSqliteFts5ActivationRepository(store)
    with pytest.raises(Exception, match="file://"):
        repo.activate(
            candidate_manifest=candidate,
            verified_job=_verified_job(candidate["id"], "http://example.com/db.sqlite"),
            activated_by="phase5-test",
            database_uri="http://example.com/db.sqlite",
        )


def test_library_search_uses_active_fts5_when_available(tmp_path: Path) -> None:
    store = _store(tmp_path)
    candidate = _candidate_manifest()
    database_path = _build_active_fts5_database(tmp_path)
    database_uri = database_path.resolve().as_uri()
    repo = ObjectStoreSqliteFts5ActivationRepository(store)
    repo.activate(
        candidate_manifest=candidate,
        verified_job=_verified_job(candidate["id"], database_uri),
        activated_by="phase5-test",
        database_uri=database_uri,
    )
    recall_index = ObjectStoreRecallIndex(store)
    active_manifest = recall_index.manifest()
    assert active_manifest is not None
    service = LibrarySearchService(
        recall_index=recall_index,
        active_manifest=active_manifest,
    )
    result = service.search(
        query="project recall evidence sqlite",
        project_id="project-alpha",
        limit=5,
    )
    assert result.status == "ready"
    assert result.backend == "sqlite_fts5"
    assert result.total > 0
    assert all(hit.backend == "sqlite_fts5" for hit in result.hits)
    object_ids = {hit.object_id for hit in result.hits}
    assert "skill-alpha" in object_ids
    assert "foreign-beta" not in object_ids  # different project


def test_stale_fts5_cannot_resurrect_hard_forgotten_source(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    candidate = _candidate_manifest()
    database_path = _build_active_fts5_database(tmp_path)
    database_uri = database_path.resolve().as_uri()
    repo = ObjectStoreSqliteFts5ActivationRepository(store)
    repo.activate(
        candidate_manifest=candidate,
        verified_job=_verified_job(candidate["id"], database_uri),
        activated_by="phase5-test",
        database_uri=database_uri,
    )
    recall_index = ObjectStoreRecallIndex(store)
    active_manifest = recall_index.manifest()
    assert active_manifest is not None

    # The active SQLite file still contains source-alpha, but the current
    # authority ledger no longer does.  Search must bypass the stale file.
    service = LibrarySearchService(
        recall_index=recall_index,
        active_manifest=active_manifest,
        source_ledger=(_sources()[1],),
    )
    result = service.search(
        query="project recall evidence sqlite",
        project_id="project-alpha",
        limit=5,
    )
    assert result.status == "empty"
    assert result.backend == "object_store_lexical"
    assert result.reason == "sqlite_fts5_stale"
    assert result.index_stale is True
    assert result.hits == ()


def test_empty_current_authority_cannot_fall_back_to_old_object_store_snapshot(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    recall_index = ObjectStoreRecallIndex(store)
    recall_index.rebuild(_entries(), source="old-snapshot")
    candidate = _candidate_manifest()
    database_path = _build_active_fts5_database(tmp_path)
    database_uri = database_path.resolve().as_uri()
    ObjectStoreSqliteFts5ActivationRepository(store).activate(
        candidate_manifest=candidate,
        verified_job=_verified_job(candidate["id"], database_uri),
        activated_by="phase5-test",
        database_uri=database_uri,
    )
    active_manifest = recall_index.manifest()
    assert active_manifest is not None

    result = LibrarySearchService(
        recall_index=recall_index,
        active_manifest=active_manifest,
        source_ledger=(),
        current_entries=(),
    ).search(query="project recall evidence sqlite", project_id="project-alpha", limit=5)

    assert result.status == "empty"
    assert result.index_stale is True
    assert result.reason == "sqlite_fts5_stale"
    assert result.hits == ()


def test_library_search_falls_back_to_object_store_when_no_manifest(tmp_path: Path) -> None:
    store = _store(tmp_path)
    recall_index = ObjectStoreRecallIndex(store)
    recall_index.rebuild(_entries(), source="phase5-fallback-test")
    service = LibrarySearchService(
        recall_index=recall_index,
        active_manifest=None,
    )
    result = service.search(query="project recall evidence sqlite", limit=5)
    assert result.backend == "object_store_lexical"
    assert result.reason == "no_active_manifest"
    assert result.index_stale is True


def test_library_search_falls_back_when_database_uri_missing(tmp_path: Path) -> None:
    store = _store(tmp_path)
    candidate = _candidate_manifest()
    repo = ObjectStoreSqliteFts5ActivationRepository(store)
    # Activate WITHOUT database_uri (legacy active manifest)
    repo.activate(
        candidate_manifest=candidate,
        verified_job=_verified_job(candidate["id"], "file:///tmp/missing.sqlite"),
        activated_by="phase5-test",
    )
    recall_index = ObjectStoreRecallIndex(store)
    recall_index.rebuild(_entries(), source="phase5-fallback-test")
    active_manifest = recall_index.manifest()
    assert active_manifest is not None
    service = LibrarySearchService(
        recall_index=recall_index,
        active_manifest=active_manifest,
    )
    result = service.search(query="project recall evidence sqlite", limit=5)
    assert result.backend == "object_store_lexical"
    assert result.reason == "sqlite_fts5_unavailable"


def test_library_search_falls_back_when_database_file_does_not_exist(tmp_path: Path) -> None:
    store = _store(tmp_path)
    candidate = _candidate_manifest()
    repo = ObjectStoreSqliteFts5ActivationRepository(store)
    missing_uri = (tmp_path / "nonexistent.sqlite").resolve().as_uri()
    repo.activate(
        candidate_manifest=candidate,
        verified_job=_verified_job(candidate["id"], missing_uri),
        activated_by="phase5-test",
        database_uri=missing_uri,
    )
    recall_index = ObjectStoreRecallIndex(store)
    recall_index.rebuild(_entries(), source="phase5-fallback-test")
    active_manifest = recall_index.manifest()
    service = LibrarySearchService(
        recall_index=recall_index,
        active_manifest=active_manifest,
    )
    result = service.search(query="project recall evidence sqlite", limit=5)
    assert result.backend == "object_store_lexical"
    assert result.reason == "sqlite_fts5_unavailable"


def test_library_search_rejects_empty_query(tmp_path: Path) -> None:
    store = _store(tmp_path)
    recall_index = ObjectStoreRecallIndex(store)
    service = LibrarySearchService(
        recall_index=recall_index,
        active_manifest=None,
    )
    with pytest.raises(LibrarySearchError, match="non-empty"):
        service.search(query="")


def test_library_search_rejects_zero_limit(tmp_path: Path) -> None:
    store = _store(tmp_path)
    recall_index = ObjectStoreRecallIndex(store)
    service = LibrarySearchService(
        recall_index=recall_index,
        active_manifest=None,
    )
    with pytest.raises(LibrarySearchError, match="positive"):
        service.search(query="evidence", limit=0)


def test_library_search_returns_empty_when_no_hits(tmp_path: Path) -> None:
    store = _store(tmp_path)
    recall_index = ObjectStoreRecallIndex(store)
    recall_index.rebuild(_entries(), source="phase5-empty-test")
    service = LibrarySearchService(
        recall_index=recall_index,
        active_manifest=None,
    )
    result = service.search(
        query="zzznonexistenttermszzz",
        project_id="project-nonexistent",
        limit=5,
    )
    assert result.status == "empty"
    assert result.total == 0
    assert result.hits == ()


def test_library_search_respects_project_filter(tmp_path: Path) -> None:
    store = _store(tmp_path)
    recall_index = ObjectStoreRecallIndex(store)
    recall_index.rebuild(_entries(), source="phase5-project-filter-test")
    service = LibrarySearchService(
        recall_index=recall_index,
        active_manifest=None,
    )
    result = service.search(
        query="project recall evidence sqlite",
        project_id="project-alpha",
        limit=5,
    )
    assert result.total > 0
    assert all("project-alpha" in hit.source_refs[0] or "source-alpha" in hit.source_refs[0] for hit in result.hits)
    foreign_result = service.search(
        query="project recall evidence sqlite",
        project_id="project-gamma",
        limit=5,
    )
    assert foreign_result.status == "empty"
