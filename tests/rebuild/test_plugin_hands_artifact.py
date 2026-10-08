from __future__ import annotations

import base64
import json
import shutil
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest

from core.plugin_host.hands_artifact import (
    PluginHandsArtifactConflict,
    PluginHandsArtifactCrash,
    PluginHandsArtifactError,
    PluginHandsArtifactService,
)
from core.plugin_host.package_intake import PluginPackageIntake
from core.storage_provider import SQLiteStructuredRecordStore


def _seed(root: Path, *, extra: dict[str, bytes] | None = None, hand_patch: dict[str, object] | None = None,
          hand_id: str = "summarize") -> SQLiteStructuredRecordStore:
    store = SQLiteStructuredRecordStore(root / "records.sqlite3")
    hand = {
        "schema_version": "1.0.0", "id": hand_id, "runtime": "python-stdio-v1",
        "entrypoint": "payload/main.py", "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        "output_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}, "effect": "read",
        "operation_semantics": "read_only", "requested_resources": ["workspace_input"],
    }
    hand.update(hand_patch or {})
    files = {
        f"hands/{hand_id}/hand.json": json.dumps(hand, separators=(",", ":")).encode(),
        f"hands/{hand_id}/payload/main.py": b"not imported or executed\r\n",
    }
    files.update(extra or {})
    raw_files = [
        {"relative_path": path, "size_bytes": len(content), "content_base64": base64.b64encode(content).decode("ascii")}
        for path, content in sorted(files.items())
    ]
    with store.begin() as uow:
        uow.put("plugin_raw_packages", "hand-plugin~1.0.0", {
            "schema_version": "1.0.0", "plugin_id": "hand-plugin", "version": "1.0.0", "files": raw_files,
        }, expected_revision=0)
        uow.put("plugin_package_states", "hand-plugin", {
            "schema_version": "1.0.0", "plugin_id": "hand-plugin", "package_record_id": "hand-plugin~1.0.0",
            "status": "installed_disabled", "enabled": False,
        }, expected_revision=0)
        uow.commit()
    return store


def _service(root: Path, **kwargs) -> PluginHandsArtifactService:
    return PluginHandsArtifactService(
        SQLiteStructuredRecordStore(root / "records.sqlite3"), managed_root=root / "managed",
        now="2026-08-26T00:00:00Z", **kwargs,
    )


def _review(service: PluginHandsArtifactService, *, state_revision: int = 1) -> dict[str, object]:
    return service.review("hand-plugin", hand_id="summarize", expected_state_revision=state_revision,
                          command_id="review-0001", confirm=True, reason="local review", containment_profile_revision="containment-r1")


def _seed_candidate_cutover(store: SQLiteStructuredRecordStore, *, cutover_id: str = "cutover-0001") -> None:
    descriptor = {
        "schema_version": "1.0.0", "id": "summarize", "runtime": "python-stdio-v1",
        "entrypoint": "payload/main.py", "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        "output_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        "effect": "read", "operation_semantics": "read_only", "requested_resources": ["workspace_input"],
    }
    files = {
        "hands/summarize/hand.json": json.dumps(descriptor, separators=(",", ":")).encode(),
        "hands/summarize/payload/main.py": b"candidate-frozen-bytes\r\n",
    }
    raw_files = [
        {"relative_path": path, "size_bytes": len(content), "content_base64": base64.b64encode(content).decode("ascii")}
        for path, content in sorted(files.items())
    ]
    candidate = "hand-plugin~2.0.0"
    old = {"package_record_id": "hand-plugin~1.0.0", "review_revision": 1, "materialization_revision": 2, "activation_revision": 1, "runtime_revision": "runtime-v1", "resource_policy_revision": "plugin-hands-resource-v1"}
    new = {"package_record_id": candidate, "review_revision": 2, "materialization_revision": 3, "activation_revision": 2, "runtime_revision": "runtime-v2", "resource_policy_revision": "plugin-hands-resource-v1"}
    active_id = "hands-upgrade-" + uuid5(NAMESPACE_URL, "hand-plugin:summarize").hex
    with store.begin() as uow:
        uow.put("plugin_raw_packages", candidate, {"schema_version": "1.0.0", "plugin_id": "hand-plugin", "version": "2.0.0", "files": raw_files}, expected_revision=0)
        uow.put("plugin_compatibility_reports", candidate, {"schema_version": "1.0.0", "plugin_id": "hand-plugin", "version": "2.0.0", "compatible": True}, expected_revision=0)
        uow.put("plugin_normalized_manifests", candidate, {"schema_version": "1.0.0", "plugin_id": "hand-plugin", "version": "2.0.0", "compatible": True, "hands_candidates": [descriptor]}, expected_revision=0)
        uow.put("plugin_hands_upgrade_cutovers", cutover_id, {"schema_version": "1.0.0", "plugin_id": "hand-plugin", "hand_id": "summarize", "old": old, "new": new, "stage": "prepared", "active_pointer": "old", "created_at": "2026-08-26T00:00:00Z", "updated_at": "2026-08-26T00:00:00Z", "rollback_mode": None, "rollback_reason": None, "rollback_authorized_at": None}, expected_revision=0)
        uow.put("plugin_hands_upgrade_active", active_id, {"plugin_id": "hand-plugin", "hand_id": "summarize", "cutover_id": cutover_id, "status": "active"}, expected_revision=0)
        uow.commit()


def test_review_materialize_resolve_exact_bytes_and_command_replay(tmp_path: Path) -> None:
    _seed(tmp_path)
    service = _service(tmp_path)
    review = _review(service)
    materialized = service.materialize("hand-plugin", hand_id="summarize",
                                       expected_review_revision=review["review_revision"], expected_materialization_revision=0, command_id="material-0001", confirm=True)
    artifact = service.resolve("hand-plugin", hand_id="summarize")

    assert review["review"]["decision"] == "approved_disabled"
    assert materialized["status"] == "ready"
    assert artifact.containment_profile_revision == "containment-r1"
    assert artifact.runtime == "python-stdio-v1"
    assert artifact.entrypoint == "payload/main.py"
    assert artifact.effect == "read"
    assert artifact.operation_semantics == "read_only"
    assert artifact.requested_resources == ("workspace_input",)
    assert artifact.payload_files == (("payload/main.py", b"not imported or executed\r\n"),)
    assert artifact.input_schema["additionalProperties"] is False
    with pytest.raises(TypeError):
        artifact.input_schema["type"] = "array"  # type: ignore[index]
    assert artifact.opaque_ref == "plugin-hands-artifact:hand-plugin:summarize:r2"
    assert str(artifact.root) not in repr(artifact)
    assert artifact.root.joinpath("payload", "main.py").read_bytes() == b"not imported or executed\r\n"
    assert service.materialize("hand-plugin", hand_id="summarize", expected_review_revision=review["review_revision"], expected_materialization_revision=0, command_id="material-0001", confirm=True)["replayed"] is True


def test_candidate_slot_binds_active_cutover_bundle_and_keeps_old_tree_immutable(tmp_path: Path) -> None:
    store = _seed(tmp_path)
    _seed_candidate_cutover(store)
    service = _service(tmp_path)
    old_review = _review(service)
    service.materialize("hand-plugin", hand_id="summarize", expected_review_revision=old_review["review_revision"], expected_materialization_revision=0, command_id="material-old-0001", confirm=True)
    reviewed = service.review("hand-plugin", hand_id="summarize", expected_state_revision=1,
                              command_id="review-candidate-0001", confirm=True, reason="candidate review",
                              containment_profile_revision="containment-r1", candidate_cutover_id="cutover-0001")
    materialized = service.materialize("hand-plugin", hand_id="summarize", expected_review_revision=reviewed["review_revision"], expected_materialization_revision=0,
                                       command_id="material-candidate-0001", confirm=True, candidate_cutover_id="cutover-0001")
    old = service.resolve("hand-plugin", hand_id="summarize")
    candidate = service.resolve("hand-plugin", hand_id="summarize", candidate_cutover_id="cutover-0001")

    assert reviewed["review"]["candidate_cutover_id"] == "cutover-0001"
    assert materialized["candidate_cutover_id"] == "cutover-0001"
    assert old.root != candidate.root
    assert old.root.joinpath("payload", "main.py").read_bytes() == b"not imported or executed\r\n"
    assert candidate.root.joinpath("payload", "main.py").read_bytes() == b"candidate-frozen-bytes\r\n"
    assert candidate.package_record_id == "hand-plugin~2.0.0"
    assert "candidate-cutover-0001" in candidate.opaque_ref


def test_candidate_slot_requires_live_cutover_report_and_manifest_authority(tmp_path: Path) -> None:
    store = _seed(tmp_path)
    _seed_candidate_cutover(store)
    service = _service(tmp_path)
    review = service.review("hand-plugin", hand_id="summarize", expected_state_revision=1,
                            command_id="review-candidate-0001", confirm=True, reason="candidate review",
                            containment_profile_revision="containment-r1", candidate_cutover_id="cutover-0001")
    service.materialize("hand-plugin", hand_id="summarize", expected_review_revision=review["review_revision"], expected_materialization_revision=0,
                        command_id="material-candidate-0001", confirm=True, candidate_cutover_id="cutover-0001")
    active_id = "hands-upgrade-" + uuid5(NAMESPACE_URL, "hand-plugin:summarize").hex
    with store.begin() as uow:
        active = uow.read("plugin_hands_upgrade_active", active_id)
        cutover = uow.read("plugin_hands_upgrade_cutovers", "cutover-0001")
        assert active is not None and cutover is not None
        uow.put("plugin_hands_upgrade_active", active_id, dict(active.payload) | {"status": "closed"}, expected_revision=active.revision)
        uow.put("plugin_hands_upgrade_cutovers", cutover.object_id, dict(cutover.payload) | {"stage": "finalized", "active_pointer": "new"}, expected_revision=cutover.revision)
        uow.commit()
    with pytest.raises(PluginHandsArtifactConflict, match="cutover is not active"):
        service.resolve("hand-plugin", hand_id="summarize", candidate_cutover_id="cutover-0001")


def test_candidate_promotion_switches_only_durable_slot_and_rollback_restores_old_slot(tmp_path: Path) -> None:
    store = _seed(tmp_path)
    _seed_candidate_cutover(store)
    service = _service(tmp_path)
    old_review = _review(service)
    service.materialize("hand-plugin", hand_id="summarize", expected_review_revision=old_review["review_revision"], expected_materialization_revision=0, command_id="material-old-0001", confirm=True)
    candidate_review = service.review("hand-plugin", hand_id="summarize", expected_state_revision=1,
                                      command_id="review-candidate-0001", confirm=True, reason="candidate review",
                                      containment_profile_revision="containment-r1", candidate_cutover_id="cutover-0001")
    service.materialize("hand-plugin", hand_id="summarize", expected_review_revision=candidate_review["review_revision"], expected_materialization_revision=0,
                        command_id="material-candidate-0001", confirm=True, candidate_cutover_id="cutover-0001")
    with store.begin() as uow:
        promoted = service.promote_candidate_to_primary(uow, "hand-plugin", hand_id="summarize", cutover_id="cutover-0001")
        uow.commit()
    assert promoted["old_slot_id"] == "primary" and promoted["status"] == "promoted"

    active_id = "hands-upgrade-" + uuid5(NAMESPACE_URL, "hand-plugin:summarize").hex
    with store.begin() as uow:
        active = uow.read("plugin_hands_upgrade_active", active_id)
        assert active is not None
        uow.put("plugin_hands_upgrade_active", active_id, dict(active.payload) | {"status": "closed"}, expected_revision=active.revision)
        uow.commit()
    assert service.resolve("hand-plugin", hand_id="summarize").root.joinpath("payload", "main.py").read_bytes() == b"candidate-frozen-bytes\r\n"
    with pytest.raises(PluginHandsArtifactConflict, match="cutover is not active"):
        service.resolve("hand-plugin", hand_id="summarize", candidate_cutover_id="cutover-0001")

    with store.begin() as uow:
        restored = service.rollback_primary_promotion(uow, "hand-plugin", hand_id="summarize", cutover_id="cutover-0001")
        uow.commit()
    assert restored["status"] == "rolled_back"
    assert service.resolve("hand-plugin", hand_id="summarize").root.joinpath("payload", "main.py").read_bytes() == b"not imported or executed\r\n"


def test_numeric_leading_hand_id_is_consistent_with_intake_grammar(tmp_path: Path) -> None:
    _seed(tmp_path, hand_id="1hand")
    service = _service(tmp_path)
    review = service.review(
        "hand-plugin", hand_id="1hand", expected_state_revision=1,
        command_id="review-0001", confirm=True, reason="local review",
        containment_profile_revision="containment-r1",
    )
    service.materialize(
        "hand-plugin", hand_id="1hand",
        expected_review_revision=review["review_revision"],
        expected_materialization_revision=0, command_id="material-0001", confirm=True,
    )
    assert service.resolve("hand-plugin", hand_id="1hand").hand_id == "1hand"


def test_reviewed_powershell_recipe_is_materialized_without_execution(tmp_path: Path) -> None:
    _seed(tmp_path, hand_patch={"runtime": "powershell-stdio-v1", "entrypoint": "payload/main.ps1"},
          extra={"hands/summarize/payload/main.ps1": b"Write-Output 'not executed'\r\n"})
    service = _service(tmp_path)
    review = _review(service)
    service.materialize(
        "hand-plugin", hand_id="summarize", expected_review_revision=review["review_revision"],
        expected_materialization_revision=0, command_id="material-powershell-0001", confirm=True,
    )
    artifact = service.resolve("hand-plugin", hand_id="summarize")
    assert artifact.runtime == "powershell-stdio-v1"
    assert artifact.entrypoint == "payload/main.ps1"


def test_unknown_hand_fields_and_non_payload_entries_fail_closed(tmp_path: Path) -> None:
    _seed(tmp_path, extra={"hands/summarize/launch.cmd": b"must-not-run"})
    with pytest.raises(PluginHandsArtifactError, match="payload only"):
        _review(_service(tmp_path))


def test_review_profile_identity_confirm_and_closed_schema_are_fail_closed(tmp_path: Path) -> None:
    _seed(tmp_path)
    service = _service(tmp_path)
    with pytest.raises(PluginHandsArtifactError, match="explicit confirmation"):
        service.review("hand-plugin", hand_id="summarize", expected_state_revision=1,
                       command_id="review-0001", confirm=False, reason="local review", containment_profile_revision="containment-r1")
    _review(service)
    with pytest.raises(PluginHandsArtifactConflict, match="command identity"):
        service.review("hand-plugin", hand_id="summarize", expected_state_revision=1,
                       command_id="review-0001", confirm=True, reason="local review", containment_profile_revision="containment-r2")

    invalid = tmp_path / "invalid"
    _seed(invalid, hand_patch={"input_schema": {"type": "object", "properties": {"value": {"$ref": "x"}}, "required": [], "additionalProperties": False}})
    with pytest.raises(PluginHandsArtifactError, match=r"cannot contain \$ref"):
        _review(_service(invalid))


def test_materialize_requires_confirm_and_exact_artifact_revision(tmp_path: Path) -> None:
    _seed(tmp_path)
    service = _service(tmp_path)
    review = _review(service)
    with pytest.raises(PluginHandsArtifactError, match="explicit confirmation"):
        service.materialize("hand-plugin", hand_id="summarize", expected_review_revision=review["review_revision"], expected_materialization_revision=0, command_id="material-0001", confirm=False)
    service.materialize("hand-plugin", hand_id="summarize", expected_review_revision=review["review_revision"], expected_materialization_revision=0, command_id="material-0001", confirm=True)
    with pytest.raises(PluginHandsArtifactConflict, match="materialization revision conflict"):
        service.materialize("hand-plugin", hand_id="summarize", expected_review_revision=review["review_revision"], expected_materialization_revision=0, command_id="material-0002", confirm=True)


def test_source_is_not_read_and_managed_drift_fails_closed(tmp_path: Path) -> None:
    _seed(tmp_path)
    service = _service(tmp_path)
    review = _review(service)
    service.materialize("hand-plugin", hand_id="summarize", expected_review_revision=review["review_revision"], expected_materialization_revision=0, command_id="material-0001", confirm=True)
    artifact = service.resolve("hand-plugin", hand_id="summarize")
    artifact.root.joinpath("payload", "main.py").write_bytes(b"changed")

    with pytest.raises(PluginHandsArtifactConflict, match="bytes drifted"):
        service.resolve("hand-plugin", hand_id="summarize")


def test_real_intake_source_deletion_and_restart_do_not_change_artifact_authority(tmp_path: Path) -> None:
    package = tmp_path / "source" / "hand-plugin"
    (package / ".codex-plugin").mkdir(parents=True)
    (package / "hands" / "summarize" / "payload").mkdir(parents=True)
    (package / ".codex-plugin" / "plugin.json").write_text(json.dumps({"name": "hand-plugin", "version": "1.0.0", "description": "fixture"}), encoding="utf-8")
    descriptor = {
        "schema_version": "1.0.0", "id": "summarize", "runtime": "python-stdio-v1", "entrypoint": "payload/main.py",
        "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        "output_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        "effect": "read", "operation_semantics": "read_only", "requested_resources": ["workspace_input"],
    }
    (package / "hands" / "summarize" / "hand.json").write_text(json.dumps(descriptor), encoding="utf-8")
    (package / "hands" / "summarize" / "payload" / "main.py").write_bytes(b"captured-before-source-deletion\r\n")
    store = SQLiteStructuredRecordStore(tmp_path / "records.sqlite3")
    intake = PluginPackageIntake(store, now="2026-08-26T00:00:00Z", source_root=tmp_path / "source")
    discovered = intake.discover(str(package), command_id="discover-0001")
    intake.install_disabled("hand-plugin", expected_state_revision=discovered["state_revision"], command_id="install-0001", confirm=True)
    shutil.rmtree(package)

    restarted = _service(tmp_path)
    review = _review(restarted, state_revision=2)
    restarted.materialize("hand-plugin", hand_id="summarize", expected_review_revision=review["review_revision"], expected_materialization_revision=0, command_id="material-0001", confirm=True)
    assert restarted.resolve("hand-plugin", hand_id="summarize").root.joinpath("payload", "main.py").read_bytes() == b"captured-before-source-deletion\r\n"


def test_managed_extra_file_is_not_repaired_from_raw(tmp_path: Path) -> None:
    _seed(tmp_path)
    service = _service(tmp_path)
    review = _review(service)
    service.materialize("hand-plugin", hand_id="summarize", expected_review_revision=review["review_revision"], expected_materialization_revision=0, command_id="material-0001", confirm=True)
    artifact = service.resolve("hand-plugin", hand_id="summarize")
    artifact.root.joinpath("unexpected.txt").write_text("drift", encoding="utf-8")
    with pytest.raises(PluginHandsArtifactConflict, match="unexpected entry"):
        service.resolve("hand-plugin", hand_id="summarize")


def test_resolve_rechecks_artifact_descriptor_against_raw_review_authority(tmp_path: Path) -> None:
    store = _seed(tmp_path)
    service = _service(tmp_path)
    review = _review(service)
    service.materialize("hand-plugin", hand_id="summarize", expected_review_revision=review["review_revision"], expected_materialization_revision=0, command_id="material-0001", confirm=True)
    with store.begin() as uow:
        record = uow.read("plugin_hands_artifacts", "hand-plugin--summarize")
        assert record is not None
        payload = dict(record.payload)
        descriptor = dict(payload["descriptor"])
        descriptor["requested_resources"] = []
        payload["descriptor"] = descriptor
        uow.put("plugin_hands_artifacts", record.object_id, payload, expected_revision=record.revision)
        uow.commit()
    with pytest.raises(PluginHandsArtifactConflict, match="artifact authority drifted"):
        service.resolve("hand-plugin", hand_id="summarize")


@pytest.mark.parametrize("point", ["after_intent", "after_stage", "after_promote", "after_finalize"])
def test_crash_resume_converges_same_operation(tmp_path: Path, point: str) -> None:
    _seed(tmp_path)
    def fault(current: str) -> None:
        if current == point:
            raise PluginHandsArtifactCrash(point)
    crashing = _service(tmp_path, fault=fault)
    review = _review(crashing)
    with pytest.raises(PluginHandsArtifactCrash):
        crashing.materialize("hand-plugin", hand_id="summarize", expected_review_revision=review["review_revision"], expected_materialization_revision=0, command_id="material-0001", confirm=True)
    result = _service(tmp_path).materialize("hand-plugin", hand_id="summarize", expected_review_revision=review["review_revision"], expected_materialization_revision=0, command_id="material-0001", confirm=True)
    assert result["status"] == "ready"
    assert _service(tmp_path).resolve("hand-plugin", hand_id="summarize").root.is_dir()


def test_startup_reconcile_resumes_incomplete_materialization(tmp_path: Path) -> None:
    _seed(tmp_path)
    def fault(current: str) -> None:
        if current == "after_intent":
            raise PluginHandsArtifactCrash(current)
    crashing = _service(tmp_path, fault=fault)
    review = _review(crashing)
    with pytest.raises(PluginHandsArtifactCrash):
        crashing.materialize(
            "hand-plugin", hand_id="summarize",
            expected_review_revision=review["review_revision"],
            expected_materialization_revision=0,
            command_id="material-reconcile-0001", confirm=True,
        )
    restarted = _service(tmp_path)
    recovered = restarted.reconcile()
    assert len(recovered) == 1 and recovered[0]["status"] == "ready"
    assert restarted.resolve("hand-plugin", hand_id="summarize").root.is_dir()
