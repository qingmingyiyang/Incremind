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
    parser.add_argument("--hotwords-file")
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

        transcriber = FasterWhisperTranscriber(
            str(model_dir),
            device="auto",
            compute_type="int8",
            transcription_mode=args.mode,
            language=args.language,
        )
        words = read_hotwords_file(args.hotwords_file, app_root) if args.hotwords_file else ()
        extra = {"hotwords": words} if args.hotwords_file else {}
        transcript = transcriber.transcribe(audio_path, app_root / "data" / "tmp" / "local-asr" / audio_path.stem, **extra)
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
        if args.hotwords_file:
            payload["hotwords"] = transcriber.hotword_receipt
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        return 0
    except Exception as error:  # noqa: BLE001 - CLI must return a bounded diagnostic.
        sys.stderr.write(_bounded_error(error))
        return 2


def read_hotwords_file(path, app_root):
    from core.product_core.audio_asset_transcriber import _lexicon_snapshot
    target = Path(path).resolve(strict=True)
    if not target.is_relative_to(Path(app_root).resolve()) or not target.is_file() or target.stat().st_size > 1024 * 1024:
        raise ValueError("local_asr_hotword_file_invalid")
    snapshot = _lexicon_snapshot(json.loads(target.read_text(encoding="utf-8")))
    return tuple(term["term"] for term in snapshot["terms"])


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
