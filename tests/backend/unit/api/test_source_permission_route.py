from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.rebuild_storage_runtime import build_rebuild_object_store
from backend.api.routes.ai import router
from core.source_processing import SourceManifestArtifactRepository, SourceManifestCodec


def _app(tmp_path: Path) -> FastAPI:
    app = FastAPI()
    app.state.container = SimpleNamespace(root_dir=tmp_path)
    app.include_router(router)
    return app


def _manifest_ref(tmp_path: Path, *, project_id: str = "alpha") -> str:
    store, settings = build_rebuild_object_store(tmp_path)
    manifest = SourceManifestCodec.decode({
        "schema_version": "1.0.0",
        "source_id": "bili-BV1xx411c7mD-p1",
        "source_ref": "crp://default/sources/bili-BV1xx411c7mD-p1",
        "platform": "bilibili",
        "input_identity": "https://www.bilibili.com/video/BV1xx411c7mD/",
        "resolver_revision": "bilibili-view-api-v1",
        "normalizer_revision": "bilibili-manifest-v1",
        "content_kind": "video",
        "body": None,
        "metadata": {"title": "fixture"},
        "permission": {
            "decision": "unknown",
            "evidence_refs": [
                f"crp://{settings.namespace_id}/source-resolution-evidence/projects/{project_id}/bili-BV1xx411c7mD-p1--view-v1"
            ],
        },
        "provenance_refs": [
            f"crp://{settings.namespace_id}/source-resolution-evidence/projects/{project_id}/bili-BV1xx411c7mD-p1--view-v1"
        ],
        "assets": [{
            "asset_id": "video-BV1xx411c7mD-p1", "ordinal": 0, "kind": "video",
            "media_type": None, "role": "primary",
            "locator": "https://www.bilibili.com/video/BV1xx411c7mD/",
            "source_ref": "crp://default/sources/bili-BV1xx411c7mD-p1/assets/video-BV1xx411c7mD-p1",
            "relations": [], "evidence_refs": [
                f"crp://{settings.namespace_id}/source-resolution-evidence/projects/{project_id}/bili-BV1xx411c7mD-p1--view-v1"
            ],
        }],
    })
    artifact = SourceManifestArtifactRepository(store, namespace_id=settings.namespace_id).put(
        project_id=project_id,
        manifest_id="bili-BV1xx411c7mD-p1--initial",
        manifest=manifest,
    )
    return artifact.public_ref


def test_source_permission_grant_current_revoke_and_command_replay(tmp_path: Path) -> None:
    manifest_ref = _manifest_ref(tmp_path)
    with TestClient(_app(tmp_path)) as client:
        body = {
            "command_id": "source-grant-1", "manifest_ref": manifest_ref,
            "expected_permission_revision": 0, "confirm": True,
        }
        granted = client.post("/api/ai/projects/alpha/source-permissions/grant", json=body)
        replay = client.post("/api/ai/projects/alpha/source-permissions/grant", json=body)
        current = client.get(
            "/api/ai/projects/alpha/source-permissions/current", params={"manifest_ref": manifest_ref}
        )
        revoked = client.post(
            f"/api/ai/projects/alpha/source-permissions/{granted.json()['permission_id']}/revoke",
            json={"command_id": "source-revoke-1", "expected_permission_revision": 1, "confirm": True},
        )
    assert granted.status_code == replay.status_code == current.status_code == revoked.status_code == 200
    assert granted.json() == replay.json()
    assert granted.json()["state"] == "granted"
    assert current.json()["permission"]["permission_revision"] == 1
    assert revoked.json()["state"] == "revoked"
    assert revoked.json()["revocation_generation"] == 1
    assert all(response.headers["cache-control"] == "no-store" for response in (granted, replay, current, revoked))


def test_source_permission_rejects_cross_project_and_authority_fields(tmp_path: Path) -> None:
    manifest_ref = _manifest_ref(tmp_path, project_id="alpha")
    with TestClient(_app(tmp_path)) as client:
        cross = client.post(
            "/api/ai/projects/beta/source-permissions/grant",
            json={"command_id": "grant-cross", "manifest_ref": manifest_ref, "expected_permission_revision": 0, "confirm": True},
        )
        forged = client.post(
            "/api/ai/projects/alpha/source-permissions/grant",
            json={"command_id": "grant-forged", "manifest_ref": manifest_ref, "expected_permission_revision": 0, "confirm": True, "source_id": "forged"},
        )
    assert cross.status_code == 404
    assert forged.status_code == 400
