from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.routes.ppt_master_installation import router
from backend.api.ppt_master_installation_runtime import PptMasterWorkflowBoundaryError


class Service:
    last_confirm = None
    def preview(self, _project):
        return {"preview_token": "a" * 64, "revision": "b" * 40, "runtime_self_manifest_revision": "r1"}

    def status(self, _project):
        return {"operation_id": "receipt-1", "state": "installed", "receipt": "ppt-master-installation:receipt-1"}

    def confirm(self, **kwargs):
        self.last_confirm = kwargs
        assert set(kwargs["risk_acknowledgements"]) == {"network-download", "third-party-content", "project-skill-activation"}
        return {"operation_id": "receipt-1", "state": "SETTLED_OK", "receipt": "ppt-master-installation:receipt-1"}

    def rollback(self, **_kwargs):
        return {"state": "SETTLED_OK"}


def _client(*, smoke=None, capability=None):
    app = FastAPI()
    app.include_router(router)
    app.state.ppt_master_installation_runtime = Service()
    if smoke is not None:
        app.state.ppt_master_smoke = smoke
    if capability is not None:
        app.state.ppt_master_capability = capability
    return TestClient(app)


def test_preview_and_status_are_public_projections_without_runtime_paths() -> None:
    client = _client()
    preview = client.get("/api/rebuild/developer-studio/ppt-master-installation/preview")
    status = client.get("/api/rebuild/developer-studio/ppt-master-installation/status")
    assert preview.status_code == 200
    assert preview.json()["runtime_self_manifest"]["compatible"] is True
    assert "repository_url" not in preview.json()
    assert status.json() == {"receipt_id": "receipt-1", "activation": {"status": "active"}}


def test_confirm_smoke_and_rollback_require_fixed_shapes() -> None:
    client = _client(smoke=lambda receipt: receipt == "receipt-1")
    preview = client.get("/api/rebuild/developer-studio/ppt-master-installation/preview").json()
    confirmed = client.post("/api/rebuild/developer-studio/ppt-master-installation/confirm", json={"preview_id": preview["preview_id"], "confirmations": ["governed_source", "isolated_runtime", "activation_rollback"]})
    smoke = client.post("/api/rebuild/developer-studio/ppt-master-installation/smoke", json={"receipt_id": confirmed.json()["receipt_id"]})
    rollback = client.post("/api/rebuild/developer-studio/ppt-master-installation/rollback", json={"receipt_id": confirmed.json()["receipt_id"], "confirmation": "rollback_ppt_master"})
    assert confirmed.json()["activation"]["status"] == "active"
    assert smoke.json() == {"status": "passed"}
    assert rollback.json() == {"status": "rolled_back"}


def test_confirm_accepts_only_explicit_operation_scoped_loopback_egress() -> None:
    client = _client()
    preview = client.get("/api/rebuild/developer-studio/ppt-master-installation/preview").json()
    body = {
        "preview_id": preview["preview_id"],
        "confirmations": ["governed_source", "isolated_runtime", "activation_rollback"],
        "network_egress": {"mode": "loopback_http_connect", "literal_address": "127.0.0.1", "port": 7890},
        "network_egress_confirmation": "confirm_loopback_network_egress",
    }
    result = client.post("/api/rebuild/developer-studio/ppt-master-installation/confirm", json=body)
    assert result.status_code == 200
    service = client.app.state.ppt_master_installation_runtime
    assert service.last_confirm["network_egress"] == body["network_egress"]
    assert service.last_confirm["confirm_loopback_egress"] is True

    body["network_egress_confirmation"] = "yes"
    assert client.post("/api/rebuild/developer-studio/ppt-master-installation/confirm", json=body).status_code == 400


def test_gate_ask_is_projected_as_versioned_workflow_without_effect_claim() -> None:
    class AskingService(Service):
        def confirm(self, **_kwargs):
            raise PptMasterWorkflowBoundaryError("decision required", {
                "schema_version": "1.0.0", "action": "need_user",
                "progression_mode": "ask",
                "reasons": ["permission_expansion", "gate_ask"],
                "decision_ref": None,
            })

    app = FastAPI()
    app.include_router(router)
    app.state.ppt_master_installation_runtime = AskingService()
    client = TestClient(app)
    preview = client.get("/api/rebuild/developer-studio/ppt-master-installation/preview").json()
    result = client.post(
        "/api/rebuild/developer-studio/ppt-master-installation/confirm",
        json={"preview_id": preview["preview_id"], "confirmations": [
            "governed_source", "isolated_runtime", "activation_rollback",
        ]},
    )
    assert result.status_code == 409
    assert result.json()["workflow"]["action"] == "need_user"
    assert result.json()["workflow"]["reasons"] == ["permission_expansion", "gate_ask"]


def test_smoke_does_not_fall_back_to_shell_execution() -> None:
    client = _client()
    result = client.post("/api/rebuild/developer-studio/ppt-master-installation/smoke", json={"receipt_id": "receipt-1"})
    assert result.status_code == 503
    assert result.json()["message"] == "PPT Master smoke runtime is unavailable"


def test_rollback_projects_pending_until_its_effect_settles() -> None:
    class PendingService(Service):
        def rollback(self, **_kwargs):
            return {"state": "INFLIGHT"}

    app = FastAPI()
    app.include_router(router)
    app.state.ppt_master_installation_runtime = PendingService()
    client = TestClient(app)
    result = client.post("/api/rebuild/developer-studio/ppt-master-installation/rollback", json={"receipt_id": "receipt-1", "confirmation": "rollback_ppt_master"})
    assert result.status_code == 202
    assert result.json() == {"status": "pending"}


def test_status_projects_runtime_rolled_back_state() -> None:
    class RolledBackService(Service):
        def status(self, _project):
            return {"operation_id": "receipt-1", "state": "rolled_back", "receipt": "ppt-master-installation:receipt-1"}

    app = FastAPI()
    app.include_router(router)
    app.state.ppt_master_installation_runtime = RolledBackService()
    client = TestClient(app)
    assert client.get("/api/rebuild/developer-studio/ppt-master-installation/status").json() == {
        "receipt_id": "receipt-1", "activation": {"status": "rolled_back"},
    }


def test_result_download_uses_host_capability_and_never_exposes_path(tmp_path: Path) -> None:
    output = tmp_path / "presentation.pptx"
    output.write_bytes(b"pptx")

    class Capability:
        def resolve_result(self, *, project_id, result_id):
            assert project_id == "default" and result_id == "pptx-result-0123456789abcdef01234567"
            return output

    client = _client(capability=Capability())
    result = client.get("/api/rebuild/developer-studio/ppt-master-installation/results/pptx-result-0123456789abcdef01234567")
    assert result.status_code == 200 and result.content == b"pptx"
    assert result.headers["cache-control"] == "no-store"
    assert "presentation.pptx" in result.headers["content-disposition"]
    assert str(tmp_path) not in result.text


def test_result_download_rejects_unavailable_and_traversal_handles() -> None:
    class Capability:
        def resolve_result(self, **_kwargs):
            raise ValueError("tampered receipt")

    client = _client(capability=Capability())
    missing = client.get("/api/rebuild/developer-studio/ppt-master-installation/results/pptx-result-0123456789abcdef01234567")
    traversal = client.get("/api/rebuild/developer-studio/ppt-master-installation/results/not-a-result")
    assert missing.status_code == 404 and missing.json() == {"message": "presentation result is unavailable"}
    assert traversal.status_code == 404
