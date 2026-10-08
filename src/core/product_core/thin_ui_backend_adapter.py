from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Literal

from .phase7_regression_closure import Phase7RegressionClosureReport
from .thin_ui_validation_path import ThinUiValidationPath, ThinUiValidationStep


ThinUiBackendViewStatus = Literal["ready", "needs_attention"]
ThinUiBackendStepStatus = Literal["ready", "pending", "blocked"]


@dataclass(frozen=True, slots=True)
class ThinUiBackendStepView:
    name: str
    status: ThinUiBackendStepStatus
    label: str
    action: str
    enabled: bool
    evidence_refs: tuple[str, ...]
    blockers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ThinUiBackendRegressionView:
    closure_id: str
    status: str
    blocking_check_names: tuple[str, ...]
    next_package: str | None


@dataclass(frozen=True, slots=True)
class ThinUiBackendContractView:
    version: str
    status: ThinUiBackendViewStatus
    primary_action: str | None
    steps: tuple[ThinUiBackendStepView, ...]
    regression: ThinUiBackendRegressionView
    phase8_regression_evidence_refs: tuple[str, ...]
    validation_refresh_evidence_refs: tuple[str, ...]
    evidence_refs: tuple[str, ...]


class ThinUiBackendAdapterError(ValueError):
    pass


class CreateThinUiBackendContractView:
    """Adapt backend validation state into a UI-consumable contract view."""

    _VERSION = "thin-ui-backend-contract.v2"
    _PHASE8_REGRESSION_REF = "R094:legacy-migration-final-writer-implementation-regression-consolidation"
    _VALIDATION_REFRESH_REF = "R095:phase9-thin-ui-validation-path-refresh"

    def execute(
        self,
        *,
        validation_path: ThinUiValidationPath,
        regression_closure: Phase7RegressionClosureReport,
    ) -> ThinUiBackendContractView:
        steps = tuple(self._step_view(step) for step in validation_path.steps)
        self._validate_steps(steps)
        ready = (
            validation_path.status == "ready"
            and regression_closure.status == "closed"
            and not regression_closure.blocking_check_names
        )
        primary_action = self._primary_action(
            validation_path=validation_path,
            regression_closure=regression_closure,
            ready=ready,
        )
        phase8_regression_evidence_refs = self._collect_phase8_regression_evidence_refs(steps)
        validation_refresh_evidence_refs = self._collect_validation_refresh_evidence_refs(
            phase8_regression_evidence_refs
        )
        evidence_refs = self._collect_evidence_refs(
            steps,
            regression_closure,
            phase8_regression_evidence_refs,
            validation_refresh_evidence_refs,
        )
        view = ThinUiBackendContractView(
            version=self._VERSION,
            status="ready" if ready else "needs_attention",
            primary_action=primary_action,
            steps=steps,
            regression=ThinUiBackendRegressionView(
                closure_id=regression_closure.closure_id,
                status=regression_closure.status,
                blocking_check_names=regression_closure.blocking_check_names,
                next_package=regression_closure.next_package,
            ),
            phase8_regression_evidence_refs=phase8_regression_evidence_refs,
            validation_refresh_evidence_refs=validation_refresh_evidence_refs,
            evidence_refs=evidence_refs,
        )
        self._validate_view(view)
        return view

    def _step_view(self, step: ThinUiValidationStep) -> ThinUiBackendStepView:
        return ThinUiBackendStepView(
            name=step.name,
            status=step.status,
            label=step.entry_label,
            action=step.entry_action,
            enabled=step.status != "blocked",
            evidence_refs=step.evidence_refs,
            blockers=step.blockers,
        )

    def _primary_action(
        self,
        *,
        validation_path: ThinUiValidationPath,
        regression_closure: Phase7RegressionClosureReport,
        ready: bool,
    ) -> str | None:
        if ready:
            return "open_input_intake"
        if validation_path.next_entry_action is not None:
            return validation_path.next_entry_action
        if regression_closure.blocking_check_names:
            return f"inspect_{regression_closure.blocking_check_names[0]}"
        return None

    def _collect_evidence_refs(
        self,
        steps: tuple[ThinUiBackendStepView, ...],
        regression_closure: Phase7RegressionClosureReport,
        phase8_regression_evidence_refs: tuple[str, ...],
        validation_refresh_evidence_refs: tuple[str, ...],
    ) -> tuple[str, ...]:
        refs: list[str] = []
        for step in steps:
            refs.extend(step.evidence_refs)
        for check in regression_closure.checks:
            refs.extend(check.evidence_refs)
        refs.append(f"closure:{regression_closure.closure_id}")
        refs.extend(phase8_regression_evidence_refs)
        refs.extend(validation_refresh_evidence_refs)
        return tuple(dict.fromkeys(refs))

    def _collect_phase8_regression_evidence_refs(
        self, steps: tuple[ThinUiBackendStepView, ...]
    ) -> tuple[str, ...]:
        refs = [
            ref
            for step in steps
            for ref in step.evidence_refs
            if ref == self._PHASE8_REGRESSION_REF
            or ref.startswith("R094:")
            or "phase8-regression" in ref
            or "regression-consolidation" in ref
        ]
        return tuple(dict.fromkeys(refs))

    def _collect_validation_refresh_evidence_refs(
        self, phase8_regression_evidence_refs: tuple[str, ...]
    ) -> tuple[str, ...]:
        if not phase8_regression_evidence_refs:
            return ()
        return (self._VALIDATION_REFRESH_REF, *phase8_regression_evidence_refs)

    def _validate_steps(self, steps: tuple[ThinUiBackendStepView, ...]) -> None:
        expected = ("input", "library", "memory", "document", "qa", "provenance")
        names = tuple(step.name for step in steps)
        if names != expected:
            raise ThinUiBackendAdapterError("thin UI backend view must expose the expected six steps")
        for step in steps:
            if not step.label or not step.action:
                raise ThinUiBackendAdapterError("thin UI backend step is missing label or action")
            if not step.evidence_refs:
                raise ThinUiBackendAdapterError("thin UI backend step is missing evidence refs")
            for ref in step.evidence_refs:
                if self._looks_like_os_path(ref):
                    raise ThinUiBackendAdapterError("thin UI backend step evidence refs must not be OS paths")
            if step.status == "ready" and step.blockers:
                raise ThinUiBackendAdapterError("ready thin UI backend step cannot have blockers")
            if step.status == "blocked" and step.enabled:
                raise ThinUiBackendAdapterError("blocked thin UI backend step cannot be enabled")

    def _validate_view(self, view: ThinUiBackendContractView) -> None:
        if view.version != self._VERSION:
            raise ThinUiBackendAdapterError("thin UI backend view version mismatch")
        if not view.evidence_refs:
            raise ThinUiBackendAdapterError("thin UI backend view is missing evidence refs")
        for ref in view.evidence_refs:
            if self._looks_like_os_path(ref):
                raise ThinUiBackendAdapterError("thin UI backend view evidence refs must not be OS paths")
        if view.status == "ready" and view.regression.status != "closed":
            raise ThinUiBackendAdapterError("ready thin UI backend view requires closed regression")
        if view.status == "ready" and self._PHASE8_REGRESSION_REF not in view.phase8_regression_evidence_refs:
            raise ThinUiBackendAdapterError("ready thin UI backend view requires Phase 8 regression evidence")
        if view.status == "ready" and self._VALIDATION_REFRESH_REF not in view.validation_refresh_evidence_refs:
            raise ThinUiBackendAdapterError("ready thin UI backend view requires R095 validation refresh evidence")

    def _looks_like_os_path(self, ref: str) -> bool:
        return bool(re.match(r"^[A-Za-z]:[\\/]", ref)) or ref.startswith(("\\\\", "/", "\\"))
