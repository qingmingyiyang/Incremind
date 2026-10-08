"""Immutable, local authorization facts for the Hook data plane.

This module is deliberately an authority for *already decided* facts only.  It
does not call Boundary, inspect a request payload, resolve a Secret, or make a
new policy decision.  The Hook hot path can therefore make one constant-time,
fail-closed identity comparison after a hook passes, while the control plane
continues to publish new immutable revisions for later invocations.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import re
from threading import RLock


FROZEN_AUTHORIZATION_FACTS_SCHEMA_VERSION = "1.1.0"
FROZEN_AUTHORIZATION_FACTS_KIND = "frozen-authorization-facts"
FROZEN_APPROVAL_FACT_SCHEMA_VERSION = "1.0.0"
FROZEN_APPROVAL_FACT_KIND = "frozen-approval-fact"

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_FACTS_REF_RE = re.compile(r"^crp://session/[A-Za-z0-9._~:/-]+$")
_CONTRACT_REF_RE = re.compile(r"^crp://[A-Za-z0-9._~:/-]+$")
_EFFECTS = frozenset({"read", "write", "external", "platform", "delete"})
_DESTINATIONS = frozenset({"local", "provider", "mcp", "platform"})
_SEMANTICS = frozenset({"none", "read_only", "receipt_required"})


class FrozenAuthorizationFactsError(ValueError):
    """Raised when frozen authorization evidence is missing or malformed."""


@dataclass(frozen=True, slots=True)
class FrozenApprovalFact:
    """Immutable proof that one concrete tool invocation was approved.

    Presence of this exact fact, not a caller-provided boolean, is the only
    affirmative approval representation accepted by the local fact checker.
    """

    approval_id: str
    revision: str
    turn_id: str
    tool_call_id: str
    capability_id: str
    facts_ref: str
    facts_revision: str
    tool_contract_binding: str
    action_ref: str
    target_event_id: str
    decision: str = "approved"

    def __post_init__(self) -> None:
        for label, value in (
            ("approval_id", self.approval_id),
            ("revision", self.revision),
            ("turn_id", self.turn_id),
            ("tool_call_id", self.tool_call_id),
            ("capability_id", self.capability_id),
            ("facts_revision", self.facts_revision),
            ("tool_contract_binding", self.tool_contract_binding),
            ("target_event_id", self.target_event_id),
        ):
            _id(value, label)
        for label, value in (
            ("facts_ref", self.facts_ref),
            ("action_ref", self.action_ref),
        ):
            _ref(value, label, _FACTS_REF_RE)
        if self.decision != "approved":
            raise FrozenAuthorizationFactsError("approval fact decision must be approved")


@dataclass(frozen=True, slots=True)
class FrozenCapabilityAuthorization:
    """One exact capability contract admitted by a frozen policy revision."""

    capability_id: str
    contract_ref: str
    contract_revision: str
    requires_approval: bool
    destination: str
    effect: str
    operation_semantics: str

    def __post_init__(self) -> None:
        _id(self.capability_id, "capability_id")
        _ref(self.contract_ref, "contract_ref", _CONTRACT_REF_RE)
        _id(self.contract_revision, "contract_revision")
        if not isinstance(self.requires_approval, bool):
            raise FrozenAuthorizationFactsError("requires_approval must be boolean")
        if self.destination not in _DESTINATIONS:
            raise FrozenAuthorizationFactsError("destination is unsupported")
        if self.effect not in _EFFECTS:
            raise FrozenAuthorizationFactsError("effect is unsupported")
        if self.operation_semantics not in _SEMANTICS:
            raise FrozenAuthorizationFactsError("operation_semantics is unsupported")
        _validate_semantics(self)


@dataclass(frozen=True, slots=True)
class FrozenAuthorizationFacts:
    """Versioned immutable fact set bound to one Turn and Boundary revision."""

    facts_id: str
    revision: str
    turn_id: str
    project_id: str | None
    capability_manifest_ref: str
    context_manifest_ref: str
    capability_profile_id: str
    capability_profile_revision: int
    boundary_profile_id: str
    boundary_profile_revision: int
    capabilities: tuple[FrozenCapabilityAuthorization, ...]

    def __post_init__(self) -> None:
        _id(self.facts_id, "facts_id")
        _id(self.revision, "revision")
        _id(self.turn_id, "turn_id")
        if self.project_id is not None:
            _id(self.project_id, "project_id")
        _ref(self.capability_manifest_ref, "capability_manifest_ref", _FACTS_REF_RE)
        _ref(self.context_manifest_ref, "context_manifest_ref", _FACTS_REF_RE)
        _id(self.capability_profile_id, "capability_profile_id")
        _positive_revision(self.capability_profile_revision, "capability_profile_revision")
        _id(self.boundary_profile_id, "boundary_profile_id")
        _positive_revision(self.boundary_profile_revision, "boundary_profile_revision")
        if not self.capabilities:
            raise FrozenAuthorizationFactsError("at least one capability fact is required")
        ids = tuple(item.capability_id for item in self.capabilities)
        if len(ids) != len(set(ids)):
            raise FrozenAuthorizationFactsError("capability facts must be unique")

    def capability(self, capability_id: str) -> FrozenCapabilityAuthorization | None:
        """Return a declared fact only; unknown capabilities are never inferred."""

        return next((item for item in self.capabilities if item.capability_id == capability_id), None)


def frozen_authorization_facts_to_payload(facts: FrozenAuthorizationFacts) -> dict[str, object]:
    """Encode the schema-versioned, non-secret authority payload."""

    return {
        "schema_version": FROZEN_AUTHORIZATION_FACTS_SCHEMA_VERSION,
        "kind": FROZEN_AUTHORIZATION_FACTS_KIND,
        "facts_id": facts.facts_id,
        "revision": facts.revision,
        "turn_id": facts.turn_id,
        "project_id": facts.project_id,
        "capability_manifest_ref": facts.capability_manifest_ref,
        "context_manifest_ref": facts.context_manifest_ref,
        "capability_profile_id": facts.capability_profile_id,
        "capability_profile_revision": facts.capability_profile_revision,
        "boundary_profile_id": facts.boundary_profile_id,
        "boundary_profile_revision": facts.boundary_profile_revision,
        "capabilities": [
            {
                "capability_id": item.capability_id,
                "contract_ref": item.contract_ref,
                "contract_revision": item.contract_revision,
                "requires_approval": item.requires_approval,
                "destination": item.destination,
                "effect": item.effect,
                "operation_semantics": item.operation_semantics,
            }
            for item in facts.capabilities
        ],
    }


def frozen_authorization_facts_from_payload(value: object) -> FrozenAuthorizationFacts:
    """Strictly decode one versioned fact set. Unknown fields fail closed."""

    payload = _mapping(value, "frozen authorization facts")
    _exact_keys(
        payload,
        {
            "schema_version", "kind", "facts_id", "revision", "turn_id",
            "project_id", "capability_manifest_ref", "context_manifest_ref",
            "capability_profile_id", "capability_profile_revision",
            "boundary_profile_id", "boundary_profile_revision", "capabilities",
        },
        "frozen authorization facts",
    )
    if payload["schema_version"] != FROZEN_AUTHORIZATION_FACTS_SCHEMA_VERSION:
        raise FrozenAuthorizationFactsError("frozen authorization facts schema version is unsupported")
    if payload["kind"] != FROZEN_AUTHORIZATION_FACTS_KIND:
        raise FrozenAuthorizationFactsError("frozen authorization facts kind is unsupported")
    project_id = payload["project_id"]
    if project_id is not None and not isinstance(project_id, str):
        raise FrozenAuthorizationFactsError("project_id must be an identifier or null")
    capabilities = payload["capabilities"]
    if not isinstance(capabilities, list):
        raise FrozenAuthorizationFactsError("capabilities must be an array")
    parsed_capabilities: list[FrozenCapabilityAuthorization] = []
    for value in capabilities:
        item = _mapping(value, "frozen capability authorization")
        _exact_keys(
            item,
            {
                "capability_id", "contract_ref", "contract_revision", "requires_approval",
                "destination", "effect", "operation_semantics",
            },
            "frozen capability authorization",
        )
        parsed_capabilities.append(FrozenCapabilityAuthorization(
            capability_id=_string(item["capability_id"], "capability_id"),
            contract_ref=_string(item["contract_ref"], "contract_ref"),
            contract_revision=_string(item["contract_revision"], "contract_revision"),
            requires_approval=_boolean(item["requires_approval"], "requires_approval"),
            destination=_string(item["destination"], "destination"),
            effect=_string(item["effect"], "effect"),
            operation_semantics=_string(item["operation_semantics"], "operation_semantics"),
        ))
    return FrozenAuthorizationFacts(
        facts_id=_string(payload["facts_id"], "facts_id"),
        revision=_string(payload["revision"], "revision"),
        turn_id=_string(payload["turn_id"], "turn_id"),
        project_id=project_id,
        capability_manifest_ref=_string(
            payload["capability_manifest_ref"], "capability_manifest_ref"
        ),
        context_manifest_ref=_string(payload["context_manifest_ref"], "context_manifest_ref"),
        capability_profile_id=_string(payload["capability_profile_id"], "capability_profile_id"),
        capability_profile_revision=_positive_revision(
            payload["capability_profile_revision"], "capability_profile_revision"
        ),
        boundary_profile_id=_string(payload["boundary_profile_id"], "boundary_profile_id"),
        boundary_profile_revision=_positive_revision(
            payload["boundary_profile_revision"], "boundary_profile_revision"
        ),
        capabilities=tuple(parsed_capabilities),
    )


def frozen_approval_fact_to_payload(fact: FrozenApprovalFact) -> dict[str, object]:
    """Encode one non-secret, append-only approval fact."""

    return {
        "schema_version": FROZEN_APPROVAL_FACT_SCHEMA_VERSION,
        "kind": FROZEN_APPROVAL_FACT_KIND,
        "approval_id": fact.approval_id,
        "revision": fact.revision,
        "turn_id": fact.turn_id,
        "tool_call_id": fact.tool_call_id,
        "capability_id": fact.capability_id,
        "facts_ref": fact.facts_ref,
        "facts_revision": fact.facts_revision,
        "tool_contract_binding": fact.tool_contract_binding,
        "action_ref": fact.action_ref,
        "target_event_id": fact.target_event_id,
        "decision": "approved",
    }


def frozen_approval_fact_from_payload(value: object) -> FrozenApprovalFact:
    """Strictly decode a positive approval fact; unknown shapes fail closed."""

    payload = _mapping(value, "frozen approval fact")
    _exact_keys(
        payload,
        {
            "schema_version", "kind", "approval_id", "revision", "turn_id",
            "tool_call_id", "capability_id", "facts_ref", "facts_revision",
            "tool_contract_binding", "action_ref", "target_event_id", "decision",
        },
        "frozen approval fact",
    )
    if payload["schema_version"] != FROZEN_APPROVAL_FACT_SCHEMA_VERSION:
        raise FrozenAuthorizationFactsError("frozen approval fact schema version is unsupported")
    if payload["kind"] != FROZEN_APPROVAL_FACT_KIND:
        raise FrozenAuthorizationFactsError("frozen approval fact kind is unsupported")
    return FrozenApprovalFact(
        approval_id=_string(payload["approval_id"], "approval_id"),
        revision=_string(payload["revision"], "revision"),
        turn_id=_string(payload["turn_id"], "turn_id"),
        tool_call_id=_string(payload["tool_call_id"], "tool_call_id"),
        capability_id=_string(payload["capability_id"], "capability_id"),
        facts_ref=_string(payload["facts_ref"], "facts_ref"),
        facts_revision=_string(payload["facts_revision"], "facts_revision"),
        tool_contract_binding=_string(
            payload["tool_contract_binding"], "tool_contract_binding"
        ),
        action_ref=_string(payload["action_ref"], "action_ref"),
        target_event_id=_string(payload["target_event_id"], "target_event_id"),
        decision=_string(payload["decision"], "decision"),
    )


class FrozenAuthorizationFactsAuthority:
    """In-process immutable payload authority for frozen authorization facts.

    ``publish`` accepts the same ref/revision only when its canonical payload
    is identical.  Any replacement, missing ref, revision drift, or contract
    mismatch fails closed.  The authority does not generate a fact set and
    therefore cannot accidentally become a second policy evaluator.
    """

    def __init__(self) -> None:
        self._lock = RLock()
        self._facts: dict[tuple[str, str], FrozenAuthorizationFacts] = {}
        self._approvals: dict[tuple[str, str], FrozenApprovalFact] = {}
        self._capabilities: dict[
            tuple[str, str], Mapping[str, FrozenCapabilityAuthorization]
        ] = {}

    def publish(
        self, facts_ref: str, facts: FrozenAuthorizationFacts
    ) -> FrozenAuthorizationFacts:
        # Codec round-trip is an inexpensive strictness boundary before a
        # control-plane fact becomes available to the data plane.
        normalized = frozen_authorization_facts_from_payload(
            frozen_authorization_facts_to_payload(facts)
        )
        _ref(facts_ref, "facts_ref", _FACTS_REF_RE)
        key = (facts_ref, normalized.revision)
        with self._lock:
            existing = self._facts.get(key)
            if existing is not None and existing != normalized:
                raise FrozenAuthorizationFactsError("frozen facts ref/revision is immutable")
            self._facts[key] = normalized
            self._capabilities[key] = {
                item.capability_id: item for item in normalized.capabilities
            }
            return normalized

    def get(self, facts_ref: str, revision: str) -> FrozenAuthorizationFacts | None:
        try:
            _ref(facts_ref, "facts_ref", _FACTS_REF_RE)
            _id(revision, "revision")
        except FrozenAuthorizationFactsError:
            return None
        with self._lock:
            return self._facts.get((facts_ref, revision))

    def require(self, facts_ref: str, revision: str) -> FrozenAuthorizationFacts:
        facts = self.get(facts_ref, revision)
        if facts is None:
            raise FrozenAuthorizationFactsError("frozen authorization facts are unavailable")
        return facts

    def capability_for(
        self, facts_ref: str, revision: str, capability_id: str
    ) -> FrozenCapabilityAuthorization | None:
        """Return one already-published capability fact by constant-time lookup."""

        with self._lock:
            return self._capabilities.get((facts_ref, revision), {}).get(capability_id)

    def publish_approval(
        self, approval_ref: str, fact: FrozenApprovalFact
    ) -> FrozenApprovalFact:
        """Publish an immutable positive approval evidence record."""

        normalized = frozen_approval_fact_from_payload(frozen_approval_fact_to_payload(fact))
        # Approval facts cannot point at a non-existent or drifted frozen
        # authorization record, even while both records remain local.
        self.require(normalized.facts_ref, normalized.facts_revision)
        _ref(approval_ref, "approval_ref", _FACTS_REF_RE)
        key = (approval_ref, normalized.revision)
        with self._lock:
            existing = self._approvals.get(key)
            if existing is not None and existing != normalized:
                raise FrozenAuthorizationFactsError("approval ref/revision is immutable")
            self._approvals[key] = normalized
            return normalized

    def require_approval(self, approval_ref: str, revision: str) -> FrozenApprovalFact:
        try:
            _ref(approval_ref, "approval_ref", _FACTS_REF_RE)
            _id(revision, "approval revision")
        except FrozenAuthorizationFactsError:
            raise FrozenAuthorizationFactsError("frozen approval fact is unavailable") from None
        with self._lock:
            fact = self._approvals.get((approval_ref, revision))
        if fact is None:
            raise FrozenAuthorizationFactsError("frozen approval fact is unavailable")
        return fact

    def verify_approval(
        self,
        fact: FrozenApprovalFact | None,
        *,
        approval_ref: str,
        turn_id: str,
        tool_call_id: str,
        capability_id: str,
        facts_ref: str,
        facts_revision: str,
        tool_contract_binding: str,
        action_ref: str,
        target_event_id: str,
    ) -> FrozenApprovalFact | None:
        """Return the verified typed evidence, otherwise ``None``.

        Returning the fact rather than a boolean keeps approval provenance in
        the data-plane call site and prevents a caller from treating arbitrary
        truthy state as an authorization grant.
        """

        if fact is None:
            return None
        try:
            published = self.require_approval(approval_ref, fact.revision)
        except FrozenAuthorizationFactsError:
            return None
        expected = (
            turn_id, tool_call_id, capability_id, facts_ref, facts_revision,
            tool_contract_binding, action_ref, target_event_id,
        )
        actual = (
            published.turn_id, published.tool_call_id, published.capability_id,
            published.facts_ref, published.facts_revision,
            published.tool_contract_binding, published.action_ref,
            published.target_event_id,
        )
        return published if actual == expected and fact == published else None

    def allows(
        self,
        *,
        facts_ref: str,
        revision: str,
        capability_id: str,
        contract_ref: str,
        contract_revision: str,
        requires_approval: bool,
        destination: str,
        effect: str,
        operation_semantics: str,
        tool_call_id: str | None = None,
        tool_contract_binding: str | None = None,
        action_ref: str | None = None,
        target_event_id: str | None = None,
        approval_ref: str | None = None,
        approval_fact: FrozenApprovalFact | None = None,
    ) -> bool:
        """Constant-time-ish local identity check with deny-by-default behavior.

        This verifies only pre-issued facts.  It neither acquires approval nor
        relaxes any fact because a Hook passed.  A caller must explicitly carry
        the previously granted approval fact for a capability that requires it.
        """

        facts = self.get(facts_ref, revision)
        if facts is None:
            return False
        try:
            expected = FrozenCapabilityAuthorization(
                capability_id=capability_id,
                contract_ref=contract_ref,
                contract_revision=contract_revision,
                requires_approval=requires_approval,
                destination=destination,
                effect=effect,
                operation_semantics=operation_semantics,
            )
        except FrozenAuthorizationFactsError:
            return False
        with self._lock:
            actual = self._capabilities.get((facts_ref, revision), {}).get(capability_id)
        if actual != expected:
            return False
        if not actual.requires_approval:
            return True
        if not all(isinstance(value, str) for value in (
            tool_call_id, tool_contract_binding, action_ref, target_event_id,
            approval_ref,
        )):
            return False
        return self.verify_approval(
            approval_fact,
            approval_ref=approval_ref,
            turn_id=facts.turn_id,
            tool_call_id=tool_call_id,
            capability_id=capability_id,
            facts_ref=facts_ref,
            facts_revision=revision,
            tool_contract_binding=tool_contract_binding,
            action_ref=action_ref,
            target_event_id=target_event_id,
        ) is not None


def _validate_semantics(fact: FrozenCapabilityAuthorization) -> None:
    if fact.effect == "read":
        if fact.operation_semantics not in {"none", "read_only"}:
            raise FrozenAuthorizationFactsError("read effect cannot require a receipt")
        return
    if fact.operation_semantics != "receipt_required":
        raise FrozenAuthorizationFactsError("side-effecting capability requires receipt semantics")
    if fact.effect == "external" and fact.destination == "local":
        raise FrozenAuthorizationFactsError("external effect cannot target local destination")


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise FrozenAuthorizationFactsError(f"{label} must be an object")
    return value


def _exact_keys(value: Mapping[str, object], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise FrozenAuthorizationFactsError(f"{label} shape is invalid")


def _string(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise FrozenAuthorizationFactsError(f"{label} must be a string")
    return value


def _boolean(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise FrozenAuthorizationFactsError(f"{label} must be boolean")
    return value


def _positive_revision(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise FrozenAuthorizationFactsError(f"{label} must be a positive integer")
    return value


def _id(value: object, label: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise FrozenAuthorizationFactsError(f"{label} must be a safe identifier")
    return value


def _ref(value: object, label: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise FrozenAuthorizationFactsError(f"{label} must be a safe crp reference")
    return value
