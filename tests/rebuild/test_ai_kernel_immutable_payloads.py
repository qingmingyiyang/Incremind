from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path

import pytest

from core.ai_kernel import InMemoryTurnPayloadStore, ScopedTurnPayloadView, SQLiteAITurnStore


TURN_ID = "turn-immutable-0123456789"


@pytest.fixture(params=("memory", "sqlite"))
def store(request, tmp_path: Path):
    if request.param == "memory":
        return InMemoryTurnPayloadStore()
    value = SQLiteAITurnStore(tmp_path / "turns.sqlite3")
    value.claim_turn(_turn_request())
    return value


def test_immutable_payload_replay_returns_same_ref_and_defensive_copy(store) -> None:
    payload = {"nested": {"value": "first"}, "items": [1, 2]}
    first = store.get_or_create_immutable_payload(TURN_ID, "skill-snapshot", payload)
    second = store.get_or_create_immutable_payload(
        TURN_ID, "skill-snapshot", {"items": [1, 2], "nested": {"value": "first"}},
    )

    assert second == first
    assert store.get(first) == payload
    assert ScopedTurnPayloadView(store, turn_id=TURN_ID, allowed_refs=(first,)).get(first) == payload
    assert ScopedTurnPayloadView(
        store, turn_id=TURN_ID, allowed_refs=(first,),
    ).get_immutable_payload(TURN_ID, "skill-snapshot") == (first, payload)
    resolved = store.get_immutable_payload(TURN_ID, "skill-snapshot")
    assert resolved == (first, payload)
    resolved[1]["nested"]["value"] = "mutated"
    assert store.get_immutable_payload(TURN_ID, "skill-snapshot") == (first, payload)


def test_immutable_payload_conflict_and_missing_fail_closed_without_affecting_regular_put(store) -> None:
    regular_ref = store.put(TURN_ID, "skill-snapshot", {"source": "regular"})
    immutable_ref = store.get_or_create_immutable_payload(TURN_ID, "skill-snapshot", {"source": "immutable"})

    assert regular_ref != immutable_ref
    assert store.get(regular_ref) == {"source": "regular"}
    with pytest.raises(ValueError, match="immutable payload identity conflict"):
        store.get_or_create_immutable_payload(TURN_ID, "skill-snapshot", {"source": "changed"})
    assert store.get_immutable_payload(TURN_ID, "missing") is None
    assert store.get_immutable_payload(TURN_ID, "skill-snapshot") == (immutable_ref, {"source": "immutable"})


def test_sqlite_immutable_payload_is_atomic_and_replay_safe(tmp_path: Path) -> None:
    database = tmp_path / "turns.sqlite3"
    store = SQLiteAITurnStore(database)
    store.claim_turn(_turn_request())

    def write() -> str:
        return SQLiteAITurnStore(database).get_or_create_immutable_payload(
            TURN_ID, "skill-snapshot", {"revision": 1},
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        refs = tuple(pool.map(lambda _index: write(), range(8)))

    assert len(set(refs)) == 1
    assert SQLiteAITurnStore(database).get_immutable_payload(TURN_ID, "skill-snapshot") == (
        refs[0], {"revision": 1},
    )


def test_sqlite_immutable_reference_can_be_bound_before_turn_admission(tmp_path: Path) -> None:
    store = SQLiteAITurnStore(tmp_path / "turns.sqlite3")

    reference = store.immutable_payload_reference(TURN_ID, "agent-policy-snapshot-v1")
    assert reference == f"crp://session/{TURN_ID}/agent-policy-snapshot-v1"
    assert store.get_immutable_payload(TURN_ID, "agent-policy-snapshot-v1") is None

    store.claim_turn(_turn_request())
    assert store.get_or_create_immutable_payload(
        TURN_ID, "agent-policy-snapshot-v1", {"revision": 1},
    ) == reference
    assert store.immutable_payload_reference(TURN_ID, "agent-policy-snapshot-v1") == reference


def test_immutable_payload_reservation_has_one_winner(store) -> None:
    first_ref, first_created = store.reserve_immutable_payload(
        TURN_ID, "mcp-side-effect-intent", {"invocation_id": "call-1"},
    )
    replay_ref, replay_created = store.reserve_immutable_payload(
        TURN_ID, "mcp-side-effect-intent", {"invocation_id": "call-1"},
    )

    assert first_created is True
    assert replay_created is False
    assert replay_ref == first_ref
    with pytest.raises(ValueError, match="immutable payload identity conflict"):
        store.reserve_immutable_payload(
            TURN_ID, "mcp-side-effect-intent", {"invocation_id": "call-drifted"},
        )


def test_sqlite_immutable_payload_reservation_is_cross_connection_atomic(tmp_path: Path) -> None:
    database = tmp_path / "turns.sqlite3"
    store = SQLiteAITurnStore(database)
    store.claim_turn(_turn_request())

    def reserve() -> tuple[str, bool]:
        return SQLiteAITurnStore(database).reserve_immutable_payload(
            TURN_ID, "mcp-side-effect-intent", {"invocation_id": "call-1"},
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = tuple(pool.map(lambda _index: reserve(), range(8)))

    assert sum(created for _ref, created in results) == 1
    assert len({ref for ref, _created in results}) == 1


def test_sqlite_immutable_payload_reservation_is_cross_process_atomic(tmp_path: Path) -> None:
    database = tmp_path / "turns.sqlite3"
    store = SQLiteAITurnStore(database)
    store.claim_turn(_turn_request())

    with ProcessPoolExecutor(max_workers=4) as pool:
        results = tuple(pool.map(_reserve_mcp_intent, (str(database),) * 8))

    assert sum(created for _ref, created in results) == 1
    assert len({ref for ref, _created in results}) == 1


def _turn_request() -> dict[str, object]:
    return {
        "turn_id": TURN_ID,
        "session_id": "session-immutable-01",
        "operation_id": "operation-immutable-01",
        "idempotency_key": "immutable-payload-key-01",
    }


def _reserve_mcp_intent(database: str) -> tuple[str, bool]:
    return SQLiteAITurnStore(Path(database)).reserve_immutable_payload(
        TURN_ID, "mcp-side-effect-intent", {"invocation_id": "call-1"},
    )
