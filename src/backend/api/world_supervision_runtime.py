"""System-owned writer for bounded project World supervision facts."""

from __future__ import annotations

from collections.abc import Sequence

from core.personal_world_model import (
    PersonalWorldModelError,
    WorldEventAppendResult,
    WorldEventDraft,
    WorldEventKind,
    validate_world_evidence_ref,
    validate_world_identifier,
)

from backend.api.personal_world_model_runtime import PersonalWorldModelRuntime


class WorldSupervisionRuntime:
    """Append only deterministic, system-derived supervision events.

    This runtime deliberately accepts no client event envelope.  It derives
    identities, provenance and claim content from an already persisted action.
    """

    def __init__(self, *, world: PersonalWorldModelRuntime) -> None:
        self._world = world

    def declare_default_claim(
        self,
        *,
        project_id: str,
        action_id: str,
        expected_outcome: str,
        action_sequence: int,
        recorded_at: str,
        supersedes_claim_id: str | None = None,
    ) -> WorldEventAppendResult:
        project = validate_world_identifier(project_id, "project id")
        action = validate_world_identifier(action_id, "action id")
        state = self._world.project(project, now=recorded_at)
        planned = next((item for item in state.planned_actions if item.action_id == action), None)
        if planned is None or planned.planned_sequence != action_sequence:
            raise PersonalWorldModelError("supervision claim action binding is unavailable")
        if planned.expected_outcome != expected_outcome:
            raise PersonalWorldModelError("supervision claim expected outcome drifted")
        supersedes = (
            None
            if supersedes_claim_id is None
            else validate_world_identifier(
                supersedes_claim_id, "superseded supervision claim id"
            )
        )
        claim_id = _identity("world-supervision-claim", action)
        source_ref = f"crp://world-supervision/{project}/actions/{action}/claim"
        return self._world.append_event(WorldEventDraft(
            event_id=_identity("world-supervision-claim-event", action),
            project_id=project,
            kind=WorldEventKind.SUPERVISION_CLAIM_DECLARED,
            actor="system",
            source_ref=source_ref,
            source_revision=str(action_sequence),
            occurred_at=recorded_at,
            recorded_at=recorded_at,
            payload={
                "claim_id": claim_id,
                "supersedes_claim_id": supersedes,
                "action_id": action,
                "hypothesis": "The planned action remains valid while its expected outcome is verified.",
                "expected_signals": [expected_outcome],
                "falsification_signals": ["The expected project outcome was not observed."],
                "checkpoint_policy": "after_effect",
                "pivot_conditions": ["Replan when the expected project outcome is not observed."],
                "stop_conditions": ["Stop when verified evidence identifies a safety or authority boundary."],
                "basis_sequence": action_sequence,
                "evidence_refs": [source_ref],
            },
        ))

    def record_verification(
        self,
        *,
        project_id: str,
        action_id: str,
        verdict: str,
        finding: str,
        checked_world_sequence: int,
        evidence_refs: Sequence[object],
        recorded_at: str,
    ) -> WorldEventAppendResult:
        project = validate_world_identifier(project_id, "project id")
        action = validate_world_identifier(action_id, "action id")
        claim = self._active_claim(project, action, recorded_at)
        verified_refs = _verified_refs(evidence_refs)
        identity = _identity("world-supervision-verification", f"{action}-{checked_world_sequence}")
        source_ref = f"crp://world-supervision/{project}/actions/{action}/verification/{checked_world_sequence}"
        return self._world.append_event(WorldEventDraft(
            event_id=_identity("world-supervision-verification-event", f"{action}-{checked_world_sequence}"),
            project_id=project,
            kind=WorldEventKind.SUPERVISION_VERIFICATION_RECORDED,
            actor="system",
            source_ref=source_ref,
            source_revision=str(checked_world_sequence),
            occurred_at=recorded_at,
            recorded_at=recorded_at,
            payload={
                "verification_id": identity,
                "claim_id": claim.claim_id,
                "action_id": action,
                "verdict": verdict,
                "finding": finding,
                "checked_world_sequence": checked_world_sequence,
                "evidence_refs": list(verified_refs),
            },
        ))

    def record_decision(
        self,
        *,
        project_id: str,
        action_id: str,
        verification_id: str,
        disposition: str,
        rationale: str,
        evidence_refs: Sequence[object],
        recorded_at: str,
    ) -> WorldEventAppendResult:
        project = validate_world_identifier(project_id, "project id")
        action = validate_world_identifier(action_id, "action id")
        verification = validate_world_identifier(verification_id, "verification id")
        claim = self._active_claim(project, action, recorded_at)
        verified_refs = _verified_refs(evidence_refs)
        source_ref = f"crp://world-supervision/{project}/actions/{action}/decision/{verification}"
        return self._world.append_event(WorldEventDraft(
            event_id=_identity("world-supervision-decision-event", verification),
            project_id=project,
            kind=WorldEventKind.SUPERVISION_DECISION_RECORDED,
            actor="system",
            source_ref=source_ref,
            source_revision="1",
            occurred_at=recorded_at,
            recorded_at=recorded_at,
            payload={
                "decision_id": _identity("world-supervision-decision", verification),
                "claim_id": claim.claim_id,
                "verification_id": verification,
                "action_id": action,
                "disposition": disposition,
                "rationale": rationale,
                "evidence_refs": list(verified_refs),
            },
        ))

    def current_claim(self, project_id: str, action_id: str):
        """Return the single active claim status for a planned action."""

        project = validate_world_identifier(project_id, "project id")
        action = validate_world_identifier(action_id, "action id")
        state = self._world.project(project)
        status = next(
            (item for item in state.supervision.claim_statuses if item.claim.action_id == action),
            None,
        )
        if status is None:
            raise PersonalWorldModelError("active supervision claim is unavailable")
        return status

    def current_world_sequence(self, project_id: str) -> int:
        """Read the projection sequence immediately before an observer write."""

        project = validate_world_identifier(project_id, "project id")
        return int(self._world.project(project).through_sequence)

    def freshness_allows(self, project_id: str) -> bool:
        """Block new cluster dispatch only for active supervisory stop signals."""

        project = validate_world_identifier(project_id, "project id")
        blocked = {"replan_required", "stop_required", "escalate_user"}
        state = self._world.project(project)
        return all(
            item.latest_decision is None or item.latest_decision.disposition not in blocked
            for item in state.supervision.claim_statuses
        )

    def _active_claim(self, project_id: str, action_id: str, recorded_at: str):
        # ``recorded_at`` is kept in this private write path so existing event
        # construction remains explicit.  Claim authority is always the live
        # projection, never a caller-provided snapshot.
        del recorded_at
        return self.current_claim(project_id, action_id).claim


def _identity(prefix: str, suffix: str) -> str:
    return validate_world_identifier(f"{prefix}-{suffix}", "supervision identity")


def _verified_refs(value: Sequence[object]) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not 1 <= len(value) <= 8:
        raise PersonalWorldModelError("supervision evidence references are invalid")
    return tuple(validate_world_evidence_ref(item) for item in value)
