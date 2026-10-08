from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from core.storage_provider import JsonObjectStore, ObjectStorePathError
from core.storage_provider import runtime as storage_runtime


COLLECTION = "legacy_migration_staged_memory_candidate_review_blocks"
OBJECT_ID = "legacy-migration-staged-atom-scenario-review-block-449e4c42dca0db29"
LONG_OBJECT_ID = "object-" + "x" * 121


def _root_at_windows_temp_boundary(tmp_path: Path) -> tuple[Path, Path, Path]:
    for depth in range(121):
        root = tmp_path / ("d" * depth) / ".rebuild-data"
        meta_path = root / "objects" / "default" / COLLECTION / f"{OBJECT_ID}.meta.json"
        legacy_temporary = meta_path.with_name(f"{meta_path.name}.tmp")
        if len(str(meta_path)) <= 259 < len(str(legacy_temporary)):
            return root, meta_path, legacy_temporary
    raise AssertionError("could not construct a Windows temporary-path boundary fixture")


def _root_past_windows_final_path_budget(tmp_path: Path, object_id: str) -> tuple[Path, Path]:
    for depth in range(121):
        root = tmp_path / ("d" * depth) / ".rebuild-data"
        meta_path = root / "objects" / "default" / COLLECTION / f"{object_id}.meta.json"
        short_meta_path = root / "objects" / "default" / COLLECTION / (
            f"{storage_runtime._short_object_file_stem(object_id)}.meta.json"
        )
        if len(str(meta_path)) > 259 and len(str(short_meta_path)) <= 259:
            return root, meta_path
    raise AssertionError("could not construct a Windows final-path overflow fixture")


def test_object_store_writes_at_windows_temporary_path_boundary(tmp_path: Path) -> None:
    root, meta_path, legacy_temporary = _root_at_windows_temp_boundary(tmp_path)
    store = JsonObjectStore(root, legacy_root=tmp_path / "library")

    revision = store.write(COLLECTION, OBJECT_ID, {"id": OBJECT_ID}, expected_revision=0)

    assert len(str(meta_path)) <= 259 < len(str(legacy_temporary))
    assert revision == 1
    assert store.read(COLLECTION, OBJECT_ID) == {"id": OBJECT_ID}
    assert meta_path.exists()
    assert (meta_path.parent / f"{OBJECT_ID}.json").exists()
    assert store.write(COLLECTION, OBJECT_ID, {"id": OBJECT_ID}, expected_revision=1) == 2
    assert store.list(COLLECTION) == ({"id": OBJECT_ID},)
    assert store.delete(COLLECTION, OBJECT_ID) is True
    assert store.read(COLLECTION, OBJECT_ID) is None


def test_object_store_uses_a_short_final_name_past_windows_path_budget(tmp_path: Path) -> None:
    root, legacy_meta_path = _root_past_windows_final_path_budget(tmp_path, LONG_OBJECT_ID)
    store = JsonObjectStore(root, legacy_root=tmp_path / "library")

    revision = store.write(COLLECTION, LONG_OBJECT_ID, {"id": LONG_OBJECT_ID}, expected_revision=0)

    assert len(str(legacy_meta_path)) > 259
    assert revision == 1
    short_stem = storage_runtime._short_object_file_stem(LONG_OBJECT_ID)
    short_payload_path = legacy_meta_path.with_name(f"{short_stem}.json")
    short_meta_path = legacy_meta_path.with_name(f"{short_stem}.meta.json")
    assert not legacy_meta_path.exists()
    assert short_payload_path.exists()
    assert json.loads(short_meta_path.read_text(encoding="utf-8")) == {
        "object_id": LONG_OBJECT_ID,
        "revision": 1,
    }
    assert store.revision(COLLECTION, LONG_OBJECT_ID) == 1
    assert store.read(COLLECTION, LONG_OBJECT_ID) == {"id": LONG_OBJECT_ID}
    assert store.list(COLLECTION) == ({"id": LONG_OBJECT_ID},)
    assert store.delete(COLLECTION, LONG_OBJECT_ID) is True


def test_short_final_name_rejects_identity_mismatch_and_ambiguous_layout(tmp_path: Path) -> None:
    collection = "items"
    ambiguous_id = "object-" + "x" * 48
    store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=tmp_path / "library")
    directory = store._collection_path(collection)
    directory.mkdir(parents=True)
    short_stem = storage_runtime._short_object_file_stem(ambiguous_id)
    (directory / f"{short_stem}.json").write_text('{"id": "untrusted"}', encoding="utf-8")

    with pytest.raises(ObjectStorePathError, match="layout is incomplete"):
        store.read(collection, ambiguous_id)

    (directory / f"{short_stem}.meta.json").write_text(
        json.dumps({"object_id": OBJECT_ID, "revision": 1}), encoding="utf-8"
    )

    with pytest.raises(ObjectStorePathError, match="identity does not match filename"):
        store.read(collection, ambiguous_id)

    (directory / f"{short_stem}.meta.json").write_text(
        json.dumps({"object_id": ambiguous_id, "revision": 1}), encoding="utf-8"
    )
    (directory / f"{ambiguous_id}.json").write_text('{"id": "legacy"}', encoding="utf-8")

    with pytest.raises(ObjectStorePathError, match="ambiguous legacy and short layouts"):
        store.read(collection, ambiguous_id)


def test_short_final_name_fails_closed_when_even_short_path_exceeds_budget(tmp_path: Path) -> None:
    for depth in range(121):
        root = tmp_path / ("d" * depth) / ".rebuild-data"
        short_meta_path = root / "objects" / "default" / COLLECTION / (
            f"{storage_runtime._short_object_file_stem(LONG_OBJECT_ID)}.meta.json"
        )
        if len(str(short_meta_path)) > 259:
            break
    else:  # pragma: no cover - platform fixture guard
        raise AssertionError("could not construct a short-path overflow fixture")

    store = JsonObjectStore(root, legacy_root=tmp_path / "library")

    with pytest.raises(ObjectStorePathError, match="Windows-compatible budget"):
        store.write(COLLECTION, LONG_OBJECT_ID, {"id": LONG_OBJECT_ID}, expected_revision=0)

    assert not (root / "objects").exists()


def test_atomic_writes_use_unique_same_directory_temporary_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "target.json"
    barrier = threading.Barrier(3)
    release = threading.Event()
    original_fsync = storage_runtime.os.fsync
    errors: list[Exception] = []

    def hold_fsync(file_descriptor: int) -> None:
        barrier.wait(timeout=5)
        if not release.wait(timeout=5):
            raise TimeoutError("test did not release temporary writers")
        original_fsync(file_descriptor)

    def write(payload: dict[str, str]) -> None:
        try:
            storage_runtime._write_json_atomic(target, payload)
        except Exception as error:  # pragma: no cover - asserted after join
            errors.append(error)

    monkeypatch.setattr(storage_runtime.os, "fsync", hold_fsync)
    threads = [
        threading.Thread(target=write, args=({"writer": "left"},)),
        threading.Thread(target=write, args=({"writer": "right"},)),
    ]
    for thread in threads:
        thread.start()

    try:
        barrier.wait(timeout=5)
        temporary_files = list(tmp_path.glob(".tmp-*.tmp"))
        assert len(temporary_files) == 2
        assert len({path.name for path in temporary_files}) == 2
    finally:
        release.set()
        for thread in threads:
            thread.join(timeout=5)

    assert errors == []
    assert json.loads(target.read_text(encoding="utf-8")) in (
        {"writer": "left"},
        {"writer": "right"},
    )
    assert list(tmp_path.glob(".tmp-*.tmp")) == []


def test_atomic_write_cleans_its_temporary_file_when_replace_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "target.json"

    def fail_replace(_temporary: Path, _target: Path) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(storage_runtime, "_replace_temporary_file", fail_replace)

    with pytest.raises(OSError, match="simulated replace failure"):
        storage_runtime._write_json_atomic(target, {"id": "target"})

    assert not target.exists()
    assert list(tmp_path.glob(".tmp-*.tmp")) == []


def test_atomic_writes_keep_different_targets_parallel_during_final_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    barrier = threading.Barrier(2)
    original_replace = storage_runtime._replace_temporary_file
    errors: list[Exception] = []

    def observe_replace(temporary: Path, target: Path) -> None:
        barrier.wait(timeout=5)
        original_replace(temporary, target)

    def write(target: Path) -> None:
        try:
            storage_runtime._write_json_atomic(target, {"target": target.name})
        except Exception as error:  # pragma: no cover - asserted after join
            errors.append(error)

    monkeypatch.setattr(storage_runtime, "_replace_temporary_file", observe_replace)
    threads = [
        threading.Thread(target=write, args=(tmp_path / "left.json",)),
        threading.Thread(target=write, args=(tmp_path / "right.json",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert errors == []
    assert json.loads((tmp_path / "left.json").read_text(encoding="utf-8")) == {"target": "left.json"}
    assert json.loads((tmp_path / "right.json").read_text(encoding="utf-8")) == {"target": "right.json"}
