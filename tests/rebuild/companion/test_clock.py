from datetime import datetime, timezone

from core.companion_core.clock import (
    E2E_CLOCK_MODE,
    E2E_CLOCK_MODE_ENV,
    E2E_CLOCK_UTC_ENV,
    FixedCompanionClock,
    SystemCompanionClock,
    build_companion_clock,
)


def test_fixed_clock_is_immutable_utc_and_projects_the_current_local_day() -> None:
    clock = FixedCompanionClock(datetime(2026, 7, 23, 23, 59, 58, tzinfo=timezone.utc))
    assert clock.mode == "e2e_fixed"
    assert clock.now_utc() == datetime(2026, 7, 23, 23, 59, 58, tzinfo=timezone.utc)
    assert clock.now_utc() is clock.now_utc()
    assert clock.local_day(datetime(2026, 7, 24, tzinfo=timezone.utc)) == datetime(2026, 7, 24, tzinfo=timezone.utc).astimezone().date().isoformat()


def test_clock_builder_requires_exact_mode_utc_value_and_no_extra_clock_fields() -> None:
    valid = {E2E_CLOCK_MODE_ENV: E2E_CLOCK_MODE, E2E_CLOCK_UTC_ENV: "2026-07-23T23:59:58Z"}
    assert build_companion_clock(valid).mode == "e2e_fixed"
    for value in (
        {},
        {E2E_CLOCK_MODE_ENV: E2E_CLOCK_MODE},
        {E2E_CLOCK_UTC_ENV: "2026-07-23T23:59:58Z"},
        {E2E_CLOCK_MODE_ENV: "development", E2E_CLOCK_UTC_ENV: "2026-07-23T23:59:58Z"},
        {E2E_CLOCK_MODE_ENV: E2E_CLOCK_MODE, E2E_CLOCK_UTC_ENV: "2026-07-23T23:59:58+08:00"},
        {**valid, "CHRIPTMAS_COMPANION_E2E_CLOCK_SET": "anything"},
        {E2E_CLOCK_MODE_ENV: E2E_CLOCK_MODE, E2E_CLOCK_UTC_ENV: "2026-02-30T23:59:58Z"},
    ):
        assert isinstance(build_companion_clock(value), SystemCompanionClock)
