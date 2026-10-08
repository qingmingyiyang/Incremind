from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass

from .persona import (
    ObjectStorePersonaRepository,
    PersonaError,
    PersonaEvidenceRef,
    PersonaRecord,
    PersonaStatement,
)


class ExternalPersonaCandidateStagingError(PersonaError):
    """Raised when an imported candidate cannot safely become an L4 draft."""


@dataclass(frozen=True, slots=True)
class StageExternalPersonaCandidate:
    repository: ObjectStorePersonaRepository
    now: str

    def execute(
        self,
        candidate: Mapping[str, object],
        *,
        scope: str,
        expected_draft_revision: int,
        expected_current_revision: int,
    ) -> Mapping[str, object]:
        if scope not in {"global", "series", "project"}:
            raise ExternalPersonaCandidateStagingError(
                "external Persona candidate scope is invalid"
            )
        if candidate.get("type") not in {"persona", "preference", "rule"}:
            raise ExternalPersonaCandidateStagingError(
                "external candidate is not a Persona candidate"
            )
        if candidate.get("target_layer") not in {None, "persona"}:
            raise ExternalPersonaCandidateStagingError(
                "external Persona candidate target layer drifted"
            )
        if candidate.get("layer") not in {"L3", "L4"}:
            raise ExternalPersonaCandidateStagingError(
                "external Persona candidate must target L4"
            )
        status = candidate.get("status")
        if status not in {"candidate", "needs_review", "pending_review"}:
            raise ExternalPersonaCandidateStagingError(
                "external Persona candidate is not pending review"
            )
        content = _required_text(
            candidate.get("proposed_content") or candidate.get("content"),
            "external Persona candidate content",
        )
        candidate_id = _required_text(
            candidate.get("id") or candidate.get("memory_id"),
            "external Persona candidate id",
        )
        source_id = _candidate_source_id(candidate)
        source_refs = candidate.get("source_refs")
        normalized_source_refs = _source_refs(source_refs, source_id)
        draft = self.repository.get_draft(scope)
        statements = _existing_statements(draft)
        statement_id = "persona-statement-external-" + hashlib.sha256(
            candidate_id.encode("utf-8")
        ).hexdigest()[:16]
        if all(statement.id != statement_id for statement in statements):
            statements.append(
                PersonaStatement(
                    id=statement_id,
                    content=content,
                    category=_category(content),
                    confidence=_confidence(candidate.get("confidence")),
                )
            )
        evidence_refs = _existing_evidence_refs(draft)
        if all(
            ref.object_type != "source" or ref.object_id != source_id
            for ref in evidence_refs
        ):
            evidence_refs.append(
                PersonaEvidenceRef(
                    object_type="source",
                    object_id=source_id,
                    source_refs=normalized_source_refs,
                )
            )
        current = self.repository.get(scope)
        current_revision = (
            current.get("revision") if isinstance(current, Mapping) else 0
        )
        if (
            not isinstance(current_revision, int)
            or isinstance(current_revision, bool)
            or current_revision < 0
        ):
            current_revision = 0
        created_at = (
            draft.get("created_at")
            if isinstance(draft, Mapping)
            and isinstance(draft.get("created_at"), str)
            and draft.get("created_at")
            else self.now
        )
        record = PersonaRecord(
            id=f"persona-{scope}",
            scope=scope,
            statements=tuple(statements),
            evidence_refs=tuple(evidence_refs),
            confirmation={
                "required": True,
                "status": "pending",
                "actor": None,
                "reason": "外部导入的 L4 Persona 候选等待用户确认。",
            },
            revision=current_revision + 1,
            trust_status="system_generated",
            created_at=str(created_at),
            updated_at=self.now,
        )
        return self.repository.save(
            record,
            actor="user",
            reason=f"外部 Persona 候选 {candidate_id} 进入 L4 草稿",
            now=self.now,
            expected_draft_revision=expected_draft_revision,
            expected_current_revision=expected_current_revision,
        )


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExternalPersonaCandidateStagingError(f"{label} is required")
    return value.strip()


def _candidate_source_id(candidate: Mapping[str, object]) -> str:
    source_id = candidate.get("source_id")
    if isinstance(source_id, str) and source_id.strip():
        return source_id.strip()
    source_ref = candidate.get("source_ref")
    if (
        isinstance(source_ref, str)
        and source_ref.startswith("crp://")
        and "/sources/" in source_ref
    ):
        derived = source_ref.rsplit("/", 1)[-1].strip()
        if derived:
            return derived
    raise ExternalPersonaCandidateStagingError(
        "external Persona candidate source id is required"
    )


def _source_refs(
    value: object,
    source_id: str,
) -> tuple[Mapping[str, object], ...]:
    refs: list[Mapping[str, object]] = []
    if isinstance(value, list):
        for item in value:
            if not isinstance(item, Mapping):
                continue
            item_source_id = item.get("source_id")
            locator = item.get("locator")
            if item_source_id == source_id and isinstance(locator, str) and locator:
                refs.append({"source_id": source_id, "locator": locator})
    if not refs:
        refs.append({"source_id": source_id, "locator": f"source:{source_id}"})
    return tuple(refs)


def _existing_statements(
    draft: Mapping[str, object] | None,
) -> list[PersonaStatement]:
    result: list[PersonaStatement] = []
    raw = draft.get("statements") if isinstance(draft, Mapping) else None
    if not isinstance(raw, list):
        return result
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        statement_id = item.get("id")
        content = item.get("content")
        category = item.get("category")
        confidence = item.get("confidence")
        if (
            isinstance(statement_id, str)
            and statement_id
            and isinstance(content, str)
            and content
            and isinstance(category, str)
            and isinstance(confidence, (int, float))
            and not isinstance(confidence, bool)
        ):
            result.append(
                PersonaStatement(
                    statement_id,
                    content,
                    category,
                    float(confidence),
                )
            )
    return result


def _existing_evidence_refs(
    draft: Mapping[str, object] | None,
) -> list[PersonaEvidenceRef]:
    result: list[PersonaEvidenceRef] = []
    raw = draft.get("evidence_refs") if isinstance(draft, Mapping) else None
    if not isinstance(raw, list):
        return result
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        object_type = item.get("object_type")
        object_id = item.get("object_id")
        source_refs = item.get("source_refs")
        if (
            isinstance(object_type, str)
            and isinstance(object_id, str)
            and object_id
            and isinstance(source_refs, list)
        ):
            result.append(
                PersonaEvidenceRef(
                    object_type,
                    object_id,
                    tuple(ref for ref in source_refs if isinstance(ref, Mapping)),
                )
            )
    return result


def _category(content: str) -> str:
    lowered = content.lower()
    if any(marker in lowered for marker in ("不要", "避免", "不使用", "never", "avoid")):
        return "constraint"
    if any(marker in lowered for marker in ("语气", "风格", "简洁", "正式", "style", "tone")):
        return "style"
    if any(marker in lowered for marker in ("流程", "步骤", "工作方式", "workflow")):
        return "workflow"
    return "preference"


def _confidence(value: object) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return max(0.0, min(float(value), 1.0))
    return 0.6
