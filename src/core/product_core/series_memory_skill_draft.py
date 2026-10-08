from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from core.memory_core import ObjectStoreMemoryCandidateRepository
from core.memory_core.runtime import memory_candidate_id
from .ports import ObjectStorePort


class SeriesMemorySkillDraftError(ValueError):
    """Raised when a Source cannot safely propose L3 series memory and Project Skill drafts."""


@dataclass(frozen=True, slots=True)
class LayeredDraftCandidate:
    candidate_id: str
    target_layer: str
    candidate_type: str
    status: str
    review_prompt: str


@dataclass(frozen=True, slots=True)
class SeriesMemorySkillDraftResult:
    status: str
    source_id: str
    project_id: str
    series_id: str
    series_name: str
    content_read_id: str
    structure_ref: str
    candidates: tuple[LayeredDraftCandidate, ...]
    series_memory_update_plan: str
    project_skill_update_plan: str
    activity_refs: tuple[str, ...]
    memory_publication_state: str
    blocked_operations: tuple[str, ...]


class CreateSeriesMemorySkillDraftsFromSourceStructure:
    """Create reviewable L3 drafts from confirmed series assignment and structured Source content."""

    _BLOCKED_OPERATIONS = (
        "automatic_memory_publication",
        "auto_promote_memory",
        "model_provider_execution",
        "project_skill_overwrite",
        "user_edit_overwrite",
    )

    def __init__(
        self,
        object_store: ObjectStorePort,
        *,
        candidates: ObjectStoreMemoryCandidateRepository | None = None,
        namespace_id: str = "default",
        now: str = "2026-07-02T20:20:00+08:00",
    ) -> None:
        self._object_store = object_store
        self._candidates = candidates or ObjectStoreMemoryCandidateRepository(object_store)
        self._namespace_id = namespace_id
        self._now = now

    def execute(self, *, source_id: str, project_id: str | None = None) -> SeriesMemorySkillDraftResult:
        clean_source_id = source_id.strip()
        if not clean_source_id:
            raise SeriesMemorySkillDraftError("source_id is required")
        source = self._object_store.read("sources", clean_source_id)
        if source is None:
            raise SeriesMemorySkillDraftError("source not found")
        metadata = dict(source.get("metadata") if isinstance(source.get("metadata"), Mapping) else {})
        structure = _required_mapping(metadata.get("content_structure"), "completed content structure is required")
        if structure.get("status") != "completed":
            raise SeriesMemorySkillDraftError("completed content structure is required")
        assignment = _required_mapping(metadata.get("series_assignment"), "confirmed series assignment is required")
        if assignment.get("status") != "confirmed":
            raise SeriesMemorySkillDraftError("confirmed series assignment is required")

        clean_project_id = _clean(project_id) or _optional_str(source.get("project_id")) or "default"
        series_id = _required_str(assignment, "series_id")
        series_name = _required_str(assignment, "series_name")
        content_read_id = _required_str(structure, "content_read_id")
        structure_ref = _required_str(structure, "structure_ref")
        summary = _required_str(structure, "summary")
        tags = _string_sequence(structure.get("tags"))
        key_points = _string_sequence(structure.get("key_points"))
        structured_body = _optional_str(structure.get("structured_body")) or summary
        source_refs = _source_refs(
            source_id=clean_source_id,
            content_read_id=content_read_id,
            structure_ref=structure_ref,
            series_id=series_id,
            series_name=series_name,
            summary=summary,
        )
        series_plan = _series_memory_plan(series_name=series_name, summary=summary, key_points=key_points)
        skill_plan = _project_skill_plan(series_name=series_name, tags=tags, key_points=key_points)
        candidate_payloads = (
            self._candidate(
                source_id=clean_source_id,
                project_id=clean_project_id,
                series_id=series_id,
                target_layer="series_memory",
                candidate_type="document_takeaway",
                proposed_content="\n".join(
                    [
                        f"系列：{series_name}",
                        "",
                        "## 系列总览",
                        series_plan,
                        "",
                        "## 结构化正文",
                        structured_body,
                    ]
                ),
                source_refs=source_refs,
                content_read_id=content_read_id,
                structure_ref=structure_ref,
                review_prompt=_review_prompt("series_memory"),
            ),
            self._candidate(
                source_id=clean_source_id,
                project_id=clean_project_id,
                series_id=series_id,
                target_layer="project_skill",
                candidate_type="document_takeaway",
                proposed_content="\n".join(
                    [
                        f"项目：{clean_project_id}",
                        f"来源系列：{series_name}",
                        "",
                        "## Project Skill 更新建议",
                        skill_plan,
                        "",
                        "## 默认阅读要求",
                        "- 回答前优先读取已确认系列、段落级标签、结构化摘要和 Source refs。",
                        "- 输出必须保留来源引用、待确认事项和不可自动发布长期 Memory 的边界。",
                    ]
                ),
                source_refs=source_refs,
                content_read_id=content_read_id,
                structure_ref=structure_ref,
                review_prompt=_review_prompt("project_skill"),
            ),
        )
        saved = tuple(self._save_or_get(candidate) for candidate in candidate_payloads)
        event_ref = self._write_event(
            source_id=clean_source_id,
            project_id=clean_project_id,
            series_id=series_id,
            candidate_ids=tuple(_required_str(candidate, "id") for candidate in saved),
        )
        self._update_source_metadata(
            source=source,
            metadata=metadata,
            project_id=clean_project_id,
            series_id=series_id,
            series_name=series_name,
            candidates=saved,
            series_plan=series_plan,
            skill_plan=skill_plan,
            activity_refs=(event_ref,),
        )
        return SeriesMemorySkillDraftResult(
            status="candidates_created",
            source_id=clean_source_id,
            project_id=clean_project_id,
            series_id=series_id,
            series_name=series_name,
            content_read_id=content_read_id,
            structure_ref=structure_ref,
            candidates=tuple(_result_candidate(candidate) for candidate in saved),
            series_memory_update_plan=series_plan,
            project_skill_update_plan=skill_plan,
            activity_refs=(event_ref,),
            memory_publication_state="candidate_created_not_published",
            blocked_operations=self._BLOCKED_OPERATIONS,
        )

    def _candidate(
        self,
        *,
        source_id: str,
        project_id: str,
        series_id: str,
        target_layer: str,
        candidate_type: str,
        proposed_content: str,
        source_refs: Sequence[Mapping[str, object]],
        content_read_id: str,
        structure_ref: str,
        review_prompt: str,
    ) -> dict[str, object]:
        candidate_id = memory_candidate_id(source_id, target_layer, candidate_type, proposed_content)
        return {
            "schema_version": "1.0.0",
            "id": candidate_id,
            "project_id": project_id,
            "series_id": series_id,
            "target_layer": target_layer,
            "candidate_type": candidate_type,
            "status": "pending_review",
            "proposed_content": proposed_content,
            "source_refs": [dict(ref) for ref in source_refs],
            "provenance": {
                "source_content_read_id": content_read_id,
                "structure_ref": structure_ref,
                "input_refs": [
                    {"kind": "source", "id": source_id, "ref": f"crp://{self._namespace_id}/sources/{source_id}.json"},
                    {
                        "kind": "source_content_read",
                        "id": content_read_id,
                        "ref": f"crp://{self._namespace_id}/source-content-reads/{content_read_id}.json",
                    },
                    {"kind": "source_structure", "id": structure_ref},
                ],
            },
            "review": {
                "requires_user_confirmation": True,
                "auto_promote_allowed": False,
                "review_prompt": review_prompt,
                "reviewed_by": None,
                "reviewed_at": None,
            },
            "created_at": self._now,
            "updated_at": self._now,
        }

    def _save_or_get(self, candidate: Mapping[str, object]) -> Mapping[str, object]:
        candidate_id = _required_str(candidate, "id")
        existing = self._candidates.get(candidate_id)
        if existing is not None:
            return existing
        return self._candidates.save(candidate)

    def _write_event(
        self,
        *,
        source_id: str,
        project_id: str,
        series_id: str,
        candidate_ids: Sequence[str],
    ) -> str:
        event_id = f"event-layered-drafts-created-{source_id}"
        event_ref = f"crp://{self._namespace_id}/activity/{event_id}.json"
        self._object_store.write(
            "activity_events",
            event_id,
            {
                "schema_version": "1.0.0",
                "id": event_id,
                "type": "series_memory_skill_drafts_created",
                "source_id": source_id,
                "status": "candidate_created",
                "contentRead": True,
                "memoryPublication": "candidate_created",
                "details": {
                    "project_id": project_id,
                    "series_id": series_id,
                    "candidate_ids": list(candidate_ids),
                },
                "created_at": self._now,
                "ref": event_ref,
            },
            expected_revision=None,
        )
        return event_ref

    def _update_source_metadata(
        self,
        *,
        source: Mapping[str, object],
        metadata: Mapping[str, object],
        project_id: str,
        series_id: str,
        series_name: str,
        candidates: Sequence[Mapping[str, object]],
        series_plan: str,
        skill_plan: str,
        activity_refs: Sequence[str],
    ) -> None:
        updated_metadata = dict(metadata)
        updated_metadata["layered_memory_drafts"] = {
            "status": "candidate_created",
            "project_id": project_id,
            "series_id": series_id,
            "series_name": series_name,
            "candidate_ids": [_required_str(candidate, "id") for candidate in candidates],
            "target_layers": [_required_str(candidate, "target_layer") for candidate in candidates],
            "series_memory_update_plan": series_plan,
            "project_skill_update_plan": skill_plan,
            "activity_refs": list(activity_refs),
            "memory_publication": "candidate_created_not_published",
            "blocked_operations": list(self._BLOCKED_OPERATIONS),
            "updated_at": self._now,
        }
        updated = dict(source)
        updated["metadata"] = updated_metadata
        self._object_store.write("sources", _required_str(source, "id"), updated, expected_revision=None)


def serialize_series_memory_skill_draft_result(result: SeriesMemorySkillDraftResult) -> dict[str, object]:
    return {
        "status": result.status,
        "source_id": result.source_id,
        "project_id": result.project_id,
        "series_id": result.series_id,
        "series_name": result.series_name,
        "content_read_id": result.content_read_id,
        "structure_ref": result.structure_ref,
        "candidates": [
            {
                "candidate_id": candidate.candidate_id,
                "target_layer": candidate.target_layer,
                "candidate_type": candidate.candidate_type,
                "status": candidate.status,
                "review_prompt": candidate.review_prompt,
            }
            for candidate in result.candidates
        ],
        "series_memory_update_plan": result.series_memory_update_plan,
        "project_skill_update_plan": result.project_skill_update_plan,
        "activity_refs": list(result.activity_refs),
        "memory_publication_state": result.memory_publication_state,
        "blocked_operations": list(result.blocked_operations),
    }


def _result_candidate(candidate: Mapping[str, object]) -> LayeredDraftCandidate:
    review = _required_mapping(candidate.get("review"), "memory candidate requires review")
    return LayeredDraftCandidate(
        candidate_id=_required_str(candidate, "id"),
        target_layer=_required_str(candidate, "target_layer"),
        candidate_type=_required_str(candidate, "candidate_type"),
        status=_required_str(candidate, "status"),
        review_prompt=_required_str(review, "review_prompt"),
    )


def _source_refs(
    *,
    source_id: str,
    content_read_id: str,
    structure_ref: str,
    series_id: str,
    series_name: str,
    summary: str,
) -> tuple[Mapping[str, object], ...]:
    return (
        {"source_id": source_id, "locator": f"source_content_read:{content_read_id}", "quote": summary},
        {"source_id": source_id, "locator": f"source_structure:{structure_ref}", "quote": summary},
        {"source_id": source_id, "locator": f"series:{series_id}", "quote": series_name},
    )


def _series_memory_plan(*, series_name: str, summary: str, key_points: Sequence[str]) -> str:
    points = "\n".join(f"- {point}" for point in key_points[:6]) if key_points else "- 暂无关键点。"
    return "\n".join(
        [
            f"围绕“{series_name}”更新 L3 Series Memory。",
            "",
            "### 总览",
            summary,
            "",
            "### 关键证据",
            points,
            "",
            "### 边界",
            "该草稿只进入待审候选，不自动发布长期 Memory。",
        ]
    )


def _project_skill_plan(*, series_name: str, tags: Sequence[str], key_points: Sequence[str]) -> str:
    tag_text = "、".join(tags[:8]) if tags else "未提取标签"
    points = "\n".join(f"- {point}" for point in key_points[:6]) if key_points else "- 暂无关键点。"
    return "\n".join(
        [
            f"根据“{series_name}”补充项目 Skill 默认阅读要求和输出规则。",
            f"段落级标签：{tag_text}",
            "",
            "### 可迁移规则",
            points,
            "",
            "### 默认提示词要求",
            "- 先读取系列总览、结构化摘要、段落级标签和来源引用。",
            "- 输出回答手册、复盘、项目总结时必须使用固定结构。",
            "- 不得把待审候选自动写入长期 Memory。",
        ]
    )


def _review_prompt(target_layer: str) -> str:
    if target_layer == "series_memory":
        return "\n".join(
            [
                "请审核该 L3 Series Memory 候选是否准确反映已确认系列。",
                "重点检查系列名称、摘要、关键证据和来源引用。",
                "通过后只进入 staging series memory，仍需二次确认才发布长期 Memory。",
            ]
        )
    return "\n".join(
        [
            "请审核该 Project Skill 候选是否适合作为项目默认阅读要求和输出规则。",
            "重点检查是否基于已读资料、是否覆盖固定输出模板、是否保留用户编辑优先。",
            "通过后只进入 staging project skill，仍需二次确认才发布长期 Memory。",
        ]
    )


def _string_sequence(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item)


def _required_mapping(value: object, message: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise SeriesMemorySkillDraftError(message)
    return value


def _required_str(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise SeriesMemorySkillDraftError(f"{key} is required")
    return value


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _clean(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None
