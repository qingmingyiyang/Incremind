from __future__ import annotations

import os
import re
import tempfile
import threading
import unicodedata
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .errors import CompanionConflict, CompanionRepositoryError


_NOTE_ID = re.compile(r"^note-[a-z0-9][a-z0-9-]{0,63}$")
_NOTE_LINE = re.compile(r"^\[([^\]]+)\]\[([^\]]+)\] (.*)$")
_MAX_NOTE_LENGTH = 4_000
_MAX_MANUAL_BYTES = 2 * 1024 * 1024
_PATH_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: dict[str, threading.RLock] = {}


@dataclass(frozen=True, slots=True)
class ManualAuthority:
    path: Path
    mode: str
    seeded: bool


@dataclass(frozen=True, slots=True)
class CompanionNote:
    note_id: str
    content: str
    created_at: str


class CompanionManualService:
    def __init__(
        self,
        *,
        development: bool,
        repository_root: Path | None = None,
        resources_path: Path | None = None,
        user_data_root: Path | None = None,
    ) -> None:
        self.development = development
        self.repository_root = repository_root
        self.resources_path = resources_path
        self.user_data_root = user_data_root

    def authority(self) -> ManualAuthority:
        if self.development:
            if self.repository_root is None:
                raise CompanionRepositoryError("development manual repository root is missing")
            path = self.repository_root.absolute() / "readme.md"
            _require_regular_file(path, "development manual")
            return ManualAuthority(path=path, mode="development", seeded=False)

        if self.resources_path is None or self.user_data_root is None:
            raise CompanionRepositoryError("packaged manual roots are missing")
        seed = self.resources_path.absolute() / "manual" / "readme.md"
        target = self.user_data_root.absolute() / "companion" / "readme.md"
        _require_safe_target(target)
        if target.exists():
            _require_regular_file(target, "user manual")
            return ManualAuthority(path=target, mode="packaged", seeded=False)
        _require_regular_file(seed, "packaged manual seed")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(target, seed.read_bytes(), create_only=True)
        except OSError as exc:
            raise CompanionRepositoryError("packaged manual cannot be initialized") from exc
        return ManualAuthority(path=target, mode="packaged", seeded=True)

    def read_markdown(self) -> str:
        authority = self.authority()
        size = authority.path.stat().st_size
        if size > _MAX_MANUAL_BYTES:
            raise CompanionRepositoryError("companion manual is too large")
        try:
            return authority.path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise CompanionRepositoryError("companion manual cannot be read as UTF-8") from exc


class CompanionNotesService:
    def __init__(
        self,
        user_data_root: Path,
        *,
        now: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.root = user_data_root.absolute() / "companion"
        self.path = self.root / "notes.txt"
        self.backup_path = self.root / "notes.txt.bak"
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._id_factory = id_factory or (lambda: f"note-{uuid.uuid4().hex}")
        self._lock = _lock_for_path(self.path)

    def append(self, content: str) -> CompanionNote:
        safe_content = _normalize_note(content)
        note_id = self._id_factory()
        _require_note_id(note_id)
        created_at = _utc_iso(self._now())
        note = CompanionNote(note_id=note_id, content=safe_content, created_at=created_at)
        with self._lock:
            self._prepare()
            if any(item.note_id == note_id for item in self._read_unlocked()):
                raise CompanionConflict("note id already exists")
            line = _serialize_note(note)
            try:
                with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                    handle.write(line)
                    handle.flush()
                    os.fsync(handle.fileno())
            except OSError as exc:
                raise CompanionRepositoryError("note cannot be appended") from exc
        return note

    def list(self) -> tuple[CompanionNote, ...]:
        with self._lock:
            self._prepare()
            return self._read_unlocked()

    def edit(self, note_id: str, content: str) -> CompanionNote:
        _require_note_id(note_id)
        safe_content = _normalize_note(content)
        with self._lock:
            self._prepare()
            notes = list(self._read_unlocked())
            index = next((index for index, note in enumerate(notes) if note.note_id == note_id), None)
            if index is None:
                raise CompanionConflict("note does not exist")
            updated = CompanionNote(note_id, safe_content, notes[index].created_at)
            notes[index] = updated
            self._replace_with_backup(notes)
            return updated

    def delete(self, note_id: str) -> bool:
        _require_note_id(note_id)
        with self._lock:
            self._prepare()
            notes = list(self._read_unlocked())
            remaining = [note for note in notes if note.note_id != note_id]
            if len(remaining) == len(notes):
                return False
            self._replace_with_backup(remaining)
            return True

    def review(self, *, enabled: bool, random_index: Callable[[int], int]) -> CompanionNote | None:
        if enabled is not True:
            return None
        notes = self.list()
        if not notes:
            return None
        index = random_index(len(notes))
        if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(notes):
            raise CompanionRepositoryError("note review random source is invalid")
        return notes[index]

    def _prepare(self) -> None:
        _require_safe_target(self.path)
        self.root.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            _require_regular_file(self.path, "notes file")
            return
        try:
            _atomic_write(self.path, b"", create_only=True)
        except OSError as exc:
            raise CompanionRepositoryError("notes file cannot be initialized") from exc

    def _read_unlocked(self) -> tuple[CompanionNote, ...]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            raise CompanionRepositoryError("notes file cannot be read as UTF-8") from exc
        notes: list[CompanionNote] = []
        identities: set[str] = set()
        for line in lines:
            if not line:
                continue
            match = _NOTE_LINE.fullmatch(line)
            if match is None:
                raise CompanionRepositoryError("notes file contains an invalid line")
            created_at, note_id, encoded = match.groups()
            _require_note_id(note_id)
            _parse_utc(created_at)
            if note_id in identities:
                raise CompanionRepositoryError("notes file contains a duplicate id")
            identities.add(note_id)
            notes.append(CompanionNote(note_id, _decode_content(encoded), created_at))
        return tuple(notes)

    def _replace_with_backup(self, notes: list[CompanionNote]) -> None:
        try:
            current = self.path.read_bytes()
            _atomic_write(self.backup_path, current, create_only=False)
            payload = "".join(_serialize_note(note) for note in notes).encode("utf-8")
            _atomic_write(self.path, payload, create_only=False)
        except OSError as exc:
            raise CompanionRepositoryError("notes file cannot be replaced") from exc


def _normalize_note(content: object) -> str:
    if not isinstance(content, str):
        raise CompanionRepositoryError("note content must be text")
    normalized = unicodedata.normalize("NFC", content).strip()
    if not normalized:
        raise CompanionRepositoryError("note content is required")
    if len(normalized) > _MAX_NOTE_LENGTH:
        raise CompanionRepositoryError("note content is too long")
    for character in normalized:
        if unicodedata.category(character) == "Cc" and character not in {"\n", "\r", "\t"}:
            raise CompanionRepositoryError("note content contains a control character")
    return normalized.replace("\r\n", "\n").replace("\r", "\n")


def _serialize_note(note: CompanionNote) -> str:
    encoded = note.content.replace("\\", "\\\\").replace("\n", "\\n").replace("\t", "\\t")
    return f"[{note.created_at}][{note.note_id}] {encoded}\n"


def _decode_content(value: str) -> str:
    output: list[str] = []
    index = 0
    while index < len(value):
        if value[index] != "\\":
            output.append(value[index])
            index += 1
            continue
        if index + 1 >= len(value) or value[index + 1] not in {"\\", "n", "t"}:
            raise CompanionRepositoryError("notes file contains an invalid escape")
        marker = value[index + 1]
        output.append({"\\": "\\", "n": "\n", "t": "\t"}[marker])
        index += 2
    content = "".join(output)
    if len(content) > _MAX_NOTE_LENGTH:
        raise CompanionRepositoryError("stored note is too long")
    return content


def _atomic_write(path: Path, payload: bytes, *, create_only: bool) -> None:
    _require_safe_target(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if create_only and path.exists():
        return
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if create_only and path.exists():
            return
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _require_regular_file(path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise CompanionRepositoryError(f"{label} is missing or unsafe")
    _require_safe_parents(path)


def _require_safe_target(path: Path) -> None:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise CompanionRepositoryError("companion text target is unsafe")
    _require_safe_parents(path)


def _require_safe_parents(path: Path) -> None:
    for parent in path.parents:
        if parent.exists() and parent.is_symlink():
            raise CompanionRepositoryError("companion text path cannot traverse a symlink")


def _require_note_id(note_id: object) -> None:
    if not isinstance(note_id, str) or _NOTE_ID.fullmatch(note_id) is None:
        raise CompanionRepositoryError("note id is invalid")


def _lock_for_path(path: Path) -> threading.RLock:
    key = os.path.normcase(str(path.absolute()))
    with _PATH_LOCKS_GUARD:
        return _PATH_LOCKS.setdefault(key, threading.RLock())


def _utc_iso(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
        raise CompanionRepositoryError("note clock must return UTC")
    return value.isoformat()


def _parse_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CompanionRepositoryError("stored note timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise CompanionRepositoryError("stored note timestamp is invalid")
    return parsed
