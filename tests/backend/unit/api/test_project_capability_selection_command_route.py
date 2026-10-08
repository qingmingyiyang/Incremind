from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.routes.ai import router
from backend.security.project_capability_selection_command import (
    ProjectCapabilitySelectionCommandService,
)
from backend.security.project_capability_profiles import ProjectCapabilityProfileStore
from core.ai_kernel import CapabilityDefinition, CapabilityRegistrySnapshot
from core.ai_tooling import ToolDefinition, ToolRetryPolicy


class _Runtime:
    def __init__(self, definitions=None, generation: int = 5) -> None:
        self.definitions = definitions if definitions is not None else (
            CapabilityDefinition(
                "memory.recall", 1, "read", False, "read_only",
                "crp://input", "crp://output",
            ),
        )
        self.generation = generation
        self.calls = 0

    def capability_registry_snapshot(self) -> CapabilityRegistrySnapshot:
        self.calls += 1
        return CapabilityRegistrySnapshot(self.generation, self.definitions)


def _disabled_plugin_capability() -> CapabilityDefinition:
    tool = ToolDefinition(
        tool_id="private.search", version=1, display_name="Private Search",
        description="private", source="plugin", owner_id="private-plugin",
        effect="read", data_classes=("unclassified",), destination="local",
        input_schema_uri="crp://private-input", output_schema_uri="crp://private-output",
        receipt_schema_uri=None, operation_semantics="read_only",
        execution_mode="parallel", resource_locks=(), idempotency="idempotent",
        retry_policy=ToolRetryPolicy(1, 0, ()), verification_tool_id=None,
        compensation_tool_id=None, mutability="read_only", egress_class="none",
        network_scope=(), data_egress_scope=(), timeout_ms=1_000,
        required_scopes=(), boundary_requirements=(),
    )
    return CapabilityDefinition(
        tool.tool_id, tool.version, tool.effect, False, tool.operation_semantics,
        tool.input_schema_uri, tool.output_schema_uri, tool,
    )


def _app(tmp_path: Path, runtime=None) -> FastAPI:
    app = FastAPI()
    app.state.container = SimpleNamespace(root_dir=tmp_path)
    if runtime is not None:
        app.state.ai_runtime = runtime
    app.include_router(router)
    return app


def _body() -> dict[str, object]:
    return {
        "command_id": "exclude-1", "action": "exclude",
        "target_stable_id": "memory.recall",
        "expected_boundary_revision": 1,
        "expected_capability_revision": 1,
        "expected_registry_generation": 5, "confirm": True,
    }


def _confirmation_body(action: str, command_id: str, capability_revision: int) -> dict[str, object]:
    return {
        "command_id": command_id, "action": action,
        "target_stable_id": "memory.recall",
        "expected_boundary_revision": 1,
        "expected_capability_revision": capability_revision,
        "expected_registry_generation": 5,
    }


def test_exclude_route_uses_snapshot_and_terminal_replay_is_offline(tmp_path: Path) -> None:
    runtime = _Runtime()
    app = _app(tmp_path, runtime)
    with TestClient(app) as client:
        first = client.post("/api/ai/projects/alpha/capability-selection", json=_body())
        assert first.status_code == 200
        assert first.json()["status"] == "completed"
        assert first.headers["cache-control"] == "no-store"
        assert runtime.calls == 1
        del app.state.ai_runtime
        replay = client.post("/api/ai/projects/alpha/capability-selection", json=_body())
        assert replay.status_code == 200 and replay.json() == first.json()
        assert runtime.calls == 1
        fetched = client.get(
            "/api/ai/projects/alpha/capability-selection-commands/exclude-1",
        )
        assert fetched.status_code == 200 and fetched.json() == first.json()


def test_selection_route_rejects_expansion_extra_fields_and_missing_runtime(tmp_path: Path) -> None:
    with TestClient(_app(tmp_path)) as client:
        select = _body()
        select["action"] = "select"
        assert client.post("/api/ai/projects/alpha/capability-selection", json=select).status_code == 400
        extra = _body()
        extra["enabled_plugin_ids"] = ["private"]
        assert client.post("/api/ai/projects/alpha/capability-selection", json=extra).status_code == 400
        unavailable = client.post("/api/ai/projects/alpha/capability-selection", json=_body())
        assert unavailable.status_code == 503


def test_confirmed_select_is_bound_single_use_and_terminal_replay_is_offline(
    tmp_path: Path,
) -> None:
    runtime = _Runtime()
    app = _app(tmp_path, runtime)
    with TestClient(app) as client:
        assert client.post(
            "/api/ai/projects/alpha/capability-selection", json=_body(),
        ).status_code == 200
        confirmation_body = _confirmation_body("select", "select-1", 2)
        confirmation = client.post(
            "/api/ai/projects/alpha/capability-selection/confirmations",
            json=confirmation_body,
        )
        assert confirmation.status_code == 200
        token = confirmation.json()["confirmation_token"]
        command_body = confirmation_body | {"confirmation_token": token}
        selected = client.post(
            "/api/ai/projects/alpha/capability-selection", json=command_body,
        )
        assert selected.status_code == 200
        assert selected.json()["status"] == "completed"
        assert "confirmation_token" not in selected.text
        assert "contract-sha256" not in selected.text
        profile = ProjectCapabilityProfileStore(tmp_path).get("alpha").profile
        assert profile.denied_tool_ids == ()
        assert profile.tool_selection_bindings[0].stable_id == "memory.recall"

        del app.state.ai_runtime
        replay = client.post(
            "/api/ai/projects/alpha/capability-selection", json=command_body,
        )
        assert replay.status_code == 200 and replay.json() == selected.json()


def test_confirmation_registry_drift_consumes_without_profile_write(tmp_path: Path) -> None:
    runtime = _Runtime()
    app = _app(tmp_path, runtime)
    with TestClient(app) as client:
        assert client.post(
            "/api/ai/projects/alpha/capability-selection", json=_body(),
        ).status_code == 200
        confirmation_body = _confirmation_body("reset_exclusion", "reset-1", 2)
        confirmation = client.post(
            "/api/ai/projects/alpha/capability-selection/confirmations",
            json=confirmation_body,
        )
        token = confirmation.json()["confirmation_token"]
        runtime.generation = 6
        command_body = confirmation_body | {"confirmation_token": token}
        drifted = client.post(
            "/api/ai/projects/alpha/capability-selection", json=command_body,
        )
        assert drifted.status_code == 400
        assert ProjectCapabilityProfileStore(tmp_path).get("alpha").profile.revision == 2
        runtime.generation = 5
        reused = client.post(
            "/api/ai/projects/alpha/capability-selection", json=command_body,
        )
        assert reused.status_code == 400


def test_selection_confirmation_requires_configured_desktop_session(
    tmp_path: Path, monkeypatch,
) -> None:
    secret = "s" * 43
    values = {
        "CHRIPTMAS_DESKTOP_SESSION_MODE": "desktop_production",
        "CHRIPTMAS_DESKTOP_SESSION_SECRET": secret,
        "CHRIPTMAS_DESKTOP_INSTANCE_ID": "desktop-1",
        "CHRIPTMAS_DESKTOP_NONCE": "n" * 43,
        "CHRIPTMAS_DESKTOP_PROTOCOL_VERSION": "desktop-loopback/1",
        "CHRIPTMAS_DESKTOP_SESSION_EXPIRES_AT": "2099-08-24T12:00:00+00:00",
        "CHRIPTMAS_DESKTOP_ALLOWED_ORIGIN": "http://127.0.0.1:8001",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    body = _confirmation_body("select", "select-1", 1)
    with TestClient(_app(tmp_path, _Runtime())) as client:
        missing = client.post(
            "/api/ai/projects/alpha/capability-selection/confirmations", json=body,
        )
        authorized = client.post(
            "/api/ai/projects/alpha/capability-selection/confirmations", json=body,
            headers={"X-Chriptmas-Desktop-Session": secret},
        )
    assert missing.status_code == 403
    assert authorized.status_code == 400
    monkeypatch.setenv(
        "CHRIPTMAS_DESKTOP_SESSION_EXPIRES_AT", "2000-01-01T00:00:00+00:00",
    )
    with TestClient(_app(tmp_path, _Runtime())) as client:
        expired = client.post(
            "/api/ai/projects/alpha/capability-selection/confirmations", json=body,
            headers={"X-Chriptmas-Desktop-Session": secret},
        )
    assert expired.status_code == 403


def test_hidden_plugin_and_unknown_selection_targets_are_indistinguishable(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path, _Runtime(definitions=(_disabled_plugin_capability(),)))
    with TestClient(app) as client:
        hidden_body = _confirmation_body("select", "select-hidden", 1)
        hidden_body["target_stable_id"] = "private.search"
        hidden = client.post(
            "/api/ai/projects/alpha/capability-selection/confirmations",
            json=hidden_body,
        )
        unknown_body = _confirmation_body("select", "select-unknown", 1)
        unknown_body["target_stable_id"] = "unknown.search"
        unknown = client.post(
            "/api/ai/projects/alpha/capability-selection/confirmations",
            json=unknown_body,
        )
    assert hidden.status_code == unknown.status_code == 400
    assert hidden.json() == unknown.json()


def test_active_exclusion_target_loss_enters_repair_and_releases_reservation(tmp_path: Path) -> None:
    service = ProjectCapabilitySelectionCommandService(tmp_path)
    original_advance = service._advance

    def crash(_receipt):
        raise RuntimeError("before profile write")

    try:
        service._advance = crash  # type: ignore[method-assign]
        try:
            service.exclude(
                project_id="alpha", command_id="exclude-1",
                target_stable_id="memory.recall", expected_boundary_revision=1,
                expected_capability_revision=1, expected_registry_generation=5,
            )
        except RuntimeError:
            pass
        service._advance = original_advance  # type: ignore[method-assign]
        assert service.get("exclude-1").status == "prepared"  # type: ignore[union-attr]
    finally:
        service.close()

    with TestClient(_app(tmp_path, _Runtime(definitions=()))) as client:
        repaired = client.post("/api/ai/projects/alpha/capability-selection", json=_body())
    assert repaired.status_code == 409
    assert repaired.json()["status"] == "requires_repair"


def test_profile_written_crash_completes_offline_instead_of_false_repair(tmp_path: Path) -> None:
    service = ProjectCapabilitySelectionCommandService(tmp_path)
    original_set = service._set

    def crash(receipt, status, **changes):
        if status == "capability_updated":
            raise RuntimeError("after profile write")
        return original_set(receipt, status, **changes)

    try:
        service._set = crash  # type: ignore[method-assign]
        try:
            service.exclude(
                project_id="alpha", command_id="exclude-1",
                target_stable_id="memory.recall", expected_boundary_revision=1,
                expected_capability_revision=1, expected_registry_generation=5,
            )
        except RuntimeError:
            pass
    finally:
        service.close()

    with TestClient(_app(tmp_path)) as client:
        recovered = client.post(
            "/api/ai/projects/alpha/capability-selection", json=_body(),
        )
    assert recovered.status_code == 200
    assert recovered.json()["status"] == "completed"


def test_unknown_and_generation_drift_are_indistinguishable(tmp_path: Path) -> None:
    with TestClient(_app(tmp_path, _Runtime())) as client:
        unknown = _body()
        unknown["target_stable_id"] = "private.tool"
        first = client.post("/api/ai/projects/alpha/capability-selection", json=unknown)
        drift = _body()
        drift["expected_registry_generation"] = 4
        second = client.post("/api/ai/projects/alpha/capability-selection", json=drift)
    assert first.status_code == second.status_code == 400
    assert first.json() == second.json()
