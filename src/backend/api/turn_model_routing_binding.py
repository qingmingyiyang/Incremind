from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from backend.api.model_routing_snapshot_authority import TurnModelRoutingSnapshotAuthority
from backend.model_routing_snapshot import (
    turn_model_routing_snapshot_revision,
    validate_turn_model_routing_snapshot,
)


class TurnModelRoutingPayloadReader(Protocol):
    def get_immutable_payload(
        self,
        turn_id: str,
        kind: str,
    ) -> tuple[str, object] | None: ...


@dataclass(frozen=True, slots=True)
class TurnModelRoutingBinding:
    project_id: str
    snapshot: dict[str, object]
    snapshot_ref: str
    snapshot_revision: str

    def parameters(self) -> dict[str, object]:
        return {
            "_routing_project_id": self.project_id,
            "_model_routing_snapshot": self.snapshot,
            "_model_routing_snapshot_ref": self.snapshot_ref,
            "_model_routing_snapshot_revision": self.snapshot_revision,
        }


def load_turn_model_routing_binding(
    payloads: TurnModelRoutingPayloadReader,
    request: Mapping[str, object],
    *,
    required_capability: str,
) -> TurnModelRoutingBinding:
    """Load the immutable routing authority already frozen for this Turn."""

    turn_id = _text(request.get("turn_id"), "turn id")
    scope = request.get("scope")
    if not isinstance(scope, Mapping):
        raise ValueError("Turn model routing project scope is unavailable")
    project_id = _text(scope.get("project_id"), "project id")
    stored = payloads.get_immutable_payload(
        turn_id, TurnModelRoutingSnapshotAuthority.snapshot_kind,
    )
    if stored is None:
        raise ValueError("Turn model routing snapshot is unavailable")
    snapshot_ref, raw_snapshot = stored
    snapshot = validate_turn_model_routing_snapshot(raw_snapshot)
    turn = snapshot.get("turn")
    project = snapshot.get("project")
    requirement = snapshot.get("requirement")
    privacy = request.get("privacy")
    if not all(isinstance(item, Mapping) for item in (turn, project, requirement)):
        raise ValueError("Turn model routing identity is unavailable")
    if (
        turn.get("turn_id") != turn_id  # type: ignore[union-attr]
        or project.get("project_id") != project_id  # type: ignore[union-attr]
        or requirement.get("required_capability") != required_capability  # type: ignore[union-attr]
        or requirement.get("privacy_scope") != "remote_allowed"  # type: ignore[union-attr]
        or not isinstance(privacy, Mapping)
        or privacy.get("mode") != "remote_allowed"
        or privacy.get("allow_remote") is not True
    ):
        raise ValueError("Turn model routing snapshot identity drifted")
    revision = turn_model_routing_snapshot_revision(snapshot)
    return TurnModelRoutingBinding(
        project_id=project_id,
        snapshot=snapshot,
        snapshot_ref=snapshot_ref,
        snapshot_revision=revision,
    )


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty")
    return value.strip()
