from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

from core.composition import build_local_command_ocr_adapter, build_source_image_authorization
from core.ingestion_core import ObjectStoreSourceRegistrar, SourceSubmission
from core.product_core import (
    AuthorizeLocalImageFileForSource,
    CreateMediaProcessingQueueJob,
    GetLocalOcrProviderSettings,
    LocalCommandImageOcrAdapter,
    LocalOcrProviderSettingsError,
    RunImageOcrAdapterForMediaJob,
    RunConfiguredLocalOcrProviderForSource,
    SaveLocalOcrProviderSettings,
    ServeLocalOcrProviderRunEndpoint,
    ServeLocalOcrProviderSettingsEndpoint,
)
from core.storage_provider import JsonObjectStore


ROOT = Path(__file__).resolve().parents[2]


def _store(tmp_path: Path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def _image_source(object_store: JsonObjectStore) -> dict[str, object]:
    source = ObjectStoreSourceRegistrar(object_store).register(
        SourceSubmission(
            kind="image",
            title="Local OCR image",
            display_name="whiteboard.png",
            media_type="image/png",
            size_bytes=128,
            image_reference="platform-image-ref-local-ocr",
            width_px=640,
            height_px=480,
        )
    )
    return dict(source)


def _image_file(tmp_path: Path) -> Path:
    image_path = tmp_path / "whiteboard.png"
    image_path.write_bytes(b"not-a-real-image-but-user-authorized")
    return image_path


def _fake_ocr_script(tmp_path: Path, *, text: str = "真实本地 OCR Provider 输出文本。") -> Path:
    script = tmp_path / "fake_ocr_provider.py"
    script.write_text(
        "\n".join(
            [
                "from pathlib import Path",
                "import sys",
                "image = Path(sys.argv[1])",
                "if not image.exists():",
                "    raise SystemExit(3)",
                f"print({text!r})",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return script


def _real_ocr_image(tmp_path: Path) -> Path:
    image_path = tmp_path / "real-ocr.png"
    image = Image.new("RGB", (1100, 220), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype(r"C:\Windows\Fonts\msyh.ttc", 72)
    draw.text((36, 55), "圣诞记忆 OCR2026", fill="black", font=font)
    image.save(image_path)
    return image_path


def test_authorize_local_image_reference_without_storing_path_in_source(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    source = _image_source(object_store)
    image_path = _image_file(tmp_path)

    result = AuthorizeLocalImageFileForSource(object_store).execute(
        source_id=str(source["id"]),
        file_path=str(image_path),
    )
    updated_source = object_store.read("sources", str(source["id"]))
    authorization = object_store.read("authorized_file_refs", result.authorization_id)

    assert result.status == "authorized"
    assert result.file_reference == "platform-image-ref-local-ocr"
    assert authorization is not None
    assert authorization["path"] == str(image_path.resolve(strict=False))
    assert authorization["path_scope"] == "local_user_authorized_image"
    assert updated_source is not None
    assert updated_source["metadata"]["image_authorization"]["authorization_id"] == result.authorization_id
    assert updated_source["metadata"]["image_authorization"]["path_stored_in_source"] is False
    assert "path" not in updated_source["metadata"]["image_authorization"]
    assert not (tmp_path / "library").exists()


def test_local_ocr_provider_is_default_disabled_and_records_failed_job(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    source = _image_source(object_store)
    image_path = _image_file(tmp_path)
    AuthorizeLocalImageFileForSource(object_store).execute(source_id=str(source["id"]), file_path=str(image_path))
    queued = CreateMediaProcessingQueueJob(object_store, enabled_capabilities=("ocr",)).execute(
        source_id=str(source["id"])
    )
    script = _fake_ocr_script(tmp_path)
    adapter = LocalCommandImageOcrAdapter(
        object_store=object_store,
        command=(sys.executable, str(script), "{image_path}"),
    )

    result = RunImageOcrAdapterForMediaJob(object_store).execute(job_id=queued.job_id, adapter=adapter)
    output = object_store.read("media_processing_outputs", f"media-output-ocr-{source['id']}")

    assert result.status == "failed"
    assert result.error == "local OCR provider is disabled"
    assert output is None
    assert not (tmp_path / "library").exists()


def test_local_ocr_provider_requires_authorized_image_reference(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    source = _image_source(object_store)
    queued = CreateMediaProcessingQueueJob(object_store, enabled_capabilities=("ocr",)).execute(
        source_id=str(source["id"])
    )
    script = _fake_ocr_script(tmp_path)
    adapter = LocalCommandImageOcrAdapter(
        object_store=object_store,
        command=(sys.executable, str(script), "{image_path}"),
        enabled=True,
    )

    result = RunImageOcrAdapterForMediaJob(object_store).execute(job_id=queued.job_id, adapter=adapter)

    assert result.status == "failed"
    assert result.error == "authorized image reference not found"


def test_local_ocr_provider_writes_output_without_path_leak(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    source = _image_source(object_store)
    image_path = _image_file(tmp_path)
    authorization = AuthorizeLocalImageFileForSource(object_store).execute(
        source_id=str(source["id"]),
        file_path=str(image_path),
    )
    queued = CreateMediaProcessingQueueJob(object_store, enabled_capabilities=("ocr",)).execute(
        source_id=str(source["id"])
    )
    script = _fake_ocr_script(tmp_path)
    adapter = LocalCommandImageOcrAdapter(
        object_store=object_store,
        command=(sys.executable, str(script), "{image_path}"),
        enabled=True,
        provider_name="local-tesseract-compatible-ocr",
    )

    result = RunImageOcrAdapterForMediaJob(object_store).execute(job_id=queued.job_id, adapter=adapter)
    output = object_store.read("media_processing_outputs", f"media-output-ocr-{source['id']}")

    assert result.status == "completed"
    assert result.output_preview == "真实本地 OCR Provider 输出文本。"
    assert output is not None
    assert output["provider"] == "local-tesseract-compatible-ocr"
    assert output["text"] == "真实本地 OCR Provider 输出文本。"
    assert output["metadata"]["local_processing"] is True
    assert output["metadata"]["remote_processing"] is False
    assert output["metadata"]["image_reference"] == "platform-image-ref-local-ocr"
    assert output["metadata"]["authorization_id"] == authorization.authorization_id
    assert output["metadata"]["path_stored_in_output"] is False
    assert "path" not in output["metadata"]
    assert not (tmp_path / "library").exists()


def test_local_ocr_provider_missing_executable_is_traceable_failure(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    source = _image_source(object_store)
    image_path = _image_file(tmp_path)
    AuthorizeLocalImageFileForSource(object_store).execute(source_id=str(source["id"]), file_path=str(image_path))
    queued = CreateMediaProcessingQueueJob(object_store, enabled_capabilities=("ocr",)).execute(
        source_id=str(source["id"])
    )
    adapter = LocalCommandImageOcrAdapter(
        object_store=object_store,
        command=("missing-local-ocr-provider-executable", "{image_path}"),
        enabled=True,
    )

    result = RunImageOcrAdapterForMediaJob(object_store).execute(job_id=queued.job_id, adapter=adapter)

    assert result.status == "failed"
    assert result.error == "local OCR provider executable not found"


def test_local_ocr_provider_composition_uses_temp_storage_only(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    source = _image_source(object_store)
    image_path = _image_file(tmp_path)
    build_source_image_authorization(ROOT, runtime_root=tmp_path).execute(
        source_id=str(source["id"]),
        file_path=str(image_path),
    )
    queued = CreateMediaProcessingQueueJob(object_store, enabled_capabilities=("ocr",)).execute(
        source_id=str(source["id"])
    )
    script = _fake_ocr_script(tmp_path, text="组合入口 OCR 输出。")
    adapter = build_local_command_ocr_adapter(
        ROOT,
        runtime_root=tmp_path,
        command=(sys.executable, str(script), "{image_path}"),
        enabled=True,
        provider_name="local-composed-ocr",
    )

    result = RunImageOcrAdapterForMediaJob(object_store).execute(job_id=queued.job_id, adapter=adapter)
    output = object_store.read("media_processing_outputs", f"media-output-ocr-{source['id']}")

    assert result.status == "completed"
    assert output is not None
    assert output["provider"] == "local-composed-ocr"
    assert output["text"] == "组合入口 OCR 输出。"
    assert not (tmp_path / "library").exists()


def test_local_ocr_provider_settings_are_default_off(tmp_path: Path) -> None:
    object_store = _store(tmp_path)

    settings = GetLocalOcrProviderSettings(object_store).execute()

    assert settings.enabled is False
    assert settings.status == "disabled"
    assert settings.diagnostic == "disabled_until_explicit_enable"
    assert settings.remote_processing is False
    assert settings.memory_publication == "not_started"


def test_enabling_local_ocr_provider_requires_confirmation_and_command(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    writer = SaveLocalOcrProviderSettings(object_store)

    try:
        writer.execute(enabled=True, command=(sys.executable,), confirm_enable=False)
    except LocalOcrProviderSettingsError as error:
        assert str(error) == "enabling local OCR provider requires confirm_enable=true"
    else:
        raise AssertionError("expected explicit enable guard")

    try:
        writer.execute(enabled=True, command=(), confirm_enable=True)
    except LocalOcrProviderSettingsError as error:
        assert str(error) == "enabled local OCR provider requires command"
    else:
        raise AssertionError("expected command guard")

    settings = writer.execute(
        enabled=True,
        command=(sys.executable, "--version"),
        provider_name="local-python-ocr-smoke",
        confirm_enable=True,
    )

    assert settings.enabled is True
    assert settings.status == "ready"
    assert settings.provider_name == "local-python-ocr-smoke"
    assert settings.command == (sys.executable, "--version")


def test_local_ocr_provider_settings_endpoint_reports_missing_executable(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    endpoint = ServeLocalOcrProviderSettingsEndpoint()
    writer = SaveLocalOcrProviderSettings(object_store)

    response = endpoint.execute(
        method="PUT",
        path="/api/rebuild/settings/local-ocr-provider",
        body={
            "enabled": True,
            "command": ["missing-local-ocr-provider-executable", "{image_path}"],
            "confirm_enable": True,
        },
        get_settings=lambda: GetLocalOcrProviderSettings(object_store).execute(),
        save_settings=writer.execute,
    )

    assert response.status_code == 200
    assert response.body["status"] == "degraded"
    assert response.body["diagnostic"] == "executable_not_found"
    assert response.body["enabled"] is True


def test_local_ocr_run_endpoint_rejects_ui_supplied_command(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    source = _image_source(object_store)
    endpoint = ServeLocalOcrProviderRunEndpoint()

    response = endpoint.execute(
        method="POST",
        path=f"/api/rebuild/sources/{source['id']}/ocr",
        body={"command": ["should-not-be-accepted", "{image_path}"]},
        run_ocr=RunConfiguredLocalOcrProviderForSource(object_store).execute,
    )

    assert response.status_code == 400
    assert response.body["reason"] == "local OCR run endpoint does not accept provider command"


def test_configured_local_ocr_run_endpoint_uses_saved_provider_settings(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    source = _image_source(object_store)
    image_path = _image_file(tmp_path)
    AuthorizeLocalImageFileForSource(object_store).execute(source_id=str(source["id"]), file_path=str(image_path))
    CreateMediaProcessingQueueJob(object_store, enabled_capabilities=("ocr",)).execute(source_id=str(source["id"]))
    script = _fake_ocr_script(tmp_path, text="从设置读取命令的 OCR 输出。")
    SaveLocalOcrProviderSettings(object_store).execute(
        enabled=True,
        command=(sys.executable, str(script), "{image_path}"),
        provider_name="local-settings-ocr",
        confirm_enable=True,
    )
    endpoint = ServeLocalOcrProviderRunEndpoint()

    response = endpoint.execute(
        method="POST",
        path=f"/api/rebuild/sources/{source['id']}/ocr",
        body={},
        run_ocr=RunConfiguredLocalOcrProviderForSource(object_store).execute,
    )
    output = object_store.read("media_processing_outputs", f"media-output-ocr-{source['id']}")

    assert response.status_code == 200
    assert response.body["status"] == "completed"
    assert response.body["output_preview"] == "从设置读取命令的 OCR 输出。"
    assert output is not None
    assert output["provider"] == "local-settings-ocr"
    assert output["text"] == "从设置读取命令的 OCR 输出。"
    assert "path" not in output["metadata"]


def test_configured_local_ocr_run_records_missing_executable_failure(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    source = _image_source(object_store)
    image_path = _image_file(tmp_path)
    AuthorizeLocalImageFileForSource(object_store).execute(source_id=str(source["id"]), file_path=str(image_path))
    CreateMediaProcessingQueueJob(object_store, enabled_capabilities=("ocr",)).execute(source_id=str(source["id"]))
    SaveLocalOcrProviderSettings(object_store).execute(
        enabled=True,
        command=("missing-local-ocr-provider-executable", "{image_path}"),
        confirm_enable=True,
    )

    result = RunConfiguredLocalOcrProviderForSource(object_store).execute(source_id=str(source["id"]))

    assert result.status == "failed"
    assert result.error == "local OCR provider executable not found"


@pytest.mark.skipif(os.name != "nt", reason="Windows.Media.Ocr is available only on Windows")
def test_builtin_windows_ocr_recognizes_real_chinese_and_english_image(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    source = _image_source(object_store)
    image_path = _real_ocr_image(tmp_path)
    authorization = AuthorizeLocalImageFileForSource(object_store).execute(
        source_id=str(source["id"]),
        file_path=str(image_path),
    )
    CreateMediaProcessingQueueJob(object_store, enabled_capabilities=("ocr",)).execute(
        source_id=str(source["id"])
    )
    settings = SaveLocalOcrProviderSettings(object_store).execute(
        enabled=True,
        command=("builtin:windows-ocr",),
        provider_name="builtin-windows-ocr",
        confirm_enable=True,
    )

    result = RunConfiguredLocalOcrProviderForSource(object_store).execute(source_id=str(source["id"]))
    output = object_store.read("media_processing_outputs", f"media-output-ocr-{source['id']}")

    assert settings.status == "ready"
    assert result.status == "completed"
    assert output is not None
    normalized_text = str(output["text"]).replace(" ", "").replace("\r", "").replace("\n", "")
    assert "圣诞记忆" in normalized_text
    assert "OCR2026" in normalized_text
    assert output["provider"] == "builtin-windows-ocr"
    assert output["metadata"]["local_processing"] is True
    assert output["metadata"]["remote_processing"] is False
    assert output["metadata"]["authorization_id"] == authorization.authorization_id
    assert "path" not in output["metadata"]
    assert not (tmp_path / "library").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows.Media.Ocr is available only on Windows")
def test_builtin_windows_ocr_records_blank_image_as_diagnostic_failure(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    source = _image_source(object_store)
    image_path = tmp_path / "blank.png"
    Image.new("RGB", (640, 320), "white").save(image_path)
    AuthorizeLocalImageFileForSource(object_store).execute(source_id=str(source["id"]), file_path=str(image_path))
    CreateMediaProcessingQueueJob(object_store, enabled_capabilities=("ocr",)).execute(source_id=str(source["id"]))
    SaveLocalOcrProviderSettings(object_store).execute(
        enabled=True,
        command=("builtin:windows-ocr",),
        provider_name="builtin-windows-ocr",
        confirm_enable=True,
    )

    result = RunConfiguredLocalOcrProviderForSource(object_store).execute(source_id=str(source["id"]))
    output = object_store.read("media_processing_outputs", f"media-output-ocr-{source['id']}")

    assert result.status == "failed"
    assert "Windows OCR returned no text" in str(result.error)
    assert output is None


@pytest.mark.skipif(os.name != "nt", reason="Windows.Media.Ocr is available only on Windows")
def test_builtin_windows_ocr_redacts_paths_for_corrupt_image(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    source = _image_source(object_store)
    image_path = tmp_path / "private-canary-corrupt.png"
    image_path.write_bytes(b"not-a-real-png\x00\xffMED01")
    AuthorizeLocalImageFileForSource(object_store).execute(
        source_id=str(source["id"]),
        file_path=str(image_path),
    )
    CreateMediaProcessingQueueJob(object_store, enabled_capabilities=("ocr",)).execute(
        source_id=str(source["id"])
    )
    SaveLocalOcrProviderSettings(object_store).execute(
        enabled=True,
        command=("builtin:windows-ocr",),
        provider_name="builtin-windows-ocr",
        confirm_enable=True,
    )

    result = RunConfiguredLocalOcrProviderForSource(object_store).execute(
        source_id=str(source["id"])
    )
    output = object_store.read("media_processing_outputs", f"media-output-ocr-{source['id']}")
    job = object_store.list("media_processing_jobs")[0]

    assert result.status == "failed"
    assert result.error == "Windows OCR failed to decode or recognize image"
    assert job["error"] == result.error
    assert str(tmp_path) not in result.error
    assert "windows_ocr.ps1" not in result.error
    assert "private-canary-corrupt.png" not in result.error
    assert output is None
