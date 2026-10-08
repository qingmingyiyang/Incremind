from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from core.job_runner.media_recipe_step_receipt import (
    RECEIPT_KIND,
    SCHEMA_VERSION,
    MediaRecipeStepReceipt,
    media_recipe_step_receipt_from_payload,
    media_recipe_step_receipt_to_payload,
    validate_media_recipe_step_receipt,
)


def _receipt(
    *,
    output_ref: str = "crp://default/media-outputs/job-media-1/fetch-audio",
    input_state_hash: str = "sha256:" + "a" * 64,
    output_state_hash: str = "sha256:" + "b" * 64,
    consumed: object = None,
) -> MediaRecipeStepReceipt:
    return MediaRecipeStepReceipt(
        job_id="job-media-1",
        execution_id="execution-media-1",
        provider_id="bilibili-public-media",
        provider_revision="provider-r1",
        step_name="fetch_audio",
        input_state_hash=input_state_hash,
        receipt_ref="crp://default/media-receipts/execution-media-1/fetch-audio",
        output_ref=output_ref,
        output_state_hash=output_state_hash,
        consumed={"wall_ms": 12, "audio_ms": 0} if consumed is None else consumed,  # type: ignore[arg-type]
    )


def test_recipe_step_receipt_is_immutable_and_round_trips_exact_safe_schema() -> None:
    receipt = _receipt()

    with pytest.raises(FrozenInstanceError):
        receipt.step_name = "probe_audio"  # type: ignore[misc]

    payload = media_recipe_step_receipt_to_payload(receipt)
    assert payload == {
        "schema_version": SCHEMA_VERSION,
        "kind": RECEIPT_KIND,
        "job_id": "job-media-1",
        "execution_id": "execution-media-1",
        "provider_id": "bilibili-public-media",
        "provider_revision": "provider-r1",
        "step_name": "fetch_audio",
        "input_state_hash": "sha256:" + "a" * 64,
        "receipt_ref": "crp://default/media-receipts/execution-media-1/fetch-audio",
        "output_ref": "crp://default/media-outputs/job-media-1/fetch-audio",
        "output_state_hash": "sha256:" + "b" * 64,
        "consumed": {"wall_ms": 12, "audio_ms": 0},
    }
    assert media_recipe_step_receipt_from_payload(payload) == receipt


@pytest.mark.parametrize(
    ("receipt", "message"),
    [
        (_receipt(output_ref="https://uncontrolled.example/output"), "output reference is invalid"),
        (_receipt(input_state_hash="state"), "input state hash is invalid"),
        (_receipt(output_state_hash="sha256:" + "A" * 64), "output state hash is invalid"),
        (_receipt(consumed={"wall_ms": -1}), "consumption is invalid"),
        (_receipt(consumed={"wall_ms": True}), "consumption is invalid"),
        (_receipt(consumed={"wall_ms": 1.5}), "consumption is invalid"),
    ],
)
def test_recipe_step_receipt_rejects_untrusted_references_hashes_and_consumption(
    receipt: MediaRecipeStepReceipt, message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        validate_media_recipe_step_receipt(receipt)


def test_payload_is_exact_and_recursively_rejects_sensitive_keys() -> None:
    payload = media_recipe_step_receipt_to_payload(_receipt())

    with pytest.raises(ValueError, match="fields are not exact"):
        media_recipe_step_receipt_from_payload({**payload, "attempt": 1})
    with pytest.raises(ValueError, match="sensitive"):
        media_recipe_step_receipt_from_payload({**payload, "download_url": "https://private.example"})
    with pytest.raises(ValueError, match="sensitive"):
        media_recipe_step_receipt_from_payload({**payload, "consumed": {"wall_ms": 1, "nested": {"cookie": "x"}}})
    with pytest.raises(ValueError, match="sensitive"):
        media_recipe_step_receipt_from_payload({**payload, "consumed": {"network_bytes": 1}})


def test_payload_rejects_non_integer_consumption_and_non_crp_references() -> None:
    payload = media_recipe_step_receipt_to_payload(_receipt())

    with pytest.raises(ValueError, match="consumption is invalid"):
        media_recipe_step_receipt_from_payload({**payload, "consumed": {"wall_ms": False}})
    with pytest.raises(ValueError, match="receipt reference is invalid"):
        media_recipe_step_receipt_from_payload({**payload, "receipt_ref": "file:///private/receipt"})
    with pytest.raises(ValueError, match="output reference is invalid"):
        media_recipe_step_receipt_from_payload({**payload, "output_ref": "https://outside.example/output"})
