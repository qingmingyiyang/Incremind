"""Pure, stable TaskReference helpers shared by task read models.

The storage candidate index never exposes these values.  It uses the same
opaque-reference ordering as the API so a keyset cursor can be translated to
a durable sortable key without importing a backend adapter.
"""
from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from datetime import datetime, timezone


TASK_REFERENCE_VERSION = "task-ref.v1"
WORKBENCH_CONTENT_TRANSFORM_KIND = "workbench_content_transform"
_SORT_PROJECT = "task-reference-sort-v1"


def workbench_transform_task_reference(*, project_id: str, job_id: str) -> str:
    return _encode({
        "v": TASK_REFERENCE_VERSION,
        "k": WORKBENCH_CONTENT_TRANSFORM_KIND,
        "p": project_id,
        "i": job_id,
    })


def workbench_transform_task_tie_key(job_id: str) -> str:
    """Return a project-independent opaque-reference ordering key.

    Canonical JSON places ``i`` before ``p``. Therefore distinct Job ids
    diverge before the project field is encoded; a fixed sentinel project
    preserves the same lexical order as every public transform reference.
    """
    return workbench_transform_task_reference(project_id=_SORT_PROJECT, job_id=job_id)


def workbench_transform_job_id_from_reference(value: str) -> str | None:
    try:
        payload = _decode(value)
    except (TypeError, ValueError, UnicodeError, json.JSONDecodeError):
        return None
    if (
        payload.get("v") != TASK_REFERENCE_VERSION
        or payload.get("k") != WORKBENCH_CONTENT_TRANSFORM_KIND
        or not isinstance(payload.get("i"), str)
        or not payload["i"]
    ):
        return None
    return payload["i"]


def task_updated_utc_key(value: object) -> str:
    """Normalize a public task timestamp into its durable cursor key.

    This is deliberately total: invalid and absent values sort with the API's
    undated owners, while naive timestamps retain the API convention of UTC.
    """
    minimum = "0001-01-01T00:00:00.000000+00:00"
    if not isinstance(value, str) or not value:
        return minimum
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return minimum
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    else:
        parsed = parsed.astimezone(timezone.utc)
    return (
        f"{parsed.year:04d}-{parsed.month:02d}-{parsed.day:02d}T"
        f"{parsed.hour:02d}:{parsed.minute:02d}:{parsed.second:02d}."
        f"{parsed.microsecond:06d}+00:00"
    )


def _encode(value: Mapping[str, str]) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "tr1_" + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode(value: str) -> dict[str, object]:
    if not isinstance(value, str) or not value.startswith("tr1_"):
        raise ValueError("task reference is invalid")
    encoded = value[4:] + "=" * (-len(value[4:]) % 4)
    payload = json.loads(base64.urlsafe_b64decode(encoded.encode("ascii")))
    if not isinstance(payload, Mapping):
        raise ValueError("task reference is invalid")
    return dict(payload)
