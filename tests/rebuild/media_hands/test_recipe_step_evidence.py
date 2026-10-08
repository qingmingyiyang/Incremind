from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from core.job_runner.media_recipe_step_evidence import (
    EVIDENCE_KIND,
    SCHEMA_VERSION,
    MediaRecipeStepEvidence,
    can_transition_media_recipe_step_evidence,
    media_recipe_step_evidence_from_payload,
    media_recipe_step_evidence_to_payload,
    transition_media_recipe_step_evidence,
    validate_media_recipe_step_evidence,
)


def _evidence(
    *,
    state: str = "started",
    receipt_ref: str | None = None,
    execution_id: str = "execution-media-1",
    provider_revision: str = "provider-r1",
    step_name: str = "fetch_audio",
    input_state_hash: str = "sha256:" + "a" * 64,
) -> MediaRecipeStepEvidence:
    return MediaRecipeStepEvidence(
        job_id="job-media-1",
        execution_id=execution_id,
        provider_id="bilibili-public-media",
        provider_revision=provider_revision,
        step_name=step_name,
        input_state_hash=input_state_hash,
        state=state,  # type: ignore[arg-type]
        receipt_ref=receipt_ref,
    )


def test_recipe_step_evidence_is_immutable_and_round_trips_exact_safe_schema() -> None:
    evidence = _evidence(
        state="completed",
        receipt_ref="crp://default/media-receipts/execution-media-1/fetch-audio",
    )

    with pytest.raises(FrozenInstanceError):
        evidence.state = "started"  # type: ignore[misc]

    payload = media_recipe_step_evidence_to_payload(evidence)
    assert payload == {
        "schema_version": SCHEMA_VERSION,
        "kind": EVIDENCE_KIND,
        "job_id": "job-media-1",
        "execution_id": "execution-media-1",
        "provider_id": "bilibili-public-media",
        "provider_revision": "provider-r1",
        "step_name": "fetch_audio",
        "input_state_hash": "sha256:" + "a" * 64,
        "state": "completed",
        "receipt_ref": "crp://default/media-receipts/execution-media-1/fetch-audio",
    }
    assert media_recipe_step_evidence_from_payload(payload) == evidence


@pytest.mark.parametrize(
    ("evidence", "message"),
    [
        (_evidence(state="completed"), "requires a receipt"),
        (_evidence(receipt_ref="crp://default/media-receipts/execution-media-1"), "contains a receipt"),
        (_evidence(state="unexpected"), "state is invalid"),
        (_evidence(execution_id="bad execution id"), "execution id is invalid"),
        (_evidence(step_name="FetchAudio"), "step name is invalid"),
        (_evidence(input_state_hash="untrusted-input"), "input state hash is invalid"),
    ],
)
def test_recipe_step_evidence_rejects_invalid_lifecycle_and_identity(
    evidence: MediaRecipeStepEvidence, message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        validate_media_recipe_step_evidence(evidence)


def test_payload_is_exact_and_rejects_sensitive_or_wrongly_typed_values() -> None:
    payload = media_recipe_step_evidence_to_payload(_evidence())

    with pytest.raises(ValueError, match="sensitive"):
        media_recipe_step_evidence_from_payload({**payload, "url": "https://private.example"})
    with pytest.raises(ValueError, match="fields are not exact"):
        media_recipe_step_evidence_from_payload({**payload, "attempt": 1})
    with pytest.raises(ValueError, match="receipt reference is invalid"):
        media_recipe_step_evidence_from_payload({**payload, "receipt_ref": 1})


def test_only_the_two_permitted_monotonic_transition_paths_are_allowed() -> None:
    started = _evidence()
    unknown = _evidence(state="unknown_effect")
    completed = _evidence(
        state="completed", receipt_ref="crp://default/media-receipts/execution-media-1/fetch-audio",
    )

    assert can_transition_media_recipe_step_evidence(started, unknown)
    assert can_transition_media_recipe_step_evidence(started, completed)
    assert transition_media_recipe_step_evidence(unknown, completed) == completed
    assert not can_transition_media_recipe_step_evidence(started, started)
    assert not can_transition_media_recipe_step_evidence(unknown, unknown)
    assert not can_transition_media_recipe_step_evidence(completed, started)
    assert not can_transition_media_recipe_step_evidence(completed, unknown)
    with pytest.raises(ValueError, match="transition is invalid"):
        transition_media_recipe_step_evidence(completed, started)


@pytest.mark.parametrize(
    "candidate",
    [
        _evidence(
            state="completed",
            receipt_ref="crp://default/media-receipts/execution-media-1/fetch-audio",
            execution_id="execution-media-2",
        ),
        _evidence(
            state="completed",
            receipt_ref="crp://default/media-receipts/execution-media-1/fetch-audio",
            provider_revision="provider-r2",
        ),
        _evidence(
            state="completed",
            receipt_ref="crp://default/media-receipts/execution-media-1/fetch-audio",
            step_name="transcribe_audio",
        ),
        _evidence(
            state="completed",
            receipt_ref="crp://default/media-receipts/execution-media-1/fetch-audio",
            input_state_hash="sha256:" + "b" * 64,
        ),
    ],
)
def test_transition_cannot_drift_any_recipe_step_identity(
    candidate: MediaRecipeStepEvidence,
) -> None:
    started = _evidence()
    assert not can_transition_media_recipe_step_evidence(started, candidate)
