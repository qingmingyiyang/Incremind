import pytest

from backend.memory_app.v2.budget import evidence_tokens, input_tokens, trim_candidate
from backend.memory_app.v2.ladder import plan_ladder
from core.search_and_recall.evidence_windows import EvidenceWindow
from tests.memory_app.v2.test_ladder import env as _env_fixture, document
from tests.memory_app.v2.test_workbench_ask import env as _ask_env_fixture, add_document, ask, publish

env = _env_fixture
ask_env = _ask_env_fixture


def candidate(identity, text, *, layer="L3", score=1, **extra):
    return {"id": identity, "title": identity, "layer": layer, "score": score, "excerpt": text, **extra}


def test_prompt_reserve_skips_large_then_admits_smaller_candidate():
    small = candidate("small", "alpha")
    big = candidate("big", "alpha " + "知识" * 200, score=2)
    budget = input_tokens([small], "alpha beta gamma") - 1
    plan = plan_ladder([big, small], "alpha beta gamma", token_budget=budget)
    assert plan["chosen"] == []
    assert sum(row["skipped_budget"] for row in plan["trace"]) == 2
    plan = plan_ladder([big, small], "alpha beta gamma", token_budget=budget * 2)
    assert [r["id"] for r in plan["chosen"]] == ["small"]
    assert evidence_tokens(plan["chosen"]) <= int(budget * 2 * 0.8)
    assert sum(row["skipped_budget"] for row in plan["trace"]) == 1


def test_layer_caps_replace_global_eight_item_limit():
    rows = [candidate(f"{layer}-{i}", "alpha " + chr(65+4*level+i)*2, layer=layer) for level, layer in enumerate(["L3", "L2", "L1", "L0"]) for i in range(4)]
    rows += [candidate(f"persona-{i}", "alpha " + chr(85+i)*2, persona=True) for i in range(4)]
    result = plan_ladder(rows, "alpha beta gamma")
    assert len(result["chosen"]) == 9
    assert sum(row["selected"] for row in result["trace"]) == 9
    assert evidence_tokens(result["chosen"]) <= 3200


def test_expansion_after_three_insights_shares_budget():
    rows = [candidate(str(i), "alpha " + chr(65+i) * 300) for i in range(3)]
    neighbor = candidate("neighbor", "alpha " + "x" * 300, expansion_only=True)
    edges = lambda identity: [{"kind": "related", "other_id": "neighbor", "score": 1}]
    result = plan_ladder(rows + [neighbor], "alpha beta gamma", neighbors=edges)
    assert len(result["chosen"]) == 4
    assert result["chosen"][-1]["expanded_from"] == "0"
    result = plan_ladder(
        rows + [neighbor],
        "alpha beta gamma",
        neighbors=edges,
        token_budget=input_tokens([{**row, "link_kind": "refutes"} for row in rows], "alpha beta gamma") + 1,
    )
    assert len(result["chosen"]) == 3
    assert sum(row["skipped_budget"] for row in result["trace"]) == 1


def test_long_recognition_is_not_cut_or_missing_conditions():
    row = candidate("long", "知" * 1500 + "\n条件：禁止外发", kind="recognition")
    before = dict(row)
    assert trim_candidate(row, "知识") is None
    assert row == before


def test_trimmed_windows_remain_exact_source_slices():
    text = "😀" * 700 + " alpha " + "😀" * 700
    row = candidate("doc", text, layer="L0", kind="source", windows=(EvidenceWindow(100, 100 + len(text), text),))
    fitted = trim_candidate(row, "alpha")
    assert fitted is not None
    assert "alpha" in fitted["excerpt"]
    for window in fitted["windows"]:
        assert window.text == text[window.start - 100 : window.end - 100]
    assert len(fitted["excerpt"]) < len(text)


@pytest.mark.parametrize("window,reserve,total", [(1000, 200, 400), (16000, 2000, 6000), (None, None, 4000)])
def test_preparation_uses_gateway_limits_or_fallback(env, window, reserve, total):
    document(env)

    class Models:
        def public(self):
            return {"generation": {"base_url": "http://localhost/v1", "revision": 7}}

        def generation_budget_limits(self, *, expected_revision, max_tokens):
            assert expected_revision == 7 and max_tokens == 512
            return {"window": window, "reserve": reserve}

    env.query.models = Models()
    result = env.query.prepare_ask("alpha", "alpha beta gamma")
    assert result["budget"] == total
    assert evidence_tokens(result["chosen"]) <= int(total * 0.8)


def test_real_answer_context_uses_same_evidence_budget_and_offsets(ask_env):
    def limits(*, expected_revision, max_tokens):
        assert expected_revision == 2 and max_tokens == 7000
        return {"window": 8000, "reserve": 7000}

    ask_env.model.generation_budget_limits = limits
    add_document(
        ask_env,
        summary="alpha",
        body="alpha beta gamma " + "😀" * 1500,
        original="alpha beta gamma 原文 small evidence",
    )
    response = ask(ask_env, text="alpha beta gamma 原文?")
    assert response.status_code == 200, response.text
    receipt = response.json()["turn"]["receipt"]["ask"]
    assert receipt["no_match"] is False and ask_env.model.calls == 1
    parts = {part["key"]: part for part in receipt["context"]["parts"]}
    assert sum(parts[key]["tokens"] for key in ["insight", "persona", "summary", "note", "source"]) <= 400
    assert sum(row["skipped_budget"] for row in receipt["trace"]) >= 1
    assert receipt["citations"] and all(c["locator"]["windows"] for c in receipt["citations"])


def test_long_ascii_recognition_remains_complete_when_it_fits_tokens(ask_env):
    from backend.memory_app.v2.budget import text_tokens

    original = "alpha " + "x" * 1900
    insight, _ = publish(ask_env, text=original)
    prepared = ask_env.domains.query.prepare_ask("alpha", "alpha beta gamma")
    row = next(candidate for candidate in prepared["chosen"] if candidate["id"] == insight.id)
    assert len(row["excerpt"]) > 1800 and text_tokens(row["excerpt"]) <= 1200
    assert original in row["excerpt"] and "来源证据" in row["excerpt"]


def test_total_prompt_cannot_exceed_budget_with_long_question():
    from backend.shared.llm.litellm_gateway import _estimate_input_tokens
    from backend.memory_app.v2.budget import ask_instruction, source_texts, user_text

    question = "问题" * 250
    result = plan_ladder([candidate("small", "alpha")], question, token_budget=100)
    assert not result["chosen"]
    assert sum(row["skipped_budget"] for row in result["trace"]) == 1
    result = plan_ladder([candidate("small", "alpha")], question, token_budget=2000)
    actual = _estimate_input_tokens(
        [
            {"role": "system", "content": ask_instruction(result["chosen"])},
            {"role": "user", "content": user_text(source_texts(result["chosen"]), question)},
        ]
    )
    assert result["chosen"] and actual <= 2000


@pytest.mark.parametrize("reserve", [1000, 1200])
def test_known_exhausted_window_does_not_fallback(env, reserve):
    document(env)

    class Models:
        def public(self):
            return {"generation": {"base_url": "http://localhost/v1", "revision": 7}}

        def generation_budget_limits(self, **kwargs):
            return {"window": 1000, "reserve": reserve}

    env.query.models = Models()
    result = env.query.prepare_ask("alpha", "alpha beta gamma")
    assert result["budget"] == 0
    assert result["chosen"] == []


def test_budget_exhaustion_preserves_trace_without_model_or_egress(ask_env):
    add_document(ask_env, original="alpha beta gamma")
    ask_env.model.generation_budget_limits = lambda **kwargs: {"window": 1000, "reserve": 1000}
    response = ask(ask_env)
    assert response.status_code == 200, response.text
    receipt = response.json()["turn"]["receipt"]["ask"]
    assert receipt["no_match"] is True
    assert ask_env.model.calls == 0 and receipt["egress_receipt_id"] is None
    assert sum(row["skipped_budget"] for row in receipt["trace"]) > 0


def test_default_budget_without_neighbors_admits_medium_atomic_insight():
    row = candidate("medium", "alpha " + "x" * 1500)
    result = plan_ladder([row], "alpha beta gamma")
    assert result["chosen"] == [row]
    assert 400 < evidence_tokens(result["chosen"]) < 1200


@pytest.mark.parametrize("window", [1200, 2200])
def test_native_schema_framing_is_reserved_before_selecting(env, window):
    from backend.memory_app.structured_generation import AskOutput
    from backend.shared.llm.litellm_gateway import _build_prompt_fallback_messages, _estimate_input_tokens
    from backend.memory_app.v2.budget import ask_instruction, source_texts, user_text

    document(env)

    class Models:
        def public(self):
            return {"generation": {"base_url": "http://localhost/v1", "revision": 7}}

        def generation_budget_limits(self, **kwargs):
            return {"window": window, "reserve": 512}

        def complete_structured(self, *args, **kwargs):
            raise AssertionError("retrieval must not generate")

    env.query.models = Models()
    result = env.query.prepare_ask("alpha", "alpha beta gamma" + "问题" * 40)
    messages = [
        {"role": "system", "content": ask_instruction(result["chosen"])},
        {"role": "user", "content": user_text(source_texts(result["chosen"]), result["question"])},
    ]
    framed = _build_prompt_fallback_messages(messages=messages, response_model=AskOutput, validation_error=None)
    if window == 2200:
        assert result["chosen"]
    if result["chosen"]:
        assert _estimate_input_tokens(framed) <= result["budget"]
    else:
        assert sum(row["skipped_budget"] for row in result["trace"]) > 0
