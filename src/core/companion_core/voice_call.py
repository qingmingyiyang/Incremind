from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
import re
import secrets
import stat
import tempfile
from threading import Lock
import time
from collections.abc import Callable

from core.product_core.local_asr_provider import (
    EphemeralLocalAsrResult,
    LocalAsrProviderError,
    transcribe_ephemeral_local_audio,
)
from core.product_core.local_asr_provider_settings import LocalAsrProviderSettings

from .errors import CompanionConflict, CompanionRepositoryError


MAX_VOICE_AUDIO_BYTES = 12 * 1024 * 1024
VOICE_GRANT_TTL_SECONDS = 120
_REQUEST_ID = re.compile(r"^voice:[a-f0-9-]{8,120}$")
_GRANT_ID = re.compile(r"^voice-grant-[a-f0-9]{48}$")
_OWNED_FILE = re.compile(r"^(?:voice-grant-[a-f0-9]{48}\.(?:webm|ogg|wav)|voice-upload-[a-f0-9]{48}\.part)$")
_SESSION_DIRECTORY = re.compile(r"^[a-f0-9]{24}$")
_MIME_SUFFIX = {"audio/webm": ".webm", "audio/ogg": ".ogg", "audio/wav": ".wav"}


@dataclass(frozen=True, slots=True)
class VoiceGrant:
    grant_id: str
    path: Path
    media_type: str
    byte_length: int
    sha256: str
    expires_at: float

    def public(self) -> dict[str, object]:
        return {"grant_id": self.grant_id, "media_type": self.media_type, "byte_length": self.byte_length}


def clean_stale_voice_grants() -> int:
    parent = (Path(tempfile.gettempdir()) / "chriptmas-companion-voice-call").resolve()
    parent.mkdir(parents=True, exist_ok=True)
    _require_plain_directory(parent)
    return _sweep_session_directories(parent)


class CompanionVoiceGrantStore:
    """Session-bound, single-use ephemeral audio grants with no Library persistence."""

    def __init__(self, *, session_id: str, now: Callable[[], float] = time.time, root: Path | None = None) -> None:
        safe_session = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:24]
        self.session_id = session_id
        self.parent_root = (Path(tempfile.gettempdir()) / "chriptmas-companion-voice-call").resolve() if root is None else Path(root).resolve().parent
        self.root = self.parent_root / safe_session if root is None else Path(root).resolve()
        self.now = now
        self._grants: dict[str, VoiceGrant] = {}
        self._lock = Lock()
        self.parent_root.mkdir(parents=True, exist_ok=True)
        _require_plain_directory(self.parent_root)
        if root is None:
            clean_stale_voice_grants()
        self.root.mkdir(parents=True, exist_ok=True)
        _require_plain_directory(self.root)
        self.cleanup(include_unknown=True)

    def create_staging(self) -> Path:
        path = self.root / f"voice-upload-{secrets.token_hex(24)}.part"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        return path

    def commit_staging(self, *, path: Path, media_type: str, expected_size: int, expected_sha256: str) -> VoiceGrant:
        candidate = Path(path).resolve()
        if candidate.parent != self.root or not re.fullmatch(r"voice-upload-[a-f0-9]{48}\.part", candidate.name):
            raise CompanionRepositoryError("voice staging path is invalid")
        if media_type not in _MIME_SUFFIX or not 1 <= expected_size <= MAX_VOICE_AUDIO_BYTES or not re.fullmatch(r"[a-f0-9]{64}", expected_sha256):
            raise CompanionRepositoryError("voice audio metadata is invalid")
        if not _is_plain_file(candidate) or candidate.stat().st_size != expected_size:
            raise CompanionRepositoryError("voice audio size is invalid")
        digest = _sha256_file(candidate)
        if digest != expected_sha256 or not _audio_magic(candidate, media_type):
            raise CompanionRepositoryError("voice audio integrity is invalid")
        grant_id = f"voice-grant-{secrets.token_hex(24)}"
        target = self.root / f"{grant_id}{_MIME_SUFFIX[media_type]}"
        os.replace(candidate, target)
        grant = VoiceGrant(grant_id, target, media_type, expected_size, digest, self.now() + VOICE_GRANT_TTL_SECONDS)
        with self._lock:
            self._grants[grant_id] = grant
        return grant

    def discard_staging(self, path: Path) -> None:
        candidate = Path(path).resolve()
        if candidate.parent == self.root and re.fullmatch(r"voice-upload-[a-f0-9]{48}\.part", candidate.name):
            candidate.unlink(missing_ok=True)

    def take(self, grant_id: object) -> VoiceGrant:
        if not isinstance(grant_id, str) or not _GRANT_ID.fullmatch(grant_id):
            raise CompanionRepositoryError("voice grant id is invalid")
        with self._lock:
            grant = self._grants.pop(grant_id, None)
        if grant is None:
            raise CompanionConflict("voice grant is missing or already consumed")
        if grant.expires_at <= self.now():
            grant.path.unlink(missing_ok=True)
            raise CompanionConflict("voice grant expired")
        if not _is_plain_file(grant.path) or grant.path.stat().st_size != grant.byte_length or _sha256_file(grant.path) != grant.sha256 or not _audio_magic(grant.path, grant.media_type):
            grant.path.unlink(missing_ok=True)
            raise CompanionConflict("voice grant changed")
        return grant

    def revoke(self, grant_id: object) -> bool:
        if not isinstance(grant_id, str):
            return False
        with self._lock:
            grant = self._grants.pop(grant_id, None)
        if grant is None:
            return False
        grant.path.unlink(missing_ok=True)
        return True

    def cleanup(self, *, include_unknown: bool = False) -> int:
        removed = 0
        current = self.now()
        with self._lock:
            expired = [key for key, value in self._grants.items() if value.expires_at <= current]
            grants = [self._grants.pop(key) for key in expired]
        for grant in grants:
            grant.path.unlink(missing_ok=True)
            removed += 1
        if include_unknown:
            for candidate in self.root.iterdir():
                if _OWNED_FILE.fullmatch(candidate.name) and _is_plain_file(candidate):
                    candidate.unlink(missing_ok=True)
                    removed += 1
        return removed

    def dispose(self) -> None:
        with self._lock:
            grants = list(self._grants.values())
            self._grants.clear()
        for grant in grants:
            grant.path.unlink(missing_ok=True)
        self.cleanup(include_unknown=True)
        try:
            self.root.rmdir()
        except OSError:
            pass


class CompanionVoiceTranscriptionService:
    def __init__(self, *, grant_store: CompanionVoiceGrantStore, settings_loader: Callable[[], LocalAsrProviderSettings]) -> None:
        self.grant_store = grant_store
        self.settings_loader = settings_loader

    def transcribe(self, *, request_id: object, grant_id: object, cancelled: Callable[[], bool] | None = None) -> dict[str, object]:
        if not isinstance(request_id, str) or not _REQUEST_ID.fullmatch(request_id):
            raise CompanionRepositoryError("voice request id is invalid")
        grant = self.grant_store.take(grant_id)
        started = time.monotonic()
        try:
            if cancelled is not None and cancelled():
                raise CompanionConflict("voice transcription cancelled")
            settings = self.settings_loader()
            if settings.enabled is not True:
                raise CompanionRepositoryError("companion_voice_asr_unavailable")
            if settings.status != "ready":
                raise CompanionRepositoryError("companion_voice_asr_unavailable")
            try:
                result: EphemeralLocalAsrResult = transcribe_ephemeral_local_audio(
                    grant.path,
                    command=settings.command,
                    provider_name=settings.provider_name,
                    model_profile=settings.model_profile,
                    model_name=settings.model_name,
                    timeout_seconds=min(120.0, settings.timeout_seconds),
                    cancelled=cancelled,
                )
            except LocalAsrProviderError as exc:
                if cancelled is not None and cancelled():
                    raise CompanionConflict("voice transcription cancelled") from exc
                raise CompanionRepositoryError("companion_voice_asr_failed") from exc
            text = result.text.strip()
            if not text or len(text) > 4_000 or any(ord(char) < 32 and char not in "\n\t" for char in text):
                raise CompanionRepositoryError("local ASR returned invalid transcript")
            return {
                "status": "completed",
                "request_id": request_id,
                "text": text,
                "language": result.language,
                "provider": "local_asr",
                "trace": {"audio_bytes": grant.byte_length, "elapsed_ms": round((time.monotonic() - started) * 1000), "remote_processing": False},
            }
        finally:
            grant.path.unlink(missing_ok=True)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _audio_magic(path: Path, media_type: str) -> bool:
    with path.open("rb") as source:
        head = source.read(12)
    return (
        (media_type == "audio/webm" and head.startswith(b"\x1aE\xdf\xa3"))
        or (media_type == "audio/ogg" and head.startswith(b"OggS"))
        or (media_type == "audio/wav" and head.startswith(b"RIFF") and head[8:12] == b"WAVE")
    )


def _is_reparse(info: os.stat_result) -> bool:
    return bool(getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _require_plain_directory(value: Path) -> None:
    info = value.lstat()
    if not stat.S_ISDIR(info.st_mode) or value.is_symlink() or _is_reparse(info):
        raise CompanionRepositoryError("voice temporary directory is unsafe")


def _is_plain_file(value: Path) -> bool:
    try:
        info = value.lstat()
        return stat.S_ISREG(info.st_mode) and not value.is_symlink() and not _is_reparse(info)
    except OSError:
        return False


def _sweep_session_directories(parent: Path) -> int:
    removed = 0
    for directory in parent.iterdir():
        if not _SESSION_DIRECTORY.fullmatch(directory.name):
            continue
        try:
            _require_plain_directory(directory)
        except (CompanionRepositoryError, OSError):
            continue
        for candidate in directory.iterdir():
            if _OWNED_FILE.fullmatch(candidate.name) and _is_plain_file(candidate):
                candidate.unlink(missing_ok=True)
                removed += 1
        try:
            directory.rmdir()
        except OSError:
            pass
    return removed
