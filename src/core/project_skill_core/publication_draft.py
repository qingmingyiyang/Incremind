"""Pure, review-bound input for the future Project Skill publication adapter."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_NAMESPACE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")


class ProjectSkillPublicationDraftError(ValueError):
    """Raised when a review result cannot safely become a publication draft."""


@dataclass(frozen=True, slots=True)
class ProjectSkillPublicationTarget:
    """A caller-supplied stable identity and CAS baseline, without storage access."""

    project_id: str
    skill_id: str
    expected_revision: int

    def __post_init__(self) -> None:
        project_id = _required_identifier(self.project_id, "project_id")
        skill_id = _required_identifier(self.skill_id, "skill_id")
        expected_revision = _required_non_negative_int(self.expected_revision, "expected_revision")
        if expected_revision == 0 and skill_id != f"skill-{project_id}":
            raise ProjectSkillPublicationDraftError(
                "create target skill_id must equal the canonical skill-{project_id} identity"
            )

    @classmethod
    def for_create(cls, project_id: str) -> ProjectSkillPublicationTarget:
        normalized_project_id = _required_identifier(project_id, "project_id")
        return cls(
            project_id=normalized_project_id,
            skill_id=f"skill-{normalized_project_id}",
            expected_revision=0,
        )

    @classmethod
    def for_update(
        cls,
        *,
        project_id: str,
        skill_id: str,
        expected_revision: int,
    ) -> ProjectSkillPublicationTarget:
        if not isinstance(expected_revision, int) or isinstance(expected_revision, bool) or expected_revision < 1:
            raise ProjectSkillPublicationDraftError("update target expected_revision must be a positive integer")
        return cls(
            project_id=project_id,
            skill_id=skill_id,
            expected_revision=expected_revision,
        )


def build_project_skill_publication_draft(
    *,
    target: ProjectSkillPublicationTarget,
    source_candidate_id: str,
    proposed_content: str,
    source_refs: Sequence[Mapping[str, object]],
    evidence_refs: Sequence[Mapping[str, object]],
    reviewed_by: str,
    reviewed_at: str,
    review_reason: str,
    namespace_id: str = "default",
    current_payload: Mapping[str, object] | None = None,
    proposed_payload: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Build a deterministic, fully materialized Project Skill publication draft.

    The builder does not read a repository, a Vault, or the filesystem. The later
    composite adapter must compare this explicit CAS baseline against its
    transaction-local aggregate before it writes any publication state.
    """

    if not isinstance(target, ProjectSkillPublicationTarget):
        raise ProjectSkillPublicationDraftError("target must be a ProjectSkillPublicationTarget")
    candidate_id = _required_identifier(source_candidate_id, "source_candidate_id")
    content = _required_text(proposed_content, "proposed_content")
    normalized_source_refs = _normalized_refs(source_refs, "source_refs")
    normalized_evidence_refs = _normalized_refs(evidence_refs, "evidence_refs")
    if reviewed_by != "user":
        raise ProjectSkillPublicationDraftError("reviewed_by must be user")
    timestamp = _required_timestamp(reviewed_at, "reviewed_at")
    reason = _required_text(review_reason, "review_reason")
    namespace = _required_namespace(namespace_id)
    current = _current_payload(target, current_payload)
    revision = target.expected_revision + 1
    title = _title_from_content(content)
    review_ref = f"crp://{namespace}/memory-candidates/{candidate_id}.json#review"
    structured_payload = _structured_payload(
        target=target,
        namespace=namespace,
        candidate_id=candidate_id,
        content=content,
        source_refs=normalized_source_refs,
        evidence_refs=normalized_evidence_refs,
        reviewed_at=timestamp,
        review_reason=reason,
        revision=revision,
        title=title,
        current=current,
        proposed=proposed_payload,
    )
    material: dict[str, object] = {
        "schema_version": "1.0.0",
        "target_layer": "project_skill",
        "source_candidate_id": candidate_id,
        "project_id": target.project_id,
        "skill_id": target.skill_id,
        "expected_project_skill_revision": target.expected_revision,
        "structured_payload": structured_payload,
        "markdown": f"# {structured_payload['name']}\n\n{structured_payload['purpose']}\n",
        "review_ref": review_ref,
        "reviewed_by": reviewed_by,
        "reviewed_at": timestamp,
        "review_reason": reason,
        "source_refs": normalized_source_refs,
        "evidence_refs": normalized_evidence_refs,
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    digest = _digest(material)
    return {
        "id": f"project-skill-publication-draft-{digest[:24]}",
        **material,
        "draft_digest": digest,
    }


def validate_project_skill_publication_draft(draft: Mapping[str, object]) -> dict[str, object]:
    """Fail closed when an externally stored draft differs from builder material."""

    if not isinstance(draft, Mapping):
        raise ProjectSkillPublicationDraftError("publication draft must be an object")
    normalized = _json_object(draft, "publication draft")
    digest = normalized.get("draft_digest")
    if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise ProjectSkillPublicationDraftError("draft_digest is invalid")
    if normalized.get("id") != f"project-skill-publication-draft-{digest[:24]}":
        raise ProjectSkillPublicationDraftError("publication draft id does not match digest")
    material = {key: value for key, value in normalized.items() if key not in {"id", "draft_digest"}}
    if _digest(material) != digest:
        raise ProjectSkillPublicationDraftError("publication draft digest drifted")
    structured = _json_object(normalized.get("structured_payload"), "structured_payload")
    project_id = _required_identifier(normalized.get("project_id"), "project_id")
    skill_id = _required_identifier(normalized.get("skill_id"), "skill_id")
    expected_revision = _required_non_negative_int(
        normalized.get("expected_project_skill_revision"), "expected_project_skill_revision"
    )
    if structured.get("id") != skill_id or structured.get("project_id") != project_id:
        raise ProjectSkillPublicationDraftError("structured payload identity drifted")
    if structured.get("status") != "draft":
        raise ProjectSkillPublicationDraftError("structured payload must be draft")
    for key in ("revision", "markdown_revision", "json_revision"):
        if structured.get(key) != expected_revision + 1:
            raise ProjectSkillPublicationDraftError(f"structured payload {key} drifted")
    for key in ("source_refs", "evidence_refs"):
        structured_refs = structured.get(key)
        draft_refs = normalized.get(key)
        if (
            not isinstance(structured_refs, list)
            or not isinstance(draft_refs, list)
            or any(reference not in structured_refs for reference in draft_refs)
        ):
            raise ProjectSkillPublicationDraftError("structured payload refs drifted")
    if normalized.get("markdown") != f"# {structured.get('name')}\n\n{structured.get('purpose')}\n":
        raise ProjectSkillPublicationDraftError("publication draft markdown drifted")
    return normalized


def _structured_payload(
    *,
    target: ProjectSkillPublicationTarget,
    namespace: str,
    candidate_id: str,
    content: str,
    source_refs: list[dict[str, str]],
    evidence_refs: list[dict[str, str]],
    reviewed_at: str,
    review_reason: str,
    revision: int,
    title: str,
    current: dict[str, object] | None,
    proposed: Mapping[str, object] | None,
) -> dict[str, object]:
    normalized_proposed = _json_object(proposed, "proposed_payload") if proposed is not None else {}
    required_context = _merge_sequence(
        normalized_proposed.get(
            "required_context",
            current.get("required_context") if current is not None else None,
        ),
        {
            "context_id": f"context-{candidate_id}",
            "kind": "source",
            "object_id": source_refs[0]["source_id"],
            "uri": f"crp://{namespace}/sources/{source_refs[0]['source_id']}.json",
            "reason": review_reason,
            "stale": False,
        },
    )
    proposed_rules = normalized_proposed.get("output_rules")
    if proposed_rules is not None:
        if not isinstance(proposed_rules, list) or not all(isinstance(item, Mapping) for item in proposed_rules):
            raise ProjectSkillPublicationDraftError("proposed output_rules must be a list of objects")
        output_rules = [
            _normalized_proposed_rule(item, source_refs=source_refs, index=index)
            for index, item in enumerate(proposed_rules)
        ]
    else:
        output_rules = _merge_sequence(
            current.get("output_rules") if current is not None else None,
            {
                "rule_id": f"rule-{candidate_id}",
                "origin": "ai",
                "rule": content,
                "priority": "must",
                "source_refs": source_refs,
                "locked_by_user": False,
            },
        )
    decision_log = _merge_sequence(
        normalized_proposed.get(
            "decision_log",
            current.get("decision_log") if current is not None else None,
        ),
        {
            "decision_id": f"decision-{candidate_id}-r{revision}",
            "reason": review_reason,
            "actor": "user",
            "created_at": reviewed_at,
        },
    )
    merged_source_refs = _merge_refs(
        normalized_proposed.get(
            "source_refs",
            current.get("source_refs") if current is not None else None,
        ),
        source_refs,
    )
    merged_evidence_refs = _merge_refs(
        normalized_proposed.get(
            "evidence_refs",
            current.get("evidence_refs") if current is not None else None,
        ),
        evidence_refs,
    )
    payload = dict(current or {})
    payload.update({
        "schema_version": "1.0.0",
        "id": target.skill_id,
        "project_id": target.project_id,
        "name": _required_text(normalized_proposed.get("name", title), "proposed name"),
        "purpose": _required_text(normalized_proposed.get("purpose", content), "proposed purpose"),
        "markdown_uri": f"crp://{namespace}/projects/{target.project_id}/project-skill.md",
        "json_uri": f"crp://{namespace}/projects/{target.project_id}/project-skill.json",
        "markdown_revision": revision,
        "json_revision": revision,
        "required_context": required_context,
        "output_rules": output_rules,
        "style_preferences": normalized_proposed.get("style_preferences", current.get("style_preferences") if current is not None else {
            "voice": "直接、具体、可执行",
            "format_defaults": ["Markdown", "分节标题", "来源引用"],
        }),
        "update_rules": normalized_proposed.get("update_rules", current.get("update_rules") if current is not None else {
            "patch_strategy": "patch_existing_first",
            "user_edit_policy": "user_wins",
            "allowed_auto_updates": [],
        }),
        "source_refs": merged_source_refs,
        "evidence_refs": merged_evidence_refs,
        "decision_log": decision_log,
        "conflict": {"status": "none", "conflict_refs": [], "resolution": None},
        "revision": revision,
        "status": "draft",
        "trust_status": "system_generated",
        "created_at": current.get("created_at", reviewed_at) if current is not None else reviewed_at,
        "updated_at": reviewed_at,
    })
    if "outline" in normalized_proposed:
        payload["outline"] = normalized_proposed["outline"]
    return payload


def _normalized_proposed_rule(
    value: Mapping[str, object],
    *,
    source_refs: list[dict[str, str]],
    index: int,
) -> dict[str, object]:
    rule = _json_object(value, f"proposed output_rules[{index}]")
    rule_id = _required_identifier(rule.get("rule_id"), f"proposed output_rules[{index}].rule_id")
    text = _required_text(rule.get("rule"), f"proposed output_rules[{index}].rule")
    origin = rule.get("origin")
    locked_by_user = rule.get("locked_by_user")
    if origin not in {"ai", "user"}:
        raise ProjectSkillPublicationDraftError(f"proposed output_rules[{index}].origin must be ai or user")
    if rule.get("priority") not in {"must", "should"}:
        raise ProjectSkillPublicationDraftError(f"proposed output_rules[{index}].priority is invalid")
    if locked_by_user is not (origin == "user"):
        raise ProjectSkillPublicationDraftError(
            f"proposed output_rules[{index}].locked_by_user must match user origin"
        )
    return {
        "rule_id": rule_id,
        "origin": origin,
        "rule": text,
        "priority": rule["priority"],
        "source_refs": _merge_refs(rule.get("source_refs"), source_refs),
        "locked_by_user": locked_by_user,
    }


def _current_payload(
    target: ProjectSkillPublicationTarget,
    value: Mapping[str, object] | None,
) -> dict[str, object] | None:
    if target.expected_revision == 0:
        if value is not None:
            raise ProjectSkillPublicationDraftError("create target cannot include current_payload")
        return None
    if value is None:
        raise ProjectSkillPublicationDraftError("update target requires current_payload")
    current = _json_object(value, "current_payload")
    if (
        current.get("id") != target.skill_id
        or current.get("project_id") != target.project_id
        or current.get("revision") != target.expected_revision
    ):
        raise ProjectSkillPublicationDraftError("current_payload identity or revision drifted")
    return current


def _merge_sequence(value: object, appended: Mapping[str, object]) -> list[dict[str, object]]:
    existing = [] if value is None else value
    if not isinstance(existing, list) or not all(isinstance(item, Mapping) for item in existing):
        raise ProjectSkillPublicationDraftError("current Project Skill sequence is invalid")
    result = [_json_object(item, "current Project Skill sequence item") for item in existing]
    normalized = _json_object(appended, "appended Project Skill sequence item")
    if normalized not in result:
        result.append(normalized)
    return result


def _merge_refs(value: object, appended: list[dict[str, str]]) -> list[dict[str, object]]:
    existing = [] if value is None else value
    if not isinstance(existing, list) or not all(isinstance(item, Mapping) for item in existing):
        raise ProjectSkillPublicationDraftError("current Project Skill refs are invalid")
    result = [_json_object(item, "current Project Skill ref") for item in existing]
    for reference in appended:
        normalized = _json_object(reference, "appended Project Skill ref")
        if normalized not in result:
            result.append(normalized)
    return result


def _normalized_refs(
    refs: Sequence[Mapping[str, object]], key: str) -> list[dict[str, str]]:
    if isinstance(refs, (str, bytes)) or not isinstance(refs, Sequence) or not refs:
        raise ProjectSkillPublicationDraftError(f"{key} must contain at least one source reference")
    normalized: list[dict[str, str]] = []
    for index, reference in enumerate(refs):
        if not isinstance(reference, Mapping):
            raise ProjectSkillPublicationDraftError(f"{key}[{index}] must be an object")
        normalized.append(
            {
                "source_id": _required_text(reference.get("source_id"), f"{key}[{index}].source_id"),
                "locator": _required_text(reference.get("locator"), f"{key}[{index}].locator"),
            }
        )
    return normalized


def _digest(material: Mapping[str, object]) -> str:
    try:
        serialized = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as error:
        raise ProjectSkillPublicationDraftError("publication draft must be JSON serializable") from error
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _json_object(value: object, key: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ProjectSkillPublicationDraftError(f"{key} must be an object")
    try:
        normalized = json.loads(json.dumps(dict(value), ensure_ascii=False, sort_keys=True))
    except (TypeError, ValueError) as error:
        raise ProjectSkillPublicationDraftError(f"{key} must be JSON serializable") from error
    if not isinstance(normalized, dict):
        raise ProjectSkillPublicationDraftError(f"{key} must be an object")
    return dict(normalized)


def _required_identifier(value: object, key: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ProjectSkillPublicationDraftError(f"{key} must be a stable identifier")
    return value


def _required_namespace(value: object) -> str:
    if not isinstance(value, str) or not _NAMESPACE.fullmatch(value):
        raise ProjectSkillPublicationDraftError("namespace_id must be a CRP namespace")
    return value


def _required_non_negative_int(value: object, key: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ProjectSkillPublicationDraftError(f"{key} must be a non-negative integer")
    return value


def _required_text(value: object, key: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProjectSkillPublicationDraftError(f"{key} must be non-empty text")
    return value.strip()


def _required_timestamp(value: object, key: str) -> str:
    timestamp = _required_text(value, key)
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as error:
        raise ProjectSkillPublicationDraftError(f"{key} must be an ISO 8601 timestamp") from error
    if parsed.tzinfo is None:
        raise ProjectSkillPublicationDraftError(f"{key} must include a timezone")
    return timestamp


def _title_from_content(content: str) -> str:
    return " ".join(content.split())[:60]
