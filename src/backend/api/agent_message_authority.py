"""One-way, public payload copies for governed Agent mailbox messages.

Agent topology decides *whether* two runs may exchange a message.  This module
decides only whether a nominated sender-Turn payload is safe to copy, then
places an immutable recipient-owned copy in the existing Turn payload store.
It deliberately does not update a ContextManifest or mailbox state.
"""

from __future__ import annotations

from collections.abc import Mapping
import json
import math
import re
from typing import Protocol


_MESSAGE_KINDS = frozenset({"task", "progress", "result", "control"})
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:[\\/]")
_PATH_LIKE_FILENAME = re.compile(r"^[^/\\]+\.[A-Za-z0-9]{1,16}$")
_FORBIDDEN_FIELD_PARTS = frozenset({
    "secret", "token", "credential", "apikey", "endpoint", "provider",
    "model", "prompt", "hiddencontext", "contextmanifest",
    "capabilitymanifest", "modelrouting", "authorization", "hookreceipt",
})
_FORBIDDEN_REF_PARTS = frozenset({
    "context-manifest", "capability-manifest", "model-routing",
    "authorization", "hook", "receipt", "secret", "token", "credential",
    "provider", "endpoint", "prompt",
})
_MAX_DEPTH = 6
_MAX_ITEMS = 128
_MAX_BYTES = 16 * 1024


class AgentMessagePayloadAuthorityError(ValueError):
    """A nominated mailbox payload is not safe to cross a Turn boundary."""


class AgentMessagePayloadReadPort(Protocol):
    def get(self, payload_ref: str) -> object: ...


class AgentMessageImmutablePayloadPort(Protocol):
    def get_or_create_immutable_payload(
        self,
        turn_id: str,
        kind: str,
        payload: object,
    ) -> str: ...


class AgentMessagePayloadAuthority:
    """Copies bounded public JSON from one Turn into another immutable scope."""

    def __init__(
        self,
        *,
        payload_reader: AgentMessagePayloadReadPort,
        immutable_payloads: AgentMessageImmutablePayloadPort,
    ) -> None:
        self._payload_reader = payload_reader
        self._immutable_payloads = immutable_payloads

    def copy_for_recipient(
        self,
        *,
        sender_turn_id: str,
        recipient_turn_id: str,
        source_payload_ref: str,
        message_id: str,
        kind: str,
    ) -> str:
        """Return a recipient-owned immutable ref or fail without copying."""
        _identifier(sender_turn_id, "sender Turn id")
        _identifier(recipient_turn_id, "recipient Turn id")
        _identifier(message_id, "message id")
        if sender_turn_id == recipient_turn_id:
            raise AgentMessagePayloadAuthorityError("Agent messages must cross distinct Turns")
        if kind not in _MESSAGE_KINDS:
            raise AgentMessagePayloadAuthorityError("Agent message kind is not public")
        source_ref = _source_ref(source_payload_ref, sender_turn_id)
        try:
            payload = self._payload_reader.get(source_ref)
        except (KeyError, ValueError, TypeError) as error:
            raise AgentMessagePayloadAuthorityError("sender message payload is unavailable") from error
        _validate_public_json(payload)
        immutable_kind = f"agent-message/{message_id}/{kind}"
        try:
            return self._immutable_payloads.get_or_create_immutable_payload(
                recipient_turn_id,
                immutable_kind,
                payload,
            )
        except ValueError as error:
            # Existing immutable identity with different contents is a replay
            # collision, never permission to replace a recipient's evidence.
            raise AgentMessagePayloadAuthorityError(
                "recipient message payload immutable identity conflicts"
            ) from error


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise AgentMessagePayloadAuthorityError(f"{label} is invalid")
    return value


def _source_ref(value: object, sender_turn_id: str) -> str:
    if not isinstance(value, str):
        raise AgentMessagePayloadAuthorityError("sender payload reference is invalid")
    prefix = f"crp://session/{sender_turn_id}/"
    if not value.startswith(prefix):
        raise AgentMessagePayloadAuthorityError("sender payload reference crossed Turn identity")
    remainder = value[len(prefix):]
    parts = remainder.split("/")
    if "\\" in remainder or any(part in {"", ".", ".."} for part in parts):
        raise AgentMessagePayloadAuthorityError("sender payload reference is invalid")
    kind = remainder.split("/", 1)[0].casefold()
    if not kind or _contains_forbidden_ref_part(kind):
        raise AgentMessagePayloadAuthorityError("sender payload reference is not public")
    return value


def _validate_public_json(value: object) -> None:
    item_count = _validate_value(value, depth=0)
    if item_count > _MAX_ITEMS:
        raise AgentMessagePayloadAuthorityError("Agent message payload has too many items")
    try:
        encoded = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise AgentMessagePayloadAuthorityError("Agent message payload is not JSON-safe") from error
    if len(encoded) > _MAX_BYTES:
        raise AgentMessagePayloadAuthorityError("Agent message payload exceeds the public size limit")


def _validate_value(value: object, *, depth: int) -> int:
    if depth > _MAX_DEPTH:
        raise AgentMessagePayloadAuthorityError("Agent message payload exceeds the public depth limit")
    if value is None or isinstance(value, (bool, int)):
        return 1
    if isinstance(value, float):
        if not math.isfinite(value):
            raise AgentMessagePayloadAuthorityError("Agent message payload contains a non-finite number")
        return 1
    if isinstance(value, str):
        if _looks_like_path(value) or _contains_forbidden_ref(value):
            raise AgentMessagePayloadAuthorityError("Agent message payload contains a protected reference")
        return 1
    if isinstance(value, list):
        return 1 + sum(_validate_value(item, depth=depth + 1) for item in value)
    if isinstance(value, Mapping):
        count = 1
        for key, item in value.items():
            if not isinstance(key, str):
                raise AgentMessagePayloadAuthorityError("Agent message payload keys must be strings")
            if _contains_forbidden_field(key):
                raise AgentMessagePayloadAuthorityError("Agent message payload contains a protected field")
            count += _validate_value(item, depth=depth + 1)
        return count
    raise AgentMessagePayloadAuthorityError("Agent message payload is not JSON-safe")


def _contains_forbidden_field(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.casefold())
    return any(part in normalized for part in _FORBIDDEN_FIELD_PARTS)


def _contains_forbidden_ref(value: str) -> bool:
    # A child must receive a copied value, never a direct capability to read
    # any payload from its sender's Turn, even if the referenced kind looks
    # public today.
    return value.startswith("crp://")


def _contains_forbidden_ref_part(value: str) -> bool:
    normalized = value.casefold().replace("_", "-")
    compact = normalized.replace("-", "")
    protected = _FORBIDDEN_REF_PARTS | {
        item.replace("-", "") for item in _FORBIDDEN_REF_PARTS
    }
    return (
        normalized in protected
        or compact in protected
        or any(part in protected for part in normalized.split("-") if part)
    )


def _looks_like_path(value: str) -> bool:
    lowered = value.casefold()
    segments = value.split("/")
    return (
        "\\" in value
        or lowered.startswith("file://")
        or value.startswith(("/", "./", "../", "~"))
        or bool(_WINDOWS_ABSOLUTE.match(value))
        or any(segment.strip() in {".", ".."} for segment in segments)
        or (
            "/" in value
            and _PATH_LIKE_FILENAME.fullmatch(segments[-1].strip()) is not None
        )
    )
