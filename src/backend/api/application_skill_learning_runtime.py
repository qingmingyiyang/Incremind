"""Trusted Turn-to-Skill-learning adapter.

This runtime has no route or startup side effect.  Its caller must provide the
authoritative completed Turn receipt and project mapping; the workshop then
uses only the recorded Application Skill trace and exact package fingerprint.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from core.application_skill import SkillLearningWorkshop, SkillLearningWorkshopError


class CompletedTurnReceiptReader(Protocol):
    def receipt_for(self, turn_id: str, *, replayed: bool = False) -> object: ...


class TurnProjectReader(Protocol):
    def project_id_for(self, turn_id: str) -> str: ...


@dataclass(frozen=True, slots=True)
class ApplicationSkillLearningRuntime:
    """Proposal-only learning from a terminal, authoritative AI Turn."""

    workshop: SkillLearningWorkshop
    receipts: CompletedTurnReceiptReader
    projects: TurnProjectReader

    def propose(
        self,
        *,
        turn_id: str,
        resolution_id: str,
        skill_id: str,
        expected_fingerprint: str,
        reusable_signal: Mapping[str, object],
        proposed_content: Mapping[str, object],
    ) -> dict[str, object]:
        receipt = self.receipts.receipt_for(turn_id)
        observed_turn_id = getattr(receipt, "turn_id", None)
        status = getattr(receipt, "status", None)
        operation_id = getattr(receipt, "operation_id", None)
        if observed_turn_id != turn_id or status != "completed" or not isinstance(operation_id, str):
            raise SkillLearningWorkshopError("Skill learning requires an authoritative completed Turn")
        project_id = self.projects.project_id_for(turn_id)
        if not isinstance(project_id, str):
            raise SkillLearningWorkshopError("Turn project authority is unavailable")
        return self.workshop.propose(
            turn_receipt={
                "turn_id": turn_id,
                "receipt_id": operation_id,
                "project_id": project_id,
                "status": "completed",
            },
            resolution_id=resolution_id,
            skill_id=skill_id,
            expected_fingerprint=expected_fingerprint,
            reusable_signal=reusable_signal,
            proposed_content=proposed_content,
        )
