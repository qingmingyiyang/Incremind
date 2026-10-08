"""Turn-scoped frozen tool authorization without a Boundary hot-path call.

``issue`` is a control-plane operation: it evaluates the existing Boundary once
per capability with empty arguments and persists the resulting immutable facts.
All later candidate checks read only that immutable payload, compare local tool
contract identity, and call the Boundary's public local sanitizer.  They never
read profiles, databases, grants, or current Boundary state.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from threading import RLock

from backend.security.ai_tool_execution_boundary import (
    AIToolExecutionBoundary,
    AIToolExecutionBoundaryError,
)
from core.ai_kernel import (
    CapabilityDefinition,
    TurnPayloadStorePort,
    context_manifest_from_payload,
    manifest_from_payload,
)
from core.ai_kernel.tool_invocation import ToolInvocationIntent
from core.ai_kernel.frozen_authorization_facts import (
    FrozenApprovalFact,
    FrozenAuthorizationFacts,
    FrozenAuthorizationFactsAuthority,
    FrozenAuthorizationFactsError,
    FrozenCapabilityAuthorization,
    frozen_approval_fact_from_payload,
    frozen_approval_fact_to_payload,
    frozen_authorization_facts_from_payload,
    frozen_authorization_facts_to_payload,
)
from core.ai_tooling import (
    tool_contract_binding_identity,
    tool_from_capability,
)


FROZEN_TOOL_AUTHORIZATION_KIND = "frozen-tool-authorization-facts-v1"
FROZEN_TOOL_APPROVAL_KIND_PREFIX = "frozen-tool-approval-fact-v1"


class TurnFrozenAuthorizationError(ValueError):
    """A frozen authorization payload is absent, drifted, or not admissible."""


@dataclass(frozen=True, slots=True)
class FrozenAuthorizationHandle:
    facts_ref: str
    facts_revision: str
    facts: FrozenAuthorizationFacts


@dataclass(frozen=True, slots=True)
class FrozenApprovalHandle:
    approval_ref: str
    approval_revision: str
    fact: FrozenApprovalFact


@dataclass(frozen=True, slots=True)
class AuthorizedToolCandidate:
    """Local-only candidate result ready for an already fenced dispatcher."""

    facts_ref: str
    facts_revision: str
    tool_contract_binding: str
    sanitized_arguments: Mapping[str, object]
    approval_ref: str | None
    requires_approval: bool


class TurnFrozenAuthorizationAuthority:
    """Durable adapter around immutable Turn payloads and frozen local facts."""

    def __init__(
        self,
        *,
        payloads: TurnPayloadStorePort,
        execution_boundary: AIToolExecutionBoundary,
        facts_authority: FrozenAuthorizationFactsAuthority | None = None,
        agent_capability_authorizer: Callable[
            [Mapping[str, object], str], object
        ] | None = None,
    ) -> None:
        self._payloads = payloads
        self._execution_boundary = execution_boundary
        self._facts_authority = facts_authority or FrozenAuthorizationFactsAuthority()
        self._agent_capability_authorizer = agent_capability_authorizer
        self._lock = RLock()
        self._handles: dict[str, FrozenAuthorizationHandle] = {}

    def issue(
        self,
        request: Mapping[str, object],
        *,
        capability_manifest_ref: str,
        context_manifest_ref: str,
        capabilities: Sequence[CapabilityDefinition],
    ) -> FrozenAuthorizationHandle:
        """Issue immutable facts through the control-plane Boundary exactly once.

        Empty argument evaluation proves the selected capability is admitted by
        the current project Boundary.  Arguments are intentionally deferred to
        the data-plane's sanitizer so this method cannot persist user content.
        """

        turn_id = _turn_id(request)
        context = _load_context(self._payloads, context_manifest_ref, turn_id)
        manifest = _load_capability_manifest(
            self._payloads, capability_manifest_ref, turn_id
        )
        if context.capability_manifest_ref != capability_manifest_ref:
            raise TurnFrozenAuthorizationError("Context capability manifest ref drifted")
        if context.project_profile_id != manifest.profile_id or (
            context.project_profile_revision != manifest.profile_revision
        ):
            raise TurnFrozenAuthorizationError("capability profile binding drifted")
        if context.boundary_profile_id != manifest.boundary_profile_id or (
            context.boundary_profile_revision != manifest.boundary_profile_revision
        ):
            raise TurnFrozenAuthorizationError("Boundary profile binding drifted")
        if manifest.boundary_profile_id is None or manifest.boundary_profile_revision is None:
            raise TurnFrozenAuthorizationError("capability manifest lacks Boundary binding")

        by_id = {item.capability_id: item for item in capabilities}
        if len(by_id) != len(capabilities) or set(by_id) != set(manifest.capability_ids):
            raise TurnFrozenAuthorizationError("registered capabilities drifted from frozen manifest")

        facts: list[FrozenCapabilityAuthorization] = []
        capability_policy = request.get("capability_policy")
        approval_ids = (
            capability_policy.get("require_approval", ())
            if isinstance(capability_policy, Mapping) else ()
        )
        for capability_id in manifest.capability_ids:
            capability = by_id[capability_id]
            if _is_host_agent_capability(capability):
                authorizer = self._agent_capability_authorizer
                if authorizer is None:
                    raise TurnFrozenAuthorizationError(
                        "host Agent capability authority is unavailable"
                    )
                try:
                    authorizer(request, capability_id)
                except Exception as error:
                    raise TurnFrozenAuthorizationError(
                        "host Agent capability was not authorized"
                    ) from error
                facts.append(_capability_fact(
                    capability,
                    requires_approval=(
                        capability.requires_approval
                        or capability_id in approval_ids
                    ),
                ))
                continue
            # This is deliberately the sole execution-boundary evaluation in
            # this adapter.  Empty arguments avoid persisting data-plane input.
            decision = self._execution_boundary.evaluate(
                request, capability, {"arguments": {}}
            )
            if decision.outcome == "deny":
                raise TurnFrozenAuthorizationError(
                    f"Boundary did not admit capability {capability_id}"
                )
            if decision.outcome not in {"allow", "allow_redacted", "ask"}:
                raise TurnFrozenAuthorizationError("Boundary returned an unsupported outcome")
            facts.append(_capability_fact(
                capability,
                requires_approval=(decision.outcome == "ask" or capability.requires_approval),
            ))

        revision = _facts_revision(context.project_profile_revision, context.boundary_profile_revision)
        frozen = FrozenAuthorizationFacts(
            facts_id=f"frozen-tool-authorization-{revision}",
            revision=revision,
            turn_id=turn_id,
            project_id=context.project_id,
            capability_manifest_ref=capability_manifest_ref,
            context_manifest_ref=context_manifest_ref,
            capability_profile_id=context.project_profile_id,
            capability_profile_revision=context.project_profile_revision,
            boundary_profile_id=context.boundary_profile_id,
            boundary_profile_revision=context.boundary_profile_revision,
            capabilities=tuple(facts),
        )
        payload = frozen_authorization_facts_to_payload(frozen)
        try:
            facts_ref = self._payloads.get_or_create_immutable_payload(
                turn_id, FROZEN_TOOL_AUTHORIZATION_KIND, payload
            )
            self._facts_authority.publish(facts_ref, frozen)
        except (FrozenAuthorizationFactsError, ValueError) as error:
            raise TurnFrozenAuthorizationError("failed to persist frozen authorization facts") from error
        handle = FrozenAuthorizationHandle(facts_ref, revision, frozen)
        with self._lock:
            existing = self._handles.get(turn_id)
            if existing is not None and existing != handle:
                raise TurnFrozenAuthorizationError("frozen authorization handle drifted")
            self._handles[turn_id] = handle
        return handle

    def load(
        self,
        *,
        turn_id: str,
        facts_ref: str,
        facts_revision: str,
    ) -> FrozenAuthorizationHandle:
        """Hydrate exactly the persisted ref/revision after a process restart."""

        immutable = self._payloads.get_immutable_payload(
            turn_id, FROZEN_TOOL_AUTHORIZATION_KIND
        )
        if immutable is None or immutable[0] != facts_ref:
            raise TurnFrozenAuthorizationError("frozen authorization facts ref is unavailable")
        try:
            facts = frozen_authorization_facts_from_payload(immutable[1])
            if facts.turn_id != turn_id or facts.revision != facts_revision:
                raise TurnFrozenAuthorizationError("frozen authorization facts revision drifted")
            self._facts_authority.publish(facts_ref, facts)
        except FrozenAuthorizationFactsError as error:
            raise TurnFrozenAuthorizationError("frozen authorization facts are unreadable") from error
        handle = FrozenAuthorizationHandle(facts_ref, facts_revision, facts)
        with self._lock:
            existing = self._handles.get(turn_id)
            if existing is not None and existing != handle:
                raise TurnFrozenAuthorizationError("frozen authorization handle drifted")
            self._handles[turn_id] = handle
        return handle

    def load_current(self, *, turn_id: str) -> FrozenAuthorizationHandle:
        immutable = self._payloads.get_immutable_payload(
            turn_id, FROZEN_TOOL_AUTHORIZATION_KIND
        )
        if immutable is None:
            raise TurnFrozenAuthorizationError("frozen authorization facts are unavailable")
        try:
            facts = frozen_authorization_facts_from_payload(immutable[1])
        except FrozenAuthorizationFactsError as error:
            raise TurnFrozenAuthorizationError("frozen authorization facts are unreadable") from error
        return self.load(
            turn_id=turn_id,
            facts_ref=immutable[0],
            facts_revision=facts.revision,
        )

    def current_handle(self, *, turn_id: str) -> FrozenAuthorizationHandle:
        """Return only an already hydrated handle; never touch persistence."""

        with self._lock:
            handle = self._handles.get(turn_id)
        if handle is None:
            raise TurnFrozenAuthorizationError("frozen authorization handle is not hydrated")
        return handle

    def create_approval(
        self,
        *,
        turn_id: str,
        facts_ref: str,
        facts_revision: str,
        capability: CapabilityDefinition,
        tool_call_id: str,
        action_ref: str,
        target_event_id: str,
        approval_revision: str = "approval-v1",
    ) -> FrozenApprovalHandle:
        """Persist a positive approval fact bound to an already frozen Tool."""

        facts = self.load(
            turn_id=turn_id, facts_ref=facts_ref, facts_revision=facts_revision
        ).facts
        tool = tool_from_capability(capability)
        fact = FrozenApprovalFact(
            approval_id=f"tool-approval-{tool_call_id}",
            revision=approval_revision,
            turn_id=turn_id,
            tool_call_id=tool_call_id,
            capability_id=capability.capability_id,
            facts_ref=facts_ref,
            facts_revision=facts.revision,
            tool_contract_binding=tool_contract_binding_identity(tool),
            action_ref=action_ref,
            target_event_id=target_event_id,
        )
        kind = _approval_kind(tool_call_id)
        payload = frozen_approval_fact_to_payload(fact)
        try:
            approval_ref = self._payloads.get_or_create_immutable_payload(
                turn_id, kind, payload
            )
            self._facts_authority.publish_approval(approval_ref, fact)
        except (FrozenAuthorizationFactsError, ValueError) as error:
            raise TurnFrozenAuthorizationError("failed to persist frozen approval fact") from error
        return FrozenApprovalHandle(approval_ref, approval_revision, fact)

    def load_approval(
        self,
        *,
        turn_id: str,
        tool_call_id: str,
        approval_ref: str,
        approval_revision: str,
    ) -> FrozenApprovalHandle:
        immutable = self._payloads.get_immutable_payload(turn_id, _approval_kind(tool_call_id))
        if immutable is None or immutable[0] != approval_ref:
            raise TurnFrozenAuthorizationError("frozen approval fact ref is unavailable")
        try:
            fact = frozen_approval_fact_from_payload(immutable[1])
            if (
                fact.turn_id != turn_id
                or fact.tool_call_id != tool_call_id
                or fact.revision != approval_revision
            ):
                raise TurnFrozenAuthorizationError("frozen approval fact identity drifted")
            self._facts_authority.publish_approval(approval_ref, fact)
        except FrozenAuthorizationFactsError as error:
            raise TurnFrozenAuthorizationError("frozen approval fact is unreadable") from error
        return FrozenApprovalHandle(approval_ref, approval_revision, fact)

    def authorize_candidate(
        self,
        *,
        turn_id: str,
        facts_ref: str,
        facts_revision: str,
        capability: CapabilityDefinition,
        arguments: Mapping[str, object],
        tool_call_id: str,
        approval_ref: str | None = None,
        approval_revision: str | None = None,
        action_ref: str | None = None,
        target_event_id: str | None = None,
        allow_pending_approval: bool = False,
    ) -> AuthorizedToolCandidate | None:
        """Perform only local frozen-contract and sanitizer checks.

        No Profile, grant, payload-store, Boundary evaluation, model, or
        network lookup occurs here.  The caller must have hydrated facts and,
        when required, an approval fact before entering this hot path.
        """

        facts = self._facts_authority.get(facts_ref, facts_revision)
        if facts is None or facts.turn_id != turn_id:
            return None
        tool = tool_from_capability(capability)
        contract_ref, contract_revision = _contract_identity(capability)
        capability_fact = self._facts_authority.capability_for(
            facts_ref, facts_revision, capability.capability_id
        )
        if capability_fact is None:
            return None
        approval_fact: FrozenApprovalFact | None = None
        if capability_fact.requires_approval:
            if allow_pending_approval and approval_ref is None:
                approval_fact = None
            elif not all(isinstance(value, str) for value in (
                approval_ref, approval_revision, action_ref, target_event_id,
            )):
                return None
            else:
                try:
                    approval_fact = self._facts_authority.require_approval(
                        approval_ref, approval_revision
                    )
                except FrozenAuthorizationFactsError:
                    return None
        approved = self._facts_authority.allows(
            facts_ref=facts_ref,
            revision=facts_revision,
            capability_id=capability.capability_id,
            contract_ref=contract_ref,
            contract_revision=contract_revision,
            requires_approval=capability_fact.requires_approval,
            destination=tool.destination,
            effect=tool.effect,
            operation_semantics=tool.operation_semantics,
            tool_call_id=tool_call_id,
            tool_contract_binding=tool_contract_binding_identity(tool),
            action_ref=action_ref,
            target_event_id=target_event_id,
            approval_ref=approval_ref,
            approval_fact=approval_fact,
        )
        if not approved and not (
            allow_pending_approval
            and capability_fact.requires_approval
            and approval_ref is None
        ):
            return None
        try:
            if _is_host_agent_capability(capability):
                sanitized = self._execution_boundary.sanitize_host_candidate_arguments(
                    capability, arguments, turn_id=turn_id,
                )
            else:
                sanitized = self._execution_boundary.sanitize_candidate_arguments(
                    capability, arguments, turn_id=turn_id
                )
        except AIToolExecutionBoundaryError:
            return None
        if sanitized is None:
            return None
        return AuthorizedToolCandidate(
            facts_ref=facts_ref,
            facts_revision=facts_revision,
            tool_contract_binding=tool_contract_binding_identity(tool),
            sanitized_arguments=dict(sanitized),
            approval_ref=approval_ref,
            requires_approval=capability_fact.requires_approval,
        )

    def fence_intent(self, intent: ToolInvocationIntent) -> bool:
        """Revalidate one durable intent using only already hydrated facts."""

        facts_ref = intent.authorization_facts_ref
        facts_revision = intent.authorization_facts_revision
        if facts_ref is None or facts_revision is None:
            return False
        facts = self._facts_authority.get(facts_ref, facts_revision)
        capability_fact = self._facts_authority.capability_for(
            facts_ref, facts_revision, intent.capability_id
        )
        if facts is None or capability_fact is None or facts.turn_id != intent.turn_id:
            return False
        approval_ref = intent.approval_fact_ref
        approval_fact: FrozenApprovalFact | None = None
        if capability_fact.requires_approval:
            if approval_ref is None:
                return False
            try:
                approval_fact = self._facts_authority.require_approval(
                    approval_ref, "approval-v1"
                )
            except FrozenAuthorizationFactsError:
                return False
        contract = intent.tool_contract
        if not isinstance(contract, Mapping):
            return False
        binding = capability_fact.contract_revision
        if approval_fact is not None and approval_fact.tool_contract_binding != binding:
            return False
        return self._facts_authority.allows(
            facts_ref=facts_ref,
            revision=facts_revision,
            capability_id=intent.capability_id,
            contract_ref=capability_fact.contract_ref,
            contract_revision=binding,
            requires_approval=capability_fact.requires_approval,
            destination=capability_fact.destination,
            effect=capability_fact.effect,
            operation_semantics=capability_fact.operation_semantics,
            tool_call_id=intent.invocation_id,
            tool_contract_binding=binding,
            action_ref=approval_fact.action_ref if approval_fact else None,
            target_event_id=approval_fact.target_event_id if approval_fact else None,
            approval_ref=approval_ref,
            approval_fact=approval_fact,
        )

    def prepare_intent(self, intent: ToolInvocationIntent) -> bool:
        """Hydrate recovery-only approval evidence, then perform the local fence.

        Normal execution already has both facts in memory and takes no
        persistence read.  A restarted process loads the immutable approval
        once before entering the dispatcher; the dispatcher fence itself
        remains a pure in-process check.
        """

        if self.fence_intent(intent):
            return True
        if intent.approval_fact_ref is None:
            return False
        try:
            self.load_approval(
                turn_id=intent.turn_id,
                tool_call_id=intent.invocation_id,
                approval_ref=intent.approval_fact_ref,
                approval_revision="approval-v1",
            )
        except TurnFrozenAuthorizationError:
            return False
        return self.fence_intent(intent)


def _load_context(payloads: TurnPayloadStorePort, ref: str, turn_id: str):
    try:
        context = context_manifest_from_payload(payloads.get(ref))
    except Exception as error:
        raise TurnFrozenAuthorizationError("Context manifest is unreadable") from error
    if context.turn_id != turn_id:
        raise TurnFrozenAuthorizationError("Context manifest turn drifted")
    return context


def _load_capability_manifest(payloads: TurnPayloadStorePort, ref: str, turn_id: str):
    try:
        manifest = manifest_from_payload(payloads.get(ref))
    except Exception as error:
        raise TurnFrozenAuthorizationError("capability manifest is unreadable") from error
    if manifest.turn_id != turn_id:
        raise TurnFrozenAuthorizationError("capability manifest turn drifted")
    return manifest


def _capability_fact(
    capability: CapabilityDefinition,
    *,
    requires_approval: bool,
) -> FrozenCapabilityAuthorization:
    tool = tool_from_capability(capability)
    contract_ref, contract_revision = _contract_identity(capability)
    return FrozenCapabilityAuthorization(
        capability_id=capability.capability_id,
        contract_ref=contract_ref,
        contract_revision=contract_revision,
        requires_approval=requires_approval,
        destination=tool.destination,
        effect=tool.effect,
        operation_semantics=tool.operation_semantics,
    )


def _is_host_agent_capability(capability: CapabilityDefinition) -> bool:
    if not capability.capability_id.startswith("agent."):
        return False
    tool = tool_from_capability(capability)
    return (
        tool.owner_id == "agent-coordinator"
        and tool.destination == "platform"
        and tool.egress_class == "none"
        and not tool.network_scope
        and not tool.data_egress_scope
        and "agent_run_parent" in tool.boundary_requirements
        and "frozen_turn_scope" in tool.boundary_requirements
    )


def _contract_identity(capability: CapabilityDefinition) -> tuple[str, str]:
    tool = tool_from_capability(capability)
    # The reference names a stable local contract coordinate; its revision is
    # the complete immutable Tool contract binding, not display metadata.
    return (
        f"crp://tool-contracts/{capability.capability_id}",
        tool_contract_binding_identity(tool),
    )


def _facts_revision(capability_revision: int, boundary_revision: int) -> str:
    return f"facts-v1-c{capability_revision}-b{boundary_revision}"


def _approval_kind(tool_call_id: str) -> str:
    if not isinstance(tool_call_id, str) or not tool_call_id:
        raise TurnFrozenAuthorizationError("tool call identity is unavailable")
    return f"{FROZEN_TOOL_APPROVAL_KIND_PREFIX}:{tool_call_id}"


def _turn_id(request: Mapping[str, object]) -> str:
    value = request.get("turn_id")
    if not isinstance(value, str) or not value:
        raise TurnFrozenAuthorizationError("Turn identity is unavailable")
    return value
