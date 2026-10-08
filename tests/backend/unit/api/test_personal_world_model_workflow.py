from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.api.ai_turn_runner import AITurnRunnerCapacityError
from backend.api.personal_world_model_runtime import PersonalWorldModelRuntime
from backend.api.project_provenance_runtime import ProjectProvenanceRuntime
from backend.api.personal_world_model_workflow import (
    PersonalWorldModelWorkflow,
    PersonalWorldModelWorkflowError,
)
from backend.api.world_supervision_runtime import WorldSupervisionRuntime
from backend.api.world_supervision_agent_observer import WorldSupervisionAgentObserver
from core.ai_kernel import TurnReceipt
from core.ai_kernel.tool_invocation import ToolInvocationOutcome, outcome_to_payload
from core.personal_world_model import PersonalWorldModelError


PROJECT = "project-workflow"
COMMAND = "12345678-1234-1234-1234-123456789abc"
TURN = f"world-turn-{COMMAND}"
ACTION = f"world-action-{COMMAND}"
OUTCOME_REF = f"crp://session/{TURN}/tool-invocation-outcome/outcome-workflow"


class _Turns:
    def __init__(self) -> None:
        self.request = None
        self.outcome = outcome_to_payload(
            ToolInvocationOutcome(
                invocation_id="tool-call-workflow",
                turn_id=TURN,
                capability_id="workbench.question.answer",
                attempt=1,
                status="completed",
                effect_certainty="confirmed_applied",
                payload_ref=None,
                receipt_ref=None,
                evidence_refs=(),
                error_code=None,
                retryable=False,
            )
        )

    def accept_and_submit(self, request):
        self.request = dict(request)
        return self.receipt_for(str(request["turn_id"]))

    def get_request(self, turn_id: str):
        return self.request if self.request and self.request["turn_id"] == turn_id else None

    def receipt_for(self, turn_id: str, *, replayed: bool = False):
        if turn_id != TURN:
            raise KeyError(turn_id)
        return TurnReceipt(TURN, "world-project", ACTION, "completed", 5, replayed)

    def events_after(self, turn_id: str, after_sequence: int = 0):
        if turn_id != TURN:
            return ()
        events = (
            _event(1, "turn.accepted", "2026-09-01T12:00:00Z"),
            _event(
                2,
                "tool.intent.recorded",
                "2026-09-01T12:00:00Z",
                data={"capability_id": "workbench.question.answer"},
                correlation={"tool_call_id": "tool-call-workflow"},
            ),
            _event(
                3,
                "tool.outcome.recorded",
                "2026-09-01T12:00:01Z",
                data={
                    "payload_ref": OUTCOME_REF,
                    "capability_id": "workbench.question.answer",
                },
                correlation={"tool_call_id": "tool-call-workflow"},
            ),
            _event(
                4,
                "tool.completed",
                "2026-09-01T12:00:01Z",
                data={"capability_id": "workbench.question.answer"},
                correlation={"tool_call_id": "tool-call-workflow"},
            ),
            _event(5, "turn.completed", "2026-09-01T12:00:02Z"),
        )
        return tuple(item for item in events if item["sequence"] > after_sequence)

    def presentation_for(self, turn_id: str):
        if turn_id != TURN:
            return None
        return {"answer": {"text": "Use the verified project evidence."}}

    def get(self, payload_ref: str):
        if payload_ref != OUTCOME_REF:
            raise KeyError(payload_ref)
        return dict(self.outcome)


class _FailOnceRunner:
    def __init__(self, turns: _Turns) -> None:
        self._turns = turns
        self.attempts = 0

    def accept_and_submit(self, request):
        self.attempts += 1
        if self.attempts == 1:
            raise AITurnRunnerCapacityError("capacity unavailable")
        return self._turns.accept_and_submit(request)


class _FailOnSecondRunner:
    def __init__(self, turns: _Turns) -> None:
        self._turns = turns
        self.attempts = 0

    def accept_and_submit(self, request):
        self.attempts += 1
        if self.attempts == 2:
            raise AITurnRunnerCapacityError("capacity unavailable")
        return self._turns.accept_and_submit(request)


class _RunningTurns(_Turns):
    def receipt_for(self, turn_id: str, *, replayed: bool = False):
        if turn_id != TURN:
            raise KeyError(turn_id)
        return TurnReceipt(TURN, "world-project", ACTION, "running", 3, replayed)

    def events_after(self, turn_id: str, after_sequence: int = 0):
        if turn_id != TURN:
            return ()
        events = (
            _event(1, "turn.accepted", "2026-09-01T12:00:00Z"),
            _event(
                2,
                "tool.intent.recorded",
                "2026-09-01T12:00:00Z",
                data={"capability_id": "workbench.question.answer"},
                correlation={"tool_call_id": "tool-call-workflow"},
            ),
            _event(
                3,
                "tool.outcome.recorded",
                "2026-09-01T12:00:01Z",
                data={
                    "payload_ref": OUTCOME_REF,
                    "capability_id": "workbench.question.answer",
                },
                correlation={"tool_call_id": "tool-call-workflow"},
            ),
        )
        return tuple(item for item in events if item["sequence"] > after_sequence)


class _MultiTurns(_Turns):
    def __init__(self) -> None:
        super().__init__()
        self.requests: dict[str, dict[str, object]] = {}

    def accept_and_submit(self, request):
        stored = dict(request)
        self.requests[str(stored["turn_id"])] = stored
        self.request = stored
        return self.receipt_for(str(stored["turn_id"]))

    def get_request(self, turn_id: str):
        return self.requests.get(turn_id)

    def receipt_for(self, turn_id: str, *, replayed: bool = False):
        request = self.requests.get(turn_id)
        if request is None:
            raise KeyError(turn_id)
        return TurnReceipt(
            turn_id,
            "world-project",
            str(request["operation_id"]),
            "completed",
            5,
            replayed,
        )


def _event(sequence, event_type, occurred_at, *, data=None, correlation=None):
    return {
        "sequence": sequence,
        "type": event_type,
        "data": data or {},
        "correlation": correlation or {},
        "occurred_at": occurred_at,
    }


def _workflow(
    root: Path,
    turns: _Turns,
    *,
    runner=None,
    organization_submitter=None,
    now: datetime | None = None,
) -> PersonalWorldModelWorkflow:
    current_time = now or datetime(2026, 9, 1, 12, 0, 5, tzinfo=timezone.utc)
    return PersonalWorldModelWorkflow(
        world=PersonalWorldModelRuntime.for_root(
            root,
            receipts=turns,
            turn_store=turns,
            now=lambda: current_time,
        ),
        turns=turns,
        turn_store=turns,
        runner=turns if runner is None else runner,
        organization_submitter=organization_submitter,
        now=lambda: current_time,
    )


def _seed_goal(workflow: PersonalWorldModelWorkflow) -> None:
    result = workflow.declare_goal(
        project_id=PROJECT,
        command_id="goal-12345678",
        title="Close one governed project loop",
        success_criteria=["The next action consumes verified feedback"],
    )
    assert result.state["phase"] == "ready"


def _submit_action(workflow: PersonalWorldModelWorkflow):
    return workflow.submit_action(
        project_id=PROJECT,
        command_id=COMMAND,
        title="Inspect the next project step",
        expected_outcome="A grounded next step is available for evaluation",
        question="What should the project do next?",
    )


def _feedback(workflow: PersonalWorldModelWorkflow):
    return workflow.record_feedback(
        project_id=PROJECT,
        turn_id=TURN,
        command_id="feedback-12345678",
        actual_outcome="The grounded next step was useful and accepted",
        outcome="achieved",
        state_delta=[{"field": "action.status", "before": "pending", "after": "accepted"}],
        user_evaluation={"verdict": "accepted", "rating": 5, "note": "Useful"},
    )


def test_workflow_generates_internal_bindings_and_replays_feedback(tmp_path: Path) -> None:
    turns = _Turns()
    workflow = _workflow(tmp_path, turns)
    _seed_goal(workflow)

    submitted = _submit_action(workflow)
    status = workflow.action_status(project_id=PROJECT, turn_id=TURN)
    feedback = _feedback(workflow)
    replay = _feedback(workflow)

    assert submitted.action_id == ACTION and submitted.turn_id == TURN
    assert submitted.state["supervision"]["active_claim"]["action_id"] == ACTION
    assert submitted.state["supervision"]["active_claim"]["basis_sequence"] == 2
    assert turns.request["operation_id"] == ACTION
    assert turns.request["session_id"] == "world-project"
    assert turns.request["privacy"]["mode"] == "local_only"
    assert status["governance"] == {
        "gate": "passed",
        "effect": "settled",
        "handler": "completed",
        "receipt": "terminal",
        "feedback": "not_recorded",
    }
    assert status["ready_for_feedback"] is True
    assert feedback["governance"]["receipt"] == "verified"
    assert feedback["state"]["phase"] == "in_progress"
    assert feedback["state"]["pending_action_ids"] == []
    assert feedback["state"]["latest_feedback_id"] == "world-feedback-feedback-12345678"
    assert replay["replayed"] is True
    provenance = ProjectProvenanceRuntime(
        world=PersonalWorldModelRuntime.for_root(tmp_path)
    ).project(PROJECT)
    assert {(item.subject.kind, item.subject.subject_id) for item in provenance.subjects} == {
        ("world_action", ACTION),
        ("hypothesis", f"world-supervision-claim-{ACTION}"),
    }
    assert [(item.relation, item.source.kind, item.target.kind) for item in provenance.links] == [
        ("depends_on", "world_action", "hypothesis")
    ]
    assert [(item.verdict, item.subject.subject_id) for item in provenance.validations] == [
        ("verified", ACTION)
    ]


@pytest.mark.parametrize(
    ("outcome", "expected_verdict"),
    (("achieved", "verified"), ("failed", "rejected"), ("partial", "inconclusive"), ("unknown", "inconclusive")),
)
def test_feedback_provenance_maps_outcomes_to_a_single_validation_verdict(
    tmp_path: Path, outcome: str, expected_verdict: str,
) -> None:
    turns = _Turns()
    workflow = _workflow(tmp_path, turns)
    _seed_goal(workflow)
    _submit_action(workflow)
    workflow.record_feedback(
        project_id=PROJECT,
        turn_id=TURN,
        command_id=f"feedback-{outcome}",
        actual_outcome="The verified receipt was classified for the project.",
        outcome=outcome,
        state_delta=[{"field": "action.status", "before": "pending", "after": outcome}],
        user_evaluation={"verdict": "accepted", "rating": 4, "note": "Observed"},
    )

    trace = ProjectProvenanceRuntime(
        world=PersonalWorldModelRuntime.for_root(tmp_path)
    ).project(PROJECT)
    assert [item.verdict for item in trace.validations] == [expected_verdict]


def test_feedback_provenance_replays_after_restart_with_a_later_clock(
    tmp_path: Path,
) -> None:
    turns = _Turns()
    workflow = _workflow(tmp_path, turns)
    _seed_goal(workflow)
    _submit_action(workflow)
    first = _feedback(workflow)

    restarted = _workflow(
        tmp_path,
        turns,
        now=datetime(2026, 9, 2, 12, 0, 5, tzinfo=timezone.utc),
    )
    replay = _feedback(restarted)
    trace = ProjectProvenanceRuntime(
        world=PersonalWorldModelRuntime.for_root(tmp_path)
    ).project(PROJECT)

    assert first["replayed"] is False
    assert replay["replayed"] is True
    assert len(trace.validations) == 1


def test_workflow_blocks_new_actions_for_freshness_but_allows_exact_replay(tmp_path: Path) -> None:
    turns = _Turns()
    workflow = _workflow(tmp_path, turns)
    _seed_goal(workflow)
    _submit_action(workflow)
    _feedback(workflow)

    world = PersonalWorldModelRuntime.for_root(tmp_path)
    supervision = WorldSupervisionRuntime(world=world)
    verification = supervision.record_verification(
        project_id=PROJECT,
        action_id=ACTION,
        verdict="weakened",
        finding="The expected outcome needs a revised plan.",
        checked_world_sequence=4,
        evidence_refs=["crp://receipts/project-workflow/reviewer-terminal"],
        recorded_at="2026-09-01T12:00:05Z",
    )
    supervision.record_decision(
        project_id=PROJECT,
        action_id=ACTION,
        verification_id=str(verification.event.payload["verification_id"]),
        disposition="replan_required",
        rationale="The verified outcome requires a revised plan.",
        evidence_refs=["crp://receipts/project-workflow/reviewer-terminal"],
        recorded_at="2026-09-01T12:00:05Z",
    )

    with pytest.raises(PersonalWorldModelWorkflowError, match="requires replanning"):
        workflow.submit_action(
            project_id=PROJECT,
            command_id="second-12345678",
            title="Run a newly proposed step",
            expected_outcome="A revised result is available",
            question="What should happen after replanning?",
        )

    replay = _submit_action(workflow)
    assert replay.replayed is True


def test_workflow_pivot_supersedes_replan_and_releases_old_pending_action(
    tmp_path: Path,
) -> None:
    turns = _MultiTurns()
    workflow = _workflow(tmp_path, turns)
    _seed_goal(workflow)
    _submit_action(workflow)
    supervision = WorldSupervisionRuntime(
        world=PersonalWorldModelRuntime.for_root(tmp_path)
    )
    verification = supervision.record_verification(
        project_id=PROJECT,
        action_id=ACTION,
        verdict="refuted",
        finding="The original direction no longer matches observed evidence.",
        checked_world_sequence=3,
        evidence_refs=["crp://receipts/project-workflow/reviewer-terminal"],
        recorded_at="2026-09-01T12:00:05Z",
    )
    supervision.record_decision(
        project_id=PROJECT,
        action_id=ACTION,
        verification_id=str(verification.event.payload["verification_id"]),
        disposition="replan_required",
        rationale="Replace the disproven direction with a bounded alternative.",
        evidence_refs=["crp://receipts/project-workflow/reviewer-terminal"],
        recorded_at="2026-09-01T12:00:05Z",
    )

    pivot = workflow.pivot_action(
        project_id=PROJECT,
        command_id="pivot-12345678",
        supersedes_action_id=ACTION,
        title="Test the corrected direction",
        expected_outcome="The alternative produces current evidence",
        question="What is the next step under the corrected direction?",
    )
    pivot_action = "world-action-pivot-12345678"

    assert pivot.action_id == pivot_action
    assert pivot.state["pending_action_ids"] == [pivot_action]
    assert pivot.state["supervision"]["active_claim"]["action_id"] == pivot_action
    assert pivot.state["supervision"]["active_claim"]["supersedes_claim_id"] == (
        f"world-supervision-claim-{ACTION}"
    )
    assert supervision.freshness_allows(PROJECT) is True
    replay = workflow.pivot_action(
        project_id=PROJECT,
        command_id="pivot-12345678",
        supersedes_action_id=ACTION,
        title="Test the corrected direction",
        expected_outcome="The alternative produces current evidence",
        question="What is the next step under the corrected direction?",
    )
    assert replay.replayed is True
    assert len(turns.requests) == 2


def test_workflow_pivot_cannot_bypass_stop_or_escalation(tmp_path: Path) -> None:
    for disposition in ("stop_required", "escalate_user"):
        root = tmp_path / disposition
        turns = _MultiTurns()
        workflow = _workflow(root, turns)
        _seed_goal(workflow)
        _submit_action(workflow)
        supervision = WorldSupervisionRuntime(
            world=PersonalWorldModelRuntime.for_root(root)
        )
        verification = supervision.record_verification(
            project_id=PROJECT,
            action_id=ACTION,
            verdict="refuted" if disposition == "stop_required" else "weakened",
            finding="The reviewer requires a protected decision.",
            checked_world_sequence=3,
            evidence_refs=["crp://receipts/project-workflow/reviewer-terminal"],
            recorded_at="2026-09-01T12:00:05Z",
        )
        supervision.record_decision(
            project_id=PROJECT,
            action_id=ACTION,
            verification_id=str(verification.event.payload["verification_id"]),
            disposition=disposition,
            rationale="The current authority cannot select a new direction.",
            evidence_refs=["crp://receipts/project-workflow/reviewer-terminal"],
            recorded_at="2026-09-01T12:00:05Z",
        )

        with pytest.raises(PersonalWorldModelWorkflowError):
            workflow.pivot_action(
                project_id=PROJECT,
                command_id=f"pivot-{disposition}",
                supersedes_action_id=ACTION,
                title="Attempt a protected pivot",
                expected_outcome="This action must not be admitted",
                question="Can the protected state be bypassed?",
            )
        assert len(PersonalWorldModelRuntime.for_root(root).events(PROJECT)) == 8
        assert len(turns.requests) == 1


def test_workflow_pivot_resumes_after_claim_persisted_before_turn_admission(
    tmp_path: Path,
) -> None:
    turns = _MultiTurns()
    runner = _FailOnSecondRunner(turns)
    workflow = _workflow(tmp_path, turns, runner=runner)
    _seed_goal(workflow)
    _submit_action(workflow)
    supervision = WorldSupervisionRuntime(
        world=PersonalWorldModelRuntime.for_root(tmp_path)
    )
    verification = supervision.record_verification(
        project_id=PROJECT,
        action_id=ACTION,
        verdict="refuted",
        finding="The original direction no longer matches observed evidence.",
        checked_world_sequence=3,
        evidence_refs=["crp://receipts/project-workflow/reviewer-terminal"],
        recorded_at="2026-09-01T12:00:05Z",
    )
    supervision.record_decision(
        project_id=PROJECT,
        action_id=ACTION,
        verification_id=str(verification.event.payload["verification_id"]),
        disposition="replan_required",
        rationale="Use a corrected direction.",
        evidence_refs=["crp://receipts/project-workflow/reviewer-terminal"],
        recorded_at="2026-09-01T12:00:05Z",
    )
    payload = {
        "project_id": PROJECT,
        "command_id": "pivot-retry-12345678",
        "supersedes_action_id": ACTION,
        "title": "Retry the corrected direction",
        "expected_outcome": "The replacement is admitted once",
        "question": "Proceed after the durable pivot checkpoint?",
    }

    with pytest.raises(AITurnRunnerCapacityError, match="capacity unavailable"):
        workflow.pivot_action(**payload)
    events_after_failure = PersonalWorldModelRuntime.for_root(tmp_path).events(PROJECT)
    assert len(events_after_failure) == 13
    assert events_after_failure[-1].kind.value == "provenance.link.recorded"

    retried = workflow.pivot_action(**payload)
    assert retried.replayed is True
    assert runner.attempts == 3
    assert len(PersonalWorldModelRuntime.for_root(tmp_path).events(PROJECT)) == 13


def test_recovered_reviewer_correction_drives_a_restart_safe_pivot(
    tmp_path: Path,
) -> None:
    turns = _MultiTurns()
    workflow = _workflow(tmp_path, turns)
    _seed_goal(workflow)
    _submit_action(workflow)
    main = SimpleNamespace(
        run_id="main-run", turn_id=TURN, project_id=PROJECT,
        profile_id="main.orchestrator", role="main", parent_run_id=None,
    )
    reviewer = SimpleNamespace(
        run_id="reviewer-run", turn_id="reviewer-turn", project_id=PROJECT,
        profile_id="subagent.reviewer", role="subagent", parent_run_id="main-run",
        is_terminal=True, status="completed",
        terminal_receipt_ref="crp://session/reviewer-turn/receipt/reviewer",
    )
    fan_in = SimpleNamespace(
        fan_in_id="fan-in-review", project_id=PROJECT, parent_run_id="main-run",
        child_run_ids=("reviewer-run",), status="completed",
    )
    result = SimpleNamespace(
        fan_in_id="fan-in-review", project_id=PROJECT, parent_run_id="main-run",
        status="completed", receipt_ref=f"crp://session/{TURN}/fan-in-receipt/review",
        result_ref=f"crp://session/{TURN}/fan-in-result/review",
        child_summaries=(SimpleNamespace(
            child_run_id="reviewer-run", project_id=PROJECT, status="completed",
            receipt_ref=reviewer.terminal_receipt_ref,
            summary_ref=f"crp://session/{TURN}/reviewer-summary/review",
        ),),
    )

    class _RecoveryRuns:
        def list_supervision_candidates(self, *, limit):
            assert limit == 4
            return (main,)

        def get_run_by_turn_id(self, turn_id, *, project_id):
            return (main, 1) if (turn_id, project_id) == (TURN, PROJECT) else None

        def list_runs(self, *, project_id, parent_run_id):
            return (reviewer,) if (project_id, parent_run_id) == (PROJECT, "main-run") else ()

        def list_fan_ins(self, *, project_id, parent_run_id):
            return (fan_in,) if (project_id, parent_run_id) == (PROJECT, "main-run") else ()

        def get_fan_in_result(self, fan_in_id, *, project_id):
            return result if (fan_in_id, project_id) == ("fan-in-review", PROJECT) else None

    observer = WorldSupervisionAgentObserver(
        supervision=WorldSupervisionRuntime(
            world=PersonalWorldModelRuntime.for_root(tmp_path)
        ),
        run_store=_RecoveryRuns(),
        request_loader=turns.get_request,
        payload_loader=lambda _ref: {
            "kind": "agent.child-terminal-summary.v1",
            "child_run_id": "reviewer-run",
            "profile_id": "subagent.reviewer",
            "status": "completed",
            "final_summary": (
                "VERDICT=refuted;DISPOSITION=replan_required;"
                "FINDING=New evidence disproved the original direction."
            ),
        },
        now=lambda: "2026-09-01T12:00:06Z",
    )

    assert observer.recover(limit=4)["recorded"] == 1
    assert observer.recover(limit=4)["noop"] == 1
    corrected = PersonalWorldModelRuntime.for_root(tmp_path).project(PROJECT)
    assert corrected.supervision.latest_decision.disposition == "replan_required"
    with pytest.raises(PersonalWorldModelWorkflowError):
        workflow.submit_action(
            project_id=PROJECT,
            command_id="ordinary-after-refute",
            title="Continue the disproven direction",
            expected_outcome="This action must remain blocked",
            question="Continue anyway?",
        )

    pivot_payload = {
        "project_id": PROJECT,
        "command_id": "pivot-after-recovery",
        "supersedes_action_id": ACTION,
        "title": "Adopt the evidence-backed direction",
        "expected_outcome": "The new direction is independently verified",
        "question": "Proceed from the corrected evidence?",
    }
    pivoted = workflow.pivot_action(**pivot_payload)
    restarted = _workflow(tmp_path, turns)
    replayed = restarted.pivot_action(**pivot_payload)

    assert pivoted.state["pending_action_ids"] == [pivoted.action_id]
    assert pivoted.state["supervision"]["state"] == "unverified"
    assert replayed.replayed is True
    assert len(PersonalWorldModelRuntime.for_root(tmp_path).events(PROJECT)) == 13


def test_workflow_restores_from_events_and_blocks_payload_or_scope_drift(tmp_path: Path) -> None:
    turns = _Turns()
    workflow = _workflow(tmp_path, turns)
    _seed_goal(workflow)
    _submit_action(workflow)

    with pytest.raises(PersonalWorldModelWorkflowError, match="feedback first"):
        workflow.submit_action(
            project_id=PROJECT,
            command_id="second-12345678",
            title="Run another step",
            expected_outcome="Another result",
            question="Continue",
        )
    with pytest.raises(PersonalWorldModelError, match="identity conflicts"):
        workflow.submit_action(
            project_id=PROJECT,
            command_id=COMMAND,
            title="Changed title",
            expected_outcome="A grounded next step is available for evaluation",
            question="What should the project do next?",
        )
    with pytest.raises(PersonalWorldModelWorkflowError, match="scope drifted"):
        workflow.action_status(project_id="project-other", turn_id=TURN)

    _feedback(workflow)
    restarted = _workflow(tmp_path, turns)
    overview = restarted.overview(project_id=PROJECT)
    assert overview["state"]["latest_feedback_id"] == "world-feedback-feedback-12345678"
    assert overview["recent_actions"][0]["feedback_id"] == "world-feedback-feedback-12345678"


def test_workflow_keeps_the_plan_durable_when_turn_admission_needs_retry(tmp_path: Path) -> None:
    turns = _Turns()
    runner = _FailOnceRunner(turns)
    workflow = _workflow(tmp_path, turns, runner=runner)
    _seed_goal(workflow)

    with pytest.raises(AITurnRunnerCapacityError, match="capacity unavailable"):
        _submit_action(workflow)

    pending = workflow.overview(project_id=PROJECT)
    assert pending["state"]["pending_action_ids"] == [ACTION]
    assert pending["recent_actions"][0]["status"] == "not_admitted"
    failed_trace = ProjectProvenanceRuntime(
        world=PersonalWorldModelRuntime.for_root(tmp_path)
    ).project(PROJECT)
    assert (len(failed_trace.subjects), len(failed_trace.links), len(failed_trace.validations)) == (2, 1, 0)

    retried = _submit_action(workflow)
    assert retried.replayed is True
    assert retried.status == "completed"
    assert runner.attempts == 2
    repaired_trace = ProjectProvenanceRuntime(
        world=PersonalWorldModelRuntime.for_root(tmp_path)
    ).project(PROJECT)
    assert (len(repaired_trace.subjects), len(repaired_trace.links), len(repaired_trace.validations)) == (2, 1, 0)


def test_workflow_rejects_feedback_until_the_turn_receipt_is_terminal(tmp_path: Path) -> None:
    turns = _RunningTurns()
    workflow = _workflow(tmp_path, turns)
    _seed_goal(workflow)

    submitted = _submit_action(workflow)
    assert submitted.status == "running"
    with pytest.raises(PersonalWorldModelWorkflowError, match="terminal Turn time"):
        _feedback(workflow)

    state = workflow.overview(project_id=PROJECT)["state"]
    assert state["pending_action_ids"] == [ACTION]
    assert state["latest_feedback_id"] is None


def test_workflow_prefers_an_injected_organization_submitter(tmp_path: Path) -> None:
    turns = _Turns()

    class _OrganizationSubmitter:
        def __init__(self) -> None:
            self.requests = []

        def accept_and_submit(self, request):
            self.requests.append(dict(request))
            return turns.receipt_for(str(request["turn_id"]))

    organization = _OrganizationSubmitter()
    workflow = _workflow(tmp_path, turns, organization_submitter=organization)
    _seed_goal(workflow)

    submitted = _submit_action(workflow)

    assert submitted.status == "completed"
    assert len(organization.requests) == 1
    assert turns.request is None
