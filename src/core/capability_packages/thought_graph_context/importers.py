from __future__ import annotations

import base64
import json
import re
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from ...context_graph import snapshot_from_dict
from ...context_graph.models import (
    ContextGraphEdge, ContextGraphNode, ContextGraphSnapshot, ContextProvenance,
    IntegrityIssue,
)
from ...context_graph.protocols import (
    AuthorizedContextFile,
    AuthorizedContextFileError,
    ImportLimits,
)
from ...context_graph.validation import ContextGraphValidationError, validate_snapshot
from .external_safety import reject_external_json_value, reject_external_secret_material
from .thoughtdag_wire import thoughtdag_canvas_edges, thoughtdag_canvas_nodes


_MARKDOWN_ENVELOPE = re.compile(r"^<!--\s*linemap-context-graph-v1:([A-Za-z0-9_-]+)\s*-->\s*$")


class _ExternalBudget:
    """Counts all external text, including titles, references and metadata."""
    def __init__(self, limits: ImportLimits) -> None:
        self.limits = limits
        self.total_chars = 0

    def inspect(self, value: object, label: str, depth: int = 0) -> None:
        # JSON object nesting is distinct from graph dependency depth.  Keep a
        # hard external bound without rejecting an ordinary node envelope when
        # a caller deliberately sets a small graph-depth limit.
        if depth > min(max(self.limits.max_depth + 16, 32), 64):
            raise ContextGraphValidationError(("external_nesting_limit_exceeded",))
        if isinstance(value, str):
            self.total_chars += len(value)
            if self.total_chars > self.limits.max_total_content_chars:
                raise ContextGraphValidationError(("total_content_limit_exceeded",))
        elif isinstance(value, Mapping):
            for key, item in value.items():
                if not isinstance(key, str):
                    raise ContextGraphValidationError((f"invalid_{label}_key",))
                self.inspect(key, label, depth + 1)
                self.inspect(item, label, depth + 1)
        elif isinstance(value, (list, tuple)):
            for item in value:
                self.inspect(item, label, depth + 1)
        elif value is not None and not isinstance(value, (bool, int, float)):
            raise ContextGraphValidationError((f"invalid_{label}_value",))

    def inspect_node(self, value: object, label: str) -> None:
        before = self.total_chars
        self.inspect(value, label)
        if self.total_chars - before > self.limits.max_node_content_chars:
            raise ContextGraphValidationError(("node_content_limit_exceeded",))


def _strict_mapping(value: object, allowed: set[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContextGraphValidationError((f"invalid_{label}",))
    unknown = set(value) - allowed
    if unknown:
        issue_label = label.split(":", 1)[0]
        raise ContextGraphValidationError((f"unknown_{issue_label}_field:{sorted(unknown)[0]}",))
    return value


def _decode_metadata(value: object) -> object:
    if isinstance(value, Mapping):
        if "__linemap_type__" in value:
            if set(value) != {"__linemap_type__", "items"} or value["__linemap_type__"] != "tuple" or not isinstance(value["items"], list):
                raise ContextGraphValidationError(("invalid_metadata_encoding",))
            return tuple(_decode_metadata(item) for item in value["items"])
        return {key: _decode_metadata(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_decode_metadata(item) for item in value]
    return value


def _decode_snapshot_metadata(payload: object) -> object:
    if not isinstance(payload, Mapping):
        raise ContextGraphValidationError(("invalid_linemap_envelope",))
    decoded = dict(payload)
    for collection_name in ("nodes", "edges"):
        collection = decoded.get(collection_name)
        if not isinstance(collection, list):
            continue
        decoded_collection = []
        for item in collection:
            if not isinstance(item, Mapping):
                decoded_collection.append(item)
                continue
            copy = dict(item)
            if "metadata" in copy:
                copy["metadata"] = _decode_metadata(copy["metadata"])
            decoded_collection.append(copy)
        decoded[collection_name] = decoded_collection
    return decoded


def _inspect_embedded_snapshot_payload(payload: object, limits: ImportLimits) -> None:
    budget = _ExternalBudget(limits)
    budget.inspect(payload, "linemap_snapshot")
    if not isinstance(payload, Mapping):
        raise ContextGraphValidationError(("invalid_linemap_envelope",))
    nodes = payload.get("nodes")
    if not isinstance(nodes, list):
        raise ContextGraphValidationError(("invalid_linemap_envelope",))
    for index, node in enumerate(nodes):
        _ExternalBudget(limits).inspect_node(node, f"linemap_node:{index}")


def _bind_import_provenance(
    snapshot: ContextGraphSnapshot,
    *,
    project_id: str,
    source_type: str,
    source_revision: str,
    imported_at: str,
    importer_id: str,
    importer_revision: str,
    source_ref: str,
) -> ContextGraphSnapshot:
    if snapshot.project_id != project_id:
        raise ContextGraphValidationError(("project_scope_violation:embedded_snapshot",))
    issue = IntegrityIssue(
        "embedded_snapshot_imported",
        "The embedded platform snapshot was treated as untrusted data; this authorized file import is the current provenance authority.",
        object_ref=source_ref,
    )
    return replace(
        snapshot,
        source_type=source_type,
        source_revision=source_revision,
        provenance=ContextProvenance(
            source_type,
            source_revision,
            imported_at,
            importer_id,
            importer_revision,
            source_ref,
        ),
        integrity_issues=tuple(snapshot.integrity_issues) + (issue,),
    )


def _read_authorized(
    grant: AuthorizedContextFile,
    *,
    importer_id: str,
    limits: ImportLimits,
) -> tuple[Path, str]:
    """Read only the frozen path, project and revision in a platform grant."""
    try:
        path = grant.validate_for(project_id=grant.project_id, importer_id=importer_id)
    except AuthorizedContextFileError as exc:
        raise ContextGraphValidationError((str(exc),)) from exc
    if path.stat().st_size > limits.max_file_bytes:
        raise ContextGraphValidationError(("file_limit_exceeded",))
    raw = path.read_text(encoding="utf-8")
    try:
        # A replace between validation and read must never be imported under a
        # stale user grant.  ``validate_for`` repeats the revision comparison.
        path = grant.validate_for(project_id=grant.project_id, importer_id=importer_id)
    except AuthorizedContextFileError as exc:
        raise ContextGraphValidationError((str(exc),)) from exc
    reject_external_secret_material(raw, secret_canaries=limits.secret_canaries)
    return path, raw


def _require_context_file_grant(
    *,
    grant: AuthorizedContextFile | None,
    authorized_path: Path | None,
    project_id: str | None,
) -> AuthorizedContextFile:
    """Reject the pre-grant API instead of treating a Path as authorization."""
    if grant is None:
        raise ContextGraphValidationError(("authorized_context_file_grant_required",))
    if authorized_path is not None or project_id is not None:
        raise ContextGraphValidationError(("authorized_context_file_legacy_arguments_rejected",))
    return grant


def _text(value: object, maximum: int, label: str) -> str:
    if not isinstance(value, str):
        raise ContextGraphValidationError((f"invalid_{label}",))
    if len(value) > maximum:
        raise ContextGraphValidationError((f"{label}_limit_exceeded",))
    return value


def _required_text(value: object, maximum: int, label: str) -> str:
    text = _text(value, maximum, label)
    if not text.strip():
        raise ContextGraphValidationError((f"invalid_{label}",))
    return text


def _optional_text(value: object, maximum: int, label: str) -> None:
    if value is not None:
        _required_text(value, maximum, label)


def _iso(value: object, fallback: str) -> str:
    if isinstance(value, str) and value.strip():
        return value
    return fallback


def _file_revision(path: Path, prefix: str) -> str:
    stat = path.stat()
    return f"{prefix}@mtime-{stat.st_mtime_ns}:size-{stat.st_size}"


class ThoughtDAGImporter:
    importer_id = "thought_graph_context.ThoughtDAGImporter"
    importer_revision = "1.1.0"
    _ROOT_FIELDS = {"version", "name", "exportedAt", "instantiatedFrom", "sharedReadonly", "nodes", "edges", "events", "linemap"}
    _NODE_FIELDS = {
        "id", "type", "position", "width", "height", "initialWidth", "initialHeight",
        "measured", "zIndex", "dragHandle", "selected", "dragging", "resizing",
        "draggable", "selectable", "connectable", "deletable", "hidden", "parentId",
        "extent", "expandParent", "origin", "style", "className", "ariaLabel", "data",
    }
    _EDGE_FIELDS = {
        "id", "source", "target", "type", "animated", "selected", "sourceHandle",
        "targetHandle", "style", "className", "hidden", "deletable", "selectable",
        "reconnectable", "focusable", "interactionWidth", "label", "labelStyle",
        "labelShowBg", "labelBgStyle", "labelBgPadding", "labelBgBorderRadius",
        "markerStart", "markerEnd", "ariaLabel", "data", "zIndex",
    }
    _POSITION_FIELDS = {"x", "y"}
    _NODE_DATA_FIELDS = {
        "question", "response", "responses", "questions", "responseIndex",
        "isCollapsed", "isEditing", "isEditingResponse", "isLoading", "generationFailed",
        "references", "model", "webSearch", "scholarSearch", "autoRerun", "restreaming",
        "archived", "archivedAt", "lastContextHash", "lastGeneratedAt", "createdAt", "askedAt",
        "generatedAts", "editedAts", "stepKind", "linkUrl", "linkTitle", "linkFetchedAt",
        "linkSnapshotHtml", "frameColor", "frameCarry", "instruction", "fanoutRoles",
        "autoRerunRounds", "tokenCount", "branchContext", "highlights", "highlightMode",
        "summary", "summaries", "generatedBy", "gatewaySearches", "summaryTypes", "summaryTopics",
        "anchor", "digestOf", "condensedFrom", "reasoning", "reasonings", "rolePrompt",
        "appliedRole", "roleSourceNodeId", "roleMode", "attachments", "excludedAttachmentIds",
        "includedAttachmentIds", "isRoot", "isBranch", "isEvaluator", "evaluatorTrigger",
        "focusRole", "linemap",
    }
    _EDGE_DATA_FIELDS = {
        "isCrossLink", "isBranchFromSelection", "isWatch", "followsTip",
        "branchYRatio", "contextDepth", "createdAt", "focusRole", "linemap",
    }
    _REFERENCE_FIELDS = {"url", "title", "media", "date"}
    _HIGHLIGHT_FIELDS = {"id", "text", "at"}
    _FANOUT_ROLE_FIELDS = {"name", "prompt"}
    _ANCHOR_FIELDS = {"page", "rects", "attId"}
    def import_authorized_file(
        self,
        *,
        grant: AuthorizedContextFile | None = None,
        authorized_path: Path | None = None,
        project_id: str | None = None,
        limits: ImportLimits = ImportLimits(),
    ) -> ContextGraphSnapshot:
        grant = _require_context_file_grant(
            grant=grant, authorized_path=authorized_path, project_id=project_id
        )
        authorized_path, raw = _read_authorized(
            grant, importer_id=self.importer_id, limits=limits
        )
        project_id = grant.project_id
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ContextGraphValidationError(("invalid_json",)) from exc
        reject_external_json_value(payload, secret_canaries=limits.secret_canaries)
        root = _strict_mapping(payload, self._ROOT_FIELDS, "root")
        nodes_raw, edges_raw = root.get("nodes"), root.get("edges")
        if not isinstance(nodes_raw, list) or not isinstance(edges_raw, list):
            raise ContextGraphValidationError(("invalid_thoughtdag_schema",))
        if root.get("version") != 1:
            raise ContextGraphValidationError(("unsupported_thoughtdag_version",))
        _required_text(root.get("name"), 256, "graph_name")
        _required_text(root.get("exportedAt"), 128, "exported_at")
        if not isinstance(root.get("events"), list):
            raise ContextGraphValidationError(("invalid_events",))
        if "sharedReadonly" in root and not isinstance(root["sharedReadonly"], bool):
            raise ContextGraphValidationError(("invalid_shared_readonly",))
        instantiated_from = root.get("instantiatedFrom")
        if instantiated_from is not None:
            instantiated_from = _strict_mapping(
                instantiated_from,
                {"name", "at"},
                "instantiated_from",
            )
            _required_text(instantiated_from.get("name"), 256, "instantiated_from_name")
            _required_text(instantiated_from.get("at"), 128, "instantiated_from_at")
        if len(nodes_raw) > limits.max_nodes or len(edges_raw) > limits.max_edges:
            raise ContextGraphValidationError(("graph_limit_exceeded",))
        imported_at = datetime.now(timezone.utc).isoformat()
        exported_at = _iso(root.get("exportedAt"), datetime.fromtimestamp(authorized_path.stat().st_mtime, timezone.utc).isoformat())
        source_revision = (
            f"thoughtdag-v{root.get('version', 'unknown')}@{exported_at}:"
            f"{_file_revision(authorized_path, 'file')}"
        )
        budget = _ExternalBudget(limits)
        budget.inspect(root, "external_graph")
        for index, item in enumerate(nodes_raw):
            node = _strict_mapping(item, self._NODE_FIELDS, f"node:{index}")
            _ExternalBudget(limits).inspect_node(node, f"node:{index}")
            position = _strict_mapping(node.get("position"), self._POSITION_FIELDS, f"node_position:{index}")
            if any(
                not isinstance(position.get(axis), (int, float)) or isinstance(position.get(axis), bool)
                for axis in ("x", "y")
            ):
                raise ContextGraphValidationError((f"invalid_node_position:{index}",))
            data = _strict_mapping(node.get("data"), self._NODE_DATA_FIELDS, f"node_data:{index}")
            references, highlights = data.get("references", []), data.get("highlights", [])
            if not isinstance(references, list) or not isinstance(highlights, list):
                raise ContextGraphValidationError((f"invalid_node_data_collections:{index}",))
            for collection_name in ("responses", "excludedAttachmentIds", "includedAttachmentIds", "condensedFrom"):
                collection = data.get(collection_name, [])
                if not isinstance(collection, list) or not all(isinstance(value, str) for value in collection):
                    raise ContextGraphValidationError((f"invalid_{collection_name}:{index}",))
            for collection_name in (
                "questions", "generatedAts", "editedAts", "summaries", "generatedBy",
                "summaryTypes", "summaryTopics", "reasonings",
            ):
                collection = data.get(collection_name, [])
                if not isinstance(collection, list) or not all(
                    value is None or isinstance(value, str) for value in collection
                ):
                    raise ContextGraphValidationError((f"invalid_{collection_name}:{index}",))
            searches = data.get("gatewaySearches", [])
            if not isinstance(searches, list) or not all(
                value is None or isinstance(value, bool) for value in searches
            ):
                raise ContextGraphValidationError((f"invalid_gatewaySearches:{index}",))
            if data.get("attachments", []) != []:
                raise ContextGraphValidationError((f"unsupported_node_attachments:{index}",))
            if data.get("excludedAttachmentIds", []) or data.get("includedAttachmentIds", []):
                raise ContextGraphValidationError((f"unsupported_attachment_selection:{index}",))
            for reference_index, reference_value in enumerate(references):
                reference = _strict_mapping(
                    reference_value,
                    self._REFERENCE_FIELDS,
                    f"reference:{index}:{reference_index}",
                )
                _required_text(
                    reference.get("title"),
                    limits.max_node_content_chars,
                    f"reference_title:{index}:{reference_index}",
                )
                for field in ("url", "media", "date"):
                    _optional_text(
                        reference.get(field),
                        limits.max_node_content_chars,
                        f"reference_{field}:{index}:{reference_index}",
                    )
            for highlight_index, highlight_value in enumerate(highlights):
                highlight = _strict_mapping(
                    highlight_value,
                    self._HIGHLIGHT_FIELDS,
                    f"highlight:{index}:{highlight_index}",
                )
                _required_text(
                    highlight.get("id"),
                    256,
                    f"highlight_id:{index}:{highlight_index}",
                )
                _required_text(
                    highlight.get("text"),
                    limits.max_node_content_chars,
                    f"highlight_text:{index}:{highlight_index}",
                )
                _optional_text(
                    highlight.get("at"),
                    128,
                    f"highlight_at:{index}:{highlight_index}",
                )
            fanout_roles = data.get("fanoutRoles", [])
            if not isinstance(fanout_roles, list):
                raise ContextGraphValidationError((f"invalid_fanoutRoles:{index}",))
            for role_index, role_value in enumerate(fanout_roles):
                role = _strict_mapping(
                    role_value,
                    self._FANOUT_ROLE_FIELDS,
                    f"fanout_role:{index}:{role_index}",
                )
                _required_text(
                    role.get("name"),
                    256,
                    f"fanout_role_name:{index}:{role_index}",
                )
                _required_text(
                    role.get("prompt"),
                    limits.max_node_content_chars,
                    f"fanout_role_prompt:{index}:{role_index}",
                )
            anchor = data.get("anchor")
            if anchor is not None:
                anchor = _strict_mapping(anchor, self._ANCHOR_FIELDS, f"anchor:{index}")
                if (
                    not isinstance(anchor.get("page"), (int, float))
                    or isinstance(anchor.get("page"), bool)
                ):
                    raise ContextGraphValidationError((f"invalid_anchor_page:{index}",))
                rects = anchor.get("rects", [])
                if not isinstance(rects, list) or any(
                    not isinstance(rect, (list, tuple))
                    or len(rect) != 4
                    or any(
                        not isinstance(coordinate, (int, float))
                        or isinstance(coordinate, bool)
                        for coordinate in rect
                    )
                    for rect in rects
                ):
                    raise ContextGraphValidationError((f"invalid_anchor_rects:{index}",))
                _optional_text(anchor.get("attId"), 256, f"anchor_att_id:{index}")
            if "linemap" in data:
                _strict_mapping(data["linemap"], {"node_type", "content_ref", "content_revision", "source_refs", "trust", "stale", "stale_reason", "metadata"}, f"node_linemap:{index}")
        for index, item in enumerate(edges_raw):
            edge = _strict_mapping(item, self._EDGE_FIELDS, f"edge:{index}")
            _ExternalBudget(limits).inspect_node(edge, f"edge:{index}")
            data = edge.get("data", {})
            if data is None:
                data = {}
            _strict_mapping(data, self._EDGE_DATA_FIELDS, f"edge_data:{index}")
            if "linemap" in data:
                _strict_mapping(data["linemap"], {"context_mode", "depth", "ordering", "active", "metadata"}, f"edge_linemap:{index}")
        node_id_list = [_text(item.get("id"), 256, "node_id") for item in nodes_raw]
        edge_id_list = [_text(item.get("id"), 256, "edge_id") for item in edges_raw]
        if len(node_id_list) != len(set(node_id_list)):
            raise ContextGraphValidationError(("duplicate_node_id",))
        if len(edge_id_list) != len(set(edge_id_list)):
            raise ContextGraphValidationError(("duplicate_edge_id",))
        node_ids = set(node_id_list)
        edge_ids = set(edge_id_list)
        if "linemap" in root:
            envelope = _strict_mapping(root["linemap"], {"format_version", "snapshot"}, "linemap_envelope")
            if envelope.get("format_version") != 1 or not isinstance(envelope.get("snapshot"), Mapping):
                raise ContextGraphValidationError(("invalid_linemap_envelope",))
            _inspect_embedded_snapshot_payload(envelope["snapshot"], limits)
            snapshot = snapshot_from_dict(_decode_snapshot_metadata(envelope["snapshot"]))
            validate_snapshot(snapshot, limits=limits)
            if {node.node_id for node in snapshot.nodes} != node_ids or {edge.edge_id for edge in snapshot.edges} != edge_ids:
                raise ContextGraphValidationError(("linemap_canvas_identity_mismatch",))
            if (
                set(root) != {
                    "version", "name", "exportedAt", "nodes", "edges", "events", "linemap",
                }
                or root.get("version") != 1
                or root.get("name") != snapshot.graph_id
                or root.get("exportedAt") != snapshot.created_at
                or root.get("events") != []
            ):
                raise ContextGraphValidationError(("linemap_canvas_projection_mismatch",))
            expected_nodes = {
                str(item["id"]): item for item in thoughtdag_canvas_nodes(snapshot)
            }
            expected_edges = {
                str(item["id"]): item for item in thoughtdag_canvas_edges(snapshot)
            }
            for item in nodes_raw:
                if item != expected_nodes[str(item["id"])]:
                    raise ContextGraphValidationError(("linemap_canvas_projection_mismatch",))
            for item in edges_raw:
                if item != expected_edges[str(item["id"])]:
                    raise ContextGraphValidationError(("linemap_canvas_projection_mismatch",))
            return _bind_import_provenance(
                snapshot,
                project_id=project_id,
                source_type="thoughtdag",
                source_revision=source_revision,
                imported_at=imported_at,
                importer_id=self.importer_id,
                importer_revision=self.importer_revision,
                source_ref=authorized_path.name,
            )
        if any("linemap" in item["data"] for item in nodes_raw) or any("linemap" in (item.get("data") or {}) for item in edges_raw):
            raise ContextGraphValidationError(("orphan_linemap_extension",))
        nodes: list[ContextGraphNode] = []
        issues: list[IntegrityIssue] = []
        for index, item in enumerate(nodes_raw):
            node_id = _text(item.get("id"), 256, "node_id")
            data = item.get("data")
            position = item.get("position")
            if not isinstance(data, Mapping) or not isinstance(position, Mapping):
                raise ContextGraphValidationError((f"invalid_canvas_node:{node_id}",))
            question = _text(data.get("question", ""), limits.max_node_content_chars, "node_content")
            response = _text(data.get("response", ""), limits.max_node_content_chars, "node_content")
            content = "\n\n".join(part for part in (question, response) if part)
            kind = str(data.get("stepKind") or "human")
            node_type = {"note": "note", "file": "material", "link": "evidence", "synthesis": "research_synthesis"}.get(kind, "answer" if response else "question")
            created = _iso(data.get("createdAt"), exported_at)
            updated = _iso(data.get("lastGeneratedAt"), created)
            refs = data.get("references") if isinstance(data.get("references"), list) else []
            source_refs = [f"file:{authorized_path.name}#node={node_id}"]
            for ref in refs:
                if isinstance(ref, Mapping) and isinstance(ref.get("url"), str):
                    source_refs.append(ref["url"])
            highlights = tuple(h.get("text") for h in data.get("highlights", []) if isinstance(h, Mapping) and isinstance(h.get("text"), str)) if isinstance(data.get("highlights"), list) else ()
            nodes.append(ContextGraphNode(
                node_id=node_id, node_type=node_type, title=(question[:120] or node_id),
                content_ref=f"external-untrusted:{node_id}",
                content_revision=f"source:{source_revision}:version:{data.get('responseIndex', 0)}:updated:{updated}:chars:{len(content)}",
                source_refs=tuple(source_refs), trust="untrusted", created_at=created, updated_at=updated,
                metadata={"project_id": project_id, "content": content, "highlights": highlights,
                          "archived": bool(data.get("archived")), "external_text_role": "untrusted_content",
                          "position": {"x": position.get("x"), "y": position.get("y")},
                          "last_context_fingerprint": data.get("lastContextHash"),
                          "external_thoughtdag": {
                              "node": {
                                  key: value for key, value in item.items()
                                  if key not in {"id", "data"}
                              },
                              "data": {
                                  key: value for key, value in data.items()
                                  if key != "linemap"
                              },
                          }},
            ))
        edges: list[ContextGraphEdge] = []
        for index, item in enumerate(edges_raw):
            edge_id = _text(item.get("id"), 256, "edge_id")
            data = item.get("data") if isinstance(item.get("data"), Mapping) else {}
            cross = bool(data.get("isCrossLink") or data.get("isWatch"))
            mode = "full_chain" if not cross else ("full_chain" if data.get("contextDepth") == "full" else "quote_only")
            edges.append(ContextGraphEdge(
                edge_id=edge_id, source_node_id=_text(item.get("source"), 256, "source_node_id"),
                target_node_id=_text(item.get("target"), 256, "target_node_id"),
                context_mode=mode, depth=1, ordering=index,
                metadata={
                    "external": dict(data),
                    "external_thoughtdag": {
                        key: value for key, value in item.items()
                        if key not in {"id", "source", "target", "data"}
                    },
                },
            ))
        if root.get("events"):
            issues.append(IntegrityIssue(
                "events_not_executed",
                "ThoughtDAG events were not imported and were never executed.",
            ))
        if root.get("instantiatedFrom") is not None:
            issues.append(IntegrityIssue(
                "instantiation_metadata_not_compiled",
                "ThoughtDAG paradigm provenance remained external metadata and was not compiled.",
            ))
        runtime_keys = {
            "model", "webSearch", "scholarSearch", "autoRerun", "restreaming",
            "instruction", "fanoutRoles", "rolePrompt", "isEvaluator", "evaluatorTrigger",
        }
        if any(runtime_keys & set(item["data"]) for item in nodes_raw):
            issues.append(IntegrityIssue(
                "external_runtime_controls_not_executed",
                "ThoughtDAG model, prompt, search, evaluator and auto-run controls remained untrusted metadata and were never executed.",
            ))
        graph_name = _text(root.get("name") or authorized_path.stem, 256, "graph_name")
        sinks = sorted({n.node_id for n in nodes} - {e.source_node_id for e in edges})
        snapshot = ContextGraphSnapshot(
            schema_version="1.0.0", graph_id=f"thoughtdag:{graph_name}", graph_revision=source_revision,
            project_id=project_id, source_type="thoughtdag", source_revision=source_revision,
            created_at=exported_at, nodes=tuple(nodes), edges=tuple(edges), selected_outputs=tuple(sinks),
            token_estimate=(budget.total_chars + 3) // 4,
            provenance=ContextProvenance("thoughtdag", source_revision, imported_at, self.importer_id, self.importer_revision, authorized_path.name),
            integrity_issues=tuple(issues),
        )
        validate_snapshot(snapshot, limits=limits)
        return snapshot


class MarkdownGraphImporter:
    importer_id = "thought_graph_context.MarkdownGraphImporter"
    importer_revision = "1.1.0"
    _heading = re.compile(r"^(#{1,6})\s+(.+?)\s*$")

    def import_authorized_file(
        self,
        *,
        grant: AuthorizedContextFile | None = None,
        authorized_path: Path | None = None,
        project_id: str | None = None,
        limits: ImportLimits = ImportLimits(),
    ) -> ContextGraphSnapshot:
        grant = _require_context_file_grant(
            grant=grant, authorized_path=authorized_path, project_id=project_id
        )
        authorized_path, raw = _read_authorized(
            grant, importer_id=self.importer_id, limits=limits
        )
        project_id = grant.project_id
        lines = raw.splitlines()
        if lines and lines[0].startswith("<!-- linemap-context-graph-v1:"):
            match = _MARKDOWN_ENVELOPE.match(lines[0])
            if not match:
                raise ContextGraphValidationError(("invalid_markdown_linemap_envelope",))
            try:
                encoded = match.group(1)
                decoded = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-8")
                reject_external_secret_material(decoded, secret_canaries=limits.secret_canaries)
                embedded_payload = json.loads(decoded)
                reject_external_json_value(
                    embedded_payload,
                    secret_canaries=limits.secret_canaries,
                )
                _inspect_embedded_snapshot_payload(embedded_payload, limits)
                snapshot = snapshot_from_dict(_decode_snapshot_metadata(embedded_payload))
            except ContextGraphValidationError:
                raise
            except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
                raise ContextGraphValidationError(("invalid_markdown_linemap_envelope",)) from exc
            validate_snapshot(snapshot, limits=limits)
            now = datetime.now(timezone.utc).isoformat()
            return _bind_import_provenance(
                snapshot,
                project_id=project_id,
                source_type="markdown",
                source_revision=_file_revision(authorized_path, "markdown"),
                imported_at=now,
                importer_id=self.importer_id,
                importer_revision=self.importer_revision,
                source_ref=authorized_path.name,
            )
        budget = _ExternalBudget(limits)
        budget.inspect(raw, "markdown_content")
        now = datetime.now(timezone.utc).isoformat()
        sections: list[tuple[int, str, list[str]]] = []
        for line in lines:
            match = self._heading.match(line)
            if match:
                sections.append((len(match.group(1)), match.group(2), []))
            elif sections:
                sections[-1][2].append(line)
        if not sections:
            sections = [(1, authorized_path.stem, lines)]
        if len(sections) > limits.max_nodes:
            raise ContextGraphValidationError(("node_limit_exceeded",))
        nodes: list[ContextGraphNode] = []
        edges: list[ContextGraphEdge] = []
        parents: list[tuple[int, str]] = []
        for index, (level, title, lines) in enumerate(sections):
            node_id = f"md-{index + 1}"
            content = "\n".join(lines).strip()
            if len(content) > limits.max_node_content_chars:
                raise ContextGraphValidationError(("node_content_limit_exceeded",))
            nodes.append(ContextGraphNode(
                node_id=node_id, node_type="note", title=title[:120],
                content_ref=f"external-untrusted:{node_id}", content_revision=f"markdown-section:{index + 1}",
                source_refs=(f"file:{authorized_path.name}#section={index + 1}",), trust="untrusted",
                created_at=now, updated_at=now,
                metadata={"project_id": project_id, "content": content, "heading_level": level, "external_text_role": "untrusted_content"},
            ))
            while parents and parents[-1][0] >= level:
                parents.pop()
            if parents:
                edges.append(ContextGraphEdge(f"md-edge-{len(edges)+1}", parents[-1][1], node_id, "full_chain", 1, len(edges)))
            parents.append((level, node_id))
        source_revision = _file_revision(authorized_path, "markdown")
        sinks = sorted({n.node_id for n in nodes} - {e.source_node_id for e in edges})
        snapshot = ContextGraphSnapshot(
            "1.0.0", f"markdown:{authorized_path.stem}", source_revision, project_id, "markdown", source_revision, now,
            tuple(nodes), tuple(edges), tuple(sinks), (budget.total_chars + 3) // 4,
            ContextProvenance("markdown", source_revision, now, self.importer_id, self.importer_revision, authorized_path.name), (),
        )
        validate_snapshot(snapshot, limits=limits)
        return snapshot
