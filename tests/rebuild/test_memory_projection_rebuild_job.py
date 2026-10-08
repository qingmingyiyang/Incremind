from __future__ import annotations

import copy
import hashlib
import json
import multiprocessing
import os
from pathlib import Path

import pytest

import core.product_core.memory_projection_rebuild_job as legacy_job_module
from core.job_runner import (
    InMemoryJobRepository,
    ObjectStoreJobRepository,
)
from core.product_core.memory_projection_rebuild_job import (
    CreateMemoryProjectionRebuildJob,
    MemoryProjectionRebuildRuntimeError,
    memory_projection_rebuild_job_id,
    recover_memory_projection_rebuild_jobs,
    run_memory_projection_rebuild_job,
)
from core.product_core.memory_projection_authority_contract import (
    MemoryProjectionAuthoritySnapshot,
    MemoryProjectionAuthoritySnapshotPort,
    authority_snapshot_fingerprint,
)
from core.product_core.memory_projection_contract import (
    GENERATOR_POLICY_ID,
    PROJECTION_VERSION,
)
from core.product_core.memory_projection_repository import (
    ARTIFACT_COLLECTION,
    FAILURE_COLLECTION,
    ObjectStoreMemoryProjectionRepository,
)
from core.storage_provider import JsonObjectStore
from tools.validate_rebuild_contracts import validate_contract_instance


ROOT = Path(__file__).resolve().parents[2]
JOB_SCHEMA_PATH = ROOT / "core-contracts" / "rebuild" / "job.schema.json"
CREATED_AT = "2026-07-26T13:00:00+08:00"
AUTHORITY_IDENTITY = "sqlite:structured-records-v1"


def test_legacy_job_module_reexports_authority_contract() -> None:
    assert (
        legacy_job_module.MemoryProjectionAuthoritySnapshot
        is MemoryProjectionAuthoritySnapshot
    )
    assert (
        legacy_job_module.MemoryProjectionAuthoritySnapshotPort
        is MemoryProjectionAuthoritySnapshotPort
    )
    assert (
        legacy_job_module.authority_snapshot_fingerprint
        is authority_snapshot_fingerprint
    )


def _store(tmp_path: Path) -> JsonObjectStore:
    return JsonObjectStore(
        tmp_path / ".rebuild-data",
        legacy_root=tmp_path / "library",
    )


def _snapshot(
    *,
    revision: int = 1,
    overview: str = "Phase C 保存非权威投影并在过期时回退。",
    generation_token: str | None = None,
) -> MemoryProjectionAuthoritySnapshot:
    series = {
        "id": "series-memory-1",
        "series_id": "memory-system",
        "overview": overview,
        "scenario_ids": ["scenario-1"],
        "source_refs": [{"source_id": "source-1", "locator": "section:memory"}],
        "project_ids": ["project-1"],
        "stale": False,
        "revision": revision,
        "trust_status": "user_confirmed",
    }
    scenario = {
        "id": "scenario-1",
        "title": "投影重建",
        "summary": "构建、校验、落盘后再切换当前指针。",
        "atom_ids": ["atom-1"],
        "source_refs": [{"source_id": "source-1", "locator": "section:rebuild"}],
        "tags": ["重建"],
        "series_id": "memory-system",
        "project_id": "project-1",
        "stale": False,
        "revision": 1,
        "trust_status": "trusted",
    }
    atom = {
        "id": "atom-1",
        "source_id": "source-1",
        "content": "过期、缺失或损坏投影不能进入回答。",
        "atom_type": "decision",
        "tags": ["回退"],
        "source_refs": [{"source_id": "source-1", "locator": "section:fallback"}],
        "revision": 1,
        "trust_status": "trusted",
    }
    return MemoryProjectionAuthoritySnapshot(
        project_id="project-1",
        authority_identity=AUTHORITY_IDENTITY,
        series_memories=(series,),
        scenarios=(scenario,),
        atoms=(atom,),
        project_skills=(),
        authority_generation_token=generation_token,
    )


class _MutableAuthority:
    def __init__(
        self,
        snapshot: MemoryProjectionAuthoritySnapshot,
    ) -> None:
        self.snapshot = snapshot
        self.sequence: list[MemoryProjectionAuthoritySnapshot] = []
        self.failures_remaining = 0
        self.load_calls = 0

    def load(self, project_id: str) -> MemoryProjectionAuthoritySnapshot:
        self.load_calls += 1
        if self.failures_remaining > 0:
            self.failures_remaining -= 1
            raise RuntimeError("injected snapshot failure")
        if self.sequence:
            return self.sequence.pop(0)
        assert project_id == self.snapshot.project_id
        return copy.deepcopy(self.snapshot)


def _create(
    *,
    jobs,
    projections: ObjectStoreMemoryProjectionRepository,
    snapshot: MemoryProjectionAuthoritySnapshot,
):
    return CreateMemoryProjectionRebuildJob(
        jobs=jobs,
        projections=projections,
    ).execute(
        project_id=snapshot.project_id,
        authority_identity=snapshot.authority_identity,
        authority_fingerprint=authority_snapshot_fingerprint(snapshot),
        created_at=CREATED_AT,
    )


def _crash_after_artifact(runtime_root: str, job_id: str) -> None:
    root = Path(runtime_root)
    store = _store(root)
    run_memory_projection_rebuild_job(
        jobs=ObjectStoreJobRepository(store),
        projections=ObjectStoreMemoryProjectionRepository(store),
        authority=_MutableAuthority(_snapshot()),
        job_id=job_id,
        now="2026-07-26T13:01:00+08:00",
        after_artifact_persisted=lambda: os._exit(73),
    )


def test_handoff_is_deterministic_and_does_not_build_projection(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    jobs = ObjectStoreJobRepository(store)
    projections = ObjectStoreMemoryProjectionRepository(store)
    snapshot = _snapshot()
    first = _create(jobs=jobs, projections=projections, snapshot=snapshot)
    second = _create(jobs=jobs, projections=projections, snapshot=snapshot)
    schema = json.loads(JOB_SCHEMA_PATH.read_text(encoding="utf-8"))

    assert first.replayed is False
    assert second.replayed is True
    assert first.job_id == second.job_id
    assert first.job == second.job
    assert first.job["status"] == "pending"
    assert first.manifest["status"] == "rebuilding"
    assert store.list(ARTIFACT_COLLECTION) == ()
    assert validate_contract_instance(
        JOB_SCHEMA_PATH.name,
        schema,
        first.job,
    ) == []


def test_job_identity_includes_namespace_and_projection_policy() -> None:
    snapshot = _snapshot()
    fingerprint = authority_snapshot_fingerprint(snapshot)
    identity_parts = (
        "default",
        snapshot.project_id,
        snapshot.authority_identity,
        fingerprint,
        PROJECTION_VERSION,
        GENERATOR_POLICY_ID,
    )
    digest = hashlib.sha256(
        "\n".join(identity_parts).encode("utf-8")
    ).hexdigest()

    job_id = memory_projection_rebuild_job_id(
        project_id=snapshot.project_id,
        authority_identity=snapshot.authority_identity,
        authority_fingerprint=fingerprint,
    )

    assert job_id == f"job-memory-projection-{digest[:32]}"


def test_worker_builds_activates_and_replays_completed_job(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    jobs = ObjectStoreJobRepository(store)
    projections = ObjectStoreMemoryProjectionRepository(store)
    snapshot = _snapshot()
    authority = _MutableAuthority(snapshot)
    handoff = _create(jobs=jobs, projections=projections, snapshot=snapshot)

    first = run_memory_projection_rebuild_job(
        jobs=jobs,
        projections=projections,
        authority=authority,
        job_id=handoff.job_id,
        now="2026-07-26T13:01:00+08:00",
    )
    second = run_memory_projection_rebuild_job(
        jobs=jobs,
        projections=projections,
        authority=authority,
        job_id=handoff.job_id,
        now="2026-07-26T13:02:00+08:00",
    )
    current = projections.load_current(
        project_id=snapshot.project_id,
        authority_identity=snapshot.authority_identity,
        authority_fingerprint=authority_snapshot_fingerprint(snapshot),
    )
    schema = json.loads(JOB_SCHEMA_PATH.read_text(encoding="utf-8"))

    assert first.job["status"] == "completed"
    assert second.replayed is True
    assert second.job == first.job
    assert authority.load_calls == 2
    assert current.status == "fresh"
    assert current.projection is not None
    assert len(store.list(ARTIFACT_COLLECTION)) == 1
    assert validate_contract_instance(
        JOB_SCHEMA_PATH.name,
        schema,
        first.job,
    ) == []


def test_worker_binds_current_authority_generation_after_activation(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    jobs = ObjectStoreJobRepository(store)
    projections = ObjectStoreMemoryProjectionRepository(store)
    snapshot = _snapshot(generation_token="e" * 64)
    handoff = _create(jobs=jobs, projections=projections, snapshot=snapshot)

    run_memory_projection_rebuild_job(
        jobs=jobs,
        projections=projections,
        authority=_MutableAuthority(snapshot),
        job_id=handoff.job_id,
        now="2026-07-26T13:01:00+08:00",
    )

    current = projections.load_current_for_generation(
        project_id=snapshot.project_id,
        authority_identity=snapshot.authority_identity,
        authority_generation_token="e" * 64,
    )
    assert current is not None
    fingerprint, read_result = current
    assert fingerprint == authority_snapshot_fingerprint(snapshot)
    assert read_result.status == "fresh"


def test_confirmed_repair_reopens_completed_job_for_same_fingerprint(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    jobs = ObjectStoreJobRepository(store)
    projections = ObjectStoreMemoryProjectionRepository(store)
    snapshot = _snapshot()
    authority = _MutableAuthority(snapshot)
    first = _create(jobs=jobs, projections=projections, snapshot=snapshot)
    completed = run_memory_projection_rebuild_job(
        jobs=jobs,
        projections=projections,
        authority=authority,
        job_id=first.job_id,
        now="2026-07-26T13:01:00+08:00",
    )
    artifact_id = completed.manifest["active_artifact_id"]
    artifact = dict(store.read(ARTIFACT_COLLECTION, artifact_id) or {})
    artifact["projection_digest"] = "0" * 64
    store.write(
        ARTIFACT_COLLECTION,
        artifact_id,
        artifact,
        expected_revision=1,
    )
    projections.discard_corrupt_current(
        project_id=snapshot.project_id,
        authority_identity=snapshot.authority_identity,
        authority_fingerprint=authority_snapshot_fingerprint(snapshot),
    )

    reopened = CreateMemoryProjectionRebuildJob(
        jobs=jobs,
        projections=projections,
    ).execute(
        project_id=snapshot.project_id,
        authority_identity=snapshot.authority_identity,
        authority_fingerprint=authority_snapshot_fingerprint(snapshot),
        created_at="2026-07-26T13:02:00+08:00",
        reopen_completed=True,
    )
    repaired = run_memory_projection_rebuild_job(
        jobs=jobs,
        projections=projections,
        authority=authority,
        job_id=reopened.job_id,
        now="2026-07-26T13:03:00+08:00",
    )

    assert reopened.replayed is False
    assert reopened.job["status"] == "pending"
    assert reopened.job["attempt"] == completed.job["attempt"]
    assert repaired.job["status"] == "completed"
    assert repaired.job["attempt"] == completed.job["attempt"] + 1
    assert projections.load_current(
        project_id=snapshot.project_id,
        authority_identity=snapshot.authority_identity,
        authority_fingerprint=authority_snapshot_fingerprint(snapshot),
    ).status == "fresh"


def test_authority_drift_fails_without_activating_candidate(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    jobs = ObjectStoreJobRepository(store)
    projections = ObjectStoreMemoryProjectionRepository(store)
    initial = _snapshot()
    changed = _snapshot(
        revision=2,
        overview="激活前发生了新的权威 revision。",
    )
    authority = _MutableAuthority(initial)
    authority.sequence = [initial, changed]
    handoff = _create(jobs=jobs, projections=projections, snapshot=initial)

    with pytest.raises(
        MemoryProjectionRebuildRuntimeError,
        match="activate_projection",
    ):
        run_memory_projection_rebuild_job(
            jobs=jobs,
            projections=projections,
            authority=authority,
            job_id=handoff.job_id,
            now="2026-07-26T13:01:00+08:00",
        )

    failed = jobs.get(handoff.job_id)
    current = projections.load_current(
        project_id=initial.project_id,
        authority_identity=initial.authority_identity,
        authority_fingerprint=authority_snapshot_fingerprint(initial),
    )

    assert failed is not None
    assert failed["status"] == "failed"
    assert failed["error"]["code"] == "authority_snapshot_drift"
    assert current.fallback_to_authority is True
    assert current.projection is None
    assert len(store.list(FAILURE_COLLECTION)) == 1
    failure = store.list(FAILURE_COLLECTION)[0]
    assert failure["contains_projection_body"] is False
    assert "projection" not in failure


def test_failed_job_retries_same_identity_and_completes(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    jobs = ObjectStoreJobRepository(store)
    projections = ObjectStoreMemoryProjectionRepository(store)
    snapshot = _snapshot()
    authority = _MutableAuthority(snapshot)
    authority.failures_remaining = 1
    handoff = _create(jobs=jobs, projections=projections, snapshot=snapshot)

    with pytest.raises(MemoryProjectionRebuildRuntimeError):
        run_memory_projection_rebuild_job(
            jobs=jobs,
            projections=projections,
            authority=authority,
            job_id=handoff.job_id,
            now="2026-07-26T13:01:00+08:00",
        )
    retried = run_memory_projection_rebuild_job(
        jobs=jobs,
        projections=projections,
        authority=authority,
        job_id=handoff.job_id,
        now="2026-07-26T13:02:00+08:00",
    )

    assert retried.job_id == handoff.job_id
    assert retried.job["status"] == "completed"
    assert retried.job["attempt"] == 2
    assert len(store.list(ARTIFACT_COLLECTION)) == 1


def test_restart_recovers_after_artifact_persisted_before_activation(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    jobs = ObjectStoreJobRepository(store)
    projections = ObjectStoreMemoryProjectionRepository(store)
    snapshot = _snapshot()
    authority = _MutableAuthority(snapshot)
    handoff = _create(jobs=jobs, projections=projections, snapshot=snapshot)

    with pytest.raises(SystemExit) as interrupted:
        run_memory_projection_rebuild_job(
            jobs=jobs,
            projections=projections,
            authority=authority,
            job_id=handoff.job_id,
            now="2026-07-26T13:01:00+08:00",
            after_artifact_persisted=lambda: (_ for _ in ()).throw(SystemExit(79)),
        )

    assert interrupted.value.code == 79
    stored = jobs.get(handoff.job_id)
    assert stored is not None
    assert stored["status"] == "running"
    assert stored["checkpoint"]["resume_step"] == "activate_projection"
    assert len(store.list(ARTIFACT_COLLECTION)) == 1

    restarted_store = _store(tmp_path)
    restarted_jobs = ObjectStoreJobRepository(restarted_store)
    restarted_projections = ObjectStoreMemoryProjectionRepository(
        restarted_store
    )
    recovered = recover_memory_projection_rebuild_jobs(
        jobs=restarted_jobs,
        projections=restarted_projections,
        authority=_MutableAuthority(snapshot),
    )

    assert recovered == (handoff.job_id,)
    completed = restarted_jobs.get(handoff.job_id)
    assert completed is not None
    assert completed["status"] == "completed"
    assert completed["attempt"] == 2
    assert restarted_projections.load_current(
        project_id=snapshot.project_id,
        authority_identity=snapshot.authority_identity,
        authority_fingerprint=authority_snapshot_fingerprint(snapshot),
    ).status == "fresh"


def test_real_child_process_exit_is_recovered_from_durable_checkpoint(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    jobs = ObjectStoreJobRepository(store)
    projections = ObjectStoreMemoryProjectionRepository(store)
    snapshot = _snapshot()
    handoff = _create(jobs=jobs, projections=projections, snapshot=snapshot)
    process = multiprocessing.get_context("spawn").Process(
        target=_crash_after_artifact,
        args=(str(tmp_path), handoff.job_id),
    )

    process.start()
    process.join(timeout=30)

    assert process.exitcode == 73
    interrupted = jobs.get(handoff.job_id)
    assert interrupted is not None
    assert interrupted["status"] == "running"
    assert interrupted["checkpoint"]["resume_step"] == "activate_projection"

    recovered = recover_memory_projection_rebuild_jobs(
        jobs=ObjectStoreJobRepository(_store(tmp_path)),
        projections=ObjectStoreMemoryProjectionRepository(_store(tmp_path)),
        authority=_MutableAuthority(snapshot),
    )

    assert recovered == (handoff.job_id,)
    completed = ObjectStoreJobRepository(_store(tmp_path)).get(handoff.job_id)
    assert completed is not None
    assert completed["status"] == "completed"


def test_startup_reconstructs_job_missing_after_manifest_commit(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    jobs = ObjectStoreJobRepository(store)
    projections = ObjectStoreMemoryProjectionRepository(store)
    snapshot = _snapshot()
    fingerprint = authority_snapshot_fingerprint(snapshot)
    job_id = memory_projection_rebuild_job_id(
        project_id=snapshot.project_id,
        authority_identity=snapshot.authority_identity,
        authority_fingerprint=fingerprint,
    )
    projections.begin_rebuild(
        project_id=snapshot.project_id,
        authority_identity=snapshot.authority_identity,
        authority_fingerprint=fingerprint,
        job_id=job_id,
        updated_at=CREATED_AT,
    )

    assert jobs.get(job_id) is None
    recovered = recover_memory_projection_rebuild_jobs(
        jobs=jobs,
        projections=projections,
        authority=_MutableAuthority(snapshot),
    )

    assert recovered == (job_id,)
    assert jobs.get(job_id)["status"] == "completed"


def test_retry_budget_is_bounded(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    jobs = ObjectStoreJobRepository(store)
    projections = ObjectStoreMemoryProjectionRepository(store)
    snapshot = _snapshot()
    authority = _MutableAuthority(snapshot)
    authority.failures_remaining = 3
    handoff = _create(jobs=jobs, projections=projections, snapshot=snapshot)

    for minute in range(1, 4):
        with pytest.raises(MemoryProjectionRebuildRuntimeError):
            run_memory_projection_rebuild_job(
                jobs=jobs,
                projections=projections,
                authority=authority,
                job_id=handoff.job_id,
                now=f"2026-07-26T13:0{minute}:00+08:00",
            )

    with pytest.raises(
        MemoryProjectionRebuildRuntimeError,
        match="exhausted retry budget",
    ):
        run_memory_projection_rebuild_job(
            jobs=jobs,
            projections=projections,
            authority=authority,
            job_id=handoff.job_id,
            now="2026-07-26T13:04:00+08:00",
        )
    assert jobs.get(handoff.job_id)["attempt"] == 3


def test_in_memory_job_repository_is_supported_for_deterministic_handoff(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    jobs = InMemoryJobRepository()
    projections = ObjectStoreMemoryProjectionRepository(store)
    snapshot = _snapshot()

    handoff = _create(jobs=jobs, projections=projections, snapshot=snapshot)

    assert handoff.job_id in {
        job["id"] for job in jobs.all()
    }
