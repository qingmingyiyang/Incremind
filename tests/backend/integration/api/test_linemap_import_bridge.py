from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from fastapi.testclient import TestClient

from backend.api.app import create_app
from backend.api.desktop_session import DESKTOP_SESSION_HEADER
from backend.security.file_grant import DesktopFileGrant, sign_desktop_file_grant
from core.storage_provider import SQLiteStructuredRecordStore


_SECRET = "s" * 43
_INSTANCE = "linemap-import-bridge-instance"


def _configure_desktop(monkeypatch) -> None:
    monkeypatch.setenv("CHRIPTMAS_DESKTOP_SESSION_MODE", "desktop_production")
    monkeypatch.setenv("CHRIPTMAS_DESKTOP_SESSION_SECRET", _SECRET)
    monkeypatch.setenv("CHRIPTMAS_DESKTOP_INSTANCE_ID", _INSTANCE)
    monkeypatch.setenv("CHRIPTMAS_DESKTOP_NONCE", "n" * 43)
    monkeypatch.setenv("CHRIPTMAS_DESKTOP_PROTOCOL_VERSION", "desktop-loopback/1")
    monkeypatch.setenv(
        "CHRIPTMAS_DESKTOP_SESSION_EXPIRES_AT",
        (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    )
    monkeypatch.setenv("CHRIPTMAS_DESKTOP_ALLOWED_ORIGIN", "http://127.0.0.1:8317")


def _file_headers(content: bytes, *, name: str, media_type: str) -> dict[str, str]:
    # codex-hash-gate: target="in-memory LineMap API fixture"; transfer=cross-process; risk=transfer-corruption; accuracy=byte-exact
    digest = hashlib.sha256(content).hexdigest()
    grant = DesktopFileGrant(
        grant_id="file-grant-" + "a" * 43,
        session_instance_id=_INSTANCE,
        display_name=name,
        media_type=media_type,
        source_kind="file",
        size_bytes=len(content),
        sha256=digest,
        expires_at_ms=int((datetime.now(UTC) + timedelta(minutes=1)).timestamp() * 1000),
    )
    return {
        DESKTOP_SESSION_HEADER: _SECRET,
        "X-Chriptmas-File-Grant": grant.grant_id,
        "X-Chriptmas-File-Session": grant.session_instance_id,
        "X-Chriptmas-File-Name": base64.urlsafe_b64encode(
            grant.display_name.encode("utf-8"),
        ).decode("ascii").rstrip("="),
        "X-Chriptmas-File-Media-Type": grant.media_type,
        "X-Chriptmas-File-Source-Kind": grant.source_kind,
        "X-Chriptmas-File-Size": str(grant.size_bytes),
        "X-Chriptmas-File-Sha256": grant.sha256,
        "X-Chriptmas-File-Expires": str(grant.expires_at_ms),
        "X-Chriptmas-File-Signature": sign_desktop_file_grant(
            grant, session_secret=_SECRET,
        ),
        "Content-Type": "application/octet-stream",
    }


def _thoughtdag_bytes() -> bytes:
    return json.dumps({
        "version": 1,
        "name": "bridge-fixture",
        "exportedAt": "2026-08-30T00:00:00Z",
        "nodes": [
            {
                "id": "n1", "type": "thought", "position": {"x": 0, "y": 0},
                "data": {"question": "Question", "response": "Answer"},
            },
            {
                "id": "n2", "type": "thought", "position": {"x": 0, "y": 100},
                "data": {"question": "Conclusion", "response": "Result"},
            },
        ],
        "edges": [{"id": "e1", "source": "n1", "target": "n2"}],
        "events": [{"t": "2026-08-30T00:00:00Z", "op": "ask"}],
    }, ensure_ascii=False).encode("utf-8")


def _import_via_production_bridge(
    client: TestClient,
    *,
    content: bytes,
    name: str,
    media_type: str,
    source_type: str,
    command_suffix: str,
) -> tuple[dict[str, object], dict[str, object]]:
    streamed = client.post(
        "/api/rebuild/workbench/original-asset-stream",
        headers=_file_headers(content, name=name, media_type=media_type),
        content=content,
    )
    assert streamed.status_code == 201, streamed.text
    asset = streamed.json()
    selection = client.post(
        "/api/rebuild/context-graph-import-selections",
        headers=_file_headers(content, name=name, media_type=media_type),
        json={
            "command_id": f"selection-command-{command_suffix}",
            "project_id": "project-bridge",
            "source_type": source_type,
            "asset_id": asset["asset_id"],
        },
    )
    assert selection.status_code == 201, selection.text
    selection_payload = selection.json()
    imported = client.post(
        "/api/rebuild/context-graphs/imports",
        headers={DESKTOP_SESSION_HEADER: _SECRET},
        json={
            "command_id": f"import-command-{command_suffix}",
            "project_id": "project-bridge",
            "source_type": source_type,
            "selection_id": selection_payload["selection_id"],
            "expected_predecessor": None,
            "confirm_read": True,
        },
    )
    assert imported.status_code == 201, imported.text
    return asset, imported.json()


def test_real_desktop_bridge_imports_thoughtdag_as_read_only_snapshot_and_survives_restart(
    tmp_path, monkeypatch,
) -> None:
    _configure_desktop(monkeypatch)
    content = _thoughtdag_bytes()
    with TestClient(create_app(SimpleNamespace(root_dir=tmp_path))) as client:
        asset, preview = _import_via_production_bridge(
            client,
            content=content,
            name="bridge.thoughtdag.json",
            media_type="application/json",
            source_type="thoughtdag",
            command_suffix="thoughtdag",
        )
        record = client.app.state.context_graph_runtime.snapshots.revision(
            "project-bridge", str(preview["graph_id"]), str(preview["graph_revision"]),
        )

    assert record is not None
    assert record.snapshot.source_type == "thoughtdag"
    assert record.snapshot.selected_outputs == ("n2",)
    assert record.permission_evidence_refs == (preview["evidence_ref"],)
    assert preview["node_count"] == 2
    assert preview["edge_count"] == 1
    assert set(preview).isdisjoint({"path", "content", "asset", "grant", "secret", "snapshot"})
    assert all(term not in json.dumps(preview).lower() for term in ("path", "secret"))
    stored = tmp_path / "library" / str(asset["vault_ref"])
    assert stored.read_bytes() == content

    with TestClient(create_app(SimpleNamespace(root_dir=tmp_path))) as restarted:
        persisted = restarted.app.state.context_graph_runtime.snapshots.revision(
            "project-bridge", str(preview["graph_id"]), str(preview["graph_revision"]),
        )
    assert persisted is not None
    assert persisted.snapshot.to_dict() == record.snapshot.to_dict()


def test_markdown_uses_the_same_production_import_contract(tmp_path, monkeypatch) -> None:
    _configure_desktop(monkeypatch)
    content = b"# Root\nMaterial\n## Conclusion\nDecision\n"
    with TestClient(create_app(SimpleNamespace(root_dir=tmp_path))) as client:
        asset, preview = _import_via_production_bridge(
            client,
            content=content,
            name="bridge.md",
            media_type="text/markdown",
            source_type="markdown",
            command_suffix="markdown",
        )
        record = client.app.state.context_graph_runtime.snapshots.revision(
            "project-bridge", str(preview["graph_id"]), str(preview["graph_revision"]),
        )

    assert record is not None
    assert record.snapshot.source_type == "markdown"
    assert [node.node_id for node in record.snapshot.nodes] == ["md-1", "md-2"]
    assert record.snapshot.edges[0].source_node_id == "md-1"
    assert preview["importer_id"].endswith("MarkdownGraphImporter")
    assert (tmp_path / "library" / str(asset["vault_ref"])).read_bytes() == content


def test_invalid_json_fails_closed_without_snapshot_append(tmp_path, monkeypatch) -> None:
    _configure_desktop(monkeypatch)
    content = b'{"version": 1, "nodes": ['
    with TestClient(create_app(SimpleNamespace(root_dir=tmp_path))) as client:
        streamed = client.post(
            "/api/rebuild/workbench/original-asset-stream",
            headers=_file_headers(content, name="broken.thoughtdag.json", media_type="application/json"),
            content=content,
        )
        assert streamed.status_code == 201
        selected = client.post(
            "/api/rebuild/context-graph-import-selections",
            headers=_file_headers(content, name="broken.thoughtdag.json", media_type="application/json"),
            json={
                "command_id": "selection-command-broken", "project_id": "project-bridge",
                "source_type": "thoughtdag", "asset_id": streamed.json()["asset_id"],
            },
        )
        assert selected.status_code == 201
        rejected = client.post(
            "/api/rebuild/context-graphs/imports",
            headers={DESKTOP_SESSION_HEADER: _SECRET},
            json={
                "command_id": "import-command-broken", "project_id": "project-bridge",
                "source_type": "thoughtdag", "selection_id": selected.json()["selection_id"],
                "expected_predecessor": None, "confirm_read": True,
            },
        )
        revisions = SQLiteStructuredRecordStore(
            tmp_path / ".rebuild-data" / "context-graphs.sqlite3",
        ).list("context_graph_snapshot_revisions")

    assert rejected.status_code == 400
    assert rejected.json()["code"] == "importer_rejected"
    assert revisions == ()
