from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from core.context_graph import (
    ContextGraphEdge,
    ContextGraphNode,
    ContextGraphSnapshot,
    ContextGraphValidationError,
    ContextProvenance,
    ImportLimits,
    IntegrityIssue,
    snapshot_from_dict,
    validate_snapshot,
)


def _node(**changes: object) -> ContextGraphNode:
    values: dict[str, object] = {
        "node_id": "n1", "node_type": "note", "title": "Note", "content_ref": "content:n1",
        "content_revision": "r1", "source_refs": ("source:n1",), "trust": "untrusted",
        "created_at": "2026-08-30T00:00:00Z", "updated_at": "2026-08-30T00:00:00Z",
        "metadata": {"project_id": "project", "content": "body"},
    }
    values.update(changes)
    return ContextGraphNode(**values)  # type: ignore[arg-type]


def _snapshot(**changes: object) -> ContextGraphSnapshot:
    values: dict[str, object] = {
        "schema_version": "1.0.0", "graph_id": "graph", "graph_revision": "graph-r1",
        "project_id": "project", "source_type": "fixture", "source_revision": "source-r1",
        "created_at": "2026-08-30T00:00:00Z", "nodes": (_node(),), "edges": (),
        "selected_outputs": ("n1",), "token_estimate": 1,
        "provenance": ContextProvenance("fixture", "source-r1", "2026-08-30T00:00:00Z", "fixture", "r1", "file:fixture.json"),
        "integrity_issues": (),
    }
    values.update(changes)
    return ContextGraphSnapshot(**values)  # type: ignore[arg-type]


def _payload() -> dict[str, object]:
    return {
        "schema_version": "1.0.0", "graph_id": "graph", "graph_revision": "graph-r1",
        "project_id": "project", "source_type": "fixture", "source_revision": "source-r1",
        "created_at": "2026-08-30T00:00:00Z", "nodes": [{
            "node_id": "n1", "node_type": "note", "title": "Note", "content_ref": "content:n1",
            "content_revision": "r1", "source_refs": ["source:n1"], "trust": "untrusted",
            "created_at": "2026-08-30T00:00:00Z", "updated_at": "2026-08-30T00:00:00Z",
            "stale": False, "stale_reason": None, "metadata": {"project_id": "project", "content": "body"},
        }], "edges": [], "selected_outputs": ["n1"], "token_estimate": 1,
        "provenance": {"source_type": "fixture", "source_revision": "source-r1", "imported_at": "2026-08-30T00:00:00Z", "importer_id": "fixture", "importer_revision": "r1", "source_ref": "file:fixture.json", "untrusted_external_text": True},
        "integrity_issues": [],
    }


@pytest.mark.parametrize(
    ("snapshot", "issue"),
    [
        (_snapshot(token_estimate=-1), "invalid_token_estimate"),
        (_snapshot(nodes=(_node(title="x" * 513),)), "node_title_length_limit"),
        (_snapshot(nodes=(_node(stale=1),)), "invalid_node_stale"),
        (_snapshot(nodes=(_node(stale=True, stale_reason=None),)), "missing_node_stale_reason"),
        (_snapshot(nodes=(_node(stale=False, stale_reason="drift"),)), "unexpected_node_stale_reason"),
        (_snapshot(nodes=(_node(metadata={"project_id": "project", "content": "body", "highlights": "not-a-list"}),)), "invalid_node_highlights"),
        (_snapshot(nodes=(_node(metadata={"project_id": "project", "content": "body", "archived": "yes"}),)), "invalid_node_archived"),
        (_snapshot(nodes=(_node(source_refs=("not-a-ref",)),)), "invalid_source_ref"),
        (_snapshot(edges=(ContextGraphEdge("e1", "n1", "n1", "full_chain", 0, -1, True, {}),)), "invalid_edge_ordering"),
        (_snapshot(edges=(ContextGraphEdge("e1", "n1", "n1", "full_chain", 0, 0, 1, {}),)), "invalid_edge_active"),
        (_snapshot(integrity_issues=(IntegrityIssue("code", "message", "notice"),)), "invalid_integrity_issue_severity"),
        (_snapshot(provenance=ContextProvenance("other", "source-r1", "at", "importer", "r1", "file:fixture.json")), "provenance_source_type_mismatch"),
        (_snapshot(provenance=ContextProvenance("fixture", "source-r1", "at", "importer", "r1", "file:fixture.json", False)), "provenance_external_text_must_be_untrusted"),
    ],
)
def test_programmatic_snapshot_contract_fails_closed(snapshot: ContextGraphSnapshot, issue: str) -> None:
    with pytest.raises(ContextGraphValidationError) as raised:
        validate_snapshot(snapshot)
    assert any(reported == issue or reported.startswith(f"{issue}:") for reported in raised.value.issues)


def test_programmatic_content_and_metadata_limits_apply_without_an_importer() -> None:
    too_deep: dict[str, object] = {"project_id": "project", "content": "body"}
    cursor = too_deep
    for _ in range(33):
        child: dict[str, object] = {}
        cursor["child"] = child
        cursor = child
    graph = _snapshot(nodes=(_node(metadata=too_deep),))
    with pytest.raises(ContextGraphValidationError, match="invalid_metadata:node:n1"):
        validate_snapshot(graph)

    graph = _snapshot(nodes=(_node(metadata={"project_id": "project", "content": "€€"}),))
    with pytest.raises(ContextGraphValidationError, match="total_content_limit_exceeded"):
        validate_snapshot(graph, limits=ImportLimits(max_total_content_chars=5))


@pytest.mark.parametrize(
    ("field", "value"),
    [("graph_id", 1), ("token_estimate", "1")],
)
def test_snapshot_from_dict_does_not_coerce_root_scalars(field: str, value: object) -> None:
    payload = _payload()
    payload[field] = value
    with pytest.raises(ContextGraphValidationError, match="invalid_snapshot_scalar_type"):
        snapshot_from_dict(payload)


def test_snapshot_from_dict_rejects_boolean_integer_and_bad_integrity_issue_shape() -> None:
    payload = _payload()
    payload["edges"] = [{"edge_id": "e1", "source_node_id": "n1", "target_node_id": "n1", "context_mode": "full_chain", "depth": True, "ordering": 0, "active": True, "metadata": {}}]
    with pytest.raises(ContextGraphValidationError, match="invalid_snapshot_edge"):
        snapshot_from_dict(payload)

    payload = _payload()
    payload["integrity_issues"] = [{"code": "x", "message": "m", "severity": "warning"}]
    with pytest.raises(ContextGraphValidationError, match="invalid_snapshot_issue"):
        snapshot_from_dict(payload)


def test_schema_tracks_runtime_ref_and_integrity_contract() -> None:
    root = Path(__file__).resolve().parents[3]
    schema = json.loads((root / "core-contracts/rebuild/schemas/context-graph-snapshot.schema.json").read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    validator.validate(_payload())

    invalid = _payload()
    invalid["nodes"][0]["source_refs"] = ["missing-scheme"]  # type: ignore[index]
    assert list(validator.iter_errors(invalid))
