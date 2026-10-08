from core.product_core.workflow_progression import (
    WorkflowDecisionBoundary,
    WorkflowProgressionMode,
    decide_workflow_progression,
)


def test_deterministic_workflow_step_advances_automatically() -> None:
    decision = decide_workflow_progression(WorkflowDecisionBoundary.DETERMINISTIC)

    assert decision.mode is WorkflowProgressionMode.AUTO
    assert decision.requires_user_confirmation is False


def test_reversible_visible_change_advances_with_notice() -> None:
    decision = decide_workflow_progression(WorkflowDecisionBoundary.REVERSIBLE_VISIBLE_CHANGE)

    assert decision.mode is WorkflowProgressionMode.AUTO_WITH_NOTICE
    assert decision.requires_user_confirmation is False


def test_material_boundaries_require_user_decision() -> None:
    for boundary in (
        WorkflowDecisionBoundary.PERMISSION_EXPANSION,
        WorkflowDecisionBoundary.IRREVERSIBLE_CHANGE,
        WorkflowDecisionBoundary.BUDGET_EXCEEDED,
        WorkflowDecisionBoundary.MATERIAL_AMBIGUITY,
        WorkflowDecisionBoundary.UNKNOWN_EFFECT,
        WorkflowDecisionBoundary.EXTERNAL_DOWNLOAD_WRITES_FILE,
        WorkflowDecisionBoundary.FORMAL_MEMORY_PUBLICATION,
        WorkflowDecisionBoundary.HARD_REDACT,
        WorkflowDecisionBoundary.HIGH_RISK,
    ):
        decision = decide_workflow_progression(boundary)
        assert decision.mode is WorkflowProgressionMode.ASK
        assert decision.reason is boundary


def test_unspecified_workflow_choice_fails_closed_to_ask() -> None:
    decision = decide_workflow_progression()

    assert decision.mode is WorkflowProgressionMode.ASK
    assert decision.reason is WorkflowDecisionBoundary.MATERIAL_AMBIGUITY
