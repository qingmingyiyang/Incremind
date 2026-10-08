"""Narrow, versioned policy authority for governed Agent evolution.

This module stores *selection and limits* only.  It deliberately cannot carry
model/provider routing, free-form instructions, capabilities, paths, or any
other execution grant.  Evolution lifecycle evidence remains in the World
Event projection; this catalog only makes a reviewed policy revision available
to new Turns through an explicit, human-controlled pointer.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import re
from typing import Protocol, runtime_checkable

from core.storage_provider import (
    SQLiteStructuredRecord,
    SQLiteStructuredRecordStore,
    SQLiteUnitOfWorkConflict,
    SQLiteUnitOfWorkError,
)


_ID = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
_COMMAND_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{7,127}$")
_COLLECTION_REVISIONS = "agent_evolution_policy_revisions"
_COLLECTION_HEADS = "agent_evolution_policy_heads"
_COLLECTION_COMMANDS = "agent_evolution_policy_commands"
_COLLECTION_TURN_SNAPSHOTS = "agent_evolution_policy_turn_snapshots"
_MAX_POLICY_PAYLOAD_BYTES = 16_384
_POLICY_STATUSES = frozenset({"candidate"})
_CLUSTER_MODES = frozenset({"main_only", "steward_optional", "expert_cluster"})
_CLUSTER_MODE_RANK = {"main_only": 0, "steward_optional": 1, "expert_cluster": 2}
_POLICY_FIELDS = frozenset({
    "policy_id", "revision", "status", "parent_revision", "target_roles",
    "routing", "scheduler", "context", "evaluation",
})


class AgentEvolutionPolicyError(ValueError):
    """Raised when a policy attempts to exceed the fixed Agent authority."""


class AgentEvolutionPolicyConflict(AgentEvolutionPolicyError):
    """Raised for a compare-and-swap or idempotency conflict."""


@dataclass(frozen=True, slots=True)
class VerifiedRolloutEvidence:
    """Immutable external verification, resolved by a trusted runtime port."""

    evidence_ref: str
    policy_id: str
    revision: int
    qualified: bool
    human_reviewed: bool
    completed_turns: int

    def __post_init__(self) -> None:
        _reference(self.evidence_ref, "evidence_ref")
        _id(self.policy_id, "policy_id")
        _positive(self.revision, "evidence revision")
        _boolean(self.qualified, "evidence qualified")
        _boolean(self.human_reviewed, "evidence human_reviewed")
        _nonnegative(self.completed_turns, "evidence completed_turns")


@runtime_checkable
class VerifiedRolloutEvidencePort(Protocol):
    def resolve(self, evidence_ref: str) -> VerifiedRolloutEvidence | None: ...


@dataclass(frozen=True, slots=True)
class AgentPolicyRouting:
    profile_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _ids(self.profile_ids, "routing profile_ids", nonempty=True, maximum=8)


@dataclass(frozen=True, slots=True)
class AgentPolicyScheduler:
    cluster_mode: str
    max_assignments: int
    parallelism_cap: int
    prefer_main_only: bool
    allowed_expert_ids: tuple[str, ...]
    allowed_skill_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.cluster_mode not in _CLUSTER_MODES:
            raise AgentEvolutionPolicyError("scheduler cluster_mode is invalid")
        _count(self.max_assignments, "scheduler max_assignments", maximum=64)
        _count(self.parallelism_cap, "scheduler parallelism_cap", maximum=8)
        if self.parallelism_cap > self.max_assignments:
            raise AgentEvolutionPolicyError("scheduler parallelism exceeds assignments")
        _boolean(self.prefer_main_only, "scheduler prefer_main_only")
        _ids(self.allowed_expert_ids, "scheduler allowed_expert_ids", maximum=16)
        _ids(self.allowed_skill_ids, "scheduler allowed_skill_ids", maximum=16)
        if self.prefer_main_only and (self.max_assignments or self.parallelism_cap):
            raise AgentEvolutionPolicyError("main-only scheduler cannot assign children")


@dataclass(frozen=True, slots=True)
class AgentPolicyContext:
    include_project_skill: bool
    include_memory: bool
    include_session_history: bool
    max_context_bytes: int

    def __post_init__(self) -> None:
        _boolean(self.include_project_skill, "context include_project_skill")
        _boolean(self.include_memory, "context include_memory")
        _boolean(self.include_session_history, "context include_session_history")
        _count(self.max_context_bytes, "context max_context_bytes", maximum=8_388_608)


@dataclass(frozen=True, slots=True)
class AgentPolicyEvaluation:
    cohort_percent: int
    minimum_completed_turns: int
    manual_promotion: bool

    def __post_init__(self) -> None:
        _count(self.cohort_percent, "evaluation cohort_percent", maximum=100)
        _count(self.minimum_completed_turns, "evaluation minimum_completed_turns", maximum=1_000_000)
        _boolean(self.manual_promotion, "evaluation manual_promotion")
        if not self.manual_promotion:
            raise AgentEvolutionPolicyError("automatic promotion is forbidden")


@dataclass(frozen=True, slots=True)
class AgentEvolutionPolicy:
    """An immutable candidate revision containing only bounded policy knobs."""

    policy_id: str
    revision: int
    status: str
    parent_revision: int | None
    target_roles: tuple[str, ...]
    routing: AgentPolicyRouting
    scheduler: AgentPolicyScheduler
    context: AgentPolicyContext
    evaluation: AgentPolicyEvaluation

    def __post_init__(self) -> None:
        _id(self.policy_id, "policy_id")
        _positive(self.revision, "policy revision")
        if self.status not in _POLICY_STATUSES:
            raise AgentEvolutionPolicyError("policy status must be candidate")
        if self.parent_revision is not None:
            _positive(self.parent_revision, "parent_revision")
            if self.parent_revision >= self.revision:
                raise AgentEvolutionPolicyError("parent_revision must precede revision")
        _ids(self.target_roles, "target_roles", nonempty=True, maximum=4)
        if not isinstance(self.routing, AgentPolicyRouting):
            raise AgentEvolutionPolicyError("routing is invalid")
        if not isinstance(self.scheduler, AgentPolicyScheduler):
            raise AgentEvolutionPolicyError("scheduler is invalid")
        if not isinstance(self.context, AgentPolicyContext):
            raise AgentEvolutionPolicyError("context is invalid")
        if not isinstance(self.evaluation, AgentPolicyEvaluation):
            raise AgentEvolutionPolicyError("evaluation is invalid")
        if _json_size(asdict(self)) > _MAX_POLICY_PAYLOAD_BYTES:
            raise AgentEvolutionPolicyError("policy payload exceeds byte limit")

    def does_not_expand(self, active: "AgentEvolutionPolicy") -> bool:
        """Return whether this candidate is a strict authority subset of active."""

        return (
            self.policy_id == active.policy_id
            and set(self.target_roles).issubset(active.target_roles)
            and set(self.routing.profile_ids).issubset(active.routing.profile_ids)
            and _CLUSTER_MODE_RANK[self.scheduler.cluster_mode]
            <= _CLUSTER_MODE_RANK[active.scheduler.cluster_mode]
            and self.scheduler.max_assignments <= active.scheduler.max_assignments
            and self.scheduler.parallelism_cap <= active.scheduler.parallelism_cap
            and (not active.scheduler.prefer_main_only or self.scheduler.prefer_main_only)
            and set(self.scheduler.allowed_expert_ids).issubset(active.scheduler.allowed_expert_ids)
            and set(self.scheduler.allowed_skill_ids).issubset(active.scheduler.allowed_skill_ids)
            and _enabled_context(self.context).issubset(_enabled_context(active.context))
            and self.context.max_context_bytes <= active.context.max_context_bytes
            and self.evaluation.cohort_percent <= active.evaluation.cohort_percent
            and self.evaluation.minimum_completed_turns >= active.evaluation.minimum_completed_turns
            and self.evaluation.manual_promotion == active.evaluation.manual_promotion
        )

    def to_payload(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_payload(cls, value: object) -> "AgentEvolutionPolicy":
        payload = _mapping(value, "policy payload")
        _exact_fields(payload, _POLICY_FIELDS, "policy payload")
        try:
            return cls(
                policy_id=_string(payload["policy_id"], "policy_id"),
                revision=_positive_value(payload["revision"], "revision"),
                status=_string(payload["status"], "status"),
                parent_revision=_optional_positive(payload["parent_revision"], "parent_revision"),
                target_roles=_id_tuple(payload["target_roles"], "target_roles", nonempty=True),
                routing=_routing_from_payload(payload["routing"]),
                scheduler=_scheduler_from_payload(payload["scheduler"]),
                context=_context_from_payload(payload["context"]),
                evaluation=_evaluation_from_payload(payload["evaluation"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            if isinstance(error, AgentEvolutionPolicyError):
                raise
            raise AgentEvolutionPolicyError("policy payload is invalid") from error


@dataclass(frozen=True, slots=True)
class AgentPolicyHead:
    policy_id: str
    latest_revision: int
    active_revision: int | None
    canary_revision: int | None
    canary_percent: int
    prior_active_revision: int | None
    revision: int


@dataclass(frozen=True, slots=True)
class AgentPolicyTurnSnapshot:
    policy_id: str
    project_id: str
    turn_id: str
    selected_revision: int


class AgentPolicyCatalog:
    """Durable catalog with immutable revisions and a CAS-protected rollout head.

    The catalog never decides whether a candidate passed evaluation.  Callers
    must obtain that decision from the separate Evolution event/review flow
    before invoking ``start_canary`` or ``activate``.
    """

    def __init__(
        self,
        records: SQLiteStructuredRecordStore,
        *,
        profile_ids: tuple[str, ...],
        trusted_baseline: AgentEvolutionPolicy,
        rollout_evidence: VerifiedRolloutEvidencePort,
        expert_ids: tuple[str, ...] = (),
        skill_ids: tuple[str, ...] = (),
    ) -> None:
        if not isinstance(records, SQLiteStructuredRecordStore):
            raise AgentEvolutionPolicyError("policy catalog store is invalid")
        _ids(profile_ids, "profile_ids", nonempty=True)
        _ids(expert_ids, "expert_ids")
        _ids(skill_ids, "skill_ids")
        self._records = records
        self._profile_ids = frozenset(profile_ids)
        self._expert_ids = frozenset(expert_ids)
        self._skill_ids = frozenset(skill_ids)
        if not isinstance(rollout_evidence, VerifiedRolloutEvidencePort):
            raise AgentEvolutionPolicyError("rollout evidence port is invalid")
        self._evidence = rollout_evidence
        self._validate_catalog_membership(trusted_baseline)
        if trusted_baseline.revision != 1 or trusted_baseline.parent_revision is not None:
            raise AgentEvolutionPolicyError("trusted baseline must be root revision one")
        self._bootstrap_trusted_baseline(trusted_baseline)

    def get_revision(self, policy_id: str, revision: int) -> AgentEvolutionPolicy | None:
        _id(policy_id, "policy_id")
        _positive(revision, "revision")
        record = self._records.read(_COLLECTION_REVISIONS, _revision_id(policy_id, revision))
        if record is None:
            return None
        return _policy_record(record, policy_id=policy_id, revision=revision)

    def active(self, policy_id: str) -> AgentEvolutionPolicy | None:
        head = self.head(policy_id)
        return None if head is None or head.active_revision is None else self._required_revision(policy_id, head.active_revision)

    def canary(self, policy_id: str) -> tuple[AgentEvolutionPolicy, int] | None:
        head = self.head(policy_id)
        if head is None or head.canary_revision is None:
            return None
        return self._required_revision(policy_id, head.canary_revision), head.canary_percent

    def head(self, policy_id: str) -> AgentPolicyHead | None:
        _id(policy_id, "policy_id")
        record = self._records.read(_COLLECTION_HEADS, policy_id)
        return None if record is None else _head_from_record(record, policy_id)

    def create_candidate(self, policy: AgentEvolutionPolicy, *, command_id: str) -> AgentEvolutionPolicy:
        """Create a revision once; same command/input replays, drift conflicts."""

        _command_id(command_id)
        self._validate_catalog_membership(policy)
        try:
            with self._records.begin() as unit:
                replay = unit.read(_COLLECTION_COMMANDS, command_id)
                if replay is not None:
                    result = self._command_policy(unit, replay)
                    if result != policy or replay.payload.get("action") != "candidate":
                        raise AgentEvolutionPolicyConflict("policy command id conflicts with immutable input")
                    unit.rollback()
                    return result
                existing = unit.read(_COLLECTION_REVISIONS, _revision_id(policy.policy_id, policy.revision))
                if existing is not None:
                    existing_policy = _policy_record(existing, policy_id=policy.policy_id, revision=policy.revision)
                    if existing_policy != policy:
                        raise AgentEvolutionPolicyConflict("policy revision identity drifted")
                    raise AgentEvolutionPolicyConflict("policy revision requires its original command id")
                head_record = unit.read(_COLLECTION_HEADS, policy.policy_id)
                head = _head_from_record(head_record, policy.policy_id) if head_record else None
                current = self._policy_from_unit(unit, policy.policy_id, head.active_revision) if head and head.active_revision else None
                if head is None or current is None:
                    raise AgentEvolutionPolicyConflict("an active policy is required before another candidate")
                else:
                    if policy.revision != head.latest_revision + 1 or policy.parent_revision != current.revision:
                        raise AgentEvolutionPolicyConflict("candidate revision or parent is stale")
                    if not policy.does_not_expand(current):
                        raise AgentEvolutionPolicyError("candidate expands active authority")
                unit.put(_COLLECTION_REVISIONS, _revision_id(policy.policy_id, policy.revision), policy.to_payload(), expected_revision=0)
                if head_record is None:
                    unit.put(_COLLECTION_HEADS, policy.policy_id, _head_payload(policy.revision, None, None, 0, None), expected_revision=0)
                else:
                    unit.put(
                        _COLLECTION_HEADS,
                        policy.policy_id,
                        _head_payload(policy.revision, head.active_revision, head.canary_revision, head.canary_percent, head.prior_active_revision),
                        expected_revision=head_record.revision,
                    )
                unit.put(_COLLECTION_COMMANDS, command_id, _command_payload("candidate", policy), expected_revision=0)
                unit.commit()
                return policy
        except AgentEvolutionPolicyError:
            raise
        except (SQLiteUnitOfWorkConflict, SQLiteUnitOfWorkError) as error:
            raise AgentEvolutionPolicyConflict("policy candidate write conflicted") from error

    def start_canary(self, policy_id: str, revision: int, *, percent: int, evidence_ref: str, expected_head_revision: int, command_id: str) -> AgentPolicyHead:
        _id(policy_id, "policy_id")
        _positive(revision, "revision")
        _percent(percent)
        _reference(evidence_ref, "evidence_ref")
        _nonnegative(expected_head_revision, "expected_head_revision")
        _command_id(command_id)
        if percent == 0:
            raise AgentEvolutionPolicyError("canary percent must be positive")
        return self._change_head("canary", policy_id, revision, percent, evidence_ref, expected_head_revision, command_id)

    def activate(self, policy_id: str, revision: int, *, evidence_ref: str, expected_head_revision: int, command_id: str) -> AgentPolicyHead:
        """Explicitly promote the current canary; no automatic promotion path exists."""

        _id(policy_id, "policy_id")
        _positive(revision, "revision")
        _reference(evidence_ref, "evidence_ref")
        _nonnegative(expected_head_revision, "expected_head_revision")
        _command_id(command_id)
        return self._change_head("activate", policy_id, revision, None, evidence_ref, expected_head_revision, command_id)

    def rollback(self, policy_id: str, *, expected_head_revision: int, command_id: str) -> AgentPolicyHead:
        _id(policy_id, "policy_id")
        _nonnegative(expected_head_revision, "expected_head_revision")
        _command_id(command_id)
        return self._change_head("rollback", policy_id, None, None, None, expected_head_revision, command_id)

    def select_for_new_turn(self, policy_id: str, *, project_id: str, turn_id: str) -> AgentEvolutionPolicy | None:
        """Resolve a policy once at Turn creation; callers persist its revision snapshot.

        Existing Turns must use their persisted snapshot and never call this
        method again, so changing a canary can affect only newly admitted Turns.
        """

        _id(policy_id, "policy_id")
        _id(project_id, "project_id")
        _id(turn_id, "turn_id")
        head = self.head(policy_id)
        if head is None:
            return None
        if head.canary_revision is not None and is_in_canary_cohort(project_id, turn_id, policy_id, head.canary_percent):
            return self._required_revision(policy_id, head.canary_revision)
        return None if head.active_revision is None else self._required_revision(policy_id, head.active_revision)

    def freeze_for_new_turn(self, policy_id: str, *, project_id: str, turn_id: str) -> AgentPolicyTurnSnapshot:
        """Atomically select and persist a policy revision for a newly admitted Turn."""

        _id(policy_id, "policy_id")
        _id(project_id, "project_id")
        _id(turn_id, "turn_id")
        snapshot_id = _turn_snapshot_id(policy_id, project_id, turn_id)
        try:
            with self._records.begin() as unit:
                existing = unit.read(_COLLECTION_TURN_SNAPSHOTS, snapshot_id)
                if existing is not None:
                    snapshot = _turn_snapshot_from_payload(existing.payload)
                    if snapshot.policy_id != policy_id or snapshot.project_id != project_id or snapshot.turn_id != turn_id:
                        raise AgentEvolutionPolicyConflict("turn snapshot identity drifted")
                    unit.rollback()
                    return snapshot
                head_record = unit.read(_COLLECTION_HEADS, policy_id)
                if head_record is None:
                    raise AgentEvolutionPolicyError("policy head does not exist")
                head = _head_from_record(head_record, policy_id)
                selected = head.canary_revision if head.canary_revision is not None and is_in_canary_cohort(project_id, turn_id, policy_id, head.canary_percent) else head.active_revision
                if selected is None:
                    raise AgentEvolutionPolicyError("policy has no active revision")
                self._policy_from_unit(unit, policy_id, selected)
                snapshot = AgentPolicyTurnSnapshot(policy_id, project_id, turn_id, selected)
                unit.put(_COLLECTION_TURN_SNAPSHOTS, snapshot_id, _turn_snapshot_payload(snapshot), expected_revision=0)
                unit.commit()
                return snapshot
        except AgentEvolutionPolicyError:
            raise
        except (SQLiteUnitOfWorkConflict, SQLiteUnitOfWorkError) as error:
            raise AgentEvolutionPolicyConflict("turn snapshot write conflicted") from error

    def load_turn_snapshot(self, policy_id: str, *, project_id: str, turn_id: str) -> AgentPolicyTurnSnapshot | None:
        _id(policy_id, "policy_id")
        _id(project_id, "project_id")
        _id(turn_id, "turn_id")
        record = self._records.read(_COLLECTION_TURN_SNAPSHOTS, _turn_snapshot_id(policy_id, project_id, turn_id))
        if record is None:
            return None
        snapshot = _turn_snapshot_from_payload(record.payload)
        if snapshot.policy_id != policy_id or snapshot.project_id != project_id or snapshot.turn_id != turn_id:
            raise AgentEvolutionPolicyError("turn snapshot identity drifted")
        return snapshot

    def _change_head(self, action: str, policy_id: str, revision: int | None, percent: int | None, evidence_ref: str | None, expected_head_revision: int, command_id: str) -> AgentPolicyHead:
        try:
            with self._records.begin() as unit:
                replay = unit.read(_COLLECTION_COMMANDS, command_id)
                if replay is not None:
                    result = self._command_head(replay)
                    expected = {"action": action, "policy_id": policy_id, "revision": revision, "percent": percent, "evidence_ref": evidence_ref, "expected_head_revision": expected_head_revision}
                    if dict(replay.payload.get("input", {})) != expected:
                        raise AgentEvolutionPolicyConflict("policy command id conflicts with immutable input")
                    unit.rollback()
                    return result
                record = unit.read(_COLLECTION_HEADS, policy_id)
                if record is None:
                    raise AgentEvolutionPolicyConflict("policy head does not exist")
                head = _head_from_record(record, policy_id)
                if head.revision != expected_head_revision:
                    raise AgentEvolutionPolicyConflict(f"expected policy head revision {expected_head_revision}, found {head.revision}")
                if action == "canary":
                    assert revision is not None and percent is not None and evidence_ref is not None
                    candidate = self._policy_from_unit(unit, policy_id, revision)
                    self._require_approved_evidence(evidence_ref, candidate)
                    active = self._policy_from_unit(unit, policy_id, head.active_revision) if head.active_revision else None
                    if head.canary_revision is not None:
                        raise AgentEvolutionPolicyConflict("a canary is already active")
                    if revision == head.active_revision:
                        raise AgentEvolutionPolicyConflict("active revision cannot reopen as canary")
                    if active is None:
                        if candidate.revision != 1 or candidate.parent_revision is not None:
                            raise AgentEvolutionPolicyConflict("initial canary identity is stale")
                    elif candidate.parent_revision != active.revision:
                        raise AgentEvolutionPolicyConflict("candidate parent does not match active revision")
                    elif not candidate.does_not_expand(active):
                        raise AgentEvolutionPolicyError("candidate expands active authority")
                    if percent > candidate.evaluation.cohort_percent:
                        raise AgentEvolutionPolicyError("canary percent exceeds evaluated cohort")
                    next_head = AgentPolicyHead(policy_id, head.latest_revision, head.active_revision, revision, percent, head.prior_active_revision, head.revision + 1)
                elif action == "activate":
                    assert revision is not None and evidence_ref is not None
                    if head.canary_revision != revision or head.canary_percent <= 0:
                        raise AgentEvolutionPolicyConflict("only the current canary may be activated")
                    candidate = self._policy_from_unit(unit, policy_id, revision)
                    evidence = self._require_approved_evidence(evidence_ref, candidate)
                    if evidence.completed_turns < candidate.evaluation.minimum_completed_turns:
                        raise AgentEvolutionPolicyConflict("canary has insufficient completed turns")
                    next_head = AgentPolicyHead(policy_id, head.latest_revision, revision, None, 0, head.active_revision, head.revision + 1)
                else:
                    if head.canary_revision is not None:
                        # A failed canary never becomes active: removing it is
                        # the rollback and preserves the already-active policy.
                        next_head = AgentPolicyHead(policy_id, head.latest_revision, head.active_revision, None, 0, head.prior_active_revision, head.revision + 1)
                    else:
                        if head.active_revision is None or head.prior_active_revision is None:
                            raise AgentEvolutionPolicyConflict("no prior active revision is available for rollback")
                        next_head = AgentPolicyHead(policy_id, head.latest_revision, head.prior_active_revision, None, 0, None, head.revision + 1)
                unit.put(_COLLECTION_HEADS, policy_id, _head_payload(next_head.latest_revision, next_head.active_revision, next_head.canary_revision, next_head.canary_percent, next_head.prior_active_revision), expected_revision=record.revision)
                unit.put(_COLLECTION_COMMANDS, command_id, _command_head_payload(action, policy_id, revision, percent, evidence_ref, expected_head_revision, next_head), expected_revision=0)
                unit.commit()
                return next_head
        except AgentEvolutionPolicyError:
            raise
        except (SQLiteUnitOfWorkConflict, SQLiteUnitOfWorkError) as error:
            raise AgentEvolutionPolicyConflict("policy head update conflicted") from error

    def _required_revision(self, policy_id: str, revision: int) -> AgentEvolutionPolicy:
        result = self.get_revision(policy_id, revision)
        if result is None:
            raise AgentEvolutionPolicyError("policy head points to missing revision")
        return result

    def _require_approved_evidence(self, evidence_ref: str, policy: AgentEvolutionPolicy) -> VerifiedRolloutEvidence:
        evidence = self._evidence.resolve(evidence_ref)
        if not isinstance(evidence, VerifiedRolloutEvidence):
            raise AgentEvolutionPolicyError("verified rollout evidence is unavailable")
        if evidence.evidence_ref != evidence_ref or evidence.policy_id != policy.policy_id or evidence.revision != policy.revision:
            raise AgentEvolutionPolicyError("verified rollout evidence identity drifted")
        if not evidence.qualified or not evidence.human_reviewed:
            raise AgentEvolutionPolicyConflict("candidate lacks qualified human review")
        return evidence

    def _bootstrap_trusted_baseline(self, baseline: AgentEvolutionPolicy) -> None:
        try:
            with self._records.begin() as unit:
                record = unit.read(_COLLECTION_REVISIONS, _revision_id(baseline.policy_id, 1))
                head_record = unit.read(_COLLECTION_HEADS, baseline.policy_id)
                if record is None and head_record is None:
                    unit.put(_COLLECTION_REVISIONS, _revision_id(baseline.policy_id, 1), baseline.to_payload(), expected_revision=0)
                    unit.put(_COLLECTION_HEADS, baseline.policy_id, _head_payload(1, 1, None, 0, None), expected_revision=0)
                    unit.commit()
                    return
                if record is None or head_record is None:
                    raise AgentEvolutionPolicyConflict("trusted baseline storage is incomplete")
                if _policy_record(record, policy_id=baseline.policy_id, revision=1) != baseline:
                    raise AgentEvolutionPolicyConflict("trusted baseline identity drifted")
                head = _head_from_record(head_record, baseline.policy_id)
                if head.active_revision != 1:
                    raise AgentEvolutionPolicyConflict("trusted baseline is not active")
                unit.rollback()
        except AgentEvolutionPolicyError:
            raise
        except (SQLiteUnitOfWorkConflict, SQLiteUnitOfWorkError) as error:
            raise AgentEvolutionPolicyConflict("trusted baseline bootstrap conflicted") from error

    def _validate_catalog_membership(self, policy: AgentEvolutionPolicy) -> None:
        if not set(policy.routing.profile_ids).issubset(self._profile_ids):
            raise AgentEvolutionPolicyError("policy references an unknown profile")
        if not set(policy.scheduler.allowed_expert_ids).issubset(self._expert_ids):
            raise AgentEvolutionPolicyError("policy references an unknown expert")
        if not set(policy.scheduler.allowed_skill_ids).issubset(self._skill_ids):
            raise AgentEvolutionPolicyError("policy references an unknown skill")

    @staticmethod
    def _policy_from_unit(unit, policy_id: str, revision: int) -> AgentEvolutionPolicy:
        record = unit.read(_COLLECTION_REVISIONS, _revision_id(policy_id, revision))
        if record is None:
            raise AgentEvolutionPolicyError("policy revision does not exist")
        return _policy_record(record, policy_id=policy_id, revision=revision)

    @staticmethod
    def _command_policy(unit, record: SQLiteStructuredRecord) -> AgentEvolutionPolicy:
        revision_id = record.payload.get("revision_id")
        if not isinstance(revision_id, str):
            raise AgentEvolutionPolicyError("policy command is malformed")
        revision = unit.read(_COLLECTION_REVISIONS, revision_id)
        if revision is None:
            raise AgentEvolutionPolicyError("policy command points to missing revision")
        return AgentEvolutionPolicy.from_payload(revision.payload)

    @staticmethod
    def _command_head(record: SQLiteStructuredRecord) -> AgentPolicyHead:
        result = record.payload.get("result")
        revision = _positive_value(record.payload.get("result_head_revision"), "result_head_revision")
        return _head_from_payload(_mapping(result, "policy command result"), _string(record.payload.get("policy_id"), "policy_id"), revision)


def is_in_canary_cohort(project_id: str, turn_id: str, policy_id: str, percent: int) -> bool:
    """Stable hash selection; 0% never selects and 100% always selects."""

    _id(project_id, "project_id")
    _id(turn_id, "turn_id")
    _id(policy_id, "policy_id")
    _percent(percent)
    if percent == 0:
        return False
    if percent == 100:
        return True
    digest = sha256(f"{project_id}\0{turn_id}\0{policy_id}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") < percent * (1 << 64) // 100


def _head_payload(latest: int, active: int | None, canary: int | None, percent: int, prior: int | None) -> dict[str, object]:
    return {"latest_revision": latest, "active_revision": active, "canary_revision": canary, "canary_percent": percent, "prior_active_revision": prior}


def _head_from_record(record: SQLiteStructuredRecord, policy_id: str) -> AgentPolicyHead:
    return _head_from_payload(record.payload, policy_id, record.revision)


def _head_from_payload(payload: Mapping[str, object], policy_id: str, record_revision: int) -> AgentPolicyHead:
    _exact_fields(payload, {"latest_revision", "active_revision", "canary_revision", "canary_percent", "prior_active_revision"}, "policy head")
    latest = _positive_value(payload["latest_revision"], "latest_revision")
    active = _optional_positive(payload["active_revision"], "active_revision")
    canary = _optional_positive(payload["canary_revision"], "canary_revision")
    percent = _percent_value(payload["canary_percent"])
    prior = _optional_positive(payload["prior_active_revision"], "prior_active_revision")
    if canary is None and percent != 0:
        raise AgentEvolutionPolicyError("policy head canary percent drifted")
    if canary is not None and percent == 0:
        raise AgentEvolutionPolicyError("policy head canary percent is missing")
    if active is not None and active > latest:
        raise AgentEvolutionPolicyError("policy head active revision drifted")
    if canary is not None and canary > latest:
        raise AgentEvolutionPolicyError("policy head canary revision drifted")
    return AgentPolicyHead(policy_id, latest, active, canary, percent, prior, record_revision)


def _command_payload(action: str, policy: AgentEvolutionPolicy) -> dict[str, object]:
    return {"action": action, "revision_id": _revision_id(policy.policy_id, policy.revision), "policy": policy.to_payload()}


def _command_head_payload(action: str, policy_id: str, revision: int | None, percent: int | None, evidence_ref: str | None, expected: int, result: AgentPolicyHead) -> dict[str, object]:
    return {"action": action, "policy_id": policy_id, "input": {"action": action, "policy_id": policy_id, "revision": revision, "percent": percent, "evidence_ref": evidence_ref, "expected_head_revision": expected}, "result": _head_payload(result.latest_revision, result.active_revision, result.canary_revision, result.canary_percent, result.prior_active_revision), "result_head_revision": result.revision}


def _policy_record(record: SQLiteStructuredRecord, *, policy_id: str, revision: int) -> AgentEvolutionPolicy:
    result = AgentEvolutionPolicy.from_payload(record.payload)
    if result.policy_id != policy_id or result.revision != revision:
        raise AgentEvolutionPolicyError("policy revision identity drifted")
    return result


def _revision_id(policy_id: str, revision: int) -> str:
    return f"{policy_id}~r{revision}"


def _turn_snapshot_id(policy_id: str, project_id: str, turn_id: str) -> str:
    return "turn-" + sha256(f"{policy_id}\0{project_id}\0{turn_id}".encode("utf-8")).hexdigest()


def _turn_snapshot_payload(snapshot: AgentPolicyTurnSnapshot) -> dict[str, object]:
    return {"policy_id": snapshot.policy_id, "project_id": snapshot.project_id, "turn_id": snapshot.turn_id, "selected_revision": snapshot.selected_revision}


def _turn_snapshot_from_payload(value: object) -> AgentPolicyTurnSnapshot:
    payload = _mapping(value, "turn snapshot")
    _exact_fields(payload, {"policy_id", "project_id", "turn_id", "selected_revision"}, "turn snapshot")
    return AgentPolicyTurnSnapshot(_string(payload["policy_id"], "policy_id"), _string(payload["project_id"], "project_id"), _string(payload["turn_id"], "turn_id"), _positive_value(payload["selected_revision"], "selected_revision"))


def _routing_from_payload(value: object) -> AgentPolicyRouting:
    payload = _mapping(value, "routing")
    _exact_fields(payload, {"profile_ids"}, "routing")
    return AgentPolicyRouting(_id_tuple(payload["profile_ids"], "routing profile_ids", nonempty=True))


def _scheduler_from_payload(value: object) -> AgentPolicyScheduler:
    payload = _mapping(value, "scheduler")
    _exact_fields(payload, {"cluster_mode", "max_assignments", "parallelism_cap", "prefer_main_only", "allowed_expert_ids", "allowed_skill_ids"}, "scheduler")
    return AgentPolicyScheduler(_string(payload["cluster_mode"], "cluster_mode"), _nonnegative_value(payload["max_assignments"], "max_assignments"), _nonnegative_value(payload["parallelism_cap"], "parallelism_cap"), _bool_value(payload["prefer_main_only"], "prefer_main_only"), _id_tuple(payload["allowed_expert_ids"], "allowed_expert_ids"), _id_tuple(payload["allowed_skill_ids"], "allowed_skill_ids"))


def _context_from_payload(value: object) -> AgentPolicyContext:
    payload = _mapping(value, "context")
    _exact_fields(payload, {"include_project_skill", "include_memory", "include_session_history", "max_context_bytes"}, "context")
    return AgentPolicyContext(_bool_value(payload["include_project_skill"], "include_project_skill"), _bool_value(payload["include_memory"], "include_memory"), _bool_value(payload["include_session_history"], "include_session_history"), _nonnegative_value(payload["max_context_bytes"], "max_context_bytes"))


def _evaluation_from_payload(value: object) -> AgentPolicyEvaluation:
    payload = _mapping(value, "evaluation")
    _exact_fields(payload, {"cohort_percent", "minimum_completed_turns", "manual_promotion"}, "evaluation")
    return AgentPolicyEvaluation(_percent_value(payload["cohort_percent"]), _nonnegative_value(payload["minimum_completed_turns"], "minimum_completed_turns"), _bool_value(payload["manual_promotion"], "manual_promotion"))


def _enabled_context(value: AgentPolicyContext) -> set[str]:
    return {name for name in ("include_project_skill", "include_memory", "include_session_history") if getattr(value, name)}


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise AgentEvolutionPolicyError(f"{label} is invalid")
    return value


def _exact_fields(value: Mapping[str, object], expected: set[str] | frozenset[str], label: str) -> None:
    if set(value) != set(expected):
        raise AgentEvolutionPolicyError(f"{label} fields are not allowed")


def _ids(value: tuple[str, ...], label: str, *, nonempty: bool = False, maximum: int = 16) -> None:
    if not isinstance(value, tuple) or (nonempty and not value) or len(value) > maximum or len(value) != len(set(value)):
        raise AgentEvolutionPolicyError(f"{label} is invalid")
    for item in value:
        _id(item, label)


def _id_tuple(value: object, label: str, *, nonempty: bool = False, maximum: int = 16) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise AgentEvolutionPolicyError(f"{label} is invalid")
    result = tuple(_string(item, label) for item in value)
    _ids(result, label, nonempty=nonempty, maximum=maximum)
    return result


def _id(value: object, label: str) -> None:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise AgentEvolutionPolicyError(f"{label} is invalid")


def _reference(value: object, label: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"crp://[A-Za-z0-9._~-]{1,64}/[A-Za-z0-9._~/-]{1,384}", value):
        raise AgentEvolutionPolicyError(f"{label} is invalid")


def _json_size(value: object) -> int:
    return len(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8"))


def _string(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise AgentEvolutionPolicyError(f"{label} is invalid")
    return value


def _boolean(value: object, label: str) -> None:
    if not isinstance(value, bool):
        raise AgentEvolutionPolicyError(f"{label} is invalid")


def _bool_value(value: object, label: str) -> bool:
    _boolean(value, label)
    return value


def _count(value: object, label: str, *, maximum: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= maximum:
        raise AgentEvolutionPolicyError(f"{label} is invalid")


def _nonnegative(value: object, label: str) -> None:
    _count(value, label, maximum=2_147_483_647)


def _nonnegative_value(value: object, label: str) -> int:
    _nonnegative(value, label)
    return value


def _positive(value: object, label: str) -> None:
    _count(value, label, maximum=2_147_483_647)
    if value == 0:
        raise AgentEvolutionPolicyError(f"{label} is invalid")


def _positive_value(value: object, label: str) -> int:
    _positive(value, label)
    return value


def _optional_positive(value: object, label: str) -> int | None:
    if value is None:
        return None
    return _positive_value(value, label)


def _percent(value: object) -> None:
    _count(value, "percent", maximum=100)


def _percent_value(value: object) -> int:
    _percent(value)
    return value


def _command_id(value: object) -> None:
    if not isinstance(value, str) or _COMMAND_ID.fullmatch(value) is None:
        raise AgentEvolutionPolicyError("command_id is invalid")
