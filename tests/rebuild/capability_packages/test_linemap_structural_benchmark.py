from __future__ import annotations

from core.capability_packages.thought_graph_context import run_structural_benchmark


def test_project_skill_structural_benefit() -> None:
    result = run_structural_benchmark()[0]
    assert result.evaluation_id == "project_skill"
    assert result.linemap_metrics["rule_completeness"] == 1.0
    assert result.linemap_metrics["source_traceability"] > result.linear_metrics["source_traceability"]
    assert result.linemap_metrics["excluded_option_control"] == 1.0
    assert result.linemap_metrics["boundary_accuracy"] > result.linear_metrics["boundary_accuracy"]
    assert result.evidence["token_reduction"] > 0
    assert result.model_quality_verified is False


def test_document_local_impact_benefit() -> None:
    result = run_structural_benchmark()[1]
    assert result.evaluation_id == "document"
    assert result.linemap_metrics["paragraph_source_coverage"] == 1.0
    assert result.linemap_metrics["affected_paragraph_precision"] == 1.0
    assert result.linemap_metrics["affected_paragraph_recall"] == 1.0
    assert result.linemap_metrics["unrelated_paragraph_stability"] == 1.0
    assert result.evidence["replay_order"] == ("evidence_a", "paragraph_a", "document")


def test_research_wrong_context_recovery_and_transparency() -> None:
    result = run_structural_benchmark()[2]
    assert result.evaluation_id == "research_turn"
    assert result.linemap_metrics["wrong_context_recovery"] == 1.0
    assert result.linear_metrics["wrong_context_recovery"] == 0.0
    assert result.linemap_metrics["evidence_merge_quality"] == 1.0
    assert result.linemap_metrics["selection_transparency"] == 1.0
    assert "wrong_evidence" in result.evidence["excluded_nodes"]
    assert result.evidence["stale_nodes"] == ("stale_conclusion", "synthesis")
    assert result.evidence["model_replay_order"] == ("stale_conclusion", "synthesis")


def test_benchmark_is_deterministic() -> None:
    assert run_structural_benchmark() == run_structural_benchmark()
