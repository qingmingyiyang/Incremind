from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime

from backend.security.file_grant import DesktopFileGrant
from backend.security.project_boundary_profiles import (
    ProjectBoundaryProfileSnapshot,
    ProjectBoundaryProfileStore,
)
from core.ai_boundary import BoundaryDecision, BoundaryGrant, BoundaryPolicyEngine, BoundaryRequest, ProjectBoundaryProfile, SanitizationResult
from core.ai_boundary.contracts import utc_now
from core.ai_kernel.contracts import AIKernelContractError, validate_turn_request, validate_capability_manifest
from core.ai_kernel.ports import CapabilityDefinition
from core.ai_tooling import ToolDefinition, tool_boundary_target_identity, tool_contract_identity, tool_from_capability


ExternalExecutionAuthority = Callable[
    [Mapping[str, object], CapabilityDefinition, BoundaryRequest], BoundaryGrant | None,
]


class TurnBoundaryAdapterError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class TurnBoundaryEvaluation:
    request: BoundaryRequest
    profile: ProjectBoundaryProfileSnapshot
    decision: BoundaryDecision


class TurnBoundaryRequestFactory:
    """Server-side V1 compatibility projection; clients cannot widen its result."""

    def __init__(
        self,
        profiles: ProjectBoundaryProfileStore,
        *,
        engine: BoundaryPolicyEngine | None = None,
        division_capabilities: Callable[[Mapping[str, object]], Sequence[str]] | None = None,
        external_execution_authority: ExternalExecutionAuthority | None = None,
    ) -> None:
        self._profiles = profiles
        self._engine = engine or BoundaryPolicyEngine()
        self._division_capabilities = division_capabilities
        if external_execution_authority is not None and not callable(external_execution_authority):
            raise TypeError("external execution authority must be callable")
        self._external_execution_authority = external_execution_authority

    def evaluate(
        self,
        turn_request: Mapping[str, object],
        capability: CapabilityDefinition,
        *,
        destination_id: str,
        sanitization: SanitizationResult | None = None,
        verified_file_grant: DesktopFileGrant | None = None,
    ) -> TurnBoundaryEvaluation:
        turn = validate_turn_request(turn_request)
        policy = _mapping(turn["capability_policy"], "capability policy")
        capability_id = capability.capability_id
        if capability_id not in policy["allowed"] or capability_id in policy["denied"]:
            raise TurnBoundaryAdapterError("capability is outside the V1 turn policy")
        tool = tool_from_capability(capability)
        declared_draft = "draft_create_only" in tool.boundary_requirements
        if declared_draft:
            validate_capability_manifest({"mode": capability.mode,
                "operation_semantics": capability.operation_semantics,
                "requires_approval": capability.requires_approval,
                "tool_exposed": True, "write_scope": "draft_create_only"})
        division_draft = (
            declared_draft and tool.effect == "write" and tool.destination == "local"
            and turn["desired_outcome"] == "project.task"
            and self._division_capabilities is not None
            and capability_id in self._division_capabilities(turn)
        )
        project_id = _project_id(_mapping(turn["scope"], "turn scope"))
        profile = (
            _global_profile()
            if project_id == "global"
            else self._profiles.get(project_id)
        )
        request = self._request(
            turn,
            tool,
            capability_id=capability.capability_id,
            project_id=project_id,
            destination_id=destination_id,
            sanitization=sanitization,
            verified_file_grant=verified_file_grant,
            division_draft=division_draft,
        )
        decision = self._engine.evaluate(request, profile.profile)
        external_execution = (
            capability_id == "external.task.execute"
            or "external_execute" in tool.boundary_requirements
        )
        if external_execution:
            decision = self._external_execution_decision(
                turn, capability, tool, request, profile.profile, decision,
            )
        if request.destination_kind != "local" and not _remote_allowed(turn):
            decision = _narrow(decision, "deny", "turn_remote_not_allowed")
        approval_ids = policy["require_approval"]
        if decision.outcome in {"allow", "allow_redacted"} and (
            capability.requires_approval or capability_id in approval_ids
            or (declared_draft and not division_draft)
        ):
            decision = _narrow(decision, "ask", "legacy_turn_approval_required")
        return TurnBoundaryEvaluation(request=request, profile=profile, decision=decision)

    def _external_execution_decision(
        self,
        turn: Mapping[str, object],
        capability: CapabilityDefinition,
        tool: ToolDefinition,
        request: BoundaryRequest,
        profile: ProjectBoundaryProfile,
        decision: BoundaryDecision,
    ) -> BoundaryDecision:
        """只向原引擎投影当次证明；硬拒绝及原持久授权保持原样。"""
        if decision.outcome == "deny":
            return decision
        if profile.remote_default == "deny":
            return _narrow(decision, "deny", "profile_remote_denied")
        if not _remote_allowed(turn):
            return _narrow(decision, "deny", "turn_remote_not_allowed")
        try:
            if capability.tool_definition is None:
                raise AIKernelContractError("external execution requires a native tool")
            validate_capability_manifest({
                "capability_id": capability.capability_id, "version": capability.version,
                "mode": capability.mode, "operation_semantics": capability.operation_semantics,
                "requires_approval": capability.requires_approval, "tool_exposed": True,
                "write_scope": "external_execute", "tool_contract": tool_contract_identity(tool),
            })
        except AIKernelContractError:
            return _narrow(decision, "deny", "external_execution_contract_invalid")
        authority = self._external_execution_authority
        if (authority is None or turn["desired_outcome"] != "project.task"
                or request.project_id == "global" or request.scan_state not in {"clean", "redacted"}):
            return _narrow(decision, "deny", "external_execution_not_authorized")
        try:
            grant = authority(turn, capability, request)
            if not _external_grant_matches(grant, request, profile):
                return _narrow(decision, "deny", "external_execution_not_authorized")
        except Exception:
            return _narrow(decision, "deny", "external_execution_not_authorized")
        # 只替换本次引擎输入，不保存 grant，也不改返回的真实 profile 快照。
        projection = replace(profile, persistent_grants=(grant,))
        return self._engine.evaluate(request, projection)

    @staticmethod
    def _request(
        turn: Mapping[str, object],
        tool: ToolDefinition,
        *,
        capability_id: str,
        project_id: str,
        destination_id: str,
        sanitization: SanitizationResult | None,
        verified_file_grant: DesktopFileGrant | None,
        division_draft: bool = False,
    ) -> BoundaryRequest:
        destination = tool.destination
        if destination != "local" and not str(destination_id).strip():
            raise TurnBoundaryAdapterError("remote tool requires server-resolved destination identity")
        data_classes = set(tool.data_classes)
        scan_state = "not_required" if destination == "local" else "unknown"
        if sanitization is not None:
            scan_state = {
                "blocked": "sensitive",
                "redacted": "redacted",
                "clean": "clean",
            }.get(sanitization.outcome, "unknown")
            data_classes.update(data_class for data_class, _count in sanitization.summary.counts)
        if verified_file_grant is not None:
            if tool.effect != "read" or destination != "local":
                raise TurnBoundaryAdapterError("file grant only proves a bounded local read")
            data_classes.add(f"asset_{verified_file_grant.source_kind}")
        return BoundaryRequest(
            request_id=f"boundary-{turn['turn_id']}-{tool.tool_id}",
            turn_id=str(turn["turn_id"]),
            project_id=project_id,
            actor_id="ai-kernel",
            target_id=tool_boundary_target_identity(tool, capability_id),
            operation_id=str(turn["operation_id"]),
            idempotency_key=str(turn["idempotency_key"]),
            effect=tool.effect,
            destination_kind=destination,
            destination_id=str(destination_id).strip() or "local-runtime",
            data_classes=tuple(sorted(data_classes)),
            scan_state=scan_state,  # type: ignore[arg-type]
            reversible=tool.effect == "read" or division_draft,
            same_project=True,
            requires_receipt=tool.operation_semantics == "receipt_required" or destination != "local",
        )


def _narrow(decision: BoundaryDecision, outcome: str, reason: str) -> BoundaryDecision:
    return BoundaryDecision(
        request_id=decision.request_id,
        outcome=outcome,  # type: ignore[arg-type]
        reason_codes=(reason,),
        matched_grant_ids=decision.matched_grant_ids,
        policy_revision=decision.policy_revision,
        requires_receipt=decision.requires_receipt,
        redaction_required=decision.redaction_required if outcome == "ask" else False,
    )


def _external_grant_matches(
    grant: object, request: BoundaryRequest, profile: ProjectBoundaryProfile,
) -> bool:
    """授予对象须逐项绑定本轮范围、原修订、有效期及脱敏结果。"""
    return (
        isinstance(grant, BoundaryGrant)
        and grant.grant_id == "external-execution-" + request.turn_id
        and grant.subject_id == request.actor_id and grant.project_id == request.project_id
        and grant.target_id == request.target_id and grant.actions == (request.effect,)
        and grant.destinations == (request.destination_kind,)
        and grant.data_classes == request.data_classes
        and type(grant.revision) is int and grant.revision == profile.revision
        and grant.revoked is False
        and isinstance(grant.expires_at, datetime) and grant.expires_at.tzinfo is not None
        and grant.is_active_at(utc_now())
        and grant.redaction_required is (request.scan_state == "redacted")
    )


def _project_id(scope: Mapping[str, object]) -> str:
    project_id = scope.get("project_id")
    return str(project_id) if isinstance(project_id, str) and project_id.strip() else "global"


def _remote_allowed(turn: Mapping[str, object]) -> bool:
    privacy = _mapping(turn["privacy"], "turn privacy")
    return privacy.get("allow_remote") is True and privacy.get("mode") == "remote_allowed"


def _global_profile() -> ProjectBoundaryProfileSnapshot:
    return ProjectBoundaryProfileSnapshot(
        profile=ProjectBoundaryProfile(
            profile_id="global-boundary-compatibility",
            project_id="global",
            mode="guarded",
            revision=1,
            remote_default="review",
        ),
        store_revision=0,
        persisted=False,
    )


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TurnBoundaryAdapterError(f"{label} must be an object")
    return value
