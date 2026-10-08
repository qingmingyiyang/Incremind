from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .health import ProductHealth, REQUIRED_CONTRACTS
from .phase6_readiness import Phase6Readiness
from .thin_ui_validation_path import ThinUiValidationPath


Phase6RegressionStatus = Literal["ready", "needs_attention"]
Phase6RegressionCheckStatus = Literal["ready", "blocked"]


@dataclass(frozen=True, slots=True)
class Phase6RegressionCheck:
    name: str
    status: Phase6RegressionCheckStatus
    evidence_refs: tuple[str, ...]
    blockers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Phase6RegressionReport:
    status: Phase6RegressionStatus
    checks: tuple[Phase6RegressionCheck, ...]
    blocking_check_names: tuple[str, ...]
    next_gate: str | None


class Phase6RegressionError(ValueError):
    pass


class ConsolidatePhase6Regression:
    """Consolidate Phase 6 hardening into one executable regression view."""

    def execute(
        self,
        *,
        product_health: ProductHealth,
        phase6_readiness: Phase6Readiness,
        thin_ui_validation_path: ThinUiValidationPath,
    ) -> Phase6RegressionReport:
        checks = (
            self._contract_check(product_health),
            self._storage_check(product_health),
            self._index_check(product_health),
            self._platform_check(product_health),
            self._phase6_entry_check(phase6_readiness),
            self._thin_ui_check(thin_ui_validation_path),
        )
        self._validate_checks(checks)
        blocking_names = tuple(check.name for check in checks if check.status != "ready")
        return Phase6RegressionReport(
            status="ready" if not blocking_names else "needs_attention",
            checks=checks,
            blocking_check_names=blocking_names,
            next_gate="phase7_entry" if not blocking_names else blocking_names[0],
        )

    def _contract_check(self, product_health: ProductHealth) -> Phase6RegressionCheck:
        blockers: list[str] = []
        if product_health.contract_count < len(REQUIRED_CONTRACTS):
            blockers.append("contract coverage is below required rebuild schema count")
        if product_health.missing_contracts:
            blockers.append("required rebuild contracts are missing")
        return _check(
            "contracts",
            blockers=blockers,
            evidence_refs=("R001-R012:contracts", "R060:phase6-regression"),
        )

    def _storage_check(self, product_health: ProductHealth) -> Phase6RegressionCheck:
        blockers: list[str] = []
        if not product_health.storage_isolated:
            blockers.append("storage boundary is not isolated")
        if product_health.legacy_access not in {"disabled", "read_only"}:
            blockers.append("legacy access is not disabled or read-only")
        if not product_health.backup_ready:
            blockers.append("backup destination is not ready")
        if not product_health.root_uri.startswith(f"crp://{product_health.namespace_id}/"):
            blockers.append("root uri is not namespace-scoped")
        if not product_health.reference_root_uri.startswith(f"crp-ref://{product_health.namespace_id}/"):
            blockers.append("reference root uri is not namespace-scoped")
        return _check(
            "storage",
            blockers=blockers,
            evidence_refs=("R005:storage-namespace", "R044:phase6-readiness"),
        )

    def _index_check(self, product_health: ProductHealth) -> Phase6RegressionCheck:
        blockers: list[str] = []
        if product_health.index_status != "ready":
            blockers.append("index health is not ready")
        if not product_health.index_manifest_present:
            blockers.append("index manifest is missing")
        if product_health.index_backend_kind not in {"object_store_lexical", "sqlite_fts5"}:
            blockers.append("index backend is not an accepted local backend")
        if product_health.index_entry_count <= 0:
            blockers.append("index has no traceable entries")
        if not product_health.index_traceable:
            blockers.append("index is not traceable")
        if product_health.index_vector_enabled:
            blockers.append("vector backend is enabled before audit")
        return _check(
            "index",
            blockers=blockers,
            evidence_refs=("R040:recall-index-boundary", "R056:fts5-health"),
        )

    def _platform_check(self, product_health: ProductHealth) -> Phase6RegressionCheck:
        blockers: list[str] = []
        if product_health.platform_status != "ready":
            blockers.append("platform health is not ready")
        if product_health.platform_missing_capabilities:
            blockers.append("platform capabilities are missing")
        if product_health.platform_os_path_leaks:
            blockers.append("platform health exposes OS path leaks")
        return _check(
            "platform",
            blockers=blockers,
            evidence_refs=("R047:platform-health", "R057:platform-recovery-resolution"),
        )

    def _phase6_entry_check(self, phase6_readiness: Phase6Readiness) -> Phase6RegressionCheck:
        blockers: list[str] = []
        if phase6_readiness.status != "ready":
            blockers.append("phase 6 entry readiness is degraded")
        degraded_checks = tuple(
            check.name for check in phase6_readiness.checks if check.status != "ready"
        )
        if degraded_checks:
            blockers.append("phase 6 readiness checks are degraded")
        return _check(
            "phase6_entry",
            blockers=blockers,
            evidence_refs=("R044:phase6-readiness", "R060:phase6-regression"),
        )

    def _thin_ui_check(
        self,
        thin_ui_validation_path: ThinUiValidationPath,
    ) -> Phase6RegressionCheck:
        blockers: list[str] = []
        if thin_ui_validation_path.status != "ready":
            blockers.append("thin UI validation path is not ready")
        blocked_steps = tuple(
            step.name for step in thin_ui_validation_path.steps if step.status != "ready"
        )
        if blocked_steps:
            blockers.append("thin UI validation steps are not ready")
        return _check(
            "thin_ui_validation",
            blockers=blockers,
            evidence_refs=("R059:thin-ui-validation", "R060:phase6-regression"),
        )

    def _validate_checks(self, checks: tuple[Phase6RegressionCheck, ...]) -> None:
        expected = ("contracts", "storage", "index", "platform", "phase6_entry", "thin_ui_validation")
        names = tuple(check.name for check in checks)
        if names != expected:
            raise Phase6RegressionError("phase 6 regression checks must keep the expected order")
        for check in checks:
            if not check.evidence_refs:
                raise Phase6RegressionError("phase 6 regression check is missing evidence refs")
            if check.status == "ready" and check.blockers:
                raise Phase6RegressionError("ready phase 6 regression check cannot have blockers")


def _check(
    name: str,
    *,
    blockers: list[str],
    evidence_refs: tuple[str, ...],
) -> Phase6RegressionCheck:
    return Phase6RegressionCheck(
        name=name,
        status="ready" if not blockers else "blocked",
        evidence_refs=evidence_refs,
        blockers=tuple(blockers),
    )
