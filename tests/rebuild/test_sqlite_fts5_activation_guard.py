from __future__ import annotations

from pathlib import Path

import pytest

from core.job_runner import ObjectStoreJobRepository
from core.product_core import CreateIndexRebuildJob, ResumeVerifiedIndexRebuildJob
from core.search_and_recall import (
    IndexSourceRecord,
    ObjectStoreRecallIndex,
    ObjectStoreSqliteFts5ActivationRepository,
    ObjectStoreSqliteFts5ManifestRepository,
    RecallIndexEntry,
    RecallQuery,
    SqliteFts5ActivationError,
    SqliteFts5DryRunIndex,
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
            updated_at="2026-07-01T03:30:00+08:00",
        ),
        IndexSourceRecord(
            source_id="source-beta",
            revision=2,
            content_hash="sha256-beta",
            updated_at="2026-07-01T03:31:00+08:00",
        ),
    )


def _entries() -> tuple[RecallIndexEntry, ...]:
    return (
        RecallIndexEntry(
            object_id="skill-alpha",
            project_id="project-alpha",
            layer="l3_project_skill",
            content="project recall evidence sqlite fts5 activation",
            source_refs=("source-alpha#char:0-25",),
            trust_status="user_confirmed",
            base_score=0.8,
        ),
        RecallIndexEntry(
            object_id="atom-alpha",
            project_id="project-alpha",
            layer="l1_atom",
            content="sqlite fts5 activation evidence atom",
            source_refs=("source-alpha#char:26-70",),
            trust_status="system_generated",
            base_score=0.6,
        ),
        RecallIndexEntry(
            object_id="foreign-beta",
            project_id="project-beta",
            layer="l1_atom",
            content="project recall evidence sqlite fts5 activation",
            source_refs=("source-beta#char:0-30",),
            trust_status="trusted",
            base_score=1.0,
        ),
    )


def _candidate_and_request():
    selection = select_default_recall_backend_policy()
    request = create_index_rebuild_request(
        freshness=evaluate_index_freshness(None, _sources()),
        backend_selection=selection,
        sources=_sources(),
        requested_at="2026-07-01T03:32:00+08:00",
    )
    manifest = create_sqlite_fts5_manifest(
        rebuild_request=request,
        backend_selection=selection,
        created_at="2026-07-01T03:33:00+08:00",
    )
    return request, sqlite_fts5_manifest_payload(manifest)


def _verified_job(tmp_path: Path, object_store: JsonObjectStore, candidate: dict[str, object]):
    jobs = ObjectStoreJobRepository(object_store)
    request, _ = _candidate_and_request()
    handoff = CreateIndexRebuildJob(jobs=jobs).execute(
        rebuild_request=request,
        candidate_manifest=candidate,
        created_at="2026-07-01T03:34:00+08:00",
    )
    verification = SqliteFts5DryRunIndex(tmp_path / ".sqlite-dry-run" / "recall_fts5.db").rebuild_and_query(
        _entries(),
        manifest=candidate,
        query=RecallQuery(
            text="project recall evidence sqlite",
            project_id="project-alpha",
            layers=("l3_project_skill", "l1_atom"),
            allowed_trust_statuses=("user_confirmed", "system_generated"),
            limit=5,
        ),
    )
    completed = ResumeVerifiedIndexRebuildJob(jobs=jobs).execute(
        job_id=handoff.job_id,
        verification=verification,
        verified_at="2026-07-01T03:35:00+08:00",
    )
    return handoff.job, completed.job


def test_sqlite_fts5_activation_requires_verified_candidate_and_preserves_previous_manifest(
    tmp_path: Path,
) -> None:
    object_store = _store(tmp_path)
    active_index = ObjectStoreRecallIndex(object_store)
    previous_manifest = active_index.rebuild(
        (
            RecallIndexEntry(
                object_id="skill-active",
                project_id="project-alpha",
                layer="l3_project_skill",
                content="active object store recall evidence",
                source_refs=("source-alpha#char:0-20",),
                trust_status="user_confirmed",
                base_score=0.8,
            ),
        ),
        source="active-object-store",
        rebuilt_at="2026-07-01T03:36:00+08:00",
    )
    _, candidate = _candidate_and_request()
    ObjectStoreSqliteFts5ManifestRepository(object_store).save_candidate_manifest(candidate)
    _, verified_job = _verified_job(tmp_path, object_store, candidate)

    result = ObjectStoreSqliteFts5ActivationRepository(object_store).activate(
        candidate_manifest=candidate,
        verified_job=verified_job,
        activated_by="system",
        activated_at="2026-07-01T03:37:00+08:00",
    )
    active = object_store.read("recall_index_manifests", "active")

    assert result.previous_manifest == previous_manifest
    assert active == result.active_manifest
    assert active["id"] == "active"
    assert active["status"] == "active"
    assert active["backend_kind"] == "sqlite_fts5"
    assert active["index_role"] == "active_manifest"
    assert active["previous_backend_kind"] == "object_store_lexical"
    assert active["source_refs"] == ["source-alpha#rev:1", "source-beta#rev:2"]
    assert active["vector"] == {"enabled": False, "provider": None, "dimension": None}
    assert active["verification_ref"] == verified_job["published_outputs"][0]["uri"]
    assert object_store.read("recall_index_manifests", "sqlite_fts5_candidate") == candidate
    assert object_store.list("recall_index_entries")
    assert not (tmp_path / "library").exists()


def test_sqlite_fts5_activation_rejects_unverified_job_without_replacing_active(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    active_index = ObjectStoreRecallIndex(object_store)
    previous_manifest = active_index.rebuild(
        (
            RecallIndexEntry(
                object_id="skill-active",
                project_id="project-alpha",
                layer="l3_project_skill",
                content="active object store recall evidence",
                source_refs=("source-alpha#char:0-20",),
                trust_status="user_confirmed",
                base_score=0.8,
            ),
        ),
        source="active-object-store",
        rebuilt_at="2026-07-01T03:38:00+08:00",
    )
    request, candidate = _candidate_and_request()
    pending = CreateIndexRebuildJob(jobs=ObjectStoreJobRepository(object_store)).execute(
        rebuild_request=request,
        candidate_manifest=candidate,
        created_at="2026-07-01T03:39:00+08:00",
    ).job

    with pytest.raises(SqliteFts5ActivationError, match="completed verified job"):
        ObjectStoreSqliteFts5ActivationRepository(object_store).activate(
            candidate_manifest=candidate,
            verified_job=pending,
            activated_by="system",
        )

    assert object_store.read("recall_index_manifests", "active") == previous_manifest
    assert not (tmp_path / "library").exists()


def test_sqlite_fts5_activation_rejects_mismatched_verified_output_and_vector(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    _, candidate = _candidate_and_request()
    _, verified_job = _verified_job(tmp_path, object_store, candidate)
    mismatched_job = dict(verified_job)
    mismatched_job["published_outputs"] = [
        {
            "kind": "other",
            "uri": verified_job["published_outputs"][0]["uri"],
            "object_id": "verified-other-manifest",
            "published": True,
        }
    ]

    with pytest.raises(SqliteFts5ActivationError, match="does not match manifest"):
        ObjectStoreSqliteFts5ActivationRepository(object_store).activate(
            candidate_manifest=candidate,
            verified_job=mismatched_job,
            activated_by="system",
        )

    vector_enabled = dict(candidate)
    vector_enabled["vector"] = {"enabled": True}
    with pytest.raises(SqliteFts5ManifestError, match="vector disabled"):
        ObjectStoreSqliteFts5ActivationRepository(object_store).activate(
            candidate_manifest=vector_enabled,
            verified_job=verified_job,
            activated_by="system",
        )

    assert object_store.read("recall_index_manifests", "active") is None
    assert not (tmp_path / "library").exists()
