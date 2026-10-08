from __future__ import annotations

import json
from pathlib import Path

from backend.video_summary.domain.models import Transcript, TranscriptSegment
from backend.video_summary.infrastructure import local_asr_cli
from backend.video_summary.infrastructure.faster_whisper_models import FasterWhisperModelManager
from backend.video_summary.infrastructure.huggingface_model_downloader import write_downloaded_model_manifest


def test_packaged_local_asr_cli_uses_app_root_model_and_returns_segments(
    tmp_path: Path, monkeypatch, capsys,
) -> None:
    model_dir = tmp_path / "data" / "models" / "faster-whisper" / "large-v3-turbo"
    model_dir.mkdir(parents=True)
    (model_dir / "model.bin").write_bytes(b"model")
    (model_dir / "config.json").write_text("{}", encoding="utf-8")
    manager = FasterWhisperModelManager(model_dir.parent)
    write_downloaded_model_manifest(model_dir, manager.download_spec("large-v3-turbo"))
    audio = tmp_path / "authorized.wav"
    audio.write_bytes(b"wav")
    observed: dict[str, object] = {}

    class FakeTranscriber:
        def __init__(self, model_size, device, compute_type, transcription_mode, language):
            observed.update({
                "model_size": model_size,
                "device": device,
                "compute_type": compute_type,
                "mode": transcription_mode,
                "language": language,
            })

        def transcribe(self, audio_path, output_stem):
            observed["audio_path"] = audio_path
            observed["output_stem"] = output_stem
            return Transcript(language="zh", segments=[
                TranscriptSegment(start_seconds=0.0, end_seconds=1.25, text="真实片段。")
            ])

    monkeypatch.setenv("CHRIPTMAS_APP_ROOT", str(tmp_path))
    monkeypatch.setattr(local_asr_cli, "FasterWhisperTranscriber", FakeTranscriber)
    result = local_asr_cli.main([
        "--audio", str(audio), "--model", "large-v3-turbo", "--mode", "balanced"
    ])
    payload = json.loads(capsys.readouterr().out)

    assert result == 0
    assert Path(str(observed["model_size"])).is_relative_to(tmp_path)
    assert observed["audio_path"] == audio
    assert payload["language"] == "zh"
    assert payload["segments"][0]["start_seconds"] == 0.0


def test_packaged_local_asr_cli_rejects_missing_model(tmp_path: Path, monkeypatch, capsys) -> None:
    audio = tmp_path / "authorized.wav"
    audio.write_bytes(b"wav")
    monkeypatch.setenv("CHRIPTMAS_APP_ROOT", str(tmp_path))

    result = local_asr_cli.main(["--audio", str(audio), "--model", "large-v3-turbo"])
    captured = capsys.readouterr()

    assert result == 2
    assert captured.out == ""
    assert "missing or incomplete" in captured.err


def test_model_manager_rejects_zero_byte_required_files(tmp_path: Path) -> None:
    manager = FasterWhisperModelManager(tmp_path / "data" / "models" / "faster-whisper")
    model_dir = manager.resolve_model_dir("large-v3-turbo")
    model_dir.mkdir(parents=True)
    (model_dir / "model.bin").write_bytes(b"")
    (model_dir / "config.json").write_text("{}", encoding="utf-8")

    assert manager.is_downloaded("large-v3-turbo") is False
