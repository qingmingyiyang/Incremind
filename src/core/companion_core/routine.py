from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from .errors import CompanionRepositoryError
from .repository import CompanionRepository


ROUTINE_SETTING_ID = "routine"
MORNING_CLAIM_SETTING_ID = "routine_morning"
DEFAULT_ROUTINE = {"enabled": True, "sleep_start": "23:00", "wake_time": "07:00"}
_TIME = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


@dataclass(frozen=True, slots=True)
class CompanionRoutineSnapshot:
    enabled: bool
    sleep_start: str
    wake_time: str
    revision: int
    updated_at: str | None

    def as_dict(self) -> dict[str, object]:
        return {"enabled": self.enabled, "sleep_start": self.sleep_start, "wake_time": self.wake_time}


def validate_routine_settings(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {"enabled", "sleep_start", "wake_time"}:
        raise CompanionRepositoryError("routine settings must contain only enabled, sleep_start and wake_time")
    enabled, sleep_start, wake_time = value["enabled"], value["sleep_start"], value["wake_time"]
    if not isinstance(enabled, bool) or not isinstance(sleep_start, str) or not isinstance(wake_time, str):
        raise CompanionRepositoryError("routine settings types are invalid")
    if _TIME.fullmatch(sleep_start) is None or _TIME.fullmatch(wake_time) is None or sleep_start == wake_time:
        raise CompanionRepositoryError("routine times are invalid")
    return {"enabled": enabled, "sleep_start": sleep_start, "wake_time": wake_time}


class CompanionRoutineService:
    def __init__(self, repository: CompanionRepository, *, now: Callable[[], datetime] | None = None) -> None:
        self.repository = repository
        self.now = now or (lambda: datetime.now(timezone.utc))

    def get(self) -> CompanionRoutineSnapshot:
        self.repository.initialize()
        stored = self.repository.get_setting(ROUTINE_SETTING_ID)
        if stored is None:
            return CompanionRoutineSnapshot(**DEFAULT_ROUTINE, revision=0, updated_at=None)
        settings = validate_routine_settings(stored.payload)
        return CompanionRoutineSnapshot(**settings, revision=stored.revision, updated_at=stored.updated_at)

    def save(self, *, expected_revision: int, settings: object) -> CompanionRoutineSnapshot:
        self.repository.initialize()
        clean = validate_routine_settings(settings)
        stored = self.repository.save_setting(
            setting_id=ROUTINE_SETTING_ID,
            expected_revision=expected_revision,
            payload=clean,
            updated_at=self._now().isoformat(),
        )
        return CompanionRoutineSnapshot(**clean, revision=stored.revision, updated_at=stored.updated_at)

    def claim_morning(self, local_day: str) -> bool:
        self.repository.initialize()
        return self.repository.claim_daily_setting(
            setting_id=MORNING_CLAIM_SETTING_ID,
            local_day=local_day,
            updated_at=self._now().isoformat(),
        )

    def local_day(self) -> str:
        return self._now().astimezone().date().isoformat()

    def _now(self) -> datetime:
        value = self.now()
        if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
            raise CompanionRepositoryError("routine clock must use UTC")
        return value
