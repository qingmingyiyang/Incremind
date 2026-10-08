from __future__ import annotations

from core.product_core import (
    ConsolidatePhase6Regression,
    Phase6Readiness,
    Phase6ReadinessCheck,
    ProductHealth,
    ThinUiValidationPath,
    ThinUiValidationStep,
)
from core.product_core.health import REQUIRED_CONTRACTS


def _product_health(
    *,
    status: str = "ready",
    index_status: str = "ready",
    index_backend_kind: str | None = "sqlite_fts5",
    index_manifest_present: bool = True,
    index_entry_count: int = 4,
    index_traceable: bool = True,
    index_vector_enabled: bool = False,
    platform_status: str = "ready",
    platform_missing_capabilities: tuple[str, ...] = (),
    platform_os_path_leaks: tuple[str, ...] = (),
) -> ProductHealth:
    return ProductHealth(
        status=status,  # type: ignore[arg-type]
        contract_count=len(REQUIRED_CONTRACTS),
        missing_contracts=(),
        storage_isolated=True,
        legacy_access="disabled",
        namespace_id="default",
        storage_version=1,
        root_uri="crp://default/",
        reference_root_uri="crp-ref://default/",
        backup_ready=True,
        index_status=index_status,
        index_manifest_present=index_manifest_present,
        index_backend_kind=index_backend_kind,
        index_entry_count=index_entry_count,
        index_traceable=index_traceable,
        index_vector_enabled=index_vector_enabled,
        platform_status=platform_status,
        platform_capability_count=5,
        platform_ready_capabilities=(
            "app_data_dir",
            "backup_destination",
            "file_picker",
            "system_info",
            "worker_lifecycle",
        ),
        platform_degraded_capabilities=(),
        platform_missing_capabilities=platform_missing_capabilities,
        platform_os_path_leaks=platform_os_path_leaks,
    )


def _phase6_readiness(status: str = "ready") -> Phase6Readiness:
    return Phase6Readiness(
        status=status,  # type: ignore[arg-type]
        checks=(
            Phase6ReadinessCheck("storage_namespace_portable", status, "storage ok"),  # type: ignore[arg-type]
            Phase6ReadinessCheck("legacy_library_protected", status, "legacy ok"),  # type: ignore[arg-type]
            Phase6ReadinessCheck("platform_capability_minimum", status, "platform ok"),  # type: ignore[arg-type]
            Phase6ReadinessCheck("platform_uri_portability", status, "uri ok"),  # type: ignore[arg-type]
            Phase6ReadinessCheck("persistent_index_ready", status, "index ok"),  # type: ignore[arg-type]
        ),
        required_capabilities=(
            "app_data_dir",
            "file_picker",
            "backup_destination",
            "worker_lifecycle",
            "system_info",
        ),
        ready_capabilities=(
            "app_data_dir",
            "backup_destination",
            "file_picker",
            "system_info",
            "worker_lifecycle",
        ),
    )


def _thin_ui_path(status: str = "ready", blocked_step: str | None = None) -> ThinUiValidationPath:
    steps = []
    for name in ("input", "library", "memory", "document", "qa", "provenance"):
        step_status = "blocked" if name == blocked_step else "ready"
        steps.append(
            ThinUiValidationStep(
                name=name,
                status=step_status,  # type: ignore[arg-type]
                entry_label=f"{name} entry",
                entry_action=f"open_{name}",
                evidence_refs=(f"R059:{name}",),
                required_capabilities=(name,),
                blockers=() if step_status == "ready" else (f"{name} not ready",),
            )
        )
    return ThinUiValidationPath(
        status=status,  # type: ignore[arg-type]
        steps=tuple(steps),
        next_entry_action=None if status == "ready" else f"open_{blocked_step}",
    )


def test_phase6_regression_report_is_ready_for_hardened_phase6_surface() -> None:
    report = ConsolidatePhase6Regression().execute(
        product_health=_product_health(),
        phase6_readiness=_phase6_readiness(),
        thin_ui_validation_path=_thin_ui_path(),
    )

    assert report.status == "ready"
    assert report.blocking_check_names == ()
    assert report.next_gate == "phase7_entry"
    assert tuple(check.name for check in report.checks) == (
        "contracts",
        "storage",
        "index",
        "platform",
        "phase6_entry",
        "thin_ui_validation",
    )
    assert all(check.status == "ready" for check in report.checks)


def test_phase6_regression_blocks_vector_before_audit() -> None:
    report = ConsolidatePhase6Regression().execute(
        product_health=_product_health(index_vector_enabled=True),
        phase6_readiness=_phase6_readiness(),
        thin_ui_validation_path=_thin_ui_path(),
    )

    index = next(check for check in report.checks if check.name == "index")
    assert report.status == "needs_attention"
    assert report.blocking_check_names == ("index",)
    assert report.next_gate == "index"
    assert index.blockers == ("vector backend is enabled before audit",)


def test_phase6_regression_blocks_platform_path_leaks() -> None:
    report = ConsolidatePhase6Regression().execute(
        product_health=_product_health(
            status="degraded",
            platform_status="degraded",
            platform_os_path_leaks=("app_data_dir",),
        ),
        phase6_readiness=_phase6_readiness(),
        thin_ui_validation_path=_thin_ui_path(),
    )

    platform = next(check for check in report.checks if check.name == "platform")
    assert report.status == "needs_attention"
    assert report.next_gate == "platform"
    assert platform.blockers == (
        "platform health is not ready",
        "platform health exposes OS path leaks",
    )


def test_phase6_regression_blocks_degraded_phase6_entry_gate() -> None:
    report = ConsolidatePhase6Regression().execute(
        product_health=_product_health(),
        phase6_readiness=_phase6_readiness("degraded"),
        thin_ui_validation_path=_thin_ui_path(),
    )

    phase6_entry = next(check for check in report.checks if check.name == "phase6_entry")
    assert report.status == "needs_attention"
    assert report.next_gate == "phase6_entry"
    assert phase6_entry.blockers == (
        "phase 6 entry readiness is degraded",
        "phase 6 readiness checks are degraded",
    )


def test_phase6_regression_blocks_thin_ui_validation_gaps() -> None:
    report = ConsolidatePhase6Regression().execute(
        product_health=_product_health(),
        phase6_readiness=_phase6_readiness(),
        thin_ui_validation_path=_thin_ui_path("needs_attention", blocked_step="library"),
    )

    thin_ui = next(check for check in report.checks if check.name == "thin_ui_validation")
    assert report.status == "needs_attention"
    assert report.next_gate == "thin_ui_validation"
    assert thin_ui.blockers == (
        "thin UI validation path is not ready",
        "thin UI validation steps are not ready",
    )
