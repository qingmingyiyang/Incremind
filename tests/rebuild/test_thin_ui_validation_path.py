from __future__ import annotations

import pytest

from core.product_core import CreateThinUiValidationPath, ProductHealth


def _product_health(
    *,
    status: str = "ready",
    storage_isolated: bool = True,
    legacy_access: str = "disabled",
    backup_ready: bool = True,
    index_status: str = "ready",
    index_manifest_present: bool = True,
    index_entry_count: int = 4,
    index_traceable: bool = True,
    platform_os_path_leaks: tuple[str, ...] = (),
) -> ProductHealth:
    return ProductHealth(
        status=status,  # type: ignore[arg-type]
        contract_count=21,
        missing_contracts=(),
        storage_isolated=storage_isolated,
        legacy_access=legacy_access,  # type: ignore[arg-type]
        namespace_id="default",
        storage_version=1,
        root_uri="crp://default/",
        reference_root_uri="crp-ref://default/",
        backup_ready=backup_ready,
        index_status=index_status,
        index_manifest_present=index_manifest_present,
        index_backend_kind="sqlite_fts5",
        index_entry_count=index_entry_count,
        index_traceable=index_traceable,
        index_vector_enabled=False,
        platform_status="ready",
        platform_capability_count=5,
        platform_ready_capabilities=(
            "app_data_dir",
            "backup_destination",
            "file_picker",
            "system_info",
            "worker_lifecycle",
        ),
        platform_degraded_capabilities=(),
        platform_missing_capabilities=(),
        platform_os_path_leaks=platform_os_path_leaks,
    )


def test_thin_ui_validation_path_is_ready_when_backend_slices_are_available() -> None:
    path = CreateThinUiValidationPath().execute(
        _product_health(),
        has_legacy_dry_run_report=True,
        has_reviewed_legacy_report=True,
        has_document_runtime=True,
        has_memory_candidate_review=True,
        has_recall_answer_path=True,
        has_provenance_chain=True,
        phase8_regression_consolidation=_phase8_regression_consolidation(),
    )

    assert path.status == "ready"
    assert path.next_entry_action is None
    assert tuple(step.name for step in path.steps) == (
        "input",
        "library",
        "memory",
        "document",
        "qa",
        "provenance",
    )
    assert all(step.status == "ready" for step in path.steps)
    assert all(step.evidence_refs for step in path.steps)
    assert all(step.entry_action for step in path.steps)
    library = next(step for step in path.steps if step.name == "library")
    provenance = next(step for step in path.steps if step.name == "provenance")
    assert "R094:legacy-migration-final-writer-implementation-regression-consolidation" in library.evidence_refs
    assert "R094:legacy-migration-final-writer-implementation-regression-consolidation" in provenance.evidence_refs


def test_thin_ui_validation_path_requires_phase8_regression_before_ready() -> None:
    path = CreateThinUiValidationPath().execute(
        _product_health(),
        has_legacy_dry_run_report=True,
        has_reviewed_legacy_report=True,
        has_document_runtime=True,
        has_memory_candidate_review=True,
        has_recall_answer_path=True,
        has_provenance_chain=True,
    )

    library = next(step for step in path.steps if step.name == "library")
    provenance = next(step for step in path.steps if step.name == "provenance")
    assert path.status == "needs_attention"
    assert path.next_entry_action == "open_phase8_regression_consolidation"
    assert library.status == "pending"
    assert library.blockers == ("Phase 8 blocker-chain regression consolidation is missing",)
    assert provenance.status == "blocked"
    assert provenance.blockers == ("Phase 8 blocker-chain regression evidence is missing",)


def test_thin_ui_validation_path_blocks_empty_shell_without_library_report() -> None:
    path = CreateThinUiValidationPath().execute(
        _product_health(),
        has_legacy_dry_run_report=False,
        has_reviewed_legacy_report=False,
        has_document_runtime=True,
        has_memory_candidate_review=True,
        has_recall_answer_path=True,
        has_provenance_chain=True,
        phase8_regression_consolidation=_phase8_regression_consolidation(),
    )

    library = next(step for step in path.steps if step.name == "library")
    assert path.status == "needs_attention"
    assert path.next_entry_action == "open_legacy_dry_run"
    assert library.status == "blocked"
    assert library.blockers == ("legacy library has no read-only dry-run report",)


def test_thin_ui_validation_path_keeps_library_pending_until_dry_run_is_reviewed() -> None:
    path = CreateThinUiValidationPath().execute(
        _product_health(),
        has_legacy_dry_run_report=True,
        has_reviewed_legacy_report=False,
        has_document_runtime=True,
        has_memory_candidate_review=True,
        has_recall_answer_path=True,
        has_provenance_chain=True,
        phase8_regression_consolidation=_phase8_regression_consolidation(),
    )

    library = next(step for step in path.steps if step.name == "library")
    assert path.status == "needs_attention"
    assert path.next_entry_action == "open_legacy_dry_run"
    assert library.status == "pending"
    assert library.blockers == ("legacy dry-run report still needs review",)


def test_thin_ui_validation_path_blocks_qa_when_index_is_unavailable() -> None:
    path = CreateThinUiValidationPath().execute(
        _product_health(index_status="degraded", index_manifest_present=False, index_entry_count=0),
        has_legacy_dry_run_report=True,
        has_reviewed_legacy_report=True,
        has_document_runtime=True,
        has_memory_candidate_review=True,
        has_recall_answer_path=True,
        has_provenance_chain=True,
        phase8_regression_consolidation=_phase8_regression_consolidation(),
    )

    qa = next(step for step in path.steps if step.name == "qa")
    assert path.status == "needs_attention"
    assert path.next_entry_action == "open_qa_validation"
    assert qa.status == "blocked"
    assert qa.blockers == (
        "recall index is not ready",
        "recall index manifest is missing",
        "recall index has no entries",
    )


def test_thin_ui_validation_path_blocks_provenance_for_path_leak_or_untraceable_index() -> None:
    path = CreateThinUiValidationPath().execute(
        _product_health(index_traceable=False, platform_os_path_leaks=("C:\\Users\\demo",)),
        has_legacy_dry_run_report=True,
        has_reviewed_legacy_report=True,
        has_document_runtime=True,
        has_memory_candidate_review=True,
        has_recall_answer_path=True,
        has_provenance_chain=True,
        phase8_regression_consolidation=_phase8_regression_consolidation(),
    )

    provenance = next(step for step in path.steps if step.name == "provenance")
    assert path.status == "needs_attention"
    assert path.next_entry_action == "open_provenance_trace"
    assert provenance.status == "blocked"
    assert provenance.blockers == (
        "recall index is not traceable",
        "platform health exposes OS path leaks",
    )


def test_thin_ui_validation_path_rejects_unsafe_phase8_regression() -> None:
    with pytest.raises(ValueError, match="keep writer blocked"):
        CreateThinUiValidationPath().execute(
            _product_health(),
            has_legacy_dry_run_report=True,
            has_reviewed_legacy_report=True,
            has_document_runtime=True,
            has_memory_candidate_review=True,
            has_recall_answer_path=True,
            has_provenance_chain=True,
            phase8_regression_consolidation={
                **_phase8_regression_consolidation(),
                "implementation_opening_allowed": True,
            },
        )

    with pytest.raises(ValueError, match="must not include writes"):
        CreateThinUiValidationPath().execute(
            _product_health(),
            has_legacy_dry_run_report=True,
            has_reviewed_legacy_report=True,
            has_document_runtime=True,
            has_memory_candidate_review=True,
            has_recall_answer_path=True,
            has_provenance_chain=True,
            phase8_regression_consolidation={
                **_phase8_regression_consolidation(),
                "regression_summary": {
                    **_phase8_regression_consolidation()["regression_summary"],
                    "observed_write_counts": {
                        **_phase8_regression_consolidation()["regression_summary"]["observed_write_counts"],
                        "memory_atoms": 1,
                    },
                },
            },
        )

    with pytest.raises(ValueError, match="refs must be portable"):
        CreateThinUiValidationPath().execute(
            _product_health(),
            has_legacy_dry_run_report=True,
            has_reviewed_legacy_report=True,
            has_document_runtime=True,
            has_memory_candidate_review=True,
            has_recall_answer_path=True,
            has_provenance_chain=True,
            phase8_regression_consolidation={
                **_phase8_regression_consolidation(),
                "evidence_refs": [
                    *_phase8_regression_consolidation()["evidence_refs"],
                    "C:\\library\\notes.md",
                ],
            },
        )


def _phase8_regression_consolidation() -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "kind": "legacy_migration_final_writer_implementation_regression_consolidation",
        "id": "legacy-writer-implementation-regression-consolidation-test",
        "status": "consolidated",
        "regression_consolidation_created": True,
        "no_write_smoke_verified": True,
        "final_writer_chain_consolidated": True,
        "regression_passed": True,
        "implementation_opening_allowed": False,
        "writer_execution_allowed": False,
        "writer_implementation_allowed": False,
        "implementation_allowed": False,
        "preflight_passed": False,
        "dry_run_plan_created": False,
        "commit_allowed": False,
        "memory_publication_allowed": False,
        "required_next_gate": "phase9_thin_ui_validation_path_refresh",
        "regression_summary": {
            "requires_phase9_thin_ui_validation_refresh": True,
            "observed_write_counts": {
                "memory_atoms": 0,
                "staging_atoms": 0,
                "legacy_writes": 0,
                "published_outputs": 0,
                "implementation_operations": 0,
                "commit_operations": 0,
            },
        },
        "evidence_refs": (
            "R094:legacy-migration-final-writer-implementation-regression-consolidation",
            "legacy-writer-implementation-regression-consolidation-test:final-writer-regression-consolidated",
        ),
    }
