from __future__ import annotations

from types import SimpleNamespace

from backend.api.world_supervision_agent_observer import (
    WorldSupervisionAgentObserver,
    parse_reviewer_verdict,
)


def test_reviewer_verdict_requires_the_fixed_single_line_contract() -> None:
    assert parse_reviewer_verdict(
        "VERDICT=supported;DISPOSITION=continue;FINDING=Evidence matches the planned outcome."
    ) == ("supported", "continue", "Evidence matches the planned outcome.")
    assert parse_reviewer_verdict(
        "VERDICT=refuted;DISPOSITION=continue;FINDING=Contradiction observed."
    ) is None
    assert parse_reviewer_verdict(
        "VERDICT=supported; DISPOSITION=continue; FINDING=spacing is not canonical"
    ) is None
    assert parse_reviewer_verdict("VERDICT=supported;DISPOSITION=continue;FINDING=two\nlines") is None
    assert parse_reviewer_verdict(
        "VERDICT=supported;DISPOSITION=continue;FINDING=See crp://session/main/secret"
    ) is None
    assert parse_reviewer_verdict(
        r"VERDICT=supported;DISPOSITION=continue;FINDING=Read C:\Users\Chrip\secret.txt"
    ) is None


class _Supervision:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.claim = SimpleNamespace(
            claim_id="claim-alpha", latest_verification=None, latest_decision=None,
        )

    def current_claim(self, project_id: str, action_id: str):
        assert (project_id, action_id) == ("project-alpha", "action-alpha")
        return self.claim

    def current_world_sequence(self, project_id: str) -> int:
        assert project_id == "project-alpha"
        return 3

    def record_verification(self, **kwargs):
        self.calls.append(("verification", kwargs))
        self.claim.latest_verification = SimpleNamespace(verification_id="verification-alpha")
        return SimpleNamespace(event=SimpleNamespace(payload={"verification_id": "verification-alpha"}))

    def record_decision(self, **kwargs):
        self.calls.append(("decision", kwargs))
        self.claim.latest_decision = SimpleNamespace(decision_id="decision-alpha")
        return SimpleNamespace()


class _Runs:
    def __init__(self, *, fan_in_status: str = "completed", reviewer_status: str = "completed") -> None:
        self.reviewer = SimpleNamespace(
            run_id="reviewer-run", turn_id="reviewer-turn", project_id="project-alpha",
            profile_id="subagent.reviewer", role="subagent", parent_run_id="main-run",
            is_terminal=True, status=reviewer_status,
            terminal_receipt_ref="crp://session/reviewer-turn/receipt/x",
        )
        self.main = SimpleNamespace(
            run_id="main-run", turn_id="main-turn", project_id="project-alpha",
            profile_id="main.orchestrator", role="main", parent_run_id=None,
        )
        self.fan_in = SimpleNamespace(
            fan_in_id="fan-in-alpha", project_id="project-alpha", parent_run_id="main-run",
            child_run_ids=("reviewer-run", "worker-run"), status="completed",
            operation_id="action-alpha",
        )
        self.result = SimpleNamespace(
            fan_in_id="fan-in-alpha", project_id="project-alpha", parent_run_id="main-run",
            status=fan_in_status, receipt_ref="crp://session/main-turn/fan-in-receipt/x",
            result_ref="crp://session/main-turn/fan-in-result/x",
            child_summaries=(SimpleNamespace(
                child_run_id="reviewer-run", project_id="project-alpha", status=reviewer_status,
                receipt_ref="crp://session/reviewer-turn/receipt/x",
                summary_ref="crp://session/main-turn/reviewer-summary/x",
            ),),
        )
        self.worker = SimpleNamespace(
            run_id="worker-run", turn_id="worker-turn", project_id="project-alpha",
            profile_id="subagent.worker", role="subagent", parent_run_id="main-run",
            is_terminal=True, status="completed",
            terminal_receipt_ref="crp://session/worker-turn/receipt/x",
        )

    def get_run_by_turn_id(self, turn_id, *, project_id):
        if project_id != "project-alpha":
            return None
        if turn_id == "reviewer-turn":
            return self.reviewer, 1
        if turn_id == "worker-turn":
            return self.worker, 1
        if turn_id == "main-turn":
            return self.main, 1
        return None

    def list_supervision_candidates(self, *, limit):
        assert limit == 1
        return (self.main,)

    def get_run_with_revision(self, run_id, *, project_id):
        return (self.main, 1) if run_id == "main-run" and project_id == "project-alpha" else None

    def list_fan_ins(self, *, project_id, parent_run_id):
        return (self.fan_in,) if (project_id, parent_run_id) == ("project-alpha", "main-run") else ()

    def get_fan_in_result(self, fan_in_id, *, project_id):
        return self.result if (fan_in_id, project_id) == ("fan-in-alpha", "project-alpha") else None

    def list_runs(self, *, project_id, parent_run_id):
        if (project_id, parent_run_id) == ("project-alpha", "main-run"):
            return (self.reviewer, self.worker)
        return ()


def _request(turn_id: str) -> dict[str, object]:
    return {
        "schema_version": "1.0.0", "turn_id": turn_id,
        "session_id": "world-project", "operation_id": "action-alpha",
        "idempotency_key": f"key-{turn_id}",
        "scope": {"kind": "project", "project_id": "project-alpha", "series_id": None},
        "input": {"kind": "text", "text": "review", "refs": []},
        "desired_outcome": "workbench.question.answer", "privacy": {"mode": "local_only", "allow_remote": False, "pii": "possible", "consent_refs": [], "retention": "local_durable"},
        "capability_policy": {"allowed": ["workbench.question.answer"], "denied": [], "require_approval": []},
        "context_policy": {"include_project_skill": True, "include_memory": True, "include_session_history": False, "max_context_bytes": 1024},
        "approval_policy": {"mode": "risk_based", "auto_approve_read_only": True},
        "created_at": "2026-09-02T08:00:00Z",
    }


def test_observer_uses_only_terminal_and_completed_fan_in_evidence() -> None:
    supervision, runs = _Supervision(), _Runs()
    observer = WorldSupervisionAgentObserver(
        supervision=supervision, run_store=runs,
        request_loader=_request,
        payload_loader=lambda ref: {
            "schema_version": "1.0.0", "kind": "agent.child-terminal-summary.v1",
            "child_run_id": "reviewer-run", "status": "completed",
            "profile_id": "subagent.reviewer",
            "final_summary": "VERDICT=supported;DISPOSITION=continue;FINDING=Evidence matches the planned outcome.",
            "error_code": None, "usage": {"model_calls": 0, "tool_calls": 0, "input_tokens": 0, "output_tokens": 0, "wall_time_ms": 0},
        },
        now=lambda: "2026-09-02T08:00:01Z",
    )

    assert observer.observe("reviewer-turn") == "recorded"
    assert [item[0] for item in supervision.calls] == ["verification", "decision"]
    evidence = supervision.calls[0][1]["evidence_refs"]
    assert evidence == [
        "crp://session/reviewer-turn/receipt/x",
        "crp://session/main-turn/fan-in-receipt/x",
        "crp://session/main-turn/fan-in-result/x",
    ]


def test_observer_falls_back_to_inconclusive_when_summary_is_not_canonical() -> None:
    supervision, runs = _Supervision(), _Runs()
    observer = WorldSupervisionAgentObserver(
        supervision=supervision, run_store=runs, request_loader=_request,
        payload_loader=lambda _ref: {"final_summary": "not a verdict"},
        now=lambda: "2026-09-02T08:00:01Z",
    )
    assert observer.observe("reviewer-turn") == "recorded"
    assert supervision.calls[0][1]["verdict"] == "inconclusive"
    assert supervision.calls[1][1]["disposition"] == "replan_required"


def test_replayed_observer_does_not_write_a_second_supervision_pair() -> None:
    supervision, runs = _Supervision(), _Runs()
    observer = WorldSupervisionAgentObserver(
        supervision=supervision, run_store=runs, request_loader=_request,
        payload_loader=lambda _ref: {
            "kind": "agent.child-terminal-summary.v1", "child_run_id": "reviewer-run",
            "profile_id": "subagent.reviewer", "status": "completed",
            "final_summary": "VERDICT=supported;DISPOSITION=continue;FINDING=Verified.",
        }, now=lambda: "2026-09-02T08:00:01Z",
    )
    assert observer.observe("reviewer-turn") == "recorded"
    assert observer.observe("worker-turn") == "noop"
    assert [item[0] for item in supervision.calls] == ["verification", "decision"]


def test_sensitive_but_grammatical_finding_uses_safe_inconclusive_fallback() -> None:
    for sensitive_finding in ("crp://session/main/secret", r"C:\Users\Chrip\secret.txt"):
        supervision, runs = _Supervision(), _Runs()
        observer = WorldSupervisionAgentObserver(
            supervision=supervision, run_store=runs, request_loader=_request,
            payload_loader=lambda _ref, finding=sensitive_finding: {
                "kind": "agent.child-terminal-summary.v1", "child_run_id": "reviewer-run",
                "profile_id": "subagent.reviewer", "status": "completed",
                "final_summary": (
                    "VERDICT=supported;DISPOSITION=continue;FINDING=" + finding
                ),
            }, now=lambda: "2026-09-02T08:00:01Z",
        )
        assert observer.observe("reviewer-turn") == "recorded"
        verification = supervision.calls[0][1]
        decision = supervision.calls[1][1]
        assert verification["verdict"] == "inconclusive"
        assert decision["disposition"] == "replan_required"
        assert sensitive_finding not in verification["finding"]
        assert sensitive_finding not in decision["rationale"]


def test_worker_terminal_observes_reviewer_fan_in_after_reviewer_callback_was_early() -> None:
    supervision, runs = _Supervision(), _Runs()
    runs.result = None
    observer = WorldSupervisionAgentObserver(
        supervision=supervision, run_store=runs, request_loader=_request,
        payload_loader=lambda _ref: {"final_summary": "not read"},
        now=lambda: "2026-09-02T08:00:01Z",
    )
    assert observer.observe("reviewer-turn") == "ignored"
    runs.result = _Runs().result
    assert observer.observe("worker-turn") == "recorded"
    assert [item[0] for item in supervision.calls] == ["verification", "decision"]


def test_failed_reviewer_records_inconclusive_without_loading_its_summary() -> None:
    supervision, runs = _Supervision(), _Runs(fan_in_status="failed", reviewer_status="failed")
    observer = WorldSupervisionAgentObserver(
        supervision=supervision, run_store=runs, request_loader=_request,
        payload_loader=lambda _ref: (_ for _ in ()).throw(AssertionError("failed summary must not be read")),
        now=lambda: "2026-09-02T08:00:01Z",
    )
    assert observer.observe("worker-turn") == "recorded"
    assert supervision.calls[0][1]["verdict"] == "inconclusive"
    assert supervision.calls[1][1]["disposition"] == "replan_required"


def test_recovery_replays_completed_fan_in_once_and_filters_non_world_session() -> None:
    supervision, runs = _Supervision(), _Runs()
    observer = WorldSupervisionAgentObserver(
        supervision=supervision, run_store=runs,
        request_loader=_request,
        payload_loader=lambda _ref: {
            "kind": "agent.child-terminal-summary.v1", "child_run_id": "reviewer-run",
            "profile_id": "subagent.reviewer", "status": "completed",
            "final_summary": "VERDICT=supported;DISPOSITION=continue;FINDING=Verified.",
        }, now=lambda: "2026-09-02T08:00:01Z",
    )
    assert observer.recover(limit=1) == {
        "status": "completed", "scanned": 1, "recorded": 1,
        "noop": 0, "ignored": 0,
    }
    assert observer.recover(limit=1)["noop"] == 1
    assert [item[0] for item in supervision.calls] == ["verification", "decision"]

    non_world = WorldSupervisionAgentObserver(
        supervision=_Supervision(), run_store=runs,
        request_loader=lambda turn_id: {
            **_request(turn_id), "session_id": "ordinary-project",
        }, payload_loader=lambda _ref: {}, now=lambda: "2026-09-02T08:00:01Z",
    )
    assert non_world.recover(limit=1)["ignored"] == 1
