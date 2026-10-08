from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from backend.model_routing_snapshot import (
    project_turn_model_routing_snapshot,
    turn_model_routing_snapshot_revision,
    validate_turn_model_routing_snapshot,
)
from core.ai_kernel import TurnPayloadStorePort


class TurnModelRoutingSnapshotAuthorityError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class TurnModelRoutingSnapshot:
    payload_ref: str
    revision: str
    payload: Mapping[str, object]


class TurnModelRoutingSnapshotAuthority:
    """Freeze one trusted model requirement, catalog and decision per supported Turn."""

    snapshot_kind = "turn-model-routing-snapshot-v1"
    _OUTCOMES = {
        # A selected video-research expert still runs through the ordinary
        # project model-route authority.  The expert identity is frozen by its
        # separate binding snapshot; this entry only freezes the Planner's
        # structured, text-only routing requirement for that same Turn.
        "media_analysis": {
            "required_capability": "structured",
            "modality": "text",
            "output_contract": "json_object",
            "egress_purpose": "search_answer",
            "egress_categories": ("instructions", "source_excerpt"),
        },
        "workbench.question.answer": {
            "required_capability": "structured",
            "modality": "text",
            "output_contract": "json_object",
            "egress_purpose": "search_answer",
            "egress_categories": ("instructions", "source_excerpt"),
        },
        "agent.child.execute": {
            "required_capability": "structured",
            "modality": "text",
            "output_contract": "json_object",
            "egress_purpose": "search_answer",
            "egress_categories": ("instructions", "source_excerpt"),
        },
        # Organization Turns use the same structured Planner gateway as the
        # ordinary project-answer path.  Freezing these outcomes is still
        # required when no route is currently eligible: ``selected=None`` is
        # the durable routing decision used by the deterministic local
        # fallback and by terminal AgentRun reconciliation.
        "agent.steward.plan": {
            "required_capability": "structured",
            "modality": "text",
            "output_contract": "json_object",
            "egress_purpose": "search_answer",
            "egress_categories": ("instructions", "source_excerpt"),
        },
        "project.answer": {
            "required_capability": "structured",
            "modality": "text",
            "output_contract": "json_object",
            "egress_purpose": "search_answer",
            "egress_categories": ("instructions", "source_excerpt"),
        },
        # LineMap benchmark turns use the normal model planner.  They are not
        # a special provider path: the same structured/text/JSON contract and
        # project route authority apply to every context evaluation.
        "context.evaluate": {
            "required_capability": "structured",
            "modality": "text",
            "output_contract": "json_object",
            "egress_purpose": "search_answer",
            "egress_categories": ("instructions", "source_excerpt"),
        },
        "companion.vision.analyze": {
            "required_capability": "vision",
            "modality": "image_input",
            "output_contract": "text",
            "egress_purpose": "companion_vision",
            "egress_categories": ("image_frame", "instructions"),
        },
        "project_skill.draft.generate": {
            "required_capability": "structured",
            "modality": "text",
            "output_contract": "json_object",
            "egress_purpose": "memory_candidate",
            "egress_categories": ("instructions", "source_excerpt"),
        },
        "source.document.draft": {
            "required_capability": "structured",
            "modality": "text",
            "output_contract": "json_object",
            "egress_purpose": "document_draft",
            "egress_categories": ("instructions", "source_excerpt"),
        },
        "memory.candidate.propose": {
            "required_capability": "structured",
            "modality": "text",
            "output_contract": "json_object",
            "egress_purpose": "memory_candidate",
            "egress_categories": ("instructions", "source_excerpt"),
        },
        "workbench.input.classification.enhance": {
            "required_capability": "structured",
            "modality": "text",
            "output_contract": "json_object",
            "egress_purpose": "intake_classification",
            "egress_categories": ("instructions", "source_excerpt"),
        },
        "companion.chat.respond": {
            "required_capability": "text",
            "modality": "text",
            "output_contract": "text",
            "egress_purpose": "companion_chat",
            "egress_categories": ("instructions", "source_excerpt"),
        },
        "image.generation.completed": {
            "required_capability": "image_generation",
            "modality": "image_generation",
            "output_contract": "image_asset",
            "egress_purpose": "image_generation",
            "egress_categories": ("instructions",),
        },
        "developer_studio.test_lab.result": {
            "required_capability": "structured",
            "modality": "text",
            "output_contract": "json_object",
            "egress_purpose": "connection_test",
            "egress_categories": ("instructions", "source_excerpt"),
        },
    }

    def __init__(
        self,
        container: object,
        payloads: TurnPayloadStorePort,
        *,
        agent_binding_verifier: Callable[
            [Mapping[str, object], Mapping[str, object]], Mapping[str, object]
        ] | None = None,
        recognition_routing: object | None = None,
        task_routing: object | None = None,

        answer_routing: object | None = None,
    ) -> None:
        self._container = container
        self._payloads = payloads
        self._agent_binding_verifier = agent_binding_verifier
        self._recognition_routing = recognition_routing
        self._task_routing = task_routing

        self._answer_routing = answer_routing

    def acquire(
        self,
        request: Mapping[str, object],
        *,
        project_id: str,
        project_profile_id: str,
        project_profile_revision: int,
        boundary_profile_id: str,
        boundary_profile_revision: int,
        capability_ids: Sequence[str],
        skill_snapshot_revision: str | None,
    ) -> TurnModelRoutingSnapshot | None:
        outcome = request.get("desired_outcome")
        if self._task_routing is not None and self._task_routing.handles(request):
            self._verified_agent_binding(request)
            return self._task_routing.acquire(request=request,project_id=project_id,
                project_profile_id=project_profile_id,project_profile_revision=project_profile_revision,
                boundary_profile_id=boundary_profile_id,boundary_profile_revision=boundary_profile_revision,
                capability_ids=capability_ids)
        if outcome == "project.answer" and request.get("execution_policy", {}).get("template_version") == 2:
            exact = request.get("capability_request", {})
            if (self._answer_routing is None or exact.get("mode") != "execute_exact_v1"
                    or exact.get("capability_id") != "workbench.answer.execute"
                    or tuple(capability_ids) != ("workbench.answer.execute",)):
                raise TurnModelRoutingSnapshotAuthorityError("product answer exact binding is unavailable")
            return self._answer_routing.acquire(request=request, project_id=project_id,
                project_profile_id=project_profile_id, project_profile_revision=project_profile_revision,
                boundary_profile_id=boundary_profile_id, boundary_profile_revision=boundary_profile_revision,
                capability_ids=capability_ids)
        if outcome == "recognition.task.result" and self._recognition_routing is not None:
            exact = request.get("capability_request")
            privacy = request.get("privacy")
            context = request.get("context_policy")
            capability_id = exact.get("capability_id") if isinstance(exact, Mapping) else None
            local_capability = capability_id == "recognition.task.execute.local"
            remote_capability = capability_id == "recognition.task.execute"
            remote_allowed = isinstance(privacy, Mapping) and privacy.get("allow_remote") is True and privacy.get("mode") == "remote_allowed"
            if (not isinstance(exact, Mapping) or exact.get("mode") != "execute_exact_v1"
                    or not (local_capability or remote_capability)
                    or tuple(capability_ids) != (capability_id,)
                    or remote_allowed != remote_capability
                    or not isinstance(exact.get("arguments"), Mapping)
                    or not isinstance(privacy, Mapping) or not isinstance(context, Mapping)
                    or any(context.get(key) is not False for key in (
                        "include_project_skill", "include_memory", "include_session_history"))):
                raise TurnModelRoutingSnapshotAuthorityError("recognition routing requires an exact confirmed context")
            return self._recognition_routing.acquire(
                turn_id=_text(request.get("turn_id"), "turn id"), project_id=project_id,
                context_packet_id=_text(exact["arguments"].get("context_packet_id"), "context packet id"),
                project_profile_id=project_profile_id, project_profile_revision=project_profile_revision,
                boundary_profile_id=boundary_profile_id, boundary_profile_revision=boundary_profile_revision,
                capability_ids=tuple(capability_ids), agent_binding=self._verified_agent_binding(request),
                allow_remote=remote_allowed,
                expected_execution_location="local_loopback" if local_capability else "remote",
            )
        spec = self._OUTCOMES.get(str(outcome))
        if spec is None:
            return None
        turn_id = _text(request.get("turn_id"), "turn id")
        agent_binding = self._verified_agent_binding(request)
        existing = self._payloads.get_immutable_payload(turn_id, self.snapshot_kind)
        if existing is not None:
            payload = validate_turn_model_routing_snapshot(existing[1])
            self._validate_identity(
                payload,
                request=request,
                project_id=project_id,
                project_profile_id=project_profile_id,
                project_profile_revision=project_profile_revision,
                boundary_profile_id=boundary_profile_id,
                boundary_profile_revision=boundary_profile_revision,
                capability_ids=capability_ids,
                skill_snapshot_revision=skill_snapshot_revision,
                outcome_spec=spec,
                agent_binding=agent_binding,
            )
            return TurnModelRoutingSnapshot(
                existing[0], turn_model_routing_snapshot_revision(payload), payload
            )

        privacy = request.get("privacy")
        context_policy = request.get("context_policy")
        input_value = request.get("input")
        if not isinstance(privacy, Mapping) or not isinstance(context_policy, Mapping) or not isinstance(input_value, Mapping):
            raise TurnModelRoutingSnapshotAuthorityError("Turn model routing input is unavailable")
        refs = input_value.get("refs")
        if not isinstance(refs, list):
            raise TurnModelRoutingSnapshotAuthorityError("Turn model routing refs are unavailable")
        privacy_scope = (
            "remote_allowed"
            if privacy.get("allow_remote") is True and privacy.get("mode") == "remote_allowed"
            else "local_only"
        )
        payload = project_turn_model_routing_snapshot(
            self._container,
            turn_id=turn_id,
            project_id=project_id,
            required_capability=str(spec["required_capability"]),
            modality=str(spec["modality"]),
            output_contract=str(spec["output_contract"]),
            egress_purpose=str(spec["egress_purpose"]),
            egress_categories=tuple(spec["egress_categories"]),
            privacy_scope=privacy_scope,
            retention_policy="turn_only",
            capability_ids=tuple(capability_ids),
            skill_snapshot_revision=skill_snapshot_revision,
            context_policy=context_policy,
            input_refs=refs,
            agent_binding=agent_binding,
        )
        self._validate_identity(
            payload,
            request=request,
            project_id=project_id,
            project_profile_id=project_profile_id,
            project_profile_revision=project_profile_revision,
            boundary_profile_id=boundary_profile_id,
            boundary_profile_revision=boundary_profile_revision,
            capability_ids=capability_ids,
            skill_snapshot_revision=skill_snapshot_revision,
            outcome_spec=spec,
            agent_binding=agent_binding,
        )
        payload_ref = self._payloads.get_or_create_immutable_payload(
            turn_id, self.snapshot_kind, payload
        )
        return TurnModelRoutingSnapshot(
            payload_ref, turn_model_routing_snapshot_revision(payload), payload
        )

    @staticmethod
    def _validate_identity(
        payload: Mapping[str, object], *, request: Mapping[str, object],
        project_id: str, project_profile_id: str, project_profile_revision: int,
        boundary_profile_id: str, boundary_profile_revision: int,
        capability_ids: Sequence[str], skill_snapshot_revision: str | None,
        outcome_spec: Mapping[str, object], agent_binding: Mapping[str, object] | None,
    ) -> None:
        turn = payload.get("turn")
        project = payload.get("project")
        profile = payload.get("profile")
        boundary = payload.get("boundary")
        requirement = payload.get("requirement")
        if not all(isinstance(item, Mapping) for item in (turn, project, profile, boundary, requirement)):
            raise TurnModelRoutingSnapshotAuthorityError("Turn model routing identity is unavailable")
        expected_capabilities = sorted(set(capability_ids))
        actual_agent_binding = payload.get("agent")
        privacy = request.get("privacy")
        expected_privacy_scope = (
            "remote_allowed"
            if isinstance(privacy, Mapping)
            and privacy.get("allow_remote") is True
            and privacy.get("mode") == "remote_allowed"
            else "local_only"
        )
        if (
            turn.get("turn_id") != request.get("turn_id")  # type: ignore[union-attr]
            or project.get("project_id") != project_id  # type: ignore[union-attr]
            or profile.get("profile_id") != project_profile_id  # type: ignore[union-attr]
            or profile.get("profile_revision") != project_profile_revision  # type: ignore[union-attr]
            or boundary.get("profile_id") != boundary_profile_id  # type: ignore[union-attr]
            or boundary.get("profile_revision") != boundary_profile_revision  # type: ignore[union-attr]
            or requirement.get("capability_ids") != expected_capabilities  # type: ignore[union-attr]
            or requirement.get("skill_snapshot_revision") != skill_snapshot_revision  # type: ignore[union-attr]
            or requirement.get("required_capability") != outcome_spec["required_capability"]  # type: ignore[union-attr]
            or requirement.get("modality") != outcome_spec["modality"]  # type: ignore[union-attr]
            or requirement.get("output_contract") != outcome_spec["output_contract"]  # type: ignore[union-attr]
            or requirement.get("egress_purpose") != outcome_spec["egress_purpose"]  # type: ignore[union-attr]
            or requirement.get("egress_categories") != sorted(outcome_spec["egress_categories"])  # type: ignore[union-attr,call-overload]
            or requirement.get("privacy_scope") != expected_privacy_scope  # type: ignore[union-attr]
            or actual_agent_binding != agent_binding
        ):
            raise TurnModelRoutingSnapshotAuthorityError("Turn model routing authority drifted")

    def _verified_agent_binding(
        self, request: Mapping[str, object],
    ) -> Mapping[str, object] | None:
        raw = request.get("agent_binding")
        if raw is None:
            return None
        if not isinstance(raw, Mapping) or self._agent_binding_verifier is None:
            raise TurnModelRoutingSnapshotAuthorityError(
                "Turn agent binding is not trusted"
            )
        try:
            verified = self._agent_binding_verifier(request, raw)
        except Exception as error:
            raise TurnModelRoutingSnapshotAuthorityError(
                "Turn agent binding is not trusted"
            ) from error
        if not isinstance(verified, Mapping) or dict(verified) != dict(raw):
            raise TurnModelRoutingSnapshotAuthorityError(
                "Turn agent binding drifted"
            )
        return dict(verified)


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TurnModelRoutingSnapshotAuthorityError(f"{label} must be non-empty")
    return value.strip()
