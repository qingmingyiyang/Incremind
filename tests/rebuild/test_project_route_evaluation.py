from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.product_core.project_route_evaluation import (
    ProjectRouteEvaluationError,
    compare_project_route_strategies,
    evaluate_project_routes,
    load_project_route_evaluation_corpus,
)


ROOT = Path(__file__).resolve().parents[2]
CORPUS = (
    ROOT
    / "config"
    / "memory"
    / "evaluation"
    / "project-route-gold-v1.json"
)


def test_gold_corpus_runs_real_router_in_deterministic_and_replay_modes() -> None:
    corpus = load_project_route_evaluation_corpus(CORPUS)

    baseline, baseline_cases = evaluate_project_routes(
        corpus,
        use_ai_replay=False,
    )
    assisted, assisted_cases = evaluate_project_routes(
        corpus,
        use_ai_replay=True,
    )

    assert corpus.version == "project-route-gold-v1"
    assert corpus.data_class == "fictional_desensitized"
    assert baseline.case_count == assisted.case_count == len(corpus.cases)
    assert len(baseline_cases) == len(assisted_cases) == len(corpus.cases)
    assert baseline.exact_outcome_accuracy == 0.9375
    assert baseline.routed_accuracy == 0.916667
    assert baseline.ambiguity_accuracy == 1.0
    assert assisted.exact_outcome_accuracy == 1.0
    assert assisted.routed_accuracy == 1.0
    assert baseline.unsafe_misselection_rate == 0.0
    assert assisted.unsafe_misselection_rate == 0.0
    assert assisted.provider_call_rate == 0.1875
    assert all(result.latency_ms >= 0 for result in assisted_cases)


def test_strategy_report_is_privacy_safe_and_cannot_change_production() -> None:
    corpus = load_project_route_evaluation_corpus(CORPUS)

    report = compare_project_route_strategies(corpus)

    assert report["production_weights_changed"] is False
    assert report["activation"]["status"] == "deferred"
    assert report["ai_replay"]["evidence_class"] == (
        "fixture_replay_not_real_provider"
    )
    assert report["recommendation"]["status"] == "lab_only"
    assert report["recommendation"]["deterministic_weight"] == 0.75
    assert report["recommendation"]["ai_weight"] == 0.25
    assert len(report["weight_grid"]) == 6
    serialized = json.dumps(report, ensure_ascii=False)
    for case in corpus.cases:
        assert case.query not in serialized
    assert "source_refs" not in serialized
    assert "locator" not in serialized


def test_corpus_rejects_non_fictional_or_incomplete_ai_scores(
    tmp_path: Path,
) -> None:
    payload = json.loads(CORPUS.read_text(encoding="utf-8"))
    payload["data_class"] = "real_user_content"
    path = tmp_path / "unsafe.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
        ProjectRouteEvaluationError,
        match="fictional_desensitized",
    ):
        load_project_route_evaluation_corpus(path)

    payload["data_class"] = "fictional_desensitized"
    payload["cases"][0]["ai_replay"]["scores"].pop("memory-os")
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(
        ProjectRouteEvaluationError,
        match="scores must cover all projects",
    ):
        load_project_route_evaluation_corpus(path)
