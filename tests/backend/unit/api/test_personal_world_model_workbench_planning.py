from __future__ import annotations

import pytest

from backend.api.personal_world_model_context import (
    PersonalWorldModelContextError,
    TurnWorldStateSnapshotAuthority,
)
from backend.api.personal_world_model_runtime import PersonalWorldModelRuntime
from backend.api.workbench_ai_runtime import WorkbenchQuestionPlanner
from core.ai_kernel import (
    ContextEntry,
    ContextManifest,
    InMemoryTurnPayloadStore,
    context_manifest_to_payload,
)

from tests.backend.unit.api.test_personal_world_model_context import (
    PROJECT,
    _action,
    _feedback,
    _goal,
    _request,
)


def test_world_project_planner_puts_frozen_feedback_into_real_tool_query(tmp_path) -> None:
    world = PersonalWorldModelRuntime.for_root(tmp_path)
    world.append_event(_goal())
    world.append_event(_action())
    world.append_event(_feedback())
    payloads = InMemoryTurnPayloadStore()
    turn_id = "turn-world-workbench"
    request = _request(turn_id, "2026-09-01T10:02:00Z")
    request.update(
        {
            "session_id": "world-project",
            "input": {"kind": "text", "text": "Plan the next step", "refs": []},
        }
    )
    snapshot = TurnWorldStateSnapshotAuthority.for_root(
        tmp_path,
        payloads=payloads,
    ).acquire(request, project_id=PROJECT, max_context_bytes=4096)
    capability_ref = payloads.put(turn_id, "capability-manifest", {"kind": "fixture"})
    context = ContextManifest(
        manifest_id=f"context-manifest-{turn_id}",
        turn_id=turn_id,
        resolver_id="world-workbench-test",
        project_id=PROJECT,
        series_id=None,
        project_profile_id="project-profile-world-workbench",
        project_profile_revision=1,
        boundary_profile_id="project-boundary-world-workbench",
        boundary_profile_revision=1,
        capability_manifest_ref=capability_ref,
        entries=(
            ContextEntry(
                entry_id="world-state",
                kind="world_state_projection",
                source_ref=snapshot.source_ref,
                payload_ref=snapshot.payload_ref,
                source_project_id=PROJECT,
                revision_identity=snapshot.revision,
                content_fingerprint=None,
                provenance_refs=snapshot.provenance_refs,
                disclosure="model",
                selection_reason="turn_frozen_derived_project_world_state",
                content_bytes=snapshot.content_bytes,
            ),
        ),
        compactions=(),
        excluded_reason_counts=(),
        max_context_bytes=4096,
        selected_context_bytes=snapshot.content_bytes,
    )
    context_ref = payloads.put(
        turn_id,
        "context-manifest",
        context_manifest_to_payload(context),
    )
    events = (
        {"type": "context.resolved", "data": {"payload_ref": context_ref}},
    )

    decision = WorkbenchQuestionPlanner(None).plan(request, events, (), payloads)
    query = decision["arguments"]["query"]
    regular = dict(request)
    regular["session_id"] = "regular-workbench"
    regular_decision = WorkbenchQuestionPlanner(None).plan(
        regular,
        events,
        (),
        payloads,
    )

    assert decision["type"] == "tool"
    assert "feedback-context" in query
    assert "One acceptance check remains" in query
    assert '"latest_feedback"' in query
    assert regular_decision["arguments"]["query"] == "Plan the next step"

    with pytest.raises(ValueError, match="lacks its frozen WorldState"):
        WorkbenchQuestionPlanner(None).plan(request, (), (), payloads)

    drifted = dict(request)
    drifted["scope"] = {"kind": "project", "project_id": "project-other"}
    with pytest.raises(PersonalWorldModelContextError, match="project identity drifted"):
        WorkbenchQuestionPlanner(None).plan(drifted, events, (), payloads)
