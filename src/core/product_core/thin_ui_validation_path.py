from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import re
from typing import Literal

from .health import ProductHealth


ThinUiValidationPathStatus = Literal["ready", "needs_attention"]
ThinUiValidationStepStatus = Literal["ready", "pending", "blocked"]


@dataclass(frozen=True, slots=True)
class ThinUiValidationStep:
    name: str
    status: ThinUiValidationStepStatus
    entry_label: str
    entry_action: str
    evidence_refs: tuple[str, ...]
    required_capabilities: tuple[str, ...]
    blockers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ThinUiValidationPath:
    status: ThinUiValidationPathStatus
    steps: tuple[ThinUiValidationStep, ...]
    next_entry_action: str | None


class ThinUiValidationPathError(ValueError):
    pass


class CreateThinUiValidationPath:
    """Build the backend-backed path a thin UI may expose for validation."""

    def execute(
        self,
        product_health: ProductHealth,
        *,
        has_legacy_dry_run_report: bool,
        has_reviewed_legacy_report: bool,
        has_document_runtime: bool,
        has_memory_candidate_review: bool,
        has_recall_answer_path: bool,
        has_provenance_chain: bool,
        phase8_regression_consolidation: Mapping[str, object] | None = None,
    ) -> ThinUiValidationPath:
        phase8_refs = _phase8_regression_evidence_refs(phase8_regression_consolidation)
        steps = (
            self._input_step(product_health),
            self._library_step(
                has_legacy_dry_run_report=has_legacy_dry_run_report,
                has_reviewed_legacy_report=has_reviewed_legacy_report,
                phase8_regression_evidence_refs=phase8_refs,
            ),
            self._memory_step(has_memory_candidate_review),
            self._document_step(has_document_runtime),
            self._qa_step(product_health, has_recall_answer_path),
            self._provenance_step(
                product_health,
                has_provenance_chain,
                phase8_regression_evidence_refs=phase8_refs,
            ),
        )
        self._validate_steps(steps)
        next_step = next((step for step in steps if step.status != "ready"), None)
        return ThinUiValidationPath(
            status="ready" if next_step is None else "needs_attention",
            steps=steps,
            next_entry_action=None if next_step is None else next_step.entry_action,
        )

    def _input_step(self, product_health: ProductHealth) -> ThinUiValidationStep:
        blockers: list[str] = []
        if not product_health.storage_isolated:
            blockers.append("storage boundary is not isolated")
        if product_health.legacy_access not in {"disabled", "read_only"}:
            blockers.append("legacy access is not constrained")
        if not product_health.backup_ready:
            blockers.append("backup destination is not ready")
        return ThinUiValidationStep(
            name="input",
            status="ready" if not blockers else "blocked",
            entry_label="Import source",
            entry_action="open_input_intake",
            evidence_refs=("R004:storage-boundary", "R041:phase5-integration"),
            required_capabilities=("storage_namespace", "backup_destination"),
            blockers=tuple(blockers),
        )

    def _library_step(
        self,
        *,
        has_legacy_dry_run_report: bool,
        has_reviewed_legacy_report: bool,
        phase8_regression_evidence_refs: tuple[str, ...],
    ) -> ThinUiValidationStep:
        status: ThinUiValidationStepStatus
        blockers: tuple[str, ...]
        if has_reviewed_legacy_report:
            if phase8_regression_evidence_refs:
                status = "ready"
                blockers = ()
                entry_action = "open_legacy_dry_run"
            else:
                status = "pending"
                blockers = ("Phase 8 blocker-chain regression consolidation is missing",)
                entry_action = "open_phase8_regression_consolidation"
        elif has_legacy_dry_run_report:
            status = "pending"
            blockers = ("legacy dry-run report still needs review",)
            entry_action = "open_legacy_dry_run"
        else:
            status = "blocked"
            blockers = ("legacy library has no read-only dry-run report",)
            entry_action = "open_legacy_dry_run"
        return ThinUiValidationStep(
            name="library",
            status=status,
            entry_label="Review library plan",
            entry_action=entry_action,
            evidence_refs=(
                "R055:legacy-dry-run",
                "R058:legacy-dry-run-review",
                *phase8_regression_evidence_refs,
            ),
            required_capabilities=(
                "legacy_library_dry_run",
                "legacy_library_review",
                "phase8_blocker_chain_regression",
            ),
            blockers=blockers,
        )

    def _memory_step(self, has_memory_candidate_review: bool) -> ThinUiValidationStep:
        return ThinUiValidationStep(
            name="memory",
            status="ready" if has_memory_candidate_review else "blocked",
            entry_label="Review memory candidate",
            entry_action="open_memory_candidate_review",
            evidence_refs=("R021:memory-candidate", "R022:memory-review"),
            required_capabilities=("memory_candidate_review",),
            blockers=() if has_memory_candidate_review else ("memory candidate review path is missing",),
        )

    def _document_step(self, has_document_runtime: bool) -> ThinUiValidationStep:
        return ThinUiValidationStep(
            name="document",
            status="ready" if has_document_runtime else "blocked",
            entry_label="Open generated document",
            entry_action="open_document_handoff",
            evidence_refs=("R026:document-handoff", "R041:phase5-integration"),
            required_capabilities=("document_runtime",),
            blockers=() if has_document_runtime else ("document runtime path is missing",),
        )

    def _qa_step(
        self,
        product_health: ProductHealth,
        has_recall_answer_path: bool,
    ) -> ThinUiValidationStep:
        blockers: list[str] = []
        if product_health.index_status != "ready":
            blockers.append("recall index is not ready")
        if not product_health.index_manifest_present:
            blockers.append("recall index manifest is missing")
        if product_health.index_entry_count <= 0:
            blockers.append("recall index has no entries")
        if not has_recall_answer_path:
            blockers.append("recall answer path is missing")
        return ThinUiValidationStep(
            name="qa",
            status="ready" if not blockers else "blocked",
            entry_label="Ask from indexed sources",
            entry_action="open_qa_validation",
            evidence_refs=("R034:project-memory-recall", "R056:fts5-health"),
            required_capabilities=("recall_index", "answer_model_request"),
            blockers=tuple(blockers),
        )

    def _provenance_step(
        self,
        product_health: ProductHealth,
        has_provenance_chain: bool,
        *,
        phase8_regression_evidence_refs: tuple[str, ...],
    ) -> ThinUiValidationStep:
        blockers: list[str] = []
        if not product_health.index_traceable:
            blockers.append("recall index is not traceable")
        if product_health.platform_os_path_leaks:
            blockers.append("platform health exposes OS path leaks")
        if not has_provenance_chain:
            blockers.append("provenance chain is missing")
        if not phase8_regression_evidence_refs:
            blockers.append("Phase 8 blocker-chain regression evidence is missing")
        return ThinUiValidationStep(
            name="provenance",
            status="ready" if not blockers else "blocked",
            entry_label="Inspect source trace",
            entry_action="open_provenance_trace",
            evidence_refs=(
                "R041:phase5-integration",
                "R057:platform-recovery-resolution",
                *phase8_regression_evidence_refs,
            ),
            required_capabilities=("source_trace", "platform_health", "phase8_blocker_chain_regression"),
            blockers=tuple(blockers),
        )

    def _validate_steps(self, steps: tuple[ThinUiValidationStep, ...]) -> None:
        if len(steps) != 6:
            raise ThinUiValidationPathError("thin UI validation path must expose six steps")
        names = tuple(step.name for step in steps)
        if len(set(names)) != len(names):
            raise ThinUiValidationPathError("thin UI validation path step names must be unique")
        for step in steps:
            if not step.name or not step.entry_label or not step.entry_action:
                raise ThinUiValidationPathError("thin UI validation step is missing display metadata")
            if not step.evidence_refs:
                raise ThinUiValidationPathError("thin UI validation step is missing evidence refs")
            if step.status == "ready" and step.blockers:
                raise ThinUiValidationPathError("ready thin UI validation step cannot have blockers")


def _phase8_regression_evidence_refs(payload: Mapping[str, object] | None) -> tuple[str, ...]:
    if payload is None:
        return ()
    _validate_phase8_regression_consolidation(payload)
    return (
        "R094:legacy-migration-final-writer-implementation-regression-consolidation",
        f"{payload['id']}:phase8-regression-consolidation",
    )


def _validate_phase8_regression_consolidation(payload: Mapping[str, object]) -> None:
    if _required_str(payload, "kind") != "legacy_migration_final_writer_implementation_regression_consolidation":
        raise ThinUiValidationPathError("thin UI validation path requires Phase 8 regression consolidation")
    if _required_str(payload, "status") != "consolidated":
        raise ThinUiValidationPathError("Phase 8 regression consolidation must be consolidated")
    for key in (
        "regression_consolidation_created",
        "no_write_smoke_verified",
        "final_writer_chain_consolidated",
        "regression_passed",
    ):
        if payload.get(key) is not True:
            raise ThinUiValidationPathError("Phase 8 regression consolidation must pass")
    for key in (
        "implementation_opening_allowed",
        "writer_execution_allowed",
        "writer_implementation_allowed",
        "implementation_allowed",
        "preflight_passed",
        "dry_run_plan_created",
        "commit_allowed",
        "memory_publication_allowed",
    ):
        if payload.get(key) is not False:
            raise ThinUiValidationPathError("Phase 8 regression consolidation must keep writer blocked")
    if _required_str(payload, "required_next_gate") != "phase9_thin_ui_validation_path_refresh":
        raise ThinUiValidationPathError("Phase 8 regression consolidation must open Phase 9 refresh")
    regression = _required_mapping(payload, "regression_summary")
    if regression.get("requires_phase9_thin_ui_validation_refresh") is not True:
        raise ThinUiValidationPathError("Phase 8 regression consolidation must require Phase 9 refresh")
    observed = _required_mapping(regression, "observed_write_counts")
    for key in (
        "memory_atoms",
        "staging_atoms",
        "legacy_writes",
        "published_outputs",
        "implementation_operations",
        "commit_operations",
    ):
        if observed.get(key) != 0:
            raise ThinUiValidationPathError("Phase 8 regression consolidation must not include writes")
    evidence_refs = _required_ref_list(payload.get("evidence_refs"))
    if "R094:legacy-migration-final-writer-implementation-regression-consolidation" not in evidence_refs:
        raise ThinUiValidationPathError("Phase 8 regression consolidation evidence is missing")
    for ref in evidence_refs:
        if _looks_like_os_path(ref):
            raise ThinUiValidationPathError("Phase 8 regression consolidation refs must be portable")


def _required_mapping(mapping: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = mapping.get(key)
    if not isinstance(value, Mapping):
        raise ThinUiValidationPathError(f"{key} is required")
    return value


def _required_str(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ThinUiValidationPathError(f"{key} is required")
    return value


def _required_ref_list(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or not value:
        raise ThinUiValidationPathError("Phase 8 regression consolidation evidence refs are required")
    refs: list[str] = []
    for ref in value:
        if not isinstance(ref, str) or not ref:
            raise ThinUiValidationPathError("Phase 8 regression consolidation evidence refs must be strings")
        refs.append(ref)
    return tuple(refs)


def _looks_like_os_path(value: str) -> bool:
    return bool(re.search(r"(^[A-Za-z]:[\\/])|(^[\\/])|(file://)", value))
