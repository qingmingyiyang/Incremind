"""Freeze one bounded derived WorldState projection for an AI Turn."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

from core.ai_kernel import (
    TurnPayloadStorePort,
    context_manifest_from_payload,
    validate_external_agent_safe_projection,
)
from core.personal_world_model import (
    PersonalWorldModelError,
    ProjectDynamicsEngine,
    SQLiteWorldEventRepository,
    parse_world_timestamp,
    validate_world_identifier,
)
from core.storage_provider import SQLiteStructuredRecordStore


class PersonalWorldModelContextError(ValueError):
    """Raised when a Turn cannot freeze a safe project-world snapshot."""


@dataclass(frozen=True, slots=True)
class TurnWorldStateSnapshot:
    payload_ref: str
    revision: str
    source_ref: str
    content_bytes: int
    provenance_refs: tuple[str, ...]
    payload: Mapping[str, object]


class TurnWorldStateSnapshotAuthority:
    """Create a Turn-owned context snapshot from the append-only WorldEvent stream."""

    snapshot_kind = "personal-world-state-projection-v1"
    max_context_bytes = 16 * 1024

    def __init__(
        self,
        *,
        repository: SQLiteWorldEventRepository,
        payloads: TurnPayloadStorePort,
    ) -> None:
        self._repository = repository
        self._payloads = payloads
        self._dynamics = ProjectDynamicsEngine()

    @classmethod
    def for_root(
        cls,
        root_dir: Path,
        *,
        payloads: TurnPayloadStorePort,
    ) -> TurnWorldStateSnapshotAuthority:
        root = Path(root_dir).expanduser().resolve(strict=False)
        return cls(
            repository=SQLiteWorldEventRepository(
                SQLiteStructuredRecordStore(root / ".rebuild-data" / "jobs.sqlite3")
            ),
            payloads=payloads,
        )

    def acquire(
        self,
        request: Mapping[str, object],
        *,
        project_id: str,
        max_context_bytes: int,
    ) -> TurnWorldStateSnapshot:
        turn_id = validate_world_identifier(request.get("turn_id"), "turn id")
        project = validate_world_identifier(project_id, "project id")
        scope = request.get("scope")
        if not isinstance(scope, Mapping) or scope.get("project_id") != project:
            raise PersonalWorldModelContextError("Turn and WorldState project scopes differ")
        budget = _budget(max_context_bytes, hard_limit=self.max_context_bytes)
        captured_at = _world_time(request.get("created_at"))

        existing = self._payloads.get_immutable_payload(turn_id, self.snapshot_kind)
        if existing is not None:
            payload = _validate_snapshot(existing[1])
            _validate_identity(
                payload,
                turn_id=turn_id,
                project_id=project,
                captured_at=captured_at,
                context_budget_bytes=budget,
            )
            return _snapshot(existing[0], payload)

        events = self._repository.list_project(project)
        prefix = tuple(
            event
            for event in events
            if parse_world_timestamp(event.recorded_at) <= parse_world_timestamp(captured_at)
        )
        if prefix and tuple(item.sequence for item in prefix) != tuple(range(1, len(prefix) + 1)):
            raise PersonalWorldModelContextError("WorldState as-of snapshot is not a stream prefix")
        projection = self._dynamics.project(prefix, project_id=project, now=captured_at)
        planning = projection.planning_payload()
        provenance_refs = (
            ()
            if projection.latest_feedback is None
            else projection.latest_feedback.evidence_refs[:8]
        )
        payload = _fit_snapshot(
            turn_id=turn_id,
            project_id=project,
            captured_at=captured_at,
            through_sequence=projection.through_sequence,
            context_budget_bytes=budget,
            planning=planning,
        )
        try:
            validate_external_agent_safe_projection(payload)
            payload_ref = self._payloads.get_or_create_immutable_payload(
                turn_id, self.snapshot_kind, payload,
            )
        except Exception as error:
            raise PersonalWorldModelContextError(
                "WorldState Turn snapshot could not be frozen safely"
            ) from error
        snapshot = _snapshot(payload_ref, payload)
        return TurnWorldStateSnapshot(
            payload_ref=snapshot.payload_ref,
            revision=snapshot.revision,
            source_ref=snapshot.source_ref,
            content_bytes=snapshot.content_bytes,
            provenance_refs=provenance_refs,
            payload=snapshot.payload,
        )


def personal_world_state_snapshot_from_payload(value: object) -> dict[str, object]:
    """Validate a frozen Turn snapshot before a later context consumer uses it."""

    return _validate_snapshot(value)


def frozen_world_state_planning(
    events: Sequence[Mapping[str, object]],
    payloads: TurnPayloadStorePort,
    *,
    turn_id: str | None = None,
    project_id: str | None = None,
) -> dict[str, object] | None:
    """Read the validated WorldState entry frozen into one Turn context.

    This is a consumer view over the existing immutable Context Manifest.  It
    never rebuilds current WorldState, so a running or replayed Turn cannot see
    feedback recorded after its creation fence.
    """

    context_events = tuple(
        event for event in events if event.get("type") == "context.resolved"
    )
    if not context_events:
        return None
    if len(context_events) != 1:
        raise PersonalWorldModelContextError("Turn context authority is ambiguous")
    data = context_events[0].get("data")
    manifest_ref = data.get("payload_ref") if isinstance(data, Mapping) else None
    if not isinstance(manifest_ref, str):
        raise PersonalWorldModelContextError("Turn context manifest ref is unavailable")
    try:
        manifest = context_manifest_from_payload(payloads.get(manifest_ref))
    except Exception as error:
        raise PersonalWorldModelContextError("Turn context manifest is invalid") from error
    if turn_id is not None and manifest.turn_id != validate_world_identifier(turn_id, "turn id"):
        raise PersonalWorldModelContextError("WorldState Turn identity drifted")
    if project_id is not None and manifest.project_id != validate_world_identifier(
        project_id, "project id"
    ):
        raise PersonalWorldModelContextError("WorldState project identity drifted")
    entries = tuple(
        entry
        for entry in manifest.entries
        if entry.kind == "world_state_projection" and entry.disclosure == "model"
    )
    if not entries:
        return None
    if len(entries) != 1:
        raise PersonalWorldModelContextError("WorldState context authority is ambiguous")
    entry = entries[0]
    if entry.payload_ref is None:
        raise PersonalWorldModelContextError("WorldState context payload is unavailable")
    try:
        snapshot = _validate_snapshot(payloads.get(entry.payload_ref))
    except Exception as error:
        raise PersonalWorldModelContextError("WorldState context payload is invalid") from error
    if (
        snapshot.get("turn_id") != manifest.turn_id
        or snapshot.get("project_id") != manifest.project_id
        or entry.source_project_id != manifest.project_id
        or entry.revision_identity != snapshot.get("snapshot_revision")
        or entry.content_bytes != snapshot.get("content_bytes")
    ):
        raise PersonalWorldModelContextError("WorldState context binding drifted")
    planning = snapshot.get("planning")
    if not isinstance(planning, Mapping):
        raise PersonalWorldModelContextError("WorldState planning projection is invalid")
    return deepcopy(dict(planning))


def _snapshot(payload_ref: str, payload: Mapping[str, object]) -> TurnWorldStateSnapshot:
    through_sequence = int(payload["through_sequence"])
    project_id = str(payload["project_id"])
    planning = payload.get("planning")
    latest = planning.get("latest_feedback") if isinstance(planning, Mapping) else None
    refs = latest.get("evidence_refs") if isinstance(latest, Mapping) else None
    provenance = tuple(item for item in refs if isinstance(item, str)) if isinstance(refs, list) else ()
    return TurnWorldStateSnapshot(
        payload_ref=payload_ref,
        revision=str(payload["snapshot_revision"]),
        source_ref=f"crp://world-model/{project_id}/events/through-{through_sequence}",
        content_bytes=int(payload["content_bytes"]),
        provenance_refs=provenance[:8],
        payload=payload,
    )


def _fit_snapshot(
    *,
    turn_id: str,
    project_id: str,
    captured_at: str,
    through_sequence: int,
    context_budget_bytes: int,
    planning: Mapping[str, object],
) -> dict[str, object]:
    for level in range(6):
        payload: dict[str, object] = {
            "schema_version": "1.0.0",
            "snapshot_kind": "turn_frozen_derived_world_state",
            "snapshot_revision": f"world-s{through_sequence}-c{level}",
            "turn_id": turn_id,
            "project_id": project_id,
            "captured_at": captured_at,
            "through_sequence": through_sequence,
            "context_budget_bytes": context_budget_bytes,
            "compaction_level": level,
            "content_bytes": 0,
            "planning": _planning_variant(planning, level),
            "projection_authority": "derived_only",
        }
        _settle_content_bytes(payload)
        if int(payload["content_bytes"]) <= context_budget_bytes:
            return _validate_snapshot(payload)
    raise PersonalWorldModelContextError("WorldState projection exceeds the Turn byte budget")


def _planning_variant(value: Mapping[str, object], level: int) -> dict[str, object]:
    item = deepcopy(dict(value))
    if level == 0:
        return item
    limits = {
        1: (12, 6, 6, 8, 512),
        2: (6, 3, 3, 6, 320),
        3: (3, 2, 1, 4, 192),
        4: (1, 1, 0, 2, 112),
    }
    if level <= 4:
        task_limit, blocker_limit, observation_limit, action_limit, text_limit = limits[level]
        for field, limit in (
            ("tasks", task_limit),
            ("blockers", blocker_limit),
            ("observations", observation_limit),
            ("planned_actions", action_limit),
            ("pending_action_ids", action_limit),
            ("risk_codes", 8 if level < 3 else 4),
            ("predictions", 1),
            ("counterfactuals", 2 if level < 3 else 1),
        ):
            if isinstance(item.get(field), list):
                item[field] = item[field][:limit]
        latest = item.get("latest_feedback")
        if isinstance(latest, dict):
            if isinstance(latest.get("state_delta"), list):
                latest["state_delta"] = latest["state_delta"][: max(1, 6 - level)]
            if isinstance(latest.get("evidence_refs"), list):
                latest["evidence_refs"] = latest["evidence_refs"][: max(1, 5 - level)]
        return _truncate_strings(item, text_limit)

    latest = item.get("latest_feedback")
    compact_feedback = None
    if isinstance(latest, Mapping):
        compact_feedback = {
            "feedback_id": latest.get("feedback_id"),
            "supersedes_feedback_id": latest.get("supersedes_feedback_id"),
            "action_id": latest.get("action_id"),
            "outcome": latest.get("outcome"),
            "actual_outcome": _short_text(latest.get("actual_outcome"), 96),
            "state_delta": list(latest.get("state_delta", []))[:1],
            "cost": latest.get("cost"),
            "user_evaluation": latest.get("user_evaluation"),
            "evidence_refs": list(latest.get("evidence_refs", []))[:1],
        }
    goal = item.get("goal")
    compact_goal = None
    if isinstance(goal, Mapping):
        compact_goal = {
            "goal_id": goal.get("goal_id"),
            "title": _short_text(goal.get("title"), 64),
        }
    return {
        "schema_version": item.get("schema_version"),
        "project_id": item.get("project_id"),
        "through_sequence": item.get("through_sequence"),
        "phase": item.get("phase"),
        "goal": compact_goal,
        "pending_action_ids": list(item.get("pending_action_ids", []))[:2],
        "latest_feedback": compact_feedback,
        "confidence": item.get("confidence"),
        "risk_codes": list(item.get("risk_codes", []))[:2],
        "predictions": list(item.get("predictions", []))[:1],
        "projection_authority": "derived_only",
    }


_NARRATIVE_FIELDS = frozenset({
    "title",
    "summary",
    "expected_outcome",
    "actual_outcome",
    "note",
    "rationale",
    "condition",
    "predicted_state",
    "horizon",
    "success_criteria",
    "assumptions",
})


def _truncate_strings(
    value: object,
    maximum: int,
    *,
    field: str | None = None,
) -> object:
    if isinstance(value, dict):
        return {
            key: _truncate_strings(item, maximum, field=key)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_truncate_strings(item, maximum, field=field) for item in value]
    return (
        _short_text(value, maximum)
        if isinstance(value, str) and field in _NARRATIVE_FIELDS
        else value
    )


def _short_text(value: object, maximum: int) -> object:
    if not isinstance(value, str):
        return value
    encoded = value.encode("utf-8")
    if len(encoded) <= maximum:
        return value
    shortened = encoded[:maximum]
    while shortened:
        try:
            return shortened.decode("utf-8").rstrip() + "…"
        except UnicodeDecodeError:
            shortened = shortened[:-1]
    return "…"


def _settle_content_bytes(payload: dict[str, object]) -> None:
    for _ in range(8):
        content_bytes = len(_canonical(payload).encode("utf-8"))
        if payload["content_bytes"] == content_bytes:
            return
        payload["content_bytes"] = content_bytes
    raise PersonalWorldModelContextError("WorldState content byte accounting did not converge")


def _validate_snapshot(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != {
        "schema_version",
        "snapshot_kind",
        "snapshot_revision",
        "turn_id",
        "project_id",
        "captured_at",
        "through_sequence",
        "context_budget_bytes",
        "compaction_level",
        "content_bytes",
        "planning",
        "projection_authority",
    }:
        raise PersonalWorldModelContextError("WorldState snapshot shape is invalid")
    payload = dict(value)
    planning = payload.get("planning")
    if (
        payload.get("schema_version") != "1.0.0"
        or payload.get("snapshot_kind") != "turn_frozen_derived_world_state"
        or payload.get("projection_authority") != "derived_only"
        or not isinstance(planning, Mapping)
        or planning.get("project_id") != payload.get("project_id")
        or planning.get("through_sequence") != payload.get("through_sequence")
        or not _non_negative_int(payload.get("through_sequence"))
        or not _non_negative_int(payload.get("compaction_level"))
        or not _positive_int(payload.get("context_budget_bytes"))
        or not _positive_int(payload.get("content_bytes"))
        or int(payload["content_bytes"]) > int(payload["context_budget_bytes"])
        or int(payload["content_bytes"]) != len(_canonical(payload).encode("utf-8"))
    ):
        raise PersonalWorldModelContextError("WorldState snapshot contract drifted")
    validate_world_identifier(payload.get("turn_id"), "turn id")
    validate_world_identifier(payload.get("project_id"), "project id")
    _world_time(payload.get("captured_at"))
    return payload


def _validate_identity(
    payload: Mapping[str, object],
    *,
    turn_id: str,
    project_id: str,
    captured_at: str,
    context_budget_bytes: int,
) -> None:
    if (
        payload.get("turn_id") != turn_id
        or payload.get("project_id") != project_id
        or payload.get("captured_at") != captured_at
        or payload.get("context_budget_bytes") != context_budget_bytes
    ):
        raise PersonalWorldModelContextError("WorldState Turn snapshot identity drifted")


def _budget(value: object, *, hard_limit: int) -> int:
    if not _positive_int(value):
        raise PersonalWorldModelContextError("WorldState context budget is invalid")
    return min(int(value), hard_limit)


def _world_time(value: object) -> str:
    if not isinstance(value, str):
        raise PersonalWorldModelContextError("WorldState capture time is invalid")
    try:
        return parse_world_timestamp(value).isoformat(timespec="seconds").replace("+00:00", "Z")
    except PersonalWorldModelError as error:
        raise PersonalWorldModelContextError("WorldState capture time is invalid") from error


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _non_negative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0
