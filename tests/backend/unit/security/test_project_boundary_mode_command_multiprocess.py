from __future__ import annotations

import multiprocessing
from pathlib import Path

from backend.security.project_boundary_mode_command import ProjectBoundaryModeCommandService
from backend.security.project_boundary_profiles import ProjectBoundaryProfileStore


def _submit_mode(root: str, command_id: str, mode: str, ready, start, results) -> None:
    ready.put(True)
    start.wait(10)
    service = None
    try:
        service = ProjectBoundaryModeCommandService(Path(root))
        receipt = service.submit(
            project_id="alpha", command_id=command_id, mode=mode,
            expected_boundary_revision=1, expected_capability_revision=1,
        )
        results.put(("receipt", receipt.status, receipt.boundary_revision, receipt.capability_revision))
    except Exception as error:
        results.put(("error", type(error).__name__, str(error)))
    finally:
        if service is not None:
            service.close()


def _set_boundary(root: str, mode: str, ready, start, results) -> None:
    ready.put(True)
    start.wait(10)
    try:
        profile = ProjectBoundaryProfileStore(Path(root)).set_mode(
            "alpha", mode=mode, remote_default="allow" if mode == "open" else "deny",
            expected_revision=1,
        ).profile
        results.put(("written", profile.mode, profile.revision))
    except Exception as error:
        results.put(("error", type(error).__name__, str(error)))


def _run_pair(tmp_path: Path, target, arguments: tuple[tuple[object, ...], tuple[object, ...]]) -> list[tuple]:
    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    results = context.Queue()
    start = context.Event()
    processes = [
        context.Process(target=target, args=(str(tmp_path), *args, ready, start, results))
        for args in arguments
    ]
    for process in processes:
        process.start()
    assert ready.get(timeout=15) is True
    assert ready.get(timeout=15) is True
    start.set()
    output = [results.get(timeout=20), results.get(timeout=20)]
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0
    return output


def test_cross_process_profile_cas_allows_exactly_one_revision_two_write(tmp_path: Path) -> None:
    output = _run_pair(tmp_path, _set_boundary, (("open",), ("sealed",)))
    assert sum(item[0] == "written" for item in output) == 1
    assert sum(item[0] == "error" and item[1] == "ProjectBoundaryProfileConflict" for item in output) == 1
    assert ProjectBoundaryProfileStore(tmp_path).get("alpha").profile.revision == 2


def test_cross_process_same_command_is_idempotent(tmp_path: Path) -> None:
    output = _run_pair(tmp_path, _submit_mode, (("mode-1", "sealed"), ("mode-1", "sealed")))
    assert output.count(("receipt", "completed", 2, 2)) == 2
    assert ProjectBoundaryProfileStore(tmp_path).get("alpha").profile.revision == 2


def test_cross_process_different_commands_fail_closed_without_500(tmp_path: Path) -> None:
    output = _run_pair(tmp_path, _submit_mode, (("mode-1", "open"), ("mode-2", "sealed")))
    assert sum(item[0] == "receipt" and item[1] == "completed" for item in output) == 1
    assert sum(
        (item[0] == "receipt" and item[1] == "requires_repair")
        or (item[0] == "error" and item[1] == "BoundaryModeCommandConflict")
        for item in output
    ) == 1
