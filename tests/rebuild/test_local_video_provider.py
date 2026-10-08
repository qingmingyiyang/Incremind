from __future__ import annotations

import json
import sys
from pathlib import Path

from core.composition import build_local_command_video_adapter, build_source_video_authorization
from core.ingestion_core import ObjectStoreSourceRegistrar, SourceSubmission
from core.product_core import (
    AuthorizeLocalVideoFileForSource,
    CreateMediaProcessingQueueJob,
    GetLocalVideoProviderSettings,
    LocalCommandVideoFrameExtractionAdapter,
    LocalVideoProviderSettingsError,
    RunConfiguredLocalVideoProviderForSource,
    RunVideoFrameExtractionAdapterForMediaJob,
    SaveLocalVideoProviderSettings,
    ServeLocalVideoProviderRunEndpoint,
    ServeLocalVideoProviderSettingsEndpoint,
)
from core.storage_provider import JsonObjectStore


ROOT = Path(__file__).resolve().parents[2]


def _store(tmp_path: Path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def _video_source(object_store: JsonObjectStore) -> dict[str, object]:
    source = ObjectStoreSourceRegistrar(object_store).register(
        SourceSubmission(
            kind="video",
            title="Local video frames",
            display_name="screen.mp4",
            media_type="video/mp4",
            size_bytes=512,
            video_reference="platform-video-ref-local-video",
            duration_ms=180000,
            width_px=1920,
            height_px=1080,
        )
    )
    return dict(source)


def _video_file(tmp_path: Path) -> Path:
    video_path = tmp_path / "screen.mp4"
    video_path.write_bytes(b"not-a-real-video-but-user-authorized")
    return video_path


def _fake_video_script(
    tmp_path: Path,
    *,
    frame_refs: tuple[str, ...] = (
        "crp-ref://default/assets/platform-video-ref-local-video/frames/frame-0001.jpg",
        "crp-ref://default/assets/platform-video-ref-local-video/frames/frame-0002.jpg",
    ),
    preview: str = "本地视频 Provider 提取 2 个关键帧。",
) -> Path:
    script = tmp_path / "fake_video_provider.py"
    payload = {"frame_refs": list(frame_refs), "preview": preview}
    script.write_text(
        "\n".join(
            [
                "from pathlib import Path",
                "import json",
                "import sys",
                "video = Path(sys.argv[1])",
                "if not video.exists():",
                "    raise SystemExit(3)",
                f"print(json.dumps({payload!r}, ensure_ascii=False))",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return script


def test_authorize_local_video_reference_without_storing_path_in_source(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    source = _video_source(object_store)
    video_path = _video_file(tmp_path)

    result = AuthorizeLocalVideoFileForSource(object_store).execute(
        source_id=str(source["id"]),
        file_path=str(video_path),
    )
    updated_source = object_store.read("sources", str(source["id"]))
    authorization = object_store.read("authorized_file_refs", result.authorization_id)

    assert result.status == "authorized"
    assert result.file_reference == "platform-video-ref-local-video"
    assert authorization is not None
    assert authorization["path"] == str(video_path.resolve(strict=False))
    assert authorization["path_scope"] == "local_user_authorized_video"
    assert updated_source is not None
    assert updated_source["metadata"]["video_authorization"]["authorization_id"] == result.authorization_id
    assert updated_source["metadata"]["video_authorization"]["path_stored_in_source"] is False
    assert "path" not in updated_source["metadata"]["video_authorization"]
    assert not (tmp_path / "library").exists()


def test_local_video_provider_is_default_disabled_and_records_failed_job(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    source = _video_source(object_store)
    video_path = _video_file(tmp_path)
    AuthorizeLocalVideoFileForSource(object_store).execute(source_id=str(source["id"]), file_path=str(video_path))
    queued = CreateMediaProcessingQueueJob(object_store, enabled_capabilities=("video_frame_extraction",)).execute(
        source_id=str(source["id"])
    )
    script = _fake_video_script(tmp_path)
    adapter = LocalCommandVideoFrameExtractionAdapter(
        object_store=object_store,
        command=(sys.executable, str(script), "{video_path}"),
    )

    result = RunVideoFrameExtractionAdapterForMediaJob(object_store).execute(job_id=queued.job_id, adapter=adapter)
    output = object_store.read("media_processing_outputs", f"media-output-frame-index-{source['id']}")

    assert result.status == "failed"
    assert result.error == "local video provider is disabled"
    assert output is None
    assert not (tmp_path / "library").exists()


def test_local_video_provider_requires_authorized_video_reference(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    source = _video_source(object_store)
    queued = CreateMediaProcessingQueueJob(object_store, enabled_capabilities=("video_frame_extraction",)).execute(
        source_id=str(source["id"])
    )
    script = _fake_video_script(tmp_path)
    adapter = LocalCommandVideoFrameExtractionAdapter(
        object_store=object_store,
        command=(sys.executable, str(script), "{video_path}"),
        enabled=True,
    )

    result = RunVideoFrameExtractionAdapterForMediaJob(object_store).execute(job_id=queued.job_id, adapter=adapter)

    assert result.status == "failed"
    assert result.error == "authorized video reference not found"


def test_local_video_provider_writes_output_without_path_leak(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    source = _video_source(object_store)
    video_path = _video_file(tmp_path)
    authorization = AuthorizeLocalVideoFileForSource(object_store).execute(
        source_id=str(source["id"]),
        file_path=str(video_path),
    )
    queued = CreateMediaProcessingQueueJob(object_store, enabled_capabilities=("video_frame_extraction",)).execute(
        source_id=str(source["id"])
    )
    script = _fake_video_script(tmp_path)
    adapter = LocalCommandVideoFrameExtractionAdapter(
        object_store=object_store,
        command=(sys.executable, str(script), "{video_path}"),
        enabled=True,
        provider_name="local-ffmpeg-compatible-video",
    )

    result = RunVideoFrameExtractionAdapterForMediaJob(object_store).execute(job_id=queued.job_id, adapter=adapter)
    output = object_store.read("media_processing_outputs", f"media-output-frame-index-{source['id']}")

    assert result.status == "completed"
    assert result.output_preview == "本地视频 Provider 提取 2 个关键帧。"
    assert output is not None
    assert output["provider"] == "local-ffmpeg-compatible-video"
    assert output["frame_count"] == 2
    assert output["frame_refs"] == [
        "crp-ref://default/assets/platform-video-ref-local-video/frames/frame-0001.jpg",
        "crp-ref://default/assets/platform-video-ref-local-video/frames/frame-0002.jpg",
    ]
    assert output["metadata"]["local_processing"] is True
    assert output["metadata"]["remote_processing"] is False
    assert output["metadata"]["video_reference"] == "platform-video-ref-local-video"
    assert output["metadata"]["authorization_id"] == authorization.authorization_id
    assert output["metadata"]["path_stored_in_output"] is False
    assert "path" not in output["metadata"]
    assert not (tmp_path / "library").exists()


def test_local_video_provider_missing_executable_is_traceable_failure(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    source = _video_source(object_store)
    video_path = _video_file(tmp_path)
    AuthorizeLocalVideoFileForSource(object_store).execute(source_id=str(source["id"]), file_path=str(video_path))
    queued = CreateMediaProcessingQueueJob(object_store, enabled_capabilities=("video_frame_extraction",)).execute(
        source_id=str(source["id"])
    )
    adapter = LocalCommandVideoFrameExtractionAdapter(
        object_store=object_store,
        command=("missing-local-video-provider-executable", "{video_path}"),
        enabled=True,
    )

    result = RunVideoFrameExtractionAdapterForMediaJob(object_store).execute(job_id=queued.job_id, adapter=adapter)

    assert result.status == "failed"
    assert result.error == "local video provider executable not found"


def test_local_video_provider_composition_uses_temp_storage_only(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    source = _video_source(object_store)
    video_path = _video_file(tmp_path)
    build_source_video_authorization(ROOT, runtime_root=tmp_path).execute(
        source_id=str(source["id"]),
        file_path=str(video_path),
    )
    queued = CreateMediaProcessingQueueJob(object_store, enabled_capabilities=("video_frame_extraction",)).execute(
        source_id=str(source["id"])
    )
    script = _fake_video_script(tmp_path, preview="组合入口视频输出。")
    adapter = build_local_command_video_adapter(
        ROOT,
        runtime_root=tmp_path,
        command=(sys.executable, str(script), "{video_path}"),
        enabled=True,
        provider_name="local-composed-video",
    )

    result = RunVideoFrameExtractionAdapterForMediaJob(object_store).execute(job_id=queued.job_id, adapter=adapter)
    output = object_store.read("media_processing_outputs", f"media-output-frame-index-{source['id']}")

    assert result.status == "completed"
    assert output is not None
    assert output["provider"] == "local-composed-video"
    assert output["preview"] == "组合入口视频输出。"
    assert not (tmp_path / "library").exists()


def test_local_video_provider_settings_are_default_off(tmp_path: Path) -> None:
    object_store = _store(tmp_path)

    settings = GetLocalVideoProviderSettings(object_store).execute()

    assert settings.enabled is False
    assert settings.status == "disabled"
    assert settings.diagnostic == "disabled_until_explicit_enable"
    assert settings.remote_processing is False
    assert settings.memory_publication == "not_started"


def test_enabling_local_video_provider_requires_confirmation_and_command(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    writer = SaveLocalVideoProviderSettings(object_store)

    try:
        writer.execute(enabled=True, command=(sys.executable,), confirm_enable=False)
    except LocalVideoProviderSettingsError as error:
        assert str(error) == "enabling local video provider requires confirm_enable=true"
    else:
        raise AssertionError("expected explicit enable guard")

    try:
        writer.execute(enabled=True, command=(), confirm_enable=True)
    except LocalVideoProviderSettingsError as error:
        assert str(error) == "enabled local video provider requires command"
    else:
        raise AssertionError("expected command guard")

    settings = writer.execute(
        enabled=True,
        command=(sys.executable, "--version"),
        provider_name="local-python-video-smoke",
        confirm_enable=True,
    )

    assert settings.enabled is True
    assert settings.status == "ready"
    assert settings.provider_name == "local-python-video-smoke"
    assert settings.command == (sys.executable, "--version")


def test_local_video_provider_settings_endpoint_reports_missing_executable(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    endpoint = ServeLocalVideoProviderSettingsEndpoint()
    writer = SaveLocalVideoProviderSettings(object_store)

    response = endpoint.execute(
        method="PUT",
        path="/api/rebuild/settings/local-video-provider",
        body={
            "enabled": True,
            "command": ["missing-local-video-provider-executable", "{video_path}"],
            "confirm_enable": True,
        },
        get_settings=lambda: GetLocalVideoProviderSettings(object_store).execute(),
        save_settings=writer.execute,
    )

    assert response.status_code == 200
    assert response.body["status"] == "degraded"
    assert response.body["diagnostic"] == "executable_not_found"
    assert response.body["enabled"] is True


def test_local_video_run_endpoint_rejects_ui_supplied_command(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    source = _video_source(object_store)
    endpoint = ServeLocalVideoProviderRunEndpoint()

    response = endpoint.execute(
        method="POST",
        path=f"/api/rebuild/sources/{source['id']}/frame-extraction",
        body={"command": ["should-not-be-accepted", "{video_path}"]},
        run_video=RunConfiguredLocalVideoProviderForSource(object_store).execute,
    )

    assert response.status_code == 400
    assert response.body["reason"] == "local video run endpoint does not accept provider command"


def test_configured_local_video_run_endpoint_uses_saved_provider_settings(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    source = _video_source(object_store)
    video_path = _video_file(tmp_path)
    AuthorizeLocalVideoFileForSource(object_store).execute(source_id=str(source["id"]), file_path=str(video_path))
    CreateMediaProcessingQueueJob(object_store, enabled_capabilities=("video_frame_extraction",)).execute(
        source_id=str(source["id"])
    )
    script = _fake_video_script(tmp_path, preview="从设置读取命令的视频输出。")
    SaveLocalVideoProviderSettings(object_store).execute(
        enabled=True,
        command=(sys.executable, str(script), "{video_path}"),
        provider_name="local-settings-video",
        confirm_enable=True,
    )
    endpoint = ServeLocalVideoProviderRunEndpoint()

    response = endpoint.execute(
        method="POST",
        path=f"/api/rebuild/sources/{source['id']}/frame-extraction",
        body={},
        run_video=RunConfiguredLocalVideoProviderForSource(object_store).execute,
    )
    output = object_store.read("media_processing_outputs", f"media-output-frame-index-{source['id']}")

    assert response.status_code == 200
    assert response.body["status"] == "completed"
    assert response.body["output_preview"] == "从设置读取命令的视频输出。"
    assert output is not None
    assert output["provider"] == "local-settings-video"
    assert output["preview"] == "从设置读取命令的视频输出。"
    assert "path" not in output["metadata"]


def test_configured_local_video_run_records_missing_executable_failure(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    source = _video_source(object_store)
    video_path = _video_file(tmp_path)
    AuthorizeLocalVideoFileForSource(object_store).execute(source_id=str(source["id"]), file_path=str(video_path))
    CreateMediaProcessingQueueJob(object_store, enabled_capabilities=("video_frame_extraction",)).execute(
        source_id=str(source["id"])
    )
    SaveLocalVideoProviderSettings(object_store).execute(
        enabled=True,
        command=("missing-local-video-provider-executable", "{video_path}"),
        confirm_enable=True,
    )

    result = RunConfiguredLocalVideoProviderForSource(object_store).execute(source_id=str(source["id"]))

    assert result.status == "failed"
    assert result.error == "local video provider executable not found"
