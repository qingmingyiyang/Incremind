from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import subprocess
import time

import psutil
import pytest


ROOT = Path(__file__).resolve().parents[2]
NODE = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "nodejs" / "node.exe"
if not NODE.exists():
    NODE = Path("node.exe")
GATE = ROOT / "tests" / "fixtures" / "plugin_hands_packaged_vertical_gate.cjs"
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TOKEN_QUERY = 0x0008
TOKEN_IS_APP_CONTAINER = 29


def _alive(pid: int, created_at: float) -> bool:
    try:
        process = psutil.Process(pid)
        return process.is_running() and abs(process.create_time() - created_at) < 0.001
    except psutil.NoSuchProcess:
        return False


def _is_appcontainer(pid: int) -> bool:
    process = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not process:
        return False
    token = ctypes.c_void_p()
    try:
        if not ctypes.windll.advapi32.OpenProcessToken(process, TOKEN_QUERY, ctypes.byref(token)):
            return False
        value = ctypes.c_uint32()
        returned = ctypes.c_uint32()
        return bool(
            ctypes.windll.advapi32.GetTokenInformation(
                token,
                TOKEN_IS_APP_CONTAINER,
                ctypes.byref(value),
                ctypes.sizeof(value),
                ctypes.byref(returned),
            )
            and value.value
        )
    finally:
        if token.value:
            ctypes.windll.kernel32.CloseHandle(token)
        ctypes.windll.kernel32.CloseHandle(process)


def _diagnostic_tail(process: subprocess.Popen[bytes], result_path: Path) -> str:
    """Return a bounded fixture-only diagnostic; never persists runtime data."""

    try:
        _stdout, stderr = process.communicate(timeout=2)
    except subprocess.TimeoutExpired:
        return "fixture diagnostic unavailable"
    text = (stderr or b"").decode("utf-8", errors="replace")
    if result_path.exists():
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
            text = f"{text} {result.get('error', '')}"
        except (OSError, ValueError, TypeError):
            pass
    text = text.replace("\r", " ").replace("\n", " ")
    return text[-2000:] or "fixture exited without stderr"


def _wait(path: Path, process: subprocess.Popen[bytes], timeout: float = 75) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        if process.poll() is not None:
            pytest.fail(
                f"packaged vertical fixture exited early: {process.returncode}; "
                f"{_diagnostic_tail(process, path.with_name('result.json'))}"
            )
        time.sleep(0.05)
    pytest.fail("packaged vertical fixture timed out")


def _wait_for_appcontainer_child(parent_pid: int, timeout: float = 15) -> psutil.Process:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            descendants = psutil.Process(parent_pid).children(recursive=True)
        except psutil.NoSuchProcess:
            break
        for child in descendants:
            if _is_appcontainer(child.pid):
                return child
        time.sleep(0.05)
    pytest.fail("packaged sidecar did not own a live AppContainer Hand")


@pytest.mark.skipif(os.name != "nt", reason="Windows packaged Plugin Hands vertical Gate")
def test_packaged_plugin_hand_http_vertical_recovers_unknown_without_replay(tmp_path: Path) -> None:
    app_data = tmp_path / "appData"
    ready_path, stop_path, result_path = tmp_path / "ready.json", tmp_path / "stop.signal", tmp_path / "result.json"
    process = subprocess.Popen([str(NODE), str(GATE), str(ROOT), str(app_data), str(ready_path), str(stop_path), str(result_path)], cwd=str(ROOT), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    identities: list[tuple[int, float]] = []
    try:
        ready = _wait(ready_path, process)
        sidecar = ready["sidecar"]
        assert isinstance(sidecar, dict)
        hand = _wait_for_appcontainer_child(int(sidecar["pid"]))
        for value in (sidecar["pid"], hand.pid):
            identities.append((int(value), psutil.Process(int(value)).create_time()))
        assert isinstance(ready["attempt_id"], str) and ready["attempt_id"]
        assert all(_alive(pid, created) for pid, created in identities)
        assert _is_appcontainer(hand.pid)
        stop_path.write_text("stop", encoding="ascii")
        result = _wait(result_path, process)
        process.wait(timeout=10)
        assert result.get("status") == "ok", result
        assert result["before_state"] == "fenced"
        assert result["after_state"] == "unknown"
        assert result["workspace_retention"] == "retained"
        assert result["started_count"] == 1
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and any(_alive(pid, created) for pid, created in identities):
            time.sleep(0.05)
        assert not any(_alive(pid, created) for pid, created in identities)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        for pid, created in identities:
            if _alive(pid, created):
                psutil.Process(pid).kill()
