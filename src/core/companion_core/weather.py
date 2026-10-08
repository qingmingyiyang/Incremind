from __future__ import annotations

import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from .errors import CompanionConflict, CompanionRepositoryError
from .repository import CompanionRepository


WEATHER_CONFIG_SETTING_ID = "weather_config"
WEATHER_CACHE_SETTING_ID = "weather_cache"
OPEN_METEO_FORECAST_ORIGIN = "https://api.open-meteo.com"
OPEN_METEO_TERMS_REVISION = "2026-07-23"
WEATHER_CONDITIONS = frozenset({"clear", "cloudy", "rain", "snow", "extreme", "unknown"})
WEATHER_FAILURES = frozenset({"offline", "timeout", "rate_limited", "provider_error", "invalid_response"})
_CURRENT_FIELDS = "temperature_2m,precipitation,rain,showers,snowfall,weather_code,is_day"
_BACKOFF_SECONDS = (300, 900, 1_800, 3_600)


@dataclass(frozen=True, slots=True)
class WeatherConfig:
    enabled: bool
    location_name: str
    latitude: float | None
    longitude: float | None
    noncommercial_acknowledged: bool
    terms_revision: str | None
    revision: int
    updated_at: str | None

    def as_dict(self) -> dict[str, object]:
        return {"enabled": self.enabled, "location_name": self.location_name, "latitude": self.latitude, "longitude": self.longitude, "noncommercial_acknowledged": self.noncommercial_acknowledged, "terms_revision": self.terms_revision}


@dataclass(frozen=True, slots=True)
class WeatherObservation:
    condition: str
    is_day: bool
    temperature_c: float


@dataclass(frozen=True, slots=True)
class WeatherCache:
    config_revision: int
    condition: str
    is_day: bool | None
    temperature_c: float | None
    fetched_at: str | None
    last_attempt_at: str | None
    next_attempt_at: str | None
    failure_count: int
    last_error: str | None
    revision: int

    def as_dict(self, *, now: datetime, current_config_revision: int, enabled: bool) -> dict[str, object]:
        same_config = self.config_revision == current_config_revision
        fetched = _parse_utc(self.fetched_at)
        stale = not same_config or fetched is None or now - fetched > timedelta(minutes=75)
        condition, is_day, temperature = self.condition, self.is_day, self.temperature_c
        if not enabled or not same_config:
            condition, is_day, temperature = "unknown", None, None
        return {"condition": condition, "is_day": is_day, "temperature_c": temperature, "fetched_at": self.fetched_at if same_config else None, "last_attempt_at": self.last_attempt_at if same_config else None, "next_attempt_at": self.next_attempt_at if enabled and same_config else None, "last_error": self.last_error if enabled and same_config else None, "stale": stale, "revision": self.revision}


class OpenMeteoWeatherProvider:
    def __init__(self, fetch_text: Callable[[str], str]) -> None:
        if not callable(fetch_text):
            raise TypeError("weather fetch function is required")
        self._fetch_text = fetch_text

    def fetch(self, *, latitude: float, longitude: float) -> WeatherObservation:
        try:
            payload = json.loads(self._fetch_text(build_open_meteo_url(latitude=latitude, longitude=longitude)))
        except (json.JSONDecodeError, TypeError) as exc:
            raise CompanionRepositoryError("weather response is invalid") from exc
        current = payload.get("current") if isinstance(payload, dict) else None
        if not isinstance(current, dict):
            raise CompanionRepositoryError("weather response is invalid")
        code = _finite_number(current.get("weather_code"), "weather code")
        temperature = _finite_number(current.get("temperature_2m"), "temperature")
        is_day = current.get("is_day")
        for field in ("precipitation", "rain", "showers", "snowfall"):
            _finite_number(current.get(field), field)
        if not code.is_integer() or not isinstance(is_day, int) or is_day not in {0, 1} or not -100 <= temperature <= 70:
            raise CompanionRepositoryError("weather response is invalid")
        return WeatherObservation(condition_for_wmo_code(int(code)), bool(is_day), round(temperature, 1))


class CompanionWeatherService:
    def __init__(self, repository: CompanionRepository, provider: OpenMeteoWeatherProvider, *, now: Callable[[], datetime] | None = None) -> None:
        self.repository = repository
        self.provider = provider
        self.now = now or (lambda: datetime.now(timezone.utc))

    def config(self) -> WeatherConfig:
        setting = self.repository.get_setting(WEATHER_CONFIG_SETTING_ID)
        if setting is None:
            return WeatherConfig(False, "", None, None, False, None, 0, None)
        try:
            return _weather_config(setting.payload, revision=setting.revision, updated_at=setting.updated_at)
        except CompanionRepositoryError as exc:
            raise CompanionRepositoryError("stored weather configuration is invalid") from exc

    def cache(self) -> WeatherCache:
        setting = self.repository.get_setting(WEATHER_CACHE_SETTING_ID)
        if setting is None:
            return WeatherCache(0, "unknown", None, None, None, None, None, 0, None, 0)
        try:
            return _weather_cache(setting.payload, revision=setting.revision)
        except CompanionRepositoryError as exc:
            raise CompanionRepositoryError("stored weather cache is invalid") from exc

    def status(self) -> dict[str, object]:
        config, cache = self.config(), self.cache()
        now = _require_aware_utc(self.now())
        return {"revision": config.revision, "config": config.as_dict(), "weather": cache.as_dict(now=now, current_config_revision=config.revision, enabled=config.enabled), "attribution": {"provider": "Open-Meteo", "terms_revision": OPEN_METEO_TERMS_REVISION, "license": "CC BY 4.0 / free API non-commercial terms", "terms_url": "https://open-meteo.com/en/terms", "privacy_notice": "Open-Meteo may retain service logs containing coordinates for up to 90 days."}}

    def configure(self, *, enabled: object, location_name: object, latitude: object, longitude: object, noncommercial_acknowledged: object, expected_revision: object) -> WeatherConfig:
        if not isinstance(expected_revision, int) or isinstance(expected_revision, bool) or expected_revision < 0:
            raise CompanionRepositoryError("weather revision is invalid")
        config = _weather_config({"enabled": enabled, "location_name": location_name, "latitude": latitude, "longitude": longitude, "noncommercial_acknowledged": noncommercial_acknowledged, "terms_revision": OPEN_METEO_TERMS_REVISION if noncommercial_acknowledged is True else None}, revision=expected_revision, updated_at=None)
        saved = self.repository.save_setting(setting_id=WEATHER_CONFIG_SETTING_ID, expected_revision=expected_revision, payload=config.as_dict(), updated_at=_iso_utc(_require_aware_utc(self.now())))
        return _weather_config(saved.payload, revision=saved.revision, updated_at=saved.updated_at)

    def due(self, *, manual: bool = False) -> bool:
        config = self.config()
        if not config.enabled:
            return False
        cache, now = self.cache(), _require_aware_utc(self.now())
        if cache.config_revision != config.revision:
            return True
        attempt = _parse_utc(cache.last_attempt_at)
        if manual:
            return attempt is None or now - attempt >= timedelta(seconds=60)
        next_attempt = _parse_utc(cache.next_attempt_at)
        return next_attempt is None or now >= next_attempt

    def refresh(self, *, manual: bool = False) -> dict[str, object]:
        config = self.config()
        if not config.enabled:
            return {"status": "disabled", **self.status()}
        if not self.due(manual=manual):
            return {"status": "not_due", **self.status()}
        assert config.latitude is not None and config.longitude is not None
        try:
            observation = self.provider.fetch(latitude=config.latitude, longitude=config.longitude)
        except Exception as exc:
            if self.config().revision != config.revision:
                return {"status": "discarded", **self.status()}
            self._record_failure(config_revision=config.revision, failure=classify_weather_failure(exc))
            return {"status": "failed", **self.status()}
        if self.config().revision != config.revision:
            return {"status": "discarded", **self.status()}
        self._record_success(config_revision=config.revision, observation=observation)
        return {"status": "refreshed", **self.status()}

    def _record_success(self, *, config_revision: int, observation: WeatherObservation) -> None:
        now = _require_aware_utc(self.now())
        self._save_cache({"config_revision": config_revision, "condition": observation.condition, "is_day": observation.is_day, "temperature_c": observation.temperature_c, "fetched_at": _iso_utc(now), "last_attempt_at": _iso_utc(now), "next_attempt_at": _iso_utc(now + timedelta(hours=1)), "failure_count": 0, "last_error": None})

    def _record_failure(self, *, config_revision: int, failure: str) -> None:
        previous, now = self.cache(), _require_aware_utc(self.now())
        count = min(32, previous.failure_count + 1 if previous.config_revision == config_revision else 1)
        delay = _BACKOFF_SECONDS[min(count - 1, len(_BACKOFF_SECONDS) - 1)]
        same = previous.config_revision == config_revision
        self._save_cache({"config_revision": config_revision, "condition": previous.condition if same else "unknown", "is_day": previous.is_day if same else None, "temperature_c": previous.temperature_c if same else None, "fetched_at": previous.fetched_at if same else None, "last_attempt_at": _iso_utc(now), "next_attempt_at": _iso_utc(now + timedelta(seconds=delay)), "failure_count": count, "last_error": failure})

    def _save_cache(self, payload: dict[str, object]) -> None:
        for _attempt in range(2):
            current = self.repository.get_setting(WEATHER_CACHE_SETTING_ID)
            try:
                self.repository.save_setting(setting_id=WEATHER_CACHE_SETTING_ID, expected_revision=current.revision if current else 0, payload=payload, updated_at=_iso_utc(_require_aware_utc(self.now())))
                return
            except CompanionConflict:
                continue
        raise CompanionConflict("weather cache changed during refresh")


def build_open_meteo_url(*, latitude: float, longitude: float) -> str:
    lat = _coordinate(latitude, minimum=-90, maximum=90, field="latitude")
    lon = _coordinate(longitude, minimum=-180, maximum=180, field="longitude")
    query = urlencode({"latitude": _format_coordinate(lat), "longitude": _format_coordinate(lon), "current": _CURRENT_FIELDS, "timezone": "auto"})
    return f"{OPEN_METEO_FORECAST_ORIGIN}/v1/forecast?{query}"


def condition_for_wmo_code(code: int) -> str:
    if code == 0:
        return "clear"
    if code in {1, 2, 3, 45, 48}:
        return "cloudy"
    if code in {51, 53, 55, 61, 63, 65, 80, 81, 82}:
        return "rain"
    if code in {71, 73, 75, 77, 85, 86}:
        return "snow"
    if code in {56, 57, 66, 67, 95, 96, 99}:
        return "extreme"
    return "unknown"


def classify_weather_failure(exc: Exception) -> str:
    message = str(exc).lower()
    if "429" in message:
        return "rate_limited"
    if "timeout" in message or isinstance(exc, TimeoutError):
        return "timeout"
    if "offline" in message or "could not be resolved" in message or "connection failed" in message:
        return "offline"
    if isinstance(exc, CompanionRepositoryError):
        return "invalid_response"
    return "provider_error"


def _weather_config(payload: object, *, revision: int, updated_at: str | None) -> WeatherConfig:
    expected = {"enabled", "location_name", "latitude", "longitude", "noncommercial_acknowledged", "terms_revision"}
    if not isinstance(payload, dict) or set(payload) != expected:
        raise CompanionRepositoryError("weather configuration is invalid")
    enabled, acknowledged, name = payload["enabled"], payload["noncommercial_acknowledged"], payload["location_name"]
    if not isinstance(enabled, bool) or not isinstance(acknowledged, bool) or not isinstance(name, str):
        raise CompanionRepositoryError("weather configuration is invalid")
    name = name.strip()
    if len(name) > 80 or any(ord(char) < 32 or ord(char) == 127 for char in name):
        raise CompanionRepositoryError("weather location name is invalid")
    latitude = None if payload["latitude"] is None else _coordinate(payload["latitude"], minimum=-90, maximum=90, field="latitude")
    longitude = None if payload["longitude"] is None else _coordinate(payload["longitude"], minimum=-180, maximum=180, field="longitude")
    if (latitude is None) != (longitude is None):
        raise CompanionRepositoryError("weather coordinates are incomplete")
    terms_revision = payload["terms_revision"]
    if terms_revision is not None and terms_revision != OPEN_METEO_TERMS_REVISION:
        raise CompanionRepositoryError("weather terms revision is invalid")
    if acknowledged != (terms_revision == OPEN_METEO_TERMS_REVISION):
        raise CompanionRepositoryError("weather terms acknowledgement is invalid")
    if enabled and (not name or latitude is None or longitude is None or not acknowledged):
        raise CompanionRepositoryError("enabled weather requires location and non-commercial acknowledgement")
    return WeatherConfig(enabled, name, latitude, longitude, acknowledged, terms_revision, revision, updated_at)


def _weather_cache(payload: object, *, revision: int) -> WeatherCache:
    expected = {"config_revision", "condition", "is_day", "temperature_c", "fetched_at", "last_attempt_at", "next_attempt_at", "failure_count", "last_error"}
    if not isinstance(payload, dict) or set(payload) != expected or payload["condition"] not in WEATHER_CONDITIONS:
        raise CompanionRepositoryError("weather cache is invalid")
    is_day, temperature = payload["is_day"], payload["temperature_c"]
    if is_day is not None and not isinstance(is_day, bool):
        raise CompanionRepositoryError("weather cache is invalid")
    if temperature is not None:
        temperature = _finite_number(temperature, "temperature")
        if not -100 <= temperature <= 70:
            raise CompanionRepositoryError("weather cache is invalid")
    config_revision, failure_count = payload["config_revision"], payload["failure_count"]
    if not isinstance(config_revision, int) or isinstance(config_revision, bool) or config_revision < 0 or not isinstance(failure_count, int) or isinstance(failure_count, bool) or not 0 <= failure_count <= 32:
        raise CompanionRepositoryError("weather cache is invalid")
    error = payload["last_error"]
    if error is not None and error not in WEATHER_FAILURES:
        raise CompanionRepositoryError("weather cache is invalid")
    for field in ("fetched_at", "last_attempt_at", "next_attempt_at"):
        if payload[field] is not None and _parse_utc(payload[field]) is None:
            raise CompanionRepositoryError("weather cache is invalid")
    return WeatherCache(config_revision, payload["condition"], is_day, temperature, payload["fetched_at"], payload["last_attempt_at"], payload["next_attempt_at"], failure_count, error, revision)


def _coordinate(value: object, *, minimum: float, maximum: float, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise CompanionRepositoryError(f"weather {field} is invalid")
    result = round(float(value), 4)
    if not minimum <= result <= maximum:
        raise CompanionRepositoryError(f"weather {field} is invalid")
    return result


def _format_coordinate(value: float) -> str:
    rendered = f"{value:.4f}".rstrip("0").rstrip(".")
    return "0" if rendered in {"-0", ""} else rendered


def _finite_number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise CompanionRepositoryError(f"weather {field} is invalid")
    return float(value)


def _require_aware_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise CompanionRepositoryError("weather clock is invalid")
    return value.astimezone(timezone.utc)


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_utc(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.endswith("Z"):
        return None
    try:
        return datetime.fromisoformat(value[:-1] + "+00:00").astimezone(timezone.utc)
    except ValueError:
        return None
