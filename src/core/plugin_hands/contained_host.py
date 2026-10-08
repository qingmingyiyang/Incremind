"""Internal Windows AppContainer composition for one Plugin Hands invocation."""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from threading import RLock
from weakref import WeakSet

from .contracts import PluginHandsControl, PluginHandsInvocation, PluginHandsLaunch, PluginHandsOutcome, plugin_hands_protocol_identity_environment
from .stdio_runner import PluginHandsProtocolExchange, validate_plugin_hands_request
from .windows_acl import PluginHandsAclError, grant_appcontainer_workspace_acl
from .windows_appcontainer import AppContainerError, AppContainerLaunchSpec, AppContainerProcess, AppContainerProfile, AppContainerResourceLimits, _add_cleanup_notes, launch_in_appcontainer
from .workspace import PluginHandsWorkspace, PluginHandsWorkspaceError, PluginHandsWorkspaceManager


@dataclass(frozen=True, slots=True)
class PluginHandsContainedExecution:
    """Outcome plus an opaque retained-workspace reference for unknown effects."""

    outcome: PluginHandsOutcome
    workspace_ref: str | None = None


class WindowsContainedPluginHandsHost:
    """Compose strict stdio with AppContainer without exposing an activation path."""

    def __init__(self, *, resource_policy_revision: str = "plugin-hands-resource-v1", resource_limits: AppContainerResourceLimits | None = None) -> None:
        if not isinstance(resource_policy_revision, str) or not resource_policy_revision:
            raise ValueError("Plugin Hands resource policy revision is invalid")
        self._resource_policy_revision = resource_policy_revision
        self._resource_limits = resource_limits or AppContainerResourceLimits(256 * 1024 * 1024, 2500)
        self._lock = RLock()
        self._active: set[AppContainerProcess | AppContainerProfile] = set()
        self._terminated: WeakSet[AppContainerProcess | AppContainerProfile] = WeakSet()
        self._closed = False

    def close(self) -> None:
        """Prevent new children and close every AppContainer Job currently owned."""

        with self._lock:
            self._closed = True
            failures = []
            for process in tuple(self._active):
                if not self._close_process_locked(process):
                    failures.append(("host resource", process._cleanup_error))
            if failures:
                error = AppContainerError("AppContainer host cleanup failed")
                _add_cleanup_notes(error, failures)
                raise error from failures[0][1]

    def execute(self, manager: PluginHandsWorkspaceManager, launch: PluginHandsLaunch, invocation: PluginHandsInvocation, control: PluginHandsControl = PluginHandsControl()) -> PluginHandsContainedExecution:
        try:
            workspace = manager.create(invocation.lease)
        except (PluginHandsWorkspaceError, TypeError, ValueError):
            return PluginHandsContainedExecution(_failed(invocation, "workspace-unavailable"))

        return _dispose(manager, workspace, self.execute_prepared(workspace, launch, invocation, control))

    def execute_prepared(self, workspace: PluginHandsWorkspace, launch: PluginHandsLaunch, invocation: PluginHandsInvocation, control: PluginHandsControl = PluginHandsControl()) -> PluginHandsOutcome:
        """Execute only a host-created workspace; disposition remains external.

        The durable lifecycle coordinator fences this call before it can create
        an AppContainer process, then persists its terminal outcome before
        asking the workspace manager to remove anything.
        """
        if os.name != "nt":
            return _failed(invocation, "containment-unavailable")
        if not isinstance(workspace, PluginHandsWorkspace):
            return _failed(invocation, "workspace-unavailable")
        if invocation.lease.resource_policy_revision != self._resource_policy_revision:
            return _failed(invocation, "resource-policy-unavailable")
        process: AppContainerProcess | None = None
        profile: AppContainerProfile | None = None
        outcome = _failed(invocation, "containment-unavailable")
        cleanup_failed = False
        try:
            validate_plugin_hands_request(launch, workspace, invocation, control)
            if control.cancelled():
                outcome = _failed(invocation, "cancelled-before-spawn")
            else:
                # Shutdown cannot return between spawn and ownership
                # registration. It either closes first (zero spawn), or waits
                # until this Job is visible and then closes it.
                with self._lock:
                    if self._closed:
                        return _failed(invocation, "host-shutdown")
                    profile = AppContainerProfile.create()
                    self._active.add(profile)
                    acl = grant_appcontainer_workspace_acl(workspace.root, profile.sid, allowed_resources=workspace.lease.allowed_resources)
                    spec = AppContainerLaunchSpec(launch.executable, launch.argv, workspace.root, _contained_environment(launch, workspace, invocation, acl.tmp_dir))
                    try:
                        process = launch_in_appcontainer(spec, profile, resource_limits=self._resource_limits)
                    except BaseException as error:
                        pending = getattr(error, "_cleanup_process", None)
                        # 未交付但仍持真实资源的 owner 必须在释放原锁前收编。
                        if isinstance(pending, AppContainerProcess) and pending._has_cleanup_resources():
                            process = pending
                            self._register_process_locked(process, profile)
                        else:
                            # 没有创建进程时，原 profile 活动项保留尚未关闭的原管道。
                            pending_stdio = getattr(error, "_cleanup_stdio", None)
                            if pending_stdio is not None and pending_stdio._has_cleanup_resources():
                                profile._cleanup_stdio = pending_stdio
                        raise
                    self._register_process_locked(process, profile)
                deadline_at = time.monotonic() + invocation.deadline_ms / 1000
                outcome = PluginHandsProtocolExchange().exchange_outcome(process, launch, invocation, deadline_at, control)
        except (AppContainerError, PluginHandsAclError, PluginHandsWorkspaceError, OSError, TypeError, ValueError):
            outcome = _unknown(invocation, "runner-failed") if process is not None else _failed(invocation, "containment-unavailable")
        finally:
            if process is not None:
                if not self._close_process(process):
                    cleanup_failed = True
            elif profile is not None:
                if not self._close_process(profile):
                    cleanup_failed = True

        if cleanup_failed:
            outcome = _unknown(invocation, "containment-cleanup-failed")
        return outcome

    def _register_process_locked(self, process: AppContainerProcess, profile: AppContainerProfile) -> None:
        # profile 随同唯一 process 留在原活动集合，不建立第二份资源登记。
        process._contained_profile = profile
        self._active.add(process)
        self._active.discard(profile)

    def _close_process(self, process: AppContainerProcess | AppContainerProfile) -> bool:
        with self._lock:
            return self._close_process_locked(process)

    def _close_process_locked(self, process: AppContainerProcess | AppContainerProfile) -> bool:
        if process in self._terminated:
            return True
        try:
            pending_stdio = getattr(process, "_cleanup_stdio", None)
            if pending_stdio is not None:
                pending_stdio.close()
                process._cleanup_stdio = None
            process.close()
            profile = getattr(process, "_contained_profile", None)
            if profile is not None:
                profile.close()
        except (OSError, AppContainerError) as error:
            process._cleanup_error = error
            return False
        process._cleanup_error = None
        self._active.discard(process)
        self._terminated.add(process)
        return True


def _contained_environment(launch: PluginHandsLaunch, workspace: PluginHandsWorkspace, invocation: PluginHandsInvocation, tmp_dir) -> dict[str, str]:
    names = ("ALLUSERSPROFILE", "APPDATA", "COMSPEC", "HOMEDRIVE", "HOMEPATH", "LOCALAPPDATA", "PATHEXT", "ProgramData", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "PUBLIC", "SystemDrive", "SYSTEMROOT", "USERPROFILE", "WINDIR")
    environment = {name: os.environ[name] for name in names if name in os.environ}
    environment.update(dict(launch.environment))
    # A no-output lease deliberately has no writable temp directory.  Pointing
    # TEMP at its traverse-only root makes accidental child writes fail closed.
    scratch = tmp_dir if tmp_dir is not None else workspace.root
    environment.update({"PATH": str(launch.executable.parent), "TEMP": str(scratch), "TMP": str(scratch)})
    environment.update(plugin_hands_protocol_identity_environment(launch, invocation))
    return environment


def _dispose(manager: PluginHandsWorkspaceManager, workspace: PluginHandsWorkspace, outcome: PluginHandsOutcome) -> PluginHandsContainedExecution:
    try:
        reference = manager.dispose(workspace, outcome)
    except PluginHandsWorkspaceError:
        return PluginHandsContainedExecution(_unknown_from(outcome, "workspace-disposition-failed"), workspace.host_ref)
    return PluginHandsContainedExecution(outcome, reference)


def _failed(invocation: PluginHandsInvocation, code: str) -> PluginHandsOutcome:
    return PluginHandsOutcome(invocation.invocation_id, invocation.lease.lease_id, "failed", error_code=code)


def _unknown(invocation: PluginHandsInvocation, code: str) -> PluginHandsOutcome:
    return PluginHandsOutcome(invocation.invocation_id, invocation.lease.lease_id, "unknown", error_code=code)


def _unknown_from(outcome: PluginHandsOutcome, code: str) -> PluginHandsOutcome:
    return PluginHandsOutcome(outcome.invocation_id, outcome.lease_id, "unknown", error_code=code)
