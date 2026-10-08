import json
import pytest
from fractions import Fraction
from types import SimpleNamespace

from backend.memory_app.v2.multi_query import fuse_candidates
from backend.memory_app.v2.ladder import candidate_order
from tests.memory_app.v2.test_workbench_ask import env as _env_fixture, publish, ask

env = _env_fixture


def candidate(identity, *, layer="L3", score=1, date="2020-01-01", expansion=False):
    return {
        "id": identity,
        "kind": "recognition",
        "layer": layer,
        "score": score,
        "scope": SimpleNamespace(project_id="alpha"),
        "sort_time": date,
        "entry": {"revision": 1},
        "windows": (),
        "excerpt": identity,
        "coordinate_space": "test",
        "expansion_only": expansion,
    }


def test_rrf_matches_hand_sum_and_does_not_round_away_rank():
    a, b, c = candidate("a"), candidate("b", date="2030-01-01"), candidate("c")
    result = fuse_candidates([[a, b, a], [b, c]])
    scores = {row["id"]: row["score"] for row in result}
    assert scores == {
        "a": float(Fraction(1, 61)),
        "b": float(Fraction(1, 62) + Fraction(1, 61)),
        "c": float(Fraction(1, 62)),
    }
    assert [row["id"] for row in sorted(result, key=candidate_order)] == ["b", "a", "c"]
    simple = fuse_candidates([[candidate("first"), candidate("newer", date="2030-01-01")]])
    assert [row["id"] for row in sorted(simple, key=candidate_order)] == ["first", "newer"]


def test_rrf_exact_ties_use_recency_and_layers_are_separate():
    old, new = candidate("old"), candidate("new", date="2030-01-01")
    rows = fuse_candidates([[old, new], [new, old], [candidate("old", layer="L2")]])
    assert [row["id"] for row in sorted([r for r in rows if r["layer"] == "L3"], key=candidate_order)] == ["new", "old"]
    assert len(rows) == 3
    assert sorted(r["rrf_rank"] for r in rows if r["layer"] == "L3") == [0, 0]


def test_expansion_only_has_no_rank_vote():
    rows = fuse_candidates([[candidate("neighbor", expansion=True), candidate("hit")]])
    assert next(r for r in rows if r["id"] == "hit")["score"] == float(Fraction(1, 61))
    assert next(r for r in rows if r["id"] == "neighbor")["expansion_only"] is True


def test_synonym_rewrite_retrieves_once_and_accounts_usage(env):
    insight, _ = publish(env)
    original = env.model.complete
    rewrites = []

    def complete(messages, **kwargs):
        if '"queries"' in messages[0]["content"]:
            kwargs["validate_current"]()
            rewrites.append(messages[-1]["content"])
            return json.dumps({"queries": ["alpha beta gamma?", "alpha beta gamma?"]}), {"usage": {"total_tokens": 5}}
        return original(messages, **kwargs)

    env.model.complete = complete
    response = ask(env, text="equivalent terminology?")
    assert response.status_code == 200, response.text
    receipt = response.json()["turn"]["receipt"]["ask"]
    assert [row["id"] for row in receipt["citations"]] == [insight.id]
    assert len(rewrites) == 1
    assert receipt["trace"][0]["rewrite"] == {"queries": ["alpha beta gamma?"], "used": True}
    assert receipt["model_usage"] == {"total_tokens": 12}


def test_sufficient_ask_does_not_rewrite(env):
    publish(env)
    response = ask(env)
    assert response.status_code == 200
    assert env.model.calls == 1
    assert response.json()["turn"]["receipt"]["ask"]["trace"][0]["rewrite"] == {"queries": [], "used": False}


def test_rewrite_failure_keeps_original_results(env):
    publish(env)
    original = env.model.complete

    def complete(messages, **kwargs):
        if '"queries"' in messages[0]["content"]:
            raise ValueError("synthetic")
        return original(messages, **kwargs)

    env.model.complete = complete
    response = ask(env, text="alpha missing missing2 missing3?")
    assert response.status_code == 200, response.text
    receipt = response.json()["turn"]["receipt"]["ask"]
    assert receipt["no_match"] is False
    assert receipt["trace"][0]["rewrite"]["used"] is False


def test_unconfigured_model_does_not_send_rewrite(env):
    import asyncio
    from tools.memory_eval import NoModels
    from backend.memory_app.v2.multi_query import expand_plan

    query = env.domains.query
    query.models = NoModels()
    collected = query.collect_candidates("alpha", "missing")
    plan = query.prepare_ask("alpha", "missing", collected=collected)
    result, observation, rewrite = asyncio.run(expand_plan(query, "alpha", "missing", plan, collected, turn_id="test"))
    assert result is plan and observation["status"] == "skipped"
    assert rewrite == {"queries": [], "used": False} and observation["receipt_ids"] == []


def test_rewrite_invalid_json_retains_actual_failed_wire_usage(env):
    original = env.model.complete

    def complete(messages, *, max_tokens, validate_current, wire_attempt_sink=None):
        if '"queries"' not in messages[0]["content"]:
            return original(messages, max_tokens=max_tokens, validate_current=validate_current, wire_attempt_sink=wire_attempt_sink)
        attempt = wire_attempt_sink.begin_model_wire_attempt()
        def wire():
            output = "invalid"
            attempt.succeeded(usage={"input_tokens": 4, "output_tokens": 5}, cache_observation=None)
            return output
        output = attempt.invoke_wire(wire)
        return output, {"usage": {"total_tokens": 9}}

    env.model.complete = complete
    response = ask(env, text="missing?")
    assert response.status_code == 200, response.text
    receipt = response.json()["turn"]["receipt"]["ask"]
    assert receipt["no_match"] is True and receipt["model_usage"]["total_tokens"] == 9
    assert receipt["trace"][0]["rewrite_status"] == "failed"
    assert len(receipt["trace"][0]["rewrite_receipt_ids"]) == 1


def test_fused_windows_keep_exact_offsets_and_do_not_cross_revisions():
    import pytest
    from backend.recognition import RecognitionError
    from core.search_and_recall.evidence_windows import EvidenceWindow

    a = {**candidate("doc", layer="L1"), "kind": "document", "windows": (EvidenceWindow(10, 15, "alpha"),)}
    b = {**a, "windows": (EvidenceWindow(30, 34, "beta"),)}
    result = fuse_candidates([[a], [b]])[0]
    assert [(w.start, w.end, w.text) for w in result["windows"]] == [(10, 15, "alpha"), (30, 34, "beta")]
    with pytest.raises(RecognitionError):
        fuse_candidates([[a], [{**b, "entry": {"revision": 2}}]])


def test_coverage_uses_term_union_and_original_detail_requirement():
    from backend.memory_app.v2.ladder import plan_ladder

    row = {**candidate("a"), "excerpt": "alpha beta"}
    plan = plan_ladder([row], "absent", coverage_questions=["absent", "alpha beta"])
    assert plan["trace"][0]["stopped"] is True
    detail = plan_ladder([row], "alpha beta", coverage_questions=["alpha beta"], answer_question="原文")
    assert detail["trace"][-1]["stopped"] is False


def test_post_rewrite_collection_failure_preserves_answer_and_usage(env, monkeypatch):
    publish(env)
    original_model, original_collect = env.model.complete, env.domains.query.collect_candidates

    def complete(messages, **kwargs):
        if '"queries"' in messages[0]["content"]:
            return json.dumps({"queries": ["variant"]}), {"usage": {"total_tokens": 5}}
        return original_model(messages, **kwargs)

    def collect(project, question, **kwargs):
        if question == "variant":
            raise RuntimeError("synthetic collection failure")
        return original_collect(project, question, **kwargs)

    env.model.complete = complete
    monkeypatch.setattr(env.domains.query, "collect_candidates", collect)
    response = ask(env, text="alpha absent1 absent2 absent3?")
    assert response.status_code == 200, response.text
    receipt = response.json()["turn"]["receipt"]["ask"]
    assert receipt["no_match"] is False
    assert receipt["model_usage"] == {"total_tokens": 12}
    assert receipt["trace"][0]["rewrite_status"] == "failed"


def test_rewrite_timeout_uses_original_and_retains_unknown_wire_flag(env, monkeypatch):
    import time
    from backend.memory_app.v2 import followup

    publish(env)
    monkeypatch.setattr(followup, "TIMEOUT_SECONDS", 0.5)
    original = env.model.complete

    def complete(messages, *, max_tokens, validate_current, wire_attempt_sink=None, timeout_seconds=None):
        if '"queries"' not in messages[0]["content"]:
            return original(messages, max_tokens=max_tokens, validate_current=validate_current, wire_attempt_sink=wire_attempt_sink)
        attempt = wire_attempt_sink.begin_model_wire_attempt()

        def wire():
            time.sleep(1.0)
            attempt.succeeded(usage={"input_tokens": 4, "output_tokens": 5}, cache_observation=None)
            return json.dumps({"queries": ["late"]})

        output = attempt.invoke_wire(wire)
        return output, {}

    env.model.complete = complete
    response = ask(env, text="alpha absent1 absent2 absent3?")
    assert response.status_code == 200, response.text
    receipt = response.json()["turn"]["receipt"]["ask"]
    assert receipt["trace"][0]["rewrite_status"] == "timeout" and receipt["trace"][0]["rewrite"]["used"] is False
    assert receipt["no_match"] is False and receipt["model_usage"]["observed_only"] is True


def test_small_model_window_skips_auxiliary_wire(env):
    import asyncio
    from backend.memory_app.v2.followup import auxiliary_call
    from backend.memory_app.v2.multi_query import QueryVariants

    env.model.generation_budget_limits = lambda **kwargs: {"window": 20, "reserve": 10}
    result = asyncio.run(
        auxiliary_call(
            env.domains.query,
            "alpha",
            [{"role": "user", "content": "x" * 400}],
            QueryVariants,
            turn_id="tiny",
            validate_current=lambda: None,
        )
    )
    assert result["status"] == "skipped" and env.model.calls == 0


@pytest.mark.parametrize("mode,complete_usage", [("guard", True), ("failure", False), ("success_unknown", False)])
def test_unobservable_call_distinguishes_not_started_from_unknown_usage(env, mode, complete_usage):
    import asyncio
    from backend.memory_app.v2.followup import auxiliary_call
    from backend.memory_app.v2.multi_query import QueryVariants

    def model(messages, **kwargs):
        if mode == "failure":
            raise RuntimeError("synthetic")
        return json.dumps({"queries": []}), {}

    def validate():
        if mode == "guard":
            raise RuntimeError("synthetic guard")

    env.model.complete = model
    result = asyncio.run(
        auxiliary_call(
            env.domains.query,
            "alpha",
            [{"role": "user", "content": "query"}],
            QueryVariants,
            turn_id="unknown",
            validate_current=validate,
        )
    )
    assert result["usage_complete"] is complete_usage
    assert result["usage"] == {} and result["receipt_ids"] == []
