import pytest

from core.product_core import (
    CandidateMemoryJobContractError,
    CandidateMemoryJobInput,
    build_candidate_memory_job,
)


def _input(**changes):
    values = {"parent_job_id": "job-parent", "source_id": "source-alpha", "project_id": "default",
              "evidence_kind": "source_content_read", "evidence_id": "content-read-source-alpha"}
    values.update(changes)
    return CandidateMemoryJobInput(**values)


def test_contract_is_deterministic_candidate_only_and_manual_review_safe():
    first = build_candidate_memory_job(_input(), now="2026-07-11T12:00:00Z")
    repeated = build_candidate_memory_job(_input(), now="2026-07-11T12:00:01Z")
    assert first["id"] == repeated["id"] == first["idempotency_key"]
    assert first["job_type"] == "extract_memory_candidate"
    assert first["execution_version"] == "effect-v2"
    assert first["steps"] == [{"name": "create_candidate", "status": "pending", "error": None}]
    assert first["checkpoint"] is None
    assert first["published_outputs"] == []
    assert first["candidate_policy"] == {"requires_user_confirmation": True, "auto_promote_allowed": False}
    assert all(step["name"] != "publish_atom" for step in first["steps"])


def test_identity_or_evidence_changes_produce_distinct_child_id():
    baseline = build_candidate_memory_job(_input(), now="2026-07-11T12:00:00Z")["id"]
    for change in ({"parent_job_id": "job-other"}, {"source_id": "source-other"},
                   {"project_id": "project-other"}, {"evidence_id": "content-read-other"}):
        assert build_candidate_memory_job(_input(**change), now="2026-07-11T12:00:00Z")["id"] != baseline


@pytest.mark.parametrize("change", [
    {"parent_job_id": ""}, {"source_id": ""}, {"project_id": ""}, {"evidence_id": ""},
    {"evidence_kind": "media_processing_output"},
])
def test_invalid_or_unsupported_input_fails_closed(change):
    with pytest.raises(CandidateMemoryJobContractError):
        build_candidate_memory_job(_input(**change), now="2026-07-11T12:00:00Z")
