from __future__ import annotations

from types import SimpleNamespace
from fastapi import FastAPI
from fastapi.testclient import TestClient
from backend.api.desktop_session import DesktopSession

from backend.api.external_extension_install_workflow import (
    ExternalExtensionEffectProjection,
    ExternalExtensionInstallConfirmation,
    ExternalExtensionInstallPreview,
    ExternalExtensionInstallWorkflowError,
)
from backend.api.external_extension_mcp_import import MCPDisabledImportPreview
from backend.api.external_extension_runtime_startup import (
    register_external_extension_runtime,
)
from backend.api.routes.external_extensions import router
from core.effect_log import EffectState, build_effect_runtime
from core.external_extension_runtime.installation import (
    InstallationRevisionSummary,
    InstallationSnapshot,
)


class _Workflow:
    def __init__(self) -> None:
        self.preview_calls: list[dict[str, object]] = []
        self.confirm_calls: list[dict[str, object]] = []
        self.lifecycle_calls: list[dict[str, object]] = []
        self.upgrade_calls: list[dict[str, object]] = []
        self.mcp_import_calls: list[dict[str, object]] = []

    def preview(self, prompt: str, project_id: str, **kwargs: object) -> ExternalExtensionInstallPreview:
        self.preview_calls.append({"prompt": prompt, "project_id": project_id, **kwargs})
        return ExternalExtensionInstallPreview(
            "intake-001", "intake-001", "review_required", ("network",),
            ("review-source",), "fixture-skill",
            (ExternalExtensionEffectProjection("op-preview", EffectState.SETTLED_OK, "receipt-preview"),),
        )

    def confirm(self, preview_id: str, **kwargs: object) -> ExternalExtensionInstallConfirmation:
        self.confirm_calls.append({"preview_id": preview_id, **kwargs})
        if kwargs["project_id"] == "other-project":
            raise ExternalExtensionInstallWorkflowError("preview does not belong to the requested project")
        if tuple(kwargs["confirmations"]) != ("review-source",):
            raise ExternalExtensionInstallWorkflowError("confirmation ids do not exactly match the immutable review plan")
        return ExternalExtensionInstallConfirmation(
            preview_id, preview_id, "active",
            InstallationSnapshot("project-001", "fixture-skill", 2, 1, 1, "revision-001", "active", 1, "revision-001"),
            (ExternalExtensionEffectProjection("op-confirm", EffectState.SETTLED_OK, "receipt-confirm"),),
        )

    def installation_status(self, extension_id: str, **kwargs: object):
        if kwargs["project_id"] != "project-001":
            raise ExternalExtensionInstallWorkflowError(
                "installation does not belong to the requested project"
            )
        return (
            InstallationSnapshot(
                "project-001", extension_id, 4, 2, None, None, None,
                2, "crp://external-extension-installation-revisions/project-001~fixture-skill~2",
            ),
            (
                InstallationRevisionSummary(
                    2,
                    "crp://external-extension-installation-revisions/project-001~fixture-skill~2",
                    "crp://external-extension-intakes/intake-002",
                    "crp://artifact-receipts/receipt-002",
                    "crp://external-extension-review-confirmations/review-002",
                    "installed_disabled",
                    True,
                    True,
                    False,
                ),
                InstallationRevisionSummary(
                    1,
                    "crp://external-extension-installation-revisions/project-001~fixture-skill~1",
                    "crp://external-extension-intakes/intake-001",
                    "crp://artifact-receipts/receipt-001",
                    "crp://external-extension-review-confirmations/review-001",
                    "installed_disabled",
                    True,
                    False,
                    False,
                ),
            ),
        )

    def execute_lifecycle_action(self, extension_id: str, **kwargs: object):
        self.lifecycle_calls.append({"extension_id": extension_id, **kwargs})
        active = None if kwargs["action"] in {"disable", "uninstall"} else kwargs["target_revision_ref"]
        uninstalled = kwargs["action"] == "uninstall"
        return SimpleNamespace(
            command_id="lifecycle-command-001",
            effect=ExternalExtensionEffectProjection(
                "lifecycle-effect-001", EffectState.SETTLED_OK, "lifecycle-receipt-001",
            ),
            snapshot=InstallationSnapshot(
                "project-001", extension_id, int(kwargs["expected_state_revision"]) + 1,
                2, None, None, None, 1 if active else None, active,
                uninstalled,
                "crp://external-extension-terminal-receipts/uninstall-001" if uninstalled else None,
            ),
        )

    def upgrade_from_intake(self, extension_id: str, **kwargs: object):
        self.upgrade_calls.append({"extension_id": extension_id, **kwargs})
        return ExternalExtensionInstallConfirmation(
            kwargs["intake_ref"], kwargs["intake_ref"], "active",
            InstallationSnapshot(
                "project-001", extension_id, int(kwargs["expected_state_revision"]) + 3,
                3, None, None, None, 3,
                "crp://external-extension-installation-revisions/project-001~fixture-skill~3",
            ),
            (ExternalExtensionEffectProjection(
                "upgrade-effect-001", EffectState.SETTLED_OK, "upgrade-receipt-001",
            ),),
        )

    def preview_mcp_import(self, **kwargs: object) -> MCPDisabledImportPreview:
        self.mcp_import_calls.append(dict(kwargs))
        return MCPDisabledImportPreview(
            artifact_ref="crp://external-extension-artifacts/hidden",
            review_receipt_ref="crp://external-extension-mcp-import-receipts/review-001",
            migration_id="migration-001",
            migration_revision=1,
            active_snapshot_revision=3,
            server_ids=("fixture",),
            state="previewed",
        )


def _client(workflow: object | None = None) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    if workflow is not None:
        app.state.external_extension_install_workflow = workflow
    return TestClient(app)


def test_preview_and_confirm_expose_only_safe_server_owned_projections() -> None:
    workflow = _Workflow()
    client = _client(workflow)

    preview = client.post("/api/rebuild/external-extensions/install/preview", json={
        "prompt": "安装 https://github.com/example/extensions",
        "project_id": "project-001",
        "requested_ref": "a" * 40,
    })
    confirmed = client.post("/api/rebuild/external-extensions/install/confirm", json={
        "preview_id": "intake-001", "project_id": "project-001",
        "confirmations": ["review-source"], "expected_state_revision": 0,
    })

    assert preview.status_code == confirmed.status_code == 200
    assert preview.json()["effects"] == [{"operation_id": "op-preview", "state": "SETTLED_OK", "receipt_ref": "receipt-preview"}]
    assert confirmed.json()["installation"] == {
        "extension_id": "fixture-skill", "status": "active", "state_revision": 2,
        "active_revision_ref": "revision-001",
    }
    assert workflow.preview_calls == [{
        "prompt": "安装 https://github.com/example/extensions", "project_id": "project-001",
        "requested_ref": "a" * 40, "subpath": None,
    }]
    assert workflow.confirm_calls[0]["actor"] == "local-development-session"
    assert "gate_fact" not in workflow.confirm_calls[0]


def test_dto_rejects_extra_gate_identity_without_touching_workflow() -> None:
    workflow = _Workflow()
    response = _client(workflow).post("/api/rebuild/external-extensions/install/preview", json={
        "prompt": "install", "project_id": "project-001", "gate_fact": {"decision": "ALLOW"},
    })

    assert response.status_code == 422
    assert workflow.preview_calls == []


def test_installation_status_exposes_project_scoped_path_free_history() -> None:
    response = _client(_Workflow()).get(
        "/api/rebuild/external-extensions/installations/fixture-skill",
        params={"project_id": "project-001"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "active"
    assert payload["state_revision"] == 4
    assert [item["revision"] for item in payload["revisions"]] == [2, 1]
    assert payload["revisions"][0]["active"] is True
    assert payload["revisions"][1]["health_verified"] is True
    serialized = response.text.lower()
    assert "artifact_ref" not in serialized
    assert "content_sha256" not in serialized
    assert "materialized" not in serialized


def test_installation_status_hides_cross_project_installation() -> None:
    response = _client(_Workflow()).get(
        "/api/rebuild/external-extensions/installations/fixture-skill",
        params={"project_id": "project-foreign"},
    )

    assert response.status_code == 404


def test_disable_is_authenticated_cas_bound_and_needs_no_redundant_target_choice() -> None:
    workflow = _Workflow()
    response = _client(workflow).post(
        "/api/rebuild/external-extensions/installations/fixture-skill/lifecycle",
        json={
            "project_id": "project-001",
            "action": "disable",
            "expected_state_revision": 4,
            "reason": "Disable this Skill from the current project.",
        },
    )

    assert response.status_code == 200
    assert response.json()["installation"]["status"] == "disabled"
    assert workflow.lifecycle_calls == [{
        "extension_id": "fixture-skill",
        "project_id": "project-001",
        "action": "disable",
        "expected_state_revision": 4,
        "actor": "local-development-session",
        "reason": "Disable this Skill from the current project.",
        "target_revision_ref": None,
    }]


def test_rollback_requires_an_exact_user_selected_revision() -> None:
    workflow = _Workflow()
    response = _client(workflow).post(
        "/api/rebuild/external-extensions/installations/fixture-skill/lifecycle",
        json={
            "project_id": "project-001",
            "action": "rollback",
            "expected_state_revision": 4,
            "reason": "Restore the reviewed first revision.",
        },
    )

    assert response.status_code == 422
    assert workflow.lifecycle_calls == []


def test_upgrade_selects_an_exact_reviewed_intake_and_current_cas() -> None:
    workflow = _Workflow()
    response = _client(workflow).post(
        "/api/rebuild/external-extensions/installations/fixture-skill/upgrade",
        json={
            "project_id": "project-001",
            "intake_ref": "crp://external-extension-intake-receipts/intake-003",
            "confirmations": ["review-source"],
            "expected_state_revision": 4,
            "reason": "Upgrade this Skill to the reviewed intake.",
        },
    )

    assert response.status_code == 200
    assert response.json()["installation"]["active_revision_ref"].endswith("~3")
    assert workflow.upgrade_calls == [{
        "extension_id": "fixture-skill",
        "intake_ref": "crp://external-extension-intake-receipts/intake-003",
        "project_id": "project-001",
        "confirmations": ("review-source",),
        "expected_state_revision": 4,
        "actor": "local-development-session",
        "reason": "Upgrade this Skill to the reviewed intake.",
    }]


def test_mcp_import_preview_uses_unified_authenticated_extension_route() -> None:
    workflow = _Workflow()
    candidate = {"schema_version": "1.1.0", "servers": []}
    response = _client(workflow).post(
        "/api/rebuild/external-extensions/install/mcp/preview",
        json={
            "intake_ref": "crp://external-extension-intake-receipts/intake-001",
            "project_id": "project-001",
            "reviewed_candidate": candidate,
            "confirmations": ["activate_external_extension"],
            "reason": "Review this disabled MCP candidate.",
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "status": "previewed",
        "review_receipt_ref": "crp://external-extension-mcp-import-receipts/review-001",
        "migration_id": "migration-001",
        "migration_revision": 1,
        "active_snapshot_revision": 3,
        "server_ids": ["fixture"],
    }
    assert workflow.mcp_import_calls == [{
        "intake_ref": "crp://external-extension-intake-receipts/intake-001",
        "project_id": "project-001",
        "reviewed_candidate": candidate,
        "confirmations": ("activate_external_extension",),
        "actor": "local-development-session",
        "reason": "Review this disabled MCP candidate.",
    }]
    assert "artifact_ref" not in response.text


def test_mcp_import_preview_rejects_caller_owned_actor_and_extra_gate_fact() -> None:
    workflow = _Workflow()
    response = _client(workflow).post(
        "/api/rebuild/external-extensions/install/mcp/preview",
        json={
            "intake_ref": "crp://external-extension-intake-receipts/intake-001",
            "project_id": "project-001",
            "reviewed_candidate": {"schema_version": "1.1.0", "servers": []},
            "confirmations": [],
            "actor": "spoofed",
            "gate_fact": {"decision": "ALLOW"},
        },
    )

    assert response.status_code == 422
    assert workflow.mcp_import_calls == []


def test_uninstall_is_a_distinct_authenticated_lifecycle_action() -> None:
    workflow = _Workflow()
    response = _client(workflow).post(
        "/api/rebuild/external-extensions/installations/fixture-skill/lifecycle",
        json={
            "project_id": "project-001",
            "action": "uninstall",
            "expected_state_revision": 4,
            "reason": "Remove the governed Skill from this project.",
        },
    )

    assert response.status_code == 200
    assert response.json()["installation"]["status"] == "uninstalled"
    assert workflow.lifecycle_calls[0]["action"] == "uninstall"


def test_confirm_rejects_caller_supplied_actor_without_touching_workflow() -> None:
    workflow = _Workflow()
    response = _client(workflow).post("/api/rebuild/external-extensions/install/confirm", json={
        "preview_id": "intake-001", "project_id": "project-001",
        "confirmations": ["review-source"], "actor": "spoofed-principal",
    })

    assert response.status_code == 422
    assert workflow.confirm_calls == []


def test_runtime_absence_is_service_unavailable() -> None:
    response = _client().post("/api/rebuild/external-extensions/install/preview", json={
        "prompt": "install", "project_id": "project-001",
    })

    assert response.status_code == 503


def test_route_requires_precomposed_workflow_and_never_reaches_raw_runtime_handles(
    tmp_path,
) -> None:
    application = FastAPI()
    application.include_router(router)
    runtime = build_effect_runtime(
        tmp_path / ".rebuild-data" / "jobs.sqlite3",
        owner_id="route-composition-test",
    )
    handles = register_external_extension_runtime(tmp_path, runtime)
    application.state.external_extension_install_workflow = handles.install_workflow

    with TestClient(application) as client:
        response = client.post(
            "/api/rebuild/external-extensions/install/preview",
            json={
                "prompt": "安装技能 useful summarizer",
                "project_id": "project-001",
            },
        )

    assert response.status_code == 200
    assert response.json()["status"] == "requires_exact_source"
    assert response.json()["risks"] == ["source_resolution_required"]
    assert application.state.external_extension_install_workflow is not None


def test_cross_project_and_wrong_confirmation_do_not_invoke_installation() -> None:
    workflow = _Workflow()
    client = _client(workflow)

    foreign = client.post("/api/rebuild/external-extensions/install/confirm", json={
        "preview_id": "intake-001", "project_id": "other-project", "confirmations": ["review-source"],
    })
    wrong_confirmation = client.post("/api/rebuild/external-extensions/install/confirm", json={
        "preview_id": "intake-001", "project_id": "project-001", "confirmations": ["wrong"],
    })

    assert foreign.status_code == 404
    assert wrong_confirmation.status_code == 409
    assert len(workflow.confirm_calls) == 2


def test_confirm_derives_actor_from_authenticated_desktop_session(monkeypatch) -> None:
    workflow = _Workflow()
    session = DesktopSession(
        secret="s" * 43,
        instance_id="desktop-instance-001",
        nonce="n" * 43,
        expires_at="2099-01-01T00:00:00+00:00",
        allowed_origin="http://127.0.0.1:8001",
    )
    monkeypatch.setattr(
        "backend.api.routes.external_extensions.desktop_session", lambda: session,
    )
    monkeypatch.setattr(
        "backend.api.routes.external_extensions.desktop_session_authorized",
        lambda value: value == session.secret,
    )

    response = _client(workflow).post(
        "/api/rebuild/external-extensions/install/confirm",
        headers={"X-Chriptmas-Desktop-Session": session.secret},
        json={
            "preview_id": "intake-001", "project_id": "project-001",
            "confirmations": ["review-source"], "expected_state_revision": 0,
        },
    )

    assert response.status_code == 200
    assert workflow.confirm_calls[0]["actor"] == "desktop:desktop-instance-001"


def test_desktop_session_rejects_missing_session_proof_before_workflow(monkeypatch) -> None:
    workflow = _Workflow()
    session = DesktopSession(
        secret="s" * 43,
        instance_id="desktop-instance-001",
        nonce="n" * 43,
        expires_at="2099-01-01T00:00:00+00:00",
        allowed_origin="http://127.0.0.1:8001",
    )
    monkeypatch.setattr(
        "backend.api.routes.external_extensions.desktop_session", lambda: session,
    )
    monkeypatch.setattr(
        "backend.api.routes.external_extensions.desktop_session_authorized",
        lambda value: False,
    )

    response = _client(workflow).post(
        "/api/rebuild/external-extensions/install/preview",
        json={"prompt": "install", "project_id": "project-001"},
    )

    assert response.status_code == 403
    assert workflow.preview_calls == []
