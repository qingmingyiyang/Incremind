import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from core.companion_core import CompanionConflict, CompanionRepository, CompanionRepositoryError
from core.companion_core.state import CompanionStateReducer, companion_chat_state_context, load_economy_rules


RULES = Path(__file__).parents[3] / "config" / "companion" / "economy-rules.json"


class Clock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs) -> None:
        self.value += timedelta(**kwargs)


def reducer_for(tmp_path, clock=None, rules=RULES):
    return CompanionStateReducer(
        CompanionRepository.at_data_root(tmp_path), rules_path=rules,
        now=clock or (lambda: datetime(2026, 7, 20, 0, 0, tzinfo=timezone.utc)),
        local_day=lambda value: value.date().isoformat(),
    )


def test_daily_check_in_is_fixed_idempotent_and_append_only(tmp_path) -> None:
    service = reducer_for(tmp_path)
    first = service.daily_check_in()
    replay = service.daily_check_in()
    assert first.coin_delta == 10 and first.snapshot.coins == 10
    assert replay.replayed is True and replay.action_id == first.action_id
    assert service.wallet()[0].reason == "每日签到"
    assert service.repository.wallet_integrity().consistent is True


def test_command_allowlist_cooldown_daily_limit_and_clock_rollback(tmp_path) -> None:
    clock = Clock(datetime(2026, 7, 20, 0, 0, tzinfo=timezone.utc))
    service = reducer_for(tmp_path, clock)
    service.apply(command="petting", idempotency_key="pet:1")
    with pytest.raises(CompanionConflict, match="cooling down"):
        service.apply(command="petting", idempotency_key="pet:2")
    clock.advance(seconds=60)
    service.apply(command="petting", idempotency_key="pet:2")
    clock.value -= timedelta(minutes=2)
    with pytest.raises(CompanionConflict, match="moved backwards"):
        service.apply(command="chat", idempotency_key="chat:rollback")
    with pytest.raises(CompanionRepositoryError, match="not allowed"):
        service.apply(command="give_me_9999", idempotency_key="bad:1")


def test_affinity_unlocks_are_emitted_once_and_mood_is_derived(tmp_path) -> None:
    clock = Clock(datetime(2026, 7, 20, 0, 0, tzinfo=timezone.utc))
    service = reducer_for(tmp_path, clock)
    unlocks = []
    for index in range(10):
        result = service.apply(command="petting", idempotency_key=f"pet:{index}")
        unlocks.extend(result.unlocks)
        clock.advance(seconds=60)
    for index in range(5):
        result = service.apply(command="chat", idempotency_key=f"chat:{index}")
        unlocks.extend(result.unlocks)
    assert unlocks == [25]
    assert result.snapshot.affinity == 25 and result.snapshot.affinity_level == 1
    assert result.snapshot.mood == "normal"
    assert service.apply(command="chat", idempotency_key="chat:4").unlocks == ()


def test_rule_upgrade_replays_original_receipt_and_negative_balance_rolls_back(tmp_path) -> None:
    clock = Clock(datetime(2026, 7, 20, 0, 0, tzinfo=timezone.utc))
    first = reducer_for(tmp_path, clock)
    original = first.apply(command="chat", idempotency_key="chat:stable")
    upgraded_path = tmp_path / "rules-v2.json"
    rules = json.loads(RULES.read_text(encoding="utf-8"))
    rules["schema_version"] = 2
    rules["commands"]["chat"]["affinity"] = 3
    rules["commands"]["spend"] = {"affinity": 0, "mood": 0, "coins": -10, "cooldown_seconds": 0, "daily_limit": 1, "reason": "测试扣减"}
    upgraded_path.write_text(json.dumps(rules), encoding="utf-8")
    upgraded = reducer_for(tmp_path, clock, upgraded_path)
    replay = upgraded.apply(command="chat", idempotency_key="chat:stable")
    assert replay.replayed is True and replay.rule_version == 1 and replay.affinity_delta == original.affinity_delta
    with pytest.raises(CompanionConflict, match="negative"):
        upgraded.apply(command="spend", idempotency_key="spend:1")
    assert upgraded.snapshot().coins == 0


def test_concurrent_daily_check_in_creates_one_ledger_entry(tmp_path) -> None:
    def apply_once():
        return reducer_for(tmp_path).daily_check_in()
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: apply_once(), range(6)))
    assert sum(not item.replayed for item in results) == 1
    service = reducer_for(tmp_path)
    assert service.snapshot().coins == 10 and len(service.wallet()) == 1


def test_rules_fail_closed_on_unknown_fields(tmp_path) -> None:
    path = tmp_path / "bad.json"
    path.write_text('{"schema_version":1,"affinity_thresholds":[0,25,50,75,100],"mood":{"sad_max":-25,"happy_min":25},"commands":{},"path":"C:/secret"}', encoding="utf-8")
    with pytest.raises(CompanionRepositoryError, match="schema"):
        load_economy_rules(path)


def test_daily_limit_resets_on_next_local_day_without_replaying_old_keys(tmp_path) -> None:
    clock = Clock(datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc))
    service = reducer_for(tmp_path, clock)
    for index in range(20):
        service.apply(command="chat", idempotency_key=f"day1:{index}")
    with pytest.raises(CompanionConflict, match="daily limit"):
        service.apply(command="chat", idempotency_key="day1:overflow")
    clock.advance(days=1)
    next_day = service.apply(command="chat", idempotency_key="day2:0")
    assert next_day.local_day == "2026-07-21"
    with pytest.raises(CompanionConflict, match="different input"):
        service.apply(command="petting", idempotency_key="day2:0")


def test_transient_mood_projection_expires_without_persisting_system_pressure(tmp_path) -> None:
    clock = Clock(datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc))
    service = reducer_for(tmp_path, clock)
    active = service.project(transient_modifier=-20, expires_at=clock.value + timedelta(seconds=30))
    assert active.transient_modifier == -20 and active.effective_mood_score == -20
    assert service.snapshot().mood_score == 0
    clock.advance(seconds=31)
    expired = service.project(transient_modifier=-20, expires_at=clock.value - timedelta(seconds=1))
    assert expired.transient_modifier == 0 and expired.effective_mood == "normal"


@pytest.mark.parametrize(
    ("mood", "reply_style"),
    (("sad", "brief"), ("normal", "balanced"), ("happy", "warm")),
)
def test_chat_state_context_is_owned_by_the_mood_domain(mood, reply_style) -> None:
    projection = type("Projection", (), {"effective_mood": mood})()
    assert companion_chat_state_context(projection) == {"mood": mood, "reply_style": reply_style}


def test_chat_state_context_rejects_an_unknown_mood() -> None:
    projection = type("Projection", (), {"effective_mood": "excited"})()
    with pytest.raises(CompanionRepositoryError, match="mood is invalid"):
        companion_chat_state_context(projection)


def test_chat_affect_uses_fixed_low_delta_idempotency_and_daily_limit(tmp_path) -> None:
    service = reducer_for(tmp_path)
    assert service.apply_chat_affect(signal="neutral", request_id="neutral") is None
    first = service.apply_chat_affect(signal="positive", request_id="one")
    replay = service.apply_chat_affect(signal="positive", request_id="one")
    assert first.mood_delta == 1 and replay.replayed is True and service.snapshot().mood_score == 1
    for index in range(8): service.apply_chat_affect(signal="negative", request_id=f"negative:{index}")
    with pytest.raises(CompanionConflict, match="daily limit"):
        service.apply_chat_affect(signal="negative", request_id="overflow")
    with pytest.raises(CompanionRepositoryError, match="signal"):
        service.apply_chat_affect(signal={"label": "positive", "delta": 99}, request_id="bad")


@pytest.mark.parametrize(("starting_score", "expected"), ((8, 6), (-8, -6), (0, 0)))
def test_daily_mood_moves_two_points_toward_normal_per_local_day(tmp_path, starting_score, expected) -> None:
    clock = Clock(datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc))
    service = reducer_for(tmp_path, clock)
    service.reconcile_daily_mood()  # The first observation establishes the durable cursor.
    if starting_score:
        service.repository.apply_state_action(
            action_id="act_seed_mood", idempotency_key="seed:mood", command="seed_mood",
            subject_id=None, local_day="2026-07-20", rule_version=1,
            affinity_delta=0, mood_delta=starting_score, coin_delta=0,
            daily_limit=1, cooldown_seconds=0, affinity_thresholds=service.rules.affinity_thresholds,
            sad_max=service.rules.sad_max, happy_min=service.rules.happy_min,
            wallet_reason="测试", created_at=clock.value.isoformat(),
        )
    clock.advance(days=1)
    result = service.reconcile_daily_mood()
    assert result.mood_delta == expected - starting_score
    assert result.snapshot.mood_score == expected
    assert service.reconcile_daily_mood().replayed is True


def test_daily_mood_catches_up_missed_days_without_overshooting_zero(tmp_path) -> None:
    clock = Clock(datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc))
    service = reducer_for(tmp_path, clock)
    service.reconcile_daily_mood()
    service.repository.apply_state_action(
        action_id="act_seed_positive", idempotency_key="seed:positive", command="seed_positive",
        subject_id=None, local_day="2026-07-20", rule_version=1,
        affinity_delta=0, mood_delta=5, coin_delta=0, daily_limit=1, cooldown_seconds=0,
        affinity_thresholds=service.rules.affinity_thresholds, sad_max=service.rules.sad_max,
        happy_min=service.rules.happy_min, wallet_reason="测试", created_at=clock.value.isoformat(),
    )
    clock.advance(days=10)
    result = service.reconcile_daily_mood()
    assert result.mood_delta == -5
    assert result.snapshot.mood_score == 0


def test_daily_mood_is_concurrent_restart_safe_and_rejects_calendar_rollback(tmp_path) -> None:
    clock = Clock(datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc))
    reducer_for(tmp_path, clock).reconcile_daily_mood()
    clock.advance(days=1)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: reducer_for(tmp_path, clock).reconcile_daily_mood(), range(6)))
    assert sum(not item.replayed for item in results) == 1
    restarted = reducer_for(tmp_path, clock)
    assert restarted.reconcile_daily_mood().replayed is True
    clock.value -= timedelta(days=2)
    with pytest.raises(CompanionConflict, match="moved backwards"):
        restarted.reconcile_daily_mood()


def test_daily_mood_rejects_utc_clock_rollback_within_same_local_day(tmp_path) -> None:
    clock = Clock(datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc))
    service = reducer_for(tmp_path, clock)
    service.reconcile_daily_mood()
    clock.advance(hours=-1)
    with pytest.raises(CompanionConflict, match="clock moved backwards"):
        service.reconcile_daily_mood()


def test_snapshot_reconciles_daily_mood_before_projection(tmp_path) -> None:
    clock = Clock(datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc))
    service = reducer_for(tmp_path, clock)
    first = service.snapshot()
    assert first.mood_score == 0 and first.revision == 1
    clock.advance(days=1)
    assert service.project().snapshot.mood_score == 0
    assert service.repository.has_state_action(command="daily_mood_decay", local_day="2026-07-21") is True


def test_daily_mood_uses_local_calendar_across_dst_fall_back(tmp_path) -> None:
    zone = ZoneInfo("America/New_York")
    clock = Clock(datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc))
    service = CompanionStateReducer(
        CompanionRepository.at_data_root(tmp_path), rules_path=RULES, now=clock,
        local_day=lambda value: value.astimezone(zone).date().isoformat(),
    )
    assert service.reconcile_daily_mood().local_day == "2026-11-01"
    service.repository.apply_state_action(
        action_id="act_dst_seed", idempotency_key="seed:dst", command="seed_dst",
        subject_id=None, local_day="2026-11-01", rule_version=1,
        affinity_delta=0, mood_delta=-6, coin_delta=0, daily_limit=1, cooldown_seconds=0,
        affinity_thresholds=service.rules.affinity_thresholds, sad_max=service.rules.sad_max,
        happy_min=service.rules.happy_min, wallet_reason="测试", created_at=clock.value.isoformat(),
    )
    clock.advance(hours=25)
    result = service.reconcile_daily_mood()
    assert result.local_day == "2026-11-02"
    assert result.mood_delta == 2 and result.snapshot.mood_score == -4


def test_daily_mood_catch_up_is_bounded_to_thirty_days(tmp_path) -> None:
    clock = Clock(datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc))
    service = reducer_for(tmp_path, clock)
    service.reconcile_daily_mood()
    service.repository.apply_state_action(
        action_id="act_cap_seed", idempotency_key="seed:cap", command="seed_cap",
        subject_id=None, local_day="2026-01-01", rule_version=1,
        affinity_delta=0, mood_delta=100, coin_delta=0, daily_limit=1, cooldown_seconds=0,
        affinity_thresholds=service.rules.affinity_thresholds, sad_max=service.rules.sad_max,
        happy_min=service.rules.happy_min, wallet_reason="测试", created_at=clock.value.isoformat(),
    )
    clock.advance(days=60)
    result = service.reconcile_daily_mood()
    assert result.mood_delta == -60 and result.snapshot.mood_score == 40
    assert service.reconcile_daily_mood().replayed is True


def test_daily_mood_rolls_back_receipt_when_state_integrity_fails(tmp_path) -> None:
    clock = Clock(datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc))
    service = reducer_for(tmp_path, clock)
    service.reconcile_daily_mood()
    database = service.repository.database_path
    import sqlite3
    connection = sqlite3.connect(database)
    try:
        connection.execute("UPDATE companion_state SET coins=1 WHERE id='current'")
        connection.commit()
    finally:
        connection.close()
    clock.advance(days=1)
    with pytest.raises(CompanionRepositoryError, match="wallet snapshot"):
        service.reconcile_daily_mood()
    connection = sqlite3.connect(database)
    try:
        count = connection.execute(
            "SELECT COUNT(*) FROM companion_state_actions WHERE command='daily_mood_decay' AND local_day='2026-07-21'"
        ).fetchone()[0]
    finally:
        connection.close()
    assert count == 0
