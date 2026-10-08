"""Typed, turn-scoped context contributions with recoverable raw evidence.

The ContextManifest remains the selection and lineage authority.  This module
only makes three existing facts useful as bounded context: a tool artefact, a
task-graph change, or a recipient-owned Agent message.  Raw values are stored
separately from the model projection so a compaction can hide an entry without
destroying its auditable result.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import json
import re

from .ports import ContextEntry, TurnPayloadStorePort


_TYPED_KINDS = frozenset({"tool_artifact", "task_graph_change", "agent_message"})
_STATUSES = frozenset({"active", "invalidated"})
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class TypedContextEntryError(ValueError):
    pass


def record_typed_context_entry(
    *,
    payloads: TurnPayloadStorePort,
    turn_id: str,
    project_id: str,
    entry_id: str,
    kind: str,
    source_ref: str,
    revision_identity: str,
    raw_result: object,
    model_projection: object,
    provenance_refs: Sequence[str] = (),
    status: str = "active",
) -> ContextEntry:
    """Persist raw evidence and a safe model projection, then return its entry.

    `raw_result` is not copied into the ContextManifest or model projection.
    Callers retrieve it later through :func:`typed_context_raw_result` using
    the immutable envelope payload ref.
    """
    _identifier(turn_id, "turn id")
    _identifier(project_id, "project id")
    _identifier(entry_id, "context entry id")
    _validate_identity(kind, source_ref, revision_identity, provenance_refs, status)
    _json_value(model_projection, "model projection")
    raw_ref = payloads.put(turn_id, f"context-raw/{kind}/{entry_id}", raw_result)
    envelope = {
        "schema_version": "1.0.0",
        "entry_kind": kind,
        "turn_id": turn_id,
        "project_id": project_id,
        "entry_id": entry_id,
        "source_ref": source_ref,
        "revision_identity": revision_identity,
        "provenance_refs": list(provenance_refs),
        "status": status,
        "raw_result_ref": raw_ref,
        "model_projection": model_projection,
    }
    payload_ref = payloads.put(turn_id, f"context-entry/{kind}/{entry_id}", envelope)
    return ContextEntry(
        entry_id=entry_id,
        kind=kind,
        source_ref=source_ref,
        payload_ref=payload_ref,
        source_project_id=project_id,
        revision_identity=revision_identity,
        content_fingerprint=None,
        provenance_refs=tuple(provenance_refs),
        disclosure="model" if status == "active" else "audit_only",
        selection_reason=f"typed_{kind}_evidence",
        content_bytes=len(_encoded(model_projection)),
    )


def typed_context_model_projection(
    value: object,
    *,
    kind: str,
    turn_id: str | None,
    project_id: str | None,
) -> object:
    envelope = _envelope(value, kind=kind, turn_id=turn_id, project_id=project_id)
    if envelope["status"] != "active":
        raise TypedContextEntryError("invalidated typed context evidence cannot be disclosed to a model")
    return envelope["model_projection"]


def typed_context_raw_result(
    payloads: TurnPayloadStorePort,
    entry: ContextEntry,
) -> object:
    """Return raw evidence for a typed ContextEntry without reselecting it."""
    if entry.kind not in _TYPED_KINDS or entry.payload_ref is None:
        raise TypedContextEntryError("typed context entry payload is unavailable")
    envelope = _envelope(
        payloads.get(entry.payload_ref), kind=entry.kind,
        turn_id=_turn_id_from_ref(entry.payload_ref), project_id=entry.source_project_id,
    )
    if envelope["entry_id"] != entry.entry_id or envelope["source_ref"] != entry.source_ref:
        raise TypedContextEntryError("typed context entry lineage drifted")
    return payloads.get(str(envelope["raw_result_ref"]))


def _envelope(
    value: object, *, kind: str, turn_id: str | None, project_id: str | None,
) -> dict[str, object]:
    fields = {
        "schema_version", "entry_kind", "turn_id", "project_id", "entry_id",
        "source_ref", "revision_identity", "provenance_refs", "status",
        "raw_result_ref", "model_projection",
    }
    if not isinstance(value, Mapping) or set(value) != fields:
        raise TypedContextEntryError("typed context payload shape is invalid")
    envelope = dict(value)
    if envelope["schema_version"] != "1.0.0" or envelope["entry_kind"] != kind:
        raise TypedContextEntryError("typed context payload authority is invalid")
    payload_turn = envelope.get("turn_id")
    payload_project = envelope.get("project_id")
    if not isinstance(payload_turn, str) or not isinstance(payload_project, str):
        raise TypedContextEntryError("typed context payload scope is invalid")
    if (turn_id is not None and payload_turn != turn_id) or (project_id is not None and payload_project != project_id):
        raise TypedContextEntryError("typed context payload scope drifted")
    _validate_identity(
        kind, envelope.get("source_ref"), envelope.get("revision_identity"),
        envelope.get("provenance_refs"), envelope.get("status"),
    )
    _identifier(envelope.get("entry_id"), "context entry id")
    raw_ref = envelope.get("raw_result_ref")
    if not isinstance(raw_ref, str) or not raw_ref.startswith(f"crp://session/{payload_turn}/"):
        raise TypedContextEntryError("typed context raw result crossed Turn identity")
    _json_value(envelope.get("model_projection"), "model projection")
    return envelope


def _validate_identity(
    kind: object, source_ref: object, revision_identity: object,
    provenance_refs: object, status: object,
) -> None:
    if kind not in _TYPED_KINDS:
        raise TypedContextEntryError("typed context kind is invalid")
    if not isinstance(source_ref, str) or not source_ref.startswith("crp://"):
        raise TypedContextEntryError("typed context source ref is invalid")
    _identifier(revision_identity, "typed context revision")
    if status not in _STATUSES:
        raise TypedContextEntryError("typed context evidence status is invalid")
    if not isinstance(provenance_refs, Sequence) or isinstance(provenance_refs, (str, bytes)):
        raise TypedContextEntryError("typed context lineage is invalid")
    refs = tuple(provenance_refs)
    if len(refs) != len(set(refs)) or any(not isinstance(ref, str) or not ref.startswith("crp://") for ref in refs):
        raise TypedContextEntryError("typed context lineage is invalid")


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise TypedContextEntryError(f"{label} is invalid")
    return value


def _turn_id_from_ref(ref: str) -> str:
    prefix = "crp://session/"
    if not ref.startswith(prefix):
        raise TypedContextEntryError("typed context payload ref is invalid")
    turn_id = ref[len(prefix):].split("/", 1)[0]
    return _identifier(turn_id, "turn id")


def _encoded(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _json_value(value: object, label: str) -> None:
    try:
        _encoded(value)
    except (TypeError, ValueError) as error:
        raise TypedContextEntryError(f"{label} must be JSON-safe") from error
