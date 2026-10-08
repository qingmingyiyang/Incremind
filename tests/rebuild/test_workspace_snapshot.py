from __future__ import annotations

from types import SimpleNamespace
import stat

import pytest

from core.product_core import workspace_snapshot
from core.product_core.workspace_snapshot import (
    WorkspaceSnapshotBudget,
    WorkspaceSnapshotError,
    create_workspace_manifest_snapshot,
)


def _snapshot(root, **kwargs):
    return create_workspace_manifest_snapshot(root, base_revision="git:base-42", **kwargs)


def test_workspace_snapshot_is_stable_sorted_and_never_includes_root_or_content(tmp_path) -> None:
    (tmp_path / "z.txt").write_text("z", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("print('a')", encoding="utf-8")

    first = _snapshot(tmp_path)
    second = _snapshot(tmp_path)

    assert [entry.relative_path for entry in first.entries] == ["src/a.py", "z.txt"]
    assert first == second
    assert first.manifest_ref.startswith("workspace-manifest:sha256:")
    payload = first.as_dict()
    assert str(tmp_path) not in repr(payload)
    assert "print('a')" not in repr(payload)
    assert {entry.base_revision for entry in first.entries} == {"git:base-42"}


def test_workspace_snapshot_excludes_sensitive_dependency_and_build_paths(tmp_path) -> None:
    (tmp_path / "keep.txt").write_text("safe", encoding="utf-8")
    for relative in (".env", "secrets/api-key.txt", "node_modules/pkg/index.js", "dist/app.js", ".git/config", ".ssh/config"):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("must-not-appear", encoding="utf-8")

    snapshot = _snapshot(tmp_path)

    assert [entry.relative_path for entry in snapshot.entries] == ["keep.txt"]


def test_workspace_snapshot_rejects_symlink_or_escape(tmp_path) -> None:
    outside = tmp_path.parent / "outside-workspace-snapshot.txt"
    outside.write_text("outside", encoding="utf-8")
    try:
        (tmp_path / "escape.txt").symlink_to(outside)
    except OSError:
        with pytest.raises(WorkspaceSnapshotError, match="symlink or reparse"):
            workspace_snapshot._assert_not_link_or_reparse(SimpleNamespace(st_mode=stat.S_IFLNK, st_file_attributes=0))
        return

    with pytest.raises(WorkspaceSnapshotError, match="symlink or reparse"):
        _snapshot(tmp_path)


def test_workspace_snapshot_rejects_entry_and_byte_budgets(tmp_path) -> None:
    (tmp_path / "a.txt").write_text("ab", encoding="utf-8")
    (tmp_path / "b.txt").write_text("cd", encoding="utf-8")

    with pytest.raises(WorkspaceSnapshotError, match="entry budget"):
        _snapshot(tmp_path, budget=WorkspaceSnapshotBudget(max_entries=1))
    with pytest.raises(WorkspaceSnapshotError, match="total snapshot byte budget"):
        _snapshot(tmp_path, budget=WorkspaceSnapshotBudget(max_total_bytes=3))
    with pytest.raises(WorkspaceSnapshotError, match="file exceeds"):
        _snapshot(tmp_path, budget=WorkspaceSnapshotBudget(max_file_bytes=1))


def test_workspace_snapshot_rejects_toctou_metadata_drift(tmp_path, monkeypatch) -> None:
    target = tmp_path / "source.txt"
    target.write_text("initial", encoding="utf-8")
    original = workspace_snapshot._digest_regular_file

    def mutate_then_digest(path, expected, budget, started, monotonic):
        with path.open("a", encoding="utf-8") as handle:
            handle.write("-changed")
        return original(path, expected, budget, started, monotonic)

    monkeypatch.setattr(workspace_snapshot, "_digest_regular_file", mutate_then_digest)

    with pytest.raises(WorkspaceSnapshotError, match="changed during scan"):
        _snapshot(tmp_path)


def test_workspace_snapshot_scan_timeout_is_enforced(tmp_path) -> None:
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    ticks = iter((0.0, 1.0))

    with pytest.raises(WorkspaceSnapshotError, match="timed out"):
        _snapshot(tmp_path, budget=WorkspaceSnapshotBudget(max_scan_seconds=0.1), monotonic=lambda: next(ticks))
