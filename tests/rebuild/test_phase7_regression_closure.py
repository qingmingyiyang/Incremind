from __future__ import annotations

from core.product_core import (
    ClosePhase7FirstRegressionSlice,
    Phase6RegressionCheck,
    Phase6RegressionReport,
    Phase7EntryCheck,
    Phase7EntryGate,
    Phase7PerformanceSample,
    ThinUiValidationPath,
    ThinUiValidationStep,
)


def _phase7_entry(status: str = "ready") -> Phase7EntryGate:
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
        performance_samples=(
            Phase7PerformanceSample("full_rebuild_tests", 6.1, 30.0, "R061:full-rebuild"),
        ),
        blocking_check_names=() if status == "ready" else ("validation_baseline",),
        next_package="phase7_first_regression_slice" if status == "ready" else "validation_baseline",
    )


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


def _thin_ui_path(
    *,
    status: str = "ready",
    blocked_step: str | None = None,
    qa_capabilities: tuple[str, ...] = ("recall_index", "answer_model_request"),
    provenance_capabilities: tuple[str, ...] = ("source_trace", "platform_health"),
) -> ThinUiValidationPath:
    steps = []
    for name in ("input", "library", "memory", "document", "qa", "provenance"):
        step_status = "blocked" if name == blocked_step else "ready"
        capabilities = (name,)
        if name == "qa":
            capabilities = qa_capabilities
        if name == "provenance":
            capabilities = provenance_capabilities
        steps.append(
            ThinUiValidationStep(
                name=name,
                status=step_status,  # type: ignore[arg-type]
                entry_label=f"{name} entry",
                entry_action=f"open_{name}",
                evidence_refs=(f"R059:{name}",),
                required_capabilities=capabilities,
                blockers=() if step_status == "ready" else (f"{name} blocked",),
            )
        )
    return ThinUiValidationPath(
        status=status,  # type: ignore[arg-type]
        steps=tuple(steps),
        next_entry_action=None if status == "ready" else f"open_{blocked_step}",
    )


def test_phase7_first_regression_slice_closes_ready_health_recall_and_ui_path() -> None:
    report = ClosePhase7FirstRegressionSlice().execute(
        phase7_entry=_phase7_entry(),
        phase6_regression=_phase6_regression(),
        thin_ui_validation_path=_thin_ui_path(),
    )

    assert report.closure_id == "phase7_first_regression_slice"
    assert report.status == "closed"
    assert report.blocking_check_names == ()
    assert report.next_package == "thin_ui_backend_contract_adapter_smoke"
    assert tuple(check.name for check in report.checks) == (
        "entry_gate",
        "health_regression",
        "recall_contract",
        "ui_validation_contract",
        "traceability_contract",
    )


def test_phase7_first_regression_slice_blocks_when_entry_gate_is_not_ready() -> None:
    report = ClosePhase7FirstRegressionSlice().execute(
        phase7_entry=_phase7_entry("blocked"),
        phase6_regression=_phase6_regression(),
        thin_ui_validation_path=_thin_ui_path(),
    )

    entry = next(check for check in report.checks if check.name == "entry_gate")
    assert report.status == "blocked"
    assert report.next_package == "entry_gate"
    assert entry.blockers == (
        "phase 7 entry gate is not ready",
        "phase 7 entry gate has blocking checks",
        "phase 7 entry gate does not open the first regression slice",
    )


def test_phase7_first_regression_slice_blocks_when_health_regression_is_not_ready() -> None:
    report = ClosePhase7FirstRegressionSlice().execute(
        phase7_entry=_phase7_entry(),
        phase6_regression=_phase6_regression("needs_attention"),
        thin_ui_validation_path=_thin_ui_path(),
    )

    health = next(check for check in report.checks if check.name == "health_regression")
    assert report.status == "blocked"
    assert report.next_package == "health_regression"
    assert health.blockers == (
        "phase 6 health regression is not ready",
        "phase 6 health regression has blocking checks",
    )


def test_phase7_first_regression_slice_blocks_when_recall_contract_is_missing() -> None:
    report = ClosePhase7FirstRegressionSlice().execute(
        phase7_entry=_phase7_entry(),
        phase6_regression=_phase6_regression(),
        thin_ui_validation_path=_thin_ui_path(qa_capabilities=("recall_index",)),
    )

    recall = next(check for check in report.checks if check.name == "recall_contract")
    assert report.status == "blocked"
    assert report.next_package == "recall_contract"
    assert recall.blockers == ("thin UI QA validation step is missing recall capabilities",)


def test_phase7_first_regression_slice_blocks_when_ui_loop_is_not_ready() -> None:
    report = ClosePhase7FirstRegressionSlice().execute(
        phase7_entry=_phase7_entry(),
        phase6_regression=_phase6_regression(),
        thin_ui_validation_path=_thin_ui_path(status="needs_attention", blocked_step="library"),
    )

    ui = next(check for check in report.checks if check.name == "ui_validation_contract")
    assert report.status == "blocked"
    assert report.next_package == "ui_validation_contract"
    assert ui.blockers == (
        "thin UI validation path is not ready",
        "thin UI validation path still has a next blocking action",
        "thin UI validation path has non-ready steps",
    )


def test_phase7_first_regression_slice_blocks_when_traceability_contract_is_missing() -> None:
    report = ClosePhase7FirstRegressionSlice().execute(
        phase7_entry=_phase7_entry(),
        phase6_regression=_phase6_regression(),
        thin_ui_validation_path=_thin_ui_path(provenance_capabilities=("platform_health",)),
    )

    traceability = next(check for check in report.checks if check.name == "traceability_contract")
    assert report.status == "blocked"
    assert report.next_package == "traceability_contract"
    assert traceability.blockers == (
        "provenance validation step is missing source trace capability",
    )
