from __future__ import annotations

from pathlib import Path

from core.composition import build_product_runtime_readiness
from core.ingestion_core import ObjectStoreSourceRegistrar
from core.job_runner import ObjectStoreJobRepository, SQLiteJobStore
from core.product_core import OrchestrateWorkbenchAutoIntake
from core.storage_provider import JsonObjectStore


def test_workbench_auto_intake_persists_a_legacy_json_job_without_sqlite_worker_authority(tmp_path) -> None:
    store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    orchestrator = OrchestrateWorkbenchAutoIntake(
        object_store=store,
        source_registrar=ObjectStoreSourceRegistrar(store),
        job_repository=ObjectStoreJobRepository(store),
        fetch_url=lambda _url: "",
    )

    result = orchestrator.execute(content="Characterize the current user intake job.", title="Producer characterization")

    job = store.read("jobs", result.job_id)
    assert job is not None
    assert job["job_type"] == "workbench_auto_intake"
    assert job["lease"] is None
    assert job["checkpoint"] is None
    assert not (tmp_path / ".rebuild-data" / "jobs.sqlite3").exists()


def test_runtime_readiness_is_a_controlled_sqlite_extract_memory_producer(tmp_path) -> None:
    repository_root = Path(__file__).resolve().parents[2]

    readiness = build_product_runtime_readiness(repository_root, runtime_root=tmp_path).execute()
    sqlite_jobs = SQLiteJobStore(tmp_path / ".rebuild-data" / "jobs.sqlite3").all()
    legacy_jobs = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library").list("jobs")

    assert readiness.status == "ready"
    assert {record.payload["job_type"] for record in sqlite_jobs} == {"extract_memory"}
    assert {record.payload["status"] for record in sqlite_jobs} == {"completed"}
    assert legacy_jobs == ()
