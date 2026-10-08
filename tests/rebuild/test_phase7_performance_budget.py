from __future__ import annotations

import pytest

from core.product_core import (
    CreatePhase7PerformanceBudgetRecord,
    Phase7EntryCheck,
    Phase7EntryGate,
    Phase7PerformanceBudgetError,
    Phase7PerformanceSample,
)
from core.storage_provider import JsonObjectStore, ObjectStorePhase7PerformanceBudgetRepository


def _entry_gate(
    *,
    status: str = "ready",
    samples: tuple[Phase7PerformanceSample, ...] | None = None,
) -> Phase7EntryGate:
    checks = tuple(
        Phase7EntryCheck(
            name=name,
            status="ready" if status == "ready" else "blocked",
            evidence_refs=(f"R061:{name}",),
            blockers=() if status == "ready" else (f"{name} blocked",),
        )
        for name in ("phase6_baseline", "validation_baseline", "tooling_baseline", "performance_budget")
    )
    return Phase7EntryGate(
        status=status,  # type: ignore[arg-type]
        checks=checks,
        performance_samples=samples
        if samples is not None
        else (
            Phase7PerformanceSample(
                "full_rebuild_tests",
                6.3,
                30.0,
                "R064:pytest-tests-rebuild",
            ),
            Phase7PerformanceSample("direct_smoke", 0.2, 2.0, "R064:direct-smoke"),
        ),
        blocking_check_names=() if status == "ready" else ("validation_baseline",),
        next_package="phase7_first_regression_slice" if status == "ready" else "validation_baseline",
    )


def test_phase7_performance_budget_record_can_be_persisted_and_reloaded(tmp_path) -> None:
    repository = ObjectStorePhase7PerformanceBudgetRepository(
        JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    )

    result = CreatePhase7PerformanceBudgetRecord(repository).execute(
        record_id="R064-phase7-budget",
        phase7_entry=_entry_gate(),
        measured_at="2026-06-30T12:00:00Z",
        source_round="R064",
    )

    stored = repository.get("R064-phase7-budget")
    assert result.status == "within_budget"
    assert result.persisted is True
    assert result.sample_count == 2
    assert result.over_budget_samples == ()
    assert stored is not None
    assert stored["status"] == "within_budget"
    assert stored["sample_count"] == 2
    assert stored["over_budget_samples"] == []
    assert stored["evidence_refs"] == [
        "R061:phase6_baseline",
        "R061:validation_baseline",
        "R061:tooling_baseline",
        "R061:performance_budget",
        "R064:pytest-tests-rebuild",
        "R064:direct-smoke",
    ]
    assert not (tmp_path / "library").exists()


def test_phase7_performance_budget_record_reports_over_budget_without_hiding_samples() -> None:
    result = CreatePhase7PerformanceBudgetRecord().execute(
        record_id="R064-phase7-over-budget",
        phase7_entry=_entry_gate(
            samples=(
                Phase7PerformanceSample("full_rebuild_tests", 31.0, 30.0, "R064:full"),
                Phase7PerformanceSample("direct_smoke", 0.2, 2.0, "R064:smoke"),
            )
        ),
        measured_at="2026-06-30T12:05:00Z",
        source_round="R064",
    )

    assert result.status == "over_budget"
    assert result.persisted is False
    assert result.sample_count == 2
    assert result.over_budget_samples == ("full_rebuild_tests",)


def test_phase7_performance_budget_record_preserves_blocked_entry_gate() -> None:
    result = CreatePhase7PerformanceBudgetRecord().execute(
        record_id="R064-phase7-blocked",
        phase7_entry=_entry_gate(status="blocked"),
        measured_at="2026-06-30T12:10:00Z",
        source_round="R064",
    )

    assert result.status == "blocked"
    assert result.over_budget_samples == ()


def test_phase7_performance_budget_requires_round_timestamp_and_samples() -> None:
    use_case = CreatePhase7PerformanceBudgetRecord()

    with pytest.raises(Phase7PerformanceBudgetError, match="source round"):
        use_case.execute(
            record_id="R064-phase7-bad-round",
            phase7_entry=_entry_gate(),
            measured_at="2026-06-30T12:00:00Z",
            source_round="phase7",
        )

    with pytest.raises(Phase7PerformanceBudgetError, match="UTC timestamp"):
        use_case.execute(
            record_id="R064-phase7-bad-time",
            phase7_entry=_entry_gate(),
            measured_at="2026-06-30 12:00:00",
            source_round="R064",
        )

    with pytest.raises(Phase7PerformanceBudgetError, match="at least one sample"):
        use_case.execute(
            record_id="R064-phase7-no-samples",
            phase7_entry=_entry_gate(samples=()),
            measured_at="2026-06-30T12:00:00Z",
            source_round="R064",
        )


def test_phase7_performance_budget_repository_rejects_invalid_payloads(tmp_path) -> None:
    repository = ObjectStorePhase7PerformanceBudgetRepository(
        JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    )
    payload = {
        "id": "R064-bad-budget",
        "schema_version": "1.0.0",
        "kind": "phase7_performance_budget",
        "source_round": "R064",
        "measured_at": "2026-06-30T12:00:00Z",
        "status": "within_budget",
        "phase7_entry_status": "ready",
        "phase7_entry_next_package": "phase7_first_regression_slice",
        "blocking_check_names": [],
        "sample_count": 1,
        "over_budget_samples": [],
        "samples": [
            {
                "name": "full_rebuild_tests",
                "duration_seconds": 31.0,
                "budget_seconds": 30.0,
                "within_budget": True,
                "evidence_ref": "R064:full",
            }
        ],
        "evidence_refs": ["R064:full"],
    }

    with pytest.raises(ValueError, match="within_budget mismatch"):
        repository.save(payload)

    payload["samples"][0]["within_budget"] = False  # type: ignore[index]
    payload["over_budget_samples"] = ["full_rebuild_tests"]
    with pytest.raises(ValueError, match="within_budget status"):
        repository.save(payload)

    payload["status"] = "over_budget"
    payload["evidence_refs"] = ["C:\\Users\\demo\\report.txt"]
    with pytest.raises(ValueError, match="must not be an OS path"):
        repository.save(payload)


def test_phase7_performance_budget_repository_rejects_duplicate_records(tmp_path) -> None:
    repository = ObjectStorePhase7PerformanceBudgetRepository(
        JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    )
    use_case = CreatePhase7PerformanceBudgetRecord(repository)
    use_case.execute(
        record_id="R064-phase7-budget",
        phase7_entry=_entry_gate(),
        measured_at="2026-06-30T12:00:00Z",
        source_round="R064",
    )

    with pytest.raises(ValueError, match="already exists"):
        use_case.execute(
            record_id="R064-phase7-budget",
            phase7_entry=_entry_gate(),
            measured_at="2026-06-30T12:01:00Z",
            source_round="R064",
        )
