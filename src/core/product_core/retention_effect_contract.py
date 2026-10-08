from __future__ import annotations

from dataclasses import replace
from typing import Mapping

from core.effect_log import (
    EFFECT_V2,
    NOT_APPLICABLE,
    V2_REVISION_KEYS,
    EffectClass,
    EffectIntent,
    EffectPurpose,
    EffectReceipt,
    GateDecision,
    GateDecisionFact,
)


def build_retention_effect(
    *,
    session_id: str,
    root_id: str,
    step_key: str,
    kind: str,
    intent_ref_prefix: str,
    gate_decision_id: str,
    payload: Mapping[str, object],
    policy_revision: str,
    boundary_revision: str,
    workflow_revision: str,
    intent_schema_version: str,
    receipt_kind: str,
    receipt_schema_version: str,
) -> tuple[EffectIntent, GateDecisionFact]:
    """Build one closed-authority retention Effect v2 contract.

    The Core operation id excludes ``intent_ref`` from identity.  A provisional
    internal ref therefore lets Core derive the id before the final immutable
    domain-fact ref is bound, without introducing a second operation identity.
    """

    revisions = {key: NOT_APPLICABLE for key in V2_REVISION_KEYS}
    revisions.update(
        policy=policy_revision,
        boundary=boundary_revision,
        handler="retention-effect-handler-v2",
        workflow=workflow_revision,
    )
    draft = EffectIntent(
        session_id=session_id,
        root_id=root_id,
        step_key=step_key,
        kind=kind,
        effect_class=EffectClass.QUERYABLE,
        purpose=EffectPurpose.PRIMARY,
        intent_ref=f"{intent_ref_prefix}/pending",
        gate_decision_id=gate_decision_id,
        rev_set=revisions,
        payload=payload,
        contract_version=EFFECT_V2,
        intent_schema_version=intent_schema_version,
        expected_receipt_kind=receipt_kind,
        expected_receipt_schema_version=receipt_schema_version,
    )
    intent = replace(draft, intent_ref=f"{intent_ref_prefix}/{draft.operation_id}")
    if intent.operation_id != draft.operation_id:
        raise RuntimeError("retention Effect v2 identity drifted while binding intent ref")
    gate = GateDecisionFact(
        decision=GateDecision.ALLOW,
        rule_ref="rule:retention-explicit-confirmation-v2",
        scope_ref=f"scope:retention/{root_id}",
        budget_after={"remaining_count": 0},
        secret_scope="scope:secret/none",
        policy_revision=policy_revision,
    )
    return intent, gate


def retention_effect_receipt(
    operation_id: str,
    *,
    receipt_ref_prefix: str,
    receipt_kind: str,
    receipt_schema_version: str,
    intent_schema_version: str,
) -> EffectReceipt:
    return EffectReceipt(
        receipt_ref=f"{receipt_ref_prefix}/{operation_id}",
        receipt_kind=receipt_kind,
        receipt_schema_version=receipt_schema_version,
        intent_schema_version=intent_schema_version,
    )
