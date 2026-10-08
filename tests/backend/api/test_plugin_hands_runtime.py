from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
from threading import Event, Thread
from types import MappingProxyType, SimpleNamespace

import pytest

from backend.api.plugin_hands_runtime import (
    FixedPluginHandsPythonRuntimeCatalog,
    FrozenPluginHandsLeaseAuthority,
    PluginHandsCapabilityProvider,
    PluginHandsInProcessAdmission,
    PluginHandsRegistrationManager,
    PluginHandsUpgradeRuntime,
    PLUGIN_HANDS_RECIPE_REVISION,
    PLUGIN_HANDS_RESOURCE_POLICY_REVISION,
    plugin_hands_capability,
    shutdown_plugin_hands_runtime,
    _rollback_payload_matches_upgrade_receipt,
)
from core.ai_kernel import ScopedCapabilityRegistry
from core.ai_kernel.dispatcher import ToolCancellationToken, ToolExecutionContext, ToolProviderFailure
from core.plugin_hands.contained_host import WindowsContainedPluginHandsHost
from core.plugin_hands.contracts import PluginHandsLease, PluginHandsOutcome
from core.plugin_hands.workspace import PluginHandsWorkspaceManager
from core.plugin_host.hands_activation import PluginHandsActivation
from core.plugin_host.hands_artifact import ManagedHandsArtifact
from core.plugin_host.hands_upgrade import PluginHandsUpgradeConflict, PluginHandsUpgradeSnapshot
from core.plugin_host.package_intake import candidate_stage_receipt_object_id
from core.storage_provider import SQLiteStructuredRecordStore


SCHEMA = {"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"], "additionalProperties": False}


class _Activation:
    def __init__(self, value: PluginHandsActivation | None) -> None:
        self.value = value

    def resolve_active(self, plugin_id: str, *, hand_id: str):
        if self.value is not None and (plugin_id, hand_id) != (self.value.plugin_id, self.value.hand_id):
            raise AssertionError
        return self.value


class _Artifacts:
    def __init__(self, value: ManagedHandsArtifact) -> None:
        self.value = value

    def resolve(self, plugin_id: str, *, hand_id: str):
        assert (plugin_id, hand_id) == (self.value.plugin_id, self.value.hand_id)
        return self.value


class _LeaseAuthority:
    def __init__(self, *, resources=("workspace_input",)) -> None:
        self.resources = resources

    def resolve(self, request, activation):
        return PluginHandsLease(
            "lease-00000001", str(request["tool_call_id"]), 1,
            "project-0000001", str(request["turn_id"]), 1,
            "recipe-0000001", tuple(self.resources), "2099-08-26T00:00:00Z",
        )


class _Host(WindowsContainedPluginHandsHost):
    def __init__(self, status: str = "success") -> None:
        self.status = status
        self.calls = 0

    def execute_prepared(self, workspace, launch, invocation, control):
        self.calls += 1
        assert launch.argv == ("-I", "-B", "-u", "code/payload/main.py")
        assert workspace.code_dir.joinpath("payload", "main.py").read_bytes() == b"print('fixture')\r\n"
        if self.status == "success":
            return PluginHandsOutcome(invocation.invocation_id, invocation.lease.lease_id, "success", {"value": "done"})
        return PluginHandsOutcome(invocation.invocation_id, invocation.lease.lease_id, "unknown", error_code="protocol-error")


def _active() -> PluginHandsActivation:
    return PluginHandsActivation(
        "plugin-0000001", "hand-00000001", "package-000001", 1, 1,
        "containment-r1", "plugin-hands-artifact:plugin-0000001:hand-00000001:r1",
        "python-stdio-v1", "payload/main.py", SCHEMA, SCHEMA, "read", "read_only",
        ("workspace_input",), 1,
    )


def _artifact(tmp_path: Path) -> ManagedHandsArtifact:
    root = tmp_path / "artifact"
    root.mkdir()
    return ManagedHandsArtifact(
        "plugin-0000001", "hand-00000001", "package-000001", 1, 1,
        "containment-r1", "python-stdio-v1", "payload/main.py", SCHEMA, SCHEMA,
        "read", "read_only", ("workspace_input",),
        (("payload/main.py", b"print('fixture')\r\n"),),
        "plugin-hands-artifact:plugin-0000001:hand-00000001:r1", root,
    )


def _request(**changes):
    capability_id = "plugin.hand.plugin-0000001.hand-00000001"
    value = {
        "turn_id": "turn-0000000001", "operation_id": "operation-0001",
        "arguments": {"value": "go"}, "tool_call_id": "invoke-0000001",
        "timeout_ms": 1000, "intent_ref": "crp://session/turn-0000000001/tool-intent/1",
        "capability_id": capability_id, "capability_version": 1,
        "tool_contract": {
            "tool_id": capability_id, "version": 1, "effect": "read", "source": "plugin",
            "owner_id": "plugin-0000001", "destination": "local",
            "operation_semantics": "read_only", "receipt_schema_uri": None,
            "egress_class": "none", "network_scope": [], "idempotency": "idempotent",
            "retry_policy": {"max_attempts": 1, "backoff_ms": 0, "retryable_error_codes": []},
        },
        "authorization_facts_ref": "crp://session/turn-0000000001/facts/1",
        "authorization_facts_revision": "facts-r1", "approval_fact_ref": None,
        "execution_context": ToolExecutionContext("invoke-0000001", 1, 1000, ToolCancellationToken()),
    }
    value.update(changes)
    return value


def _provider(tmp_path: Path, *, active=None, artifact=None, host=None, resources=("workspace_input",), admission=None, workspace_manager_factory=None):
    workspace_root = tmp_path / "workspaces"
    if workspace_manager_factory is None:
        workspace_root.mkdir()
    runtime = tmp_path / "python.exe"
    return PluginHandsCapabilityProvider(
        plugin_id="plugin-0000001", hand_id="hand-00000001",
        capability_id="plugin.hand.plugin-0000001.hand-00000001",
        launch_recipe_revision="recipe-0000001",
        activation=_Activation(_active() if active is None else active),
        artifacts=_Artifacts(_artifact(tmp_path) if artifact is None else artifact),
        lease_authority=_LeaseAuthority(resources=resources),
        runtime_catalog=FixedPluginHandsPythonRuntimeCatalog(runtime.resolve(), recipe_revision="recipe-0000001"),
        lifecycle_records=SQLiteStructuredRecordStore(tmp_path / "records.sqlite3"),
        workspace_manager=(None if workspace_manager_factory is not None else PluginHandsWorkspaceManager(workspace_root.resolve())),
        workspace_manager_factory=workspace_manager_factory,
        contained_host=host or _Host(), containment_profile_exists=lambda revision: revision == "containment-r1", admission=admission,
    )


def test_unregistered_bridge_stages_reviewed_code_and_returns_atomic_receipt_candidate(tmp_path: Path) -> None:
    provider = _provider(tmp_path)
    result = provider.invoke(_request())
    assert result["result"] == {"value": "done"}
    assert result["operation_receipt"]["artifact_ref"].startswith("plugin-hands-artifact:")
    assert not (tmp_path / "workspaces" / "lease-00000001").exists()


def test_provider_defers_workspace_factory_until_real_invocation(tmp_path: Path) -> None:
    workspace_root = tmp_path / "lazy-workspaces"
    calls = 0
    manager = None

    def factory() -> PluginHandsWorkspaceManager:
        nonlocal calls, manager
        calls += 1
        if manager is None:
            workspace_root.mkdir(exist_ok=True)
            manager = PluginHandsWorkspaceManager(workspace_root.resolve())
        return manager

    provider = _provider(tmp_path, workspace_manager_factory=factory)
    assert calls == 0 and not workspace_root.exists()

    result = provider.invoke(_request())

    assert result["result"] == {"value": "done"}
    assert calls >= 1 and workspace_root.is_dir()


def test_shared_capacity_rejects_before_lifecycle_and_releases_exactly_once(tmp_path: Path) -> None:
    admission = PluginHandsInProcessAdmission(1)
    held = admission.try_acquire()
    assert held is not None and admission.try_acquire() is None
    provider = _provider(tmp_path, admission=admission)
    with pytest.raises(ToolProviderFailure) as failure:
        provider.invoke(_request())
    assert (failure.value.error_code, failure.value.effect_certainty) == ("plugin_hands_capacity_exhausted", "confirmed_none")
    assert provider._lifecycle.load("invoke-0000001") is None
    assert not any((tmp_path / "workspaces").iterdir())
    held.close(); held.close()
    released = admission.try_acquire()
    assert released is not None
    released.close()


def test_two_providers_share_capacity_and_terminal_releases_slot(tmp_path: Path) -> None:
    admission = PluginHandsInProcessAdmission(1)
    entered, release = Event(), Event()

    class BlockingHost(_Host):
        def execute_prepared(self, workspace, launch, invocation, control):
            self.calls += 1
            entered.set()
            assert release.wait(2)
            return PluginHandsOutcome(invocation.invocation_id, invocation.lease.lease_id, "success", {"value": "done"})

    first_root, second_root = tmp_path / "first", tmp_path / "second"
    first_root.mkdir(); second_root.mkdir()
    first = _provider(first_root, host=BlockingHost(), admission=admission)
    second = _provider(second_root, admission=admission)
    outcomes = []
    worker = Thread(target=lambda: outcomes.append(first.invoke(_request())))
    worker.start()
    assert entered.wait(2)
    with pytest.raises(ToolProviderFailure) as exhausted:
        second.invoke(_request())
    assert exhausted.value.error_code == "plugin_hands_capacity_exhausted"
    assert second._lifecycle.load("invoke-0000001") is None
    release.set(); worker.join(3)
    assert not worker.is_alive() and outcomes[0]["result"] == {"value": "done"}
    assert second.invoke(_request())["result"] == {"value": "done"}


def test_disabled_input_schema_and_resource_expansion_fail_before_host(tmp_path: Path) -> None:
    disabled_root = tmp_path / "disabled"
    disabled_root.mkdir()
    host = _Host()
    with pytest.raises(ToolProviderFailure) as disabled:
        _provider(disabled_root, active=False, host=host).invoke(_request())
    assert disabled.value.effect_certainty == "confirmed_none" and host.calls == 0

    schema_root = tmp_path / "schema"
    schema_root.mkdir()
    host = _Host()
    with pytest.raises(ToolProviderFailure) as invalid:
        _provider(schema_root, host=host).invoke(_request(arguments={"other": "x"}))
    assert invalid.value.effect_certainty == "confirmed_none" and host.calls == 0

    resource_root = tmp_path / "resource"
    resource_root.mkdir()
    host = _Host()
    with pytest.raises(ToolProviderFailure) as expanded:
        _provider(resource_root, host=host, resources=("workspace_input", "workspace_output")).invoke(_request())
    assert expanded.value.effect_certainty == "confirmed_none" and host.calls == 0


def test_unknown_host_effect_maps_to_tool_unknown_and_retains_workspace(tmp_path: Path) -> None:
    host = _Host("unknown")
    provider = _provider(tmp_path, host=host)
    with pytest.raises(ToolProviderFailure) as failure:
        provider.invoke(_request())
    assert failure.value.effect_certainty == "unknown"
    assert host.calls == 1
    assert (tmp_path / "workspaces" / "lease-00000001").is_dir()


def test_artifact_and_activation_drift_fail_before_staging(tmp_path: Path) -> None:
    artifact = _artifact(tmp_path)
    drifted = replace(artifact, materialization_revision=2)
    host = _Host()
    with pytest.raises(ToolProviderFailure) as failure:
        _provider(tmp_path, artifact=drifted, host=host).invoke(_request())
    assert failure.value.effect_certainty == "confirmed_none" and host.calls == 0


def test_frozen_tool_effect_semantics_and_owner_must_match_before_workspace(tmp_path: Path) -> None:
    for index, field in enumerate(("effect", "operation_semantics", "owner_id")):
        root = tmp_path / str(index)
        root.mkdir()
        request = _request()
        request["tool_contract"] = dict(request["tool_contract"]) | {field: "drifted"}
        host = _Host()
        with pytest.raises(ToolProviderFailure) as failure:
            _provider(root, host=host).invoke(request)
        assert failure.value.effect_certainty == "confirmed_none" and host.calls == 0


class _FailingLeaseAuthority:
    def resolve(self, _request, _activation):
        raise RuntimeError(r"private C:\\plugin-hands\\activation-42")


class _UnavailableArtifacts:
    def resolve(self, _plugin_id, *, hand_id):
        raise OSError(r"private C:\\plugin-hands\\managed-tree\\package-42")


@pytest.mark.parametrize(
    ("phase", "error_code"),
    (
        ("activation", "plugin_hands_activation_unavailable"),
        ("frozen_tool_contract", "plugin_hands_frozen_tool_contract_invalid"),
        ("artifact_unavailable", "plugin_hands_artifact_unavailable"),
        ("artifact_binding", "plugin_hands_artifact_binding_invalid"),
        ("input_schema", "plugin_hands_input_schema_invalid"),
        ("intent_identity", "plugin_hands_intent_identity_invalid"),
        ("frozen_lease", "plugin_hands_frozen_lease_unavailable"),
        ("lease_binding", "plugin_hands_lease_binding_invalid"),
        ("execution_context", "plugin_hands_execution_context_invalid"),
    ),
)
def test_pre_lifecycle_failures_have_stable_non_sensitive_codes_and_no_attempt(
    tmp_path: Path, phase: str, error_code: str,
) -> None:
    root = tmp_path / phase
    root.mkdir()
    host = _Host()
    request = _request()
    kwargs = {"host": host}
    if phase == "activation":
        kwargs["active"] = False
    elif phase == "artifact_binding":
        kwargs["artifact"] = replace(_artifact(root), materialization_revision=2)
    elif phase == "input_schema":
        request["arguments"] = {"unreviewed": "value"}
    elif phase == "frozen_tool_contract":
        request["tool_contract"] = None
    elif phase == "intent_identity":
        request["intent_ref"] = ""
    elif phase == "lease_binding":
        kwargs["resources"] = ("workspace_input", "workspace_output")
    elif phase == "execution_context":
        request["execution_context"] = object()

    provider = _provider(root, **kwargs)
    if phase == "frozen_lease":
        provider._lease_authority = _FailingLeaseAuthority()
    elif phase == "artifact_unavailable":
        provider._artifacts = _UnavailableArtifacts()

    with pytest.raises(ToolProviderFailure) as failure:
        provider.invoke(request)

    assert failure.value.error_code == error_code
    assert failure.value.effect_certainty == "confirmed_none"
    assert "activation-42" not in str(failure.value)
    assert "managed-tree" not in str(failure.value)
    assert host.calls == 0
    assert provider._lifecycle.load("invoke-0000001") is None


def test_frozen_artifact_schema_is_semantically_equal_to_json_activation_schema(tmp_path: Path) -> None:
    root = tmp_path / "frozen-schema"
    root.mkdir()
    frozen_schema = MappingProxyType({
        "type": "object",
        "properties": MappingProxyType({"value": MappingProxyType({"type": "string"})}),
        "required": ("value",),
        "additionalProperties": False,
    })
    artifact = replace(_artifact(root), input_schema=frozen_schema, output_schema=frozen_schema)
    assert _provider(root, artifact=artifact).invoke(_request())["result"] == {"value": "done"}


def test_artifact_schema_value_drift_remains_a_binding_failure(tmp_path: Path) -> None:
    root = tmp_path / "schema-drift"
    root.mkdir()
    drifted_schema = dict(SCHEMA) | {"required": ["other"]}
    provider = _provider(root, artifact=replace(_artifact(root), input_schema=drifted_schema))

    with pytest.raises(ToolProviderFailure) as failure:
        provider.invoke(_request())

    assert failure.value.error_code == "plugin_hands_artifact_binding_invalid"
    assert failure.value.effect_certainty == "confirmed_none"
    assert provider._lifecycle.load("invoke-0000001") is None


class _FrozenFacts:
    project_id = "project-0000001"
    boundary_profile_revision = 7
    revision = "profile-1-boundary-7"
    capabilities = (type("Fact", (), {"capability_id": "plugin.hand.plugin-0000001.hand-00000001"})(),)


class _FrozenAuthority:
    def current_handle(self, *, turn_id):
        assert turn_id == "turn-0000000001"
        return type("Handle", (), {
            "facts_ref": "crp://session/turn-0000000001/facts/1",
            "facts_revision": "facts-r1", "facts": _FrozenFacts(),
        })()


def test_frozen_lease_consumes_only_hydrated_turn_facts() -> None:
    authority = FrozenPluginHandsLeaseAuthority(
        _FrozenAuthority(), recipe_revision="recipe-0000001",
    )
    lease = authority.resolve(_request(scope={"kind": "project", "project_id": "project-0000001"}), _active())
    assert lease.project_id == "project-0000001"
    assert lease.boundary_revision == 7
    assert lease.allowed_resources == ("workspace_input",)


def test_recovery_validates_original_lease_without_extending_expiry() -> None:
    from datetime import datetime, timezone

    now = datetime(2026, 8, 30, 0, 0, tzinfo=timezone.utc)
    authority = FrozenPluginHandsLeaseAuthority(
        _FrozenAuthority(), recipe_revision="recipe-0000001", now=lambda: now,
    )
    request = _request(scope={"kind": "project", "project_id": "project-0000001"})
    original = authority.resolve(request, _active())

    authority.validate_recovery(request, _active(), original)
    expired = PluginHandsLease(
        original.lease_id, original.invocation_id, original.generation,
        original.project_id, original.turn_id, original.boundary_revision,
        original.recipe_revision, original.allowed_resources,
        "2026-08-29T23:59:59Z", original.resource_policy_revision,
    )
    with pytest.raises(ValueError, match="unavailable"):
        authority.validate_recovery(request, _active(), expired)


class _ActiveSnapshot:
    def __init__(self, values): self.values = values
    def all_active(self): return tuple(self.values)


def test_registration_manager_uses_revocable_sole_registry_lease() -> None:
    active = _active()
    activation = _ActiveSnapshot((active,))
    registry = ScopedCapabilityRegistry()
    class Provider:
        closed = False
        def close(self): self.closed = True
    provider = Provider()
    manager = PluginHandsRegistrationManager(
        activation=activation, registry=registry,
        provider_factory=lambda _active, _capability_id: provider,
    )
    registered = manager.reconcile()
    capability = plugin_hands_capability(active)
    assert registered == (capability,)
    assert registry.resolve(capability.capability_id) == (capability, provider)
    activation.values = ()
    assert manager.reconcile() == ()
    assert registry.resolve(capability.capability_id) is None
    assert provider.closed is True


class _UpgradeActivation:
    def __init__(self, old: PluginHandsActivation, new: PluginHandsActivation) -> None:
        self.old, self.new, self.value = old, new, old
        self.switches = []

    def resolve_active(self, plugin_id: str, *, hand_id: str):
        assert (plugin_id, hand_id) == (self.value.plugin_id, self.value.hand_id)
        return self.value

    def all_active(self):
        return (self.value,)

    def switch_upgrade(self, plugin_id: str, *, hand_id: str, cutover_id: str, phase: str, old, new):
        assert (plugin_id, hand_id) == (self.old.plugin_id, self.old.hand_id)
        self.switches.append((cutover_id, phase, old, new))
        self.value = self.new if phase == "old_revoked" else self.old


def _upgrade_snapshot(active: PluginHandsActivation) -> PluginHandsUpgradeSnapshot:
    return PluginHandsUpgradeSnapshot(
        active.package_record_id, active.review_revision, active.materialization_revision,
        active.activation_revision, PLUGIN_HANDS_RECIPE_REVISION, PLUGIN_HANDS_RESOURCE_POLICY_REVISION,
    )


def test_upgrade_runtime_revokes_before_switch_and_reprojects_without_double_registration(tmp_path: Path) -> None:
    old = _active()
    new = replace(
        old, package_record_id="package-000002", review_revision=2,
        materialization_revision=2, activation_revision=2,
        artifact_opaque_ref="plugin-hands-artifact:plugin-0000001:hand-00000001:r2",
    )
    activation = _UpgradeActivation(old, new)
    registry = ScopedCapabilityRegistry()
    providers = []

    class Provider:
        closed = False
        def close(self): self.closed = True

    manager = PluginHandsRegistrationManager(
        activation=activation, registry=registry,
        provider_factory=lambda _active, _capability_id: providers.append(Provider()) or providers[-1],
    )
    manager.reconcile()
    old_provider = providers[-1]
    runtime = PluginHandsUpgradeRuntime(
        records=SQLiteStructuredRecordStore(tmp_path / "upgrades.sqlite3"),
        activation=activation, registration_manager=manager,
        now="2026-08-26T00:00:00Z", validate_snapshots=lambda *_args: None,
        attempts=lambda _old, _new: (),
    )
    cutover = "cutover-00000001"
    runtime.authority.begin(cutover, old.plugin_id, hand_id=old.hand_id, old=_upgrade_snapshot(old), new=_upgrade_snapshot(new))
    completed = runtime.authority.resume(cutover, ports=runtime.ports)

    assert completed["stage"] == "completed"
    assert old_provider.closed is True
    assert [phase for _cutover, phase, _old, _new in activation.switches] == ["old_revoked"]
    assert len(providers) == 2
    assert manager.is_registered(new) is True
    assert registry.resolve(plugin_hands_capability(new).capability_id)[1] is providers[-1]

    replay = runtime.authority.resume(cutover, ports=runtime.ports)
    assert replay["stage"] == "completed"
    assert len(providers) == 2


def test_rollback_runtime_accepts_new_activation_revision_only_with_durable_switch_receipt() -> None:
    old, new = _upgrade_snapshot(_active()), _upgrade_snapshot(replace(
        _active(), package_record_id="package-000002", review_revision=2,
        materialization_revision=2, activation_revision=2,
    ))
    cutover = "cutover-00000001"
    restored = {
        "package_record_id": old.package_record_id,
        "review_revision": old.review_revision,
        "materialization_revision": old.materialization_revision,
    }
    receipt = SimpleNamespace(payload={
        "schema_version": "1.0.0",
        "plugin_id": "plugin-0000001", "hand_id": "hand-00000001",
        "cutover_id": cutover, "phase": "rollback_new_revoked",
        "old": {
            "package_record_id": old.package_record_id, "review_revision": old.review_revision,
            "materialization_revision": old.materialization_revision, "activation_revision": old.activation_revision,
            "runtime_revision": old.runtime_revision, "resource_policy_revision": old.resource_policy_revision,
        },
        "new": {
            "package_record_id": new.package_record_id, "review_revision": new.review_revision,
            "materialization_revision": new.materialization_revision, "activation_revision": new.activation_revision,
            "runtime_revision": new.runtime_revision, "resource_policy_revision": new.resource_policy_revision,
        },
        "prior_activation": {
            "package_record_id": new.package_record_id,
            "review_revision": new.review_revision,
            "materialization_revision": new.materialization_revision,
        },
        "result": {"activation": dict(restored), "activation_revision": 3},
    })
    owner = SimpleNamespace(payload={"cutover_id": cutover})

    def read(collection, _object_id):
        if collection == "plugin_hands_upgrade_active":
            return owner
        if collection == "plugin_hands_activation_upgrade_receipts":
            return receipt
        return None

    assert _rollback_payload_matches_upgrade_receipt(
        restored, 3, read, "plugin-0000001", "hand-00000001", old, new,
    ) is True
    assert _rollback_payload_matches_upgrade_receipt(
        restored, 1, read, "plugin-0000001", "hand-00000001", old, new,
    ) is False


def test_upgrade_acquisition_requires_the_frozen_candidate_stage_receipt(tmp_path: Path) -> None:
    plugin, hand = "plugin-0000001", "hand-00000001"
    old = PluginHandsUpgradeSnapshot(
        f"{plugin}~1.0.0", 1, 1, 1,
        PLUGIN_HANDS_RECIPE_REVISION, PLUGIN_HANDS_RESOURCE_POLICY_REVISION,
    )
    new = PluginHandsUpgradeSnapshot(
        f"{plugin}~2.0.0", 2, 2, 2,
        PLUGIN_HANDS_RECIPE_REVISION, PLUGIN_HANDS_RESOURCE_POLICY_REVISION,
    )
    records = SQLiteStructuredRecordStore(tmp_path / "candidate-receipt.sqlite3")
    manager = PluginHandsRegistrationManager(
        activation=_ActiveSnapshot(()), registry=ScopedCapabilityRegistry(),
        provider_factory=lambda *_args: object(),
    )
    runtime = PluginHandsUpgradeRuntime(
        records=records, activation=SimpleNamespace(), registration_manager=manager,
        now="2026-08-26T00:00:00Z", attempts=lambda _old, _new: (),
    )
    activation_payload = {
        "status": "active", "package_record_id": old.package_record_id,
        "review_revision": old.review_revision, "materialization_revision": old.materialization_revision,
    }
    state_payload = {"package_record_id": old.package_record_id}
    receipt_payload = {
        "schema": "1.0.0", "plugin": plugin, "old": old.package_record_id,
        "candidate": new.package_record_id, "state_revision": 1,
        "command_id": "stage-upgrade-0000001",
    }
    with records.begin() as uow:
        uow.put("plugin_hands_activations", f"{plugin}--{hand}", activation_payload, expected_revision=0)
        uow.put("plugin_package_states", plugin, state_payload, expected_revision=0)
        uow.put(
            "plugin_upgrade_stage_receipts", candidate_stage_receipt_object_id(plugin, new.package_record_id),
            receipt_payload, expected_revision=0,
        )
        uow.commit()
    with records.begin() as uow:
        runtime._validate_snapshots(uow, plugin, hand, old, new, "begin", "old")
        uow.rollback()
    with records.begin() as uow:
        state = uow.read("plugin_package_states", plugin)
        uow.put("plugin_package_states", plugin, state_payload, expected_revision=state.revision)
        uow.commit()
    with records.begin() as uow:
        with pytest.raises(PluginHandsUpgradeConflict):
            runtime._validate_snapshots(uow, plugin, hand, old, new, "begin", "old")
        uow.rollback()


def test_shutdown_revokes_registry_projection_and_is_idempotent() -> None:
    active = _active()
    registry = ScopedCapabilityRegistry()
    class Provider:
        closed = False
        def close(self): self.closed = True
    provider = Provider()
    manager = PluginHandsRegistrationManager(
        activation=_ActiveSnapshot((active,)), registry=registry,
        provider_factory=lambda _active, _capability_id: provider,
    )
    manager.reconcile()
    runtime = SimpleNamespace(plugin_hands_registration_manager=manager)
    application = SimpleNamespace(state=SimpleNamespace(
        plugin_hands_registration_manager=manager, ai_runtime=runtime,
    ))

    shutdown_plugin_hands_runtime(application)
    shutdown_plugin_hands_runtime(application)

    assert registry.resolve(plugin_hands_capability(active).capability_id) is None
    assert application.state.plugin_hands_registration_manager is None
    assert runtime.plugin_hands_registration_manager is None
    assert provider.closed is True


@pytest.mark.skipif(os.name != "nt", reason="Windows AppContainer Plugin Hands fixture")
def test_real_powershell_plugin_hand_runs_through_activation_artifact_lifecycle_and_appcontainer(tmp_path: Path) -> None:
    source = (
        "$hello = @{protocol='plugin-hands/1';type='hello';launch_id=$env:CHRIPTMAS_PLUGIN_HANDS_LAUNCH_ID;lease_id=$env:CHRIPTMAS_PLUGIN_HANDS_LEASE_ID;invocation_id=$env:CHRIPTMAS_PLUGIN_HANDS_INVOCATION_ID}\n"
        "[Console]::Out.WriteLine(($hello | ConvertTo-Json -Compress))\n"
        "[Console]::In.ReadLine() | Out-Null\n"
        "[Console]::Error.WriteLine('diagnostic must not enter protocol stdout')\n"
        "[IO.File]::WriteAllText((Join-Path (Get-Location) 'output\\probe.txt'),'contained')\n"
        "$result = @{protocol='plugin-hands/1';type='result';launch_id=$env:CHRIPTMAS_PLUGIN_HANDS_LAUNCH_ID;lease_id=$env:CHRIPTMAS_PLUGIN_HANDS_LEASE_ID;invocation_id=$env:CHRIPTMAS_PLUGIN_HANDS_INVOCATION_ID;output=@{value='contained'}}\n"
        "[Console]::Out.WriteLine(($result | ConvertTo-Json -Compress))\n"
    ).encode("utf-8")
    active = replace(_active(), runtime="powershell-stdio-v1", entrypoint="payload/main.ps1", requested_resources=("workspace_output",))
    artifact = replace(
        _artifact(tmp_path), runtime="powershell-stdio-v1", entrypoint="payload/main.ps1",
        requested_resources=("workspace_output",), payload_files=(("payload/main.ps1", source),),
    )
    workspace_root = tmp_path / "real-workspaces"
    workspace_root.mkdir()
    runtime = Path(__file__).resolve().parents[3] / "runtime" / "python.exe"
    provider = PluginHandsCapabilityProvider(
        plugin_id="plugin-0000001", hand_id="hand-00000001",
        capability_id="plugin.hand.plugin-0000001.hand-00000001",
        launch_recipe_revision="recipe-0000001", activation=_Activation(active),
        artifacts=_Artifacts(artifact), lease_authority=_LeaseAuthority(resources=("workspace_output",)),
        runtime_catalog=FixedPluginHandsPythonRuntimeCatalog(runtime.resolve(strict=True), recipe_revision="recipe-0000001"),
        lifecycle_records=SQLiteStructuredRecordStore(tmp_path / "real-records.sqlite3"),
        workspace_manager=PluginHandsWorkspaceManager(workspace_root.resolve()),
        contained_host=WindowsContainedPluginHandsHost(),
        containment_profile_exists=lambda revision: revision == "containment-r1",
    )
    response = provider.invoke(_request(timeout_ms=5_000))
    assert response["result"] == {"value": "contained"}
    assert not (workspace_root / "lease-00000001").exists()
