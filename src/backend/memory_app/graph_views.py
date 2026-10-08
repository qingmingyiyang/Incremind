"""Persistent, content-free layout state for the recognition graph.

The graph projection is derived from the recognition authority.  A saved view
therefore stores only stable node identifiers and presentation choices; it is
never another copy of a recognition, experience, or question.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence

from backend.recognition import RecognitionConflict, RecognitionError, WorkScope
from core.storage_provider import (
    SQLiteStructuredRecord,
    SQLiteStructuredRecordStore,
    SQLiteStructuredRecordUnitOfWork,
    SQLiteUnitOfWorkConflict,
)


COLLECTION = "recognition_graph_views"
_NODE_COLLECTIONS = ("recognition_experiences", "recognitions", "recognition_questions")
_RECOGNITIONS = "recognitions"
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$")
_MAX_NODES = 200
_MAX_COORDINATE = 100_000


class GraphViewService:
    """CAS write boundary for one scoped, saved graph layout."""

    def __init__(self, records: SQLiteStructuredRecordStore) -> None:
        self._records = records

    def get(self, scope: WorkScope, view_id: str) -> dict[str, object] | None:
        """Return the current projection, or ``None`` when this view is absent.

        The projection removes deleted nodes and selections that ceased to be
        selectable.  It deliberately does not repair the stored layout, so a
        caller can see what changed without an invisible write.
        """
        view_id = _id(view_id, "view_id")
        with self._records.begin() as uow:
            record = uow.read(COLLECTION, view_id)
            if record is None:
                uow.rollback()
                return None
            _require_scope(record, scope)
            known = _known_nodes(uow, scope)
            result = _project(record, known)
            uow.rollback()
        return result

    def list(self, scope: WorkScope) -> tuple[dict[str, object], ...]:
        """Return all saved views for exactly one work scope."""
        with self._records.begin() as uow:
            records = tuple(record for record in uow.list(COLLECTION) if _same_scope(record.payload, scope))
            known = _known_nodes(uow, scope)
            result = tuple(_project(record, known) for record in records)
            uow.rollback()
        return result

    def upsert(
        self,
        scope: WorkScope,
        view_id: str,
        expected_revision: int,
        *,
        node_ids: Sequence[str],
        positions: Mapping[str, object],
        collapsed_ids: Sequence[str],
        hidden_ids: Sequence[str],
        selected_ids: Sequence[str],
        focus_id: str | None = None,
    ) -> dict[str, object]:
        """Create or replace a complete layout using mandatory CAS.

        ``expected_revision`` is zero only for creation.  Passing exactly the
        already saved state is a no-op and leaves the revision unchanged.
        """
        view_id = _id(view_id, "view_id")
        _expected_revision(expected_revision)
        normalized = _layout(
            node_ids=node_ids,
            positions=positions,
            collapsed_ids=collapsed_ids,
            hidden_ids=hidden_ids,
            selected_ids=selected_ids,
            focus_id=focus_id,
        )
        with self._records.begin() as uow:
            existing = uow.read(COLLECTION, view_id)
            if existing is not None:
                _require_scope(existing, scope)
            actual_revision = existing.revision if existing is not None else 0
            if actual_revision != expected_revision:
                raise RecognitionConflict("graph view revision conflicted")
            known = _known_nodes(uow, scope)
            _validate_nodes(normalized, known)
            payload = {
                "id": view_id,
                "scope": _scope(scope),
                "project_id": scope.project_id,
                "node_ids": list(normalized["node_ids"]),
                "positions": dict(normalized["positions"]),
                "collapsed_ids": list(normalized["collapsed_ids"]),
                "hidden_ids": list(normalized["hidden_ids"]),
                "selected_ids": list(normalized["selected_ids"]),
                "focus_id": normalized["focus_id"],
            }
            if existing is not None and dict(existing.payload) == payload:
                result = _project(existing, known)
                uow.rollback()
                return result
            try:
                saved = uow.put(COLLECTION, view_id, payload, expected_revision=expected_revision)
            except SQLiteUnitOfWorkConflict as exc:
                raise RecognitionConflict("graph view revision conflicted") from exc
            uow.commit()
        return _project(saved, known)


def _layout(*, node_ids, positions, collapsed_ids, hidden_ids, selected_ids, focus_id):
    nodes = _ids(node_ids, "node_ids")
    if len(nodes) > _MAX_NODES:
        raise RecognitionError("graph view contains too many nodes")
    node_set = set(nodes)
    parsed_positions = _positions(positions)
    for label, values in (
        ("position keys", tuple(parsed_positions)),
        ("collapsed_ids", _ids(collapsed_ids, "collapsed_ids")),
        ("hidden_ids", _ids(hidden_ids, "hidden_ids")),
        ("selected_ids", _ids(selected_ids, "selected_ids")),
    ):
        if not set(values).issubset(node_set):
            raise RecognitionError(f"graph view {label} must belong to node_ids")
    parsed_focus = None if focus_id is None else _id(focus_id, "focus_id")
    if parsed_focus is not None and parsed_focus not in node_set:
        raise RecognitionError("graph view focus_id must belong to node_ids")
    hidden = _ids(hidden_ids, "hidden_ids")
    selected = _ids(selected_ids, "selected_ids")
    if set(hidden).intersection(selected):
        raise RecognitionError("selected graph nodes cannot be hidden")
    return {
        "node_ids": nodes,
        "positions": parsed_positions,
        "collapsed_ids": _ids(collapsed_ids, "collapsed_ids"),
        "hidden_ids": hidden,
        "selected_ids": selected,
        "focus_id": parsed_focus,
    }


def _known_nodes(uow: SQLiteStructuredRecordUnitOfWork, scope: WorkScope) -> dict[str, SQLiteStructuredRecord]:
    """Read all legitimate graph nodes through one transaction snapshot."""
    result: dict[str, SQLiteStructuredRecord] = {}
    for collection in _NODE_COLLECTIONS:
        for record in uow.list(collection):
            if _same_scope(record.payload, scope):
                if record.object_id in result:
                    raise RecognitionError("graph node id is ambiguous")
                result[record.object_id] = record
    return result


def _validate_nodes(layout: Mapping[str, object], known: Mapping[str, SQLiteStructuredRecord]) -> None:
    missing = [node_id for node_id in layout["node_ids"] if node_id not in known]
    if missing:
        raise RecognitionConflict("graph node is unavailable in this work scope")
    selected = layout["selected_ids"]
    for node_id in selected:
        record = known[node_id]
        if record.collection != _RECOGNITIONS or record.payload.get("state") != "active":
            raise RecognitionConflict("selected graph node is not an active recognition")


def _project(record: SQLiteStructuredRecord, known: Mapping[str, SQLiteStructuredRecord]) -> dict[str, object]:
    payload = record.payload
    nodes = _stored_ids(payload, "node_ids")
    current = tuple(node_id for node_id in nodes if node_id in known)
    current_set = set(current)
    excluded = tuple(node_id for node_id in nodes if node_id not in known)
    positions = _stored_positions(payload.get("positions"))
    collapsed = tuple(node_id for node_id in _stored_ids(payload, "collapsed_ids") if node_id in current_set)
    hidden = tuple(node_id for node_id in _stored_ids(payload, "hidden_ids") if node_id in current_set)
    hidden_set = set(hidden)
    selected_raw = _stored_ids(payload, "selected_ids")
    selected: list[str] = []
    selection_excluded: list[str] = []
    for node_id in selected_raw:
        node = known.get(node_id)
        if node is None or node_id in hidden_set or node.collection != _RECOGNITIONS or node.payload.get("state") != "active":
            selection_excluded.append(node_id)
        else:
            selected.append(node_id)
    focus = payload.get("focus_id")
    if focus is not None and (not isinstance(focus, str) or focus not in current_set):
        focus = None
    return {
        "id": record.object_id,
        "revision": record.revision,
        "project_id": _scope_from_payload(payload).project_id,
        "node_ids": list(current),
        "positions": {node_id: position for node_id, position in positions.items() if node_id in current_set},
        "collapsed_ids": list(collapsed),
        "hidden_ids": list(hidden),
        "selected_ids": selected,
        "focus_id": focus,
        "excluded_ids": list(excluded),
        "selection_excluded_ids": selection_excluded,
    }


def _positions(value: object) -> dict[str, dict[str, float]]:
    if not isinstance(value, Mapping):
        raise RecognitionError("graph view positions are invalid")
    result: dict[str, dict[str, float]] = {}
    for raw_id, raw_position in value.items():
        node_id = _id(raw_id, "position key")
        if not isinstance(raw_position, Mapping) or set(raw_position) != {"x", "y"}:
            raise RecognitionError("graph view position is invalid")
        x, y = raw_position["x"], raw_position["y"]
        if not _coordinate(x) or not _coordinate(y):
            raise RecognitionError("graph view coordinate is invalid")
        result[node_id] = {"x": float(x), "y": float(y)}
    return result


def _stored_positions(value: object) -> dict[str, dict[str, float]]:
    try:
        return _positions(value)
    except RecognitionError as exc:
        raise RecognitionError("stored graph view positions are invalid") from exc


def _coordinate(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and abs(value) <= _MAX_COORDINATE and math.isfinite(value)


def _stored_ids(payload: Mapping[str, object], label: str) -> tuple[str, ...]:
    try:
        return _ids(payload.get(label), label)
    except RecognitionError as exc:
        raise RecognitionError(f"stored graph view {label} is invalid") from exc


def _ids(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise RecognitionError(f"graph view {label} is invalid")
    result = tuple(_id(item, label) for item in value)
    if len(result) != len(set(result)):
        raise RecognitionError(f"graph view {label} contains duplicates")
    return result


def _id(value: object, label: str) -> str:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        raise RecognitionError(f"graph view {label} is invalid")
    return value


def _expected_revision(value: object) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise RecognitionError("graph view expected_revision is invalid")


def _scope(scope: WorkScope) -> dict[str, str | None]:
    return {"user_id": scope.user_id, "project_id": scope.project_id}


def _scope_from_payload(payload: Mapping[str, object]) -> WorkScope:
    value = payload.get("scope")
    if not isinstance(value, Mapping):
        raise RecognitionError("stored graph view scope is invalid")
    try:
        scope = WorkScope(value.get("user_id"), value.get("project_id"))
    except (TypeError, ValueError) as exc:
        raise RecognitionError("stored graph view scope is invalid") from exc
    if payload.get("project_id") != scope.project_id:
        raise RecognitionError("stored graph view project scope is invalid")
    return scope


def _same_scope(payload: Mapping[str, object], scope: WorkScope) -> bool:
    try:
        return _scope_from_payload(payload) == scope
    except RecognitionError:
        return False


def _require_scope(record: SQLiteStructuredRecord, scope: WorkScope) -> None:
    if not _same_scope(record.payload, scope):
        raise RecognitionConflict("graph view is unavailable in this work scope")
