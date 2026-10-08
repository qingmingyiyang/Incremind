from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from core.product_core import (
    CreateLegacyLibraryDryRunReport,
    LegacyLibraryDryRunError,
    ReviewLegacyLibraryDryRunReport,
)
from core.storage_provider import JsonObjectStore, ObjectStoreLegacyLibraryDryRunRepository


def test_legacy_library_dry_run_persists_read_only_report_without_mutating_legacy(
    tmp_path: Path,
) -> None:
    legacy_root = tmp_path / "library"
    (legacy_root / "notes").mkdir(parents=True)
    (legacy_root / "data").mkdir(parents=True)
    note = legacy_root / "notes" / "a.md"
    data = legacy_root / "data" / "item.json"
    note.write_text("# A\nlegacy markdown\n", encoding="utf-8")
    data.write_text('{"name":"legacy"}\n', encoding="utf-8")
    before = _legacy_state(legacy_root)

    object_store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=legacy_root)
    repository = ObjectStoreLegacyLibraryDryRunRepository(object_store)
    use_case = CreateLegacyLibraryDryRunReport(reports=repository)

    result = use_case.execute(
        legacy_root=legacy_root,
        requested_by="user",
        created_at=datetime(2026, 6, 30, 8, 0, tzinfo=UTC),
    )

    report = result.report
    serialized = json.dumps(report, ensure_ascii=False, sort_keys=True)
    assert report["status"] == "review_ready"
    assert report["mode"] == "read_only"
    assert report["file_count"] == 2
    assert report["total_bytes"] == sum(item["size"] for item in before.values())
    assert report["legacy_root_uri"] == "legacy-readonly://library"
    assert str(legacy_root) not in serialized
    assert "file://" not in serialized
    assert report["safety"] == {
        "legacy_write_allowed": False,
        "migration_executed": False,
        "published_outputs": [],
    }
    assert report["review_decision"] is None
    assert object_store.list("jobs") == ()

    candidates = report["candidates"]
    assert isinstance(candidates, list)
    assert {candidate["relative_path"] for candidate in candidates} == {
        "data/item.json",
        "notes/a.md",
    }
    assert {candidate["kind"] for candidate in candidates} == {"json", "markdown"}
    assert all(not Path(candidate["relative_path"]).is_absolute() for candidate in candidates)
    assert all(candidate["imported"] is False for candidate in candidates)
    assert repository.get(result.report_id) == report
    assert _legacy_state(legacy_root) == before


def test_legacy_library_dry_run_report_can_be_accepted_for_planning_without_migration(
    tmp_path: Path,
) -> None:
    legacy_root = tmp_path / "library"
    legacy_root.mkdir()
    legacy_file = legacy_root / "notes.md"
    legacy_file.write_text("legacy planning candidate\n", encoding="utf-8")
    before = _legacy_state(legacy_root)
    object_store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=legacy_root)
    repository = ObjectStoreLegacyLibraryDryRunRepository(object_store)
    report = CreateLegacyLibraryDryRunReport(reports=repository).execute(
        legacy_root=legacy_root,
        requested_by="user",
        created_at=datetime(2026, 6, 30, 8, 10, tzinfo=UTC),
    ).report

    reviewed = ReviewLegacyLibraryDryRunReport(reports=repository).accept_for_planning(
        report_id=str(report["id"]),
        reviewed_by="user",
        reviewed_at=datetime(2026, 6, 30, 8, 11, tzinfo=UTC),
        note="Use this report for later migration planning.",
    ).report

    assert reviewed["status"] == "accepted_for_planning"
    assert reviewed["review_decision"] == {
        "decision": "accepted_for_planning",
        "reviewed_by": "user",
        "reviewed_at": "2026-06-30T08:11:00Z",
        "note": "Use this report for later migration planning.",
        "migration_approved": False,
        "migration_executed": False,
        "published_outputs": [],
    }
    assert reviewed["safety"] == {
        "legacy_write_allowed": False,
        "migration_executed": False,
        "published_outputs": [],
    }
    assert all(candidate["imported"] is False for candidate in reviewed["candidates"])
    assert repository.get(str(report["id"])) == reviewed
    assert object_store.list("jobs") == ()
    assert object_store.list("memory_atoms") == ()
    assert _legacy_state(legacy_root) == before


def test_legacy_library_dry_run_report_can_be_rejected_without_side_effects(tmp_path: Path) -> None:
    legacy_root = tmp_path / "library"
    legacy_root.mkdir()
    (legacy_root / "notes.md").write_text("reject this legacy candidate\n", encoding="utf-8")
    before = _legacy_state(legacy_root)
    object_store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=legacy_root)
    repository = ObjectStoreLegacyLibraryDryRunRepository(object_store)
    report = CreateLegacyLibraryDryRunReport(reports=repository).execute(
        legacy_root=legacy_root,
        requested_by="user",
        created_at=datetime(2026, 6, 30, 8, 12, tzinfo=UTC),
    ).report

    reviewed = ReviewLegacyLibraryDryRunReport(reports=repository).reject(
        report_id=str(report["id"]),
        reviewed_by="user",
        reviewed_at=datetime(2026, 6, 30, 8, 13, tzinfo=UTC),
        note=None,
    ).report

    assert reviewed["status"] == "rejected"
    assert reviewed["review_decision"]["decision"] == "rejected"
    assert reviewed["review_decision"]["migration_approved"] is False
    assert reviewed["review_decision"]["migration_executed"] is False
    assert reviewed["review_decision"]["published_outputs"] == []
    assert object_store.list("jobs") == ()
    assert _legacy_state(legacy_root) == before


def test_legacy_library_dry_run_review_requires_review_ready_report(tmp_path: Path) -> None:
    legacy_root = tmp_path / "library"
    legacy_root.mkdir()
    (legacy_root / "notes.md").write_text("one review only\n", encoding="utf-8")
    repository = ObjectStoreLegacyLibraryDryRunRepository(
        JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=legacy_root)
    )
    report = CreateLegacyLibraryDryRunReport(reports=repository).execute(
        legacy_root=legacy_root,
        requested_by="user",
        created_at=datetime(2026, 6, 30, 8, 14, tzinfo=UTC),
    ).report
    review = ReviewLegacyLibraryDryRunReport(reports=repository)
    review.accept_for_planning(
        report_id=str(report["id"]),
        reviewed_by="user",
        reviewed_at=datetime(2026, 6, 30, 8, 15, tzinfo=UTC),
    )

    with pytest.raises(LegacyLibraryDryRunError, match="review_ready status"):
        review.reject(
            report_id=str(report["id"]),
            reviewed_by="user",
            reviewed_at=datetime(2026, 6, 30, 8, 16, tzinfo=UTC),
        )


def test_legacy_library_dry_run_requires_existing_root_and_never_creates_it(tmp_path: Path) -> None:
    missing_legacy_root = tmp_path / "library"
    object_store = JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=missing_legacy_root)
    repository = ObjectStoreLegacyLibraryDryRunRepository(object_store)
    use_case = CreateLegacyLibraryDryRunReport(reports=repository)

    with pytest.raises(LegacyLibraryDryRunError, match="legacy root must already exist"):
        use_case.execute(legacy_root=missing_legacy_root, requested_by="user")

    assert not missing_legacy_root.exists()
    assert repository.list_reports() == ()


def test_legacy_library_dry_run_repository_rejects_paths_and_write_flags(tmp_path: Path) -> None:
    legacy_root = tmp_path / "library"
    legacy_root.mkdir()
    repository = ObjectStoreLegacyLibraryDryRunRepository(
        JsonObjectStore(tmp_path / ".rebuild-data", legacy_root=legacy_root)
    )

    with pytest.raises(ValueError, match="candidate path must be relative"):
        repository.save_report(
            {
                "schema_version": "1.0.0",
                "id": "legacy-dry-run-badpath",
                "status": "review_ready",
                "legacy_root_uri": "legacy-readonly://library",
                "mode": "read_only",
                "candidates": (
                    {
                        "candidate_id": "legacy-source-bad",
                        "relative_path": "C:\\library\\x.md",
                        "kind": "markdown",
                        "size_bytes": 1,
                        "content_sha256": "sha256:abc",
                        "source_ref": "legacy-readonly://library/x.md#sha256:abc",
                        "imported": False,
                    },
                ),
                "safety": {
                    "legacy_write_allowed": False,
                    "migration_executed": False,
                    "published_outputs": (),
                },
            }
        )

    with pytest.raises(ValueError, match="must not allow legacy writes"):
        repository.save_report(
            {
                "schema_version": "1.0.0",
                "id": "legacy-dry-run-write",
                "status": "review_ready",
                "legacy_root_uri": "legacy-readonly://library",
                "mode": "read_only",
                "candidates": (),
                "safety": {
                    "legacy_write_allowed": True,
                    "migration_executed": False,
                    "published_outputs": (),
                },
            }
        )

    accepted_with_outputs = {
        "schema_version": "1.0.0",
        "id": "legacy-dry-run-output",
        "status": "accepted_for_planning",
        "legacy_root_uri": "legacy-readonly://library",
        "mode": "read_only",
        "candidates": (),
        "safety": {
            "legacy_write_allowed": False,
            "migration_executed": False,
            "published_outputs": (),
        },
        "review_decision": {
            "decision": "accepted_for_planning",
            "reviewed_by": "user",
            "reviewed_at": "2026-06-30T08:17:00Z",
            "note": None,
            "migration_approved": False,
            "migration_executed": False,
            "published_outputs": ["crp://default/memory/atom/legacy"],
        },
    }
    with pytest.raises(ValueError, match="must not publish outputs"):
        repository.save_report(accepted_with_outputs)


def _legacy_state(root: Path) -> dict[str, dict[str, object]]:
    state: dict[str, dict[str, object]] = {}
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative_path = path.relative_to(root).as_posix()
        state[relative_path] = {
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "mtime_ns": path.stat().st_mtime_ns,
            "size": path.stat().st_size,
        }
    return state
