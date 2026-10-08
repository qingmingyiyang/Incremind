from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import re
from urllib.parse import urlsplit

import psutil

from .errors import CompanionConflict, CompanionRepositoryError
from .repository import CompanionRepository


_PROCESS = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")
_NETWORK_STATES = {"normal", "slow", "offline", "unknown"}
_GAME_BEHAVIORS = {"quiet", "corner", "hide"}


@dataclass(frozen=True, slots=True)
class CompanionSensorConfig:
    enabled: bool
    network_enabled: bool
    health_origin: str | None
    game_enabled: bool
    game_processes: tuple[str, ...]
    game_behavior: str
    revision: int
    updated_at: str

    def as_dict(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "network_enabled": self.network_enabled,
            "health_origin": self.health_origin,
            "game_enabled": self.game_enabled,
            "game_processes": list(self.game_processes),
            "game_behavior": self.game_behavior,
        }


class CompanionSystemSensorService:
    SETTING_ID = "system_sensors"

    def __init__(
        self,
        repository: CompanionRepository,
        *,
        cpu_percent: Callable[[], float] | None = None,
        virtual_memory: Callable[[], object] | None = None,
        process_names: Callable[[], Iterable[str]] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.repository = repository
        self.cpu_percent = cpu_percent or (lambda: float(psutil.cpu_percent(interval=None)))
        self.virtual_memory = virtual_memory or psutil.virtual_memory
        self.process_names = process_names or _process_names
        self.now = now or (lambda: datetime.now(timezone.utc))
        self._cpu_hot = False
        self._cpu_high_streak = 0
        self._cpu_low_streak = 0
        self._memory_low = False
        self._memory_bad_streak = 0
        self._memory_good_streak = 0
        self._last_alert_at: datetime | None = None
        self._last = _unknown_snapshot("not_sampled")

    def config(self) -> CompanionSensorConfig:
        self.repository.initialize()
        setting = self.repository.get_setting(self.SETTING_ID)
        payload = _config(setting.payload if setting else None)
        return _sensor_config(payload, revision=setting.revision if setting else 0, updated_at=setting.updated_at if setting else "")

    def configure(
        self,
        *,
        enabled: object,
        network_enabled: object,
        health_origin: object,
        game_enabled: object,
        game_processes: object,
        game_behavior: object,
        expected_revision: object,
    ) -> CompanionSensorConfig:
        payload = _config({
            "enabled": enabled,
            "network_enabled": network_enabled,
            "health_origin": health_origin,
            "game_enabled": game_enabled,
            "game_processes": game_processes,
            "game_behavior": game_behavior,
        })
        if not isinstance(expected_revision, int) or isinstance(expected_revision, bool) or expected_revision < 0:
            raise CompanionRepositoryError("sensor settings revision is invalid")
        self.repository.initialize()
        setting = self.repository.save_setting(
            setting_id=self.SETTING_ID,
            payload=payload,
            expected_revision=expected_revision,
            updated_at=_utc(self.now()).isoformat(),
        )
        if not payload["enabled"]:
            self.reset_runtime("disabled")
        return _sensor_config(payload, revision=setting.revision, updated_at=setting.updated_at)

    def status(self) -> dict[str, object]:
        config = self.config()
        return {"config": config.as_dict(), "revision": config.revision, "sample": dict(self._last)}

    def sample(self, *, network_state: object = "unknown", latency_ms: object = None) -> dict[str, object]:
        config = self.config()
        if not config.enabled:
            self.reset_runtime("disabled")
            return {"config": config.as_dict(), "revision": config.revision, "sample": dict(self._last)}
        network, latency_bucket = _network(network_state, latency_ms, enabled=config.network_enabled)
        now = _utc(self.now())
        try:
            cpu = max(0.0, min(100.0, float(self.cpu_percent())))
            memory = self.virtual_memory()
            memory_percent = max(0.0, min(100.0, float(getattr(memory, "percent"))))
            available = int(getattr(memory, "available"))
            if available < 0:
                raise ValueError("negative available memory")
            names = set(self.process_names()) if config.game_enabled else set()
            game_active = bool(names.intersection(config.game_processes))
        except (psutil.Error, OSError, RuntimeError, TypeError, ValueError):
            self._last = {**_unknown_snapshot("sampling_unavailable"), "network": network, "latency_bucket": latency_bucket, "sampled_at": now.isoformat()}
            return {"config": config.as_dict(), "revision": config.revision, "sample": dict(self._last)}
        self._advance_cpu(cpu)
        self._advance_memory(memory_percent, available)
        resource = "memory_low" if self._memory_low else "hot" if self._cpu_hot else "normal"
        should_alert = resource in {"hot", "memory_low"} and (self._last.get("resource") != resource or self._last_alert_at is None or now - self._last_alert_at >= timedelta(minutes=10))
        if should_alert:
            self._last_alert_at = now
        self._last = {
            "resource": resource,
            "cpu_bucket": _bucket(cpu),
            "memory_percent_bucket": _bucket(memory_percent),
            "memory_available_bucket": _bytes_bucket(available),
            "network": network,
            "latency_bucket": latency_bucket,
            "game_active": game_active,
            "game_behavior": config.game_behavior if game_active else "none",
            "should_alert": should_alert,
            "alert_kind": resource if should_alert else "none",
            "sampled_at": now.isoformat(),
            "error": None,
        }
        return {"config": config.as_dict(), "revision": config.revision, "sample": dict(self._last)}

    def reset_runtime(self, reason: str = "stopped") -> None:
        self._cpu_hot = False
        self._cpu_high_streak = self._cpu_low_streak = 0
        self._memory_low = False
        self._memory_bad_streak = self._memory_good_streak = 0
        self._last_alert_at = None
        self._last = _unknown_snapshot(reason)

    def _advance_cpu(self, value: float) -> None:
        self._cpu_high_streak = self._cpu_high_streak + 1 if value > 85 else 0
        self._cpu_low_streak = self._cpu_low_streak + 1 if value < 70 else 0
        if not self._cpu_hot and self._cpu_high_streak >= 3:
            self._cpu_hot = True
            self._cpu_low_streak = 0
        elif self._cpu_hot and self._cpu_low_streak >= 3:
            self._cpu_hot = False
            self._cpu_high_streak = 0

    def _advance_memory(self, percent: float, available: int) -> None:
        bad = percent >= 90 and available <= 1024**3
        good = percent <= 80 and available >= 2 * 1024**3
        self._memory_bad_streak = self._memory_bad_streak + 1 if bad else 0
        self._memory_good_streak = self._memory_good_streak + 1 if good else 0
        if not self._memory_low and self._memory_bad_streak >= 2:
            self._memory_low = True
            self._memory_good_streak = 0
        elif self._memory_low and self._memory_good_streak >= 2:
            self._memory_low = False
            self._memory_bad_streak = 0


def normalize_health_origin(value: object) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str) or len(value) > 240 or "\x00" in value:
        raise CompanionRepositoryError("sensor health origin is invalid")
    parsed = urlsplit(value.strip())
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise CompanionRepositoryError("sensor health origin must be an HTTPS origin")
    if parsed.path not in {"", "/"}:
        raise CompanionRepositoryError("sensor health origin must not contain a path")
    host = parsed.hostname.encode("idna").decode("ascii").lower()
    port = f":{parsed.port}" if parsed.port and parsed.port != 443 else ""
    return f"https://{host}{port}"


def normalize_game_processes(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or len(value) > 32:
        raise CompanionRepositoryError("game process whitelist is invalid")
    result = []
    for item in value:
        if not isinstance(item, str):
            raise CompanionRepositoryError("game process whitelist is invalid")
        name = item.strip().lower()
        if _PROCESS.fullmatch(name) is None or name == "unknown":
            raise CompanionRepositoryError("game process whitelist is invalid")
        if name not in result:
            result.append(name)
    return tuple(result)


def _config(value: object) -> dict[str, object]:
    if value is None:
        return {"enabled": True, "network_enabled": True, "health_origin": None, "game_enabled": True, "game_processes": [], "game_behavior": "quiet"}
    if not isinstance(value, Mapping) or set(value) != {"enabled", "network_enabled", "health_origin", "game_enabled", "game_processes", "game_behavior"}:
        raise CompanionRepositoryError("sensor settings are invalid")
    if any(not isinstance(value[key], bool) for key in ("enabled", "network_enabled", "game_enabled")) or value["game_behavior"] not in _GAME_BEHAVIORS:
        raise CompanionRepositoryError("sensor settings are invalid")
    return {
        "enabled": value["enabled"],
        "network_enabled": value["network_enabled"],
        "health_origin": normalize_health_origin(value["health_origin"]),
        "game_enabled": value["game_enabled"],
        "game_processes": list(normalize_game_processes(value["game_processes"])),
        "game_behavior": value["game_behavior"],
    }


def _sensor_config(payload: Mapping[str, object], *, revision: int, updated_at: str) -> CompanionSensorConfig:
    return CompanionSensorConfig(
        enabled=bool(payload["enabled"]),
        network_enabled=bool(payload["network_enabled"]),
        health_origin=payload["health_origin"] if isinstance(payload["health_origin"], str) else None,
        game_enabled=bool(payload["game_enabled"]),
        game_processes=tuple(str(item) for item in payload["game_processes"]),
        game_behavior=str(payload["game_behavior"]),
        revision=revision,
        updated_at=updated_at,
    )


def _network(state: object, latency_ms: object, *, enabled: bool) -> tuple[str, str]:
    if not enabled:
        return "unknown", "disabled"
    if state not in _NETWORK_STATES:
        raise CompanionRepositoryError("network sensor state is invalid")
    if state in {"offline", "unknown"}:
        return str(state), str(state)
    if not isinstance(latency_ms, (int, float)) or isinstance(latency_ms, bool) or latency_ms < 0 or latency_ms > 60_000:
        raise CompanionRepositoryError("network sensor latency is invalid")
    bucket = "fast" if latency_ms < 100 else "normal" if latency_ms < 300 else "slow"
    return str(state), bucket


def _process_names() -> tuple[str, ...]:
    names: list[str] = []
    for process in psutil.process_iter(["name"]):
        try:
            name = str(process.info.get("name") or "").strip().lower()
            if _PROCESS.fullmatch(name):
                names.append(name)
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
    return tuple(names)


def _bucket(value: float) -> int:
    return int(max(0, min(100, round(value / 5) * 5)))


def _bytes_bucket(value: int) -> str:
    if value < 1024**3:
        return "under_1gb"
    if value < 2 * 1024**3:
        return "1_to_2gb"
    if value < 4 * 1024**3:
        return "2_to_4gb"
    return "over_4gb"


def _unknown_snapshot(reason: str) -> dict[str, object]:
    return {"resource": "unknown", "cpu_bucket": None, "memory_percent_bucket": None, "memory_available_bucket": "unknown", "network": "unknown", "latency_bucket": "unknown", "game_active": False, "game_behavior": "none", "should_alert": False, "alert_kind": "none", "sampled_at": None, "error": reason}


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise CompanionRepositoryError("sensor clock must be timezone aware")
    return value.astimezone(timezone.utc)
