from __future__ import annotations

from dataclasses import replace

import pytest

from core.effect_log import (
    EFFECT_V2,
    NOT_APPLICABLE,
    V2_REVISION_KEYS,
    EffectClass,
    EffectIntent,
    GateDecision,
    GateDecisionFact,
)
from core.job_runner.admission import JobAdmissionAuthorization, JobAdmissionCommandKind


def _revisions() -> dict[str, str]:
    revisions = {key: NOT_APPLICABLE for key in V2_REVISION_KEYS}
    revisions.update(policy="policy-v2", handler="handler-v2", budget="budget-v2")
    return revisions


def _gate(*, decision: GateDecision = GateDecision.ALLOW, policy_revision: str = "policy-v2") -> GateDecisionFact:
    return GateDecisionFact(
        decision=decision,
        rule_ref="rule:job-admission",
        scope_ref="scope:job-admission",
        budget_after={},
        secret_scope="scope:secret-job-admission",
        policy_revision=policy_revision,
    )


def _authorization(**changes: object) -> JobAdmissionAuthorization:
    values: dict[str, object] = {
        "job_id": "root-job-admission",
        "admission_ref": "facts:job-admission-1",
        "command_kind": JobAdmissionCommandKind.ADMIT,
        "gate_decision_id": "gate:job-admission-1",
        "gate_fact": _gate(),
        "revision_set": _revisions(),
        "intent_refs": {"job_execution": "intent:job-execution-1", "step_1": "intent:job-step-1"},
        "admitted_at": 1,
    }
    values.update(changes)
    return JobAdmissionAuthorization(**values)  # type: ignore[arg-type]


def _intent(**changes: object) -> EffectIntent:
    values: dict[str, object] = {
        "session_id": "session-job-admission",
        "root_id": "root-job-admission",
        "step_key": "job-execution",
        "kind": "job-execution",
        "effect_class": EffectClass.IDEMPOTENT,
        "intent_ref": "intent:job-execution-1",
        "gate_decision_id": "gate:job-admission-1",
        "rev_set": _revisions(),
        "payload": {
            "job_ref": "facts:job-admission-1",
            "admission_ref": "facts:job-admission-1",
            "mode": "admit",
        },
        "contract_version": EFFECT_V2,
        "intent_schema_version": "job-execution/v2",
        "expected_receipt_kind": "job-execution.receipt",
        "expected_receipt_schema_version": "job-execution-receipt/v2",
    }
    values.update(changes)
    return EffectIntent(**values)  # type: ignore[arg-type]


def test_admission_captures_boundary_evidence_without_constructing_gate() -> None:
    authorization = _authorization()

    assert authorization.command_kind is JobAdmissionCommandKind.ADMIT
    assert dict(authorization.revision_set)["policy"] == "policy-v2"
    assert dict(authorization.intent_refs)["job_execution"] == "intent:job-execution-1"
    with pytest.raises(TypeError):
        authorization.revision_set["policy"] = "changed"  # type: ignore[index]


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"admission_ref": ""}, "admission_ref"),
        ({"job_id": ""}, "job_id"),
        ({"command_kind": "admit"}, "command_kind"),
        ({"gate_decision_id": ""}, "gate_decision_id"),
        ({"intent_refs": {}}, "intent reference"),
        ({"intent_refs": {"one": "intent:same", "two": "intent:same"}}, "must be unique"),
        ({"admitted_at": True}, "admitted_at"),
        ({"gate_fact": _gate(decision=GateDecision.DENY)}, "allow Gate"),
        ({"revision_set": {"policy": "policy-v2"}}, "exact v2"),
        ({"gate_fact": _gate(policy_revision="policy-other")}, "policy revision drifted"),
    ],
)
def test_admission_rejects_missing_or_untrusted_evidence(changes: dict[str, object], message: str) -> None:
    with pytest.raises((TypeError, ValueError), match=message):
        _authorization(**changes)


def test_admission_rejects_drift_before_v2_planning() -> None:
    authorization = _authorization()
    authorization.validate_for_intent(_intent())

    with pytest.raises(ValueError, match="Gate decision id drifted"):
        authorization.validate_for_intent(_intent(gate_decision_id="gate:other"))
    with pytest.raises(ValueError, match="identity drifted"):
        authorization.validate_for_intent(_intent(root_id="root-other"))
    with pytest.raises(ValueError, match="authority revisions drifted"):
        authorization.validate_for_intent(_intent(rev_set={**_revisions(), "policy": "policy-other"}))
    with pytest.raises(ValueError, match="does not authorize"):
        authorization.validate_for_intent(_intent(intent_ref="intent:not-admitted"))
    with pytest.raises(ValueError, match="reference drifted"):
        authorization.validate_for_intent(_intent(payload={
            "job_ref": "facts:job-admission-1",
            "admission_ref": "facts:other",
            "mode": "admit",
        }))
    with pytest.raises(ValueError, match="command kind drifted"):
        authorization.validate_for_intent(_intent(payload={
            "job_ref": "facts:job-admission-1",
            "admission_ref": "facts:job-admission-1",
            "mode": "retry",
        }))
    with pytest.raises(ValueError, match="effect-v2"):
        authorization.validate_for_intent(replace(_intent(), contract_version="legacy-v1"))


def test_admission_detects_mutation_of_boundary_gate_fact() -> None:
    gate = _gate()
    authorization = _authorization(gate_fact=gate)
    gate.budget_after["remaining"] = 0

    with pytest.raises(ValueError, match="changed after Job admission"):
        authorization.validate_for_intent(_intent())
