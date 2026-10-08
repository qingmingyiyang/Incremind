from __future__ import annotations

import os
import socket
import sys
import inspect
import ctypes
from pathlib import Path

import pytest
import psutil
import core.plugin_hands.windows_appcontainer as appcontainer_module

from core.plugin_hands.windows_appcontainer import (
    AppContainerCapability,
    AppContainerError,
    AppContainerProfile,
    AppContainerResourceLimits,
    AppContainerLaunchSpec,
    AppContainerUnavailable,
    _SECURITY_CAPABILITIES,
    _command_line,
    _environment_block,
    _require_windows,
    launch_in_appcontainer,
)

LIMITS = AppContainerResourceLimits(256 * 1024 * 1024, 2500)


def _system_environment(workspace: Path, executable: Path) -> dict[str, str]:
    system_names = ("ALLUSERSPROFILE", "APPDATA", "COMSPEC", "HOMEDRIVE", "HOMEPATH", "LOCALAPPDATA", "PATHEXT", "ProgramData", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "PUBLIC", "SystemDrive", "SYSTEMROOT", "USERPROFILE", "WINDIR")
    environment = {name: os.environ[name] for name in system_names if name in os.environ}
    environment.update({"PATH": str(executable.parent), "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "TEMP": str(workspace), "TMP": str(workspace)})
    return environment


def _is_inheritable(stream: object) -> bool:
    import msvcrt

    flags = ctypes.c_ulong()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetHandleInformation.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    kernel32.GetHandleInformation.restype = ctypes.c_int
    assert kernel32.GetHandleInformation(ctypes.c_void_p(msvcrt.get_osfhandle(stream.fileno())), ctypes.byref(flags))
    return bool(flags.value & 1)


def test_host_derived_launch_spec_rejects_relative_missing_and_nul_paths(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="absolute"):
        AppContainerLaunchSpec(Path("python.exe"), (), tmp_path.resolve(), {})
    with pytest.raises(ValueError, match="missing"):
        AppContainerLaunchSpec((tmp_path / "missing.exe").resolve(), (), tmp_path.resolve(), {})
    with pytest.raises(ValueError, match="NUL"):
        AppContainerLaunchSpec(Path(sys.executable).resolve(), ("bad\x00argument",), tmp_path.resolve(), {})


def test_security_capabilities_contract_is_zero_capability() -> None:
    # The launcher must never add internetClient, privateNetworkClientServer,
    # loopback or any other capability SID.
    assert [name for name, _ in _SECURITY_CAPABILITIES._fields_] == ["AppContainerSid", "Capabilities", "CapabilityCount", "Reserved"]


def test_job_assignment_is_ordered_before_first_resume() -> None:
    source = inspect.getsource(launch_in_appcontainer)
    assert source.index("job.assign") < source.index("_resume_suspended_process")
    assert "CREATE_BREAKAWAY_FROM_JOB" not in source


def test_contained_stdio_uses_only_explicit_child_handles() -> None:
    source = inspect.getsource(launch_in_appcontainer)
    assert "_AnonymousStdio.create" in source
    assert "_security_attributes(profile.sid, stdio.child_handle_list())" in source
    assert "dwFlags = _STARTF_USESTDHANDLES" in source
    assert "True, flags" in source  # CreateProcessW bInheritHandles
    assert source.index("stdio.close_child") < source.index("_resume_suspended_process")
    assert source.index("stdio.parent_files") < source.index("_resume_suspended_process")
    assert "hStdError = stdio.child_stderr" in source
    assert "hStdError = stdio.child_stdout" not in source


def test_command_line_and_environment_are_single_host_derived_blocks() -> None:
    command = _command_line(Path(r"C:\Program Files\worker.exe"), ("two words", 'quote"inside'))
    assert command.startswith('"C:\\Program Files\\worker.exe"')
    assert "two words" in command
    block = _environment_block({"TEMP": r"C:\workspace", "LANG": "C"})
    assert "".join(block[:]) == "LANG=C\0TEMP=C:\\workspace\0\0"


def test_non_windows_is_strictly_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("core.plugin_hands.windows_appcontainer.probe_appcontainer", lambda: AppContainerCapability(False, "windows-required"))
    with pytest.raises(AppContainerUnavailable, match="windows-required"):
        _require_windows()


@pytest.mark.skipif(os.name != "nt", reason="Windows AppContainer native probe")
def test_real_machine_reports_required_native_api_surface() -> None:
    # This is only a capability probe.  It does not claim that workspace ACL,
    # managed-artifact attestation, or network-denial evidence has passed.
    from core.plugin_hands.windows_appcontainer import probe_appcontainer

    assert probe_appcontainer() == AppContainerCapability(True)


@pytest.mark.skipif(os.name != "nt", reason="Windows AppContainer profile probe")
def test_real_profile_launch_succeeds_with_appcontainer_token(tmp_path: Path) -> None:
    comspec = Path(os.environ["COMSPEC"]).resolve()
    environment = _system_environment(tmp_path, comspec)
    spec = AppContainerLaunchSpec(comspec, ("/d", "/c", "exit 0"), tmp_path.resolve(), environment)
    with AppContainerProfile.create() as profile:
        assert profile.sid
        process = launch_in_appcontainer(spec, profile, resource_limits=LIMITS)
        try:
            assert process.is_appcontainer() is True
            assert process.resource_limits() == LIMITS
            assert process.wait(5) is not None
        finally:
            process.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows AppContainer stdio probe")
def test_real_contained_stdio_round_trip_and_close(tmp_path: Path) -> None:
    comspec = Path(os.environ["COMSPEC"]).resolve()
    spec = AppContainerLaunchSpec(
        comspec,
        ("/d", "/v:on", "/c", "set /p value=& echo received:!value!"),
        tmp_path.resolve(),
        _system_environment(tmp_path, comspec),
    )
    with AppContainerProfile.create() as profile:
        process = launch_in_appcontainer(spec, profile, resource_limits=LIMITS)
        try:
            assert _is_inheritable(process.stdin) is False
            assert _is_inheritable(process.stdout) is False
            process.stdin.write(b"contained-stdin\r\n")
            process.stdin.flush()
            process.stdin.close()
            assert process.stdout.readline() == b"received:contained-stdin\r\n"
            assert process.wait(5) == 0
        finally:
            process.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows AppContainer resume fault injection")
def test_resume_failure_closes_every_handle_and_never_runs_child(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    marker = tmp_path / "must-not-exist.txt"
    comspec = Path(os.environ["COMSPEC"]).resolve()
    spec = AppContainerLaunchSpec(comspec, ("/d", "/c", "echo escaped > " + str(marker)), tmp_path.resolve(), _system_environment(tmp_path, comspec))
    process = psutil.Process()
    baseline_handles = process.num_handles()

    def fail_resume(*_args) -> None:
        raise AppContainerError("injected ResumeThread failure")

    monkeypatch.setattr(appcontainer_module, "_resume_suspended_process", fail_resume)
    with AppContainerProfile.create() as profile:
        with pytest.raises(AppContainerError, match="injected"):
            launch_in_appcontainer(spec, profile, resource_limits=LIMITS)
    assert not marker.exists()
    assert process.num_handles() == baseline_handles


@pytest.mark.skipif(os.name != "nt", reason="Windows AppContainer pipe conversion fault injection")
def test_parent_pipe_conversion_failure_occurs_while_suspended_and_closes_handles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    marker = tmp_path / "must-not-exist.txt"
    comspec = Path(os.environ["COMSPEC"]).resolve()
    spec = AppContainerLaunchSpec(comspec, ("/d", "/c", "echo escaped > " + str(marker)), tmp_path.resolve(), _system_environment(tmp_path, comspec))
    process = psutil.Process()
    baseline_handles = process.num_handles()

    def fail_parent_files(_self):
        raise AppContainerError("injected parent pipe conversion failure")

    monkeypatch.setattr(appcontainer_module._AnonymousStdio, "parent_files", fail_parent_files)
    with AppContainerProfile.create() as profile:
        with pytest.raises(AppContainerError, match="injected"):
            launch_in_appcontainer(spec, profile, resource_limits=LIMITS)
    assert not marker.exists()
    assert process.num_handles() == baseline_handles


@pytest.mark.skipif(os.name != "nt", reason="Windows AppContainer descriptor transfer fault injection")
@pytest.mark.parametrize("fail_on_call", (1, 2, 3))
def test_each_parent_descriptor_transfer_failure_preserves_raw_handle_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_on_call: int) -> None:
    marker = tmp_path / "must-not-exist.txt"
    comspec = Path(os.environ["COMSPEC"]).resolve()
    spec = AppContainerLaunchSpec(comspec, ("/d", "/c", "echo escaped > " + str(marker)), tmp_path.resolve(), _system_environment(tmp_path, comspec))
    process = psutil.Process()
    baseline_handles = process.num_handles()
    original = appcontainer_module._descriptor_from_handle
    calls = 0

    def fail_selected(handle: int, *, write: bool) -> int:
        nonlocal calls
        calls += 1
        if calls == fail_on_call:
            raise OSError("injected descriptor transfer failure")
        return original(handle, write=write)

    monkeypatch.setattr(appcontainer_module, "_descriptor_from_handle", fail_selected)
    with AppContainerProfile.create() as profile:
        with pytest.raises(OSError, match="injected"):
            launch_in_appcontainer(spec, profile, resource_limits=LIMITS)
    assert not marker.exists()
    assert process.num_handles() == baseline_handles


@pytest.mark.skipif(os.name != "nt", reason="Windows AppContainer network probe")
def test_real_zero_capability_appcontainer_denies_loopback(tmp_path: Path) -> None:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    executable = Path(os.environ["SYSTEMROOT"]) / "System32" / "curl.exe"
    spec = AppContainerLaunchSpec(executable.resolve(), ("--silent", "--max-time", "1", f"http://127.0.0.1:{port}/"), tmp_path.resolve(), _system_environment(tmp_path, executable))
    try:
        with AppContainerProfile.create() as profile:
            process = launch_in_appcontainer(spec, profile, resource_limits=LIMITS)
            try:
                assert process.is_appcontainer() is True
                assert process.wait(5) != 0
            finally:
                process.close()
        listener.settimeout(0.1)
        with pytest.raises((TimeoutError, socket.timeout)):
            listener.accept()
    finally:
        listener.close()
