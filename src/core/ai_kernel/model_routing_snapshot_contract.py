"""Kernel-side binding contract for frozen model-routing snapshots.

The backend owns routing projection and performs the complete authority
validation before egress. The kernel only needs a provider-neutral,
deterministic contract to bind an already-frozen payload to one Turn.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping


class ModelRoutingSnapshotContractError(ValueError):
    """Raised when the immutable routing payload cannot be safely bound."""


def validate_planner_routing_snapshot(value: object) -> dict[str, object]:
    """Return a detached, JSON-safe routing payload for kernel planning.

    This does not select a provider or reproject mutable routing state.
    Backend composition remains the full schema and authority validator before
    model egress.
    """
    try:
        payload = json.loads(json.dumps(value, ensure_ascii=False))
    except (TypeError, ValueError) as error:
        raise ModelRoutingSnapshotContractError(
            "Turn model routing snapshot is not JSON-safe"
        ) from error
    if not isinstance(payload, dict):
        raise ModelRoutingSnapshotContractError("Turn model routing snapshot is invalid")
    for field in ("turn", "project", "requirement"):
        if not isinstance(payload.get(field), Mapping):
            raise ModelRoutingSnapshotContractError("Turn model routing snapshot is invalid")
    return payload


def planner_routing_snapshot_revision(value: Mapping[str, object]) -> str:
    """Canonical revision for the exact detached payload passed to a gateway."""
    payload = validate_planner_routing_snapshot(value)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
