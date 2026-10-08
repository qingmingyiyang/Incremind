import pytest

from backend.memory_app.v2.ladder import candidate_order, plan_ladder
from backend.recognition import WorkScope
from tests.memory_app.v2.test_ladder import env as _env_fixture, document, recognition

env = _env_fixture


@pytest.mark.parametrize("layer", ["L3", "L2", "L1", "L0"])
def test_equal_scores_use_recency_before_id_without_changing_scores(layer):
    candidates = [
        {"id": "a-old", "layer": layer, "score": 1.0004, "excerpt": "alpha autumn", "sort_time": "2020-01-01T00:00:00Z"},
        {"id": "z-new", "layer": layer, "score": 1.0001, "excerpt": "alpha winter", "sort_time": "2021-01-01T00:00:00+00:00"},
    ]
    assert [c["id"] for c in plan_ladder(candidates, "alpha beta gamma")["chosen"]] == ["z-new", "a-old"]
    assert [c["score"] for c in candidates] == [1.0004, 1.0001]


def test_score_and_connected_group_keep_priority_over_time():
    candidates = [
        {"id": "score", "layer": "L3", "score": 3, "excerpt": "alpha apples", "document_id": "linked"},
        {"id": "recent", "layer": "L3", "score": 2, "excerpt": "alpha bananas", "sort_time": "2099-01-01T00:00:00Z"},
        {"id": "linked", "layer": "L2", "score": 1, "excerpt": "alpha cherries", "document_id": "linked"},
        {"id": "unlinked", "layer": "L2", "score": 10, "excerpt": "alpha dates", "sort_time": "2099-01-01T00:00:00Z"},
    ]
    assert [c["id"] for c in plan_ladder(candidates, "alpha beta gamma")["chosen"]] == [
        "score",
        "recent",
        "linked",
        "unlinked",
    ]


def test_persona_timezones_missing_times_and_id_ties():
    candidates = [
        {"id": identity, "layer": "L3", "score": 1, "excerpt": "alpha " + identity, "persona": True, "sort_time": time}
        for identity, time in [
            ("missing", None),
            ("invalid", "invalid"),
            ("a", "2020-01-01T08:00:00+08:00"),
            ("b", "2020-01-01T00:00:00Z"),
        ]
    ]
    assert [c["id"] for c in sorted(candidates, key=candidate_order)] == ["a", "b", "invalid", "missing"]
    assert candidate_order(candidates[2])[:2] == candidate_order(candidates[3])[:2]
    assert plan_ladder(candidates, "alpha")["chosen"] == []


def test_collection_reads_internal_times_from_original_objects(env):
    env.documents.now = "2020-01-01T00:00:00+00:00"
    identity = document(env)
    insight = recognition(env, "alpha beta", doc=identity)
    env.query.source_store.write(
        "sources",
        "separate",
        {
            "id": "separate",
            "title": "alpha",
            "project_id": "alpha",
            "metadata": {"content": "alpha beta"},
            "created_at": "2019-01-01T00:00:00Z",
        },
        expected_revision=0,
    )
    rows = env.query.collect_candidates("alpha", "alpha beta")["candidates"]
    for row in rows:
        if row["kind"] == "recognition":
            assert row["sort_time"] == env.records.read("recognitions", insight.id).payload["updated_at"]
        elif row["layer"] in {"L1", "L2"}:
            assert row["sort_time"] == env.documents.read(identity)["updated_at"]
        elif row["id"] == "separate":
            assert row["sort_time"] == "2019-01-01T00:00:00Z"
        else:
            assert (
                row["sort_time"] == env.records.read("workspace_items", row["entry"]["item_id"]).payload["created_at"]
            )
    assert all("sort_time" not in entry for entry in env.query.query_entries("alpha"))


def test_duplicate_evidence_keeps_newer_recognition_before_ladder(env, monkeypatch):
    clock = {"now": "2020-01-01T00:00:00Z"}
    monkeypatch.setattr("backend.recognition.service._now", lambda: clock["now"])
    scope = WorkScope("local-user", "alpha")
    experience = env.service.stage_experience(scope=scope, content="alpha beta")
    for identity, at in [("a-old", "2020-01-01T00:00:00Z"), ("z-new", "2021-01-01T00:00:00Z")]:
        clock["now"] = at
        candidate = env.service.propose(scope=scope, content="alpha beta", source_experience_ids=[experience])
        env.service.publish(
            scope=scope, candidate_id=candidate.id, expected_revision=1, reviewer="local-user", recognition_id=identity
        )
    collected = env.query.collect_candidates("alpha", "alpha beta")
    assert [r["id"] for r in collected["candidates"]] == ["z-new"]
    assert collected["excluded_sources"][0]["duplicate_of"]["id"] == "z-new"
