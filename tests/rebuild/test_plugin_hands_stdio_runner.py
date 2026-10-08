from __future__ import annotations

import json
import io
import os
import sys
import time
from pathlib import Path

import psutil
import pytest
import core.plugin_hands.stdio_runner as stdio_runner

from core.plugin_hands.contracts import PluginHandsControl, PluginHandsInvocation, PluginHandsLaunch, PluginHandsLease
from core.plugin_hands.stdio_runner import MAX_FRAME_BYTES, PluginHandsProtocolExchange, PluginHandsStdioRunner
from core.plugin_hands.workspace import PluginHandsWorkspaceManager


def _lease() -> PluginHandsLease:
    return PluginHandsLease("lease-0001", "invoke-0001", 1, "project-0001", "turn-0001", 1, "recipe-0001", (), "2099-08-26T00:00:00Z")


def _launch(body: str) -> PluginHandsLaunch:
    return PluginHandsLaunch("launch-0001", Path(sys.executable).resolve(), ("-u", "-c", body))


def _invocation(launch: PluginHandsLaunch, lease: PluginHandsLease, *, deadline_ms: int = 1000) -> PluginHandsInvocation:
    return PluginHandsInvocation("invoke-0001", "plugin-0001", launch, lease, deadline_ms, {"task": "fixture"})


def _workspace(root: Path, lease: PluginHandsLease):
    root.mkdir(parents=True)
    manager = PluginHandsWorkspaceManager(root)
    return manager, manager.create(lease)


def _child(terminal: str) -> str:
    return "import json,sys\nhello={'protocol':'plugin-hands/1','type':'hello','launch_id':'launch-0001','lease_id':'lease-0001','invocation_id':'invoke-0001'}\nprint(json.dumps(hello), flush=True)\nsys.stdin.readline()\n" + terminal


def _run(tmp_path: Path, body: str, *, deadline_ms: int = 1000, control: PluginHandsControl = PluginHandsControl()):
    launch, lease = _launch(body), _lease()
    invocation = _invocation(launch, lease, deadline_ms=deadline_ms)
    manager, workspace = _workspace(tmp_path / "host", lease)
    outcome = PluginHandsStdioRunner().run(launch, workspace, invocation, control)
    return outcome, manager, workspace


class _RecordingInput(io.BytesIO):
    def close(self) -> None:
        self.flush()


class _FixtureTransport:
    def __init__(self, output: bytes) -> None:
        self.stdin = _RecordingInput()
        self.stdout = io.BytesIO(output)
        self.wait_calls: list[float | None] = []

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls.append(timeout)
        return 0


def test_protocol_exchange_is_reusable_with_a_transport_neutral_process_surface() -> None:
    launch, lease = _launch("pass"), _lease()
    invocation = _invocation(launch, lease)
    hello = {"protocol": "plugin-hands/1", "type": "hello", "launch_id": launch.launch_id, "lease_id": lease.lease_id, "invocation_id": invocation.invocation_id}
    result = {**hello, "type": "result", "output": {"ok": True}}
    transport = _FixtureTransport((json.dumps(hello) + "\n" + json.dumps(result) + "\n").encode())

    outcome = PluginHandsProtocolExchange().exchange(transport, launch, invocation, time.monotonic() + 1, PluginHandsControl())

    assert outcome.status == "success"
    assert outcome.output == {"ok": True}
    assert json.loads(transport.stdin.getvalue()) == {"protocol": "plugin-hands/1", "type": "invoke", "launch_id": launch.launch_id, "lease_id": lease.lease_id, "invocation_id": invocation.invocation_id, "input": {"task": "fixture"}}
    assert transport.wait_calls


def test_real_child_success_uses_workspace_cwd_and_minimal_environment(tmp_path: Path) -> None:
    body = _child("import os; print(json.dumps({'protocol':'plugin-hands/1','type':'result','launch_id':'launch-0001','lease_id':'lease-0001','invocation_id':'invoke-0001','output':{'ok':True,'cwd':os.getcwd(),'home':os.environ.get('HOME'),'temp':os.environ['TEMP']}}), flush=True)")
    outcome, manager, workspace = _run(tmp_path, body)
    assert outcome.status == "success"
    assert outcome.output == {"ok": True, "cwd": str(workspace.root), "home": None, "temp": str(workspace.root)}
    assert manager.dispose(workspace, outcome) is None


def test_real_child_constructs_hello_from_host_reserved_protocol_identity_environment(tmp_path: Path) -> None:
    body = (
        "import json,os,sys\n"
        "identity={'launch_id':os.environ['CHRIPTMAS_PLUGIN_HANDS_LAUNCH_ID'],'lease_id':os.environ['CHRIPTMAS_PLUGIN_HANDS_LEASE_ID'],'invocation_id':os.environ['CHRIPTMAS_PLUGIN_HANDS_INVOCATION_ID']}\n"
        "hello={'protocol':'plugin-hands/1','type':'hello',**identity}\n"
        "print(json.dumps(hello),flush=True)\n"
        "sys.stdin.readline()\n"
        "print(json.dumps({**hello,'type':'result','output':identity}),flush=True)\n"
    )
    outcome, manager, workspace = _run(tmp_path, body)
    assert outcome.status == "success"
    assert outcome.output == {
        "launch_id": "launch-0001",
        "lease_id": "lease-0001",
        "invocation_id": "invoke-0001",
    }
    assert manager.dispose(workspace, outcome) is None


def test_spawn_failure_is_failed_before_any_effect(tmp_path: Path) -> None:
    launch = PluginHandsLaunch("launch-0001", (tmp_path / "missing.exe").resolve(), ())
    lease = _lease()
    invocation = _invocation(launch, lease)
    _, workspace = _workspace(tmp_path / "host", lease)
    outcome = PluginHandsStdioRunner().run(launch, workspace, invocation)
    assert (outcome.status, outcome.error_code) == ("failed", "spawn-failed")


def test_expired_lease_and_non_json_input_fail_before_spawn(tmp_path: Path) -> None:
    launch = PluginHandsLaunch("launch-0001", (tmp_path / "missing.exe").resolve(), ())
    expired = PluginHandsLease("lease-0001", "invoke-0001", 1, "project-0001", "turn-0001", 1, "recipe-0001", (), "2020-01-01T00:00:00Z")
    _, expired_workspace = _workspace(tmp_path / "expired", expired)
    expired_outcome = PluginHandsStdioRunner().run(launch, expired_workspace, _invocation(launch, expired))
    assert (expired_outcome.status, expired_outcome.error_code) == ("failed", "invalid-invocation")

    lease = _lease()
    _, invalid_workspace = _workspace(tmp_path / "invalid", lease)
    invocation = PluginHandsInvocation("invoke-0001", "plugin-0001", launch, lease, 1000, {"bad": object()})
    invalid_outcome = PluginHandsStdioRunner().run(launch, invalid_workspace, invocation)
    assert (invalid_outcome.status, invalid_outcome.error_code) == ("failed", "invalid-invocation")


def test_timeout_and_cancel_are_unknown_after_spawn(tmp_path: Path) -> None:
    body = _child("import time; time.sleep(10)")
    timed_out, manager, workspace = _run(tmp_path / "timeout", body, deadline_ms=100)
    assert (timed_out.status, timed_out.error_code) == ("unknown", "deadline-exceeded")
    assert manager.dispose(workspace, timed_out) == workspace.host_ref
    cancelled, _, _ = _run(tmp_path / "cancel", body, control=PluginHandsControl(is_cancelled=lambda: True))
    assert (cancelled.status, cancelled.error_code) == ("failed", "cancelled-before-spawn")


def test_pre_cancel_never_spawns_and_post_spawn_cancel_is_unknown(tmp_path: Path) -> None:
    marker = tmp_path / "spawned.txt"
    body = "from pathlib import Path; Path(" + repr(str(marker)) + ").write_text('spawned'); import time; time.sleep(10)"
    outcome, _, _ = _run(tmp_path / "pre", body, control=PluginHandsControl(is_cancelled=lambda: True))
    assert (outcome.status, outcome.error_code) == ("failed", "cancelled-before-spawn")
    assert not marker.exists()

    checks = 0

    def cancel_after_spawn() -> bool:
        nonlocal checks
        checks += 1
        return checks > 1

    outcome, _, _ = _run(tmp_path / "post", _child("import time; time.sleep(10)"), control=PluginHandsControl(is_cancelled=cancel_after_spawn))
    assert (outcome.status, outcome.error_code) == ("unknown", "cancelled")


def test_child_terminal_error_is_a_safe_failed_outcome(tmp_path: Path) -> None:
    body = _child("print(json.dumps({'protocol':'plugin-hands/1','type':'error','launch_id':'launch-0001','lease_id':'lease-0001','invocation_id':'invoke-0001','code':'private-child-detail'}), flush=True)")
    outcome, _, _ = _run(tmp_path, body)
    assert (outcome.status, outcome.error_code) == ("unknown", "child-error")


def test_malformed_oversize_and_wrong_identity_are_unknown(tmp_path: Path) -> None:
    malformed, _, _ = _run(tmp_path / "malformed", _child("print('{bad json', flush=True)"))
    assert (malformed.status, malformed.error_code) == ("unknown", "protocol-error")
    unknown = "import json\nprint(json.dumps({'protocol':'plugin-hands/1','type':'hello','launch_id':'launch-0001','lease_id':'lease-0001','invocation_id':'invoke-0001','unexpected':True}), flush=True)"
    unknown_outcome, _, _ = _run(tmp_path / "unknown", unknown)
    assert unknown_outcome.error_code == "protocol-error"
    duplicate = "print('{\\\"protocol\\\":\\\"plugin-hands/1\\\",\\\"type\\\":\\\"hello\\\",\\\"type\\\":\\\"hello\\\",\\\"launch_id\\\":\\\"launch-0001\\\",\\\"lease_id\\\":\\\"lease-0001\\\",\\\"invocation_id\\\":\\\"invoke-0001\\\"}', flush=True)"
    duplicate_outcome, _, _ = _run(tmp_path / "duplicate", duplicate)
    assert duplicate_outcome.error_code == "protocol-error"
    oversized, _, _ = _run(tmp_path / "oversize", _child("sys.stdout.write('x' * " + str(MAX_FRAME_BYTES) + " + '\\n'); sys.stdout.flush()"))
    assert oversized.error_code == "output-oversize"
    wrong, _, _ = _run(tmp_path / "wrong", _child("print(json.dumps({'protocol':'plugin-hands/1','type':'result','launch_id':'launch-0001','lease_id':'other-0001','invocation_id':'invoke-0001','output':{}}), flush=True)"))
    assert (wrong.status, wrong.error_code) == ("unknown", "identity-mismatch")

    non_finite, _, _ = _run(tmp_path / "non-finite", _child("print('{\"protocol\":\"plugin-hands/1\",\"type\":\"result\",\"launch_id\":\"launch-0001\",\"lease_id\":\"lease-0001\",\"invocation_id\":\"invoke-0001\",\"output\":{\"value\":NaN}}', flush=True)"))
    assert (non_finite.status, non_finite.error_code) == ("unknown", "protocol-error")


def test_descendant_cleanup_uses_real_python_child(tmp_path: Path) -> None:
    pid_file = tmp_path / "descendant.pid"
    body = _child("import subprocess,time,pathlib; child=subprocess.Popen([sys.executable,'-u','-c','import time; time.sleep(30)']); pathlib.Path(" + repr(str(pid_file)) + ").write_text(str(child.pid)); time.sleep(30)")
    outcome, _, _ = _run(tmp_path / "descendant", body, deadline_ms=500)
    assert outcome.error_code == "deadline-exceeded"
    pid = int(pid_file.read_text())
    time.sleep(0.1)
    assert not psutil.pid_exists(pid)


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object behavior")
def test_windows_job_close_fences_descendant_without_psutil_tree_walk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pid_file = tmp_path / "job-descendant.pid"
    body = _child("import subprocess,time,pathlib; child=subprocess.Popen([sys.executable,'-u','-c','import time; time.sleep(30)']); pathlib.Path(" + repr(str(pid_file)) + ").write_text(str(child.pid)); time.sleep(30)")

    def terminate_root_only(process) -> None:
        process.kill()
        process.wait(timeout=1)

    monkeypatch.setattr(stdio_runner, "_terminate_tree", terminate_root_only)
    outcome, _, _ = _run(tmp_path / "job", body, deadline_ms=500)
    assert outcome.error_code == "deadline-exceeded"
    pid = int(pid_file.read_text())
    time.sleep(0.1)
    assert not psutil.pid_exists(pid)
