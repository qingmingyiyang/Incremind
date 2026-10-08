from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from core.job_runner.media_execution_evidence import (
    EVIDENCE_KIND,
    SCHEMA_VERSION,
    MediaOperationExecutionEvidence,
    can_transition_media_operation_execution_evidence,
    media_operation_execution_evidence_from_payload,
    media_operation_execution_evidence_to_payload,
    transition_media_operation_execution_evidence,
    validate_media_operation_execution_evidence,
)


def _evidence(
    *,
    state="started",
    receipt_ref=None,
    execution_id="execution-1",
    manifest_revision="r1",
    provider_revision="r1",
):
    return MediaOperationExecutionEvidence(
        job_id="job-media-1",
        source_id="source-media-1",
        operation="analyze_source",
        manifest_ref="crp://default/source-manifests/projects/project-1/media-manifest-1",
        manifest_revision=manifest_revision,
        provider_id="bilibili-public-media",
        provider_revision=provider_revision,
        execution_id=execution_id,
        state=state,
        receipt_ref=receipt_ref,
    )


def test_evidence_is_immutable_and_round_trips_through_exact_safe_schema() -> None:
    evidence = _evidence(state="completed", receipt_ref="crp://default/receipts/media-1")

    with pytest.raises(FrozenInstanceError):
        evidence.state = "started"  # type: ignore[misc]

    payload = media_operation_execution_evidence_to_payload(evidence)
    assert payload == {
        "schema_version": SCHEMA_VERSION,
        "kind": EVIDENCE_KIND,
        "job_id": "job-media-1",
        "source_id": "source-media-1",
        "operation": "analyze_source",
        "manifest_ref": "crp://default/source-manifests/projects/project-1/media-manifest-1",
        "manifest_revision": "r1",
        "provider_id": "bilibili-public-media",
        "provider_revision": "r1",
        "execution_id": "execution-1",
        "state": "completed",
        "receipt_ref": "crp://default/receipts/media-1",
    }
    assert media_operation_execution_evidence_from_payload(payload) == evidence


@pytest.mark.parametrize(
    ("evidence", "message"),
    [
        (_evidence(state="completed"), "requires a receipt"),
        (_evidence(state="started", receipt_ref="crp://default/receipts/media-1"), "contains a receipt"),
        (_evidence(state="unexpected"), "state is invalid"),
        (_evidence(execution_id="bad execution id"), "execution id is invalid"),
        (_evidence(manifest_revision="bad revision"), "manifest revision is invalid"),
    ],
)
def test_evidence_validation_rejects_invalid_lifecycle_values(evidence, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        validate_media_operation_execution_evidence(evidence)


def test_payload_rejects_sensitive_or_unrecognized_fields() -> None:
    payload = media_operation_execution_evidence_to_payload(_evidence())

    with pytest.raises(ValueError, match="sensitive"):
        media_operation_execution_evidence_from_payload({**payload, "prompt": "private"})
    with pytest.raises(ValueError, match="fields are not exact"):
        media_operation_execution_evidence_from_payload({**payload, "attempt": 1})


def test_started_can_finish_or_quarantine_but_cannot_be_replayed() -> None:
    started = _evidence()
    completed = _evidence(state="completed", receipt_ref="crp://default/receipts/media-1")
    unknown = _evidence(state="unknown_effect")

    assert can_transition_media_operation_execution_evidence(started, completed)
    assert can_transition_media_operation_execution_evidence(started, unknown)
    assert not can_transition_media_operation_execution_evidence(completed, started)
    with pytest.raises(ValueError, match="transition is invalid"):
        transition_media_operation_execution_evidence(completed, started)


def test_unknown_effect_can_only_reconcile_to_verified_completed_receipt() -> None:
    unknown = _evidence(state="unknown_effect")
    reconciled = _evidence(state="completed", receipt_ref="crp://default/receipts/media-1")

    assert transition_media_operation_execution_evidence(unknown, reconciled) == reconciled
    assert not can_transition_media_operation_execution_evidence(unknown, _evidence())
    assert not can_transition_media_operation_execution_evidence(
        unknown, _evidence(state="completed", receipt_ref="crp://default/receipts/media-2", execution_id="execution-2")
    )


def test_transition_requires_the_admitted_manifest_and_provider_identity() -> None:
    started = _evidence()
    completed = _evidence(state="completed", receipt_ref="crp://default/receipts/media-1")

    assert not can_transition_media_operation_execution_evidence(
        started, _evidence(state="completed", receipt_ref="crp://default/receipts/media-1", manifest_revision="r2")
    )
    assert not can_transition_media_operation_execution_evidence(
        started, _evidence(state="completed", receipt_ref="crp://default/receipts/media-1", provider_revision="r2")
    )
    assert can_transition_media_operation_execution_evidence(started, completed)
