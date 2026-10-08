from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def split_local_audio_chunk(source_path: str, output_path: str, start: float, end: float) -> str:
    """Create one bounded 16 kHz mono PCM WAV chunk with the bundled FFmpeg."""

    runtime_root = Path(sys.executable).resolve().parent
    executable_name = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
    bundled = runtime_root / "Library" / "bin" / executable_name
    executable = str(bundled) if bundled.is_file() else shutil.which(executable_name)
    if not executable:
        raise RuntimeError("bundled FFmpeg is unavailable for long audio ASR")
    source = Path(source_path).expanduser().resolve(strict=True)
    output = Path(output_path).expanduser().resolve(strict=False)
    partial = output.with_name(f".{output.name}.partial")
    output.parent.mkdir(parents=True, exist_ok=True)
    partial.unlink(missing_ok=True)
    duration = float(end) - float(start)
    if duration <= 0:
        raise ValueError("audio chunk duration must be positive")
    completed = subprocess.run(
        [
            executable, "-hide_banner", "-loglevel", "error", "-ss", str(float(start)),
            "-t", str(duration), "-i", str(source), "-vn", "-ac", "1", "-ar", "16000",
            "-c:a", "pcm_s16le", "-f", "wav", "-y", str(partial),
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=max(120.0, duration / 2.0),
    )
    if completed.returncode != 0 or not partial.is_file() or partial.stat().st_size == 0:
        partial.unlink(missing_ok=True)
        detail = " ".join((completed.stderr or "audio chunk extraction failed").split())[:240]
        raise RuntimeError(detail or "audio chunk extraction failed")
    os.replace(partial, output)
    return str(output)
