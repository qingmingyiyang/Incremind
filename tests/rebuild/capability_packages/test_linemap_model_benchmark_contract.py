from __future__ import annotations

from dataclasses import replace
import json

import pytest

from core.capability_packages.thought_graph_context import (
    ModelBenchmarkError,
    TurnModelObservation,
    build_model_benchmark_cases,
    build_model_benchmark_turn_pair,
    freeze_case_binding,
    score_model_benchmark,
    score_model_benchmark_suite,
)
from core.capability_packages.thought_graph_context.model_benchmark import (
    build_model_benchmark_definition,
)
from core.context_graph import FrozenContextRevisions
from core.ai_kernel import validate_turn_request


def _observation(
    case_id: str,
    variant: str,
    output: str,
    *,
    route: str = "route-r4",
    execution_location: str = "remote",
    suite_run_id: str = "suite-test-a",
    replicate_index: int = 0,
) -> TurnModelObservation:
    turn_id = f"turn-{case_id}-{variant}"
    return TurnModelObservation(
        suite_run_id=suite_run_id,
        replicate_index=replicate_index,
        operation_id=(
            f"op-lm-{suite_run_id}-{case_id}-r{replicate_index}-{variant}"
        ),
        case_id=case_id,
        variant=variant,
        turn_id=turn_id,
        turn_terminal_event_id=f"event-{case_id}-{variant}",
        model_receipt_ref=f"crp://session/{turn_id}/model-receipt/ref",
        routing_snapshot_ref=f"crp://session/{turn_id}/model-routing/ref",
        model_request_id=f"model-request-{case_id}-{variant}",
        model_attempt_id=f"model-wire-attempt-{case_id}-{variant}",
        routing_snapshot_revision="a" * 64,
        status="completed",
        output_text=output,
        input_tokens=100,
        output_tokens=20,
        total_tokens=120,
        route_key="standard",
        route_revision=route,
        provider_id="provider-a",
        provider_revision="provider-r2",
        model_name="model-a",
        execution_location=execution_location,
        capability_revision="4.0.0",
        compiler_revision="2.0.0",
        boundary_revision="7",
        decoding_revision="model-planner-json-t0-v1",
    )


def _project_output(*, complete: bool) -> str:
    return json.dumps({
        "proposal_status": "proposal_only" if complete else "direct_publish",
        "rules": [
            "proposal-only; Gate and Effect Runner protect formal writes; preserve source revision and failure condition"
            if complete else "publish the result"
        ],
        "source_refs": (
            ["source:requirement", "source:architecture", "source:decision"]
            if complete else ["source:unknown"]
        ),
        "excluded_option_ids": ["rejected_direct_write" if complete else "other"],
        "usage_boundaries": [
            "Formal writes require Gate and Effect Runner" if complete else "No boundary"
        ],
        "failure_conditions": ["failure condition: missing source revision" if complete else "unknown"],
        "validation_steps": ["validate source revision" if complete else "inspect"],
        "maintenance_actions": (
            ["platform_evidence", "decision", "skill_proposal"]
            if complete else ["rewrite_everything"]
        ),
    }, separators=(",", ":"))


def _document_output(*, correct: bool) -> str:
    return json.dumps({
        "paragraphs": [
            {"paragraph_id": "paragraph_a", "text": (
                "Paragraph A: Evidence A no longer verifies Fact A."
                if correct else "Paragraph A uses Evidence A."
            ),
             "source_refs": ["source:a", "source:a-evidence"] if correct else ["source:b"]},
            {"paragraph_id": "paragraph_b", "text": "Paragraph B follows only from Fact B.",
             "source_refs": ["source:b"]},
        ],
        "affected_paragraph_ids": ["paragraph_a" if correct else "paragraph_b"],
        "regeneration_order": ["paragraph_a" if correct else "paragraph_b"],
        "unchanged_paragraph_ids": ["paragraph_b" if correct else "paragraph_a"],
    }, separators=(",", ":"))


def _research_output(*, correct: bool) -> str:
    return json.dumps({
        "supported_hypothesis": "Hypothesis A" if correct else "Hypothesis B",
        "evidence_refs": ["source:evidence-a", "source:open"] if correct else ["source:wrong"],
        "excluded_claim_ids": ["wrong_evidence" if correct else "evidence_a"],
        "stale_conclusion_ids": ["stale_conclusion" if correct else "synthesis"],
        "replay_order": ["stale_conclusion", "synthesis"] if correct else ["synthesis", "stale_conclusion"],
        "open_questions": ["external replication remains open"],
        "included_node_ids": (
            ["evidence_a", "open_question", "stale_conclusion", "synthesis"]
            if correct else ["wrong_evidence"]
        ),
        "budget_note": "No selected conclusion was trimmed.",
        "selection_explanation": (
            "The excluded branch is removed and the stale conclusion is replayed from source context."
            if correct else "All branches were accepted."
        ),
    }, separators=(",", ":"))


def test_model_benchmark_freezes_three_real_task_cases_without_provider_access() -> None:
    cases = build_model_benchmark_cases()
    assert [case.case_id for case in cases] == [
        "project_skill", "document", "research_turn",
    ]
    revisions = FrozenContextRevisions("4.0.0", "7", "provider-r2", "route-r4", "2.0.0")
    for case in cases:
        binding = freeze_case_binding(case, revisions)
        assert binding.capability_revision == "4.0.0"
        assert binding.boundary_revision == "7"
        assert binding.provider_revision == "provider-r2"
        assert binding.model_route_revision == "route-r4"


def test_model_benchmark_rejects_unknown_compiler_revision_before_binding_creation() -> None:
    case = build_model_benchmark_cases()[0]

    with pytest.raises(ModelBenchmarkError, match="compiler revision drifted"):
        freeze_case_binding(
            case,
            FrozenContextRevisions(
                "4.0.0", "7", "provider-r2", "route-r4", "compiler-r9",
            ),
        )


def test_model_benchmark_scores_only_same_frozen_route_and_actual_usage() -> None:
    case = build_model_benchmark_cases()[0]
    linear = _observation(case.case_id, "linear", _project_output(complete=False))
    linemap = _observation(
        case.case_id,
        "linemap",
        _project_output(complete=True),
    )

    result = score_model_benchmark(case, linear, linemap, suite_run_id="suite-test-a")

    assert result.model_quality_verified is True
    assert result.same_frozen_route is True
    assert result.benefit_gate_passed is True
    assert result.linemap_metrics["rule_completeness"] == 1.0
    assert result.linemap_metrics["source_traceability"] == 1.0
    assert result.linemap_metrics["token_cost"] == 120.0
    assert result.linear_metrics["usage_boundary_accuracy"] == 0.0
    assert result.evidence["execution_location"] == "remote"

    with pytest.raises(ModelBenchmarkError, match="different frozen routes"):
        score_model_benchmark(
            case, linear, replace(linemap, route_revision="route-r5"),
            suite_run_id="suite-test-a",
        )
    differing_snapshot = score_model_benchmark(
        case,
        linear,
        replace(linemap, routing_snapshot_revision="b" * 64),
        suite_run_id="suite-test-a",
    )
    assert differing_snapshot.same_frozen_route is True
    assert differing_snapshot.evidence["linear_routing_snapshot_revision"] == "a" * 64
    assert differing_snapshot.evidence["linemap_routing_snapshot_revision"] == "b" * 64
    with pytest.raises(ModelBenchmarkError, match="evidence is incomplete"):
        score_model_benchmark(
            case,
            linear,
            replace(linemap, routing_snapshot_revision="not-a-revision"),
            suite_run_id="suite-test-a",
        )
    no_gain = score_model_benchmark(
        case,
        _observation(case.case_id, "linear", _project_output(complete=True)),
        _observation(case.case_id, "linemap", _project_output(complete=True)),
        suite_run_id="suite-test-a",
    )
    assert no_gain.benefit_gate_passed is False


def test_model_benchmark_rejects_local_loopback_observations_from_benefit_qualification() -> None:
    case = build_model_benchmark_cases()[0]
    linear = _observation(
        case.case_id,
        "linear",
        _project_output(complete=False),
        execution_location="local_loopback",
    )
    linemap = _observation(
        case.case_id,
        "linemap",
        _project_output(complete=True),
        execution_location="local_loopback",
    )

    with pytest.raises(ModelBenchmarkError, match="remote execution location"):
        score_model_benchmark(case, linear, linemap, suite_run_id="suite-test-a")


def test_model_benchmark_definition_exposes_only_pure_suite_callables() -> None:
    definition = build_model_benchmark_definition()

    assert set(definition) == {
        "schema_version", "case_builder", "pair_builder", "case_scorer",
        "suite_scorer",
    }
    assert definition["schema_version"] == "1.0.0"
    assert all(
        callable(definition[name])
        for name in ("case_builder", "pair_builder", "case_scorer", "suite_scorer")
    )


def test_model_benchmark_builds_valid_governed_turn_pair() -> None:
    case = build_model_benchmark_cases()[1]
    arguments = {
        "project_id": "project-alpha",
        "session_id": "session-linemap-benchmark",
        "linear_turn_id": "turn-linemap-document-linear",
        "linemap_turn_id": "turn-linemap-document-graph",
        "binding_id": "binding-document-r1",
        "suite_run_id": "suite-20260830-a",
        "revisions": FrozenContextRevisions("4.0.0", "7", "provider-r2", "route-r4", "2.0.0"),
        "created_at": "2026-08-29T10:00:00Z",
        "consent_refs": ("crp://consents/project-alpha/model-benchmark-r1",),
    }
    pair = build_model_benchmark_turn_pair(case, **arguments)

    assert validate_turn_request(pair.linear_turn)["turn_id"] == "turn-linemap-document-linear"
    assert validate_turn_request(pair.linemap_turn)["turn_id"] == "turn-linemap-document-graph"
    assert pair.linear_turn["input"]["refs"] == []
    assert pair.linemap_turn["input"]["refs"][0]["uri"] == (
        "crp://context-bindings/project-alpha/binding-document-r1"
    )
    assert pair.binding_creation["capability_revision"] == "4.0.0"
    assert "affected_paragraph_ids" in pair.linear_turn["input"]["text"]
    assert pair.linear_turn["desired_outcome"] == "context.evaluate"
    assert pair.linemap_turn["desired_outcome"] == "context.evaluate"
    assert pair.linear_turn["capability_policy"]["allowed"] == []
    assert pair.linemap_turn["input"]["text"] == case.instruction
    assert pair.linear_turn["operation_id"].startswith("op-lm-")
    assert pair.linemap_turn["operation_id"].startswith("op-lm-")
    assert pair.suite_run_id == "suite-20260830-a"
    assert pair.linear_turn["idempotency_key"] != pair.linemap_turn["idempotency_key"]

    with pytest.raises(ModelBenchmarkError, match="suite run identity is invalid"):
        build_model_benchmark_turn_pair(case, **{**arguments, "suite_run_id": "bad run"})


def test_model_benchmark_scores_document_and_research_gold_structures() -> None:
    document = build_model_benchmark_cases()[1]
    document_result = score_model_benchmark(
        document,
        _observation(document.case_id, "linear", _document_output(correct=False)),
        _observation(document.case_id, "linemap", _document_output(correct=True)),
        suite_run_id="suite-test-a",
    )
    assert document_result.linemap_metrics["paragraph_source_coverage"] == 1.0
    assert document_result.linemap_metrics["affected_paragraph_identification"] == 1.0
    assert document_result.linemap_metrics["local_regeneration_accuracy"] == 1.0
    assert document_result.linemap_metrics["unrelated_paragraph_stability"] == 1.0
    assert document_result.linear_metrics["affected_paragraph_identification"] == 0.0

    research = build_model_benchmark_cases()[2]
    research_result = score_model_benchmark(
        research,
        _observation(research.case_id, "linear", _research_output(correct=False)),
        _observation(research.case_id, "linemap", _research_output(correct=True)),
        suite_run_id="suite-test-a",
    )
    assert research_result.linemap_metrics["wrong_context_recovery"] == 1.0
    assert research_result.linemap_metrics["stale_conclusion_identification"] == 1.0
    assert research_result.linemap_metrics["dependency_replay_order"] == 1.0
    assert research_result.linemap_metrics["selection_transparency"] == 1.0


def test_model_benchmark_invalid_output_contract_cannot_pass() -> None:
    case = build_model_benchmark_cases()[0]
    result = score_model_benchmark(
        case,
        _observation(case.case_id, "linear", _project_output(complete=False)),
        _observation(case.case_id, "linemap", "proposal-only Gate Effect Runner"),
        suite_run_id="suite-test-a",
    )
    assert result.model_quality_verified is False
    assert result.benefit_gate_passed is False
    assert result.evidence["linemap_output_contract_valid"] is False


def test_model_benchmark_suite_requires_all_three_case_gates() -> None:
    cases = build_model_benchmark_cases()
    outputs = (
        (_project_output(complete=False), _project_output(complete=True)),
        (_document_output(correct=False), _document_output(correct=True)),
        (_research_output(correct=False), _research_output(correct=True)),
    )
    results = tuple(
        score_model_benchmark(
            case,
            _observation(
                case.case_id,
                "linear",
                pair[0],
                suite_run_id="suite-20260830-a",
            ),
            _observation(
                case.case_id,
                "linemap",
                pair[1],
                suite_run_id="suite-20260830-a",
            ),
            suite_run_id="suite-20260830-a",
        )
        for case, pair in zip(cases, outputs, strict=True)
    )
    suite = score_model_benchmark_suite("suite-20260830-a", results)
    assert suite.all_required_cases_present is True
    assert suite.all_model_outputs_verified is True
    assert suite.all_case_gates_passed is True
    assert suite.native_canvas_eligible is True
    assert suite.hard_failures == ()

    partial = score_model_benchmark_suite("suite-20260830-a", results[:1])
    assert partial.native_canvas_eligible is False
    assert partial.hard_failures == ("missing_required_case",)

    with pytest.raises(ModelBenchmarkError, match="suite run identity drifted"):
        score_model_benchmark_suite("suite-20260830-b", results)

    drifted = replace(
        results[-1],
        evidence={**results[-1].evidence, "route_revision": "route-r5"},
    )
    route_drift = score_model_benchmark_suite(
        "suite-20260830-a",
        (*results[:-1], drifted),
    )
    assert route_drift.native_canvas_eligible is False
    assert "suite_frozen_identity_drift" in route_drift.hard_failures


def test_model_benchmark_rejects_fabricated_or_incomplete_observations() -> None:
    case = build_model_benchmark_cases()[2]
    good = _observation(case.case_id, "linear", _research_output(correct=True))
    with pytest.raises(ModelBenchmarkError, match="usage total drifted"):
        score_model_benchmark(
            case,
            replace(good, total_tokens=999),
            _observation(case.case_id, "linemap", _research_output(correct=True)),
            suite_run_id="suite-test-a",
        )
    with pytest.raises(ModelBenchmarkError, match="incomplete"):
        score_model_benchmark(
            case,
            good,
            replace(_observation(case.case_id, "linemap", _research_output(correct=True)), output_text=""),
            suite_run_id="suite-test-a",
        )
