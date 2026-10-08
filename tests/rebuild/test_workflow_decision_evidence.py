from __future__ import annotations

import pytest

from core.product_core.workflow_decision_evidence import (
    WorkflowDecisionEvidenceError,
    WorkflowGateOutcome,
    WorkflowTransitionAction,
    WorkflowUserChoice,
    WorkflowUserDecisionRepository,
    decide_workflow_transition,
)
from core.product_core.workflow_progression import WorkflowDecisionBoundary
from core.storage_provider import JsonObjectStore


FINGERPRINT = "a" * 64


def _store(tmp_path):
    return JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")


def test_pure_transition_aggregates_gate_unknown_budget_and_permission_ask():
    transition = decide_workflow_transition(
        effect_states=("SETTLED_OK", "UNKNOWN"),
        boundaries=(
            WorkflowDecisionBoundary.BUDGET_EXCEEDED,
            WorkflowDecisionBoundary.PERMISSION_EXPANSION,
        ),
        gate_outcome=WorkflowGateOutcome.ASK,
        intent_fingerprint=FINGERPRINT,
    )
    assert transition.action is WorkflowTransitionAction.NEED_USER
    assert transition.reasons == (
        "unknown_effect", "budget_exceeded", "permission_expansion", "gate_ask",
    )


def test_exact_user_decision_replays_as_recheck_or_terminal_abandon(tmp_path):
    repository = WorkflowUserDecisionRepository(_store(tmp_path))
    approved = repository.record(
        workflow_id="workflow-a", intent_fingerprint=FINGERPRINT,
        reasons=("unknown_effect",), choice=WorkflowUserChoice.APPROVE,
        decided_at="2026-08-30T20:00:00+00:00", confirm=True,
    )
    replay = repository.record(
        workflow_id="workflow-a", intent_fingerprint=FINGERPRINT,
        reasons=("unknown_effect",), choice=WorkflowUserChoice.APPROVE,
        decided_at="2026-08-30T20:00:00+00:00", confirm=True,
    )
    assert replay == approved
    resumed = decide_workflow_transition(
        effect_states=("UNKNOWN",), boundaries=(WorkflowDecisionBoundary.UNKNOWN_EFFECT,),
        gate_outcome=WorkflowGateOutcome.ASK, intent_fingerprint=FINGERPRINT,
        user_decision=approved,
    )
    assert resumed.action is WorkflowTransitionAction.NEXT_EFFECTS
    assert resumed.decision_ref == approved["decision_ref"]

    abandoned = repository.record(
        workflow_id="workflow-b", intent_fingerprint=FINGERPRINT,
        reasons=("unknown_effect",), choice=WorkflowUserChoice.ABANDON,
        decided_at="2026-08-30T20:01:00+00:00", confirm=True,
    )
    stopped = decide_workflow_transition(
        effect_states=("UNKNOWN",), boundaries=(WorkflowDecisionBoundary.UNKNOWN_EFFECT,),
        gate_outcome=WorkflowGateOutcome.ASK, intent_fingerprint=FINGERPRINT,
        user_decision=abandoned,
    )
    assert stopped.action is WorkflowTransitionAction.DONE


def test_decision_cannot_bypass_gate_deny_or_intent_drift(tmp_path):
    decision = WorkflowUserDecisionRepository(_store(tmp_path)).record(
        workflow_id="workflow-a", intent_fingerprint=FINGERPRINT,
        reasons=("permission_expansion",), choice=WorkflowUserChoice.APPROVE,
        decided_at="2026-08-30T20:00:00+00:00", confirm=True,
    )
    denied = decide_workflow_transition(
        effect_states=("PLANNED",), boundaries=(WorkflowDecisionBoundary.PERMISSION_EXPANSION,),
        gate_outcome=WorkflowGateOutcome.DENY, intent_fingerprint=FINGERPRINT,
        user_decision=decision,
    )
    assert denied.action is WorkflowTransitionAction.BLOCKED
    with pytest.raises(WorkflowDecisionEvidenceError, match="intent drifted"):
        decide_workflow_transition(
            effect_states=("UNKNOWN",), boundaries=(WorkflowDecisionBoundary.UNKNOWN_EFFECT,),
            gate_outcome=WorkflowGateOutcome.ASK, intent_fingerprint="b" * 64,
            user_decision=decision,
        )


def test_deterministic_notice_and_terminal_projection():
    automatic = decide_workflow_transition(
        effect_states=("PLANNED",), boundaries=(WorkflowDecisionBoundary.DETERMINISTIC,),
        gate_outcome=WorkflowGateOutcome.ALLOW, intent_fingerprint=FINGERPRINT,
    )
    notice = decide_workflow_transition(
        effect_states=("PLANNED",),
        boundaries=(WorkflowDecisionBoundary.REVERSIBLE_VISIBLE_CHANGE,),
        gate_outcome=WorkflowGateOutcome.ALLOW, intent_fingerprint=FINGERPRINT,
    )
    done = decide_workflow_transition(
        effect_states=("SETTLED_OK", "COMPENSATED"),
        boundaries=(WorkflowDecisionBoundary.DETERMINISTIC,),
        gate_outcome=WorkflowGateOutcome.ALLOW, intent_fingerprint=FINGERPRINT,
    )
    assert automatic.action is WorkflowTransitionAction.NEXT_EFFECTS
    assert automatic.progression_mode.value == "auto"
    assert notice.progression_mode.value == "auto_with_notice"
    assert done.action is WorkflowTransitionAction.DONE
    assert notice.to_projection() == {
        "schema_version": "1.0.0",
        "action": "next_effects",
        "progression_mode": "auto_with_notice",
        "reasons": ["reversible_visible_change"],
        "decision_ref": None,
    }


def test_hard_redact_is_a_closed_ask_reason():
    transition = decide_workflow_transition(
        effect_states=(), boundaries=(WorkflowDecisionBoundary.HARD_REDACT,),
        gate_outcome=WorkflowGateOutcome.ALLOW, intent_fingerprint=FINGERPRINT,
    )
    assert transition.action is WorkflowTransitionAction.NEED_USER
    assert transition.reasons == ("hard_redact",)


def test_decision_evidence_rejects_open_ended_reason_codes(tmp_path):
    with pytest.raises(WorkflowDecisionEvidenceError, match="closed schema"):
        WorkflowUserDecisionRepository(_store(tmp_path)).record(
            workflow_id="workflow-a", intent_fingerprint=FINGERPRINT,
            reasons=("private_confirmation_state",), choice=WorkflowUserChoice.APPROVE,
            decided_at="2026-08-30T20:00:00+00:00", confirm=True,
        )


def test_frozen_intent_accepts_only_one_immutable_user_choice(tmp_path):
    repository = WorkflowUserDecisionRepository(_store(tmp_path))
    first = repository.record(
        workflow_id="workflow-a", intent_fingerprint=FINGERPRINT,
        reasons=("unknown_effect",), choice=WorkflowUserChoice.APPROVE,
        decided_at="2026-08-30T20:00:00+00:00", confirm=True,
    )
    assert repository.find(
        workflow_id="workflow-a", intent_fingerprint=FINGERPRINT,
    ) == first
    with pytest.raises(WorkflowDecisionEvidenceError, match="immutable"):
        repository.record(
            workflow_id="workflow-a", intent_fingerprint=FINGERPRINT,
            reasons=("unknown_effect",), choice=WorkflowUserChoice.ABANDON,
            decided_at="2026-08-30T20:01:00+00:00", confirm=True,
        )
