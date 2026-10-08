from __future__ import annotations

import base64
import json
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest

from core.plugin_host.hands_activation import (
    PluginHandsActivationAuthority,
    PluginHandsActivationConflict,
    PluginHandsActivationError,
)
from core.plugin_host.hands_artifact import PluginHandsArtifactService
from core.plugin_host.hands_upgrade import PluginHandsUpgradeSnapshot
from core.storage_provider import SQLiteStructuredRecordStore


def _seed(root: Path, *, effect: str = "read") -> tuple[SQLiteStructuredRecordStore, PluginHandsArtifactService]:
    store = SQLiteStructuredRecordStore(root / "records.sqlite3")
    descriptor = {
        "schema_version": "1.0.0", "id": "summarize", "runtime": "python-stdio-v1",
        "entrypoint": "payload/main.py",
        "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"], "additionalProperties": False},
        "output_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        "effect": effect,
        "operation_semantics": "read_only" if effect == "read" else "receipt_required",
        "requested_resources": ["workspace_input"] if effect == "read" else ["workspace_input", "workspace_output"],
    }
    files = {
        "hands/summarize/hand.json": json.dumps(descriptor, separators=(",", ":")).encode(),
        "hands/summarize/payload/main.py": b"must not be imported or run\n",
    }
    raw_files = [
        {"relative_path": path, "size_bytes": len(content), "content_base64": base64.b64encode(content).decode("ascii")}
        for path, content in sorted(files.items())
    ]
    with store.begin() as uow:
        uow.put("plugin_raw_packages", "hand-plugin~1.0.0", {"schema_version": "1.0.0", "plugin_id": "hand-plugin", "version": "1.0.0", "files": raw_files}, expected_revision=0)
        uow.put("plugin_package_states", "hand-plugin", {"schema_version": "1.0.0", "plugin_id": "hand-plugin", "package_record_id": "hand-plugin~1.0.0", "status": "installed_disabled", "enabled": False}, expected_revision=0)
        uow.commit()
    artifacts = PluginHandsArtifactService(store, managed_root=root / "managed", now="2026-08-26T00:00:00Z")
    review = artifacts.review("hand-plugin", hand_id="summarize", expected_state_revision=1, command_id="review-0001", confirm=True, reason="review", containment_profile_revision="containment-r1")
    artifacts.materialize("hand-plugin", hand_id="summarize", expected_review_revision=review["review_revision"], expected_materialization_revision=0, command_id="material-0001", confirm=True)
    return store, artifacts


def _authority(root: Path) -> PluginHandsActivationAuthority:
    store = SQLiteStructuredRecordStore(root / "records.sqlite3")
    artifacts = PluginHandsArtifactService(store, managed_root=root / "managed", now="2026-08-26T00:00:00Z")
    return PluginHandsActivationAuthority(store, artifacts=artifacts, now="2026-08-26T00:00:00Z")


def _activate(service: PluginHandsActivationAuthority, *, command_id: str = "activate-0001") -> dict[str, object]:
    return service.activate(
        "hand-plugin", hand_id="summarize", expected_review_revision=1,
        expected_materialization_revision=2, expected_activation_revision=0,
        containment_profile_revision="containment-r1", command_id=command_id, confirm=True,
    )


def test_disabled_by_default_explicit_activation_freezes_contract_and_replays(tmp_path: Path) -> None:
    _seed(tmp_path)
    service = _authority(tmp_path)
    assert service.resolve_active("hand-plugin", hand_id="summarize") is None
    with pytest.raises(PluginHandsActivationError, match="explicit confirmation"):
        service.activate("hand-plugin", hand_id="summarize", expected_review_revision=1, expected_materialization_revision=2, expected_activation_revision=0, containment_profile_revision="containment-r1", command_id="activate-0001", confirm=False)

    activated = _activate(service)
    assert activated["activation"]["status"] == "active"
    assert activated["activation"]["artifact_opaque_ref"] == "plugin-hands-artifact:hand-plugin:summarize:r2"
    assert activated["activation"]["effect"] == "read"
    assert activated["activation"]["requested_resources"] == ["workspace_input"]
    active = service.resolve_active("hand-plugin", hand_id="summarize")
    assert active is not None and active.runtime == "python-stdio-v1" and active.entrypoint == "payload/main.py"
    # A new authority instance reads only durable activation and command facts.
    replayed = _activate(_authority(tmp_path))
    assert replayed["replayed"] is True and replayed["activation_revision"] == 1


def test_activate_rejects_command_cas_profile_and_artifact_revision_drift(tmp_path: Path) -> None:
    _seed(tmp_path)
    service = _authority(tmp_path)
    _activate(service)
    with pytest.raises(PluginHandsActivationConflict, match="command identity"):
        service.activate("hand-plugin", hand_id="summarize", expected_review_revision=1, expected_materialization_revision=2, expected_activation_revision=0, containment_profile_revision="containment-r2", command_id="activate-0001", confirm=True)
    with pytest.raises(PluginHandsActivationConflict, match="activation revision"):
        service.activate("hand-plugin", hand_id="summarize", expected_review_revision=1, expected_materialization_revision=2, expected_activation_revision=0, containment_profile_revision="containment-r1", command_id="activate-0002", confirm=True)


def test_disable_is_explicit_reversible_and_replay_safe(tmp_path: Path) -> None:
    _seed(tmp_path)
    service = _authority(tmp_path)
    activated = _activate(service)
    with pytest.raises(PluginHandsActivationError, match="explicit confirmation"):
        service.disable("hand-plugin", hand_id="summarize", expected_activation_revision=activated["activation_revision"], command_id="disable-0001", confirm=False, reason="pause")
    disabled = service.disable("hand-plugin", hand_id="summarize", expected_activation_revision=activated["activation_revision"], command_id="disable-0001", confirm=True, reason="pause")
    assert disabled["activation"]["status"] == "disabled" and service.resolve_active("hand-plugin", hand_id="summarize") is None
    assert service.disable("hand-plugin", hand_id="summarize", expected_activation_revision=activated["activation_revision"], command_id="disable-0001", confirm=True, reason="pause")["replayed"] is True
    reactivated = service.activate("hand-plugin", hand_id="summarize", expected_review_revision=1, expected_materialization_revision=2, expected_activation_revision=disabled["activation_revision"], containment_profile_revision="containment-r1", command_id="activate-0002", confirm=True)
    assert reactivated["activation"]["status"] == "active" and reactivated["activation_revision"] == 3


@pytest.mark.parametrize("collection, mutate", [
    ("plugin_package_states", lambda payload: payload | {"enabled": True}),
    ("plugin_hands_reviews", lambda payload: payload | {"containment_profile_revision": "containment-r2"}),
    ("plugin_hands_artifacts", lambda payload: payload | {"status": "materializing"}),
])
def test_activation_revalidates_durable_predecessors(tmp_path: Path, collection: str, mutate) -> None:
    store, _ = _seed(tmp_path)
    key = "hand-plugin" if collection == "plugin_package_states" else "hand-plugin--summarize"
    with store.begin() as uow:
        current = uow.read(collection, key)
        assert current is not None
        uow.put(collection, key, mutate(dict(current.payload)), expected_revision=current.revision)
        uow.commit()
    with pytest.raises(PluginHandsActivationConflict):
        _activate(_authority(tmp_path))


def test_active_resolution_revalidates_descriptor_and_managed_bytes(tmp_path: Path) -> None:
    _seed(tmp_path, effect="write")
    service = _authority(tmp_path)
    result = _activate(service)
    assert result["activation"]["operation_semantics"] == "receipt_required"
    artifact_root = tmp_path / "managed" / "artifacts" / "hand-plugin--summarize"
    artifact_root.joinpath("payload", "main.py").write_bytes(b"drift")
    with pytest.raises(PluginHandsActivationConflict, match="bytes drifted"):
        service.resolve_active("hand-plugin", hand_id="summarize")


def _seed_upgrade_candidate(store: SQLiteStructuredRecordStore, *, cutover_id: str) -> None:
    descriptor = {
        "schema_version": "1.0.0", "id": "summarize", "runtime": "python-stdio-v1",
        "entrypoint": "payload/main.py", "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"], "additionalProperties": False},
        "output_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        "effect": "read", "operation_semantics": "read_only", "requested_resources": ["workspace_input"],
    }
    files = {
        "hands/summarize/hand.json": json.dumps(descriptor, separators=(",", ":")).encode(),
        "hands/summarize/payload/main.py": b"candidate-only-bytes\n",
    }
    raw_files = [{"relative_path": path, "size_bytes": len(content), "content_base64": base64.b64encode(content).decode("ascii")} for path, content in sorted(files.items())]
    old = {"package_record_id": "hand-plugin~1.0.0", "review_revision": 1, "materialization_revision": 2, "activation_revision": 1, "runtime_revision": "runtime-v1", "resource_policy_revision": "plugin-hands-resource-v1"}
    new = {"package_record_id": "hand-plugin~2.0.0", "review_revision": 1, "materialization_revision": 2, "activation_revision": 2, "runtime_revision": "runtime-v2", "resource_policy_revision": "plugin-hands-resource-v1"}
    active_id = "hands-upgrade-" + uuid5(NAMESPACE_URL, "hand-plugin:summarize").hex
    with store.begin() as uow:
        uow.put("plugin_raw_packages", "hand-plugin~2.0.0", {"schema_version": "1.0.0", "plugin_id": "hand-plugin", "version": "2.0.0", "files": raw_files}, expected_revision=0)
        uow.put("plugin_compatibility_reports", "hand-plugin~2.0.0", {"schema_version": "1.0.0", "plugin_id": "hand-plugin", "version": "2.0.0", "compatible": True}, expected_revision=0)
        uow.put("plugin_normalized_manifests", "hand-plugin~2.0.0", {"schema_version": "1.0.0", "plugin_id": "hand-plugin", "version": "2.0.0", "compatible": True, "hands_candidates": [descriptor]}, expected_revision=0)
        uow.put("plugin_hands_upgrade_cutovers", cutover_id, {"schema_version": "1.0.0", "plugin_id": "hand-plugin", "hand_id": "summarize", "old": old, "new": new, "stage": "prepared", "active_pointer": "old", "created_at": "2026-08-26T00:00:00Z", "updated_at": "2026-08-26T00:00:00Z", "rollback_mode": None, "rollback_reason": None, "rollback_authorized_at": None}, expected_revision=0)
        uow.put("plugin_hands_upgrade_active", active_id, {"plugin_id": "hand-plugin", "hand_id": "summarize", "cutover_id": cutover_id, "status": "active"}, expected_revision=0)
        uow.commit()


def _snapshots() -> tuple[PluginHandsUpgradeSnapshot, PluginHandsUpgradeSnapshot]:
    return (
        PluginHandsUpgradeSnapshot("hand-plugin~1.0.0", 1, 2, 1, "runtime-v1", "plugin-hands-resource-v1"),
        PluginHandsUpgradeSnapshot("hand-plugin~2.0.0", 1, 2, 2, "runtime-v2", "plugin-hands-resource-v1"),
    )


def _advance_cutover(store: SQLiteStructuredRecordStore, *, stage: str, pointer: str) -> None:
    with store.begin() as uow:
        record = uow.read("plugin_hands_upgrade_cutovers", "cutover-0001")
        assert record is not None
        uow.put("plugin_hands_upgrade_cutovers", record.object_id, dict(record.payload) | {"stage": stage, "active_pointer": pointer}, expected_revision=record.revision)
        uow.commit()


def test_upgrade_switch_promotes_stable_artifact_slot_and_rolls_back_from_phase_receipt(tmp_path: Path) -> None:
    store, artifacts = _seed(tmp_path)
    _seed_upgrade_candidate(store, cutover_id="cutover-0001")
    candidate_review = artifacts.review("hand-plugin", hand_id="summarize", expected_state_revision=1, command_id="review-new-0001", confirm=True, reason="new", containment_profile_revision="containment-r1", candidate_cutover_id="cutover-0001")
    artifacts.materialize("hand-plugin", hand_id="summarize", expected_review_revision=candidate_review["review_revision"], expected_materialization_revision=0, command_id="material-new-0001", confirm=True, candidate_cutover_id="cutover-0001")
    service = _authority(tmp_path)
    _activate(service)
    old, new = _snapshots()
    _advance_cutover(store, stage="old_revoked", pointer="old")
    switched = service.switch_upgrade("hand-plugin", hand_id="summarize", cutover_id="cutover-0001", phase="old_revoked", old=old, new=new)
    assert switched["activation"]["package_record_id"] == "hand-plugin~2.0.0"
    assert service.resolve_active("hand-plugin", hand_id="summarize").package_record_id == "hand-plugin~2.0.0"
    assert artifacts.resolve("hand-plugin", hand_id="summarize").root.joinpath("payload", "main.py").read_bytes() == b"candidate-only-bytes\n"
    assert service.switch_upgrade("hand-plugin", hand_id="summarize", cutover_id="cutover-0001", phase="old_revoked", old=old, new=new)["replayed"] is True

    _advance_cutover(store, stage="rollback_new_revoked", pointer="new")
    rolled_back = service.switch_upgrade("hand-plugin", hand_id="summarize", cutover_id="cutover-0001", phase="rollback_new_revoked", old=old, new=new)
    assert rolled_back["activation"]["package_record_id"] == "hand-plugin~1.0.0"
    assert service.resolve_active("hand-plugin", hand_id="summarize").package_record_id == "hand-plugin~1.0.0"
    assert service.switch_upgrade("hand-plugin", hand_id="summarize", cutover_id="cutover-0001", phase="rollback_new_revoked", old=old, new=new)["replayed"] is True
