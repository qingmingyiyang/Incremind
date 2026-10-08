from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from core.product_core.expert_benchmark import (
    ExpertBenchmarkError,
    FrozenModelPrice,
    evaluate_expert_ab_case,
    evaluate_expert_benchmark_corpus,
    load_expert_benchmark_corpus,
)


FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "expert_benchmark"
    / "expert-ab-synthetic-v1.json"
)


def test_synthetic_fixture_is_offline_inconclusive_and_content_free() -> None:
    corpus = load_expert_benchmark_corpus(FIXTURE)
    result = evaluate_expert_benchmark_corpus(corpus)

    assert result["case_count"] == 1
    assert result["aggregate"] == {
        "quality_score_delta": 0.09,
        "total_token_delta": 8.0,
        "latency_ms_delta": 25.0,
        "cost_available": False,
    }
    case = result["results"][0]
    assert case["cost"] == {
        "available": False,
        "currency": None,
        "price_revision": None,
        "source_revision": None,
        "baseline": None,
        "treatment": None,
    }
    assert case["governance"] == {
        "provider_called": False,
        "network_used": False,
        "user_session_read": False,
        "prompt_or_output_recorded": False,
        "expert_binding_changed": False,
        "benefit_claimable": False,
        "status": "inconclusive",
        "activation": "disabled",
        "reason": "offline_observations_cannot_authorize_expert_activation",
    }
    assert result["recommendation"]["benefit_claimable"] is False
    assert result["recommendation"]["activation"] == "disabled"
    assert set(corpus.cases[0].__dataclass_fields__) == {
        "case_id", "route_id", "boundary_revision", "model_id", "decoder",
        "tool_budget", "rubric_revision", "treatment_expert_id", "price",
    }
    assert set(corpus.observations[0].__dataclass_fields__) == {
        "case_id", "arm", "expert_id", "route_id", "boundary_revision",
        "model_id", "decoder", "tool_budget", "quality_score", "input_tokens",
        "output_tokens", "latency_ms", "rubric_revision",
    }


def test_comparison_rejects_any_frozen_envelope_difference() -> None:
    corpus = load_expert_benchmark_corpus(FIXTURE)
    case = corpus.cases[0]
    baseline, treatment = corpus.observations

    with pytest.raises(ExpertBenchmarkError, match="frozen envelope drifted"):
        evaluate_expert_ab_case(
            case,
            baseline=baseline,
            treatment=replace(treatment, decoder="temperature=0.2"),
        )
    with pytest.raises(ExpertBenchmarkError, match="differs by more than explicit Expert"):
        evaluate_expert_ab_case(
            case,
            baseline=replace(baseline, expert_id="video-research-expert"),
            treatment=treatment,
        )
    with pytest.raises(ExpertBenchmarkError, match="frozen envelope drifted"):
        evaluate_expert_ab_case(
            case,
            baseline=baseline,
            treatment=replace(treatment, rubric_revision="rubric:drift-r2"),
        )


def test_cost_is_available_only_with_frozen_price() -> None:
    corpus = load_expert_benchmark_corpus(FIXTURE)
    base_case = corpus.cases[0]
    baseline, treatment = corpus.observations
    priced = replace(
        base_case,
        price=FrozenModelPrice(
            input_per_token=0.001,
            output_per_token=0.002,
            currency="USD",
            price_revision="price:fixture-r1",
            source_revision="source:fixture-r1",
        ),
    )

    result = evaluate_expert_ab_case(
        priced, baseline=baseline, treatment=treatment
    )

    assert result["cost"] == {
        "available": True,
        "currency": "USD",
        "price_revision": "price:fixture-r1",
        "source_revision": "source:fixture-r1",
        "baseline": 0.28,
        "treatment": 0.29,
    }
    assert result["deltas"]["cost"] == 0.01


def test_loader_refuses_non_synthetic_corpus(tmp_path) -> None:
    target = tmp_path / "invalid.json"
    target.write_text(
        FIXTURE.read_text(encoding="utf-8").replace(
            "synthetic_non_personal", "user_session_export"
        ),
        encoding="utf-8",
    )

    with pytest.raises(ExpertBenchmarkError, match="synthetic_non_personal"):
        load_expert_benchmark_corpus(target)

    corpus = load_expert_benchmark_corpus(FIXTURE)
    with pytest.raises(ExpertBenchmarkError, match="synthetic_non_personal"):
        replace(corpus, data_class="user_session_export")


def test_loader_rejects_quality_score_above_one(tmp_path) -> None:
    target = tmp_path / "invalid-quality.json"
    target.write_text(
        FIXTURE.read_text(encoding="utf-8").replace(
            '"quality_score": 0.71', '"quality_score": 1.01'
        ),
        encoding="utf-8",
    )

    with pytest.raises(ExpertBenchmarkError, match="quality_score must be between 0 and 1"):
        load_expert_benchmark_corpus(target)

    corpus = load_expert_benchmark_corpus(FIXTURE)
    with pytest.raises(ExpertBenchmarkError, match="quality_score must be between 0 and 1"):
        replace(corpus.observations[0], quality_score=1.01)
