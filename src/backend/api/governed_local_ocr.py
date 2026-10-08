"""Bounded local image OCR for already-authorized Media Hands staging.

This module intentionally has no Source, Job, or authorized-file-reference
dependency.  Its caller has already made those decisions; this runner only
accepts a staged raster file and the persisted local OCR provider settings.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
import hashlib
import json
import os
import subprocess
import tempfile
import time

from core.product_core.local_ocr_provider_settings import (
    BUILTIN_WINDOWS_OCR_COMMAND,
    GetLocalOcrProviderSettings,
    LocalOcrProviderSettings,
    _runtime_command,
)
from core.job_runner import JobStepBlockedError


_RASTER_MEDIA_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})
_CHILD_ENV_ALLOWLIST = frozenset({
    "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP",
    "LANG", "LC_ALL",
})


class GovernedLocalOcrError(ValueError):
    """Stable OCR failure which never carries command output or local paths."""


CommandRunner = Callable[
    [Sequence[str], Mapping[str, str], float, Callable[[], None] | None],
    subprocess.CompletedProcess[str],
]
SettingsReader = Callable[[], LocalOcrProviderSettings]


@dataclass(frozen=True, slots=True)
class GovernedLocalOcrOutcome:
    text: str
    provider_id: str
    provider_revision: str
    wall_ms: int


@dataclass(frozen=True, slots=True)
class GovernedLocalOcrRunner:
    """Execute one explicitly enabled OCR provider against governed staging only."""

    staging_root: Path
    object_store: object | None = None
    settings_reader: SettingsReader | None = None
    command_runner: CommandRunner | None = None
    default_timeout_seconds: float = 30.0
    monotonic: Callable[[], float] = time.monotonic

    def __post_init__(self) -> None:
        if self.settings_reader is None and self.object_store is None:
            raise ValueError("local OCR settings authority is required")
        if self.default_timeout_seconds <= 0:
            raise ValueError("local OCR default timeout must be positive")

    @property
    def provider_revision(self) -> str:
        """Bind durable recipe identity to the current persisted OCR command."""

        return _provider_revision(self._settings())

    def assert_ready(self) -> None:
        settings = self._settings()
        if settings.enabled is not True:
            raise GovernedLocalOcrError("local_ocr_disabled")
        if not settings.command:
            raise GovernedLocalOcrError("local_ocr_command_not_configured")
        if settings.status != "ready" or settings.diagnostic != "ready":
            raise GovernedLocalOcrError("local_ocr_not_ready")

    def extract_text(
        self,
        staged_path: Path,
        *,
        media_type: str,
        remaining_wall_ms: int,
        remaining_media_cpu_ms: int | None = None,
        control_check: Callable[[], None] | None = None,
    ) -> GovernedLocalOcrOutcome:
        """Run configured OCR without allowing a caller to select its command."""

        path = self._staged_raster(staged_path, media_type)
        if not isinstance(remaining_wall_ms, int) or isinstance(remaining_wall_ms, bool) or remaining_wall_ms < 1:
            raise GovernedLocalOcrError("local_ocr_budget_exhausted")
        if remaining_media_cpu_ms is not None and (
            not isinstance(remaining_media_cpu_ms, int)
            or isinstance(remaining_media_cpu_ms, bool)
            or remaining_media_cpu_ms < 1
        ):
            raise GovernedLocalOcrError("local_ocr_budget_exhausted")
        self._safe_checkpoint(control_check)
        settings = self._settings()
        if settings.enabled is not True:
            raise GovernedLocalOcrError("local_ocr_disabled")
        if not settings.command:
            raise GovernedLocalOcrError("local_ocr_command_not_configured")
        command = _command_for_image(_runtime_command(settings.command), path)
        execution_budget_ms = min(
            remaining_wall_ms,
            remaining_media_cpu_ms if remaining_media_cpu_ms is not None else remaining_wall_ms,
        )
        timeout_seconds = min(self.default_timeout_seconds, execution_budget_ms / 1000)
        if timeout_seconds <= 0:
            raise GovernedLocalOcrError("local_ocr_budget_exhausted")
        started = self.monotonic()
        try:
            completed = (self.command_runner or _run_governed_command)(
                command, {}, timeout_seconds,
                lambda: self._safe_checkpoint(control_check),
            )
        except GovernedLocalOcrError:
            raise
        except FileNotFoundError as error:
            raise GovernedLocalOcrError("local_ocr_executable_not_found") from error
        except subprocess.TimeoutExpired as error:
            raise GovernedLocalOcrError("local_ocr_timed_out") from error
        except OSError as error:
            raise GovernedLocalOcrError("local_ocr_process_unavailable") from error
        except JobStepBlockedError:
            raise
        except Exception as error:
            raise GovernedLocalOcrError("local_ocr_interrupted") from error
        wall_ms = max(0, int((self.monotonic() - started) * 1000))
        if wall_ms > execution_budget_ms:
            raise GovernedLocalOcrError("local_ocr_budget_exhausted")
        if completed.returncode != 0:
            raise GovernedLocalOcrError(_stable_failure(settings.provider_name, completed.stderr, completed.stdout))
        text = _bounded_text(completed.stdout)
        if not text:
            raise GovernedLocalOcrError("local_ocr_empty")
        self._safe_checkpoint(control_check)
        return GovernedLocalOcrOutcome(
            text=text,
            provider_id="governed-local-ocr",
            provider_revision=_provider_revision(settings),
            wall_ms=wall_ms,
        )

    def _settings(self) -> LocalOcrProviderSettings:
        try:
            if self.settings_reader is not None:
                settings = self.settings_reader()
            else:
                settings = GetLocalOcrProviderSettings(self.object_store).execute()  # type: ignore[arg-type]
        except JobStepBlockedError:
            raise
        except Exception as error:
            raise GovernedLocalOcrError("local_ocr_settings_unavailable") from error
        if not isinstance(settings, LocalOcrProviderSettings):
            raise GovernedLocalOcrError("local_ocr_settings_invalid")
        return settings

    def _staged_raster(self, staged_path: Path, media_type: str) -> Path:
        if media_type not in _RASTER_MEDIA_TYPES:
            raise GovernedLocalOcrError("local_ocr_media_type_unsupported")
        original = Path(staged_path)
        root = Path(self.staging_root).resolve(strict=False)
        if original.is_symlink() or not original.is_file():
            raise GovernedLocalOcrError("local_ocr_path_not_governed")
        path = original.resolve(strict=False)
        if not path.is_relative_to(root) or path.stat().st_size < 1:
            raise GovernedLocalOcrError("local_ocr_path_not_governed")
        return path

    @staticmethod
    def _safe_checkpoint(control_check: Callable[[], None] | None) -> None:
        if control_check is None:
            return
        try:
            control_check()
        except JobStepBlockedError:
            raise
        except Exception as error:
            raise GovernedLocalOcrError("local_ocr_interrupted") from error


def _command_for_image(command: Sequence[str], image_path: Path) -> tuple[str, ...]:
    if not isinstance(command, Sequence) or isinstance(command, (str, bytes)) or not command:
        raise GovernedLocalOcrError("local_ocr_command_not_configured")
    replaced: list[str] = []
    has_placeholder = False
    for part in command:
        if not isinstance(part, str) or not part:
            raise GovernedLocalOcrError("local_ocr_command_invalid")
        if "{image_path}" in part:
            has_placeholder = True
            replaced.append(part.replace("{image_path}", str(image_path)))
        else:
            replaced.append(part)
    if not has_placeholder:
        replaced.append(str(image_path))
    return tuple(replaced)


def _provider_revision(settings: LocalOcrProviderSettings) -> str:
    payload = json.dumps(
        {"provider_name": settings.provider_name, "command": list(settings.command)},
        ensure_ascii=True, sort_keys=True, separators=(",", ":"),
    ).encode("ascii")
    return "ocr-" + hashlib.sha256(payload).hexdigest()


def _stable_failure(provider_name: str, stderr: str, stdout: str) -> str:
    if provider_name == "builtin-windows-ocr":
        detail = f"{stderr}\n{stdout}"
        if "Windows OCR returned no text" in detail:
            return "Windows OCR returned no text"
        if "Windows OCR language pack is unavailable" in detail:
            return "Windows OCR language pack is unavailable"
        if "authorized image does not exist" in detail:
            return "authorized image does not exist"
        return "Windows OCR failed to decode or recognize image"
    return "local_ocr_failed"


def _bounded_text(value: object) -> str:
    if not isinstance(value, str) or len(value.encode("utf-8", errors="strict")) > 8 * 1024 * 1024:
        raise GovernedLocalOcrError("local_ocr_output_invalid")
    return " ".join(value.split())


def _run_governed_command(
    argv: Sequence[str],
    environment: Mapping[str, str],
    timeout_seconds: float,
    control_check: Callable[[], None] | None,
) -> subprocess.CompletedProcess[str]:
    """Run a command with bounded output and cancellation-safe child cleanup."""

    with tempfile.TemporaryFile(mode="w+b") as stdout_file, tempfile.TemporaryFile(mode="w+b") as stderr_file:
        child_env = {
            key: value for key, value in os.environ.items()
            if key.upper() in _CHILD_ENV_ALLOWLIST
        }
        child_env.update(environment)
        try:
            process = subprocess.Popen(
                list(argv), stdout=stdout_file, stderr=stderr_file, env=child_env,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except OSError as error:
            raise GovernedLocalOcrError("local_ocr_process_unavailable") from error
        deadline = time.monotonic() + timeout_seconds
        try:
            while process.poll() is None:
                if control_check is not None:
                    control_check()
                if time.monotonic() >= deadline:
                    raise GovernedLocalOcrError("local_ocr_timed_out")
                time.sleep(0.05)
        except BaseException:
            _terminate(process)
            raise
        stdout_file.seek(0)
        stderr_file.seek(0)
        try:
            stdout = stdout_file.read(8 * 1024 * 1024 + 1).decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise GovernedLocalOcrError("local_ocr_output_invalid") from error
        stderr = stderr_file.read(4096).decode("utf-8", errors="replace")
        return subprocess.CompletedProcess(list(argv), process.returncode, stdout, stderr)


def _terminate(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
