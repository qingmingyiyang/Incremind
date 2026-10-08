from __future__ import annotations

import math

import pytest

from backend.memory_app.graph_views import GraphViewService
from backend.recognition import RecognitionConflict, RecognitionError, RecognitionService, WorkScope
from core.storage_provider import SQLiteStructuredRecordStore


@pytest.fixture
def records(tmp_path):
    return SQLiteStructuredRecordStore(tmp_path / "recognitions.sqlite3")


@pytest.fixture
def scope():
    return WorkScope("user-1", "project-1")


def _nodes(service, scope):
    experience = service.stage_experience(scope=scope, experience_id="experience-1", content="source evidence")
    candidate = service.propose(scope=scope, candidate_id="candidate-1", content="published recognition", source_experience_ids=[experience])
    recognition = service.publish(scope=scope, candidate_id=candidate.id, expected_revision=1, reviewer="user-1", recognition_id="recognition-1")
    service.upsert_question(
        scope=scope, question_id="question-1", question="What is current?", content="Use reviewed recognition.",
        recognition_ids=[recognition.id], source_revisions={recognition.id: recognition.revision}, expected_revision=0,
    )
    return experience, recognition.id, "question-1"


def _layout(nodes, *, selected_ids=(), **overrides):
    data = {
        "node_ids": list(nodes),
        "positions": {node_id: {"x": index * 10, "y": -index * 10} for index, node_id in enumerate(nodes)},
        "collapsed_ids": [],
        "hidden_ids": [],
        "selected_ids": list(selected_ids),
        "focus_id": None,
    }
    data.update(overrides)
    return data


def test_view_round_trip_is_content_free_and_noop_preserves_revision(records, scope):
    service = RecognitionService(records)
    nodes = _nodes(service, scope)
    views = GraphViewService(records)
    layout = _layout(nodes, selected_ids=["recognition-1"], collapsed_ids=["question-1"], focus_id="recognition-1")

    created = views.upsert(scope, "view-1", 0, **layout)
    same = views.upsert(scope, "view-1", created["revision"], **layout)
    stored = records.read("recognition_graph_views", "view-1")

    assert created["revision"] == same["revision"] == stored.revision == 1
    assert same["node_ids"] == list(nodes)
    assert same["selected_ids"] == ["recognition-1"]
    assert set(stored.payload) == {"id", "scope", "project_id", "node_ids", "positions", "collapsed_ids", "hidden_ids", "selected_ids", "focus_id"}
    assert "published recognition" not in str(stored.payload)


def test_scope_isolation_and_compare_and_swap(records, scope):
    service = RecognitionService(records)
    nodes = _nodes(service, scope)
    views = GraphViewService(records)
    created = views.upsert(scope, "shared-view", 0, **_layout(nodes))

    assert views.list(WorkScope("user-1", "project-2")) == ()
    with pytest.raises(RecognitionConflict, match="work scope"):
        views.get(WorkScope("user-1", "project-2"), "shared-view")
    with pytest.raises(RecognitionConflict, match="work scope"):
        views.upsert(WorkScope("user-1", "project-2"), "shared-view", 1, **_layout(()))
    with pytest.raises(RecognitionConflict, match="revision"):
        views.upsert(scope, "shared-view", 0, **_layout(nodes))
    assert created["revision"] == 1


def test_layout_update_is_recovered_through_a_fresh_service(records, scope):
    service = RecognitionService(records)
    nodes = _nodes(service, scope)
    views = GraphViewService(records)
    created = views.upsert(scope, "view-1", 0, **_layout(nodes))
    updated_layout = _layout(
        nodes,
        positions={"recognition-1": {"x": 72.5, "y": -14}},
        hidden_ids=["question-1"],
        focus_id="recognition-1",
    )

    updated = views.upsert(scope, "view-1", created["revision"], **updated_layout)
    restored = GraphViewService(records).get(scope, "view-1")

    assert updated["revision"] == 2
    assert restored == updated
    assert restored["positions"] == {"recognition-1": {"x": 72.5, "y": -14.0}}
    assert restored["hidden_ids"] == ["question-1"]


@pytest.mark.parametrize(
    "layout",
    [
        {"node_ids": ["missing"], "positions": {}, "collapsed_ids": [], "hidden_ids": [], "selected_ids": [], "focus_id": None},
        {"node_ids": ["recognition-1"], "positions": {"recognition-1": {"x": math.inf, "y": 0}}, "collapsed_ids": [], "hidden_ids": [], "selected_ids": [], "focus_id": None},
        {"node_ids": ["recognition-1"], "positions": {"recognition-1": {"x": 10 ** 400, "y": 0}}, "collapsed_ids": [], "hidden_ids": [], "selected_ids": [], "focus_id": None},
        {"node_ids": ["recognition-1"], "positions": {"recognition-1": {"x": 100001, "y": 0}}, "collapsed_ids": [], "hidden_ids": [], "selected_ids": [], "focus_id": None},
        {"node_ids": ["recognition-1", "recognition-1"], "positions": {}, "collapsed_ids": [], "hidden_ids": [], "selected_ids": [], "focus_id": None},
        {"node_ids": ["experience-1"], "positions": {}, "collapsed_ids": [], "hidden_ids": [], "selected_ids": ["experience-1"], "focus_id": None},
        {"node_ids": ["recognition-1"], "positions": {}, "collapsed_ids": [], "hidden_ids": ["recognition-1"], "selected_ids": ["recognition-1"], "focus_id": None},
    ],
)
def test_invalid_nodes_coordinates_and_selection_are_rejected(records, scope, layout):
    service = RecognitionService(records)
    _nodes(service, scope)
    with pytest.raises((RecognitionError, RecognitionConflict)):
        GraphViewService(records).upsert(scope, "view-invalid", 0, **layout)
    assert records.read("recognition_graph_views", "view-invalid") is None


def test_layout_is_bounded_before_any_domain_lookup(records, scope):
    node_ids = [f"node-{index}" for index in range(201)]
    with pytest.raises(RecognitionError, match="too many nodes"):
        GraphViewService(records).upsert(scope, "view-too-large", 0, **_layout(node_ids))
    assert records.read("recognition_graph_views", "view-too-large") is None


def test_revoked_recognition_is_removed_only_from_selection_projection(records, scope):
    service = RecognitionService(records)
    nodes = _nodes(service, scope)
    views = GraphViewService(records)
    views.upsert(scope, "view-1", 0, **_layout(nodes, selected_ids=["recognition-1"]))
    service.revoke(scope=scope, recognition_id="recognition-1", expected_revision=1, reason="superseded")

    current = views.get(scope, "view-1")

    assert current["node_ids"] == list(nodes)
    assert current["selected_ids"] == []
    assert current["selection_excluded_ids"] == ["recognition-1"]
    assert current["revision"] == 1


def test_views_do_not_mutate_domain_records(records, scope):
    service = RecognitionService(records)
    nodes = _nodes(service, scope)
    before = {collection: tuple((item.object_id, item.revision) for item in records.list(collection)) for collection in (
        "recognition_experiences", "recognitions", "recognition_questions",
    )}

    GraphViewService(records).upsert(scope, "view-1", 0, **_layout(nodes, selected_ids=["recognition-1"]))
    GraphViewService(records).get(scope, "view-1")
    GraphViewService(records).list(scope)

    after = {collection: tuple((item.object_id, item.revision) for item in records.list(collection)) for collection in before}
    assert after == before
