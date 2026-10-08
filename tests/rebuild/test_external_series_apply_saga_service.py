from pathlib import Path

import pytest

from backend.api.external_series_apply_saga import ExternalSeriesApplyConflict, ExternalSeriesApplySagaService
from core.storage_provider import JsonObjectStore, SQLiteExternalSeriesApplySagaStore, SQLiteStructuredRecordStore


def _store(root): return JsonObjectStore(root / "objects", legacy_root=root / "library")
def _operations(root): return SQLiteExternalSeriesApplySagaStore(SQLiteStructuredRecordStore(root / "records.sqlite3"))


def _seed(root, draft_id="draft-series-service"):
    store = _store(root)
    current = {"id": "series-memory-alpha", "series_id": "series-alpha", "overview": "before", "revision": 1}
    proposed = {"id": "series-memory-alpha", "series_id": "series-alpha", "overview": "after", "revision": 2}
    store.write("memory_series_memory", current["id"], current, expected_revision=0)
    store.write("external_agent_review_drafts", draft_id, {
        "id": draft_id, "draft_type": "series_update", "status": "pending_review", "target_id": "series-alpha",
        "suggested_changes": {"structured": proposed}, "review": {"state": "pending_review"},
        "application": {"state": "blocked"},
    }, expected_revision=0)
    return store, proposed


class _FailSeriesAppliedTransition:
    def __init__(self, delegate): self.delegate = delegate
    def prepare(self, **kwargs): return self.delegate.prepare(**kwargs)
    def mark_series_applied(self, *args, **kwargs): raise OSError("injected interruption after series write")
    def finalize(self, *args, **kwargs): return self.delegate.finalize(*args, **kwargs)


class _FailDraftFinalize:
    def __init__(self, delegate): self.delegate = delegate
    def read(self, collection, object_id): return self.delegate.read(collection, object_id)
    def revision(self, collection, object_id): return self.delegate.revision(collection, object_id)
    def write(self, collection, object_id, payload, expected_revision):
        if collection == "external_agent_review_drafts" and payload.get("status") == "applied":
            raise OSError("injected draft finalize failure")
        return self.delegate.write(collection, object_id, payload, expected_revision)


def test_normal_apply_and_repeated_service_call_do_not_advance_revisions(tmp_path: Path):
    store, proposed = _seed(tmp_path)
    operations = _operations(tmp_path)
    service = ExternalSeriesApplySagaService(objects=store, operations=operations)
    first = service.apply("draft-series-service", expected_object_revision=1)
    repeated = service.apply("draft-series-service", expected_object_revision=1)
    assert first.state == repeated.state == "finalized"
    assert first.series_revision == repeated.series_revision == 2
    assert store.read("memory_series_memory", proposed["id"]) == proposed
    assert store.revision("memory_series_memory", proposed["id"]) == 2
    assert store.read("external_agent_review_drafts", "draft-series-service")["application"]["operation_id"] == "draft-series-service"


def test_prepared_replay_recovers_exact_payload_after_series_write(tmp_path: Path):
    store, proposed = _seed(tmp_path)
    operations = _operations(tmp_path)
    with pytest.raises(OSError, match="interruption"):
        ExternalSeriesApplySagaService(objects=store, operations=_FailSeriesAppliedTransition(operations)).apply(
            "draft-series-service", expected_object_revision=1
        )
    assert operations.get("draft-series-service").state == "prepared"
    assert store.revision("memory_series_memory", proposed["id"]) == 2

    result = ExternalSeriesApplySagaService(objects=store, operations=operations).apply(
        "draft-series-service", expected_object_revision=1
    )
    assert result.state == "finalized"
    assert store.revision("memory_series_memory", proposed["id"]) == 2


def test_series_applied_replay_finishes_draft_without_rewriting_series(tmp_path: Path):
    store, proposed = _seed(tmp_path)
    operations = _operations(tmp_path)
    with pytest.raises(OSError, match="draft finalize failure"):
        ExternalSeriesApplySagaService(objects=_FailDraftFinalize(store), operations=operations).apply(
            "draft-series-service", expected_object_revision=1
        )
    assert operations.get("draft-series-service").state == "series_applied"
    result = ExternalSeriesApplySagaService(objects=store, operations=operations).apply(
        "draft-series-service", expected_object_revision=1
    )
    assert result.state == "finalized"
    assert store.revision("memory_series_memory", proposed["id"]) == 2


def test_payload_and_object_revision_drift_fail_closed(tmp_path: Path):
    store, proposed = _seed(tmp_path)
    operations = _operations(tmp_path)
    with pytest.raises(OSError):
        ExternalSeriesApplySagaService(objects=store, operations=_FailSeriesAppliedTransition(operations)).apply(
            "draft-series-service", expected_object_revision=1
        )
    drifted = dict(proposed); drifted["overview"] = "unrelated writer"
    store.write("memory_series_memory", proposed["id"], drifted, expected_revision=2)
    with pytest.raises(ExternalSeriesApplyConflict, match="revision evidence drifted|advanced without operation evidence"):
        ExternalSeriesApplySagaService(objects=store, operations=operations).apply(
            "draft-series-service", expected_object_revision=1
        )


def test_public_object_revision_is_read_only_and_tracks_cas(tmp_path: Path):
    store = _store(tmp_path)
    assert store.revision("memory_series_memory", "missing") == 0
    store.write("memory_series_memory", "series-memory", {"revision": 1}, expected_revision=0)
    assert store.revision("memory_series_memory", "series-memory") == 1
