from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.job_runner import ObjectStoreJobRepository
from core.product_core import (
    CreateIndexRebuildJob,
    IndexRebuildJobResumeError,
    ResumeVerifiedIndexRebuildJob,
)
from core.search_and_recall import (
    IndexSourceRecord,
    ObjectStoreRecallIndex,
    RecallIndexEntry,
    RecallQuery,
    SqliteFts5DryRunIndex,
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
            updated_at="2026-07-01T02:30:00+08:00",
        ),
        IndexSourceRecord(
            source_id="source-beta",
            revision=2,
            content_hash="sha256-beta",
            updated_at="2026-07-01T02:31:00+08:00",
        ),
    )


def _entries() -> tuple[RecallIndexEntry, ...]:
    return (
        RecallIndexEntry(
            object_id="skill-alpha",
            project_id="project-alpha",
            layer="l3_project_skill",
            content="project recall evidence sqlite fts5 verification",
            source_refs=("source-alpha#char:0-25",),
            trust_status="user_confirmed",
            base_score=0.8,
        ),
        RecallIndexEntry(
            object_id="atom-alpha",
            project_id="project-alpha",
            layer="l1_atom",
            content="sqlite fts5 verification evidence atom",
            source_refs=("source-alpha#char:26-70",),
            trust_status="system_generated",
            base_score=0.6,
        ),
        RecallIndexEntry(
            object_id="foreign-beta",
            project_id="project-beta",
            layer="l1_atom",
            content="project recall evidence sqlite fts5 verification",
            source_refs=("source-beta#char:0-30",),
            trust_status="trusted",
            base_score=1.0,
        ),
    )


def _pending_rebuild_job(tmp_path: Path):
    object_store = _store(tmp_path)
    jobs = ObjectStoreJobRepository(object_store)
    selection = select_default_recall_backend_policy()
    request = create_index_rebuild_request(
        freshness=evaluate_index_freshness(None, _sources()),
        backend_selection=selection,
        sources=_sources(),
        requested_at="2026-07-01T02:32:00+08:00",
    )
    manifest = create_sqlite_fts5_manifest(
        rebuild_request=request,
        backend_selection=selection,
        created_at="2026-07-01T02:33:00+08:00",
    )
    result = CreateIndexRebuildJob(jobs=jobs).execute(
        rebuild_request=request,
        candidate_manifest=manifest,
        created_at="2026-07-01T02:34:00+08:00",
    )
    return object_store, jobs, result, sqlite_fts5_manifest_payload(manifest)


def _verified_dry_run(tmp_path: Path, manifest: dict[str, object]):
    return SqliteFts5DryRunIndex(tmp_path / ".sqlite-dry-run" / "recall_fts5.db").rebuild_and_query(
        _entries(),
        manifest=manifest,
        query=RecallQuery(
            text="project recall evidence sqlite",
            project_id="project-alpha",
            layers=("l3_project_skill", "l1_atom"),
            allowed_trust_statuses=("user_confirmed", "system_generated"),
            limit=5,
        ),
    )


def test_index_rebuild_job_resume_guard_rejects_unverified_worker_result_without_outputs(
    tmp_path: Path,
) -> None:
    _, jobs, handoff, _ = _pending_rebuild_job(tmp_path)

    with pytest.raises(IndexRebuildJobResumeError, match="must be ready"):
        ResumeVerifiedIndexRebuildJob(jobs=jobs).execute(
            job_id=handoff.job_id,
            verification={
                "status": "degraded",
                "backend_kind": "sqlite_fts5",
                "database_uri": "file:///tmp/not-ready.db",
                "manifest_id": "sqlite_fts5_candidate",
                "entry_count": 3,
                "hit_count": 2,
                "vector_enabled": False,
                "source_refs": ["source-alpha#char:0-25"],
            },
            verified_at="2026-07-01T02:35:00+08:00",
        )

    stored = jobs.get(handoff.job_id)
    assert stored is not None
    assert stored["status"] == "failed"
    assert stored["published_outputs"] == []
    assert stored["error"]["code"] == "index_verification_failed"
    assert validate_contract_instance("job.schema.json", _schema("job.schema.json"), stored) == []
    assert not (tmp_path / "library").exists()


def test_index_rebuild_job_completes_only_after_verified_dry_run_and_keeps_active_index(
    tmp_path: Path,
) -> None:
    object_store, jobs, handoff, manifest = _pending_rebuild_job(tmp_path)
    active_index = ObjectStoreRecallIndex(object_store)
    active_manifest = active_index.rebuild(
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
        rebuilt_at="2026-07-01T02:36:00+08:00",
    )
    verification = _verified_dry_run(tmp_path, manifest)

    result = ResumeVerifiedIndexRebuildJob(jobs=jobs).execute(
        job_id=handoff.job_id,
        verification=verification,
        verified_at="2026-07-01T02:37:00+08:00",
    )
    stored = jobs.get(handoff.job_id)

    assert stored == result.job
    assert stored is not None
    assert stored["status"] == "completed"
    assert stored["progress"]["percent"] == 100
    assert stored["staged_outputs"] == []
    assert stored["published_outputs"] == [
        {
            "kind": "other",
            "uri": f"crp://default/recall/index-verifications/{handoff.job_id}",
            "object_id": "verified-sqlite_fts5_candidate",
            "published": True,
        }
    ]
    assert result.published_output_uri == f"crp://default/recall/index-verifications/{handoff.job_id}"
    assert [step["status"] for step in stored["steps"]] == ["completed", "completed", "completed"]
    assert active_index.manifest() == active_manifest
    assert active_index.manifest()["backend_kind"] == "object_store_lexical"
    assert validate_contract_instance("job.schema.json", _schema("job.schema.json"), stored) == []
    assert not (tmp_path / "library").exists()


def test_index_rebuild_job_resume_guard_is_idempotent_after_completion(tmp_path: Path) -> None:
    _, jobs, handoff, manifest = _pending_rebuild_job(tmp_path)
    verification = _verified_dry_run(tmp_path, manifest)
    use_case = ResumeVerifiedIndexRebuildJob(jobs=jobs)

    first = use_case.execute(
        job_id=handoff.job_id,
        verification=verification,
        verified_at="2026-07-01T02:38:00+08:00",
    )
    second = use_case.execute(
        job_id=handoff.job_id,
        verification=verification,
        verified_at="2026-07-01T02:39:00+08:00",
    )

    assert second == first
    assert jobs.get(handoff.job_id)["published_outputs"] == first.job["published_outputs"]
    assert not (tmp_path / "library").exists()
