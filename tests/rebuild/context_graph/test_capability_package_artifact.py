from __future__ import annotations

import json
import hashlib
import os
import sqlite3
from dataclasses import asdict
from pathlib import Path

import pytest

from backend.api.capability_package_runtime import compile_capability_package_contributions
from core.context_graph import CapabilityPackageError, CapabilityPackageLoader
import core.context_graph.capability_artifact as artifact_module


def _bundle(root: Path, revision: str, value: str, *, nested: bool = False) -> Path:
    package = root / "fixture_graph"
    package.mkdir(parents=True, exist_ok=True)
    (package / "manifest.json").write_text(json.dumps({
        "schema_version": "1.0.0", "core_api": "1", "capability_id": "fixture_graph",
        "capability_revision": revision, "display_name": "Fixture", "kind": "read_only_view",
        "execution_state_owner": "core_effect_log", "recovery_owner": "core_reaper",
        "secret_access": "lease_reference_only", "memory_write": "proposal_only",
        "document_write": "draft_only", "project_skill_write": "proposal_only",
        "contributions": ["read_only_preview"], "provides": {},
        "tools": [{"id": "fixture.preview", "exposure": "internal", "contributes": "read_only_preview", "handler": "preview.py:preview"}],
        "workflows": [], "context": [], "permissions": {"net": [], "fs": [], "secrets": []},
        "budgets": {"max_bytes": 10, "max_seconds": 1}, "effects": {},
        "ui": {"label": "Fixture", "icon": "fixture", "progress_steps": []}, "tests": [],
    }), encoding="utf-8")
    if nested:
        nested_dir = package / "nested"
        nested_dir.mkdir()
        (nested_dir / "__init__.py").write_text("", encoding="utf-8")
        (nested_dir / "helper.py").write_text(f"def value():\n    return {value!r}\n", encoding="utf-8")
        (package / "preview.py").write_text("from .nested.helper import value\ndef preview():\n    return value()\n", encoding="utf-8")
    else:
        (package / "preview.py").write_text(f"def preview():\n    return {value!r}\n", encoding="utf-8")
    return package / "manifest.json"


def test_active_artifact_survives_live_source_removal_and_restart(tmp_path: Path) -> None:
    database = tmp_path / "catalog.sqlite3"
    path = _bundle(tmp_path / "live", "1.0.0", "v1")
    loader = CapabilityPackageLoader(database)
    loader.install(loader.load_manifest(path))
    path.unlink()
    (path.parent / "preview.py").unlink()

    restarted = CapabilityPackageLoader(database)
    assert compile_capability_package_contributions(restarted).tools["fixture.preview"]() == "v1"


def test_upgrade_then_rollback_loads_prior_artifact_not_live_source(tmp_path: Path) -> None:
    database = tmp_path / "catalog.sqlite3"
    path = _bundle(tmp_path / "live", "1.0.0", "v1")
    loader = CapabilityPackageLoader(database)
    loader.install(loader.load_manifest(path))
    path = _bundle(tmp_path / "live", "2.0.0", "v2")
    loader.upgrade(loader.load_manifest(path), expected_revision="1.0.0")
    loader.rollback("fixture_graph", expected_revision="2.0.0")
    (path.parent / "preview.py").write_text("def preview():\n    return 'live-mutated'\n", encoding="utf-8")

    assert compile_capability_package_contributions(CapabilityPackageLoader(database)).tools["fixture.preview"]() == "v1"


def test_artifact_drift_fails_closed_and_orphan_is_not_active(tmp_path: Path) -> None:
    database = tmp_path / "catalog.sqlite3"
    path = _bundle(tmp_path / "live", "1.0.0", "v1")
    loader = CapabilityPackageLoader(database)
    installed = loader.install(loader.load_manifest(path))
    root = loader.artifact_root(installed)
    (root / "preview.py").write_text("def preview():\n    return 'tampered'\n", encoding="utf-8")
    with pytest.raises(CapabilityPackageError, match="artifact_drift"):
        loader.artifact_root(CapabilityPackageLoader(database).active()[0])
    assert (tmp_path / "capability-artifacts").exists()


def test_metadata_tampering_and_extra_files_fail_closed(tmp_path: Path) -> None:
    database = tmp_path / "catalog.sqlite3"
    path = _bundle(tmp_path / "live", "1.0.0", "v1")
    loader = CapabilityPackageLoader(database)
    installed = loader.install(loader.load_manifest(path))
    root = loader.artifact_root(installed)
    metadata_path = root / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    preview = root / "preview.py"
    preview.write_text("def preview():\n    return 'tampered'\n", encoding="utf-8")
    metadata["files"][1]["sha256"] = hashlib.sha256(preview.read_bytes()).hexdigest()
    metadata["files"][1]["size"] = preview.stat().st_size
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    with pytest.raises(CapabilityPackageError, match="artifact_identity_drift"):
        loader.artifact_root(installed)

    metadata_path.write_text(json.dumps({"invalid": True}), encoding="utf-8")
    with pytest.raises(CapabilityPackageError, match="artifact_metadata_invalid"):
        loader.artifact_root(installed)


def test_extra_artifact_file_fails_closed(tmp_path: Path) -> None:
    database = tmp_path / "catalog.sqlite3"
    path = _bundle(tmp_path / "live", "1.0.0", "v1")
    loader = CapabilityPackageLoader(database)
    installed = loader.install(loader.load_manifest(path))
    (loader.artifact_root(installed) / "unexpected.py").write_text("pass\n", encoding="utf-8")

    with pytest.raises(CapabilityPackageError, match="artifact_drift"):
        loader.artifact_root(installed)


def test_extra_or_unapproved_source_files_fail_closed(tmp_path: Path) -> None:
    database = tmp_path / "catalog.sqlite3"
    path = _bundle(tmp_path / "live", "1.0.0", "v1")
    (path.parent / "payload.txt").write_text("not code", encoding="utf-8")
    loader = CapabilityPackageLoader(database)
    with pytest.raises(CapabilityPackageError, match="artifact_unapproved_file"):
        loader.install(loader.load_manifest(path))
    (path.parent / "payload.txt").unlink()
    (path.parent / "metadata.json").write_text("{}", encoding="utf-8")
    with pytest.raises(CapabilityPackageError, match="artifact_metadata_collision"):
        loader.install(loader.load_manifest(path))


def test_trusted_root_escape_and_symlink_package_fail_closed(tmp_path: Path) -> None:
    trusted = tmp_path / "trusted"
    path = _bundle(tmp_path / "outside", "1.0.0", "v1")
    loader = CapabilityPackageLoader(tmp_path / "catalog.sqlite3", trusted_packages_root=trusted)
    with pytest.raises(CapabilityPackageError, match="source_outside_trusted_root"):
        loader.load_manifest(path)

    trusted.mkdir()
    link = trusted / "fixture_graph"
    try:
        os.symlink(path.parent, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are unavailable on this host")
    with pytest.raises(CapabilityPackageError):
        loader.discover(trusted)


def test_ignored_bytecode_directory_still_rejects_reparse_points(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _bundle(tmp_path / "live", "1.0.0", "v1")
    bytecode = path.parent / "__pycache__"
    bytecode.mkdir()
    original = artifact_module._is_link_or_reparse

    def simulated_reparse(candidate: Path) -> bool:
        return Path(candidate) == bytecode or original(Path(candidate))

    monkeypatch.setattr(
        artifact_module, "_is_link_or_reparse", simulated_reparse,
    )
    loader = CapabilityPackageLoader(tmp_path / "catalog.sqlite3")
    with pytest.raises(CapabilityPackageError, match="link_forbidden"):
        loader.install(loader.load_manifest(path))


def test_nested_relative_import_is_loaded_from_artifact_only(tmp_path: Path) -> None:
    database = tmp_path / "catalog.sqlite3"
    path = _bundle(tmp_path / "live", "1.0.0", "v1", nested=True)
    loader = CapabilityPackageLoader(database)
    loader.install(loader.load_manifest(path))
    (path.parent / "nested" / "helper.py").write_text("def value():\n    return 'live'\n", encoding="utf-8")

    assert compile_capability_package_contributions(CapabilityPackageLoader(database)).tools["fixture.preview"]() == "v1"


def test_disabled_capability_never_loads_its_artifact(tmp_path: Path) -> None:
    database = tmp_path / "catalog.sqlite3"
    path = _bundle(tmp_path / "live", "1.0.0", "v1")
    loader = CapabilityPackageLoader(database)
    installed = loader.install(loader.load_manifest(path))
    loader.uninstall(installed.capability_id, expected_revision=installed.capability_revision)

    assert compile_capability_package_contributions(CapabilityPackageLoader(database)).tools == {}


def test_legacy_empty_artifact_pointer_is_optional_and_does_not_load_live_source(tmp_path: Path) -> None:
    database = tmp_path / "catalog.sqlite3"
    path = _bundle(tmp_path / "live", "1.0.0", "v1")
    loader = CapabilityPackageLoader(database)
    manifest = loader.load_manifest(path)
    encoded = json.dumps({key: value for key, value in asdict(manifest).items() if key != "artifact_id"}, sort_keys=True)
    with loader._connect() as connection:  # test creates the pre-artifact durable shape.
        connection.execute("INSERT INTO capability_package_revisions(capability_id, capability_revision, manifest_json, sequence, artifact_id) VALUES (?, ?, ?, ?, '')", (manifest.capability_id, manifest.capability_revision, encoded, 1))
        connection.execute("INSERT INTO capability_package_active(capability_id, capability_revision) VALUES (?, ?)", (manifest.capability_id, manifest.capability_revision))

    restarted = CapabilityPackageLoader(database)
    assert restarted.active() == ()
    assert compile_capability_package_contributions(restarted).tools == {}


def test_artifact_orphan_created_before_pointer_failure_is_never_active(tmp_path: Path) -> None:
    database = tmp_path / "catalog.sqlite3"
    path = _bundle(tmp_path / "live", "1.0.0", "v1")
    loader = CapabilityPackageLoader(database)
    with loader._connect() as connection:
        connection.execute("CREATE TRIGGER fail_active_insert BEFORE INSERT ON capability_package_active BEGIN SELECT RAISE(ABORT, 'injected'); END")
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        loader.install(loader.load_manifest(path))

    assert CapabilityPackageLoader(database).active() == ()
    assert any((tmp_path / "capability-artifacts").rglob("metadata.json"))
