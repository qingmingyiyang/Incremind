from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from backend.security.network_adapter import SafeTextNetworkAdapter
from core.companion_core.repository import CompanionRepository
from core.companion_core.weather import CompanionWeatherService, OpenMeteoWeatherProvider


class CompanionWeatherRuntime:
    """Runs bounded weather refreshes outside scheduler and API request threads."""

    def __init__(
        self,
        data_root: Path,
        *,
        service: CompanionWeatherService | None = None,
        now: Callable[[], datetime] | None = None,
        poll_seconds: float = 30.0,
    ) -> None:
        if poll_seconds <= 0:
            raise ValueError("weather poll interval must be positive")
        self.now = now or (lambda: datetime.now(timezone.utc))
        if service is None:
            adapter = SafeTextNetworkAdapter(max_redirects=0, max_response_bytes=64 * 1024, timeout_seconds=5.0)
            repository = CompanionRepository.at_data_root(data_root)
            service = CompanionWeatherService(repository, OpenMeteoWeatherProvider(adapter.fetch_text), now=self.now)
        self.service = service
        self.poll_seconds = poll_seconds
        self._condition = threading.Condition()
        self._thread: threading.Thread | None = None
        self._stopping = False
        self._manual = False

    def start(self) -> bool:
        with self._condition:
            if self._thread is not None and self._thread.is_alive():
                return False
            self.service.repository.initialize()
            self._stopping = False
            self._manual = False
            self._thread = threading.Thread(target=self._run, name="companion-weather", daemon=True)
            self._thread.start()
            return True

    def stop(self, timeout_seconds: float = 6.0) -> bool:
        with self._condition:
            thread = self._thread
            if thread is None:
                return False
            self._stopping = True
            self._condition.notify_all()
        thread.join(timeout_seconds)
        with self._condition:
            if not thread.is_alive() and self._thread is thread:
                self._thread = None
        return not thread.is_alive()

    def wake(self, *, manual: bool = False) -> None:
        with self._condition:
            self._manual = self._manual or manual
            self._condition.notify_all()

    def status(self) -> dict[str, object]:
        return self.service.status()

    def tick(self, *, manual: bool = False) -> dict[str, object]:
        if not self.service.due(manual=manual):
            return {"status": "not_due", **self.service.status()}
        return self.service.refresh(manual=manual)

    def _run(self) -> None:
        while True:
            with self._condition:
                if self._stopping:
                    return
                manual = self._manual
                self._manual = False
            try:
                self.tick(manual=manual)
            except Exception:
                # Do not log a request URL or coordinates. Repository/network
                # failures are retried on the next bounded poll.
                pass
            with self._condition:
                if self._stopping:
                    return
                self._condition.wait(timeout=self.poll_seconds)
