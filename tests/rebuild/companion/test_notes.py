from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pytest

from core.companion_core import (
    CompanionConflict,
    CompanionManualService,
    CompanionNotesService,
    CompanionRepositoryError,
)


FIXED_NOW = datetime(2026, 7, 19, 13, 14, 15, tzinfo=timezone.utc)


def test_development_manual_reads_root_readme_without_copying(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    readme = root / "readme.md"
    readme.write_text("# 动态说明\n\n快捷键：Ctrl+K", encoding="utf-8")
    service = CompanionManualService(development=True, repository_root=root)

    authority = service.authority()
    assert authority.path == readme
    assert authority.mode == "development"
    assert authority.seeded is False
    assert service.read_markdown() == "# 动态说明\n\n快捷键：Ctrl+K"
    assert list(root.iterdir()) == [readme]


def test_packaged_manual_seeds_once_and_upgrade_does_not_overwrite_user_copy(tmp_path: Path) -> None:
    resources = tmp_path / "resources"
    user_data = tmp_path / "user-data"
    seed = resources / "manual" / "readme.md"
    seed.parent.mkdir(parents=True)
    seed.write_text("seed v1", encoding="utf-8")
    service = CompanionManualService(
        development=False,
        resources_path=resources,
        user_data_root=user_data,
    )
    first = service.authority()
    assert first.seeded is True
    assert service.read_markdown() == "seed v1"

    first.path.write_text("用户自己修改", encoding="utf-8")
    seed.write_text("seed v2", encoding="utf-8")
    second = service.authority()
    assert second.seeded is False
    assert service.read_markdown() == "用户自己修改"


@pytest.mark.parametrize("mode", ["development", "packaged"])
def test_missing_manual_fails_with_diagnostic_error(tmp_path: Path, mode: str) -> None:
    service = CompanionManualService(
        development=mode == "development",
        repository_root=tmp_path / "missing-repository",
        resources_path=tmp_path / "missing-resources",
        user_data_root=tmp_path / "user-data",
    )
    with pytest.raises(CompanionRepositoryError, match="missing|root"):
        service.authority()


def test_manual_rejects_directory_symlink_and_oversize(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    (root / "readme.md").mkdir()
    with pytest.raises(CompanionRepositoryError, match="unsafe"):
        CompanionManualService(development=True, repository_root=root).authority()

    (root / "readme.md").rmdir()
    (root / "readme.md").write_bytes(b"x" * (2 * 1024 * 1024 + 1))
    with pytest.raises(CompanionRepositoryError, match="too large"):
        CompanionManualService(development=True, repository_root=root).read_markdown()


def test_packaged_manual_write_failure_is_normalized(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    resources = tmp_path / "resources"
    seed = resources / "manual" / "readme.md"
    seed.parent.mkdir(parents=True)
    seed.write_text("seed", encoding="utf-8")
    monkeypatch.setattr(os, "replace", lambda *_args: (_ for _ in ()).throw(PermissionError("read only")))
    service = CompanionManualService(
        development=False,
        resources_path=resources,
        user_data_root=tmp_path / "user-data",
    )
    with pytest.raises(CompanionRepositoryError, match="cannot be initialized"):
        service.authority()


def notes_service(root: Path, *, note_id: str = "note-fixed") -> CompanionNotesService:
    return CompanionNotesService(root, now=lambda: FIXED_NOW, id_factory=lambda: note_id)


def test_note_append_is_visible_utf8_and_round_trips_multiline_unicode(tmp_path: Path) -> None:
    service = notes_service(tmp_path)
    note = service.append("灵感：熊熊 🐻\n第二行\\路径字面量")

    assert note.note_id == "note-fixed"
    assert service.path == tmp_path / "companion" / "notes.txt"
    raw = service.path.read_text(encoding="utf-8")
    assert raw.startswith("[2026-07-19T13:14:15+00:00][note-fixed] ")
    assert "\\n" in raw
    assert service.list() == (note,)


def test_note_duplicate_id_and_invalid_content_fail_closed(tmp_path: Path) -> None:
    service = notes_service(tmp_path)
    service.append("第一条")
    with pytest.raises(CompanionConflict, match="already exists"):
        service.append("第二条")
    for invalid in ("", " \n ", "bad\x00text", "x" * 4_001):
        with pytest.raises(CompanionRepositoryError):
            notes_service(tmp_path, note_id="note-other").append(invalid)


def test_concurrent_unique_appends_preserve_every_complete_line(tmp_path: Path) -> None:
    def append(index: int) -> None:
        notes_service(tmp_path, note_id=f"note-{index}").append(f"并发笔记 {index}")

    with ThreadPoolExecutor(max_workers=8) as executor:
        tuple(executor.map(append, range(32)))

    notes = CompanionNotesService(tmp_path).list()
    assert len(notes) == 32
    assert {note.note_id for note in notes} == {f"note-{index}" for index in range(32)}


def test_edit_and_delete_use_atomic_replacement_and_backup(tmp_path: Path) -> None:
    first = notes_service(tmp_path, note_id="note-one")
    second = notes_service(tmp_path, note_id="note-two")
    first.append("第一版")
    second.append("保留")
    before_edit = first.path.read_bytes()

    updated = first.edit("note-one", "第二版")
    assert updated.content == "第二版"
    assert first.backup_path.read_bytes() == before_edit
    before_delete = first.path.read_bytes()
    assert first.delete("note-one") is True
    assert first.backup_path.read_bytes() == before_delete
    assert [note.note_id for note in first.list()] == ["note-two"]
    assert first.delete("note-missing") is False


def test_invalid_or_duplicate_stored_lines_are_rejected(tmp_path: Path) -> None:
    service = CompanionNotesService(tmp_path)
    service.root.mkdir(parents=True)
    service.path.write_text("not-a-note\n", encoding="utf-8")
    with pytest.raises(CompanionRepositoryError, match="invalid line"):
        service.list()

    line = "[2026-07-19T13:14:15+00:00][note-duplicate] content\n"
    service.path.write_text(line + line, encoding="utf-8")
    with pytest.raises(CompanionRepositoryError, match="duplicate"):
        service.list()


def test_random_review_is_default_off_and_validates_injected_index(tmp_path: Path) -> None:
    service = notes_service(tmp_path)
    note = service.append("只在本地复习")
    called = False

    def should_not_run(_length: int) -> int:
        nonlocal called
        called = True
        return 0

    assert service.review(enabled=False, random_index=should_not_run) is None
    assert called is False
    assert service.review(enabled=True, random_index=lambda length: length - 1) == note
    with pytest.raises(CompanionRepositoryError, match="random source"):
        service.review(enabled=True, random_index=lambda length: length)
