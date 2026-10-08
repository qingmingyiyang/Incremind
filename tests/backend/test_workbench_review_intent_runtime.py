from __future__ import annotations

from pathlib import Path

import pytest

from backend.api.workbench_review_intent_runtime import (
    COLLECTION,
    ReviewIntentAdmission,
    ReviewIntentJobRepository,
)
from core.job_runner import ObjectStoreJobRepository
from core.storage_provider import JsonObjectStore, SQLiteStructuredRecordStore


def _setup(root: Path):
    store = JsonObjectStore(root / ".rebuild-data", legacy_root=root / "library")
    records = SQLiteStructuredRecordStore(root / ".rebuild-data" / "structured-records.sqlite3")
    admission = ReviewIntentAdmission(records, store)
    repository = ReviewIntentJobRepository(ObjectStoreJobRepository(store), admission)
    return store, records, admission, repository


def _source(store: JsonObjectStore, source_id: str, project_id: str = "default") -> None:
    store.write("sources", source_id, {"id": source_id, "project_id": project_id}, expected_revision=None)


def test_capture_job_registers_review_intent_before_job_save_and_repeats_idempotently(tmp_path: Path) -> None:
    store, records, _, repository = _setup(tmp_path)
    _source(store, "source-one", "project-a")
    job = {"id": "job-capture-source-one", "source_id": "source-one"}
    repository.save(job)
    first = records.read(COLLECTION, "review-source-one")
    assert first is not None
    assert first.payload == {
        "schema_version": "1.0.0", "id": "review-source-one", "source_id": "source-one",
        "project_id": "project-a", "job_id": "job-capture-source-one",
        "state": "pending", "source_revision": 1,
    }
    repository.save(job)
    assert records.read(COLLECTION, "review-source-one").revision == first.revision
    assert store.read("jobs", "job-capture-source-one") is not None


def test_batch_child_intents_are_atomic_and_precede_parent_job(tmp_path: Path) -> None:
    store, records, _, repository = _setup(tmp_path)
    for source_id in ("source-parent", "source-child-a", "source-child-b"):
        _source(store, source_id)
    job = {
        "id": "job-capture-source-parent", "source_id": "source-parent",
        "outputs": [{"kind": "link_sources", "source_ids": ["source-child-a", "source-child-b"]}],
    }
    repository.save(job)
    assert {row.object_id for row in records.list(COLLECTION)} == {
        "review-source-parent", "review-source-child-a", "review-source-child-b",
    }
    with pytest.raises(ValueError, match="Source is unavailable"):
        repository.save({
            "id": "job-capture-missing", "source_id": "source-parent",
            "outputs": [{"kind": "link_sources", "source_ids": ["missing"]}],
        })
    assert store.read("jobs", "job-capture-missing") is None


def test_mismatched_project_or_capture_job_fails_closed(tmp_path: Path) -> None:
    store, records, admission, repository = _setup(tmp_path)
    _source(store, "source-one")
    repository.save({"id": "job-capture-source-one", "source_id": "source-one"})
    with pytest.raises(ValueError, match="binding conflict"):
        admission.admit("job-other", ["source-one"])
    source = dict(store.read("sources", "source-one"))
    source["project_id"] = "project-other"
    store.write("sources", "source-one", source, expected_revision=1)
    with pytest.raises(ValueError, match="binding conflict"):
        admission.admit("job-intake-source-one", ["source-one"], existing_job_ok=True)
    assert records.read(COLLECTION, "review-source-one").payload["project_id"] == "default"


def test_final_job_cannot_take_over_another_final_job(tmp_path: Path) -> None:
    store, records, admission, _ = _setup(tmp_path)
    _source(store, "source-one")
    admission.admit("job-intake-source-one", ["source-one"])
    with pytest.raises(ValueError, match="binding conflict"):
        admission.admit("job-intake-other", ["source-one"], existing_job_ok=True)
    assert records.read(COLLECTION, "review-source-one").payload["job_id"] == "job-intake-source-one"


def test_final_job_hook_admits_missing_child_and_preserves_capture_binding(tmp_path: Path) -> None:
    store, records, admission, repository = _setup(tmp_path)
    _source(store, "source-parent")
    _source(store, "source-child")
    repository.save({"id": "job-capture-source-parent", "source_id": "source-parent"})
    admission.admit("job-intake-source-parent", ["source-parent", "source-child"], existing_job_ok=True)
    assert records.read(COLLECTION, "review-source-parent").payload["job_id"] == "job-capture-source-parent"
    assert records.read(COLLECTION, "review-source-child").payload["job_id"] == "job-intake-source-parent"
