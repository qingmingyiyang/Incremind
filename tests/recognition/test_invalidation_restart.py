"""Durable invalidation and rollback across fresh SQLite service instances."""
import pytest

from backend.recognition import RecognitionConflict, RecognitionService, WorkScope
from core.storage_provider import SQLiteStructuredRecordStore


@pytest.mark.parametrize("failure_point", [None, "_append_version", "_invalidate_questions"])
def test_cascade_is_atomic_and_remains_excluded_after_reopening(tmp_path, monkeypatch, failure_point):
    path = tmp_path / "recognitions.sqlite3"
    store = SQLiteStructuredRecordStore(path)
    service = RecognitionService(store)
    scope = WorkScope("local-user", "project-one")
    eid = service.stage_experience(scope=scope, content="Original evidence")
    candidate = service.propose(scope=scope, content="First conclusion", source_experience_ids=[eid])
    first = service.publish(scope=scope, candidate_id=candidate.id,
                            expected_revision=candidate.revision, reviewer="local-user")
    candidate = service.propose(scope=scope, content="Derived conclusion",
                                source_experience_ids=[], source_recognition_ids=[first.id])
    second = service.publish(scope=scope, candidate_id=candidate.id,
                             expected_revision=candidate.revision, reviewer="local-user")
    pending = service.propose(scope=scope, content="Late draft",
                              source_experience_ids=[], source_recognition_ids=[second.id])
    service.upsert_question(scope=scope, question_id="delivery", question="What next?",
                            content="Use derived conclusion", recognition_ids=[second.id],
                            source_revisions={second.id: second.revision}, expected_revision=0)
    experience = service.list_experiences(scope=scope)[0]
    before = store.list_all()

    if failure_point:
        original = getattr(service, failure_point)

        def write_then_fail(*args, **kwargs):
            original(*args, **kwargs)
            raise OSError("injected failure before commit")

        monkeypatch.setattr(service, failure_point, write_then_fail)
        with pytest.raises(OSError, match="injected failure"):
            service.revoke_experience(scope=scope, experience_id=eid,
                                      expected_revision=experience.revision)
        reopened_store = SQLiteStructuredRecordStore(path)
        assert reopened_store.list_all() == before
        service = RecognitionService(reopened_store)

    # A retry after rollback uses the original revision; a committed revoke is durable.
    service.revoke_experience(scope=scope, experience_id=eid,
                              expected_revision=experience.revision)
    committed = SQLiteStructuredRecordStore(path).list_all()
    reopened = RecognitionService(SQLiteStructuredRecordStore(path))
    assert reopened.get_recognition(scope=scope, recognition_id=first.id).state == "stale"
    assert reopened.get_recognition(scope=scope, recognition_id=second.id).state == "stale"
    assert reopened.list_questions(scope=scope)[0].state == "stale"
    assert reopened.retrieval_entries(scope=scope) == ()
    invalid = next(item for item in reopened.list_candidates(scope=scope, include_inactive=True)
                   if item.id == pending.id)
    assert invalid.state == "invalidated"
    with pytest.raises(RecognitionConflict, match="pending"):
        reopened.publish(scope=scope, candidate_id=invalid.id,
                         expected_revision=invalid.revision, reviewer="local-user")
    assert SQLiteStructuredRecordStore(path).list_all() == committed
