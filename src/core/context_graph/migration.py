from __future__ import annotations

from typing import Mapping

from .models import ContextGraphEdge, ContextGraphNode, ContextGraphSnapshot, ContextProvenance, IntegrityIssue
from .validation import ContextGraphValidationError, validate_snapshot

_ROOT_FIELDS = {"schema_version", "graph_id", "graph_revision", "project_id", "source_type", "source_revision", "created_at", "nodes", "edges", "selected_outputs", "token_estimate", "provenance", "integrity_issues"}
_NODE_FIELDS = {"node_id", "node_type", "title", "content_ref", "content_revision", "source_refs", "trust", "created_at", "updated_at", "stale", "stale_reason", "metadata"}
_EDGE_FIELDS = {"edge_id", "source_node_id", "target_node_id", "context_mode", "depth", "ordering", "active", "metadata"}
_PROVENANCE_FIELDS = {"source_type", "source_revision", "imported_at", "importer_id", "importer_revision", "source_ref", "untrusted_external_text"}


def snapshot_from_dict(payload: Mapping[str, object]) -> ContextGraphSnapshot:
    if not isinstance(payload, Mapping):
        raise ContextGraphValidationError(("invalid_snapshot_payload",))
    version = payload.get("schema_version")
    if version == "0.9.0":
        payload = _migrate_v0_9(payload)
    if payload.get("schema_version") != "1.0.0" or set(payload) != _ROOT_FIELDS:
        raise ContextGraphValidationError(("unsupported_or_unknown_snapshot_fields",))
    nodes_raw, edges_raw, provenance_raw = payload["nodes"], payload["edges"], payload["provenance"]
    if not isinstance(nodes_raw, list) or not isinstance(edges_raw, list) or not isinstance(provenance_raw, Mapping):
        raise ContextGraphValidationError(("invalid_snapshot_shape",))
    if not _is_string_fields(payload, ("schema_version", "graph_id", "graph_revision", "project_id", "source_type", "source_revision", "created_at")):
        raise ContextGraphValidationError(("invalid_snapshot_scalar_type",))
    if type(payload["token_estimate"]) is not int or type(payload["selected_outputs"]) is not list:
        raise ContextGraphValidationError(("invalid_snapshot_scalar_type",))
    if any(not isinstance(item, str) for item in payload["selected_outputs"]):
        raise ContextGraphValidationError(("invalid_selected_outputs",))
    nodes = []
    for item in nodes_raw:
        if not isinstance(item, Mapping) or set(item) != _NODE_FIELDS or not isinstance(item["metadata"], Mapping):
            raise ContextGraphValidationError(("invalid_snapshot_node",))
        if not _is_string_fields(item, ("node_id", "node_type", "title", "content_ref", "content_revision", "trust", "created_at", "updated_at")):
            raise ContextGraphValidationError(("invalid_snapshot_node",))
        if type(item["source_refs"]) is not list or any(not isinstance(ref, str) for ref in item["source_refs"]):
            raise ContextGraphValidationError(("invalid_snapshot_node",))
        if type(item["stale"]) is not bool or (item["stale_reason"] is not None and not isinstance(item["stale_reason"], str)):
            raise ContextGraphValidationError(("invalid_snapshot_node",))
        nodes.append(ContextGraphNode(**{**item, "source_refs": tuple(item["source_refs"])}))
    edges = []
    for item in edges_raw:
        if not isinstance(item, Mapping) or set(item) != _EDGE_FIELDS or not isinstance(item["metadata"], Mapping):
            raise ContextGraphValidationError(("invalid_snapshot_edge",))
        if not _is_string_fields(item, ("edge_id", "source_node_id", "target_node_id", "context_mode")):
            raise ContextGraphValidationError(("invalid_snapshot_edge",))
        if type(item["depth"]) is not int or type(item["ordering"]) is not int or type(item["active"]) is not bool:
            raise ContextGraphValidationError(("invalid_snapshot_edge",))
        edges.append(ContextGraphEdge(**item))
    if set(provenance_raw) != _PROVENANCE_FIELDS or not _is_string_fields(provenance_raw, ("source_type", "source_revision", "imported_at", "importer_id", "importer_revision", "source_ref")) or provenance_raw["untrusted_external_text"] is not True:
        raise ContextGraphValidationError(("invalid_snapshot_provenance",))
    issues_raw = payload["integrity_issues"]
    if not isinstance(issues_raw, list):
        raise ContextGraphValidationError(("invalid_snapshot_issues",))
    issues: list[IntegrityIssue] = []
    for item in issues_raw:
        if not isinstance(item, Mapping) or set(item) != {"code", "message", "severity", "object_ref"}:
            raise ContextGraphValidationError(("invalid_snapshot_issue",))
        if not isinstance(item["code"], str) or not isinstance(item["message"], str) or not isinstance(item["severity"], str) or (item["object_ref"] is not None and not isinstance(item["object_ref"], str)):
            raise ContextGraphValidationError(("invalid_snapshot_issue",))
        issues.append(IntegrityIssue(**item))
    snapshot = ContextGraphSnapshot(
        schema_version=payload["schema_version"], graph_id=payload["graph_id"],
        graph_revision=payload["graph_revision"], project_id=payload["project_id"],
        source_type=payload["source_type"], source_revision=payload["source_revision"],
        created_at=payload["created_at"], nodes=tuple(nodes), edges=tuple(edges),
        selected_outputs=tuple(payload["selected_outputs"]), token_estimate=payload["token_estimate"],
        provenance=ContextProvenance(**provenance_raw),
        integrity_issues=tuple(issues),
    )
    validate_snapshot(snapshot)
    return snapshot


def _is_string_fields(payload: Mapping[str, object], names: tuple[str, ...]) -> bool:
    return all(isinstance(payload.get(name), str) for name in names)


def _migrate_v0_9(payload: Mapping[str, object]) -> dict[str, object]:
    allowed = _ROOT_FIELDS - {"integrity_issues"}
    if set(payload) - allowed:
        raise ContextGraphValidationError(("unknown_legacy_snapshot_field",))
    nodes = []
    for item in payload.get("nodes", []):
        if not isinstance(item, Mapping) or set(item) - (_NODE_FIELDS - {"stale", "stale_reason"}):
            raise ContextGraphValidationError(("invalid_legacy_snapshot_node",))
        nodes.append({**item, "stale": False, "stale_reason": None})
    provenance = dict(payload.get("provenance", {}))
    provenance.setdefault("untrusted_external_text", True)
    return {**payload, "schema_version": "1.0.0", "nodes": nodes, "provenance": provenance,
            "integrity_issues": [{"code": "migrated_from_0_9", "message": "Snapshot migrated without executing external content.", "severity": "warning", "object_ref": None}]}
