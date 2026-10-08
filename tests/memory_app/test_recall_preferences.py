import pytest

from backend.recognition import RecognitionService, WorkScope, RecognitionConflict
from backend.recognition_retrieval import retrieve
from backend.memory_app.context_adapter import compile_selected
from backend.memory_app.recall_preferences import set_preference, annotate
from backend.memory_app.erasure import ErasureService
from core.storage_provider import SQLiteStructuredRecordStore, SQLiteUnitOfWorkConflict


def test_cooling_changes_rank_without_invalidating_truth_or_manual_selection(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / "memory.sqlite3")
    service = RecognitionService(records)
    scope = WorkScope("local-user", "p1")
    experience = service.stage_experience(scope=scope, content="原型采用网页验证")
    for item_id in ("a", "b"):
        candidate = service.propose(scope=scope, content="网页原型验证", source_experience_ids=[experience])
        service.publish(scope=scope, candidate_id=candidate.id, expected_revision=1, reviewer="local-user", recognition_id=item_id)
    service.upsert_question(scope=scope, question_id="q1", question="如何验证", content="采用网页原型",
        recognition_ids=["a"], source_revisions={"a": 1}, expected_revision=0)
    result = set_preference(records, scope, "a", recognition_revision=1, preference_revision=0, state="cooled")
    assert result == {"recall_state": "cooled", "recall_preference_revision": 1}
    entries = annotate(records, scope, service.retrieval_entries(scope=scope))
    retrieved = retrieve("p1", "网页原型验证", entries)
    assert [hit.id for hit in retrieved.hits] == ["b", "a"]
    assert retrieved.trace["recall_priority"]["cooled_ids"] == ["a"]
    assert service.get_recognition(scope=scope, recognition_id="a").revision == 1
    assert service.list_questions(scope=scope)[0].state == "current"
    assert compile_selected("p1", entries, ["a"], "使用指定认识", 1)["items"][0]["id"] == "a"
    with pytest.raises(SQLiteUnitOfWorkConflict):
        set_preference(records, scope, "a", recognition_revision=1, preference_revision=0, state="normal")
    with pytest.raises(RecognitionConflict):
        set_preference(records, WorkScope("local-user", "p2"), "a", recognition_revision=1, preference_revision=1, state="normal")
    set_preference(records, scope, "a", recognition_revision=1, preference_revision=1, state="normal")
    assert retrieve("p1", "网页原型验证", annotate(records, scope, service.retrieval_entries(scope=scope))).hits[0].id == "a"
    preview = ErasureService(records, tmp_path).preview(scope=scope, recognition_id="a", expected_revision=1)
    assert preview.counts["recognition_recall_preferences"] == 1
    ErasureService(records, tmp_path).erase(scope=scope, recognition_id="a", expected_revision=1)
    assert records.read("recognition_recall_preferences", "a") is None
