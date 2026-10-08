from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker


ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "core-contracts" / "desktop"


def _load(name: str) -> dict[str, object]:
    return json.loads((CONTRACT / "fixtures" / name).read_text(encoding="utf-8"))


def _validator() -> Draft202012Validator:
    schema = json.loads((CONTRACT / "loopback-session.schema.json").read_text(encoding="utf-8"))
    return Draft202012Validator(schema, format_checker=FormatChecker())


def _parse_time(value: object) -> datetime:
    assert isinstance(value, str)
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def _require_session_compatibility(
    manifest: dict[str, object], health: dict[str, object], *, now: datetime
) -> None:
    if _parse_time(manifest["expires_at"]) <= now:
        raise ValueError("desktop_session_expired")
    for key in ("secret", "nonce"):
        value = manifest[key]
        assert isinstance(value, str)
        if len(set(value)) < 16:
            raise ValueError("desktop_session_config_invalid")
    if health["status"] != "ready":
        raise ValueError("desktop_session_health_mismatch")
    for key in ("protocol_version", "instance_id", "nonce", "child_pid"):
        if health[key] != manifest[key]:
            raise ValueError("desktop_session_health_mismatch")
    if health["session_expires_at"] != manifest["expires_at"]:
        raise ValueError("desktop_session_health_mismatch")
    if health["auth_required"] is not True or health["renderer_secret_access"] is not False:
        raise ValueError("desktop_session_health_mismatch")


def test_loopback_manifest_health_and_error_fixtures_validate() -> None:
    validator = _validator()
    for name in ("valid-session-manifest.json", "valid-health-response.json", "valid-session-error.json"):
        assert list(validator.iter_errors(_load(name))) == [], name


@pytest.mark.parametrize(
    "name",
    [
        "invalid-short-secret.json",
        "invalid-fixed-port.json",
        "invalid-renderer-secret-access.json",
        "invalid-health-version-mismatch.json",
    ],
)
def test_schema_rejects_invalid_session_shapes(name: str) -> None:
    assert _validator().is_valid(_load(name)) is False


def test_valid_health_binds_to_exact_unexpired_instance() -> None:
    manifest = _load("valid-session-manifest.json")
    health = _load("valid-health-response.json")

    _require_session_compatibility(
        manifest,
        health,
        now=datetime(2026, 7, 10, 3, 0, 0, tzinfo=timezone.utc),
    )


@pytest.mark.parametrize(
    "name",
    [
        "invalid-health-instance-mismatch.json",
        "invalid-health-pid-mismatch.json",
    ],
)
def test_health_identity_mismatch_fails_closed(name: str) -> None:
    with pytest.raises(ValueError, match="desktop_session_health_mismatch"):
        _require_session_compatibility(
            _load("valid-session-manifest.json"),
            _load(name),
            now=datetime(2026, 7, 10, 3, 0, 0, tzinfo=timezone.utc),
        )


def test_expired_or_low_entropy_session_fails_closed() -> None:
    with pytest.raises(ValueError, match="desktop_session_expired"):
        _require_session_compatibility(
            _load("invalid-expired-session.json"),
            _load("valid-health-response.json"),
            now=datetime(2026, 7, 10, 3, 0, 0, tzinfo=timezone.utc),
        )

    with pytest.raises(ValueError, match="desktop_session_config_invalid"):
        _require_session_compatibility(
            _load("invalid-fixed-nonce.json"),
            _load("valid-health-response.json"),
            now=datetime(2026, 7, 10, 3, 0, 0, tzinfo=timezone.utc),
        )


def test_contract_keeps_renderer_and_error_payloads_secret_free() -> None:
    manifest = _load("valid-session-manifest.json")
    error = _load("valid-session-error.json")

    assert manifest["requested_port"] == 0
    assert manifest["port_allocation"] == "os_ephemeral"
    assert manifest["renderer_secret_access"] is False
    assert manifest["secret_exposure"] == {
        "command_line": False,
        "url": False,
        "logs": False,
        "crash_report": False,
    }
    assert manifest["secret"] not in json.dumps(error)
