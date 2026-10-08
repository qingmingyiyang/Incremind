from __future__ import annotations

from copy import deepcopy
import json
from threading import RLock
from uuid import uuid4

from .contracts import validate_governed_payload


class TurnPayloadNotFound(KeyError):
    pass


class InMemoryTurnPayloadStore:
    """Reference implementation; durable adapters must preserve the same opaque URI contract."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._payloads: dict[str, object] = {}
        self._immutable_payloads: dict[tuple[str, str], tuple[str, object, str]] = {}

    def put(self, turn_id: str, kind: str, payload: object) -> str:
        validate_governed_payload(payload)
        ref = f"crp://session/{turn_id}/{kind}/{uuid4().hex}"
        with self._lock:
            self._payloads[ref] = deepcopy(payload)
        return ref

    def get(self, payload_ref: str) -> object:
        with self._lock:
            if payload_ref not in self._payloads:
                raise TurnPayloadNotFound(payload_ref)
            return deepcopy(self._payloads[payload_ref])

    def get_or_create_immutable_payload(self, turn_id: str, kind: str, payload: object) -> str:
        _immutable_identity(turn_id, kind)
        validate_governed_payload(payload)
        encoded = _encode(payload)
        identity = (turn_id, kind)
        with self._lock:
            existing = self._immutable_payloads.get(identity)
            if existing is not None:
                if existing[2] != encoded:
                    raise ValueError("immutable payload identity conflict")
                return existing[0]
            ref = f"crp://session/{turn_id}/{kind}/{uuid4().hex}"
            stored = deepcopy(payload)
            self._immutable_payloads[identity] = (ref, stored, encoded)
            self._payloads[ref] = deepcopy(stored)
            return ref

    def reserve_immutable_payload(
        self, turn_id: str, kind: str, payload: object,
    ) -> tuple[str, bool]:
        _immutable_identity(turn_id, kind)
        validate_governed_payload(payload)
        encoded = _encode(payload)
        identity = (turn_id, kind)
        with self._lock:
            existing = self._immutable_payloads.get(identity)
            if existing is not None:
                if existing[2] != encoded:
                    raise ValueError("immutable payload identity conflict")
                return existing[0], False
            ref = f"crp://session/{turn_id}/{kind}/{uuid4().hex}"
            stored = deepcopy(payload)
            self._immutable_payloads[identity] = (ref, stored, encoded)
            self._payloads[ref] = deepcopy(stored)
            return ref, True

    def get_immutable_payload(self, turn_id: str, kind: str) -> tuple[str, object] | None:
        _immutable_identity(turn_id, kind)
        with self._lock:
            existing = self._immutable_payloads.get((turn_id, kind))
            if existing is None:
                return None
            return (existing[0], deepcopy(existing[1]))


def _immutable_identity(turn_id: str, kind: str) -> None:
    if not isinstance(turn_id, str) or not turn_id.strip():
        raise ValueError("immutable payload turn identity is invalid")
    if not isinstance(kind, str) or not kind.strip():
        raise ValueError("immutable payload kind is invalid")


def _encode(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
