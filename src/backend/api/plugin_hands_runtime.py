"""Production composition for governed Plugin Hands execution.

This is the only seam allowed to know the existing Tool provider request, the
Plugin activation/artifact authorities, and the contained Hands lifecycle.
Registration remains fail-closed: only durably active, production-admitted
recipes enter the sole AI runtime Registry.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
from threading import RLock
from typing import Protocol
from uuid import NAMESPACE_URL, uuid5

from jsonschema import Draft202012Validator, SchemaError

from core.ai_kernel import CapabilityDefinition, CapabilityRegistryPort, SQLiteAITurnStore
from core.ai_kernel.dispatcher import ToolCancellationToken, ToolExecutionContext, ToolProviderFailure
from core.ai_kernel.ports import CapabilityRegistrationPort
from core.ai_kernel.tool_invocation import ToolInvocationIntent, intent_from_payload
from core.ai_tooling import ToolDefinition, ToolRetryPolicy
from core.effect_log import (
    Effect, EffectClass, EffectHandlerAbandoned, EffectHandlerRegistration, EffectIntent, EffectLog, EffectRunner,
    backfill_interrupted_effects, is_effect_planned,
)
from core.plugin_hands.contained_host import WindowsContainedPluginHandsHost
from core.plugin_hands.contracts import (
    PluginHandsControl,
    PluginHandsInvocation,
    PluginHandsLaunch,
    PluginHandsLease,
    require_exact_workspace_resources,
)
from core.plugin_hands.durable_lifecycle import (
    PluginHandsDurableLifecycle,
    PluginHandsDurableLifecycleError,
    PluginHandsExecutionScope,
    PluginHandsLifecycleBinding,
    execute_plugin_hands_workspace_cleanup,
)
from core.plugin_hands.workspace import PluginHandsWorkspace, PluginHandsWorkspaceManager
from core.plugin_hands.windows_appcontainer import AppContainerResourceLimits
from core.plugin_host.hands_activation import (
    PluginHandsActivation,
    PluginHandsActivationAuthority,
    PluginHandsActivationConflict,
)
from core.plugin_host.hands_artifact import ManagedHandsArtifact, PluginHandsArtifactService
from core.plugin_host.package_intake import candidate_stage_receipt_object_id
from core.plugin_host.hands_upgrade import (
    PluginHandsUpgradeAuthority,
    PluginHandsUpgradeConflict,
    PluginHandsUpgradePorts,
    PluginHandsUpgradeSnapshot,
    decode_plugin_hands_upgrade_record,
)
from core.storage_provider import SQLiteStructuredRecordStore


PLUGIN_HANDS_CONTAINMENT_PROFILE = "appcontainer-v1"
PLUGIN_HANDS_RECIPE_REVISION = "powershell-stdio-v1-r1"
PLUGIN_HANDS_RESOURCE_POLICY_REVISION = "plugin-hands-resource-v1"
PLUGIN_HANDS_MAX_CONCURRENT = 2
PLUGIN_HANDS_PROCESS_MEMORY_BYTES = 256 * 1024 * 1024
PLUGIN_HANDS_CPU_RATE = 2500


def load_plugin_hands_tool_intent(
    payloads: SQLiteAITurnStore, effect: Effect,
) -> ToolInvocationIntent:
    """Load the immutable parent Tool intent needed by a restart Handler.

    The Hands lifecycle deliberately does not copy arguments.  A reconstructable
    Handler must consume the AI Kernel's already-governed intent and prove its
    identity against the Core Effect before composing any executable objects.
    """

    if (
        not isinstance(payloads, SQLiteAITurnStore)
        or not isinstance(effect, Effect)
        or effect.kind != "plugin_hands_execution"
        or effect.effect_class is not EffectClass.AT_MOST_ONCE
        or effect.operation_id != f"plugin-hands-effect-{effect.root_id}"
        or not isinstance(effect.turn_id, str)
        or not effect.turn_id
        or not isinstance(effect.intent_ref, str)
        or not effect.intent_ref
    ):
        raise PluginHandsDurableLifecycleError(
            "Plugin Hands execution Effect identity is invalid"
        )
    try:
        intent = intent_from_payload(payloads.get(effect.intent_ref))
    except Exception as error:
        raise PluginHandsDurableLifecycleError(
            "Plugin Hands immutable Tool intent is unavailable"
        ) from error
    contract = intent.tool_contract
    if (
        intent.invocation_id != effect.root_id
        or intent.turn_id != effect.turn_id
        or intent.idempotency_key != f"{intent.operation_id}:{intent.invocation_id}"
        or not isinstance(contract, Mapping)
        or contract.get("tool_id") != intent.capability_id
        or contract.get("version") != intent.capability_version
        or contract.get("source") != "plugin"
    ):
        raise PluginHandsDurableLifecycleError(
            "Plugin Hands immutable Tool intent drifted"
        )
    return intent


class PluginHandsAdmissionLease:
    def __init__(self, release: Callable[[], None]) -> None:
        self._release = release
        self._closed = False

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._release()


class PluginHandsInProcessAdmission:
    """Shared sidecar capacity fence; the Windows Job enforces per-Hand limits."""

    def __init__(self, max_concurrent: int) -> None:
        if not isinstance(max_concurrent, int) or isinstance(max_concurrent, bool) or max_concurrent < 1:
            raise ValueError("Plugin Hands capacity is invalid")
        self._max = max_concurrent
        self._used = 0
        self._lock = RLock()

    def try_acquire(self) -> PluginHandsAdmissionLease | None:
        with self._lock:
            if self._used >= self._max:
                return None
            self._used += 1
        return PluginHandsAdmissionLease(self._release)

    def _release(self) -> None:
        with self._lock:
            if self._used < 1:
                raise RuntimeError("Plugin Hands capacity release drifted")
            self._used -= 1


def build_plugin_hands_artifacts(root_dir: Path) -> PluginHandsArtifactService:
    root = root_dir.expanduser().resolve(strict=False)
    records = SQLiteStructuredRecordStore(root / ".rebuild-data" / "jobs.sqlite3")
    return PluginHandsArtifactService(
        records,
        managed_root=root / ".rebuild-data" / "plugin-hands-materializations",
        now=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    )


def build_plugin_hands_activation(root_dir: Path) -> PluginHandsActivationAuthority:
    root = root_dir.expanduser().resolve(strict=False)
    records = SQLiteStructuredRecordStore(root / ".rebuild-data" / "jobs.sqlite3")
    return PluginHandsActivationAuthority(
        records,
        artifacts=build_plugin_hands_artifacts(root),
        now=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    )


def _workspace_manager_factory(workspace_root: Path) -> Callable[[], PluginHandsWorkspaceManager]:
    """Create one strict manager only when cleanup or an invocation needs it."""
    if not isinstance(workspace_root, Path) or not workspace_root.is_absolute():
        raise ValueError("Plugin Hands workspace root is invalid")
    # Preserve the configured lexical path: resolving here would erase a
    # root-level link/reparse identity before the strict manager can reject it.
    configured_root = workspace_root.absolute()
    lock = RLock()
    manager: PluginHandsWorkspaceManager | None = None

    def resolve() -> PluginHandsWorkspaceManager:
        nonlocal manager
        with lock:
            if manager is None:
                configured_root.mkdir(parents=True, exist_ok=True)
                manager = PluginHandsWorkspaceManager(configured_root)
            return manager

    return resolve


class PluginHandsLeaseAuthority(Protocol):
    """Resolve an already-frozen Tool intent into its exact workspace lease."""

    def resolve(
        self,
        request: Mapping[str, object],
        activation: PluginHandsActivation,
    ) -> PluginHandsLease: ...


class PluginHandsRuntimeCatalog(Protocol):
    """Resolve one OS-owned runtime recipe; Plugins never provide this path."""

    def resolve(self, *, runtime: str, recipe_revision: str) -> Path: ...


class FixedPluginHandsPythonRuntimeCatalog:
    """OS-owned Python plus Windows PowerShell recipes; Plugins supply neither path."""

    def __init__(self, executable: Path, *, recipe_revision: str) -> None:
        if not isinstance(executable, Path) or not executable.is_absolute():
            raise ValueError("Plugin Hands runtime executable is invalid")
        if not isinstance(recipe_revision, str) or not recipe_revision or "\x00" in recipe_revision:
            raise ValueError("Plugin Hands runtime recipe revision is invalid")
        self._executable = executable
        self._revision = recipe_revision

    def resolve(self, *, runtime: str, recipe_revision: str) -> Path:
        if recipe_revision != self._revision:
            raise ValueError("Plugin Hands runtime recipe is unavailable")
        if runtime == "python-stdio-v1":
            return self._executable
        if runtime == "powershell-stdio-v1" and os.name == "nt":
            executable = Path(os.environ["SYSTEMROOT"]) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
            return executable.resolve(strict=True)
        raise ValueError("Plugin Hands runtime recipe is unavailable")


class FrozenPluginHandsLeaseAuthority:
    """Derive one lease only from the already-hydrated Turn authorization facts."""

    def __init__(self, frozen_authorization, *, recipe_revision: str, resource_policy_revision: str = PLUGIN_HANDS_RESOURCE_POLICY_REVISION, now: Callable[[], datetime] | None = None) -> None:
        self._frozen = frozen_authorization
        self._recipe = _text(recipe_revision, "recipe_revision")
        self._resource_policy = _text(resource_policy_revision, "resource_policy_revision")
        self._now = now or (lambda: datetime.now(timezone.utc))

    def resolve(self, request: Mapping[str, object], activation: PluginHandsActivation) -> PluginHandsLease:
        turn_id = _text(request.get("turn_id"), "turn_id")
        invocation_id = _text(request.get("tool_call_id"), "tool_call_id")
        facts_ref = _text(request.get("authorization_facts_ref"), "authorization_facts_ref")
        facts_revision = _text(request.get("authorization_facts_revision"), "authorization_facts_revision")
        handle = self._frozen.current_handle(turn_id=turn_id)
        if handle.facts_ref != facts_ref or handle.facts_revision != facts_revision:
            raise ValueError("Plugin Hands frozen authorization binding drifted")
        facts = handle.facts
        capability_id = _text(request.get("capability_id"), "capability_id")
        authorized = next((item for item in facts.capabilities if item.capability_id == capability_id), None)
        if authorized is None:
            raise ValueError("Plugin Hands capability is absent from frozen authorization")
        scope = request.get("scope")
        if not isinstance(scope, Mapping) or scope.get("kind") != "project" or scope.get("project_id") != facts.project_id:
            raise ValueError("Plugin Hands project scope drifted")
        timeout_ms = min(_positive_int(request.get("timeout_ms"), "timeout_ms"), 300_000)
        expires = self._now().astimezone(timezone.utc) + timedelta(milliseconds=timeout_ms)
        lease_id = "handslease-" + uuid5(NAMESPACE_URL, f"{turn_id}:{invocation_id}:{facts.revision}").hex
        return PluginHandsLease(
            lease_id, invocation_id, activation.activation_revision, facts.project_id,
            turn_id, facts.boundary_profile_revision, self._recipe,
            activation.requested_resources, expires.isoformat().replace("+00:00", "Z"), self._resource_policy,
        )

    def validate_recovery(
        self,
        request: Mapping[str, object],
        activation: PluginHandsActivation,
        lease: PluginHandsLease,
    ) -> None:
        """Validate the original frozen lease without silently extending it."""

        expected = self.resolve(request, activation)
        actual_identity = (
            lease.lease_id, lease.invocation_id, lease.generation,
            lease.project_id, lease.turn_id, lease.boundary_revision,
            lease.recipe_revision, lease.allowed_resources,
            lease.resource_policy_revision,
        )
        expected_identity = (
            expected.lease_id, expected.invocation_id, expected.generation,
            expected.project_id, expected.turn_id, expected.boundary_revision,
            expected.recipe_revision, expected.allowed_resources,
            expected.resource_policy_revision,
        )
        try:
            expires_at = datetime.fromisoformat(
                lease.expires_at[:-1] + "+00:00"
            )
        except (TypeError, ValueError) as error:
            raise ValueError("Plugin Hands recovery lease expiry is invalid") from error
        if actual_identity != expected_identity or expires_at <= self._now():
            raise ValueError("Plugin Hands recovery lease is unavailable")


class PluginHandsRegistrationManager:
    """Project durable active Hands into the sole AI capability Registry."""

    def __init__(
        self, *, activation: PluginHandsActivationAuthority, registry: CapabilityRegistryPort,
        provider_factory: Callable[[PluginHandsActivation, str], "PluginHandsCapabilityProvider"],
    ) -> None:
        self._activation = activation
        self._registry = registry
        self._provider_factory = provider_factory
        self._lock = RLock()
        self._leases: dict[str, tuple[tuple[object, ...], CapabilityRegistrationPort, object]] = {}
        self._conflicts: tuple[str, ...] = ()

    @property
    def conflicting_capability_ids(self) -> tuple[str, ...]:
        with self._lock:
            return self._conflicts

    @property
    def activation_authority(self) -> PluginHandsActivationAuthority:
        """Expose the one durable activation authority used by this manager.

        Startup cutover recovery must use this exact instance: creating a
        second activation authority after the initial projection could observe
        a different point-in-time view of the SQLite authority and break the
        revoke -> switch -> register ordering.
        """

        return self._activation

    def reconcile(self) -> tuple[CapabilityDefinition, ...]:
        with self._lock:
            desired: dict[str, tuple[tuple[object, ...], PluginHandsActivation]] = {}
            conflicts: set[str] = set()
            for active in self._activation.all_active():
                capability_id = plugin_hands_capability_id(active.plugin_id, active.hand_id)
                fingerprint = _activation_fingerprint(active)
                if capability_id in desired:
                    conflicts.add(capability_id)
                    desired.pop(capability_id, None)
                elif capability_id not in conflicts:
                    desired[capability_id] = (fingerprint, active)
            for capability_id in tuple(self._leases):
                if capability_id not in desired or self._leases[capability_id][0] != desired[capability_id][0]:
                    _fingerprint, lease, provider = self._leases.pop(capability_id)
                    _close_plugin_hands_provider(provider)
                    lease.close()
            registered: list[CapabilityDefinition] = []
            for capability_id in sorted(desired):
                fingerprint, active = desired[capability_id]
                if capability_id not in self._leases:
                    if self._registry.resolve(capability_id) is not None:
                        conflicts.add(capability_id)
                        continue
                    try:
                        definition = plugin_hands_capability(active)
                        provider = self._provider_factory(active, capability_id)
                        lease = self._registry.register(definition, provider)
                    except Exception:
                        conflicts.add(capability_id)
                        continue
                    self._leases[capability_id] = (fingerprint, lease, provider)
                registered.append(plugin_hands_capability(active))
            self._conflicts = tuple(sorted(conflicts))
            return tuple(registered)

    def revoke(self, plugin_id: str, *, hand_id: str) -> bool:
        """Withdraw only this manager's ephemeral Registry lease.

        A cutover never tries to remove an entry owned by another runtime
        component.  Repeating this operation after a crash is therefore safe:
        an absent local lease already means the old projection is withdrawn.
        """

        capability_id = plugin_hands_capability_id(plugin_id, hand_id)
        with self._lock:
            owned = self._leases.pop(capability_id, None)
            if owned is None:
                return False
            _fingerprint, lease, provider = owned
            _close_plugin_hands_provider(provider)
            lease.close()
            return True

    def is_registered(self, activation: PluginHandsActivation) -> bool:
        """Return whether this manager owns the exact projection snapshot."""

        capability_id = plugin_hands_capability_id(activation.plugin_id, activation.hand_id)
        with self._lock:
            lease = self._leases.get(capability_id)
            return lease is not None and lease[0] == _activation_fingerprint(activation)

    def resolve_provider(self, capability_id: str) -> "PluginHandsCapabilityProvider" | None:
        """Return only the provider projected and owned by this manager."""

        with self._lock:
            owned = self._leases.get(capability_id)
            provider = owned[2] if owned is not None else None
            return provider if isinstance(provider, PluginHandsCapabilityProvider) else None

    def close(self) -> None:
        with self._lock:
            for _fingerprint, lease, provider in tuple(self._leases.values()):
                _close_plugin_hands_provider(provider)
                lease.close()
            self._leases.clear()


class PluginHandsUpgradeRuntime:
    """Production-only ports for one durable Plugin Hands cutover.

    Activation and artifact mutation remain in their own authorities.  This
    adapter owns solely the ephemeral Registry projection: every forward or
    rollback path first revokes the old lease, then lets the activation
    authority switch its durable pointer, then projects the new pointer.
    ``revoke`` and ``reconcile`` are deliberately idempotent, keyed by the
    frozen snapshot; replaying an interrupted phase cannot create a second
    Registry registration.
    """

    def __init__(
        self,
        *,
        records: SQLiteStructuredRecordStore,
        activation: PluginHandsActivationAuthority,
        registration_manager: PluginHandsRegistrationManager,
        now: str,
        validate_snapshots: Callable[..., None] | None = None,
        attempts: Callable[[PluginHandsUpgradeSnapshot, PluginHandsUpgradeSnapshot], Iterable[Mapping[str, object]]] | None = None,
    ) -> None:
        if not isinstance(records, SQLiteStructuredRecordStore):
            raise ValueError("Plugin Hands upgrade records are invalid")
        self._records = records
        self._activation = activation
        self._manager = registration_manager
        validator = validate_snapshots or self._validate_snapshots
        self.authority = PluginHandsUpgradeAuthority(
            records, now=now, validate_snapshots=validator, attempts=attempts,
        )
        self.ports = PluginHandsUpgradePorts(
            revoke=self._revoke,
            switch=self._switch,
            register=self._register,
        )

    def dispatch_effects(self, runner: EffectRunner) -> tuple[dict[str, object], ...]:
        """Resume only cutovers selected as PLANNED by Core Reaper."""

        effects = runner.log
        recovered: list[dict[str, object]] = []
        for record in self._records.list("plugin_hands_upgrade_cutovers"):
            try:
                current = self.authority.load(record.object_id)
                if current is None or current.get("stage") in {"completed", "rolled_back", "finalized"}:
                    continue
                if not is_effect_planned(effects, _upgrade_intent(current).operation_id):
                    continue
                result: dict[str, object] | None = None

                def cutover(_effect):
                    nonlocal result
                    result = self.authority.resume(record.object_id, ports=self.ports)
                    return (
                        f"crp://plugin-hands-upgrade/{result['plugin_id']}/"
                        f"{record.object_id}:r{result['revision']}"
                    )

                runner.execute_planned(
                    record.object_id, cutover,
                    now=int(datetime.now(timezone.utc).timestamp()),
                    receipt_kind="plugin-hands-upgrade-receipt",
                )
                assert result is not None
                recovered.append(result)
            except (PluginHandsUpgradeConflict, PluginHandsActivationConflict, ValueError):
                # Startup recovery is best-effort and must never invent a
                # rollback or projection for a corrupt/foreign record.
                continue
        return tuple(recovered)

    def _revoke(self, snapshot: PluginHandsUpgradeSnapshot, cutover_id: str, phase: str) -> None:
        plugin, hand, old, new = self._cutover(cutover_id)
        _require_upgrade_phase(snapshot, phase, old, new, "revoke")
        self._manager.revoke(plugin, hand_id=hand)

    def _switch(self, snapshot: PluginHandsUpgradeSnapshot, cutover_id: str, phase: str) -> None:
        plugin, hand, old, new = self._cutover(cutover_id)
        _require_upgrade_phase(snapshot, phase, old, new, "switch")
        self._activation.switch_upgrade(
            plugin, hand_id=hand, cutover_id=cutover_id, phase=phase, old=old, new=new,
        )

    def _register(self, snapshot: PluginHandsUpgradeSnapshot, cutover_id: str, phase: str) -> None:
        plugin, hand, old, new = self._cutover(cutover_id)
        _require_upgrade_phase(snapshot, phase, old, new, "register")
        expected = new if phase == "new_switched" else old
        active = self._activation.resolve_active(plugin, hand_id=hand)
        rollback = phase == "rollback_old_switched"
        if active is None or (
            not _activation_matches_upgrade_snapshot(active, expected)
            if not rollback
            else not _rollback_activation_matches_upgrade_receipt(
                active, self._records, plugin, hand, cutover_id, old, new,
            )
        ):
            raise PluginHandsUpgradeConflict("Plugin Hands upgrade activation projection drifted")
        self._manager.reconcile()
        if not self._manager.is_registered(active):
            raise PluginHandsUpgradeConflict("Plugin Hands upgrade Registry projection is unavailable")

    def _cutover(self, cutover_id: str) -> tuple[str, str, PluginHandsUpgradeSnapshot, PluginHandsUpgradeSnapshot]:
        loaded = self.authority.load(cutover_id)
        if loaded is None:
            raise PluginHandsUpgradeConflict("Plugin Hands upgrade is missing")
        try:
            plugin, hand = str(loaded["plugin_id"]), str(loaded["hand_id"])
            old = PluginHandsUpgradeSnapshot(**dict(loaded["old"]))
            new = PluginHandsUpgradeSnapshot(**dict(loaded["new"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise PluginHandsUpgradeConflict("Plugin Hands upgrade record is invalid") from exc
        for snapshot in (old, new):
            if (
                snapshot.runtime_revision != PLUGIN_HANDS_RECIPE_REVISION
                or snapshot.resource_policy_revision != PLUGIN_HANDS_RESOURCE_POLICY_REVISION
            ):
                raise PluginHandsUpgradeConflict("Plugin Hands upgrade runtime policy drifted")
        return plugin, hand, old, new

    def _validate_snapshots(
        self,
        uow,
        plugin_id: str,
        hand_id: str,
        old: PluginHandsUpgradeSnapshot,
        new: PluginHandsUpgradeSnapshot,
        _stage: str,
        active_pointer: str,
    ) -> None:
        """Validate the currently executable pointer in the caller's UoW.

        Candidate byte/tree validation is repeated by ``switch_upgrade`` in
        its committing UoW.  Here we only accept the pointer that is actually
        active now; this prevents a stale cutover from acquiring the Hand.
        """

        expected = old if active_pointer == "old" else new
        current = uow.read("plugin_hands_activations", f"{plugin_id}--{hand_id}")
        if current is None:
            raise PluginHandsUpgradeConflict("Plugin Hands upgrade activation is missing")
        payload = dict(current.payload)
        rollback_old_pointer = active_pointer == "old" and _stage in {"rollback_old_switched", "rollback_old_registered"}
        valid = (
            _rollback_payload_matches_upgrade_receipt(
                payload, current.revision, uow.read, plugin_id, hand_id, old, new,
            )
            if rollback_old_pointer
            else _payload_matches_upgrade_snapshot(payload, current.revision, expected)
        )
        if payload.get("status") != "active" or not valid:
            raise PluginHandsUpgradeConflict("Plugin Hands upgrade activation snapshot drifted")
        if _stage in {"preview", "begin"}:
            _require_staged_upgrade_candidate(uow.read, plugin_id, old, new)


def build_plugin_hands_upgrade_runtime(
    *,
    root_dir: Path,
    activation: PluginHandsActivationAuthority,
    registration_manager: PluginHandsRegistrationManager,
) -> PluginHandsUpgradeRuntime:
    """Compose durable cutover orchestration beside the live Registry manager."""

    root = root_dir.expanduser().resolve(strict=False)
    return PluginHandsUpgradeRuntime(
        records=SQLiteStructuredRecordStore(root / ".rebuild-data" / "jobs.sqlite3"),
        activation=activation,
        registration_manager=registration_manager,
        now=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    )


def backfill_plugin_hands_upgrade_effects(
    *, root_dir: Path, effects: EffectLog,
) -> tuple[str, ...]:
    records = SQLiteStructuredRecordStore(
        root_dir.expanduser().resolve(strict=False) / ".rebuild-data" / "jobs.sqlite3"
    )
    intents: list[EffectIntent] = []
    for record in records.list("plugin_hands_upgrade_cutovers"):
        try:
            decoded = decode_plugin_hands_upgrade_record(record)
        except (PluginHandsUpgradeConflict, ValueError):
            continue
        if decoded.get("stage") in {"completed", "rolled_back", "finalized"}:
            continue
        intents.append(_upgrade_intent(decoded))
    return backfill_interrupted_effects(
        effects, intents, now=int(datetime.now(timezone.utc).timestamp()),
        lease_owner="legacy-plugin-hands-upgrade",
    )


def register_plugin_hands_upgrade_handler(
    application, *, root_dir: Path, effect_runtime,
) -> None:
    """Register cutover execution without eagerly composing the AI Runtime."""

    def handle(effect) -> str:
        ai_runtime = getattr(application.state, "ai_runtime", None)
        manager = getattr(ai_runtime, "plugin_hands_registration_manager", None)
        if manager is None:
            raise RuntimeError("Plugin Hands registration manager is unavailable")
        runtime = build_plugin_hands_upgrade_runtime(
            root_dir=root_dir,
            activation=manager.activation_authority,
            registration_manager=manager,
        )
        current = runtime.authority.load(effect.operation_id)
        if current is None:
            raise EffectHandlerAbandoned("plugin_hands_upgrade.cutover_missing")
        try:
            result = runtime.authority.resume(effect.operation_id, ports=runtime.ports)
        except (PluginHandsUpgradeConflict, PluginHandsActivationConflict, ValueError) as error:
            raise EffectHandlerAbandoned(
                f"plugin_hands_upgrade.invalid:{type(error).__name__}"
            ) from error
        return (
            f"crp://plugin-hands-upgrade/{result['plugin_id']}/"
            f"{effect.operation_id}:r{result['revision']}"
        )

    effect_runtime.handlers.register(EffectHandlerRegistration(
        kind="plugin_hands_upgrade",
        effect_class=EffectClass.IDEMPOTENT,
        handler=handle,
    ))


def register_plugin_hands_cleanup_handler(
    *, root_dir: Path, effect_runtime,
) -> None:
    """Register the idempotent workspace compensation child Handler."""

    root = root_dir.expanduser().resolve(strict=False)
    records = SQLiteStructuredRecordStore(root / ".rebuild-data" / "jobs.sqlite3")
    managers = _workspace_manager_factory(
        root / ".rebuild-data" / "plugin-hands-workspaces"
    )

    effect_runtime.handlers.register(EffectHandlerRegistration(
        kind="plugin_hands_workspace_cleanup",
        effect_class=EffectClass.IDEMPOTENT,
        handler=lambda effect: execute_plugin_hands_workspace_cleanup(
            records, managers(), effect,
        ),
    ))


def register_plugin_hands_execution_handler(
    application,
    *,
    effect_runtime,
    tool_intents: SQLiteAITurnStore,
    runtime_factory: Callable[[], object],
) -> None:
    """Register the reconstructable primary Hands Handler in Core."""

    if not callable(runtime_factory):
        raise ValueError("Plugin Hands runtime factory is invalid")

    def handle(effect: Effect) -> str:
        intent = load_plugin_hands_tool_intent(tool_intents, effect)
        runtime = runtime_factory()
        manager = getattr(runtime, "plugin_hands_registration_manager", None)
        if not isinstance(manager, PluginHandsRegistrationManager):
            raise EffectHandlerAbandoned("plugin_hands.runtime_unavailable")
        provider = manager.resolve_provider(intent.capability_id)
        if provider is None:
            raise EffectHandlerAbandoned("plugin_hands.capability_unavailable")
        return provider.handle_claimed_effect(effect, intent)

    effect_runtime.handlers.register(EffectHandlerRegistration(
        kind="plugin_hands_execution",
        effect_class=EffectClass.AT_MOST_ONCE,
        handler=handle,
    ))


def dispatch_plugin_hands_upgrade_effects(
    *,
    root_dir: Path,
    runner: EffectRunner,
    activation: PluginHandsActivationAuthority,
    registration_manager: PluginHandsRegistrationManager,
) -> tuple[dict[str, object], ...]:
    """Core-selected dispatcher for interrupted Registry/activation cutovers."""

    return build_plugin_hands_upgrade_runtime(
        root_dir=root_dir, activation=activation, registration_manager=registration_manager,
    ).dispatch_effects(runner)


def _upgrade_intent(value: Mapping[str, object]) -> EffectIntent:
    old = value.get("old")
    new = value.get("new")
    if isinstance(old, PluginHandsUpgradeSnapshot):
        old_payload = asdict(old)
    elif isinstance(old, Mapping):
        old_payload = dict(old)
    else:
        raise PluginHandsUpgradeConflict("Plugin Hands upgrade old snapshot is invalid")
    if isinstance(new, PluginHandsUpgradeSnapshot):
        new_payload = asdict(new)
    elif isinstance(new, Mapping):
        new_payload = dict(new)
    else:
        raise PluginHandsUpgradeConflict("Plugin Hands upgrade new snapshot is invalid")
    cutover_id = str(value["cutover_id"])
    return EffectIntent(
        session_id=f"plugin-hands-upgrade:{value['plugin_id']}",
        root_id=f"{value['plugin_id']}:{value['hand_id']}",
        step_key="cutover-plugin-hand", kind="plugin_hands_upgrade",
        effect_class=EffectClass.IDEMPOTENT,
        intent_ref=f"crp://plugin-hands-upgrade/{value['plugin_id']}/{cutover_id}",
        gate_decision_id="plugin-hands-reviewed-cutover",
        rev_set={
            "old_activation_revision": old_payload["activation_revision"],
            "new_activation_revision": new_payload["activation_revision"],
            "old_runtime_revision": old_payload["runtime_revision"],
            "new_runtime_revision": new_payload["runtime_revision"],
            "old_resource_policy_revision": old_payload["resource_policy_revision"],
            "new_resource_policy_revision": new_payload["resource_policy_revision"],
        },
        payload={
            "cutover_id": cutover_id, "plugin_id": value["plugin_id"],
            "hand_id": value["hand_id"], "old": old_payload, "new": new_payload,
        },
        operation_id_override=cutover_id,
    )


def _require_upgrade_phase(
    snapshot: PluginHandsUpgradeSnapshot,
    phase: str,
    old: PluginHandsUpgradeSnapshot,
    new: PluginHandsUpgradeSnapshot,
    operation: str,
) -> None:
    expected = {
        "revoke": {"prepared": old, "rollback_prepared": new},
        "switch": {"old_revoked": new, "rollback_new_revoked": old},
        "register": {"new_switched": new, "rollback_old_switched": old},
    }.get(operation, {})
    if expected.get(phase) != snapshot:
        raise PluginHandsUpgradeConflict("Plugin Hands upgrade phase snapshot drifted")


def _payload_matches_upgrade_snapshot(
    payload: Mapping[str, object], revision: int, snapshot: PluginHandsUpgradeSnapshot,
) -> bool:
    return (
        payload.get("package_record_id") == snapshot.package_record_id
        and payload.get("review_revision") == snapshot.review_revision
        and payload.get("materialization_revision") == snapshot.materialization_revision
        and revision == snapshot.activation_revision
    )


def _activation_matches_upgrade_snapshot(
    activation: PluginHandsActivation, snapshot: PluginHandsUpgradeSnapshot,
) -> bool:
    return (
        activation.package_record_id == snapshot.package_record_id
        and activation.review_revision == snapshot.review_revision
        and activation.materialization_revision == snapshot.materialization_revision
        and activation.activation_revision == snapshot.activation_revision
    )


def _rollback_activation_matches_upgrade_receipt(
    activation: PluginHandsActivation,
    records: SQLiteStructuredRecordStore,
    plugin_id: str,
    hand_id: str,
    cutover_id: str,
    old: PluginHandsUpgradeSnapshot,
    new: PluginHandsUpgradeSnapshot,
) -> bool:
    return _rollback_payload_matches_upgrade_receipt(
        {
            "package_record_id": activation.package_record_id,
            "review_revision": activation.review_revision,
            "materialization_revision": activation.materialization_revision,
        },
        activation.activation_revision,
        lambda collection, object_id: records.read(collection, object_id),
        plugin_id,
        hand_id,
        old,
        new,
        cutover_id=cutover_id,
    )


def _rollback_payload_matches_upgrade_receipt(
    payload: Mapping[str, object],
    revision: int,
    read: Callable[[str, str], object],
    plugin_id: str,
    hand_id: str,
    old: PluginHandsUpgradeSnapshot,
    new: PluginHandsUpgradeSnapshot,
    *,
    cutover_id: str | None = None,
) -> bool:
    """Accept a rollback revision only when the activation receipt proves it.

    A rollback writes the old contract as a new activation record revision.
    The old snapshot revision therefore cannot remain equal.  The immutable
    ``rollback_new_revoked`` switch receipt is the only authority permitted to
    bridge that revision evolution.
    """

    if not _payload_contract_matches_upgrade_snapshot(payload, old):
        return False
    cutover = cutover_id
    if cutover is None:
        # The UoW caller already validated the active cutover record.  It has
        # no need to pass the id separately because the deterministic receipt
        # is recovered from the one active owner in this same transaction.
        active_id = "hands-upgrade-" + uuid5(NAMESPACE_URL, f"{plugin_id}:{hand_id}").hex
        owner = read("plugin_hands_upgrade_active", active_id)
        owner_payload = getattr(owner, "payload", None)
        cutover = owner_payload.get("cutover_id") if isinstance(owner_payload, Mapping) else None
    if not isinstance(cutover, str):
        return False
    receipt = read("plugin_hands_activation_upgrade_receipts", f"activation-{cutover}-rollback_new_revoked")
    receipt_payload = getattr(receipt, "payload", None)
    if not isinstance(receipt_payload, Mapping):
        return False
    required = {
        "schema_version", "plugin_id", "hand_id", "cutover_id", "phase",
        "old", "new", "prior_activation", "result",
    }
    if set(receipt_payload) != required or receipt_payload.get("schema_version") != "1.0.0":
        return False
    result, prior = receipt_payload.get("result"), receipt_payload.get("prior_activation")
    result_activation = result.get("activation") if isinstance(result, Mapping) else None
    return (
        receipt_payload.get("plugin_id") == plugin_id
        and receipt_payload.get("hand_id") == hand_id
        and receipt_payload.get("cutover_id") == cutover
        and receipt_payload.get("phase") == "rollback_new_revoked"
        and receipt_payload.get("old") == asdict(old)
        and receipt_payload.get("new") == asdict(new)
        and isinstance(prior, Mapping)
        and _payload_contract_matches_upgrade_snapshot(prior, new)
        and isinstance(result, Mapping)
        and result.get("activation_revision") == revision
        and isinstance(result_activation, Mapping)
        and _payload_contract_matches_upgrade_snapshot(result_activation, old)
        and _payload_contract_matches_upgrade_snapshot(payload, old)
    )


def _payload_contract_matches_upgrade_snapshot(
    payload: Mapping[str, object], snapshot: PluginHandsUpgradeSnapshot,
) -> bool:
    return (
        payload.get("package_record_id") == snapshot.package_record_id
        and payload.get("review_revision") == snapshot.review_revision
        and payload.get("materialization_revision") == snapshot.materialization_revision
    )


def _require_staged_upgrade_candidate(
    read: Callable[[str, str], object],
    plugin_id: str,
    old: PluginHandsUpgradeSnapshot,
    new: PluginHandsUpgradeSnapshot,
) -> None:
    """Require the immutable intake receipt while an upgrade acquires a Hand.

    Package intake intentionally retains its current pointer through the
    cutover.  This check is consequently limited to preview/begin; recovery
    validates the frozen activation pointer and its switch receipts instead.
    """

    if new.package_record_id == old.package_record_id:
        raise PluginHandsUpgradeConflict("Plugin Hands upgrade candidate must differ from current package")
    state = read("plugin_package_states", plugin_id)
    state_payload = getattr(state, "payload", None)
    state_revision = getattr(state, "revision", None)
    try:
        receipt_id = candidate_stage_receipt_object_id(plugin_id, new.package_record_id)
    except ValueError as exc:
        raise PluginHandsUpgradeConflict("Plugin Hands upgrade candidate identity is invalid") from exc
    receipt = read("plugin_upgrade_stage_receipts", receipt_id)
    receipt_payload = getattr(receipt, "payload", None)
    required = {"schema", "plugin", "old", "candidate", "state_revision", "command_id"}
    if (
        not isinstance(state_payload, Mapping)
        or not isinstance(receipt_payload, Mapping)
        or getattr(receipt, "object_id", None) != receipt_id
        or getattr(receipt, "revision", None) != 1
        or set(receipt_payload) != required
        or receipt_payload.get("schema") != "1.0.0"
        or receipt_payload.get("plugin") != plugin_id
        or receipt_payload.get("old") != old.package_record_id
        or receipt_payload.get("candidate") != new.package_record_id
        or receipt_payload.get("state_revision") != state_revision
        or state_payload.get("package_record_id") != old.package_record_id
        or not _valid_stage_command_id(receipt_payload.get("command_id"))
    ):
        raise PluginHandsUpgradeConflict("Plugin Hands upgrade candidate staging receipt drifted")


def _valid_stage_command_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and 8 <= len(value) <= 128
        and value[0].isalnum()
        and all(item.isalnum() or item in "._~-" for item in value)
    )


def _close_plugin_hands_provider(provider: object) -> None:
    close = getattr(provider, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


def shutdown_plugin_hands_runtime(application: object) -> None:
    """Revoke the application's ephemeral Hands Registry projection."""

    state = getattr(application, "state", None)
    manager = getattr(state, "plugin_hands_registration_manager", None)
    runtime = getattr(state, "ai_runtime", None)
    if manager is not None:
        manager.close()
        state.plugin_hands_registration_manager = None
        if getattr(runtime, "plugin_hands_registration_manager", None) is manager:
            runtime.plugin_hands_registration_manager = None
    hook_runner = getattr(runtime, "plugin_hook_runner", None)
    if hook_runner is not None:
        _close_plugin_hands_provider(hook_runner)
        runtime.plugin_hook_runner = None
        runtime.plugin_hook_projection_manager = None
    if hasattr(state, "plugin_hook_projection_manager"):
        state.plugin_hook_projection_manager = None


def plugin_hands_capability_id(plugin_id: str, hand_id: str) -> str:
    return f"plugin.hand.{plugin_id}.{hand_id}"


def plugin_hands_capability(active: PluginHandsActivation) -> CapabilityDefinition:
    capability_id = plugin_hands_capability_id(active.plugin_id, active.hand_id)
    side_effect = active.effect == "write"
    tool = ToolDefinition(
        tool_id=capability_id, version=1, display_name=f"{active.plugin_id} {active.hand_id}",
        description="Reviewed local Plugin Hand", source="plugin", owner_id=active.plugin_id,
        effect=active.effect, data_classes=("project_content",), destination="local",
        input_schema_uri=f"crp://schemas/plugin-hands/{active.plugin_id}/{active.hand_id}/input/r{active.review_revision}",
        output_schema_uri=f"crp://schemas/plugin-hands/{active.plugin_id}/{active.hand_id}/output/r{active.review_revision}",
        receipt_schema_uri="crp://schemas/plugin-hands/operation-receipt-v1" if side_effect else None,
        operation_semantics=active.operation_semantics,
        execution_mode="exclusive" if side_effect else "parallel",
        resource_locks=(f"plugin-hand:{active.plugin_id}:{active.hand_id}",) if side_effect else (),
        idempotency="never_retry" if side_effect else "idempotent",
        retry_policy=ToolRetryPolicy(1, 0, ()), verification_tool_id=None,
        compensation_tool_id=None, mutability="reversible" if side_effect else "read_only",
        egress_class="none", network_scope=(), data_egress_scope=(), timeout_ms=300_000,
        required_scopes=(), boundary_requirements=("project_plugin_enabled",),
    )
    return CapabilityDefinition(
        capability_id=capability_id, version=1, mode=active.effect,
        requires_approval=side_effect, operation_semantics=active.operation_semantics,
        input_schema_uri=tool.input_schema_uri, output_schema_uri=tool.output_schema_uri,
        tool_definition=tool,
    )


def _activation_fingerprint(active: PluginHandsActivation) -> tuple[object, ...]:
    return (
        active.plugin_id, active.hand_id, active.package_record_id, active.review_revision,
        active.materialization_revision, active.activation_revision,
        active.containment_profile_revision, active.artifact_opaque_ref,
    )


def build_plugin_hands_registration_manager(
    *, root_dir: Path, registry: CapabilityRegistryPort, frozen_authorization,
    runtime_executable: Path, effect_runner=None,
) -> PluginHandsRegistrationManager:
    """Compose the production manager from the existing durable authorities."""

    root = root_dir.expanduser().resolve(strict=False)
    records = SQLiteStructuredRecordStore(root / ".rebuild-data" / "jobs.sqlite3")
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    artifacts = build_plugin_hands_artifacts(root)
    artifacts.reconcile()
    activation = PluginHandsActivationAuthority(records, artifacts=artifacts, now=now)
    workspace_root = root / ".rebuild-data" / "plugin-hands-workspaces"
    workspace_manager_factory = _workspace_manager_factory(workspace_root)
    lease_authority = FrozenPluginHandsLeaseAuthority(
        frozen_authorization, recipe_revision=PLUGIN_HANDS_RECIPE_REVISION,
    )
    runtime_catalog = FixedPluginHandsPythonRuntimeCatalog(
        runtime_executable.resolve(strict=True), recipe_revision=PLUGIN_HANDS_RECIPE_REVISION,
    )
    admission = PluginHandsInProcessAdmission(PLUGIN_HANDS_MAX_CONCURRENT)

    def provider(active: PluginHandsActivation, capability_id: str) -> PluginHandsCapabilityProvider:
        if active.runtime != "powershell-stdio-v1":
            raise ValueError("Plugin Hands runtime recipe is not production-admitted")
        return PluginHandsCapabilityProvider(
            plugin_id=active.plugin_id, hand_id=active.hand_id, capability_id=capability_id,
            launch_recipe_revision=PLUGIN_HANDS_RECIPE_REVISION, activation=activation,
            artifacts=artifacts, lease_authority=lease_authority,
            runtime_catalog=runtime_catalog, lifecycle_records=records,
            workspace_manager_factory=workspace_manager_factory, contained_host=WindowsContainedPluginHandsHost(
                resource_policy_revision=PLUGIN_HANDS_RESOURCE_POLICY_REVISION,
                resource_limits=AppContainerResourceLimits(PLUGIN_HANDS_PROCESS_MEMORY_BYTES, PLUGIN_HANDS_CPU_RATE),
            ), admission=admission,
            containment_profile_exists=lambda revision: revision == PLUGIN_HANDS_CONTAINMENT_PROFILE,
            effect_runner=effect_runner,
        )

    return PluginHandsRegistrationManager(
        activation=activation, registry=registry, provider_factory=provider,
    )


class PluginHandsCapabilityProvider:
    """CapabilityProvider-compatible bridge, kept disconnected until its Gate."""

    def __init__(
        self,
        *,
        plugin_id: str,
        hand_id: str,
        capability_id: str,
        launch_recipe_revision: str,
        activation: PluginHandsActivationAuthority,
        artifacts: PluginHandsArtifactService,
        lease_authority: PluginHandsLeaseAuthority,
        runtime_catalog: PluginHandsRuntimeCatalog,
        lifecycle_records,
        contained_host: WindowsContainedPluginHandsHost,
        containment_profile_exists,
        workspace_manager: PluginHandsWorkspaceManager | None = None,
        workspace_manager_factory: Callable[[], PluginHandsWorkspaceManager] | None = None,
        admission: PluginHandsInProcessAdmission | None = None,
        effect_runner=None,
    ) -> None:
        self._plugin_id = plugin_id
        self._hand_id = hand_id
        self._capability_id = capability_id
        self._recipe = launch_recipe_revision
        self._activation = activation
        self._artifacts = artifacts
        self._lease_authority = lease_authority
        self._runtime_catalog = runtime_catalog
        if workspace_manager_factory is None:
            if not isinstance(workspace_manager, PluginHandsWorkspaceManager):
                raise ValueError("Plugin Hands workspace manager is invalid")
            workspace_manager_factory = lambda: workspace_manager
        if not callable(workspace_manager_factory):
            raise ValueError("Plugin Hands workspace manager factory is invalid")
        self._workspace_manager_factory = workspace_manager_factory
        self._host = contained_host
        self._profile_exists = containment_profile_exists
        self._admission = admission
        self._lifecycle = PluginHandsDurableLifecycle(
            lifecycle_records, self, effect_runner=effect_runner,
        )

    def close(self) -> None:
        """Stop any contained child owned by this ephemeral provider."""

        self._host.close()

    def invoke(self, request: Mapping[str, object]) -> Mapping[str, object]:
        if not isinstance(request, Mapping):
            raise ToolProviderFailure("plugin_hands_request_invalid", effect_certainty="confirmed_none")
        try:
            active = _pre_lifecycle_stage("plugin_hands_activation_unavailable", self._active)
            _pre_lifecycle_stage(
                "plugin_hands_frozen_tool_contract_invalid",
                lambda: _validate_frozen_tool_contract(request, active, self._capability_id),
            )
            artifact = _pre_lifecycle_stage(
                "plugin_hands_artifact_unavailable",
                lambda: self._resolve_artifact(active),
            )
            _pre_lifecycle_stage(
                "plugin_hands_artifact_binding_invalid",
                lambda: self._validate_artifact_binding(artifact, active),
            )
            arguments = _pre_lifecycle_stage(
                "plugin_hands_input_schema_invalid",
                lambda: _validated_input_arguments(active, request),
            )
            intent_ref, capability_id = _pre_lifecycle_stage(
                "plugin_hands_intent_identity_invalid",
                lambda: _intent_identity(request, self._capability_id),
            )
            lease = _pre_lifecycle_stage(
                "plugin_hands_frozen_lease_unavailable",
                lambda: self._lease_authority.resolve(request, active),
            )
            _pre_lifecycle_stage(
                "plugin_hands_lease_binding_invalid",
                lambda: _validate_lease_request(lease, request, active),
            )
            binding = PluginHandsLifecycleBinding(
                intent_ref, capability_id, active.artifact_opaque_ref,
                active.plugin_id, active.hand_id, active.package_record_id,
                active.review_revision, active.materialization_revision,
                active.activation_revision, active.containment_profile_revision,
                self._recipe,
            )
            invocation, context = _pre_lifecycle_stage(
                "plugin_hands_execution_context_invalid",
                lambda: self._invocation_context(request, active, binding, lease, arguments),
            )
            admission_lease = self._admission.try_acquire() if self._admission is not None else None
            if self._admission is not None and admission_lease is None:
                raise ToolProviderFailure("plugin_hands_capacity_exhausted", effect_certainty="confirmed_none")
            try:
                execution = self._lifecycle.execute(
                    self._host, self._workspace_manager_factory(), binding, invocation,
                    PluginHandsControl(is_cancelled=lambda: context.cancel_requested),
                )
            finally:
                if admission_lease is not None:
                    admission_lease.close()
        except ToolProviderFailure:
            raise
        except Exception as error:
            certainty = self._certainty_after_failure(request)
            raise ToolProviderFailure(
                "plugin_hands_authority_unavailable", effect_certainty=certainty,
            ) from error

        outcome = execution.outcome
        if outcome.status == "unknown":
            raise ToolProviderFailure("plugin_hands_unknown_effect", effect_certainty="unknown")
        if outcome.status != "success" or outcome.output is None:
            raise ToolProviderFailure(
                outcome.error_code or "plugin_hands_failed", effect_certainty="confirmed_none",
            )
        try:
            _validate_schema(active.output_schema, outcome.output, "output")
        except ValueError as error:
            # The child ran and returned an invalid terminal payload.  Its
            # external effect cannot be inferred from schema failure.
            raise ToolProviderFailure("plugin_hands_output_invalid", effect_certainty="unknown") from error
        receipt = {
            "schema_version": "1.0.0",
            "operation": "plugin_hand",
            "status": "completed",
            "invocation_id": lease.invocation_id,
            "turn_id": lease.turn_id,
            "capability_id": capability_id,
            "plugin_id": active.plugin_id,
            "hand_id": active.hand_id,
            "artifact_ref": active.artifact_opaque_ref,
            "activation_revision": active.activation_revision,
            "effect": active.effect,
            "operation_semantics": active.operation_semantics,
            "resource_policy_revision": lease.resource_policy_revision,
        }
        return {
            "summary": "Plugin Hand completed",
            "result": dict(outcome.output),
            "operation_receipt": receipt,
            "evidence_refs": (active.artifact_opaque_ref,),
        }

    def handle_claimed_effect(
        self, effect: Effect, intent: ToolInvocationIntent,
    ) -> str:
        """Reconstruct and execute a Core-claimed PLANNED restart attempt."""

        if intent.invocation_id != effect.root_id or intent.capability_id != self._capability_id:
            raise PluginHandsDurableLifecycleError("Plugin Hands Handler intent drifted")
        active = self._active()
        request = {
            "turn_id": intent.turn_id,
            "tool_call_id": intent.invocation_id,
            "capability_id": intent.capability_id,
            "capability_version": intent.capability_version,
            "tool_contract": dict(intent.tool_contract or {}),
            "arguments": dict(intent.arguments),
            "intent_ref": effect.intent_ref,
            "authorization_facts_ref": intent.authorization_facts_ref,
            "authorization_facts_revision": intent.authorization_facts_revision,
            "approval_fact_ref": intent.approval_fact_ref,
            "timeout_ms": intent.timeout_ms,
            "scope": {"kind": "project", "project_id": effect.session_id},
        }
        _validate_frozen_tool_contract(request, active, self._capability_id)
        artifact = self._resolve_artifact(active)
        self._validate_artifact_binding(artifact, active)
        arguments = _validated_input_arguments(active, request)
        record = self._lifecycle.load(effect.root_id)
        if record is None or record.state != "prepared":
            raise PluginHandsDurableLifecycleError(
                "Plugin Hands Handler lifecycle fact is unavailable"
            )
        lease = PluginHandsLease(
            record.lease_id, record.invocation_id, record.generation,
            record.project_id, record.turn_id, record.boundary_revision,
            record.recipe_revision, record.allowed_resources, record.expires_at,
            record.resource_policy_revision,
        )
        self._lease_authority.validate_recovery(request, active, lease)
        _validate_lease_request(lease, request, active)
        binding = PluginHandsLifecycleBinding(
            effect.intent_ref, intent.capability_id, active.artifact_opaque_ref,
            active.plugin_id, active.hand_id, active.package_record_id,
            active.review_revision, active.materialization_revision,
            active.activation_revision, active.containment_profile_revision,
            self._recipe,
        )
        launch = self.resolve(binding, _scope(lease))
        invocation = PluginHandsInvocation(
            lease.invocation_id, active.plugin_id, launch, lease,
            min(intent.timeout_ms, 300_000), arguments,
        )
        admission_lease = self._admission.try_acquire() if self._admission is not None else None
        if self._admission is not None and admission_lease is None:
            raise PluginHandsDurableLifecycleError("Plugin Hands capacity is unavailable")
        try:
            return self._lifecycle.handle_claimed(
                effect, self._host, self._workspace_manager_factory(),
                binding, invocation, PluginHandsControl(),
            )
        finally:
            if admission_lease is not None:
                admission_lease.close()

    def _invocation_context(
        self,
        request: Mapping[str, object],
        active: PluginHandsActivation,
        binding: PluginHandsLifecycleBinding,
        lease: PluginHandsLease,
        arguments: Mapping[str, object],
    ) -> tuple[PluginHandsInvocation, ToolExecutionContext]:
        """Build all inputs that must exist before an attempt may be recorded."""

        scope = _scope(lease)
        launch = self.resolve(binding, scope)
        invocation = PluginHandsInvocation(
            lease.invocation_id, active.plugin_id, launch, lease,
            min(_positive_int(request.get("timeout_ms"), "timeout_ms"), 300_000),
            arguments,
        )
        context = request.get("execution_context")
        if not isinstance(context, ToolExecutionContext):
            raise ValueError("Plugin Hands execution context is unavailable")
        return invocation, context

    # PluginHandsExecutionAuthority implementation.  Every call re-resolves
    # activation, artifact, profile and runtime recipe.
    def resolve(self, binding: PluginHandsLifecycleBinding, scope: PluginHandsExecutionScope) -> PluginHandsLaunch:
        active = self._active()
        artifact = self._artifact(active)
        _match_binding(binding, active, scope, self._capability_id, self._recipe)
        if not bool(self._profile_exists(active.containment_profile_revision)):
            raise ValueError("Plugin Hands containment profile is unavailable")
        executable = self._runtime_catalog.resolve(runtime=artifact.runtime, recipe_revision=self._recipe)
        relative_entrypoint = f"code/{artifact.entrypoint}"
        if artifact.runtime == "python-stdio-v1":
            argv = ("-I", "-B", "-u", relative_entrypoint)
            environment = {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
        elif artifact.runtime == "powershell-stdio-v1":
            entrypoint = ".\\" + relative_entrypoint.replace("/", "\\")
            argv = ("-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", entrypoint)
            environment = {}
        else:
            raise ValueError("Plugin Hands runtime recipe is unavailable")
        return PluginHandsLaunch(scope.invocation_id, executable, argv, environment)

    def prepare_workspace(self, binding: PluginHandsLifecycleBinding, scope: PluginHandsExecutionScope, workspace: PluginHandsWorkspace) -> None:
        active = self._active()
        artifact = self._artifact(active)
        _match_binding(binding, active, scope, self._capability_id, self._recipe)
        require_exact_workspace_resources(scope.allowed_resources, artifact.requested_resources)
        self._workspace_manager_factory().stage_code(workspace, artifact.payload_files)

    def verify_workspace(self, binding: PluginHandsLifecycleBinding, scope: PluginHandsExecutionScope, workspace: PluginHandsWorkspace) -> None:
        active = self._active()
        artifact = self._artifact(active)
        _match_binding(binding, active, scope, self._capability_id, self._recipe)
        require_exact_workspace_resources(scope.allowed_resources, artifact.requested_resources)
        self._workspace_manager_factory().verify_code(workspace, artifact.payload_files)

    def validate_outcome(self, binding: PluginHandsLifecycleBinding, scope: PluginHandsExecutionScope, outcome: PluginHandsOutcome) -> None:
        active = self._active()
        self._artifact(active)
        _match_binding(binding, active, scope, self._capability_id, self._recipe)
        if outcome.status == "success":
            if outcome.output is None:
                raise ValueError("Plugin Hands successful output is unavailable")
            _validate_schema(active.output_schema, outcome.output, "output")

    def _active(self) -> PluginHandsActivation:
        active = self._activation.resolve_active(self._plugin_id, hand_id=self._hand_id)
        if active is None:
            raise ValueError("Plugin Hand is disabled")
        return active

    def _artifact(self, active: PluginHandsActivation) -> ManagedHandsArtifact:
        artifact = self._resolve_artifact(active)
        self._validate_artifact_binding(artifact, active)
        return artifact

    def _resolve_artifact(self, active: PluginHandsActivation) -> ManagedHandsArtifact:
        return self._artifacts.resolve(active.plugin_id, hand_id=active.hand_id)

    @staticmethod
    def _validate_artifact_binding(artifact: ManagedHandsArtifact, active: PluginHandsActivation) -> None:
        if (
            artifact.opaque_ref != active.artifact_opaque_ref
            or artifact.package_record_id != active.package_record_id
            or artifact.review_revision != active.review_revision
            or artifact.materialization_revision != active.materialization_revision
            or artifact.containment_profile_revision != active.containment_profile_revision
            or not _schema_structurally_equal(artifact.input_schema, active.input_schema)
            or not _schema_structurally_equal(artifact.output_schema, active.output_schema)
            or artifact.effect != active.effect
            or artifact.operation_semantics != active.operation_semantics
            or artifact.requested_resources != active.requested_resources
        ):
            raise ValueError("Plugin Hands artifact binding drifted")

    def _certainty_after_failure(self, request: Mapping[str, object]) -> str:
        invocation_id = request.get("tool_call_id")
        if not isinstance(invocation_id, str):
            return "confirmed_none"
        record = self._lifecycle.load(invocation_id)
        if record is not None and record.state in {"fenced", "unknown"}:
            return "unknown"
        if record is not None and record.state in {"cleanup_pending", "cleaned"}:
            if record.outcome_status != "success":
                return "confirmed_none"
            try:
                active = self._active()
            except Exception:
                return "unknown"
            return "confirmed_none" if active.effect == "read" else "confirmed_applied"
        return "confirmed_none"


def _match_binding(binding: PluginHandsLifecycleBinding, active: PluginHandsActivation, scope: PluginHandsExecutionScope, capability_id: str, recipe: str) -> None:
    expected = (
        capability_id, active.artifact_opaque_ref, active.plugin_id, active.hand_id,
        active.package_record_id, active.review_revision, active.materialization_revision,
        active.containment_profile_revision, active.activation_revision, recipe,
    )
    actual = (
        binding.capability_id, binding.artifact_opaque_ref, binding.plugin_id, binding.hand_id,
        binding.package_record_id, binding.review_revision, binding.materialization_revision,
        binding.containment_profile_revision, binding.activation_revision,
        binding.launch_recipe_revision,
    )
    if actual != expected or scope.recipe_revision != recipe:
        raise ValueError("Plugin Hands execution binding drifted")


def _validate_lease_request(lease: PluginHandsLease, request: Mapping[str, object], active: PluginHandsActivation) -> None:
    if not isinstance(lease, PluginHandsLease):
        raise ValueError("Plugin Hands lease authority returned an invalid lease")
    if lease.invocation_id != request.get("tool_call_id") or lease.turn_id != request.get("turn_id"):
        raise ValueError("Plugin Hands lease identity drifted")
    require_exact_workspace_resources(lease.allowed_resources, active.requested_resources)


def _validate_frozen_tool_contract(request: Mapping[str, object], active: PluginHandsActivation, capability_id: str) -> None:
    contract = request.get("tool_contract")
    version = request.get("capability_version")
    if not isinstance(contract, Mapping) or not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise ValueError("Plugin Hands frozen Tool contract is unavailable")
    retry = contract.get("retry_policy")
    receipt_schema = contract.get("receipt_schema_uri")
    expected_idempotency = "idempotent" if active.effect == "read" else "never_retry"
    if (
        contract.get("tool_id") != capability_id
        or contract.get("version") != version
        or contract.get("effect") != active.effect
        or contract.get("source") != "plugin"
        or contract.get("owner_id") != active.plugin_id
        or contract.get("destination") != "local"
        or contract.get("operation_semantics") != active.operation_semantics
        or contract.get("egress_class") != "none"
        or contract.get("network_scope") != []
        or contract.get("idempotency") != expected_idempotency
        or not isinstance(retry, Mapping)
        or retry.get("max_attempts") != 1
        or retry.get("retryable_error_codes") != []
        or (active.operation_semantics == "receipt_required" and not isinstance(receipt_schema, str))
        or (active.operation_semantics == "read_only" and receipt_schema is not None)
    ):
        raise ValueError("Plugin Hands frozen Tool contract drifted")


def _schema_structurally_equal(left: object, right: object) -> bool:
    """Compare JSON-schema values across mutable JSON and frozen durable forms.

    Durable artifacts freeze JSON arrays as tuples and mappings as mapping
    proxies.  Only those two JSON representation differences are equivalent;
    sets and every non-JSON type remain rejected by strict type/value checks.
    """

    if isinstance(left, Mapping) or isinstance(right, Mapping):
        if not isinstance(left, Mapping) or not isinstance(right, Mapping) or len(left) != len(right):
            return False
        return all(key in right and _schema_structurally_equal(value, right[key]) for key, value in left.items())
    if isinstance(left, (list, tuple)) or isinstance(right, (list, tuple)):
        if not isinstance(left, (list, tuple)) or not isinstance(right, (list, tuple)) or len(left) != len(right):
            return False
        return all(_schema_structurally_equal(item, other) for item, other in zip(left, right, strict=True))
    return type(left) is type(right) and left == right


def _validated_input_arguments(
    active: PluginHandsActivation, request: Mapping[str, object],
) -> Mapping[str, object]:
    arguments = _mapping(request.get("arguments"), "arguments")
    _validate_schema(active.input_schema, arguments, "input")
    return arguments


def _intent_identity(
    request: Mapping[str, object], capability_id: str,
) -> tuple[str, str]:
    intent_ref = _text(request.get("intent_ref"), "intent_ref")
    actual_capability_id = _text(request.get("capability_id"), "capability_id")
    if actual_capability_id != capability_id:
        raise ValueError("Plugin Hands capability identity drifted")
    return intent_ref, actual_capability_id


def _pre_lifecycle_stage(error_code: str, operation: Callable[[], object]):
    """Fail closed before durable lifecycle ownership begins.

    These categories intentionally disclose only a stable public code. The
    underlying authority exception may include local paths or durable IDs, so
    it must not cross the Tool provider boundary.
    """

    try:
        return operation()
    except ToolProviderFailure:
        raise
    except Exception:
        raise ToolProviderFailure(error_code, effect_certainty="confirmed_none") from None


def _validate_schema(schema: Mapping[str, object], value: Mapping[str, object], label: str) -> None:
    try:
        Draft202012Validator.check_schema(dict(schema))
        errors = tuple(Draft202012Validator(dict(schema)).iter_errors(dict(value)))
    except SchemaError as error:
        raise ValueError(f"Plugin Hands {label} schema is invalid") from error
    if errors:
        raise ValueError(f"Plugin Hands {label} does not match the reviewed schema")


def _scope(lease: PluginHandsLease) -> PluginHandsExecutionScope:
    return PluginHandsExecutionScope(
        lease.invocation_id, lease.lease_id, lease.generation, lease.project_id,
        lease.turn_id, lease.boundary_revision, lease.recipe_revision,
        lease.allowed_resources, lease.resource_policy_revision, lease.expires_at,
    )


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"Plugin Hands {label} is invalid")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 320 or "\x00" in value:
        raise ValueError(f"Plugin Hands {label} is invalid")
    return value


def _positive_int(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"Plugin Hands {label} is invalid")
    return value
