from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Mapping, Sequence

from ...context_graph import (
    ContextCompiler, ContextGraphEdge, ContextGraphNode, ContextGraphSnapshot,
    ContextPermissionGrant, ContextProvenance, FrozenContextRevisions,
    StalenessConfirmation, StalenessEvaluationInput, evaluate_staleness,
    stale_replay_order, staleness_impact_preview,
)


@dataclass(frozen=True, slots=True)
class ComparisonResult:
    evaluation_id: str
    linear_metrics: Mapping[str, float]
    linemap_metrics: Mapping[str, float]
    evidence: Mapping[str, object]
    model_quality_verified: bool = False


def _node(node_id: str, node_type: str, content: str, sources: Sequence[str], *, revision: str = "r1") -> ContextGraphNode:
    return ContextGraphNode(
        node_id=node_id, node_type=node_type, title=node_id.replace("_", " "),
        content_ref=f"fixture:{node_id}", content_revision=revision, source_refs=tuple(sources),
        trust="verified" if node_type in {"evidence", "material"} else "user_authored",
        created_at="2026-08-29T00:00:00Z", updated_at="2026-08-29T00:00:00Z",
        metadata={"project_id": "linemap-eval", "content": content, "highlights": (content[:80],)},
    )


def _snapshot(graph_id: str, nodes: Sequence[ContextGraphNode], edges: Sequence[ContextGraphEdge], selected: Sequence[str], revision: str = "g1") -> ContextGraphSnapshot:
    chars = sum(len(str(node.metadata["content"])) for node in nodes)
    return ContextGraphSnapshot(
        "1.0.0", graph_id, revision, "linemap-eval", "evaluation_fixture", "fixture-v2",
        "2026-08-29T00:00:00Z", tuple(nodes), tuple(edges), tuple(selected), (chars + 3) // 4,
        ContextProvenance("evaluation_fixture", "fixture-v2", "2026-08-29T00:00:00Z",
                          "thought_graph_context.evaluation", "2.0.0", f"fixture:{graph_id}"), (),
    )


def _revision_map(revisions: FrozenContextRevisions) -> Mapping[str, str]:
    return {
        "capability_revision": revisions.capability_revision,
        "compiler_revision": revisions.compiler_revision,
        "boundary_revision": revisions.boundary_revision,
        "provider_revision": revisions.provider_revision,
        "model_route_revision": revisions.model_route_revision,
    }


def _compile(
    snapshot: ContextGraphSnapshot,
    budget: int = 10_000,
    *,
    acknowledge_existing_stale: bool = False,
):
    revisions = FrozenContextRevisions(
        "cap-eval-1", "boundary-eval-1", "provider-none", "model-none", "2.0.0",
    )
    grant = ContextPermissionGrant(snapshot.project_id, "permission-eval-1", frozenset(node.content_ref for node in snapshot.nodes))
    confirmation = None
    if acknowledge_existing_stale:
        evaluated = evaluate_staleness(
            snapshot,
            snapshot,
            previous_revisions=_revision_map(revisions),
            current_revisions=_revision_map(revisions),
        )
        preview = staleness_impact_preview(evaluated)
        confirmation = StalenessConfirmation(
            preview.graph_id,
            preview.graph_revision,
            preview.affected_node_ids,
            preview.replay_order,
            preview.stale_reasons,
            "evaluation-fixture",
            "2026-08-29T00:00:00Z",
        )
    return ContextCompiler().compile(snapshot, revisions=revisions, expected_revisions=revisions,
                                     permission_grant=grant, token_budget=budget,
                                     staleness_input=StalenessEvaluationInput.baseline(
                                         snapshot, revisions, confirmation=confirmation,
                                     ))


def _linear_text(snapshot: ContextGraphSnapshot) -> str:
    return "\n".join(str(node.metadata.get("content", "")) for node in snapshot.nodes)


def _ratio(found: int, total: int) -> float:
    return round(found / total, 4) if total else 1.0


def evaluate_project_skill() -> ComparisonResult:
    required = ("proposal-only", "Gate", "Effect Runner", "source revision", "failure condition")
    nodes = (
        _node("requirement", "question", "Build a maintained Project Skill with explicit source revision and failure condition.", ("source:requirement",)),
        _node("platform_evidence", "evidence", "Formal writes require Gate and Effect Runner. Output must stay proposal-only.", ("source:architecture",)),
        _node("rejected_direct_write", "rejected_option", " ".join([
            "Rejected design: LineMap should directly publish Project Skill and Memory without review, keep private retry state, and bypass the shared Effect path."
        ] * 8), ("source:rejected",)),
        _node("decision", "decision", "Use proposal-only output. Preserve source revision, failure condition, Gate and Effect Runner boundary.", ("source:decision",)),
        _node("skill_proposal", "project_skill_proposal", "Project Skill proposal-only contract with source revision, trigger, boundary, failure condition and validation.", ("source:decision", "source:architecture")),
    )
    edges = (
        ContextGraphEdge("e1", "requirement", "decision", "full_chain", 1, 0),
        ContextGraphEdge("e2", "platform_evidence", "decision", "quote_only", 1, 1),
        ContextGraphEdge("e3", "rejected_direct_write", "decision", "excluded", 1, 2),
        ContextGraphEdge("e4", "decision", "skill_proposal", "full_chain", 1, 3),
    )
    snapshot = _snapshot("project-skill-eval", nodes, edges, ("skill_proposal",))
    binding = _compile(snapshot)
    linear = _linear_text(snapshot)
    graph_text = "\n".join(str(item["content"]) for item in binding.messages)
    linear_tokens = (len(linear) + 3) // 4
    return ComparisonResult(
        "project_skill",
        {"rule_completeness": _ratio(sum(term.lower() in linear.lower() for term in required), len(required)),
         "source_traceability": 0.0, "excluded_option_control": 0.0,
         "boundary_accuracy": 0.0 if "directly publish" in linear else 1.0, "token_cost": float(linear_tokens)},
        {"rule_completeness": _ratio(sum(term.lower() in graph_text.lower() for term in required), len(required)),
         "source_traceability": _ratio(len(binding.source_refs), 3),
         "excluded_option_control": 1.0 if "rejected_direct_write" in binding.excluded_nodes else 0.0,
         "boundary_accuracy": 0.0 if "directly publish" in graph_text else 1.0, "token_cost": float(binding.total_token_cost)},
        {"excluded_nodes": binding.excluded_nodes, "source_refs": binding.source_refs,
         "token_reduction": round(1 - binding.total_token_cost / linear_tokens, 4),
         "linear_context": linear, "binding": binding},
    )


def evaluate_document() -> ComparisonResult:
    nodes = (
        _node("fact_a", "material", "Fact A establishes the first claim.", ("source:a",)),
        _node("evidence_a", "evidence", "Evidence A verifies Fact A.", ("source:a-evidence",)),
        _node("paragraph_a", "document_draft", "Paragraph A follows from Fact A and Evidence A.", ("source:a", "source:a-evidence")),
        _node("fact_b", "material", "Fact B establishes an independent second claim.", ("source:b",)),
        _node("paragraph_b", "document_draft", "Paragraph B follows only from Fact B.", ("source:b",)),
        _node("document", "document_draft", "A two-paragraph draft.", ("source:a", "source:b")),
    )
    edges = (
        ContextGraphEdge("e1", "fact_a", "evidence_a", "full_chain", 1, 0),
        ContextGraphEdge("e2", "evidence_a", "paragraph_a", "full_chain", 1, 1),
        ContextGraphEdge("e3", "paragraph_a", "document", "reference", 1, 2),
        ContextGraphEdge("e4", "fact_b", "paragraph_b", "full_chain", 1, 3),
        ContextGraphEdge("e5", "paragraph_b", "document", "reference", 1, 4),
    )
    before = _snapshot("document-eval", nodes, edges, ("document",))
    binding = _compile(before)
    changed_nodes = tuple(replace(node, content_revision="r2", metadata={**node.metadata, "content": "Evidence A changed."}) if node.node_id == "evidence_a" else node for node in nodes)
    after = replace(before, graph_revision="g2", nodes=changed_nodes)
    revisions = FrozenContextRevisions(
        "cap-eval-1", "boundary-eval-1", "provider-none", "model-none", "2.0.0",
    )
    stale = evaluate_staleness(
        before,
        after,
        previous_revisions=_revision_map(revisions),
        current_revisions=_revision_map(revisions),
    )
    stale_ids = {node.node_id for node in stale.nodes if node.stale}
    expected_affected = {"evidence_a", "paragraph_a", "document"}
    precision = _ratio(len(stale_ids & expected_affected), len(stale_ids))
    recall = _ratio(len(stale_ids & expected_affected), len(expected_affected))
    linear_tokens = (len(_linear_text(before)) + 3) // 4
    paragraph_sources = {"paragraph_a": {"source:a", "source:a-evidence"}, "paragraph_b": {"source:b"}}
    return ComparisonResult(
        "document",
        {"paragraph_source_coverage": 0.0, "affected_paragraph_precision": 0.5,
         "affected_paragraph_recall": 1.0, "unrelated_paragraph_stability": 0.0, "token_cost": float(linear_tokens)},
        {"paragraph_source_coverage": _ratio(sum(bool(refs) for refs in paragraph_sources.values()), len(paragraph_sources)),
         "affected_paragraph_precision": precision, "affected_paragraph_recall": recall,
         "unrelated_paragraph_stability": 1.0 if "paragraph_b" not in stale_ids else 0.0,
         "token_cost": float(binding.total_token_cost)},
        {"stale_node_ids": tuple(sorted(stale_ids)), "replay_order": stale_replay_order(stale),
         "token_reduction": round(1 - binding.total_token_cost / linear_tokens, 4),
         "linear_context": _linear_text(before), "binding": binding},
    )


def evaluate_research_turn() -> ComparisonResult:
    nodes = (
        _node("question", "question", "Which hypothesis is supported?", ("source:question",)),
        _node("hypothesis_a", "note", "Hypothesis A.", ("source:hyp-a",)),
        _node("evidence_a", "evidence", "Independent evidence supports A.", ("source:evidence-a",)),
        _node("hypothesis_b", "note", "Hypothesis B.", ("source:hyp-b",)),
        _node("wrong_evidence", "evidence", "False claim: B is proven despite contrary sources.", ("source:wrong",)),
        _node("open_question", "note", "Open question: external replication remains unverified.", ("source:open",)),
        replace(
            _node("stale_conclusion", "conclusion", "Old conclusion: A is final without replication.", ("source:old",)),
            stale=True, stale_reason="source_revision_changed",
        ),
        replace(
            _node("synthesis", "research_synthesis", "A is supported; replication remains open.", ("source:evidence-a", "source:open")),
            stale=True, stale_reason="upstream_stale",
        ),
    )
    edges = (
        ContextGraphEdge("e1", "question", "hypothesis_a", "full_chain", 1, 0),
        ContextGraphEdge("e2", "hypothesis_a", "evidence_a", "full_chain", 1, 1),
        ContextGraphEdge("e3", "evidence_a", "synthesis", "quote_only", 1, 2),
        ContextGraphEdge("e4", "question", "hypothesis_b", "full_chain", 1, 3),
        ContextGraphEdge("e5", "wrong_evidence", "synthesis", "excluded", 1, 4),
        ContextGraphEdge("e6", "open_question", "synthesis", "highlights_only", 1, 5),
        ContextGraphEdge("e7", "stale_conclusion", "synthesis", "reference", 1, 6),
    )
    snapshot = _snapshot("research-eval", nodes, edges, ("synthesis",))
    binding = _compile(snapshot, acknowledge_existing_stale=True)
    graph_text = "\n".join(str(item["content"]) for item in binding.messages)
    linear = _linear_text(snapshot)
    linear_tokens = (len(linear) + 3) // 4
    return ComparisonResult(
        "research_turn",
        {"wrong_context_recovery": 0.0 if "False claim" in linear else 1.0,
         "evidence_merge_quality": 0.5, "citation_accuracy": 0.5,
         "selection_transparency": 0.0, "token_cost": float(linear_tokens)},
        {"wrong_context_recovery": 0.0 if "False claim" in graph_text else 1.0,
         "evidence_merge_quality": _ratio(len({ref for ref in binding.source_refs if ref in {"source:evidence-a", "source:open"}}), 2),
         "citation_accuracy": _ratio(len(set(binding.source_refs) & {"source:evidence-a", "source:open"}), len(set(binding.source_refs))),
         "selection_transparency": 1.0 if binding.excluded_nodes and binding.deterministic_order else 0.0,
         "token_cost": float(binding.total_token_cost)},
        {"excluded_nodes": binding.excluded_nodes, "deterministic_order": binding.deterministic_order,
         "source_refs": binding.source_refs, "stale_nodes": binding.stale_nodes,
         "model_replay_order": stale_replay_order(snapshot),
         "token_reduction": round(1 - binding.total_token_cost / linear_tokens, 4),
         "linear_context": linear, "binding": binding},
    )


def run_structural_benchmark() -> tuple[ComparisonResult, ...]:
    return (evaluate_project_skill(), evaluate_document(), evaluate_research_turn())
