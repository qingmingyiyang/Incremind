from __future__ import annotations

import pytest

from core.product_core import (
    EvaluatePhase7EntryGate,
    Phase6RegressionCheck,
    Phase6RegressionReport,
    Phase7EntryGateError,
    Phase7PerformanceSample,
    Phase7ValidationEvidence,
)
from core.product_core.health import REQUIRED_CONTRACTS


def _phase6_regression(status: str = "ready") -> Phase6RegressionReport:
    checks = tuple(
        Phase6RegressionCheck(
            name=name,
            status="ready" if status == "ready" else "blocked",
            evidence_refs=(f"R060:{name}",),
            blockers=() if status == "ready" else (f"{name} blocked",),
        )
        for name in ("contracts", "storage", "index", "platform", "phase6_entry", "thin_ui_validation")
    )
    return Phase6RegressionReport(
        status=status,  # type: ignore[arg-type]
        checks=checks,
        blocking_check_names=() if status == "ready" else ("index",),
        next_gate="phase7_entry" if status == "ready" else "index",
    )


def _validation(
    *,
    contract_schema_count: int = len(REQUIRED_CONTRACTS),
    rebuild_test_count: int = 231,
    focused_regression_test_count: int = 5,
    contract_validator_passed: bool = True,
    full_rebuild_tests_passed: bool = True,
    compile_check_passed: bool = True,
    diff_check_passed: bool = True,
    direct_smoke_passed: bool = True,
) -> Phase7ValidationEvidence:
    return Phase7ValidationEvidence(
        contract_schema_count=contract_schema_count,
        rebuild_test_count=rebuild_test_count,
        focused_regression_test_count=focused_regression_test_count,
        contract_validator_passed=contract_validator_passed,
        full_rebuild_tests_passed=full_rebuild_tests_passed,
        compile_check_passed=compile_check_passed,
        diff_check_passed=diff_check_passed,
        direct_smoke_passed=direct_smoke_passed,
    )


def _samples() -> tuple[Phase7PerformanceSample, ...]:
    return (
        Phase7PerformanceSample(
            name="full_rebuild_tests",
            duration_seconds=6.4,
            budget_seconds=30.0,
            evidence_ref="R061:pytest-tests-rebuild",
        ),
        Phase7PerformanceSample(
            name="phase7_entry_direct_smoke",
            duration_seconds=0.2,
            budget_seconds=2.0,
            evidence_ref="R061:direct-smoke",
        ),
    )


def test_phase7_entry_gate_is_ready_after_phase6_regression_and_validation_baseline() -> None:
    gate = EvaluatePhase7EntryGate().execute(
        phase6_regression=_phase6_regression(),
        validation=_validation(),
        performance_samples=_samples(),
    )

    assert gate.status == "ready"
    assert gate.blocking_check_names == ()
    assert gate.next_package == "phase7_first_regression_slice"
    assert tuple(check.name for check in gate.checks) == (
        "phase6_baseline",
        "validation_baseline",
        "tooling_baseline",
        "performance_budget",
    )
    assert all(check.status == "ready" for check in gate.checks)


def test_phase7_entry_gate_blocks_when_phase6_regression_is_not_ready() -> None:
    gate = EvaluatePhase7EntryGate().execute(
        phase6_regression=_phase6_regression("needs_attention"),
        validation=_validation(),
        performance_samples=_samples(),
    )

    phase6 = next(check for check in gate.checks if check.name == "phase6_baseline")
    assert gate.status == "blocked"
    assert gate.next_package == "phase6_baseline"
    assert phase6.blockers == (
        "phase 6 regression report is not ready",
        "phase 6 regression report has blocking checks",
        "phase 6 regression report does not open phase7_entry",
    )


def test_phase7_entry_gate_blocks_weak_validation_baseline() -> None:
    gate = EvaluatePhase7EntryGate().execute(
        phase6_regression=_phase6_regression(),
        validation=_validation(
            contract_schema_count=20,
            rebuild_test_count=230,
            focused_regression_test_count=4,
            full_rebuild_tests_passed=False,
            direct_smoke_passed=False,
        ),
        performance_samples=_samples(),
    )

    validation = next(check for check in gate.checks if check.name == "validation_baseline")
    assert gate.status == "blocked"
    assert gate.next_package == "validation_baseline"
    assert validation.blockers == (
        "contract schema count is below required rebuild coverage",
        "full rebuild test suite did not pass",
        "rebuild test count is below the Phase 7 entry baseline",
        "focused regression test count is below the Phase 7 entry baseline",
        "direct smoke did not pass",
    )


def test_phase7_entry_gate_blocks_tooling_failures() -> None:
    gate = EvaluatePhase7EntryGate().execute(
        phase6_regression=_phase6_regression(),
        validation=_validation(compile_check_passed=False, diff_check_passed=False),
        performance_samples=_samples(),
    )

    tooling = next(check for check in gate.checks if check.name == "tooling_baseline")
    assert gate.status == "blocked"
    assert gate.next_package == "tooling_baseline"
    assert tooling.blockers == (
        "compile check did not pass",
        "diff check did not pass",
    )


def test_phase7_entry_gate_blocks_performance_budget_overrun() -> None:
    gate = EvaluatePhase7EntryGate().execute(
        phase6_regression=_phase6_regression(),
        validation=_validation(),
        performance_samples=(
            Phase7PerformanceSample(
                name="full_rebuild_tests",
                duration_seconds=31.0,
                budget_seconds=30.0,
                evidence_ref="R061:pytest-tests-rebuild",
            ),
        ),
    )

    performance = next(check for check in gate.checks if check.name == "performance_budget")
    assert gate.status == "blocked"
    assert gate.next_package == "performance_budget"
    assert performance.blockers == ("full_rebuild_tests exceeded budget",)


def test_phase7_entry_gate_requires_valid_performance_samples() -> None:
    with pytest.raises(Phase7EntryGateError, match="at least one performance sample"):
        EvaluatePhase7EntryGate().execute(
            phase6_regression=_phase6_regression(),
            validation=_validation(),
            performance_samples=(),
        )

    with pytest.raises(Phase7EntryGateError, match="names must be unique"):
        EvaluatePhase7EntryGate().execute(
            phase6_regression=_phase6_regression(),
            validation=_validation(),
            performance_samples=(
                Phase7PerformanceSample("smoke", 0.1, 1.0, "R061:a"),
                Phase7PerformanceSample("smoke", 0.2, 1.0, "R061:b"),
            ),
        )
