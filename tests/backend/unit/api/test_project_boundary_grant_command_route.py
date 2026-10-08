from pathlib import Path
from types import SimpleNamespace
from contextlib import contextmanager

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from backend.api.routes.ai import router
from backend.security.project_boundary_grant_command import (
    ProjectBoundaryGrantCommandService,
    resolve_grant_target,
)
from backend.security.project_boundary_profiles import ProjectBoundaryProfileStore
from backend.security.project_capability_profiles import ProjectCapabilityProfileStore
from backend.security.project_boundary_mutation_reservation import (
    ProjectBoundaryMutationReservation,
    ProjectBoundaryMutationReservationConflict,
)
from core.ai_kernel import CapabilityDefinition, CapabilityRegistrySnapshot


class _Runtime:
    def __init__(self) -> None:
        self.snapshot_calls = 0

    def capability_registry_snapshot(self) -> CapabilityRegistrySnapshot:
        self.snapshot_calls += 1
        return CapabilityRegistrySnapshot(7, (
            CapabilityDefinition(
                "memory.recall", 1, "read", False, "read_only",
                "crp://input", "crp://output",
            ),
        ))


class _EmptyRuntime:
    def __init__(self, generation: int = 7) -> None:
        self.generation = generation

    def capability_registry_snapshot(self) -> CapabilityRegistrySnapshot:
        return CapabilityRegistrySnapshot(self.generation, ())


def _app(tmp_path: Path, *, runtime: object | None = None) -> FastAPI:
    app = FastAPI()
    app.state.container = SimpleNamespace(root_dir=tmp_path)
    if runtime is not None:
        app.state.ai_runtime = runtime
    app.include_router(router)
    return app


def _create_body() -> dict[str, object]:
    return {
        "command_id": "create-1", "target_stable_id": "memory.recall",
        "duration": "until_revoked", "expected_boundary_revision": 1,
        "expected_capability_revision": 1,
        "expected_registry_generation": 7, "confirm": True,
    }


def test_create_route_uses_existing_runtime_snapshot_and_terminal_replay_needs_no_runtime(tmp_path: Path) -> None:
    runtime = _Runtime()
    app = _app(tmp_path, runtime=runtime)
    with TestClient(app) as client:
        created = client.post("/api/ai/projects/alpha/boundary-grants", json=_create_body())
        assert created.status_code == 200
        assert created.json()["status"] == "completed"
        assert created.headers["cache-control"] == "no-store"
        assert runtime.snapshot_calls == 1
        del app.state.ai_runtime
        replay = client.post("/api/ai/projects/alpha/boundary-grants", json=_create_body())
        assert replay.status_code == 200
        assert replay.json() == created.json()
        assert runtime.snapshot_calls == 1


def test_revoke_route_does_not_require_runtime_and_get_is_pure(tmp_path: Path) -> None:
    app = _app(tmp_path, runtime=_Runtime())
    with TestClient(app) as client:
        created = client.post("/api/ai/projects/alpha/boundary-grants", json=_create_body()).json()
        del app.state.ai_runtime
        revoked = client.post(
            f"/api/ai/projects/alpha/boundary-grants/{created['grant_id']}/revoke",
            json={
                "command_id": "revoke-1", "expected_boundary_revision": 2,
                "expected_capability_revision": 2,
                "expected_grant_revision": 1, "confirm": True,
            },
        )
        assert revoked.status_code == 200
        assert revoked.json()["grant_revision"] == 2
        fetched = client.get("/api/ai/projects/alpha/boundary-grant-commands/revoke-1")
        assert fetched.status_code == 200 and fetched.json() == revoked.json()


def test_create_route_rejects_authority_fields_and_missing_runtime(tmp_path: Path) -> None:
    app = _app(tmp_path)
    with TestClient(app) as client:
        body = _create_body()
        body["target_id"] = "mcp:private"
        rejected = client.post("/api/ai/projects/alpha/boundary-grants", json=body)
        assert rejected.status_code == 400
        unavailable = client.post("/api/ai/projects/alpha/boundary-grants", json=_create_body())
        assert unavailable.status_code == 503
        assert "mcp" not in unavailable.text.lower()


def test_create_route_hides_unknown_and_generation_drift_with_same_error(tmp_path: Path) -> None:
    with TestClient(_app(tmp_path, runtime=_Runtime())) as client:
        unknown = _create_body()
        unknown["target_stable_id"] = "private.tool"
        first = client.post("/api/ai/projects/alpha/boundary-grants", json=unknown)
        drift = _create_body()
        drift["expected_registry_generation"] = 6
        second = client.post("/api/ai/projects/alpha/boundary-grants", json=drift)
    assert first.status_code == second.status_code == 400
    assert first.json() == second.json()


def test_create_route_resumes_after_boundary_write_before_capability_binding(tmp_path: Path) -> None:
    runtime = _Runtime()
    snapshot = runtime.capability_registry_snapshot()
    target = resolve_grant_target(
        stable_id="memory.recall",
        profile=ProjectCapabilityProfileStore(tmp_path).get("alpha").profile,
        boundary=ProjectBoundaryProfileStore(tmp_path).get("alpha").profile,
        capabilities=snapshot.definitions, registry_generation=snapshot.generation,
        expected_registry_generation=7,
    )
    service = ProjectBoundaryGrantCommandService(tmp_path)
    original_set = service._set

    def crash(receipt, status, **changes):
        if status == "boundary_updated":
            raise RuntimeError("simulated crash")
        return original_set(receipt, status, **changes)

    try:
        service._set = crash  # type: ignore[method-assign]
        try:
            service.create(
                project_id="alpha", command_id="create-1", target=target,
                duration="until_revoked", expected_boundary_revision=1,
                expected_capability_revision=1,
            )
        except RuntimeError:
            pass
    finally:
        service.close()

    with TestClient(_app(tmp_path, runtime=runtime)) as client:
        resumed = client.post("/api/ai/projects/alpha/boundary-grants", json=_create_body())
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "completed"


def test_execution_lock_timeout_maps_to_conflict_not_bad_request_or_500(tmp_path: Path, monkeypatch) -> None:
    @contextmanager
    def busy(_self, _project_id):
        raise ProjectBoundaryMutationReservationConflict("execution busy")
        yield

    monkeypatch.setattr(ProjectBoundaryMutationReservation, "execution", busy)
    with TestClient(_app(tmp_path, runtime=_Runtime())) as client:
        response = client.post("/api/ai/projects/alpha/boundary-grants", json=_create_body())
    assert response.status_code == 409
    assert response.json()["detail"] == "AI Boundary grant command conflict"


def test_active_create_target_loss_enters_repair_and_releases_reservation(tmp_path: Path) -> None:
    runtime = _Runtime()
    snapshot = runtime.capability_registry_snapshot()
    target = resolve_grant_target(
        stable_id="memory.recall",
        profile=ProjectCapabilityProfileStore(tmp_path).get("alpha").profile,
        boundary=ProjectBoundaryProfileStore(tmp_path).get("alpha").profile,
        capabilities=snapshot.definitions, registry_generation=7,
        expected_registry_generation=7,
    )
    service = ProjectBoundaryGrantCommandService(tmp_path)
    original_set = service._set

    def crash(receipt, status, **changes):
        if status == "boundary_updated":
            raise RuntimeError("simulated crash")
        return original_set(receipt, status, **changes)

    try:
        service._set = crash  # type: ignore[method-assign]
        with pytest.raises(RuntimeError):
            service.create(
                project_id="alpha", command_id="create-1", target=target,
                duration="until_revoked", expected_boundary_revision=1,
                expected_capability_revision=1,
            )
    finally:
        service.close()

    with TestClient(_app(tmp_path, runtime=_EmptyRuntime())) as client:
        repaired = client.post("/api/ai/projects/alpha/boundary-grants", json=_create_body())
        assert repaired.status_code == 409
        assert repaired.json()["status"] == "requires_repair"
        # The reservation was released. The next mutation reaches the binding
        # drift precheck instead of reporting an active-command conflict.
        mode = client.post("/api/ai/projects/alpha/boundary-mode", json={
            "command_id": "mode-2", "mode": "sealed",
            "expected_boundary_revision": 2,
            "expected_capability_revision": 1, "confirm": True,
        })
        assert mode.status_code == 409
        assert "binding drifted" in mode.json()["reason"]
