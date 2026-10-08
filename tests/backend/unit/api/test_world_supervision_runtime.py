from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from backend.api.personal_world_model_runtime import PersonalWorldModelRuntime
from backend.api.world_supervision_runtime import WorldSupervisionRuntime
from core.personal_world_model import WorldEventDraft, WorldEventKind


PROJECT = "project-supervision-runtime"


def test_system_writer_declares_a_deterministic_default_claim_after_action(tmp_path: Path) -> None:
    world = PersonalWorldModelRuntime.for_root(
        tmp_path,
        now=lambda: datetime(2026, 9, 2, 8, 0, tzinfo=timezone.utc),
    )
    world.append_event(_goal())
    action = world.append_event(_action()).event

    supervision = WorldSupervisionRuntime(world=world)
    first = supervision.declare_default_claim(
        project_id=PROJECT,
        action_id="action-supervision",
        expected_outcome="A verified next project step is available",
        action_sequence=action.sequence,
        recorded_at="2026-09-02T08:00:00Z",
    )
    replay = supervision.declare_default_claim(
        project_id=PROJECT,
        action_id="action-supervision",
        expected_outcome="A verified next project step is available",
        action_sequence=action.sequence,
        recorded_at="2026-09-02T08:00:00Z",
    )

    projection = world.project(PROJECT)
    claim = projection.supervision.active_claim
    assert first.replayed is False and replay.replayed is True
    assert claim is not None
    assert claim.action_id == "action-supervision"
    assert claim.basis_sequence == action.sequence
    assert claim.expected_signals == ("A verified next project step is available",)
    assert claim.checkpoint_policy == "after_effect"


def test_system_writer_preserves_adapter_verified_evidence_refs(tmp_path: Path) -> None:
    world = PersonalWorldModelRuntime.for_root(tmp_path)
    world.append_event(_goal())
    action = world.append_event(_action()).event
    supervision = WorldSupervisionRuntime(world=world)
    supervision.declare_default_claim(
        project_id=PROJECT,
        action_id="action-supervision",
        expected_outcome="A verified next project step is available",
        action_sequence=action.sequence,
        recorded_at="2026-09-02T08:00:00Z",
    )

    verification = supervision.record_verification(
        project_id=PROJECT,
        action_id="action-supervision",
        verdict="supported",
        finding="A reviewer terminal receipt confirmed the expected outcome.",
        checked_world_sequence=3,
        evidence_refs=["crp://receipts/project-supervision/reviewer-terminal"],
        recorded_at="2026-09-02T08:00:00Z",
    )
    decision = supervision.record_decision(
        project_id=PROJECT,
        action_id="action-supervision",
        verification_id=str(verification.event.payload["verification_id"]),
        disposition="continue",
        rationale="The reviewed receipt supports the next action.",
        evidence_refs=["crp://receipts/project-supervision/fan-in"],
        recorded_at="2026-09-02T08:00:00Z",
    )

    assert verification.event.payload["evidence_refs"] == [
        "crp://receipts/project-supervision/reviewer-terminal"
    ]
    assert decision.event.payload["evidence_refs"] == [
        "crp://receipts/project-supervision/fan-in"
    ]


def _goal() -> WorldEventDraft:
    return WorldEventDraft(
        event_id="goal-supervision-runtime",
        project_id=PROJECT,
        kind=WorldEventKind.GOAL_DECLARED,
        actor="user",
        source_ref="crp://tests/project-supervision-runtime/goal",
        source_revision="1",
        occurred_at="2026-09-02T08:00:00Z",
        recorded_at="2026-09-02T08:00:00Z",
        payload={
            "goal_id": "goal-supervision-runtime",
            "title": "Keep project actions supervised",
            "success_criteria": ["Each action has an accountable verification claim"],
            "target_at": None,
            "evidence_refs": ["crp://tests/project-supervision-runtime/goal"],
        },
    )


def _action() -> WorldEventDraft:
    return WorldEventDraft(
        event_id="action-supervision-runtime",
        project_id=PROJECT,
        kind=WorldEventKind.ACTION_PLANNED,
        actor="user",
        source_ref="crp://tests/project-supervision-runtime/action",
        source_revision="1",
        occurred_at="2026-09-02T08:00:00Z",
        recorded_at="2026-09-02T08:00:00Z",
        payload={
            "action_id": "action-supervision",
            "title": "Plan a supervised action",
            "expected_outcome": "A verified next project step is available",
            "effect_class": "PURE",
            "gate_requirement": "none",
            "due_at": None,
            "evidence_refs": ["crp://tests/project-supervision-runtime/action"],
        },
    )
