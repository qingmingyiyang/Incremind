from __future__ import annotations

import os
import re
import time
from datetime import datetime, timezone
from typing import Mapping, Protocol


E2E_CLOCK_MODE = "packaged-fixed"
E2E_CLOCK_MODE_ENV = "CHRIPTMAS_COMPANION_E2E_CLOCK_MODE"
E2E_CLOCK_UTC_ENV = "CHRIPTMAS_COMPANION_E2E_CLOCK_UTC"
_E2E_CLOCK_ENV_PREFIX = "CHRIPTMAS_COMPANION_E2E_CLOCK_"
_UTC = re.compile(r"^(?:202[0-9]|20[3-9][0-9]|2100)-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\dZ$")


class CompanionClock(Protocol):
    mode: str

    def now_utc(self) -> datetime: ...

    def wall_now(self) -> datetime: ...

    def monotonic(self) -> float: ...

    def local_day(self, value: datetime | None = None) -> str: ...


class SystemCompanionClock:
    mode = "system"

    def now_utc(self) -> datetime:
        return datetime.now(timezone.utc)

    def wall_now(self) -> datetime:
        return self.now_utc()

    def monotonic(self) -> float:
        return time.monotonic()

    def local_day(self, value: datetime | None = None) -> str:
        instant = value or self.now_utc()
        return instant.astimezone().date().isoformat()


class FixedCompanionClock:
    mode = "e2e_fixed"

    def __init__(self, instant: datetime) -> None:
        if instant.tzinfo is None or instant.utcoffset() != timezone.utc.utcoffset(instant):
            raise ValueError("fixed companion clock must use UTC")
        self._instant = instant.replace(microsecond=0)

    def now_utc(self) -> datetime:
        return self._instant

    def wall_now(self) -> datetime:
        return self._instant

    def monotonic(self) -> float:
        return time.monotonic()

    def local_day(self, value: datetime | None = None) -> str:
        instant = value or self._instant
        return instant.astimezone().date().isoformat()


def build_companion_clock(env: Mapping[str, str] | None = None) -> CompanionClock:
    values = os.environ if env is None else env
    clock_keys = {key for key in values if key.startswith(_E2E_CLOCK_ENV_PREFIX)}
    if clock_keys != {E2E_CLOCK_MODE_ENV, E2E_CLOCK_UTC_ENV}:
        return SystemCompanionClock()
    if values.get(E2E_CLOCK_MODE_ENV) != E2E_CLOCK_MODE:
        return SystemCompanionClock()
    raw = values.get(E2E_CLOCK_UTC_ENV)
    if not isinstance(raw, str) or _UTC.fullmatch(raw) is None:
        return SystemCompanionClock()
    try:
        return FixedCompanionClock(datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc))
    except ValueError:
        return SystemCompanionClock()
