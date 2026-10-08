from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from core.job_runner import ObjectStoreJobRepository
from core.product_core.memory_projection_observability import (
    memory_projection_diagnostics_payload,
    memory_retrieval_settings_payload,
)
from core.product_core.memory_projection_authority_contract import (
    MemoryProjectionAuthoritySnapshot,
    authority_snapshot_fingerprint,
)
from core.product_core.memory_projection_rebuild_job import (
    CreateMemoryProjectionRebuildJob,
    run_memory_projection_rebuild_job,
)
from core.product_core.memory_projection_repository import (
    ObjectStoreMemoryProjectionRepository,
)
from core.storage_provider import JsonObjectStore


class _Authority:
    def __init__(self, snapshot: MemoryProjectionAuthoritySnapshot) -> None:
        self.snapshot = snapshot

    def load(self, project_id: str) -> MemoryProjectionAuthoritySnapshot:
        assert project_id == self.snapshot.project_id
        return self.snapshot


class _Effects:
    def __init__(self, operation_id: str) -> None:
        self.operation_id = operation_id

    def get(self, operation_id: str):
        if operation_id != self.operation_id:
            return None
        return SimpleNamespace(
            operation_id=operation_id,
            state=SimpleNamespace(value="SETTLED_OK"),
            result_ref=f"receipt:memory-projection-rebuild/{operation_id}",
            error_ref=None,
        )


def _snapshot(revision: int = 1) -> MemoryProjectionAuthoritySnapshot:
    source_refs = ({"source_id": "PRIVATE-SOURCE", "locator": "private://locator"},)
    return MemoryProjectionAuthoritySnapshot(
        project_id="project-1",
        authority_identity="progressive-memory-authority-v1:test:test",
        series_memories=({
            "id": "series-memory-1",
            "series_id": "series-1",
            "scope": "project",
            "overview": "PRIVATE-CONTENT",
            "scenario_ids": ["scenario-1"],
            "source_refs": source_refs,
            "project_ids": ["project-1"],
            "stale": False,
            "revision": revision,
            "trust_status": "user_confirmed",
        },),
        scenarios=({
            "id": "scenario-1",
            "title": "PRIVATE SCENARIO",
            "summary": "PRIVATE SUMMARY",
            "atom_ids": ["atom-1"],
            "source_refs": source_refs,
            "series_id": "series-1",
            "project_id": "project-1",
            "stale": False,
            "revision": 1,
            "trust_status": "user_confirmed",
        },),
        atoms=({
            "id": "atom-1",
            "source_id": "PRIVATE-SOURCE",
            "content": "PRIVATE ATOM",
            "atom_type": "decision",
            "source_refs": source_refs,
            "revision": 1,
            "trust_status": "user_confirmed",
        },),
        project_skills=(),
    )


def _runtime(tmp_path: Path):
    store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    return (
        ObjectStoreMemoryProjectionRepository(store),
        ObjectStoreJobRepository(store),
    )


def test_settings_are_user_facing_and_read_only(tmp_path: Path) -> None:
    projections, _ = _runtime(tmp_path)
    payload = memory_retrieval_settings_payload(
        project_id="project-1",
        authority=_Authority(_snapshot()),
        projections=projections,
    )

    assert payload["status"]["status"] == "preparing"
    assert payload["automatic_organization"]["blocks_first_answer"] is False
    assert [item["label"] for item in payload["reading_order"]] == [
        "项目总览", "系列概况", "详细资料", "原始来源",
    ]
    assert payload["privacy"]["team_memory_included"] is False


def test_diagnostics_report_safe_metrics_and_completed_job(tmp_path: Path) -> None:
    projections, jobs = _runtime(tmp_path)
    snapshot = _snapshot()
    authority = _Authority(snapshot)
    created = CreateMemoryProjectionRebuildJob(
        jobs=jobs,
        projections=projections,
    ).execute(
        project_id=snapshot.project_id,
        authority_identity=snapshot.authority_identity,
        authority_fingerprint=authority_snapshot_fingerprint(snapshot),
        created_at="2026-07-26T21:00:00+08:00",
    )
    run_memory_projection_rebuild_job(
        jobs=jobs,
        projections=projections,
        authority=authority,
        job_id=created.job_id,
        now="2026-07-26T21:01:00+08:00",
    )

    payload = memory_projection_diagnostics_payload(
        project_id="project-1",
        authority=authority,
        projections=projections,
        effects=_Effects(created.job_id),
    )
    serialized = json.dumps(payload, ensure_ascii=False)

    assert payload["public_status"]["status"] == "ready"
    assert payload["projection"]["metrics"]["series_count"] == 1
    assert payload["latest_rebuild"]["status"] == "completed"
    assert payload["actions"]["refresh_available"] is False
    assert "PRIVATE-CONTENT" not in serialized
    assert "PRIVATE-SOURCE" not in serialized
    assert "private://locator" not in serialized
    assert '"source_id"' not in serialized
    assert '"locator"' not in serialized


def test_stale_diagnostics_allow_explicit_refresh(tmp_path: Path) -> None:
    projections, jobs = _runtime(tmp_path)
    original = _snapshot(revision=1)
    authority = _Authority(original)
    created = CreateMemoryProjectionRebuildJob(jobs=jobs, projections=projections).execute(
        project_id=original.project_id,
        authority_identity=original.authority_identity,
        authority_fingerprint=authority_snapshot_fingerprint(original),
    )
    run_memory_projection_rebuild_job(
        jobs=jobs,
        projections=projections,
        authority=authority,
        job_id=created.job_id,
    )
    authority.snapshot = _snapshot(revision=2)

    payload = memory_projection_diagnostics_payload(
        project_id="project-1",
        authority=authority,
        projections=projections,
        effects=_Effects(created.job_id),
    )

    assert payload["public_status"]["status"] == "needs_refresh"
    assert payload["actions"]["refresh_available"] is True
    assert payload["projection"]["authority_fingerprint_hint"]
