from __future__ import annotations

import pytest

from core.product_core import (
    CreateThinUiBackendContractView,
    Phase7RegressionClosureCheck,
    Phase7RegressionClosureReport,
    ThinUiBackendAdapterError,
    ThinUiValidationPath,
    ThinUiValidationStep,
)


def _validation_path(
    *,
    status: str = "ready",
    blocked_step: str | None = None,
    pending_step: str | None = None,
    step_names: tuple[str, ...] = ("input", "library", "memory", "document", "qa", "provenance"),
    include_phase8_regression: bool = True,
) -> ThinUiValidationPath:
    steps = []
    for name in step_names:
        step_status = "ready"
        blockers: tuple[str, ...] = ()
        if name == blocked_step:
            step_status = "blocked"
            blockers = (f"{name} blocked",)
        if name == pending_step:
            step_status = "pending"
            blockers = (f"{name} pending",)
        evidence_refs = [f"R059:{name}"]
        if include_phase8_regression and name in {"library", "provenance"}:
            evidence_refs.append("R094:legacy-migration-final-writer-implementation-regression-consolidation")
        steps.append(
            ThinUiValidationStep(
                name=name,
                status=step_status,  # type: ignore[arg-type]
                entry_label=f"{name} label",
                entry_action=f"open_{name}",
                evidence_refs=tuple(evidence_refs),
                required_capabilities=(name,),
                blockers=blockers,
            )
        )
    return ThinUiValidationPath(
        status=status,  # type: ignore[arg-type]
        steps=tuple(steps),
        next_entry_action=None if status == "ready" else f"open_{blocked_step or pending_step}",
    )


def _closure(status: str = "closed") -> Phase7RegressionClosureReport:
    checks = tuple(
        Phase7RegressionClosureCheck(
            name=name,
            status="ready" if status == "closed" else "blocked",
            evidence_refs=(f"R062:{name}",),
            blockers=() if status == "closed" else (f"{name} blocked",),
        )
        for name in (
            "entry_gate",
            "health_regression",
            "recall_contract",
            "ui_validation_contract",
            "traceability_contract",
        )
    )
    return Phase7RegressionClosureReport(
        closure_id="phase7_first_regression_slice",
        status=status,  # type: ignore[arg-type]
        checks=checks,
        blocking_check_names=() if status == "closed" else ("recall_contract",),
        next_package="thin_ui_backend_contract_adapter_smoke" if status == "closed" else "recall_contract",
    )


def test_thin_ui_backend_contract_view_exposes_ready_six_step_loop() -> None:
    view = CreateThinUiBackendContractView().execute(
        validation_path=_validation_path(),
        regression_closure=_closure(),
    )

    assert view.version == "thin-ui-backend-contract.v2"
    assert view.status == "ready"
    assert view.primary_action == "open_input_intake"
    assert tuple(step.name for step in view.steps) == (
        "input",
        "library",
        "memory",
        "document",
        "qa",
        "provenance",
    )
    assert all(step.enabled for step in view.steps)
    assert view.regression.status == "closed"
    assert view.regression.next_package == "thin_ui_backend_contract_adapter_smoke"
    assert view.phase8_regression_evidence_refs == (
        "R094:legacy-migration-final-writer-implementation-regression-consolidation",
    )
    assert view.validation_refresh_evidence_refs == (
        "R095:phase9-thin-ui-validation-path-refresh",
        "R094:legacy-migration-final-writer-implementation-regression-consolidation",
    )
    assert "closure:phase7_first_regression_slice" in view.evidence_refs
    assert "R095:phase9-thin-ui-validation-path-refresh" in view.evidence_refs


def test_thin_ui_backend_contract_view_surfaces_blocked_step_without_fake_enablement() -> None:
    view = CreateThinUiBackendContractView().execute(
        validation_path=_validation_path(status="needs_attention", blocked_step="library"),
        regression_closure=_closure(),
    )

    library = next(step for step in view.steps if step.name == "library")
    assert view.status == "needs_attention"
    assert view.primary_action == "open_library"
    assert library.status == "blocked"
    assert library.enabled is False
    assert library.blockers == ("library blocked",)


def test_thin_ui_backend_contract_view_keeps_pending_step_enabled_for_review() -> None:
    view = CreateThinUiBackendContractView().execute(
        validation_path=_validation_path(status="needs_attention", pending_step="library"),
        regression_closure=_closure(),
    )

    library = next(step for step in view.steps if step.name == "library")
    assert view.status == "needs_attention"
    assert view.primary_action == "open_library"
    assert library.status == "pending"
    assert library.enabled is True
    assert library.blockers == ("library pending",)


def test_thin_ui_backend_contract_view_surfaces_regression_blocker_as_primary_action() -> None:
    view = CreateThinUiBackendContractView().execute(
        validation_path=_validation_path(),
        regression_closure=_closure("blocked"),
    )

    assert view.status == "needs_attention"
    assert view.primary_action == "inspect_recall_contract"
    assert view.regression.blocking_check_names == ("recall_contract",)


def test_thin_ui_backend_contract_view_requires_phase8_evidence_before_ready() -> None:
    with pytest.raises(ThinUiBackendAdapterError, match="Phase 8 regression evidence"):
        CreateThinUiBackendContractView().execute(
            validation_path=_validation_path(include_phase8_regression=False),
            regression_closure=_closure(),
        )


def test_thin_ui_backend_contract_view_surfaces_missing_phase8_refresh_blocker() -> None:
    view = CreateThinUiBackendContractView().execute(
        validation_path=_validation_path(
            status="needs_attention",
            pending_step="library",
            include_phase8_regression=False,
        ),
        regression_closure=_closure(),
    )

    assert view.status == "needs_attention"
    assert view.primary_action == "open_library"
    assert view.phase8_regression_evidence_refs == ()
    assert view.validation_refresh_evidence_refs == ()


def test_thin_ui_backend_contract_view_requires_expected_six_step_loop() -> None:
    with pytest.raises(ThinUiBackendAdapterError, match="expected six steps"):
        CreateThinUiBackendContractView().execute(
            validation_path=_validation_path(step_names=("input", "library", "qa")),
            regression_closure=_closure(),
        )


def test_thin_ui_backend_contract_view_requires_step_evidence() -> None:
    steps = list(_validation_path().steps)
    steps[0] = ThinUiValidationStep(
        name="input",
        status="ready",
        entry_label="input label",
        entry_action="open_input",
        evidence_refs=(),
        required_capabilities=("input",),
        blockers=(),
    )
    with pytest.raises(ThinUiBackendAdapterError, match="missing evidence refs"):
        CreateThinUiBackendContractView().execute(
            validation_path=ThinUiValidationPath("ready", tuple(steps), None),
            regression_closure=_closure(),
        )


def test_thin_ui_backend_contract_view_rejects_os_path_evidence_refs() -> None:
    steps = list(_validation_path().steps)
    steps[1] = ThinUiValidationStep(
        name="library",
        status="ready",
        entry_label="library label",
        entry_action="open_library",
        evidence_refs=(
            "R059:library",
            "R094:legacy-migration-final-writer-implementation-regression-consolidation",
            r"F:\Chriptmas_Replay\docs\product-rebuild\autopilot\validation\R094-validation.md",
        ),
        required_capabilities=("library",),
        blockers=(),
    )
    with pytest.raises(ThinUiBackendAdapterError, match="must not be OS paths"):
        CreateThinUiBackendContractView().execute(
            validation_path=ThinUiValidationPath("ready", tuple(steps), None),
            regression_closure=_closure(),
        )
