from __future__ import annotations

import time

from backend.companion_weather_runtime import CompanionWeatherRuntime


class Service:
    def __init__(self) -> None:
        self.repository = Repository()
        self.due_values = [True, False]
        self.refreshes = []

    def due(self, *, manual=False):
        self.refreshes.append(("due", manual))
        return self.due_values.pop(0) if self.due_values else False

    def refresh(self, *, manual=False):
        self.refreshes.append(("refresh", manual))
        return {"status": "refreshed"}

    def status(self):
        return {"revision": 0, "config": {"enabled": False}, "weather": {"condition": "unknown"}}


class Repository:
    def __init__(self) -> None:
        self.initialized = 0

    def initialize(self):
        self.initialized += 1


def test_runtime_tick_does_not_fetch_when_not_due(tmp_path) -> None:
    current = Service()
    current.due_values = [False]
    runtime = CompanionWeatherRuntime(tmp_path, service=current)
    assert runtime.tick() == {"status": "not_due", **current.status()}
    assert current.refreshes == [("due", False)]


def test_runtime_thread_is_singleton_wakes_and_stops(tmp_path) -> None:
    current = Service()
    runtime = CompanionWeatherRuntime(tmp_path, service=current, poll_seconds=60)
    assert runtime.start() is True
    assert runtime.start() is False
    deadline = time.monotonic() + 1
    while ("refresh", False) not in current.refreshes and time.monotonic() < deadline:
        time.sleep(0.01)
    assert ("refresh", False) in current.refreshes
    current.due_values = [True]
    runtime.wake(manual=True)
    deadline = time.monotonic() + 1
    while ("refresh", True) not in current.refreshes and time.monotonic() < deadline:
        time.sleep(0.01)
    assert ("refresh", True) in current.refreshes
    assert runtime.stop() is True
    assert runtime.stop() is False
    assert current.repository.initialized == 1


def test_default_runtime_uses_the_fixed_five_second_no_redirect_64k_network_boundary(tmp_path) -> None:
    runtime = CompanionWeatherRuntime(tmp_path)
    adapter = runtime.service.provider._fetch_text.__self__
    assert adapter._max_redirects == 0
    assert adapter._max_response_bytes == 64 * 1024
    assert adapter._timeout_seconds == 5.0
