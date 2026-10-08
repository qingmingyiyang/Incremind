from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI

from backend.api import app as app_module
from backend.api.app import (
    _has_world_supervision_recovery_candidate,
    _recover_agent_runtime_coordination,
)


class _Store:
    def __init__(self, candidates=(), *, fail: bool = False) -> None:
        self.candidates, self.fail, self.limit = candidates, fail, None

    def list_recovery_candidates(self, *, limit: int):
        self.limit = limit
        if self.fail:
            raise RuntimeError("scan unavailable")
        return self.candidates


class _Coordinator:
    def __init__(self, *, failing=(), order=None) -> None:
        self.calls = []
        self.failing = set(failing)
        self.order = order

    def reconcile_recovery_candidate(self, candidate):
        self.calls.append(candidate)
        if self.order is not None:
            self.order.append(f"topology:{candidate}")
        if candidate in self.failing:
            raise RuntimeError("candidate unavailable")


class _Organization:
    def __init__(self, *, order=None, fail: bool = False) -> None:
        self.order, self.fail, self.limit = order, fail, None

    def recover(self, limit: int):
        self.limit = limit
        if self.order is not None:
            self.order.append("organization")
        if self.fail:
            raise RuntimeError("private task must not leak")
        return {
            "status": "recovered", "scanned": 4,
            "plans": ({"plan_id": "safe-a"}, {"plan_id": "safe-b"}),
            "replayed_turn_ids": ("private-turn-id",),
        }


class _WorldObserver:
    def __init__(self, *, order=None, fail: bool = False) -> None:
        self.order, self.fail, self.limit = order, fail, None

    def recover(self, *, limit: int):
        self.limit = limit
        if self.order is not None:
            self.order.append("world")
        if self.fail:
            raise RuntimeError("private world data must not leak")
        return {"status": "completed", "scanned": 2, "recorded": 1, "noop": 1, "ignored": 0}


def _app(composition=None, organization=None, observer=None) -> FastAPI:
    application = FastAPI()
    if composition is not None:
        application.state.agent_runtime_composition = composition
    if organization is not None:
        application.state.agent_organization_runtime = organization
    if observer is not None:
        application.state.world_supervision_agent_observer = observer
    return application


def test_startup_agent_recovery_is_bounded_and_isolates_each_candidate() -> None:
    order = []
    store = _Store(("a", "b", "c"))
    coordinator = _Coordinator(failing=("b",), order=order)
    organization = _Organization(order=order)
    observer = _WorldObserver(order=order)
    application = _app(
        SimpleNamespace(store=store, coordinator=coordinator), organization, observer,
    )

    report = _recover_agent_runtime_coordination(application)

    assert store.limit == 128
    assert coordinator.calls == ["a", "b", "c"]
    assert order == ["topology:a", "topology:b", "topology:c", "organization", "world"]
    assert report == {"status": "completed", "candidates": 3, "reconciled": 2, "failed": 1}
    assert application.state.agent_runtime_startup_recovery == report
    assert organization.limit == 128
    assert application.state.agent_organization_startup_recovery == {
        "status": "recovered", "scanned": 4, "plans": 2,
        "replayed": 1, "failed": 0,
    }
    assert observer.limit == 128
    assert application.state.world_supervision_agent_startup_recovery == {
        "status": "completed", "scanned": 2, "recorded": 1,
        "noop": 1, "ignored": 0, "failed": 0,
    }


def test_startup_agent_recovery_skips_missing_composition_and_scan_failure() -> None:
    skipped_application = _app()
    skipped = _recover_agent_runtime_coordination(skipped_application)
    assert skipped["status"] == "skipped"
    assert skipped_application.state.agent_organization_startup_recovery["status"] == "skipped"
    organization = _Organization()
    failed_application = _app(
        SimpleNamespace(store=_Store(fail=True), coordinator=_Coordinator()),
        organization,
    )
    failed = _recover_agent_runtime_coordination(failed_application)
    assert failed == {"status": "scan_failed", "candidates": 0, "reconciled": 0, "failed": 1}
    assert organization.limit == 128
    assert failed_application.state.agent_organization_startup_recovery["status"] == "recovered"


def test_startup_recovery_can_lazily_build_runtime_for_terminal_world_review() -> None:
    application = _app()
    built = []

    def build_runtime():
        built.append(True)
        application.state.agent_runtime_composition = SimpleNamespace(
            store=_Store(()), coordinator=_Coordinator(),
        )
        application.state.agent_organization_runtime = _Organization()
        application.state.world_supervision_agent_observer = _WorldObserver()
        return object()

    report = _recover_agent_runtime_coordination(
        application, runtime_factory=build_runtime,
    )

    assert built == [True]
    assert report["status"] == "completed"
    assert application.state.agent_organization_startup_recovery["status"] == "recovered"
    assert application.state.world_supervision_agent_startup_recovery["status"] == "completed"


def test_cold_start_runtime_probe_requires_a_world_session_candidate(
    tmp_path, monkeypatch,
) -> None:
    database = tmp_path / ".rebuild-data" / "ai-turns.sqlite3"
    database.parent.mkdir(parents=True)
    database.touch()
    session = {"value": "world-project"}

    class _Turns:
        def __init__(self, path):
            assert path == database

        def get_request(self, turn_id):
            assert turn_id == "main-turn"
            return {"session_id": session["value"]}

    class _Runs:
        def __init__(self, path):
            assert path == database

        def list_supervision_candidates(self, *, limit):
            assert limit == 128
            return (SimpleNamespace(turn_id="main-turn"),)

    monkeypatch.setattr(app_module, "SQLiteAITurnStore", _Turns)
    monkeypatch.setattr(app_module, "SQLiteAgentStore", _Runs)

    assert _has_world_supervision_recovery_candidate(tmp_path) is True
    session["value"] = "ordinary-session"
    assert _has_world_supervision_recovery_candidate(tmp_path) is False


def test_startup_organization_failure_is_separate_and_redacted() -> None:
    application = _app(
        SimpleNamespace(store=_Store(()), coordinator=_Coordinator()),
        _Organization(fail=True), _WorldObserver(),
    )

    report = _recover_agent_runtime_coordination(application)

    assert report == {
        "status": "completed", "candidates": 0,
        "reconciled": 0, "failed": 0,
    }
    assert application.state.agent_organization_startup_recovery == {
        "status": "failed", "scanned": 0, "plans": 0,
        "replayed": 0, "failed": 1,
    }
    assert application.state.world_supervision_agent_startup_recovery["status"] == "completed"


def test_world_observer_failure_is_separate_after_organization_recovery() -> None:
    order = []
    application = _app(
        SimpleNamespace(store=_Store(()), coordinator=_Coordinator()),
        _Organization(order=order), _WorldObserver(order=order, fail=True),
    )
    _recover_agent_runtime_coordination(application)
    assert order == ["organization", "world"]
    assert application.state.agent_organization_startup_recovery["status"] == "recovered"
    assert application.state.world_supervision_agent_startup_recovery == {
        "status": "failed", "scanned": 0, "recorded": 0,
        "noop": 0, "ignored": 0, "failed": 1,
    }
