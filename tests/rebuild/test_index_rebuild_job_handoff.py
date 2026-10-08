from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.job_runner import InMemoryJobRepository, ObjectStoreJobRepository
from core.product_core import CreateIndexRebuildJob, IndexRebuildJobHandoffError
from core.search_and_recall import (
    IndexSourceRecord,
    ObjectStoreRecallIndex,
    ObjectStoreSqliteFts5ManifestRepository,
    RecallIndexEntry,
    SqliteFts5ManifestError,
    create_index_rebuild_request,
    create_sqlite_fts5_manifest,
    evaluate_index_freshness,
    select_default_recall_backend_policy,
    sqlite_fts5_manifest_payload,
)
from core.storage_provider import JsonObjectStore
from tools.validate_rebuild_contracts import validate_contract_instance


ROOT = Path(__file__).resolve().parents[2]
CONTRACT_ROOT = ROOT / "core-contracts" / "rebuild"


def _schema(name: str) -> dict[str, object]:
    return json.loads((CONTRACT_ROOT / name).read_text(encoding="utf-8"))


def _store(tmp_path: Path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def _sources() -> tuple[IndexSourceRecord, ...]:
    return (
        IndexSourceRecord(
            source_id="source-alpha",
            revision=1,
            content_hash="sha256-alpha",
            updated_at="2026-07-01T01:30:00+08:00",
        ),
        IndexSourceRecord(
            source_id="source-beta",
            revision=2,
            content_hash="sha256-beta",
            updated_at="2026-07-01T01:31:00+08:00",
        ),
    )


def _request_and_manifest():
    selection = select_default_recall_backend_policy()
    request = create_index_rebuild_request(
        freshness=evaluate_index_freshness(None, _sources()),
        backend_selection=selection,
        sources=_sources(),
        requested_at="2026-07-01T01:32:00+08:00",
    )
    manifest = create_sqlite_fts5_manifest(
        rebuild_request=request,
        backend_selection=selection,
        created_at="2026-07-01T01:33:00+08:00",
    )
    return request, manifest


def test_index_rebuild_request_persists_as_pending_job_without_executing_backend(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    request, manifest = _request_and_manifest()
    ObjectStoreSqliteFts5ManifestRepository(object_store).save_candidate_manifest(manifest)

    result = CreateIndexRebuildJob(
        jobs=ObjectStoreJobRepository(object_store),
        namespace_id="default",
    ).execute(
        rebuild_request=request,
        candidate_manifest=manifest,
        created_at="2026-07-01T01:34:00+08:00",
    )
    stored = ObjectStoreJobRepository(object_store).get(result.job_id)

    assert stored == result.job
    assert stored is not None
    assert stored["job_type"] == "rebuild_index"
    assert stored["status"] == "pending"
    assert stored["attempt"] == 0
    assert stored["progress"] == {
        "current": 0,
        "total": 3,
        "percent": 0,
        "message": "Index rebuild request is persisted and waiting for an index worker.",
    }
    assert [step["name"] for step in stored["steps"]] == [
        "load_rebuild_request",
        "build_sqlite_fts5_index",
        "verify_index_traceability",
    ]
    assert all(step["status"] == "pending" for step in stored["steps"])
    assert stored["staged_outputs"] == []
    assert stored["published_outputs"] == []
    assert validate_contract_instance("job.schema.json", _schema("job.schema.json"), stored) == []
    assert not (tmp_path / "library").exists()


def test_index_rebuild_job_preserves_manifest_and_source_refs_without_replacing_active_index(tmp_path: Path) -> None:
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
        rebuilt_at="2026-07-01T01:35:00+08:00",
    )
    request, manifest = _request_and_manifest()
    candidate = ObjectStoreSqliteFts5ManifestRepository(object_store).save_candidate_manifest(manifest)

    result = CreateIndexRebuildJob(jobs=ObjectStoreJobRepository(object_store)).execute(
        rebuild_request=request,
        candidate_manifest=candidate,
        created_at="2026-07-01T01:36:00+08:00",
    )

    build_step = result.job["steps"][1]
    assert active_index.manifest() == active_manifest
    assert active_index.manifest()["backend_kind"] == "object_store_lexical"
    assert candidate["backend_kind"] == "sqlite_fts5"
    assert build_step["input_refs"] == [
        "crp://default/recall/index-manifests/sqlite_fts5_candidate",
        "crp://default/sources/source-alpha/revisions/1",
        "crp://default/sources/source-beta/revisions/2",
    ]
    assert object_store.list("recall_index_entries")
    assert object_store.list("jobs")
    assert not (tmp_path / "library").exists()


def test_index_rebuild_job_rejects_mismatched_manifest_or_vector_enabled() -> None:
    request, manifest = _request_and_manifest()
    use_case = CreateIndexRebuildJob(jobs=InMemoryJobRepository())

    bad_fingerprint = sqlite_fts5_manifest_payload(manifest)
    bad_fingerprint["source_fingerprint"] = "different"

    with pytest.raises(IndexRebuildJobHandoffError, match="source fingerprint"):
        use_case.execute(rebuild_request=request, candidate_manifest=bad_fingerprint)

    vector_enabled = sqlite_fts5_manifest_payload(manifest)
    vector_enabled["vector"] = {"enabled": True}
    with pytest.raises(SqliteFts5ManifestError, match="vector disabled"):
        use_case.execute(rebuild_request=request, candidate_manifest=vector_enabled)
