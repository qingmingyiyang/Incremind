from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from core.companion_core import CompanionRepository, CompanionRepositoryError, CompanionSystemSensorService, normalize_game_processes, normalize_health_origin


class Clock:
    def __init__(self): self.value = datetime(2026, 7, 22, tzinfo=timezone.utc)
    def __call__(self): return self.value
    def advance(self, **kwargs): self.value += timedelta(**kwargs)


class Samples:
    def __init__(self, cpu=20, percent=40, available=8 * 1024**3, processes=()):
        self.cpu, self.percent, self.available, self.processes = cpu, percent, available, processes
    def cpu_value(self): return self.cpu
    def memory(self): return SimpleNamespace(percent=self.percent, available=self.available)
    def names(self): return self.processes


def service(tmp_path, samples=None, clock=None):
    samples = samples or Samples(); clock = clock or Clock()
    repo = CompanionRepository(tmp_path / "companion.sqlite3", now=clock)
    return CompanionSystemSensorService(repo, cpu_percent=samples.cpu_value, virtual_memory=samples.memory, process_names=samples.names, now=clock)


def test_cpu_requires_three_high_and_three_low_samples(tmp_path):
    values = Samples(cpu=90)
    sensor = service(tmp_path, values)
    assert sensor.sample(network_state="normal", latency_ms=50)["sample"]["resource"] == "normal"
    assert sensor.sample(network_state="normal", latency_ms=50)["sample"]["resource"] == "normal"
    hot = sensor.sample(network_state="normal", latency_ms=50)["sample"]
    assert hot["resource"] == "hot" and hot["should_alert"] is True and hot["cpu_bucket"] == 90
    values.cpu = 75
    assert sensor.sample(network_state="normal", latency_ms=50)["sample"]["resource"] == "hot"
    values.cpu = 60
    assert sensor.sample(network_state="normal", latency_ms=50)["sample"]["resource"] == "hot"
    assert sensor.sample(network_state="normal", latency_ms=50)["sample"]["resource"] == "hot"
    assert sensor.sample(network_state="normal", latency_ms=50)["sample"]["resource"] == "normal"


def test_memory_requires_both_thresholds_and_recovers_with_hysteresis(tmp_path):
    values = Samples(cpu=10, percent=95, available=3 * 1024**3)
    sensor = service(tmp_path, values)
    assert sensor.sample(network_state="normal", latency_ms=100)["sample"]["resource"] == "normal"
    values.available = 512 * 1024**2
    assert sensor.sample(network_state="normal", latency_ms=100)["sample"]["resource"] == "normal"
    assert sensor.sample(network_state="normal", latency_ms=100)["sample"]["resource"] == "memory_low"
    values.percent, values.available = 79, 3 * 1024**3
    assert sensor.sample(network_state="normal", latency_ms=100)["sample"]["resource"] == "memory_low"
    assert sensor.sample(network_state="normal", latency_ms=100)["sample"]["resource"] == "normal"


def test_alert_has_ten_minute_cooldown_and_network_is_bucketed(tmp_path):
    clock = Clock(); values = Samples(cpu=99)
    sensor = service(tmp_path, values, clock)
    for _ in range(2): sensor.sample(network_state="slow", latency_ms=450)
    first = sensor.sample(network_state="slow", latency_ms=450)["sample"]
    assert first["should_alert"] is True and first["network"] == "slow" and first["latency_bucket"] == "slow"
    assert sensor.sample(network_state="slow", latency_ms=450)["sample"]["should_alert"] is False
    clock.advance(minutes=10)
    assert sensor.sample(network_state="slow", latency_ms=450)["sample"]["should_alert"] is True


def test_game_whitelist_is_exact_and_process_details_are_not_projected(tmp_path):
    values = Samples(processes=("steam.exe", "code.exe"))
    sensor = service(tmp_path, values)
    configured = sensor.configure(enabled=True, network_enabled=True, health_origin=None, game_enabled=True, game_processes=["Steam.EXE"], game_behavior="corner", expected_revision=0)
    assert configured.game_processes == ("steam.exe",)
    sample = sensor.sample(network_state="offline", latency_ms=None)["sample"]
    assert sample["game_active"] is True and sample["game_behavior"] == "corner"
    assert "steam" not in str(sample).lower() and "code" not in str(sample).lower()


def test_disable_resets_runtime_and_sampling_failure_is_one_unknown_state(tmp_path):
    values = Samples(cpu=99)
    sensor = service(tmp_path, values)
    for _ in range(3): sensor.sample(network_state="normal", latency_ms=1)
    config = sensor.configure(enabled=False, network_enabled=True, health_origin=None, game_enabled=True, game_processes=[], game_behavior="quiet", expected_revision=0)
    assert config.revision == 1
    assert sensor.sample(network_state="normal", latency_ms=1)["sample"]["error"] == "disabled"
    broken = CompanionSystemSensorService(CompanionRepository(tmp_path / "broken.sqlite3"), cpu_percent=lambda: (_ for _ in ()).throw(OSError("denied")))
    result = broken.sample(network_state="unknown", latency_ms=None)["sample"]
    assert result["resource"] == "unknown" and result["error"] == "sampling_unavailable"


def test_health_origin_and_process_validation_fail_closed():
    assert normalize_health_origin("https://Example.COM:443/") == "https://example.com"
    assert normalize_health_origin("") is None
    assert normalize_game_processes(["Steam.EXE", "steam.exe"]) == ("steam.exe",)
    for value in ("http://example.com", "https://user@example.com", "https://example.com/path", "https://example.com?q=x"):
        with pytest.raises(CompanionRepositoryError): normalize_health_origin(value)
    with pytest.raises(CompanionRepositoryError): normalize_game_processes(["game.exe --arg"])
