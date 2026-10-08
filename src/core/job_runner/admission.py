"""Caller-owned v2 admission evidence for Job-derived Effects.

This module deliberately does not evaluate a Gate.  A boundary that has
already evaluated policy must pass its durable :class:`GateDecisionFact` here;
Job code cannot manufacture an allow decision as a fallback.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType

from core.effect_log import EFFECT_V2, V2_REVISION_KEYS, EffectIntent, GateDecision, GateDecisionFact


class JobAdmissionCommandKind(StrEnum):
    """Commands that may create a new Job-derived execution attempt."""

    ADMIT = "admit"
    RETRY = "retry"
    RESUME = "resume"


@dataclass(frozen=True, slots=True)
class JobAdmissionAuthorization:
    """Immutable authorization evidence supplied by a Job command boundary.

    It is intentionally a value contract, not a Gate adapter.  In particular,
    it has no defaults and it never constructs ``GateDecisionFact``.  A caller
    must retain the authorization alongside its domain facts and pass the same
    evidence to ``EffectLog.plan_v2_in_connection``.
    """

    job_id: str
    admission_ref: str
    command_kind: JobAdmissionCommandKind
    gate_decision_id: str
    gate_fact: GateDecisionFact
    revision_set: Mapping[str, str]
    intent_refs: Mapping[str, str]
    admitted_at: int
    _gate_decision_digest: str = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        _require_text(self.job_id, "job_id")
        _require_text(self.admission_ref, "admission_ref")
        _require_text(self.gate_decision_id, "gate_decision_id")
        if not isinstance(self.command_kind, JobAdmissionCommandKind):
            raise TypeError("command_kind must be a JobAdmissionCommandKind")
        if not isinstance(self.gate_fact, GateDecisionFact):
            raise TypeError("gate_fact must be a complete GateDecisionFact")
        # A MUTATE decision must first materialize its changed intent at the
        # command boundary.  This contract only authorizes the resulting ALLOW.
        if self.gate_fact.decision is not GateDecision.ALLOW:
            raise ValueError("Job admission requires a boundary-supplied allow Gate decision")
        if not isinstance(self.admitted_at, int) or isinstance(self.admitted_at, bool) or self.admitted_at < 0:
            raise ValueError("admitted_at must be a non-negative Unix timestamp")

        revisions = _freeze_revision_set(self.revision_set)
        if self.gate_fact.policy_revision != revisions["policy"]:
            raise ValueError("Gate policy revision drifted from Job admission authority set")
        refs = _freeze_intent_refs(self.intent_refs)
        object.__setattr__(self, "revision_set", revisions)
        object.__setattr__(self, "intent_refs", refs)
        object.__setattr__(self, "_gate_decision_digest", self.gate_fact.decision_digest)

    def validate_for_intent(self, intent: EffectIntent) -> None:
        """Reject any drift before caller-owned v2 planning.

        The command boundary should call this immediately before handing the
        evidence and intent to ``plan_v2_in_connection``.  It detects mutation
        of the supplied Gate fact as well as mismatched intent/gate/revisions.
        """

        if not isinstance(intent, EffectIntent):
            raise TypeError("intent must be an EffectIntent")
        if intent.contract_version != EFFECT_V2:
            raise ValueError("Job admission only permits effect-v2 intents")
        if self.gate_fact.decision_digest != self._gate_decision_digest:
            raise ValueError("Gate decision fact changed after Job admission")
        if intent.gate_decision_id != self.gate_decision_id:
            raise ValueError("Job admission Gate decision id drifted from intent")
        if intent.root_id != self.job_id:
            raise ValueError("Job admission identity drifted from intent")
        if dict(intent.rev_set) != dict(self.revision_set):
            raise ValueError("Job admission authority revisions drifted from intent")
        if intent.intent_ref not in self.intent_refs.values():
            raise ValueError("Job admission does not authorize this intent reference")
        if intent.payload.get("admission_ref") != self.admission_ref:
            raise ValueError("Job admission reference drifted from intent")
        if intent.payload.get("mode") != self.command_kind.value:
            raise ValueError("Job admission command kind drifted from intent")


def _freeze_revision_set(value: Mapping[str, str]) -> Mapping[str, str]:
    if not isinstance(value, Mapping) or set(value) != set(V2_REVISION_KEYS):
        raise ValueError("Job admission requires the exact v2 authority revision set")
    copied = dict(value)
    for key, revision in copied.items():
        _require_text(key, "v2 revision key")
        _require_text(revision, f"revision_set.{key}")
    return MappingProxyType(copied)


def _freeze_intent_refs(value: Mapping[str, str]) -> Mapping[str, str]:
    if not isinstance(value, Mapping) or not value:
        raise ValueError("Job admission requires at least one intent reference")
    copied = dict(value)
    for name, ref in copied.items():
        _require_text(name, "intent_refs key")
        _require_text(ref, f"intent_refs.{name}")
    if len(set(copied.values())) != len(copied):
        raise ValueError("Job admission intent references must be unique")
    return MappingProxyType(copied)


def _require_text(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
