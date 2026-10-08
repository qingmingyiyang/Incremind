from __future__ import annotations

import json

import pytest

from backend.security.network_egress_profile import (
    NetworkEgressProfileConflict,
    NetworkEgressProfileError,
    NetworkEgressProfileStore,
    loopback_proxy_for_capability,
)


def test_missing_profile_is_non_persisted_direct_default(tmp_path) -> None:
    snapshot = NetworkEgressProfileStore(tmp_path).get()

    assert snapshot.persisted is False
    assert snapshot.store_revision == 0
    assert snapshot.profile.mode == "direct"
    assert snapshot.profile.literal_address is None
    assert snapshot.profile.port is None
    assert snapshot.profile.allowed_capabilities == ("anonymous_public_media",)


def test_confirmed_loopback_connect_profile_round_trips_with_cas_revision(tmp_path) -> None:
    store = NetworkEgressProfileStore(tmp_path)
    created = store.update(
        mode="loopback_http_connect",
        literal_address="127.0.0.1",
        port=7890,
        allowed_capabilities=("anonymous_public_media",),
        confirm_enable=True,
        expected_revision=0,
    )

    assert created.profile.revision == 1
    assert created.persisted is True
    assert NetworkEgressProfileStore(tmp_path).get() == created
    assert loopback_proxy_for_capability(
        created.profile, "anonymous_public_media"
    ).address == "127.0.0.1"

    updated = store.update(mode="direct", expected_revision=1)
    assert updated.profile.revision == 2
    assert updated.profile.mode == "direct"
    assert loopback_proxy_for_capability(updated.profile, "anonymous_public_media") is None


def test_loopback_enable_requires_explicit_confirmation(tmp_path) -> None:
    with pytest.raises(NetworkEgressProfileError, match="explicit confirmation"):
        NetworkEgressProfileStore(tmp_path).update(
            mode="loopback_http_connect",
            literal_address="::1",
            port=1080,
            expected_revision=0,
        )


@pytest.mark.parametrize("address", ["localhost", "proxy.example", "http://127.0.0.1", "10.0.0.1", "[::1]"])
def test_loopback_profile_rejects_non_literal_or_non_loopback_endpoint(tmp_path, address) -> None:
    with pytest.raises(NetworkEgressProfileError):
        NetworkEgressProfileStore(tmp_path).update(
            mode="loopback_http_connect",
            literal_address=address,
            port=8080,
            confirm_enable=True,
            expected_revision=0,
        )


@pytest.mark.parametrize("port", [0, 65536, True, "7890", None])
def test_loopback_profile_rejects_invalid_port(tmp_path, port) -> None:
    with pytest.raises(NetworkEgressProfileError):
        NetworkEgressProfileStore(tmp_path).update(
            mode="loopback_http_connect",
            literal_address="127.0.0.1",
            port=port,
            confirm_enable=True,
            expected_revision=0,
        )


@pytest.mark.parametrize("capabilities", [(), ("download",), ("anonymous_public_media", "anonymous_public_media")])
def test_profile_capabilities_are_strictly_allowlisted(tmp_path, capabilities) -> None:
    if capabilities == ():
        result = NetworkEgressProfileStore(tmp_path).update(
            mode="direct", allowed_capabilities=capabilities, expected_revision=0,
        )
        assert result.profile.allowed_capabilities == ()
    else:
        with pytest.raises(NetworkEgressProfileError):
            NetworkEgressProfileStore(tmp_path).update(
                mode="direct", allowed_capabilities=capabilities, expected_revision=0,
            )


def test_stale_writer_is_rejected(tmp_path) -> None:
    store = NetworkEgressProfileStore(tmp_path)
    store.update(mode="direct", expected_revision=0)

    with pytest.raises(NetworkEgressProfileConflict, match="expected 0, current 1"):
        store.update(mode="direct", expected_revision=0)


@pytest.mark.parametrize(
    "payload",
    [
        "{bad json",
        json.dumps({"schema_version": "1.0.0", "secret": "never"}),
        json.dumps({
            "schema_version": "1.0.0", "profile_id": "network-egress-local", "revision": 1,
            "mode": "loopback_http_connect", "literal_address": "localhost", "port": 7890,
            "allowed_capabilities": ["anonymous_public_media"],
        }),
        json.dumps({
            "schema_version": "1.0.0", "profile_id": "network-egress-local", "revision": 1,
            "mode": ["direct"], "literal_address": None, "port": None,
            "allowed_capabilities": ["anonymous_public_media"],
        }),
    ],
)
def test_corrupt_or_unsafe_persisted_profile_fails_closed(tmp_path, payload) -> None:
    path = tmp_path / "security/network-egress-profile.json"
    path.parent.mkdir(parents=True)
    path.write_text(payload, encoding="utf-8")

    with pytest.raises(NetworkEgressProfileError):
        NetworkEgressProfileStore(tmp_path).get()
