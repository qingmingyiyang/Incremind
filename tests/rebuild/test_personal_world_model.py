from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from core.long_horizon_runtime import (
    TaskBudget,
    TaskGraphEvent,
    TaskGraphNode,
    TaskGraphSpec,
    TraceLink,
    TraceSubject,
    ValidationFact,
    VersionBinding,
    provenance_event_identity,
    task_graph_event_identity,
    task_graph_world_identity,
)
from core.personal_world_model import (
    FeedbackCost,
    FeedbackFact,
    OutcomeStatus,
    PersonalWorldModelError,
    ProjectDynamicsEngine,
    SQLiteWorldEventRepository,
    StateDelta,
    UserEvaluation,
    UserEvaluationVerdict,
    WorldEvent,
    WorldEventDraft,
    WorldEventKind,
    feedback_event_draft,
)
from core.recursive_evolution import (
    EvolutionEpisode,
    EvolutionEvent,
    EvolutionPolicy,
    EvolutionTargetKind,
    evolution_event_world_identity,
)
from core.storage_provider import SQLiteStructuredRecordStore


PROJECT = "project-alpha"
T0 = "2026-08-31T08:00:00Z"


def _repository(tmp_path: Path) -> SQLiteWorldEventRepository:
    return SQLiteWorldEventRepository(
        SQLiteStructuredRecordStore(tmp_path / ".rebuild-data" / "personal-world.sqlite3")
    )


def test_world_projection_fail_closes_on_forged_recursive_evolution_envelope() -> None:
    evolution = EvolutionEvent.episode_created(
        EvolutionEpisode(
            "episode-alpha", PROJECT, EvolutionTargetKind.PROMPT_STRATEGY,
            EvolutionPolicy(2, 2, 2, 8, 1, "2026-09-05T12:00:00Z"),
        ),
        T0,
    )
    event_id, source_ref, source_revision = evolution_event_world_identity(evolution)
    valid = WorldEvent(
        event_id, PROJECT, 1, WorldEventKind.EVOLUTION_EVENT_RECORDED, "system",
        source_ref, source_revision, T0, T0, evolution.to_payload(),
    )
    ProjectDynamicsEngine().project((valid,), project_id=PROJECT, now=T0)
    forged = replace(valid, actor="external")
    with pytest.raises(PersonalWorldModelError, match="recursive evolution stream"):
        ProjectDynamicsEngine().project((forged,), project_id=PROJECT, now=T0)


def test_recursive_evolution_world_event_exact_replay_survives_repository_restart(tmp_path: Path) -> None:
    evolution = EvolutionEvent.episode_created(
        EvolutionEpisode(
            "episode-replay", PROJECT, EvolutionTargetKind.PROMPT_STRATEGY,
            EvolutionPolicy(2, 2, 2, 8, 1, "2026-09-05T12:00:00Z"),
        ),
        T0,
    )
    event_id, source_ref, source_revision = evolution_event_world_identity(evolution)
    draft = WorldEventDraft(
        event_id, PROJECT, WorldEventKind.EVOLUTION_EVENT_RECORDED, "system",
        source_ref, source_revision, T0, T0, evolution.to_payload(),
    )
    first = _repository(tmp_path).append(draft)
    restarted = _repository(tmp_path).append(draft)
    assert first.replayed is False
    assert restarted.replayed is True
    assert restarted.event.sequence == 1


def test_world_task_graph_event_is_validated_and_deeply_frozen() -> None:
    subject = TraceSubject(PROJECT, "artifact", "artifact-graph")
    version = VersionBinding("crp://artifacts/project-alpha/artifact-graph", "r1", None)
    spec = TaskGraphSpec(
        "graph-alpha", PROJECT, "graph-command", TaskBudget(1), 1,
        (TaskGraphNode("node-alpha", (), 1, TaskBudget(1), "all", subject, version),),
    )
    graph_event_id = task_graph_event_identity(
        PROJECT, "graph-alpha", "graph.created", "graph-command",
    )
    graph_event = TaskGraphEvent(
        graph_event_id, PROJECT, "graph-alpha", 1, "graph.created", "graph-command",
        {"spec": spec.to_payload()},
    )
    event_id, source_ref, source_revision = task_graph_world_identity(graph_event)
    draft = WorldEventDraft(
        event_id=event_id,
        project_id=PROJECT,
        kind=WorldEventKind.TASK_GRAPH_EVENT_RECORDED,
        actor="system",
        source_ref=source_ref,
        source_revision=source_revision,
        occurred_at=T0,
        recorded_at=T0,
        payload=graph_event.to_payload(),
    )
    with pytest.raises(TypeError):
        draft.payload["payload"]["spec"]["graph_id"] = "changed"  # type: ignore[index]
    ProjectDynamicsEngine().project(
        (WorldEvent(**_event_from_draft(draft, 1)),), project_id=PROJECT, now=T0,
    )

    forged = replace(draft, actor="external")
    with pytest.raises(PersonalWorldModelError, match="task graph stream"):
        ProjectDynamicsEngine().project(
            (WorldEvent(**_event_from_draft(forged, 1)),),
            project_id=PROJECT,
            now=T0,
        )


def _draft(
    event_id: str,
    kind: WorldEventKind,
    payload: dict[str, object],
    *,
    project_id: str = PROJECT,
    occurred_at: str | None = T0,
    recorded_at: str = T0,
    actor: str = "user",
) -> WorldEventDraft:
    return WorldEventDraft(
        event_id=event_id,
        project_id=project_id,
        kind=kind,
        actor=actor,
        source_ref=f"crp://world-input/{project_id}/{event_id}",
        source_revision="revision-1",
        occurred_at=occurred_at,
        recorded_at=recorded_at,
        payload=payload,
    )


def _provenance_draft(
    kind: WorldEventKind,
    payload: dict[str, object],
    *,
    recorded_at: str,
) -> WorldEventDraft:
    event_id, source_ref, source_revision = provenance_event_identity(
        kind.value,
        PROJECT,
        payload,
    )
    return WorldEventDraft(
        event_id=event_id,
        project_id=PROJECT,
        kind=kind,
        actor="system",
        source_ref=source_ref,
        source_revision=source_revision,
        occurred_at=recorded_at,
        recorded_at=recorded_at,
        payload=payload,
    )


def _goal() -> WorldEventDraft:
    return _draft(
        "event-goal",
        WorldEventKind.GOAL_DECLARED,
        {
            "goal_id": "goal-alpha",
            "title": "完成项目推进纵向闭环",
            "success_criteria": [
                "动作经过统一治理",
                "结果反馈进入下一轮规划",
            ],
            "target_at": "2026-09-05T12:00:00Z",
            "evidence_refs": ["crp://sources/project-alpha/goal-note"],
        },
    )


def _task(state: str, *, event_id: str, recorded_at: str) -> WorldEventDraft:
    return _draft(
        event_id,
        WorldEventKind.TASK_OBSERVED,
        {
            "task_id": "task-alpha",
            "title": "完成第一条受治理行动",
            "state": state,
            "evidence_refs": [f"crp://tasks/project-alpha/{event_id}"],
        },
        occurred_at=recorded_at,
        recorded_at=recorded_at,
    )


def _action() -> WorldEventDraft:
    return _draft(
        "event-action",
        WorldEventKind.ACTION_PLANNED,
        {
            "action_id": "action-alpha",
            "title": "生成并验证项目状态文件",
            "expected_outcome": "项目状态文件存在且聚焦验证通过",
            "effect_class": "QUERYABLE",
            "gate_requirement": "notice",
            "due_at": "2026-09-01T10:00:00Z",
            "evidence_refs": ["crp://plans/project-alpha/action-alpha"],
        },
        occurred_at="2026-08-31T08:03:00Z",
        recorded_at="2026-08-31T08:03:00Z",
        actor="system",
    )


def _feedback() -> WorldEventDraft:
    fact = FeedbackFact(
        feedback_id="feedback-alpha",
        supersedes_feedback_id=None,
        project_id=PROJECT,
        action_id="action-alpha",
        expected_outcome="项目状态文件存在且聚焦验证通过",
        actual_outcome="项目状态文件已生成且聚焦验证全部通过",
        outcome=OutcomeStatus.ACHIEVED,
        state_delta=(
            StateDelta("task.task-alpha.state", "in_progress", "completed"),
            StateDelta("project.phase", "in_progress", "ready_for_review"),
        ),
        cost=FeedbackCost(
            elapsed_ms=4200,
            model_input_tokens=180,
            model_output_tokens=64,
            external_calls=1,
            human_attention_seconds=30,
        ),
        user_evaluation=UserEvaluation(
            UserEvaluationVerdict.ACCEPTED,
            rating=5,
            note="结果符合预期，可以进入下一阶段。",
        ),
        evidence_refs=(
            "crp://effects/effect-alpha/receipts/receipt-alpha",
            "crp://file-observations/project-alpha/state-file-alpha",
        ),
    )
    return feedback_event_draft(
        fact,
        event_id="event-feedback",
        source_ref="crp://effects/effect-alpha/receipts/receipt-alpha",
        source_revision="receipt-revision-1",
        occurred_at="2026-08-31T08:04:00Z",
        recorded_at="2026-08-31T08:05:00Z",
    )


def test_repository_is_append_only_idempotent_and_project_scoped(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    first = repository.append(_goal())
    replay = repository.append(_goal())
    other = repository.append(
        _draft(
            "event-beta-goal",
            WorldEventKind.GOAL_DECLARED,
            {
                "goal_id": "goal-beta",
                "title": "保持另一个项目独立",
                "success_criteria": ["独立事件流从一开始"],
                "target_at": None,
                "evidence_refs": ["crp://sources/project-beta/goal-note"],
            },
            project_id="project-beta",
        )
    )

    assert first.replayed is False
    assert replay.replayed is True
    assert first.event == replay.event
    assert first.event.sequence == 1
    assert other.event.sequence == 1
    assert [item.event_id for item in repository.list_project(PROJECT)] == ["event-goal"]
    assert [item.event_id for item in repository.list_project("project-beta")] == ["event-beta-goal"]

    conflicting = replace(_goal(), payload={**dict(_goal().payload), "title": "冲突目标"})
    with pytest.raises(PersonalWorldModelError, match="identity conflicts"):
        repository.append(conflicting)

    reopened = _repository(tmp_path)
    assert reopened.get("event-goal") == first.event
    assert reopened.list_project(PROJECT) == (first.event,)


def test_world_stream_accepts_system_owned_project_provenance_without_changing_state() -> None:
    action = TraceSubject(PROJECT, "world_action", "action-alpha")
    data = TraceSubject(PROJECT, "data", "dataset-alpha")
    action_version = VersionBinding(
        "crp://world-model/project-alpha/actions/action-alpha", "revision-1", None,
    )
    data_version = VersionBinding(
        "crp://sources/project-alpha/dataset-alpha", "revision-7", "dataset-fingerprint-7",
    )
    link = TraceLink(
        PROJECT,
        action,
        action_version,
        data,
        data_version,
        "depends_on",
    )
    validation = ValidationFact(
        PROJECT,
        "validation-action-alpha",
        action,
        action_version,
        "verified",
        "receipt",
        "receipt-schema-1",
        ("crp://receipts/project-alpha/action-alpha",),
    )
    drafts = (
        _goal(),
        _action(),
        _provenance_draft(
            WorldEventKind.PROVENANCE_SUBJECT_RECORDED,
            {"subject": action.to_payload(), "version": action_version.to_payload()},
            recorded_at="2026-08-31T08:03:10Z",
        ),
        _provenance_draft(
            WorldEventKind.PROVENANCE_SUBJECT_RECORDED,
            {"subject": data.to_payload(), "version": data_version.to_payload()},
            recorded_at="2026-08-31T08:03:20Z",
        ),
        _provenance_draft(
            WorldEventKind.PROVENANCE_LINK_RECORDED,
            link.to_payload(),
            recorded_at="2026-08-31T08:03:30Z",
        ),
        _provenance_draft(
            WorldEventKind.PROVENANCE_VALIDATION_RECORDED,
            validation.to_payload(),
            recorded_at="2026-08-31T08:03:40Z",
        ),
    )

    events = tuple(
        WorldEvent(
            sequence=index,
            **{
                key: value
                for key, value in draft.identity_payload().items()
                if key != "schema_version"
            },
        )
        for index, draft in enumerate(drafts, start=1)
    )
    projection = ProjectDynamicsEngine().project(
        events,
        project_id=PROJECT,
        now="2026-08-31T08:04:00Z",
    )

    assert projection.through_sequence == 6
    assert projection.pending_action_ids == ("action-alpha",)
    assert events[-2].payload == link.to_payload()
    assert ValidationFact.from_payload(events[-1].payload) == validation


def test_provenance_world_event_payload_is_deeply_immutable() -> None:
    subject = TraceSubject(PROJECT, "data", "dataset-alpha")
    version = VersionBinding(
        "crp://sources/project-alpha/dataset-alpha",
        "revision-7",
        "dataset-fingerprint-7",
    )
    draft = _provenance_draft(
        WorldEventKind.PROVENANCE_SUBJECT_RECORDED,
        {"subject": subject.to_payload(), "version": version.to_payload()},
        recorded_at="2026-08-31T08:03:10Z",
    )

    nested_subject = draft.payload["subject"]
    assert not isinstance(nested_subject, dict)
    with pytest.raises(TypeError):
        nested_subject["subject_id"] = "mutated"  # type: ignore[index]
    plain_payload = draft.identity_payload()["payload"]
    assert plain_payload["subject"]["subject_id"] == "dataset-alpha"  # type: ignore[index]


def test_world_provenance_payload_rejects_cross_project_subject() -> None:
    subject = TraceSubject("project-beta", "data", "dataset-beta")
    version = VersionBinding(
        "crp://sources/project-beta/dataset-beta", "revision-1", None,
    )

    with pytest.raises(PersonalWorldModelError, match="project scope"):
        _draft(
            "event-cross-project-provenance",
            WorldEventKind.PROVENANCE_SUBJECT_RECORDED,
            {"subject": subject.to_payload(), "version": version.to_payload()},
            actor="system",
        )


def test_full_project_slice_rebuilds_state_feedback_and_next_prediction(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    drafts = (
        _goal(),
        _draft(
            "event-resource",
            WorldEventKind.PROJECT_OBSERVATION,
            {
                "observation_id": "observation-resource",
                "category": "resource",
                "summary": "项目已有状态、动作与回执权威可复用。",
                "evidence_refs": ["crp://architecture/project-alpha/current-authorities"],
            },
            occurred_at="2026-08-31T08:01:00Z",
            recorded_at="2026-08-31T08:01:00Z",
            actor="system",
        ),
        _task("in_progress", event_id="event-task-started", recorded_at="2026-08-31T08:02:00Z"),
        _draft(
            "event-blocker-opened",
            WorldEventKind.BLOCKER_OPENED,
            {
                "blocker_id": "blocker-evidence",
                "summary": "尚未取得动作结果证据。",
                "severity": "high",
                "evidence_refs": ["crp://risks/project-alpha/evidence-gap"],
            },
            occurred_at="2026-08-31T08:02:10Z",
            recorded_at="2026-08-31T08:02:10Z",
            actor="system",
        ),
        _draft(
            "event-blocker-resolved",
            WorldEventKind.BLOCKER_RESOLVED,
            {
                "blocker_id": "blocker-evidence",
                "resolution": "动作计划已绑定验证证据入口。",
                "evidence_refs": ["crp://plans/project-alpha/action-alpha"],
            },
            occurred_at="2026-08-31T08:02:30Z",
            recorded_at="2026-08-31T08:02:30Z",
            actor="system",
        ),
        _action(),
        _feedback(),
        _task("completed", event_id="event-task-completed", recorded_at="2026-08-31T08:06:00Z"),
    )
    for draft in drafts:
        repository.append(draft)

    events = repository.list_project(PROJECT)
    projection = ProjectDynamicsEngine().project(
        events,
        project_id=PROJECT,
        now="2026-08-31T08:07:00Z",
    )
    planning = projection.planning_payload()

    assert projection.persisted_as_authority is False
    assert projection.through_sequence == len(drafts)
    assert projection.phase == "achieved"
    assert projection.goal is not None
    assert projection.goal.goal_id == "goal-alpha"
    assert projection.tasks[0].state == "completed"
    assert projection.blockers == ()
    assert projection.pending_action_ids == ()
    assert projection.latest_feedback is not None
    assert projection.latest_feedback.outcome is OutcomeStatus.ACHIEVED
    assert projection.latest_feedback.cost.elapsed_ms == 4200
    assert projection.latest_feedback.user_evaluation.verdict is UserEvaluationVerdict.ACCEPTED
    assert projection.predictions[0].predicted_state == "goal_review"
    assert projection.confidence >= 0.88
    assert projection.source_event_ids == tuple(item.event_id for item in events)
    assert planning["projection_authority"] == "derived_only"
    assert planning["latest_feedback"]["cost"]["external_calls"] == 1
    assert planning["latest_feedback"]["evidence_refs"] == [
        "crp://effects/effect-alpha/receipts/receipt-alpha",
        "crp://file-observations/project-alpha/state-file-alpha",
    ]

    rebuilt = ProjectDynamicsEngine().project(
        _repository(tmp_path).list_project(PROJECT),
        project_id=PROJECT,
        now="2026-08-31T08:07:00Z",
    )
    assert rebuilt == projection


def test_dynamics_exposes_overdue_feedback_risk_and_counterfactuals(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    repository.append(_goal())
    repository.append(_action())

    projection = ProjectDynamicsEngine().project(
        repository.list_project(PROJECT),
        project_id=PROJECT,
        now="2026-09-20T10:00:00Z",
    )

    assert projection.phase == "awaiting_outcome"
    assert projection.pending_action_ids == ("action-alpha",)
    assert "outcome_feedback_missing" in projection.risk_codes
    assert "planned_action_overdue" in projection.risk_codes
    assert "observations_stale" in projection.risk_codes
    assert projection.predictions[0].predicted_state == "evaluated"
    assert {item.predicted_state for item in projection.counterfactuals} == {
        "evaluated",
        "causal_attribution_risk",
    }


def test_failed_feedback_requires_replanning_and_keeps_causal_evidence(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    repository.append(_goal())
    repository.append(_action())
    failed = FeedbackFact(
        feedback_id="feedback-failed",
        supersedes_feedback_id=None,
        project_id=PROJECT,
        action_id="action-alpha",
        expected_outcome="项目状态文件存在且聚焦验证通过",
        actual_outcome="验证发现状态文件缺少动作回执引用",
        outcome=OutcomeStatus.FAILED,
        state_delta=(StateDelta("action.action-alpha.state", "planned", "failed"),),
        cost=FeedbackCost(elapsed_ms=900),
        user_evaluation=UserEvaluation(
            UserEvaluationVerdict.CORRECTED,
            rating=2,
            note="需要补齐回执引用后再执行。",
        ),
        evidence_refs=("crp://effects/effect-alpha/receipts/receipt-failed",),
    )
    repository.append(
        feedback_event_draft(
            failed,
            event_id="event-feedback-failed",
            source_ref="crp://effects/effect-alpha/receipts/receipt-failed",
            source_revision="receipt-revision-1",
            occurred_at="2026-08-31T08:04:00Z",
            recorded_at="2026-08-31T08:04:30Z",
        )
    )

    projection = ProjectDynamicsEngine().project(
        repository.list_project(PROJECT),
        project_id=PROJECT,
        now="2026-08-31T08:05:00Z",
    )

    assert projection.phase == "needs_revision"
    assert "latest_action_failed" in projection.risk_codes
    assert projection.predictions[0].predicted_state == "in_progress"
    assert {item.predicted_state for item in projection.counterfactuals} == {
        "in_progress",
        "repeated_failure_risk",
    }


def test_supervision_claim_verification_and_decision_are_projected_causally(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    repository.append(_goal())
    repository.append(_action())
    claim = _draft(
        "event-supervision-claim",
        WorldEventKind.SUPERVISION_CLAIM_DECLARED,
        {
            "claim_id": "claim-alpha",
            "supersedes_claim_id": None,
            "action_id": "action-alpha",
            "hypothesis": "The action output requires an independent verification.",
            "expected_signals": ["verified-output"],
            "falsification_signals": ["missing-output"],
            "checkpoint_policy": "after_effect",
            "pivot_conditions": ["missing-output"],
            "stop_conditions": ["evidence-unsafe"],
            "basis_sequence": 2,
            "evidence_refs": ["crp://supervision/project-alpha/claim-alpha"],
        },
        occurred_at="2026-08-31T08:04:00Z",
        recorded_at="2026-08-31T08:04:00Z",
        actor="system",
    )
    repository.append(claim)
    claimed = ProjectDynamicsEngine().project(
        repository.list_project(PROJECT), project_id=PROJECT, now="2026-08-31T08:04:00Z",
    )
    assert claimed.supervision.active_claim is not None
    planning_supervision = claimed.planning_payload()["supervision"]
    assert planning_supervision["active_claim"]["claim_id"] == "claim-alpha"
    assert planning_supervision["active_claim"]["stop_conditions"] == ["evidence-unsafe"]
    assert planning_supervision["active_claim"]["evidence_refs"] == [
        "crp://supervision/project-alpha/claim-alpha"
    ]
    assert planning_supervision["verdict"] is None
    assert planning_supervision["disposition"] is None
    assert planning_supervision["checked_sequence"] is None
    assert planning_supervision["supervision_state"] == "unverified"

    repository.append(_draft(
        "event-supervision-verification",
        WorldEventKind.SUPERVISION_VERIFICATION_RECORDED,
        {
            "verification_id": "verification-alpha",
            "claim_id": "claim-alpha",
            "action_id": "action-alpha",
            "finding": "The independent verification observed the expected output.",
            "verdict": "supported",
            "checked_world_sequence": 3,
            "evidence_refs": ["crp://supervision/project-alpha/verification-alpha"],
        },
        occurred_at="2026-08-31T08:05:00Z",
        recorded_at="2026-08-31T08:05:00Z",
        actor="system",
    ))
    repository.append(_draft(
        "event-supervision-decision",
        WorldEventKind.SUPERVISION_DECISION_RECORDED,
        {
            "decision_id": "decision-alpha",
            "claim_id": "claim-alpha",
            "verification_id": "verification-alpha",
            "action_id": "action-alpha",
            "disposition": "continue",
            "rationale": "The evidence supports the active hypothesis.",
            "evidence_refs": ["crp://supervision/project-alpha/decision-alpha"],
        },
        occurred_at="2026-08-31T08:06:00Z",
        recorded_at="2026-08-31T08:06:00Z",
        actor="system",
    ))
    projection = ProjectDynamicsEngine().project(
        repository.list_project(PROJECT), project_id=PROJECT, now="2026-08-31T08:06:00Z",
    )

    assert projection.supervision.active_claim is not None
    assert projection.supervision.latest_verification is not None
    assert projection.supervision.latest_verification.checked_world_sequence == 3
    assert projection.supervision.latest_decision is not None
    assert projection.supervision.latest_decision.disposition == "continue"
    assert projection.supervision.supervision_state == "on_track"
    assert projection.to_payload()["supervision"]["latest_verification"] == {
        "verification_id": "verification-alpha", "claim_id": "claim-alpha",
        "action_id": "action-alpha",
        "finding": "The independent verification observed the expected output.",
        "verdict": "supported", "checked_world_sequence": 3,
        "evidence_refs": ["crp://supervision/project-alpha/verification-alpha"],
    }
    assert projection.to_payload()["supervision"]["latest_decision"]["evidence_refs"] == [
        "crp://supervision/project-alpha/decision-alpha"
    ]
    claim_status = projection.planning_payload()["supervision"]["claim_statuses"][0]
    assert claim_status["latest_verification"]["evidence_refs"] == [
        "crp://supervision/project-alpha/verification-alpha"
    ]
    assert claim_status["latest_decision"]["evidence_refs"] == [
        "crp://supervision/project-alpha/decision-alpha"
    ]


def test_supervision_fails_closed_on_missing_same_action_or_forward_causality(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    repository.append(_goal())
    repository.append(_action())
    repository.append(_draft(
        "event-supervision-unknown-action",
        WorldEventKind.SUPERVISION_CLAIM_DECLARED,
        {
            "claim_id": "claim-unknown", "action_id": "action-unknown",
            "supersedes_claim_id": None, "hypothesis": "The action needs verification.",
            "expected_signals": ["signal-expected"], "falsification_signals": ["signal-false"],
            "checkpoint_policy": "after_effect", "pivot_conditions": ["signal-false"],
            "stop_conditions": ["signal-stop"], "basis_sequence": 2,
            "evidence_refs": ["crp://supervision/project-alpha/claim-unknown"],
        }, occurred_at="2026-08-31T08:04:00Z", recorded_at="2026-08-31T08:04:00Z", actor="system",
    ))
    with pytest.raises(PersonalWorldModelError, match="unknown planned action"):
        ProjectDynamicsEngine().project(
            repository.list_project(PROJECT), project_id=PROJECT, now=T0,
        )


def test_supervision_continue_keeps_claim_active_and_supersede_replaces_it(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    repository.append(_goal())
    repository.append(_action())
    repository.append(_supervision_claim("event-claim-one", "claim-one", None, "2026-08-31T08:04:00Z"))
    repository.append(_supervision_verification(
        "event-verification-one", "verification-one", "claim-one", "supported", 3,
        "2026-08-31T08:05:00Z",
    ))
    repository.append(_supervision_decision(
        "event-decision-one", "decision-one", "claim-one", "verification-one", "continue",
        "2026-08-31T08:06:00Z",
    ))
    continued = ProjectDynamicsEngine().project(
        repository.list_project(PROJECT), project_id=PROJECT, now="2026-08-31T08:06:00Z",
    )
    assert continued.supervision.active_claim is not None
    assert continued.supervision.active_claim.claim_id == "claim-one"
    assert continued.supervision.supervision_state == "on_track"

    repository.append(_supervision_claim(
        "event-claim-two", "claim-two", "claim-one", "2026-08-31T08:07:00Z",
    ))
    superseded = ProjectDynamicsEngine().project(
        repository.list_project(PROJECT), project_id=PROJECT, now="2026-08-31T08:07:00Z",
    )
    assert superseded.supervision.active_claim is not None
    assert superseded.supervision.active_claim.claim_id == "claim-two"
    assert superseded.supervision.supervision_state == "unverified"

    repository.append(_supervision_verification(
        "event-verification-superseded", "verification-superseded", "claim-one", "supported", 4,
        "2026-08-31T08:08:00Z",
    ))
    with pytest.raises(PersonalWorldModelError, match="superseded claim"):
        ProjectDynamicsEngine().project(
            repository.list_project(PROJECT), project_id=PROJECT, now="2026-08-31T08:08:00Z",
        )

    latest = _repository(tmp_path / "latest-verification")
    latest.append(_goal())
    latest.append(_action())
    latest.append(_supervision_claim("event-claim-latest", "claim-latest", None, "2026-08-31T08:04:00Z"))
    latest.append(_supervision_verification(
        "event-verification-old", "verification-old", "claim-latest", "supported", 3,
        "2026-08-31T08:05:00Z",
    ))
    latest.append(_supervision_verification(
        "event-verification-new", "verification-new", "claim-latest", "supported", 3,
        "2026-08-31T08:06:00Z",
    ))
    latest.append(_supervision_decision(
        "event-decision-old", "decision-old", "claim-latest", "verification-old", "continue",
        "2026-08-31T08:07:00Z",
    ))
    with pytest.raises(PersonalWorldModelError, match="latest claim verification"):
        ProjectDynamicsEngine().project(
            latest.list_project(PROJECT), project_id=PROJECT, now="2026-08-31T08:07:00Z",
        )


def test_cross_action_supervision_supersede_requires_replan_and_invalidates_old_pending(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    repository.append(_goal())
    repository.append(_action())
    repository.append(_supervision_claim(
        "event-claim-old", "claim-old", None, "2026-08-31T08:04:00Z",
    ))
    repository.append(_supervision_verification(
        "event-verification-old", "verification-old", "claim-old", "refuted", 3,
        "2026-08-31T08:05:00Z",
    ))
    repository.append(_supervision_decision(
        "event-decision-old", "decision-old", "claim-old", "verification-old",
        "replan_required", "2026-08-31T08:06:00Z",
    ))
    repository.append(_draft(
        "event-action-pivot",
        WorldEventKind.ACTION_PLANNED,
        {
            "action_id": "action-pivot",
            "title": "Test the corrected direction",
            "expected_outcome": "The corrected direction produces evidence",
            "effect_class": "PURE",
            "gate_requirement": "none",
            "due_at": None,
            "evidence_refs": ["crp://plans/project-alpha/action-pivot"],
        },
        occurred_at="2026-08-31T08:07:00Z",
        recorded_at="2026-08-31T08:07:00Z",
    ))
    repository.append(_supervision_claim(
        "event-claim-pivot", "claim-pivot", "claim-old",
        "2026-08-31T08:08:00Z", action_id="action-pivot", basis_sequence=6,
    ))

    state = ProjectDynamicsEngine().project(
        repository.list_project(PROJECT),
        project_id=PROJECT,
        now="2026-08-31T08:08:00Z",
    )

    assert state.pending_action_ids == ("action-pivot",)
    assert state.supervision.active_claim is not None
    assert state.supervision.active_claim.claim_id == "claim-pivot"
    assert state.supervision.supervision_state == "unverified"


def test_cross_action_supervision_supersede_rejects_missing_replan_decision(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    repository.append(_goal())
    repository.append(_action())
    repository.append(_supervision_claim(
        "event-claim-old", "claim-old", None, "2026-08-31T08:04:00Z",
    ))
    repository.append(_draft(
        "event-action-pivot",
        WorldEventKind.ACTION_PLANNED,
        {
            "action_id": "action-pivot",
            "title": "Attempt an unsupported pivot",
            "expected_outcome": "This direction must remain blocked",
            "effect_class": "PURE",
            "gate_requirement": "none",
            "due_at": None,
            "evidence_refs": ["crp://plans/project-alpha/action-pivot"],
        },
        occurred_at="2026-08-31T08:05:00Z",
        recorded_at="2026-08-31T08:05:00Z",
    ))
    repository.append(_supervision_claim(
        "event-claim-pivot", "claim-pivot", "claim-old",
        "2026-08-31T08:06:00Z", action_id="action-pivot", basis_sequence=4,
    ))

    with pytest.raises(PersonalWorldModelError, match="earlier replan decision"):
        ProjectDynamicsEngine().project(
            repository.list_project(PROJECT),
            project_id=PROJECT,
            now="2026-08-31T08:06:00Z",
        )


def test_supervision_rejects_continue_after_refuted_verdict_and_projects_risks(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    repository.append(_goal())
    repository.append(_action())
    repository.append(_supervision_claim("event-claim-risk", "claim-risk", None, "2026-08-31T08:04:00Z"))
    repository.append(_supervision_verification(
        "event-verification-risk", "verification-risk", "claim-risk", "refuted", 3,
        "2026-08-31T08:05:00Z",
    ))
    repository.append(_supervision_decision(
        "event-decision-invalid", "decision-invalid", "claim-risk", "verification-risk", "continue",
        "2026-08-31T08:06:00Z",
    ))
    with pytest.raises(PersonalWorldModelError, match="cannot continue"):
        ProjectDynamicsEngine().project(
            repository.list_project(PROJECT), project_id=PROJECT, now="2026-08-31T08:06:00Z",
        )

    repository = _repository(tmp_path / "risk")
    repository.append(_goal())
    repository.append(_action())
    repository.append(_supervision_claim("event-claim-risk-ok", "claim-risk-ok", None, "2026-08-31T08:04:00Z"))
    repository.append(_supervision_verification(
        "event-verification-risk-ok", "verification-risk-ok", "claim-risk-ok", "refuted", 3,
        "2026-08-31T08:05:00Z",
    ))
    repository.append(_supervision_decision(
        "event-decision-risk-ok", "decision-risk-ok", "claim-risk-ok", "verification-risk-ok", "replan_required",
        "2026-08-31T08:06:00Z",
    ))
    projection = ProjectDynamicsEngine().project(
        repository.list_project(PROJECT), project_id=PROJECT, now="2026-08-31T08:06:00Z",
    )
    assert projection.supervision.supervision_state == "invalidated"
    assert {"supervision_refuted", "supervision_replan_required"} <= set(projection.risk_codes)


def test_supervision_keeps_active_claims_per_action_and_projects_status_matrix(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "claims-per-action")
    repository.append(_goal())
    repository.append(_action())
    repository.append(_supervision_claim("event-claim-alpha", "claim-alpha", None, "2026-08-31T08:04:00Z"))
    repository.append(_supervision_verification(
        "event-verification-alpha", "verification-alpha", "claim-alpha", "supported", 3,
        "2026-08-31T08:05:00Z",
    ))
    repository.append(_supervision_decision(
        "event-decision-alpha", "decision-alpha", "claim-alpha", "verification-alpha", "stop_required",
        "2026-08-31T08:06:00Z",
    ))
    repository.append(_draft(
        "event-action-beta",
        WorldEventKind.ACTION_PLANNED,
        {
            "action_id": "action-beta", "title": "Check a second project action",
            "expected_outcome": "The second action is independently observed",
            "effect_class": "QUERYABLE", "gate_requirement": "notice", "due_at": None,
            "evidence_refs": ["crp://plans/project-alpha/action-beta"],
        },
        occurred_at="2026-08-31T08:07:00Z", recorded_at="2026-08-31T08:07:00Z", actor="system",
    ))
    repository.append(_supervision_claim(
        "event-claim-beta", "claim-beta", None, "2026-08-31T08:08:00Z", action_id="action-beta", basis_sequence=6,
    ))
    per_action = ProjectDynamicsEngine().project(
        repository.list_project(PROJECT), project_id=PROJECT, now="2026-08-31T08:08:00Z",
    )
    assert [item.claim_id for item in per_action.supervision.active_claims] == ["claim-alpha", "claim-beta"]
    assert per_action.supervision.latest_active_claim is not None
    assert per_action.supervision.latest_active_claim.claim_id == "claim-beta"
    assert per_action.supervision.supervision_state == "stop_required"
    assert "supervision_stop_required" in per_action.risk_codes

    scenarios = (
        ("weakened", "replan_required", "at_risk", "supervision_replan_required"),
        ("inconclusive", "escalate_user", "at_risk", "supervision_inconclusive"),
        ("supported", "stop_required", "stop_required", "supervision_stop_required"),
    )
    for index, (verdict, disposition, state, risk) in enumerate(scenarios):
        checked = _repository(tmp_path / f"status-{index}")
        checked.append(_goal())
        checked.append(_action())
        checked.append(_supervision_claim("event-claim-status", "claim-status", None, "2026-08-31T08:04:00Z"))
        checked.append(_supervision_verification(
            "event-verification-status", "verification-status", "claim-status", verdict, 3,
            "2026-08-31T08:05:00Z",
        ))
        checked.append(_supervision_decision(
            "event-decision-status", "decision-status", "claim-status", "verification-status", disposition,
            "2026-08-31T08:06:00Z",
        ))
        projection = ProjectDynamicsEngine().project(
            checked.list_project(PROJECT), project_id=PROJECT, now="2026-08-31T08:06:00Z",
        )
        assert projection.supervision.supervision_state == state
        assert risk in projection.risk_codes

    invalid = _repository(tmp_path / "inconclusive-continue")
    invalid.append(_goal())
    invalid.append(_action())
    invalid.append(_supervision_claim("event-claim-invalid", "claim-invalid", None, "2026-08-31T08:04:00Z"))
    invalid.append(_supervision_verification(
        "event-verification-invalid", "verification-invalid", "claim-invalid", "inconclusive", 3,
        "2026-08-31T08:05:00Z",
    ))
    invalid.append(_supervision_decision(
        "event-decision-invalid-inconclusive", "decision-invalid-inconclusive", "claim-invalid", "verification-invalid", "continue",
        "2026-08-31T08:06:00Z",
    ))
    with pytest.raises(PersonalWorldModelError, match="cannot continue"):
        ProjectDynamicsEngine().project(
            invalid.list_project(PROJECT), project_id=PROJECT, now="2026-08-31T08:06:00Z",
        )

    repository = _repository(tmp_path / "causal")
    repository.append(_goal())
    repository.append(_action())
    repository.append(_draft(
        "event-supervision-claim-causal",
        WorldEventKind.SUPERVISION_CLAIM_DECLARED,
        {
            "claim_id": "claim-causal", "action_id": "action-alpha",
            "supersedes_claim_id": None, "hypothesis": "The action needs verification.",
            "expected_signals": ["signal-expected"], "falsification_signals": ["signal-false"],
            "checkpoint_policy": "after_effect", "pivot_conditions": ["signal-false"],
            "stop_conditions": ["signal-stop"], "basis_sequence": 2,
            "evidence_refs": ["crp://supervision/project-alpha/claim-causal"],
        }, occurred_at="2026-08-31T08:04:00Z", recorded_at="2026-08-31T08:04:00Z", actor="system",
    ))
    repository.append(_draft(
        "event-supervision-wrong-action",
        WorldEventKind.SUPERVISION_VERIFICATION_RECORDED,
        {
            "verification_id": "verification-wrong", "claim_id": "claim-causal",
            "action_id": "action-other", "finding": "The evidence was checked.",
            "verdict": "supported", "checked_world_sequence": 3,
            "evidence_refs": ["crp://supervision/project-alpha/verification-wrong"],
        }, occurred_at="2026-08-31T08:05:00Z", recorded_at="2026-08-31T08:05:00Z", actor="system",
    ))
    with pytest.raises(PersonalWorldModelError, match="same action"):
        ProjectDynamicsEngine().project(
            repository.list_project(PROJECT), project_id=PROJECT, now=T0,
        )

    repository = _repository(tmp_path / "before-claim")
    repository.append(_goal())
    repository.append(_action())
    repository.append(_supervision_claim(
        "event-supervision-claim-before", "claim-before", None, "2026-08-31T08:04:00Z",
    ))
    repository.append(_supervision_verification(
        "event-supervision-before", "verification-before", "claim-before", "supported", 2,
        "2026-08-31T08:05:00Z",
    ))
    with pytest.raises(PersonalWorldModelError, match="forward causality"):
        ProjectDynamicsEngine().project(
            repository.list_project(PROJECT), project_id=PROJECT, now=T0,
        )

    repository = _repository(tmp_path / "forward")
    repository.append(_goal())
    repository.append(_action())
    repository.append(_draft(
        "event-supervision-claim-forward",
        WorldEventKind.SUPERVISION_CLAIM_DECLARED,
        {
            "claim_id": "claim-forward", "action_id": "action-alpha",
            "supersedes_claim_id": None, "hypothesis": "The action needs verification.",
            "expected_signals": ["signal-expected"], "falsification_signals": ["signal-false"],
            "checkpoint_policy": "after_effect", "pivot_conditions": ["signal-false"],
            "stop_conditions": ["signal-stop"], "basis_sequence": 2,
            "evidence_refs": ["crp://supervision/project-alpha/claim-forward"],
        }, occurred_at="2026-08-31T08:04:00Z", recorded_at="2026-08-31T08:04:00Z", actor="system",
    ))
    repository.append(_draft(
            "event-supervision-forward",
            WorldEventKind.SUPERVISION_VERIFICATION_RECORDED,
            {
                "verification_id": "verification-forward", "claim_id": "claim-forward",
                "action_id": "action-alpha", "finding": "The evidence was checked.",
                "verdict": "supported", "checked_world_sequence": 99,
                "evidence_refs": ["crp://supervision/project-alpha/verification-forward"],
            }, occurred_at="2026-08-31T08:05:00Z", recorded_at="2026-08-31T08:05:00Z", actor="system",
    ))
    with pytest.raises(PersonalWorldModelError, match="forward causality"):
        ProjectDynamicsEngine().project(
            repository.list_project(PROJECT), project_id=PROJECT, now=T0,
        )


def test_projection_fails_closed_on_gap_scope_and_expected_outcome_drift() -> None:
    first = WorldEvent(
        event_id="event-one",
        project_id=PROJECT,
        sequence=1,
        kind=WorldEventKind.GOAL_DECLARED,
        actor="user",
        source_ref="crp://world-input/project-alpha/event-one",
        source_revision="revision-1",
        occurred_at=T0,
        recorded_at=T0,
        payload=_goal().payload,
    )
    gap = replace(first, event_id="event-three", sequence=3)
    with pytest.raises(PersonalWorldModelError, match="gap"):
        ProjectDynamicsEngine().project(
            (first, gap), project_id=PROJECT, now="2026-08-31T08:10:00Z"
        )
    with pytest.raises(PersonalWorldModelError, match="project scope"):
        ProjectDynamicsEngine().project(
            (first,), project_id="project-beta", now="2026-08-31T08:10:00Z"
        )

    action = WorldEvent(
        **{
            **_event_from_draft(_action(), 2),
        }
    )
    drifted_feedback = replace(
        _feedback(),
        payload={**dict(_feedback().payload), "expected_outcome": "另一个预期"},
    )
    feedback = WorldEvent(**_event_from_draft(drifted_feedback, 3))
    with pytest.raises(PersonalWorldModelError, match="expected outcome drifted"):
        ProjectDynamicsEngine().project(
            (first, action, feedback),
            project_id=PROJECT,
            now="2026-08-31T08:10:00Z",
        )


def test_world_event_rejects_secret_locator_and_unproven_feedback() -> None:
    with pytest.raises(PersonalWorldModelError, match="evidence reference"):
        replace(_goal(), source_ref="file:///private/project-goal.txt")
    with pytest.raises(PersonalWorldModelError, match="evidence reference"):
        replace(
            _goal(),
            payload={
                **dict(_goal().payload),
                "evidence_refs": ["C:\\private\\project-goal.txt"],
            },
        )
    with pytest.raises(PersonalWorldModelError, match="material"):
        replace(
            _goal(),
            payload={**dict(_goal().payload), "title": "api_key=secret-value-123456"},
        )
    with pytest.raises(PersonalWorldModelError, match="state deltas"):
        FeedbackFact(
            feedback_id="feedback-empty",
            supersedes_feedback_id=None,
            project_id=PROJECT,
            action_id="action-alpha",
            expected_outcome="动作有明确预期",
            actual_outcome="动作没有可验证变化",
            outcome=OutcomeStatus.UNKNOWN,
            state_delta=(),
            cost=FeedbackCost(elapsed_ms=0),
            user_evaluation=UserEvaluation(UserEvaluationVerdict.NOT_PROVIDED),
            evidence_refs=("crp://effects/effect-alpha/receipts/receipt-unknown",),
        )


def _supervision_claim(
    event_id: str, claim_id: str, supersedes_claim_id: str | None, recorded_at: str,
    *, action_id: str = "action-alpha", basis_sequence: int = 2,
) -> WorldEventDraft:
    return _draft(
        event_id,
        WorldEventKind.SUPERVISION_CLAIM_DECLARED,
        {
            "claim_id": claim_id,
            "supersedes_claim_id": supersedes_claim_id,
            "action_id": action_id,
            "hypothesis": "The action remains valid while expected evidence is observed.",
            "expected_signals": ["expected-evidence"],
            "falsification_signals": ["contrary-evidence"],
            "checkpoint_policy": "after_effect",
            "pivot_conditions": ["contrary-evidence"],
            "stop_conditions": ["safety-evidence"],
            "basis_sequence": basis_sequence,
            "evidence_refs": [f"crp://supervision/project-alpha/{claim_id}"],
        },
        occurred_at=recorded_at,
        recorded_at=recorded_at,
        actor="system",
    )


def _supervision_verification(
    event_id: str, verification_id: str, claim_id: str, verdict: str,
    checked_world_sequence: int, recorded_at: str,
) -> WorldEventDraft:
    return _draft(
        event_id,
        WorldEventKind.SUPERVISION_VERIFICATION_RECORDED,
        {
            "verification_id": verification_id,
            "claim_id": claim_id,
            "action_id": "action-alpha",
            "finding": "The available evidence was checked against the active hypothesis.",
            "verdict": verdict,
            "checked_world_sequence": checked_world_sequence,
            "evidence_refs": [f"crp://supervision/project-alpha/{verification_id}"],
        },
        occurred_at=recorded_at,
        recorded_at=recorded_at,
        actor="system",
    )


def _supervision_decision(
    event_id: str, decision_id: str, claim_id: str, verification_id: str,
    disposition: str, recorded_at: str,
) -> WorldEventDraft:
    return _draft(
        event_id,
        WorldEventKind.SUPERVISION_DECISION_RECORDED,
        {
            "decision_id": decision_id,
            "claim_id": claim_id,
            "verification_id": verification_id,
            "action_id": "action-alpha",
            "disposition": disposition,
            "rationale": "The decision follows the recorded verification verdict.",
            "evidence_refs": [f"crp://supervision/project-alpha/{decision_id}"],
        },
        occurred_at=recorded_at,
        recorded_at=recorded_at,
        actor="system",
    )


def _event_from_draft(draft: WorldEventDraft, sequence: int) -> dict[str, object]:
    return {
        "event_id": draft.event_id,
        "project_id": draft.project_id,
        "sequence": sequence,
        "kind": draft.kind,
        "actor": draft.actor,
        "source_ref": draft.source_ref,
        "source_revision": draft.source_revision,
        "occurred_at": draft.occurred_at,
        "recorded_at": draft.recorded_at,
        "payload": draft.payload,
    }
