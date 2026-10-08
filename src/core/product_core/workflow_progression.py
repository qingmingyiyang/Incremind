from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class WorkflowProgressionMode(StrEnum):
    AUTO = "auto"
    AUTO_WITH_NOTICE = "auto_with_notice"
    ASK = "ask"


class WorkflowDecisionBoundary(StrEnum):
    DETERMINISTIC = "deterministic"
    REVERSIBLE_VISIBLE_CHANGE = "reversible_visible_change"
    PERMISSION_EXPANSION = "permission_expansion"
    IRREVERSIBLE_CHANGE = "irreversible_change"
    BUDGET_EXCEEDED = "budget_exceeded"
    MATERIAL_AMBIGUITY = "material_ambiguity"
    UNKNOWN_EFFECT = "unknown_effect"
    EXTERNAL_DOWNLOAD_WRITES_FILE = "external_download_writes_file"
    FORMAL_MEMORY_PUBLICATION = "formal_memory_publication"
    HARD_REDACT = "hard_redact"
    HIGH_RISK = "high_risk"


@dataclass(frozen=True, slots=True)
class WorkflowProgressionDecision:
    mode: WorkflowProgressionMode
    reason: WorkflowDecisionBoundary

    def __post_init__(self) -> None:
        if not isinstance(self.reason, WorkflowDecisionBoundary):
            raise TypeError("workflow progression reason must be a closed decision boundary")

    @property
    def requires_user_confirmation(self) -> bool:
        return self.mode is WorkflowProgressionMode.ASK


_ASK_PRIORITY = (
    WorkflowDecisionBoundary.UNKNOWN_EFFECT,
    WorkflowDecisionBoundary.EXTERNAL_DOWNLOAD_WRITES_FILE,
    WorkflowDecisionBoundary.FORMAL_MEMORY_PUBLICATION,
    WorkflowDecisionBoundary.HARD_REDACT,
    WorkflowDecisionBoundary.PERMISSION_EXPANSION,
    WorkflowDecisionBoundary.IRREVERSIBLE_CHANGE,
    WorkflowDecisionBoundary.BUDGET_EXCEEDED,
    WorkflowDecisionBoundary.HIGH_RISK,
    WorkflowDecisionBoundary.MATERIAL_AMBIGUITY,
)


def decide_workflow_progression(
    *boundaries: WorkflowDecisionBoundary,
) -> WorkflowProgressionDecision:
    """Choose progression without owning Gate policy or execution state."""

    boundary_set = frozenset(boundaries)
    for boundary in _ASK_PRIORITY:
        if boundary in boundary_set:
            return WorkflowProgressionDecision(WorkflowProgressionMode.ASK, boundary)
    if WorkflowDecisionBoundary.REVERSIBLE_VISIBLE_CHANGE in boundary_set:
        return WorkflowProgressionDecision(
            WorkflowProgressionMode.AUTO_WITH_NOTICE,
            WorkflowDecisionBoundary.REVERSIBLE_VISIBLE_CHANGE,
        )
    if WorkflowDecisionBoundary.DETERMINISTIC in boundary_set:
        return WorkflowProgressionDecision(
            WorkflowProgressionMode.AUTO,
            WorkflowDecisionBoundary.DETERMINISTIC,
        )
    return WorkflowProgressionDecision(
        WorkflowProgressionMode.ASK,
        WorkflowDecisionBoundary.MATERIAL_AMBIGUITY,
    )
