from __future__ import annotations

import subprocess
import json
import os
import sys
import tempfile
import time
import psutil
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .ports import ObjectStorePort

from .local_asr_defaults import (
    DEFAULT_ASR_MODEL_NAME,
    DEFAULT_ASR_MODEL_PROFILE,
    DEFAULT_ASR_TIMEOUT_SECONDS,
    LOCAL_ASR_PROVIDER_NAME,
)
from .media_processing_queue import MediaTranscriptionAdapterResult
from .audio_asset_transcriber import BUILTIN_FASTER_WHISPER_COMMAND


class LocalAsrProviderError(ValueError):
    """Raised when a local ASR provider cannot read an authorized audio reference."""


@dataclass(frozen=True, slots=True)
class EphemeralLocalAsrResult:
    text: str
    language: str | None
    provider_name: str


def transcribe_ephemeral_local_audio(
    audio_path: Path,
    *,
    command: Sequence[str],
    provider_name: str,
    model_profile: str,
    model_name: str,
    timeout_seconds: float,
    cancelled: Callable[[], bool] | None = None,
) -> EphemeralLocalAsrResult:
    """Run the existing local ASR authority without creating a Source or output record."""
    path = Path(audio_path).resolve(strict=False)
    if not path.is_file() or path.is_symlink() or path.stat().st_size < 1:
        raise LocalAsrProviderError("ephemeral audio file is unavailable")
    if timeout_seconds <= 0:
        raise LocalAsrProviderError("local ASR provider timeout must be positive")
    argv = _command_for_audio(command, path, model_profile=model_profile, model_name=model_name)
    completed = _run_cancellable_command(argv, timeout_seconds, cancelled=cancelled)
    if completed.returncode != 0:
        # Ephemeral companion audio has a stricter privacy contract than Library
        # transcription. Provider output may echo command, model, or audio paths.
        raise LocalAsrProviderError("ephemeral_asr_failed")
    text, language, _segments = _transcript_output(completed.stdout)
    if not text:
        raise LocalAsrProviderError("local ASR provider returned empty transcript")
    return EphemeralLocalAsrResult(text=text, language=language, provider_name=_required_str({"provider": provider_name}, "provider"))


@dataclass(frozen=True, slots=True)
class LocalCommandAudioTranscriptionAdapter:
    """Audio transcription adapter backed by an explicitly configured local command.

    The command may be a local ASR executable such as:
    ("faster-whisper", "{audio_path}")
    """

    object_store: ObjectStorePort
    command: Sequence[str]
    enabled: bool = False
    provider_name: str = LOCAL_ASR_PROVIDER_NAME
    timeout_seconds: float = DEFAULT_ASR_TIMEOUT_SECONDS
    model_profile: str = DEFAULT_ASR_MODEL_PROFILE
    model_name: str = DEFAULT_ASR_MODEL_NAME

    def transcribe(
        self,
        *,
        source: Mapping[str, object],
        job: Mapping[str, object],
    ) -> MediaTranscriptionAdapterResult:
        if self.enabled is not True:
            raise LocalAsrProviderError("local ASR provider is disabled")
        if self.timeout_seconds <= 0:
            raise LocalAsrProviderError("local ASR provider timeout must be positive")
        if source.get("type") != "audio":
            raise LocalAsrProviderError("local ASR provider requires audio Source")
        if job.get("required_capability") != "audio_transcription":
            raise LocalAsrProviderError("local ASR provider requires audio_transcription job")
        audio_path, authorization = self._authorized_audio_path(source)
        command = _command_for_audio(
            self.command,
            audio_path,
            model_profile=self.model_profile,
            model_name=self.model_name,
        )
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="strict",
                env=_local_asr_subprocess_env(),
                timeout=self.timeout_seconds,
            )
        except FileNotFoundError as exc:
            raise LocalAsrProviderError("local ASR provider executable not found") from exc
        except subprocess.TimeoutExpired as exc:
            raise LocalAsrProviderError("local ASR provider timed out") from exc
        except UnicodeDecodeError as exc:
            raise LocalAsrProviderError("local ASR provider output is not UTF-8") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "local ASR provider failed").strip()
            raise LocalAsrProviderError(_preview_error(detail))
        text, language, segments = _transcript_output(completed.stdout)
        if not text:
            raise LocalAsrProviderError("local ASR provider returned empty transcript")
        return MediaTranscriptionAdapterResult(
            text=text,
            provider=self.provider_name,
            language=language,
            confidence=None,
            metadata={
                "local_processing": True,
                "remote_processing": False,
                "audio_reference": _required_str(authorization, "audio_reference"),
                "authorization_id": _required_str(authorization, "id"),
                "path_stored_in_output": False,
                "model_profile": self.model_profile,
                "model_name": self.model_name,
                "segments": segments,
            },
        )

    def _authorized_audio_path(self, source: Mapping[str, object]) -> tuple[Path, Mapping[str, object]]:
        source_id = _required_str(source, "id")
        metadata = source.get("metadata")
        if not isinstance(metadata, Mapping):
            raise LocalAsrProviderError("audio Source metadata is required")
        audio_reference = _required_str(metadata, "audio_reference")
        authorization_id = None
        audio_authorization = metadata.get("audio_authorization")
        if isinstance(audio_authorization, Mapping):
            authorization_id = _optional_str(audio_authorization.get("authorization_id"))
        authorization = (
            self.object_store.read("authorized_file_refs", authorization_id)
            if authorization_id is not None
            else None
        )
        if authorization is None:
            authorization = _find_authorization(
                self.object_store.list("authorized_file_refs"),
                source_id=source_id,
                audio_reference=audio_reference,
            )
        if authorization is None:
            raise LocalAsrProviderError("authorized audio reference not found")
        if authorization.get("status") != "authorized":
            raise LocalAsrProviderError("authorized audio reference is not authorized")
        if authorization.get("source_id") != source_id or authorization.get("audio_reference") != audio_reference:
            raise LocalAsrProviderError("authorized audio reference does not match source")
        path = Path(_required_str(authorization, "path")).expanduser().resolve(strict=False)
        if not path.exists():
            raise LocalAsrProviderError("authorized audio path does not exist")
        if not path.is_file():
            raise LocalAsrProviderError("authorized audio path is not a file")
        return path, authorization


def _find_authorization(
    records: Sequence[Mapping[str, object]],
    *,
    source_id: str,
    audio_reference: str,
) -> Mapping[str, object] | None:
    for record in records:
        if record.get("source_id") == source_id and record.get("audio_reference") == audio_reference:
            return dict(record)
    return None


def _command_for_audio(
    command: Sequence[str],
    audio_path: Path,
    *,
    model_profile: str,
    model_name: str,
) -> tuple[str, ...]:
    if not isinstance(command, Sequence) or isinstance(command, (str, bytes)) or not command:
        raise LocalAsrProviderError("local ASR provider command is not configured")
    if tuple(command) == (BUILTIN_FASTER_WHISPER_COMMAND,):
        return (
            sys.executable,
            "-m",
            "backend.video_summary.infrastructure.local_asr_cli",
            "--audio",
            str(audio_path),
            "--model",
            model_name,
            "--mode",
            "balanced",
            "--language",
            "zh",
        )
    replaced: list[str] = []
    has_placeholder = False
    for part in command:
        if not isinstance(part, str) or not part:
            raise LocalAsrProviderError("local ASR provider command parts must be non-empty strings")
        if "{audio_path}" in part:
            has_placeholder = True
        replaced.append(
            part
            .replace("{audio_path}", str(audio_path))
            .replace("{model_profile}", model_profile)
            .replace("{model_name}", model_name)
        )
    if not has_placeholder:
        replaced.append(str(audio_path))
    return tuple(replaced)


def _transcript_output(stdout: str) -> tuple[str, str | None, list[dict[str, object]]]:
    clean = stdout.strip()
    if not clean:
        return "", None, []
    try:
        payload = json.loads(clean)
    except json.JSONDecodeError:
        return clean, None, []
    if not isinstance(payload, Mapping) or not isinstance(payload.get("segments"), list):
        raise LocalAsrProviderError("local ASR provider returned invalid transcript JSON")
    segments: list[dict[str, object]] = []
    for item in payload["segments"]:
        if not isinstance(item, Mapping):
            raise LocalAsrProviderError("local ASR provider returned invalid transcript segment")
        text = _optional_str(item.get("text"))
        if text:
            segments.append(dict(item))
    text = "\n".join(str(item["text"]).strip() for item in segments)
    language = _optional_str(payload.get("language"))
    return text, language, segments


def _run_cancellable_command(
    command: Sequence[str],
    timeout_seconds: float,
    *,
    cancelled: Callable[[], bool] | None,
) -> subprocess.CompletedProcess[str]:
    with (
        tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="strict") as stdout_file,
        tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="strict") as stderr_file,
    ):
        try:
            process = subprocess.Popen(
                list(command),
                text=True,
                encoding="utf-8",
                errors="strict",
                env=_local_asr_subprocess_env(),
                stdout=stdout_file,
                stderr=stderr_file,
                creationflags=(subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == "nt" else 0,
                start_new_session=os.name != "nt",
            )
        except FileNotFoundError as exc:
            raise LocalAsrProviderError("local ASR provider executable not found") from exc
        deadline = time.monotonic() + timeout_seconds
        while process.poll() is None:
            if cancelled is not None and cancelled():
                _terminate_process(process)
                raise LocalAsrProviderError("local ASR transcription cancelled")
            if time.monotonic() >= deadline:
                _terminate_process(process)
                raise LocalAsrProviderError("local ASR provider timed out")
            time.sleep(0.05)
        stdout_file.seek(0)
        stderr_file.seek(0)
        try:
            stdout = stdout_file.read()
            stderr = stderr_file.read()
        except UnicodeDecodeError as exc:
            raise LocalAsrProviderError("local ASR provider output is not UTF-8") from exc
        return subprocess.CompletedProcess(list(command), process.returncode, stdout, stderr)


def _local_asr_subprocess_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def _terminate_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        parent = psutil.Process(process.pid)
        children = parent.children(recursive=True)
    except (psutil.Error, OSError):
        children = []
        parent = None
    targets = [*children, *([parent] if parent is not None else [])]
    for target in reversed(targets):
        try:
            target.terminate()
        except (psutil.Error, OSError):
            pass
    _gone, alive = psutil.wait_procs(targets, timeout=2.0) if targets else ([], [])
    for target in alive:
        try:
            target.kill()
        except (psutil.Error, OSError):
            pass
    if alive:
        psutil.wait_procs(alive, timeout=2.0)
    if process.poll() is None:
        process.kill()
    try:
        process.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        pass


def _preview_error(text: str, limit: int = 240) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return f"{compact[: limit - 1]}..."


def _required_str(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise LocalAsrProviderError(f"{key} is required")
    return value


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
