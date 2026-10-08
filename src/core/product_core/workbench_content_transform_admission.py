"""Gate and atomic Job admission facts for local Workbench transforms."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
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
from core.job_runner import JobAdmissionAuthorization, JobAdmissionCommandKind

from .workbench_content_transform_effect_contract import (
    EFFECT_KIND,
    INTENT_SCHEMA,
    RECEIPT_KIND,
    RECEIPT_SCHEMA,
)


PIPELINES = frozenset({
    "document_extract", "local_video", "web_read", "image_ocr", "audio_transcript",
})
ASR_PROVIDERS = frozenset({"not-applicable", "local-faster-whisper", "tokenhub-asr"})
_POLICY = "workbench-content-transform-governed-asr-v2"


@dataclass(frozen=True, slots=True)
class WorkbenchContentTransformAdmission:
    authorization: JobAdmissionAuthorization
    intent: EffectIntent


class WorkbenchContentTransformAdmissionFactory:
    def __init__(self, *, admitted_at: int) -> None:
        if not isinstance(admitted_at, int) or isinstance(admitted_at, bool) or admitted_at < 0:
            raise ValueError("admitted_at must be a non-negative Unix timestamp")
        self._admitted_at = admitted_at

    def build(self, *, job_payload: Mapping[str, object]) -> WorkbenchContentTransformAdmission:
        job_id = _required(job_payload, "id")
        if job_payload.get("job_type") != "workbench_content_transform":
            raise ValueError("workbench transform admission requires its Job type")
        if job_payload.get("execution_version") != EFFECT_V2 or job_payload.get("attempt") != 0:
            raise ValueError("workbench transform admission requires Effect-v2 attempt zero")
        items = exact_transform_items(job_payload.get("transform_items"))
        if not items:
            raise ValueError("workbench transform admission requires transform items")
        project_id = _required(job_payload, "project_id")
        source_id = _required(job_payload, "source_id")
        admission_ref = f"facts:workbench-content-transform/{job_id}/attempt-0"
        gate_id = f"gate:workbench-content-transform/{job_id}/attempt-0"
        intent_ref = f"intent:workbench-content-transform/{job_id}/attempt-0"
        context_revision = "workbench-transform:" + ":".join(
            f"{item['source_id']}@{item['source_revision']}@{item['authorization_revision']}"
            for item in items
        )
        revisions = {key: NOT_APPLICABLE for key in V2_REVISION_KEYS}
        revisions.update({
            "policy": _POLICY,
            "boundary": "workbench-managed-original-v1",
            "capability": "document-video-transform-with-bound-asr-v2",
            "context_manifest": context_revision,
            "provider": "workbench-bound-asr-providers-v2",
            "bundle": "workbench-content-transform-bundle-v1",
            "handler": "workbench-content-transform-handler-v1",
            "budget": "workbench-content-transform-one-batch-v1",
            "workflow": "workbench-content-transform-workflow-v1",
        })
        gate = GateDecisionFact(
            decision=GateDecision.ALLOW,
            rule_ref="rule:workbench-managed-original-transform-v1",
            scope_ref=f"scope:workbench-content-transform/{project_id}",
            budget_after={
                "job_ref": admission_ref,
                "source_ref": f"crp://workbench/sources/{source_id}",
                "item_count": len(items),
                "pipeline_count": len({item["pipeline"] for item in items}),
                "document_pipeline_count": sum(item["pipeline"] == "document_extract" for item in items),
                "video_pipeline_count": sum(item["pipeline"] == "local_video" for item in items),
                "remote_processing_count": sum(
                    item["asr_provider"] == "tokenhub-asr" for item in items
                ),
                "memory_auto_publish_count": 0,
            },
            secret_scope=(
                "scope:workbench-content-transform-secret/tokenhub-asr"
                if any(item["asr_provider"] == "tokenhub-asr" for item in items)
                else "scope:workbench-content-transform-secret/not-applicable"
            ),
            policy_revision=_POLICY,
        )
        authorization = JobAdmissionAuthorization(
            job_id=job_id,
            admission_ref=admission_ref,
            command_kind=JobAdmissionCommandKind.ADMIT,
            gate_decision_id=gate_id,
            gate_fact=gate,
            revision_set=MappingProxyType(revisions),
            intent_refs={EFFECT_KIND: intent_ref},
            admitted_at=self._admitted_at,
        )
        intent = EffectIntent(
            session_id=f"workbench-content-transform:{project_id}",
            root_id=job_id,
            step_key="execution",
            kind=EFFECT_KIND,
            effect_class=EffectClass.QUERYABLE,
            intent_ref=intent_ref,
            gate_decision_id=gate_id,
            rev_set=revisions,
            payload={
                "job_ref": admission_ref,
                "admission_ref": admission_ref,
                "mode": "admit",
                "attempt_index": 0,
            },
            contract_version=EFFECT_V2,
            intent_schema_version=INTENT_SCHEMA,
            expected_receipt_kind=RECEIPT_KIND,
            expected_receipt_schema_version=RECEIPT_SCHEMA,
        )
        authorization.validate_for_intent(intent)
        return WorkbenchContentTransformAdmission(authorization, intent)


def exact_transform_items(value: object) -> tuple[dict[str, object], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError("transform_items must be a sequence")
    result: list[dict[str, object]] = []
    fields = {
        "source_id", "pipeline", "source_revision", "authorization_id",
        "authorization_revision", "original_asset_ref", "source_title", "project_id",
        "asr_provider", "asr_binding_id",
    }
    for raw in value:
        if not isinstance(raw, Mapping) or set(raw) != fields:
            raise ValueError("transform item fields are not exact")
        item = dict(raw)
        for field in ("source_id", "authorization_id", "original_asset_ref", "source_title", "project_id"):
            _required(item, field)
        if item.get("pipeline") not in PIPELINES:
            raise ValueError("workbench transform pipeline is unsupported")
        if item.get("asr_provider") not in ASR_PROVIDERS:
            raise ValueError("workbench transform ASR provider is unsupported")
        if item["pipeline"] in {"document_extract", "web_read", "image_ocr"} and item["asr_provider"] != "not-applicable":
            raise ValueError("non-audio transform cannot bind an ASR provider")
        if item["pipeline"] in {"local_video", "audio_transcript"} and item["asr_provider"] == "not-applicable":
            raise ValueError("audio transform requires an ASR provider")
        if item["pipeline"] == "web_read":
            if item["authorization_id"] != "not-applicable" or item["authorization_revision"] != 0:
                raise ValueError("web transform cannot bind a local-file authorization")
        elif item["authorization_revision"] < 1:
            raise ValueError("local transform authorization_revision must be positive")
        for field in ("source_revision",):
            revision = item.get(field)
            if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
                raise ValueError(f"{field} must be a positive integer")
        result.append(item)
    return tuple(result)


def _required(value: Mapping[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item or item != item.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return item
