from types import SimpleNamespace

import pytest

from backend.memory_app.context_adapter import ContextSelectionError, compile_selected
from backend.memory_app.packet_verification import _verify_packet_current
from backend.memory_app.recall_preferences import annotate, set_preference
from backend.memory_app.workspace_query import WorkspaceQuery
from backend.recognition import RecognitionConflict, RecognitionError, RecognitionService, WorkScope
from backend.recognition_retrieval import retrieve
from core.document_engine import SQLiteDocumentRepository
from core.storage_provider import JsonObjectStore, SQLiteStructuredRecordStore, SQLiteUnitOfWorkConflict


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("CHRIPTMAS_APP_ROOT", str(tmp_path))
    records = SQLiteStructuredRecordStore(tmp_path / "records.sqlite3")
    service = RecognitionService(records)
    scope = WorkScope("local-user", "alpha")
    experience = service.stage_experience(scope=scope, content="alphaomega evidence")
    recognized = []
    for name in ("one", "two"):
        candidate = service.propose(scope=scope, content="alphaomega evidence " + name, source_experience_ids=[experience])
        recognized.append(service.publish(scope=scope, candidate_id=candidate.id, expected_revision=1,
            reviewer="local-user", recognition_id=name))
    query = WorkspaceQuery(records, SQLiteDocumentRepository(records),
        JsonObjectStore(tmp_path / "objects", namespace_id="test"), SimpleNamespace(), service)
    return SimpleNamespace(records=records, service=service, scope=scope, query=query,
        recognition=recognized[0], experience=experience)


def set_state(env, state, preference_revision=0):
    return set_preference(env.records, env.scope, "one", recognition_revision=1,
        preference_revision=preference_revision, state=state)


def entries(env):
    return annotate(env.records, env.scope, env.service.retrieval_entries(scope=env.scope))


def test_forgotten_excludes_workbench_and_task_recall_then_restores(env):
    from backend.memory_app.recall_preferences import is_recall_excluded
    original = env.records.read("recognitions", "one")
    experience = env.records.read("recognition_experiences", env.experience)
    assert {row["id"] for row in env.query.prepare_ask("alpha", "alphaomega")["chosen"]} == {"one", "two"}
    assert is_recall_excluded(env.records, env.scope, "one") is False
    assert set_state(env, "forgotten") == {"recall_state": "forgotten", "recall_preference_revision": 1}
    assert is_recall_excluded(env.records, env.scope, "one") is True
    retrieved = retrieve("alpha", "alphaomega evidence", entries(env))
    assert [row.id for row in retrieved.hits] == ["two"]
    assert retrieved.trace["candidate_count"] == 1 and retrieved.trace["excluded"]["invalid"] == 0
    assert [row["id"] for row in env.query.prepare_ask("alpha", "alphaomega")["chosen"]] == ["two"]
    with pytest.raises(ContextSelectionError, match="selected_recognition_unavailable"):
        compile_selected("alpha", entries(env), ["one"], "alphaomega", 1)
    current = env.service.get_recognition(scope=env.scope, recognition_id="one")
    assert current.revision == 1 and current.authorized is True
    assert env.records.read("recognitions", "one") == original
    assert env.records.read("recognition_experiences", env.experience) == experience
    set_state(env, "normal", 1)
    assert is_recall_excluded(env.records, env.scope, "one") is False
    assert {row.id for row in retrieve("alpha", "alphaomega evidence", entries(env)).hits} == {"one", "two"}
    assert {row["id"] for row in env.query.prepare_ask("alpha", "alphaomega")["chosen"]} == {"one", "two"}
    assert compile_selected("alpha", entries(env), ["one"], "alphaomega", 1)["items"][0]["id"] == "one"


def test_cooled_is_only_rank_reduction_and_manual_selection_still_works(env):
    from backend.memory_app.recall_preferences import is_recall_excluded
    before = {hit.id: hit.score for hit in retrieve("alpha", "alphaomega evidence", entries(env)).hits}
    set_state(env, "cooled")
    after = retrieve("alpha", "alphaomega evidence", entries(env))
    assert [hit.id for hit in after.hits] == ["two", "one"]
    assert next(hit.score for hit in after.hits if hit.id == "one") == pytest.approx(before["one"] * .5, abs=1e-6)
    assert is_recall_excluded(env.records, env.scope, "one") is False
    assert {row["id"] for row in env.query.prepare_ask("alpha", "alphaomega")["chosen"]} == {"one", "two"}
    assert compile_selected("alpha", entries(env), ["one"], "alphaomega", 1)["items"][0]["id"] == "one"
    assert env.service.get_recognition(scope=env.scope, recognition_id="one").authorized is True


def test_old_question_preview_and_task_packet_are_revalidated(env):
    plan = env.query.prepare_ask("alpha", "alphaomega")
    packet = [{"id": "one", "revision": 1}]
    env.query.validate_ask_plan(plan)
    _verify_packet_current(env.service, env.scope, packet)
    set_state(env, "forgotten")
    with pytest.raises(RecognitionError):
        env.query.validate_ask_plan(plan)
    with pytest.raises(RecognitionConflict):
        _verify_packet_current(env.service, env.scope, packet)
    assert env.service.get_recognition(scope=env.scope, recognition_id="one").authorized is True
    set_state(env, "normal", 1)
    env.query.validate_ask_plan(env.query.prepare_ask("alpha", "alphaomega"))
    _verify_packet_current(env.service, env.scope, packet)


def test_forgotten_state_keeps_cas_and_scope_guards(env):
    from backend.memory_app.recall_preferences import is_recall_excluded
    set_state(env, "forgotten")
    with pytest.raises(SQLiteUnitOfWorkConflict):
        set_state(env, "normal", 0)
    with pytest.raises(RecognitionConflict):
        is_recall_excluded(env.records, WorkScope("other-user", "alpha"), "one")
    with pytest.raises(RecognitionConflict):
        set_preference(env.records, WorkScope("local-user", "other"), "one", recognition_revision=1,
            preference_revision=1, state="normal")
    with pytest.raises(RecognitionConflict):
        set_preference(env.records, env.scope, "one", recognition_revision=2, preference_revision=1, state="normal")
