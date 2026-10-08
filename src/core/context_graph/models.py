from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Mapping

NodeType = Literal[
    "question", "answer", "material", "evidence", "note", "conclusion",
    "decision", "rejected_option", "document_draft",
    "project_skill_proposal", "memory_proposal", "research_synthesis",
]
ContextMode = Literal[
    "full_chain", "quote_only", "highlights_only", "reference", "excluded"
]


@dataclass(frozen=True, slots=True)
class IntegrityIssue:
    code: str
    message: str
    severity: Literal["warning", "error"] = "warning"
    object_ref: str | None = None


@dataclass(frozen=True, slots=True)
class ContextProvenance:
    source_type: str
    source_revision: str
    imported_at: str
    importer_id: str
    importer_revision: str
    source_ref: str
    untrusted_external_text: bool = True


@dataclass(frozen=True, slots=True)
class ContextGraphNode:
    node_id: str
    node_type: NodeType
    title: str
    content_ref: str
    content_revision: str
    source_refs: tuple[str, ...]
    trust: Literal["untrusted", "user_authored", "verified"]
    created_at: str
    updated_at: str
    stale: bool = False
    stale_reason: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ContextGraphEdge:
    edge_id: str
    source_node_id: str
    target_node_id: str
    context_mode: ContextMode
    depth: int
    ordering: int
    active: bool = True
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ContextGraphSnapshot:
    schema_version: str
    graph_id: str
    graph_revision: str
    project_id: str
    source_type: str
    source_revision: str
    created_at: str
    nodes: tuple[ContextGraphNode, ...]
    edges: tuple[ContextGraphEdge, ...]
    selected_outputs: tuple[str, ...]
    token_estimate: int
    provenance: ContextProvenance
    integrity_issues: tuple[IntegrityIssue, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ContextBinding:
    schema_version: str
    graph_id: str
    graph_revision: str
    capability_revision: str
    compiler_revision: str
    boundary_revision: str
    provider_revision: str
    model_route_revision: str
    messages: tuple[Mapping[str, Any], ...]
    layers: Mapping[str, tuple[Mapping[str, Any], ...]]
    layer_token_costs: Mapping[str, int]
    total_token_cost: int
    trimmed_nodes: tuple[Mapping[str, Any], ...]
    excluded_nodes: tuple[str, ...]
    stale_nodes: tuple[str, ...]
    source_refs: tuple[str, ...]
    deterministic_order: tuple[str, ...]
    budget_explanation: Mapping[str, Any]
