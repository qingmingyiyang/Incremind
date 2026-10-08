from __future__ import annotations

import pytest

from core.memory_core import ObjectStoreMemoryCandidateRepository
from core.product_core import (
    MemoryPublicationReviewStagingSagaService,
    MemoryPublicationReviewStagingServiceConflict,
)
from core.storage_provider import (
    JsonObjectStore,
    SQLiteMemoryPublicationReviewStagingSagaStore,
    SQLiteStructuredRecordStore,
)


def _candidate(layer: str, candidate_id: str) -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "id": candidate_id,
        "project_id": "project-alpha",
        "target_layer": layer,
        "candidate_type": "answer_summary",
        "status": "pending_review",
        "proposed_content": f"{layer} candidate must first become a durable staging draft.",
        "source_refs": [{"source_id": "source-alpha", "locator": "char:0-80"}],
        "provenance": {
            "model_result_id": None,
            "model_request_id": None,
            "recall_result_id": None,
            "document_id": "document-alpha",
            "document_revision": 1,
            "source_content_read_id": None,
            "media_processing_output_id": None,
            "media_processing_job_id": None,
            "input_refs": [
                {
                    "kind": "document",
                    "object_id": "document-alpha",
                    "uri": "crp://default/documents/document-alpha.json",
                }
            ],
        },
        "review": {
            "requires_user_confirmation": True,
            "auto_promote_allowed": False,
            "reason": "awaiting user confirmation",
            "reviewed_by": None,
            "reviewed_at": None,
        },
        "created_at": "2026-07-12T19:00:00+08:00",
        "updated_at": "2026-07-12T19:00:00+08:00",
    }


def _service(tmp_path):
    candidates = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    records = SQLiteStructuredRecordStore(tmp_path / ".rebuild-data" / "structured-records.sqlite3")
    operations = SQLiteMemoryPublicationReviewStagingSagaStore(records)
    return candidates, records, operations, MemoryPublicationReviewStagingSagaService(candidates, records, operations)


def _save(candidates: JsonObjectStore, layer: str, candidate_id: str) -> str:
    ObjectStoreMemoryCandidateRepository(candidates).save(_candidate(layer, candidate_id))
    return candidate_id


@pytest.mark.parametrize(
    ("layer", "staging", "current"),
    [
        ("atom", "staging_atoms", "memory_atoms"),
        ("scenario", "staging_scenarios", "memory_scenarios"),
        ("series_memory", "staging_series_memory", "memory_series_memory"),
    ],
)
def test_generic_review_staging_finalizes_without_long_term_memory(
    tmp_path, layer: str, staging: str, current: str
) -> None:
    candidates, records, operations, service = _service(tmp_path)
    candidate_id = _save(candidates, layer, f"candidate-{layer}")

    result = service.review_to_staging(
        candidate_id,
        review_reason="user confirms a durable staging draft",
        reviewed_at="2026-07-12T19:05:00+08:00",
    )
    replay = service.review_to_staging(
        candidate_id,
        review_reason="user confirms a durable staging draft",
        reviewed_at="2026-07-12T19:05:00+08:00",
    )

    assert result.state == "finalized"
    assert replay == result
    candidate = candidates.read("memory_candidates", candidate_id)
    assert candidate is not None and candidate["status"] == "promoted"
    assert candidates.revision("memory_candidates", candidate_id) == 2
    assert records.read(staging, result.evidence.draft_id) is not None
    assert records.read("staging_memory_publication_contexts", result.evidence.context_id) is not None
    assert records.list(current) == ()
    assert records.list("memory_publications") == ()
    assert records.list("memory_transitions") == ()
    assert operations.list_recoverable() == ()


def test_prepared_operation_recovers_in_new_service_without_duplicate_candidate_revision(tmp_path) -> None:
    candidates, records, operations, service = _service(tmp_path)
    candidate_id = _save(candidates, "atom", "candidate-prepared")
    prepared = service.prepare_review(
        candidate_id,
        review_reason="user confirms durable recovery",
        reviewed_at="2026-07-12T19:10:00+08:00",
    )

    recovered = MemoryPublicationReviewStagingSagaService(
        candidates,
        records,
        SQLiteMemoryPublicationReviewStagingSagaStore(records),
    ).resume(prepared.operation_id)

    assert recovered.state == "finalized"
    assert candidates.revision("memory_candidates", candidate_id) == 2
    assert records.read("staging_atoms", recovered.evidence.draft_id) is not None


@pytest.mark.parametrize("failure", ("candidate-drift", "staging-drift", "current-exists"))
def test_review_staging_fails_closed_for_drift(tmp_path, failure: str) -> None:
    candidates, records, operations, service = _service(tmp_path)
    candidate_id = _save(candidates, "atom", f"candidate-{failure}")
    operation = service.prepare_review(
        candidate_id,
        review_reason="user confirms strict evidence",
        reviewed_at="2026-07-12T19:15:00+08:00",
    )
    if failure == "candidate-drift":
        candidate = candidates.read("memory_candidates", candidate_id)
        assert candidate is not None
        candidates.write("memory_candidates", candidate_id, {**candidate, "proposed_content": "drift"}, expected_revision=1)
    elif failure == "staging-drift":
        with records.begin() as uow:
            uow.put("staging_atoms", operation.evidence.draft_id, {"id": operation.evidence.draft_id, "drift": True}, expected_revision=0)
            uow.commit()
    else:
        with records.begin() as uow:
            uow.put("memory_atoms", operation.evidence.draft_id, {"id": operation.evidence.draft_id}, expected_revision=0)
            uow.commit()

    with pytest.raises(MemoryPublicationReviewStagingServiceConflict):
        service.resume(operation.operation_id)
    assert operations.get(operation.operation_id).state == "prepared"


def test_external_series_json_base_candidate_is_rejected_before_sqlite_side_effect(tmp_path) -> None:
    candidates, records, operations, service = _service(tmp_path)
    candidate_id = _save(candidates, "series_memory", "candidate-external-series")
    candidate = candidates.read("memory_candidates", candidate_id)
    assert candidate is not None
    candidates.write(
        "memory_candidates",
        candidate_id,
        {
            **candidate,
            "external_series_update": {
                "authority_identity": "json:object-store-v1",
            },
        },
        expected_revision=1,
    )

    with pytest.raises(MemoryPublicationReviewStagingServiceConflict, match="External Series"):
        service.review_to_staging(
            candidate_id,
            review_reason="user confirms update",
            reviewed_at="2026-07-12T19:20:00+08:00",
        )
    assert records.list("memory_publication_review_staging_operations") == ()
    assert records.list("staging_series_memory") == ()
    assert candidates.revision("memory_candidates", candidate_id) == 2
