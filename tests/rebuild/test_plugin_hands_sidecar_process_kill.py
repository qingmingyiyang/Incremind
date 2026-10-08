from __future__ import annotations

import os
import json
from pathlib import Path
import subprocess
import time

import psutil
import pytest

from core.storage_provider import SQLiteStructuredRecordStore


ROOT = Path(__file__).resolve().parents[2]
WORKER = ROOT / "tests" / "fixtures" / "plugin_hands_sidecar_crash_worker.py"
RECOVERY_WORKER = ROOT / "tests" / "fixtures" / "plugin_hands_sidecar_recovery_worker.py"
PYTHON = ROOT / "runtime" / "python.exe"


def _record(root: Path):
    return SQLiteStructuredRecordStore(root / "records.sqlite3").read(
        "plugin_hands_lifecycles", "invoke-sidecar-0001",
    )


def _same_process_is_alive(pid: int, created_at: float) -> bool:
    try:
        process = psutil.Process(pid)
        return process.is_running() and abs(process.create_time() - created_at) < 0.001
    except psutil.NoSuchProcess:
        return False


@pytest.mark.skipif(os.name != "nt", reason="Windows sidecar process-kill and AppContainer Gate")
def test_parent_pid_kill_closes_appcontainer_job_and_fresh_recovery_is_unknown(tmp_path: Path) -> None:
    root = tmp_path / "sidecar-root"
    root.mkdir()
    pid_path = root / "child.pid"
    launch_count_path = root / "launch.count"
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT / "src")
    worker = subprocess.Popen(
        [str(PYTHON), str(WORKER), str(root), str(pid_path), str(launch_count_path)],
        cwd=str(ROOT), env=environment, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    child_pid: int | None = None
    child_created_at: float | None = None
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if worker.poll() is not None:
                pytest.fail(f"sidecar fixture exited before spawn: {worker.returncode}")
            raw = _record(root)
            if pid_path.exists() and raw is not None and raw.payload.get("state") == "fenced":
                process_fact = json.loads(pid_path.read_text(encoding="ascii"))
                assert process_fact == {"pid": process_fact["pid"], "is_appcontainer": True}
                child_pid = int(process_fact["pid"])
                child_created_at = psutil.Process(child_pid).create_time()
                break
            time.sleep(0.05)
        assert child_pid is not None and child_created_at is not None
        assert launch_count_path.read_text(encoding="ascii") == "1"
        assert _same_process_is_alive(child_pid, child_created_at)

        # Popen.kill uses TerminateProcess on Windows and targets only this
        # parent PID. It deliberately does not use taskkill /T.
        worker.kill()
        worker.wait(timeout=5)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and _same_process_is_alive(child_pid, child_created_at):
            time.sleep(0.05)
        assert not _same_process_is_alive(child_pid, child_created_at)
        crashed = _record(root)
        assert crashed is not None and crashed.payload["state"] == "fenced"

        recovery_result = root / "recovery.json"
        recovered_process = subprocess.run(
            [str(PYTHON), str(RECOVERY_WORKER), str(root), str(recovery_result)],
            cwd=str(ROOT), env=environment, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
            check=False,
        )
        assert recovered_process.returncode == 0
        assert json.loads(recovery_result.read_text(encoding="utf-8")) == {
            "count": 1,
            "state": "unknown",
            "workspace_ref": "plugin-hands-workspace:lease-sidecar-0001:1",
            "workspace_exists": True,
        }
        assert launch_count_path.read_text(encoding="ascii") == "1"
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.wait(timeout=5)
        if child_pid is not None and child_created_at is not None and _same_process_is_alive(child_pid, child_created_at):
            psutil.Process(child_pid).kill()
