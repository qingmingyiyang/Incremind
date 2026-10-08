"""Proposal-only v2 admission evidence for ``extract_memory_candidate`` Jobs.

This command-boundary builder deliberately has no Store or projection
dependency.  It validates an already constructed Job payload, evaluates the
candidate-only policy from its durable evidence, and returns the Gate and
Effect Intent which a caller may later persist atomically.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

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

from .candidate_effect_contract import (
    EFFECT_KIND as _INTENT_KIND,
    INTENT_SCHEMA as _INTENT_SCHEMA,
    RECEIPT_KIND as _RECEIPT_KIND,
    RECEIPT_SCHEMA as _RECEIPT_SCHEMA,
    candidate_evidence_revision,
)


_POLICY_CONTRACT = "candidate-proposal-only-policy-v1"
_CAPABILITY_CONTRACT = "extract-memory-candidate-v2"
_BUNDLE_CONTRACT = "candidate-memory-bundle-v1"
_HANDLER_CONTRACT = "candidate-memory-handler-v1"
_BUDGET_CONTRACT = "candidate-single-proposal-budget-v1"
_WORKFLOW_CONTRACT = "candidate-memory-workflow-v1"
_EXECUTION_VERSION = EFFECT_V2
_JOB_FIELDS = frozenset({
    "schema_version", "id", "job_type", "parent_job_id", "source_id", "project_id",
    "evidence_kind", "evidence_id", "idempotency_key", "status", "attempt",
    "max_attempts", "lease", "progress", "steps", "error", "checkpoint",
    "staged_outputs", "published_outputs", "events", "input_refs", "candidate_policy",
    "created_at", "updated_at", "execution_version",
})


@dataclass(frozen=True, slots=True)
class CandidateJobAdmission:
    """Caller-owned authorization and intent for one candidate Job attempt."""

    authorization: JobAdmissionAuthorization
    intent: EffectIntent


class CandidateJobAdmissionFactory:
    """Evaluate the immutable candidate policy before any Job projection exists."""

    def __init__(self, *, admitted_at: int) -> None:
        if not isinstance(admitted_at, int) or isinstance(admitted_at, bool) or admitted_at < 0:
            raise ValueError("admitted_at must be a non-negative Unix timestamp")
        self._admitted_at = admitted_at

    def build(
        self,
        *,
        job_payload: Mapping[str, object],
        source_record: Mapping[str, object],
        source_revision: int,
        source_content_read_record: Mapping[str, object],
        source_content_read_revision: int,
    ) -> CandidateJobAdmission:
        if not isinstance(job_payload, Mapping):
            raise TypeError("job_payload must be a mapping")
        if set(job_payload) != _JOB_FIELDS:
            raise ValueError("candidate Job payload fields are not exact")
        if job_payload.get("execution_version") != _EXECUTION_VERSION:
            raise ValueError("candidate admission requires execution_version=effect-v2")
        job_id = _required(job_payload, "id")
        if job_payload.get("job_type") != "extract_memory_candidate":
            raise ValueError("candidate admission requires job_type=extract_memory_candidate")
        if job_payload.get("attempt") != 0:
            raise ValueError("candidate admission requires attempt=0")

        parent_job_id = _required(job_payload, "parent_job_id")
        source_id = _required(job_payload, "source_id")
        project_id = _required(job_payload, "project_id")
        evidence_id = _required(job_payload, "evidence_id")
        if job_payload.get("evidence_kind") != "source_content_read":
            raise ValueError("candidate admission requires source_content_read evidence")
        text_sha256 = _validate_source_evidence(
            source_record=source_record,
            source_id=source_id,
            source_revision=source_revision,
            source_content_read_record=source_content_read_record,
            source_content_read_id=evidence_id,
            source_content_read_revision=source_content_read_revision,
            project_id=project_id,
        )
        context_revision = candidate_evidence_revision(
            "candidate-context",
            source_id=source_id,
            content_read_id=evidence_id,
            text_sha256=text_sha256,
        )
        policy = _exact(job_payload.get("candidate_policy"), {
            "requires_user_confirmation", "auto_promote_allowed",
        }, "candidate policy")
        if policy != {"requires_user_confirmation": True, "auto_promote_allowed": False}:
            raise ValueError("candidate admission requires proposal-only policy")

        admission_ref = f"facts:candidate-job-admission/{job_id}/{evidence_id}"
        gate_id = f"gate:candidate-job-admission/{job_id}/{evidence_id}"
        intent_ref = f"intent:candidate-job-execution/{job_id}/{evidence_id}"
        revisions = {key: NOT_APPLICABLE for key in V2_REVISION_KEYS}
        revisions.update({
            "policy": _POLICY_CONTRACT,
            "boundary": "candidate-job-admission-boundary-v1",
            "capability": _CAPABILITY_CONTRACT,
            "context_manifest": context_revision,
            "bundle": _BUNDLE_CONTRACT,
            "handler": _HANDLER_CONTRACT,
            "budget": candidate_evidence_revision(
                _BUDGET_CONTRACT,
                source_id=source_id,
                content_read_id=evidence_id,
                text_sha256=text_sha256,
            ),
            "workflow": _WORKFLOW_CONTRACT,
        })
        gate = GateDecisionFact(
            decision=GateDecision.ALLOW,
            rule_ref="rule:candidate-job-proposal-only-v1",
            scope_ref=f"scope:candidate-job/{project_id}",
            budget_after={
                "project_ref": f"crp://candidate-job/projects/{project_id}",
                "source_ref": f"crp://candidate-job/sources/{source_id}",
                "source_identity_ref": f"facts:candidate-job/sources/{source_id}",
                "parent_job_ref": f"facts:candidate-job/parents/{parent_job_id}",
                "source_content_read_ref": f"crp://candidate-job/source-content-reads/{evidence_id}",
                "source_content_read_content_ref": (
                    f"facts:candidate-job/source-content-reads/{evidence_id}/sha256/{text_sha256}"
                ),
                "requires_user_confirmation_budget": 1,
                "auto_promote_allowed_budget": 0,
                "proposal_count": 1,
            },
            secret_scope="scope:candidate-job-secret/not-applicable",
            policy_revision=_POLICY_CONTRACT,
        )
        authorization = JobAdmissionAuthorization(
            job_id=job_id,
            admission_ref=admission_ref,
            command_kind=JobAdmissionCommandKind.ADMIT,
            gate_decision_id=gate_id,
            gate_fact=gate,
            revision_set=MappingProxyType(revisions),
            intent_refs={_INTENT_KIND: intent_ref},
            admitted_at=self._admitted_at,
        )
        intent = EffectIntent(
            session_id=f"candidate-memory:{project_id}",
            root_id=job_id,
            parent_id=None,
            step_key="execution",
            kind=_INTENT_KIND,
            effect_class=EffectClass.QUERYABLE,
            intent_ref=intent_ref,
            gate_decision_id=gate_id,
            rev_set=revisions,
            payload={
                "job_ref": admission_ref,
                "admission_ref": admission_ref,
                "parent_job_ref": f"facts:candidate-job/parents/{parent_job_id}",
                "source_ref": f"crp://candidate-job/sources/{source_id}",
                "evidence_ref": f"crp://candidate-job/source-content-reads/{evidence_id}",
                "mode": "admit",
                "attempt_index": 0,
            },
            contract_version=EFFECT_V2,
            intent_schema_version=_INTENT_SCHEMA,
            expected_receipt_kind=_RECEIPT_KIND,
            expected_receipt_schema_version=_RECEIPT_SCHEMA,
        )
        authorization.validate_for_intent(intent)
        return CandidateJobAdmission(authorization=authorization, intent=intent)


def _exact(value: object, keys: set[str], label: str) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise ValueError(f"{label} fields are not exact")
    return dict(value)


def _required(value: Mapping[str, object], key: str) -> str:
    item = value.get(key)
    if (not isinstance(item, str) or not item or item != item.strip()
            or any(character.isspace() or ord(character) < 32 for character in item)):
        raise ValueError(f"{key} must be a non-empty opaque token")
    return item


def _validate_source_evidence(
    *,
    source_record: Mapping[str, object],
    source_id: str,
    source_revision: int,
    source_content_read_record: Mapping[str, object],
    source_content_read_id: str,
    source_content_read_revision: int,
    project_id: str,
) -> str:
    if not isinstance(source_record, Mapping):
        raise TypeError("source_record must be a mapping")
    if not isinstance(source_content_read_record, Mapping):
        raise TypeError("source_content_read_record must be a mapping")
    if _required(source_record, "id") != source_id:
        raise ValueError("source record does not match source_id")
    source_project_id = source_record.get("project_id")
    if source_project_id is not None and source_project_id != project_id:
        raise ValueError("source record does not match project_id")
    if _required(source_content_read_record, "id") != source_content_read_id:
        raise ValueError("source content read record does not match evidence_id")
    if _required(source_content_read_record, "source_id") != source_id:
        raise ValueError("source content read record does not match source_id")
    if source_content_read_record.get("status") != "completed":
        raise ValueError("source content read record must be completed")
    _positive_revision(source_revision, "source_revision")
    _positive_revision(source_content_read_revision, "source_content_read_revision")
    text_sha256 = _required(source_content_read_record, "text_sha256")
    candidate_evidence_revision(
        "candidate-context",
        source_id=source_id,
        content_read_id=source_content_read_id,
        text_sha256=text_sha256,
    )
    return text_sha256


def _positive_revision(value: object, label: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"{label} must be a positive integer")
