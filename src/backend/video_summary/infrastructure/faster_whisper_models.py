from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from backend.video_summary.infrastructure.huggingface_model_downloader import (
    HuggingFaceDownloadSpec,
    HuggingFaceModelDownloader,
    verify_downloaded_model,
)


SUPPORTED_FASTER_WHISPER_MODELS = (
    ("small", "Small"),
    ("medium", "Medium"),
    ("large-v3", "Large V3"),
    ("large-v3-turbo", "Large V3 Turbo"),
)

FASTER_WHISPER_MODEL_SOURCES = {
    "small": ("Systran/faster-whisper-small", "536b0662742c02347bc0e980a01041f333bce120"),
    "medium": ("Systran/faster-whisper-medium", "08e178d48790749d25932bbc082711ddcfdfbc4f"),
    "large-v3": ("Systran/faster-whisper-large-v3", "edaa852ec7e145841d8ffdb056a99866b5f0a478"),
    "large-v3-turbo": ("mobiuslabsgmbh/faster-whisper-large-v3-turbo", "0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf"),
}

_MODEL_ALLOW_PATTERNS = (
    "config.json", "preprocessor_config.json", "model.bin", "tokenizer.json", "vocabulary.*",
)


@dataclass(frozen=True)
class FasterWhisperModelInfo:
    id: str
    label: str
    downloaded: bool
    current: bool
    recommended: bool


class FasterWhisperModelManager:
    def __init__(
        self,
        models_dir: Path,
        *,
        downloader: HuggingFaceModelDownloader | None = None,
    ) -> None:
        self._models_dir = models_dir
        self._downloader = downloader or HuggingFaceModelDownloader()

    def list_models(self, current_model_size: str) -> list[FasterWhisperModelInfo]:
        return [
            FasterWhisperModelInfo(
                id=model_id,
                label=label,
                downloaded=self.is_downloaded(model_id),
                current=model_id == current_model_size,
                recommended=model_id == "large-v3-turbo",
            )
            for model_id, label in SUPPORTED_FASTER_WHISPER_MODELS
        ]

    def is_supported(self, model_size: str) -> bool:
        return any(candidate == model_size for candidate, _ in SUPPORTED_FASTER_WHISPER_MODELS)

    def is_downloaded(self, model_size: str) -> bool:
        if model_size not in FASTER_WHISPER_MODEL_SOURCES:
            return False
        try:
            verify_downloaded_model(self.resolve_model_dir(model_size), self.download_spec(model_size))
        except (OSError, RuntimeError):
            return False
        return True

    def resolve_model_dir(self, model_size: str) -> Path:
        return self._models_dir / model_size

    def resolve_model_source(self, model_size: str) -> str:
        model_dir = self.resolve_model_dir(model_size)
        return str(model_dir) if self.is_downloaded(model_size) else model_size

    def download(self, model_size: str, progress_reporter=None) -> Path:
        if not self.is_supported(model_size):
            raise ValueError(f"unsupported faster-whisper model '{model_size}'")

        if self.is_downloaded(model_size):
            if progress_reporter is not None:
                progress_reporter.update("download", 100.0, "模型已存在于项目目录")
                progress_reporter.completed("模型已准备就绪")
            return self.resolve_model_dir(model_size)

        target_dir = self.resolve_model_dir(model_size)
        reporter = progress_reporter or _NullProgressReporter()
        self._downloader.download(self.download_spec(model_size), reporter)
        reporter.completed("模型下载完成")
        return target_dir

    def download_spec(self, model_size: str) -> HuggingFaceDownloadSpec:
        if model_size not in FASTER_WHISPER_MODEL_SOURCES:
            raise ValueError(f"unsupported faster-whisper model '{model_size}'")
        repo_id, revision = FASTER_WHISPER_MODEL_SOURCES[model_size]
        return HuggingFaceDownloadSpec(
            repo_id=repo_id,
            revision=revision,
            target_dir=self.resolve_model_dir(model_size),
            allow_patterns=_MODEL_ALLOW_PATTERNS,
            required_files=("model.bin", "config.json"),
            required_file_patterns=(),
        )


class _NullProgressReporter:
    def update(self, stage: str, progress: float | None = None, detail: str | None = None) -> None:
        pass

    def completed(self, detail: str | None = None) -> None:
        pass

    def raise_if_cancelled(self) -> None:
        pass
