from __future__ import annotations

import pytest

from core.effect_log import (
    EffectClass, EffectIntent, EffectLog, EffectPurpose, EffectRunner,
    EffectState, InvalidEffectTransition,
)


def _intent(step: str) -> EffectIntent:
    return EffectIntent(
        session_id="session", turn_id="turn", root_id="root", parent_id=None,
        step_key=step, kind="model_call", effect_class=EffectClass.AT_MOST_ONCE,
        purpose=EffectPurpose.PRIMARY, intent_ref="payload:intent",
        gate_decision_id="gate:decision", rev_set={"policy": "p1"},
        payload={"source": "controlled"}, idem_key=step,
    )


def _renew(log: EffectLog, runner: EffectRunner, effect, *, expiry: float) -> None:
    with log._connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        runner.renew(log.get_in_connection(connection, effect.operation_id), now=100, lease_expires_at=expiry, connection=connection)
        connection.commit()


def test_external_same_generation_renewal_settles(tmp_path, monkeypatch) -> None:
    import core.effect_log.core as core
    clock = [0.0]
    monkeypatch.setattr(core.time, "monotonic", lambda: clock[0])
    log = EffectLog(tmp_path / "effects.sqlite3")
    runner = EffectRunner(log, owner_id="runner", lease_seconds=1)
    planned, _ = log.plan(_intent("same-generation"), now=100)

    def handler(effect):
        _renew(log, runner, effect, expiry=200)
        clock[0] = 2.0
        return "receipt:success"

    assert runner.execute_planned(planned.operation_id, handler, now=100).state is EffectState.SETTLED_OK


def test_same_owner_new_generation_is_rejected(tmp_path, monkeypatch) -> None:
    import core.effect_log.core as core
    clock = [0.0]
    monkeypatch.setattr(core.time, "monotonic", lambda: clock[0])
    log = EffectLog(tmp_path / "effects.sqlite3")
    runner = EffectRunner(log, owner_id="runner", lease_seconds=10)
    planned, _ = log.plan(_intent("new-generation"), now=100)

    def handler(effect):
        clock[0] = 11.0
        with log._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            taken = log.renew_or_take_over_lease_in_connection(
                connection, effect.operation_id, lease_owner="runner",
                lease_expires_at=200, now=111,
            )
            connection.commit()
        assert taken.attempt == effect.attempt + 1
        return "receipt:wrong-generation"

    with pytest.raises(InvalidEffectTransition, match="generation drifted"):
        runner.execute_planned(planned.operation_id, handler, now=100)


def test_expired_without_renewal_is_rejected(tmp_path, monkeypatch) -> None:
    import core.effect_log.core as core
    clock = [0.0]
    monkeypatch.setattr(core.time, "monotonic", lambda: clock[0])
    log = EffectLog(tmp_path / "effects.sqlite3")
    runner = EffectRunner(log, owner_id="runner", lease_seconds=1)
    planned, _ = log.plan(_intent("expired"), now=100)

    def handler(_effect):
        clock[0] = 2.0
        return "receipt:late"

    with pytest.raises(InvalidEffectTransition, match="expired"):
        runner.execute_planned(planned.operation_id, handler, now=100)
    assert log.get(planned.operation_id).state is EffectState.INFLIGHT


def test_external_state_change_is_rejected(tmp_path) -> None:
    log = EffectLog(tmp_path / "effects.sqlite3")
    runner = EffectRunner(log, owner_id="runner", lease_seconds=10)
    planned, _ = log.plan(_intent("state-change"), now=100)

    def handler(effect):
        log.transition(effect.operation_id, expected=EffectState.INFLIGHT, target=EffectState.ABANDONED, now=100, error_ref="error:abandoned")
        return "receipt:late"

    with pytest.raises(InvalidEffectTransition, match="generation drifted|not owned"):
        runner.execute_planned(planned.operation_id, handler, now=100)
    assert log.get(planned.operation_id).state is EffectState.ABANDONED


def test_lock_wait_expiry_is_checked_after_fresh_read(tmp_path, monkeypatch) -> None:
    import core.effect_log.core as core
    clock = [0.0]
    monkeypatch.setattr(core.time, "monotonic", lambda: clock[0])
    log = EffectLog(tmp_path / "effects.sqlite3")
    runner = EffectRunner(log, owner_id="runner", lease_seconds=1)
    planned, _ = log.plan(_intent("lock-wait"), now=100)
    original_read = log.get_in_connection
    handler_returned = [False]

    def read_after_lock(connection, operation_id):
        result = original_read(connection, operation_id)
        if handler_returned[0]:
            clock[0] = 2.0
        return result

    monkeypatch.setattr(log, "get_in_connection", read_after_lock)

    def handler(_effect):
        handler_returned[0] = True
        return "receipt:late"

    with pytest.raises(InvalidEffectTransition, match="expired"):
        runner.execute_planned(planned.operation_id, handler, now=100)
