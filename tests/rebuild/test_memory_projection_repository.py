from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from core.product_core.memory_projection_builder import (
    build_r0_r1_memory_projection,
)
from core.product_core.memory_projection_contract import (
    GENERATOR_POLICY_ID,
    PROJECTION_VERSION,
)
from core.product_core.memory_projection_repository import (
    ARTIFACT_COLLECTION,
    FAILURE_COLLECTION,
    GENERATION_COLLECTION,
    MANIFEST_COLLECTION,
    MemoryProjectionRepositoryConflict,
    MemoryProjectionRepositoryIntegrityError,
    ObjectStoreMemoryProjectionRepository,
    projection_manifest_id,
)
from core.storage_provider import (
    JsonObjectStore,
    ObjectStoreRevisionError,
)


GENERATED_AT = "2026-07-26T12:00:00+08:00"
AUTHORITY_IDENTITY = "sqlite:structured-records-v1"


def _store(tmp_path: Path) -> JsonObjectStore:
    return JsonObjectStore(
        tmp_path / ".rebuild-data",
        legacy_root=tmp_path / "library",
    )


def _projection(
    *,
    revision: int = 1,
    overview: str = "当前记忆架构采用权威平面与读取投影平面。",
    generated_at: str = GENERATED_AT,
):
    series = {
        "id": "series-memory-1",
        "series_id": "memory-system",
        "overview": overview,
        "scenario_ids": ["scenario-1"],
        "source_refs": [
            {
                "source_id": "source-1",
                "locator": "section:memory",
                "quote": "PRIVATE-SOURCE-BODY",
            }
        ],
        "project_ids": ["project-1"],
        "stale": False,
        "revision": revision,
        "trust_status": "user_confirmed",
    }
    scenario = {
        "id": "scenario-1",
        "title": "渐进召回",
        "summary": "先读取概况，再按需下钻。",
        "atom_ids": ["atom-1"],
        "source_refs": [{"source_id": "source-1", "locator": "section:recall"}],
        "tags": ["召回"],
        "series_id": "memory-system",
        "project_id": "project-1",
        "stale": False,
        "revision": 1,
        "trust_status": "trusted",
    }
    atom = {
        "id": "atom-1",
        "source_id": "source-1",
        "content": "投影过期时回退当前权威读取。",
        "atom_type": "decision",
        "tags": ["回退"],
        "source_refs": [{"source_id": "source-1", "locator": "section:fallback"}],
        "revision": 1,
        "trust_status": "trusted",
    }
    skill = {
        "id": "skill-1",
        "project_id": "project-1",
        "name": "记忆架构交付",
        "purpose": "按合同和证据分阶段交付。",
        "output_rules": [{"rule": "PRIVATE-SKILL-BODY"}],
        "revision": 1,
        "status": "active",
        "trust_status": "user_confirmed",
        "conflict": {"status": "none"},
    }
    return build_r0_r1_memory_projection(
        project_id="project-1",
        authority_identity=AUTHORITY_IDENTITY,
        series_memories=[series],
        scenarios=[scenario],
        atoms=[atom],
        project_skills=[skill],
        generated_at=generated_at,
    )


def _publish(
    repository: ObjectStoreMemoryProjectionRepository,
    projection,
    *,
    job_id: str,
    timestamp: str = GENERATED_AT,
) -> tuple[str, Mapping[str, object]]:
    repository.begin_rebuild(
        project_id=projection.project_id,
        authority_identity=projection.authority_identity,
        authority_fingerprint=projection.authority_fingerprint,
        job_id=job_id,
        updated_at=timestamp,
    )
    artifact_id = repository.stage_projection(projection)
    manifest = repository.activate_staged(
        project_id=projection.project_id,
        authority_identity=projection.authority_identity,
        authority_fingerprint=projection.authority_fingerprint,
        job_id=job_id,
        artifact_id=artifact_id,
        updated_at=timestamp,
    )
    return artifact_id, manifest


def test_repository_atomically_switches_pointer_and_retains_old_artifacts(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    repository = ObjectStoreMemoryProjectionRepository(store)
    first = _projection()
    first_artifact, first_manifest = _publish(
        repository,
        first,
        job_id="job-first",
    )
    fresh = repository.load_current(
        project_id="project-1",
        authority_identity=AUTHORITY_IDENTITY,
        authority_fingerprint=first.authority_fingerprint,
    )

    assert first_manifest["status"] == "ready"
    assert first_manifest["projection_version"] == PROJECTION_VERSION
    assert first_manifest["generator_policy_id"] == GENERATOR_POLICY_ID
    assert first_manifest["active_metrics"]["r0_item_count"] == 1
    assert first_manifest["active_metrics"]["r1_item_count"] == 1
    assert first_manifest["active_metrics"]["scenario_ref_count"] == 1
    assert first_manifest["active_metrics"]["atom_ref_count"] == 1
    assert first_manifest["active_metrics"]["project_skill_ref_count"] == 1
    assert first_manifest["active_metrics"]["content_length"] > 0
    assert first_manifest["active_metrics"]["limits_reached"] == {
        "scenario_per_series": False,
        "atom_per_series": False,
        "project_skill_per_project": False,
    }
    assert fresh.status == "fresh"
    assert fresh.fallback_to_authority is False
    assert fresh.projection is not None
    artifact = repository.artifact(
        project_id=first.project_id,
        authority_identity=first.authority_identity,
        authority_fingerprint=first.authority_fingerprint,
    )
    assert artifact is not None
    assert artifact["projection_version"] == PROJECTION_VERSION
    assert artifact["generator_policy_id"] == GENERATOR_POLICY_ID

    second = _projection(
        revision=2,
        overview="第二版权威记忆已经生效。",
        generated_at="2026-07-26T12:10:00+08:00",
    )
    repository.begin_rebuild(
        project_id="project-1",
        authority_identity=AUTHORITY_IDENTITY,
        authority_fingerprint=second.authority_fingerprint,
        job_id="job-second",
        updated_at="2026-07-26T12:10:00+08:00",
    )
    before_activation = repository.load_current(
        project_id="project-1",
        authority_identity=AUTHORITY_IDENTITY,
        authority_fingerprint=second.authority_fingerprint,
    )

    assert before_activation.status == "stale"
    assert before_activation.fallback_to_authority is True
    assert before_activation.projection is None

    second_artifact = repository.stage_projection(second)
    repository.activate_staged(
        project_id="project-1",
        authority_identity=AUTHORITY_IDENTITY,
        authority_fingerprint=second.authority_fingerprint,
        job_id="job-second",
        artifact_id=second_artifact,
        updated_at="2026-07-26T12:11:00+08:00",
    )

    assert first_artifact != second_artifact
    assert store.read(ARTIFACT_COLLECTION, first_artifact) is not None
    assert store.read(ARTIFACT_COLLECTION, second_artifact) is not None
    assert len(store.list(ARTIFACT_COLLECTION)) == 2
    assert repository.load_current(
        project_id="project-1",
        authority_identity=AUTHORITY_IDENTITY,
        authority_fingerprint=first.authority_fingerprint,
    ).projection is None
    assert repository.load_current(
        project_id="project-1",
        authority_identity=AUTHORITY_IDENTITY,
        authority_fingerprint=second.authority_fingerprint,
    ).status == "fresh"


def test_generation_binding_reads_fresh_projection_without_snapshot_fingerprint(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    repository = ObjectStoreMemoryProjectionRepository(store)
    projection = _projection()
    _publish(repository, projection, job_id="job-first")
    token = "a" * 64

    first = repository.bind_generation(
        project_id=projection.project_id,
        authority_identity=projection.authority_identity,
        authority_generation_token=token,
        authority_fingerprint=projection.authority_fingerprint,
    )
    replay = repository.bind_generation(
        project_id=projection.project_id,
        authority_identity=projection.authority_identity,
        authority_generation_token=token,
        authority_fingerprint=projection.authority_fingerprint,
    )
    current = repository.load_current_for_generation(
        project_id=projection.project_id,
        authority_identity=projection.authority_identity,
        authority_generation_token=token,
    )

    assert replay == first
    assert len(store.list(GENERATION_COLLECTION)) == 1
    assert current is not None
    assert current[0] == projection.authority_fingerprint
    assert current[1].status == "fresh"
    assert repository.load_current_for_generation(
        project_id=projection.project_id,
        authority_identity=projection.authority_identity,
        authority_generation_token="b" * 64,
    ) is None


def test_rollback_fingerprint_reuses_immutable_old_artifact(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    repository = ObjectStoreMemoryProjectionRepository(store)
    first = _projection()
    first_artifact, _ = _publish(repository, first, job_id="job-first")
    second = _projection(
        revision=2,
        overview="临时第二版。",
        generated_at="2026-07-26T12:10:00+08:00",
    )
    _publish(repository, second, job_id="job-second")

    repository.begin_rebuild(
        project_id="project-1",
        authority_identity=AUTHORITY_IDENTITY,
        authority_fingerprint=first.authority_fingerprint,
        job_id="job-rollback",
        updated_at="2026-07-26T12:20:00+08:00",
    )
    replayed_artifact = repository.stage_projection(first)
    repository.activate_staged(
        project_id="project-1",
        authority_identity=AUTHORITY_IDENTITY,
        authority_fingerprint=first.authority_fingerprint,
        job_id="job-rollback",
        artifact_id=replayed_artifact,
        updated_at="2026-07-26T12:21:00+08:00",
    )

    assert replayed_artifact == first_artifact
    assert len(store.list(ARTIFACT_COLLECTION)) == 2
    assert repository.load_current(
        project_id="project-1",
        authority_identity=AUTHORITY_IDENTITY,
        authority_fingerprint=first.authority_fingerprint,
    ).status == "fresh"


def test_missing_manifest_falls_back_without_projection_body(
    tmp_path: Path,
) -> None:
    projection = _projection()
    repository = ObjectStoreMemoryProjectionRepository(_store(tmp_path))

    result = repository.load_current(
        project_id=projection.project_id,
        authority_identity=projection.authority_identity,
        authority_fingerprint=projection.authority_fingerprint,
    )

    assert result.status == "missing"
    assert result.reason_code == "projection_manifest_missing"
    assert result.fallback_to_authority is True
    assert result.projection is None
    assert result.manifest is None


def test_missing_or_corrupt_artifact_never_returns_projection_body(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    repository = ObjectStoreMemoryProjectionRepository(store)
    projection = _projection()
    artifact_id, _ = _publish(repository, projection, job_id="job-first")

    store.delete(ARTIFACT_COLLECTION, artifact_id)
    missing = repository.load_current(
        project_id="project-1",
        authority_identity=AUTHORITY_IDENTITY,
        authority_fingerprint=projection.authority_fingerprint,
    )

    assert missing.status == "corrupt"
    assert missing.fallback_to_authority is True
    assert missing.projection is None

    artifact_id = repository.stage_projection(projection)
    artifact = dict(store.read(ARTIFACT_COLLECTION, artifact_id) or {})
    artifact["projection_digest"] = "0" * 64
    store.write(
        ARTIFACT_COLLECTION,
        artifact_id,
        artifact,
        expected_revision=1,
    )
    corrupt = repository.load_current(
        project_id="project-1",
        authority_identity=AUTHORITY_IDENTITY,
        authority_fingerprint=projection.authority_fingerprint,
    )

    assert corrupt.status == "corrupt"
    assert corrupt.projection is None


def test_confirmed_repair_discards_only_current_corrupt_derived_state(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    repository = ObjectStoreMemoryProjectionRepository(store)
    projection = _projection()
    artifact_id, _ = _publish(repository, projection, job_id="job-first")
    unrelated = _projection(
        revision=2,
        overview="不可被当前项目 repair 删除的历史派生 artifact。",
    )
    unrelated_id = repository.stage_projection(unrelated)
    artifact = dict(store.read(ARTIFACT_COLLECTION, artifact_id) or {})
    artifact["projection_digest"] = "0" * 64
    store.write(
        ARTIFACT_COLLECTION,
        artifact_id,
        artifact,
        expected_revision=1,
    )

    repair = repository.discard_corrupt_current(
        project_id=projection.project_id,
        authority_identity=projection.authority_identity,
        authority_fingerprint=projection.authority_fingerprint,
    )

    assert repair == {
        "status": "discarded",
        "manifest_discarded": True,
        "artifact_discarded": True,
    }
    assert store.read(MANIFEST_COLLECTION, projection_manifest_id("project-1")) is None
    assert store.read(ARTIFACT_COLLECTION, artifact_id) is None
    assert store.read(ARTIFACT_COLLECTION, unrelated_id) is not None
    assert repository.load_current(
        project_id=projection.project_id,
        authority_identity=projection.authority_identity,
        authority_fingerprint=projection.authority_fingerprint,
    ).status == "missing"

    repository.begin_rebuild(
        project_id=projection.project_id,
        authority_identity=projection.authority_identity,
        authority_fingerprint=projection.authority_fingerprint,
        job_id="job-repair",
        updated_at="2026-07-26T12:30:00+08:00",
    )
    repaired_artifact = repository.stage_projection(projection)
    repository.activate_staged(
        project_id=projection.project_id,
        authority_identity=projection.authority_identity,
        authority_fingerprint=projection.authority_fingerprint,
        job_id="job-repair",
        artifact_id=repaired_artifact,
        updated_at="2026-07-26T12:31:00+08:00",
    )
    assert repository.load_current(
        project_id=projection.project_id,
        authority_identity=projection.authority_identity,
        authority_fingerprint=projection.authority_fingerprint,
    ).status == "fresh"


def test_repair_refuses_to_discard_fresh_projection(tmp_path: Path) -> None:
    repository = ObjectStoreMemoryProjectionRepository(_store(tmp_path))
    projection = _projection()
    _publish(repository, projection, job_id="job-first")

    with pytest.raises(
        MemoryProjectionRepositoryIntegrityError,
        match="only accepts corrupt",
    ):
        repository.discard_corrupt_current(
            project_id=projection.project_id,
            authority_identity=projection.authority_identity,
            authority_fingerprint=projection.authority_fingerprint,
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("projection_digest", "0" * 64, "digest drifted"),
        ("authority_identity", "sqlite:other-authority", "identity"),
        ("derived", False, "safety metadata"),
    ),
)
def test_artifact_payload_drift_is_rejected_at_immutable_revision(
    field: str,
    value: object,
    message: str,
) -> None:
    store = _InterruptedManifestStore()
    store.interrupt_next_write = False
    repository = ObjectStoreMemoryProjectionRepository(store)
    projection = _projection()
    artifact_id, _ = _publish(repository, projection, job_id="job-first")
    store.payloads[(ARTIFACT_COLLECTION, artifact_id)][field] = value

    with pytest.raises(
        MemoryProjectionRepositoryIntegrityError,
        match=message,
    ):
        repository.artifact(
            project_id=projection.project_id,
            authority_identity=projection.authority_identity,
            authority_fingerprint=projection.authority_fingerprint,
        )


def test_previous_projection_policy_is_stale_instead_of_corrupt(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    repository = ObjectStoreMemoryProjectionRepository(store)
    projection = _projection()
    _publish(repository, projection, job_id="job-first")
    manifest_id = projection_manifest_id(projection.project_id)
    manifest = dict(store.read(MANIFEST_COLLECTION, manifest_id) or {})
    revision = store.revision(MANIFEST_COLLECTION, manifest_id)
    manifest["projection_version"] = "progressive-memory-r0-r1-v0"
    manifest["generator_policy_id"] = "deterministic-r0-r1-builder-v0"
    manifest["repository_revision"] = revision + 1
    store.write(
        MANIFEST_COLLECTION,
        manifest_id,
        manifest,
        expected_revision=revision,
    )

    result = repository.load_current(
        project_id=projection.project_id,
        authority_identity=projection.authority_identity,
        authority_fingerprint=projection.authority_fingerprint,
    )

    assert result.status == "stale"
    assert result.reason_code == "projection_policy_changed"
    assert result.fallback_to_authority is True
    assert result.projection is None


def test_manifest_metrics_cannot_smuggle_projection_body(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    repository = ObjectStoreMemoryProjectionRepository(store)
    projection = _projection()
    _publish(repository, projection, job_id="job-first")
    manifest_id = projection_manifest_id(projection.project_id)
    manifest = dict(store.read(MANIFEST_COLLECTION, manifest_id) or {})
    revision = store.revision(MANIFEST_COLLECTION, manifest_id)
    metrics = dict(manifest["active_metrics"])
    metrics["source_body"] = "PRIVATE-SOURCE-BODY"
    manifest["active_metrics"] = metrics
    manifest["repository_revision"] = revision + 1
    store.write(
        MANIFEST_COLLECTION,
        manifest_id,
        manifest,
        expected_revision=revision,
    )

    result = repository.load_current(
        project_id=projection.project_id,
        authority_identity=projection.authority_identity,
        authority_fingerprint=projection.authority_fingerprint,
    )

    assert result.status == "corrupt"
    assert result.projection is None
    assert result.manifest is None


def test_repository_rejects_extra_project_skill_or_source_body_fields(
    tmp_path: Path,
) -> None:
    repository = ObjectStoreMemoryProjectionRepository(_store(tmp_path))
    projection = _projection().to_payload()
    projection["project_skill_markdown"] = "PRIVATE-SKILL-BODY"

    with pytest.raises(
        MemoryProjectionRepositoryIntegrityError,
        match="fields drifted",
    ):
        repository.stage_projection(projection)

    projection = _projection().to_payload()
    projection["r0_items"][0]["source_refs"][0]["quote"] = "PRIVATE-SOURCE-BODY"
    with pytest.raises(
        MemoryProjectionRepositoryIntegrityError,
        match="source ref fields drifted",
    ):
        repository.stage_projection(projection)


def test_failed_rebuild_preserves_old_artifact_and_records_no_body(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    repository = ObjectStoreMemoryProjectionRepository(store)
    first = _projection()
    first_artifact, _ = _publish(repository, first, job_id="job-first")
    second = _projection(
        revision=2,
        overview="失败中的第二版。",
        generated_at="2026-07-26T12:10:00+08:00",
    )
    repository.begin_rebuild(
        project_id="project-1",
        authority_identity=AUTHORITY_IDENTITY,
        authority_fingerprint=second.authority_fingerprint,
        job_id="job-second",
        updated_at="2026-07-26T12:10:00+08:00",
    )

    failure = repository.record_failure(
        project_id="project-1",
        authority_identity=AUTHORITY_IDENTITY,
        authority_fingerprint=second.authority_fingerprint,
        job_id="job-second",
        attempt=1,
        failure_code="projection_rebuild_failed",
        recorded_at="2026-07-26T12:11:00+08:00",
    )
    result = repository.load_current(
        project_id="project-1",
        authority_identity=AUTHORITY_IDENTITY,
        authority_fingerprint=second.authority_fingerprint,
    )

    assert failure["contains_projection_body"] is False
    assert "projection" not in failure
    assert result.status == "stale"
    assert result.projection is None
    assert store.read(ARTIFACT_COLLECTION, first_artifact) is not None
    assert len(store.list(FAILURE_COLLECTION)) == 1


class _ConflictStore:
    def __init__(self, delegate: JsonObjectStore) -> None:
        self.delegate = delegate
        self.fail_next_manifest = False

    def read(self, collection: str, object_id: str):
        return self.delegate.read(collection, object_id)

    def list(self, collection: str):
        return self.delegate.list(collection)

    def delete(self, collection: str, object_id: str) -> bool:
        return self.delegate.delete(collection, object_id)

    def revision(self, collection: str, object_id: str) -> int:
        return self.delegate.revision(collection, object_id)

    def is_revision_conflict(self, error: BaseException) -> bool:
        return self.delegate.is_revision_conflict(error)

    def write(
        self,
        collection: str,
        object_id: str,
        payload: Mapping[str, object],
        expected_revision: int | None,
    ) -> int:
        if self.fail_next_manifest and collection == MANIFEST_COLLECTION:
            self.fail_next_manifest = False
            raise ObjectStoreRevisionError("injected CAS conflict")
        return self.delegate.write(
            collection,
            object_id,
            payload,
            expected_revision,
        )


class _NonConflictStore(_ConflictStore):
    def write(
        self,
        collection: str,
        object_id: str,
        payload: Mapping[str, object],
        expected_revision: int | None,
    ) -> int:
        raise RuntimeError("injected storage outage")


def test_non_cas_storage_failure_is_not_misclassified_or_swallowed(
    tmp_path: Path,
) -> None:
    repository = ObjectStoreMemoryProjectionRepository(
        _NonConflictStore(_store(tmp_path))
    )
    projection = _projection()

    with pytest.raises(RuntimeError, match="storage outage"):
        repository.begin_rebuild(
            project_id="project-1",
            authority_identity=AUTHORITY_IDENTITY,
            authority_fingerprint=projection.authority_fingerprint,
            job_id="job-storage-outage",
            updated_at=GENERATED_AT,
        )


def test_manifest_cas_conflict_does_not_overwrite_winner(
    tmp_path: Path,
) -> None:
    delegate = _store(tmp_path)
    store = _ConflictStore(delegate)
    repository = ObjectStoreMemoryProjectionRepository(store)
    projection = _projection()
    store.fail_next_manifest = True

    with pytest.raises(
        MemoryProjectionRepositoryConflict,
        match="CAS conflict",
    ):
        repository.begin_rebuild(
            project_id="project-1",
            authority_identity=AUTHORITY_IDENTITY,
            authority_fingerprint=projection.authority_fingerprint,
            job_id="job-first",
            updated_at=GENERATED_AT,
        )

    assert delegate.list(MANIFEST_COLLECTION) == ()


class _InterruptedManifestStore:
    def __init__(
        self,
        interrupt_collection: str = MANIFEST_COLLECTION,
    ) -> None:
        self.payloads: dict[tuple[str, str], dict[str, object]] = {}
        self.revisions: dict[tuple[str, str], int] = {}
        self.interrupt_collection = interrupt_collection
        self.interrupt_next_write = True

    def read(self, collection: str, object_id: str):
        payload = self.payloads.get((collection, object_id))
        return dict(payload) if payload is not None else None

    def list(self, collection: str) -> Sequence[Mapping[str, object]]:
        return tuple(
            dict(payload)
            for (stored_collection, _), payload in self.payloads.items()
            if stored_collection == collection
        )

    def delete(self, collection: str, object_id: str) -> bool:
        key = (collection, object_id)
        existed = key in self.payloads
        self.payloads.pop(key, None)
        self.revisions.pop(key, None)
        return existed

    def revision(self, collection: str, object_id: str) -> int:
        return self.revisions.get((collection, object_id), 0)

    @staticmethod
    def is_revision_conflict(error: BaseException) -> bool:
        return isinstance(error, ObjectStoreRevisionError)

    def write(
        self,
        collection: str,
        object_id: str,
        payload: Mapping[str, object],
        expected_revision: int | None,
    ) -> int:
        key = (collection, object_id)
        current = self.revisions.get(key, 0)
        if expected_revision is not None and current != expected_revision:
            raise ObjectStoreRevisionError("revision conflict")
        if self.interrupt_next_write and collection == self.interrupt_collection:
            self.interrupt_next_write = False
            self.payloads[key] = dict(payload)
            raise ObjectStoreRevisionError("interrupted after payload replace")
        next_revision = current + 1
        self.payloads[key] = dict(payload)
        self.revisions[key] = next_revision
        return next_revision


def test_interrupted_manifest_metadata_commit_is_repaired_on_restart() -> None:
    store = _InterruptedManifestStore()
    projection = _projection()
    repository = ObjectStoreMemoryProjectionRepository(store)

    with pytest.raises(MemoryProjectionRepositoryConflict):
        repository.begin_rebuild(
            project_id="project-1",
            authority_identity=AUTHORITY_IDENTITY,
            authority_fingerprint=projection.authority_fingerprint,
            job_id="job-first",
            updated_at=GENERATED_AT,
        )

    restarted = ObjectStoreMemoryProjectionRepository(store)
    repaired = restarted.begin_rebuild(
        project_id="project-1",
        authority_identity=AUTHORITY_IDENTITY,
        authority_fingerprint=projection.authority_fingerprint,
        job_id="job-first",
        updated_at=GENERATED_AT,
    )

    assert repaired["status"] == "rebuilding"
    assert repaired["repository_revision"] == 1
    assert store.revision(MANIFEST_COLLECTION, repaired["manifest_id"]) == 1


def test_interrupted_artifact_metadata_commit_is_repaired_before_activation() -> None:
    store = _InterruptedManifestStore(ARTIFACT_COLLECTION)
    projection = _projection()
    repository = ObjectStoreMemoryProjectionRepository(store)
    repository.begin_rebuild(
        project_id="project-1",
        authority_identity=AUTHORITY_IDENTITY,
        authority_fingerprint=projection.authority_fingerprint,
        job_id="job-first",
        updated_at=GENERATED_AT,
    )

    artifact_id = repository.stage_projection(projection)
    manifest = repository.activate_staged(
        project_id="project-1",
        authority_identity=AUTHORITY_IDENTITY,
        authority_fingerprint=projection.authority_fingerprint,
        job_id="job-first",
        artifact_id=artifact_id,
        updated_at=GENERATED_AT,
    )

    assert store.revision(ARTIFACT_COLLECTION, artifact_id) == 1
    assert manifest["status"] == "ready"


def test_mark_stale_is_idempotent_for_current_fingerprint(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    repository = ObjectStoreMemoryProjectionRepository(store)
    projection = _projection()
    _publish(repository, projection, job_id="job-first")

    unchanged = repository.mark_stale(
        project_id="project-1",
        authority_identity=AUTHORITY_IDENTITY,
        authority_fingerprint=projection.authority_fingerprint,
        updated_at="2026-07-26T12:20:00+08:00",
    )

    assert unchanged["status"] == "ready"
    assert len(store.list(MANIFEST_COLLECTION)) == 1
