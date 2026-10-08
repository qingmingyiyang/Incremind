from __future__ import annotations

import sys
import time
from pathlib import Path

import psutil
import pytest

from core.composition import build_local_command_asr_adapter, build_source_audio_authorization
from core.ingestion_core import ObjectStoreSourceRegistrar, SourceSubmission
from core.product_core import (
    AuthorizeLocalAudioFileForSource,
    CreateMediaProcessingQueueJob,
    GetLocalAsrProviderSettings,
    LocalAsrProviderSettingsError,
    LocalCommandAudioTranscriptionAdapter,
    RunAudioTranscriptionAdapterForMediaJob,
    RunConfiguredLocalAsrProviderForSource,
    SaveLocalAsrProviderSettings,
    ServeLocalAsrProviderRunEndpoint,
    ServeLocalAsrProviderSettingsEndpoint,
)
from core.storage_provider import JsonObjectStore
from core.product_core.local_asr_provider import LocalAsrProviderError, transcribe_ephemeral_local_audio


ROOT = Path(__file__).resolve().parents[2]


def test_ephemeral_asr_cancellation_terminates_child_process_tree(tmp_path: Path) -> None:
    audio = tmp_path / "private.webm"; audio.write_bytes(b"private-audio")
    child_pid = tmp_path / "child.pid"
    script = tmp_path / "tree_asr.py"
    child_code = "import os,sys,time; handle=open(sys.argv[1],'rb'); open(sys.argv[2],'w').write(str(os.getpid())); time.sleep(60)"
    script.write_text(
        "import subprocess,sys,time\n"
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}, sys.argv[2], sys.argv[1]])\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    started = time.monotonic()
    with pytest.raises(LocalAsrProviderError, match="cancelled"):
        transcribe_ephemeral_local_audio(
            audio,
            command=(sys.executable, str(script), str(child_pid), "{audio_path}"),
            provider_name="tree-test", model_profile="small", model_name="small", timeout_seconds=10,
            cancelled=lambda: child_pid.exists() and time.monotonic() - started > 0.1,
        )
    pid = int(child_pid.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 3
    while psutil.pid_exists(pid) and time.monotonic() < deadline: time.sleep(0.05)
    assert not psutil.pid_exists(pid)
    audio.unlink()


def test_ephemeral_asr_rejects_non_utf8_provider_output(tmp_path: Path) -> None:
    audio = tmp_path / "private.webm"
    audio.write_bytes(b"private-audio")
    script = tmp_path / "invalid_utf8_asr.py"
    script.write_text("import sys\nsys.stdout.buffer.write(b'\\xff')\n", encoding="utf-8")

    with pytest.raises(LocalAsrProviderError, match="output is not UTF-8"):
        transcribe_ephemeral_local_audio(
            audio,
            command=(sys.executable, str(script), "{audio_path}"),
            provider_name="invalid-encoding-test",
            model_profile="small",
            model_name="small",
            timeout_seconds=10,
        )


def _store(tmp_path: Path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def _audio_source(object_store: JsonObjectStore) -> dict[str, object]:
    source = ObjectStoreSourceRegistrar(object_store).register(
        SourceSubmission(
            kind="audio",
            title="Local ASR audio",
            display_name="meeting.m4a",
            media_type="audio/mp4",
            size_bytes=256,
            audio_reference="platform-audio-ref-local-asr",
            duration_ms=120000,
        )
    )
    return dict(source)


def _audio_file(tmp_path: Path) -> Path:
    audio_path = tmp_path / "meeting.m4a"
    audio_path.write_bytes(b"not-a-real-audio-but-user-authorized")
    return audio_path


def _fake_asr_script(tmp_path: Path, *, text: str = "真实本地 ASR Provider 输出转写。") -> Path:
    script = tmp_path / "fake_asr_provider.py"
    script.write_text(
        "\n".join(
            [
                "from pathlib import Path",
                "import sys",
                "audio = Path(sys.argv[1])",
                "if not audio.exists():",
                "    raise SystemExit(3)",
                f"print({text!r})",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return script


def _fake_asr_model_script(tmp_path: Path) -> Path:
    script = tmp_path / "fake_asr_model_provider.py"
    script.write_text(
        "\n".join(
            [
                "from pathlib import Path",
                "import sys",
                "model = sys.argv[1]",
                "audio = Path(sys.argv[2])",
                "if model != 'large-v3-turbo':",
                "    raise SystemExit(4)",
                "if not audio.exists():",
                "    raise SystemExit(3)",
                "print('模型 profile 驱动的本地 ASR 输出。')",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return script


def test_authorize_local_audio_reference_without_storing_path_in_source(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    source = _audio_source(object_store)
    audio_path = _audio_file(tmp_path)

    result = AuthorizeLocalAudioFileForSource(object_store).execute(
        source_id=str(source["id"]),
        file_path=str(audio_path),
    )
    updated_source = object_store.read("sources", str(source["id"]))
    authorization = object_store.read("authorized_file_refs", result.authorization_id)

    assert result.status == "authorized"
    assert result.file_reference == "platform-audio-ref-local-asr"
    assert authorization is not None
    assert authorization["path"] == str(audio_path.resolve(strict=False))
    assert authorization["path_scope"] == "local_user_authorized_audio"
    assert updated_source is not None
    assert updated_source["metadata"]["audio_authorization"]["authorization_id"] == result.authorization_id
    assert updated_source["metadata"]["audio_authorization"]["path_stored_in_source"] is False
    assert "path" not in updated_source["metadata"]["audio_authorization"]
    assert not (tmp_path / "library").exists()


def test_local_asr_provider_is_default_disabled_and_records_failed_job(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    source = _audio_source(object_store)
    audio_path = _audio_file(tmp_path)
    AuthorizeLocalAudioFileForSource(object_store).execute(source_id=str(source["id"]), file_path=str(audio_path))
    queued = CreateMediaProcessingQueueJob(object_store, enabled_capabilities=("audio_transcription",)).execute(
        source_id=str(source["id"])
    )
    script = _fake_asr_script(tmp_path)
    adapter = LocalCommandAudioTranscriptionAdapter(
        object_store=object_store,
        command=(sys.executable, str(script), "{audio_path}"),
    )

    result = RunAudioTranscriptionAdapterForMediaJob(object_store).execute(job_id=queued.job_id, adapter=adapter)
    output = object_store.read("media_processing_outputs", f"media-output-transcript-{source['id']}")

    assert result.status == "failed"
    assert result.error == "local ASR provider is disabled"
    assert output is None
    assert not (tmp_path / "library").exists()


def test_local_asr_provider_requires_authorized_audio_reference(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    source = _audio_source(object_store)
    queued = CreateMediaProcessingQueueJob(object_store, enabled_capabilities=("audio_transcription",)).execute(
        source_id=str(source["id"])
    )
    script = _fake_asr_script(tmp_path)
    adapter = LocalCommandAudioTranscriptionAdapter(
        object_store=object_store,
        command=(sys.executable, str(script), "{audio_path}"),
        enabled=True,
    )

    result = RunAudioTranscriptionAdapterForMediaJob(object_store).execute(job_id=queued.job_id, adapter=adapter)

    assert result.status == "failed"
    assert result.error == "authorized audio reference not found"


def test_local_asr_provider_writes_output_without_path_leak(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    source = _audio_source(object_store)
    audio_path = _audio_file(tmp_path)
    authorization = AuthorizeLocalAudioFileForSource(object_store).execute(
        source_id=str(source["id"]),
        file_path=str(audio_path),
    )
    queued = CreateMediaProcessingQueueJob(object_store, enabled_capabilities=("audio_transcription",)).execute(
        source_id=str(source["id"])
    )
    script = _fake_asr_script(tmp_path)
    adapter = LocalCommandAudioTranscriptionAdapter(
        object_store=object_store,
        command=(sys.executable, str(script), "{audio_path}"),
        enabled=True,
        provider_name="local-faster-whisper-compatible-asr",
    )

    result = RunAudioTranscriptionAdapterForMediaJob(object_store).execute(job_id=queued.job_id, adapter=adapter)
    output = object_store.read("media_processing_outputs", f"media-output-transcript-{source['id']}")

    assert result.status == "completed"
    assert result.output_preview == "真实本地 ASR Provider 输出转写。"
    assert output is not None
    assert output["provider"] == "local-faster-whisper-compatible-asr"
    assert output["text"] == "真实本地 ASR Provider 输出转写。"
    assert output["metadata"]["local_processing"] is True
    assert output["metadata"]["remote_processing"] is False
    assert output["metadata"]["audio_reference"] == "platform-audio-ref-local-asr"
    assert output["metadata"]["authorization_id"] == authorization.authorization_id
    assert output["metadata"]["path_stored_in_output"] is False
    assert "path" not in output["metadata"]
    assert not (tmp_path / "library").exists()


def test_local_asr_provider_missing_executable_is_traceable_failure(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    source = _audio_source(object_store)
    audio_path = _audio_file(tmp_path)
    AuthorizeLocalAudioFileForSource(object_store).execute(source_id=str(source["id"]), file_path=str(audio_path))
    queued = CreateMediaProcessingQueueJob(object_store, enabled_capabilities=("audio_transcription",)).execute(
        source_id=str(source["id"])
    )
    adapter = LocalCommandAudioTranscriptionAdapter(
        object_store=object_store,
        command=("missing-local-asr-provider-executable", "{audio_path}"),
        enabled=True,
    )

    result = RunAudioTranscriptionAdapterForMediaJob(object_store).execute(job_id=queued.job_id, adapter=adapter)

    assert result.status == "failed"
    assert result.error == "local ASR provider executable not found"


def test_local_asr_provider_composition_uses_temp_storage_only(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    source = _audio_source(object_store)
    audio_path = _audio_file(tmp_path)
    build_source_audio_authorization(ROOT, runtime_root=tmp_path).execute(
        source_id=str(source["id"]),
        file_path=str(audio_path),
    )
    queued = CreateMediaProcessingQueueJob(object_store, enabled_capabilities=("audio_transcription",)).execute(
        source_id=str(source["id"])
    )
    script = _fake_asr_script(tmp_path, text="组合入口 ASR 输出。")
    adapter = build_local_command_asr_adapter(
        ROOT,
        runtime_root=tmp_path,
        command=(sys.executable, str(script), "{audio_path}"),
        enabled=True,
        provider_name="local-composed-asr",
    )

    result = RunAudioTranscriptionAdapterForMediaJob(object_store).execute(job_id=queued.job_id, adapter=adapter)
    output = object_store.read("media_processing_outputs", f"media-output-transcript-{source['id']}")

    assert result.status == "completed"
    assert output is not None
    assert output["provider"] == "local-composed-asr"
    assert output["text"] == "组合入口 ASR 输出。"
    assert not (tmp_path / "library").exists()


def test_local_asr_provider_settings_are_default_off(tmp_path: Path) -> None:
    object_store = _store(tmp_path)

    settings = GetLocalAsrProviderSettings(object_store).execute()

    assert settings.enabled is False
    assert settings.status == "disabled"
    assert settings.model_profile == "large-v3-turbo"
    assert settings.model_name == "large-v3-turbo"
    assert any(option["recommended"] is True for option in settings.model_options)
    assert settings.diagnostic == "disabled_until_explicit_enable"
    assert settings.remote_processing is False
    assert settings.memory_publication == "not_started"


def test_enabling_local_asr_provider_requires_confirmation_and_command(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    writer = SaveLocalAsrProviderSettings(object_store)

    try:
        writer.execute(enabled=True, command=(sys.executable,), confirm_enable=False)
    except LocalAsrProviderSettingsError as error:
        assert str(error) == "enabling local ASR provider requires confirm_enable=true"
    else:
        raise AssertionError("expected explicit enable guard")

    try:
        writer.execute(enabled=True, command=(), confirm_enable=True)
    except LocalAsrProviderSettingsError as error:
        assert str(error) == "enabled local ASR provider requires command"
    else:
        raise AssertionError("expected command guard")

    settings = writer.execute(
        enabled=True,
        command=(sys.executable, "--version"),
        provider_name="local-python-asr-smoke",
        confirm_enable=True,
    )

    assert settings.enabled is True
    assert settings.status == "ready"
    assert settings.provider_name == "local-python-asr-smoke"
    assert settings.command == (sys.executable, "--version")
    assert settings.model_profile == "large-v3-turbo"


def test_configured_local_asr_provider_injects_saved_model_profile(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    source = _audio_source(object_store)
    audio_path = _audio_file(tmp_path)
    AuthorizeLocalAudioFileForSource(object_store).execute(source_id=str(source["id"]), file_path=str(audio_path))
    CreateMediaProcessingQueueJob(object_store, enabled_capabilities=("audio_transcription",)).execute(
        source_id=str(source["id"])
    )
    script = _fake_asr_model_script(tmp_path)
    SaveLocalAsrProviderSettings(object_store).execute(
        enabled=True,
        command=(sys.executable, str(script), "{model_name}", "{audio_path}"),
        provider_name="local-settings-asr",
        model_profile="large-v3-turbo",
        model_name="large-v3-turbo",
        confirm_enable=True,
    )

    result = RunConfiguredLocalAsrProviderForSource(object_store).execute(source_id=str(source["id"]))
    output = object_store.read("media_processing_outputs", f"media-output-transcript-{source['id']}")

    assert result.status == "completed"
    assert result.output_preview == "模型 profile 驱动的本地 ASR 输出。"
    assert output is not None
    assert output["metadata"]["model_profile"] == "large-v3-turbo"
    assert output["metadata"]["model_name"] == "large-v3-turbo"


def test_local_asr_provider_settings_endpoint_reports_missing_executable(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    endpoint = ServeLocalAsrProviderSettingsEndpoint()
    writer = SaveLocalAsrProviderSettings(object_store)

    response = endpoint.execute(
        method="PUT",
        path="/api/rebuild/settings/local-asr-provider",
        body={
            "enabled": True,
            "command": ["missing-local-asr-provider-executable", "{audio_path}"],
            "model_profile": "large-v3-turbo",
            "confirm_enable": True,
        },
        get_settings=lambda: GetLocalAsrProviderSettings(object_store).execute(),
        save_settings=writer.execute,
    )

    assert response.status_code == 200
    assert response.body["status"] == "degraded"
    assert response.body["diagnostic"] == "executable_not_found"
    assert response.body["enabled"] is True
    assert response.body["model_profile"] == "large-v3-turbo"


def test_builtin_local_asr_reports_missing_and_ready_model(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CHRIPTMAS_APP_ROOT", str(tmp_path))
    object_store = _store(tmp_path)
    endpoint = ServeLocalAsrProviderSettingsEndpoint()
    writer = SaveLocalAsrProviderSettings(object_store)
    missing = endpoint.execute(
        method="PUT",
        path="/api/rebuild/settings/local-asr-provider",
        body={"enabled": True, "confirm_enable": True},
        get_settings=lambda: GetLocalAsrProviderSettings(object_store).execute(),
        save_settings=writer.execute,
    )
    assert missing.body["status"] == "degraded"
    assert missing.body["diagnostic"] == "model_missing"
    assert missing.body["model_status"] == "missing"
    assert missing.body["remote_processing"] is False

    model_dir = tmp_path / "data" / "models" / "faster-whisper" / "large-v3-turbo"
    model_dir.mkdir(parents=True)
    (model_dir / "model.bin").write_bytes(b"model")
    (model_dir / "config.json").write_text("{}", encoding="utf-8")
    from backend.video_summary.infrastructure.faster_whisper_models import FasterWhisperModelManager
    from backend.video_summary.infrastructure.huggingface_model_downloader import write_downloaded_model_manifest
    manager = FasterWhisperModelManager(model_dir.parent)
    write_downloaded_model_manifest(model_dir, manager.download_spec("large-v3-turbo"))
    ready = endpoint.execute(
        method="GET",
        path="/api/rebuild/settings/local-asr-provider",
        body=None,
        get_settings=lambda: GetLocalAsrProviderSettings(object_store).execute(),
        save_settings=writer.execute,
    )
    assert ready.body["status"] == "ready"
    assert ready.body["diagnostic"] == "ready"
    assert ready.body["model_status"] == "ready"


def test_local_asr_run_endpoint_rejects_ui_supplied_command(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    source = _audio_source(object_store)
    endpoint = ServeLocalAsrProviderRunEndpoint()

    response = endpoint.execute(
        method="POST",
        path=f"/api/rebuild/sources/{source['id']}/transcription",
        body={"command": ["should-not-be-accepted", "{audio_path}"]},
        run_asr=RunConfiguredLocalAsrProviderForSource(object_store).execute,
    )

    assert response.status_code == 400
    assert response.body["reason"] == "local ASR run endpoint does not accept provider command"


def test_configured_local_asr_run_endpoint_uses_saved_provider_settings(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    source = _audio_source(object_store)
    audio_path = _audio_file(tmp_path)
    AuthorizeLocalAudioFileForSource(object_store).execute(source_id=str(source["id"]), file_path=str(audio_path))
    CreateMediaProcessingQueueJob(object_store, enabled_capabilities=("audio_transcription",)).execute(
        source_id=str(source["id"])
    )
    script = _fake_asr_script(tmp_path, text="从设置读取命令的 ASR 输出。")
    SaveLocalAsrProviderSettings(object_store).execute(
        enabled=True,
        command=(sys.executable, str(script), "{audio_path}"),
        provider_name="local-settings-asr",
        confirm_enable=True,
    )
    endpoint = ServeLocalAsrProviderRunEndpoint()

    response = endpoint.execute(
        method="POST",
        path=f"/api/rebuild/sources/{source['id']}/transcription",
        body={},
        run_asr=RunConfiguredLocalAsrProviderForSource(object_store).execute,
    )
    output = object_store.read("media_processing_outputs", f"media-output-transcript-{source['id']}")

    assert response.status_code == 200
    assert response.body["status"] == "completed"
    assert response.body["output_preview"] == "从设置读取命令的 ASR 输出。"
    assert output is not None
    assert output["provider"] == "local-settings-asr"
    assert output["text"] == "从设置读取命令的 ASR 输出。"
    assert "path" not in output["metadata"]


def test_configured_local_asr_run_records_missing_executable_failure(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    source = _audio_source(object_store)
    audio_path = _audio_file(tmp_path)
    AuthorizeLocalAudioFileForSource(object_store).execute(source_id=str(source["id"]), file_path=str(audio_path))
    CreateMediaProcessingQueueJob(object_store, enabled_capabilities=("audio_transcription",)).execute(
        source_id=str(source["id"])
    )
    SaveLocalAsrProviderSettings(object_store).execute(
        enabled=True,
        command=("missing-local-asr-provider-executable", "{audio_path}"),
        confirm_enable=True,
    )

    result = RunConfiguredLocalAsrProviderForSource(object_store).execute(source_id=str(source["id"]))

    assert result.status == "failed"
    assert result.error == "local ASR provider executable not found"
