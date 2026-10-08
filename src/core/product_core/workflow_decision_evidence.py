from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from .ports import ObjectStorePort
from .workflow_progression import (
    WorkflowDecisionBoundary,
    WorkflowProgressionMode,
    decide_workflow_progression,
)


class WorkflowTransitionAction(StrEnum):
    NEXT_EFFECTS = "next_effects"
    NEED_USER = "need_user"
    DONE = "done"
    BLOCKED = "blocked"


class WorkflowGateOutcome(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class WorkflowUserChoice(StrEnum):
    APPROVE = "approve"
    ABANDON = "abandon"


class WorkflowDecisionReason(StrEnum):
    GATE_DENIED = "gate_denied"
    GATE_ASK = "gate_ask"
    EFFECTS_TERMINAL = "effects_terminal"
    USER_ABANDONED = "user_abandoned"
    USER_APPROVED_RECHECK = "user_approved_recheck"
    DETERMINISTIC = "deterministic"
    REVERSIBLE_VISIBLE_CHANGE = "reversible_visible_change"
    PERMISSION_EXPANSION = "permission_expansion"
    IRREVERSIBLE_CHANGE = "irreversible_change"
    BUDGET_EXCEEDED = "budget_exceeded"
    MATERIAL_AMBIGUITY = "material_ambiguity"
    UNKNOWN_EFFECT = "unknown_effect"
    EXTERNAL_DOWNLOAD_WRITES_FILE = "external_download_writes_file"
    FORMAL_MEMORY_PUBLICATION = "formal_memory_publication"
    HARD_REDACT = "hard_redact"
    HIGH_RISK = "high_risk"


@dataclass(frozen=True, slots=True)
class WorkflowTransition:
    schema_version: str
    action: WorkflowTransitionAction
    progression_mode: WorkflowProgressionMode
    reasons: tuple[str, ...]
    decision_ref: str | None = None

    def __post_init__(self) -> None:
        if self.schema_version != "1.0.0":
            raise WorkflowDecisionEvidenceError("workflow transition schema version is invalid")
        _closed_reasons(self.reasons)

    def to_projection(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "action": self.action.value,
            "progression_mode": self.progression_mode.value,
            "reasons": list(self.reasons),
            "decision_ref": self.decision_ref,
        }


class WorkflowDecisionEvidenceError(ValueError):
    pass


def decide_workflow_transition(
    *,
    effect_states: Sequence[str],
    boundaries: Sequence[WorkflowDecisionBoundary],
    gate_outcome: WorkflowGateOutcome,
    intent_fingerprint: str,
    user_decision: Mapping[str, object] | None = None,
) -> WorkflowTransition:
    """Purely project workflow state from frozen intent, Gate and Effect history."""
    fingerprint = _fingerprint(intent_fingerprint)
    states = tuple(str(state) for state in effect_states)
    if any(state not in {
        "PLANNED", "INFLIGHT", "SETTLED_OK", "SETTLED_ERR", "UNKNOWN",
        "COMPENSATED", "ABANDONED",
    } for state in states):
        raise WorkflowDecisionEvidenceError("workflow effect state is invalid")
    progression = decide_workflow_progression(*boundaries)
    decision = _validated_decision(user_decision, fingerprint) if user_decision is not None else None

    if gate_outcome is WorkflowGateOutcome.DENY:
        return WorkflowTransition(
            "1.0.0", WorkflowTransitionAction.BLOCKED, WorkflowProgressionMode.ASK,
            ("gate_denied",),
        )
    ask_reasons: list[str] = []
    if "UNKNOWN" in states:
        ask_reasons.append("unknown_effect")
    ask_reasons.extend(
        boundary.value
        for boundary in boundaries
        if decide_workflow_progression(boundary).mode is WorkflowProgressionMode.ASK
    )
    if gate_outcome is WorkflowGateOutcome.ASK:
        ask_reasons.append("gate_ask")
    asks = tuple(dict.fromkeys(ask_reasons))
    if asks:
        if decision is None:
            return WorkflowTransition(
                "1.0.0", WorkflowTransitionAction.NEED_USER, WorkflowProgressionMode.ASK, asks,
            )
        if decision["choice"] == WorkflowUserChoice.ABANDON.value:
            return WorkflowTransition(
                "1.0.0", WorkflowTransitionAction.DONE, WorkflowProgressionMode.ASK,
                (*asks, "user_abandoned"), str(decision["decision_ref"]),
            )
        return WorkflowTransition(
            "1.0.0", WorkflowTransitionAction.NEXT_EFFECTS, WorkflowProgressionMode.AUTO,
            (*asks, "user_approved_recheck"), str(decision["decision_ref"]),
        )
    if states and all(state in {"SETTLED_OK", "COMPENSATED", "ABANDONED"} for state in states):
        return WorkflowTransition(
            "1.0.0", WorkflowTransitionAction.DONE, progression.mode, ("effects_terminal",),
        )
    return WorkflowTransition(
        "1.0.0", WorkflowTransitionAction.NEXT_EFFECTS, progression.mode,
        (progression.reason.value,),
    )


class WorkflowUserDecisionRepository:
    collection = "workflow_user_decisions"

    def __init__(self, store: ObjectStorePort) -> None:
        self._store = store

    def record(
        self,
        *,
        workflow_id: str,
        intent_fingerprint: str,
        reasons: Sequence[str],
        choice: WorkflowUserChoice,
        decided_at: str,
        confirm: bool,
    ) -> Mapping[str, object]:
        if confirm is not True:
            raise WorkflowDecisionEvidenceError("workflow user decision requires confirm=true")
        clean_workflow = _required(workflow_id, "workflow_id")
        fingerprint = _fingerprint(intent_fingerprint)
        clean_reasons = _closed_reasons(
            tuple(dict.fromkeys(_required(reason, "reason") for reason in reasons))
        )
        if not clean_reasons or len(clean_reasons) > 16:
            raise WorkflowDecisionEvidenceError("workflow decision reasons are invalid")
        timestamp = _required(decided_at, "decided_at")
        identity = hashlib.sha256(json.dumps({
            "workflow_id": clean_workflow,
            "intent_fingerprint": fingerprint,
            "reasons": clean_reasons,
            "choice": choice.value,
        }, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:24]
        decision_id = f"workflow-decision-{identity}"
        payload = {
            "schema_version": "1.0.0", "id": decision_id,
            "decision_ref": f"decision:{decision_id}",
            "workflow_id": clean_workflow, "intent_fingerprint": fingerprint,
            "reasons": list(clean_reasons), "choice": choice.value,
            "decided_at": timestamp,
        }
        prior = self.find(
            workflow_id=clean_workflow, intent_fingerprint=fingerprint,
        )
        if prior is not None and dict(prior) != payload:
            raise WorkflowDecisionEvidenceError(
                "workflow user decision is immutable for the frozen intent"
            )
        existing = self._store.read(self.collection, decision_id)
        if existing is not None and dict(existing) != payload:
            raise WorkflowDecisionEvidenceError("workflow decision identity drifted")
        if existing is None:
            self._store.write(self.collection, decision_id, payload, expected_revision=0)
        return payload

    def find(
        self, *, workflow_id: str, intent_fingerprint: str,
    ) -> Mapping[str, object] | None:
        clean_workflow = _required(workflow_id, "workflow_id")
        fingerprint = _fingerprint(intent_fingerprint)
        matches = tuple(
            item for item in self._store.list(self.collection)
            if item.get("workflow_id") == clean_workflow
            and item.get("intent_fingerprint") == fingerprint
        )
        if len(matches) > 1:
            raise WorkflowDecisionEvidenceError("workflow user decision history is ambiguous")
        return matches[0] if matches else None


def _validated_decision(value: Mapping[str, object], fingerprint: str) -> dict[str, object]:
    payload = dict(value)
    if payload.get("intent_fingerprint") != fingerprint:
        raise WorkflowDecisionEvidenceError("workflow decision intent drifted")
    if payload.get("choice") not in {item.value for item in WorkflowUserChoice}:
        raise WorkflowDecisionEvidenceError("workflow user choice is invalid")
    decision_ref = payload.get("decision_ref")
    if not isinstance(decision_ref, str) or not decision_ref.startswith("decision:workflow-decision-"):
        raise WorkflowDecisionEvidenceError("workflow decision ref is invalid")
    return payload


def _fingerprint(value: object) -> str:
    text = str(value or "")
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise WorkflowDecisionEvidenceError("workflow intent fingerprint is invalid")
    return text


def _required(value: object, label: str) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not text or len(text) > 256:
        raise WorkflowDecisionEvidenceError(f"workflow {label} is invalid")
    return text


def _closed_reasons(reasons: Sequence[str]) -> tuple[str, ...]:
    allowed = {reason.value for reason in WorkflowDecisionReason}
    clean = tuple(str(reason) for reason in reasons)
    if any(reason not in allowed for reason in clean):
        raise WorkflowDecisionEvidenceError("workflow decision reason is not in the closed schema")
    return clean
