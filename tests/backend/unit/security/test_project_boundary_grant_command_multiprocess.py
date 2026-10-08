from __future__ import annotations

import multiprocessing
from pathlib import Path

from backend.security.project_boundary_grant_command import (
    ProjectBoundaryGrantCommandService,
    resolve_grant_target,
)
from backend.security.project_boundary_profiles import ProjectBoundaryProfileStore
from backend.security.project_capability_profiles import ProjectCapabilityProfileStore
from core.ai_kernel import CapabilityDefinition


def _create(root: str, ready, start, results) -> None:
    ready.put(True)
    start.wait(10)
    service = None
    try:
        path = Path(root)
        capability = CapabilityDefinition(
            "memory.recall", 1, "read", False, "read_only",
            "crp://input", "crp://output",
        )
        target = resolve_grant_target(
            stable_id="memory.recall",
            profile=ProjectCapabilityProfileStore(path).get("alpha").profile,
            boundary=ProjectBoundaryProfileStore(path).get("alpha").profile,
            capabilities=(capability,), registry_generation=1,
            expected_registry_generation=1,
        )
        service = ProjectBoundaryGrantCommandService(path)
        receipt = service.create(
            project_id="alpha", command_id="create-1", target=target,
            duration="until_revoked", expected_boundary_revision=1,
            expected_capability_revision=1,
        )
        results.put(("receipt", receipt.status, receipt.grant_id))
    except Exception as error:
        results.put(("error", type(error).__name__, str(error)))
    finally:
        if service is not None:
            service.close()


def test_cross_process_exact_grant_replay_has_one_write_and_one_receipt(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    start = context.Event()
    results = context.Queue()
    processes = [
        context.Process(target=_create, args=(str(tmp_path), ready, start, results))
        for _ in range(2)
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
    assert all(item[:2] == ("receipt", "completed") for item in output)
    assert output[0][2] == output[1][2]
    boundary = ProjectBoundaryProfileStore(tmp_path).get("alpha").profile
    assert boundary.revision == 2 and len(boundary.persistent_grants) == 1
