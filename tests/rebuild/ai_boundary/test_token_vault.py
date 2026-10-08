from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.ai_boundary import EphemeralTokenVault, TokenVaultError


NOW = datetime(2026, 8, 23, tzinfo=timezone.utc)


def test_exact_token_rehydrates_once_in_trusted_bound_projection() -> None:
    vault = EphemeralTokenVault()
    token = _token(vault)
    output = vault.rehydrate_exact(
        f"联系人：{token}",
        turn_id="turn-1",
        destination_id="provider-a",
        trusted_projection=True,
        now=NOW,
    )
    assert output == "联系人：alice@example.com"
    with pytest.raises(TokenVaultError, match="unknown or expired"):
        vault.rehydrate_exact(
            token,
            turn_id="turn-1",
            destination_id="provider-a",
            trusted_projection=True,
            now=NOW,
        )


@pytest.mark.parametrize(
    ("turn_id", "destination_id"),
    [("turn-2", "provider-a"), ("turn-1", "provider-b")],
)
def test_token_binding_mismatch_fails_closed(turn_id: str, destination_id: str) -> None:
    vault = EphemeralTokenVault()
    token = _token(vault)
    with pytest.raises(TokenVaultError, match="binding mismatch"):
        vault.rehydrate_exact(
            token,
            turn_id=turn_id,
            destination_id=destination_id,
            trusted_projection=True,
            now=NOW,
        )


def test_untrusted_projection_cannot_rehydrate() -> None:
    vault = EphemeralTokenVault()
    token = _token(vault)
    with pytest.raises(TokenVaultError, match="trusted local projection"):
        vault.rehydrate_exact(
            token,
            turn_id="turn-1",
            destination_id="provider-a",
            trusted_projection=False,
            now=NOW,
        )


def test_expired_and_unknown_tokens_fail_closed() -> None:
    vault = EphemeralTokenVault()
    token = _token(vault, ttl=timedelta(seconds=1))
    with pytest.raises(TokenVaultError, match="unknown or expired"):
        vault.rehydrate_exact(
            token,
            turn_id="turn-1",
            destination_id="provider-a",
            trusted_projection=True,
            now=NOW + timedelta(seconds=2),
        )
    with pytest.raises(TokenVaultError, match="unknown or expired"):
        vault.rehydrate_exact(
            "[[CRP:EMAIL:abcdefghijklmnopqrstuvwxyz]]",
            turn_id="turn-1",
            destination_id="provider-a",
            trusted_projection=True,
            now=NOW,
        )


def test_duplicate_token_in_one_projection_is_rejected_as_replay() -> None:
    vault = EphemeralTokenVault()
    token = _token(vault)
    with pytest.raises(TokenVaultError, match="replay"):
        vault.rehydrate_exact(
            f"{token} {token}",
            turn_id="turn-1",
            destination_id="provider-a",
            trusted_projection=True,
            now=NOW,
        )


def test_vault_capacity_and_ttl_are_bounded() -> None:
    vault = EphemeralTokenVault(max_entries=1)
    _token(vault)
    with pytest.raises(TokenVaultError, match="capacity"):
        _token(vault, value="bob@example.com")
    with pytest.raises(TokenVaultError, match="within one hour"):
        _token(EphemeralTokenVault(), ttl=timedelta(hours=2))


def _token(
    vault: EphemeralTokenVault,
    *,
    value: str = "alice@example.com",
    ttl: timedelta = timedelta(minutes=10),
) -> str:
    return vault.tokenize(
        value,
        data_class="email",
        turn_id="turn-1",
        destination_id="provider-a",
        ttl=ttl,
        now=NOW,
    )
