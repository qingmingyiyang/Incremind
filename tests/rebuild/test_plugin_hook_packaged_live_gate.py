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
GATE = ROOT / "tests" / "fixtures" / "plugin_hook_packaged_live_gate.cjs"
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
        value, returned = ctypes.c_uint32(), ctypes.c_uint32()
        return bool(ctypes.windll.advapi32.GetTokenInformation(token, TOKEN_IS_APP_CONTAINER, ctypes.byref(value), ctypes.sizeof(value), ctypes.byref(returned)) and value.value)
    finally:
        if token.value:
            ctypes.windll.kernel32.CloseHandle(token)
        ctypes.windll.kernel32.CloseHandle(process)


def _wait_json(path: Path, process: subprocess.Popen[bytes], timeout: float = 75) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        if process.poll() is not None:
            stderr = (process.communicate(timeout=2)[1] or b"").decode("utf-8", errors="replace")[-2000:]
            result = path.with_name("result.json")
            if result.exists():
                stderr = f"{stderr} {result.read_text(encoding='utf-8')[-2000:]}"
            pytest.fail(f"packaged Hook fixture exited early: {process.returncode}; {stderr}")
        time.sleep(0.05)
    pytest.fail("packaged Hook fixture timed out")


@pytest.mark.skipif(os.name != "nt", reason="Windows packaged Plugin Hook live Gate")
def test_packaged_plugin_hook_denies_disables_and_stays_disabled_after_restart(tmp_path: Path) -> None:
    app_data = tmp_path / "appData"
    ready_path, result_path = tmp_path / "ready.json", tmp_path / "result.json"
    process = subprocess.Popen([str(NODE), str(GATE), str(ROOT), str(app_data), str(ready_path), str(result_path)], cwd=str(ROOT), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    identities: list[tuple[int, float]] = []
    try:
        ready = _wait_json(ready_path, process)
        sidecar_pid = int(ready["sidecar_pid"])
        identities.append((sidecar_pid, psutil.Process(sidecar_pid).create_time()))
        deadline = time.monotonic() + 15
        observed_appcontainer = False
        while time.monotonic() < deadline:
            try:
                for item in psutil.Process(sidecar_pid).children(recursive=True):
                    if _is_appcontainer(item.pid):
                        identities.append((item.pid, item.create_time()))
                        observed_appcontainer = True
                        break
            except psutil.NoSuchProcess:
                break
            if observed_appcontainer:
                break
            time.sleep(0.02)
        assert observed_appcontainer, "packaged sidecar did not execute the Hook in AppContainer"
        result = _wait_json(result_path, process)
        restarted_pid = int(result["sidecar_pid"])
        if all(pid != restarted_pid for pid, _created in identities):
            try:
                identities.append((restarted_pid, psutil.Process(restarted_pid).create_time()))
            except psutil.NoSuchProcess:
                pass
        process.wait(timeout=10)
        assert result["status"] == "ok", result
        assert result["denied_hook_receipts"] == 1
        assert result["denied_handler"] == "plugin.packaged-hook-plugin.pre-tool-policy"
        assert result["private_lifecycle_absent"] is True
        assert isinstance(result["disabled_revision"], int) and result["disabled_revision"] > 0
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and any(_alive(pid, created) for pid, created in identities):
            time.sleep(0.05)
        assert identities and not any(_alive(pid, created) for pid, created in identities)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
