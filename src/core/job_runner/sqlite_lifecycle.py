from __future__ import annotations

import time
from collections.abc import Callable
from threading import BoundedSemaphore, Event, Lock, Thread

from .sqlite_store import SQLiteJobStore


class SQLiteJobRuntimeLifecycle:
    def __init__(
        self,
        store: SQLiteJobStore,
        *,
        execution_dispatcher: Callable[[str], object],
        max_concurrency: int = 4,
        max_in_flight: int = 100,
    ) -> None:
        if not callable(execution_dispatcher):
            raise TypeError("execution_dispatcher must be callable")
        if (
            max_concurrency <= 0
            or max_in_flight < max_concurrency
        ):
            raise ValueError("lifecycle bounds are invalid")
        self._store = store
        self._max_in_flight = max_in_flight
        self._slots = BoundedSemaphore(max_concurrency)
        self._stopping = Event()
        self._lock = Lock()
        self._in_flight: set[str] = set()
        self._durable_deferred: set[str] = set()
        self._threads: dict[str, Thread] = {}
        self._execution_dispatcher = execution_dispatcher

    def enqueue(self, job_id: str) -> bool:
        with self._lock:
            if self._stopping.is_set() or job_id in self._in_flight or len(self._in_flight) >= self._max_in_flight:
                return False
            thread = self._prepare_thread_locked(job_id)
        thread.start()
        return True

    def enqueue_durable(self, job_id: str) -> bool:
        """Accept a durable Job for eventual in-process execution without dropping it on saturation."""

        with self._lock:
            if self._stopping.is_set():
                return False
            if job_id in self._in_flight or job_id in self._durable_deferred:
                return True
            if len(self._in_flight) >= self._max_in_flight:
                self._durable_deferred.add(job_id)
                return True
            thread = self._prepare_thread_locked(job_id)
        thread.start()
        return True

    @property
    def job_types(self) -> frozenset[str]:
        """The Core dispatch queue does not register executable Job handlers."""
        return frozenset()

    @property
    def database_path(self):
        return self._store.database_path

    @property
    def accepting(self) -> bool:
        return not self._stopping.is_set()

    def shutdown(self, *, timeout_seconds: float = 5.0) -> tuple[str, ...]:
        if timeout_seconds < 0:
            raise ValueError("shutdown timeout must be non-negative")
        self._stopping.set()
        deadline = time.monotonic() + timeout_seconds
        while True:
            with self._lock:
                threads = tuple(self._threads.values())
                remaining = tuple(sorted(self._in_flight | self._durable_deferred))
            if not remaining or time.monotonic() >= deadline:
                return remaining
            for thread in threads:
                thread.join(timeout=min(0.05, max(0.0, deadline - time.monotonic())))

    def _run_background(self, job_id: str) -> None:
        try:
            with self._slots:
                if self._stopping.is_set():
                    return
                self._execution_dispatcher(job_id)
        finally:
            next_thread: Thread | None = None
            with self._lock:
                self._in_flight.discard(job_id)
                self._threads.pop(job_id, None)
                if not self._stopping.is_set() and self._durable_deferred:
                    next_job_id = min(self._durable_deferred)
                    self._durable_deferred.remove(next_job_id)
                    next_thread = self._prepare_thread_locked(next_job_id)
            if next_thread is not None:
                next_thread.start()

    def _prepare_thread_locked(self, job_id: str) -> Thread:
        self._in_flight.add(job_id)
        thread = Thread(target=self._run_background, args=(job_id,), daemon=True, name=f"job-{job_id}")
        self._threads[job_id] = thread
        return thread
