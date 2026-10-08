from __future__ import annotations

import base64
import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.api import ai_runtime
import backend.api.app as api_app
from backend.api.plugin_hands_runtime import (
    PLUGIN_HANDS_RECIPE_REVISION,
    PLUGIN_HANDS_RESOURCE_POLICY_REVISION,
    PluginHandsRegistrationManager,
    PluginHandsUpgradeRuntime,
    plugin_hands_capability,
    _upgrade_intent,
)
from core.effect_log import EffectLog, EffectReaper, EffectRunner, backfill_interrupted_effects
from core.ai_kernel import ScopedCapabilityRegistry
from core.plugin_host.hands_activation import PluginHandsActivation, PluginHandsActivationAuthority
from core.plugin_host.hands_artifact import PluginHandsArtifactService
from core.plugin_host.hands_upgrade import PluginHandsUpgradeAuthority, PluginHandsUpgradeSnapshot
from core.storage_provider import SQLiteStructuredRecordStore


def _activation(*, package: str = "package-000001", revision: int = 1) -> PluginHandsActivation:
    return PluginHandsActivation(
        plugin_id="hand-plugin", hand_id="summarize", package_record_id=package,
        review_revision=revision, materialization_revision=revision,
        containment_profile_revision="appcontainer-v1",
        artifact_opaque_ref=f"plugin-hands-artifact:hand-plugin:summarize:r{revision}",
        runtime="powershell-stdio-v1", entrypoint="payload/main.ps1",
        input_schema={"type": "object", "additionalProperties": False},
        output_schema={"type": "object", "additionalProperties": False},
        effect="read", operation_semantics="read_only",
        requested_resources=("workspace_input",), activation_revision=revision,
    )


def _snapshot(active: PluginHandsActivation) -> PluginHandsUpgradeSnapshot:
    return PluginHandsUpgradeSnapshot(
        active.package_record_id, active.review_revision, active.materialization_revision,
        active.activation_revision, PLUGIN_HANDS_RECIPE_REVISION,
        PLUGIN_HANDS_RESOURCE_POLICY_REVISION,
    )


class _Activation:
    def __init__(self, old: PluginHandsActivation, new: PluginHandsActivation) -> None:
        self.old, self.new, self.value = old, new, old

    def resolve_active(self, plugin_id: str, *, hand_id: str) -> PluginHandsActivation:
        assert (plugin_id, hand_id) == (self.value.plugin_id, self.value.hand_id)
        return self.value

    def all_active(self) -> tuple[PluginHandsActivation, ...]:
        return (self.value,)

    def switch_upgrade(self, _plugin_id: str, *, hand_id: str, cutover_id: str, phase: str, old, new):
        assert hand_id == self.value.hand_id and cutover_id == "cutover-00000001"
        assert (old, new) == (_snapshot(self.old), _snapshot(self.new))
        self.value = self.new if phase == "old_revoked" else self.old


@pytest.mark.parametrize("interrupted_stage, expected_providers", [
    ("prepared", 2),
    ("old_revoked", 2),
    ("new_switched", 1),
])
def test_startup_recovery_converges_interrupted_cutover_to_one_registry_lease(
    tmp_path: Path, interrupted_stage: str, expected_providers: int,
) -> None:
    """A fresh manager resumes each representative durable interruption.

    The registry begins from the durable activation pointer, exactly as
    ``build_ai_runtime`` does.  Recovery must finish the cutover without a
    duplicate stable capability registration.
    """

    old = _activation()
    new = replace(
        old, package_record_id="package-000002", review_revision=2,
        materialization_revision=2, activation_revision=2,
        artifact_opaque_ref="plugin-hands-artifact:hand-plugin:summarize:r2",
    )
    activation = _Activation(old, new)
    registry = ScopedCapabilityRegistry()
    providers: list[object] = []

    class Provider:
        def close(self) -> None:
            return None

    manager = PluginHandsRegistrationManager(
        activation=activation,
        registry=registry,
        provider_factory=lambda _active, _capability_id: providers.append(Provider()) or providers[-1],
    )
    records = SQLiteStructuredRecordStore(tmp_path / "jobs.sqlite3")
    runtime = PluginHandsUpgradeRuntime(
        records=records, activation=activation, registration_manager=manager,
        now="2026-08-26T00:00:00Z", validate_snapshots=lambda *_args: None,
        attempts=lambda _old, _new: (),
    )
    cutover_id = "cutover-00000001"
    runtime.authority.begin(
        cutover_id, old.plugin_id, hand_id=old.hand_id,
        old=_snapshot(old), new=_snapshot(new),
    )
    if interrupted_stage == "old_revoked":
        current = runtime.authority.load(cutover_id)
        runtime.authority._advance(cutover_id, current["revision"], "old_revoked")
    elif interrupted_stage == "new_switched":
        current = runtime.authority.load(cutover_id)
        runtime.authority._advance(cutover_id, current["revision"], "old_revoked")
        activation.value = new
        current = runtime.authority.load(cutover_id)
        runtime.authority._advance(cutover_id, current["revision"], "new_switched")

    # This is the startup sequence: project the existing pointer first, then
    # resume unfinished work through the same manager and activation authority.
    manager.reconcile()
    effects = EffectLog(records.database_path)
    backfill_interrupted_effects(
        effects, (_upgrade_intent(runtime.authority.load(cutover_id)),),
        now=100, lease_owner="legacy-test",
    )
    EffectReaper(effects).recover_expired(now=101)
    recovered = runtime.dispatch_effects(EffectRunner(effects, owner_id="test-upgrade"))

    assert recovered and recovered[0]["stage"] == "completed"
    capability_id = plugin_hands_capability(new).capability_id
    assert registry.resolve(capability_id) is not None
    assert manager.is_registered(new) is True
    assert len(providers) == expected_providers


def test_ai_runtime_recovers_after_initial_projection_with_the_same_manager(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    calls: list[tuple[str, object]] = []
    activation = object()

    class Manager:
        activation_authority = activation

        def reconcile(self) -> tuple[object, ...]:
            calls.append(("reconcile", self))
            return ()

    manager = Manager()
    hook_host = SimpleNamespace(current_snapshot=lambda: SimpleNamespace(handlers=()))
    monkeypatch.setattr(ai_runtime, "build_codex_hook_host", lambda _config, **_kwargs: hook_host)
    monkeypatch.setattr(ai_runtime, "build_plugin_hands_registration_manager", lambda **_kwargs: manager)

    runtime = ai_runtime.build_ai_runtime(SimpleNamespace(root_dir=tmp_path))

    assert calls == [("reconcile", manager)]
    assert runtime.plugin_hands_registration_manager is manager


def _seed_startup_cutover(root: Path, stage: str) -> None:
    records = SQLiteStructuredRecordStore(root / ".rebuild-data" / "jobs.sqlite3")
    old = PluginHandsUpgradeSnapshot(
        "hand-plugin~1.0.0", 1, 2, 1,
        PLUGIN_HANDS_RECIPE_REVISION, PLUGIN_HANDS_RESOURCE_POLICY_REVISION,
    )
    new = PluginHandsUpgradeSnapshot(
        "hand-plugin~2.0.0", 1, 2, 2,
        PLUGIN_HANDS_RECIPE_REVISION, PLUGIN_HANDS_RESOURCE_POLICY_REVISION,
    )
    authority = PluginHandsUpgradeAuthority(
        records, now="2026-08-26T00:00:00Z", validate_snapshots=lambda *_args: None,
    )
    authority.begin("cutover-00000001", "hand-plugin", hand_id="summarize", old=old, new=new)
    rollback_targets = {
        "rollback_prepared", "rollback_new_revoked", "rollback_old_switched",
        "rollback_old_registered", "rolled_back",
    }
    if stage in rollback_targets:
        for next_stage in ("old_revoked", "new_switched", "new_registered", "completed"):
            current = authority.load("cutover-00000001")
            assert current is not None
            authority._advance("cutover-00000001", current["revision"], next_stage)
        completed = authority.load("cutover-00000001")
        assert completed is not None
        authority._prepare_rollback(
            "cutover-00000001", completed["revision"], automatic=True, reason="automatic-safe",
        )
    while authority.load("cutover-00000001")["stage"] != stage:
        current = authority.load("cutover-00000001")
        assert current is not None
        next_stage = {
            "prepared": "old_revoked", "old_revoked": "new_switched",
            "new_switched": "new_registered", "new_registered": "completed",
            "completed": "finalized", "rollback_prepared": "rollback_new_revoked",
            "rollback_new_revoked": "rollback_old_switched",
            "rollback_old_switched": "rollback_old_registered",
            "rollback_old_registered": "rolled_back",
        }.get(current["stage"])
        if next_stage is None:
            break
        authority._advance("cutover-00000001", current["revision"], next_stage)


@pytest.mark.parametrize("stage, bootstrap_expected", [
    ("prepared", True),
    ("rollback_new_revoked", True),
    ("completed", False),
    ("finalized", False),
])
def test_fastapi_startup_bootstraps_ai_only_for_unfinished_plugin_hands_cutovers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stage: str, bootstrap_expected: bool,
) -> None:
    """TestClient drives the real FastAPI startup event, not a route helper."""

    _seed_startup_cutover(tmp_path, stage)
    calls: list[object] = []

    def build_for_cutover(request, _container):
        calls.append(request.app)
        request.app.state.ai_runtime = SimpleNamespace(plugin_hands_registration_manager=None)
        return request.app.state.ai_runtime

    monkeypatch.setattr(api_app, "get_or_build_ai_runtime", build_for_cutover)
    application = api_app.create_app(SimpleNamespace(root_dir=tmp_path))

    with TestClient(application):
        pass

    assert bool(calls) is bootstrap_expected
    assert len(calls) == int(bootstrap_expected)


@pytest.mark.parametrize("payload", [
    {"stage": "prepared"},
    {"schema_version": "2.0.0", "plugin_id": "hand-plugin"},
])
def test_fastapi_startup_does_not_bootstrap_for_corrupt_or_future_cutover_records(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, payload: dict[str, object],
) -> None:
    records = SQLiteStructuredRecordStore(tmp_path / ".rebuild-data" / "jobs.sqlite3")
    with records.begin() as uow:
        uow.put("plugin_hands_upgrade_cutovers", "cutover-00000001", payload, expected_revision=0)
        uow.commit()
    calls: list[object] = []
    monkeypatch.setattr(api_app, "get_or_build_ai_runtime", lambda *_args: calls.append(object()))
    with TestClient(api_app.create_app(SimpleNamespace(root_dir=tmp_path))):
        pass
    assert calls == []


def test_startup_scan_accepts_a_valid_cutover_among_corrupt_records(tmp_path: Path) -> None:
    _seed_startup_cutover(tmp_path, "prepared")
    records = SQLiteStructuredRecordStore(tmp_path / ".rebuild-data" / "jobs.sqlite3")
    with records.begin() as uow:
        uow.put("plugin_hands_upgrade_cutovers", "corrupt-00000001", {"stage": "prepared"}, expected_revision=0)
        uow.commit()
    assert api_app._has_unfinished_plugin_hands_upgrades(tmp_path) is True


def test_startup_scan_fails_closed_when_cutover_collection_exceeds_bound(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    _seed_startup_cutover(tmp_path, "prepared")
    records = SQLiteStructuredRecordStore(tmp_path / ".rebuild-data" / "jobs.sqlite3")
    with records.begin() as uow:
        for index in range(api_app._PLUGIN_HANDS_UPGRADE_STARTUP_SCAN_LIMIT):
            uow.put("plugin_hands_upgrade_cutovers", f"corrupt-{index:08d}", {"stage": "prepared"}, expected_revision=0)
        uow.commit()
    with caplog.at_level("WARNING"):
        assert api_app._has_unfinished_plugin_hands_upgrades(tmp_path) is False
    assert "plugin_hands_upgrade_startup_scan_over_limit" in caplog.messages


def test_startup_scan_missing_or_schema_invalid_database_is_quietly_fail_closed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    assert api_app._has_unfinished_plugin_hands_upgrades(tmp_path) is False
    database_path = tmp_path / ".rebuild-data" / "jobs.sqlite3"
    database_path.parent.mkdir()
    sqlite3.connect(database_path).close()
    with caplog.at_level("WARNING"):
        assert api_app._has_unfinished_plugin_hands_upgrades(tmp_path) is False
    assert "plugin_hands_upgrade_startup_scan_failed" not in caplog.messages


def _raw_files(descriptor: dict[str, object], payload: bytes) -> list[dict[str, object]]:
    files = {
        "hands/summarize/hand.json": json.dumps(descriptor, separators=(",", ":")).encode(),
        "hands/summarize/payload/main.py": payload,
    }
    return [
        {"relative_path": path, "size_bytes": len(content), "content_base64": base64.b64encode(content).decode("ascii")}
        for path, content in sorted(files.items())
    ]


def test_restart_recovers_real_rollback_new_revoked_to_one_old_registry_lease(tmp_path: Path) -> None:
    """A fresh process converges a real artifact/activation rollback safely."""

    root = tmp_path
    records = SQLiteStructuredRecordStore(root / ".rebuild-data" / "jobs.sqlite3")
    descriptor = {
        "schema_version": "1.0.0", "id": "summarize", "runtime": "python-stdio-v1",
        "entrypoint": "payload/main.py",
        "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        "output_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        "effect": "read", "operation_semantics": "read_only", "requested_resources": ["workspace_input"],
    }
    with records.begin() as uow:
        uow.put("plugin_raw_packages", "hand-plugin~1.0.0", {
            "schema_version": "1.0.0", "plugin_id": "hand-plugin", "version": "1.0.0",
            "files": _raw_files(descriptor, b"old-frozen-bytes\n"),
        }, expected_revision=0)
        uow.put("plugin_package_states", "hand-plugin", {
            "schema_version": "1.0.0", "plugin_id": "hand-plugin", "package_record_id": "hand-plugin~1.0.0",
            "status": "installed_disabled", "enabled": False,
        }, expected_revision=0)
        uow.put("plugin_raw_packages", "hand-plugin~2.0.0", {
            "schema_version": "1.0.0", "plugin_id": "hand-plugin", "version": "2.0.0",
            "files": _raw_files(descriptor, b"candidate-frozen-bytes\n"),
        }, expected_revision=0)
        uow.put("plugin_compatibility_reports", "hand-plugin~2.0.0", {
            "schema_version": "1.0.0", "plugin_id": "hand-plugin", "version": "2.0.0", "compatible": True,
        }, expected_revision=0)
        uow.put("plugin_normalized_manifests", "hand-plugin~2.0.0", {
            "schema_version": "1.0.0", "plugin_id": "hand-plugin", "version": "2.0.0", "compatible": True,
            "hands_candidates": [descriptor],
        }, expected_revision=0)
        uow.commit()

    artifacts = PluginHandsArtifactService(records, managed_root=root / ".rebuild-data" / "plugin-hands-materializations", now="2026-08-26T00:00:00Z")
    old_review = artifacts.review("hand-plugin", hand_id="summarize", expected_state_revision=1, command_id="review-old-0001", confirm=True, reason="old review", containment_profile_revision="appcontainer-v1")
    artifacts.materialize("hand-plugin", hand_id="summarize", expected_review_revision=old_review["review_revision"], expected_materialization_revision=0, command_id="material-old-0001", confirm=True)
    activation = PluginHandsActivationAuthority(records, artifacts=artifacts, now="2026-08-26T00:00:00Z")
    activation.activate("hand-plugin", hand_id="summarize", expected_review_revision=1, expected_materialization_revision=2, expected_activation_revision=0, containment_profile_revision="appcontainer-v1", command_id="activate-old-0001", confirm=True)
    old = PluginHandsUpgradeSnapshot("hand-plugin~1.0.0", 1, 2, 1, PLUGIN_HANDS_RECIPE_REVISION, PLUGIN_HANDS_RESOURCE_POLICY_REVISION)
    new = PluginHandsUpgradeSnapshot("hand-plugin~2.0.0", 1, 2, 2, PLUGIN_HANDS_RECIPE_REVISION, PLUGIN_HANDS_RESOURCE_POLICY_REVISION)
    cutover = "cutover-00000001"
    setup = PluginHandsUpgradeAuthority(records, now="2026-08-26T00:00:00Z", validate_snapshots=lambda *_args: None)
    setup.begin(cutover, "hand-plugin", hand_id="summarize", old=old, new=new)
    candidate_review = artifacts.review("hand-plugin", hand_id="summarize", expected_state_revision=1, command_id="review-new-0001", confirm=True, reason="new review", containment_profile_revision="appcontainer-v1", candidate_cutover_id=cutover)
    artifacts.materialize("hand-plugin", hand_id="summarize", expected_review_revision=candidate_review["review_revision"], expected_materialization_revision=0, command_id="material-new-0001", confirm=True, candidate_cutover_id=cutover)

    class Provider:
        def __init__(self, package: str) -> None:
            self.package = package
            self.closed = 0
        def close(self) -> None:
            self.closed += 1

    first_registry = ScopedCapabilityRegistry()
    first_manager = PluginHandsRegistrationManager(activation=activation, registry=first_registry, provider_factory=lambda active, _capability: Provider(active.package_record_id))
    first_manager.reconcile()
    first_runtime = PluginHandsUpgradeRuntime(records=records, activation=activation, registration_manager=first_manager, now="2026-08-26T00:00:00Z", attempts=lambda _old, _new: ())
    assert first_runtime.authority.resume(cutover, ports=first_runtime.ports)["stage"] == "completed"
    completed = first_runtime.authority.load(cutover)
    assert completed is not None
    first_runtime.authority._prepare_rollback(cutover, completed["revision"], automatic=True, reason="automatic-safe")
    first_runtime.ports.revoke(new, cutover, "rollback_prepared")
    rollback_prepared = first_runtime.authority.load(cutover)
    assert rollback_prepared is not None
    first_runtime.authority._advance(cutover, rollback_prepared["revision"], "rollback_new_revoked")

    restarted_artifacts = PluginHandsArtifactService(records, managed_root=root / ".rebuild-data" / "plugin-hands-materializations", now="2026-08-26T00:00:01Z")
    restarted_activation = PluginHandsActivationAuthority(records, artifacts=restarted_artifacts, now="2026-08-26T00:00:01Z")
    restarted_registry = ScopedCapabilityRegistry()
    providers: list[Provider] = []
    restarted_manager = PluginHandsRegistrationManager(activation=restarted_activation, registry=restarted_registry, provider_factory=lambda active, _capability: providers.append(Provider(active.package_record_id)) or providers[-1])
    restarted_manager.reconcile()
    restarted = PluginHandsUpgradeRuntime(records=records, activation=restarted_activation, registration_manager=restarted_manager, now="2026-08-26T00:00:01Z", attempts=lambda _old, _new: ())
    effects = EffectLog(records.database_path)
    backfill_interrupted_effects(
        effects, (_upgrade_intent(restarted.authority.load(cutover)),),
        now=100, lease_owner="legacy-test",
    )
    EffectReaper(effects).recover_expired(now=101)
    assert restarted.dispatch_effects(
        EffectRunner(effects, owner_id="test-upgrade-restart")
    )[0]["stage"] == "rolled_back"

    old_artifact = restarted_artifacts.resolve("hand-plugin", hand_id="summarize")
    capability_id = plugin_hands_capability(restarted_activation.resolve_active("hand-plugin", hand_id="summarize")).capability_id
    lease = restarted_registry.resolve(capability_id)
    assert old_artifact.package_record_id == "hand-plugin~1.0.0"
    assert old_artifact.root.joinpath("payload", "main.py").read_bytes() == b"old-frozen-bytes\n"
    assert restarted.authority.load(cutover)["stage"] == "rolled_back"
    assert len(restarted_registry.list()) == 1 and lease is not None
    assert getattr(lease[1], "package") == "hand-plugin~1.0.0"
    assert [provider.package for provider in providers] == ["hand-plugin~2.0.0", "hand-plugin~1.0.0"]
    assert providers[0].closed == 1 and providers[1].closed == 0
