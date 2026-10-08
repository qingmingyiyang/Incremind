from __future__ import annotations

from pathlib import Path

import pytest

from core.search_and_recall import (
    IndexSourceRecord,
    ObjectStoreRecallIndex,
    ObjectStoreSqliteFts5ManifestRepository,
    RecallIndexEntry,
    RecallQuery,
    SqliteFts5ManifestError,
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
            updated_at="2026-07-01T01:00:00+08:00",
        ),
        IndexSourceRecord(
            source_id="source-beta",
            revision=2,
            content_hash="sha256-beta",
            updated_at="2026-07-01T01:01:00+08:00",
        ),
    )


def _rebuild_request():
    sources = _sources()
    freshness = evaluate_index_freshness(None, sources)
    return create_index_rebuild_request(
        freshness=freshness,
        backend_selection=select_default_recall_backend_policy(),
        sources=sources,
        requested_at="2026-07-01T01:02:00+08:00",
    )


def test_sqlite_fts5_manifest_shape_records_traceable_candidate_backend() -> None:
    manifest = create_sqlite_fts5_manifest(
        rebuild_request=_rebuild_request(),
        backend_selection=select_default_recall_backend_policy(),
        created_at="2026-07-01T01:03:00+08:00",
    )

    payload = sqlite_fts5_manifest_payload(manifest)

    assert payload["id"] == "sqlite_fts5_candidate"
    assert payload["status"] == "planned"
    assert payload["backend_kind"] == "sqlite_fts5"
    assert payload["index_role"] == "candidate_manifest"
    assert payload["source_count"] == 2
    assert payload["source_refs"] == ["source-alpha#rev:1", "source-beta#rev:2"]
    assert payload["fts"] == {
        "engine": "sqlite",
        "module": "fts5",
        "table": "recall_fts",
        "content_columns": ["content", "search_text"],
        "tokenizer": "unicode61",
        "ranker": "bm25",
        "filters": ["project_id", "layer", "trust_status"],
    }
    assert payload["vector"] == {"enabled": False, "provider": None, "dimension": None}


def test_sqlite_fts5_candidate_manifest_does_not_replace_active_object_store_index(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    active_index = ObjectStoreRecallIndex(object_store)
    active_manifest = active_index.rebuild(
        (
            RecallIndexEntry(
                object_id="skill-alpha",
                project_id="project-alpha",
                layer="l3_project_skill",
                content="project recall evidence",
                source_refs=("source-alpha#char:0-20",),
                trust_status="user_confirmed",
                base_score=0.8,
            ),
        ),
        source="active-object-store",
        rebuilt_at="2026-07-01T01:04:00+08:00",
    )
    candidate = create_sqlite_fts5_manifest(
        rebuild_request=_rebuild_request(),
        backend_selection=select_default_recall_backend_policy(),
        created_at="2026-07-01T01:05:00+08:00",
    )

    stored_candidate = ObjectStoreSqliteFts5ManifestRepository(object_store).save_candidate_manifest(candidate)
    hits = active_index.recall(
        RecallQuery(
            text="project recall evidence",
            project_id="project-alpha",
            layers=("l3_project_skill",),
            allowed_trust_statuses=("user_confirmed",),
            limit=3,
        )
    )

    assert active_index.manifest() == active_manifest
    assert active_index.manifest()["backend_kind"] == "object_store_lexical"
    assert stored_candidate["backend_kind"] == "sqlite_fts5"
    assert object_store.read("recall_index_manifests", "sqlite_fts5_candidate") == stored_candidate
    assert [hit.object_id for hit in hits] == ["skill-alpha"]
    assert not (tmp_path / "library").exists()


def test_sqlite_fts5_manifest_rejects_active_replacement_vector_and_untraceable_sources() -> None:
    request = _rebuild_request()
    selection = select_default_recall_backend_policy()

    with pytest.raises(SqliteFts5ManifestError, match="active index"):
        create_sqlite_fts5_manifest(
            rebuild_request=request,
            backend_selection=selection,
            manifest_id="active",
        )

    with pytest.raises(SqliteFts5ManifestError, match="vector disabled"):
        sqlite_fts5_manifest_payload(
            {
                "schema_version": "1.0.0",
                "id": "sqlite_fts5_candidate",
                "status": "planned",
                "backend_kind": "sqlite_fts5",
                "source_fingerprint": request.source_fingerprint,
                "source_count": 1,
                "source_refs": ["source-alpha#rev:1"],
                "index_role": "candidate_manifest",
                "fts": {
                    "engine": "sqlite",
                    "module": "fts5",
                    "ranker": "bm25",
                    "filters": ["project_id", "layer", "trust_status"],
                },
                "vector": {"enabled": True},
                "created_at": "2026-07-01T01:06:00+08:00",
            }
        )

    with pytest.raises(SqliteFts5ManifestError, match="source refs"):
        sqlite_fts5_manifest_payload(
            {
                "schema_version": "1.0.0",
                "id": "sqlite_fts5_candidate",
                "status": "planned",
                "backend_kind": "sqlite_fts5",
                "source_fingerprint": request.source_fingerprint,
                "source_count": 1,
                "source_refs": ["source-alpha"],
                "index_role": "candidate_manifest",
                "fts": {
                    "engine": "sqlite",
                    "module": "fts5",
                    "ranker": "bm25",
                    "filters": ["project_id", "layer", "trust_status"],
                },
                "vector": {"enabled": False},
                "created_at": "2026-07-01T01:07:00+08:00",
            }
        )
