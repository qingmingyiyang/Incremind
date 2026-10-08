import multiprocessing
import os
from pathlib import Path

import pytest

from core.job_runner import ObjectStoreJobRepository
from core.product_core import (
    CreateIndexRebuildJob,
    IndexRebuildRuntimeError,
    recover_index_rebuild_jobs,
    run_index_rebuild_job,
)
from core.search_and_recall import (
    LibrarySearchService,
    ObjectStoreRecallIndex,
    ObjectStoreSqliteFts5ManifestRepository,
    RecallIndexEntry,
    RecallQuery,
    build_recall_authority_ledger,
    build_recall_entries_from_object_store,
    create_index_rebuild_request,
    create_sqlite_fts5_manifest,
    evaluate_index_freshness,
    select_default_recall_backend_policy,
)
from core.storage_provider import JsonObjectStore


def _store(tmp_path: Path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def _source(store: JsonObjectStore, source_id: str, text: str, *, created_at: str) -> None:
    store.write("sources", source_id, {
        "schema_version": "1.0.0", "id": source_id, "type": "text", "title": text,
        "content_hash": f"sha256-{source_id}-{text}", "created_at": created_at,
        "trust_status": "user_confirmed", "metadata": {"content": text, "project_id": "project-alpha"},
    }, expected_revision=None)


def _pending(store: JsonObjectStore, *, entries_loader=None):
    entries = tuple(entries_loader()) if entries_loader is not None else build_recall_entries_from_object_store(store)
    ledger = build_recall_authority_ledger(entries)
    selection = select_default_recall_backend_policy()
    request = create_index_rebuild_request(
        freshness=evaluate_index_freshness(ObjectStoreRecallIndex(store).manifest(), ledger),
        backend_selection=selection, sources=ledger,
    )
    manifest = create_sqlite_fts5_manifest(
        rebuild_request=request, backend_selection=selection,
        manifest_id=f"sqlite_fts5_candidate_{request.source_fingerprint[:16]}",
    )
    ObjectStoreSqliteFts5ManifestRepository(
        store, manifest_id=manifest.manifest_id
    ).save_candidate_manifest(manifest)
    jobs = ObjectStoreJobRepository(store)
    handoff = CreateIndexRebuildJob(jobs=jobs).execute(rebuild_request=request, candidate_manifest=manifest)
    return jobs, handoff


def _crash_after_verification(runtime_root: str, job_id: str) -> None:
    root = Path(runtime_root)
    store = _store(root)
    jobs = ObjectStoreJobRepository(store)
    run_index_rebuild_job(
        object_store=store,
        jobs=jobs,
        runtime_root=root,
        job_id=job_id,
        after_verification_persisted=lambda: os._exit(79),
    )


def test_runtime_builds_activates_searches_and_replays(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _source(store, "source-alpha", "真实索引新词 lighthouse", created_at="2026-07-22T01:00:00+00:00")
    jobs, handoff = _pending(store)

    first = run_index_rebuild_job(object_store=store, jobs=jobs, runtime_root=tmp_path, job_id=handoff.job_id)
    second = run_index_rebuild_job(object_store=store, jobs=jobs, runtime_root=tmp_path, job_id=handoff.job_id)

    assert first.job["status"] == "completed"
    assert first.active_manifest["verified_job_id"] == handoff.job_id
    assert second.replayed is True and second.active_manifest == first.active_manifest
    database_uri = first.active_manifest["database_uri"]
    assert isinstance(database_uri, str) and database_uri.startswith("file://")
    result = LibrarySearchService(
        recall_index=ObjectStoreRecallIndex(store), active_manifest=first.active_manifest,
        source_ledger=build_recall_authority_ledger(build_recall_entries_from_object_store(store)),
    ).search(query="lighthouse", project_id="project-alpha")
    assert result.backend == "sqlite_fts5"
    assert [hit.object_id for hit in result.hits] == ["source-alpha"]


def test_runtime_indexes_entries_from_resolved_current_authority_loader(tmp_path: Path) -> None:
    store = _store(tmp_path)
    entry = RecallIndexEntry(
        object_id="atom-sqlite-current",
        project_id="default",
        layer="l1_atom",
        content="CP_B05_MEMORY_CANARY 正式 SQLite 记忆霜鲸七号",
        source_refs=("message:user:sqlite#companion:message",),
        trust_status="user_confirmed",
        base_score=0.65,
    )
    loader = lambda: (entry,)
    jobs, handoff = _pending(store, entries_loader=loader)

    result = run_index_rebuild_job(
        object_store=store,
        jobs=jobs,
        runtime_root=tmp_path,
        job_id=handoff.job_id,
        recall_entries_loader=loader,
    )
    search = LibrarySearchService(
        recall_index=ObjectStoreRecallIndex(store),
        active_manifest=result.active_manifest,
        source_ledger=build_recall_authority_ledger(loader()),
    ).search(query="CP_B05_MEMORY_CANARY", project_id="default", layers=("l1_atom",))

    assert store.list("memory_atoms") == ()
    assert search.backend == "sqlite_fts5"
    assert [hit.object_id for hit in search.hits] == ["atom-sqlite-current"]


def test_runtime_rejects_source_drift_and_preserves_old_active(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _source(store, "source-alpha", "旧索引内容", created_at="2026-07-22T01:00:00+00:00")
    previous = ObjectStoreRecallIndex(store).rebuild((), source="previous")
    jobs, handoff = _pending(store)
    _source(store, "source-beta", "构建中新增", created_at="2026-07-22T01:01:00+00:00")

    with pytest.raises(IndexRebuildRuntimeError, match="source ledger changed"):
        run_index_rebuild_job(object_store=store, jobs=jobs, runtime_root=tmp_path, job_id=handoff.job_id)

    assert store.read("recall_index_manifests", "active") == previous
    failed = jobs.get(handoff.job_id)
    assert failed is not None and failed["status"] == "failed"
    assert failed["error"]["code"] == "index_rebuild_failed"
    assert not list((tmp_path / ".rebuild-data" / "recall-indexes").glob("*.sqlite3"))


def test_handoff_does_not_reset_completed_idempotent_job(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _source(store, "source-alpha", "幂等索引", created_at="2026-07-22T01:00:00+00:00")
    jobs, handoff = _pending(store)
    completed = run_index_rebuild_job(object_store=store, jobs=jobs, runtime_root=tmp_path, job_id=handoff.job_id)
    ledger = build_recall_authority_ledger(build_recall_entries_from_object_store(store))
    selection = select_default_recall_backend_policy()
    request = create_index_rebuild_request(
        freshness=evaluate_index_freshness(None, ledger), backend_selection=selection, sources=ledger,
    )
    manifest = store.read("recall_index_manifests", f"sqlite_fts5_candidate_{request.source_fingerprint[:16]}")
    replay = CreateIndexRebuildJob(jobs=jobs).execute(rebuild_request=request, candidate_manifest=manifest)
    assert replay.job == completed.job
    assert jobs.get(handoff.job_id)["status"] == "completed"


def test_startup_recovers_real_process_exit_after_verified_candidate(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _source(store, "source-alpha", "进程中断后恢复索引", created_at="2026-07-22T01:00:00+00:00")
    jobs, handoff = _pending(store)
    process = multiprocessing.get_context("spawn").Process(
        target=_crash_after_verification,
        args=(str(tmp_path), handoff.job_id),
    )

    process.start()
    process.join(timeout=30)

    assert process.exitcode == 79
    interrupted = jobs.get(handoff.job_id)
    assert interrupted is not None and interrupted["status"] == "running"
    assert store.read("recall_index_verifications", handoff.job_id) is not None
    assert store.read("recall_index_manifests", "active") is None

    recovered = recover_index_rebuild_jobs(object_store=store, jobs=jobs, runtime_root=tmp_path)

    assert recovered == (handoff.job_id,)
    completed = jobs.get(handoff.job_id)
    active = store.read("recall_index_manifests", "active")
    assert completed is not None and completed["status"] == "completed"
    assert active is not None and active["verified_job_id"] == handoff.job_id
