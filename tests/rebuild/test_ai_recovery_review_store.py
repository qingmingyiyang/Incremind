from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

import pytest

from core.ai_kernel import (
    InMemoryTurnStateStore,
    RecoveryDecision,
    RecoveryReviewAuthorization,
    RunLeaseRevoked,
    SQLiteAITurnStore,
)


ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 8, 24, 8, 0, tzinfo=timezone.utc)
AUTH = RecoveryReviewAuthorization(
    actor_id="local-operator",
    boundary_outcome="ask",
    boundary_reason_codes=("guarded_mutation_requires_approval",),
    policy_revision=7,
    human_confirmed=True,
)


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path: Path):
    return InMemoryTurnStateStore() if request.param == "memory" else SQLiteAITurnStore(tmp_path / "turns.sqlite3")


def test_quarantine_creates_opaque_public_review_and_internal_request_lookup(store) -> None:
    turn_id, token = _quarantined_turn(store, 1)
    reviews = store.list_recovery_reviews(project_id="project-alpha")
    assert len(reviews) == 1
    review = reviews[0]
    assert review.review_id.startswith("review-")
    assert review.status == "quarantined" and review.revision == 1
    assert {field for field in review.__dataclass_fields__} == {
        "review_id", "project_id", "status", "revision", "reason_code", "created_at", "updated_at",
    }
    assert "owner" not in repr(review).lower() and "generation" not in repr(review).lower()
    request = store.get_recovery_review_request(review.review_id)
    assert request is not None and request["turn_id"] == turn_id
    assert store.get_recovery_review("review-no-such") is None
    assert store.get_run_lease(turn_id).token == token  # type: ignore[union-attr]


def test_confirm_is_cas_fenced_and_queues_server_issued_generation(store) -> None:
    turn_id, old_token = _quarantined_turn(store, 2)
    review = store.list_recovery_reviews()[0]
    confirmed = store.confirm_no_effect_and_queue_recovery_review(
        review.review_id, expected_revision=review.revision, authorization=AUTH, observed_at=NOW + timedelta(seconds=2),
    )
    assert confirmed is not None and confirmed.status == "resume_queued" and confirmed.revision == 2
    assert store.confirm_no_effect_and_queue_recovery_review(
        review.review_id, expected_revision=review.revision, authorization=AUTH, observed_at=NOW + timedelta(seconds=3),
    ) is None
    item = store.claim_safe_recovery_queue(now=NOW + timedelta(seconds=4), stale_after=NOW + timedelta(seconds=10), limit=1)[0]
    assert item.turn_id == turn_id and item.prior_generation == 1 and item.run_lease.generation == 2
    assert item.reason_code == "ai.recovery_manual_confirmed_no_effect"
    if isinstance(store, SQLiteAITurnStore):
        with pytest.raises(RunLeaseRevoked):
            store.append(_event(turn_id, 2, "context.resolved", "running"), expected_sequence=1, run_lease=old_token)
    else:
        assert store.assert_active_run_lease(old_token) is None
    assert store.complete_recovery_queue(item, observed_at=NOW + timedelta(seconds=5))
    finished = store.get_recovery_review(review.review_id)
    assert finished is not None and finished.status == "turn_completed" and finished.revision == 3


@pytest.mark.parametrize(("result_status", "review_status"), [
    ("completed", "turn_completed"), ("failed", "turn_failed"),
    ("cancelled", "turn_cancelled"), ("waiting_approval", "waiting_approval"),
])
def test_manual_completion_preserves_actual_turn_result_status(store, result_status: str, review_status: str) -> None:
    _quarantined_turn(store, 20)
    review = store.list_recovery_reviews()[0]
    assert store.confirm_no_effect_and_queue_recovery_review(
        review.review_id, expected_revision=1, authorization=AUTH, observed_at=NOW + timedelta(seconds=2),
    ) is not None
    item = store.claim_safe_recovery_queue(now=NOW + timedelta(seconds=3), stale_after=NOW + timedelta(seconds=10), limit=1)[0]
    assert store.complete_recovery_queue(item, observed_at=NOW + timedelta(seconds=4), result_status=result_status)
    result = store.get_recovery_review(review.review_id)
    assert result is not None and result.status == review_status and result.revision == 3


def test_keep_and_confirm_fail_closed_when_event_identity_or_revision_drifts(tmp_path: Path) -> None:
    store = SQLiteAITurnStore(tmp_path / "turns.sqlite3")
    turn_id, _token = _quarantined_turn(store, 3)
    review = store.list_recovery_reviews()[0]
    store.append(_event(turn_id, 2, "context.resolved", "running"), expected_sequence=1)
    assert store.keep_recovery_review(review.review_id, expected_revision=1, authorization=AUTH, observed_at=NOW) is None
    assert store.confirm_no_effect_and_queue_recovery_review(review.review_id, expected_revision=1, authorization=AUTH, observed_at=NOW) is None


def test_sqlite_double_decision_has_one_winner_and_survives_restart(tmp_path: Path) -> None:
    database = tmp_path / "turns.sqlite3"
    store = SQLiteAITurnStore(database)
    _quarantined_turn(store, 4)
    review = store.list_recovery_reviews()[0]
    def decide():
        return SQLiteAITurnStore(database).keep_recovery_review(
            review.review_id, expected_revision=1, authorization=AUTH, observed_at=NOW + timedelta(seconds=1),
        )
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: decide(), range(2)))
    assert sum(result is not None for result in results) == 1
    restored = SQLiteAITurnStore(database).get_recovery_review(review.review_id)
    assert restored is not None and restored.revision == 2 and restored.status == "kept_quarantined"


@pytest.mark.parametrize("actions", [
    ("confirm", "confirm"),
    ("confirm", "keep"),
])
def test_sqlite_competing_manual_actions_have_exactly_one_winner(tmp_path: Path, actions: tuple[str, str]) -> None:
    database = tmp_path / "turns.sqlite3"
    store = SQLiteAITurnStore(database)
    turn_id, _token = _quarantined_turn(store, 40)
    review = store.list_recovery_reviews()[0]

    def decide(action: str):
        current = SQLiteAITurnStore(database)
        method = (
            current.confirm_no_effect_and_queue_recovery_review
            if action == "confirm" else current.keep_recovery_review
        )
        return method(
            review.review_id,
            expected_revision=1,
            authorization=AUTH,
            observed_at=NOW + timedelta(seconds=1),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(decide, actions))
    assert sum(result is not None for result in results) == 1
    restored = SQLiteAITurnStore(database).get_recovery_review(review.review_id)
    assert restored is not None and restored.revision == 2
    with sqlite3.connect(database) as connection:
        queued = connection.execute(
            "SELECT COUNT(*) FROM ai_turn_recovery_queue WHERE turn_id=? AND status='pending'",
            (turn_id,),
        ).fetchone()[0]
    if restored.status == "resume_queued":
        assert queued == 1 and SQLiteAITurnStore(database).get_run_lease(turn_id).status == "recovery_required"  # type: ignore[union-attr]
    else:
        assert restored.status == "kept_quarantined" and queued == 0
        assert SQLiteAITurnStore(database).get_run_lease(turn_id).status == "quarantined"  # type: ignore[union-attr]


def test_worker_unknown_quarantine_creates_private_review_audit_without_tokens_or_payload(tmp_path: Path) -> None:
    database = tmp_path / "turns.sqlite3"
    store = SQLiteAITurnStore(database)
    turn_id, _token = _safe_recovery_turn(store, 5)
    item = store.claim_safe_recovery_queue(now=NOW + timedelta(seconds=2), stale_after=NOW + timedelta(seconds=10), limit=1)[0]
    assert store.quarantine_recovery_queue(item, reason_code="ai.recovery_execution_unknown", observed_at=NOW + timedelta(seconds=3))
    review = SQLiteAITurnStore(database).list_recovery_reviews()[0]
    assert review.reason_code == "ai.recovery_execution_unknown"
    with sqlite3.connect(database) as connection:
        review_columns = {row[1] for row in connection.execute("PRAGMA table_info(ai_turn_recovery_reviews)")}
        audit_columns = {row[1] for row in connection.execute("PRAGMA table_info(ai_turn_recovery_review_audit)")}
        assert not ({"owner_id", "payload", "payload_ref", "token"} & review_columns)
        assert not ({"owner_id", "payload", "payload_ref", "token"} & audit_columns)
    assert turn_id not in repr(review)


def test_manual_recovery_unknown_marks_source_failed_then_creates_new_review(store) -> None:
    _quarantined_turn(store, 7)
    source = store.list_recovery_reviews()[0]
    assert store.confirm_no_effect_and_queue_recovery_review(
        source.review_id, expected_revision=1, authorization=AUTH, observed_at=NOW + timedelta(seconds=2),
    ) is not None
    item = store.claim_safe_recovery_queue(now=NOW + timedelta(seconds=3), stale_after=NOW + timedelta(seconds=10), limit=1)[0]
    assert store.quarantine_recovery_queue(item, reason_code="ai.recovery_execution_unknown", observed_at=NOW + timedelta(seconds=4))
    failed = store.get_recovery_review(source.review_id)
    assert failed is not None and failed.status == "resume_failed" and failed.revision == 3
    current = [review for review in store.list_recovery_reviews() if review.review_id != source.review_id]
    assert len(current) == 1 and current[0].status == "quarantined" and current[0].reason_code == "ai.recovery_execution_unknown"


def test_authorization_rejects_deny_or_unconfirmed_ask(store) -> None:
    _quarantined_turn(store, 6)
    review = store.list_recovery_reviews()[0]
    deny = RecoveryReviewAuthorization("local-operator", "deny", ("profile_explicit_deny",), 7, True)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="outcome"):
        store.keep_recovery_review(review.review_id, expected_revision=1, authorization=deny, observed_at=NOW)
    unconfirmed = RecoveryReviewAuthorization("local-operator", "ask", ("guarded_mutation_requires_approval",), 7, False)
    with pytest.raises(ValueError, match="ask"):
        store.keep_recovery_review(review.review_id, expected_revision=1, authorization=unconfirmed, observed_at=NOW)


def _quarantined_turn(store, index: int):
    request = _request(index)
    turn_id = store.claim_turn(request)[0]
    token = store.try_acquire_run_lease(turn_id, "old-owner", now=NOW, stale_after=NOW)
    assert token is not None
    event = _event(turn_id, 1, "turn.accepted", "accepted")
    if isinstance(store, SQLiteAITurnStore):
        store.append(event, expected_sequence=0, run_lease=token)
    assert store.mark_run_lease_stale(token, now=NOW) is not None
    decision = RecoveryDecision(turn_id, 1, "quarantine", "ai.recovery_tool_effect_unknown", 1, str(event["event_id"]), "turn.accepted")
    assert store.record_recovery_decision(decision, observed_at=NOW + timedelta(seconds=1))
    return turn_id, token


def _safe_recovery_turn(store, index: int):
    request = _request(index)
    turn_id = store.claim_turn(request)[0]
    token = store.try_acquire_run_lease(turn_id, "old-owner", now=NOW, stale_after=NOW)
    assert token is not None
    event = _event(turn_id, 1, "turn.accepted", "accepted")
    if isinstance(store, SQLiteAITurnStore):
        store.append(event, expected_sequence=0, run_lease=token)
    assert store.mark_run_lease_stale(token, now=NOW) is not None
    decision = RecoveryDecision(turn_id, 1, "safe_resume", "ai.recovery_no_effect_started", 1, str(event["event_id"]), "turn.accepted")
    assert store.record_recovery_decision(decision, observed_at=NOW + timedelta(seconds=1))
    return turn_id, token


def _request(index: int) -> dict[str, object]:
    request = json.loads((ROOT / "core-contracts" / "ai" / "fixtures" / "turn-request" / "valid-project-answer.json").read_text(encoding="utf-8"))
    suffix = f"{index:032x}"[-32:]
    request.update({"turn_id": f"turn-{suffix}", "session_id": f"session-{suffix}", "operation_id": f"operation-{suffix}", "idempotency_key": f"turn-key-{suffix}"})
    return request


def _event(turn_id: str, sequence: int, kind: str, status: str) -> dict[str, object]:
    return {
        "schema_version": "1.0.0", "sequence": sequence, "event_id": f"event-{sequence}-{uuid4().hex}",
        "turn_id": turn_id, "session_id": "session-" + turn_id.removeprefix("turn-"), "type": kind,
        "actor": "kernel", "occurred_at": NOW.isoformat(),
        "correlation": {"operation_id": "op-" + turn_id.removeprefix("turn-"), "model_request_id": None, "tool_call_id": None, "step_id": None},
        "data": {"status": status, "summary": "state", "capability_id": None, "payload_ref": None, "receipt_ref": None, "evidence_refs": [], "error_code": None, "retryable": False},
    }
