from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .health import REQUIRED_CONTRACTS
from .phase6_regression import Phase6RegressionReport


Phase7EntryStatus = Literal["ready", "blocked"]
Phase7EntryCheckStatus = Literal["ready", "blocked"]


@dataclass(frozen=True, slots=True)
class Phase7ValidationEvidence:
    contract_schema_count: int
    rebuild_test_count: int
    focused_regression_test_count: int
    contract_validator_passed: bool
    full_rebuild_tests_passed: bool
    compile_check_passed: bool
    diff_check_passed: bool
    direct_smoke_passed: bool


@dataclass(frozen=True, slots=True)
class Phase7PerformanceSample:
    name: str
    duration_seconds: float
    budget_seconds: float
    evidence_ref: str


@dataclass(frozen=True, slots=True)
class Phase7EntryCheck:
    name: str
    status: Phase7EntryCheckStatus
    evidence_refs: tuple[str, ...]
    blockers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Phase7EntryGate:
    status: Phase7EntryStatus
    checks: tuple[Phase7EntryCheck, ...]
    performance_samples: tuple[Phase7PerformanceSample, ...]
    blocking_check_names: tuple[str, ...]
    next_package: str | None


class Phase7EntryGateError(ValueError):
    pass


class EvaluatePhase7EntryGate:
    """Executable entry gate for Phase 7 performance and regression closure."""

    def __init__(
        self,
        *,
        minimum_rebuild_test_count: int = 231,
        minimum_focused_regression_test_count: int = 5,
    ) -> None:
        if minimum_rebuild_test_count <= 0:
            raise Phase7EntryGateError("minimum rebuild test count must be positive")
        if minimum_focused_regression_test_count <= 0:
            raise Phase7EntryGateError("minimum focused regression test count must be positive")
        self._minimum_rebuild_test_count = minimum_rebuild_test_count
        self._minimum_focused_regression_test_count = minimum_focused_regression_test_count

    def execute(
        self,
        *,
        phase6_regression: Phase6RegressionReport,
        validation: Phase7ValidationEvidence,
        performance_samples: tuple[Phase7PerformanceSample, ...],
    ) -> Phase7EntryGate:
        self._validate_samples(performance_samples)
        checks = (
            self._phase6_baseline_check(phase6_regression),
            self._validation_baseline_check(validation),
            self._tooling_baseline_check(validation),
            self._performance_budget_check(performance_samples),
        )
        self._validate_checks(checks)
        blocking_names = tuple(check.name for check in checks if check.status != "ready")
        return Phase7EntryGate(
            status="ready" if not blocking_names else "blocked",
            checks=checks,
            performance_samples=performance_samples,
            blocking_check_names=blocking_names,
            next_package="phase7_first_regression_slice" if not blocking_names else blocking_names[0],
        )

    def _phase6_baseline_check(
        self,
        phase6_regression: Phase6RegressionReport,
    ) -> Phase7EntryCheck:
        blockers: list[str] = []
        if phase6_regression.status != "ready":
            blockers.append("phase 6 regression report is not ready")
        if phase6_regression.blocking_check_names:
            blockers.append("phase 6 regression report has blocking checks")
        if phase6_regression.next_gate != "phase7_entry":
            blockers.append("phase 6 regression report does not open phase7_entry")
        return _check(
            "phase6_baseline",
            blockers=blockers,
            evidence_refs=("R060:phase6-regression",),
        )

    def _validation_baseline_check(self, validation: Phase7ValidationEvidence) -> Phase7EntryCheck:
        blockers: list[str] = []
        if validation.contract_schema_count < len(REQUIRED_CONTRACTS):
            blockers.append("contract schema count is below required rebuild coverage")
        if not validation.contract_validator_passed:
            blockers.append("contract validator did not pass")
        if not validation.full_rebuild_tests_passed:
            blockers.append("full rebuild test suite did not pass")
        if validation.rebuild_test_count < self._minimum_rebuild_test_count:
            blockers.append("rebuild test count is below the Phase 7 entry baseline")
        if validation.focused_regression_test_count < self._minimum_focused_regression_test_count:
            blockers.append("focused regression test count is below the Phase 7 entry baseline")
        if not validation.direct_smoke_passed:
            blockers.append("direct smoke did not pass")
        return _check(
            "validation_baseline",
            blockers=blockers,
            evidence_refs=("R060:validation-baseline", "R061:phase7-entry"),
        )

    def _tooling_baseline_check(self, validation: Phase7ValidationEvidence) -> Phase7EntryCheck:
        blockers: list[str] = []
        if not validation.compile_check_passed:
            blockers.append("compile check did not pass")
        if not validation.diff_check_passed:
            blockers.append("diff check did not pass")
        return _check(
            "tooling_baseline",
            blockers=blockers,
            evidence_refs=("R061:compileall", "R061:diff-check"),
        )

    def _performance_budget_check(
        self,
        performance_samples: tuple[Phase7PerformanceSample, ...],
    ) -> Phase7EntryCheck:
        blockers = [
            f"{sample.name} exceeded budget"
            for sample in performance_samples
            if sample.duration_seconds > sample.budget_seconds
        ]
        return _check(
            "performance_budget",
            blockers=blockers,
            evidence_refs=tuple(sample.evidence_ref for sample in performance_samples),
        )

    def _validate_samples(self, performance_samples: tuple[Phase7PerformanceSample, ...]) -> None:
        if not performance_samples:
            raise Phase7EntryGateError("phase 7 entry requires at least one performance sample")
        names = tuple(sample.name for sample in performance_samples)
        if len(set(names)) != len(names):
            raise Phase7EntryGateError("phase 7 performance sample names must be unique")
        for sample in performance_samples:
            if not sample.name or not sample.evidence_ref:
                raise Phase7EntryGateError("phase 7 performance sample is missing metadata")
            if sample.duration_seconds < 0:
                raise Phase7EntryGateError("phase 7 performance duration cannot be negative")
            if sample.budget_seconds <= 0:
                raise Phase7EntryGateError("phase 7 performance budget must be positive")

    def _validate_checks(self, checks: tuple[Phase7EntryCheck, ...]) -> None:
        expected = ("phase6_baseline", "validation_baseline", "tooling_baseline", "performance_budget")
        names = tuple(check.name for check in checks)
        if names != expected:
            raise Phase7EntryGateError("phase 7 entry checks must keep the expected order")
        for check in checks:
            if not check.evidence_refs:
                raise Phase7EntryGateError("phase 7 entry check is missing evidence refs")
            if check.status == "ready" and check.blockers:
                raise Phase7EntryGateError("ready phase 7 entry check cannot have blockers")


def _check(
    name: str,
    *,
    blockers: list[str],
    evidence_refs: tuple[str, ...],
) -> Phase7EntryCheck:
    return Phase7EntryCheck(
        name=name,
        status="ready" if not blockers else "blocked",
        evidence_refs=evidence_refs,
        blockers=tuple(blockers),
    )
