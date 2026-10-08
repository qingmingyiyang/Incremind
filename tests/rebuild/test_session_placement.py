from __future__ import annotations

from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
import sqlite3

import pytest

from core.product_core.session_placement import (
    CapabilityDescriptor, DeviceIdentity, HostSigningIdentity, PairedDeviceRegistry,
    PlacementSafetyState, ResumeBundleService, SessionPlacementConflict,
    SessionPlacementError, WorkspaceManifestEntry,
)


def _expiry() -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat().replace("+00:00", "Z")


def _services(tmp_path):
    host = HostSigningIdentity(device_id="host-1")
    registry = PairedDeviceRegistry(tmp_path)
    registry.trust(host.public_identity)
    target = HostSigningIdentity(device_id="laptop-2").public_identity
    registry.trust(target)
    return ResumeBundleService(host=host, pairs=registry), registry, target


def _bundle(service: ResumeBundleService):
    return service.create(
        target_device_id="laptop-2", project_id="project-1",
        session_ref="session:private-1", turn_refs=("turn:1", "turn:2"),
        context_manifest_ref="context:project-1", context_manifest_revision="context-revision:4", last_event_cursor="event:12",
        display_summary="继续整理第 5 阶段的本地会话恢复合同。",
        workspace_base_manifest_ref="workspace-manifest:base-1",
        workspace_manifest=(WorkspaceManifestEntry("src/main.py", "a" * 64, 1024, "git:abc123"),),
        capability_descriptors=(CapabilityDescriptor("companion.chat", 3, ("read",)),), expires_at=_expiry(),
    )


def test_registry_uses_injected_file_authority_lock_port(tmp_path) -> None:
    acquired: list[str] = []

    @contextmanager
    def lock(path, *, timeout_seconds=5.0):
        acquired.append(path.name)
        yield

    registry = PairedDeviceRegistry(tmp_path, file_authority_lock=lock)
    registry.trust(HostSigningIdentity(device_id="device-lock").public_identity)

    assert acquired == ["paired-devices.sqlite3", "paired-devices.sqlite3"]


def test_signed_bundle_import_is_one_time_read_resume_only(tmp_path) -> None:
    service, _, _ = _services(tmp_path)
    bundle = _bundle(service)
    resumed = service.import_for_paired_device(bundle.wire(), local_device_id="laptop-2")
    assert resumed.read_only and resumed.host_authorization_required
    assert resumed.session_ref == "session:private-1"
    with pytest.raises(SessionPlacementConflict, match="already been consumed"):
        service.import_for_paired_device(bundle.wire(), local_device_id="laptop-2")


def test_import_rejects_wrong_target_tamper_expiry_and_unpaired_host(tmp_path) -> None:
    service, registry, _ = _services(tmp_path)
    bundle = _bundle(service)
    with pytest.raises(SessionPlacementError, match="target device"):
        service.import_for_paired_device(bundle.wire(), local_device_id="other-3")
    tampered = bundle.wire(); tampered["display_summary"] = "tampered"
    with pytest.raises(SessionPlacementError, match="signature"):
        service.import_for_paired_device(tampered, local_device_id="laptop-2")
    expired = bundle.wire(); expired["expires_at"] = "2020-01-01T00:00:00Z"
    with pytest.raises(SessionPlacementError, match="expired"):
        service.import_for_paired_device(expired, local_device_id="laptop-2")
    assert registry.identity("host-1") is not None


@pytest.mark.parametrize("safety", [PlacementSafetyState(active_lease_count=1), PlacementSafetyState(effect_states=("UNKNOWN",))])
def test_active_lease_or_unknown_effect_blocks_export_and_import(tmp_path, safety) -> None:
    service, _, _ = _services(tmp_path)
    with pytest.raises(SessionPlacementError):
        service.create(target_device_id="laptop-2", project_id="project-1", session_ref="session:1", turn_refs=("turn:1",), context_manifest_ref="ctx:1", context_manifest_revision="context-revision:1", last_event_cursor="event:1", display_summary="安全恢复的会话摘要内容。", workspace_base_manifest_ref="workspace-manifest:base-1", workspace_manifest=(), capability_descriptors=(), expires_at=_expiry(), safety=safety)


@pytest.mark.parametrize("field,value", [
    ("workspace_manifest", [{"relative_path": "C:/secret.txt", "digest": "a" * 64, "size_bytes": 1, "base_revision": "git:x"}]),
    ("workspace_manifest", [{"relative_path": "src/a.py", "digest": "a" * 64, "size_bytes": 1, "base_revision": "git:x", "secret": "x"}]),
    ("receipt_payload", {"body": "no"}),
    ("display_summary", "api_key=not-allowed"),
])
def test_bundle_rejects_protected_material_and_absolute_paths(tmp_path, field, value) -> None:
    service, _, _ = _services(tmp_path)
    wire = _bundle(service).wire(); wire[field] = value
    with pytest.raises(SessionPlacementError):
        service.import_for_paired_device(wire, local_device_id="laptop-2")


def test_pairing_registry_persists_public_identity_and_revision(tmp_path) -> None:
    identity = HostSigningIdentity(device_id="device-1").public_identity
    registry = PairedDeviceRegistry(tmp_path)
    assert registry.trust(identity) == 1
    restarted = PairedDeviceRegistry(tmp_path)
    assert restarted.identity("device-1") == identity
    assert restarted.trust(identity, expected_revision=1) == 1
    other = HostSigningIdentity(device_id="device-1").public_identity
    with pytest.raises(SessionPlacementConflict, match="identity key changed"):
        restarted.trust(other, expected_revision=1)


def test_pairing_revoke_is_cas_auditable_and_excludes_revoked_identity(tmp_path) -> None:
    identity = HostSigningIdentity(device_id="device-1").public_identity
    registry = PairedDeviceRegistry(tmp_path)
    assert registry.trust(identity) == 1
    assert registry.revoke("device-1", expected_revision=1) == 2
    assert registry.identity("device-1") is None
    assert registry.list_identities() == ()
    record = registry.trust_record("device-1")
    assert record is not None and record[0] == identity
    assert record[1:3] == (2, "revoked") and record[4] is not None
    with pytest.raises(SessionPlacementConflict, match="already revoked"):
        registry.revoke("device-1", expected_revision=2)
    with pytest.raises(SessionPlacementConflict, match="trust revision changed"):
        registry.revoke("device-1", expected_revision=1)
    assert registry.trust(identity, expected_revision=2) == 3
    assert registry.trust_record("device-1")[1:3] == (3, "active")


def test_registry_migrates_legacy_pairings_as_active(tmp_path) -> None:
    directory = tmp_path / ".rebuild-data" / "session-placement"
    directory.mkdir(parents=True)
    database = directory / "paired-devices.sqlite3"
    identity = HostSigningIdentity(device_id="legacy-device").public_identity
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE paired_devices (device_id TEXT PRIMARY KEY, public_key TEXT NOT NULL, trust_revision INTEGER NOT NULL, trusted_at TEXT NOT NULL)")
        conn.execute("INSERT INTO paired_devices VALUES (?,?,?,?)", (identity.device_id, identity.public_key, 4, "2026-01-01T00:00:00+00:00"))
    registry = PairedDeviceRegistry(tmp_path)
    assert registry.identity("legacy-device") == identity
    assert registry.trust_record("legacy-device")[1:3] == (4, "active")


def test_v3_bundle_freezes_source_evidence_and_uses_local_trust_for_import(tmp_path) -> None:
    service, registry, target = _services(tmp_path)
    bundle = _bundle(service)
    assert bundle.schema_version == "resume_bundle.v3"
    assert bundle.source_trust_revision == 1 and bundle.target_trust_revision == 1
    assert registry.revoke(target.device_id, expected_revision=1) == 2
    with pytest.raises(SessionPlacementError, match="target device is not paired"):
        _bundle(service)
    with pytest.raises(SessionPlacementError, match="target is not paired"):
        service.import_for_paired_device(bundle.wire(), local_device_id=target.device_id)
    assert registry.trust(target, expected_revision=2) == 3
    resumed = service.import_for_paired_device(bundle.wire(), local_device_id=target.device_id)
    assert resumed.bundle_target_trust_revision == 1
    assert resumed.local_target_trust_revision == 3
    service.validate_projection_access(resumed)
    assert registry.revoke(target.device_id, expected_revision=3) == 4
    with pytest.raises(SessionPlacementError, match="target pairing is unavailable"):
        service.validate_projection_access(resumed)


def test_signed_v2_bundle_remains_readable_and_projection_freezes_active_trust(tmp_path) -> None:
    service, _, target = _services(tmp_path)
    current = _bundle(service)
    legacy_payload = current.unsigned_payload()
    legacy_payload["schema_version"] = "resume_bundle.v2"
    legacy_payload.pop("source_trust_revision")
    legacy_payload.pop("target_trust_revision")
    legacy_wire = {**legacy_payload, "signature": service._host.sign(legacy_payload)}
    resumed = service.import_for_paired_device(legacy_wire, local_device_id=target.device_id)
    assert resumed.bundle_source_trust_revision is None
    assert resumed.bundle_target_trust_revision is None
    assert resumed.local_source_trust_revision == 1
    assert resumed.local_target_trust_revision == 1
    assert resumed.expires_at == current.expires_at


@pytest.mark.parametrize("field,value", [
    ("source_trust_revision", "1"),
    ("target_trust_revision", 0),
    ("project_id", 7),
    ("capability_descriptors", [{"capability_id": "companion.chat", "revision": "3", "effect_classes": []}]),
])
def test_bundle_rejects_type_or_trust_field_tampering(tmp_path, field, value) -> None:
    service, _, _ = _services(tmp_path)
    wire = _bundle(service).wire()
    wire[field] = value
    with pytest.raises(SessionPlacementError):
        service.import_for_paired_device(wire, local_device_id="laptop-2")


def test_bundle_requires_reconcile_compatible_workspace_entries_and_base_manifest_ref(tmp_path) -> None:
    service, _, _ = _services(tmp_path)
    wire = _bundle(service).wire()
    assert wire["workspace_base_manifest_ref"] == "workspace-manifest:base-1"
    assert wire["workspace_manifest"] == [{
        "relative_path": "src/main.py", "digest": "a" * 64,
        "size_bytes": 1024, "base_revision": "git:abc123",
    }]
    wire["workspace_manifest"] = [{
        "relative_path": ".env", "digest": "a" * 64,
        "size_bytes": 1, "base_revision": "git:x",
    }]
    with pytest.raises(SessionPlacementError, match="workspace path"):
        service.import_for_paired_device(wire, local_device_id="laptop-2")


def test_bundle_expiry_is_bounded_to_twenty_four_hours(tmp_path) -> None:
    service, _, _ = _services(tmp_path)
    expiry = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    with pytest.raises(SessionPlacementError, match="allowed lifetime"):
        service.create(
            target_device_id="laptop-2",
            project_id="project-1",
            session_ref="session:1",
            turn_refs=("turn:1",),
            context_manifest_ref="context:1",
            context_manifest_revision="context-revision:1",
            last_event_cursor="event:1",
            display_summary="受控恢复的会话摘要。",
            workspace_base_manifest_ref="workspace:none",
            workspace_manifest=(),
            capability_descriptors=(),
            expires_at=expiry,
        )
