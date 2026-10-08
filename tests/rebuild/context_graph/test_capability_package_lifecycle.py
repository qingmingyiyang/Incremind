from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from core.context_graph import CapabilityPackageError, CapabilityPackageLoader, ContextCompiler
from backend.api.capability_package_runtime import reconcile_bundled_capability_packages


MANIFEST = {
    "schema_version": "1.0.0", "core_api": "1", "capability_id": "fixture_graph",
    "capability_revision": "1.0.0", "display_name": "Fixture Graph",
    "kind": "context_extension", "execution_state_owner": "core_effect_log",
    "recovery_owner": "core_reaper", "secret_access": "lease_reference_only",
    "memory_write": "proposal_only", "document_write": "draft_only",
    "project_skill_write": "proposal_only", "contributions": ["importer", "read_only_preview"],
    "provides": {"context_formats": ["fixture"]}, "tools": [], "workflows": [],
    "permissions": {"net": [], "fs": [], "secrets": []},
    "budgets": {"max_bytes": 1024, "max_seconds": 5}, "effects": {},
    "ui": {"label": "Fixture", "icon": "fixture", "progress_steps": []},
    "tests": ["tests/contract_test.py"],
}

IMPORTER_CONTEXT = {
    "id": "context.import.fixture", "kind": "importer",
    "source_type": "fixture", "entrypoint": "tests/contract_test.py:test_contract",
}


def _manifest(tmp_path: Path, payload: dict) -> Path:
    package = tmp_path / f"fixture_graph_{payload.get('capability_revision', 'bad')}"
    package.mkdir(parents=True, exist_ok=True)
    path = package / "manifest.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    contract = package / "tests" / "contract_test.py"
    contract.parent.mkdir(exist_ok=True)
    contract.write_text("def test_contract():\n    pass\n", encoding="utf-8")
    return path


def _bundled_manifest(root: Path, revision: str = "1.0.0") -> Path:
    package = root / "fixture_graph"
    package.mkdir(parents=True, exist_ok=True)
    payload = {**MANIFEST, "capability_revision": revision}
    path = package / "manifest.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    contract = package / "tests" / "contract_test.py"
    contract.parent.mkdir(exist_ok=True)
    contract.write_text("def test_contract():\n    pass\n", encoding="utf-8")
    return path


def test_install_upgrade_rollback_uninstall_and_core_survival(tmp_path: Path) -> None:
    loader = CapabilityPackageLoader()
    first = loader.load_manifest(_manifest(tmp_path, MANIFEST))
    loader.install(first)
    second = loader.load_manifest(_manifest(tmp_path, {**MANIFEST, "capability_revision": "2.0.0"}))
    loader.upgrade(second, expected_revision="1.0.0")
    assert loader.active()[0].capability_revision == "2.0.0"
    assert loader.rollback("fixture_graph", expected_revision="2.0.0").capability_revision == "1.0.0"
    loader.uninstall("fixture_graph", expected_revision="1.0.0")
    assert loader.active() == ()
    assert ContextCompiler.compiler_revision == "2.0.0"


@pytest.mark.parametrize(
    "change, code",
    [
        ({"recovery_owner": "private_reaper"}, "forbidden_capability_authority"),
        ({"secret_access": "plaintext"}, "forbidden_capability_authority"),
        ({"memory_write": "formal"}, "forbidden_capability_authority"),
        ({"contributions": ["private_runtime"]}, "unknown_contribution"),
        ({"tools": [{"id": "fixture.preview", "exposure": "internal", "contributes": "private_runtime", "handler": "tests/contract_test.py:test_contract"}]}, "undeclared_tool_contribution"),
        ({"schema_version": "3.0.0"}, "unsupported_manifest_version"),
        ({"core_api": "3"}, "unsupported_core_api"),
    ],
)
def test_forbidden_package_authority_fails_closed(tmp_path: Path, change: dict, code: str) -> None:
    loader = CapabilityPackageLoader()
    with pytest.raises(CapabilityPackageError, match=code):
        loader.load_manifest(_manifest(tmp_path, {**MANIFEST, **change}))


def test_revision_drift_fails_closed(tmp_path: Path) -> None:
    loader = CapabilityPackageLoader()
    first = loader.install(loader.load_manifest(_manifest(tmp_path, MANIFEST)))
    second = replace(first, capability_revision="2.0.0")
    with pytest.raises(CapabilityPackageError, match="revision_drift"):
        loader.upgrade(second, expected_revision="stale")


def test_core_api_v2_package_is_accepted_without_retiring_v1_packages(tmp_path: Path) -> None:
    loader = CapabilityPackageLoader()
    first = loader.load_manifest(_manifest(tmp_path, MANIFEST))
    second = loader.load_manifest(_manifest(
        tmp_path,
        {**MANIFEST, "capability_revision": "2.0.0", "core_api": "2"},
    ))

    assert first.core_api == "1"
    assert second.core_api == "2"


@pytest.mark.parametrize(
    "context, code",
    [
        ([{key: value for key, value in IMPORTER_CONTEXT.items() if key != "source_type"}], "invalid_context_contribution"),
        ([{**IMPORTER_CONTEXT, "source_type": "bad-source"}], "invalid_context_importer_source_type"),
        ([IMPORTER_CONTEXT, {**IMPORTER_CONTEXT, "id": "context.import.fixture_copy"}], "duplicate_context_importer_source_type"),
        ([{**IMPORTER_CONTEXT, "unexpected": "value"}], "invalid_context_contribution"),
        ([{"id": "context.preview", "kind": "read_only_preview", "entrypoint": "tests/contract_test.py:test_contract", "source_type": "fixture"}], "invalid_context_contribution"),
    ],
)
def test_context_importer_declarations_fail_closed_on_missing_unknown_or_duplicate_source_type(
    tmp_path: Path, context: list[dict], code: str,
) -> None:
    loader = CapabilityPackageLoader()
    payload = {**MANIFEST, "context": context}

    with pytest.raises(CapabilityPackageError, match=code):
        loader.load_manifest(_manifest(tmp_path, payload))


def test_persisted_legacy_importer_is_readable_but_inactive_until_strict_upgrade(
    tmp_path: Path,
) -> None:
    database = tmp_path / "capability-packages.sqlite3"
    CapabilityPackageLoader(database)
    legacy = {
        **MANIFEST,
        "capability_revision": "0.9.0",
        "context": [{key: value for key, value in IMPORTER_CONTEXT.items() if key != "source_type"}],
    }
    encoded = json.dumps(legacy, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO capability_package_revisions"
            "(capability_id, capability_revision, manifest_json, sequence) VALUES (?, ?, ?, ?)",
            ("fixture_graph", "0.9.0", encoded, 1),
        )
        connection.execute(
            "INSERT INTO capability_package_active(capability_id, capability_revision) VALUES (?, ?)",
            ("fixture_graph", "0.9.0"),
        )

    restarted = CapabilityPackageLoader(database)
    # Pre-artifact revisions remain auditable history but cannot be activated
    # or loaded from a mutable live package tree after restart.
    assert restarted.active() == ()
    strict_path = _bundled_manifest(tmp_path / "strict")
    strict_path.write_text(json.dumps({**MANIFEST, "context": [IMPORTER_CONTEXT]}), encoding="utf-8")
    strict = restarted.load_manifest(strict_path)
    restarted.install(strict)

    assert CapabilityPackageLoader(database).active()[0].context[0]["source_type"] == "fixture"


def test_durable_lifecycle_survives_restart_upgrade_rollback_and_uninstall(tmp_path: Path) -> None:
    database = tmp_path / "capability-packages.sqlite3"
    first_path = _manifest(tmp_path, MANIFEST)
    second_path = _manifest(tmp_path, {**MANIFEST, "capability_revision": "2.0.0"})

    loader = CapabilityPackageLoader(database)
    loader.install(loader.load_manifest(first_path))
    restarted = CapabilityPackageLoader(database)
    assert restarted.active()[0].capability_revision == "1.0.0"

    restarted.upgrade(restarted.load_manifest(second_path), expected_revision="1.0.0")
    upgraded = CapabilityPackageLoader(database)
    assert upgraded.active()[0].capability_revision == "2.0.0"
    assert [item.capability_revision for item in upgraded.history("fixture_graph")] == [
        "1.0.0", "2.0.0",
    ]

    upgraded.rollback("fixture_graph", expected_revision="2.0.0")
    rolled_back = CapabilityPackageLoader(database)
    assert rolled_back.active()[0].capability_revision == "1.0.0"
    rolled_back.uninstall("fixture_graph", expected_revision="1.0.0")
    uninstalled = CapabilityPackageLoader(database)
    assert uninstalled.active() == ()
    assert [item.capability_revision for item in uninstalled.history("fixture_graph")] == [
        "1.0.0", "2.0.0",
    ]


def test_durable_upgrade_revision_drift_rolls_back_new_revision_insert(tmp_path: Path) -> None:
    database = tmp_path / "capability-packages.sqlite3"
    loader = CapabilityPackageLoader(database)
    loader.install(loader.load_manifest(_manifest(tmp_path, MANIFEST)))
    second = loader.load_manifest(
        _manifest(tmp_path, {**MANIFEST, "capability_revision": "2.0.0"})
    )

    with pytest.raises(CapabilityPackageError, match="revision_drift"):
        loader.upgrade(second, expected_revision="stale")

    restarted = CapabilityPackageLoader(database)
    assert restarted.active()[0].capability_revision == "1.0.0"
    assert [item.capability_revision for item in restarted.history("fixture_graph")] == [
        "1.0.0",
    ]


def test_generic_discovery_loads_new_package_without_capability_specific_core_branch() -> None:
    packages = Path(__file__).resolve().parents[3] / "src/core/capability_packages"
    discovered = CapabilityPackageLoader().discover(packages)

    assert {item.capability_id for item in discovered} >= {
        "thought_graph_context", "timeline_preview",
    }


def test_explicit_uninstall_persists_disabled_desired_state_and_reconcile_skips_bundle(
    tmp_path: Path,
) -> None:
    packages = tmp_path / "packages"
    database = tmp_path / "capability-packages.sqlite3"
    _bundled_manifest(packages)
    loader = CapabilityPackageLoader(database)
    reconcile_bundled_capability_packages(loader, packages)

    loader.uninstall(
        "fixture_graph", expected_revision="1.0.0",
        command_id="cmd-disable-fixture", audit_ref="audit://operator/disable-fixture",
    )
    restarted = CapabilityPackageLoader(database)
    desired = restarted.desired_state("fixture_graph")

    assert restarted.active() == ()
    assert desired is not None
    assert (desired.state, desired.command_id, desired.audit_ref) == (
        "disabled", "cmd-disable-fixture", "audit://operator/disable-fixture",
    )
    reconcile_bundled_capability_packages(restarted, packages)
    assert CapabilityPackageLoader(database).active() == ()


def test_explicit_generic_reinstall_reenables_disabled_bundle(tmp_path: Path) -> None:
    packages = tmp_path / "packages"
    database = tmp_path / "capability-packages.sqlite3"
    manifest_path = _bundled_manifest(packages)
    loader = CapabilityPackageLoader(database)
    reconcile_bundled_capability_packages(loader, packages)
    loader.uninstall("fixture_graph", expected_revision="1.0.0")

    manifest = loader.load_manifest(manifest_path)
    loader.install(manifest)

    restarted = CapabilityPackageLoader(database)
    assert restarted.desired_state("fixture_graph").state == "enabled"
    assert restarted.active()[0].capability_revision == "1.0.0"


def test_missing_bundle_detaches_active_without_erasing_operator_disabled_intent(tmp_path: Path) -> None:
    packages = tmp_path / "packages"
    database = tmp_path / "capability-packages.sqlite3"
    manifest_path = _bundled_manifest(packages)
    loader = CapabilityPackageLoader(database)
    reconcile_bundled_capability_packages(loader, packages)
    loader.uninstall("fixture_graph", expected_revision="1.0.0")
    manifest_path.unlink()

    reconcile_bundled_capability_packages(CapabilityPackageLoader(database), packages)

    restarted = CapabilityPackageLoader(database)
    assert restarted.active() == ()
    assert restarted.desired_state("fixture_graph").state == "disabled"


def test_repeated_explicit_uninstall_command_is_idempotent_and_identity_drift_fails_closed(
    tmp_path: Path,
) -> None:
    database = tmp_path / "capability-packages.sqlite3"
    loader = CapabilityPackageLoader(database)
    loader.install(loader.load_manifest(_manifest(tmp_path, MANIFEST)))
    kwargs = {
        "expected_revision": "1.0.0",
        "command_id": "cmd-disable-fixture",
        "audit_ref": "audit://operator/disable-fixture",
    }

    loader.uninstall("fixture_graph", **kwargs)
    replay = CapabilityPackageLoader(database).uninstall("fixture_graph", **kwargs)

    assert replay.capability_revision == "1.0.0"
    with pytest.raises(CapabilityPackageError, match="command_identity_drift"):
        CapabilityPackageLoader(database).uninstall(
            "fixture_graph", expected_revision="1.0.0",
            command_id="cmd-disable-fixture", audit_ref="audit://operator/different",
        )
