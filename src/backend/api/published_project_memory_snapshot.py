"""Freeze safe, project-scoped published memory for one Turn.

This authority intentionally sits before Context Manifest composition.  It reads
only the currently selected aggregate authorities and stores a compact,
redacted snapshot in the Turn immutable payload store.  Only stable project
guidance and L1 atoms may become model context.  L2 scenarios and L3 series
memory remain authoritative records, but are deliberately excluded here and
must be reached through the bounded recall/drilldown capability.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json

from core.aggregate_repository_factory import AggregateRepositoryFactory
from core.ai_kernel import TurnPayloadStorePort, validate_external_agent_safe_projection
from core.memory_core import ObjectStoreMemoryStore, SQLiteMemoryReader
from core.product_core.memory_quality_gate import redact_content


class PublishedProjectMemorySnapshotError(ValueError):
    """Raised when a published project-memory snapshot cannot be frozen safely."""


@dataclass(frozen=True, slots=True)
class PublishedProjectMemorySnapshot:
    payload_ref: str
    revision: str
    payload: Mapping[str, object]


class PublishedProjectMemorySnapshotAuthority:
    """Select redacted, trusted same-project published records for a Turn.

    The authority deliberately records no source URLs, locators, evidence
    quotes, filesystem paths, secret fields, or raw storage payloads.  It is a
    read-only precursor to Context Router composition, not another Memory
    authority and not a Context Manifest writer.
    """

    snapshot_kind = "published-project-memory-snapshot-v1"
    max_context_bytes = 32 * 1024
    max_entries = 12
    _trusted = frozenset({"trusted", "user_confirmed", "system_generated"})
    _layer_order = {
        "project_skill": 0,
        "l3_series_memory": 1,
        "l2_scenario": 2,
        "l1_atom": 3,
    }
    _manifest_kind = {
        "project_skill": "project_skill",
        "l3_series_memory": "memory_r3",
        "l2_scenario": "memory_r2",
        "l1_atom": "memory_r1",
    }
    # Project Skill is the existing independent project-guidance contract.  It
    # is intentionally not folded into Memory authority.  The remaining model
    # memory layer is L1 only; L2/L3 are dynamic material and would destabilise
    # the reusable prompt prefix if injected automatically.
    _model_injected_kinds = frozenset({"project_skill", "l1_atom"})
    model_injected_manifest_kinds = frozenset({"project_skill", "memory_r1"})

    def __init__(
        self,
        *,
        factory: AggregateRepositoryFactory,
        payloads: TurnPayloadStorePort,
    ) -> None:
        self._factory = factory
        self._payloads = payloads

    def acquire(
        self,
        request: Mapping[str, object],
        *,
        project_id: str,
        profile_id: str,
        profile_revision: int,
        max_context_bytes: int | None = None,
    ) -> PublishedProjectMemorySnapshot:
        turn_id = _text(request.get("turn_id"), "Turn id")
        project_id = _text(project_id, "Project id")
        profile_id = _text(profile_id, "Project profile id")
        _integer(profile_revision, "Project profile revision", minimum=0)
        budget = self._budget(request, max_context_bytes)
        memory_resolution = self._factory.memory_publication_authority_resolution()
        skill_resolution = self._factory.project_skill_repository_resolution()
        existing = self._payloads.get_immutable_payload(turn_id, self.snapshot_kind)
        if existing is not None:
            payload = _validate_snapshot(existing[1])
            _validate_identity(
                payload,
                turn_id=turn_id,
                project_id=project_id,
                profile_id=profile_id,
                profile_revision=profile_revision,
                memory_authority_identity=memory_resolution.authority_identity,
                project_skill_authority_identity=skill_resolution.authority_identity,
                context_budget_bytes=budget,
            )
            return PublishedProjectMemorySnapshot(
                existing[0], str(payload["snapshot_revision"]), payload,
            )

        memory = (
            SQLiteMemoryReader(memory_resolution.records)
            if memory_resolution.records is not None
            else ObjectStoreMemoryStore(self._factory.json_store)
        )
        candidates, excluded = self._candidates(
            project_id=project_id,
            skill=skill_resolution.repository.load(project_id),
            memories=memory.list_by_project(project_id),
        )
        selected, budget_excluded = _apply_budget(
            candidates, max_entries=self.max_entries, max_bytes=budget,
        )
        excluded.extend(budget_excluded)
        excluded.sort(key=lambda item: (str(item["kind"]), str(item["object_id"]), str(item["reason"])))
        selected_refs = self._write_selected_payloads(
            turn_id=turn_id, project_id=project_id, selected=selected,
        )
        snapshot_revision = _revision(
            memory_authority_identity=memory_resolution.authority_identity,
            project_skill_authority_identity=skill_resolution.authority_identity,
            profile_revision=profile_revision,
            selected_count=len(selected_refs),
        )
        payload = _validate_snapshot({
            "schema_version": "1.0.0",
            "turn_id": turn_id,
            "project_id": project_id,
            "profile_id": profile_id,
            "profile_revision": profile_revision,
            "memory_authority_identity": memory_resolution.authority_identity,
            "project_skill_authority_identity": skill_resolution.authority_identity,
            "snapshot_revision": snapshot_revision,
            "context_budget_bytes": budget,
            "selected_context_bytes": sum(_integer(item["context_bytes"], "entry context bytes", minimum=0) for item in selected_refs),
            "selected": selected_refs,
            "excluded": excluded,
        })
        payload_ref = self._payloads.get_or_create_immutable_payload(
            turn_id, self.snapshot_kind, payload,
        )
        return PublishedProjectMemorySnapshot(payload_ref, snapshot_revision, payload)

    def _write_selected_payloads(
        self, *, turn_id: str, project_id: str, selected: Sequence[Mapping[str, object]],
    ) -> list[dict[str, object]]:
        result: list[dict[str, object]] = []
        for item in selected:
            kind = _text(item.get("kind"), "Published memory entry kind")
            manifest_kind = self._manifest_kind[kind]
            entry_payload = _validate_entry_payload({
                "schema_version": "1.0.0",
                "kind": manifest_kind,
                "project_id": project_id,
                "object_id": item["object_id"],
                "revision": str(item["revision"]),
                "trust_status": item["trust_status"],
                "markdown": item["content"],
            })
            payload_ref = self._payloads.get_or_create_immutable_payload(
                turn_id,
                (
                    "published-project-memory-item-v1-"
                    f"{manifest_kind}-{entry_payload['object_id']}-r{entry_payload['revision']}"
                ),
                entry_payload,
            )
            result.append({
                "manifest_kind": manifest_kind,
                "object_id": entry_payload["object_id"],
                "revision": item["revision"],
                "trust_status": entry_payload["trust_status"],
                "payload_ref": payload_ref,
                "context_bytes": item["context_bytes"],
            })
        return result

    def _budget(
        self, request: Mapping[str, object], requested: int | None,
    ) -> int:
        if requested is None:
            policy = request.get("context_policy")
            requested = policy.get("max_context_bytes") if isinstance(policy, Mapping) else None
        value = _integer(requested, "Published memory context budget", minimum=0)
        return min(value, self.max_context_bytes)

    def _candidates(
        self,
        *,
        project_id: str,
        skill: Mapping[str, object] | None,
        memories: Sequence[Mapping[str, object]],
    ) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        selected: list[dict[str, object]] = []
        excluded: list[dict[str, object]] = []
        if skill is not None:
            outcome = _project_skill_entry(project_id, skill, self._trusted)
            (selected if outcome[0] is not None else excluded).append(outcome[0] or outcome[1])
        for memory in memories:
            outcome = _memory_entry(project_id, memory, self._trusted)
            candidate, exclusion = outcome
            if candidate is None:
                excluded.append(exclusion)
            elif candidate["kind"] in self._model_injected_kinds:
                selected.append(candidate)
            else:
                # Do not freeze an opaque L2/L3 body merely because it is
                # published.  Progressive recall owns their query, project
                # authorization and time/size budgets.
                excluded.append(_excluded(
                    str(candidate["kind"]), str(candidate["object_id"]),
                    "tool_recall_required",
                ))
        selected.sort(
            key=lambda item: (
                self._layer_order[str(item["kind"])],
                -_integer(item["revision"], "entry revision", minimum=0),
                str(item["object_id"]),
            )
        )
        return selected, excluded


def published_project_memory_snapshot_from_payload(value: object) -> dict[str, object]:
    """Validate a frozen snapshot before a later model-context consumer uses it."""

    return _validate_snapshot(value)


def published_project_memory_entry_from_payload(value: object) -> dict[str, object]:
    """Validate one safe, model-readable selected-memory payload."""

    return _validate_entry_payload(value)


def _project_skill_entry(
    project_id: str, skill: Mapping[str, object], trusted: frozenset[str],
) -> tuple[dict[str, object] | None, dict[str, object]]:
    object_id = _safe_object_id(skill.get("id"), "project-skill")
    reason = _ineligible_reason(project_id, skill, trusted, project_skill=True)
    if reason is not None:
        return None, _excluded("project_skill", object_id, reason)
    purpose = _text(skill.get("purpose"), "Project Skill purpose")
    rules = skill.get("output_rules")
    first_rule = ""
    if isinstance(rules, Sequence) and not isinstance(rules, (str, bytes)):
        for rule in rules:
            if isinstance(rule, Mapping) and isinstance(rule.get("rule"), str) and rule["rule"].strip():
                first_rule = rule["rule"].strip()
                break
    return _entry(
        kind="project_skill", object_id=object_id, revision=_integer(skill.get("revision"), "Project Skill revision", minimum=1),
        trust_status=str(skill["trust_status"]), content="\n".join(part for part in (purpose, first_rule) if part),
    ), _excluded("project_skill", object_id, "selected")


def _memory_entry(
    project_id: str, item: Mapping[str, object], trusted: frozenset[str],
) -> tuple[dict[str, object] | None, dict[str, object]]:
    object_id = _safe_object_id(item.get("id"), "memory")
    kind = _memory_kind(item)
    if kind is None:
        return None, _excluded("memory", object_id, "unsupported_layer")
    reason = _ineligible_reason(project_id, item, trusted, project_skill=False)
    if reason is not None:
        return None, _excluded(kind, object_id, reason)
    content_key = {"l3_series_memory": "overview", "l2_scenario": "summary", "l1_atom": "content"}[kind]
    return _entry(
        kind=kind, object_id=object_id, revision=_integer(item.get("revision"), "Memory revision", minimum=1),
        trust_status=str(item["trust_status"]), content=_text(item.get(content_key), f"Memory {content_key}"),
    ), _excluded(kind, object_id, "selected")


def _ineligible_reason(
    project_id: str, item: Mapping[str, object], trusted: frozenset[str], *, project_skill: bool,
) -> str | None:
    if not _belongs_to_project(item, project_id, project_skill=project_skill):
        return "cross_project"
    if item.get("stale") is True:
        return "stale"
    if str(item.get("trust_status", "")) not in trusted:
        return "untrusted"
    if project_skill and item.get("status") != "active":
        return "inactive"
    conflict = item.get("conflict")
    if isinstance(conflict, Mapping) and conflict.get("status") not in {None, "none"}:
        return "conflict"
    if item.get("conflict_status") not in {None, "none"}:
        return "conflict"
    if not _has_source_refs(item, project_skill=project_skill):
        return "missing_evidence"
    return None


def _belongs_to_project(item: Mapping[str, object], project_id: str, *, project_skill: bool) -> bool:
    if project_skill:
        return item.get("project_id") == project_id
    direct = item.get("project_id")
    if isinstance(direct, str) and direct:
        return direct == project_id
    project_ids = item.get("project_ids")
    return isinstance(project_ids, Sequence) and not isinstance(project_ids, (str, bytes)) and project_id in project_ids


def _has_source_refs(item: Mapping[str, object], *, project_skill: bool) -> bool:
    for field in (("source_refs", "evidence_refs", "output_rules") if project_skill else ("source_refs",)):
        value = item.get(field)
        if field == "output_rules" and isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            if any(isinstance(rule, Mapping) and _has_source_refs(rule, project_skill=False) for rule in value):
                return True
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)) and any(isinstance(ref, Mapping) for ref in value):
            return True
    return False


def _memory_kind(item: Mapping[str, object]) -> str | None:
    if "overview" in item and "scenario_ids" in item and "project_ids" in item:
        return "l3_series_memory"
    if "summary" in item and "atom_ids" in item:
        return "l2_scenario"
    if "content" in item and "atom_type" in item:
        return "l1_atom"
    return None


def _entry(*, kind: str, object_id: str, revision: int, trust_status: str, content: str) -> dict[str, object]:
    safe_content = redact_content(content)
    try:
        validate_external_agent_safe_projection({"markdown": safe_content})
    except ValueError as error:
        raise PublishedProjectMemorySnapshotError(
            "Published Project Memory content is unsafe"
        ) from error
    return {
        "kind": kind,
        "object_id": object_id,
        "revision": revision,
        "trust_status": trust_status,
        "content": safe_content,
        "context_bytes": len(safe_content.encode("utf-8")),
    }


def _excluded(kind: str, object_id: str, reason: str) -> dict[str, object]:
    return {"kind": kind, "object_id": object_id, "reason": reason}


def _apply_budget(
    candidates: Sequence[dict[str, object]], *, max_entries: int, max_bytes: int,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    selected: list[dict[str, object]] = []
    excluded: list[dict[str, object]] = []
    used = 0
    for item in candidates:
        size = _integer(item["context_bytes"], "entry context bytes", minimum=0)
        if len(selected) >= max_entries or used + size > max_bytes:
            excluded.append(_excluded(str(item["kind"]), str(item["object_id"]), "budget"))
            continue
        selected.append(item)
        used += size
    return selected, excluded


def _revision(
    *, memory_authority_identity: str, project_skill_authority_identity: str,
    profile_revision: int, selected_count: int,
) -> str:
    """Stable audit identity without inventing a second content-hash authority."""

    return (
        "published-memory/"
        f"memory={memory_authority_identity}/skill={project_skill_authority_identity}/"
        f"profile={profile_revision}/selected={selected_count}"
    )


def _validate_entry_payload(value: object) -> dict[str, object]:
    fields = {"schema_version", "kind", "project_id", "object_id", "revision", "trust_status", "markdown"}
    if not isinstance(value, Mapping) or set(value) != fields or value.get("schema_version") != "1.0.0":
        raise PublishedProjectMemorySnapshotError("Published Project Memory entry payload shape is invalid")
    payload = json.loads(json.dumps(dict(value), ensure_ascii=False))
    if payload.get("kind") not in set(PublishedProjectMemorySnapshotAuthority._manifest_kind.values()):
        raise PublishedProjectMemorySnapshotError("Published Project Memory entry payload kind is invalid")
    for field in ("project_id", "object_id", "trust_status", "markdown"):
        _text(payload.get(field), f"Published Memory entry payload {field}")
    revision = _text(payload.get("revision"), "Published Memory entry payload revision")
    if not revision.isdigit() or int(revision) < 1:
        raise PublishedProjectMemorySnapshotError("Published Project Memory entry payload revision is invalid")
    if payload["trust_status"] not in PublishedProjectMemorySnapshotAuthority._trusted:
        raise PublishedProjectMemorySnapshotError("Published Project Memory entry payload trust is invalid")
    if payload["markdown"] != redact_content(payload["markdown"]):
        raise PublishedProjectMemorySnapshotError("Published Project Memory entry payload contains sensitive content")
    try:
        validate_external_agent_safe_projection(payload)
    except ValueError as error:
        raise PublishedProjectMemorySnapshotError(
            "Published Project Memory entry payload contains unsafe content"
        ) from error
    return payload


def _validate_snapshot(value: object) -> dict[str, object]:
    fields = {
        "schema_version", "turn_id", "project_id", "profile_id", "profile_revision",
        "memory_authority_identity", "project_skill_authority_identity", "snapshot_revision",
        "context_budget_bytes", "selected_context_bytes", "selected", "excluded",
    }
    if not isinstance(value, Mapping) or set(value) != fields or value.get("schema_version") != "1.0.0":
        raise PublishedProjectMemorySnapshotError("Published Project Memory Snapshot shape is invalid")
    payload = json.loads(json.dumps(dict(value), ensure_ascii=False))
    for field in (
        "turn_id", "project_id", "profile_id", "memory_authority_identity",
        "project_skill_authority_identity", "snapshot_revision",
    ):
        _text(payload.get(field), f"Published Memory Snapshot {field}")
    _integer(payload.get("profile_revision"), "Published Memory Snapshot profile revision", minimum=0)
    budget = _integer(payload.get("context_budget_bytes"), "Published Memory Snapshot context budget")
    selected = payload.get("selected")
    excluded = payload.get("excluded")
    if not isinstance(selected, list) or len(selected) > PublishedProjectMemorySnapshotAuthority.max_entries or not isinstance(excluded, list):
        raise PublishedProjectMemorySnapshotError("Published Project Memory Snapshot collections are invalid")
    total = 0
    prior_key: tuple[int, int, str] | None = None
    identities: set[tuple[str, str]] = set()
    for item in selected:
        if not isinstance(item, Mapping) or set(item) != {"manifest_kind", "object_id", "revision", "trust_status", "payload_ref", "context_bytes"}:
            raise PublishedProjectMemorySnapshotError("Published Project Memory Snapshot entry is invalid")
        manifest_kind = _text(item.get("manifest_kind"), "Published Memory entry manifest kind")
        if manifest_kind not in PublishedProjectMemorySnapshotAuthority._manifest_kind.values():
            raise PublishedProjectMemorySnapshotError("Published Project Memory Snapshot layer is invalid")
        kind = next(
            source_kind
            for source_kind, mapped in PublishedProjectMemorySnapshotAuthority._manifest_kind.items()
            if mapped == manifest_kind
        )
        object_id = _text(item.get("object_id"), "Published Memory entry id")
        identity = (kind, object_id)
        if identity in identities:
            raise PublishedProjectMemorySnapshotError("Published Project Memory Snapshot identity drifted")
        identities.add(identity)
        revision = _integer(item.get("revision"), "Published Memory entry revision", minimum=1)
        trust = _text(item.get("trust_status"), "Published Memory entry trust")
        if trust not in PublishedProjectMemorySnapshotAuthority._trusted:
            raise PublishedProjectMemorySnapshotError("Published Project Memory Snapshot trust is invalid")
        payload_ref = _text(item.get("payload_ref"), "Published Memory entry payload ref")
        if not payload_ref.startswith(f"crp://session/{payload['turn_id']}/"):
            raise PublishedProjectMemorySnapshotError("Published Project Memory Snapshot payload ref is invalid")
        context_bytes = _integer(item.get("context_bytes"), "Published Memory entry bytes", minimum=0)
        key = (PublishedProjectMemorySnapshotAuthority._layer_order[kind], -revision, object_id)
        if prior_key is not None and key < prior_key:
            raise PublishedProjectMemorySnapshotError("Published Project Memory Snapshot ordering drifted")
        prior_key = key
        total += context_bytes
    if total != _integer(payload.get("selected_context_bytes"), "Published Memory Snapshot selected bytes", minimum=0):
        raise PublishedProjectMemorySnapshotError("Published Project Memory Snapshot byte total drifted")
    if total > budget or budget > PublishedProjectMemorySnapshotAuthority.max_context_bytes:
        raise PublishedProjectMemorySnapshotError("Published Project Memory Snapshot exceeds its budget")
    for item in excluded:
        if not isinstance(item, Mapping) or set(item) != {"kind", "object_id", "reason"}:
            raise PublishedProjectMemorySnapshotError("Published Project Memory Snapshot exclusion is invalid")
        _text(item.get("kind"), "Published Memory exclusion kind")
        _text(item.get("object_id"), "Published Memory exclusion id")
        _text(item.get("reason"), "Published Memory exclusion reason")
    return payload


def _validate_identity(
    payload: Mapping[str, object], *, turn_id: str, project_id: str, profile_id: str,
    profile_revision: int, memory_authority_identity: str,
    project_skill_authority_identity: str, context_budget_bytes: int,
) -> None:
    if (
        payload.get("turn_id") != turn_id or payload.get("project_id") != project_id
        or payload.get("profile_id") != profile_id or payload.get("profile_revision") != profile_revision
        or payload.get("memory_authority_identity") != memory_authority_identity
        or payload.get("project_skill_authority_identity") != project_skill_authority_identity
        or payload.get("context_budget_bytes") != context_budget_bytes
    ):
        raise PublishedProjectMemorySnapshotError("Published Project Memory Snapshot authority drifted")


def _safe_object_id(value: object, fallback: str) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else fallback


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PublishedProjectMemorySnapshotError(f"{label} must be non-empty")
    return value.strip()


def _integer(value: object, label: str, *, minimum: int = 1) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise PublishedProjectMemorySnapshotError(f"{label} is invalid")
    return value
