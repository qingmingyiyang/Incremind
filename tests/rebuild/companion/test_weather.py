from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import pytest

from core.companion_core import (
    CompanionConflict,
    CompanionRepository,
    CompanionRepositoryError,
    CompanionWeatherService,
    OpenMeteoWeatherProvider,
    build_open_meteo_url,
    classify_weather_failure,
    condition_for_wmo_code,
)


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 7, 23, 1, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self.value

    def advance(self, **kwargs: int) -> None:
        self.value += timedelta(**kwargs)


def response(*, code: int = 61, is_day: int = 1, temperature: float = 18.4) -> str:
    return json.dumps({"current": {"temperature_2m": temperature, "precipitation": 0.3, "rain": 0.3, "showers": 0, "snowfall": 0, "weather_code": code, "is_day": is_day}})


def service(tmp_path, fetch=None):
    clock = Clock()
    repository = CompanionRepository.at_data_root(tmp_path)
    repository.initialize()
    calls: list[str] = []

    def request(url: str) -> str:
        calls.append(url)
        if fetch:
            return fetch(url)
        return response()

    return CompanionWeatherService(repository, OpenMeteoWeatherProvider(request), now=clock.now), clock, calls


def enable(current: CompanionWeatherService, *, revision: int = 0, latitude: float = 31.230416, longitude: float = 121.473701):
    return current.configure(enabled=True, location_name="上海", latitude=latitude, longitude=longitude, noncommercial_acknowledged=True, expected_revision=revision)


def test_fixed_url_rounds_coordinates_and_contains_only_allowlisted_query() -> None:
    url = build_open_meteo_url(latitude=31.230416, longitude=121.473701)
    parsed = urlsplit(url)
    assert parsed.scheme == "https"
    assert parsed.netloc == "api.open-meteo.com"
    assert parsed.path == "/v1/forecast"
    query = parse_qs(parsed.query)
    assert query == {
        "latitude": ["31.2304"],
        "longitude": ["121.4737"],
        "current": ["temperature_2m,precipitation,rain,showers,snowfall,weather_code,is_day"],
        "timezone": ["auto"],
    }
    for bad in (float("nan"), float("inf"), True, "31.2"):
        with pytest.raises(CompanionRepositoryError):
            build_open_meteo_url(latitude=bad, longitude=1)


@pytest.mark.parametrize(
    ("codes", "condition"),
    [
        ((0,), "clear"),
        ((1, 2, 3, 45, 48), "cloudy"),
        ((51, 53, 55, 61, 63, 65, 80, 81, 82), "rain"),
        ((71, 73, 75, 77, 85, 86), "snow"),
        ((56, 57, 66, 67, 95, 96, 99), "extreme"),
        ((4, -1, 1000), "unknown"),
    ],
)
def test_wmo_mapping(codes, condition) -> None:
    assert {condition_for_wmo_code(code) for code in codes} == {condition}


def test_provider_rejects_unbounded_or_malformed_current_values() -> None:
    for payload in (
        "not-json",
        "{}",
        None,
        json.dumps({"current": {}}),
        response(code=1, temperature=float("nan")),
        response(code=1, temperature=90),
        response(code=1, is_day=3),
        response(code=1, is_day=1.0),
    ):
        provider = OpenMeteoWeatherProvider(lambda _url, value=payload: value)
        with pytest.raises(CompanionRepositoryError, match="weather response is invalid|weather .* is invalid"):
            provider.fetch(latitude=1, longitude=2)


def test_default_is_disabled_and_enabled_configuration_requires_explicit_terms(tmp_path) -> None:
    current, _clock, calls = service(tmp_path)
    status = current.status()
    assert status["revision"] == 0
    assert status["config"]["enabled"] is False
    assert status["weather"]["condition"] == "unknown"
    assert calls == []
    with pytest.raises(CompanionRepositoryError):
        current.configure(enabled=True, location_name="上海", latitude=31.2, longitude=121.4, noncommercial_acknowledged=False, expected_revision=0)
    saved = enable(current)
    assert saved.latitude == 31.2304
    assert saved.longitude == 121.4737
    with pytest.raises(CompanionConflict):
        enable(current, revision=0)


def test_success_caches_one_hour_and_manual_refresh_is_rate_limited(tmp_path) -> None:
    current, clock, calls = service(tmp_path)
    config = enable(current)
    result = current.refresh()
    assert result["status"] == "refreshed"
    assert result["weather"] == {
        "condition": "rain",
        "is_day": True,
        "temperature_c": 18.4,
        "fetched_at": "2026-07-23T01:00:00Z",
        "last_attempt_at": "2026-07-23T01:00:00Z",
        "next_attempt_at": "2026-07-23T02:00:00Z",
        "last_error": None,
        "stale": False,
        "revision": 1,
    }
    assert len(calls) == 1
    assert current.refresh()["status"] == "not_due"
    assert current.refresh(manual=True)["status"] == "not_due"
    clock.advance(seconds=60)
    assert current.refresh(manual=True)["status"] == "refreshed"
    assert len(calls) == 2
    # Background cache writes do not mutate the UI configuration CAS revision.
    assert current.config().revision == config.revision == 1


def test_failure_backoff_keeps_last_known_good_and_becomes_stale(tmp_path) -> None:
    failures = [False]

    def fetch(_url):
        if failures[0]:
            raise TimeoutError("private-coordinate-canary timeout")
        return response(code=0, temperature=20)

    current, clock, _calls = service(tmp_path, fetch)
    enable(current)
    assert current.refresh()["weather"]["condition"] == "clear"
    failures[0] = True
    clock.advance(hours=1)
    failed = current.refresh()
    assert failed["status"] == "failed"
    assert failed["weather"]["condition"] == "clear"
    assert failed["weather"]["last_error"] == "timeout"
    assert failed["weather"]["next_attempt_at"] == "2026-07-23T02:05:00Z"
    clock.advance(minutes=5)
    failed = current.refresh()
    assert failed["weather"]["next_attempt_at"] == "2026-07-23T02:20:00Z"
    clock.advance(minutes=71)
    assert current.status()["weather"]["stale"] is True
    assert "private-coordinate-canary" not in json.dumps(current.status())


def test_config_change_discards_late_result_and_hides_previous_location(tmp_path) -> None:
    holder = {}

    def fetch(_url):
        current = holder["service"]
        current.configure(enabled=True, location_name="北京", latitude=39.9042, longitude=116.4074, noncommercial_acknowledged=True, expected_revision=1)
        return response(code=61)

    current, _clock, _calls = service(tmp_path, fetch)
    holder["service"] = current
    enable(current)
    result = current.refresh()
    assert result["status"] == "discarded"
    assert result["config"]["location_name"] == "北京"
    assert result["weather"]["condition"] == "unknown"
    assert current.cache().revision == 0


def test_disable_stops_due_and_clears_public_projection(tmp_path) -> None:
    current, _clock, calls = service(tmp_path)
    enable(current)
    current.refresh()
    disabled = current.configure(enabled=False, location_name="上海", latitude=31.2304, longitude=121.4737, noncommercial_acknowledged=True, expected_revision=1)
    assert disabled.enabled is False
    assert current.due() is False
    assert current.refresh()["status"] == "disabled"
    assert current.status()["weather"]["condition"] == "unknown"
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (RuntimeError("response status is not successful: 429"), "rate_limited"),
        (TimeoutError("timeout"), "timeout"),
        (RuntimeError("URL connection failed"), "offline"),
        (CompanionRepositoryError("weather response is invalid"), "invalid_response"),
        (RuntimeError("HTTP 500"), "provider_error"),
    ],
)
def test_failure_classification_never_returns_error_body(error, expected) -> None:
    assert classify_weather_failure(error) == expected
