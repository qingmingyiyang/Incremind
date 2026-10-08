from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from typing import Protocol

from .health import ProductHealth


class PlatformRecoveryActionWriterPort(Protocol):
    """Persists platform recovery action records."""

    def save(self, action: Mapping[str, object]) -> Mapping[str, object]:
        """Persist and return a platform recovery action."""


class PlatformRecoveryActionStorePort(PlatformRecoveryActionWriterPort, Protocol):
    """Reads and updates persisted platform recovery action records."""

    def get(self, action_id: str) -> Mapping[str, object] | None:
        """Read a platform recovery action."""

    def update(self, action: Mapping[str, object]) -> Mapping[str, object]:
        """Persist an updated platform recovery action."""


class PlatformRecoveryActionError(ValueError):
    """Raised when Product Health cannot safely become a recovery action."""


@dataclass(frozen=True, slots=True)
class PlatformRecoveryActionResult:
    actions: tuple[Mapping[str, object], ...]


@dataclass(frozen=True, slots=True)
class PlatformRecoveryActionDecisionResult:
    action: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class PlatformRecoveryActionResolutionResult:
    action: Mapping[str, object]


class CreatePlatformRecoveryActions:
    """Create non-executing recovery action records from degraded Product Health."""

    def __init__(
        self,
        *,
        actions: PlatformRecoveryActionWriterPort,
        namespace_id: str = "default",
    ) -> None:
        self._actions = actions
        self._namespace_id = namespace_id

    def execute(
        self,
        *,
        health: ProductHealth,
        created_at: str | None = None,
    ) -> PlatformRecoveryActionResult:
        if health.platform_status == "ready":
            return PlatformRecoveryActionResult(actions=())
        timestamp = created_at or _utc_now()
        candidates = tuple(_actions_from_health(health, namespace_id=self._namespace_id, timestamp=timestamp))
        saved = tuple(self._actions.save(action) for action in candidates)
        return PlatformRecoveryActionResult(actions=saved)


class ReviewPlatformRecoveryAction:
    """Acknowledge or dismiss a recovery action without executing platform repair."""

    def __init__(self, *, actions: PlatformRecoveryActionStorePort) -> None:
        self._actions = actions

    def acknowledge(
        self,
        *,
        action_id: str,
        decided_by: str,
        note: str | None = None,
        decided_at: str | None = None,
    ) -> PlatformRecoveryActionDecisionResult:
        return self._decide(
            action_id=action_id,
            decision="acknowledged",
            decided_by=decided_by,
            note=note,
            decided_at=decided_at,
        )

    def dismiss(
        self,
        *,
        action_id: str,
        decided_by: str,
        note: str | None = None,
        decided_at: str | None = None,
    ) -> PlatformRecoveryActionDecisionResult:
        return self._decide(
            action_id=action_id,
            decision="dismissed",
            decided_by=decided_by,
            note=note,
            decided_at=decided_at,
        )

    def _decide(
        self,
        *,
        action_id: str,
        decision: str,
        decided_by: str,
        note: str | None,
        decided_at: str | None,
    ) -> PlatformRecoveryActionDecisionResult:
        if decision not in {"acknowledged", "dismissed"}:
            raise PlatformRecoveryActionError("platform recovery action decision is not supported")
        if not decided_by:
            raise PlatformRecoveryActionError("platform recovery action decision requires decided_by")
        action = self._actions.get(action_id)
        if action is None:
            raise PlatformRecoveryActionError("platform recovery action was not found")
        if action.get("status") != "open":
            raise PlatformRecoveryActionError("platform recovery action decision requires open status")
        execution = action.get("execution")
        if not isinstance(execution, Mapping) or execution.get("auto_execute") is not False:
            raise PlatformRecoveryActionError("platform recovery action decision must not execute repair")
        timestamp = decided_at or _utc_now()
        updated = dict(action)
        updated["status"] = decision
        updated["user_decision"] = {
            "decision": decision,
            "decided_by": decided_by,
            "decided_at": timestamp,
            "note": note,
            "execution_requested": False,
        }
        updated["updated_at"] = timestamp
        saved = self._actions.update(updated)
        return PlatformRecoveryActionDecisionResult(action=saved)


class ResolvePlatformRecoveryAction:
    """Resolve a recovery action only when current Product Health proves recovery."""

    def __init__(self, *, actions: PlatformRecoveryActionStorePort) -> None:
        self._actions = actions

    def execute(
        self,
        *,
        action_id: str,
        recovered_health: ProductHealth,
        resolved_by: str,
        resolved_at: str | None = None,
    ) -> PlatformRecoveryActionResolutionResult:
        if not resolved_by:
            raise PlatformRecoveryActionError("platform recovery action resolution requires resolved_by")
        action = self._actions.get(action_id)
        if action is None:
            raise PlatformRecoveryActionError("platform recovery action was not found")
        if action.get("status") not in {"open", "acknowledged"}:
            raise PlatformRecoveryActionError("platform recovery action resolution requires open or acknowledged status")
        execution = action.get("execution")
        if not isinstance(execution, Mapping) or execution.get("auto_execute") is not False:
            raise PlatformRecoveryActionError("platform recovery action resolution must not execute repair")
        capability = _required_action_string(action, "capability")
        if not _capability_recovered(capability, recovered_health):
            raise PlatformRecoveryActionError("platform recovery action resolution requires verified ready capability")
        source = action.get("source")
        if not isinstance(source, Mapping):
            raise PlatformRecoveryActionError("platform recovery action requires source")
        timestamp = resolved_at or _utc_now()
        updated = dict(action)
        updated["status"] = "resolved"
        updated["resolution"] = {
            "resolved_by": resolved_by,
            "resolved_at": timestamp,
            "health_status": "ready",
            "health_ref": _required_action_string(source, "health_ref"),
            "recovered_capability": capability,
            "readiness_verified": True,
            "remaining_degraded_capabilities": list(recovered_health.platform_degraded_capabilities),
            "remaining_missing_capabilities": list(recovered_health.platform_missing_capabilities),
            "remaining_os_path_leaks": list(recovered_health.platform_os_path_leaks),
        }
        updated["updated_at"] = timestamp
        saved = self._actions.update(updated)
        return PlatformRecoveryActionResolutionResult(action=saved)


def _actions_from_health(
    health: ProductHealth,
    *,
    namespace_id: str,
    timestamp: str,
) -> tuple[dict[str, object], ...]:
    actions: list[dict[str, object]] = []
    for capability in health.platform_degraded_capabilities:
        actions.append(
            _action(
                namespace_id=namespace_id,
                capability=capability,
                severity="warning",
                action_type=_action_type(capability, degraded=True),
                title=_title(capability, degraded=True),
                description=_description(capability, degraded=True),
                health=health,
                timestamp=timestamp,
            )
        )
    for capability in health.platform_missing_capabilities:
        actions.append(
            _action(
                namespace_id=namespace_id,
                capability=capability,
                severity="blocking",
                action_type=_action_type(capability, missing=True),
                title=_title(capability, missing=True),
                description=_description(capability, missing=True),
                health=health,
                timestamp=timestamp,
            )
        )
    for capability in health.platform_os_path_leaks:
        actions.append(
            _action(
                namespace_id=namespace_id,
                capability=capability,
                severity="blocking",
                action_type="fix_platform_uri",
                title=f"{capability} exposes an OS-specific path.",
                description="Replace the platform capability URI with a platform-neutral URI before it reaches Product Core.",
                health=health,
                timestamp=timestamp,
            )
        )
    return tuple(actions)


def _action(
    *,
    namespace_id: str,
    capability: str,
    severity: str,
    action_type: str,
    title: str,
    description: str,
    health: ProductHealth,
    timestamp: str,
) -> dict[str, object]:
    action_id = _action_id(capability, action_type, health)
    return {
        "schema_version": "1.0.0",
        "id": action_id,
        "capability": capability,
        "severity": severity,
        "status": "open",
        "action_type": action_type,
        "title": title,
        "description": description,
        "source": {
            "health_status": "degraded",
            "health_ref": f"crp://{namespace_id}/product-health/platform",
        },
        "evidence": {
            "degraded_capabilities": list(health.platform_degraded_capabilities),
            "missing_capabilities": list(health.platform_missing_capabilities),
            "os_path_leaks": list(health.platform_os_path_leaks),
        },
        "execution": {
            "auto_execute": False,
            "requires_user_confirmation": True,
            "job_id": None,
        },
        "user_decision": None,
        "resolution": None,
        "created_at": timestamp,
        "updated_at": timestamp,
    }


def _action_type(capability: str, *, degraded: bool = False, missing: bool = False) -> str:
    if capability == "worker_lifecycle":
        return "restart_local_worker"
    if capability == "backup_destination":
        return "select_backup_destination"
    if capability in {"file_picker", "documents_dir"}:
        return "request_permission"
    if missing:
        return "retry_capability_probe"
    if degraded:
        return "inspect_platform_logs"
    return "retry_capability_probe"


def _title(capability: str, *, degraded: bool = False, missing: bool = False) -> str:
    if capability == "worker_lifecycle" and degraded:
        return "Worker lifecycle is running in local-only fallback."
    if missing:
        return f"{capability} platform capability is missing."
    return f"{capability} platform capability is degraded."


def _description(capability: str, *, degraded: bool = False, missing: bool = False) -> str:
    if capability == "worker_lifecycle" and degraded:
        return (
            "The platform worker lifecycle is unavailable. Keep local browsing available "
            "and offer a controlled worker restart when the platform adapter is ready."
        )
    if missing:
        return "Run the platform capability probe again and keep the product in degraded mode until it is available."
    return "Inspect platform adapter logs and keep the product in degraded mode until the capability is ready."


def _action_id(capability: str, action_type: str, health: ProductHealth) -> str:
    digest = hashlib.sha256(
        "\n".join(
            (
                capability,
                action_type,
                ",".join(health.platform_degraded_capabilities),
                ",".join(health.platform_missing_capabilities),
                ",".join(health.platform_os_path_leaks),
            )
        ).encode("utf-8")
    ).hexdigest()[:16]
    return f"platform-recovery-{capability}-{digest}"


def _capability_recovered(capability: str, health: ProductHealth) -> bool:
    if health.platform_status != "ready":
        return False
    if capability not in health.platform_ready_capabilities:
        return False
    return (
        capability not in health.platform_degraded_capabilities
        and capability not in health.platform_missing_capabilities
        and capability not in health.platform_os_path_leaks
    )


def _required_action_string(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise PlatformRecoveryActionError(f"platform recovery action requires {key}")
    return value


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
