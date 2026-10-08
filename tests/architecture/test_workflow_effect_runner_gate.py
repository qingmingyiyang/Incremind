from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_production_has_no_legacy_workflow_state_machine_or_retry_action() -> None:
    production = ROOT / "src"
    source = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in production.rglob("*.py")
    )

    assert "class RunAudioAutoWorkflow" not in source
    assert "class RunVideoAutoWorkflow" not in source
    assert "class RunLongAudioChunkedWorkflow" not in source
    assert "retry_failed_chunks" not in source


def test_workbench_workflows_compose_pure_deciders_and_effect_runtimes() -> None:
    auto_runtime = (
        ROOT / "src/backend/api/workbench_auto_intake_runtime.py"
    ).read_text(encoding="utf-8")
    audio_runtime = (
        ROOT / "src/backend/api/audio_auto_effect_runtime.py"
    ).read_text(encoding="utf-8")
    video_runtime = (
        ROOT / "src/backend/api/video_auto_effect_runtime.py"
    ).read_text(encoding="utf-8")
    video_intake_runtime = (
        ROOT / "src/backend/api/workbench_video_intake_runtime.py"
    ).read_text(encoding="utf-8")
    long_runtime = (
        ROOT / "src/backend/api/long_audio_effect_runtime.py"
    ).read_text(encoding="utf-8")

    assert "AudioAutoEffectRuntime" in auto_runtime
    assert "LongAudioEffectRuntime" in auto_runtime
    assert "run_video_auto_workflow=None" in auto_runtime
    assert "VideoAutoEffectRuntime" in video_intake_runtime
    assert "RunWorkbenchVideoIntakeFlow" in video_intake_runtime
    assert "decide_audio_auto_workflow" in audio_runtime
    assert "decide_video_auto_workflow" in video_runtime
    assert "decide_long_audio_result" in long_runtime
    assert "EffectWorkflowHandler" in audio_runtime
    assert "EffectWorkflowHandler" in video_runtime
    assert "EffectWorkflowHandler" in long_runtime


def test_workflow_projection_records_declare_effect_tree_source() -> None:
    paths = (
        ROOT / "src/backend/api/audio_auto_effect_runtime.py",
        ROOT / "src/backend/api/video_auto_effect_runtime.py",
        ROOT / "src/backend/api/long_audio_effect_runtime.py",
    )
    for path in paths:
        assert '"projection_source": "effect_tree"' in path.read_text(encoding="utf-8")


def test_media_job_records_are_declared_as_effect_owned_projections() -> None:
    paths = (
        ROOT / "src/core/product_core/audio_asset_transcriber.py",
        ROOT / "src/core/product_core/video_audio_extractor.py",
        ROOT / "src/core/product_core/transcript_summary_adapter.py",
        ROOT / "src/core/product_core/media_processing_queue.py",
    )
    for path in paths:
        source = path.read_text(encoding="utf-8")
        assert '"projection_source": "effect_tree"' in source
        assert '"execution_state_owner": "core_effect_log"' in source


def test_provider_model_discovery_is_effect_wrapped() -> None:
    source = (ROOT / "src/backend/api/routes/settings.py").read_text(encoding="utf-8")
    app_source = (ROOT / "src/backend/api/app.py").read_text(encoding="utf-8")

    assert 'kind="provider_model_discovery"' in source
    assert "EffectWorkflowHandler(" in source
    assert "EffectHandlerRegistration(" in source
    assert "effect_runtime.execute(intent" in source
    assert "register_provider_model_discovery_handler(" in app_source


def test_http_and_subprocess_routes_cannot_bypass_effect_handler() -> None:
    rebuild_routes = (ROOT / "src/backend/api/routes/product/source_content.py").read_text(
        encoding="utf-8"
    )
    settings_routes = (ROOT / "src/backend/api/routes/settings.py").read_text(
        encoding="utf-8"
    )

    assert "subprocess.run(" not in rebuild_routes
    assert rebuild_routes.count("fetch_url=_fetch_url_text") == 2
    assert 'operation_id=f"web-content:{source_id}:read"' in rebuild_routes
    assert 'operation_id=f"collection-web-content:{source_id}:read"' in rebuild_routes
    assert 'operation_id=f"document-text:{source_id}:extract"' in rebuild_routes
    assert settings_routes.count("httpx.get(") == 1
    assert 'kind="provider_model_discovery"' in settings_routes
