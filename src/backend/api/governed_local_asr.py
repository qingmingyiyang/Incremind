"""Fixed-command local ASR sidecar adapter for governed Media Hands staging."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from typing import Protocol

from backend.video_intake.structured import build_chunks
from backend.video_summary.infrastructure.faster_whisper_models import (
    FASTER_WHISPER_MODEL_SOURCES,
    FasterWhisperModelManager,
)


_CHILD_ENV_ALLOWLIST = frozenset({
    "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP",
    "LANG", "LC_ALL",
})


class GovernedLocalAsrError(ValueError):
    """Bounded local ASR failure without command output or filesystem paths."""


class AudioDurationProbePort(Protocol):
    def duration_ms(
        self, audio_path: Path, *, max_wall_ms: int | None = None,
        control_check: Callable[[], None] | None = None,
    ) -> int: ...


CommandRunner = Callable[
    [Sequence[str], Mapping[str, str], float, Callable[[], None] | None],
    subprocess.CompletedProcess[str],
]


@dataclass(frozen=True, slots=True)
class GovernedLocalAsrOutcome:
    transcript: Mapping[str, object]
    chunks: tuple[Mapping[str, object], ...]
    audio_duration_ms: int
    wall_ms: int
    provider_id: str
    provider_revision: str


@dataclass(frozen=True, slots=True)
class FfprobeAudioDurationProbe:
    ffprobe_path: Path
    command_runner: CommandRunner | None = None
    timeout_seconds: float = 30.0

    def duration_ms(
        self, audio_path: Path, *, max_wall_ms: int | None = None,
        control_check: Callable[[], None] | None = None,
    ) -> int:
        executable = Path(self.ffprobe_path).resolve(strict=False)
        if not executable.is_file() or self.timeout_seconds <= 0:
            raise GovernedLocalAsrError("ffprobe_unavailable")
        argv = (
            str(executable),
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "json",
            str(Path(audio_path).resolve(strict=False)),
        )
        if max_wall_ms is not None and (
            not isinstance(max_wall_ms, int) or isinstance(max_wall_ms, bool) or max_wall_ms < 1
        ):
            raise GovernedLocalAsrError("asr_budget_exhausted")
        timeout_seconds = min(
            self.timeout_seconds,
            max_wall_ms / 1000 if max_wall_ms is not None else self.timeout_seconds,
        )
        completed = (self.command_runner or _run_fixed_command)(argv, {}, timeout_seconds, control_check)
        if completed.returncode != 0:
            raise GovernedLocalAsrError("ffprobe_failed")
        try:
            payload = json.loads(completed.stdout)
            value = payload["format"]["duration"]
            duration_ms = int(float(value) * 1000)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise GovernedLocalAsrError("ffprobe_output_invalid") from error
        if duration_ms < 1:
            raise GovernedLocalAsrError("ffprobe_output_invalid")
        return duration_ms


@dataclass(frozen=True, slots=True)
class GovernedLocalAsrRunner:
    app_root: Path
    staging_root: Path
    model_name: str
    duration_probe: AudioDurationProbePort
    command_runner: CommandRunner | None = None
    monotonic: Callable[[], float] = time.monotonic

    provider_id = "local-faster-whisper"

    @property
    def provider_revision(self) -> str:
        source = FASTER_WHISPER_MODEL_SOURCES.get(self.model_name)
        if source is None:
            return "local-faster-whisper-cli-r2:unregistered"
        return f"local-faster-whisper-cli-r2:{self.model_name}:{source[1]}"

    def assert_ready(self) -> None:
        _assert_model_ready(Path(self.app_root).resolve(strict=False), self.model_name)

    def transcribe(
        self,
        audio_path: Path,
        *,
        title: str,
        max_audio_ms: int,
        max_wall_ms: int,
        control_check: Callable[[], None] | None = None,
    ) -> GovernedLocalAsrOutcome:
        duration_ms = self.probe_duration(
            audio_path, max_audio_ms=max_audio_ms, control_check=control_check
        )
        return self.transcribe_known_duration(
            audio_path, title=title, duration_ms=duration_ms,
            max_wall_ms=max_wall_ms, control_check=control_check,
        )

    def probe_duration(
        self, audio_path: Path, *, max_audio_ms: int,
        max_wall_ms: int | None = None,
        control_check: Callable[[], None] | None = None,
    ) -> int:
        path = self._staged_audio(audio_path)
        if max_audio_ms < 1:
            raise GovernedLocalAsrError("asr_budget_exhausted")
        if control_check is not None:
            control_check()
        if max_wall_ms is None:
            duration_ms = self.duration_probe.duration_ms(path, control_check=control_check)
        else:
            duration_ms = self.duration_probe.duration_ms(
                path, max_wall_ms=max_wall_ms, control_check=control_check
            )
        if duration_ms < 1 or duration_ms > max_audio_ms:
            raise GovernedLocalAsrError("asr_audio_budget_exceeded")
        return duration_ms

    def transcribe_known_duration(
        self, audio_path: Path, *, title: str, duration_ms: int, max_wall_ms: int,
        control_check: Callable[[], None] | None = None,
    ) -> GovernedLocalAsrOutcome:
        path = self._staged_audio(audio_path)
        app_root = Path(self.app_root).resolve(strict=False)
        if duration_ms < 1 or max_wall_ms < 1:
            raise GovernedLocalAsrError("asr_budget_exhausted")
        self.assert_ready()
        if control_check is not None:
            control_check()
        argv = (
            sys.executable,
            "-m",
            "backend.video_summary.infrastructure.local_asr_cli",
            "--audio",
            str(path),
            "--model",
            self.model_name,
            "--mode",
            "balanced",
            "--language",
            "zh",
        )
        start = self.monotonic()
        from backend.shared.server_resources import RESOURCE_POOL
        from backend.api.server_asr_runner import run_shared_asr
        runner = self.command_runner or (
            (lambda args, env, timeout, check: run_shared_asr(args, app_root, timeout, check))
            if RESOURCE_POOL.get() is not None else _run_fixed_command)
        completed = runner(
            argv,
            {"CHRIPTMAS_APP_ROOT": str(app_root)},
            max_wall_ms / 1000,
            control_check,
        )
        wall_ms = max(0, int((self.monotonic() - start) * 1000))
        if wall_ms > max_wall_ms:
            raise GovernedLocalAsrError("asr_wall_budget_exceeded")
        if completed.returncode != 0:
            raise GovernedLocalAsrError("local_asr_failed")
        # Recheck the governed model after the child exits. This closes
        # accidental drift during execution; OS-level local-admin tampering is
        # outside this manifest's trust boundary and remains a release ACL Gate.
        self.assert_ready()
        transcript = _parse_transcript(completed.stdout, title=title, duration_ms=duration_ms)
        chunks = tuple(build_chunks(transcript, source_type="local_asr"))
        if not chunks:
            raise GovernedLocalAsrError("local_asr_empty")
        if control_check is not None:
            control_check()
        return GovernedLocalAsrOutcome(
            transcript=transcript,
            chunks=chunks,
            audio_duration_ms=duration_ms,
            wall_ms=wall_ms,
            provider_id=self.provider_id,
            provider_revision=self.provider_revision,
        )

    def _staged_audio(self, audio_path: Path) -> Path:
        path = Path(audio_path).resolve(strict=False)
        root = Path(self.staging_root).resolve(strict=False)
        if (
            not path.is_relative_to(root)
            or not path.is_file()
            or path.is_symlink()
            or path.stat().st_size < 1
        ):
            raise GovernedLocalAsrError("asr_audio_outside_governed_staging")
        return path


def _assert_model_ready(app_root: Path, model_name: str) -> None:
    if not isinstance(model_name, str) or not model_name or any(
        character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
        for character in model_name
    ):
        raise GovernedLocalAsrError("local_asr_model_invalid")
    root = (app_root / "data" / "models" / "faster-whisper").resolve(strict=False)
    from backend.shared.server_resources import RESOURCE_POOL
    pool = RESOURCE_POOL.get()
    if pool is not None:
        root = pool.model_path('faster-whisper')
    manager = FasterWhisperModelManager(root)
    if not manager.is_supported(model_name) or not manager.is_downloaded(model_name):
        raise GovernedLocalAsrError("local_asr_model_unavailable")


def _parse_transcript(raw: str, *, title: str, duration_ms: int) -> dict[str, object]:
    if not isinstance(raw, str) or len(raw.encode("utf-8", errors="strict")) > 8 * 1024 * 1024:
        raise GovernedLocalAsrError("local_asr_output_invalid")
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError, UnicodeEncodeError) as error:
        raise GovernedLocalAsrError("local_asr_output_invalid") from error
    values = payload.get("segments") if isinstance(payload, Mapping) else None
    language = payload.get("language") if isinstance(payload, Mapping) else None
    if not isinstance(values, list) or not values or len(values) > 100_000:
        raise GovernedLocalAsrError("local_asr_output_invalid")
    segments: list[dict[str, object]] = []
    previous = -1.0
    for value in values:
        if not isinstance(value, Mapping):
            raise GovernedLocalAsrError("local_asr_output_invalid")
        start = value.get("start_seconds")
        end = value.get("end_seconds")
        text = value.get("text")
        if (
            not isinstance(start, (int, float))
            or isinstance(start, bool)
            or not isinstance(end, (int, float))
            or isinstance(end, bool)
            or not isinstance(text, str)
            or not text.strip()
            or float(start) < previous
            or float(start) < 0
            or float(end) <= float(start)
        ):
            raise GovernedLocalAsrError("local_asr_output_invalid")
        segments.append(
            {
                "start_seconds": float(start),
                "end_seconds": float(end),
                "text": " ".join(text.split())[:4000],
            }
        )
        previous = float(start)
    return {
        "title": " ".join(title.split())[:300] or "本地转写",
        "language": language.strip() if isinstance(language, str) and language.strip() else "unknown",
        "duration_seconds": duration_ms / 1000,
        "source": "local_asr",
        "segments": segments,
    }


def _run_fixed_command(
    argv: Sequence[str],
    environment: Mapping[str, str],
    timeout_seconds: float,
    control_check: Callable[[], None] | None,
) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryFile(mode="w+b") as stdout_file, tempfile.TemporaryFile(
        mode="w+b"
    ) as stderr_file:
        child_env = {
            key: value for key, value in os.environ.items()
            if key.upper() in _CHILD_ENV_ALLOWLIST
        }
        child_env.update(environment)
        child_env["PYTHONIOENCODING"] = "utf-8"
        child_env["PYTHONUTF8"] = "1"
        try:
            process = subprocess.Popen(
                list(argv),
                stdout=stdout_file,
                stderr=stderr_file,
                env=child_env,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except OSError as error:
            raise GovernedLocalAsrError("local_asr_process_unavailable") from error
        deadline = time.monotonic() + timeout_seconds
        try:
            while process.poll() is None:
                if control_check is not None:
                    control_check()
                if time.monotonic() >= deadline:
                    raise GovernedLocalAsrError("local_asr_timed_out")
                time.sleep(0.1)
        except BaseException:
            _terminate(process)
            raise
        stdout_file.seek(0)
        stderr_file.seek(0)
        try:
            stdout_bytes = stdout_file.read(8 * 1024 * 1024 + 1)
            if len(stdout_bytes) > 8 * 1024 * 1024:
                raise GovernedLocalAsrError("local_asr_output_invalid")
            stdout = stdout_bytes.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise GovernedLocalAsrError("local_asr_output_invalid") from error
        return subprocess.CompletedProcess(
            list(argv),
            process.returncode,
            stdout,
            stderr_file.read().decode("utf-8", errors="replace")[:240],
        )


def _terminate(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
