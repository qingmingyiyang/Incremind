from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
import re
import secrets
import stat
import tempfile
from threading import Lock
import time
from collections.abc import Callable, Mapping

from .errors import CompanionConflict, CompanionRepositoryError
from .model_routes import CompanionModelRouter, compose_companion_prompt
from .repository import CompanionRepository


MAX_VISION_BYTES = 2 * 1024 * 1024
VISION_GRANT_TTL_SECONDS = 120
_ID = re.compile(r"^[a-z0-9][a-z0-9:_-]{0,127}$")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_MIME_SUFFIX = {"image/jpeg": ".jpg", "image/png": ".png"}
_OWNED_FILE = re.compile(r"^vision-grant-[a-f0-9]{48}\.(?:jpg|png)$")
_SESSION_DIRECTORY = re.compile(r"^[a-f0-9]{24}$")


def clean_stale_vision_grants() -> int:
    """Remove only owned regular grant files from prior sidecar sessions."""
    parent = (Path(tempfile.gettempdir()) / "chriptmas-companion-vision").resolve()
    parent.mkdir(parents=True, exist_ok=True)
    _require_plain_directory(parent)
    return _sweep_session_directories(parent)


@dataclass(frozen=True, slots=True)
class VisionGrant:
    grant_id: str
    path: Path
    media_type: str
    byte_length: int
    sha256: str
    expires_at: float

    def public(self) -> dict[str, object]:
        return {
            "grant_id": self.grant_id,
            "media_type": self.media_type,
            "byte_length": self.byte_length,
            "sha256": self.sha256,
        }


class CompanionVisionGrantStore:
    """Process-local, single-use grants backed by an aggressively cleaned system temp directory."""

    def __init__(self, *, session_id: str, now: Callable[[], float] = time.time, root: Path | None = None) -> None:
        safe_session = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:24]
        self.session_id = session_id
        self.parent_root = (Path(tempfile.gettempdir()) / "chriptmas-companion-vision").resolve() if root is None else Path(root).resolve().parent
        self.root = ((self.parent_root / safe_session) if root is None else Path(root).resolve())
        self.now = now
        self._grants: dict[str, VisionGrant] = {}
        self._lock = Lock()
        self.parent_root.mkdir(parents=True, exist_ok=True)
        _require_plain_directory(self.parent_root)
        if root is None:
            clean_stale_vision_grants()
        self.root.mkdir(parents=True, exist_ok=True)
        _require_plain_directory(self.root)
        self.cleanup(include_unknown=True)

    def issue(self, *, media_type: str, data: bytes, expected_sha256: str) -> VisionGrant:
        if media_type not in _MIME_SUFFIX or not 1 <= len(data) <= MAX_VISION_BYTES:
            raise CompanionRepositoryError("vision image type or size is invalid")
        digest = hashlib.sha256(data).hexdigest()
        if digest != expected_sha256 or not _image_magic(data, media_type):
            raise CompanionRepositoryError("vision image integrity is invalid")
        grant_id = f"vision-grant-{secrets.token_hex(24)}"
        target = self.root / f"{grant_id}{_MIME_SUFFIX[media_type]}"
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
        except Exception:
            target.unlink(missing_ok=True)
            raise
        grant = VisionGrant(grant_id, target, media_type, len(data), digest, self.now() + VISION_GRANT_TTL_SECONDS)
        with self._lock:
            self._grants[grant_id] = grant
        return grant

    def consume(self, grant_id: object) -> tuple[VisionGrant, bytes]:
        """Consume a grant using the metadata currently held by this authority.

        This remains the compatibility entry point for callers that do not need
        a Turn-bound metadata snapshot. New callers must inspect first and pass
        that snapshot to :meth:`consume_expected`.
        """
        try:
            return self.consume_expected(grant_id, self.inspect(grant_id))
        except CompanionConflict:
            # ``inspect`` intentionally leaves an expired grant untouched so it
            # is a pure read. The legacy consuming path still guarantees prompt
            # cleanup for every terminal outcome.
            self.revoke(grant_id)
            raise

    def inspect(self, grant_id: object) -> dict[str, object]:
        """Return public grant metadata without reading pixels or consuming it."""
        if not isinstance(grant_id, str) or not re.fullmatch(r"vision-grant-[a-f0-9]{48}", grant_id):
            raise CompanionRepositoryError("vision grant id is invalid")
        with self._lock:
            grant = self._grants.get(grant_id)
        if grant is None:
            raise CompanionConflict("vision grant is missing or already consumed")
        if grant.expires_at <= self.now():
            raise CompanionConflict("vision grant expired")
        return grant.public()

    def consume_expected(
        self,
        grant_id: object,
        expected_public: object,
    ) -> tuple[VisionGrant, bytes]:
        """Atomically consume a grant only if its inspected public basis still matches.

        A metadata mismatch consumes and removes the grant as well: a stale or
        tampered Turn must never be retried against the same pixels.
        """
        if not isinstance(grant_id, str) or not re.fullmatch(r"vision-grant-[a-f0-9]{48}", grant_id):
            raise CompanionRepositoryError("vision grant id is invalid")
        expected = _expected_public_grant(grant_id, expected_public)
        with self._lock:
            grant = self._grants.pop(grant_id, None)
        if grant is None:
            raise CompanionConflict("vision grant is missing or already consumed")
        try:
            if grant.expires_at <= self.now():
                raise CompanionConflict("vision grant expired")
            if expected != grant.public():
                raise CompanionConflict("vision grant metadata changed")
            if not _is_plain_file(grant.path):
                raise CompanionConflict("vision grant changed")
            try:
                data = grant.path.read_bytes()
            except OSError as error:
                raise CompanionConflict("vision grant changed") from error
            if len(data) != grant.byte_length or hashlib.sha256(data).hexdigest() != grant.sha256 or not _image_magic(data, grant.media_type):
                raise CompanionConflict("vision grant changed")
            return grant, data
        finally:
            grant.path.unlink(missing_ok=True)

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
            grant.path.unlink(missing_ok=True); removed += 1
        if include_unknown:
            for candidate in self.root.iterdir():
                if _OWNED_FILE.fullmatch(candidate.name) and _is_plain_file(candidate):
                    candidate.unlink(missing_ok=True); removed += 1
        return removed

    def dispose(self) -> None:
        with self._lock:
            grants = list(self._grants.values()); self._grants.clear()
        for grant in grants:
            grant.path.unlink(missing_ok=True)
        self.cleanup(include_unknown=True)


class CompanionVisionService:
    def __init__(self, repository: CompanionRepository, *, model_router: CompanionModelRouter, character_prompt_loader, grant_store: CompanionVisionGrantStore) -> None:
        self.repository = repository
        self.model_router = model_router
        self.character_prompt_loader = character_prompt_loader
        self.grant_store = grant_store

    def analyze(self, *, request_id: object, grant_id: object, question: object, confirm_egress: object, cancelled=None) -> dict[str, object]:
        if not isinstance(request_id, str) or _ID.fullmatch(request_id) is None:
            raise CompanionRepositoryError("vision request id is invalid")
        if not isinstance(question, str) or not question.strip() or len(question.strip()) > 2_000 or _CONTROL.search(question):
            raise CompanionRepositoryError("vision question is invalid")
        if confirm_egress is not True:
            raise CompanionRepositoryError("vision analysis requires explicit confirmation")
        self.repository.initialize()
        grant, image = self.grant_store.consume(grant_id)
        prompt_text, prompt_revision = self.character_prompt_loader()
        profile = self.repository.get_master_profile()
        prompt = compose_companion_prompt(
            route_key="companion.vision",
            master_profile={} if profile is None else {"nickname": profile.nickname, "oc_address": profile.oc_address, "relationship": profile.relationship},
            character_prompt=prompt_text,
            modifiers={"task": "screen_assistance", "evidence_rule": "only describe visible evidence"},
            published_context=(), short_term_messages=(),
            user_payload={"question": question.strip(), "image_bytes": grant.byte_length},
            context_epoch=1,
        )
        outcome = self.model_router.execute(
            route_key="companion.vision", prompt=prompt, request_id=request_id,
            image_grant=grant.public(), image_payload={"media_type": grant.media_type, "bytes": image}, cancelled=cancelled,
        )
        return {
            "request_id": request_id,
            "status": outcome.get("status"), "source": outcome.get("source"), "reason": outcome.get("reason"),
            "text": str(outcome.get("text") or "")[:4_000],
            "trace": {"route_key": "companion.vision", "prompt_revision": prompt_revision, "image_bytes": grant.byte_length, "usage": dict(outcome.get("trace", {}).get("usage", {}))},
        }


def _image_magic(data: bytes, media_type: str) -> bool:
    return (media_type == "image/jpeg" and data.startswith(b"\xff\xd8\xff")) or (media_type == "image/png" and data.startswith(b"\x89PNG\r\n\x1a\n"))


def _expected_public_grant(grant_id: str, value: object) -> dict[str, object]:
    """Validate the bounded public snapshot accepted from the Turn authority."""
    if not isinstance(value, Mapping):
        raise CompanionRepositoryError("vision grant public metadata is invalid")
    expected = dict(value)
    if set(expected) != {"grant_id", "media_type", "byte_length", "sha256"}:
        raise CompanionRepositoryError("vision grant public metadata is invalid")
    if (
        expected.get("grant_id") != grant_id
        or expected.get("media_type") not in _MIME_SUFFIX
        or not isinstance(expected.get("byte_length"), int)
        or isinstance(expected.get("byte_length"), bool)
        or not 1 <= expected["byte_length"] <= MAX_VISION_BYTES
        or not isinstance(expected.get("sha256"), str)
        or re.fullmatch(r"[a-f0-9]{64}", expected["sha256"]) is None
    ):
        raise CompanionRepositoryError("vision grant public metadata is invalid")
    return expected


def _is_reparse(info: os.stat_result) -> bool:
    return bool(getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _require_plain_directory(value: Path) -> None:
    info = value.lstat()
    if not stat.S_ISDIR(info.st_mode) or value.is_symlink() or _is_reparse(info):
        raise CompanionRepositoryError("vision temporary directory is unsafe")


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
                candidate.unlink(missing_ok=True); removed += 1
        try:
            directory.rmdir()
        except OSError:
            pass
    return removed
