from __future__ import annotations

import pytest

from core.effect_log import EFFECT_V2, NOT_APPLICABLE, V2_REVISION_KEYS, EffectClass, GateDecision
from core.product_core.candidate_job_admission import CandidateJobAdmissionFactory


def _payload(**changes: object) -> dict[str, object]:
    values: dict[str, object] = {
        "id": "job-extract-memory-candidate-alpha",
        "job_type": "extract_memory_candidate",
        "parent_job_id": "job-parent-alpha",
        "source_id": "source-alpha",
        "project_id": "project-alpha",
        "evidence_kind": "source_content_read",
        "evidence_id": "content-read-alpha-r3",
        "candidate_policy": {
            "requires_user_confirmation": True,
            "auto_promote_allowed": False,
        },
        "attempt": 0,
        "schema_version": "1.0.0",
        "idempotency_key": "job-extract-memory-candidate-alpha",
        "status": "pending",
        "max_attempts": 3,
        "lease": None,
        "progress": {"current": 0, "total": 1, "percent": 0, "message": "awaiting candidate creation"},
        "steps": [{"name": "create_candidate", "status": "pending", "error": None}],
        "error": None,
        "checkpoint": None,
        "staged_outputs": [],
        "published_outputs": [],
        "events": [],
        "input_refs": [
            {"kind": "source", "object_id": "source-alpha", "uri": "crp://default/sources/source-alpha"},
            {"kind": "source_content_read", "object_id": "content-read-alpha-r3", "uri": "crp://default/source-content-reads/content-read-alpha-r3.json"},
        ],
        "created_at": "2026-08-30T00:00:00Z",
        "updated_at": "2026-08-30T00:00:00Z",
        "execution_version": "effect-v2",
    }
    values.update(changes)
    return values


def _source(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {"id": "source-alpha", "project_id": "project-alpha"}
    value.update(changes)
    return value


def _read(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "id": "content-read-alpha-r3", "source_id": "source-alpha", "status": "completed",
        "text_sha256": "a" * 64,
    }
    value.update(changes)
    return value


def _build(
    payload: dict[str, object] | None = None,
    *,
    source: dict[str, object] | None = None,
    source_revision: object = 3,
    read: dict[str, object] | None = None,
    read_revision: object = 5,
):
    return CandidateJobAdmissionFactory(admitted_at=100).build(
        job_payload=payload or _payload(),
        source_record=source or _source(),
        source_revision=source_revision,
        source_content_read_record=read or _read(),
        source_content_read_revision=read_revision,
    )


def test_builds_queryable_v2_proposal_only_candidate_admission() -> None:
    admitted = _build()

    assert admitted.intent.contract_version == EFFECT_V2
    assert admitted.intent.effect_class is EffectClass.QUERYABLE
    assert admitted.intent.kind == "memory_candidate_from_source_output"
    assert admitted.intent.parent_id is None
    assert admitted.intent.payload == {
        "job_ref": admitted.authorization.admission_ref,
        "admission_ref": admitted.authorization.admission_ref,
        "parent_job_ref": "facts:candidate-job/parents/job-parent-alpha",
        "source_ref": "crp://candidate-job/sources/source-alpha",
        "evidence_ref": "crp://candidate-job/source-content-reads/content-read-alpha-r3",
        "mode": "admit",
        "attempt_index": 0,
    }
    assert admitted.authorization.gate_fact.decision is GateDecision.ALLOW
    assert admitted.authorization.gate_fact.budget_after["requires_user_confirmation_budget"] == 1
    assert admitted.authorization.gate_fact.budget_after["auto_promote_allowed_budget"] == 0
    assert admitted.authorization.gate_fact.budget_after["project_ref"] == "crp://candidate-job/projects/project-alpha"
    assert admitted.authorization.gate_fact.budget_after["source_ref"] == "crp://candidate-job/sources/source-alpha"
    assert admitted.authorization.gate_fact.budget_after["source_identity_ref"] == "facts:candidate-job/sources/source-alpha"
    assert admitted.authorization.gate_fact.budget_after["source_content_read_content_ref"] == (
        f"facts:candidate-job/source-content-reads/content-read-alpha-r3/sha256/{'a' * 64}"
    )
    assert admitted.authorization.gate_fact.budget_after["parent_job_ref"] == "facts:candidate-job/parents/job-parent-alpha"
    assert admitted.intent.expected_receipt_kind == "candidate-memory-job-execution.receipt"
    assert admitted.intent.expected_receipt_schema_version == "candidate-memory-job-execution-receipt-v2"


def test_freezes_closed_v2_revisions_and_marks_unneeded_authorities_not_applicable() -> None:
    admitted = _build()

    assert set(admitted.intent.rev_set) == set(V2_REVISION_KEYS)
    assert admitted.intent.rev_set["policy"] == "candidate-proposal-only-policy-v1"
    assert admitted.intent.rev_set["capability"] == "extract-memory-candidate-v2"
    assert admitted.intent.rev_set["context_manifest"] == (
        f"candidate-context:sha256:{'a' * 64}"
    )
    assert admitted.intent.rev_set["handler"] == "candidate-memory-handler-v1"
    assert admitted.intent.rev_set["budget"] == (
        f"candidate-single-proposal-budget-v1:sha256:{'a' * 64}"
    )
    assert admitted.intent.rev_set["workflow"] == "candidate-memory-workflow-v1"
    for key in ("provider", "model_route", "secret"):
        assert admitted.intent.rev_set[key] == NOT_APPLICABLE


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"candidate_policy": {"requires_user_confirmation": False, "auto_promote_allowed": False}}, "proposal-only policy"),
        ({"candidate_policy": {"requires_user_confirmation": True, "auto_promote_allowed": True}}, "proposal-only policy"),
        ({"evidence_kind": "model_result"}, "source_content_read evidence"),
        ({"project_id": "project other"}, "project_id"),
        ({"source_id": ""}, "source_id"),
        ({"parent_job_id": ""}, "parent_job_id"),
    ],
)
def test_rejects_candidate_policy_evidence_and_binding_drift(changes: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _build(_payload(**changes))


def test_payload_and_gate_evidence_never_capture_content_or_secrets() -> None:
    secret = "never-persist-secret"
    content = "private source content must stay behind evidence reference"
    admitted = _build()

    rendered = repr(admitted.intent.payload) + repr(dict(admitted.authorization.gate_fact.budget_after))
    assert content not in rendered
    assert secret not in rendered
    assert "source-content-reads/content-read-alpha-r3" in rendered


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (_payload(execution_version="legacy-v1-readonly"), "execution_version"),
        (_payload(source_content="private"), "fields are not exact"),
        (_payload(credential={"secret": "never-persist-secret"}), "fields are not exact"),
    ],
)
def test_rejects_non_v2_or_noncanonical_job_payload(payload: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _build(payload)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"source": _source(id="other-source")}, "source record does not match source_id"),
        ({"source": _source(project_id="other-project")}, "source record does not match project_id"),
        ({"read": _read(id="other-read")}, "evidence_id"),
        ({"read": _read(source_id="other-source")}, "source_id"),
        ({"read": _read(status="pending")}, "must be completed"),
        ({"read": _read(text_sha256="not-a-content-evidence-token")}, "text_sha256"),
        ({"source_revision": 0}, "source_revision"),
        ({"source_revision": True}, "source_revision"),
        ({"read_revision": 0}, "source_content_read_revision"),
    ],
)
def test_requires_bound_completed_source_evidence_and_positive_revisions(
    kwargs: dict[str, object], message: str,
) -> None:
    with pytest.raises((TypeError, ValueError), match=message):
        _build(**kwargs)


def test_volatile_object_store_cas_does_not_change_effect_identity() -> None:
    first = _build(source_revision=3, read_revision=5)
    replay = _build(source_revision=4, read_revision=8)

    assert replay.intent.operation_id == first.intent.operation_id
    assert replay.intent.rev_set == first.intent.rev_set
    assert replay.authorization.gate_fact == first.authorization.gate_fact
