from __future__ import annotations

from core.ingestion_core import DeterministicSourceRegistrar
from core.job_runner import InMemoryJobRepository
from core.memory_core import InMemoryMemoryStore
from core.product_core import GetProductRuntimeReadiness, SourceJobMemoryLoop


def test_runtime_readiness_reports_ready_for_fixture_loop() -> None:
    source_registrar = DeterministicSourceRegistrar()
    job_repository = InMemoryJobRepository()
    memory_store = InMemoryMemoryStore()
    loop = SourceJobMemoryLoop(
        source_registrar=source_registrar,
        job_repository=job_repository,
        memory_reader=memory_store,
        memory_writer=memory_store,
    )

    readiness = GetProductRuntimeReadiness(
        loop=loop,
        jobs=job_repository,
        memory=memory_store,
    ).execute()

    assert readiness.status == "ready"
    assert [check.name for check in readiness.checks] == [
        "completed_multi_atom_publish",
        "checkpoint_staging_boundary",
        "resume_publish_idempotent",
        "series_memory_merge",
    ]
