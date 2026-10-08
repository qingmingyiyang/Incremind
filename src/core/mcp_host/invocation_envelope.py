from __future__ import annotations

import re
from collections.abc import Mapping


INVOCATION_META_KEY = "io.chriptmas/invocation"
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,511}$")
_FIELDS = frozenset({
    "turn_id", "invocation_id", "operation_id", "idempotency_key", "attempt",
})


def validated_invocation_envelope(
    value: Mapping[str, object] | None,
) -> dict[str, object] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != _FIELDS:
        raise ValueError("MCP invocation envelope is invalid")
    normalized: dict[str, object] = {}
    for field in ("turn_id", "invocation_id", "operation_id"):
        item = value.get(field)
        if not isinstance(item, str) or _IDENTIFIER.fullmatch(item) is None:
            raise ValueError("MCP invocation envelope is invalid")
        normalized[field] = item
    key = value.get("idempotency_key")
    if not isinstance(key, str) or _IDEMPOTENCY_KEY.fullmatch(key) is None:
        raise ValueError("MCP invocation envelope is invalid")
    if key != f"{normalized['operation_id']}:{normalized['invocation_id']}":
        raise ValueError("MCP invocation envelope is invalid")
    attempt = value.get("attempt")
    if not isinstance(attempt, int) or isinstance(attempt, bool) or not 1 <= attempt <= 16:
        raise ValueError("MCP invocation envelope is invalid")
    normalized["idempotency_key"] = key
    normalized["attempt"] = attempt
    return normalized
