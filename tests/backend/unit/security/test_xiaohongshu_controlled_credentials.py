from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from backend.security.secrets import InMemorySecretStore
from backend.security.xiaohongshu_controlled_credentials import (
    XiaohongshuControlledCredentialAuthority,
    XiaohongshuControlledCredentialConflict,
    XiaohongshuControlledCredentialError,
    XiaohongshuControlledCredentialIndeterminate,
)


_EXPIRY = "2030-01-02T03:04:05Z"


def test_grant_verify_rotate_revoke_and_historical_command_replay_never_expose_cookie(tmp_path: Path) -> None:
    secrets = InMemorySecretStore()
    authority = XiaohongshuControlledCredentialAuthority(tmp_path, secret_store=secrets)

    granted = authority.grant(**_grant(command_id="grant-1"))
    assert granted.public() == {
        "authorization_id": granted.authorization_id,
        "project_id": "project-a", "provider": "xiaohongshu", "credential_subject_id": "account-a",
        "boundary_profile_id": "boundary-a", "boundary_revision": 3,
        "authorization_revision": 1, "state": "active", "expires_at": _EXPIRY, "secret_generation": 1,
    }
    assert authority.grant(**_grant(command_id="grant-1")) == granted
    assert "xhs-cookie-one" not in str(granted.public())
    database = tmp_path / ".rebuild-data" / "security" / "xiaohongshu-controlled-credentials.sqlite3"
    assert b"xhs-cookie-one" not in database.read_bytes()

    witness = authority.verify(**_frozen(granted))
    assert witness.public()["secret_generation"] == 1
    assert "cookie" not in repr(witness).lower()

    rotated = authority.rotate(**_grant(command_id="rotate-1", cookie_value="xhs-cookie-two", expected_authorization_revision=1))
    assert rotated.authorization_revision == 2
    assert rotated.secret_generation == 2
    assert authority.rotate(**_grant(command_id="rotate-1", cookie_value="different-but-never-stored", expected_authorization_revision=1)) == rotated
    assert authority.grant(**_grant(command_id="grant-1")) == granted
    with pytest.raises(XiaohongshuControlledCredentialError, match="drifted"):
        authority.verify(**_frozen(granted))

    revoked = authority.revoke(project_id="project-a", credential_subject_id="account-a", expected_authorization_revision=2, command_id="revoke-1")
    assert revoked.state == "revoked" and revoked.authorization_revision == 3
    assert authority.revoke(project_id="project-a", credential_subject_id="account-a", expected_authorization_revision=2, command_id="revoke-1") == revoked
    with pytest.raises(XiaohongshuControlledCredentialError, match="unavailable"):
        authority.verify(**_frozen(rotated))


def test_verify_fails_closed_for_project_subject_boundary_expiry_and_secret_generation_drift(tmp_path: Path) -> None:
    secrets = InMemorySecretStore()
    authority = XiaohongshuControlledCredentialAuthority(tmp_path, secret_store=secrets)
    granted = authority.grant(**_grant(command_id="grant-1"))
    frozen = _frozen(granted)
    for key, value in (("project_id", "project-b"), ("credential_subject_id", "account-b"), ("boundary_profile_id", "boundary-b"), ("boundary_revision", 4), ("secret_generation", 2)):
        candidate = dict(frozen)
        candidate[key] = value
        with pytest.raises(XiaohongshuControlledCredentialError):
            authority.verify(**candidate)
    with pytest.raises(XiaohongshuControlledCredentialError, match="drifted"):
        authority.verify(**frozen, now=datetime(2031, 1, 1, tzinfo=timezone.utc))
    # An out-of-band Secret Store rotation invalidates a frozen manifest before
    # any caller can reuse it for another platform request.
    secret_key = next(iter(secrets._values))
    secrets.set(secret_key, "changed-outside-authority")
    with pytest.raises(XiaohongshuControlledCredentialError, match="drifted"):
        authority.verify(**frozen)


def test_os_owned_wire_lease_has_no_public_cookie_and_stops_after_rotation(tmp_path: Path) -> None:
    authority = XiaohongshuControlledCredentialAuthority(tmp_path, secret_store=InMemorySecretStore())
    granted = authority.grant(**_grant(command_id="grant-1"))
    lease = authority.issue_wire_lease(**_frozen(granted))
    assert lease.public() == lease.witness.public()
    assert "xhs-cookie-one" not in repr(lease)
    assert "xhs-cookie-one" not in str(lease.public())
    assert not hasattr(lease, "_cookie_value")
    assert lease.generation_current() is True
    # Only the OS-owned governed adapter receives this value; it is not part of
    # an event/public projection and revalidates immediately before return.
    assert lease.cookie_header_value() == "xhs-cookie-one"
    with pytest.raises(XiaohongshuControlledCredentialError, match="already used") as error:
        lease.cookie_header_value()
    assert "xhs-cookie-one" not in str(error.value)
    # The lease's one-wire state does not prevent its caller from observing a
    # later rotation/revocation fence after that one request has been issued.
    assert lease.generation_current() is True
    authority.rotate(**_grant(command_id="rotate-1", cookie_value="xhs-cookie-two", expected_authorization_revision=1))
    assert lease.generation_current() is False
    with pytest.raises(XiaohongshuControlledCredentialError, match="already used"):
        lease.cookie_header_value()


def test_cas_command_semantics_and_failed_secret_write_do_not_delete_existing_secret(tmp_path: Path) -> None:
    secrets = _FailingSecretStore()
    authority = XiaohongshuControlledCredentialAuthority(tmp_path, secret_store=secrets)
    granted = authority.grant(**_grant(command_id="grant-1"))
    secret_key = next(iter(secrets._values))
    before = secrets.get_snapshot(secret_key)
    with pytest.raises(XiaohongshuControlledCredentialConflict, match="revision changed"):
        authority.rotate(**_grant(command_id="rotate-stale", expected_authorization_revision=0))
    with pytest.raises(XiaohongshuControlledCredentialConflict, match="already used"):
        authority.grant(**_grant(command_id="grant-1", boundary_profile_id="boundary-other", expected_authorization_revision=1))
    secrets.fail = True
    with pytest.raises(XiaohongshuControlledCredentialError, match="secret update failed") as error:
        authority.rotate(**_grant(command_id="rotate-fails", expected_authorization_revision=1))
    assert "xhs-cookie-one" not in str(error.value)
    assert secrets.get_snapshot(secret_key) == before
    assert authority.current(project_id="project-a", credential_subject_id="account-a") == granted


def test_secret_effect_crash_leaves_durable_indeterminate_intent_and_never_replays(tmp_path: Path) -> None:
    secrets = _WriteThenCrashSecretStore()
    authority = XiaohongshuControlledCredentialAuthority(tmp_path, secret_store=secrets)
    with pytest.raises(XiaohongshuControlledCredentialError, match="secret update failed"):
        authority.grant(**_grant(command_id="grant-crash"))
    assert authority.current(project_id="project-a", credential_subject_id="account-a") is None
    with pytest.raises(XiaohongshuControlledCredentialIndeterminate, match="indeterminate"):
        authority.grant(**_grant(command_id="grant-crash", cookie_value="must-not-replay"))
    with pytest.raises(XiaohongshuControlledCredentialIndeterminate, match="indeterminate"):
        authority.grant(**_grant(command_id="another-command"))
    assert "must-not-replay" not in repr(secrets._values)
    assert authority.quarantine_indeterminate(
        project_id="project-a", credential_subject_id="account-a",
        pending_command_id="grant-crash", reconciliation_command_id="reconcile-1",
    ) is None
    with pytest.raises(XiaohongshuControlledCredentialConflict, match="identity"):
        authority.quarantine_indeterminate(
            project_id="project-b", credential_subject_id="account-a",
            pending_command_id="grant-crash", reconciliation_command_id="reconcile-1",
        )
    assert authority.quarantine_indeterminate(
        project_id="project-a", credential_subject_id="account-a",
        pending_command_id="grant-crash", reconciliation_command_id="reconcile-1",
    ) is None
    recovered = authority.grant(**_grant(command_id="grant-recovered"))
    assert recovered.state == "active" and recovered.authorization_revision == 1


def test_rotate_crash_quarantine_revokes_old_binding_before_reauthorization(tmp_path: Path) -> None:
    secrets = _CrashOnDemandSecretStore()
    authority = XiaohongshuControlledCredentialAuthority(tmp_path, secret_store=secrets)
    granted = authority.grant(**_grant(command_id="grant-1"))
    secrets.crash_next = True
    with pytest.raises(XiaohongshuControlledCredentialError, match="secret update failed"):
        authority.rotate(**_grant(
            command_id="rotate-crash", expected_authorization_revision=1,
            cookie_value="rotated-before-crash",
        ))
    with pytest.raises(XiaohongshuControlledCredentialError):
        authority.verify(**_frozen(granted))
    quarantined = authority.quarantine_indeterminate(
        project_id="project-a", credential_subject_id="account-a",
        pending_command_id="rotate-crash", reconciliation_command_id="reconcile-rotate",
    )
    assert quarantined is not None
    assert quarantined.state == "revoked" and quarantined.authorization_revision == 2
    recovered = authority.grant(**_grant(
        command_id="grant-after-quarantine", expected_authorization_revision=2,
        cookie_value="fresh-explicit-cookie",
    ))
    assert recovered.state == "active" and recovered.authorization_revision == 3


class _FailingSecretStore(InMemorySecretStore):
    fail = False

    def set(self, key: str, value: str) -> None:
        if self.fail:
            raise RuntimeError("simulated Secret Store failure")
        super().set(key, value)


class _WriteThenCrashSecretStore(InMemorySecretStore):
    crashed = False

    def set(self, key: str, value: str) -> None:
        super().set(key, value)
        if not self.crashed:
            self.crashed = True
            raise RuntimeError("simulated crash after Secret Store write")


class _CrashOnDemandSecretStore(InMemorySecretStore):
    crash_next = False

    def set(self, key: str, value: str) -> None:
        super().set(key, value)
        if self.crash_next:
            self.crash_next = False
            raise RuntimeError("simulated crash after Secret Store rotation")


def _grant(**overrides: object) -> dict[str, object]:
    result: dict[str, object] = {
        "project_id": "project-a", "credential_subject_id": "account-a", "boundary_profile_id": "boundary-a",
        "boundary_revision": 3, "expires_at": _EXPIRY, "cookie_value": "xhs-cookie-one",
        "expected_authorization_revision": 0, "command_id": "grant-default",
    }
    result.update(overrides)
    return result


def _frozen(item) -> dict[str, object]:
    return {
        "project_id": item.project_id, "provider": item.provider,
        "credential_subject_id": item.credential_subject_id, "boundary_profile_id": item.boundary_profile_id,
        "boundary_revision": item.boundary_revision, "authorization_revision": item.authorization_revision,
        "secret_generation": item.secret_generation,
    }
