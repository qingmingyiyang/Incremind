from __future__ import annotations

import re
from datetime import datetime


_RFC3339_TIMESTAMP = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)


def require_rfc3339_timestamp(value: object, *, field: str) -> str:
    """Return a timezone-aware RFC 3339 timestamp without changing its spelling."""

    text = str(value).strip() if value is not None else ""
    if not text or _RFC3339_TIMESTAMP.fullmatch(text) is None:
        raise ValueError(f"{field} must be a timezone-aware RFC 3339 timestamp")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(
            f"{field} must be a timezone-aware RFC 3339 timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must be a timezone-aware RFC 3339 timestamp")
    return text


def optional_rfc3339_timestamp(value: object, *, field: str) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return require_rfc3339_timestamp(value, field=field)
