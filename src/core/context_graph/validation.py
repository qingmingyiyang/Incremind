from __future__ import annotations

from collections import defaultdict, deque
import json
import math
import re
from typing import Iterable, Mapping

from .models import (
    ContextGraphEdge,
    ContextGraphNode,
    ContextGraphSnapshot,
    ContextProvenance,
    IntegrityIssue,
)
from .protocols import ImportLimits

NODE_TYPES = frozenset({
    "question", "answer", "material", "evidence", "note", "conclusion",
    "decision", "rejected_option", "document_draft", "project_skill_proposal",
    "memory_proposal", "research_synthesis",
})
TRUST_LEVELS = frozenset({"untrusted", "user_authored", "verified"})
CONTEXT_MODES = frozenset({"full_chain", "quote_only", "highlights_only", "reference", "excluded"})

_MAX_ID_LENGTH = 256
_MAX_PROJECT_ID_LENGTH = 128
_MAX_SOURCE_TYPE_LENGTH = 128
_MAX_TITLE_LENGTH = 512
_MAX_TEXT_REF_LENGTH = 4_096
_MAX_TIMESTAMP_LENGTH = 128
_MAX_STALE_REASON_LENGTH = 4_096
_MAX_ISSUE_CODE_LENGTH = 128
_MAX_ISSUE_MESSAGE_LENGTH = 4_096
_MAX_METADATA_DEPTH = 32
# Metadata may preserve external format details, but it cannot become an
# unbounded second content store.  The aggregate limit is independently
# constrained by ImportLimits.max_total_content_chars below.
_MAX_NODE_METADATA_BYTES = 256 * 1024
_OPAQUE_REF = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]{0,63}:[^\s\x00-\x1f]+$")


class ContextGraphValidationError(ValueError):
    def __init__(self, issues: Iterable[str]) -> None:
        self.issues = tuple(issues)
        super().__init__("; ".join(self.issues))


def validate_snapshot(snapshot: ContextGraphSnapshot, *, limits: ImportLimits = ImportLimits()) -> None:
    issues: list[str] = []
    if not isinstance(snapshot, ContextGraphSnapshot):
        raise ContextGraphValidationError(("invalid_snapshot_type",))
    if type(snapshot.nodes) is not tuple:
        issues.append("invalid_nodes_container")
    if type(snapshot.edges) is not tuple:
        issues.append("invalid_edges_container")
    if type(snapshot.selected_outputs) is not tuple:
        issues.append("invalid_selected_outputs_container")
    if type(snapshot.integrity_issues) is not tuple:
        issues.append("invalid_integrity_issues_container")
    if not isinstance(snapshot.provenance, ContextProvenance):
        issues.append("invalid_provenance")
    if type(snapshot.token_estimate) is not int or snapshot.token_estimate < 0:
        issues.append("invalid_token_estimate")
    if snapshot.schema_version != "1.0.0":
        issues.append("unsupported_schema_version")
    for name, value, maximum in (
        ("graph_id", snapshot.graph_id, _MAX_ID_LENGTH), ("graph_revision", snapshot.graph_revision, _MAX_ID_LENGTH),
        ("project_id", snapshot.project_id, _MAX_PROJECT_ID_LENGTH), ("source_type", snapshot.source_type, _MAX_SOURCE_TYPE_LENGTH),
        ("source_revision", snapshot.source_revision, _MAX_ID_LENGTH), ("created_at", snapshot.created_at, _MAX_TIMESTAMP_LENGTH),
    ):
        if not isinstance(value, str) or not value.strip():
            issues.append(f"missing_{name}")
        elif len(value) > maximum:
            issues.append(f"{name}_length_limit")
    if len(snapshot.nodes) > limits.max_nodes:
        issues.append("node_limit_exceeded")
    if len(snapshot.edges) > limits.max_edges:
        issues.append("edge_limit_exceeded")
    node_ids = [node.node_id for node in snapshot.nodes if isinstance(node, ContextGraphNode)]
    edge_ids = [edge.edge_id for edge in snapshot.edges if isinstance(edge, ContextGraphEdge)]
    if len(node_ids) != len(snapshot.nodes):
        issues.append("invalid_node_shape")
    if len(edge_ids) != len(snapshot.edges):
        issues.append("invalid_edge_shape")
    valid_node_ids = [node_id for node_id in node_ids if isinstance(node_id, str) and node_id.strip() and len(node_id) <= _MAX_ID_LENGTH]
    valid_edge_ids = [edge_id for edge_id in edge_ids if isinstance(edge_id, str) and edge_id.strip() and len(edge_id) <= _MAX_ID_LENGTH]
    if len(valid_node_ids) != len(node_ids):
        issues.append("missing_node_id")
    if len(valid_edge_ids) != len(edge_ids):
        issues.append("missing_edge_id")
    if len(valid_node_ids) != len(set(valid_node_ids)):
        issues.append("duplicate_node_id")
    if len(valid_edge_ids) != len(set(valid_edge_ids)):
        issues.append("duplicate_edge_id")
    known = set(valid_node_ids)
    adjacency: dict[str, list[str]] = defaultdict(list)
    for edge in snapshot.edges:
        if not isinstance(edge, ContextGraphEdge):
            continue
        if not isinstance(edge.source_node_id, str) or not edge.source_node_id.strip() or len(edge.source_node_id) > _MAX_ID_LENGTH:
            issues.append(f"missing_edge_source:{edge.edge_id}")
        if not isinstance(edge.target_node_id, str) or not edge.target_node_id.strip() or len(edge.target_node_id) > _MAX_ID_LENGTH:
            issues.append(f"missing_edge_target:{edge.edge_id}")
        if not isinstance(edge.context_mode, str) or edge.context_mode not in CONTEXT_MODES:
            issues.append(f"invalid_context_mode:{edge.edge_id}")
        if type(edge.depth) is not int or edge.depth < 0 or edge.depth > limits.max_depth:
            issues.append(f"edge_depth_limit:{edge.edge_id}")
        if type(edge.ordering) is not int or edge.ordering < 0:
            issues.append(f"invalid_edge_ordering:{edge.edge_id}")
        if type(edge.active) is not bool:
            issues.append(f"invalid_edge_active:{edge.edge_id}")
        _validate_metadata(edge.metadata, f"edge:{edge.edge_id}", issues, limits)
        if not isinstance(edge.source_node_id, str) or not isinstance(edge.target_node_id, str) or edge.source_node_id not in known or edge.target_node_id not in known:
            issues.append(f"invalid_edge_reference:{edge.edge_id}")
        # Exclusion controls Context walking, not structural validity.  A cycle
        # hidden behind an excluded or inactive edge would become unsafe when a
        # user later reactivates that edge, so every valid edge participates.
        if isinstance(edge.source_node_id, str) and isinstance(edge.target_node_id, str) and edge.source_node_id in known and edge.target_node_id in known:
            adjacency[edge.source_node_id].append(edge.target_node_id)
    indegree = {node_id: 0 for node_id in known}
    for source in adjacency:
        for target in adjacency[source]:
            if target in indegree:
                indegree[target] += 1
    ready = deque(sorted(node_id for node_id, degree in indegree.items() if degree == 0))
    longest = {node_id: 0 for node_id in known}
    visited_count = 0
    while ready:
        node_id = ready.popleft()
        visited_count += 1
        for target in sorted(adjacency[node_id]):
            longest[target] = max(longest[target], longest[node_id] + 1)
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)
    if visited_count != len(known):
        issues.append("cycle_detected")
    if longest and max(longest.values()) > limits.max_depth:
        issues.append("graph_depth_limit_exceeded")
    valid_selected_outputs = [node_id for node_id in snapshot.selected_outputs if isinstance(node_id, str) and node_id.strip() and len(node_id) <= _MAX_ID_LENGTH]
    if len(valid_selected_outputs) != len(snapshot.selected_outputs):
        issues.append("missing_selected_output_id")
    if len(valid_selected_outputs) != len(set(valid_selected_outputs)):
        issues.append("duplicate_selected_output")
    if not set(valid_selected_outputs).issubset(known):
        issues.append("invalid_selected_output")
    total_content_bytes = 0
    total_metadata_bytes = 0
    for node in snapshot.nodes:
        if not isinstance(node, ContextGraphNode):
            continue
        if not isinstance(node.node_type, str) or node.node_type not in NODE_TYPES:
            issues.append(f"invalid_node_type:{node.node_id}")
        if not isinstance(node.trust, str) or node.trust not in TRUST_LEVELS:
            issues.append(f"invalid_node_trust:{node.node_id}")
        if not isinstance(node.content_ref, str) or not node.content_ref.strip():
            issues.append(f"missing_content_ref:{node.node_id}")
        elif len(node.content_ref) > _MAX_TEXT_REF_LENGTH:
            issues.append(f"content_ref_length_limit:{node.node_id}")
        if not isinstance(node.content_revision, str) or not node.content_revision.strip():
            issues.append(f"missing_content_revision:{node.node_id}")
        elif len(node.content_revision) > _MAX_ID_LENGTH:
            issues.append(f"content_revision_length_limit:{node.node_id}")
        if not isinstance(node.source_refs, tuple) or not node.source_refs:
            issues.append(f"incomplete_node_provenance:{node.node_id}")
        elif len(node.source_refs) > 256:
            issues.append(f"source_ref_limit:{node.node_id}")
        elif any(not _is_opaque_ref(source_ref) for source_ref in node.source_refs):
            issues.append(f"invalid_source_ref:{node.node_id}")
        elif len(node.source_refs) != len(set(node.source_refs)):
            issues.append(f"duplicate_source_ref:{node.node_id}")
        if not isinstance(node.created_at, str) or not node.created_at.strip():
            issues.append(f"missing_node_created_at:{node.node_id}")
        elif len(node.created_at) > _MAX_TIMESTAMP_LENGTH:
            issues.append(f"node_created_at_length_limit:{node.node_id}")
        if not isinstance(node.updated_at, str) or not node.updated_at.strip():
            issues.append(f"missing_node_updated_at:{node.node_id}")
        elif len(node.updated_at) > _MAX_TIMESTAMP_LENGTH:
            issues.append(f"node_updated_at_length_limit:{node.node_id}")
        if not isinstance(node.title, str):
            issues.append(f"invalid_node_title:{node.node_id}")
        elif len(node.title) > _MAX_TITLE_LENGTH:
            issues.append(f"node_title_length_limit:{node.node_id}")
        if type(node.stale) is not bool:
            issues.append(f"invalid_node_stale:{node.node_id}")
        if node.stale_reason is not None and (not isinstance(node.stale_reason, str) or not node.stale_reason.strip() or len(node.stale_reason) > _MAX_STALE_REASON_LENGTH):
            issues.append(f"invalid_node_stale_reason:{node.node_id}")
        if node.stale and node.stale_reason is None:
            issues.append(f"missing_node_stale_reason:{node.node_id}")
        if not node.stale and node.stale_reason is not None:
            issues.append(f"unexpected_node_stale_reason:{node.node_id}")
        metadata_bytes = _validate_metadata(node.metadata, f"node:{node.node_id}", issues, limits)
        total_metadata_bytes += metadata_bytes
        content = node.metadata.get("content") if isinstance(node.metadata, Mapping) else None
        if content is not None:
            if not isinstance(content, str):
                issues.append(f"invalid_node_content:{node.node_id}")
            else:
                content_bytes = _utf8_size(content, f"node_content:{node.node_id}", issues)
                total_content_bytes += content_bytes
                if content_bytes > limits.max_node_content_chars:
                    issues.append(f"node_content_limit_exceeded:{node.node_id}")
        highlights = node.metadata.get("highlights") if isinstance(node.metadata, Mapping) else None
        if highlights is not None and (
            not isinstance(highlights, (list, tuple))
            or any(not isinstance(item, str) for item in highlights)
        ):
            issues.append(f"invalid_node_highlights:{node.node_id}")
        archived = node.metadata.get("archived") if isinstance(node.metadata, Mapping) else None
        if archived is not None and type(archived) is not bool:
            issues.append(f"invalid_node_archived:{node.node_id}")
        if isinstance(node.metadata, Mapping) and node.metadata.get("project_id", snapshot.project_id) != snapshot.project_id:
            issues.append(f"project_scope_violation:{node.node_id}")
    if total_content_bytes > limits.max_total_content_chars:
        issues.append("total_content_limit_exceeded")
    if total_metadata_bytes > limits.max_total_content_chars:
        issues.append("total_metadata_limit_exceeded")
    provenance = snapshot.provenance
    if isinstance(provenance, ContextProvenance):
        for name, value, maximum in (
            ("source_type", provenance.source_type, _MAX_SOURCE_TYPE_LENGTH), ("source_revision", provenance.source_revision, _MAX_ID_LENGTH),
            ("imported_at", provenance.imported_at, _MAX_TIMESTAMP_LENGTH), ("importer_id", provenance.importer_id, _MAX_ID_LENGTH),
            ("importer_revision", provenance.importer_revision, _MAX_ID_LENGTH), ("source_ref", provenance.source_ref, _MAX_TEXT_REF_LENGTH),
        ):
            if not isinstance(value, str) or not value.strip():
                issues.append(f"missing_provenance_{name}")
            elif len(value) > maximum:
                issues.append(f"provenance_{name}_length_limit")
        if provenance.untrusted_external_text is not True:
            issues.append("provenance_external_text_must_be_untrusted")
        if provenance.source_type != snapshot.source_type:
            issues.append("provenance_source_type_mismatch")
        if provenance.source_revision != snapshot.source_revision:
            issues.append("provenance_source_revision_mismatch")
    for issue in snapshot.integrity_issues:
        _validate_integrity_issue(issue, issues)
    if issues:
        raise ContextGraphValidationError(dict.fromkeys(issues))


def _is_opaque_ref(value: object) -> bool:
    return isinstance(value, str) and len(value) <= _MAX_TEXT_REF_LENGTH and bool(_OPAQUE_REF.fullmatch(value))


def _utf8_size(value: str, label: str, issues: list[str]) -> int:
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError:
        issues.append(f"invalid_utf8:{label}")
        return 0


def _validate_metadata(metadata: object, object_ref: str, issues: list[str], limits: ImportLimits) -> int:
    if not isinstance(metadata, Mapping):
        issues.append(f"invalid_metadata:{object_ref}")
        return 0
    if len(metadata) > 128:
        issues.append(f"metadata_property_limit:{object_ref}")
    max_depth = min(limits.max_depth, _MAX_METADATA_DEPTH)
    try:
        _validate_json_value(metadata, depth=1, max_depth=max_depth, ancestor_ids=set())
        encoded = json.dumps(metadata, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError):
        issues.append(f"invalid_metadata:{object_ref}")
        return 0
    if len(encoded) > _MAX_NODE_METADATA_BYTES:
        issues.append(f"metadata_size_limit:{object_ref}")
    return len(encoded)


def _validate_json_value(value: object, *, depth: int, max_depth: int, ancestor_ids: set[int]) -> None:
    if depth > max_depth:
        raise ValueError("metadata depth")
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("non-finite metadata number")
        return
    if isinstance(value, Mapping):
        marker = id(value)
        if marker in ancestor_ids or any(not isinstance(key, str) for key in value):
            raise ValueError("invalid metadata mapping")
        ancestor_ids.add(marker)
        try:
            for child in value.values():
                _validate_json_value(child, depth=depth + 1, max_depth=max_depth, ancestor_ids=ancestor_ids)
        finally:
            ancestor_ids.remove(marker)
        return
    if isinstance(value, (list, tuple)):
        marker = id(value)
        if marker in ancestor_ids:
            raise ValueError("metadata cycle")
        ancestor_ids.add(marker)
        try:
            for child in value:
                _validate_json_value(child, depth=depth + 1, max_depth=max_depth, ancestor_ids=ancestor_ids)
        finally:
            ancestor_ids.remove(marker)
        return
    raise TypeError("unsupported metadata type")


def _validate_integrity_issue(issue: object, issues: list[str]) -> None:
    if not isinstance(issue, IntegrityIssue):
        issues.append("invalid_integrity_issue")
        return
    if not isinstance(issue.code, str) or not issue.code.strip() or len(issue.code) > _MAX_ISSUE_CODE_LENGTH:
        issues.append("invalid_integrity_issue_code")
    if not isinstance(issue.message, str) or not issue.message.strip() or len(issue.message) > _MAX_ISSUE_MESSAGE_LENGTH:
        issues.append("invalid_integrity_issue_message")
    if not isinstance(issue.severity, str) or issue.severity not in {"warning", "error"}:
        issues.append("invalid_integrity_issue_severity")
    if issue.object_ref is not None and (not isinstance(issue.object_ref, str) or not issue.object_ref.strip() or len(issue.object_ref) > _MAX_TEXT_REF_LENGTH):
        issues.append("invalid_integrity_issue_object_ref")
