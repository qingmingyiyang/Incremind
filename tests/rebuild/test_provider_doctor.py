from __future__ import annotations

import sys
from pathlib import Path

from core.product_core import (
    PROVIDER_DOCTOR_STATUSES,
    GetProviderDoctorReport,
    SaveLocalAsrProviderSettings,
    SaveLocalDocumentTextExtractorSettings,
    SaveLocalOcrProviderSettings,
    SaveLocalVideoProviderSettings,
    classify_provider_diagnostic,
    serialize_provider_doctor_report,
)
from core.storage_provider import JsonObjectStore


def _store(tmp_path: Path) -> JsonObjectStore:
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def test_provider_doctor_reports_all_capabilities_disabled_without_running_providers(tmp_path: Path) -> None:
    object_store = _store(tmp_path)

    report = GetProviderDoctorReport(object_store).execute()
    payload = serialize_provider_doctor_report(report)

    assert payload["status"] == "needs_attention"
    assert payload["reads_user_files"] is False
    assert payload["runs_providers"] is False
    assert payload["publishes_memory"] is False
    assert {item["provider_key"] for item in payload["capabilities"]} == {
        "document_text_extractor",
        "image_ocr",
        "audio_transcription",
        "video_frame_extraction",
    }
    for item in payload["capabilities"]:
        assert item["status"] == "disabled"
        assert item["diagnostic"] == "disabled_until_explicit_enable"
        assert item["supported_statuses"] == list(PROVIDER_DOCTOR_STATUSES)
        assert item["reads_user_files"] is False
        assert item["runs_provider"] is False
        assert item["remote_processing"] is False
        assert item["memory_publication"] == "not_started"


def test_provider_doctor_maps_missing_executables_to_missing_status(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    SaveLocalOcrProviderSettings(object_store).execute(
        enabled=True,
        command=("missing-local-ocr-provider-executable", "{image_path}"),
        confirm_enable=True,
    )

    report = serialize_provider_doctor_report(GetProviderDoctorReport(object_store).execute())
    image_ocr = _capability(report, "image_ocr")

    assert image_ocr["status"] == "missing"
    assert image_ocr["diagnostic"] == "executable_not_found"
    assert image_ocr["actionable"] is True
    assert "可执行文件不存在" in str(image_ocr["reason"])
    assert "安装工具" in str(image_ocr["next_step"])


def test_provider_doctor_reports_ready_only_when_all_four_providers_are_ready(tmp_path: Path) -> None:
    object_store = _store(tmp_path)
    command = (sys.executable, "--version")
    SaveLocalDocumentTextExtractorSettings(object_store).execute(
        enabled=True,
        command=command,
        confirm_enable=True,
    )
    SaveLocalOcrProviderSettings(object_store).execute(
        enabled=True,
        command=command,
        confirm_enable=True,
    )
    SaveLocalAsrProviderSettings(object_store).execute(
        enabled=True,
        command=command,
        confirm_enable=True,
    )
    SaveLocalVideoProviderSettings(object_store).execute(
        enabled=True,
        command=command,
        confirm_enable=True,
    )

    report = serialize_provider_doctor_report(GetProviderDoctorReport(object_store).execute())

    assert report["status"] == "ready"
    assert {item["status"] for item in report["capabilities"]} == {"ready"}
    assert report["reads_user_files"] is False
    assert report["runs_providers"] is False
    assert report["publishes_memory"] is False


def test_provider_doctor_status_contract_includes_misconfigured_and_failed() -> None:
    assert classify_provider_diagnostic(enabled=False, diagnostic="ready") == "disabled"
    assert classify_provider_diagnostic(enabled=True, diagnostic="ready") == "ready"
    assert classify_provider_diagnostic(enabled=True, diagnostic="executable_not_found") == "missing"
    assert classify_provider_diagnostic(enabled=True, diagnostic="command_not_configured") == "misconfigured"
    assert classify_provider_diagnostic(enabled=True, diagnostic="provider_execution_failed") == "failed"


def _capability(payload: dict[str, object], provider_key: str) -> dict[str, object]:
    capabilities = payload["capabilities"]
    assert isinstance(capabilities, list)
    for item in capabilities:
        assert isinstance(item, dict)
        if item["provider_key"] == provider_key:
            return item
    raise AssertionError(f"missing capability {provider_key}")
