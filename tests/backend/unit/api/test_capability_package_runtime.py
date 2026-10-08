from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from backend.api.capability_package_runtime import (
    CapabilityPackageCatalogConflict,
    compile_capability_package_contributions,
    compile_context_graph_adapter_registry,
    compose_capability_package_catalog,
    reconcile_bundled_capability_packages,
)
from core.context_graph import CapabilityPackageLoader


def _manifest(root: Path, package_id: str, revision: str, *, display_name: str = "Fixture") -> None:
    package = root / package_id
    package.mkdir(parents=True, exist_ok=True)
    (package / "manifest.json").write_text(json.dumps({
        "schema_version": "1.0.0", "core_api": "1", "capability_id": package_id,
        "capability_revision": revision, "display_name": display_name, "kind": "read_only_view",
        "execution_state_owner": "core_effect_log", "recovery_owner": "core_reaper",
        "secret_access": "lease_reference_only", "memory_write": "proposal_only",
        "document_write": "draft_only", "project_skill_write": "proposal_only",
        "contributions": ["read_only_preview"], "provides": {"views": ["fixture"]},
        "tools": [], "workflows": [],
        "permissions": {"net": [], "fs": [], "secrets": []},
        "budgets": {"max_bytes": 1024, "max_seconds": 5}, "effects": {},
        "ui": {"label": "Fixture", "icon": "fixture", "progress_steps": []},
        "tests": ["tests/contract_test.py"],
    }), encoding="utf-8")
    tests = package / "tests"
    tests.mkdir(exist_ok=True)
    (tests / "contract_test.py").write_text(
        "def test_contract():\n    pass\n", encoding="utf-8"
    )


def test_production_composition_persists_bundled_catalog(tmp_path: Path) -> None:
    catalog = compose_capability_package_catalog(tmp_path)
    ids = {item.capability_id for item in catalog.active()}

    assert {"thought_graph_context", "timeline_preview"}.issubset(ids)
    restarted = CapabilityPackageLoader(
        tmp_path / ".rebuild-data" / "capability-packages.sqlite3"
    )
    assert {item.capability_id for item in restarted.active()} == ids


def test_production_composition_restarts_with_same_manifest_and_bound_artifact(
    tmp_path: Path,
) -> None:
    first = compose_capability_package_catalog(tmp_path)
    before = {
        item.capability_id: (item.capability_revision, item.artifact_id)
        for item in first.active()
    }

    restarted = compose_capability_package_catalog(tmp_path)

    after = {
        item.capability_id: (item.capability_revision, item.artifact_id)
        for item in restarted.active()
    }
    assert after == before
    assert all(artifact_id for _revision, artifact_id in after.values())


def test_new_package_contribution_is_loaded_without_capability_specific_core_branch(
    tmp_path: Path,
) -> None:
    catalog = compose_capability_package_catalog(tmp_path)

    contributions = compile_capability_package_contributions(catalog)
    preview = contributions.tools["timeline.preview"](
        ({"ref": "crp://event/1", "title": "One", "occurred_at": "1"},)
    )

    assert preview == (
        {"ref": "crp://event/1", "title": "One", "occurred_at": "1"},
    )
    assert contributions.workflows == {}


def test_linemap_context_contributions_are_resolved_from_manifest_without_core_format_branch(
    tmp_path: Path,
) -> None:
    catalog = compose_capability_package_catalog(tmp_path)

    contributions = compile_capability_package_contributions(catalog)
    thoughtdag = contributions.context["context.import.thoughtdag"]()
    markdown = contributions.context["context.import.markdown_graph"]()
    handoff = contributions.context["context.proposal.handoff"]()
    model_runner = contributions.context["context.evaluate.model_runner"]
    model_suite_definition = contributions.context[
        "context.evaluate.model_suite_definition"
    ]()

    assert thoughtdag.importer_id.endswith("ThoughtDAGImporter")
    assert markdown.importer_id.endswith("MarkdownGraphImporter")
    assert type(handoff).__name__ == "PlatformProposalHandoffAdapter"
    assert model_runner.__name__ == "run_model_benchmark"
    assert set(model_suite_definition) == {
        "schema_version", "case_builder", "pair_builder", "case_scorer",
        "suite_scorer",
    }
    assert model_suite_definition["schema_version"] == "1.0.0"
    assert all(
        callable(model_suite_definition[name])
        for name in ("case_builder", "pair_builder", "case_scorer", "suite_scorer")
    )


def test_active_importer_declarations_compile_into_generic_source_type_registry(
    tmp_path: Path,
) -> None:
    catalog = compose_capability_package_catalog(tmp_path)

    registry = compile_context_graph_adapter_registry(catalog)

    assert registry.registered()["importers"] == ("markdown", "thoughtdag")
    assert registry.importer("thoughtdag").importer_id.endswith("ThoughtDAGImporter")
    assert registry.importer("markdown").importer_id.endswith("MarkdownGraphImporter")
    registration = registry.resolve("thoughtdag")
    assert registration is not None
    assert registration.source_type == "thoughtdag"
    assert registration.contribution_id == "context.import.thoughtdag"
    assert registration.capability_id == "thought_graph_context"
    assert registration.capability_revision == "4.2.0"


def test_context_registry_reuses_supplied_compiled_contributions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = compose_capability_package_catalog(tmp_path)
    contributions = compile_capability_package_contributions(catalog)

    def should_not_compile(_loader):
        raise AssertionError("context registry recompiled package contributions")

    monkeypatch.setattr(
        "backend.api.capability_package_runtime.compile_capability_package_contributions",
        should_not_compile,
    )

    registry = compile_context_graph_adapter_registry(
        catalog, contributions,
    )

    assert registry.registered()["importers"] == ("markdown", "thoughtdag")


def test_uninstalled_capability_cannot_supply_importer_to_generic_registry(tmp_path: Path) -> None:
    catalog = compose_capability_package_catalog(tmp_path)
    live_registry = compile_context_graph_adapter_registry(catalog)
    current = next(
        item for item in catalog.active()
        if item.capability_id == "thought_graph_context"
    )
    catalog.uninstall(current.capability_id, expected_revision=current.capability_revision)

    assert live_registry.resolve("thoughtdag") is None

    registry = compile_context_graph_adapter_registry(catalog)

    assert registry.registered()["importers"] == ()
    with pytest.raises(ValueError, match="importer_not_registered"):
        registry.importer("thoughtdag")


def test_uninstall_removes_all_linemap_context_entrypoints_from_compiled_catalog(
    tmp_path: Path,
) -> None:
    catalog = compose_capability_package_catalog(tmp_path)
    current = next(
        item for item in catalog.active()
        if item.capability_id == "thought_graph_context"
    )

    catalog.uninstall(
        current.capability_id, expected_revision=current.capability_revision,
    )
    contributions = compile_capability_package_contributions(catalog)

    assert not any(key.startswith("context.") for key in contributions.context)
    assert "timeline.preview" in contributions.tools


def test_reconcile_upgrades_rolls_back_and_uninstalls_atomically(tmp_path: Path) -> None:
    packages = tmp_path / "packages"
    database = tmp_path / "catalog.sqlite3"
    _manifest(packages, "fixture_package", "1.0.0")
    loader = CapabilityPackageLoader(database)
    reconcile_bundled_capability_packages(loader, packages)

    _manifest(packages, "fixture_package", "2.0.0")
    reconcile_bundled_capability_packages(CapabilityPackageLoader(database), packages)
    assert CapabilityPackageLoader(database).active()[0].capability_revision == "2.0.0"

    _manifest(packages, "fixture_package", "1.0.0")
    reconcile_bundled_capability_packages(CapabilityPackageLoader(database), packages)
    assert CapabilityPackageLoader(database).active()[0].capability_revision == "1.0.0"

    (packages / "fixture_package" / "manifest.json").unlink()
    reconcile_bundled_capability_packages(CapabilityPackageLoader(database), packages)
    assert CapabilityPackageLoader(database).active() == ()


def test_same_revision_manifest_drift_fails_closed(tmp_path: Path) -> None:
    packages = tmp_path / "packages"
    database = tmp_path / "catalog.sqlite3"
    _manifest(packages, "fixture_package", "1.0.0")
    reconcile_bundled_capability_packages(CapabilityPackageLoader(database), packages)
    _manifest(packages, "fixture_package", "1.0.0", display_name="Changed")

    with pytest.raises(CapabilityPackageCatalogConflict, match="without revision"):
        reconcile_bundled_capability_packages(CapabilityPackageLoader(database), packages)

    assert CapabilityPackageLoader(database).active()[0].display_name == "Fixture"


def test_two_startup_reconcilers_converge_on_one_active_pointer(tmp_path: Path) -> None:
    packages = tmp_path / "packages"
    database = tmp_path / "catalog.sqlite3"
    _manifest(packages, "fixture_package", "1.0.0")
    loaders = (CapabilityPackageLoader(database), CapabilityPackageLoader(database))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(
            pool.map(lambda loader: reconcile_bundled_capability_packages(loader, packages), loaders)
        )

    assert all(result[0].capability_revision == "1.0.0" for result in results)
    assert len(CapabilityPackageLoader(database).history("fixture_package")) == 1


def test_uninstalled_revision_stays_disabled_until_explicit_reinstall_without_duplicate_history(
    tmp_path: Path,
) -> None:
    packages = tmp_path / "packages"
    database = tmp_path / "catalog.sqlite3"
    _manifest(packages, "fixture_package", "1.0.0")
    loader = CapabilityPackageLoader(database)
    reconcile_bundled_capability_packages(loader, packages)
    loader.uninstall("fixture_package", expected_revision="1.0.0")

    reconcile_bundled_capability_packages(CapabilityPackageLoader(database), packages)

    restarted = CapabilityPackageLoader(database)
    assert restarted.active() == ()
    assert restarted.desired_state("fixture_package").state == "disabled"
    restarted.install(restarted.load_manifest(packages / "fixture_package" / "manifest.json"))

    enabled = CapabilityPackageLoader(database)
    assert enabled.active()[0].capability_revision == "1.0.0"
    assert enabled.desired_state("fixture_package").state == "enabled"
    assert len(enabled.history("fixture_package")) == 1


def test_upgrade_transaction_crash_leaves_previous_pointer_and_history(tmp_path: Path) -> None:
    packages = tmp_path / "packages"
    database = tmp_path / "catalog.sqlite3"
    _manifest(packages, "fixture_package", "1.0.0")
    reconcile_bundled_capability_packages(CapabilityPackageLoader(database), packages)
    _manifest(packages, "fixture_package", "2.0.0")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TRIGGER fail_capability_pointer_update "
            "BEFORE UPDATE ON capability_package_active "
            "BEGIN SELECT RAISE(ABORT, 'injected crash'); END"
        )

    with pytest.raises(sqlite3.DatabaseError, match="injected crash"):
        reconcile_bundled_capability_packages(CapabilityPackageLoader(database), packages)

    restarted = CapabilityPackageLoader(database)
    assert restarted.active()[0].capability_revision == "1.0.0"
    assert [item.capability_revision for item in restarted.history("fixture_package")] == [
        "1.0.0",
    ]


def test_application_rollback_walks_multiple_recorded_revisions(tmp_path: Path) -> None:
    packages = tmp_path / "packages"
    database = tmp_path / "catalog.sqlite3"
    for revision in ("1.0.0", "2.0.0", "3.0.0"):
        _manifest(packages, "fixture_package", revision)
        reconcile_bundled_capability_packages(CapabilityPackageLoader(database), packages)

    _manifest(packages, "fixture_package", "1.0.0")
    reconcile_bundled_capability_packages(CapabilityPackageLoader(database), packages)

    assert CapabilityPackageLoader(database).active()[0].capability_revision == "1.0.0"
