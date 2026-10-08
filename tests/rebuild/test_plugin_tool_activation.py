from __future__ import annotations

import json
import base64
from pathlib import Path

import pytest

from core.plugin_host import PluginPackageIntake, PluginPackageIntakeConflict, PluginPackageIntakeError, PluginToolActivation
from core.storage_provider import SQLiteStructuredRecordStore


def _package(root: Path, *, entries: dict[str, str] | None = None) -> Path:
    package = root / "lookup-plugin"
    (package / ".codex-plugin").mkdir(parents=True)
    (package / "tools" / "country.lookup").mkdir(parents=True)
    (package / ".codex-plugin" / "plugin.json").write_text(json.dumps({"name": "lookup-plugin", "version": "1.0.0", "description": "Local values"}), encoding="utf-8")
    (package / "tools" / "country.lookup" / "tool.json").write_text(json.dumps({"id": "country.lookup", "version": 1, "description": "Look up a country", "entries": entries or {"CN": "China", "FR": "France"}}), encoding="utf-8")
    return package


def _intake(root: Path) -> PluginPackageIntake:
    return PluginPackageIntake(SQLiteStructuredRecordStore(root / "jobs.sqlite3"), now="2026-08-26T00:00:00Z", source_root=root / "source")


def _activation(root: Path) -> PluginToolActivation:
    return PluginToolActivation(SQLiteStructuredRecordStore(root / "jobs.sqlite3"), now="2026-08-26T00:01:00Z")


def _installed(root: Path) -> dict[str, object]:
    discovered = _intake(root).discover(str(_package(root / "source")), command_id="discover-0001")
    return _intake(root).install_disabled("lookup-plugin", expected_state_revision=discovered["state_revision"], command_id="install-0001", confirm=True)


def test_declarative_tool_stays_installed_disabled_then_reviews_activates_and_lookup_is_os_owned(tmp_path: Path) -> None:
    installed = _installed(tmp_path)
    assert installed["compatibility_report"]["compatible"] is True
    assert installed["state"]["status"] == "installed_disabled"
    assert installed["normalized_manifest"]["declarative_tools"][0]["id"] == "country.lookup"

    reviewed = _activation(tmp_path).review("lookup-plugin", tool_ids=["country.lookup"], expected_state_revision=installed["state_revision"], command_id="review-0001", confirm=True, reason="local lookup only")
    activated = _activation(tmp_path).activate("lookup-plugin", expected_review_revision=reviewed["review_revision"], expected_activation_revision=0, command_id="activate-0001", confirm=True)

    bindings = _activation(tmp_path).active_contributions(["lookup-plugin"])
    assert len(bindings) == 1
    assert bindings[0].definition.source == "plugin"
    assert bindings[0].definition.owner_id == "lookup-plugin"
    assert bindings[0].definition.effect == "read"
    assert bindings[0].definition.destination == "local"
    assert bindings[0].provider.invoke({"arguments": {"key": "CN"}})["result"] == {"found": True, "key": "CN", "value": "China"}
    assert activated["activation"]["status"] == "active"


def test_review_activation_disable_commands_require_confirmation_cas_and_replay(tmp_path: Path) -> None:
    installed = _installed(tmp_path)
    activation = _activation(tmp_path)
    with pytest.raises(PluginPackageIntakeError, match="explicit confirmation"):
        activation.review("lookup-plugin", tool_ids=["country.lookup"], expected_state_revision=installed["state_revision"], command_id="review-0001", confirm=False, reason="no")
    reviewed = activation.review("lookup-plugin", tool_ids=["country.lookup"], expected_state_revision=installed["state_revision"], command_id="review-0001", confirm=True, reason="yes")
    assert activation.review("lookup-plugin", tool_ids=["country.lookup"], expected_state_revision=installed["state_revision"], command_id="review-0001", confirm=True, reason="changed") ["replayed"] is True
    with pytest.raises(PluginPackageIntakeConflict, match="revision conflict"):
        activation.activate("lookup-plugin", expected_review_revision=reviewed["review_revision"] + 1, expected_activation_revision=0, command_id="activate-0001", confirm=True)
    active = activation.activate("lookup-plugin", expected_review_revision=reviewed["review_revision"], expected_activation_revision=0, command_id="activate-0001", confirm=True)
    assert activation.activate("lookup-plugin", expected_review_revision=reviewed["review_revision"], expected_activation_revision=0, command_id="activate-0001", confirm=True)["replayed"] is True
    disabled = activation.disable("lookup-plugin", expected_activation_revision=active["activation_revision"], command_id="disable-0001", confirm=True, reason="revoke")
    assert disabled["activation"]["status"] == "disabled"
    assert _activation(tmp_path).all_active_tools() == ()


def test_active_snapshot_fails_closed_for_raw_or_review_drift(tmp_path: Path) -> None:
    installed = _installed(tmp_path)
    activation = _activation(tmp_path)
    reviewed = activation.review("lookup-plugin", tool_ids=["country.lookup"], expected_state_revision=installed["state_revision"], command_id="review-0001", confirm=True, reason="yes")
    activation.activate("lookup-plugin", expected_review_revision=reviewed["review_revision"], expected_activation_revision=0, command_id="activate-0001", confirm=True)
    store = SQLiteStructuredRecordStore(tmp_path / "jobs.sqlite3")
    raw = store.read("plugin_raw_packages", "lookup-plugin~1.0.0")
    assert raw is not None
    tampered = dict(raw.payload)
    tampered["files"] = list(tampered["files"])
    original = base64.b64decode(tampered["files"][-1]["content_base64"])
    changed = original + b"\n"  # equivalent JSON, but not the reviewed immutable bytes
    tampered["files"][-1] = dict(tampered["files"][-1]) | {"content_base64": base64.b64encode(changed).decode("ascii"), "size_bytes": len(changed)}
    with store.begin() as uow:
        uow.put("plugin_raw_packages", raw.object_id, tampered, expected_revision=raw.revision)
        uow.commit()
    assert _activation(tmp_path).all_active_tools() == ()


def test_unsafe_or_nondeclarative_tools_remain_quarantined(tmp_path: Path) -> None:
    package = _package(tmp_path / "source", entries={"home": "C:\\private"})
    result = _intake(tmp_path).discover(str(package), command_id="discover-0001")
    assert result["state"]["status"] == "quarantined"
    assert result["compatibility_report"]["issues"] == [{"code": "invalid_declarative_local_lookup_tool", "path": "tools/country.lookup/tool.json"}]
