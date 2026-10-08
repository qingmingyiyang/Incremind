from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock

from core.ai_kernel import CapabilityDefinition, CapabilityRegistryPort
from core.ai_kernel.ports import CapabilityRegistrationPort
from core.plugin_host import (
    PluginPackageIntake,
    PluginPackageIntakeError,
    PluginSkillActivation,
    PluginToolActivation,
    PluginToolBinding,
    PluginMCPReferenceActivation,
)
from backend.security.mcp_approved_servers import JsonMCPApprovedServerStore
from core.storage_provider import SQLiteStructuredRecordStore


def build_plugin_package_intake(root_dir: Path) -> PluginPackageIntake:
    root = root_dir.expanduser().resolve(strict=False)
    return PluginPackageIntake(
        SQLiteStructuredRecordStore(root / ".rebuild-data" / "jobs.sqlite3"),
        now=datetime.now(timezone.utc).isoformat(),
        source_root=root / ".rebuild-data" / "plugin-package-inbox",
    )


def build_plugin_skill_activation(root_dir: Path) -> PluginSkillActivation:
    root = root_dir.expanduser().resolve(strict=False)
    return PluginSkillActivation(
        SQLiteStructuredRecordStore(root / ".rebuild-data" / "jobs.sqlite3"),
        managed_root=root / ".rebuild-data" / "plugin-skill-materializations",
        now=datetime.now(timezone.utc).isoformat(),
    )


def build_plugin_tool_activation(root_dir: Path) -> PluginToolActivation:
    root = root_dir.expanduser().resolve(strict=False)
    return PluginToolActivation(
        SQLiteStructuredRecordStore(root / ".rebuild-data" / "jobs.sqlite3"),
        now=datetime.now(timezone.utc).isoformat(),
    )


def build_plugin_mcp_reference_activation(root_dir: Path) -> PluginMCPReferenceActivation:
    root = root_dir.expanduser().resolve(strict=False)
    return PluginMCPReferenceActivation(
        SQLiteStructuredRecordStore(root / ".rebuild-data" / "jobs.sqlite3"),
        authority_loader=JsonMCPApprovedServerStore(root).snapshot,
        now=datetime.now(timezone.utc).isoformat(),
    )


class PluginToolRegistrationManager:
    """Project one durable Plugin Tool snapshot into the sole AI registry.

    The manager intentionally owns registrations only, never a second tool
    authority.  Every reconciliation reloads the durable activation records;
    malformed, disabled, or byte-drifted records simply disappear from the
    desired snapshot and their existing registry leases are closed.
    """

    def __init__(self, *, activation: PluginToolActivation, registry: CapabilityRegistryPort) -> None:
        self._activation = activation
        self._registry = registry
        self._lock = RLock()
        self._leases: dict[str, tuple[tuple[object, ...], CapabilityRegistrationPort]] = {}
        self._conflicting_tool_ids: tuple[str, ...] = ()

    @property
    def conflicting_tool_ids(self) -> tuple[str, ...]:
        """Active durable Plugin Tools withheld for a registry identity collision."""
        with self._lock:
            return self._conflicting_tool_ids

    def reconcile(self) -> tuple[CapabilityDefinition, ...]:
        with self._lock:
            desired, duplicate_ids = self._desired()
            desired_keys = set(desired)
            conflicts: set[str] = set(duplicate_ids)
            for tool_id in tuple(self._leases):
                if tool_id not in desired_keys or self._leases[tool_id][0] != desired[tool_id][0]:
                    self._leases.pop(tool_id)[1].close()
            registered: list[CapabilityDefinition] = []
            for tool_id in sorted(desired):
                fingerprint, binding = desired[tool_id]
                current = self._leases.get(tool_id)
                if current is None:
                    # Existing registrations are owned by core/MCP managers.
                    # Do not let a durable Plugin record break the whole AI
                    # runtime or replace their capability identity.
                    if self._registry.resolve(tool_id) is not None:
                        conflicts.add(tool_id)
                        continue
                    try:
                        definition = _capability_definition(binding)
                        lease = self._registry.register(
                            definition,
                            _DurablePluginToolProvider(
                                activation=self._activation,
                                expected=binding,
                            ),
                        )
                    except Exception:
                        # Registry implementations only promise a duplicate
                        # rejection. Treat any admission failure as withheld,
                        # never as a reason to fail startup or leak a provider.
                        conflicts.add(tool_id)
                        continue
                    self._leases[tool_id] = (fingerprint, lease)
                registered.append(_capability_definition(binding))
            self._conflicting_tool_ids = tuple(sorted(conflicts))
            return tuple(registered)

    def close(self) -> None:
        with self._lock:
            for _fingerprint, lease in tuple(self._leases.values()):
                lease.close()
            self._leases.clear()

    def _desired(self) -> tuple[dict[str, tuple[tuple[object, ...], PluginToolBinding]], set[str]]:
        desired: dict[str, tuple[tuple[object, ...], PluginToolBinding]] = {}
        duplicates: set[str] = set()
        for binding in self._activation.all_active_tools():
            tool = binding.definition
            fingerprint = (
                binding.plugin_id, tool.tool_id, tool.version, tool.description,
                tool.input_schema_uri, tool.output_schema_uri, tool.operation_semantics,
            )
            if tool.tool_id in desired:
                duplicates.add(tool.tool_id)
            else:
                desired[tool.tool_id] = (fingerprint, binding)
        for tool_id in duplicates:
            desired.pop(tool_id, None)
        return desired, duplicates


class _DurablePluginToolProvider:
    """Recheck durable raw/review/activation binding before every invocation."""

    def __init__(self, *, activation: PluginToolActivation, expected: PluginToolBinding) -> None:
        self._activation = activation
        self._expected = expected

    def invoke(self, request: Mapping[str, object]) -> Mapping[str, object]:
        current = next(
            (
                binding
                for binding in self._activation.active_contributions((self._expected.plugin_id,))
                if binding.definition.tool_id == self._expected.definition.tool_id
            ),
            None,
        )
        if current is None or current.definition != self._expected.definition:
            raise PluginPackageIntakeError("Plugin Tool durable activation drifted")
        return current.provider.invoke(request)


def _capability_definition(binding: PluginToolBinding) -> CapabilityDefinition:
    tool = binding.definition
    return CapabilityDefinition(
        capability_id=tool.tool_id,
        version=tool.version,
        mode=tool.effect,
        requires_approval=False,
        operation_semantics=tool.operation_semantics,
        input_schema_uri=tool.input_schema_uri,
        output_schema_uri=tool.output_schema_uri,
        tool_definition=tool,
    )
