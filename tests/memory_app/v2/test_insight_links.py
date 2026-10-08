import json
import pytest
from tests.memory_app.v2.kernel_receipts import wire_receipts, requests
from backend.recognition import WorkScope, RecognitionConflict
from backend.memory_app.relations import RelationProposalService
from backend.memory_app.v2.links import InsightLinks, link_id
from backend.memory_app.v2.usage import UsageService
from backend.memory_app.recall_preferences import preference, set_preference
from tests.memory_app.v2.test_workbench_ask import env as _env_fixture, publish

env = _env_fixture


def pair(env):
    a, _ = publish(env, "缓存策略按需加载可以减少请求")
    b, _ = publish(env, "缓存策略按需加载可以减少等待", "me")
    return a, b


def test_related_recompute_no_model_and_scope(env):
    a, b = pair(env)
    foreign, _ = publish(env, "缓存策略按需加载可以减少等待", "beta")
    links = InsightLinks(env.records, env.service)
    links.discover("alpha", a.id)
    rows = links.list("alpha", a.id)
    assert rows == [
        {"id": link_id(a.id, b.id), "other_id": b.id, "kind": "related", "state": "active", "score": rows[0]["score"]}
    ]
    assert links.list("beta", a.id) == []
    assert links.list("beta", b.id) == []
    first = env.records.read("v2_insight_links", rows[0]["id"])
    links.discover("alpha", a.id)
    assert env.records.read("v2_insight_links", rows[0]["id"]).revision == first.revision + 1
    assert env.records.list("recognition_relation_proposals") == ()
    assert foreign.id not in {r["other_id"] for r in rows}


def suggest(env, a, b, kind):
    return InsightLinks(env.records, env.service).propose("alpha", a.id, b.id, kind, "新的材料支持这个判断")


def test_cross_me_review_atomic_cool_and_cas(env):
    a, b = pair(env)
    before = env.records.read("recognitions", b.id)
    row = suggest(env, a, b, "supersedes")
    assert env.records.list("recognition_relations") == ()
    service = InsightLinks(env.records, env.service)
    with pytest.raises(RecognitionConflict):
        service.review("beta", row["id"], row["revision"], True)
    service.review("alpha", row["id"], row["revision"], True)
    assert preference(env.records, WorkScope("local-user", "me"), b.id)["recall_state"] == "cooled"
    assert env.records.read("recognition_recall_preferences", b.id).payload["by"] == "auto"
    assert env.records.read("recognitions", b.id) == before
    assert service.list("alpha", a.id)[0]["kind"] == "supersedes"
    with pytest.raises(RecognitionConflict):
        service.review("alpha", row["id"], row["revision"], True)
    assert len(env.records.list("recognition_relations")) == 1


def test_dismiss_and_stale_endpoint(env):
    a, b = pair(env)
    row = suggest(env, a, b, "supports")
    InsightLinks(env.records, env.service).review("alpha", row["id"], row["revision"], False)
    assert InsightLinks(env.records, env.service).list("alpha", a.id) == []
    row = suggest(env, a, b, "refutes")
    with env.records.begin() as tx:
        old = tx.read("recognitions", b.id)
        tx.put("recognitions", b.id, dict(old.payload), expected_revision=old.revision)
        tx.commit()
    with pytest.raises(RecognitionConflict):
        InsightLinks(env.records, env.service).review("alpha", row["id"], row["revision"], True)
    assert env.records.list("recognition_relations") == ()
    assert env.records.read("recognition_relation_proposals", row["id"]).payload["state"] == "pending"


def test_spread_scores_without_counts_or_cascade(env):
    a, b = pair(env)
    c, _ = publish(env, "缓存策略按需加载可以减少带宽", "me")
    links = InsightLinks(env.records, env.service)
    links.discover("alpha", a.id)
    links.discover("me", b.id)
    usage = UsageService(env.records)
    for identity, project in [(a.id, "alpha"), (b.id, "me"), (c.id, "me")]:
        usage.initialize("insight", identity, project)
    old_b = env.records.read("v2_usage_insight", b.id).payload
    usage.record_usage("insight", a.id, "alpha", 1)
    new_b = env.records.read("v2_usage_insight", b.id).payload
    assert new_b["count"] == old_b["count"]
    assert new_b["score"] == pytest.approx(old_b["score"] + 0.2, abs=0.00001)
    assert env.records.read("v2_usage_insight", a.id).payload["count"] == 2


def test_recall_expands_two_neighbors_and_conflict_with_same_budget(env):
    a, _ = publish(env, "独特提问锚点")
    others = [publish(env, f"邻居知识条目{i}")[0] for i in range(4)]
    links = InsightLinks(env.records, env.service)
    for b in others[:3]:
        p = suggest(env, a, b, "supports")
        links.review("alpha", p["id"], p["revision"], True)
    p = suggest(env, a, others[3], "refutes")
    links.review("alpha", p["id"], p["revision"], True)
    plan = env.domains.query.prepare_ask("alpha", "独特提问锚点")
    expanded = [c for c in plan["chosen"] if c.get("expanded_from")]
    assert len([c for c in expanded if c.get("link_kind") != "refutes"]) == 2
    assert others[3].id in {c["id"] for c in plan["chosen"]}
    assert len(plan["chosen"]) <= 8
    assert all(c["expanded_from"] == a.id for c in expanded)
    assert any(t.get("expanded_from") == a.id for t in plan["trace"])
    import asyncio

    preview = env.domains.query.store_ask_preview(plan)
    asyncio.run(env.domains.query.execute_ask(preview, "alpha", "独特提问锚点", True))
    assert "分歧" in env.model.messages[0]["content"]


def test_link_routes_review_require_revision_and_scope(env):
    a, b = pair(env)
    p = suggest(env, a, b, "supports")
    result = env.http.get(f"/api/v2/library/insights/{a.id}/links", params={"project_id": "alpha"})
    assert result.status_code == 200 and result.json()["links"][0]["state"] == "suggested"
    url = f"/api/v2/library/link-suggestions/{p['id']}/accept"
    assert env.http.post(url, json={"project_id": "alpha", "expected_revision": 99}).status_code == 409
    assert env.http.post(url, json={"project_id": "alpha", "expected_revision": True}).status_code == 400
    assert env.http.post(url, json={"project_id": "alpha", "expected_revision": p["revision"]}).status_code == 200
    assert (
        env.http.get(f"/api/v2/library/insights/{a.id}/links", params={"project_id": "alpha"}).json()["links"][0][
            "state"
        ]
        == "active"
    )


class SuggestionModel:
    def __init__(self, other):
        self.other, self.calls = other, 0
        self.before = lambda: None
        self.after = lambda: None
        self.allowed = True

    def public(self):
        return {
            "generation": {
                "configured": True,
                "enabled": True,
                "allow_remote": self.allowed,
                "base_url": "https://example.invalid/v1",
                "model": "fake",
                "revision": 1,
            },
            "generation_mode": {"revision": 2},
        }

    def complete(self, messages, *, max_tokens, validate_current, wire_attempt_sink=None):
        self.before()
        validate_current()
        attempt = wire_attempt_sink.begin_model_wire_attempt() if wire_attempt_sink else None

        def wire():
            self.calls += 1
            text = json.dumps(
                {"suggestions": [{"other_id": self.other, "kind": "supports", "evidence": "两条材料支持相同判断"}]}
            )

            if attempt:
                attempt.succeeded(usage={"prompt_tokens": 9, "completion_tokens": 4, "total_tokens": 13}, cache_observation=None)
            return text

        text = attempt.invoke_wire(wire) if attempt else wire()
        self.after()
        validate_current()
        return text, {"usage": {"total_tokens": 13}}


def test_model_one_call_bounded_replayed_with_body_free_wire_receipts(env):
    a, b = pair(env)
    for i in range(6):
        publish(env, f"缓存按需加载减少请求{i}")
    model = SuggestionModel(b.id)
    # Make the requested endpoint a top-three neighbor.
    from backend.memory_app.v2.links import similarity

    model.other = sorted(
        [r for r in env.records.list("recognitions") if r.object_id != a.id],
        key=lambda r: (-similarity(a.content, r.payload["content"]), r.object_id),
    )[0].object_id
    links = InsightLinks(env.records, env.service, model)
    links.discover("alpha", a.id)
    assert model.calls == 1
    proposals = env.records.list("recognition_relation_proposals")
    assert len(proposals) == 1 and proposals[0].payload["state"] == "pending"
    assert proposals[0].payload["source"] == "model"
    assert env.records.list("recognition_relations") == ()
    receipts = wire_receipts(env.records)
    assert env.records.list("v2_egress_receipts") == ()
    assert len(receipts) == 1
    assert receipts[0]["status"] == "succeeded" and len(requests(env.records)[0]["input"]["refs"]) == 4
    assert receipts[0]["usage"] == {"input_tokens": 9, "output_tokens": 4, "total_tokens": 13}
    assert "两条材料" not in json.dumps(receipts[0], ensure_ascii=False)
    links.discover("alpha", a.id)
    assert model.calls == 1


def test_disabled_private_and_changed_sources_do_not_make_suggestions(env):
    from backend.memory_app.v2.privacy import set_private_project

    a, b = pair(env)
    model = SuggestionModel(b.id)
    model.allowed = False
    InsightLinks(env.records, env.service, model).discover("alpha", a.id)
    assert model.calls == 0 and env.records.list("v2_egress_receipts") == ()
    model.allowed = True
    set_private_project(env.records, "me", True, 0)
    InsightLinks(env.records, env.service, model).discover("alpha", a.id)
    assert model.calls == 0
    set_private_project(env.records, "me", False, 1)

    def change():
        with env.records.begin() as tx:
            row = tx.read("recognitions", b.id)
            tx.put("recognitions", b.id, dict(row.payload), expected_revision=row.revision)
            tx.commit()

    model.after = change
    InsightLinks(env.records, env.service, model).discover("alpha", a.id)
    assert model.calls == 1 and env.records.list("recognition_relation_proposals") == ()
    assert wire_receipts(env.records)[0]["status"] == "succeeded"


def test_forgotten_neighbor_never_expands_or_spreads(env):
    a, b = pair(env)
    links = InsightLinks(env.records, env.service)
    links.discover("alpha", a.id)
    set_preference(
        env.records,
        WorkScope("local-user", "me"),
        b.id,
        recognition_revision=1,
        preference_revision=0,
        state="forgotten",
    )
    UsageService(env.records).record_usage("insight", a.id, "alpha", 1)
    assert env.records.read("v2_usage_insight", b.id) is None
    plan = env.domains.query.prepare_ask("alpha", "缓存策略按需加载可以减少请求")
    assert b.id not in {c["id"] for c in plan["chosen"]}


def test_keyword_threshold_calibrated_on_fixed_corpus():
    from pathlib import Path
    from backend.memory_app.v2.links import similarity, KEYWORD_THRESHOLD

    fixture = json.loads(
        (Path(__file__).parents[2] / "fixtures" / "memory_eval" / "corpus.json").read_text(encoding="utf8")
    )
    # The original calibration projects have known 10 positive/12 negative
    # pairs. T12.0 adds intentionally related cross-document history elsewhere;
    # those pairs cannot be reclassified as negatives for this fixed threshold.
    rows = {r["id"]: r for r in fixture["insights"] if r["project_id"] in {"eval", "me"}}
    assert len(rows) == 40
    positive = [
        similarity(rows[b["id"][:-1] + "a"]["text"], b["text"])
        for b in rows.values()
        if b["role"] in {"synonym", "conflict", "supersedes"}
    ]
    negative = [
        similarity(a["text"], b["text"])
        for a in rows.values()
        for b in rows.values()
        if a["id"] < b["id"] and a.get("document_id") != b.get("document_id") and a["project_id"] == b["project_id"]
    ]
    assert sum(s >= KEYWORD_THRESHOLD for s in positive) == 10
    assert sum(s >= KEYWORD_THRESHOLD for s in negative) == 12


class VectorModel:
    def public(self):
        return {
            "embedding": {
                "purpose": "embedding",
                "provider": "openai",
                "base_url": "https://example.invalid/v1",
                "model": "vectors",
                "allow_remote": True,
                "enabled": True,
                "revision": 1,
                "configured": True,
                "has_api_key": True,
            },
            "generation": {"configured": False},
        }

    def snapshot(self, purpose):
        return {**self.public()[purpose], "api_key": "synthetic-key"}


def test_vector_priority_reuses_cache_and_actual_wire_receipt(env, monkeypatch):
    from backend.memory_app.retrieval_models import ConfiguredTransport

    a, b = pair(env)
    calls = []

    def wire(transport, *, endpoint, payload):
        transport._check_current()
        calls.append(payload["input"])
        return {
            "data": [{"index": i, "embedding": [1.0, 0.0]} for i, _ in enumerate(payload["input"])],
            "usage": {"prompt_tokens": 5},
        }

    monkeypatch.setattr(ConfiguredTransport, "post_json", wire)
    links = InsightLinks(env.records, env.service, VectorModel())
    links.discover("alpha", a.id)
    assert len(calls) == 1 and len(calls[0]) == 2
    assert links.list("alpha", a.id)[0]["score"] == pytest.approx(1)
    links.discover("alpha", a.id)
    assert len(calls) == 2 and len(calls[1]) == 1
    receipts = wire_receipts(env.records)
    assert env.records.list("v2_egress_receipts") == ()
    assert sorted(len(r.payload["identity"]["key"]["embedding"]) for r in env.records.list("v2_memory_turn_keys")) == [1, 2]
    assert all(r["status"] == "succeeded" for r in receipts)
    assert all(r["execution_policy"]["purpose"] == "aux" for r in requests(env.records))


def test_local_vectors_do_not_require_external_permission_or_make_egress_receipts(env, monkeypatch):
    from backend.memory_app.retrieval_models import ConfiguredTransport

    class Local(VectorModel):
        def public(self):
            config = super().public()
            config["embedding"].update(base_url="http://localhost:9000/v1", allow_remote=False)
            return config

    def wire(transport, *, endpoint, payload):
        transport._check_current()
        return {"data": [{"index": i, "embedding": [1.0, 0.0]} for i, _ in enumerate(payload["input"])]}

    monkeypatch.setattr(ConfiguredTransport, "post_json", wire)
    a, b = pair(env)
    links = InsightLinks(env.records, env.service, Local())
    links.discover("alpha", a.id)
    assert links.list("alpha", a.id)[0]["score"] == 1
    assert env.records.list("v2_egress_receipts") == ()


def test_confirmation_and_shared_daily_scheduler_discover_links(env):
    a, b = pair(env)
    scope = WorkScope("local-user", "alpha")
    experience = env.service.stage_experience(scope=scope, content="新的缓存资料")
    candidate = env.service.propose(
        scope=scope, content="缓存策略按需加载可以减少请求和等待", source_experience_ids=[experience]
    )
    result = env.http.post(
        f"/api/v2/library/insights/{candidate.id}/confirm", json={"project_id": "alpha", "expected_revision": 1}
    )
    assert result.status_code == 200
    identity = result.json()["id"]
    assert env.records.read("v2_insight_links", link_id(identity, b.id)) is not None
    jobs = env.http.app.state.memory_daily_jobs
    assert "insight_links" in jobs.jobs
    jobs.run()
    assert env.records.read("v2_insight_links", link_id(a.id, b.id)) is not None


def test_persona_opt_in_keeps_other_users_and_projects_isolated(env):
    a, b = pair(env)
    domain = RelationProposalService(env.records, allow_persona=True)
    with pytest.raises(RecognitionConflict):
        domain.propose(WorkScope("another-user", "alpha"), a.id, b.id, "supports", "same evidence")
    foreign, _ = publish(env, "另一个项目", "beta")
    with pytest.raises(RecognitionConflict):
        domain.propose(WorkScope("local-user", "alpha"), b.id, foreign.id, "supports", "same evidence")
    row = domain.propose(WorkScope("local-user", "alpha"), b.id, a.id, "refutes", "两个认识矛盾", source="model")
    assert row["state"] == "pending" and row["from_id"] == b.id


def test_persona_semantic_edges_are_global_but_activation_stays_in_question_scope(env):
    from backend.memory_app.v2.usage import record_answer_usage

    a, b = pair(env)
    other, _ = publish(env, "另一画像知识", "me")
    links = InsightLinks(env.records, env.service)
    p = links.propose("me", b.id, other.id, "refutes", "两个画像依据矛盾")
    links.review("me", p["id"], p["revision"], True)
    plan = env.domains.query.prepare_ask("alpha", b.content)
    assert {item["id"] for item in plan["profile"]["items"]} == {b.id, other.id}
    assert b.content in plan["profile"]["text"]
    assert other.content in plan["profile"]["text"]
    assert not {b.id, other.id}.intersection(c["id"] for c in plan["chosen"])
    assert not any(c.get("persona") for c in plan["chosen"])
    assert any(edge["other_id"] == other.id and edge["kind"] == "refutes"
               and edge["state"] == "active" for edge in links.neighbors("alpha", b.id))
    assert any(row.payload["from_id"] == b.id and row.payload["to_id"] == other.id
               and row.payload["relation"] == "refutes"
               for row in env.records.list("recognition_relations"))
    links.discover("alpha", a.id)
    foreign, _ = publish(env, "缓存策略按需加载可以减少等待", "beta")
    links.discover("beta", foreign.id)
    record_answer_usage(env.records, [{"layer": "L3", "entry": {"id": b.id}, "project_id": "me"}], [{"n": 1}], "alpha")
    assert env.records.read("v2_usage_insight", a.id).payload["score"] == pytest.approx(1.2, abs=0.00001)
    assert env.records.read("v2_usage_insight", a.id).payload["count"] == 1
    assert env.records.read("v2_usage_insight", foreign.id) is None


def test_superseded_persona_stays_cooled_until_actual_use_and_score_threshold(env):
    from datetime import timedelta
    from backend.memory_app.v2.auto_forget import AutoForget
    from tests.memory_app.v2.test_auto_forget import START, age

    a, b = pair(env)
    age(env, "recognitions", b.id)
    now = START + timedelta(days=60)
    usage = UsageService(env.records, now=lambda: now)
    usage.initialize("insight", b.id, "me")
    links = InsightLinks(env.records, env.service)
    p = suggest(env, a, b, "supersedes")
    links.review("alpha", p["id"], p["revision"], True)
    body = env.records.read("recognitions", b.id)
    count = env.records.read("v2_usage_insight", b.id).payload["count"]
    AutoForget(env.records, now=lambda: now).run()
    assert preference(env.records, WorkScope("local-user", "me"), b.id)["recall_state"] == "cooled"
    usage.record_usage("insight", b.id, "me", 0.2, count=False)
    AutoForget(env.records, now=lambda: now).run()
    assert preference(env.records, WorkScope("local-user", "me"), b.id)["recall_state"] == "cooled"
    assert env.records.read("v2_usage_insight", b.id).payload["count"] == count
    usage.record_usage("insight", b.id, "me", 0.1, reset=True)
    AutoForget(env.records, now=lambda: now).run()
    assert preference(env.records, WorkScope("local-user", "me"), b.id)["recall_state"] == "cooled"
    usage.record_usage("insight", b.id, "me", 1)
    AutoForget(env.records, now=lambda: now).run()
    assert preference(env.records, WorkScope("local-user", "me"), b.id)["recall_state"] == "normal"
    assert env.records.read("recognitions", b.id) == body


def test_manual_forgetting_after_supersession_stays_forgotten_even_when_used(env):
    from datetime import timedelta
    from backend.memory_app.v2.auto_forget import AutoForget
    from tests.memory_app.v2.test_auto_forget import START, age

    a, b = pair(env)
    age(env, "recognitions", b.id)
    links = InsightLinks(env.records, env.service)
    p = suggest(env, a, b, "supersedes")
    links.review("alpha", p["id"], p["revision"], True)
    preference_row = env.records.read("recognition_recall_preferences", b.id)
    set_preference(
        env.records,
        WorkScope("local-user", "me"),
        b.id,
        recognition_revision=env.records.read("recognitions", b.id).revision,
        preference_revision=preference_row.revision,
        state="forgotten",
    )
    now = START + timedelta(days=60)
    UsageService(env.records, now=lambda: now).record_usage("insight", b.id, "me", 2)
    AutoForget(env.records, now=lambda: now).run()
    assert preference(env.records, WorkScope("local-user", "me"), b.id)["recall_state"] == "forgotten"
