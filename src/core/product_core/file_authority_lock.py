"""Portable lock port for product-core file authorities."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
import os
from pathlib import Path
import time
from typing import Protocol


class FileAuthorityLockPort(Protocol):
    def __call__(
        self, authority_path: Path, *, timeout_seconds: float = 5.0,
    ) -> AbstractContextManager[None]: ...


@contextmanager
def interprocess_file_lock(
    authority_path: Path, *, timeout_seconds: float = 5.0,
) -> Iterator[None]:
    """Lock one authority across processes without persisting authority data."""
    lock_path = authority_path.with_name(f".{authority_path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
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
                    raise TimeoutError("file authority is busy") from error
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
