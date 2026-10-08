from __future__ import annotations

from pathlib import Path

import pytest

from backend.api.external_series_candidate_saga import (
    ExternalSeriesCandidateConflict,
    ExternalSeriesCandidateSagaService,
    SQLiteSeriesCurrentProjection,
)
from core.product_core import (
    MemoryPublicationReviewStagingSagaService,
    MemoryPublicationReviewStagingServiceConflict,
)
from core.storage_provider import (
    JsonObjectStore,
    SQLiteExternalSeriesCandidateSagaStore,
    SQLiteMemoryPublicationReviewStagingSagaStore,
    SQLiteStructuredRecordStore,
)


def _store(root: Path) -> JsonObjectStore:
    return JsonObjectStore(root / "objects", legacy_root=root / "library")


def _operations(root: Path) -> SQLiteExternalSeriesCandidateSagaStore:
    return SQLiteExternalSeriesCandidateSagaStore(SQLiteStructuredRecordStore(root / "records.sqlite3"))


def _series() -> tuple[dict[str, object], dict[str, object]]:
    current = {
        "schema_version": "1.0.0",
        "id": "series-memory-alpha",
        "series_id": "series-alpha",
        "scope": "project",
        "overview": "before",
        "scenario_ids": [],
        "source_refs": [{"source_id": "source-alpha", "locator": "char:0-20"}],
        "project_ids": ["project-alpha"],
        "stale": False,
        "stale_reason": None,
        "revision": 1,
        "created_at": "2026-07-12T12:00:00+08:00",
        "updated_at": "2026-07-12T12:00:00+08:00",
        "trust_status": "user_confirmed",
    }
    proposed = dict(current)
    proposed.update({"overview": "after", "revision": 2, "updated_at": "2026-07-12T12:05:00+08:00"})
    return current, proposed


def _seed(root: Path, draft_id: str = "draft-series-candidate") -> tuple[JsonObjectStore, dict[str, object]]:
    store = _store(root)
    current, proposed = _series()
    store.write("memory_series_memory", str(current["id"]), current, expected_revision=0)
    store.write(
        "external_agent_review_drafts",
        draft_id,
        {
            "id": draft_id,
            "draft_type": "series_update",
            "status": "pending_review",
            "project_id": "project-alpha",
            "proposal_id": "proposal-alpha",
            "target_id": "series-alpha",
            "suggested_changes": {"structured": proposed},
            "source_refs": [{"source_id": "source-alpha", "locator": "char:0-20"}],
            "evidence_refs": [{"source_id": "source-alpha", "locator": "char:0-20"}],
            "review": {"state": "pending_review"},
            "application": {"state": "not_applied"},
        },
        expected_revision=0,
    )
    return store, proposed


class _FailCandidateTransition:
    def __init__(self, delegate: SQLiteExternalSeriesCandidateSagaStore) -> None:
        self._delegate = delegate

    def prepare(self, **kwargs):
        return self._delegate.prepare(**kwargs)

    def mark_candidate_created(self, *args, **kwargs):
        raise OSError("injected interruption after candidate write")

    def finalize(self, *args, **kwargs):
        return self._delegate.finalize(*args, **kwargs)


def test_apply_creates_pending_candidate_without_staging_or_long_term_write(tmp_path: Path) -> None:
    store, proposed = _seed(tmp_path)
    operations = _operations(tmp_path)

    first = ExternalSeriesCandidateSagaService(objects=store, operations=operations).apply(
        "draft-series-candidate", expected_object_revision=1
    )
    repeated = ExternalSeriesCandidateSagaService(objects=store, operations=operations).apply(
        "draft-series-candidate", expected_object_revision=1
    )

    assert first == repeated
    candidate = store.read("memory_candidates", first.candidate_id)
    assert candidate is not None
    assert candidate["status"] == "pending_review"
    assert candidate["external_series_update"]["proposed"] == proposed
    assert store.read("memory_series_memory", "series-memory-alpha")["overview"] == "before"
    assert store.revision("memory_series_memory", "series-memory-alpha") == 1
    assert store.list("staging_series_memory") == ()
    assert store.list("memory_publications") == ()
    draft = store.read("external_agent_review_drafts", "draft-series-candidate")
    assert draft is not None
    assert draft["status"] == "candidate_created"
    assert draft["application"]["writes_long_term_memory"] is False
    assert draft["application"]["memory_candidate_id"] == first.candidate_id


def test_apply_can_bind_pending_candidate_to_sqlite_current_authority(tmp_path: Path) -> None:
    store, _proposed = _seed(tmp_path)
    records = SQLiteStructuredRecordStore(tmp_path / "records.sqlite3")
    current, _ = _series()
    with records.begin() as transaction:
        transaction.put("memory_series_memory", str(current["id"]), current, expected_revision=0)
        transaction.commit()

    result = ExternalSeriesCandidateSagaService(
        objects=store,
        operations=_operations(tmp_path),
        authority_identity="sqlite:structured-records-v1",
        current=SQLiteSeriesCurrentProjection(records),
    ).apply("draft-series-candidate", expected_object_revision=1)

    candidate = store.read("memory_candidates", result.candidate_id)
    assert candidate is not None
    assert candidate["external_series_update"]["authority_identity"] == "sqlite:structured-records-v1"
    assert records.read("memory_series_memory", "series-memory-alpha").payload["overview"] == "before"


def test_prepared_replay_after_candidate_write_does_not_duplicate_candidate(tmp_path: Path) -> None:
    store, _proposed = _seed(tmp_path)
    operations = _operations(tmp_path)
    with pytest.raises(OSError, match="interruption"):
        ExternalSeriesCandidateSagaService(
            objects=store,
            operations=_FailCandidateTransition(operations),
        ).apply("draft-series-candidate", expected_object_revision=1)
    assert operations.get("draft-series-candidate").state == "prepared"
    assert len(store.list("memory_candidates")) == 1

    result = ExternalSeriesCandidateSagaService(objects=store, operations=operations).apply(
        "draft-series-candidate", expected_object_revision=1
    )
    assert result.state == "finalized"
    assert len(store.list("memory_candidates")) == 1
    assert store.revision("memory_series_memory", "series-memory-alpha") == 1


def test_target_drift_after_prepared_candidate_fails_closed(tmp_path: Path) -> None:
    store, proposed = _seed(tmp_path)
    operations = _operations(tmp_path)
    with pytest.raises(OSError):
        ExternalSeriesCandidateSagaService(
            objects=store,
            operations=_FailCandidateTransition(operations),
        ).apply("draft-series-candidate", expected_object_revision=1)
    drifted = dict(proposed)
    drifted["overview"] = "unrelated writer"
    drifted["revision"] = 3
    store.write("memory_series_memory", "series-memory-alpha", drifted, expected_revision=1)

    with pytest.raises(ExternalSeriesCandidateConflict, match="advanced"):
        ExternalSeriesCandidateSagaService(objects=store, operations=operations).apply(
            "draft-series-candidate", expected_object_revision=1
        )
    assert operations.get("draft-series-candidate").state == "prepared"
    assert store.list("staging_series_memory") == ()
    assert store.list("memory_publications") == ()


def test_candidate_review_via_saga_stages_with_cas_and_canonical_context(tmp_path: Path) -> None:
    store, _proposed = _seed(tmp_path)
    records = SQLiteStructuredRecordStore(tmp_path / "records.sqlite3")
    current, _ = _series()
    with records.begin() as transaction:
        transaction.put("memory_series_memory", str(current["id"]), current, expected_revision=0)
        transaction.commit()

    result = ExternalSeriesCandidateSagaService(
        objects=store,
        operations=_operations(tmp_path),
        authority_identity="sqlite:structured-records-v1",
        current=SQLiteSeriesCurrentProjection(records),
    ).apply("draft-series-candidate", expected_object_revision=1)

    operation = MemoryPublicationReviewStagingSagaService(
        store,
        records,
        SQLiteMemoryPublicationReviewStagingSagaStore(records),
    ).review_to_staging(
        result.candidate_id,
        review_reason="用户确认外部 Series 候选进入 staging。",
        reviewed_at="2026-07-12T12:08:00+08:00",
    )

    staged = records.read("staging_series_memory", "series-memory-alpha")
    context = records.read("staging_memory_publication_contexts", operation.evidence.context_id)
    candidate = store.read("memory_candidates", result.candidate_id)

    assert operation.state == "finalized"
    assert operation.evidence.draft_id == "series-memory-alpha"
    assert staged is not None
    assert staged.payload["external_series_update"]["expected_object_revision"] == 1
    assert staged.payload["external_series_update"]["base_series_revision"] == 1
    assert staged.payload["external_series_update"]["authority_identity"] == "sqlite:structured-records-v1"
    assert context is not None
    assert candidate is not None
    assert candidate["status"] == "promoted"
    assert candidate["application"]["operation_id"] == operation.operation_id
    current_record = records.read("memory_series_memory", "series-memory-alpha")
    assert current_record is not None
    assert current_record.payload["overview"] == "before"
    assert current_record.revision == 1
    assert store.list("staging_series_memory") == ()
    assert store.list("memory_publications") == ()
    assert records.list("memory_publications") == ()


def test_candidate_review_rejects_current_revision_drift_before_staging(tmp_path: Path) -> None:
    store, _proposed = _seed(tmp_path)
    records = SQLiteStructuredRecordStore(tmp_path / "records.sqlite3")
    current, _ = _series()
    with records.begin() as transaction:
        transaction.put("memory_series_memory", str(current["id"]), current, expected_revision=0)
        transaction.commit()

    result = ExternalSeriesCandidateSagaService(
        objects=store,
        operations=_operations(tmp_path),
        authority_identity="sqlite:structured-records-v1",
        current=SQLiteSeriesCurrentProjection(records),
    ).apply("draft-series-candidate", expected_object_revision=1)

    drifted = dict(current)
    drifted["revision"] = 3
    with records.begin() as transaction:
        transaction.put("memory_series_memory", "series-memory-alpha", drifted, expected_revision=1)
        transaction.commit()

    service = MemoryPublicationReviewStagingSagaService(
        store,
        records,
        SQLiteMemoryPublicationReviewStagingSagaStore(records),
    )
    with pytest.raises(MemoryPublicationReviewStagingServiceConflict):
        service.review_to_staging(
            result.candidate_id,
            review_reason="用户确认。",
            reviewed_at="2026-07-12T12:09:00+08:00",
        )
    assert records.list("staging_series_memory") == ()
    assert store.list("staging_series_memory") == ()
    candidate = store.read("memory_candidates", result.candidate_id)
    assert candidate is not None
    assert candidate["status"] == "pending_review"
