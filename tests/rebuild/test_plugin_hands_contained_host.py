from __future__ import annotations

import os
from pathlib import Path
from threading import Event, Thread
import time
from types import SimpleNamespace

import pytest

from core.plugin_hands.contained_host import WindowsContainedPluginHandsHost
from core.plugin_hands.contracts import PluginHandsControl, PluginHandsInvocation, PluginHandsLaunch, PluginHandsLease, PluginHandsOutcome
import core.plugin_hands.contained_host as contained_host_module
from core.plugin_hands.workspace import PluginHandsWorkspaceManager
from tests.rebuild.test_plugin_hands_appcontainer_stdio import _NativeFaults, _cleanup_job_red, _cleanup_raw_red, _gone, _handle_open


def _lease() -> PluginHandsLease:
    return PluginHandsLease("lease-0001", "invoke-0001", 1, "project-0001", "turn-0001", 1, "recipe-0001", (), "2099-08-26T00:00:00Z")


def _launch(command: str) -> PluginHandsLaunch:
    executable = Path(os.environ["SYSTEMROOT"]) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    return PluginHandsLaunch("launch-0001", executable.resolve(), ("-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command))


def _invocation(launch: PluginHandsLaunch, *, deadline_ms: int = 2000) -> PluginHandsInvocation:
    lease = _lease()
    return PluginHandsInvocation("invoke-0001", "plugin-0001", launch, lease, deadline_ms, {"task": "fixture"})


def _message(kind: str, tail: str = "") -> str:
    return '{"protocol":"plugin-hands/1","type":"' + kind + '","launch_id":"launch-0001","lease_id":"lease-0001","invocation_id":"invoke-0001"' + tail + "}"


def _manager(tmp_path: Path) -> PluginHandsWorkspaceManager:
    root = tmp_path / "host"
    root.mkdir(parents=True)
    return PluginHandsWorkspaceManager(root.resolve())


@pytest.mark.skipif(os.name != "nt", reason="Windows AppContainer contained stdio")
def test_real_contained_protocol_success_disposes_workspace(tmp_path: Path) -> None:
    command = "[Console]::Out.WriteLine('" + _message("hello") + "'); [Console]::In.ReadLine() | Out-Null; [Console]::Out.WriteLine('" + _message("result", ',"output":{"ok":true}') + "')"
    launch = _launch(command)
    result = WindowsContainedPluginHandsHost().execute(_manager(tmp_path), launch, _invocation(launch))
    assert result.outcome.status == "success"
    assert result.outcome.output == {"ok": True}
    assert result.workspace_ref is None
    assert not (tmp_path / "host" / "lease-0001").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows AppContainer contained stdio")
def test_real_contained_protocol_fault_retains_unknown_workspace(tmp_path: Path) -> None:
    launch = _launch("[Console]::Out.WriteLine('" + _message("hello") + "'); [Console]::In.ReadLine() | Out-Null; [Console]::Out.WriteLine('{bad-json')")
    result = WindowsContainedPluginHandsHost().execute(_manager(tmp_path), launch, _invocation(launch))
    assert (result.outcome.status, result.outcome.error_code) == ("unknown", "protocol-error")
    assert result.workspace_ref == "plugin-hands-workspace:lease-0001:1"
    assert (tmp_path / "host" / "lease-0001").is_dir()


@pytest.mark.skipif(os.name != "nt", reason="Windows AppContainer contained stdio")
@pytest.mark.parametrize(
    ("terminal", "error_code"),
    (
        ("[Console]::Out.WriteLine('" + _message("result", ',"output":{},"unexpected":true') + "')", "protocol-error"),
        ("[Console]::Out.WriteLine('" + _message("result", ',"output":{}').replace('lease-0001', 'other-0001') + "')", "identity-mismatch"),
        ("[Console]::Out.WriteLine('" + _message("error", ',"code":"fixture-error"') + "')", "child-error"),
        ("exit 0", "eof"),
        ("[Console]::Out.WriteLine(('x' * 65536))", "output-oversize"),
    ),
)
def test_real_contained_terminal_fault_matrix_is_unknown(tmp_path: Path, terminal: str, error_code: str) -> None:
    command = "[Console]::Out.WriteLine('" + _message("hello") + "'); [Console]::In.ReadLine() | Out-Null; " + terminal
    launch = _launch(command)
    result = WindowsContainedPluginHandsHost().execute(_manager(tmp_path), launch, _invocation(launch))
    assert (result.outcome.status, result.outcome.error_code) == ("unknown", error_code)
    assert result.workspace_ref == "plugin-hands-workspace:lease-0001:1"


@pytest.mark.skipif(os.name != "nt", reason="Windows AppContainer contained stdio")
def test_real_contained_deadline_and_post_spawn_cancel_are_unknown(tmp_path: Path) -> None:
    command = "[Console]::Out.WriteLine('" + _message("hello") + "'); [Console]::In.ReadLine() | Out-Null; while ($true) {}"
    launch = _launch(command)
    timed_out = WindowsContainedPluginHandsHost().execute(_manager(tmp_path / "timeout"), launch, _invocation(launch, deadline_ms=100))
    assert (timed_out.outcome.status, timed_out.outcome.error_code) == ("unknown", "deadline-exceeded")

    checks = 0

    def cancel_after_spawn() -> bool:
        nonlocal checks
        checks += 1
        return checks > 1

    launch = _launch(command)
    cancelled = WindowsContainedPluginHandsHost().execute(_manager(tmp_path / "cancel"), launch, _invocation(launch), PluginHandsControl(is_cancelled=cancel_after_spawn))
    assert (cancelled.outcome.status, cancelled.outcome.error_code) == ("unknown", "cancelled")


@pytest.mark.skipif(os.name != "nt", reason="Windows AppContainer contained stdio")
def test_pre_cancel_is_confirmed_none_and_disposes_workspace(tmp_path: Path) -> None:
    launch = _launch("[Console]::Out.WriteLine('should-not-run')")
    result = WindowsContainedPluginHandsHost().execute(_manager(tmp_path), launch, _invocation(launch), PluginHandsControl(is_cancelled=lambda: True))
    assert (result.outcome.status, result.outcome.error_code) == ("failed", "cancelled-before-spawn")
    assert result.workspace_ref is None
    assert not (tmp_path / "host" / "lease-0001").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows AppContainer contained stdio")
def test_resource_policy_drift_fails_before_spawn_and_disposes_workspace(tmp_path: Path) -> None:
    launch = _launch("[Console]::Out.WriteLine('should-not-run')")
    host = WindowsContainedPluginHandsHost(resource_policy_revision="plugin-hands-resource-v2")
    result = host.execute(_manager(tmp_path), launch, _invocation(launch))
    assert (result.outcome.status, result.outcome.error_code) == ("failed", "resource-policy-unavailable")
    assert result.workspace_ref is None
    assert not (tmp_path / "host" / "lease-0001").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows AppContainer contained stdio")
def test_host_close_kills_active_appcontainer_and_retains_unknown_workspace(tmp_path: Path) -> None:
    command = "[Console]::Out.WriteLine('" + _message("hello") + "'); [Console]::In.ReadLine() | Out-Null; while ($true) {}"
    launch = _launch(command)
    host = WindowsContainedPluginHandsHost()
    manager = _manager(tmp_path)
    results = []
    worker = Thread(target=lambda: results.append(host.execute(
        manager, launch, _invocation(launch, deadline_ms=10_000),
    )))
    worker.start()
    time.sleep(0.5)

    host.close()
    worker.join(timeout=3)

    assert not worker.is_alive()
    assert len(results) == 1
    assert results[0].outcome.status == "unknown"
    assert results[0].workspace_ref == "plugin-hands-workspace:lease-0001:1"
    assert (tmp_path / "host" / "lease-0001").is_dir()


@pytest.mark.skipif(os.name != "nt", reason="Windows AppContainer contained stdio")
def test_shutdown_cannot_return_between_spawn_and_active_registration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    launch_entered, release_launch, close_done = Event(), Event(), Event()

    class Profile:
        sid = "S-1-15-2-fixture"
        def close(self): pass

    class Process:
        closed = Event()
        def close(self): self.closed.set()

    process = Process()

    def launch_blocked(_spec, _profile, *, resource_limits):
        assert resource_limits.process_memory_bytes > 0
        launch_entered.set()
        assert release_launch.wait(2)
        return process

    def exchange(_self, _process, _launch, invocation, _deadline, _control):
        assert process.closed.wait(2)
        return PluginHandsOutcome(invocation.invocation_id, invocation.lease.lease_id, "unknown", error_code="host-shutdown")

    monkeypatch.setattr(contained_host_module.AppContainerProfile, "create", lambda: Profile())
    monkeypatch.setattr(contained_host_module, "grant_appcontainer_workspace_acl", lambda *_args, **_kwargs: SimpleNamespace(tmp_dir=None))
    monkeypatch.setattr(contained_host_module, "launch_in_appcontainer", launch_blocked)
    monkeypatch.setattr(contained_host_module.PluginHandsProtocolExchange, "exchange_outcome", exchange)
    host = WindowsContainedPluginHandsHost()
    manager = _manager(tmp_path)
    launch = _launch("exit 0")
    worker = Thread(target=lambda: host.execute(manager, launch, _invocation(launch)))
    worker.start()
    assert launch_entered.wait(2)
    closer = Thread(target=lambda: (host.close(), close_done.set()))
    closer.start()
    assert not close_done.wait(0.05)

    release_launch.set()
    assert close_done.wait(2)
    worker.join(timeout=2)
    closer.join(timeout=2)

    assert not worker.is_alive()
    assert process.closed.is_set()


def test_failed_launcher_owner_and_profile_remain_active_until_real_host_retry(tmp_path, monkeypatch):
    controls = _NativeFaults(monkeypatch)
    controls.fail_job = controls.block_terminate = True
    host = WindowsContainedPluginHandsHost()
    manager = _manager(tmp_path)
    launch = _launch("exit 0")
    try:
        result = host.execute(manager, launch, _invocation(launch))
        assert (result.outcome.status, result.outcome.error_code) == ("unknown", "containment-cleanup-failed")
        assert result.workspace_ref == "plugin-hands-workspace:lease-0001:1"
        assert (tmp_path / "host" / "lease-0001").is_dir()
        assert len(host._active) == 1
        owner = next(iter(host._active))
        assert controls.owners == [owner]
        profile = owner._contained_profile
        assert controls.profiles == [profile]
        assert profile.sid and not profile._deleted
        assert owner.process_id == controls.process_ids[0]
        assert _handle_open(owner.process_handle)
        with pytest.raises(contained_host_module.AppContainerError, match="cleanup"):
            host.close()
        assert host._active == {owner}
        assert owner not in host._terminated
        assert profile.sid and not profile._deleted
        controls.recover()
        host.close()
        assert not host._active
        assert owner in host._terminated
        assert profile._deleted and profile._sid == 0
        _gone(owner.process_id)
        controls.assert_released()
        assert (tmp_path / "host" / "lease-0001").is_dir()
        host.close()
    finally:
        controls.cleanup_red()


def test_profile_only_cleanup_failure_retains_same_native_profile_for_host_retry(tmp_path, monkeypatch):
    controls = _NativeFaults(monkeypatch)
    controls.block_create = controls.block_delete = True
    host = WindowsContainedPluginHandsHost()
    manager = _manager(tmp_path)
    launch = _launch("exit 0")
    try:
        result = host.execute(manager, launch, _invocation(launch))
        assert (result.outcome.status, result.outcome.error_code) == ("unknown", "containment-cleanup-failed")
        assert result.workspace_ref == "plugin-hands-workspace:lease-0001:1"
        assert controls.process_ids == controls.owners == []
        assert len(controls.profiles) == 1
        profile = controls.profiles[0]
        sid, name = profile.sid, profile.name
        assert host._active == {profile}
        with pytest.raises(contained_host_module.AppContainerError, match="cleanup"):
            host.close()
        assert profile.sid == sid and profile.name == name and not profile._deleted
        assert host._active == {profile}
        assert profile not in host._terminated
        controls.recover()
        host.close()
        assert profile._deleted and profile._sid == 0
        assert not host._active and profile in host._terminated
        controls.assert_released()
        assert (tmp_path / "host" / "lease-0001").is_dir()
        host.close()
    finally:
        controls.cleanup_red()


def test_normal_job_exit_fact_timeout_retains_host_owner_profile_and_workspace(tmp_path, monkeypatch):
    controls = _NativeFaults(monkeypatch)
    controls.block_wait = True
    host = WindowsContainedPluginHandsHost()
    manager = _manager(tmp_path)
    command = "[Console]::Out.WriteLine('" + _message("hello") + "'); [Console]::In.ReadLine() | Out-Null; [Console]::Out.WriteLine('" + _message("result", ',"output":{"ok":true}') + "'); while ($true) { Start-Sleep -Milliseconds 100 }"
    launch = _launch(command)
    try:
        result = host.execute(manager, launch, _invocation(launch))
        assert (result.outcome.status, result.outcome.error_code) == ("unknown", "containment-cleanup-failed")
        assert result.workspace_ref == "plugin-hands-workspace:lease-0001:1"
        assert (tmp_path / "host" / "lease-0001").is_dir()
        assert len(host._active) == 1
        owner = next(iter(host._active))
        assert controls.owners == [owner]
        profile = owner._contained_profile
        assert controls.profiles == [profile]
        assert owner._job._handle == 0 and "job" in controls.closed_handles
        assert owner.process_handle == controls.handles["process"] and _handle_open(owner.process_handle)
        assert profile.sid and not profile._deleted
        with pytest.raises(contained_host_module.AppContainerError, match="cleanup"):
            host.close()
        assert host._active == {owner} and owner not in host._terminated
        assert profile.sid and not profile._deleted
        controls.recover()
        host.close()
        assert not host._active and owner in host._terminated
        assert owner.process_handle == owner.thread_handle == 0
        assert owner.stdin is owner.stdout is owner.stderr is None
        assert profile._deleted and profile._sid == 0
        _gone(owner.process_id)
        controls.assert_released()
        assert (tmp_path / "host" / "lease-0001").is_dir()
        host.close()
    finally:
        controls.cleanup_red()


@pytest.mark.parametrize("stage", ("pre-job", "partial-conversion", "create-process", "create-pipe"))
def test_pending_raw_stdio_remains_with_original_host_resource_until_retry(tmp_path, monkeypatch, stage):
    controls = _NativeFaults(monkeypatch)
    name = "parent_stdin"
    if stage == "pre-job":
        controls.fail_job = True
    elif stage == "partial-conversion":
        controls.fail_descriptor_at = 3
        name = "parent_stderr"
    elif stage == "create-process":
        controls.block_create = True
    else:
        controls.fail_pipe_at = 2
    controls.block_close = name
    host = WindowsContainedPluginHandsHost()
    launch = _launch("exit 0")
    try:
        result = host.execute(_manager(tmp_path), launch, _invocation(launch))
        assert (result.outcome.status, result.outcome.error_code) == ("unknown", "containment-cleanup-failed")
        assert result.workspace_ref == "plugin-hands-workspace:lease-0001:1"
        assert len(host._active) == len(controls.profiles) == len(controls.stdios) == 1
        resource = next(iter(host._active))
        profile, stdio = controls.profiles[0], controls.stdios[0]
        if stage in ("pre-job", "partial-conversion"):
            assert controls.owners == [resource]
            assert resource._stdio is stdio and resource._contained_profile is profile
            assert resource.process_handle == resource.thread_handle == 0
            _gone(resource.process_id)
        else:
            assert controls.owners == controls.process_ids == []
            assert resource is profile and profile._cleanup_stdio is stdio
        handle = controls.handles[name]
        assert getattr(stdio, name) == handle and _handle_open(handle)
        assert profile.sid and not profile._deleted
        assert all(stream.closed for stream in controls.parent_streams.values())
        with pytest.raises(contained_host_module.AppContainerError, match="cleanup"):
            host.close()
        assert host._active == {resource} and resource not in host._terminated
        assert getattr(stdio, name) == handle and _handle_open(handle)
        assert profile.sid and not profile._deleted
        controls.recover()
        host.close()
        assert not host._active and resource in host._terminated
        assert profile._deleted and profile._sid == 0
        assert not stdio._has_cleanup_resources() and not _handle_open(handle)
        controls.assert_released()
        assert (tmp_path / "host" / "lease-0001").is_dir()
        host.close()
    finally:
        _cleanup_raw_red(controls, name)


@pytest.mark.parametrize("failure", ("limits", "cpu"))
def test_failed_native_job_configuration_keeps_original_host_profile_until_job_close_retry(tmp_path, monkeypatch, failure):
    controls = _NativeFaults(monkeypatch)
    controls.fail_job_information = failure
    controls.block_close = "job"
    host = WindowsContainedPluginHandsHost()
    launch = _launch("exit 0")
    try:
        result = host.execute(_manager(tmp_path), launch, _invocation(launch))
        assert _handle_open(controls.handles["job"])
        assert "job" not in controls.closed_handles
        print(f"OBSERVED failed_host_job={failure} handle={controls.handles['job']} native_valid=True close_true=False active={len(host._active)} profile_deleted={controls.profiles[0]._deleted}")
        assert (result.outcome.status, result.outcome.error_code) == ("unknown", "containment-cleanup-failed")
        assert result.workspace_ref == "plugin-hands-workspace:lease-0001:1"
        assert len(host._active) == len(controls.jobs) == len(controls.owners) == len(controls.profiles) == 1
        owner = next(iter(host._active))
        job, profile = controls.jobs[0], controls.profiles[0]
        assert controls.owners == [owner] and owner._job is job and owner._contained_profile is profile
        handle = controls.handles["job"]
        assert job._handle == handle and _handle_open(handle)
        assert owner.process_handle == owner.thread_handle == 0
        _gone(owner.process_id)
        assert profile.sid and not profile._deleted
        assert ("job", False) in controls.close_events and ("job", True) not in controls.close_events
        with pytest.raises(contained_host_module.AppContainerError, match="cleanup"):
            host.close()
        assert host._active == {owner} and owner not in host._terminated
        assert profile.sid and not profile._deleted
        assert job._handle == handle and _handle_open(handle)
        controls.recover()
        host.close()
        assert not host._active and owner in host._terminated
        assert profile._deleted and profile._sid == 0
        assert job._handle == 0 and not _handle_open(handle)
        assert controls.close_events.count(("job", True)) == 1
        controls.assert_released()
        assert (tmp_path / "host" / "lease-0001").is_dir()
        host.close()
        assert controls.close_events.count(("job", True)) == 1
    finally:
        _cleanup_job_red(controls)
