from __future__ import annotations

import pytest
from types import SimpleNamespace

from backend.api.project_task_case_projection import build_project_task_case_projection


PROJECT = "task-case-project"
TURN = "world-turn-current"
ACTION = "world-action-current"


class _Workflow:
    def __init__(self, *, pending: bool = True, recent: bool = True, feedback: bool = False, action_status: str = "running") -> None:
        self.pending = pending
        self.recent = recent
        self.feedback = feedback
        self.action_status = action_status

    def overview(self, *, project_id: str):
        assert project_id == PROJECT
        return {
            "state": {
                "project_id": PROJECT,
                "derived_at": "2026-09-02T00:00:00Z",
                "phase": "in_progress",
                "goal": {"goal_id": "goal-1", "title": "Organize the project", "success_criteria": ["One task case is visible"], "evidence_refs": ["crp://private"]},
                "tasks": [{"task_id": "task-1", "title": "Map features", "state": "open", "evidence_refs": ["crp://private"]}],
                "blockers": [{"blocker_id": "blocker-1", "summary": "Awaiting source", "severity": "medium", "evidence_refs": ["crp://private"]}],
                "planned_actions": [{"action_id": ACTION, "title": "Build the task center", "expected_outcome": "A safe projection is available", "evidence_refs": ["crp://private"]}],
                "pending_action_ids": [ACTION] if self.pending else [],
                "supervision": {
                    "state": "at_risk",
                    "active_claims": [{"action_id": ACTION}],
                    "claim_statuses": [{
                        "state": "at_risk",
                        "claim": {
                            "action_id": ACTION,
                            "hypothesis": "The action remains valid.",
                            "expected_signals": ["A safe projection is available"],
                            "falsification_signals": ["No safe projection"],
                            "pivot_conditions": ["Replan the task"],
                            "stop_conditions": ["Stop at the authority boundary"],
                            "evidence_refs": ["crp://private"],
                        },
                        "latest_verification": {"verdict": "weakened", "evidence_refs": ["crp://private"]},
                        "latest_decision": {"disposition": "replan_required", "evidence_refs": ["crp://private"]},
                    }],
                    "latest_verification": {"action_id": ACTION, "verdict": "weakened", "finding": "The result needs a correction.", "evidence_refs": ["crp://private"]},
                    "latest_decision": {"action_id": ACTION, "disposition": "replan_required", "rationale": "The next plan must address the gap.", "evidence_refs": ["crp://private"]},
                },
                "latest_feedback_id": "feedback-1" if self.feedback else None,
                "feedback_facts": [{}] if self.feedback else [],
                "confidence": 0.75,
                "risk_codes": ["review"],
                "observations": [{"observation_id": "observation-1", "category": "project", "summary": "The task is active", "evidence_refs": ["crp://private"]}],
                "predictions": [{"predicted_state": "feedback", "confidence": 0.6, "horizon": "next", "assumptions": ["turn completes"], "evidence_refs": ["crp://private"]}],
                "counterfactuals": [{"condition": "turn stalls", "predicted_state": "blocked", "confidence": 0.5, "rationale": "No receipt", "evidence_refs": ["crp://private"]}],
            },
            "recent_actions": ([{
                "turn_id": TURN,
                "action_id": ACTION,
                "status": self.action_status,
                "terminal": self.action_status == "completed",
                "ready_for_feedback": False,
                "answer": "private answer",
                "context": {"private": True, "compaction": {"applied": True, "count": 2, "input_bytes": 1000, "output_bytes": 400, "saved_bytes": 600, "strategy_label": "语义摘要", "source_refs": ["crp://private"]}},
                "governance": {"gate": "passed", "effect": "running", "handler": "running", "receipt": "pending", "feedback": "not_recorded"},
            }] if self.recent else []),
        }


def _organization(project_id: str = PROJECT):
    return {
        "project_id": project_id,
        "projection_revision": "organization-revision-7",
        "has_active_run": True,
        "overview": {"status": "working", "progress": {"completed": 0, "total": 1}, "load": {"active": 1, "queued": 0, "capacity": 2}},
        "main": {"profile_id": "main.orchestrator", "display_name": "Main", "organization_role": "Coordinator", "role": "main", "model_tier": "deep", "status": "running", "prompt": "private"},
        "steward": None,
        "expert_clusters": [{"cluster_id": "research", "status": "working", "mode": "cluster", "assignments": [{"profile_id": "subagent.explorer", "display_name": "Research", "organization_role": "Expert", "role": "subagent", "model_tier": "fast", "status": "running", "expert_identity": "researcher", "skill_ids": ["evidence-read"], "task": {"assignment_id": "assignment-1", "label": "Assigned", "expert_id": "researcher", "skill_ids": ["evidence-read"], "status": "running", "context_ref": "crp://private"}}]}],
    }


def test_task_case_uses_only_pending_action_turn_and_strips_private_fields() -> None:
    turns: list[str] = []
    result = build_project_task_case_projection(
        project_id=PROJECT,
        workflow=_Workflow(),
        agent_organization_for_turn=lambda turn_id: turns.append(turn_id) or _organization(),
    )

    assert turns == [TURN]
    assert result["lifecycle"] == {"stage": "action", "reason": "turn_active"}
    assert result["action"]["turn_id"] == TURN
    assert result["organization"]["main"]["profile_id"] == "main.orchestrator"
    assert result["organization"]["main"]["model_tier"] == "deep"
    assert result["derived_at"] == "2026-09-02T00:00:00Z"
    assert result["deliverables"]["library_href"] == (
        "#view=rebuild-library-overview&project_id=task-case-project"
    )
    assert "organization-revision-7" in result["projection_revision"]
    assert result["dynamics"]["predictions"][0]["predicted_state"] == "feedback"
    assert result["supervision"] == {
        "state": "at_risk", "active_count": 1,
        "claims": [{
            "action_id": ACTION, "replaced_prior_direction": False,
            "hypothesis": "The action remains valid.",
            "expected": ["A safe projection is available"], "falsification": ["No safe projection"],
            "pivot": ["Replan the task"], "stop": ["Stop at the authority boundary"],
            "state": "at_risk", "verdict": "weakened", "finding": "The result needs a correction.",
            "disposition": "replan_required", "rationale": "The next plan must address the gap.",
        }],
    }
    assert result["action"]["compaction"] == {
        "applied": True, "count": 2, "input_bytes": 1000, "output_bytes": 400,
        "saved_bytes": 600, "strategy_label": "语义摘要",
    }
    assert result["trace"] == {
        "dependencies": {"total": 2, "waiting": 0, "items": [
            {"from_label": "项目目标", "relation_label": "决定", "to_label": "当前计划", "state_label": "已建立"},
            {"from_label": "当前计划", "relation_label": "进入", "to_label": "受治理执行", "state_label": "正在执行"},
        ]},
        "verification": {"state": "pending", "verified_count": 1, "pending_count": 3, "latest_kind_label": "执行回执"},
        "freshness": {"state": "fresh", "stale_count": 0, "affected_kind_labels": [], "review_library_link": None},
        "agent_load": {"running": 2, "waiting": 0, "cancelling": 0, "attention": 0},
        "direction_history": [{"stage_label": "当前方向", "state_label": "需要纠偏", "disposition_label": "需要重新规划", "replaced": False}],
    }
    trace_rendered = str(result["trace"]).lower()
    for forbidden in ("action-current", "world-turn", "crp://", "evidence", "payload", "prompt", "context", "provider", "model", "path"):
        assert forbidden not in trace_rendered
    rendered = str(result).lower()
    for forbidden in ("prompt", "context", "private", "crp://", "answer"):
        assert forbidden not in rendered
    assert "model_tier" in rendered


def test_task_case_rejects_invalid_compaction_accounting_without_exposing_context_lineage() -> None:
    class _InvalidContextWorkflow(_Workflow):
        def overview(self, *, project_id: str):
            result = super().overview(project_id=project_id)
            result["recent_actions"][0]["context"]["compaction"] = {
                "applied": True, "count": 1, "input_bytes": 10, "output_bytes": 11,
                "saved_bytes": -1, "strategy_label": "private", "payload_ref": "crp://private",
            }
            return result

    result = build_project_task_case_projection(project_id=PROJECT, workflow=_InvalidContextWorkflow())

    assert result["action"]["compaction"] == {
        "applied": False, "count": 0, "input_bytes": 0, "output_bytes": 0,
        "saved_bytes": 0, "strategy_label": "未使用压缩",
    }
    assert "payload_ref" not in str(result)


def test_task_case_trace_bounds_stale_range_to_safe_labels_and_library_navigation() -> None:
    class _StaleWorkflow(_Workflow):
        def overview(self, *, project_id: str):
            result = super().overview(project_id=project_id)
            result["state"]["risk_codes"] = ["observations_stale", "review"]
            return result

    result = build_project_task_case_projection(project_id=PROJECT, workflow=_StaleWorkflow())

    assert result["trace"]["freshness"] == {
        "state": "stale", "stale_count": 1, "affected_kind_labels": ["项目观察"],
        "review_library_link": "#view=rebuild-library-overview&project_id=task-case-project",
    }


def test_task_case_prefers_latest_project_task_graph_as_a_safe_trace() -> None:
    graph = SimpleNamespace(
        spec=SimpleNamespace(project_id=PROJECT, nodes=(
            SimpleNamespace(node_id="private-node-one", dependency_ids=()),
            SimpleNamespace(node_id="private-node-two", dependency_ids=("private-node-one",)),
        )),
        nodes=(
            SimpleNamespace(node_id="private-node-one", status="settled", validation_status="verified"),
            SimpleNamespace(node_id="private-node-two", status="invalidated", validation_status="invalidated"),
        ),
    )

    result = build_project_task_case_projection(
        project_id=PROJECT,
        workflow=_Workflow(),
        task_graph_for_project=lambda project_id: graph if project_id == PROJECT else None,
    )

    assert result["trace"]["dependencies"] == {
        "total": 1, "node_count": 2, "waiting": 0,
        "items": [{
            "from_label": "任务节点 2", "relation_label": "依赖于",
            "to_label": "任务节点 1", "state_label": "前提已失效",
        }],
        "state_counts": [{"label": "已完成", "count": 1}, {"label": "前提已失效", "count": 1}],
    }
    assert result["trace"]["verification"] == {
        "state": "pending", "verified_count": 1, "pending_count": 1,
        "latest_kind_label": "任务图节点核验",
    }
    assert result["trace"]["freshness"] == {
        "state": "stale", "stale_count": 1, "invalidated_count": 1,
        "affected_kind_labels": ["任务图节点"],
        "review_library_link": "#view=rebuild-library-overview&project_id=task-case-project",
    }
    rendered = str(result["trace"]).lower()
    for forbidden in ("private-node", "subject", "revision", "ref", "budget", "prompt", "provider", "model"):
        assert forbidden not in rendered


def test_task_case_does_not_load_organization_when_no_action_exists() -> None:
    result = build_project_task_case_projection(
        project_id=PROJECT,
        workflow=_Workflow(pending=False, recent=False),
        agent_organization_for_turn=lambda _turn_id: pytest.fail("organization must not be loaded"),
    )

    assert result["lifecycle"] == {"stage": "dynamics", "reason": "action_required"}
    assert result["action"] is None
    assert result["organization"] is None


def test_task_case_rejects_organization_project_scope_drift() -> None:
    with pytest.raises(ValueError, match="scope drifted"):
        build_project_task_case_projection(
            project_id=PROJECT,
            workflow=_Workflow(),
            agent_organization_for_turn=lambda _turn_id: _organization("other-project"),
        )


def test_task_case_retains_latest_action_and_organization_after_feedback() -> None:
    turns: list[str] = []
    result = build_project_task_case_projection(
        project_id=PROJECT,
        workflow=_Workflow(pending=False, feedback=True, action_status="completed"),
        agent_organization_for_turn=lambda turn_id: turns.append(turn_id) or _organization(),
    )

    assert turns == [TURN]
    assert result["lifecycle"] == {"stage": "feedback", "reason": "feedback_recorded"}
    assert result["action"]["turn_id"] == TURN
    assert result["action"]["terminal"] is True
    assert result["organization"]["has_active_run"] is True
    assert result["feedback"] == {
        "recorded": True,
        "ready_for_feedback": False,
        "latest_feedback_id": "feedback-1",
        "feedback_count": 1,
    }
