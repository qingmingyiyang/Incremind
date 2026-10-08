from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
from threading import RLock, local
import time

from core.storage_provider import (
    SQLiteStructuredRecord,
    SQLiteStructuredRecordStore,
    SQLiteUnitOfWorkConflict,
    SQLiteUnitOfWorkError,
)


_SCOPE = "bilibili-rebuild"
_MODES = frozenset({"legacy", "hands"})
_COMMAND_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{7,127}$")
_REVISIONS = "media_ingress_selection_revisions"
_HEADS = "media_ingress_selection_heads"
_COMMANDS = "media_ingress_selection_commands"
_REQUESTS = "media_ingress_request_bindings"
_LOCKS: dict[Path, RLock] = {}
_LOCKS_GUARD = RLock()
_THREAD = local()


class MediaIngressSelectionError(ValueError):
    pass


class MediaIngressSelectionConflict(MediaIngressSelectionError):
    pass


class MediaIngressRequestConflict(MediaIngressSelectionConflict):
    pass


class LegacyMediaIngressDisabled(MediaIngressSelectionError):
    def __init__(self, selection: "MediaIngressSelectionRevision") -> None:
        super().__init__("legacy_ingress_disabled")
        self.selection = selection


class HandsMediaIngressDisabled(MediaIngressSelectionError):
    def __init__(self, selection: "MediaIngressSelectionRevision") -> None:
        super().__init__("hands_ingress_disabled")
        self.selection = selection


@dataclass(frozen=True, slots=True)
class MediaIngressSelectionRevision:
    revision: int
    mode: str
    public_ref: str
    command_id: str
    actor: str
    created_at: str
    persisted: bool = True


class MediaIngressSelectionAuthority:
    """Durable CAS authority selecting exactly one Bilibili ingress writer."""

    def __init__(self, records: SQLiteStructuredRecordStore) -> None:
        self._records = records

    @contextmanager
    def writer(self, mode: str) -> Iterator[MediaIngressSelectionRevision]:
        """Drain a cutover and bind one ingress effect to the observed selection."""

        with media_ingress_selection_fence(self._records.database_path):
            if mode == "legacy":
                yield self.require_legacy()
                return
            if mode == "hands":
                yield self.require_hands()
                return
            raise MediaIngressSelectionError("media ingress writer mode is invalid")

    def bind_request(
        self,
        *,
        operation: str,
        request_id: str,
        project_id: str,
        input_ref: str,
        selection: MediaIngressSelectionRevision,
    ) -> None:
        """Persist an immutable request-to-input binding before a Hands effect."""

        if operation not in {"resolve", "admit"}:
            raise MediaIngressSelectionError("media ingress request operation is invalid")
        if not all(
            isinstance(value, str) and value
            for value in (request_id, project_id, input_ref)
        ):
            raise MediaIngressSelectionError("media ingress request binding is invalid")
        object_id = hashlib.sha256(
            f"{operation}\0{request_id}".encode("utf-8")
        ).hexdigest()
        payload = {
            "scope": _SCOPE,
            "operation": operation,
            "request_id": request_id,
            "project_id": project_id,
            "input_ref": input_ref,
            "selection_revision": selection.revision,
            "selection_ref": selection.public_ref,
        }
        try:
            with self._records.begin() as unit:
                existing = unit.read(_REQUESTS, object_id)
                if existing is not None:
                    if existing.revision != 1 or dict(existing.payload) != payload:
                        raise MediaIngressRequestConflict(
                            "ingress request id conflicts with immutable input"
                        )
                    unit.rollback()
                    return
                unit.put(_REQUESTS, object_id, payload, expected_revision=0)
                unit.commit()
        except MediaIngressSelectionError:
            raise
        except (SQLiteUnitOfWorkConflict, SQLiteUnitOfWorkError) as error:
            raise MediaIngressRequestConflict(
                "media ingress request binding conflicted"
            ) from error

    def effective(self) -> MediaIngressSelectionRevision:
        current = self.current()
        if current is not None:
            return current
        return MediaIngressSelectionRevision(
            revision=0,
            mode="legacy",
            public_ref="crp://media-ingress/selections/bilibili-rebuild/r0",
            command_id="bootstrap-legacy",
            actor="system-default",
            created_at="1970-01-01T00:00:00Z",
            persisted=False,
        )

    def current(self) -> MediaIngressSelectionRevision | None:
        try:
            with self._records.begin() as unit:
                head = unit.read(_HEADS, _SCOPE)
                if head is None:
                    unit.rollback()
                    return None
                revision = self._head_revision(head)
                revision_id = head.payload.get("revision_id")
                record = unit.read(_REVISIONS, revision_id) if isinstance(revision_id, str) else None
                result = self._resolve_head(head, record, revision=revision)
                unit.rollback()
                return result
        except MediaIngressSelectionError:
            raise
        except SQLiteUnitOfWorkError as error:
            raise MediaIngressSelectionError("media ingress selection read failed") from error

    def require_legacy(self) -> MediaIngressSelectionRevision:
        selection = self.effective()
        if selection.mode != "legacy":
            raise LegacyMediaIngressDisabled(selection)
        return selection

    def require_hands(self) -> MediaIngressSelectionRevision:
        selection = self.effective()
        if selection.mode != "hands":
            raise HandsMediaIngressDisabled(selection)
        return selection

    def publish(
        self,
        mode: str,
        *,
        expected_revision: int,
        command_id: str,
        actor: str,
        created_at: str,
    ) -> MediaIngressSelectionRevision:
        if (
            not isinstance(mode, str)
            or mode not in _MODES
            or not isinstance(expected_revision, int)
            or isinstance(expected_revision, bool)
            or expected_revision < 0
            or not isinstance(command_id, str)
            or _COMMAND_ID.fullmatch(command_id) is None
            or not isinstance(actor, str)
            or not actor
            or not isinstance(created_at, str)
            or not created_at
        ):
            raise MediaIngressSelectionError("media ingress selection command is invalid")
        with media_ingress_selection_fence(self._records.database_path):
            return self._publish(
                mode,
                expected_revision=expected_revision,
                command_id=command_id,
                actor=actor,
                created_at=created_at,
            )

    def _publish(
        self,
        mode: str,
        *,
        expected_revision: int,
        command_id: str,
        actor: str,
        created_at: str,
    ) -> MediaIngressSelectionRevision:
        try:
            with self._records.begin() as unit:
                replay = unit.read(_COMMANDS, command_id)
                if replay is not None:
                    revision_id = replay.payload.get("revision_id")
                    record = unit.read(_REVISIONS, revision_id) if isinstance(revision_id, str) else None
                    result = self._revision_from_command(replay, record)
                    if (
                        result.mode != mode
                        or result.actor != actor
                        or expected_revision != result.revision - 1
                    ):
                        raise MediaIngressSelectionConflict(
                            "media ingress command id conflicts with immutable input"
                        )
                    unit.rollback()
                    return result
                head = unit.read(_HEADS, _SCOPE)
                current_revision = self._head_revision(head)
                if current_revision != expected_revision:
                    raise MediaIngressSelectionConflict(
                        f"expected ingress revision {expected_revision}, found {current_revision}"
                    )
                revision = current_revision + 1
                revision_id = f"{_SCOPE}~r{revision}"
                public_ref = f"crp://media-ingress/selections/{_SCOPE}/r{revision}"
                payload = {
                    "scope": _SCOPE,
                    "revision": revision,
                    "mode": mode,
                    "public_ref": public_ref,
                    "command_id": command_id,
                    "actor": actor,
                    "created_at": created_at,
                }
                unit.put(_REVISIONS, revision_id, payload, expected_revision=0)
                unit.put(
                    _HEADS,
                    _SCOPE,
                    {"revision": revision, "revision_id": revision_id, "public_ref": public_ref},
                    expected_revision=head.revision if head is not None else 0,
                )
                unit.put(
                    _COMMANDS,
                    command_id,
                    {"revision_id": revision_id, **payload},
                    expected_revision=0,
                )
                unit.commit()
        except MediaIngressSelectionError:
            raise
        except (SQLiteUnitOfWorkConflict, SQLiteUnitOfWorkError) as error:
            raise MediaIngressSelectionConflict(
                "media ingress selection publication conflicted"
            ) from error
        record = self._records.read(_REVISIONS, revision_id)
        if record is None:
            raise MediaIngressSelectionError(
                "media ingress selection revision disappeared after commit"
            )
        return self._revision_from_record(record)

    def _resolve_head(
        self,
        head: SQLiteStructuredRecord,
        record: SQLiteStructuredRecord | None,
        *,
        revision: int,
    ) -> MediaIngressSelectionRevision:
        revision_id = head.payload.get("revision_id")
        public_ref = head.payload.get("public_ref")
        if (
            revision_id != f"{_SCOPE}~r{revision}"
            or public_ref != f"crp://media-ingress/selections/{_SCOPE}/r{revision}"
            or record is None
        ):
            raise MediaIngressSelectionError("media ingress selection head drifted")
        result = self._revision_from_record(record)
        if result.public_ref != public_ref:
            raise MediaIngressSelectionError("media ingress selection head ref drifted")
        return result

    @staticmethod
    def _head_revision(head: SQLiteStructuredRecord | None) -> int:
        if head is None:
            return 0
        revision = head.payload.get("revision")
        if (
            not isinstance(revision, int)
            or isinstance(revision, bool)
            or revision < 1
            or head.revision != revision
        ):
            raise MediaIngressSelectionError("media ingress selection head revision drifted")
        return revision

    def _revision_from_command(
        self,
        command: SQLiteStructuredRecord,
        record: SQLiteStructuredRecord | None,
    ) -> MediaIngressSelectionRevision:
        revision_id = command.payload.get("revision_id")
        if (
            not isinstance(revision_id, str)
            or record is None
            or record.object_id != revision_id
            or dict(record.payload)
            != {key: value for key, value in command.payload.items() if key != "revision_id"}
        ):
            raise MediaIngressSelectionError("media ingress command evidence drifted")
        return self._revision_from_record(record)

    @staticmethod
    def _revision_from_record(record: SQLiteStructuredRecord) -> MediaIngressSelectionRevision:
        value = record.payload
        if set(value) != {
            "scope", "revision", "mode", "public_ref", "command_id", "actor", "created_at"
        }:
            raise MediaIngressSelectionError("media ingress selection fields drifted")
        revision = value.get("revision")
        mode = value.get("mode")
        public_ref = value.get("public_ref")
        command_id = value.get("command_id")
        actor = value.get("actor")
        created_at = value.get("created_at")
        if (
            value.get("scope") != _SCOPE
            or record.revision != 1
            or not isinstance(revision, int)
            or isinstance(revision, bool)
            or revision < 1
            or record.object_id != f"{_SCOPE}~r{revision}"
            or mode not in _MODES
            or public_ref != f"crp://media-ingress/selections/{_SCOPE}/r{revision}"
            or not all(isinstance(item, str) and item for item in (command_id, actor, created_at))
        ):
            raise MediaIngressSelectionError("media ingress selection identity drifted")
        return MediaIngressSelectionRevision(
            revision=revision,
            mode=str(mode),
            public_ref=str(public_ref),
            command_id=str(command_id),
            actor=str(actor),
            created_at=str(created_at),
        )


def media_ingress_selection_authority_for_root(
    root_dir: Path,
) -> MediaIngressSelectionAuthority:
    """Return the one durable Bilibili ingress selection authority for a root."""

    return MediaIngressSelectionAuthority(
        SQLiteStructuredRecordStore(Path(root_dir) / ".rebuild-data" / "jobs.sqlite3")
    )


class SelectionGatedLegacyBilibiliDownloader:
    """Thin legacy adapter: preserve the old downloader, fence each real effect."""

    def __init__(self, downloader: object, authority: MediaIngressSelectionAuthority) -> None:
        self._downloader = downloader
        self._authority = authority

    def download(self, *args: object, **kwargs: object) -> Path:
        method = getattr(self._downloader, "download", None)
        if not callable(method):
            raise MediaIngressSelectionError("legacy bilibili downloader is invalid")
        with self._authority.writer("legacy"):
            return method(*args, **kwargs)

    async def download_async(self, *args: object, **kwargs: object) -> Path:
        import asyncio

        return await asyncio.to_thread(self.download, *args, **kwargs)


def media_ingress_selection_public(
    selection: MediaIngressSelectionRevision,
) -> dict[str, object]:
    return {
        "scope": _SCOPE,
        "persisted": selection.persisted,
        "revision": selection.revision,
        "mode": selection.mode,
        "selection_ref": selection.public_ref,
        "command_id": selection.command_id,
        "actor": selection.actor,
        "created_at": selection.created_at,
    }


@contextmanager
def media_ingress_selection_fence(database_path: Path) -> Iterator[None]:
    """Linearize selection publication with one admitted ingress effect."""

    database = Path(database_path).resolve(strict=False)
    with _LOCKS_GUARD:
        thread_lock = _LOCKS.setdefault(database, RLock())
    depths = getattr(_THREAD, "media_ingress_depths", None)
    if depths is None:
        depths = {}
        _THREAD.media_ingress_depths = depths
    with thread_lock:
        depth = int(depths.get(database, 0))
        depths[database] = depth + 1
        try:
            if depth:
                yield
            else:
                with _interprocess_lock(
                    database.parent / ".media-ingress-selection.lock"
                ):
                    yield
        finally:
            if depth:
                depths[database] = depth
            else:
                depths.pop(database, None)


@contextmanager
def _interprocess_lock(
    path: Path, *, timeout_seconds: float = 30.0,
) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                _try_lock(handle)
                break
            except OSError as error:
                if time.monotonic() >= deadline:
                    raise MediaIngressSelectionConflict(
                        "media ingress cutover is busy"
                    ) from error
                time.sleep(0.02)
        try:
            yield
        finally:
            handle.seek(0)
            _unlock(handle)
    finally:
        handle.close()


def _try_lock(handle: object) -> None:
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)  # type: ignore[attr-defined]


def _unlock(handle: object) -> None:
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
