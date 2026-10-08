from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import os
from pathlib import Path
from threading import RLock, local
import time
from typing import Callable, Literal, TypedDict


_LOCKS: dict[Path, RLock] = {}
_LOCKS_GUARD = RLock()
_THREAD = local()


class ModelDispatchAuthorityMeasurement(TypedDict):
    """Privacy-safe timing result for one outermost authority fence."""

    wait_ms: int
    hold_ms: int
    outcome: Literal["completed", "failed", "timed_out"]


MeasurementSink = Callable[[ModelDispatchAuthorityMeasurement], None]


@contextmanager
def model_dispatch_authority_fence(
    root_dir: Path,
    *,
    measurement_sink: MeasurementSink | None = None,
) -> Iterator[None]:
    """Linearize model-authority mutation with one Provider dispatch."""

    root = Path(root_dir).resolve()
    started_monotonic = time.monotonic()
    measurement: ModelDispatchAuthorityMeasurement | None = None
    with _LOCKS_GUARD:
        thread_lock = _LOCKS.setdefault(root, RLock())
    depths = getattr(_THREAD, "model_dispatch_depths", None)
    if depths is None:
        depths = {}
        _THREAD.model_dispatch_depths = depths
    try:
        with thread_lock:
            depth = int(depths.get(root, 0))
            depths[root] = depth + 1
            try:
                if depth:
                    yield
                else:
                    lock_path = (
                        root / "library" / "global" / "model-routes"
                        / ".dispatch-authority.lock"
                    )
                    lock_acquired = False
                    try:
                        with _interprocess_lock(lock_path):
                            lock_acquired = True
                            acquired_monotonic = time.monotonic()
                            outcome: Literal["completed", "failed", "timed_out"] = "completed"
                            try:
                                yield
                            except TimeoutError:
                                outcome = "timed_out"
                                raise
                            except BaseException:
                                outcome = "failed"
                                raise
                            finally:
                                measurement = {
                                    "wait_ms": _elapsed_ms(started_monotonic, acquired_monotonic),
                                    "hold_ms": _elapsed_ms(acquired_monotonic, time.monotonic()),
                                    "outcome": outcome,
                                }
                    except TimeoutError:
                        if not lock_acquired:
                            measurement = {
                                "wait_ms": _elapsed_ms(started_monotonic, time.monotonic()),
                                "hold_ms": 0,
                                "outcome": "timed_out",
                            }
                        raise
            finally:
                if depth:
                    depths[root] = depth
                else:
                    depths.pop(root, None)
    finally:
        # The observer runs after both locks are released. Its failure is never
        # allowed to mask a Provider failure or extend the critical section.
        if measurement is not None and measurement_sink is not None:
            try:
                measurement_sink(measurement)
            except Exception:
                pass


def _elapsed_ms(started: float, ended: float) -> int:
    return min(86_400_000, max(0, int((ended - started) * 1000)))


@contextmanager
def _interprocess_lock(path: Path, *, timeout_seconds: float = 30.0) -> Iterator[None]:
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
                    raise TimeoutError("model dispatch authority is busy") from error
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
