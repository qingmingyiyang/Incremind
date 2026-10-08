import pytest

from backend.memory_app.constraints import ProjectConstraintError, ProjectConstraintService
from backend.recognition import WorkScope
from core.storage_provider import SQLiteStructuredRecordStore


def _scope(project_id="project-a"):
    return WorkScope("local-user", project_id)


def _service(tmp_path, now="2026-09-16T12:00:00Z"):
    return ProjectConstraintService(SQLiteStructuredRecordStore(tmp_path / "records.sqlite3"), now=lambda: now)


def test_lists_only_explicit_project_constraints_and_marks_effective(tmp_path):
    service = _service(tmp_path)
    created = service.upsert(_scope(), "web-only", 0, "Use the web prototype.")
    service.upsert(_scope("project-b"), "other", 0, "Other project.")
    disabled = service.upsert(_scope(), "paused", 0, "Keep visible.", enabled=False)

    listed = service.list(_scope())
    assert [item["id"] for item in listed] == ["paused", "web-only"]
    assert [item["effective"] for item in listed] == [False, True]
    assert service.active(_scope()) == (created,)
    assert disabled["revision"] == 1 and "memory_role" not in created


def test_time_window_requires_timezone_and_uses_closed_open_interval(tmp_path):
    service = _service(tmp_path, "2026-09-16T12:00:00Z")
    current = service.upsert(_scope(), "current", 0, "Current", valid_from="2026-09-16T12:00:00+00:00", valid_until="2026-09-16T13:00:00Z")
    service.upsert(_scope(), "expired", 0, "Expired", valid_until="2026-09-16T12:00:00Z")
    with pytest.raises(ProjectConstraintError, match="timezone"):
        service.upsert(_scope(), "bad", 0, "Bad", valid_from="2026-09-16T12:00:00")
    with pytest.raises(ProjectConstraintError, match="before"):
        service.upsert(_scope(), "empty", 0, "Empty", valid_from="2026-09-16T13:00:00Z", valid_until="2026-09-16T13:00:00Z")
    assert service.active(_scope()) == (current,)


def test_upsert_is_cas_and_noop_preserves_revision_and_server_timestamps(tmp_path):
    service = _service(tmp_path)
    first = service.upsert(_scope(), "constraint", 0, "Use SQLite.")
    replay = service.upsert(_scope(), "constraint", 1, "Use SQLite.")
    assert replay == first and replay["created_at"] == "2026-09-16T12:00:00Z"
    with pytest.raises(ProjectConstraintError, match="expected revision 0, found 1"):
        service.upsert(_scope(), "constraint", 0, "Changed.")
    changed = service.upsert(_scope(), "constraint", 1, "Changed.", enabled=False)
    assert changed["revision"] == 2 and not changed["effective"]
    with pytest.raises(ProjectConstraintError, match="unavailable"):
        service.upsert(_scope("project-b"), "constraint", 2, "Foreign")


def test_snapshot_requires_exact_current_active_id_revision_set(tmp_path):
    service = _service(tmp_path)
    first = service.upsert(_scope(), "first", 0, "First")
    snapshot = [{"id": first["id"], "revision": first["revision"], "content": "ignored"}]
    assert service.validate_snapshot(_scope(), snapshot) == (first,)

    edited = service.upsert(_scope(), "first", 1, "First revised")
    with pytest.raises(ProjectConstraintError, match="preview again"):
        service.validate_snapshot(_scope(), snapshot)
    assert service.validate_snapshot(_scope(), [{"id": "first", "revision": edited["revision"]}]) == (edited,)

    second = service.upsert(_scope(), "second", 0, "Second")
    with pytest.raises(ProjectConstraintError, match="preview again"):
        service.validate_snapshot(_scope(), [{"id": "first", "revision": edited["revision"]}])
    current = [{"id": edited["id"], "revision": edited["revision"]}, {"id": second["id"], "revision": second["revision"]}]
    assert service.validate_snapshot(_scope(), current) == (edited, second)
    disabled = service.upsert(_scope(), "second", 1, "Second", enabled=False)
    with pytest.raises(ProjectConstraintError, match="preview again"):
        service.validate_snapshot(_scope(), current)
    assert service.validate_snapshot(_scope(), [{"id": "first", "revision": edited["revision"]}]) == (edited,)


def test_old_empty_snapshot_conflicts_when_new_constraint_becomes_effective(tmp_path):
    service = _service(tmp_path, "2026-09-16T12:00:00Z")
    assert service.validate_snapshot(_scope(), []) == ()
    service.upsert(_scope(), "later", 0, "Later", valid_from="2026-09-16T11:00:00Z")
    with pytest.raises(ProjectConstraintError, match="preview again"):
        service.validate_snapshot(_scope(), [])


def test_automatic_expiry_invalidates_a_previously_current_snapshot(tmp_path):
    clock = ["2026-09-16T12:00:00Z"]
    service = ProjectConstraintService(
        SQLiteStructuredRecordStore(tmp_path / "records.sqlite3"), now=lambda: clock[0]
    )
    item = service.upsert(_scope(), "temporary", 0, "Temporary", valid_until="2026-09-16T13:00:00Z")
    snapshot = [{"id": item["id"], "revision": item["revision"]}]
    assert service.validate_snapshot(_scope(), snapshot) == (item,)
    clock[0] = "2026-09-16T13:00:00Z"
    with pytest.raises(ProjectConstraintError, match="preview again"):
        service.validate_snapshot(_scope(), snapshot)
    assert service.validate_snapshot(_scope(), []) == ()
