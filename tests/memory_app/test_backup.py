import pytest

from backend.memory_app.backup import backup_database
from backend.recognition import RecognitionService, WorkScope
from core.storage_provider import SQLiteStructuredRecordStore


def test_online_backup_restores_authority_and_refuses_overwrite(tmp_path):
    source = tmp_path / "live.sqlite3"
    service = RecognitionService(SQLiteStructuredRecordStore(source))
    scope = WorkScope("local-user", "project-a")
    experience = service.stage_experience(scope=scope, content="由用户选择的工作经历")
    candidate = service.propose(scope=scope, content="可迁移的认识", source_experience_ids=[experience])
    original = service.publish(scope=scope, candidate_id=candidate.id, expected_revision=candidate.revision, reviewer="local-user")
    backup = tmp_path / "portable.sqlite3"
    receipt = backup_database(source, backup)
    assert receipt["records"] >= 3 and receipt["credentials_included"] is False
    restored = RecognitionService(SQLiteStructuredRecordStore(backup))
    assert restored.get_recognition(scope=scope, recognition_id=original.id).content == original.content
    assert restored.get_recognition(scope=scope, recognition_id=original.id).revision == original.revision
    with pytest.raises(ValueError, match="already exist"):
        backup_database(source, backup)


def test_backup_then_restore_preserves_versions_constraints_and_graph_views(tmp_path):
    from backend.memory_app.constraints import ProjectConstraintService
    from backend.memory_app.graph_views import GraphViewService
    source = tmp_path / "live.sqlite3"
    records = SQLiteStructuredRecordStore(source)
    service = RecognitionService(records)
    scope = WorkScope("local-user", "project-a")
    eid = service.stage_experience(scope=scope, content="Selected evidence")
    candidate = service.propose(scope=scope, content="Before", source_experience_ids=[eid])
    original = service.publish(scope=scope, candidate_id=candidate.id,
                               expected_revision=candidate.revision, reviewer="local-user")
    revised = service.revise(scope=scope, recognition_id=original.id,
                              expected_revision=original.revision, content="After")
    service.upsert_question(scope=scope, question_id="question", question="Next action?", content="After",
                            recognition_ids=[revised.id], source_revisions={revised.id: revised.revision},
                            expected_revision=0)
    constraint = ProjectConstraintService(records).upsert(scope, "rule", 0, "Use reviewed evidence")
    view = GraphViewService(records).upsert(scope, "layout", 0, node_ids=[eid, revised.id],
        positions={revised.id: {"x": 40, "y": 60}}, selected_ids=[revised.id],
        collapsed_ids=[], hidden_ids=[], focus_id=revised.id)
    expected = records.list_all()
    backup = tmp_path / "backup.sqlite3"
    target = tmp_path / "restored.sqlite3"
    backup_database(source, backup)
    backup_database(backup, target)
    restored = SQLiteStructuredRecordStore(target)
    assert restored.list_all() == expected
    assert RecognitionService(restored).get_recognition(scope=scope, recognition_id=revised.id) == revised
    assert ProjectConstraintService(restored).active(scope)[0]["content"] == constraint["content"]
    assert GraphViewService(restored).get(scope, "layout") == view
    assert RecognitionService(restored).get_recognition(
        scope=WorkScope("local-user", "another-project"), recognition_id=revised.id) is None


@pytest.mark.parametrize("corrupt", [False, True])
def test_backup_closes_connections_on_success_and_failure(tmp_path, monkeypatch, corrupt):
    import sqlite3
    source = tmp_path / "source.sqlite3"
    if corrupt:
        source.write_bytes(b"not a sqlite database" * 100)
    else:
        records = SQLiteStructuredRecordStore(source)
        with records.begin() as tx:
            tx.put("fixture", "record", {"content": "synthetic"}, expected_revision=0)
            tx.commit()
    original_connect = sqlite3.connect
    opened = []

    def connect(*args, **kwargs):
        connection = original_connect(*args, **kwargs)
        opened.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", connect)
    if corrupt:
        with pytest.raises(sqlite3.DatabaseError):
            backup_database(source, tmp_path / "backup.sqlite3")
    else:
        backup_database(source, tmp_path / "backup.sqlite3")
    assert len(opened) == 2
    for connection in opened:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")
