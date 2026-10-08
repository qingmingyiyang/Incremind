"""Windows production Gate for supervised sidecar parent-pipe liveness."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import time

import psutil
import pytest


ROOT = Path(__file__).resolve().parents[2]
NODE = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "nodejs" / "node.exe"
if not NODE.exists():
    NODE = Path("node.exe")
GATE = ROOT / "tests" / "fixtures" / "electron_sidecar_parent_liveness_gate.cjs"


def _same_process_is_alive(pid: int, created_at: float) -> bool:
    try:
        process = psutil.Process(pid)
        return process.is_running() and abs(process.create_time() - created_at) < 0.001
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def _wait_json(path: Path, owner: subprocess.Popen[bytes], timeout: float = 15) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        if owner.poll() is not None:
            pytest.fail(f"parent-liveness fixture exited early: {owner.returncode}")
        time.sleep(0.05)
    pytest.fail(f"parent-liveness fixture timed out: {path.name}")


def _wait_process_exit(pid: int, created_at: float, timeout: float = 8) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _same_process_is_alive(pid, created_at):
            return
        time.sleep(0.05)
    pytest.fail(f"orphaned_sidecar_after_parent_kill:{pid}")


@pytest.mark.skipif(os.name != "nt", reason="Windows Electron sidecar parent-pipe Gate")
def test_taskkill_owner_without_tree_stops_sidecar_and_same_root_can_restart(tmp_path: Path) -> None:
    runtime_root = tmp_path / "sidecar-root"
    (runtime_root / "config").mkdir(parents=True)
    shutil.copyfile(ROOT / "config" / "settings.toml", runtime_root / "config" / "settings.toml")
    first_ready = tmp_path / "first-ready.json"
    first_stop = tmp_path / "first-stop.signal"
    first_result = tmp_path / "first-result.json"
    owner = subprocess.Popen(
        [str(NODE), str(GATE), str(ROOT), str(runtime_root), str(first_ready), str(first_stop), str(first_result)],
        cwd=str(ROOT), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    fresh: subprocess.Popen[bytes] | None = None
    first_sidecar_pid: int | None = None
    first_sidecar_created_at: float | None = None
    fresh_sidecar_pid: int | None = None
    fresh_sidecar_created_at: float | None = None
    try:
        first = _wait_json(first_ready, owner)
        session = first["session"]
        assert isinstance(session, dict)
        first_sidecar_pid = int(session["child_pid"])
        first_sidecar = psutil.Process(first_sidecar_pid)
        first_sidecar_created_at = first_sidecar.create_time()
        assert _same_process_is_alive(first_sidecar_pid, first_sidecar_created_at)

        # Deliberately omit /T: EOF on the supervised private stdin pipe must
        # request the sidecar's normal ASGI shutdown on its own.
        subprocess.run(["taskkill", "/PID", str(owner.pid), "/F"], check=True, capture_output=True, timeout=5)
        owner.wait(timeout=5)
        # Windows exposes the exit status through the already-open process
        # handle even though the sidecar is not this pytest process's child.
        # Zero distinguishes the watchdog-triggered Uvicorn/ASGI shutdown from
        # a taskkill-style forced termination.
        assert first_sidecar.wait(timeout=8) == 0
        _wait_process_exit(first_sidecar_pid, first_sidecar_created_at)

        fresh_ready = tmp_path / "fresh-ready.json"
        fresh_stop = tmp_path / "fresh-stop.signal"
        fresh_result = tmp_path / "fresh-result.json"
        fresh = subprocess.Popen(
            [str(NODE), str(GATE), str(ROOT), str(runtime_root), str(fresh_ready), str(fresh_stop), str(fresh_result)],
            cwd=str(ROOT), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        restarted = _wait_json(fresh_ready, fresh)
        restarted_session = restarted["session"]
        assert isinstance(restarted_session, dict)
        fresh_sidecar_pid = int(restarted_session["child_pid"])
        fresh_sidecar_created_at = psutil.Process(fresh_sidecar_pid).create_time()
        assert (fresh_sidecar_pid, fresh_sidecar_created_at) != (
            first_sidecar_pid, first_sidecar_created_at
        )
        assert _same_process_is_alive(fresh_sidecar_pid, fresh_sidecar_created_at)

        fresh_stop.write_text("stop", encoding="ascii")
        assert _wait_json(fresh_result, fresh) == {"status": "stopped"}
        fresh.wait(timeout=5)
        _wait_process_exit(fresh_sidecar_pid, fresh_sidecar_created_at)
    finally:
        for owner_process in (owner, fresh):
            if owner_process is not None and owner_process.poll() is None:
                owner_process.kill()
                owner_process.wait(timeout=5)
        for pid, created_at in (
            (first_sidecar_pid, first_sidecar_created_at),
            (fresh_sidecar_pid, fresh_sidecar_created_at),
        ):
            if pid is not None and created_at is not None and _same_process_is_alive(pid, created_at):
                psutil.Process(pid).kill()
