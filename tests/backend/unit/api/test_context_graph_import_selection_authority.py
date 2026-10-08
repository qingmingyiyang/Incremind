from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from backend.api.context_graph_import_selection_authority import (
    ContextGraphImportSelectionAuthority,
    ContextGraphImportSelectionError,
)
from backend.security import DesktopFileGrant
from core.storage_provider import JsonObjectStore, SQLiteStructuredRecordStore


class _Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 30, 3, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value


def _authority(tmp_path: Path, *, clock: _Clock | None = None, ttl: timedelta = timedelta(minutes=10)):
    store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    assets = tmp_path / "library" / "assets" / "originals"
    return ContextGraphImportSelectionAuthority(
        records=SQLiteStructuredRecordStore(tmp_path / ".rebuild-data" / "context-selections.sqlite3"),
        object_store=store,
        managed_assets_root=assets,
        now=clock or _Clock(),
        ttl=ttl,
    ), store, assets


def _asset(store: JsonObjectStore, root: Path, *, asset_id: str = "asset-1", content: bytes = b"graph") -> Path:
    target = root / "aa" / asset_id
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    store.write("workbench_original_assets", asset_id, {
        "id": asset_id,
        "asset_ref": f"asset-ref-{asset_id}",
        "vault_ref": f"assets/originals/aa/{asset_id}",
        "display_name": "graph.thoughtdag.json",
        "media_type": "application/json",
        "byte_count": len(content),
        "sha256": "a" * 64,
    }, expected_revision=0)
    return target


def _create(authority: ContextGraphImportSelectionAuthority, *, command: str = "command-1"):
    return authority.create(
        command, "project-a", "thoughtdag", "asset-1", "actor-a", "session-a",
        file_grant=_grant(),
    )


def _grant() -> DesktopFileGrant:
    return DesktopFileGrant(
        grant_id="file-grant-" + "a" * 32,
        session_instance_id="session-a",
        display_name="graph.thoughtdag.json",
        media_type="application/json",
        source_kind="file",
        size_bytes=5,
        sha256="a" * 64,
        expires_at_ms=2_000_000_000_000,
    )


def test_create_and_consume_are_persistent_opaque_and_one_time(tmp_path: Path) -> None:
    authority, store, root = _authority(tmp_path)
    source = _asset(store, root)

    receipt = _create(authority)
    assert receipt.project_id == "project-a"
    assert set(receipt.to_dict()) == {
        "selection_id", "project_id", "display_name", "source_type", "expires_at",
    }
    assert str(source) not in str(receipt.to_dict())

    reloaded = ContextGraphImportSelectionAuthority(
        records=SQLiteStructuredRecordStore(tmp_path / ".rebuild-data" / "context-selections.sqlite3"),
        object_store=store, managed_assets_root=root, now=_Clock(),
    )
    selected = reloaded.consume(receipt.selection_id, project_id="project-a", actor_id="actor-a", source_type="thoughtdag", session_instance_id="session-a")
    assert selected.selected_path == source.resolve()
    assert selected.source_revision.startswith("mtime-")
    with pytest.raises(ContextGraphImportSelectionError, match="already_consumed"):
        reloaded.consume(receipt.selection_id, project_id="project-a", actor_id="actor-a", source_type="thoughtdag", session_instance_id="session-a")
    with pytest.raises(ContextGraphImportSelectionError, match="unavailable"):
        reloaded.consume("ctxsel-forged", project_id="project-a", actor_id="actor-a", source_type="thoughtdag", session_instance_id="session-a")


@pytest.mark.parametrize("field,value,error", [
    ("project_id", "project-b", "scope_mismatch"),
    ("actor_id", "actor-b", "scope_mismatch"),
    ("source_type", "markdown", "source_type_mismatch"),
    ("session_instance_id", "session-b", "session_mismatch"),
])
def test_consume_rejects_scope_forgery(tmp_path: Path, field: str, value: str, error: str) -> None:
    authority, store, root = _authority(tmp_path)
    _asset(store, root)
    receipt = _create(authority)
    values = {"project_id": "project-a", "actor_id": "actor-a", "source_type": "thoughtdag", "session_instance_id": "session-a"}
    values[field] = value
    with pytest.raises(ContextGraphImportSelectionError, match=error):
        authority.consume(receipt.selection_id, **values)


def test_expired_and_changed_source_are_rejected_without_consuming(tmp_path: Path) -> None:
    clock = _Clock()
    authority, store, root = _authority(tmp_path, clock=clock, ttl=timedelta(seconds=1))
    source = _asset(store, root)
    expired = _create(authority)
    clock.value += timedelta(seconds=2)
    with pytest.raises(ContextGraphImportSelectionError, match="expired"):
        authority.consume(expired.selection_id, project_id="project-a", actor_id="actor-a", source_type="thoughtdag", session_instance_id="session-a")

    clock.value = datetime(2026, 8, 30, 4, 0, tzinfo=UTC)
    fresh = _create(authority, command="command-2")
    source.write_bytes(b"size drift")
    with pytest.raises(ContextGraphImportSelectionError, match="size_drift|revision_drift"):
        authority.consume(fresh.selection_id, project_id="project-a", actor_id="actor-a", source_type="thoughtdag", session_instance_id="session-a")


def test_create_is_idempotent_only_for_same_command_identity(tmp_path: Path) -> None:
    authority, store, root = _authority(tmp_path)
    _asset(store, root)
    first = _create(authority)
    retry = _create(authority)
    assert retry == first
    with pytest.raises(ContextGraphImportSelectionError, match="command_drift"):
        authority.create(
            "command-1", "project-b", "thoughtdag", "asset-1", "actor-a", "session-a",
            file_grant=_grant(),
        )


def test_rejects_vault_escape_and_mismatched_asset_record(tmp_path: Path) -> None:
    authority, store, root = _authority(tmp_path)
    _asset(store, root)
    store.write("workbench_original_assets", "asset-1", {
        "id": "asset-1", "asset_ref": "asset-ref", "vault_ref": "assets/originals/../../outside", "display_name": "bad", "byte_count": 5,
        "sha256": "a" * 64,
    }, expected_revision=1)
    with pytest.raises(ContextGraphImportSelectionError, match="path_invalid|path_escape"):
        _create(authority)

    outside = tmp_path / "outside"
    outside.write_bytes(b"x")
    store.write("workbench_original_assets", "asset-1", {
        "id": "asset-1", "asset_ref": "asset-ref", "vault_ref": "assets/originals/aa/asset-1", "display_name": "bad", "byte_count": 2,
        "sha256": "a" * 64,
    }, expected_revision=2)
    with pytest.raises(ContextGraphImportSelectionError, match="grant_mismatch"):
        _create(authority, command="command-2")
