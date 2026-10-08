from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.routes.ai import router as ai_router
from backend.api.routes.bilibili_media_ingress import router as ingress_router
from backend.api.job_execution_runtime import register_job_execution_handler
from backend.api.media_hands_composition import compose_application_media_hands
from backend.api.rebuild_storage_runtime import build_rebuild_object_store
from core.effect_log import build_effect_runtime
from core.job_runner import SQLiteJobStore
from core.job_runner.media_execution_receipt import media_job_uri_segment
from core.media_hands import (
    MediaHandsPolicyAuthority,
    MediaOperationReceipt,
    default_personal_workbench_policy_snapshot,
)
from core.source_processing import SourceManifestCodec
from core.storage_provider import SQLiteStructuredRecordStore


ROOT = Path(__file__).resolve().parents[4]


class _PlatformProvider:
    def provide(self, text: str, *, project_id: str):
        assert text == "https://www.bilibili.com/video/BV1xx411c7mD/"
        assert project_id == "project-1"
        value = json.loads(
            (
                ROOT
                / "core-contracts/rebuild/source-processing/fixtures/bilibili-video.json"
            ).read_text(encoding="utf-8")
        )
        value["platform"] = "bilibili"
        value["permission"] = {
            "decision": "unknown",
            "evidence_refs": [
                "crp://default/source-resolution-evidence/projects/project-1/bili-fixture-r1"
            ],
        }
        return SourceManifestCodec.decode(value)


class _MediaProvider:
    provider_id = "fixture-media-provider"
    provider_revision = "fixture-r1"
    supported_platforms = frozenset({"bilibili"})

    def execute(self, request):
        return MediaOperationReceipt(
            output={
                "kind": "document",
                "uri": f"crp://default/jobs/{request.job_id}/outputs/result",
                "object_id": "media-result-1",
                "published": True,
            },
            checkpoint={
                "resume_step": "execute_operation",
                "checkpoint_uri": (
                    f"crp://default/jobs/{request.job_id}/checkpoints/final"
                ),
                "state_hash": "sha256:" + "a" * 64,
                "updated_at": "2026-08-26T00:00:01Z",
            },
            consumed={key: 0 for key in request.budget},
            execution_receipt_ref=(
                f"crp://default/jobs/{media_job_uri_segment(request.job_id)}/receipts/execution"
            ),
        )


class _OutputVerifier:
    def assert_output_committed(self, *, output, request) -> None:
        _ = output, request


def _publish_enabled_policy(tmp_path: Path) -> None:
    snapshot = default_personal_workbench_policy_snapshot()
    snapshot["enabled"] = True
    snapshot["revision"] = "personal-workbench-r1"
    MediaHandsPolicyAuthority(
        SQLiteStructuredRecordStore(tmp_path / ".rebuild-data" / "jobs.sqlite3")
    ).publish(
        snapshot,
        expected_revision=0,
        command_id="media-policy-production-vertical-0001",
        actor="local-user",
        created_at="2026-08-26T00:00:00Z",
    )


def test_production_composition_resolves_grants_and_admits_one_media_job(tmp_path) -> None:
    _publish_enabled_policy(tmp_path)
    container = SimpleNamespace(
        root_dir=tmp_path,
        media_operation_provider=_MediaProvider(),
        platform_manifest_providers={"bilibili": _PlatformProvider()},
        media_output_verifier=_OutputVerifier(),
    )
    application = FastAPI()
    application.state.container = container
    application.state.effect_runtime = build_effect_runtime(
        tmp_path / ".rebuild-data" / "jobs.sqlite3",
        owner_id="bilibili-production-vertical",
    )
    register_job_execution_handler(
        application, tmp_path, application.state.effect_runtime,
    )
    compose_application_media_hands(application, container)
    application.include_router(ai_router)
    application.include_router(ingress_router)

    with TestClient(application) as client:
        selected = client.post(
            "/api/ai/media-ingress-selection/revisions",
            json={
                "command_id": "select-hands-production-vertical-0001",
                "expected_revision": 0,
                "confirm": True,
                "mode": "hands",
            },
        )
        assert selected.status_code == 200

        resolved = client.post(
            "/api/rebuild/media-ingress/bilibili/resolve",
            json={
                "ingress_request_id": "production-resolve-0001",
                "project_id": "project-1",
                "url": "https://www.bilibili.com/video/BV1xx411c7mD/",
            },
        )
        assert resolved.status_code == 200
        resolved_body = resolved.json()
        assert resolved_body["status"] == "permission_required"

        granted = client.post(
            "/api/ai/projects/project-1/source-permissions/grant",
            json={
                "command_id": "grant-production-vertical-0001",
                "manifest_ref": resolved_body["manifest_ref"],
                "expected_permission_revision": 0,
                "confirm": True,
            },
        )
        assert granted.status_code == 200
        assert granted.json()["state"] == "granted"

        admit_body = {
            "ingress_request_id": "production-resolve-0001",
            "project_id": "project-1",
            "manifest_ref": resolved_body["manifest_ref"],
        }
        admitted = client.post(
            "/api/rebuild/media-ingress/bilibili/admit", json=admit_body
        )
        replay = client.post(
            "/api/rebuild/media-ingress/bilibili/admit", json=admit_body
        )

    assert admitted.status_code == 202
    assert replay.status_code == 200
    assert admitted.json()["job_id"] == replay.json()["job_id"]
    jobs = SQLiteJobStore(tmp_path / ".rebuild-data" / "jobs.sqlite3").all()
    assert len(jobs) == 1
    assert jobs[0].payload["job_type"] == "media_hands"
    media_hands = jobs[0].payload["media_hands"]
    assert media_hands["manifest"]["ref"] == admitted.json()["manifest_ref"]
    assert media_hands["permission_snapshot"]["grant_ref"] == granted.json()["permission_ref"]
    store, _settings = build_rebuild_object_store(tmp_path)
    assert store.list("sources") == ()
