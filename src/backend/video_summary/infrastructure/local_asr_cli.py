from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from backend.video_summary.infrastructure.faster_whisper_models import (
    FasterWhisperModelManager,
)
from backend.video_summary.infrastructure.faster_whisper_transcriber import (
    FasterWhisperTranscriber,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Chriptmas OS packaged local ASR")
    parser.add_argument("--audio", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--mode", choices=("fast", "balanced", "accurate"), default="balanced")
    parser.add_argument("--language", default="zh")
    args = parser.parse_args(argv)

    try:
        app_root = _app_root()
        audio_path = Path(args.audio).expanduser().resolve(strict=True)
        if not audio_path.is_file():
            raise ValueError("authorized audio path is not a file")
        manager = FasterWhisperModelManager(app_root / "data" / "models" / "faster-whisper")
        if not manager.is_supported(args.model):
            raise ValueError("unsupported local ASR model")
        if not manager.is_downloaded(args.model):
            raise ValueError("local ASR model is missing or incomplete")
        model_dir = manager.resolve_model_dir(args.model).resolve(strict=True)
        models_root = (app_root / "data" / "models" / "faster-whisper").resolve(strict=True)
        if not model_dir.is_relative_to(models_root):
            raise ValueError("local ASR model escaped app data root")

        transcript = FasterWhisperTranscriber(
            str(model_dir),
            device="auto",
            compute_type="int8",
            transcription_mode=args.mode,
            language=args.language,
        ).transcribe(audio_path, app_root / "data" / "tmp" / "local-asr" / audio_path.stem)
        payload = {
            "language": transcript.language,
            "segments": [
                {
                    "start_seconds": segment.start_seconds,
                    "end_seconds": segment.end_seconds,
                    "text": segment.text,
                }
                for segment in transcript.segments
            ],
        }
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        return 0
    except Exception as error:  # noqa: BLE001 - CLI must return a bounded diagnostic.
        sys.stderr.write(_bounded_error(error))
        return 2


def _app_root() -> Path:
    configured = os.environ.get("CHRIPTMAS_APP_ROOT", "").strip()
    if not configured:
        raise ValueError("CHRIPTMAS_APP_ROOT is required for packaged local ASR")
    root = Path(configured).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError("CHRIPTMAS_APP_ROOT is not a directory")
    return root


def _bounded_error(error: Exception) -> str:
    detail = " ".join((str(error) or error.__class__.__name__).split())
    return detail[:240] or "local ASR failed"


if __name__ == "__main__":
    raise SystemExit(main())
