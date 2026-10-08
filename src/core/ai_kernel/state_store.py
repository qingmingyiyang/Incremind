from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime
from threading import RLock
from uuid import uuid4

from .ports import (
    RecoveryDecision,
    RecoveryQueueItem,
    PublicRecoveryReview,
    RecoveryReviewAuthorization,
    RunLeaseRecord,
    RunLeaseRecoveryDisposition,
    RunLeaseToken,
    TurnReceipt,
    validate_run_lease_record,
    validate_recovery_decision,
    validate_recovery_queue_item,
    validate_recovery_review_authorization,
    validate_recovery_review_identity,
    validate_public_recovery_review,
    validate_run_lease_time,
    validate_run_lease_token,
)


class TurnStateConflict(ValueError):
    pass


class InMemoryTurnStateStore:
    def __init__(self) -> None:
        self._lock = RLock()
        self._requests: dict[str, dict[str, object]] = {}
        self._idempotency: dict[str, str] = {}
        self._pending: dict[str, dict[str, object]] = {}
        self._actions: dict[str, tuple[dict[str, object], TurnReceipt]] = {}
        self._run_leases: dict[str, tuple[str, int]] = {}
        self._run_generations: dict[str, int] = {}
        self._strict_run_leases: dict[str, RunLeaseRecord] = {}
        self._recovery_decisions: dict[tuple[str, int], RecoveryDecision] = {}
        self._recovery_attempts: dict[tuple[str, int], int] = {}
        self._recovery_queue: dict[tuple[str, int], tuple[str, str, int]] = {}
        self._recovery_reviews: dict[str, dict[str, object]] = {}
        self._recovery_review_keys: dict[tuple[str, int], str] = {}
        self._recovery_review_audit: list[dict[str, object]] = []

    def claim_turn(self, request: Mapping[str, object]) -> tuple[str, bool]:
        payload = dict(request)
        key = str(payload["idempotency_key"])
        turn_id = str(payload["turn_id"])
        with self._lock:
            existing_id = self._idempotency.get(key)
            if existing_id is not None:
                if self._requests[existing_id] != payload:
                    raise TurnStateConflict("turn idempotency identity conflict")
                return existing_id, False
            if turn_id in self._requests:
                raise TurnStateConflict("turn identity already exists")
            self._requests[turn_id] = deepcopy(payload)
            self._idempotency[key] = turn_id
            return turn_id, True

    def get_request(self, turn_id: str) -> Mapping[str, object] | None:
        with self._lock:
            request = self._requests.get(turn_id)
            return deepcopy(request) if request is not None else None

    def try_claim_run_lease(self, turn_id: str, owner_id: str) -> int | None:
        with self._lock:
            self._require_turn(turn_id)
            if turn_id in self._run_leases or turn_id in self._strict_run_leases:
                return None
            generation = self._run_generations.get(turn_id, 0) + 1
            self._run_generations[turn_id] = generation
            self._run_leases[turn_id] = (owner_id, generation)
            return generation

    def release_run_lease(self, turn_id: str, owner_id: str, generation: int) -> None:
        with self._lock:
            if self._run_leases.get(turn_id) == (owner_id, generation):
                self._run_leases.pop(turn_id, None)

    def try_acquire_run_lease(
        self, turn_id: str, owner_id: str, *, now: datetime, stale_after: datetime,
    ) -> RunLeaseToken | None:
        _lease_times(now, stale_after)
        with self._lock:
            self._require_turn(turn_id)
            if turn_id in self._strict_run_leases or turn_id in self._run_leases:
                return None
            generation = self._run_generations.get(turn_id, 0) + 1
            self._run_generations[turn_id] = generation
            token = RunLeaseToken(turn_id, owner_id, generation)
            self._strict_run_leases[turn_id] = _lease_record(token, "active", now, now, stale_after)
            return token

    def renew_run_lease(self, token: RunLeaseToken, *, now: datetime, stale_after: datetime) -> RunLeaseRecord | None:
        validate_run_lease_token(token)
        _lease_times(now, stale_after)
        with self._lock:
            current = self._strict_run_leases.get(token.turn_id)
            if current is None or current.token != token or current.status != "active":
                return None
            if now < current.heartbeat_at:
                return None
            updated = _lease_record(token, "active", current.acquired_at, now, stale_after)
            self._strict_run_leases[token.turn_id] = updated
            return updated

    def mark_run_lease_stale(self, token: RunLeaseToken, *, now: datetime) -> RunLeaseRecord | None:
        validate_run_lease_token(token)
        validate_run_lease_time(now, name="run lease stale check time")
        with self._lock:
            current = self._strict_run_leases.get(token.turn_id)
            if current is None or current.token != token or current.status != "active" or now < current.stale_after:
                return None
            updated = _lease_record(token, "recovery_required", current.acquired_at, current.heartbeat_at, current.stale_after)
            self._strict_run_leases[token.turn_id] = updated
            return updated

    def takeover_run_lease(
        self, turn_id: str, *, expected_generation: int, owner_id: str, now: datetime,
        stale_after: datetime, disposition: RunLeaseRecoveryDisposition,
    ) -> RunLeaseRecord | None:
        _lease_times(now, stale_after)
        if disposition not in {"safe", "quarantined"}:
            raise ValueError("run lease recovery disposition is invalid")
        with self._lock:
            current = self._strict_run_leases.get(turn_id)
            if current is None or current.status != "recovery_required" or current.token.generation != expected_generation:
                return None
            if now < current.stale_after:
                return None
            if disposition == "quarantined":
                updated = _lease_record(current.token, "quarantined", current.acquired_at, current.heartbeat_at, current.stale_after)
            else:
                generation = current.token.generation + 1
                self._run_generations[turn_id] = generation
                token = RunLeaseToken(turn_id, owner_id, generation)
                updated = _lease_record(token, "active", now, now, stale_after)
            self._strict_run_leases[turn_id] = updated
            return updated

    def assert_active_run_lease(self, token: RunLeaseToken) -> RunLeaseRecord | None:
        validate_run_lease_token(token)
        with self._lock:
            current = self._strict_run_leases.get(token.turn_id)
            return current if current is not None and current.token == token and current.status == "active" else None

    def get_run_lease(self, turn_id: str) -> RunLeaseRecord | None:
        with self._lock:
            return self._strict_run_leases.get(turn_id)

    def claim_due_run_leases(self, *, now: datetime, limit: int) -> tuple[RunLeaseRecord, ...]:
        validate_run_lease_time(now, name="run lease claim time")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 256:
            raise ValueError("run lease claim limit is invalid")
        with self._lock:
            candidates = [
                record for turn_id, record in self._strict_run_leases.items()
                if (
                    (record.status == "recovery_required" or (record.status == "active" and record.stale_after <= now))
                    and (turn_id, record.token.generation) not in self._recovery_decisions
                )
            ]
            candidates.sort(key=lambda item: (
                self._recovery_attempts.get((item.token.turn_id, item.token.generation), 0),
                item.stale_after,
                item.token.turn_id,
            ))
            claimed: list[RunLeaseRecord] = []
            for record in candidates[:limit]:
                turn_id = record.token.turn_id
                if record.status == "active":
                    record = _lease_record(record.token, "recovery_required", record.acquired_at, record.heartbeat_at, record.stale_after)
                    self._strict_run_leases[turn_id] = record
                key = (turn_id, record.token.generation)
                self._recovery_attempts[key] = self._recovery_attempts.get(key, 0) + 1
                claimed.append(record)
            return tuple(claimed)

    def record_recovery_decision(self, decision: RecoveryDecision, *, observed_at: datetime) -> bool:
        """Apply a trusted snapshot decision in the single-process reference store.

        This state-only test store has no event journal with which to repeat the
        SQLite identity CAS. Production recovery must use SQLiteAITurnStore.
        """
        validate_recovery_decision(decision)
        validate_run_lease_time(observed_at, name="recovery observation time")
        with self._lock:
            record = self._strict_run_leases.get(decision.turn_id)
            key = (decision.turn_id, decision.generation)
            if record is None or record.status != "recovery_required" or record.token.generation != decision.generation or key in self._recovery_decisions:
                return False
            self._recovery_decisions[key] = decision
            if decision.disposition in {"terminal_noop", "waiting_noop"}:
                self._strict_run_leases.pop(decision.turn_id, None)
            elif decision.disposition == "quarantine":
                self._strict_run_leases[decision.turn_id] = _lease_record(record.token, "quarantined", record.acquired_at, record.heartbeat_at, record.stale_after)
                self._create_recovery_review_locked(
                    decision.turn_id, decision.generation, decision.reason_code,
                    decision.last_sequence, decision.last_event_id, decision.last_event_type,
                    observed_at,
                )
            else:
                self._recovery_queue[key] = (decision.reason_code, "pending", 0)
            return True

    def claim_safe_recovery_queue(self, *, now: datetime, stale_after: datetime, limit: int) -> tuple[RecoveryQueueItem, ...]:
        _lease_times(now, stale_after)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 256:
            raise ValueError("recovery queue claim limit is invalid")
        with self._lock:
            claimed: list[RecoveryQueueItem] = []
            for key, (reason, status, attempts) in tuple(self._recovery_queue.items()):
                if len(claimed) >= limit or status != "pending":
                    continue
                decision = self._recovery_decisions.get(key)
                current = self._strict_run_leases.get(key[0])
                manual_review_id = self._recovery_review_keys.get(key)
                manual_review = self._recovery_reviews.get(manual_review_id) if manual_review_id else None
                manual_ready = (
                    reason == "ai.recovery_manual_confirmed_no_effect"
                    and manual_review is not None
                    and manual_review["status"] == "resume_queued"
                )
                scanner_ready = decision is not None and decision.disposition == "safe_resume" and reason == "ai.recovery_no_effect_started"
                if not (manual_ready or scanner_ready) or current is None or current.status != "recovery_required" or current.token.generation != key[1]:
                    continue
                if self._run_generations.get(key[0]) != key[1]:
                    continue
                generation = key[1] + 1
                self._run_generations[key[0]] = generation
                token = RunLeaseToken(key[0], f"recovery-{uuid4().hex}", generation)
                self._strict_run_leases[key[0]] = _lease_record(token, "active", now, now, stale_after)
                self._recovery_queue[key] = (reason, "running", attempts + 1)
                claimed.append(RecoveryQueueItem(key[0], key[1], reason, attempts + 1, token))
            return tuple(claimed)

    def has_pending_safe_recovery(self) -> bool:
        with self._lock:
            return any(
                status == "pending"
                and (
                    (self._recovery_decisions.get(key) is not None
                     and self._recovery_decisions[key].disposition == "safe_resume"
                     and reason == "ai.recovery_no_effect_started")
                    or (reason == "ai.recovery_manual_confirmed_no_effect"
                        and (review_id := self._recovery_review_keys.get(key)) is not None
                        and self._recovery_reviews[review_id]["status"] == "resume_queued")
                )
                and (current := self._strict_run_leases.get(key[0])) is not None
                and current.status == "recovery_required"
                and current.token.generation == key[1]
                for key, (reason, status, _attempts) in self._recovery_queue.items()
            )

    def complete_recovery_queue(self, item: RecoveryQueueItem, *, observed_at: datetime, result_status: str = "completed") -> bool:
        validate_recovery_queue_item(item)
        validate_run_lease_time(observed_at, name="recovery completion time")
        _manual_review_terminal_status(result_status)
        with self._lock:
            value = self._recovery_queue.get((item.turn_id, item.prior_generation))
            current = self._strict_run_leases.get(item.turn_id)
            if value is None or value[1] != "running" or value[2] != item.attempt or current is None or current.token != item.run_lease or current.status != "active": return False
            self._recovery_queue[(item.turn_id, item.prior_generation)] = (value[0], "completed", value[2])
            if value[0] == "ai.recovery_manual_confirmed_no_effect":
                self._finish_manual_review_locked(
                    item.turn_id, item.prior_generation, _manual_review_terminal_status(result_status),
                    f"recovery_turn_{result_status}", observed_at,
                )
            return True

    def quarantine_recovery_queue(self, item: RecoveryQueueItem, *, reason_code: str, observed_at: datetime) -> bool:
        validate_recovery_queue_item(item)
        if reason_code != "ai.recovery_execution_unknown":
            raise ValueError("recovery quarantine reason is invalid")
        validate_run_lease_time(observed_at, name="recovery quarantine time")
        with self._lock:
            value = self._recovery_queue.get((item.turn_id, item.prior_generation))
            current = self._strict_run_leases.get(item.turn_id)
            if value is None or value[1] != "running" or value[2] != item.attempt or current is None or current.token != item.run_lease or current.status != "active": return False
            self._recovery_queue[(item.turn_id, item.prior_generation)] = (value[0], "failed", value[2])
            if current is not None and current.token == item.run_lease and current.status == "active":
                self._strict_run_leases[item.turn_id] = _lease_record(current.token, "quarantined", current.acquired_at, current.heartbeat_at, current.stale_after)
                if value[0] == "ai.recovery_manual_confirmed_no_effect":
                    self._finish_manual_review_locked(
                        item.turn_id, item.prior_generation, "resume_failed", "recovery_execution_unknown", observed_at,
                    )
                prior = self._recovery_decisions.get((item.turn_id, item.prior_generation))
                self._create_recovery_review_locked(
                    item.turn_id, item.run_lease.generation, reason_code,
                    prior.last_sequence if prior else 0,
                    prior.last_event_id if prior else "",
                    prior.last_event_type if prior else "",
                    observed_at,
                )
            return True

    def list_recovery_reviews(self, *, project_id: str | None = None, limit: int = 128) -> tuple[PublicRecoveryReview, ...]:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 256:
            raise ValueError("recovery review list limit is invalid")
        if project_id is not None:
            validate_recovery_review_identity(project_id, name="recovery review project identity")
        with self._lock:
            rows = [item for item in self._recovery_reviews.values() if project_id is None or item["project_id"] == project_id]
            rows.sort(key=lambda item: (item["updated_at"], item["review_id"]), reverse=True)
            return tuple(_public_review(item) for item in rows[:limit])

    def get_recovery_review(self, review_id: str) -> PublicRecoveryReview | None:
        validate_recovery_review_identity(review_id)
        with self._lock:
            item = self._recovery_reviews.get(review_id)
            return _public_review(item) if item is not None else None

    def get_recovery_review_request(self, review_id: str) -> Mapping[str, object] | None:
        validate_recovery_review_identity(review_id)
        with self._lock:
            review = self._recovery_reviews.get(review_id)
            if review is None:
                return None
            request = self._requests.get(str(review["turn_id"]))
            if request is None or _request_project_id(request) != review["project_id"]:
                return None
            return deepcopy(request)

    def keep_recovery_review(self, review_id: str, *, expected_revision: int, authorization: RecoveryReviewAuthorization, observed_at: datetime) -> PublicRecoveryReview | None:
        return self._decide_recovery_review(
            review_id, expected_revision=expected_revision, authorization=authorization,
            observed_at=observed_at, action="keep_quarantined",
        )

    def confirm_no_effect_and_queue_recovery_review(self, review_id: str, *, expected_revision: int, authorization: RecoveryReviewAuthorization, observed_at: datetime) -> PublicRecoveryReview | None:
        if not authorization.human_confirmed:
            raise ValueError("recovery no-effect confirmation is required")
        return self._decide_recovery_review(
            review_id, expected_revision=expected_revision, authorization=authorization,
            observed_at=observed_at, action="confirm_no_effect_and_queue",
        )

    def _decide_recovery_review(self, review_id: str, *, expected_revision: int, authorization: RecoveryReviewAuthorization, observed_at: datetime, action: str) -> PublicRecoveryReview | None:
        validate_recovery_review_identity(review_id)
        validate_recovery_review_authorization(authorization)
        validate_run_lease_time(observed_at, name="recovery review decision time")
        if not isinstance(expected_revision, int) or isinstance(expected_revision, bool) or expected_revision < 1:
            raise ValueError("recovery review expected revision is invalid")
        with self._lock:
            review = self._recovery_reviews.get(review_id)
            if review is None or review["revision"] != expected_revision or review["status"] != "quarantined":
                return None
            current = self._strict_run_leases.get(str(review["turn_id"]))
            request = self._requests.get(str(review["turn_id"]))
            if current is None or current.status != "quarantined" or current.token.generation != review["generation"] or request is None or _request_project_id(request) != review["project_id"]:
                return None
            if action == "confirm_no_effect_and_queue":
                self._strict_run_leases[current.token.turn_id] = _lease_record(current.token, "recovery_required", current.acquired_at, current.heartbeat_at, current.stale_after)
                self._recovery_queue[(current.token.turn_id, current.token.generation)] = ("ai.recovery_manual_confirmed_no_effect", "pending", 0)
                review["status"] = "resume_queued"
            else:
                review["status"] = "kept_quarantined"
            review["revision"] = int(review["revision"]) + 1
            review["updated_at"] = observed_at
            self._recovery_review_audit.append({
                "review_id": review_id, "action": action, "actor_id": authorization.actor_id,
                "boundary_outcome": authorization.boundary_outcome,
                "boundary_reason_codes": authorization.boundary_reason_codes,
                "policy_revision": authorization.policy_revision, "observed_at": observed_at,
            })
            return _public_review(review)

    def release_strict_run_lease(self, token: RunLeaseToken) -> None:
        validate_run_lease_token(token)
        with self._lock:
            current = self._strict_run_leases.get(token.turn_id)
            if current is not None and current.token == token and current.status == "active":
                self._strict_run_leases.pop(token.turn_id, None)

    def put_pending(self, turn_id: str, decision: Mapping[str, object]) -> None:
        with self._lock:
            self._require_turn(turn_id)
            self._pending[turn_id] = deepcopy(dict(decision))

    def get_pending(self, turn_id: str) -> Mapping[str, object] | None:
        with self._lock:
            pending = self._pending.get(turn_id)
            return deepcopy(pending) if pending is not None else None

    def clear_pending(self, turn_id: str) -> None:
        with self._lock:
            self._pending.pop(turn_id, None)

    def get_action(self, idempotency_key: str) -> tuple[Mapping[str, object], TurnReceipt] | None:
        with self._lock:
            item = self._actions.get(idempotency_key)
            return (deepcopy(item[0]), item[1]) if item is not None else None

    def save_action(self, action: Mapping[str, object], receipt: TurnReceipt) -> None:
        key = str(action["idempotency_key"])
        payload = dict(action)
        with self._lock:
            existing = self._actions.get(key)
            if existing is not None and (existing[0] != payload or existing[1] != receipt):
                raise TurnStateConflict("turn action idempotency identity conflict")
            self._actions[key] = (deepcopy(payload), receipt)

    def _require_turn(self, turn_id: str) -> None:
        if turn_id not in self._requests:
            raise TurnStateConflict("turn was not found")

    def _create_recovery_review_locked(self, turn_id: str, generation: int, reason_code: str, last_sequence: int, last_event_id: str, last_event_type: str, observed_at: datetime) -> None:
        key = (turn_id, generation)
        if key in self._recovery_review_keys:
            return
        request = self._requests.get(turn_id)
        if request is None:
            raise TurnStateConflict("turn was not found")
        review_id = f"review-{uuid4().hex}"
        self._recovery_review_keys[key] = review_id
        self._recovery_reviews[review_id] = {
            "review_id": review_id, "turn_id": turn_id, "generation": generation,
            "project_id": _request_project_id(request), "status": "quarantined", "revision": 1,
            "reason_code": reason_code, "last_sequence": last_sequence,
            "last_event_id": last_event_id, "last_event_type": last_event_type,
            "created_at": observed_at, "updated_at": observed_at,
        }

    def _finish_manual_review_locked(self, turn_id: str, generation: int, status: str, action: str, observed_at: datetime) -> None:
        review_id = self._recovery_review_keys.get((turn_id, generation))
        review = self._recovery_reviews.get(review_id) if review_id else None
        if review is None or review["status"] != "resume_queued":
            raise RuntimeError("manual recovery review identity lost")
        review["status"] = status
        review["revision"] = int(review["revision"]) + 1
        review["updated_at"] = observed_at
        self._recovery_review_audit.append({
            "review_id": review_id, "action": action, "actor_id": "recovery-worker",
            "boundary_outcome": "allow", "boundary_reason_codes": (action,),
            "policy_revision": 1, "observed_at": observed_at,
        })


def _lease_times(now: datetime, stale_after: datetime) -> None:
    validate_run_lease_time(now, name="run lease current time")
    validate_run_lease_time(stale_after, name="run lease stale time")
    if stale_after < now:
        raise ValueError("run lease stale time precedes current time")


def _lease_record(token: RunLeaseToken, status: str, acquired_at: datetime, heartbeat_at: datetime, stale_after: datetime) -> RunLeaseRecord:
    return validate_run_lease_record(RunLeaseRecord(token, status, acquired_at, heartbeat_at, stale_after))


def _request_project_id(request: Mapping[str, object]) -> str | None:
    scope = request.get("scope")
    return scope.get("project_id") if isinstance(scope, Mapping) and isinstance(scope.get("project_id"), str) else None


def _public_review(value: Mapping[str, object]) -> PublicRecoveryReview:
    return validate_public_recovery_review(PublicRecoveryReview(
        review_id=str(value["review_id"]), project_id=value["project_id"] if isinstance(value["project_id"], str) else None,
        status=str(value["status"]), revision=int(value["revision"]), reason_code=str(value["reason_code"]),
        created_at=value["created_at"], updated_at=value["updated_at"],  # type: ignore[arg-type]
    ))


def _manual_review_terminal_status(result_status: str) -> str:
    statuses = {
        "completed": "turn_completed",
        "failed": "turn_failed",
        "cancelled": "turn_cancelled",
        "waiting_approval": "waiting_approval",
    }
    if result_status not in statuses:
        raise ValueError("recovery completion result status is invalid")
    return statuses[result_status]
