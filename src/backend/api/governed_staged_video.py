"""Fixed, bounded ffmpeg derivatives for an already-authorized staged MP4.

This module deliberately knows neither Source nor Job authorities.  A caller
may name a Job only to select its existing staging directory; it cannot supply
an executable argument, output path, filter, or arbitrary ffmpeg option.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
import os
import shutil
import subprocess
import time

from core.job_runner import JobStepBlockedError
from core.job_runner.media_execution_receipt import media_job_uri_segment


_CHILD_ENV_ALLOWLIST = frozenset({
    "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP",
    "LANG", "LC_ALL",
})
_VIDEO_MEDIA_TYPE = "video/mp4"
_AUDIO_MEDIA_TYPE = "audio/wav"
_FRAME_MEDIA_TYPE = "image/jpeg"


class GovernedStagedVideoError(ValueError):
    """Stable video-derivative failure which never exposes local process data."""


CommandRunner = Callable[
    [Sequence[str], Mapping[str, str], float, Callable[[], None] | None],
    subprocess.CompletedProcess[str],
]


@dataclass(frozen=True, slots=True)
class GovernedStagedVideoDerivative:
    kind: str
    ordinal: int
    media_type: str
    staged_path: str
    byte_count: int


@dataclass(frozen=True, slots=True)
class GovernedStagedVideoOutcome:
    audio: GovernedStagedVideoDerivative
    frames: tuple[GovernedStagedVideoDerivative, ...]
    wall_ms: int

    @property
    def audio_bytes(self) -> int:
        return self.audio.byte_count

    @property
    def frame_bytes(self) -> int:
        return sum(item.byte_count for item in self.frames)


@dataclass(frozen=True, slots=True)
class GovernedStagedVideoRunner:
    """Use one fixed ffmpeg recipe against a non-symlink staged MP4 only."""

    staging_root: Path
    ffmpeg_executable: str = "ffmpeg"
    max_frames: int = 12
    frame_fps: str = "1/5"
    max_output_bytes: int = 512 * 1024 * 1024
    default_timeout_seconds: float = 120.0
    command_runner: CommandRunner | None = None
    monotonic: Callable[[], float] = time.monotonic

    def __post_init__(self) -> None:
        if not isinstance(self.ffmpeg_executable, str) or not self.ffmpeg_executable or any(
            character in self.ffmpeg_executable for character in "\r\n\x00"
        ):
            raise ValueError("ffmpeg executable is invalid")
        if not isinstance(self.max_frames, int) or isinstance(self.max_frames, bool) or not 1 <= self.max_frames <= 120:
            raise ValueError("video derivative frame limit is invalid")
        if self.frame_fps not in {"1/1", "1/2", "1/5", "1/10"}:
            raise ValueError("video derivative frame cadence is invalid")
        if not isinstance(self.max_output_bytes, int) or isinstance(self.max_output_bytes, bool) or self.max_output_bytes < 1:
            raise ValueError("video derivative output budget is invalid")
        if self.default_timeout_seconds <= 0:
            raise ValueError("video derivative timeout is invalid")

    @property
    def provider_revision(self) -> str:
        return f"fixed-ffmpeg-audio-frames-{self.frame_fps.replace('/', '-')}-{self.max_frames}-r1"

    def assert_ready(self) -> None:
        candidate = Path(self.ffmpeg_executable)
        if candidate.is_absolute():
            if not candidate.is_file():
                raise GovernedStagedVideoError("video_derivative_executable_not_found")
        elif shutil.which(self.ffmpeg_executable) is None:
            raise GovernedStagedVideoError("video_derivative_executable_not_found")

    def derive(
        self,
        staged_video: Path,
        *,
        job_id: str,
        media_type: str,
        remaining_wall_ms: int,
        remaining_media_cpu_ms: int | None = None,
        asset_key: str | None = None,
        control_check: Callable[[], None] | None = None,
    ) -> GovernedStagedVideoOutcome:
        """Extract fixed 16kHz mono WAV and fixed-cadence JPEG frames."""

        video = self._staged_video(staged_video, media_type)
        budget_ms = self._execution_budget(remaining_wall_ms, remaining_media_cpu_ms)
        self._safe_checkpoint(control_check)
        directory = self._output_directory(job_id, asset_key=asset_key)
        directory.mkdir(parents=True, exist_ok=True)
        if directory.is_symlink():
            raise GovernedStagedVideoError("video_derivative_path_not_governed")
        audio_path = directory / "audio-16k-mono.wav"
        frame_pattern = directory / "frame-%03d.jpg"
        self._remove_prior_outputs(audio_path, directory)
        argv = self._argv(video, audio_path, frame_pattern)
        timeout = min(self.default_timeout_seconds, budget_ms / 1000)
        started = self.monotonic()
        try:
            completed = (self.command_runner or _run_governed_ffmpeg)(
                argv, {}, timeout, lambda: self._safe_checkpoint(control_check)
            )
        except GovernedStagedVideoError:
            self._remove_prior_outputs(audio_path, directory)
            raise
        except FileNotFoundError as error:
            self._remove_prior_outputs(audio_path, directory)
            raise GovernedStagedVideoError("video_derivative_executable_not_found") from error
        except subprocess.TimeoutExpired as error:
            self._remove_prior_outputs(audio_path, directory)
            raise GovernedStagedVideoError("video_derivative_timed_out") from error
        except OSError as error:
            self._remove_prior_outputs(audio_path, directory)
            raise GovernedStagedVideoError("video_derivative_process_unavailable") from error
        except JobStepBlockedError:
            self._remove_prior_outputs(audio_path, directory)
            raise
        except Exception as error:
            self._remove_prior_outputs(audio_path, directory)
            raise GovernedStagedVideoError("video_derivative_interrupted") from error
        wall_ms = max(0, int((self.monotonic() - started) * 1000))
        if wall_ms > budget_ms:
            self._remove_prior_outputs(audio_path, directory)
            raise GovernedStagedVideoError("video_derivative_budget_exhausted")
        if completed.returncode != 0:
            self._remove_prior_outputs(audio_path, directory)
            raise GovernedStagedVideoError("video_derivative_failed")
        try:
            outcome = self._outcome(audio_path, directory, wall_ms)
            self._safe_checkpoint(control_check)
            return outcome
        except JobStepBlockedError:
            self._remove_prior_outputs(audio_path, directory)
            raise
        except GovernedStagedVideoError:
            self._remove_prior_outputs(audio_path, directory)
            raise
        except Exception as error:
            self._remove_prior_outputs(audio_path, directory)
            raise GovernedStagedVideoError("video_derivative_interrupted") from error

    def _staged_video(self, staged_video: Path, media_type: str) -> Path:
        if media_type != _VIDEO_MEDIA_TYPE:
            raise GovernedStagedVideoError("video_derivative_media_type_unsupported")
        original = Path(staged_video)
        root = Path(self.staging_root).resolve(strict=False)
        if original.is_symlink() or not original.is_file():
            raise GovernedStagedVideoError("video_derivative_path_not_governed")
        resolved = original.resolve(strict=False)
        if not resolved.is_relative_to(root) or resolved.stat().st_size < 1:
            raise GovernedStagedVideoError("video_derivative_path_not_governed")
        return resolved

    def _output_directory(self, job_id: str, *, asset_key: str | None) -> Path:
        if not isinstance(job_id, str) or not job_id:
            raise GovernedStagedVideoError("video_derivative_job_invalid")
        root = Path(self.staging_root).resolve(strict=False) / media_job_uri_segment(job_id) / "xiaohongshu" / "derivatives"
        if asset_key is None:
            return root
        if (
            not isinstance(asset_key, str)
            or not asset_key
            or len(asset_key) > 160
            or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for character in asset_key)
        ):
            raise GovernedStagedVideoError("video_derivative_asset_invalid")
        return root / asset_key

    def _argv(self, video: Path, audio: Path, frames: Path) -> tuple[str, ...]:
        return (
            self.ffmpeg_executable, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-i", str(video),
            "-map", "0:a:0?", "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(audio),
            "-map", "0:v:0", "-an", "-vf", f"fps={self.frame_fps}", "-frames:v", str(self.max_frames),
            "-q:v", "2", str(frames),
        )

    def _outcome(self, audio_path: Path, directory: Path, wall_ms: int) -> GovernedStagedVideoOutcome:
        audio = self._checked_file(audio_path, directory, kind="audio", ordinal=0, media_type=_AUDIO_MEDIA_TYPE)
        present = tuple(directory.glob("frame-*.jpg"))
        expected = tuple(directory / f"frame-{number:03d}.jpg" for number in range(1, self.max_frames + 1))
        if not present or any(path not in expected for path in present):
            raise GovernedStagedVideoError("video_derivative_frames_invalid")
        paths = tuple(path for path in expected if path.exists() or path.is_symlink())
        if not paths:
            raise GovernedStagedVideoError("video_derivative_frames_invalid")
        frames = tuple(
            self._checked_file(path, directory, kind="frame", ordinal=index, media_type=_FRAME_MEDIA_TYPE)
            for index, path in enumerate(paths)
        )
        if audio.byte_count + sum(item.byte_count for item in frames) > self.max_output_bytes:
            raise GovernedStagedVideoError("video_derivative_output_budget_exhausted")
        return GovernedStagedVideoOutcome(audio, frames, wall_ms)

    @staticmethod
    def _checked_file(path: Path, directory: Path, *, kind: str, ordinal: int, media_type: str) -> GovernedStagedVideoDerivative:
        if path.is_symlink() or not path.is_file() or not path.resolve(strict=False).is_relative_to(directory.resolve(strict=False)):
            raise GovernedStagedVideoError("video_derivative_path_not_governed")
        size = path.stat().st_size
        if size < 1:
            raise GovernedStagedVideoError("video_derivative_output_invalid")
        return GovernedStagedVideoDerivative(kind, ordinal, media_type, str(path.resolve()), size)

    @staticmethod
    def _execution_budget(remaining_wall_ms: int, remaining_media_cpu_ms: int | None) -> int:
        if not isinstance(remaining_wall_ms, int) or isinstance(remaining_wall_ms, bool) or remaining_wall_ms < 1:
            raise GovernedStagedVideoError("video_derivative_budget_exhausted")
        if remaining_media_cpu_ms is not None and (
            not isinstance(remaining_media_cpu_ms, int) or isinstance(remaining_media_cpu_ms, bool) or remaining_media_cpu_ms < 1
        ):
            raise GovernedStagedVideoError("video_derivative_budget_exhausted")
        return min(remaining_wall_ms, remaining_media_cpu_ms or remaining_wall_ms)

    @staticmethod
    def _safe_checkpoint(control_check: Callable[[], None] | None) -> None:
        if control_check is None:
            return
        try:
            control_check()
        except JobStepBlockedError:
            raise
        except Exception as error:
            raise GovernedStagedVideoError("video_derivative_interrupted") from error

    @staticmethod
    def _remove_prior_outputs(audio: Path, directory: Path) -> None:
        audio.unlink(missing_ok=True)
        for frame in directory.glob("frame-*.jpg"):
            if frame.is_file() and not frame.is_symlink():
                frame.unlink(missing_ok=True)


def _run_governed_ffmpeg(
    argv: Sequence[str], environment: Mapping[str, str], timeout_seconds: float,
    control_check: Callable[[], None] | None,
) -> subprocess.CompletedProcess[str]:
    """Run a fixed ffmpeg argv with bounded diagnostics and child cleanup."""

    child_env = {key: value for key, value in os.environ.items() if key.upper() in _CHILD_ENV_ALLOWLIST}
    child_env.update(environment)
    try:
        process = subprocess.Popen(
            list(argv), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=child_env,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except OSError as error:
        raise GovernedStagedVideoError("video_derivative_process_unavailable") from error
    deadline = time.monotonic() + timeout_seconds
    try:
        while process.poll() is None:
            if control_check is not None:
                control_check()
            if time.monotonic() >= deadline:
                raise GovernedStagedVideoError("video_derivative_timed_out")
            time.sleep(0.05)
    except BaseException:
        _terminate(process)
        raise
    return subprocess.CompletedProcess(list(argv), process.returncode, "", "")


def _terminate(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
