"""Frozen, host-owned contracts for the isolated Plugin Hands process host.

This module deliberately has no Plugin activation, network, or secret-store
dependencies.  A launch is already host-approved before it reaches here.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType


PLUGIN_HANDS_PROTOCOL = "plugin-hands/1"
PLUGIN_HANDS_LAUNCH_ID_ENV = "CHRIPTMAS_PLUGIN_HANDS_LAUNCH_ID"
PLUGIN_HANDS_LEASE_ID_ENV = "CHRIPTMAS_PLUGIN_HANDS_LEASE_ID"
PLUGIN_HANDS_INVOCATION_ID_ENV = "CHRIPTMAS_PLUGIN_HANDS_INVOCATION_ID"
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{7,127}$")
_LEASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{7,127}$")
_ALLOWED_ENVIRONMENT_NAMES = frozenset({"LANG", "LC_ALL", "PYTHONUTF8", "PYTHONIOENCODING"})
_SECRET_LIKE_ENVIRONMENT_NAME = re.compile(r"(?i)(?:secret|token|key|password|credential|auth|cookie)")
_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_WORKSPACE_RESOURCES = frozenset({"workspace_input", "workspace_output"})


class PluginHandsError(ValueError):
    """Stable safe failure raised by the Plugin Hands host boundary."""


class PluginHandsContractError(PluginHandsError):
    """A frozen Plugin Hands contract is malformed or internally inconsistent."""


class PluginHandsWorkspaceError(PluginHandsError):
    """A host-owned Plugin Hands workspace cannot be safely used."""


def _identifier(value: object, label: str, *, lease: bool = False) -> str:
    pattern = _LEASE_ID if lease else _IDENTIFIER
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise PluginHandsContractError(f"Plugin Hands {label} is invalid")
    return value


def _text_items(value: object, label: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, tuple) or (not allow_empty and not value) or len(value) > 64:
        raise PluginHandsContractError(f"Plugin Hands {label} is invalid")
    items: list[str] = []
    total = 0
    for item in value:
        if not isinstance(item, str) or not item or "\x00" in item or len(item) > 4096:
            raise PluginHandsContractError(f"Plugin Hands {label} is invalid")
        total += len(item.encode("utf-8"))
        if total > 24 * 1024:
            raise PluginHandsContractError(f"Plugin Hands {label} is invalid")
        items.append(item)
    return tuple(items)


def _workspace_resources(value: object, label: str = "allowed resources") -> tuple[str, ...]:
    """Freeze the only resource vocabulary understood by the Hands host.

    Empty is deliberately valid for legacy, non-filesystem fixtures.  It does
    not imply a writable scratch directory: callers must create no optional
    resource directories for that scope.
    """

    items = _text_items(value, label, allow_empty=True)
    if len(set(items)) != len(items) or set(items) - _WORKSPACE_RESOURCES:
        raise PluginHandsContractError(f"Plugin Hands {label} is invalid")
    return items


def require_exact_workspace_resources(
    allowed_resources: tuple[str, ...], requested_resources: tuple[str, ...],
) -> None:
    """Reject both capability expansion and silent resource omission.

    This comparison is intentionally ordered as declared by the reviewed
    artifact.  An outer authority must carry that frozen declaration into the
    lease exactly; it cannot broaden, narrow, sort, or infer a resource set.
    """

    allowed = _workspace_resources(allowed_resources, "allowed resources")
    requested = _workspace_resources(requested_resources, "requested resources")
    if allowed != requested:
        raise PluginHandsContractError("Plugin Hands workspace resources drifted")


def _environment(value: object) -> Mapping[str, str]:
    if not isinstance(value, Mapping) or len(value) > 64:
        raise PluginHandsContractError("Plugin Hands environment is invalid")
    normalized: dict[str, str] = {}
    for name, item in value.items():
        if (
            not isinstance(name, str)
            or not name
            or "=" in name
            or "\x00" in name
            or name not in _ALLOWED_ENVIRONMENT_NAMES
            or _SECRET_LIKE_ENVIRONMENT_NAME.search(name) is not None
            or not isinstance(item, str)
            or "\x00" in item
        ):
            raise PluginHandsContractError("Plugin Hands environment is invalid")
        normalized[name] = item
    return MappingProxyType(normalized)


def _shallow_mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or len(value) > 128:
        raise PluginHandsContractError(f"Plugin Hands {label} is invalid")
    normalized: dict[str, object] = {}
    for name, item in value.items():
        if not isinstance(name, str) or not name or len(name) > 128 or "\x00" in name:
            raise PluginHandsContractError(f"Plugin Hands {label} is invalid")
        normalized[name] = item
    return MappingProxyType(normalized)


def _utc_timestamp(value: object) -> str:
    if not isinstance(value, str) or not value.endswith("Z") or len(value) > 64:
        raise PluginHandsContractError("Plugin Hands lease expiry is invalid")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise PluginHandsContractError("Plugin Hands lease expiry is invalid") from None
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise PluginHandsContractError("Plugin Hands lease expiry is invalid")
    return value


@dataclass(frozen=True, slots=True)
class PluginHandsLaunch:
    """Host-approved process launch details, never supplied by a Plugin package."""

    launch_id: str
    executable: Path = field(repr=False)
    argv: tuple[str, ...] = field(repr=False)
    environment: Mapping[str, str] = field(default_factory=dict, repr=False)
    protocol: str = PLUGIN_HANDS_PROTOCOL

    def __post_init__(self) -> None:
        object.__setattr__(self, "launch_id", _identifier(self.launch_id, "launch id"))
        if self.protocol != PLUGIN_HANDS_PROTOCOL:
            raise PluginHandsContractError("Plugin Hands protocol is invalid")
        if not isinstance(self.executable, Path) or not self.executable.is_absolute():
            raise PluginHandsContractError("Plugin Hands executable is invalid")
        object.__setattr__(self, "argv", _text_items(self.argv, "argv", allow_empty=True))
        object.__setattr__(self, "environment", _environment(self.environment))


@dataclass(frozen=True, slots=True)
class PluginHandsLease:
    """Frozen execution identity used to derive a single host workspace."""

    lease_id: str
    invocation_id: str
    generation: int
    project_id: str
    turn_id: str
    boundary_revision: int
    recipe_revision: str
    allowed_resources: tuple[str, ...]
    expires_at: str
    resource_policy_revision: str = "plugin-hands-resource-v1"

    def __post_init__(self) -> None:
        object.__setattr__(self, "lease_id", _identifier(self.lease_id, "lease id", lease=True))
        object.__setattr__(self, "invocation_id", _identifier(self.invocation_id, "invocation id"))
        object.__setattr__(self, "project_id", _identifier(self.project_id, "project id"))
        object.__setattr__(self, "turn_id", _identifier(self.turn_id, "turn id"))
        if not isinstance(self.generation, int) or isinstance(self.generation, bool) or self.generation < 1:
            raise PluginHandsContractError("Plugin Hands lease generation is invalid")
        if not isinstance(self.boundary_revision, int) or isinstance(self.boundary_revision, bool) or self.boundary_revision < 1:
            raise PluginHandsContractError("Plugin Hands boundary revision is invalid")
        object.__setattr__(self, "recipe_revision", _identifier(self.recipe_revision, "recipe revision"))
        object.__setattr__(self, "allowed_resources", _workspace_resources(self.allowed_resources))
        object.__setattr__(self, "expires_at", _utc_timestamp(self.expires_at))
        object.__setattr__(self, "resource_policy_revision", _identifier(self.resource_policy_revision, "resource policy revision"))


@dataclass(frozen=True, slots=True)
class PluginHandsInvocation:
    """A process invocation bound to exactly one launch and lease snapshot."""

    invocation_id: str
    plugin_id: str
    launch: PluginHandsLaunch
    lease: PluginHandsLease
    deadline_ms: int
    input: Mapping[str, object] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "invocation_id", _identifier(self.invocation_id, "invocation id"))
        object.__setattr__(self, "plugin_id", _identifier(self.plugin_id, "plugin id"))
        if not isinstance(self.launch, PluginHandsLaunch) or not isinstance(self.lease, PluginHandsLease):
            raise PluginHandsContractError("Plugin Hands invocation identity is invalid")
        if self.lease.invocation_id != self.invocation_id:
            raise PluginHandsContractError("Plugin Hands lease does not match invocation")
        if not isinstance(self.deadline_ms, int) or isinstance(self.deadline_ms, bool) or not 1 <= self.deadline_ms <= 300_000:
            raise PluginHandsContractError("Plugin Hands deadline is invalid")
        object.__setattr__(self, "input", _shallow_mapping(self.input, "input"))


@dataclass(frozen=True, slots=True)
class PluginHandsOutcome:
    """Terminal host disposition; only ``unknown`` retains a workspace."""

    invocation_id: str
    lease_id: str
    status: str
    output: Mapping[str, object] | None = field(default=None, repr=False)
    error_code: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "invocation_id", _identifier(self.invocation_id, "invocation id"))
        object.__setattr__(self, "lease_id", _identifier(self.lease_id, "lease id", lease=True))
        if self.status not in {"success", "failed", "cancelled", "unknown"}:
            raise PluginHandsContractError("Plugin Hands outcome status is invalid")
        if self.status == "success":
            if self.output is None or self.error_code is not None:
                raise PluginHandsContractError("Plugin Hands success outcome is invalid")
            object.__setattr__(self, "output", _shallow_mapping(self.output, "output"))
            return
        if self.output is not None or not isinstance(self.error_code, str) or _ERROR_CODE.fullmatch(self.error_code) is None:
            raise PluginHandsContractError("Plugin Hands error outcome is invalid")


@dataclass(frozen=True, slots=True)
class PluginHandsControl:
    """Optional host callback used by the runner to observe cancellation."""

    is_cancelled: Callable[[], bool] | None = field(default=None, repr=False, compare=False)

    def cancelled(self) -> bool:
        if self.is_cancelled is None:
            return False
        try:
            return bool(self.is_cancelled())
        except Exception:
            return True


def plugin_hands_protocol_identity_environment(
    launch: PluginHandsLaunch,
    invocation: PluginHandsInvocation,
) -> dict[str, str]:
    """Return the three non-sensitive facts needed before a child can say hello.

    These names are Host-reserved: ``PluginHandsLaunch.environment`` cannot
    contain them because it only permits reviewed locale/runtime settings.
    They intentionally exclude paths, process identifiers, grants, and all
    authorization or secret material.
    """

    if not isinstance(launch, PluginHandsLaunch) or not isinstance(invocation, PluginHandsInvocation):
        raise TypeError("Plugin Hands protocol identity is invalid")
    if launch != invocation.launch:
        raise PluginHandsContractError("Plugin Hands launch does not match invocation")
    return {
        PLUGIN_HANDS_LAUNCH_ID_ENV: launch.launch_id,
        PLUGIN_HANDS_LEASE_ID_ENV: invocation.lease.lease_id,
        PLUGIN_HANDS_INVOCATION_ID_ENV: invocation.invocation_id,
    }
