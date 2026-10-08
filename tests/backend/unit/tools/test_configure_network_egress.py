from __future__ import annotations

from tools.configure_network_egress import configure


def test_configure_requires_confirmation_and_never_echoes_endpoint(tmp_path) -> None:
    rejected = configure(
        tmp_path,
        mode="loopback_http_connect",
        address="127.0.0.1",
        port=7890,
        expected_revision=0,
    )
    assert rejected == {
        "status": "input_invalid",
        "error_code": "network_profile_invalid",
    }

    updated = configure(
        tmp_path,
        mode="loopback_http_connect",
        address="127.0.0.1",
        port=7890,
        confirm_enable=True,
        expected_revision=0,
    )
    assert updated == {
        "status": "updated",
        "mode": "loopback_http_connect",
        "revision": 1,
        "allowed_capabilities": ["anonymous_public_media"],
    }
    assert "address" not in updated
    assert "port" not in updated


def test_configure_uses_cas_and_can_return_to_direct(tmp_path) -> None:
    first = configure(tmp_path, mode="direct", expected_revision=0)
    assert first["revision"] == 1
    assert configure(tmp_path, mode="direct", expected_revision=0) == {
        "status": "conflict",
        "error_code": "revision_conflict",
    }
    second = configure(tmp_path, mode="direct", expected_revision=1)
    assert second["revision"] == 2
