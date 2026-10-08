from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from .phase7_entry import Phase7EntryGate, Phase7PerformanceSample


Phase7PerformanceBudgetStatus = Literal["within_budget", "over_budget", "blocked"]


class Phase7PerformanceBudgetStorePort(Protocol):
    def save(self, record: dict[str, object]) -> dict[str, object]:
        """Persist a Phase 7 performance budget record."""


@dataclass(frozen=True, slots=True)
class Phase7PerformanceBudgetResult:
    record_id: str
    status: Phase7PerformanceBudgetStatus
    sample_count: int
    over_budget_samples: tuple[str, ...]
    persisted: bool


class Phase7PerformanceBudgetError(ValueError):
    pass


class CreatePhase7PerformanceBudgetRecord:
    """Create and optionally persist reusable Phase 7 performance budget evidence."""

    def __init__(self, store: Phase7PerformanceBudgetStorePort | None = None) -> None:
        self._store = store

    def execute(
        self,
        *,
        record_id: str,
        phase7_entry: Phase7EntryGate,
        measured_at: str,
        source_round: str,
    ) -> Phase7PerformanceBudgetResult:
        samples = phase7_entry.performance_samples
        self._validate_inputs(
            record_id=record_id,
            measured_at=measured_at,
            source_round=source_round,
            samples=samples,
        )
        over_budget = tuple(
            sample.name for sample in samples if sample.duration_seconds > sample.budget_seconds
        )
        status: Phase7PerformanceBudgetStatus
        if phase7_entry.status != "ready" or phase7_entry.blocking_check_names:
            status = "blocked"
        elif over_budget:
            status = "over_budget"
        else:
            status = "within_budget"
        record = {
            "id": record_id,
            "schema_version": "1.0.0",
            "kind": "phase7_performance_budget",
            "source_round": source_round,
            "measured_at": measured_at,
            "status": status,
            "phase7_entry_status": phase7_entry.status,
            "phase7_entry_next_package": phase7_entry.next_package,
            "blocking_check_names": list(phase7_entry.blocking_check_names),
            "sample_count": len(samples),
            "over_budget_samples": list(over_budget),
            "samples": [self._sample_payload(sample) for sample in samples],
            "evidence_refs": self._evidence_refs(phase7_entry),
        }
        persisted = False
        if self._store is not None:
            self._store.save(record)
            persisted = True
        return Phase7PerformanceBudgetResult(
            record_id=record_id,
            status=status,
            sample_count=len(samples),
            over_budget_samples=over_budget,
            persisted=persisted,
        )

    def _validate_inputs(
        self,
        *,
        record_id: str,
        measured_at: str,
        source_round: str,
        samples: tuple[Phase7PerformanceSample, ...],
    ) -> None:
        if not record_id:
            raise Phase7PerformanceBudgetError("performance budget record id is required")
        if not source_round.startswith("R"):
            raise Phase7PerformanceBudgetError("performance budget source round must be an R-round id")
        if "T" not in measured_at or not measured_at.endswith("Z"):
            raise Phase7PerformanceBudgetError("performance budget measured_at must be an UTC timestamp")
        if not samples:
            raise Phase7PerformanceBudgetError("performance budget requires at least one sample")
        names = tuple(sample.name for sample in samples)
        if len(set(names)) != len(names):
            raise Phase7PerformanceBudgetError("performance budget sample names must be unique")
        for sample in samples:
            if not sample.name or not sample.evidence_ref:
                raise Phase7PerformanceBudgetError("performance budget sample metadata is required")
            if sample.duration_seconds < 0:
                raise Phase7PerformanceBudgetError("performance budget sample duration cannot be negative")
            if sample.budget_seconds <= 0:
                raise Phase7PerformanceBudgetError("performance budget sample budget must be positive")

    def _sample_payload(self, sample: Phase7PerformanceSample) -> dict[str, object]:
        return {
            "name": sample.name,
            "duration_seconds": sample.duration_seconds,
            "budget_seconds": sample.budget_seconds,
            "within_budget": sample.duration_seconds <= sample.budget_seconds,
            "evidence_ref": sample.evidence_ref,
        }

    def _evidence_refs(self, phase7_entry: Phase7EntryGate) -> list[str]:
        refs: list[str] = []
        for check in phase7_entry.checks:
            refs.extend(check.evidence_refs)
        for sample in phase7_entry.performance_samples:
            refs.append(sample.evidence_ref)
        return list(dict.fromkeys(refs))
