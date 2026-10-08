from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.application_skill import ApplicationSkillCatalog
from core.plugin_host import (
    PluginPackageIntake,
    PluginPackageIntakeError,
    PluginSkillActivation,
)
from core.storage_provider import SQLiteStructuredRecordStore


def _package(root: Path) -> Path:
    package = root / "method-plugin"
    metadata = package / ".codex-plugin"
    skill = package / "skills" / "careful-review"
    metadata.mkdir(parents=True)
    skill.mkdir(parents=True)
    (metadata / "plugin.json").write_text(
        json.dumps({"name": "method-plugin", "version": "1.0.0", "description": "Methods"}),
        encoding="utf-8",
    )
    (skill / "SKILL.md").write_text(
        "---\nname: careful-review\ndescription: Review evidence carefully\n---\nCheck evidence before conclusions.\n",
        encoding="utf-8",
    )
    return package


def _services(tmp_path: Path):
    store = SQLiteStructuredRecordStore(tmp_path / "jobs.sqlite3")
    intake = PluginPackageIntake(store, now="2026-08-26T00:00:00Z", source_root=tmp_path / "inbox")
    activation = PluginSkillActivation(
        store, managed_root=tmp_path / "managed", now="2026-08-26T00:01:00Z",
    )
    return store, intake, activation


def _installed(tmp_path: Path):
    package = _package(tmp_path / "inbox")
    store, intake, activation = _services(tmp_path)
    discovered = intake.discover(str(package), command_id="discover-skill-0001")
    installed = intake.install_disabled(
        "method-plugin", expected_state_revision=discovered["state_revision"],
        command_id="install-skill-0001", confirm=True,
    )
    return store, activation, installed


def test_review_activate_restart_source_and_disable(tmp_path: Path) -> None:
    store, activation, installed = _installed(tmp_path)
    reviewed = activation.review(
        "method-plugin", skill_ids=["careful-review"],
        expected_state_revision=installed["state_revision"], command_id="review-skill-0001",
        confirm=True, reason="User reviewed the method text",
    )
    activated = activation.activate(
        "method-plugin", expected_review_revision=reviewed["review_revision"],
        expected_activation_revision=0,
        command_id="activate-skill-0001", confirm=True,
    )

    assert activated["activation"]["status"] == "active"
    sources = PluginSkillActivation(
        store, managed_root=tmp_path / "managed", now="2026-08-26T00:02:00Z",
    ).active_sources(("method-plugin",))
    catalog = ApplicationSkillCatalog().discover_selected(sources, ("careful-review",))
    assert [package.skill_id for package in catalog.packages] == ["careful-review"]
    assert catalog.packages[0].source_kind == "plugin"
    assert activation.active_sources(()) == ()

    disabled = activation.disable(
        "method-plugin", expected_activation_revision=activated["activation_revision"],
        command_id="disable-skill-0001", confirm=True, reason="No longer needed",
    )
    assert disabled["activation"]["status"] == "disabled"
    assert activation.active_sources(("method-plugin",)) == ()
    assert store.read("plugin_raw_packages", "method-plugin~1.0.0") is not None


def test_invalid_skill_is_rejected_without_active_source(tmp_path: Path) -> None:
    package = _package(tmp_path / "inbox")
    (package / "skills" / "careful-review" / "SKILL.md").write_text("not a skill", encoding="utf-8")
    _store, intake, activation = _services(tmp_path)
    discovered = intake.discover(str(package), command_id="discover-skill-0001")
    installed = intake.install_disabled(
        "method-plugin", expected_state_revision=discovered["state_revision"],
        command_id="install-skill-0001", confirm=True,
    )
    reviewed = activation.review(
        "method-plugin", skill_ids=["careful-review"],
        expected_state_revision=installed["state_revision"], command_id="review-skill-0001",
        confirm=True, reason="Review candidate",
    )

    with pytest.raises(PluginPackageIntakeError, match="materialization failed"):
        activation.activate(
            "method-plugin", expected_review_revision=reviewed["review_revision"],
            expected_activation_revision=0,
            command_id="activate-skill-0001", confirm=True,
        )
    assert activation.active_sources(("method-plugin",)) == ()


def test_materialized_byte_drift_fails_closed(tmp_path: Path) -> None:
    _store, activation, installed = _installed(tmp_path)
    reviewed = activation.review(
        "method-plugin", skill_ids=["careful-review"],
        expected_state_revision=installed["state_revision"], command_id="review-skill-0001",
        confirm=True, reason="Reviewed",
    )
    activation.activate(
        "method-plugin", expected_review_revision=reviewed["review_revision"],
        expected_activation_revision=0,
        command_id="activate-skill-0001", confirm=True,
    )
    target = tmp_path / "managed" / "method-plugin~1.0.0" / "careful-review" / "SKILL.md"
    target.write_text("changed", encoding="utf-8")

    assert activation.active_sources(("method-plugin",)) == ()


def test_review_requires_installed_disabled_and_exact_skill_identity(tmp_path: Path) -> None:
    package = _package(tmp_path / "inbox")
    _store, intake, activation = _services(tmp_path)
    discovered = intake.discover(str(package), command_id="discover-skill-0001")
    with pytest.raises(PluginPackageIntakeError, match="installed disabled"):
        activation.review(
            "method-plugin", skill_ids=["careful-review"],
            expected_state_revision=discovered["state_revision"], command_id="review-skill-0001",
            confirm=True, reason="Reviewed",
        )
