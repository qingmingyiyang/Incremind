"""Media Hands command-boundary evidence for a v2 Job execution Effect.

The builder is intentionally independent of a Store or projection.  It turns
an already-admitted Media Hands Job plus its caller-owned selection command
into immutable admission evidence; persistence remains the responsibility of
``JobExecutionAdmissionAuthority``.
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

from .effect_contract import (
    EFFECT_KIND,
    INTENT_SCHEMA,
    RECEIPT_KIND,
    RECEIPT_SCHEMA,
    provider_revision_identity,
)


_BUNDLE_CONTRACT = "media-hands-bundle-v2"
_HANDLER_CONTRACT = "media-hands-handler-v2"
_WORKFLOW_CONTRACT = "media-hands-workflow-v2"
_OPERATIONS = {"analyze_source", "extract_audio_track", "transcribe_video", "extract_images"}
_BUDGET_FIELDS = {
    "max_download_bytes", "max_media_cpu_ms", "max_asr_audio_ms",
    "max_vision_frames", "max_model_input_tokens", "max_model_output_tokens",
    "max_wall_ms",
}


@dataclass(frozen=True, slots=True)
class MediaHandsAdmissionCommand:
    """Caller-owned selection evidence for the only supported Media Hands mode."""

    request_id: str
    project_id: str
    selection_ref: str
    selection_revision: str
    selection_mode: str

    def __post_init__(self) -> None:
        for field_name in ("request_id", "project_id", "selection_ref", "selection_revision"):
            _require_token(getattr(self, field_name), field_name)
        if self.selection_mode != "hands":
            raise ValueError("Media Hands admission requires selection_mode=hands")
        if not self.selection_ref.startswith("crp://"):
            raise ValueError("selection_ref must be a durable crp reference")
        object.__setattr__(self, "request_id", str(self.request_id))


@dataclass(frozen=True, slots=True)
class MediaHandsJobAdmission:
    authorization: JobAdmissionAuthorization
    intent: EffectIntent


class MediaHandsJobAdmissionFactory:
    """Build real ALLOW evidence after validating a frozen Media Hands Job.

    ``handler`` is deliberately a read-only provider identity source.  The
    factory does not call it, does not consult a Store, and cannot synthesize a
    Gate later from Job or projection state.
    """

    def __init__(self, *, admitted_at: int) -> None:
        if not isinstance(admitted_at, int) or isinstance(admitted_at, bool) or admitted_at < 0:
            raise ValueError("admitted_at must be a non-negative Unix timestamp")
        self._admitted_at = admitted_at

    def build(
        self,
        *,
        job_payload: Mapping[str, object],
        command: MediaHandsAdmissionCommand,
        handler: object,
    ) -> MediaHandsJobAdmission:
        if not isinstance(job_payload, Mapping):
            raise TypeError("job_payload must be a mapping")
        if not isinstance(command, MediaHandsAdmissionCommand):
            raise TypeError("command must be a MediaHandsAdmissionCommand")
        job_id = _required(job_payload, "id")
        if job_payload.get("job_type") != "media_hands":
            raise ValueError("Media Hands admission requires job_type=media_hands")
        if job_payload.get("execution_version") != EFFECT_V2:
            raise ValueError("Media Hands admission requires execution_version=effect-v2")
        if job_payload.get("attempt") != 0:
            raise ValueError("Media Hands admission requires attempt=0")
        media = _mapping(job_payload, "media_hands")
        manifest = _mapping(media, "manifest")
        permission = _mapping(media, "permission_snapshot")
        selection = _mapping(media, "selection")
        policy = _mapping(media, "policy")
        budget = _budget(media)
        operation = _required(media, "operation")
        if operation not in _OPERATIONS:
            raise ValueError("Media Hands admission operation is unsupported")
        policy_revision = _required(policy, "revision")

        if _required(permission, "project_id") != command.project_id:
            raise ValueError("permission project drifted from admission command")
        if (_required(permission, "manifest_ref"), _required(permission, "manifest_revision")) != (
            _required(manifest, "ref"), _required(manifest, "revision"),
        ):
            raise ValueError("permission snapshot drifted from Job manifest")
        if _exact(selection, {"ref", "revision", "mode"}) != {
            "ref": command.selection_ref,
            "revision": command.selection_revision,
            "mode": command.selection_mode,
        }:
            raise ValueError("selection drifted from Media Hands admission command")

        credential = _credential_binding(media.get("credential_use_binding"))
        provider_id, provider_revision = _provider_identity(handler)
        if credential is not None and credential["provider"] != provider_id:
            raise ValueError("credential provider drifted from Media Hands handler")
        boundary_profile_revision = credential["authorization_revision"] if credential else None
        boundary_revision = command.selection_revision
        if boundary_profile_revision is not None:
            boundary_revision = f"{boundary_revision}@{boundary_profile_revision}"
        revisions = {key: NOT_APPLICABLE for key in V2_REVISION_KEYS}
        revisions.update({
            "policy": policy_revision,
            "boundary": boundary_revision,
            "capability": f"media-hands-{operation}-v2",
            "context_manifest": _required(manifest, "revision"),
            "provider": provider_revision_identity(provider_id, provider_revision),
            "bundle": _BUNDLE_CONTRACT,
            "handler": _HANDLER_CONTRACT,
            "secret": credential["secret_generation"] if credential else NOT_APPLICABLE,
            "budget": f"{policy_revision}@{operation}",
            "workflow": _WORKFLOW_CONTRACT,
        })
        admission_ref = f"facts:media-hands-admission/{job_id}/{command.request_id}"
        gate_id = f"gate:media-hands-admission/{job_id}/{command.request_id}"
        intent_ref = f"intent:media-hands-job-execution/{job_id}/{command.request_id}"
        budget_ref = f"budget:media-hands/{policy_revision}/{operation}"
        evidence: dict[str, object] = {
            "selection_ref": command.selection_ref,
            "selection_revision": command.selection_revision,
            "manifest_ref": _required(manifest, "ref"),
            "manifest_revision": _required(manifest, "revision"),
            "permission_grant_ref": _required(permission, "grant_ref"),
            "permission_grant_revision": _required(permission, "grant_revision"),
            "provider_ref": f"provider:{provider_id}/{provider_revision}",
            "provider_revision": provider_revision,
            "budget_ref": budget_ref,
            **{f"{key}_budget": value for key, value in budget.items()},
        }
        if credential is not None:
            evidence["credential_ref"] = f"boundary:credential/{credential['provider']}/{boundary_profile_revision}"
            evidence["boundary_profile_revision"] = boundary_profile_revision
            evidence["secret_generation_revision"] = credential["secret_generation"]
        gate = GateDecisionFact(
            decision=GateDecision.ALLOW,
            rule_ref="rule:media-hands-admission-v2",
            scope_ref=f"scope:media-hands/{command.project_id}",
            budget_after=evidence,
            secret_scope=(f"scope:media-hands-secret/{credential['secret_generation']}"
                          if credential else "scope:media-hands-secret/not-applicable"),
            policy_revision=policy_revision,
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
            session_id=f"media-hands:{command.project_id}",
            root_id=job_id,
            parent_id=None,
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
        return MediaHandsJobAdmission(authorization=authorization, intent=intent)


def _provider_identity(handler: object) -> tuple[str, str]:
    identity = getattr(handler, "provider_identity", None)
    if isinstance(identity, tuple) and len(identity) == 2:
        provider_id, provider_revision = identity
    else:
        provider_id = getattr(handler, "provider_id", None)
        provider_revision = getattr(handler, "provider_revision", None)
    _require_token(provider_id, "provider_id")
    _require_token(provider_revision, "provider_revision")
    return provider_id, provider_revision


def _credential_binding(value: object) -> dict[str, str] | None:
    if value is None:
        return None
    binding = _exact(value, {"provider", "authorization_revision", "secret_generation"})
    for key, item in binding.items():
        _require_token(item, f"credential_use_binding.{key}")
    return binding


def _budget(media: Mapping[str, object]) -> dict[str, int]:
    try:
        budget = _exact(media.get("budget"), _BUDGET_FIELDS)
    except ValueError as exc:
        raise ValueError("Media Hands budget fields are not exact") from exc
    if any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in budget.values()):
        raise ValueError("Media Hands budget must contain exact non-negative limits")
    return budget  # type: ignore[return-value]


def _mapping(value: Mapping[str, object], key: str) -> Mapping[str, object]:
    item = value.get(key)
    if not isinstance(item, Mapping):
        raise ValueError(f"{key} is required")
    return item


def _exact(value: object, keys: set[str]) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise ValueError("Media Hands admission evidence fields are not exact")
    return dict(value)


def _required(value: Mapping[str, object], key: str) -> str:
    item = value.get(key)
    _require_token(item, key)
    return item  # type: ignore[return-value]


def _require_token(value: object, field_name: str) -> None:
    if (not isinstance(value, str) or not value or value != value.strip()
            or len(value) > 160 or any(char.isspace() or ord(char) < 32 for char in value)
            or any(character in value for character in ("?", "&", "="))):
        raise ValueError(f"{field_name} must be a non-empty opaque token")
