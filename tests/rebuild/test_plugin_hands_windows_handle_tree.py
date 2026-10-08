from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from core.plugin_hands.windows_handle_tree import WindowsHandleTreeError, WindowsHandleTreeRemover


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows NT handle-relative cleanup")


def test_removes_ordinary_multilevel_tree(tmp_path: Path) -> None:
    root = tmp_path / "root"
    target = root / "lease-1"
    (target / "code" / "payload").mkdir(parents=True)
    (target / "code" / "payload" / "main.py").write_text("print('ok')", encoding="utf-8")
    (target / "output" / "nested").mkdir(parents=True)
    (target / "output" / "nested" / "result.txt").write_text("done", encoding="utf-8")
    os.chmod(target / "output" / "nested" / "result.txt", stat.S_IREAD)
    sentinel = root / "sibling-sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")

    assert WindowsHandleTreeRemover().remove(root, "lease-1") is True

    assert not target.exists()
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_rejects_target_reparse_point_and_preserves_external_sentinel(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "sentinel.txt"
    sentinel.write_text("never-delete", encoding="utf-8")
    target = root / "lease-1"
    _directory_reparse_or_skip(target, external)

    with pytest.raises(WindowsHandleTreeError, match="reparse point"):
        WindowsHandleTreeRemover().remove(root, "lease-1")

    assert target.exists()
    assert sentinel.read_text(encoding="utf-8") == "never-delete"


def test_enumeration_to_open_replacement_race_fails_closed_and_preserves_external_sentinel(tmp_path: Path) -> None:
    root = tmp_path / "root"
    target = root / "lease-1"
    victim = target / "victim"
    victim.mkdir(parents=True)
    (victim / "ordinary.txt").write_text("inside", encoding="utf-8")
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "sentinel.txt"
    sentinel.write_text("never-delete", encoding="utf-8")

    class _RaceRemover(WindowsHandleTreeRemover):
        def __init__(self) -> None:
            self.fired = False

        def _before_child_open(self, _parent_handle: int, name: str) -> None:
            if self.fired or name != "victim":
                return
            self.fired = True
            shutil.rmtree(victim)
            _directory_reparse_or_skip(victim, external)

    remover = _RaceRemover()
    with pytest.raises(WindowsHandleTreeError, match="reparse point"):
        remover.remove(root, "lease-1")

    assert remover.fired is True
    assert sentinel.read_text(encoding="utf-8") == "never-delete"
    assert victim.exists()


def test_rejects_non_component_target(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()

    with pytest.raises(WindowsHandleTreeError, match="one component"):
        WindowsHandleTreeRemover().remove(root, "lease-1/child")


def test_missing_target_is_an_idempotent_noop(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    sentinel = root / "sibling.txt"
    sentinel.write_text("keep", encoding="utf-8")

    assert WindowsHandleTreeRemover().remove(root, "missing-lease") is False
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_root_path_replacement_after_handle_open_cannot_redirect_cleanup(tmp_path: Path) -> None:
    root = tmp_path / "root"
    target = root / "lease-1"
    target.mkdir(parents=True)
    (target / "owned.txt").write_text("owned", encoding="utf-8")
    external = tmp_path / "external"
    external_target = external / "lease-1"
    external_target.mkdir(parents=True)
    sentinel = external_target / "sentinel.txt"
    sentinel.write_text("never-delete", encoding="utf-8")
    parked_root = tmp_path / "parked-root"

    class _RootRaceRemover(WindowsHandleTreeRemover):
        def _after_root_open(self, _root_handle: int) -> None:
            root.rename(parked_root)
            _directory_reparse_or_skip(root, external)

    assert _RootRaceRemover().remove(root, "lease-1") is True

    assert not (parked_root / "lease-1").exists()
    assert sentinel.read_text(encoding="utf-8") == "never-delete"


def _directory_reparse_or_skip(link: Path, target: Path) -> None:
    try:
        os.symlink(target, link, target_is_directory=True)
        return
    except OSError as error:
        symlink_error = error
    # Directory junctions are reparse points too, but normally do not require
    # the SeCreateSymbolicLink privilege that a symlink does.
    result = subprocess.run(
        ["cmd", "/d", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        pytest.skip(f"directory reparse creation unavailable: symlink={symlink_error}; junction={result.stderr.strip()}")
