from __future__ import annotations

import json
import base64
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.api.ai_runtime import build_ai_runtime
from backend.api.app import create_app
from backend.api.plugin_runtime import (
    PluginToolRegistrationManager,
    build_plugin_package_intake,
    build_plugin_tool_activation,
)
from core.ai_kernel import (
    CapabilityDefinition,
    InMemoryTurnEventStore,
    InMemoryTurnPayloadStore,
    InMemoryTurnStateStore,
    ScopedCapabilityRegistry,
    SynchronousAIRuntime,
)
from core.ai_tooling import tool_from_capability
from core.plugin_host import PluginPackageIntakeError
from core.storage_provider import SQLiteStructuredRecordStore


def _package(root, *, tools=None):
    package = root / ".rebuild-data" / "plugin-package-inbox" / "lookup-plugin"
    (package / ".codex-plugin").mkdir(parents=True, exist_ok=True)
    (package / ".codex-plugin" / "plugin.json").write_text(json.dumps({
        "name": "lookup-plugin", "version": "1.0.0", "description": "Local lookup",
    }), encoding="utf-8")
    for tool in tools or [{
        "id": "country.lookup", "version": 1, "description": "Country lookup",
        "entries": {"CN": "China"},
    }]:
        directory = package / "tools" / tool["id"]
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "tool.json").write_text(json.dumps(tool), encoding="utf-8")
    return package


def _named_package(root, plugin_id, tool):
    package = root / ".rebuild-data" / "plugin-package-inbox" / plugin_id
    (package / ".codex-plugin").mkdir(parents=True)
    (package / "tools" / tool["id"]).mkdir(parents=True)
    (package / ".codex-plugin" / "plugin.json").write_text(json.dumps({
        "name": plugin_id, "version": "1.0.0", "description": "Local lookup",
    }), encoding="utf-8")
    (package / "tools" / tool["id"] / "tool.json").write_text(json.dumps(tool), encoding="utf-8")
    return package


def _activate(root, *, tool_ids=("country.lookup",)):
    intake = build_plugin_package_intake(root)
    package = _package(root)
    discovered = intake.discover(str(package), command_id="discover-tool-0001")
    installed = intake.install_disabled(
        "lookup-plugin", expected_state_revision=discovered["state_revision"],
        command_id="install-tool-0001", confirm=True,
    )
    activation = build_plugin_tool_activation(root)
    reviewed = activation.review(
        "lookup-plugin", tool_ids=list(tool_ids),
        expected_state_revision=installed["state_revision"], command_id="review-tool-0001",
        confirm=True, reason="local only",
    )
    return activation.activate(
        "lookup-plugin", expected_review_revision=reviewed["review_revision"],
        expected_activation_revision=0, command_id="activate-tool-0001", confirm=True,
    )


def test_manager_reconciles_durable_snapshot_with_one_reversible_registry_lease(tmp_path) -> None:
    activated = _activate(tmp_path)
    registry = ScopedCapabilityRegistry()
    manager = PluginToolRegistrationManager(
        activation=build_plugin_tool_activation(tmp_path), registry=registry,
    )

    first = manager.reconcile()
    assert [item.capability_id for item in first] == ["country.lookup"]
    assert tool_from_capability(first[0]).source == "plugin"
    assert registry.resolve("country.lookup")[1].invoke({"key": "CN"})["result"]["value"] == "China"
    generation = registry.snapshot().generation
    assert manager.reconcile() == first
    assert registry.snapshot().generation == generation

    # A byte-level raw package change, even if it is still JSON, must remove
    # the previously registered provider without needing an explicit disable.
    store = SQLiteStructuredRecordStore(tmp_path / ".rebuild-data" / "jobs.sqlite3")
    raw = store.read("plugin_raw_packages", "lookup-plugin~1.0.0")
    assert raw is not None
    drifted = dict(raw.payload)
    drifted["files"] = list(drifted["files"])
    original = base64.b64decode(drifted["files"][-1]["content_base64"])
    changed = original + b"\n"
    drifted["files"][-1] = dict(drifted["files"][-1]) | {
        "content_base64": base64.b64encode(changed).decode("ascii"), "size_bytes": len(changed),
    }
    with store.begin() as uow:
        uow.put("plugin_raw_packages", raw.object_id, drifted, expected_revision=raw.revision)
        uow.commit()
    resolved = registry.resolve("country.lookup")
    assert resolved is not None
    with pytest.raises(PluginPackageIntakeError, match="durable activation drifted"):
        resolved[1].invoke({"key": "CN"})
    assert manager.reconcile() == ()
    assert registry.resolve("country.lookup") is None

    # Repairing a durable activation is intentionally out of scope. Disabling
    # after a drift remains safe and does not resurrect the registry lease.
    build_plugin_tool_activation(tmp_path).disable(
        "lookup-plugin", expected_activation_revision=activated["activation_revision"],
        command_id="disable-tool-0001", confirm=True, reason="revoke",
    )
    assert manager.reconcile() == ()
    assert registry.resolve("country.lookup") is None


def test_production_runtime_restart_registers_only_durable_active_plugin_tools(tmp_path) -> None:
    _activate(tmp_path)
    first = build_ai_runtime(SimpleNamespace(root_dir=tmp_path))
    definitions = first.capability_registry_snapshot().definitions
    plugin = next(item for item in definitions if item.capability_id == "country.lookup")
    assert tool_from_capability(plugin).owner_id == "lookup-plugin"

    restarted = build_ai_runtime(SimpleNamespace(root_dir=tmp_path))
    assert "country.lookup" in {
        item.capability_id for item in restarted.capability_registry_snapshot().definitions
    }


def test_registered_plugin_tool_runs_through_kernel_dispatch_and_receipt(tmp_path) -> None:
    _activate(tmp_path)
    registry = ScopedCapabilityRegistry()
    PluginToolRegistrationManager(
        activation=build_plugin_tool_activation(tmp_path), registry=registry,
    ).reconcile()
    events = InMemoryTurnEventStore()
    payloads = InMemoryTurnPayloadStore()

    class _Planner:
        def plan(self, request, prior_events, capabilities, payload_store, execution_control=None):
            if any(event["type"] == "tool.completed" for event in prior_events):
                return {"type": "complete", "summary": "lookup finished"}
            return {"type": "tool", "capability_id": "country.lookup", "arguments": {"key": "CN"}}

    runtime = SynchronousAIRuntime(
        planner=_Planner(), registry=registry, events=events, payloads=payloads,
        state=InMemoryTurnStateStore(),
    )
    request = json.loads(
        (Path(__file__).resolve().parents[4] / "core-contracts" / "ai" / "fixtures" / "turn-request" / "valid-project-answer.json").read_text(encoding="utf-8")
    )
    request["capability_policy"]["allowed"] = ["country.lookup"]
    receipt = runtime.submit_turn(request)

    assert receipt.status == "completed"
    completed = next(event for event in events.events_after(receipt.turn_id) if event["type"] == "tool.completed")
    outcome = payloads.get(completed["data"]["payload_ref"])
    assert outcome == {"found": True, "key": "CN", "value": "China"}
    assert completed["data"]["receipt_ref"] is None  # read-only Tool; no side-effect Receipt
    assert any(event["type"] == "tool.requested" for event in events.events_after(receipt.turn_id))


def test_registry_collision_with_core_is_withheld_without_breaking_other_plugin_tools(tmp_path) -> None:
    _package(tmp_path, tools=[
        {"id": "memory.recall", "version": 1, "description": "Collision", "entries": {"x": "x"}},
        {"id": "country.lookup", "version": 1, "description": "Country", "entries": {"CN": "China"}},
    ])
    _activate(tmp_path, tool_ids=("country.lookup", "memory.recall"))
    registry = ScopedCapabilityRegistry()
    registry.register(
        CapabilityDefinition("memory.recall", 1, "read", False, "read_only", "crp://core/in", "crp://core/out"),
        object(),
    )
    manager = PluginToolRegistrationManager(
        activation=build_plugin_tool_activation(tmp_path), registry=registry,
    )

    registered = manager.reconcile()
    assert [item.capability_id for item in registered] == ["country.lookup"]
    assert manager.conflicting_tool_ids == ("memory.recall",)
    assert registry.resolve("memory.recall") is not None
    assert registry.resolve("country.lookup") is not None


def test_tool_api_uses_cas_replay_and_revokes_running_runtime_immediately(tmp_path) -> None:
    package = _package(tmp_path)
    app = create_app(SimpleNamespace(root_dir=tmp_path))
    app.state.ai_runtime = build_ai_runtime(app.state.container, application=app)
    with TestClient(app) as client:
        discovered = client.post("/api/ai/governance/plugins/packages/discover", json={
            "source_path": str(package), "command_id": "discover-api-tool-0001",
        })
        installed = client.post("/api/ai/governance/plugins/packages/lookup-plugin/install-disabled", json={
            "expected_state_revision": discovered.json()["state_revision"],
            "command_id": "install-api-tool-0001", "confirm": True,
        })
        reviewed = client.post("/api/ai/governance/plugins/packages/lookup-plugin/tools/review", json={
            "tool_ids": ["country.lookup"], "expected_state_revision": installed.json()["state_revision"],
            "command_id": "review-api-tool-0001", "confirm": True, "reason": "local only",
        })
        assert reviewed.status_code == 200
        active = client.post("/api/ai/governance/plugins/packages/lookup-plugin/tools/activate", json={
            "expected_review_revision": reviewed.json()["review_revision"],
            "expected_activation_revision": 0, "command_id": "activate-api-tool-0001", "confirm": True,
        })
        assert active.status_code == 200
        assert "country.lookup" in {
            item.capability_id
            for item in app.state.ai_runtime.capability_registry_snapshot().definitions
        }
        replay = client.post("/api/ai/governance/plugins/packages/lookup-plugin/tools/activate", json={
            "expected_review_revision": reviewed.json()["review_revision"],
            "expected_activation_revision": 0, "command_id": "activate-api-tool-0001", "confirm": True,
        })
        assert replay.status_code == 200 and replay.json()["replayed"] is True
        conflict = client.post("/api/ai/governance/plugins/packages/lookup-plugin/tools/disable", json={
            "expected_activation_revision": 999, "command_id": "disable-api-tool-bad", "confirm": True, "reason": "revoke",
        })
        assert conflict.status_code == 409
        disabled = client.post("/api/ai/governance/plugins/packages/lookup-plugin/tools/disable", json={
            "expected_activation_revision": active.json()["activation_revision"],
            "command_id": "disable-api-tool-0001", "confirm": True, "reason": "revoke",
        })
        assert disabled.status_code == 200
        assert "country.lookup" not in {
            item.capability_id
            for item in app.state.ai_runtime.capability_registry_snapshot().definitions
        }


def test_tool_activation_reports_runtime_collision_without_breaking_ai_composition(tmp_path) -> None:
    package = _package(tmp_path, tools=[{
        "id": "memory.recall", "version": 1, "description": "Collision", "entries": {"x": "x"},
    }])
    app = create_app(SimpleNamespace(root_dir=tmp_path))
    app.state.ai_runtime = build_ai_runtime(app.state.container, application=app)
    with TestClient(app) as client:
        discovered = client.post("/api/ai/governance/plugins/packages/discover", json={
            "source_path": str(package), "command_id": "discover-collision-tool-0001",
        })
        installed = client.post("/api/ai/governance/plugins/packages/lookup-plugin/install-disabled", json={
            "expected_state_revision": discovered.json()["state_revision"],
            "command_id": "install-collision-tool-0001", "confirm": True,
        })
        reviewed = client.post("/api/ai/governance/plugins/packages/lookup-plugin/tools/review", json={
            "tool_ids": ["memory.recall"], "expected_state_revision": installed.json()["state_revision"],
            "command_id": "review-collision-tool-0001", "confirm": True, "reason": "local only",
        })
        collision = client.post("/api/ai/governance/plugins/packages/lookup-plugin/tools/activate", json={
            "expected_review_revision": reviewed.json()["review_revision"],
            "expected_activation_revision": 0, "command_id": "activate-collision-tool-0001", "confirm": True,
        })
        disabled = client.post("/api/ai/governance/plugins/packages/lookup-plugin/tools/disable", json={
            "expected_activation_revision": collision.json()["activation_revision"],
            "command_id": "disable-collision-tool-0001", "confirm": True, "reason": "resolve collision",
        })
    assert collision.status_code == 409
    assert collision.json()["status"] == "plugin_tool_runtime_conflict"
    assert collision.json()["runtime_status"] == "withheld"
    assert collision.json()["activation_revision"] >= 1
    assert collision.json()["conflicting_tool_ids"] == ["memory.recall"]
    assert disabled.status_code == 200
    assert "memory.recall" in {
        item.capability_id for item in app.state.ai_runtime.capability_registry_snapshot().definitions
    }


def test_unrelated_existing_collision_does_not_misreport_new_plugin_as_withheld(tmp_path) -> None:
    app = create_app(SimpleNamespace(root_dir=tmp_path))
    app.state.ai_runtime = build_ai_runtime(app.state.container, application=app)

    def admit(client, plugin_id, tool, suffix):
        package = _named_package(tmp_path, plugin_id, tool)
        discovered = client.post("/api/ai/governance/plugins/packages/discover", json={
            "source_path": str(package), "command_id": f"discover-{suffix}-0001",
        }).json()
        installed = client.post(f"/api/ai/governance/plugins/packages/{plugin_id}/install-disabled", json={
            "expected_state_revision": discovered["state_revision"],
            "command_id": f"install-{suffix}-0001", "confirm": True,
        }).json()
        reviewed = client.post(f"/api/ai/governance/plugins/packages/{plugin_id}/tools/review", json={
            "tool_ids": [tool["id"]], "expected_state_revision": installed["state_revision"],
            "command_id": f"review-{suffix}-0001", "confirm": True, "reason": "local only",
        }).json()
        return client.post(f"/api/ai/governance/plugins/packages/{plugin_id}/tools/activate", json={
            "expected_review_revision": reviewed["review_revision"],
            "expected_activation_revision": 0, "command_id": f"activate-{suffix}-0001", "confirm": True,
        })

    with TestClient(app) as client:
        collision = admit(client, "collision-plugin", {
            "id": "memory.recall", "version": 1, "description": "Collision", "entries": {"x": "x"},
        }, "collision")
        healthy = admit(client, "healthy-plugin", {
            "id": "country.lookup", "version": 1, "description": "Country", "entries": {"CN": "China"},
        }, "healthy")

    assert collision.status_code == 409
    assert healthy.status_code == 200
    assert healthy.json()["runtime_status"] == "active"
    assert healthy.json()["other_conflicting_tool_ids"] == ["memory.recall"]
    assert "country.lookup" in {
        item.capability_id for item in app.state.ai_runtime.capability_registry_snapshot().definitions
    }


def test_plugin_tool_project_enable_requires_active_tool_and_preserves_tool_selection(tmp_path) -> None:
    package = _package(tmp_path)
    app = create_app(SimpleNamespace(root_dir=tmp_path))
    with TestClient(app) as client:
        discovered = client.post("/api/ai/governance/plugins/packages/discover", json={
            "source_path": str(package), "command_id": "discover-project-tool-0001",
        })
        installed = client.post("/api/ai/governance/plugins/packages/lookup-plugin/install-disabled", json={
            "expected_state_revision": discovered.json()["state_revision"],
            "command_id": "install-project-tool-0001", "confirm": True,
        })
        inactive = client.post("/api/ai/governance/plugins/packages/lookup-plugin/tools/projects/project-tools/enable", json={
            "expected_profile_revision": 0, "confirm": True,
        })
        assert inactive.status_code == 400
        reviewed = client.post("/api/ai/governance/plugins/packages/lookup-plugin/tools/review", json={
            "tool_ids": ["country.lookup"], "expected_state_revision": installed.json()["state_revision"],
            "command_id": "review-project-tool-0001", "confirm": True, "reason": "local only",
        })
        active = client.post("/api/ai/governance/plugins/packages/lookup-plugin/tools/activate", json={
            "expected_review_revision": reviewed.json()["review_revision"],
            "expected_activation_revision": 0, "command_id": "activate-project-tool-0001", "confirm": True,
        })
        assert active.status_code == 200
        conflict = client.post("/api/ai/governance/plugins/packages/lookup-plugin/tools/projects/project-tools/enable", json={
            "expected_profile_revision": 1, "confirm": True,
        })
        assert conflict.status_code == 409
        enabled = client.post("/api/ai/governance/plugins/packages/lookup-plugin/tools/projects/project-tools/enable", json={
            "expected_profile_revision": 0, "confirm": True,
        })
        assert enabled.status_code == 200
        profile = enabled.json()["profile"]
        assert profile["enabled_sources"] == ["core", "plugin"]
        assert profile["enabled_plugin_ids"] == ["lookup-plugin"]
        assert profile["allowed_tool_ids"] == []
        assert profile["tool_selection_bindings"] == []
