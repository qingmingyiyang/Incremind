from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.context_graph_import_composition import (
    ContextGraphImportEvidence,
    ContextGraphImportPreview,
    ContextGraphImportResult,
)
from backend.api.context_graph_import_selection_authority import (
    ContextGraphImportSelectionReceipt,
)
from backend.api.desktop_session import DESKTOP_SESSION_HEADER
from backend.api.routes.context_graph import router
from backend.security.file_grant import DesktopFileGrant, sign_desktop_file_grant


SECRET = "s" * 43
INSTANCE = "linemap-route-instance"


class _Registry:
    def __init__(self, supported: bool = True) -> None:
        self.supported = supported

    def resolve(self, source_type: str):
        return object() if self.supported and source_type == "thoughtdag" else None


class _SelectionAuthority:
    def __init__(self, receipt: ContextGraphImportSelectionReceipt | None = None) -> None:
        self.receipt = receipt or ContextGraphImportSelectionReceipt(
            selection_id="ctxsel-opaque", project_id="project-a",
            source_type="thoughtdag", display_name="graph.thoughtdag.json",
            expires_at="2026-08-30T04:00:00Z",
        )
        self.calls: list[tuple[object, ...]] = []
        self.error: Exception | None = None

    def create(self, *args, file_grant):
        self.calls.append((*args, file_grant))
        if self.error is not None:
            raise self.error
        return self.receipt


class _ImportService:
    def __init__(self, result: ContextGraphImportResult | None = None) -> None:
        self.result = result or _success_result()
        self.calls = []

    def import_file(self, command):
        self.calls.append(command)
        return self.result


def _success_result() -> ContextGraphImportResult:
    return ContextGraphImportResult(
        record=object(),
        preview=ContextGraphImportPreview(
            project_id="project-a", graph_id="graph-a", graph_revision="r1",
            source_type="thoughtdag", source_revision="source-r1", node_count=2,
            edge_count=1, selected_outputs=("n2",), token_estimate=21,
            integrity_issue_codes=(), evidence_ref="context-import-evidence-1",
        ),
        evidence=ContextGraphImportEvidence(
            evidence_ref="context-import-evidence-1", command_id="import-command-1",
            project_id="project-a", selection_id="ctxsel-opaque",
            expected_predecessor=None, graph_id="graph-a", graph_revision="r1",
            source_type="thoughtdag", source_revision="source-r1",
            authorized_file_revision="mtime-1:size-3",
            importer_id="thought_graph_context.ThoughtDAGImporter", importer_revision="1.0.0",
            capability_id="thought_graph_context", capability_revision="4.1.0",
            actor_id=f"desktop:{INSTANCE}", selection_evidence_ref="selection-evidence-1",
            imported_at="2026-08-30T03:01:00Z",
        ),
    )


def _configure_desktop(monkeypatch) -> None:
    monkeypatch.setenv("CHRIPTMAS_DESKTOP_SESSION_MODE", "desktop_production")
    monkeypatch.setenv("CHRIPTMAS_DESKTOP_SESSION_SECRET", SECRET)
    monkeypatch.setenv("CHRIPTMAS_DESKTOP_INSTANCE_ID", INSTANCE)
    monkeypatch.setenv("CHRIPTMAS_DESKTOP_NONCE", "n" * 43)
    monkeypatch.setenv("CHRIPTMAS_DESKTOP_PROTOCOL_VERSION", "desktop-loopback/1")
    monkeypatch.setenv(
        "CHRIPTMAS_DESKTOP_SESSION_EXPIRES_AT",
        (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    )
    monkeypatch.setenv("CHRIPTMAS_DESKTOP_ALLOWED_ORIGIN", "http://127.0.0.1:8317")


def _grant_headers() -> dict[str, str]:
    grant = DesktopFileGrant(
        grant_id="file-grant-" + "a" * 43,
        session_instance_id=INSTANCE,
        display_name="graph.thoughtdag.json",
        media_type="application/json",
        source_kind="file",
        size_bytes=3,
        sha256="a" * 64,
        expires_at_ms=int((datetime.now(UTC) + timedelta(minutes=1)).timestamp() * 1000),
    )
    return {
        DESKTOP_SESSION_HEADER: SECRET,
        "X-Chriptmas-File-Grant": grant.grant_id,
        "X-Chriptmas-File-Session": grant.session_instance_id,
        "X-Chriptmas-File-Name": base64.urlsafe_b64encode(
            grant.display_name.encode(),
        ).decode().rstrip("="),
        "X-Chriptmas-File-Media-Type": grant.media_type,
        "X-Chriptmas-File-Source-Kind": grant.source_kind,
        "X-Chriptmas-File-Size": str(grant.size_bytes),
        "X-Chriptmas-File-Sha256": grant.sha256,
        "X-Chriptmas-File-Expires": str(grant.expires_at_ms),
        "X-Chriptmas-File-Signature": sign_desktop_file_grant(
            grant, session_secret=SECRET,
        ),
    }


def _client(*, registry=None, authority=None, service=None) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.state.context_graph_import_registry = registry or _Registry()
    app.state.context_graph_import_selection_authority = authority or _SelectionAuthority()
    app.state.context_graph_import_service = service or _ImportService()
    return TestClient(app)


def _selection_body(**extra) -> dict[str, object]:
    return {
        "command_id": "selection-command-1", "project_id": "project-a",
        "source_type": "thoughtdag", "asset_id": "asset-a", **extra,
    }


def _import_body(**extra) -> dict[str, object]:
    return {
        "command_id": "import-command-1", "project_id": "project-a",
        "source_type": "thoughtdag", "selection_id": "ctxsel-opaque",
        "expected_predecessor": None, "confirm_read": True, **extra,
    }


def test_selection_requires_desktop_session_and_signed_grant_and_keeps_authority_server_side(monkeypatch) -> None:
    _configure_desktop(monkeypatch)
    authority = _SelectionAuthority()
    with _client(authority=authority) as client:
        response = client.post(
            "/api/rebuild/context-graph-import-selections",
            json=_selection_body(), headers=_grant_headers(),
        )

    assert response.status_code == 201
    assert response.headers["cache-control"] == "no-store"
    assert set(response.json()) == {
        "selection_id", "project_id", "source_type", "display_name", "expires_at",
    }
    assert all(word not in str(response.json()).lower() for word in ("path", "asset", "grant", "secret"))
    call = authority.calls[0]
    assert call[:6] == (
        "selection-command-1", "project-a", "thoughtdag", "asset-a",
        f"desktop:{INSTANCE}", INSTANCE,
    )
    assert isinstance(call[6], DesktopFileGrant)


def test_import_derives_actor_and_session_and_returns_content_free_preview(monkeypatch) -> None:
    _configure_desktop(monkeypatch)
    service = _ImportService()
    with _client(service=service) as client:
        response = client.post("/api/rebuild/context-graphs/imports", json=_import_body(), headers={DESKTOP_SESSION_HEADER: SECRET})

    assert response.status_code == 201
    assert response.headers["cache-control"] == "no-store"
    assert set(response.json()) == {
        "project_id", "graph_id", "graph_revision", "source_type", "source_revision",
        "node_count", "edge_count", "selected_outputs", "token_estimate",
        "integrity_issue_codes", "evidence_ref", "importer_id", "importer_revision",
        "capability_id", "capability_revision",
    }
    assert not {"snapshot", "content", "path", "asset", "grant", "secret"} & set(response.json())
    command = service.calls[0]
    assert command.actor_id == f"desktop:{INSTANCE}"
    assert command.session_instance_id == INSTANCE


@pytest.mark.parametrize(
    ("path", "body"),
    (
        ("/api/rebuild/context-graph-import-selections", _selection_body(path="C:/forged.thoughtdag.json")),
        ("/api/rebuild/context-graph-import-selections", _selection_body(grant="forged")),
        ("/api/rebuild/context-graph-import-selections", _selection_body(importer="forged")),
        ("/api/rebuild/context-graph-import-selections", _selection_body(capability="forged")),
        ("/api/rebuild/context-graph-import-selections", _selection_body(limits={"max_nodes": 999999})),
        ("/api/rebuild/context-graphs/imports", _import_body(path="C:/forged.thoughtdag.json")),
        ("/api/rebuild/context-graphs/imports", _import_body(grant="forged")),
        ("/api/rebuild/context-graphs/imports", _import_body(importer="forged")),
        ("/api/rebuild/context-graphs/imports", _import_body(capability="forged")),
        ("/api/rebuild/context-graphs/imports", _import_body(actor_id="forged")),
        ("/api/rebuild/context-graphs/imports", _import_body(session_instance_id="forged")),
    ),
)
def test_import_routes_fail_closed_for_client_authority_fields(monkeypatch, path: str, body: dict[str, object]) -> None:
    _configure_desktop(monkeypatch)
    headers = _grant_headers() if path.endswith("selections") else {DESKTOP_SESSION_HEADER: SECRET}
    with _client() as client:
        response = client.post(path, json=body, headers=headers)

    assert response.status_code == 400
    assert response.headers["cache-control"] == "no-store"


def test_selection_rejects_unsupported_source_and_bad_grant(monkeypatch) -> None:
    _configure_desktop(monkeypatch)
    with _client(registry=_Registry(supported=False)) as client:
        unsupported = client.post(
            "/api/rebuild/context-graph-import-selections",
            json=_selection_body(), headers=_grant_headers(),
        )
    with _client() as client:
        bad_grant = client.post(
            "/api/rebuild/context-graph-import-selections",
            json=_selection_body(), headers={DESKTOP_SESSION_HEADER: SECRET},
        )

    assert unsupported.status_code == 409
    assert unsupported.json()["code"] == "importer_unavailable"
    assert bad_grant.status_code == 403
    assert bad_grant.json()["code"] == "desktop_file_grant_rejected"
    assert unsupported.headers["cache-control"] == "no-store"
    assert bad_grant.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    ("error_code", "expected_status"),
    (("snapshot_conflict", 409), ("file_authorization_rejected", 403), ("importer_execution_unavailable", 503), ("importer_rejected", 400)),
)
def test_import_maps_composition_failures_and_never_caches(monkeypatch, error_code: str, expected_status: int) -> None:
    _configure_desktop(monkeypatch)
    service = _ImportService(ContextGraphImportResult(error_code=error_code))
    with _client(service=service) as client:
        response = client.post("/api/rebuild/context-graphs/imports", json=_import_body(), headers={DESKTOP_SESSION_HEADER: SECRET})

    assert response.status_code == expected_status
    assert response.json()["code"] == error_code
    assert response.headers["cache-control"] == "no-store"


def test_import_routes_reject_missing_or_nonproduction_desktop_session(monkeypatch) -> None:
    monkeypatch.delenv("CHRIPTMAS_DESKTOP_SESSION_MODE", raising=False)
    with _client() as client:
        selection = client.post("/api/rebuild/context-graph-import-selections", json=_selection_body(), headers=_grant_headers())
        imported = client.post("/api/rebuild/context-graphs/imports", json=_import_body())

    assert selection.status_code == 403
    assert selection.json()["code"] == "desktop_file_selection_required"
    assert imported.status_code == 403
    assert imported.json()["code"] == "desktop_import_session_required"
