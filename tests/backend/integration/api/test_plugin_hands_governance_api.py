from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from backend.api.ai_runtime import build_ai_runtime
from backend.api.app import create_app
from backend.api.plugin_hands_runtime import build_plugin_hands_activation, plugin_hands_capability
from backend.security.project_capability_profiles import ProjectCapabilityProfileStore
from core.ai_tooling import EffectiveToolPolicyResolver
from core.plugin_hands.contracts import PluginHandsInvocation, PluginHandsLaunch, PluginHandsLease
from core.plugin_hands.durable_lifecycle import PluginHandsDurableLifecycle, PluginHandsLifecycleBinding
from core.storage_provider import SQLiteStructuredRecordStore


def _package(root: Path) -> Path:
    package = root / ".rebuild-data" / "plugin-package-inbox" / "hand-plugin"
    (package / ".codex-plugin").mkdir(parents=True)
    payload = package / "hands" / "summarize" / "payload"
    payload.mkdir(parents=True)
    (package / ".codex-plugin" / "plugin.json").write_text(json.dumps({
        "name": "hand-plugin", "version": "1.0.0", "description": "Contained Hand",
    }), encoding="utf-8")
    (package / "hands" / "summarize" / "hand.json").write_text(json.dumps({
        "schema_version": "1.0.0", "id": "summarize", "runtime": "powershell-stdio-v1",
        "entrypoint": "payload/main.ps1",
        "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        "output_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        "effect": "read", "operation_semantics": "read_only",
        "requested_resources": ["workspace_input"],
    }), encoding="utf-8")
    (payload / "main.ps1").write_text("throw 'not executed during governance'\n", encoding="utf-8")
    writer = package / "hands" / "writer"
    (writer / "payload").mkdir(parents=True)
    (writer / "hand.json").write_text(json.dumps({
        "schema_version": "1.0.0", "id": "writer", "runtime": "powershell-stdio-v1",
        "entrypoint": "payload/main.ps1",
        "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        "output_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        "effect": "write", "operation_semantics": "receipt_required",
        "requested_resources": ["workspace_output"],
    }), encoding="utf-8")
    (writer / "payload" / "main.ps1").write_text("throw 'not executed during governance'\n", encoding="utf-8")
    python_hand = package / "hands" / "python-helper"
    (python_hand / "payload").mkdir(parents=True)
    (python_hand / "hand.json").write_text(json.dumps({
        "schema_version": "1.0.0", "id": "python-helper", "runtime": "python-stdio-v1",
        "entrypoint": "payload/main.py",
        "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        "output_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        "effect": "read", "operation_semantics": "read_only",
        "requested_resources": ["workspace_input"],
    }), encoding="utf-8")
    (python_hand / "payload" / "main.py").write_text("raise RuntimeError('not executed during governance')\n", encoding="utf-8")
    hook_hand = package / "hands" / "policy-hand"
    (hook_hand / "payload").mkdir(parents=True)
    (hook_hand / "hand.json").write_text(json.dumps({
        "schema_version": "1.0.0", "id": "policy-hand", "runtime": "powershell-stdio-v1",
        "entrypoint": "payload/main.ps1",
        "input_schema": {
            "type": "object", "properties": {"hook_event": {"type": "string"}, "payload": {"type": "object"}},
            "required": ["hook_event", "payload"], "additionalProperties": False,
        },
        "output_schema": {
            "type": "object", "properties": {"exit_code": {"type": "integer"}, "stdout": {"type": "string"}, "stderr": {"type": "string"}},
            "required": ["exit_code", "stdout", "stderr"], "additionalProperties": False,
        },
        "effect": "read", "operation_semantics": "read_only", "requested_resources": [],
    }), encoding="utf-8")
    (hook_hand / "payload" / "main.ps1").write_text("throw 'not executed during governance'\n", encoding="utf-8")
    hook = package / "hooks" / "pre-tool-policy"
    hook.mkdir(parents=True)
    (hook / "hook.json").write_text(json.dumps({
        "schema_version": "1.0.0", "id": "pre-tool-policy", "hand_id": "policy-hand",
        "event": "PreToolUse", "order": 10, "sync": True, "timeout_ms": 500,
        "metadata_projection": "codex-hook-v1", "recursion": "deny",
    }), encoding="utf-8")
    return package


def _upgrade_package(root: Path) -> Path:
    package = root / ".rebuild-data" / "plugin-package-inbox" / "hand-plugin-upgrade"
    (package / ".codex-plugin").mkdir(parents=True)
    payload = package / "hands" / "summarize" / "payload"
    payload.mkdir(parents=True)
    (package / ".codex-plugin" / "plugin.json").write_text(json.dumps({
        "name": "hand-plugin", "version": "2.0.0", "description": "Contained Hand upgrade",
    }), encoding="utf-8")
    (package / "hands" / "summarize" / "hand.json").write_text(json.dumps({
        "schema_version": "1.0.0", "id": "summarize", "runtime": "powershell-stdio-v1",
        "entrypoint": "payload/main.ps1",
        "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        "output_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        "effect": "read", "operation_semantics": "read_only",
        "requested_resources": ["workspace_input"],
    }), encoding="utf-8")
    (payload / "main.ps1").write_text("throw 'upgraded hand is not executed during governance'\n", encoding="utf-8")
    return package


def _activate_summarize_hand(client: TestClient, package: Path) -> tuple[dict[str, object], dict[str, object]]:
    discovered = client.post("/api/ai/governance/plugins/packages/discover", json={
        "source_path": str(package), "command_id": "upgrade-base-discover-0001",
    })
    assert discovered.status_code == 201
    installed = client.post("/api/ai/governance/plugins/packages/hand-plugin/install-disabled", json={
        "expected_state_revision": discovered.json()["state_revision"],
        "command_id": "upgrade-base-install-0001", "confirm": True,
    })
    assert installed.status_code == 200
    reviewed = client.post("/api/ai/governance/plugins/packages/hand-plugin/hands/summarize/review", json={
        "expected_state_revision": installed.json()["state_revision"],
        "command_id": "upgrade-base-review-0001", "confirm": True, "reason": "upgrade base",
    })
    assert reviewed.status_code == 200
    materialized = client.post("/api/ai/governance/plugins/packages/hand-plugin/hands/summarize/materialize", json={
        "expected_review_revision": reviewed.json()["review_revision"],
        "expected_materialization_revision": 0, "command_id": "upgrade-base-materialize-0001", "confirm": True,
    })
    assert materialized.status_code == 200
    activated = client.post("/api/ai/governance/plugins/packages/hand-plugin/hands/summarize/activate", json={
        "expected_review_revision": reviewed.json()["review_revision"],
        "expected_materialization_revision": materialized.json()["materialization_revision"],
        "expected_activation_revision": 0, "command_id": "upgrade-base-activate-0001", "confirm": True,
    })
    assert activated.status_code == 200
    return installed.json(), activated.json()


class _LifecycleAuthority:
    def __init__(self, launch: PluginHandsLaunch) -> None:
        self._launch = launch

    def resolve(self, _binding, _scope) -> PluginHandsLaunch:
        return self._launch

    def prepare_workspace(self, _binding, _scope, _workspace) -> None:
        return None

    def verify_workspace(self, _binding, _scope, _workspace) -> None:
        return None

    def validate_outcome(self, _binding, _scope, _outcome) -> None:
        return None


def _prepare_lifecycle_attempt(root: Path) -> str:
    attempt_id = "attempt-0000001"
    lease = PluginHandsLease(
        "lease-00000001", attempt_id, 1, "project-0000001", "turn-0000000001", 1,
        "powershell-stdio-v1-r1", (), "2099-08-26T00:00:00Z",
    )
    launch = PluginHandsLaunch("launch-0000001", (root / "runner.exe").resolve(), (), {})
    invocation = PluginHandsInvocation(attempt_id, "hand-plugin", launch, lease, 1_000, {"task": "private"})
    binding = PluginHandsLifecycleBinding(
        "intent-0000001", "plugin.hand.hand-plugin.summarize", "artifact-000001",
        "hand-plugin", "summarize", "package-000001", 1, 1, 1,
        "appcontainer-v1", "powershell-stdio-v1-r1",
    )
    lifecycle = PluginHandsDurableLifecycle(
        SQLiteStructuredRecordStore(root / ".rebuild-data" / "jobs.sqlite3"),
        _LifecycleAuthority(launch), now=lambda: "2026-08-26T00:00:00Z",
    )
    lifecycle.prepare(binding, invocation)
    return attempt_id


def test_hands_governance_api_registers_restarts_enables_project_and_revokes(tmp_path: Path) -> None:
    package = _package(tmp_path)
    app = create_app(SimpleNamespace(root_dir=tmp_path))
    app.state.ai_runtime = build_ai_runtime(app.state.container, application=app)
    capability_id = "plugin.hand.hand-plugin.summarize"

    with TestClient(app) as client:
        discovered = client.post("/api/ai/governance/plugins/packages/discover", json={
            "source_path": str(package), "command_id": "discover-hand-api-0001",
        })
        assert discovered.status_code == 201
        installed = client.post("/api/ai/governance/plugins/packages/hand-plugin/install-disabled", json={
            "expected_state_revision": discovered.json()["state_revision"],
            "command_id": "install-hand-api-0001", "confirm": True,
        })
        assert installed.status_code == 200
        reviewed = client.post("/api/ai/governance/plugins/packages/hand-plugin/hands/summarize/review", json={
            "expected_state_revision": installed.json()["state_revision"],
            "command_id": "review-hand-api-0001", "confirm": True, "reason": "local contained execution",
        })
        assert reviewed.status_code == 200
        materialized = client.post("/api/ai/governance/plugins/packages/hand-plugin/hands/summarize/materialize", json={
            "expected_review_revision": reviewed.json()["review_revision"],
            "expected_materialization_revision": 0,
            "command_id": "material-hand-api-0001", "confirm": True,
        })
        assert materialized.status_code == 200
        activated = client.post("/api/ai/governance/plugins/packages/hand-plugin/hands/summarize/activate", json={
            "expected_review_revision": reviewed.json()["review_revision"],
            "expected_materialization_revision": materialized.json()["materialization_revision"],
            "expected_activation_revision": 0,
            "command_id": "activate-hand-api-0001", "confirm": True,
        })
        assert activated.status_code == 200
        writer_review = client.post("/api/ai/governance/plugins/packages/hand-plugin/hands/writer/review", json={
            "expected_state_revision": installed.json()["state_revision"],
            "command_id": "review-writer-api-0001", "confirm": True, "reason": "write isolation check",
        })
        writer_material = client.post("/api/ai/governance/plugins/packages/hand-plugin/hands/writer/materialize", json={
            "expected_review_revision": writer_review.json()["review_revision"],
            "expected_materialization_revision": 0,
            "command_id": "material-writer-api-0001", "confirm": True,
        })
        writer_active = client.post("/api/ai/governance/plugins/packages/hand-plugin/hands/writer/activate", json={
            "expected_review_revision": writer_review.json()["review_revision"],
            "expected_materialization_revision": writer_material.json()["materialization_revision"],
            "expected_activation_revision": 0,
            "command_id": "activate-writer-api-0001", "confirm": True,
        })
        assert writer_active.status_code == 200
        python_review = client.post("/api/ai/governance/plugins/packages/hand-plugin/hands/python-helper/review", json={
            "expected_state_revision": installed.json()["state_revision"],
            "command_id": "review-python-api-0001", "confirm": True, "reason": "production admission check",
        })
        python_material = client.post("/api/ai/governance/plugins/packages/hand-plugin/hands/python-helper/materialize", json={
            "expected_review_revision": python_review.json()["review_revision"],
            "expected_materialization_revision": 0,
            "command_id": "material-python-api-0001", "confirm": True,
        })
        python_active = client.post("/api/ai/governance/plugins/packages/hand-plugin/hands/python-helper/activate", json={
            "expected_review_revision": python_review.json()["review_revision"],
            "expected_materialization_revision": python_material.json()["materialization_revision"],
            "expected_activation_revision": 0,
            "command_id": "activate-python-api-0001", "confirm": True,
        })
        assert python_active.status_code == 409
        assert python_active.json()["runtime_status"] == "withheld"
        assert "plugin.hand.hand-plugin.python-helper" not in {
            item.capability_id for item in app.state.ai_runtime.capability_registry_snapshot().definitions
        }
        assert capability_id in {
            item.capability_id for item in app.state.ai_runtime.capability_registry_snapshot().definitions
        }
        enabled = client.post(
            "/api/ai/governance/plugins/packages/hand-plugin/hands/summarize/projects/alpha/enable",
            json={"expected_profile_revision": 0, "confirm": True},
        )
        assert enabled.status_code == 200
        assert enabled.json()["profile"]["enabled_plugin_ids"] == ["hand-plugin"]
        assert enabled.json()["tool"]["tool_id"] == capability_id
        assert enabled.json()["profile"]["allowed_tool_ids"] == [capability_id]
        active_hands = build_plugin_hands_activation(tmp_path).all_active()
        tools = tuple(plugin_hands_capability(item).tool_definition for item in active_hands)
        resolution = EffectiveToolPolicyResolver().resolve(
            ProjectCapabilityProfileStore(tmp_path).get("alpha").profile, tools,
            turn_allowed=tuple(tool.tool_id for tool in tools), boundary_mode="open",
        )
        assert [tool.tool_id for tool in resolution.tools] == [capability_id]

    assert capability_id not in {
        item.capability_id for item in app.state.ai_runtime.capability_registry_snapshot().definitions
    }
    assert app.state.plugin_hands_registration_manager is None

    restarted_app = create_app(SimpleNamespace(root_dir=tmp_path))
    restarted_app.state.ai_runtime = build_ai_runtime(
        restarted_app.state.container, application=restarted_app,
    )
    assert capability_id in {
        item.capability_id for item in restarted_app.state.ai_runtime.capability_registry_snapshot().definitions
    }
    with TestClient(restarted_app) as restarted_client:
        disabled = restarted_client.post("/api/ai/governance/plugins/packages/hand-plugin/hands/summarize/disable", json={
            "expected_activation_revision": activated.json()["activation_revision"],
            "command_id": "disable-hand-api-0001", "confirm": True, "reason": "revoke",
        })
        assert disabled.status_code == 200
        assert capability_id not in {
            item.capability_id for item in restarted_app.state.ai_runtime.capability_registry_snapshot().definitions
        }

    assert restarted_app.state.plugin_hands_registration_manager is None


def test_plugin_hook_governance_projects_and_revokes_existing_codex_host(tmp_path: Path) -> None:
    package = _package(tmp_path)
    app = create_app(SimpleNamespace(root_dir=tmp_path))
    assert not hasattr(app.state, "ai_runtime")

    with TestClient(app) as client:
        discovered = client.post("/api/ai/governance/plugins/packages/discover", json={
            "source_path": str(package), "command_id": "hook-api-discover-0001",
        })
        installed = client.post("/api/ai/governance/plugins/packages/hand-plugin/install-disabled", json={
            "expected_state_revision": discovered.json()["state_revision"],
            "command_id": "hook-api-install-0001", "confirm": True,
        })
        reviewed_hand = client.post("/api/ai/governance/plugins/packages/hand-plugin/hands/policy-hand/review", json={
            "expected_state_revision": installed.json()["state_revision"],
            "command_id": "hook-api-hand-review-0001", "confirm": True, "reason": "Hook isolation",
        })
        materialized = client.post("/api/ai/governance/plugins/packages/hand-plugin/hands/policy-hand/materialize", json={
            "expected_review_revision": reviewed_hand.json()["review_revision"],
            "expected_materialization_revision": 0, "command_id": "hook-api-hand-material-0001", "confirm": True,
        })
        activated_hand = client.post("/api/ai/governance/plugins/packages/hand-plugin/hands/policy-hand/activate", json={
            "expected_review_revision": reviewed_hand.json()["review_revision"],
            "expected_materialization_revision": materialized.json()["materialization_revision"],
            "expected_activation_revision": 0, "command_id": "hook-api-hand-activate-0001", "confirm": True,
        })
        assert activated_hand.status_code == 200
        reviewed_hook = client.post("/api/ai/governance/plugins/packages/hand-plugin/hooks/pre-tool-policy/review", json={
            "expected_state_revision": installed.json()["state_revision"],
            "expected_hand_activation_revision": activated_hand.json()["activation_revision"],
            "command_id": "hook-api-review-0001", "confirm": True, "reason": "direct local interception",
        })
        assert reviewed_hook.status_code == 200
        activated_hook = client.post("/api/ai/governance/plugins/packages/hand-plugin/hooks/pre-tool-policy/activate", json={
            "expected_review_revision": reviewed_hook.json()["review_revision"],
            "expected_activation_revision": 0, "command_id": "hook-api-activate-0001", "confirm": True,
        })
        assert activated_hook.status_code == 200
        assert app.state.ai_runtime is not None
        projected = tuple(
            item for item in app.state.ai_runtime._hook_host.current_snapshot().handlers
            if (item.handler_ref or "").startswith("crp://plugin-hands/")
        )
        assert len(projected) == 1 and projected[0].event.value == "PreToolUse"

        disabled = client.post("/api/ai/governance/plugins/packages/hand-plugin/hooks/pre-tool-policy/disable", json={
            "expected_activation_revision": activated_hook.json()["activation_revision"],
            "command_id": "hook-api-disable-0001", "reason": "operator revoke",
        })
        assert disabled.status_code == 200
        assert not any(
            (item.handler_ref or "").startswith("crp://plugin-hands/")
            for item in app.state.ai_runtime._hook_host.current_snapshot().handlers
        )


def test_hands_governance_api_exposes_one_non_replaying_safe_lifecycle_attempt(tmp_path: Path) -> None:
    attempt_id = _prepare_lifecycle_attempt(tmp_path)
    app = create_app(SimpleNamespace(root_dir=tmp_path))

    with TestClient(app) as client:
        observed = client.get(
            f"/api/ai/governance/plugins/packages/hand-plugin/hands/summarize/attempts/{attempt_id}"
        )
        absent = client.get(
            "/api/ai/governance/plugins/packages/other-plugin/hands/summarize/attempts/attempt-0000001"
        )

    assert observed.status_code == 200
    payload = observed.json()
    assert payload["attempt_id"] == attempt_id
    assert payload["limit"] == 1
    assert payload["replay"] is False
    assert payload["retention"] == {
        "record": "durable_while_present", "workspace": "not_reported",
    }
    assert payload["attempt"] | {"revision": None, "updated_at": None} == {
        "attempt_id": attempt_id,
        "plugin_id": "hand-plugin",
        "hand_id": "summarize",
        "project_id": "project-0000001",
        "turn_id": "turn-0000000001",
        "state": "pre_fence_cleaned",
        "outcome_status": None,
        "error_code": None,
        "revision": None,
        "updated_at": None,
        "workspace_retention": "removed_or_not_retained",
    }
    assert payload["attempt"]["revision"] == 3
    assert isinstance(payload["attempt"]["updated_at"], str)
    serialized = json.dumps(payload).lower()
    for forbidden in ("runner.exe", "argv", "environment", "workspace_ref", "private"):
        assert forbidden not in serialized
    assert absent.status_code == 404
    assert absent.json() == {"status": "plugin_hand_attempt_not_found"}


def test_hands_governance_api_rejects_invalid_lifecycle_attempt_identity(tmp_path: Path) -> None:
    app = create_app(SimpleNamespace(root_dir=tmp_path))

    with TestClient(app) as client:
        response = client.get(
            "/api/ai/governance/plugins/packages/hand-plugin/hands/summarize/attempts/short"
        )

    assert response.status_code == 400
    assert response.json() == {
        "status": "plugin_hand_attempt_rejected",
        "reason": "Plugin Hands lifecycle attempt identity is invalid",
    }


def test_hands_upgrade_governance_api_freezes_candidate_cuts_over_once_and_rolls_back(tmp_path: Path) -> None:
    package, upgrade = _package(tmp_path), _upgrade_package(tmp_path)
    app = create_app(SimpleNamespace(root_dir=tmp_path))
    app.state.ai_runtime = build_ai_runtime(app.state.container, application=app)
    capability_id = "plugin.hand.hand-plugin.summarize"

    with TestClient(app) as client:
        installed, old_activation = _activate_summarize_hand(client, package)
        assert build_plugin_hands_activation(tmp_path).resolve_active(
            "hand-plugin", hand_id="summarize"
        ).package_record_id == "hand-plugin~1.0.0"

        staged = client.post("/api/ai/governance/plugins/packages/hand-plugin/hands/summarize/upgrades/stage-candidate", json={
            "source_path": str(upgrade), "expected_state_revision": installed["state_revision"],
            "expected_package_record_id": installed["package_record_id"],
            "command_id": "upgrade-stage-api-0001", "confirm": True,
        })
        assert staged.status_code == 201
        assert staged.json()["candidate_package_record_id"] == "hand-plugin~2.0.0"

        begun = client.post("/api/ai/governance/plugins/packages/hand-plugin/hands/summarize/upgrades/begin", json={
            "cutover_id": "upgrade-api-0001", "candidate_package_record_id": staged.json()["candidate_package_record_id"],
            "expected_activation_revision": old_activation["activation_revision"], "confirm": True,
        })
        assert begun.status_code == 201
        assert begun.json()["stage"] == "prepared"
        # Production composition has no attempt projection port yet, so it
        # deliberately blocks automatic rollback.  Manual rollback remains
        # explicitly confirmed below.
        assert begun.json()["preview"]["automatic_rollback_blocked_for_write_attempts"] is True

        candidate_review = client.post(
            "/api/ai/governance/plugins/packages/hand-plugin/hands/summarize/upgrades/upgrade-api-0001/candidate/review",
            json={"expected_state_revision": installed["state_revision"], "command_id": "upgrade-candidate-review-0001", "confirm": True, "reason": "candidate upgrade review"},
        )
        assert candidate_review.status_code == 200
        candidate_material = client.post(
            "/api/ai/governance/plugins/packages/hand-plugin/hands/summarize/upgrades/upgrade-api-0001/candidate/materialize",
            json={"expected_review_revision": candidate_review.json()["review_revision"], "expected_materialization_revision": 0, "command_id": "upgrade-candidate-materialize-0001", "confirm": True},
        )
        assert candidate_material.status_code == 200

        completed = client.post(
            "/api/ai/governance/plugins/packages/hand-plugin/hands/summarize/upgrades/upgrade-api-0001/resume",
            json={"confirm": True},
        )
        assert completed.status_code == 200, completed.json()
        assert completed.json()["stage"] == "completed"
        assert build_plugin_hands_activation(tmp_path).resolve_active(
            "hand-plugin", hand_id="summarize"
        ).package_record_id == "hand-plugin~2.0.0"
        assert [item.capability_id for item in app.state.ai_runtime.capability_registry_snapshot().definitions].count(capability_id) == 1

        replayed = client.post(
            "/api/ai/governance/plugins/packages/hand-plugin/hands/summarize/upgrades/upgrade-api-0001/resume",
            json={"confirm": True},
        )
        assert replayed.status_code == 200
        assert replayed.json()["stage"] == "completed"
        assert [item.capability_id for item in app.state.ai_runtime.capability_registry_snapshot().definitions].count(capability_id) == 1

        loaded = client.get(
            "/api/ai/governance/plugins/packages/hand-plugin/hands/summarize/upgrades/upgrade-api-0001"
        )
        assert loaded.status_code == 200
        assert loaded.json()["stage"] == "completed"

        rolled_back = client.post(
            "/api/ai/governance/plugins/packages/hand-plugin/hands/summarize/upgrades/upgrade-api-0001/manual-rollback",
            json={"confirm": True, "rollback_reason": "operator-rollback-001"},
        )
        assert rolled_back.status_code == 200, rolled_back.json()
        assert rolled_back.json()["stage"] == "rolled_back"
        assert build_plugin_hands_activation(tmp_path).resolve_active(
            "hand-plugin", hand_id="summarize"
        ).package_record_id == "hand-plugin~1.0.0"
        assert [item.capability_id for item in app.state.ai_runtime.capability_registry_snapshot().definitions].count(capability_id) == 1


def test_hands_upgrade_governance_api_finalizes_promoted_primary_slot(tmp_path: Path) -> None:
    package, upgrade = _package(tmp_path), _upgrade_package(tmp_path)
    app = create_app(SimpleNamespace(root_dir=tmp_path))
    app.state.ai_runtime = build_ai_runtime(app.state.container, application=app)

    with TestClient(app) as client:
        installed, old_activation = _activate_summarize_hand(client, package)
        staged = client.post("/api/ai/governance/plugins/packages/hand-plugin/hands/summarize/upgrades/stage-candidate", json={
            "source_path": str(upgrade), "expected_state_revision": installed["state_revision"],
            "expected_package_record_id": installed["package_record_id"], "command_id": "upgrade-final-stage-0001", "confirm": True,
        })
        assert staged.status_code == 201
        begun = client.post("/api/ai/governance/plugins/packages/hand-plugin/hands/summarize/upgrades/begin", json={
            "cutover_id": "upgrade-final-0001", "candidate_package_record_id": staged.json()["candidate_package_record_id"],
            "expected_activation_revision": old_activation["activation_revision"], "confirm": True,
        })
        assert begun.status_code == 201
        review = client.post(
            "/api/ai/governance/plugins/packages/hand-plugin/hands/summarize/upgrades/upgrade-final-0001/candidate/review",
            json={"expected_state_revision": installed["state_revision"], "command_id": "upgrade-final-review-0001", "confirm": True, "reason": "finalize candidate"},
        )
        assert review.status_code == 200
        materialized = client.post(
            "/api/ai/governance/plugins/packages/hand-plugin/hands/summarize/upgrades/upgrade-final-0001/candidate/materialize",
            json={"expected_review_revision": review.json()["review_revision"], "expected_materialization_revision": 0, "command_id": "upgrade-final-materialize-0001", "confirm": True},
        )
        assert materialized.status_code == 200
        assert client.post(
            "/api/ai/governance/plugins/packages/hand-plugin/hands/summarize/upgrades/upgrade-final-0001/resume",
            json={"confirm": True},
        ).status_code == 200
        finalized = client.post(
            "/api/ai/governance/plugins/packages/hand-plugin/hands/summarize/upgrades/upgrade-final-0001/finalize",
            json={"confirm": True},
        )
        assert finalized.status_code == 200
        assert finalized.json()["stage"] == "finalized"
        # After the active-cutover record closes, ordinary resolution follows
        # the promoted durable primary slot rather than a candidate reference.
        assert build_plugin_hands_activation(tmp_path).resolve_active(
            "hand-plugin", hand_id="summarize"
        ).package_record_id == "hand-plugin~2.0.0"
