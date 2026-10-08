from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import time

import psutil
import pytest

from core.storage_provider import SQLiteStructuredRecordStore


ROOT = Path(__file__).resolve().parents[2]
NODE = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "nodejs" / "node.exe"
if not NODE.exists():
    NODE = Path("node.exe")
GATE = ROOT / "tests" / "fixtures" / "plugin_hands_electron_supervisor_gate.cjs"
RECOVERY = ROOT / "tests" / "fixtures" / "plugin_hands_sidecar_recovery_worker.py"
PYTHON = ROOT / "runtime" / "python.exe"


def _same_process_is_alive(pid: int, created_at: float) -> bool:
    try:
        process = psutil.Process(pid)
        return process.is_running() and abs(process.create_time() - created_at) < 0.001
    except psutil.NoSuchProcess:
        return False


def _wait_json(path: Path, process: subprocess.Popen[bytes], timeout: float = 15) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        if process.poll() is not None:
            pytest.fail(f"Electron supervisor gate exited early: {process.returncode}")
        time.sleep(0.05)
    pytest.fail(f"Electron supervisor gate timed out: {path.name}")


@pytest.mark.skipif(os.name != "nt", reason="Windows Electron SidecarSupervisor process-tree Gate")
def test_production_electron_supervisor_stops_python_and_appcontainer_tree_then_recovers(tmp_path: Path) -> None:
    runtime_root = tmp_path / "electron-sidecar-root"
    ready_path = tmp_path / "ready.json"
    stop_path = tmp_path / "stop.signal"
    result_path = tmp_path / "result.json"
    node = subprocess.Popen(
        [str(NODE), str(GATE), str(ROOT), str(runtime_root), str(ready_path), str(stop_path), str(result_path)],
        cwd=str(ROOT), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    driver_pid: int | None = None
    plugin_pid: int | None = None
    driver_created_at: float | None = None
    plugin_created_at: float | None = None
    try:
        ready = _wait_json(ready_path, node)
        session = ready["session"]
        plugin = ready["plugin"]
        requested = ready["requested"]
        assert isinstance(session, dict) and isinstance(plugin, dict) and isinstance(requested, dict)
        driver_pid = int(session["child_pid"])
        plugin_pid = int(plugin["pid"])
        assert session["protocol_version"] == "desktop-loopback/1"
        assert str(session["origin"]).startswith("http://127.0.0.1:")
        assert plugin["is_appcontainer"] is True
        session_port = str(session["origin"]).rsplit(":", 1)[1]
        assert requested["executable"] == str(PYTHON)
        assert requested["args"] == [
            "-m", "backend.api.server", "--host", "127.0.0.1", "--port", session_port,
            "--parent-stdin-watchdog",
        ]
        assert requested["windowsHide"] is True
        assert Path(str(requested["cwd"])) == runtime_root
        driver_created_at = psutil.Process(driver_pid).create_time()
        plugin_created_at = psutil.Process(plugin_pid).create_time()
        assert _same_process_is_alive(driver_pid, driver_created_at)
        assert _same_process_is_alive(plugin_pid, plugin_created_at)
        raw = SQLiteStructuredRecordStore(runtime_root / "records.sqlite3").read(
            "plugin_hands_lifecycles", "invoke-sidecar-0001",
        )
        assert raw is not None and raw.payload["state"] == "fenced"

        stop_path.write_text("stop", encoding="ascii")
        result = _wait_json(result_path, node)
        node.wait(timeout=5)
        assert result == {"status": "stopped", "launch_count": "1"}
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and (
            _same_process_is_alive(driver_pid, driver_created_at)
            or _same_process_is_alive(plugin_pid, plugin_created_at)
        ):
            time.sleep(0.05)
        assert not _same_process_is_alive(driver_pid, driver_created_at)
        assert not _same_process_is_alive(plugin_pid, plugin_created_at)
        crashed = SQLiteStructuredRecordStore(runtime_root / "records.sqlite3").read(
            "plugin_hands_lifecycles", "invoke-sidecar-0001",
        )
        assert crashed is not None and crashed.payload["state"] == "fenced"

        recovery_result = tmp_path / "recovery.json"
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(ROOT / "src")
        recovered = subprocess.run(
            [str(PYTHON), str(RECOVERY), str(runtime_root), str(recovery_result)],
            cwd=str(ROOT), env=environment, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False,
        )
        assert recovered.returncode == 0
        assert json.loads(recovery_result.read_text(encoding="utf-8")) == {
            "count": 1,
            "state": "unknown",
            "workspace_ref": "plugin-hands-workspace:lease-sidecar-0001:1",
            "workspace_exists": True,
        }
        assert (runtime_root / "launch.count").read_text(encoding="ascii") == "1"
    finally:
        if node.poll() is None:
            node.kill()
            node.wait(timeout=5)
        for pid, created_at in ((driver_pid, driver_created_at), (plugin_pid, plugin_created_at)):
            if pid is not None and created_at is not None and _same_process_is_alive(pid, created_at):
                psutil.Process(pid).kill()
