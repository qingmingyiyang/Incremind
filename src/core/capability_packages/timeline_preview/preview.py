from __future__ import annotations

from collections.abc import Mapping, Sequence


def timeline_preview(items: Sequence[Mapping[str, object]]) -> tuple[dict[str, object], ...]:
    """Return a stable read-only projection without owning execution or storage."""

    projected = [
        {
            "ref": str(item.get("ref") or ""),
            "title": str(item.get("title") or ""),
            "occurred_at": str(item.get("occurred_at") or ""),
        }
        for item in items
    ]
    return tuple(sorted(projected, key=lambda item: (item["occurred_at"], item["ref"])))
