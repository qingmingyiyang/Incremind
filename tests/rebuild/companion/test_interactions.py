from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from core.companion_core import CompanionConflict, CompanionRepository, CompanionRepositoryError


NOW = datetime(2026, 7, 20, 0, 0, tzinfo=timezone.utc)


def test_petting_interaction_is_fixed_idempotent_and_does_not_mutate_economy(tmp_path) -> None:
    repository = CompanionRepository(tmp_path / "companion.sqlite3", now=lambda: NOW)
    repository.initialize()
    before = repository.wallet_integrity()

    first = repository.record_interaction(event_id="gesture:petting:alpha", kind="petting")
    replay = repository.record_interaction(event_id="gesture:petting:alpha", kind="petting")

    assert first.replayed is False
    assert replay.replayed is True
    assert replay.occurred_at == first.occurred_at == NOW.isoformat()
    assert repository.wallet_integrity() == before
    with sqlite3.connect(repository.database_path) as connection:
        assert connection.execute("SELECT affinity, mood, coins FROM companion_state").fetchone() == (0, "normal", 0)
        assert connection.execute(
            "SELECT event_id, kind, value_json, expires_at FROM companion_interaction_events"
        ).fetchall() == [("gesture:petting:alpha", "petting", "{}", None)]


def test_interaction_rejects_unknown_kind_invalid_id_and_conflicting_reuse(tmp_path) -> None:
    repository = CompanionRepository(tmp_path / "companion.sqlite3", now=lambda: NOW)
    repository.record_interaction(event_id="gesture:petting:one", kind="petting")

    with pytest.raises(CompanionRepositoryError, match="kind"):
        repository.record_interaction(event_id="gesture:feeding:one", kind="feeding")
    with pytest.raises(CompanionRepositoryError, match="event_id"):
        repository.record_interaction(event_id="INVALID ID", kind="petting")
    with sqlite3.connect(repository.database_path) as connection:
        connection.execute(
            "UPDATE companion_interaction_events SET value_json = ? WHERE event_id = ?",
            ('{"affinity":100}', "gesture:petting:one"),
        )
    with pytest.raises(CompanionConflict, match="different input"):
        repository.record_interaction(event_id="gesture:petting:one", kind="petting")
