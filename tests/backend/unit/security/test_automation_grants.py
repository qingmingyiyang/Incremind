from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from backend.security.automation_grants import (
    AutomationGrantBinding,
    AutomationGrantConflict,
    AutomationGrantError,
    AutomationGrantRepository,
    canonical_parameter_digest,
)
from backend.security.secrets import InMemorySecretStore


def _expiry() -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat().replace("+00:00", "Z")


def _binding(secret_generation: int = 1, *, target: str = "api.example.com") -> AutomationGrantBinding:
    return AutomationGrantBinding(
        project_id="project-1", operation_id="publish.post",
        parameter_digest=canonical_parameter_digest({"post_id": "post-1", "visibility": "private"}),
        effect_kind="network_write", capability_revision=3, target=target,
        boundary_profile_id="boundary-1", boundary_revision=7,
        secret_refs=(("publish-token", secret_generation),),
    )


def _repository(tmp_path):
    secrets = InMemorySecretStore({"publish-token": "not-in-grant"})
    return AutomationGrantRepository(tmp_path, secret_store=secrets), secrets


def test_grant_claim_and_receipt_are_durable_and_exact(tmp_path) -> None:
    repository, _ = _repository(tmp_path)
    created = repository.create(binding=_binding(), expires_at=_expiry(), max_uses=2, command_id="create-1")
    assert created.uses_consumed == 0
    assert created.receipt_ref is None
    assert "not-in-grant" not in str(created.public())

    claim = repository.claim(grant_id=created.grant_id, binding=_binding(), expected_revision=created.revision)
    assert claim.grant.uses_consumed == 1
    completed = repository.complete(claim_id=claim.claim_id, receipt_ref="receipt-1")
    assert completed.receipt_ref == "receipt-1"
    assert repository.get(created.grant_id) == completed


def test_completion_accepts_bounded_canonical_receipt_reference(tmp_path) -> None:
    repository, _ = _repository(tmp_path)
    created = repository.create(
        binding=_binding(), expires_at=_expiry(), max_uses=1, command_id="long-receipt",
    )
    claim = repository.claim(
        grant_id=created.grant_id, binding=_binding(), expected_revision=created.revision,
    )
    receipt_ref = "receipt:memory-projection-rebuild/eff2_" + "a" * 64

    assert repository.complete(
        claim_id=claim.claim_id, receipt_ref=receipt_ref,
    ).receipt_ref == receipt_ref


@pytest.mark.parametrize("changed", ["target", "effect", "capability", "boundary", "parameter"])
def test_any_authorized_binding_drift_invalidates_grant(tmp_path, changed: str) -> None:
    repository, _ = _repository(tmp_path)
    created = repository.create(binding=_binding(), expires_at=_expiry(), max_uses=2, command_id=f"create-{changed}")
    field = {"effect": "effect_kind", "capability": "capability_revision", "boundary": "boundary_revision", "parameter": "parameter_digest"}.get(changed, changed)
    changed_value = 4 if field.endswith("revision") else "different-target" if field == "target" else canonical_parameter_digest({"post_id": "post-2"}) if field == "parameter_digest" else "file_write"
    replacement = replace(_binding(), **{field: changed_value})
    with pytest.raises(AutomationGrantError, match="binding_drift"):
        repository.claim(grant_id=created.grant_id, binding=replacement, expected_revision=created.revision)
    assert repository.get(created.grant_id).state == "invalidated"  # type: ignore[union-attr]


def test_secret_rotation_expiry_revoke_and_exhaustion_reject(tmp_path) -> None:
    repository, secrets = _repository(tmp_path)
    created = repository.create(binding=_binding(), expires_at=_expiry(), max_uses=1, command_id="create-one")
    secrets.set("publish-token", "rotated")
    with pytest.raises(AutomationGrantError, match="secret_drift"):
        repository.claim(grant_id=created.grant_id, binding=_binding(), expected_revision=created.revision)
    assert repository.get(created.grant_id).state == "invalidated"  # type: ignore[union-attr]

    usable = repository.create(binding=_binding(secret_generation=2), expires_at=_expiry(), max_uses=1, command_id="create-two")
    claim = repository.claim(grant_id=usable.grant_id, binding=_binding(secret_generation=2), expected_revision=usable.revision)
    with pytest.raises(AutomationGrantError, match="exhausted"):
        repository.claim(grant_id=usable.grant_id, binding=_binding(secret_generation=2), expected_revision=claim.grant.revision)
    revoked = repository.create(binding=_binding(secret_generation=2), expires_at=_expiry(), max_uses=2, command_id="create-three")
    repository.revoke(grant_id=revoked.grant_id, expected_revision=revoked.revision)
    with pytest.raises(AutomationGrantError, match="revoked"):
        repository.claim(grant_id=revoked.grant_id, binding=_binding(secret_generation=2), expected_revision=2)


def test_parameters_refuse_secret_fields_and_request_bodies() -> None:
    with pytest.raises(AutomationGrantError, match="protected field"):
        canonical_parameter_digest({"api_key": "value"})
    with pytest.raises(AutomationGrantError, match="body"):
        canonical_parameter_digest({"title": "hello\n" + "x"})
    assert canonical_parameter_digest({"b": 2, "a": 1}) == canonical_parameter_digest({"a": 1, "b": 2})


def test_command_replay_is_idempotent_and_conflicting_command_is_rejected(tmp_path) -> None:
    repository, _ = _repository(tmp_path)
    first = repository.create(binding=_binding(), expires_at=_expiry(), max_uses=1, command_id="same-command")
    replay = repository.create(binding=_binding(), expires_at=first.expires_at, max_uses=1, command_id="same-command")
    assert replay == first
    with pytest.raises(AutomationGrantConflict):
        repository.create(binding=_binding(target="other.example.com"), expires_at=first.expires_at, max_uses=1, command_id="same-command")
