from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from core.companion_core import CompanionConflict, CompanionIntegrityError, CompanionRepository, CompanionRepositoryError
from core.companion_core.routine import CompanionRoutineService, DEFAULT_ROUTINE, validate_routine_settings


def test_routine_defaults_save_and_cas(tmp_path) -> None:
    service = CompanionRoutineService(CompanionRepository.at_data_root(tmp_path))
    initial = service.get()
    assert initial.as_dict() == DEFAULT_ROUTINE
    assert initial.revision == 0
    saved = service.save(expected_revision=0, settings={"enabled": False, "sleep_start": "22:15", "wake_time": "06:45"})
    assert saved.revision == 1
    assert service.get() == saved
    with pytest.raises(CompanionConflict):
        service.save(expected_revision=0, settings=DEFAULT_ROUTINE)


@pytest.mark.parametrize("value", [
    {},
    {"enabled": True, "sleep_start": "23:00", "wake_time": "23:00"},
    {"enabled": True, "sleep_start": "24:00", "wake_time": "07:00"},
    {"enabled": 1, "sleep_start": "23:00", "wake_time": "07:00"},
    {"enabled": True, "sleep_start": "23:00", "wake_time": "07:00", "path": "C:/secret"},
])
def test_routine_validation_rejects_unsafe_or_ambiguous_values(value) -> None:
    with pytest.raises(CompanionRepositoryError):
        validate_routine_settings(value)


def test_morning_claim_is_atomic_per_local_day(tmp_path) -> None:
    service = CompanionRoutineService(CompanionRepository.at_data_root(tmp_path))
    assert service.claim_morning("2026-07-20") is True
    assert service.claim_morning("2026-07-20") is False
    assert service.claim_morning("2026-07-21") is True
    with pytest.raises(CompanionRepositoryError):
        service.claim_morning("2026-99-99")


def test_routine_uses_the_injected_utc_clock_for_writes_and_local_day(tmp_path) -> None:
    instant = datetime(2026, 7, 23, 23, 59, 58, tzinfo=timezone.utc)
    service = CompanionRoutineService(CompanionRepository.at_data_root(tmp_path), now=lambda: instant)
    saved = service.save(expected_revision=0, settings=DEFAULT_ROUTINE)
    assert saved.updated_at == instant.isoformat()
    assert service.local_day() == instant.astimezone().date().isoformat()
    assert service.claim_morning(service.local_day()) is True

    non_utc = CompanionRoutineService(CompanionRepository.at_data_root(tmp_path / "non-utc"), now=lambda: datetime(2026, 7, 23))
    with pytest.raises(CompanionRepositoryError, match="UTC"):
        non_utc.local_day()


def test_corrupt_stored_routine_fails_closed(tmp_path) -> None:
    repository = CompanionRepository.at_data_root(tmp_path)
    repository.initialize()
    connection = sqlite3.connect(repository.database_path)
    connection.execute(
        "INSERT INTO companion_settings (id, revision, payload_json, updated_at) VALUES ('routine', 1, ?, '2026-07-20T00:00:00+00:00')",
        ('{"enabled":true,"sleep_start":"23:00"}',),
    )
    connection.commit()
    connection.close()
    with pytest.raises(CompanionRepositoryError):
        CompanionRoutineService(repository).get()
