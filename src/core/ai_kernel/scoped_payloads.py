from __future__ import annotations

from collections.abc import Iterable, Mapping

from .context_manifest import ContextManifestError, context_manifest_from_payload, context_payload_refs
from .ports import TurnPayloadStorePort


class ScopedTurnPayloadError(PermissionError):
    pass


class ScopedTurnPayloadView:
    """Turn-bound Planner view with allowlisted reads and governed same-Turn writes."""

    def __init__(
        self,
        store: TurnPayloadStorePort,
        *,
        turn_id: str,
        allowed_refs: Iterable[str],
    ) -> None:
        if not isinstance(turn_id, str) or not turn_id.strip():
            raise ScopedTurnPayloadError("scoped payload turn identity is invalid")
        self._store = store
        self._turn_id = turn_id
        self._prefix = f"crp://session/{turn_id}/"
        self._allowed_refs = frozenset(_valid_ref(ref) for ref in allowed_refs)
        if any(not ref.startswith(self._prefix) for ref in self._allowed_refs):
            raise ScopedTurnPayloadError("scoped payload allowlist crossed Turn identity")

    def put(self, turn_id: str, kind: str, payload: object) -> str:
        if turn_id != self._turn_id:
            raise ScopedTurnPayloadError("Planner payload write crossed Turn identity")
        return self._store.put(turn_id, kind, payload)

    def get(self, payload_ref: str) -> object:
        ref = _valid_ref(payload_ref)
        if not ref.startswith(self._prefix) or ref not in self._allowed_refs:
            raise ScopedTurnPayloadError("payload ref is outside Planner context manifest")
        return self._store.get(ref)

    def get_immutable_payload(self, turn_id: str, kind: str) -> tuple[str, object] | None:
        """Read an immutable authority only when its ref is in planner context."""
        if turn_id != self._turn_id:
            raise ScopedTurnPayloadError("immutable payload crossed Turn identity")
        resolved = self._store.get_immutable_payload(turn_id, kind)
        if resolved is None:
            return None
        ref, payload = resolved
        if ref not in self._allowed_refs:
            raise ScopedTurnPayloadError("immutable payload is outside Planner context manifest")
        return ref, payload


def event_payload_refs(events: Iterable[Mapping[str, object]]) -> tuple[str, ...]:
    refs = []
    for event in events:
        data = event.get("data")
        payload_ref = data.get("payload_ref") if isinstance(data, Mapping) else None
        if isinstance(payload_ref, str):
            refs.append(_valid_ref(payload_ref))
    return tuple(dict.fromkeys(refs))


def planner_context_payload_refs(
    events: Iterable[Mapping[str, object]],
    store: TurnPayloadStorePort,
    *,
    turn_id: str | None = None,
) -> tuple[str, ...]:
    event_values = tuple(events)
    refs = list(event_payload_refs(event_values))
    if turn_id is not None:
        if not isinstance(turn_id, str) or not turn_id.strip():
            raise ScopedTurnPayloadError("scoped payload turn identity is invalid")
        role = store.get_immutable_payload(turn_id, "agent-role-brief-v1")
        if role is not None:
            role_ref = _valid_ref(role[0])
            if not role_ref.startswith(f"crp://session/{turn_id}/"):
                raise ScopedTurnPayloadError("role brief crossed Turn identity")
            refs.append(role_ref)
    for event in event_values:
        if event.get("type") != "expert.binding.frozen":
            continue
        data = event.get("data")
        evidence_refs = data.get("evidence_refs") if isinstance(data, Mapping) else None
        if isinstance(evidence_refs, list):
            refs.extend(_valid_ref(ref) for ref in evidence_refs)
    for event in event_values:
        if event.get("type") != "context.resolved":
            continue
        data = event.get("data")
        payload_ref = data.get("payload_ref") if isinstance(data, Mapping) else None
        if not isinstance(payload_ref, str):
            continue
        try:
            manifest = context_manifest_from_payload(store.get(payload_ref))
        except (ContextManifestError, KeyError, ValueError):
            continue
        refs.extend(context_payload_refs(manifest))
    return tuple(dict.fromkeys(refs))


def _valid_ref(value: object) -> str:
    if not isinstance(value, str) or not value.startswith("crp://session/"):
        raise ScopedTurnPayloadError("scoped payload ref is invalid")
    return value
