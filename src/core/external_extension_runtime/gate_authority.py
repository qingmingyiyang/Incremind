"""Core-owned Gate authority for governed external extension Effects.

The workflow and lifecycle command facade may request an authorization, but
they cannot manufacture an ALLOW fact.  This small authority is composed at
startup and derives every grant from an immutable source-confirmation fact.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from core.effect_log import GateDecision, GateDecisionFact

from .fact_store import ExternalExtensionFactStore


_COMMIT = re.compile(r"^[0-9a-f]{40}$")


class ExternalExtensionGateAuthorizationError(ValueError):
    """Raised when immutable confirmation evidence cannot authorize an Effect."""


@dataclass(frozen=True, slots=True)
class ExternalExtensionGateAuthorization:
    authorization_ref: str
    decision_id: str
    fact: GateDecisionFact


@dataclass(frozen=True, slots=True)
class ExternalExtensionGateRequest:
    phase: str
    project_id: str
    subject_ref: str
    authorization_ref: str
    policy_revision: str
    revision_ref: str | None = None
    expected_state_revision: int | None = None


class ExternalExtensionGateAuthority:
    """Evaluate external-extension Gate grants from immutable confirmations.

    This is intentionally an explicit composition service, not a fallback
    helper.  A missing, foreign, or drifted confirmation raises before any
    Effect can be planned.
    """

    def __init__(self, facts: ExternalExtensionFactStore) -> None:
        if not isinstance(facts, ExternalExtensionFactStore):
            raise TypeError("external extension Gate authority requires fact store")
        self._facts = facts

    def authorize(self, request: ExternalExtensionGateRequest) -> ExternalExtensionGateAuthorization:
        if request.phase not in {
            "source_resolve", "source_acquire", "lifecycle_health", "lifecycle_activation",
            "lifecycle_disable", "lifecycle_rollback", "lifecycle_uninstall",
        }:
            raise ExternalExtensionGateAuthorizationError("external extension Gate phase is invalid")
        confirmation = self._facts.load_gate_confirmation(request.authorization_ref)
        if confirmation["project_id"] != request.project_id:
            raise ExternalExtensionGateAuthorizationError("Gate confirmation project drifted")
        authorization_kind = confirmation.get("authorization_kind")
        if request.phase == "source_resolve":
            if authorization_kind != "source" or request.subject_ref != confirmation.get("intent_ref"):
                raise ExternalExtensionGateAuthorizationError(
                    "source resolution requires the exact source confirmation"
                )
        elif request.phase == "source_acquire":
            try:
                _resolution_intent_id, resolved_source = self._facts.load_resolution(
                    request.subject_ref,
                )
            except ValueError as error:
                raise ExternalExtensionGateAuthorizationError(
                    "source acquisition requires a valid immutable resolution"
                ) from error
            if authorization_kind == "source":
                intent = self._facts.load_intent(str(confirmation.get("intent_ref") or ""))
                requested_ref = (
                    intent.source_spec.requested_ref
                    if intent.source_spec is not None
                    else None
                )
                if not isinstance(requested_ref, str) or not _COMMIT.fullmatch(requested_ref):
                    raise ExternalExtensionGateAuthorizationError(
                        "floating source acquisition requires an exact revision confirmation"
                    )
                if requested_ref != resolved_source.immutable_revision:
                    raise ExternalExtensionGateAuthorizationError(
                        "fixed source revision does not match the immutable resolution"
                    )
            elif authorization_kind != "revision":
                raise ExternalExtensionGateAuthorizationError(
                    "source acquisition confirmation kind is invalid"
                )
        elif request.phase in {"lifecycle_disable", "lifecycle_rollback", "lifecycle_uninstall"}:
            if authorization_kind != "lifecycle":
                raise ExternalExtensionGateAuthorizationError(
                    "user lifecycle work requires an exact action confirmation"
                )
            expected = {
                "project_id": request.project_id,
                "action": request.phase.removeprefix("lifecycle_"),
                "revision_ref": request.revision_ref,
                "subject_ref": request.subject_ref,
                "expected_state_revision": request.expected_state_revision,
            }
            if any(confirmation.get(key) != value for key, value in expected.items()):
                raise ExternalExtensionGateAuthorizationError(
                    "lifecycle action confirmation drifted"
                )
        elif authorization_kind != "source":
            raise ExternalExtensionGateAuthorizationError(
                "lifecycle work requires the reviewed source confirmation"
            )
        if authorization_kind != "lifecycle" and not self._facts.confirmation_authorizes_subject(
            confirmation, subject_ref=request.subject_ref,
        ):
            raise ExternalExtensionGateAuthorizationError("Gate confirmation does not bind the requested action")
        material = "\0".join((
            request.phase, request.project_id, request.subject_ref,
            request.authorization_ref, request.policy_revision,
        )).encode("utf-8")
        digest = hashlib.sha256(material).hexdigest()
        return ExternalExtensionGateAuthorization(
            authorization_ref=request.authorization_ref,
            decision_id=f"gate:external-extension/{digest}",
            fact=GateDecisionFact(
                decision=GateDecision.ALLOW,
                rule_ref="rule:external-extension/core-gate-v1",
                scope_ref=f"scope:external-extension/{request.phase}/{digest}",
                budget_after={"network_bytes": 32 * 1024 * 1024 if request.phase.startswith("source_") else 0},
                secret_scope="scope:secret/none",
                policy_revision=request.policy_revision,
            ),
        )
