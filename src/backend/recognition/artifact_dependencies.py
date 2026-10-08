"""Read verified evidence links of retained model results without granting use.

Completion and historical document identity are checked here. Root revisions
may have since changed: lifecycle and egress owners decide current eligibility
and permission separately. This module never writes, reads egress policies,
or treats historical restructuring parents as supporting evidence.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import re


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$")


class ArtifactDependencyError(ValueError):
    """A retained result's task/document/packet evidence cannot be verified."""


@dataclass(frozen=True)
class ArtifactDependencies:
    roots: tuple[tuple[str, str, int], ...]
    packet: Mapping[str, object]
    revisions: Mapping[str, int]


def read_artifact_dependencies(reader, scope, payload: Mapping[str, object]) -> ArtifactDependencies | None:
    provenance = payload.get("provenance")
    if not isinstance(provenance, Mapping) or provenance.get("kind") != "model_generated_artifact":
        return None
    refs = _artifact_refs(provenance)
    task_id, task_revision = refs["task"]
    document_id, document_revision = refs["document"]
    packet_id, packet_revision = refs["context_packet"]
    task = reader.read("recognition_tasks", task_id)
    if (task is None or task.revision != task_revision or task.payload.get("project_id") != scope.project_id
            or task.payload.get("state") != "completed" or task.payload.get("kind") == "restructure"
            or task.payload.get("document_id") != document_id or task.payload.get("context_packet_id") != packet_id):
        raise ArtifactDependencyError("model artifact task provenance is unavailable")
    turn_id = task.payload.get("turn_id")
    if (turn_id is None and "turn" in refs) or (turn_id is not None and refs.get("turn") != (turn_id, None)):
        raise ArtifactDependencyError("model artifact turn provenance is unavailable")
    document = reader.read("documents", document_id)
    revision_key = f"{document_id}~r{document_revision}"
    document_record = reader.read("document_revisions", revision_key)
    markdown = reader.read("document_markdown", revision_key)
    if (document is None or document.payload.get("project_id") != scope.project_id
            or document_record is None or markdown is None
            or document_record.payload.get("document_id") != document_id
            or document_record.payload.get("revision") != document_revision
            or markdown.payload.get("document_id") != document_id
            or markdown.payload.get("revision") != document_revision
            or not isinstance(markdown.payload.get("markdown"), str)
            or not _document_has_task_ref(document_record.payload, task_id)):
        raise ArtifactDependencyError("model artifact document provenance is unavailable")
    packet = reader.read("recognition_context_packets", packet_id)
    if (packet is None or packet.revision != packet_revision or packet.payload.get("project_id") != scope.project_id
            or packet.payload.get("state") != "consumed" or packet.payload.get("task_id") != task_id
            or packet.payload.get("kind", "context") != "context"):
        raise ArtifactDependencyError("model artifact context provenance is unavailable")
    # A completed non-restructure task retains only its ordinary context roots.
    # Read these from the bound packet, never a body mention or an egress grant.
    items = packet.payload.get("items", ())
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
        raise ArtifactDependencyError("model artifact context roots are invalid")
    roots = []
    seen = set()
    for item in items:
        if not isinstance(item, Mapping):
            raise ArtifactDependencyError("model artifact context root is invalid")
        item_id, revision = item.get("id"), item.get("revision")
        if (not isinstance(item_id, str) or _SAFE_ID.fullmatch(item_id) is None
                or type(revision) is not int or revision < 1 or item_id in seen):
            raise ArtifactDependencyError("model artifact context root is invalid")
        root = reader.read("recognitions", item_id)
        if root is None or root.payload.get("scope") != {"user_id": scope.user_id, "project_id": scope.project_id}:
            raise ArtifactDependencyError("model artifact context root is unavailable in this work scope")
        seen.add(item_id)
        roots.append(("recognition", item_id, revision))
    return ArtifactDependencies(tuple(sorted(roots)), packet.payload, {
        "task_revision": task.revision, "document_revision": document_revision,
        "document_revision_record_revision": document_record.revision,
        "document_markdown_revision": markdown.revision, "context_packet_revision": packet.revision,
    })


def _artifact_refs(provenance: Mapping[str, object]) -> dict[str, tuple[str, int | None]]:
    raw = provenance.get("source_refs")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise ArtifactDependencyError("model artifact provenance is invalid")
    refs = {}
    for item in raw:
        if not isinstance(item, Mapping) or set(item).difference({"type", "id", "revision"}):
            raise ArtifactDependencyError("model artifact provenance is invalid")
        kind, item_id, revision = item.get("type"), item.get("id"), item.get("revision")
        if (not isinstance(kind, str) or kind not in {"task", "document", "context_packet", "turn"}
                or kind in refs or not isinstance(item_id, str) or not item_id):
            raise ArtifactDependencyError("model artifact provenance is invalid")
        if kind == "turn":
            if revision is not None:
                raise ArtifactDependencyError("model artifact turn provenance is invalid")
        elif type(revision) is not int or revision < 1:
            raise ArtifactDependencyError("model artifact provenance is invalid")
        refs[kind] = (item_id, revision)
    if not {"task", "document", "context_packet"}.issubset(refs):
        raise ArtifactDependencyError("model artifact provenance is incomplete")
    return refs


def _document_has_task_ref(document_revision: Mapping[str, object], task_id: str) -> bool:
    snapshot = document_revision.get("source_snapshot")
    refs = snapshot.get("source_refs") if isinstance(snapshot, Mapping) else None
    return (isinstance(refs, Sequence) and not isinstance(refs, (str, bytes))
            and {"source_id": task_id, "locator": f"task://{task_id}"} in refs)
