from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from .ports import ObjectStorePort

from .local_asr_provider_settings import GetLocalAsrProviderSettings
from .local_document_text_extractor_settings import GetLocalDocumentTextExtractorSettings
from .local_ocr_provider_settings import GetLocalOcrProviderSettings
from .local_video_provider_settings import GetLocalVideoProviderSettings


PROVIDER_DOCTOR_STATUSES = ("ready", "disabled", "missing", "misconfigured", "failed")


@dataclass(frozen=True, slots=True)
class ProviderDoctorCapability:
    capability: str
    provider_key: str
    status: str
    diagnostic: str
    provider_name: str
    enabled: bool
    actionable: bool
    reason: str
    next_step: str
    supported_statuses: tuple[str, ...]
    reads_user_files: bool
    runs_provider: bool
    remote_processing: bool
    memory_publication: str


@dataclass(frozen=True, slots=True)
class ProviderDoctorReport:
    status: str
    capabilities: tuple[ProviderDoctorCapability, ...]
    reads_user_files: bool
    runs_providers: bool
    publishes_memory: bool


class GetProviderDoctorReport:
    """Summarize local Provider readiness without executing any Provider."""

    def __init__(self, object_store: ObjectStorePort) -> None:
        self._object_store = object_store

    def execute(self) -> ProviderDoctorReport:
        capabilities = (
            _capability_from_settings(
                capability="文档正文读取",
                provider_key="document_text_extractor",
                settings=GetLocalDocumentTextExtractorSettings(self._object_store).execute(),
                disabled_reason="文档正文读取 Provider 未显式启用。",
                missing_reason="文档正文读取命令的可执行文件不存在。",
                misconfigured_reason="文档正文读取 Provider 配置不完整。",
                failed_reason="文档正文读取 Provider 最近执行失败，需要查看 Source 输出错误。",
                ready_reason="文档正文读取 Provider 已配置，可在用户授权文档后运行。",
            ),
            _capability_from_settings(
                capability="图片 OCR",
                provider_key="image_ocr",
                settings=GetLocalOcrProviderSettings(self._object_store).execute(),
                disabled_reason="图片 OCR Provider 未显式启用。",
                missing_reason="图片 OCR 命令的可执行文件不存在。",
                misconfigured_reason="图片 OCR Provider 配置不完整。",
                failed_reason="图片 OCR Provider 最近执行失败，需要查看媒体处理错误。",
                ready_reason="图片 OCR Provider 已配置，可在用户授权图片后运行。",
            ),
            _capability_from_settings(
                capability="音频转写",
                provider_key="audio_transcription",
                settings=GetLocalAsrProviderSettings(self._object_store).execute(),
                disabled_reason="音频转写 Provider 未显式启用。",
                missing_reason="音频转写命令的可执行文件不存在。",
                misconfigured_reason="音频转写 Provider 配置不完整。",
                failed_reason="音频转写 Provider 最近执行失败，需要查看媒体处理错误。",
                ready_reason="音频转写 Provider 已配置，可在用户授权音频后运行。",
            ),
            _capability_from_settings(
                capability="视频帧提取",
                provider_key="video_frame_extraction",
                settings=GetLocalVideoProviderSettings(self._object_store).execute(),
                disabled_reason="视频帧提取 Provider 未显式启用。",
                missing_reason="视频帧提取命令的可执行文件不存在。",
                misconfigured_reason="视频帧提取 Provider 配置不完整。",
                failed_reason="视频帧提取 Provider 最近执行失败，需要查看媒体处理错误。",
                ready_reason="视频帧提取 Provider 已配置，可在用户授权视频后运行。",
            ),
        )
        report_status = "ready" if all(item.status == "ready" for item in capabilities) else "needs_attention"
        return ProviderDoctorReport(
            status=report_status,
            capabilities=capabilities,
            reads_user_files=False,
            runs_providers=False,
            publishes_memory=False,
        )


def serialize_provider_doctor_report(report: ProviderDoctorReport) -> dict[str, object]:
    return {
        "status": report.status,
        "capabilities": [
            {
                "capability": item.capability,
                "provider_key": item.provider_key,
                "status": item.status,
                "diagnostic": item.diagnostic,
                "provider_name": item.provider_name,
                "enabled": item.enabled,
                "actionable": item.actionable,
                "reason": item.reason,
                "next_step": item.next_step,
                "supported_statuses": list(item.supported_statuses),
                "reads_user_files": item.reads_user_files,
                "runs_provider": item.runs_provider,
                "remote_processing": item.remote_processing,
                "memory_publication": item.memory_publication,
            }
            for item in report.capabilities
        ],
        "reads_user_files": report.reads_user_files,
        "runs_providers": report.runs_providers,
        "publishes_memory": report.publishes_memory,
    }


def classify_provider_diagnostic(*, enabled: bool, diagnostic: str) -> str:
    if not enabled:
        return "disabled"
    if diagnostic == "ready":
        return "ready"
    if diagnostic == "executable_not_found":
        return "missing"
    if diagnostic in {"command_not_configured", "invalid_command", "invalid_provider_settings"}:
        return "misconfigured"
    if diagnostic in {"provider_execution_failed", "last_run_failed"}:
        return "failed"
    return "misconfigured"


def _capability_from_settings(
    *,
    capability: str,
    provider_key: str,
    settings: object,
    disabled_reason: str,
    missing_reason: str,
    misconfigured_reason: str,
    failed_reason: str,
    ready_reason: str,
) -> ProviderDoctorCapability:
    values = _settings_values(settings)
    diagnostic = values["diagnostic"]
    enabled = values["enabled"] is True
    status = classify_provider_diagnostic(enabled=enabled, diagnostic=diagnostic)
    reason_by_status: Mapping[str, str] = {
        "ready": ready_reason,
        "disabled": disabled_reason,
        "missing": missing_reason,
        "misconfigured": misconfigured_reason,
        "failed": failed_reason,
    }
    next_step_by_status: Mapping[str, str] = {
        "ready": "在 Source 详情中由用户授权后手动运行。",
        "disabled": "到设置页显式启用并确认本地命令。",
        "missing": "安装工具或修正命令中的可执行文件路径。",
        "misconfigured": "补齐命令模板和占位符后重新保存设置。",
        "failed": "查看最近一次 Source 或媒体处理输出的错误，再重新运行。",
    }
    return ProviderDoctorCapability(
        capability=capability,
        provider_key=provider_key,
        status=status,
        diagnostic=diagnostic,
        provider_name=str(values["provider_name"]),
        enabled=enabled,
        actionable=status != "ready",
        reason=reason_by_status[status],
        next_step=next_step_by_status[status],
        supported_statuses=PROVIDER_DOCTOR_STATUSES,
        reads_user_files=False,
        runs_provider=False,
        remote_processing=values["remote_processing"] is True,
        memory_publication=str(values["memory_publication"]),
    )


def _settings_values(settings: object) -> dict[str, object]:
    getter: Callable[[str], object] = lambda name: getattr(settings, name)
    return {
        "enabled": getter("enabled"),
        "diagnostic": str(getter("diagnostic")),
        "provider_name": str(getter("provider_name")),
        "remote_processing": getter("remote_processing"),
        "memory_publication": str(getter("memory_publication")),
    }
