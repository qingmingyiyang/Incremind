from __future__ import annotations

import json
from pathlib import Path

from core.product_core.memory_projection_authority_contract import (
    MemoryProjectionAuthoritySnapshot,
    authority_snapshot_fingerprint,
)
from core.product_core.memory_projection_repository import (
    ObjectStoreMemoryProjectionRepository,
)
from core.product_core.project_brain_projection_status import (
    get_project_brain_projection_status,
)
from core.storage_provider import JsonObjectStore


NOW = "2026-07-26T15:00:00+08:00"


class _Authority:
    def __init__(self, snapshot: MemoryProjectionAuthoritySnapshot) -> None:
        self.snapshot = snapshot

    def load(self, project_id: str) -> MemoryProjectionAuthoritySnapshot:
        assert project_id == self.snapshot.project_id
        return self.snapshot


def _snapshot(revision: int = 1) -> MemoryProjectionAuthoritySnapshot:
    return MemoryProjectionAuthoritySnapshot(
        project_id="project-1",
        authority_identity="progressive-memory-authority-v1:test:test",
        series_memories=(
            {
                "id": "series-memory-1",
                "series_id": "memory-system",
                "overview": "按系列概况和详细资料组织长期记忆。",
                "scenario_ids": ["scenario-1"],
                "source_refs": [
                    {"source_id": "source-1", "locator": "section:overview"}
                ],
                "project_ids": ["project-1"],
                "stale": False,
                "revision": revision,
                "trust_status": "user_confirmed",
            },
        ),
        scenarios=(
            {
                "id": "scenario-1",
                "title": "记忆整理",
                "summary": "先判断系列，再读取细节。",
                "atom_ids": ["atom-1"],
                "source_refs": [
                    {"source_id": "source-1", "locator": "section:scenario"},
                    {"source_id": "source-2", "locator": "section:scenario"},
                ],
                "tags": ["记忆"],
                "series_id": "memory-system",
                "project_id": "project-1",
                "stale": False,
                "revision": 1,
                "trust_status": "trusted",
            },
        ),
        atoms=(
            {
                "id": "atom-1",
                "source_id": "source-1",
                "content": "概况是可重建派生数据。",
                "atom_type": "decision",
                "tags": ["概况"],
                "source_refs": [
                    {"source_id": "source-1", "locator": "section:atom"}
                ],
                "revision": 1,
                "trust_status": "trusted",
            },
        ),
        project_skills=(),
    )


def _repository(tmp_path: Path) -> ObjectStoreMemoryProjectionRepository:
    return ObjectStoreMemoryProjectionRepository(
        JsonObjectStore(
            tmp_path / ".rebuild-data",
            legacy_root=tmp_path / "library",
        )
    )


def _activate(
    repository: ObjectStoreMemoryProjectionRepository,
    snapshot: MemoryProjectionAuthoritySnapshot,
) -> None:
    fingerprint = authority_snapshot_fingerprint(snapshot)
    repository.begin_rebuild(
        project_id=snapshot.project_id,
        authority_identity=snapshot.authority_identity,
        authority_fingerprint=fingerprint,
        job_id="projection-job-1",
        updated_at=NOW,
    )
    artifact_id = repository.stage_projection(snapshot.build(generated_at=NOW))
    repository.activate_staged(
        project_id=snapshot.project_id,
        authority_identity=snapshot.authority_identity,
        authority_fingerprint=fingerprint,
        job_id="projection-job-1",
        artifact_id=artifact_id,
        updated_at=NOW,
    )


def test_missing_projection_returns_user_facing_preparing_status(
    tmp_path: Path,
) -> None:
    result = get_project_brain_projection_status(
        project_id="project-1",
        authority=_Authority(_snapshot()),
        projections=_repository(tmp_path),
    ).to_payload()

    assert result["status"] == "preparing"
    assert result["label"] == "项目概况正在整理"
    assert result["source"]["label"] == "已发布项目记忆"
    assert result["generated_at"] is None


def test_fresh_projection_reports_series_sources_and_generation_time(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    snapshot = _snapshot()
    _activate(repository, snapshot)

    result = get_project_brain_projection_status(
        project_id="project-1",
        authority=_Authority(snapshot),
        projections=repository,
    ).to_payload()

    assert result["status"] == "ready"
    assert result["source"] == {
        "kind": "published_project_memory",
        "label": "已发布项目记忆",
        "series_count": 1,
        "source_count": 2,
    }
    assert result["generated_at"] == NOW


def test_changed_authority_reports_needs_refresh_without_old_counts(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    _activate(repository, _snapshot(revision=1))

    result = get_project_brain_projection_status(
        project_id="project-1",
        authority=_Authority(_snapshot(revision=2)),
        projections=repository,
    ).to_payload()

    assert result["status"] == "needs_refresh"
    assert result["source"]["series_count"] == 0
    assert result["source"]["source_count"] == 0
    assert result["generated_at"] is None


def test_projection_status_payload_excludes_content_locator_path_and_url(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    snapshot = _snapshot()
    _activate(repository, snapshot)
    payload = get_project_brain_projection_status(
        project_id="project-1",
        authority=_Authority(snapshot),
        projections=repository,
    ).to_payload()
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)

    assert "按系列概况" not in serialized
    assert "section:overview" not in serialized
    assert '"source_id"' not in serialized
    assert '"locator"' not in serialized
    assert "http://" not in serialized
    assert "https://" not in serialized
