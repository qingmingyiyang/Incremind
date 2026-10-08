"""Contained Plugin Hands adapter for the existing Codex Hook data plane."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from threading import RLock
from uuid import NAMESPACE_URL, uuid4, uuid5

from core.ai_kernel import HookHandlerManifest, HookPolicySnapshot
from core.ai_kernel.codex_hook_parity import HookEvent, HookRun
from core.plugin_hands import (
    PluginHandsInvocation,
    PluginHandsLaunch,
    PluginHandsLease,
    PluginHandsWorkspaceManager,
    WindowsContainedPluginHandsHost,
)
from core.plugin_hands.windows_appcontainer import AppContainerResourceLimits
from core.plugin_host.hands_activation import PluginHandsActivationAuthority
from core.plugin_host.hands_artifact import PluginHandsArtifactService
from core.plugin_host.hook_activation import (
    PluginHookActivation,
    PluginHookActivationAuthority,
    PluginHookActivationConflict,
)
from core.storage_provider import SQLiteStructuredRecordStore

from .plugin_hands_runtime import (
    PLUGIN_HANDS_CONTAINMENT_PROFILE,
    PLUGIN_HANDS_CPU_RATE,
    PLUGIN_HANDS_PROCESS_MEMORY_BYTES,
    PLUGIN_HANDS_RECIPE_REVISION,
    PLUGIN_HANDS_RESOURCE_POLICY_REVISION,
    FixedPluginHandsPythonRuntimeCatalog,
    build_plugin_hands_artifacts,
)


_MAX_HOOK_PAYLOAD_BYTES = 64 * 1024
_MAX_HOOK_TEXT_BYTES = 64 * 1024


class PluginHookRuntimeError(ValueError):
    pass


def plugin_hook_handler_manifest(binding: PluginHookActivation) -> HookHandlerManifest:
    return HookHandlerManifest(
        hook_id=f"plugin.{binding.plugin_id}.{binding.hook_id}",
        revision=binding.handler_revision,
        event=HookEvent(binding.event),
        config_order=binding.order,
        synchronous=binding.synchronous,
        handler_ref=f"crp://plugin-hands/{binding.plugin_id}/{binding.hook_id}",
        timeout_ms=binding.timeout_ms,
    )


class PluginHandsHookRunner:
    """Revision-pinned runner that never creates a Tool or Boundary decision."""

    def __init__(
        self,
        *,
        hooks: PluginHookActivationAuthority,
        hands: PluginHandsActivationAuthority,
        artifacts: PluginHandsArtifactService,
        frozen_authorization,
        runtime_catalog: FixedPluginHandsPythonRuntimeCatalog,
        workspace_manager: PluginHandsWorkspaceManager | None = None,
        workspace_manager_factory=None,
        contained_host: WindowsContainedPluginHandsHost,
        now=None,
    ) -> None:
        self._hooks = hooks
        self._hands = hands
        self._artifacts = artifacts
        self._frozen = frozen_authorization
        self._runtime = runtime_catalog
        if workspace_manager_factory is None:
            if not isinstance(workspace_manager, PluginHandsWorkspaceManager):
                raise PluginHookRuntimeError("Plugin Hook workspace manager is invalid")
            workspace_manager_factory = lambda: workspace_manager
        if not callable(workspace_manager_factory):
            raise PluginHookRuntimeError("Plugin Hook workspace manager is invalid")
        self._workspace_manager_factory = workspace_manager_factory
        self._host = contained_host
        self._active_lock = RLock()
        self._active_invocations: set[str] = set()
        self._now = now or (lambda: datetime.now(timezone.utc))

    def close(self) -> None:
        self._host.close()

    def manifests(self) -> tuple[HookHandlerManifest, ...]:
        return tuple(plugin_hook_handler_manifest(item) for item in self._hooks.all_active())

    def supports(self, manifest: HookHandlerManifest) -> bool:
        try:
            binding = self._binding(manifest)
        except PluginHookRuntimeError:
            return False
        return plugin_hook_handler_manifest(binding) == manifest

    def __call__(self, manifest: HookHandlerManifest, payload: Mapping[str, object]) -> HookRun:
        binding = self._binding(manifest)
        if plugin_hook_handler_manifest(binding) != manifest:
            raise PluginHookRuntimeError("Plugin Hook frozen binding drifted")
        projected = _project_payload(manifest.event, payload)
        turn_id = projected["payload"].get("turn_id")
        if not isinstance(turn_id, str):
            raise PluginHookRuntimeError("Plugin Hook Turn identity is unavailable")
        handle = self._frozen.current_handle(turn_id=turn_id)
        facts = handle.facts
        hand = self._hands.resolve_active(binding.plugin_id, hand_id=binding.hand_id)
        if hand is None or hand.activation_revision != binding.hand_activation_revision:
            raise PluginHookRuntimeError("Plugin Hook Hand activation drifted")
        if hand.containment_profile_revision != PLUGIN_HANDS_CONTAINMENT_PROFILE:
            raise PluginHookRuntimeError("Plugin Hook containment profile is unavailable")
        artifact = self._artifacts.resolve(binding.plugin_id, hand_id=binding.hand_id)
        if artifact.package_record_id != binding.package_record_id or artifact.opaque_ref != hand.artifact_opaque_ref:
            raise PluginHookRuntimeError("Plugin Hook artifact binding drifted")
        if artifact.effect != "read" or artifact.operation_semantics != "read_only" or artifact.requested_resources:
            raise PluginHookRuntimeError("Plugin Hook Hand contract drifted")
        if artifact.runtime != "powershell-stdio-v1":
            raise PluginHookRuntimeError("Plugin Hook runtime recipe is not production-admitted")
        identity = _invocation_identity(binding, projected)
        invocation_id = f"hookinv-{identity}"
        # The invocation identity is stable for in-process single-flight, but
        # the containment lease is deliberately attempt-scoped. A quarantined
        # UNKNOWN workspace from an earlier attempt must not become durable
        # execution authority or prevent a later read-only Gate evaluation.
        lease_id = f"hooklease-{uuid4().hex}"
        expires = self._now().astimezone(timezone.utc) + timedelta(milliseconds=binding.timeout_ms)
        lease = PluginHandsLease(
            lease_id, invocation_id, binding.activation_revision, facts.project_id, turn_id,
            facts.boundary_profile_revision, PLUGIN_HANDS_RECIPE_REVISION, (),
            expires.isoformat().replace("+00:00", "Z"), PLUGIN_HANDS_RESOURCE_POLICY_REVISION,
        )
        executable = self._runtime.resolve(runtime=artifact.runtime, recipe_revision=PLUGIN_HANDS_RECIPE_REVISION)
        relative_entrypoint = f"code/{artifact.entrypoint}"
        if artifact.runtime == "python-stdio-v1":
            argv = ("-I", "-B", "-u", relative_entrypoint)
            environment = {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
        elif artifact.runtime == "powershell-stdio-v1":
            argv = ("-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", ".\\" + relative_entrypoint.replace("/", "\\"))
            environment = {}
        else:
            raise PluginHookRuntimeError("Plugin Hook runtime recipe is unavailable")
        launch = PluginHandsLaunch(invocation_id, executable, argv, environment)
        invocation = PluginHandsInvocation(
            invocation_id, binding.plugin_id, launch, lease, binding.timeout_ms, projected,
        )
        with self._active_lock:
            if invocation_id in self._active_invocations:
                raise PluginHookRuntimeError("Plugin Hook invocation is already running")
            self._active_invocations.add(invocation_id)
        workspaces = self._workspace_manager_factory()
        workspace = None
        process_started = False
        try:
            workspace = workspaces.create(lease)
            workspaces.stage_code(workspace, artifact.payload_files)
            workspaces.verify_code(workspace, artifact.payload_files)
            process_started = True
            outcome = self._host.execute_prepared(workspace, launch, invocation)
            run = _hook_run(binding, outcome.output) if outcome.status == "success" and outcome.output is not None else None
            try:
                retained = workspaces.dispose(workspace, outcome)
            except Exception:
                # Cleanup is local containment hygiene, never Hook execution
                # truth. A cleanup failure cannot replace a completed Gate
                # evaluation or create a domain recovery state machine.
                try:
                    workspaces.cleanup_known_identity(lease_id, invocation_id)
                except Exception:
                    pass
                retained = None
            # UNKNOWN remains quarantined. It is not replayed, interpreted as
            # a Gate fact, or scanned by a domain recovery scheduler.
            del retained
        except Exception:
            if workspace is not None and not process_started:
                try:
                    workspaces.cleanup_known_identity(lease_id, invocation_id)
                except Exception:
                    pass
            raise
        finally:
            with self._active_lock:
                self._active_invocations.discard(invocation_id)
        if outcome.status != "success" or outcome.output is None or run is None:
            raise PluginHookRuntimeError("Plugin Hook contained execution failed")
        return run

    def _binding(self, manifest: HookHandlerManifest) -> PluginHookActivation:
        prefix = "crp://plugin-hands/"
        ref = manifest.handler_ref or ""
        if not ref.startswith(prefix):
            raise PluginHookRuntimeError("Plugin Hook handler reference is invalid")
        parts = ref[len(prefix):].split("/")
        if len(parts) != 2 or not all(parts):
            raise PluginHookRuntimeError("Plugin Hook handler reference is invalid")
        try:
            binding = self._hooks.resolve_active(parts[0], hook_id=parts[1])
        except PluginHookActivationConflict as exc:
            raise PluginHookRuntimeError("Plugin Hook activation is unavailable") from exc
        if binding is None:
            raise PluginHookRuntimeError("Plugin Hook activation is unavailable")
        return binding


class PluginHookProjectionManager:
    """Atomically project durable Hook activations into the existing catalog."""

    def __init__(self, host, runner: PluginHandsHookRunner, *, fault_probe=None) -> None:
        self._host = host
        self._runner = runner
        self._fault_probe = fault_probe or (lambda: None)
        self._base = tuple(
            item for item in host.current_snapshot().handlers
            if not (item.handler_ref or "").startswith("crp://plugin-hands/")
        )

    def reconcile(self) -> HookPolicySnapshot:
        self._fault_probe()
        current = self._host.current_snapshot()
        plugins = self._runner.manifests()
        identities = {(item.event, item.hook_id) for item in self._base}
        if any((item.event, item.hook_id) in identities for item in plugins):
            raise PluginHookRuntimeError("Plugin Hook handler identity conflicts")
        material = "|".join(
            f"{item.event.value}:{item.hook_id}:{item.revision}" for item in plugins
        ) or "empty"
        suffix = uuid5(NAMESPACE_URL, material).hex
        snapshot = HookPolicySnapshot(
            revision=f"plugin-hooks.{suffix}", handlers=self._base + plugins,
            codex_revision=current.codex_revision,
            manifest_ref=current.manifest_ref,
            manifest_revision=f"plugin-hooks.{suffix}",
            local_hard_guard_revision=current.local_hard_guard_revision,
        )
        installed = self._host.install_snapshot(snapshot)
        self._host.set_handler_prefix_enabled("crp://plugin-hands/", enabled=True)
        return installed

    def fail_closed(self) -> HookPolicySnapshot:
        """Remove every third-party Hook when durable projection cannot reconcile."""

        self._host.set_handler_prefix_enabled("crp://plugin-hands/", enabled=False)
        current = self._host.current_snapshot()
        suffix = uuid5(NAMESPACE_URL, "plugin-hooks:fail-closed").hex
        return self._host.install_snapshot(HookPolicySnapshot(
            revision=f"plugin-hooks.{suffix}", handlers=self._base,
            codex_revision=current.codex_revision,
            manifest_ref=current.manifest_ref,
            manifest_revision=f"plugin-hooks.{suffix}",
            local_hard_guard_revision=current.local_hard_guard_revision,
        ))


def build_plugin_hook_runner(
    *, root_dir: Path, frozen_authorization, runtime_executable: Path,
) -> PluginHandsHookRunner:
    root = root_dir.expanduser().resolve(strict=False)
    records = SQLiteStructuredRecordStore(root / ".rebuild-data" / "jobs.sqlite3")
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    artifacts = build_plugin_hands_artifacts(root)
    artifacts.reconcile()
    hands = PluginHandsActivationAuthority(records, artifacts=artifacts, now=now)
    hooks = PluginHookActivationAuthority(records, hands=hands, now=now)
    workspace_root = root / ".rebuild-data" / "plugin-hook-workspaces"
    workspace_lock = RLock()
    workspace_manager: PluginHandsWorkspaceManager | None = None

    def workspaces() -> PluginHandsWorkspaceManager:
        nonlocal workspace_manager
        with workspace_lock:
            if workspace_manager is None:
                workspace_root.mkdir(parents=True, exist_ok=True)
                workspace_manager = PluginHandsWorkspaceManager(workspace_root)
            return workspace_manager
    return PluginHandsHookRunner(
        hooks=hooks, hands=hands, artifacts=artifacts, frozen_authorization=frozen_authorization,
        runtime_catalog=FixedPluginHandsPythonRuntimeCatalog(
            runtime_executable.resolve(strict=True), recipe_revision=PLUGIN_HANDS_RECIPE_REVISION,
        ),
        workspace_manager_factory=workspaces,
        contained_host=WindowsContainedPluginHandsHost(
            resource_policy_revision=PLUGIN_HANDS_RESOURCE_POLICY_REVISION,
            resource_limits=AppContainerResourceLimits(
                PLUGIN_HANDS_PROCESS_MEMORY_BYTES, PLUGIN_HANDS_CPU_RATE,
            ),
        ),
    )


def build_plugin_hook_activation(root_dir: Path) -> PluginHookActivationAuthority:
    root = root_dir.expanduser().resolve(strict=False)
    records = SQLiteStructuredRecordStore(root / ".rebuild-data" / "jobs.sqlite3")
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    artifacts = build_plugin_hands_artifacts(root)
    hands = PluginHandsActivationAuthority(records, artifacts=artifacts, now=now)
    return PluginHookActivationAuthority(records, hands=hands, now=now)


def plugin_hook_projection_fault_probe(root_dir: Path):
    """Build a fail-only E2E probe bound to the private desktop nonce."""

    root = root_dir.expanduser().resolve(strict=False)
    marker = root / ".rebuild-data" / "e2e-plugin-hook-projection-fault"

    def probe() -> None:
        fixture_nonce = os.environ.get("CHRIPTMAS_E2E_PLUGIN_HOOK_FAULT_NONCE", "")
        desktop_nonce = os.environ.get("CHRIPTMAS_DESKTOP_NONCE", "")
        if not fixture_nonce or fixture_nonce != desktop_nonce:
            return
        try:
            status = marker.lstat()
        except FileNotFoundError:
            return
        if not marker.is_file() or marker.is_symlink() or status.st_size > 32:
            raise PluginHookRuntimeError("Plugin Hook projection fault marker is invalid")
        raise PluginHookRuntimeError("Plugin Hook projection fault injected")

    return probe


def _project_payload(event: HookEvent, payload: Mapping[str, object]) -> dict[str, object]:
    if event is not HookEvent.PRE_TOOL_USE:
        raise PluginHookRuntimeError("third-party Plugin Hooks currently admit PreToolUse only")
    required = ("turn_id", "step_id", "tool_call_id", "tool_name")
    if any(not isinstance(payload.get(field), str) or not payload.get(field) for field in required):
        raise PluginHookRuntimeError("Plugin Hook invocation identity is unavailable")
    # Third-party policy code receives identifiers only. Raw tool arguments may
    # contain secrets and remain inside the Kernel authorization path.
    safe = {field: payload[field] for field in required}
    try:
        encoded = json.dumps(safe, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise PluginHookRuntimeError("Plugin Hook payload is not serializable") from exc
    if len(encoded) > _MAX_HOOK_PAYLOAD_BYTES:
        raise PluginHookRuntimeError("Plugin Hook payload exceeds the size limit")
    return {"hook_event": event.value, "payload": json.loads(encoded.decode("utf-8"))}


def _invocation_identity(binding: PluginHookActivation, projected: Mapping[str, object]) -> str:
    payload = projected.get("payload")
    if not isinstance(payload, Mapping):
        raise PluginHookRuntimeError("Plugin Hook payload is invalid")
    material = "|".join(str(payload[field]) for field in ("turn_id", "step_id", "tool_call_id"))
    material += f"|{binding.plugin_id}|{binding.hook_id}|{binding.handler_revision}"
    return uuid5(NAMESPACE_URL, material).hex


def _hook_run(binding: PluginHookActivation, output: Mapping[str, object]) -> HookRun:
    if set(output) != {"exit_code", "stdout", "stderr"}:
        raise PluginHookRuntimeError("Plugin Hook output fields are invalid")
    exit_code, stdout, stderr = output.get("exit_code"), output.get("stdout"), output.get("stderr")
    if not isinstance(exit_code, int) or isinstance(exit_code, bool) or not isinstance(stdout, str) or not isinstance(stderr, str):
        raise PluginHookRuntimeError("Plugin Hook output is invalid")
    if len(stdout.encode("utf-8")) > _MAX_HOOK_TEXT_BYTES or len(stderr.encode("utf-8")) > _MAX_HOOK_TEXT_BYTES:
        raise PluginHookRuntimeError("Plugin Hook output exceeds the size limit")
    try:
        control = json.loads(stdout) if stdout.strip() else {}
    except json.JSONDecodeError:
        control = {}
    specific = control.get("hookSpecificOutput") if isinstance(control, Mapping) else None
    if isinstance(specific, Mapping) and "updatedInput" in specific:
        raise PluginHookRuntimeError("third-party Plugin Hooks cannot rewrite tool input")
    return HookRun(binding.order, 0, binding.synchronous, exit_code, stdout, stderr, binding.hook_id)
