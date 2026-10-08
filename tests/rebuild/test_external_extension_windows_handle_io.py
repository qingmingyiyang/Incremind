from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from core.external_extension_runtime.windows_handle_io import (
    WindowsHandleIoError,
    WindowsHandleTreeIo,
)


def _create_junction(link: Path, target: Path) -> None:
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip(f"junction creation unavailable: exit {result.returncode}")


@pytest.mark.skipif(os.name != "nt", reason="NT handle-relative contract is Windows-only")
def test_handle_relative_publish_is_create_new_and_exact(tmp_path: Path) -> None:
    root = tmp_path / "managed"
    root.mkdir()
    tree = WindowsHandleTreeIo()

    assert tree.write_new_tree(root, ("project", "skill", "revision-1"), {"skill/SKILL.md": b"body"})
    assert tree.read_exact_tree(root, ("project", "skill", "revision-1"), ("skill/SKILL.md",)) == {
        "skill/SKILL.md": b"body"
    }
    assert not tree.write_new_tree(root, ("project", "skill", "revision-1"), {"skill/SKILL.md": b"replacement"})
    assert (root / "project" / "skill" / "revision-1" / "skill" / "SKILL.md").read_bytes() == b"body"


@pytest.mark.skipif(os.name != "nt", reason="NT handle-relative contract is Windows-only")
def test_handle_relative_reader_handles_empty_and_exact_buffer_files(tmp_path: Path) -> None:
    root = tmp_path / "managed"
    root.mkdir()
    contents = {"skill/empty": b"", "skill/buffer": b"x" * (64 * 1024)}
    tree = WindowsHandleTreeIo()

    assert tree.write_new_tree(root, ("project", "skill", "revision-1"), contents)
    assert tree.read_exact_tree(
        root, ("project", "skill", "revision-1"), tuple(contents),
        expected_sizes={name: len(value) for name, value in contents.items()},
    ) == contents


@pytest.mark.skipif(os.name != "nt", reason="NT handle-relative contract is Windows-only")
def test_handle_relative_reader_rejects_oversized_file_before_unbounded_read(tmp_path: Path) -> None:
    root = tmp_path / "managed"
    root.mkdir()
    tree = WindowsHandleTreeIo()
    assert tree.write_new_tree(root, ("project", "skill", "revision-1"), {"skill/SKILL.md": b"ab"})

    with pytest.raises(WindowsHandleIoError, match="exceeds expected byte length"):
        tree.read_exact_tree(
            root, ("project", "skill", "revision-1"), ("skill/SKILL.md",),
            expected_sizes={"skill/SKILL.md": 1},
        )


@pytest.mark.skipif(os.name != "nt", reason="NT handle-relative contract is Windows-only")
def test_handle_relative_bounded_tree_freezes_canonical_mapping(tmp_path: Path) -> None:
    root = tmp_path / "managed"
    root.mkdir()
    contents = {"skill/SKILL.md": b"body", "skill/references/a.md": b"reference"}
    tree = WindowsHandleTreeIo()
    assert tree.write_new_tree(root, ("quarantine", "artifact"), contents)

    assert tree.read_bounded_tree(
        root, ("quarantine", "artifact"), max_files=2, max_file_bytes=16, max_total_bytes=32,
    ) == contents


@pytest.mark.skipif(os.name != "nt", reason="NT handle-relative contract is Windows-only")
def test_handle_relative_directory_chain_is_idempotent_and_rejects_empty_tree_state(
    tmp_path: Path,
) -> None:
    root = tmp_path / "managed"
    root.mkdir()
    tree = WindowsHandleTreeIo()

    tree.ensure_directory_chain(root, ("quarantine", "operations"))
    tree.ensure_directory_chain(root, ("quarantine", "operations"))
    assert (root / "quarantine" / "operations").is_dir()

    (root / "quarantine" / "operations" / "unexpected-empty").mkdir()
    with pytest.raises(WindowsHandleIoError, match="empty directory"):
        tree.read_bounded_tree(
            root,
            ("quarantine", "operations"),
            max_files=1,
            max_file_bytes=16,
            max_total_bytes=16,
        )


@pytest.mark.skipif(os.name != "nt", reason="NT handle-relative contract is Windows-only")
@pytest.mark.parametrize(
    ("max_files", "max_file_bytes", "max_total_bytes", "message"),
    [
        (1, 16, 32, "too many files"),
        (2, 3, 32, "per-file byte limit"),
        (2, 16, 7, "total byte limit"),
    ],
)
def test_handle_relative_bounded_tree_enforces_limits(
    tmp_path: Path, max_files: int, max_file_bytes: int, max_total_bytes: int, message: str,
) -> None:
    root = tmp_path / "managed"
    root.mkdir()
    tree = WindowsHandleTreeIo()
    assert tree.write_new_tree(root, ("quarantine", "artifact"), {"one": b"four", "two": b"four"})

    with pytest.raises(WindowsHandleIoError, match=message):
        tree.read_bounded_tree(
            root, ("quarantine", "artifact"), max_files=max_files,
            max_file_bytes=max_file_bytes, max_total_bytes=max_total_bytes,
        )


@pytest.mark.skipif(os.name != "nt", reason="NT handle-relative contract is Windows-only")
def test_handle_relative_move_dir_is_no_replace_and_root_relative(tmp_path: Path) -> None:
    root = tmp_path / "managed"
    root.mkdir()
    (root / "materialized").mkdir()
    tree = WindowsHandleTreeIo()
    assert tree.write_new_tree(root, ("quarantine", "source"), {"skill/SKILL.md": b"body"})

    tree.move_dir_no_replace(root, ("quarantine", "source"), ("materialized",), "revision-1")
    assert tree.read_bounded_tree(
        root, ("materialized", "revision-1"), max_files=1, max_file_bytes=16, max_total_bytes=16,
    ) == {"skill/SKILL.md": b"body"}
    with pytest.raises(FileNotFoundError):
        tree.move_dir_no_replace(root, ("quarantine", "source"), ("materialized",), "revision-2")
    with pytest.raises(WindowsHandleIoError, match="target already exists"):
        tree.move_dir_no_replace(root, ("materialized", "revision-1"), ("materialized",), "revision-1")


@pytest.mark.skipif(os.name != "nt", reason="NT handle-relative contract is Windows-only")
def test_bounded_reader_root_replacement_after_handle_acquisition_does_not_redirect(tmp_path: Path) -> None:
    root = tmp_path / "managed"
    root.mkdir()
    moved = tmp_path / "managed-held"
    tree = WindowsHandleTreeIo()
    assert tree.write_new_tree(root, ("quarantine", "artifact"), {"skill/SKILL.md": b"body"})

    class RootSwapTree(WindowsHandleTreeIo):
        swapped = False

        def _after_root_open(self, _root_handle: int) -> None:
            if self.swapped:
                return
            self.swapped = True
            root.rename(moved)
            root.mkdir()
            (root / "outside-sentinel.txt").write_bytes(b"do-not-read")

    assert RootSwapTree().read_bounded_tree(
        root, ("quarantine", "artifact"), max_files=1, max_file_bytes=16, max_total_bytes=16,
    ) == {"skill/SKILL.md": b"body"}
    assert (root / "outside-sentinel.txt").read_bytes() == b"do-not-read"


@pytest.mark.skipif(os.name != "nt", reason="NT handle-relative contract is Windows-only")
def test_move_calls_child_open_seam_for_source_and_target_parent(tmp_path: Path) -> None:
    root = tmp_path / "managed"
    root.mkdir()
    (root / "destination").mkdir()

    class RecordingTree(WindowsHandleTreeIo):
        opened: list[str]

        def __init__(self) -> None:
            self.opened = []

        def _before_child_open(self, _parent_handle: int, name: str) -> None:
            self.opened.append(name)

    setup = WindowsHandleTreeIo()
    assert setup.write_new_tree(root, ("source-parent", "source"), {"skill/SKILL.md": b"body"})
    tree = RecordingTree()
    tree.move_dir_no_replace(root, ("source-parent", "source"), ("destination",), "revision-1")
    assert tree.opened == ["source-parent", "source", "destination"]


@pytest.mark.skipif(os.name != "nt", reason="NT handle-relative contract is Windows-only")
@pytest.mark.parametrize("swap_name", ("source-parent", "source", "destination"))
def test_move_reparse_swap_at_ancestor_source_or_target_parent_fails_closed(tmp_path: Path, swap_name: str) -> None:
    root = tmp_path / "managed"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "destination").mkdir()
    setup = WindowsHandleTreeIo()
    assert setup.write_new_tree(root, ("source-parent", "source"), {"skill/SKILL.md": b"body"})
    sentinel = outside / "sentinel.txt"
    sentinel.write_bytes(b"outside-sentinel")

    class ReparseSwapTree(WindowsHandleTreeIo):
        swapped = False

        def _before_child_open(self, _parent_handle: int, name: str) -> None:
            if self.swapped or name != swap_name:
                return
            self.swapped = True
            target = root / name if name != "source" else root / "source-parent" / "source"
            shutil.rmtree(target)
            _create_junction(target, outside)

    with pytest.raises(WindowsHandleIoError):
        ReparseSwapTree().move_dir_no_replace(root, ("source-parent", "source"), ("destination",), "revision-1")
    assert sentinel.read_bytes() == b"outside-sentinel"


@pytest.mark.skipif(os.name != "nt", reason="NT handle-relative contract is Windows-only")
def test_bounded_reader_reparse_swap_at_artifact_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "managed"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    setup = WindowsHandleTreeIo()
    assert setup.write_new_tree(root, ("quarantine", "artifact"), {"skill/SKILL.md": b"body"})
    sentinel = outside / "sentinel.txt"
    sentinel.write_bytes(b"outside-sentinel")

    class ReparseSwapTree(WindowsHandleTreeIo):
        swapped = False

        def _before_child_open(self, _parent_handle: int, name: str) -> None:
            if self.swapped or name != "artifact":
                return
            self.swapped = True
            target = root / "quarantine" / "artifact"
            shutil.rmtree(target)
            _create_junction(target, outside)

    with pytest.raises(WindowsHandleIoError):
        ReparseSwapTree().read_bounded_tree(
            root, ("quarantine", "artifact"), max_files=1, max_file_bytes=16, max_total_bytes=16,
        )
    assert sentinel.read_bytes() == b"outside-sentinel"


@pytest.mark.skipif(os.name != "nt", reason="NT handle-relative contract is Windows-only")
def test_staging_name_collision_is_not_adopted(tmp_path: Path) -> None:
    root = tmp_path / "managed"
    collision = root / ".staging" / "fixed-stage"
    collision.mkdir(parents=True)
    (collision / "sentinel.txt").write_bytes(b"do-not-adopt")

    class CollisionTree(WindowsHandleTreeIo):
        @staticmethod
        def _new_stage_name() -> str:
            return "fixed-stage"

    with pytest.raises(WindowsHandleIoError):
        CollisionTree().write_new_tree(root, ("project", "skill", "revision-1"), {"skill/SKILL.md": b"body"})
    assert (collision / "sentinel.txt").read_bytes() == b"do-not-adopt"


@pytest.mark.skipif(os.name != "nt", reason="NT handle-relative contract is Windows-only")
def test_root_name_replacement_after_handle_acquisition_does_not_redirect_write(tmp_path: Path) -> None:
    root = tmp_path / "managed"
    root.mkdir()
    moved = tmp_path / "managed-held"

    class RootSwapTree(WindowsHandleTreeIo):
        swapped = False

        def _after_root_open(self, _root_handle: int) -> None:
            if self.swapped:
                return
            self.swapped = True
            root.rename(moved)
            root.mkdir()
            (root / "outside-sentinel.txt").write_bytes(b"do-not-touch")

    assert RootSwapTree().write_new_tree(root, ("project", "skill", "revision-1"), {"skill/SKILL.md": b"body"})
    assert (root / "outside-sentinel.txt").read_bytes() == b"do-not-touch"
    assert not (root / "project").exists()
    assert (moved / "project" / "skill" / "revision-1" / "skill" / "SKILL.md").read_bytes() == b"body"


@pytest.mark.skipif(os.name != "nt", reason="NT handle-relative contract is Windows-only")
def test_junction_swap_before_child_open_fails_closed_without_reading_sentinel(tmp_path: Path) -> None:
    root = tmp_path / "managed"
    outside = tmp_path / "outside"
    (root / "project").mkdir(parents=True)
    outside.mkdir()
    (root / "project" / "skill.txt").write_bytes(b"trusted")
    sentinel = outside / "skill.txt"
    sentinel.write_bytes(b"outside-sentinel")

    class SwapTree(WindowsHandleTreeIo):
        swapped = False

        def _before_child_open(self, _parent_handle: int, name: str) -> None:
            if self.swapped or name != "project":
                return
            self.swapped = True
            shutil.rmtree(root / "project")
            _create_junction(root / "project", outside)

    with pytest.raises(WindowsHandleIoError):
        SwapTree().read_exact_tree(root, ("project",), ("skill.txt",))
    assert sentinel.read_bytes() == b"outside-sentinel"
