"""Freeze and re-check the source authority of a context packet.

Packets contain model-ready text, so their source policy must be captured at
preview time and revalidated before the preserved Turn runtime can send that
text.  This module deliberately keeps the snapshot body-free; the existing
``SourceEgressService`` remains the sole provenance and policy authority.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from urllib.parse import urlsplit

from backend.recognition import RecognitionConflict

from .source_egress import SourceEgressService
from .privacy_policy import egress_allowed, is_private_project


_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def capture_packet_egress(service, scope, packet: Mapping[str, object]) -> dict[str, object] | None:
    """Return the exact source snapshot required by a newly built packet.

    Capturing only freezes current versions and permissions.  It does not
    require the generation purpose because local preview/persistence is not an
    egress event.  Unknown or incomplete provenance remains fail-closed in the
    underlying source authority.
    """
    roots = _roots(packet)
    if not roots:
        return None
    return SourceEgressService(service.records).snapshot(scope, roots)


def validate_packet_egress(service, models, scope, packet: Mapping[str, object]) -> None:
    """Validate the frozen packet roots and require remote generation consent."""
    if _is_remote_generation(models) and not egress_allowed(service.records, models, scope.project_id, "generation"):
        raise RecognitionConflict("private_project_remote_blocked" if is_private_project(service.records, scope.project_id) else "remote_disabled")
    from .research_packets import validate_research_packet
    validate_research_packet(service.records,scope,packet,authority=SourceEgressService(service.records),remote=_is_remote_generation(models))
    roots = _roots(packet)
    stored = packet.get("source_egress")
    if not roots:
        if stored is not None:
            raise RecognitionConflict("context packet source egress is invalid")
        return
    if not isinstance(stored, Mapping):
        raise RecognitionConflict("context packet source egress is unavailable")
    if stored.get("roots") != roots:
        raise RecognitionConflict("context packet source egress roots changed")
    authority = SourceEgressService(service.records)
    authority.validate_snapshot(scope, stored)
    if _is_remote_generation(models):
        authority.require(stored, "generation")


def _roots(packet: Mapping[str, object]) -> list[dict[str, object]]:
    if not isinstance(packet, Mapping):
        raise RecognitionConflict("context packet is invalid")
    raw_roots: list[tuple[str, str, int]] = []
    kind = packet.get("kind", "context")
    if kind == "restructure":
        snapshot = packet.get("snapshot")
        if not isinstance(snapshot, Mapping):
            raise RecognitionConflict("restructure packet source snapshot is invalid")
        raw_roots.extend(_snapshot_roots(snapshot.get("recognitions"), "recognition"))
        raw_roots.extend(_snapshot_roots(snapshot.get("experiences"), "experience"))
    elif kind == "context":
        raw_roots.extend(_item_roots(packet.get("items", ())))
    else:
        raise RecognitionConflict("context packet kind is invalid")
    if len(raw_roots) != len({(source_type, source_id) for source_type, source_id, _ in raw_roots}):
        raise RecognitionConflict("context packet source roots contain duplicates")
    return [
        {"type": source_type, "id": source_id, "revision": revision}
        for source_type, source_id, revision in sorted(raw_roots)
    ]


def _item_roots(items: object) -> list[tuple[str, str, int]]:
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
        raise RecognitionConflict("context packet items are invalid")
    return [_root(item, "recognition") for item in items]


def _snapshot_roots(rows: object, source_type: str) -> list[tuple[str, str, int]]:
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        raise RecognitionConflict("restructure packet source snapshot is invalid")
    return [_root(row, source_type) for row in rows]


def _root(value: object, source_type: str) -> tuple[str, str, int]:
    if not isinstance(value, Mapping):
        raise RecognitionConflict("context packet source root is invalid")
    source_id, revision = value.get("id"), value.get("revision")
    if (not isinstance(source_id, str) or not source_id.strip() or len(source_id) > 128
            or type(revision) is not int or revision < 1):
        raise RecognitionConflict("context packet source root is invalid")
    return source_type, source_id, revision


def _is_remote_generation(models) -> bool:
    try:
        public = models.public()
        generation = public["generation"]
        base_url = generation["base_url"]
    except (AttributeError, KeyError, TypeError):
        # A malformed model object cannot make a remote egress safe.  Let the
        # regular model configuration path issue its own user-facing error
        # after this authority has imposed the conservative permission check.
        return True
    try:
        host = urlsplit(str(base_url)).hostname
    except ValueError:
        return True
    return host not in _LOCAL_HOSTS
