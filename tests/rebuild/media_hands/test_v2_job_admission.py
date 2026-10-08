from __future__ import annotations

import pytest

from core.effect_log import EFFECT_V2, EffectClass, GateDecision
from core.media_hands.effect_contract import EFFECT_KIND, provider_revision_identity
from core.media_hands.job_admission import (
    MediaHandsAdmissionCommand,
    MediaHandsJobAdmissionFactory,
)


class _Handler:
    provider_identity = ("bilibili-local", "provider-r7")


def _payload(*, credential: dict[str, object] | None = None) -> dict[str, object]:
    return {
        "id": "media_hands:bili-1:analyze_source",
        "job_type": "media_hands",
        "execution_version": EFFECT_V2,
        "attempt": 0,
        "media_hands": {
            "manifest": {"ref": "crp://jobs/source-manifests/bili-1", "revision": "manifest-r7"},
            "permission_snapshot": {
                "project_id": "project-media-test", "manifest_ref": "crp://jobs/source-manifests/bili-1",
                "manifest_revision": "manifest-r7", "grant_ref": "crp://jobs/source-permissions/project-media-test/bili-1/r1",
                "grant_revision": "grant-r1", "revocation_generation": 0,
            },
            "selection": {"ref": "crp://selections/media/bili-1", "revision": "selection-r3", "mode": "hands"},
            "operation": "analyze_source",
            "policy": {"revision": "policy-r9"},
            "budget": {
                "max_download_bytes": 10, "max_media_cpu_ms": 20, "max_asr_audio_ms": 30,
                "max_vision_frames": 4, "max_model_input_tokens": 5, "max_model_output_tokens": 6,
                "max_wall_ms": 40,
            },
            "credential_use_binding": credential,
        },
    }


def _command(**changes: object) -> MediaHandsAdmissionCommand:
    values: dict[str, object] = {
        "request_id": "request-media-admission-0001", "project_id": "project-media-test",
        "selection_ref": "crp://selections/media/bili-1", "selection_revision": "selection-r3",
        "selection_mode": "hands",
    }
    values.update(changes)
    return MediaHandsAdmissionCommand(**values)  # type: ignore[arg-type]


def _build(payload: dict[str, object] | None = None, **changes: object):
    return MediaHandsJobAdmissionFactory(admitted_at=100).build(
        job_payload=payload or _payload(), command=_command(**changes), handler=_Handler(),
    )


def test_builds_queryable_v2_admission_from_real_media_evidence() -> None:
    admitted = _build()

    assert admitted.intent.contract_version == EFFECT_V2
    assert admitted.intent.effect_class is EffectClass.QUERYABLE
    assert admitted.intent.kind == EFFECT_KIND
    assert admitted.intent.root_id == "media_hands:bili-1:analyze_source"
    assert admitted.intent.parent_id is None
    assert admitted.intent.payload == {
        "job_ref": admitted.authorization.admission_ref,
        "admission_ref": admitted.authorization.admission_ref,
        "mode": "admit", "attempt_index": 0,
    }
    assert admitted.authorization.gate_fact.decision is GateDecision.ALLOW
    assert admitted.authorization.gate_fact.budget_after["selection_ref"] == "crp://selections/media/bili-1"
    assert admitted.authorization.gate_fact.budget_after["permission_grant_revision"] == "grant-r1"
    assert admitted.intent.rev_set["capability"] == "media-hands-analyze_source-v2"
    assert admitted.intent.rev_set["budget"] == "policy-r9@analyze_source"
    assert admitted.intent.rev_set["provider"] == provider_revision_identity("bilibili-local", "provider-r7")


def test_closed_revision_set_and_controlled_credential_boundary_are_frozen() -> None:
    credential = {
        "provider": "bilibili-local", "authorization_revision": "profile-r4",
        "secret_generation": "secret-gen-8",
    }
    admitted = _build(_payload(credential=credential))

    assert set(admitted.intent.rev_set) == {
        "policy", "boundary", "capability", "context_manifest", "provider", "model_route",
        "bundle", "handler", "secret", "budget", "workflow",
    }
    assert admitted.intent.rev_set["boundary"] == "selection-r3@profile-r4"
    assert admitted.intent.rev_set["secret"] == "secret-gen-8"
    assert admitted.authorization.gate_fact.budget_after["boundary_profile_revision"] == "profile-r4"
    assert admitted.authorization.gate_fact.budget_after["secret_generation_revision"] == "secret-gen-8"


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda payload: payload.update(job_type="other"), "job_type"),
        (lambda payload: payload.pop("execution_version"), "execution_version=effect-v2"),
        (lambda payload: payload.update(attempt=1), "attempt=0"),
        (lambda payload: payload["media_hands"]["permission_snapshot"].update(project_id="other"), "permission project"),  # type: ignore[index]
        (lambda payload: payload["media_hands"]["permission_snapshot"].update(manifest_revision="other"), "permission snapshot"),  # type: ignore[index]
        (lambda payload: payload["media_hands"]["selection"].update(revision="other"), "selection drifted"),  # type: ignore[index]
        (lambda payload: payload["media_hands"]["budget"].pop("max_wall_ms"), "budget"),  # type: ignore[index]
    ],
)
def test_rejects_job_permission_selection_and_budget_drift(mutate, message: str) -> None:
    payload = _payload()
    mutate(payload)
    with pytest.raises(ValueError, match=message):
        _build(payload)


def test_rejects_command_provider_and_credential_drift() -> None:
    with pytest.raises(ValueError, match="selection drifted"):
        _build(selection_revision="selection-other")
    with pytest.raises(ValueError, match="selection_mode=hands"):
        _command(selection_mode="all")

    class BadHandler:
        provider_identity = ("provider id", "provider-r7")

    with pytest.raises(ValueError, match="provider_id"):
        MediaHandsJobAdmissionFactory(admitted_at=100).build(
            job_payload=_payload(), command=_command(), handler=BadHandler(),
        )
    payload = _payload(credential={
        "provider": "bilibili-local", "authorization_revision": "profile-r4",
        "secret_generation": "secret-gen-8", "secret": "plaintext-secret",
    })
    with pytest.raises(ValueError, match="fields are not exact"):
        _build(payload)
    with pytest.raises(ValueError, match="credential provider drifted"):
        _build(_payload(credential={
            "provider": "provider-other", "authorization_revision": "profile-r4",
            "secret_generation": "secret-gen-8",
        }))


def test_secret_body_never_enters_gate_or_intent() -> None:
    secret_body = "do-not-persist-this-secret"
    payload = _payload(credential={
        "provider": "bilibili-local", "authorization_revision": "profile-r4",
        "secret_generation": "secret-gen-8",
    })
    admitted = _build(payload)
    rendered = repr(admitted.intent.payload) + repr(dict(admitted.authorization.gate_fact.budget_after))

    assert secret_body not in rendered
    assert "secret-gen-8" in rendered
    with pytest.raises(ValueError, match="fields are not exact"):
        _build(_payload(credential={
            "provider": "bilibili-local", "authorization_revision": "profile-r4",
            "secret_generation": "secret-gen-8", "secret_body": secret_body,
        }))
