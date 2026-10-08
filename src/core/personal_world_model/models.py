"""Contracts for the Personal Memory World Model.

The records in this module are observations and evaluations.  They never own
Source, Job, Effect, Receipt, Memory, Skill, or Document execution state.  A
``WorldStateProjection`` is rebuilt from these immutable facts by the sibling
projection module.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from types import MappingProxyType

from core.long_horizon_runtime.provenance import (
    ProvenanceContractError,
    TraceLink,
    TraceSubject,
    ValidationFact,
    VersionBinding,
)
from core.long_horizon_runtime.task_graph import TaskGraphContractError, TaskGraphEvent
from core.long_horizon_runtime.trajectory import (
    TrajectoryContractError,
    TrustCheckpoint,
)
from core.recursive_evolution import EvolutionContractError, EvolutionEvent


_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:~-]{0,127}$")
_STATE_FIELD = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:~-]{0,127}$")
_OPAQUE_REF = re.compile(r"^crp://[A-Za-z0-9][A-Za-z0-9._~:/?#%+=@-]{1,511}$")
_ABSOLUTE_PATH = re.compile(
    r"(?:(?:[A-Za-z]:[\\/])|(?:^|\s)/(?:Users|home|var|etc|tmp|opt|srv)/)",
    re.IGNORECASE,
)
_LOCATOR = re.compile(r"(?:https?://|file://)", re.IGNORECASE)
_SECRET = re.compile(
    r"(?i)(?:authorization\s*:|cookie\s*:|\bsk-[A-Za-z0-9_-]{16,}\b|"
    r"(?:api[_-]?key|secret|token|password)\s*[:=])"
)
class PersonalWorldModelError(ValueError):
    """Raised when a world observation would weaken scope or provenance."""


class WorldEventKind(StrEnum):
    GOAL_DECLARED = "goal.declared"
    PROJECT_OBSERVATION = "project.observation"
    TASK_OBSERVED = "task.observed"
    BLOCKER_OPENED = "blocker.opened"
    BLOCKER_RESOLVED = "blocker.resolved"
    ACTION_PLANNED = "action.planned"
    FEEDBACK_RECORDED = "feedback.recorded"
    CLOCK_OBSERVED = "clock.observed"
    SUPERVISION_CLAIM_DECLARED = "supervision.claim.declared"
    SUPERVISION_VERIFICATION_RECORDED = "supervision.verification.recorded"
    SUPERVISION_DECISION_RECORDED = "supervision.decision.recorded"
    PROVENANCE_SUBJECT_RECORDED = "provenance.subject.recorded"
    PROVENANCE_LINK_RECORDED = "provenance.link.recorded"
    PROVENANCE_VALIDATION_RECORDED = "provenance.validation.recorded"
    TASK_GRAPH_EVENT_RECORDED = "task_graph.event.recorded"
    TRAJECTORY_CHECKPOINT_RECORDED = "trajectory.checkpoint.recorded"
    EVOLUTION_EVENT_RECORDED = "evolution.event.recorded"


class OutcomeStatus(StrEnum):
    ACHIEVED = "achieved"
    PARTIAL = "partial"
    FAILED = "failed"
    UNKNOWN = "unknown"


class UserEvaluationVerdict(StrEnum):
    ACCEPTED = "accepted"
    CORRECTED = "corrected"
    REJECTED = "rejected"
    NOT_PROVIDED = "not_provided"


@dataclass(frozen=True, slots=True)
class StateDelta:
    field: str
    before: str | int | bool | None
    after: str | int | bool | None

    def __post_init__(self) -> None:
        if not isinstance(self.field, str) or _STATE_FIELD.fullmatch(self.field) is None:
            raise PersonalWorldModelError("state delta field is invalid")
        if not _json_scalar(self.before) or not _json_scalar(self.after):
            raise PersonalWorldModelError("state delta values must be JSON scalars")
        if self.before == self.after:
            raise PersonalWorldModelError("state delta must describe an observed change")

    def to_payload(self) -> dict[str, object]:
        return {"field": self.field, "before": self.before, "after": self.after}

    @classmethod
    def from_payload(cls, value: object) -> StateDelta:
        item = _exact_mapping(value, {"field", "before", "after"}, "state delta")
        return cls(
            field=_identifier(item.get("field"), "state delta field"),
            before=item.get("before"),
            after=item.get("after"),
        )


@dataclass(frozen=True, slots=True)
class FeedbackCost:
    elapsed_ms: int
    model_input_tokens: int = 0
    model_output_tokens: int = 0
    external_calls: int = 0
    human_attention_seconds: int = 0

    def __post_init__(self) -> None:
        for label, value in (
            ("elapsed_ms", self.elapsed_ms),
            ("model_input_tokens", self.model_input_tokens),
            ("model_output_tokens", self.model_output_tokens),
            ("external_calls", self.external_calls),
            ("human_attention_seconds", self.human_attention_seconds),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise PersonalWorldModelError(f"feedback cost {label} is invalid")

    def to_payload(self) -> dict[str, int]:
        return {
            "elapsed_ms": self.elapsed_ms,
            "model_input_tokens": self.model_input_tokens,
            "model_output_tokens": self.model_output_tokens,
            "external_calls": self.external_calls,
            "human_attention_seconds": self.human_attention_seconds,
        }

    @classmethod
    def from_payload(cls, value: object) -> FeedbackCost:
        item = _exact_mapping(
            value,
            {
                "elapsed_ms",
                "model_input_tokens",
                "model_output_tokens",
                "external_calls",
                "human_attention_seconds",
            },
            "feedback cost",
        )
        return cls(**{key: _non_negative_int(item.get(key), key) for key in item})


@dataclass(frozen=True, slots=True)
class UserEvaluation:
    verdict: UserEvaluationVerdict
    rating: int | None = None
    note: str | None = None

    def __post_init__(self) -> None:
        verdict = _enum(self.verdict, UserEvaluationVerdict, "user evaluation verdict")
        object.__setattr__(self, "verdict", verdict)
        if self.rating is not None and (
            not isinstance(self.rating, int)
            or isinstance(self.rating, bool)
            or not 1 <= self.rating <= 5
        ):
            raise PersonalWorldModelError("user evaluation rating must be between 1 and 5")
        if verdict is UserEvaluationVerdict.NOT_PROVIDED and (
            self.rating is not None or self.note is not None
        ):
            raise PersonalWorldModelError("missing user evaluation cannot carry rating or note")
        if self.note is not None:
            object.__setattr__(
                self,
                "note",
                _narrative(self.note, "user evaluation note", maximum=1000),
            )

    def to_payload(self) -> dict[str, object]:
        return {"verdict": self.verdict.value, "rating": self.rating, "note": self.note}

    @classmethod
    def from_payload(cls, value: object) -> UserEvaluation:
        item = _exact_mapping(value, {"verdict", "rating", "note"}, "user evaluation")
        return cls(
            verdict=_enum(item.get("verdict"), UserEvaluationVerdict, "user evaluation verdict"),
            rating=item.get("rating"),
            note=item.get("note"),
        )


@dataclass(frozen=True, slots=True)
class FeedbackFact:
    feedback_id: str
    supersedes_feedback_id: str | None
    project_id: str
    action_id: str
    expected_outcome: str
    actual_outcome: str
    outcome: OutcomeStatus
    state_delta: tuple[StateDelta, ...]
    cost: FeedbackCost
    user_evaluation: UserEvaluation
    evidence_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "feedback_id", _identifier(self.feedback_id, "feedback id"))
        object.__setattr__(
            self,
            "supersedes_feedback_id",
            (
                None
                if self.supersedes_feedback_id is None
                else _identifier(self.supersedes_feedback_id, "superseded feedback id")
            ),
        )
        if self.supersedes_feedback_id == self.feedback_id:
            raise PersonalWorldModelError("feedback cannot supersede itself")
        object.__setattr__(self, "project_id", _identifier(self.project_id, "project id"))
        object.__setattr__(self, "action_id", _identifier(self.action_id, "action id"))
        object.__setattr__(
            self,
            "expected_outcome",
            _narrative(self.expected_outcome, "expected outcome", maximum=1200),
        )
        object.__setattr__(
            self,
            "actual_outcome",
            _narrative(self.actual_outcome, "actual outcome", maximum=2000),
        )
        object.__setattr__(self, "outcome", _enum(self.outcome, OutcomeStatus, "outcome"))
        deltas = tuple(self.state_delta)
        if not 1 <= len(deltas) <= 16 or not all(isinstance(item, StateDelta) for item in deltas):
            raise PersonalWorldModelError("feedback requires one to sixteen state deltas")
        object.__setattr__(self, "state_delta", deltas)
        if not isinstance(self.cost, FeedbackCost):
            raise PersonalWorldModelError("feedback cost is invalid")
        if not isinstance(self.user_evaluation, UserEvaluation):
            raise PersonalWorldModelError("feedback user evaluation is invalid")
        object.__setattr__(self, "evidence_refs", _evidence_refs(self.evidence_refs, minimum=1))

    def to_payload(self) -> dict[str, object]:
        return {
            "feedback_id": self.feedback_id,
            "supersedes_feedback_id": self.supersedes_feedback_id,
            "action_id": self.action_id,
            "expected_outcome": self.expected_outcome,
            "actual_outcome": self.actual_outcome,
            "outcome": self.outcome.value,
            "state_delta": [item.to_payload() for item in self.state_delta],
            "cost": self.cost.to_payload(),
            "user_evaluation": self.user_evaluation.to_payload(),
            "evidence_refs": list(self.evidence_refs),
        }

    @classmethod
    def from_payload(cls, project_id: str, value: object) -> FeedbackFact:
        item = _exact_mapping(
            value,
            {
                "feedback_id",
                "supersedes_feedback_id",
                "action_id",
                "expected_outcome",
                "actual_outcome",
                "outcome",
                "state_delta",
                "cost",
                "user_evaluation",
                "evidence_refs",
            },
            "feedback fact",
        )
        deltas = item.get("state_delta")
        refs = item.get("evidence_refs")
        if not isinstance(deltas, list) or not isinstance(refs, list):
            raise PersonalWorldModelError("feedback fact collections are invalid")
        return cls(
            feedback_id=_identifier(item.get("feedback_id"), "feedback id"),
            supersedes_feedback_id=(
                None
                if item.get("supersedes_feedback_id") is None
                else _identifier(item.get("supersedes_feedback_id"), "superseded feedback id")
            ),
            project_id=project_id,
            action_id=_identifier(item.get("action_id"), "action id"),
            expected_outcome=_narrative(item.get("expected_outcome"), "expected outcome", maximum=1200),
            actual_outcome=_narrative(item.get("actual_outcome"), "actual outcome", maximum=2000),
            outcome=_enum(item.get("outcome"), OutcomeStatus, "outcome"),
            state_delta=tuple(StateDelta.from_payload(delta) for delta in deltas),
            cost=FeedbackCost.from_payload(item.get("cost")),
            user_evaluation=UserEvaluation.from_payload(item.get("user_evaluation")),
            evidence_refs=tuple(refs),
        )


@dataclass(frozen=True, slots=True)
class WorldEventDraft:
    event_id: str
    project_id: str
    kind: WorldEventKind
    actor: str
    source_ref: str
    source_revision: str | None
    occurred_at: str | None
    recorded_at: str
    payload: Mapping[str, object]

    def __post_init__(self) -> None:
        event_id = _identifier(self.event_id, "event id")
        project_id = _identifier(self.project_id, "project id")
        kind = _enum(self.kind, WorldEventKind, "world event kind")
        actor = self.actor.strip() if isinstance(self.actor, str) else ""
        if actor not in {"user", "system", "effect", "external"}:
            raise PersonalWorldModelError("world event actor is invalid")
        source_ref = _evidence_ref(self.source_ref)
        source_revision = (
            None
            if self.source_revision is None
            else _identifier(self.source_revision, "source revision")
        )
        occurred_at = None if self.occurred_at is None else _timestamp(self.occurred_at, "occurred_at")
        recorded_at = _timestamp(self.recorded_at, "recorded_at")
        if occurred_at is not None and _parse_timestamp(occurred_at) > _parse_timestamp(recorded_at):
            raise PersonalWorldModelError("world event occurred_at cannot follow recorded_at")
        payload = _event_payload(kind, self.payload, project_id=project_id)
        object.__setattr__(self, "event_id", event_id)
        object.__setattr__(self, "project_id", project_id)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "actor", actor)
        object.__setattr__(self, "source_ref", source_ref)
        object.__setattr__(self, "source_revision", source_revision)
        object.__setattr__(self, "occurred_at", occurred_at)
        object.__setattr__(self, "recorded_at", recorded_at)
        frozen_payload = (
            _freeze_payload(payload)
            if kind in {
                WorldEventKind.PROVENANCE_SUBJECT_RECORDED,
                WorldEventKind.PROVENANCE_LINK_RECORDED,
                WorldEventKind.PROVENANCE_VALIDATION_RECORDED,
                WorldEventKind.TASK_GRAPH_EVENT_RECORDED,
                WorldEventKind.TRAJECTORY_CHECKPOINT_RECORDED,
                WorldEventKind.EVOLUTION_EVENT_RECORDED,
            }
            else MappingProxyType(payload)
        )
        object.__setattr__(self, "payload", frozen_payload)

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": "1.0.0",
            "event_id": self.event_id,
            "project_id": self.project_id,
            "kind": self.kind.value,
            "actor": self.actor,
            "source_ref": self.source_ref,
            "source_revision": self.source_revision,
            "occurred_at": self.occurred_at,
            "recorded_at": self.recorded_at,
            "payload": _plain(self.payload),
        }


@dataclass(frozen=True, slots=True)
class WorldEvent:
    event_id: str
    project_id: str
    sequence: int
    kind: WorldEventKind
    actor: str
    source_ref: str
    source_revision: str | None
    occurred_at: str | None
    recorded_at: str
    payload: Mapping[str, object]

    def __post_init__(self) -> None:
        if not isinstance(self.sequence, int) or isinstance(self.sequence, bool) or self.sequence < 1:
            raise PersonalWorldModelError("world event sequence is invalid")
        draft = WorldEventDraft(
            event_id=self.event_id,
            project_id=self.project_id,
            kind=self.kind,
            actor=self.actor,
            source_ref=self.source_ref,
            source_revision=self.source_revision,
            occurred_at=self.occurred_at,
            recorded_at=self.recorded_at,
            payload=self.payload,
        )
        for field in (
            "event_id",
            "project_id",
            "kind",
            "actor",
            "source_ref",
            "source_revision",
            "occurred_at",
            "recorded_at",
            "payload",
        ):
            object.__setattr__(self, field, getattr(draft, field))

    def to_record(self) -> dict[str, object]:
        return {**self.as_draft().identity_payload(), "sequence": self.sequence}

    def as_draft(self) -> WorldEventDraft:
        return WorldEventDraft(
            event_id=self.event_id,
            project_id=self.project_id,
            kind=self.kind,
            actor=self.actor,
            source_ref=self.source_ref,
            source_revision=self.source_revision,
            occurred_at=self.occurred_at,
            recorded_at=self.recorded_at,
            payload=self.payload,
        )

    @classmethod
    def from_record(cls, value: object) -> WorldEvent:
        item = _exact_mapping(
            value,
            {
                "schema_version",
                "event_id",
                "project_id",
                "sequence",
                "kind",
                "actor",
                "source_ref",
                "source_revision",
                "occurred_at",
                "recorded_at",
                "payload",
            },
            "world event",
        )
        if item.get("schema_version") != "1.0.0":
            raise PersonalWorldModelError("world event schema version is unsupported")
        return cls(
            event_id=_identifier(item.get("event_id"), "event id"),
            project_id=_identifier(item.get("project_id"), "project id"),
            sequence=_positive_int(item.get("sequence"), "sequence"),
            kind=_enum(item.get("kind"), WorldEventKind, "world event kind"),
            actor=str(item.get("actor") or ""),
            source_ref=str(item.get("source_ref") or ""),
            source_revision=item.get("source_revision"),
            occurred_at=item.get("occurred_at"),
            recorded_at=str(item.get("recorded_at") or ""),
            payload=_mapping(item.get("payload"), "world event payload"),
        )


def feedback_event_draft(
    fact: FeedbackFact,
    *,
    event_id: str,
    source_ref: str,
    source_revision: str | None,
    occurred_at: str | None,
    recorded_at: str,
    actor: str = "system",
) -> WorldEventDraft:
    """Wrap one complete outcome evaluation as an immutable WorldEvent."""

    if not isinstance(fact, FeedbackFact):
        raise PersonalWorldModelError("feedback fact is invalid")
    return WorldEventDraft(
        event_id=event_id,
        project_id=fact.project_id,
        kind=WorldEventKind.FEEDBACK_RECORDED,
        actor=actor,
        source_ref=source_ref,
        source_revision=source_revision,
        occurred_at=occurred_at,
        recorded_at=recorded_at,
        payload=fact.to_payload(),
    )


def _event_payload(
    kind: WorldEventKind,
    value: object,
    *,
    project_id: str,
) -> dict[str, object]:
    item = _mapping(value, "world event payload")
    if kind is WorldEventKind.GOAL_DECLARED:
        _exact_keys(item, {"goal_id", "title", "success_criteria", "target_at", "evidence_refs"}, kind)
        criteria = item.get("success_criteria")
        if not isinstance(criteria, Sequence) or isinstance(criteria, (str, bytes)) or not 1 <= len(criteria) <= 12:
            raise PersonalWorldModelError("goal success criteria are invalid")
        return {
            "goal_id": _identifier(item.get("goal_id"), "goal id"),
            "title": _narrative(item.get("title"), "goal title", maximum=512),
            "success_criteria": [
                _narrative(entry, "success criterion", maximum=512) for entry in criteria
            ],
            "target_at": _optional_timestamp(item.get("target_at"), "goal target_at"),
            "evidence_refs": list(_evidence_refs(item.get("evidence_refs"), minimum=1)),
        }
    if kind is WorldEventKind.PROJECT_OBSERVATION:
        _exact_keys(item, {"observation_id", "category", "summary", "evidence_refs"}, kind)
        category = item.get("category")
        if category not in {"resource", "constraint", "decision", "environment"}:
            raise PersonalWorldModelError("project observation category is invalid")
        return {
            "observation_id": _identifier(item.get("observation_id"), "observation id"),
            "category": str(category),
            "summary": _narrative(item.get("summary"), "observation summary", maximum=1200),
            "evidence_refs": list(_evidence_refs(item.get("evidence_refs"), minimum=1)),
        }
    if kind is WorldEventKind.TASK_OBSERVED:
        _exact_keys(item, {"task_id", "title", "state", "evidence_refs"}, kind)
        state = item.get("state")
        if state not in {"pending", "in_progress", "completed", "blocked"}:
            raise PersonalWorldModelError("project task state is invalid")
        return {
            "task_id": _identifier(item.get("task_id"), "task id"),
            "title": _narrative(item.get("title"), "task title", maximum=512),
            "state": str(state),
            "evidence_refs": list(_evidence_refs(item.get("evidence_refs"), minimum=1)),
        }
    if kind is WorldEventKind.BLOCKER_OPENED:
        _exact_keys(item, {"blocker_id", "summary", "severity", "evidence_refs"}, kind)
        severity = item.get("severity")
        if severity not in {"low", "medium", "high"}:
            raise PersonalWorldModelError("project blocker severity is invalid")
        return {
            "blocker_id": _identifier(item.get("blocker_id"), "blocker id"),
            "summary": _narrative(item.get("summary"), "blocker summary", maximum=1000),
            "severity": str(severity),
            "evidence_refs": list(_evidence_refs(item.get("evidence_refs"), minimum=1)),
        }
    if kind is WorldEventKind.BLOCKER_RESOLVED:
        _exact_keys(item, {"blocker_id", "resolution", "evidence_refs"}, kind)
        return {
            "blocker_id": _identifier(item.get("blocker_id"), "blocker id"),
            "resolution": _narrative(item.get("resolution"), "blocker resolution", maximum=1000),
            "evidence_refs": list(_evidence_refs(item.get("evidence_refs"), minimum=1)),
        }
    if kind is WorldEventKind.ACTION_PLANNED:
        _exact_keys(
            item,
            {
                "action_id",
                "title",
                "expected_outcome",
                "effect_class",
                "gate_requirement",
                "due_at",
                "evidence_refs",
            },
            kind,
        )
        effect_class = item.get("effect_class")
        if effect_class not in {"PURE", "IDEMPOTENT", "QUERYABLE", "AT_MOST_ONCE", "NEEDS_REAUTH"}:
            raise PersonalWorldModelError("planned action Effect class is invalid")
        gate = item.get("gate_requirement")
        if gate not in {"none", "notice", "approval"}:
            raise PersonalWorldModelError("planned action Gate requirement is invalid")
        return {
            "action_id": _identifier(item.get("action_id"), "action id"),
            "title": _narrative(item.get("title"), "action title", maximum=512),
            "expected_outcome": _narrative(item.get("expected_outcome"), "expected outcome", maximum=1200),
            "effect_class": str(effect_class),
            "gate_requirement": str(gate),
            "due_at": _optional_timestamp(item.get("due_at"), "action due_at"),
            "evidence_refs": list(_evidence_refs(item.get("evidence_refs"), minimum=1)),
        }
    if kind is WorldEventKind.FEEDBACK_RECORDED:
        return FeedbackFact.from_payload(project_id, item).to_payload()
    if kind is WorldEventKind.SUPERVISION_CLAIM_DECLARED:
        _exact_keys(
            item,
            {
                "claim_id", "supersedes_claim_id", "action_id", "hypothesis",
                "expected_signals", "falsification_signals", "checkpoint_policy",
                "pivot_conditions", "stop_conditions", "basis_sequence", "evidence_refs",
            },
            kind,
        )
        checkpoint_policy = item.get("checkpoint_policy")
        if checkpoint_policy not in {
            "before_effect", "after_effect", "child_fan_in", "elapsed",
        }:
            raise PersonalWorldModelError("supervision checkpoint policy is invalid")
        return {
            "claim_id": _identifier(item.get("claim_id"), "supervision claim id"),
            "supersedes_claim_id": (
                None if item.get("supersedes_claim_id") is None
                else _identifier(item.get("supersedes_claim_id"), "superseded supervision claim id")
            ),
            "action_id": _identifier(item.get("action_id"), "supervision action id"),
            "hypothesis": _narrative(item.get("hypothesis"), "supervision hypothesis", maximum=1200),
            "expected_signals": _supervision_signals(item.get("expected_signals"), "expected signals"),
            "falsification_signals": _supervision_signals(item.get("falsification_signals"), "falsification signals"),
            "checkpoint_policy": str(checkpoint_policy),
            "pivot_conditions": _supervision_signals(item.get("pivot_conditions"), "pivot conditions"),
            "stop_conditions": _supervision_signals(item.get("stop_conditions"), "stop conditions"),
            "basis_sequence": _non_negative_int(item.get("basis_sequence"), "supervision basis sequence"),
            "evidence_refs": list(_evidence_refs(item.get("evidence_refs"), minimum=1)),
        }
    if kind is WorldEventKind.SUPERVISION_VERIFICATION_RECORDED:
        _exact_keys(
            item,
            {
                "verification_id", "claim_id", "action_id", "verdict",
                "finding", "checked_world_sequence", "evidence_refs",
            },
            kind,
        )
        verdict = item.get("verdict")
        if verdict not in {"supported", "weakened", "refuted", "inconclusive"}:
            raise PersonalWorldModelError("supervision verification verdict is invalid")
        return {
            "verification_id": _identifier(item.get("verification_id"), "supervision verification id"),
            "claim_id": _identifier(item.get("claim_id"), "supervision claim id"),
            "action_id": _identifier(item.get("action_id"), "supervision action id"),
            "finding": _narrative(item.get("finding"), "supervision finding", maximum=1200),
            "verdict": str(verdict),
            "checked_world_sequence": _non_negative_int(item.get("checked_world_sequence"), "supervision checked world sequence"),
            "evidence_refs": list(_evidence_refs(item.get("evidence_refs"), minimum=1)),
        }
    if kind is WorldEventKind.SUPERVISION_DECISION_RECORDED:
        _exact_keys(
            item,
            {
                "decision_id", "claim_id", "verification_id", "action_id",
                "disposition", "rationale", "evidence_refs",
            },
            kind,
        )
        disposition = item.get("disposition")
        if disposition not in {
            "continue", "replan_required", "stop_required", "escalate_user",
        }:
            raise PersonalWorldModelError("supervision decision disposition is invalid")
        return {
            "decision_id": _identifier(item.get("decision_id"), "supervision decision id"),
            "claim_id": _identifier(item.get("claim_id"), "supervision claim id"),
            "verification_id": _identifier(item.get("verification_id"), "supervision verification id"),
            "action_id": _identifier(item.get("action_id"), "supervision action id"),
            "disposition": str(disposition),
            "rationale": _narrative(item.get("rationale"), "supervision decision rationale", maximum=1200),
            "evidence_refs": list(_evidence_refs(item.get("evidence_refs"), minimum=1)),
        }
    if kind is WorldEventKind.PROVENANCE_SUBJECT_RECORDED:
        _exact_keys(item, {"subject", "version"}, kind)
        try:
            subject = TraceSubject.from_payload(item.get("subject"))
            version = VersionBinding.from_payload(item.get("version"))
        except ProvenanceContractError as error:
            raise PersonalWorldModelError("provenance subject payload is invalid") from error
        if subject.project_id != project_id:
            raise PersonalWorldModelError("provenance subject project scope conflicts")
        return _deep_freeze({
            "subject": subject.to_payload(),
            "version": version.to_payload(),
        })
    if kind is WorldEventKind.PROVENANCE_LINK_RECORDED:
        try:
            link = TraceLink.from_payload(item)
        except ProvenanceContractError as error:
            raise PersonalWorldModelError("provenance link payload is invalid") from error
        if link.project_id != project_id:
            raise PersonalWorldModelError("provenance link project scope conflicts")
        return _deep_freeze(link.to_payload())
    if kind is WorldEventKind.PROVENANCE_VALIDATION_RECORDED:
        try:
            validation = ValidationFact.from_payload(item)
        except ProvenanceContractError as error:
            raise PersonalWorldModelError("provenance validation payload is invalid") from error
        if validation.project_id != project_id:
            raise PersonalWorldModelError("provenance validation project scope conflicts")
        return _deep_freeze(validation.to_payload())
    if kind is WorldEventKind.TASK_GRAPH_EVENT_RECORDED:
        try:
            graph_event = TaskGraphEvent.from_payload(item)
        except TaskGraphContractError as error:
            raise PersonalWorldModelError("task graph event payload is invalid") from error
        if graph_event.project_id != project_id:
            raise PersonalWorldModelError("task graph event project scope conflicts")
        return _deep_freeze(graph_event.to_payload())
    if kind is WorldEventKind.TRAJECTORY_CHECKPOINT_RECORDED:
        try:
            checkpoint = TrustCheckpoint.from_payload(item)
        except TrajectoryContractError as error:
            raise PersonalWorldModelError("trajectory checkpoint payload is invalid") from error
        if checkpoint.project_id != project_id:
            raise PersonalWorldModelError("trajectory checkpoint project scope conflicts")
        if any(value is None for value in (
            checkpoint.workload_snapshot_ref,
            checkpoint.workload_snapshot_revision,
            checkpoint.capacity_snapshot_ref,
            checkpoint.capacity_snapshot_revision,
            checkpoint.main_run_id,
            checkpoint.main_cancel_epoch,
            checkpoint.frozen_agent_budget,
        )):
            raise PersonalWorldModelError("trajectory checkpoint dispatch authority is required")
        return _deep_freeze(checkpoint.to_payload())
    if kind is WorldEventKind.EVOLUTION_EVENT_RECORDED:
        try:
            evolution_event = EvolutionEvent.from_payload(item)
        except EvolutionContractError as error:
            raise PersonalWorldModelError("evolution event payload is invalid") from error
        return _deep_freeze(evolution_event.to_payload())
    if kind is WorldEventKind.CLOCK_OBSERVED:
        _exact_keys(item, {"reason", "evidence_refs"}, kind)
        return {
            "reason": _narrative(item.get("reason"), "clock observation reason", maximum=512),
            "evidence_refs": list(_evidence_refs(item.get("evidence_refs"), minimum=1)),
        }
    raise PersonalWorldModelError("world event kind is unsupported")


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


def _deep_freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _deep_freeze(item) for key, item in value.items()})
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(_deep_freeze(item) for item in value)
    return value


def _freeze_payload(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze_payload(item) for key, item in value.items()})
    if isinstance(value, (tuple, list)):
        return tuple(_freeze_payload(item) for item in value)
    return value


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise PersonalWorldModelError(f"{label} must be an object")
    return value


def _exact_mapping(value: object, fields: set[str], label: str) -> Mapping[str, object]:
    item = _mapping(value, label)
    if set(item) != fields:
        raise PersonalWorldModelError(f"{label} shape is invalid")
    return item


def _exact_keys(value: Mapping[str, object], fields: set[str], kind: WorldEventKind) -> None:
    if set(value) != fields:
        raise PersonalWorldModelError(f"{kind.value} payload shape is invalid")


def _supervision_signals(value: object, label: str) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or not 1 <= len(value) <= 8:
        raise PersonalWorldModelError(f"supervision {label} are invalid")
    return [_narrative(item, f"supervision {label}", maximum=512) for item in value]


def _identifier(value: object, label: str) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if _ID.fullmatch(text) is None:
        raise PersonalWorldModelError(f"{label} is invalid")
    return text


def _narrative(value: object, label: str, *, maximum: int) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not text or len(text.encode("utf-8")) > maximum:
        raise PersonalWorldModelError(f"{label} is invalid or exceeds its budget")
    if "\x00" in text or _SECRET.search(text) or _ABSOLUTE_PATH.search(text) or _LOCATOR.search(text):
        raise PersonalWorldModelError(f"{label} contains material that must remain an evidence reference")
    return text


def _evidence_ref(value: object) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if (
        _OPAQUE_REF.fullmatch(text) is None
        or "\\" in text
        or any(part == ".." for part in text.split("/"))
    ):
        raise PersonalWorldModelError("evidence reference must be an opaque crp ref")
    return text


def _evidence_refs(value: object, *, minimum: int) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise PersonalWorldModelError("evidence references must be an array")
    refs = tuple(_evidence_ref(item) for item in value)
    if not minimum <= len(refs) <= 32 or len(refs) != len(set(refs)):
        raise PersonalWorldModelError("evidence references are missing, duplicated, or over budget")
    return refs


def _timestamp(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise PersonalWorldModelError(f"{label} must be ISO-8601")
    parsed = _parse_timestamp(value)
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _optional_timestamp(value: object, label: str) -> str | None:
    return None if value is None else _timestamp(value, label)


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise PersonalWorldModelError("timestamp must be ISO-8601") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PersonalWorldModelError("timestamp must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def parse_world_timestamp(value: str) -> datetime:
    """Parse one validated world-model timestamp for projection calculations."""

    return _parse_timestamp(_timestamp(value, "timestamp"))


def validate_world_identifier(value: object, label: str = "identifier") -> str:
    """Validate an identifier at application boundaries without duplicating the contract."""

    return _identifier(value, label)


def validate_world_evidence_ref(value: object) -> str:
    """Validate a resolvable opaque evidence reference at application boundaries."""

    return _evidence_ref(value)


def validate_world_narrative(
    value: object,
    label: str = "narrative",
    *,
    maximum: int = 2000,
) -> str:
    """Apply the WorldEvent privacy and locator scan to explicit proposal text."""

    if not isinstance(maximum, int) or isinstance(maximum, bool) or maximum < 1:
        raise PersonalWorldModelError("narrative byte budget is invalid")
    return _narrative(value, label, maximum=maximum)


def _positive_int(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise PersonalWorldModelError(f"{label} must be positive")
    return value


def _non_negative_int(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise PersonalWorldModelError(f"{label} must be non-negative")
    return value


def _json_scalar(value: object) -> bool:
    return value is None or isinstance(value, (str, int, bool))


def _enum(value: object, enum_type, label: str):
    try:
        return value if isinstance(value, enum_type) else enum_type(value)
    except (TypeError, ValueError) as error:
        raise PersonalWorldModelError(f"{label} is invalid") from error
