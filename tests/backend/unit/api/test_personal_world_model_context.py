from __future__ import annotations

from pathlib import Path

import pytest

from backend.api.ai_profile_resolvers import (
    ProjectAwareCapabilityManifestResolver,
    ProjectAwareContextManifestResolver,
    ProjectProfileResolutionError,
    TurnProjectProfileSnapshotAuthority,
)
from backend.api.personal_world_model_context import (
    PersonalWorldModelContextError,
    TurnWorldStateSnapshotAuthority,
)
from backend.api.personal_world_model_runtime import PersonalWorldModelRuntime
from backend.security.project_boundary_profiles import ProjectBoundaryProfileStore
from backend.security.project_capability_profiles import ProjectCapabilityProfileStore
from core.ai_kernel import (
    CapabilityDefinition,
    ContextEntry,
    ContextManifest,
    InMemoryTurnPayloadStore,
    ModelGatewayAgentPlanner,
    context_manifest_to_payload,
)
from core.model_gateway import ModelResult
from core.personal_world_model import (
    FeedbackCost,
    FeedbackFact,
    OutcomeStatus,
    StateDelta,
    UserEvaluation,
    UserEvaluationVerdict,
    WorldEventDraft,
    WorldEventKind,
    feedback_event_draft,
)


PROJECT = "project-context"
ACTION = "operation-context"


def test_same_turn_snapshot_is_immutable_and_next_turn_consumes_feedback(tmp_path: Path) -> None:
    payloads = InMemoryTurnPayloadStore()
    runtime = PersonalWorldModelRuntime.for_root(tmp_path)
    runtime.append_event(_goal())
    runtime.append_event(_action())
    authority = TurnWorldStateSnapshotAuthority.for_root(tmp_path, payloads=payloads)
    first_request = _request("turn-context-1", "2026-09-01T10:00:00Z")

    first = authority.acquire(
        first_request,
        project_id=PROJECT,
        max_context_bytes=4096,
    )
    runtime.append_event(_feedback())
    replay = authority.acquire(
        first_request,
        project_id=PROJECT,
        max_context_bytes=4096,
    )
    next_turn = authority.acquire(
        _request("turn-context-2", "2026-09-01T10:02:00Z"),
        project_id=PROJECT,
        max_context_bytes=4096,
    )

    assert first.payload_ref == replay.payload_ref
    assert first.payload["through_sequence"] == 2
    assert first.payload["planning"]["latest_feedback"] is None
    assert next_turn.payload["through_sequence"] == 3
    assert next_turn.payload["planning"]["latest_feedback"]["feedback_id"] == "feedback-context"
    assert next_turn.payload["planning"]["latest_feedback"]["outcome"] == "partial"
    assert next_turn.provenance_refs == (
        "crp://session/turn-effect/tool-invocation-outcome/outcome-context",
    )

    turn_id = "turn-context-2"
    capability_ref = payloads.put(turn_id, "capability-manifest", {"kind": "fixture"})
    manifest = ContextManifest(
        manifest_id=f"context-manifest-{turn_id}",
        turn_id=turn_id,
        resolver_id="world-feedback-fixture",
        project_id=PROJECT,
        series_id=None,
        project_profile_id="project-profile-context",
        project_profile_revision=1,
        boundary_profile_id="project-boundary-context",
        boundary_profile_revision=1,
        capability_manifest_ref=capability_ref,
        entries=(ContextEntry(
            entry_id="context-entry-world-state-projection",
            kind="world_state_projection",
            source_ref=next_turn.source_ref,
            payload_ref=next_turn.payload_ref,
            source_project_id=PROJECT,
            revision_identity=next_turn.revision,
            content_fingerprint=None,
            provenance_refs=next_turn.provenance_refs,
            disclosure="model",
            selection_reason="turn_frozen_derived_project_world_state",
            content_bytes=next_turn.content_bytes,
        ),),
        compactions=(),
        excluded_reason_counts=(),
        max_context_bytes=4096,
        selected_context_bytes=next_turn.content_bytes,
    )
    manifest_ref = payloads.put(
        turn_id, "context-manifest", context_manifest_to_payload(manifest),
    )
    gateway = _Gateway()
    ModelGatewayAgentPlanner(gateway).plan(
        _planner_request(),
        [{"type": "context.resolved", "data": {"payload_ref": manifest_ref}}],
        [_capability()],
        payloads,
    )
    assert "feedback-context" in gateway.requests[0].input
    assert "One acceptance check remains" in gateway.requests[0].input


def test_as_of_fence_excludes_events_recorded_after_turn_creation(tmp_path: Path) -> None:
    runtime = PersonalWorldModelRuntime.for_root(tmp_path)
    runtime.append_event(_goal())
    runtime.append_event(_action())
    runtime.append_event(_feedback())
    authority = TurnWorldStateSnapshotAuthority.for_root(
        tmp_path,
        payloads=InMemoryTurnPayloadStore(),
    )

    snapshot = authority.acquire(
        _request("turn-context-before-feedback", "2026-09-01T10:00:00Z"),
        project_id=PROJECT,
        max_context_bytes=4096,
    )

    assert snapshot.payload["through_sequence"] == 2
    assert snapshot.payload["planning"]["latest_feedback"] is None


def test_snapshot_compacts_within_budget_and_rejects_identity_drift(tmp_path: Path) -> None:
    runtime = PersonalWorldModelRuntime.for_root(tmp_path)
    runtime.append_event(_goal())
    for index in range(18):
        runtime.append_event(WorldEventDraft(
            event_id=f"task-context-{index}",
            project_id=PROJECT,
            kind=WorldEventKind.TASK_OBSERVED,
            actor="system",
            source_ref=f"crp://tasks/project-context/task-{index}",
            source_revision="1",
            occurred_at="2026-09-01T09:30:00Z",
            recorded_at="2026-09-01T09:30:00Z",
            payload={
                "task_id": f"task-{index}",
                "title": "A deliberately detailed governed project task " + ("x" * 260),
                "state": "pending",
                "evidence_refs": [f"crp://tasks/project-context/task-{index}"],
            },
        ))
    payloads = InMemoryTurnPayloadStore()
    authority = TurnWorldStateSnapshotAuthority.for_root(tmp_path, payloads=payloads)
    request = _request("turn-context-compact", "2026-09-01T10:00:00Z")

    snapshot = authority.acquire(request, project_id=PROJECT, max_context_bytes=1024)

    assert snapshot.content_bytes <= 1024
    assert snapshot.payload["compaction_level"] > 0
    with pytest.raises(PersonalWorldModelContextError, match="identity drifted"):
        authority.acquire(request, project_id=PROJECT, max_context_bytes=2048)


def test_project_aware_manifest_exposes_world_state_as_its_own_model_kind(tmp_path: Path) -> None:
    payloads = InMemoryTurnPayloadStore()
    PersonalWorldModelRuntime.for_root(tmp_path).append_event(_goal())
    snapshots = TurnProjectProfileSnapshotAuthority(
        ProjectCapabilityProfileStore(tmp_path),
        ProjectBoundaryProfileStore(tmp_path),
    )
    request = _request("turn-context-manifest", "2026-09-01T10:00:00Z")
    capability = _capability()
    manifest = ProjectAwareCapabilityManifestResolver(snapshots).resolve(
        request, (capability,),
    )
    resolver = ProjectAwareContextManifestResolver(
        snapshots,
        world_state=TurnWorldStateSnapshotAuthority.for_root(tmp_path, payloads=payloads),
    )

    context = resolver.resolve(
        request,
        f"crp://session/{request['turn_id']}/capability-manifest/ref",
        manifest,
    )

    entry = next(item for item in context.entries if item.kind == "world_state_projection")
    assert entry.disclosure == "model"
    assert entry.source_project_id == PROJECT
    assert entry.source_ref == "crp://world-model/project-context/events/through-1"
    assert payloads.get(entry.payload_ref)["projection_authority"] == "derived_only"
    assert context.selected_context_bytes == entry.content_bytes

    manifest_ref = payloads.put(
        str(request["turn_id"]),
        "context-manifest",
        context_manifest_to_payload(context),
    )
    gateway = _Gateway()
    ModelGatewayAgentPlanner(gateway).plan(
        {
            "desired_outcome": "Plan the next governed project action",
            "input": {"kind": "text", "text": "Continue", "refs": []},
            "scope": {"kind": "project", "project_id": PROJECT, "series_id": None},
            "privacy": {"allow_remote": False, "mode": "local_only"},
        },
        [{"type": "context.resolved", "data": {"payload_ref": manifest_ref}}],
        [capability],
        payloads,
    )
    assert '"kind":"world_state_projection"' in gateway.requests[0].input
    assert "Make the next project plan consume verified feedback" in gateway.requests[0].input


def test_manifest_rejects_world_snapshot_project_drift(tmp_path: Path) -> None:
    class _DriftedWorldState:
        def acquire(self, request, *, project_id, max_context_bytes):
            raise PersonalWorldModelContextError("Turn and WorldState project scopes differ")

    snapshots = TurnProjectProfileSnapshotAuthority(
        ProjectCapabilityProfileStore(tmp_path),
        ProjectBoundaryProfileStore(tmp_path),
    )
    request = _request("turn-context-drift", "2026-09-01T10:00:00Z")
    capability = _capability()
    manifest = ProjectAwareCapabilityManifestResolver(snapshots).resolve(
        request, (capability,),
    )

    with pytest.raises(ProjectProfileResolutionError, match="project scopes differ"):
        ProjectAwareContextManifestResolver(
            snapshots,
            world_state=_DriftedWorldState(),  # type: ignore[arg-type]
        ).resolve(
            request,
            f"crp://session/{request['turn_id']}/capability-manifest/ref",
            manifest,
        )


def _request(turn_id: str, created_at: str) -> dict[str, object]:
    return {
        "turn_id": turn_id,
        "scope": {"kind": "project", "project_id": PROJECT, "series_id": None},
        "privacy": {"mode": "local_only"},
        "capability_policy": {
            "allowed": ["memory.recall"],
            "denied": [],
            "require_approval": [],
        },
        "context_policy": {"max_context_bytes": 16_384},
        "input": {"refs": []},
        "created_at": created_at,
    }


class _Gateway:
    def __init__(self) -> None:
        self.requests = []

    def invoke(self, request):
        self.requests.append(request)
        return ModelResult(
            {"type": "complete", "summary": "planned", "evidence_refs": []},
            "fixture-provider",
            "fixture-model",
            {},
        )


def _capability() -> CapabilityDefinition:
    return CapabilityDefinition(
        "memory.recall",
        1,
        "read",
        False,
        "read_only",
        "crp://default/contracts/in.schema.json",
        "crp://default/contracts/out.schema.json",
    )


def _planner_request() -> dict[str, object]:
    return {
        "desired_outcome": "Plan the next governed project action",
        "input": {"kind": "text", "text": "Continue", "refs": []},
        "scope": {"kind": "project", "project_id": PROJECT, "series_id": None},
        "privacy": {"allow_remote": False, "mode": "local_only"},
    }


def _goal() -> WorldEventDraft:
    return WorldEventDraft(
        event_id="goal-context-event",
        project_id=PROJECT,
        kind=WorldEventKind.GOAL_DECLARED,
        actor="user",
        source_ref="crp://projects/project-context/goals/goal-context",
        source_revision="1",
        occurred_at="2026-09-01T09:00:00Z",
        recorded_at="2026-09-01T09:00:00Z",
        payload={
            "goal_id": "goal-context",
            "title": "Make the next project plan consume verified feedback",
            "success_criteria": ["A later Turn contains the previous outcome"],
            "target_at": None,
            "evidence_refs": ["crp://projects/project-context/goals/goal-context"],
        },
    )


def _action() -> WorldEventDraft:
    return WorldEventDraft(
        event_id="action-context-event",
        project_id=PROJECT,
        kind=WorldEventKind.ACTION_PLANNED,
        actor="system",
        source_ref="crp://plans/project-context/operation-context",
        source_revision="1",
        occurred_at="2026-09-01T09:10:00Z",
        recorded_at="2026-09-01T09:10:00Z",
        payload={
            "action_id": ACTION,
            "title": "Apply the first governed project change",
            "expected_outcome": "The next plan observes the resulting revision",
            "effect_class": "QUERYABLE",
            "gate_requirement": "approval",
            "due_at": None,
            "evidence_refs": ["crp://plans/project-context/operation-context"],
        },
    )


def _feedback() -> WorldEventDraft:
    fact = FeedbackFact(
        feedback_id="feedback-context",
        supersedes_feedback_id=None,
        project_id=PROJECT,
        action_id=ACTION,
        expected_outcome="The next plan observes the resulting revision",
        actual_outcome="The revision changed but an acceptance check remains",
        outcome=OutcomeStatus.PARTIAL,
        state_delta=(StateDelta("acceptance.status", "assumed", "pending"),),
        cost=FeedbackCost(
            elapsed_ms=800,
            model_input_tokens=20,
            model_output_tokens=10,
            external_calls=1,
            human_attention_seconds=2,
        ),
        user_evaluation=UserEvaluation(
            UserEvaluationVerdict.CORRECTED,
            rating=3,
            note="One acceptance check remains",
        ),
        evidence_refs=(
            "crp://session/turn-effect/tool-invocation-outcome/outcome-context",
        ),
    )
    return feedback_event_draft(
        fact,
        event_id="feedback-context-event",
        source_ref="crp://session/turn-effect/tool-invocation-outcome/outcome-context",
        source_revision="4",
        occurred_at="2026-09-01T10:01:00Z",
        recorded_at="2026-09-01T10:01:00Z",
        actor="user",
    )
