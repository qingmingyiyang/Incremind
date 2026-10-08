from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from backend.video_summary.generation.usecases.generate_mindmap import GenerateMindmap
from backend.video_summary.generation.usecases.generate_summary import GenerateVideoSummary
from backend.video_summary.infrastructure.filesystem_generation_artifact_store import FileSystemGenerationArtifactStore
from backend.video_summary.infrastructure.litellm_mindmap_generator import LiteLLMMindmapGenerator
from backend.video_summary.infrastructure.litellm_transcript_enhancer import LiteLLMTranscriptEnhancer
from backend.video_summary.infrastructure.media_tools import FfmpegMediaProcessor
from backend.video_summary.infrastructure.video_summary_runtime import (
    build_litellm_completion_gateway,
    build_video_summary_runtime,
)
from backend.video_summary.infrastructure.settings import AppSettings, load_settings
from backend.security import build_active_provider_egress_guard


@dataclass(frozen=True)
class VideoSummaryApplication:
    settings: AppSettings
    use_case: GenerateVideoSummary


@dataclass(frozen=True)
class MindmapApplication:
    settings: AppSettings
    use_case: GenerateMindmap


def build_video_summary_application(
    config_path: Path,
    root_dir: Path,
    transcript_enhancement_enabled: bool | None = None,
) -> VideoSummaryApplication:
    settings = load_settings(config_path=config_path, root_dir=root_dir)
    resolved_transcript_enhancement_enabled = (
        settings.asr.transcript_enhancement_enabled
        if transcript_enhancement_enabled is None
        else transcript_enhancement_enabled
    )
    runtime = build_video_summary_runtime(
        settings,
        egress_guard=build_active_provider_egress_guard(root_dir, endpoint=settings.openai.base_url),
    )
    artifact_store = FileSystemGenerationArtifactStore()
    use_case = GenerateVideoSummary(
        media_processor=FfmpegMediaProcessor(),
        transcriber=runtime.transcriber,
        transcript_enhancer=(
            LiteLLMTranscriptEnhancer(gateway=runtime.gateway)
            if resolved_transcript_enhancement_enabled
            else None
        ),
        summarizer=runtime.summarizer,
        artifact_store=artifact_store,
    )
    return VideoSummaryApplication(settings=settings, use_case=use_case)


def build_mindmap_application(config_path: Path, root_dir: Path) -> MindmapApplication:
    settings = load_settings(config_path=config_path, root_dir=root_dir)
    gateway = build_litellm_completion_gateway(
        settings,
        egress_guard=build_active_provider_egress_guard(root_dir, endpoint=settings.openai.base_url),
        egress_purpose="mindmap",
    )
    use_case = GenerateMindmap(
        generator=LiteLLMMindmapGenerator(gateway=gateway),
        artifact_store=FileSystemGenerationArtifactStore(),
    )
    return MindmapApplication(settings=settings, use_case=use_case)
