from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .phase6_regression import Phase6RegressionReport
from .phase7_entry import Phase7EntryGate
from .thin_ui_validation_path import ThinUiValidationPath, ThinUiValidationStep


Phase7RegressionClosureStatus = Literal["closed", "blocked"]
Phase7RegressionClosureCheckStatus = Literal["ready", "blocked"]


@dataclass(frozen=True, slots=True)
class Phase7RegressionClosureCheck:
    name: str
    status: Phase7RegressionClosureCheckStatus
    evidence_refs: tuple[str, ...]
    blockers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Phase7RegressionClosureReport:
    closure_id: str
    status: Phase7RegressionClosureStatus
    checks: tuple[Phase7RegressionClosureCheck, ...]
    blocking_check_names: tuple[str, ...]
    next_package: str | None


class Phase7RegressionClosureError(ValueError):
    pass


class ClosePhase7FirstRegressionSlice:
    """Close the first Phase 7 regression slice across health, recall and UI validation."""

    _CLOSURE_ID = "phase7_first_regression_slice"

    def execute(
        self,
        *,
        phase7_entry: Phase7EntryGate,
        phase6_regression: Phase6RegressionReport,
        thin_ui_validation_path: ThinUiValidationPath,
    ) -> Phase7RegressionClosureReport:
        checks = (
            self._entry_gate_check(phase7_entry),
            self._health_regression_check(phase6_regression),
            self._recall_contract_check(phase6_regression, thin_ui_validation_path),
            self._ui_validation_contract_check(thin_ui_validation_path),
            self._traceability_contract_check(thin_ui_validation_path),
        )
        self._validate_checks(checks)
        blocking_names = tuple(check.name for check in checks if check.status != "ready")
        return Phase7RegressionClosureReport(
            closure_id=self._CLOSURE_ID,
            status="closed" if not blocking_names else "blocked",
            checks=checks,
            blocking_check_names=blocking_names,
            next_package="thin_ui_backend_contract_adapter_smoke" if not blocking_names else blocking_names[0],
        )

    def _entry_gate_check(self, phase7_entry: Phase7EntryGate) -> Phase7RegressionClosureCheck:
        blockers: list[str] = []
        if phase7_entry.status != "ready":
            blockers.append("phase 7 entry gate is not ready")
        if phase7_entry.blocking_check_names:
            blockers.append("phase 7 entry gate has blocking checks")
        if phase7_entry.next_package != self._CLOSURE_ID:
            blockers.append("phase 7 entry gate does not open the first regression slice")
        return _check(
            "entry_gate",
            blockers=blockers,
            evidence_refs=("R061:phase7-entry",),
        )

    def _health_regression_check(
        self,
        phase6_regression: Phase6RegressionReport,
    ) -> Phase7RegressionClosureCheck:
        blockers: list[str] = []
        if phase6_regression.status != "ready":
            blockers.append("phase 6 health regression is not ready")
        if phase6_regression.blocking_check_names:
            blockers.append("phase 6 health regression has blocking checks")
        missing = tuple(
            name
            for name in ("contracts", "storage", "index", "platform", "phase6_entry", "thin_ui_validation")
            if name not in {check.name for check in phase6_regression.checks}
        )
        if missing:
            blockers.append("phase 6 health regression is missing expected checks")
        return _check(
            "health_regression",
            blockers=blockers,
            evidence_refs=("R060:phase6-regression",),
        )

    def _recall_contract_check(
        self,
        phase6_regression: Phase6RegressionReport,
        thin_ui_validation_path: ThinUiValidationPath,
    ) -> Phase7RegressionClosureCheck:
        blockers: list[str] = []
        index = _step_like_check(phase6_regression.checks, "index")
        qa = _thin_ui_step(thin_ui_validation_path.steps, "qa")
        if index is None or index.status != "ready":
            blockers.append("index regression check is not ready")
        if qa is None or qa.status != "ready":
            blockers.append("thin UI QA validation step is not ready")
        elif not {"recall_index", "answer_model_request"}.issubset(set(qa.required_capabilities)):
            blockers.append("thin UI QA validation step is missing recall capabilities")
        if qa is not None and not qa.evidence_refs:
            blockers.append("thin UI QA validation step is missing evidence refs")
        return _check(
            "recall_contract",
            blockers=blockers,
            evidence_refs=("R056:fts5-health", "R059:thin-ui-validation"),
        )

    def _ui_validation_contract_check(
        self,
        thin_ui_validation_path: ThinUiValidationPath,
    ) -> Phase7RegressionClosureCheck:
        blockers: list[str] = []
        if thin_ui_validation_path.status != "ready":
            blockers.append("thin UI validation path is not ready")
        if thin_ui_validation_path.next_entry_action is not None:
            blockers.append("thin UI validation path still has a next blocking action")
        names = tuple(step.name for step in thin_ui_validation_path.steps)
        if names != ("input", "library", "memory", "document", "qa", "provenance"):
            blockers.append("thin UI validation path does not expose the expected six-step loop")
        not_ready = tuple(step.name for step in thin_ui_validation_path.steps if step.status != "ready")
        if not_ready:
            blockers.append("thin UI validation path has non-ready steps")
        return _check(
            "ui_validation_contract",
            blockers=blockers,
            evidence_refs=("R059:thin-ui-validation",),
        )

    def _traceability_contract_check(
        self,
        thin_ui_validation_path: ThinUiValidationPath,
    ) -> Phase7RegressionClosureCheck:
        blockers: list[str] = []
        provenance = _thin_ui_step(thin_ui_validation_path.steps, "provenance")
        if provenance is None or provenance.status != "ready":
            blockers.append("provenance validation step is not ready")
        elif "source_trace" not in provenance.required_capabilities:
            blockers.append("provenance validation step is missing source trace capability")
        if provenance is not None and not provenance.evidence_refs:
            blockers.append("provenance validation step is missing evidence refs")
        return _check(
            "traceability_contract",
            blockers=blockers,
            evidence_refs=("R041:phase5-integration", "R059:thin-ui-validation"),
        )

    def _validate_checks(self, checks: tuple[Phase7RegressionClosureCheck, ...]) -> None:
        expected = (
            "entry_gate",
            "health_regression",
            "recall_contract",
            "ui_validation_contract",
            "traceability_contract",
        )
        names = tuple(check.name for check in checks)
        if names != expected:
            raise Phase7RegressionClosureError("phase 7 regression closure checks must keep the expected order")
        for check in checks:
            if not check.evidence_refs:
                raise Phase7RegressionClosureError("phase 7 regression closure check is missing evidence refs")
            if check.status == "ready" and check.blockers:
                raise Phase7RegressionClosureError("ready phase 7 regression closure check cannot have blockers")


def _step_like_check(
    checks: tuple[object, ...],
    name: str,
) -> object | None:
    return next((check for check in checks if getattr(check, "name", None) == name), None)


def _thin_ui_step(
    steps: tuple[ThinUiValidationStep, ...],
    name: str,
) -> ThinUiValidationStep | None:
    return next((step for step in steps if step.name == name), None)


def _check(
    name: str,
    *,
    blockers: list[str],
    evidence_refs: tuple[str, ...],
) -> Phase7RegressionClosureCheck:
    return Phase7RegressionClosureCheck(
        name=name,
        status="ready" if not blockers else "blocked",
        evidence_refs=evidence_refs,
        blockers=tuple(blockers),
    )
