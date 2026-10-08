from __future__ import annotations

from pathlib import Path
import sqlite3
import sys
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.rebuild_storage_runtime import build_rebuild_object_store
from backend.memory_app.workspace import install_workspace_routes
from backend.api.job_execution_runtime import register_job_execution_handler
from backend.api.job_runtime import build_rebuild_job_repository
from backend.api.media_ingress_selection_authority import MediaIngressSelectionAuthority
from backend.api.routes.workbench_auto_intake import router
from backend.api.routes import workbench_auto_intake as auto_intake_routes
from backend.api.workbench_auto_intake_runtime import build_workbench_auto_intake_runtime
from backend.api.workbench_original_asset_runtime import build_original_asset_store
from backend.api.workbench_content_transform_runtime import (
    _workbench_video_audio_settings,
    readmit_workbench_content_transform,
)
from core.effect_log import EffectRecoveryCoordinator, EffectState, build_effect_runtime
from core.ingestion_core import ObjectStoreSourceRegistrar, SourceSubmission
from core.document_engine import SQLiteDocumentRepository
from core.job_runner import (
    RoutedJobRepository,
)
from core.product_core import ReadSourceTextContent
from core.product_core.daily_reminders import GenerateDailyReminders
from core.product_core.local_asr_provider_settings import SaveLocalAsrProviderSettings
from core.storage_provider import SQLiteStructuredRecordStore


class _Settings:
    def get_provider_settings(self):
        return SimpleNamespace(
            llm_provider="openai",
            openai_base_url="http://127.0.0.1:8317",
            openai_model="fallback-model",
        )


def _container(root_dir: Path):
    return SimpleNamespace(
        root_dir=root_dir,
        settings_service=_Settings(),
        secret_store=SimpleNamespace(get=lambda _name: ""),
    )


def test_runtime_composes_domain_service_with_recipe_trace_without_fastapi(tmp_path: Path) -> None:
    container = _container(tmp_path)
    store, settings = build_rebuild_object_store(tmp_path)
    application = SimpleNamespace(
        state=SimpleNamespace(
            effect_runtime=build_effect_runtime(
                tmp_path / ".rebuild-data" / "jobs.sqlite3",
                owner_id="auto-intake-test",
            )
        )
    )
    response = build_workbench_auto_intake_runtime(
        container,
        store,
        namespace_id=settings.namespace_id,
        application=application,
    ).execute(
        method="POST",
        path="/api/rebuild/workbench/auto-intake",
        body={"content": "下一步完成自动入库 runtime 拆分"},
    )

    assert response.status_code == 201
    assert response.body["status"] == "accepted"
    source_id = response.body["items"][0]["source_id"]
    assert response.body["review_item_id"] == f"review-{source_id}"
    assert response.body["items"][0]["review_item_id"] == f"review-{source_id}"
    records = SQLiteStructuredRecordStore(tmp_path / ".rebuild-data" / "structured-records.sqlite3")
    assert records.read("workspace_review_intents", f"review-{source_id}") is not None
    assert response.body["processing_recipe_trace"]["consumer"] == "workbench.auto-intake"
    assert response.headers["Cache-Control"] == "no-store"


def test_video_url_rejected_before_creating_an_unauthorized_local_video_source(tmp_path: Path) -> None:
    container = _container(tmp_path)
    store, settings = build_rebuild_object_store(tmp_path)
    application = SimpleNamespace(state=SimpleNamespace(effect_runtime=build_effect_runtime(
        tmp_path / ".rebuild-data" / "jobs.sqlite3", owner_id="video-url-preflight-test")))
    runtime = build_workbench_auto_intake_runtime(
        container, store, namespace_id=settings.namespace_id, application=application)

    response = runtime.execute(method="POST", path="/api/rebuild/workbench/auto-intake", body={
        "project_id": "video-url-qa",
        "content": "https://www.bilibili.com/video/BV1mE5564Eud/?spm_id_from=333.788.top_right_bar_window_custom_collection.content.click&vd_source=example",
    })

    assert response.status_code == 400
    assert response.body["reason"] == "video_link_requires_workspace_review"
    assert store.list("sources") == ()
    assert store.list("jobs") == ()


@pytest.mark.parametrize("project_id", ["default", "project-a"])
def test_audio_callbacks_keep_project_in_short_long_projection_and_reminder(
    tmp_path: Path, project_id: str,
) -> None:
    container = _container(tmp_path)
    store, settings = build_rebuild_object_store(tmp_path)
    application = SimpleNamespace(state=SimpleNamespace(effect_runtime=build_effect_runtime(
        tmp_path / ".rebuild-data" / "jobs.sqlite3", owner_id="audio-project-callback-test")))
    runtime = build_workbench_auto_intake_runtime(
        container, store, namespace_id=settings.namespace_id, application=application,
    )
    source_id = "source-audio-project-callback"
    store.write("sources", source_id, {
        "id": source_id, "source_type": "audio", "project_id": project_id, "metadata": {},
    }, expected_revision=None)
    orchestrator = runtime._orchestrator(None, project_id=project_id)

    short = orchestrator._run_audio_auto_workflow(source_id, None)
    assert short.project_id == project_id
    assert store.read("audio_auto_workflows", short.workflow_id)["project_id"] == project_id
    assert store.read("sources", source_id)["metadata"]["audio_auto_workflow"]["project_id"] == project_id

    long = orchestrator._run_long_audio_chunked_workflow(source_id, "missing-asset")
    assert long.project_id == project_id
    project_reminders = GenerateDailyReminders(store).execute(project_id=project_id).reminders
    other_reminders = GenerateDailyReminders(store).execute(
        project_id="default" if project_id != "default" else "project-a",
    ).reminders
    assert any(item.reminder_type == "audio_auto_workflow_blocked" for item in project_reminders)
    assert not any(item.reminder_type == "audio_auto_workflow_blocked" for item in other_reminders)


def test_auto_intake_keeps_identical_text_in_separate_projects(tmp_path: Path) -> None:
    container = _container(tmp_path)
    store, settings = build_rebuild_object_store(tmp_path)
    application = SimpleNamespace(state=SimpleNamespace(effect_runtime=build_effect_runtime(
        tmp_path / ".rebuild-data" / "jobs.sqlite3", owner_id="project-scope-test")))
    runtime = build_workbench_auto_intake_runtime(
        container, store, namespace_id=settings.namespace_id, application=application)
    responses = [runtime.execute(
        method="POST", path="/api/rebuild/workbench/auto-intake",
        body={"content": "相同材料需要分别归入项目", "project_id": project_id},
    ) for project_id in ("default", "project-a")]
    assert [response.status_code for response in responses] == [201, 201], responses[1].body.get("reason")
    first, second = (response.body["items"][0]["source_id"] for response in responses)
    assert first != second
    assert [store.read("sources", source_id)["project_id"] for source_id in (first, second)] == ["default", "project-a"]
    assert [response.body["project_id"] for response in responses] == ["default", "project-a"]
    records = SQLiteStructuredRecordStore(tmp_path / ".rebuild-data" / "structured-records.sqlite3")
    assert records.read("workspace_review_intents", f"review-{second}").payload["project_id"] == "project-a"
    invalid = runtime.execute(method="POST", path="/api/rebuild/workbench/auto-intake",
                              body={"content": "x", "project_id": "../other"})
    assert invalid.status_code == 400

    review_app = FastAPI()
    install_workspace_routes(review_app, runtime_root=tmp_path, records=records,
                             models=SimpleNamespace(), documents=SQLiteDocumentRepository(records),
                             service=SimpleNamespace())
    with TestClient(review_app) as client:
        review_list = client.get("/api/workspace/v1/legacy-reviews", params={"project_id": "project-a"})
        assert review_list.status_code == 200, review_list.text
        review = review_list.json()["items"][0]
        assert review["source_id"] == second
        assert review["status"] == "ready"
        assert client.put(f"/api/workspace/v1/legacy-reviews/{second}/draft", json={
            "project_id": "project-a", "markdown": "# 经人工检查的资料",
        }).status_code == 200
        confirmed = client.post(f"/api/workspace/v1/legacy-reviews/{second}/confirm", json={
            "project_id": "project-a",
        })
        assert confirmed.status_code == 200, confirmed.text
        assert confirmed.json()["status"] == "confirmed"
        assert client.get("/api/workspace/v1/legacy-reviews", params={"project_id": "default"}).json()["items"][0]["source_id"] == first


def test_document_intake_returns_202_after_effect_v2_admission_without_running_extractor(
    tmp_path: Path,
) -> None:
    container = _container(tmp_path)
    store, settings = build_rebuild_object_store(tmp_path)
    effect_runtime = build_effect_runtime(
        tmp_path / ".rebuild-data" / "jobs.sqlite3",
        owner_id="auto-intake-document-background-test",
    )
    application = SimpleNamespace(state=SimpleNamespace(effect_runtime=effect_runtime))
    register_job_execution_handler(application, tmp_path, effect_runtime)
    original = build_original_asset_store(
        tmp_path, store, namespace_id=settings.namespace_id,
    ).execute(
        display_name="queued.docx",
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        size_bytes=7,
        content_base64="Zml4dHVyZQ==",
        source_kind="file",
    )

    response = build_workbench_auto_intake_runtime(
        container,
        store,
        namespace_id=settings.namespace_id,
        application=application,
    ).execute(
        method="POST",
        path="/api/rebuild/workbench/auto-intake",
        body={
            "content": "",
            "media_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "file_name": "queued.docx",
            "title": "queued.docx",
            "original_asset_ref": original.asset_ref,
            "add_to_knowledge_base": True,
            "project_id": "project-a",
        },
    )

    assert response.status_code == 202
    assert response.body["status"] == "accepted"
    assert response.body["project_id"] == "project-a"
    assert store.read("sources", response.body["items"][0]["source_id"])["project_id"] == "project-a"
    assert response.body["items"][0]["status"] == "queued"
    assert response.body["items"][0]["next_step"] == "await_background_transform"
    assert response.body["items"][0]["workflow_steps"][1]["status"] == "running"
    job = build_rebuild_job_repository(tmp_path, store).get(str(response.body["job_id"]))
    assert job is not None
    assert job["job_type"] == "workbench_content_transform"
    assert job["execution_version"] == "effect-v2"
    assert job["status"] == "pending"
    assert job["project_id"] == "project-a"
    assert store.list("source_content_reads") == ()
    with sqlite3.connect(effect_runtime.log.database) as connection:
        effect = connection.execute(
            "SELECT state FROM effect WHERE root_id=? AND kind='workbench_content_transform'",
            (response.body["job_id"],),
        ).fetchone()
    assert effect == ("PLANNED",)
    assert "workbench_content_transform" in effect_runtime.handlers.kinds()

    first_pass_at = int(time.time()) + 1
    EffectRecoveryCoordinator(effect_runtime).recover_once(now=first_pass_at)
    assert build_rebuild_job_repository(tmp_path, store).get(str(response.body["job_id"]))["status"] == "running"
    EffectRecoveryCoordinator(effect_runtime).recover_once(now=first_pass_at + 60)
    failed = build_rebuild_job_repository(tmp_path, store).get(str(response.body["job_id"]))
    assert failed is not None
    assert failed["status"] == "failed"
    assert store.list("documents") == ()


@pytest.mark.parametrize(
    ("body", "expected_pipeline"),
    (
        (
            {"content": "https://example.test/a04-web", "urls": ["https://example.test/a04-web"]},
            "web_read",
        ),
        (
            {
                "content": "",
                "media_type": "image/png",
                "file_name": "a04-image.png",
                "title": "a04-image.png",
                "original_asset_ref": "__asset_ref__",
                "add_to_knowledge_base": True,
            },
            "image_ocr",
        ),
        (
            {
                "content": "",
                "media_type": "audio/mpeg",
                "file_name": "a04-audio.mp3",
                "title": "a04-audio.mp3",
                "original_asset_ref": "__asset_ref__",
                "add_to_knowledge_base": True,
            },
            "audio_transcript",
        ),
    ),
)
def test_a04_web_image_and_audio_are_admitted_before_any_slow_transform(
    tmp_path: Path, body: dict[str, object], expected_pipeline: str,
) -> None:
    container = _container(tmp_path)
    store, settings = build_rebuild_object_store(tmp_path)
    effect_runtime = build_effect_runtime(
        tmp_path / ".rebuild-data" / "jobs.sqlite3", owner_id="a04-durable-admission-test",
    )
    application = SimpleNamespace(state=SimpleNamespace(effect_runtime=effect_runtime))
    register_job_execution_handler(application, tmp_path, effect_runtime)
    if expected_pipeline == "audio_transcript":
        SaveLocalAsrProviderSettings(store).execute(
            enabled=True, confirm_enable=True, command=(sys.executable, "--version"),
        )
    if body.get("original_asset_ref") == "__asset_ref__":
        asset = build_original_asset_store(tmp_path, store, namespace_id=settings.namespace_id).execute(
            display_name=str(body["file_name"]), media_type=str(body["media_type"]),
            size_bytes=7, content_base64="Zml4dHVyZQ==",
            source_kind="image" if expected_pipeline == "image_ocr" else "audio",
        )
        body = {**body, "original_asset_ref": asset.asset_ref}

    response = build_workbench_auto_intake_runtime(
        container, store, namespace_id=settings.namespace_id, application=application,
    ).execute(method="POST", path="/api/rebuild/workbench/auto-intake", body=body)

    assert response.status_code == 202, response.body
    assert response.body["items"][0]["status"] == "queued"
    job = build_rebuild_job_repository(tmp_path, store).get(str(response.body["job_id"]))
    assert job is not None
    assert job["execution_version"] == "effect-v2"
    assert job["transform_items"][0]["pipeline"] == expected_pipeline
    assert store.list("documents") == ()


def test_audio_intake_without_asr_selection_rejects_job_and_keeps_original(tmp_path: Path) -> None:
    container = _container(tmp_path)
    store, settings = build_rebuild_object_store(tmp_path)
    effect_runtime = build_effect_runtime(
        tmp_path / ".rebuild-data" / "jobs.sqlite3", owner_id="asr-selection-test",
    )
    application = SimpleNamespace(state=SimpleNamespace(effect_runtime=effect_runtime))
    register_job_execution_handler(application, tmp_path, effect_runtime)
    original = build_original_asset_store(
        tmp_path, store, namespace_id=settings.namespace_id,
    ).execute(
        display_name="keep-audio.mp3", media_type="audio/mpeg", size_bytes=7,
        content_base64="Zml4dHVyZQ==", source_kind="audio",
    )

    response = build_workbench_auto_intake_runtime(
        container, store, namespace_id=settings.namespace_id, application=application,
    ).execute(method="POST", path="/api/rebuild/workbench/auto-intake", body={
        "content": "", "title": "keep-audio.mp3", "file_name": "keep-audio.mp3",
        "media_type": "audio/mpeg", "original_asset_ref": original.asset_ref,
        "add_to_knowledge_base": True,
    })

    assert response.status_code == 400
    assert "local ASR is disabled" in response.body["reason"]
    assert store.list("workbench_asr_bindings") == ()
    assert store.list("documents") == ()
    assert store.list("sources")
    assert store.read("workbench_original_assets", original.asset_id)["availability"] == "available"


def test_a04_unknown_transform_outcome_cannot_create_a_new_retry_identity(tmp_path: Path) -> None:
    store, _settings = build_rebuild_object_store(tmp_path)
    previous = {
        "id": "job-a04-unknown",
        "job_type": "workbench_content_transform",
        "execution_version": "effect-v2",
        "status": "unknown",
        "transform_items": [],
    }

    with pytest.raises(ValueError, match="not rebuildable"):
        readmit_workbench_content_transform(
            database_path=tmp_path / ".rebuild-data" / "jobs.sqlite3",
            runtime_root=tmp_path,
            object_store=store,
            previous_job=previous,
            command_id="retry-a04-unknown-command",
        )


def test_workbench_video_uses_scoped_extractor_without_enabling_global_settings(
    tmp_path: Path,
) -> None:
    store, _settings = build_rebuild_object_store(tmp_path)

    extractor = _workbench_video_audio_settings(tmp_path, store)

    assert extractor.enabled is True
    assert extractor.ffprobe_path == "builtin:pyav-probe"
    assert Path(extractor.ffmpeg_path).is_file()
    assert Path(extractor.output_root).is_relative_to(tmp_path.resolve(strict=False))
    assert store.read("video_audio_extractor_settings", "default") is None
    assert store.read("audio_asset_transcriber_settings", "default") is None


def _candidate_v2_production_parts(tmp_path: Path):
    container = _container(tmp_path)
    store, settings = build_rebuild_object_store(tmp_path)
    effect_runtime = build_effect_runtime(
        tmp_path / ".rebuild-data" / "jobs.sqlite3",
        owner_id="auto-intake-candidate-v2-test",
    )
    application = SimpleNamespace(state=SimpleNamespace(effect_runtime=effect_runtime))
    register_job_execution_handler(application, tmp_path, effect_runtime)
    runtime = build_workbench_auto_intake_runtime(
        container,
        store,
        namespace_id=settings.namespace_id,
        application=application,
    )
    repository = build_rebuild_job_repository(tmp_path, store)
    assert isinstance(repository, RoutedJobRepository)
    source = ObjectStoreSourceRegistrar(store).register(SourceSubmission(
        kind="text",
        title="Candidate v2 production composition",
        content="Create exactly one review proposal from this completed source read.",
    ))
    read = ReadSourceTextContent(store).execute(source_id=str(source["id"]))
    item = SimpleNamespace(
        source_id=str(source["id"]),
        content_read_status="completed",
        auto_organization={
            "content_read_id": str(read.read_ref).removesuffix(".json").rsplit("/", 1)[-1],
        },
    )
    return runtime, repository, store, application, effect_runtime, item


def _candidate_effect(effect_runtime, job_id: str):
    with sqlite3.connect(effect_runtime.log.database) as connection:
        row = connection.execute(
            "SELECT operation_id FROM effect WHERE root_id=? "
            "AND kind='memory_candidate_from_source_output'",
            (job_id,),
        ).fetchone()
    assert row is not None
    return effect_runtime.log.get(str(row[0]))


def test_candidate_v2_auto_intake_admission_is_planned_then_core_coordinator_settles_receipt(
    tmp_path: Path,
) -> None:
    runtime, repository, store, _application, effect_runtime, item = _candidate_v2_production_parts(tmp_path)

    produced = runtime._produce_candidate_jobs(
        repository, "parent-candidate-v2", (item,), "2026-08-30T00:00:00Z",
    )

    assert len(produced) == 1
    job_id = str(produced[0]["id"])
    planned = _candidate_effect(effect_runtime, job_id)
    assert planned.state is EffectState.PLANNED
    report = EffectRecoveryCoordinator(effect_runtime).recover_once(now=101)
    settled = effect_runtime.log.get(planned.operation_id)

    assert settled.state is EffectState.SETTLED_OK
    assert settled.result_ref == f"receipt:memory-candidate/{planned.operation_id}"
    assert any(item.operation_id == planned.operation_id for item in report.effect_outcomes) is False
    candidates = store.list("memory_candidates")
    assert len(candidates) == 1
    assert candidates[0]["status"] == "pending_review"
    with sqlite3.connect(effect_runtime.log.database) as connection:
        receipt = connection.execute(
            "SELECT receipt_ref,receipt_kind,receipt_schema_version "
            "FROM effect_receipt WHERE operation_id=?", (planned.operation_id,),
        ).fetchone()
        domain_receipts = connection.execute(
            "SELECT COUNT(*) FROM candidate_effect_domain_receipt WHERE operation_id=?",
            (planned.operation_id,),
        ).fetchone()[0]
    assert receipt == (
        settled.result_ref,
        "candidate-memory-job-execution.receipt",
        "candidate-memory-job-execution-receipt-v2",
    )
    assert planned.intent_schema_version == "candidate-memory-job-execution-v2"
    assert domain_receipts == 1


def test_candidate_v2_auto_intake_replay_after_marker_cas_keeps_one_job_effect_and_proposal(
    tmp_path: Path,
) -> None:
    runtime, repository, store, _application, effect_runtime, item = _candidate_v2_production_parts(tmp_path)
    first = runtime._produce_candidate_jobs(
        repository, "parent-candidate-v2", (item,), "2026-08-30T00:00:00Z",
    )
    assert len(first) == 1
    job_id = str(first[0]["id"])
    first_effect = _candidate_effect(effect_runtime, job_id)
    EffectRecoveryCoordinator(effect_runtime).recover_once(now=101)
    assert effect_runtime.log.get(first_effect.operation_id).state is EffectState.SETTLED_OK

    replayed = runtime._produce_candidate_jobs(
        repository, "parent-candidate-v2", (item,), "2026-08-30T00:01:00Z",
    )

    assert [job["id"] for job in replayed] == [job_id]
    replayed_effect = _candidate_effect(effect_runtime, job_id)
    assert replayed_effect.operation_id == first_effect.operation_id
    candidates = store.list("memory_candidates")
    assert len(candidates) == 1
    candidate_id = str(candidates[0]["id"])
    read_id = str(item.auto_organization["content_read_id"])
    source = store.read("sources", str(item.source_id))
    read = store.read("source_content_reads", read_id)
    assert read["memory_candidate_id"] == candidate_id
    assert read["memory_candidate_execution_ref"] == f"facts:effect/{first_effect.operation_id}"
    assert source["metadata"]["content_read"]["memory_candidate_id"] == candidate_id
    assert source["metadata"]["content_read"]["memory_candidate_execution_ref"] == (
        f"facts:effect/{first_effect.operation_id}"
    )
    candidate_events = [
        event for event in store.list("activity_events")
        if event.get("type") == "memory_candidate_created"
    ]
    assert len(candidate_events) == 1
    with sqlite3.connect(effect_runtime.log.database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM effect WHERE root_id=? "
            "AND kind='memory_candidate_from_source_output'", (job_id,),
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM candidate_effect_domain_receipt WHERE operation_id=?",
            (first_effect.operation_id,),
        ).fetchone()[0] == 1


def test_route_only_adapts_http_and_preserves_invalid_input_contract(tmp_path: Path) -> None:
    app = FastAPI()
    app.state.container = _container(tmp_path)
    app.state.effect_runtime = build_effect_runtime(
        tmp_path / ".rebuild-data" / "jobs.sqlite3",
        owner_id="auto-intake-route-test",
    )
    app.include_router(router)

    with TestClient(app) as client:
        response = client.post("/api/rebuild/workbench/auto-intake", json={"content": 7})

    assert response.status_code == 400
    assert response.json()["detail"] == "content, media_type and file_name must be strings"
    assert response.headers["cache-control"] == "no-store"


def test_runtime_and_router_are_independent_from_legacy_rebuild_route() -> None:
    root = Path(__file__).resolve().parents[2]
    runtime = (root / "src/backend/api/workbench_auto_intake_runtime.py").read_text(encoding="utf-8")
    route = (root / "src/backend/api/routes/workbench_auto_intake.py").read_text(encoding="utf-8")
    legacy_route = (root / "src/backend/api/routes/rebuild.py").read_text(encoding="utf-8")
    route_registry = (root / "src/backend/api/routes/__init__.py").read_text(encoding="utf-8")

    assert "backend.api.routes.rebuild" not in runtime
    assert "backend.api.routes.rebuild" not in route
    assert "core.ai_kernel" not in runtime
    assert "_legacy_workbench_auto_intake" not in legacy_route
    assert '@router.post("/api/rebuild/workbench/auto-intake")' not in legacy_route
    assert "app.include_router(workbench_auto_intake_router)" in route_registry


@pytest.mark.parametrize(
    "body",
    (
        {
            "content": "https://www.bilibili.com/video/BV1234567890",
            "urls": [],
            "add_to_knowledge_base": True,
        },
        {
            "content": "任意标题",
            "urls": ["https://www.bilibili.com/video/BV1234567890"],
            "add_to_knowledge_base": True,
        },
        {
            "content": "请分析 https://www.bilibili.com/video/BV1234567890 后续说明",
            "urls": [],
            "add_to_knowledge_base": True,
        },
    ),
)
def test_hands_selection_blocks_bilibili_target_before_legacy_effect(
    tmp_path: Path, monkeypatch, body: dict[str, object],
) -> None:
    MediaIngressSelectionAuthority(
        SQLiteStructuredRecordStore(tmp_path / ".rebuild-data" / "jobs.sqlite3")
    ).publish(
        "hands",
        expected_revision=0,
        command_id="select-hands-auto-intake-0001",
        actor="local-user",
        created_at="2026-08-26T00:00:00Z",
    )
    def fail_legacy_effect(*_args):
        raise AssertionError("legacy effect reached")

    monkeypatch.setattr(auto_intake_routes, "_execute_auto_intake", fail_legacy_effect)
    app = FastAPI()
    app.state.container = _container(tmp_path)
    app.include_router(router)

    with TestClient(app) as client:
        response = client.post(
            "/api/rebuild/workbench/auto-intake",
            json=body,
        )

    assert response.status_code == 409
    assert response.json()["status"] == "legacy_ingress_disabled"
    assert response.json()["next_step"] == "use_bilibili_media_ingress_resolve"
