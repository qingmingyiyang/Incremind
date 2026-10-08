from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
from typing import Protocol

from core.document_engine import DocumentDraft, DocumentRepositoryPort
from core.product_core.outline import (
    Outline,
    OutlineApplier,
    OutlineError,
    OutlineRenderInputs,
)
from .ports import ObjectStorePort


class SourceTemplateDocumentError(ValueError):
    """Raised when a Source cannot create a traceable template Document."""




@dataclass(frozen=True, slots=True)
class SourceTemplateDocumentResult:
    status: str
    source_id: str
    template_type: str
    template_label: str
    document_id: str
    document_revision: int
    document_type: str
    title: str
    source_refs: tuple[Mapping[str, object], ...]
    template_prompt: str
    template_prompt_refs: tuple[Mapping[str, object], ...]
    output_ref: str
    memory_publication_state: str
    blocked_operations: tuple[str, ...]
    provider_name: str | None = None
    provider_enhanced: bool = False
    media_output_id: str | None = None
    media_output_kind: str | None = None


@dataclass(frozen=True, slots=True)
class ApprovedSourceDocumentDraftResult:
    """Result of one approval-gated AI Document write."""

    document: SourceTemplateDocumentResult
    generation_id: str
    receipt_ref: str
    replayed: bool


SOURCE_DOCUMENT_AI_EVIDENCE_KIND = "source.document.evidence"
SOURCE_DOCUMENT_AI_NORMALIZED_KIND = "source.document.normalized"
SOURCE_DOCUMENT_AI_PROMPT_VERSION = "source-document-ai-v1"
SOURCE_DOCUMENT_AI_BLOCKED_OPERATIONS = (
    "memory_candidate_auto_creation",
    "long_term_memory_publication",
    "document_overwrite_without_revision",
    "provider_request_payload_persistence",
    "provider_key_material_return",
)


class CreateSourceTemplateDocument:
    """Create editable fixed-template output from structured Source content."""

    _BLOCKED_OPERATIONS = (
        "model_provider_execution",
        "memory_candidate_auto_creation",
        "long_term_memory_publication",
        "document_overwrite_without_revision",
    )

    _TEMPLATES: Mapping[str, Mapping[str, object]] = {
        "answer_manual": {
            "label": "回答手册",
            "document_type": "answer_manual",
            "instruction": "把资料整理成可复用问答手册，优先保留结论、依据、适用边界和追溯来源。",
            "outline": [
                {"section_id": "summary", "title": "结论", "kind": "summary", "required": True},
                {"section_id": "evidence", "title": "依据", "kind": "key_points", "required": True},
                {"section_id": "body", "title": "适用边界", "kind": "body", "required": False},
                {"section_id": "uncertain", "title": "待确认", "kind": "uncertain", "required": True},
                {"section_id": "sources", "title": "追溯来源", "kind": "sources", "required": True},
            ],
        },
        "review": {
            "label": "复盘",
            "document_type": "review",
            "instruction": "把资料整理成复盘结构，明确背景、关键事实、判断、行动和后续检查点。",
            "outline": [
                {"section_id": "background", "title": "背景", "kind": "summary", "required": True},
                {"section_id": "facts", "title": "关键事实", "kind": "key_points", "required": True},
                {"section_id": "judgment", "title": "判断", "kind": "body", "required": True},
                {"section_id": "actions", "title": "行动", "kind": "key_points", "required": False},
                {"section_id": "checks", "title": "后续检查点", "kind": "key_points", "required": False},
                {"section_id": "uncertain", "title": "待确认", "kind": "uncertain", "required": True},
                {"section_id": "sources", "title": "来源", "kind": "sources", "required": True},
            ],
        },
        "project_summary": {
            "label": "项目总结",
            "document_type": "project_summary",
            "instruction": "把资料整理成项目总结，突出目标、当前进展、风险、下一步和项目 Skill 更新线索。",
            "outline": [
                {"section_id": "goal", "title": "目标", "kind": "summary", "required": True},
                {"section_id": "progress", "title": "当前进展", "kind": "body", "required": True},
                {"section_id": "risks", "title": "风险", "kind": "key_points", "required": False},
                {"section_id": "next_steps", "title": "下一步", "kind": "key_points", "required": False},
                {"section_id": "skill_hints", "title": "项目 Skill 更新线索", "kind": "key_points", "required": False},
                {"section_id": "uncertain", "title": "待确认", "kind": "uncertain", "required": True},
                {"section_id": "sources", "title": "来源", "kind": "sources", "required": True},
            ],
        },
        "media_summary": {
            "label": "媒体总结",
            "document_type": "media_summary",
            "instruction": "把视频或音频转写与摘要整理成媒体总结，保留核心摘要、关键结论、媒体正文和追溯来源。",
            "outline": [
                {"section_id": "summary", "title": "核心摘要", "kind": "summary", "required": True},
                {"section_id": "key_takeaways", "title": "关键结论", "kind": "key_points", "required": True},
                {"section_id": "body", "title": "媒体正文", "kind": "body", "required": True},
                {"section_id": "uncertain", "title": "待确认", "kind": "uncertain", "required": True},
                {"section_id": "sources", "title": "追溯来源", "kind": "sources", "required": True},
            ],
        },
    }

    _DEFAULT_OUTLINE_PROMPT_SERIES: tuple[dict[str, object], ...] = (
        {"section_id": "prompt", "title": "生成提示词", "kind": "prompt", "required": True},
        {"section_id": "series", "title": "系列", "kind": "series", "required": True},
    )

    def __init__(
        self,
        *,
        object_store: ObjectStorePort,
        documents: DocumentRepositoryPort,
        namespace_id: str = "default",
        now: str = "2026-07-02T19:20:00+08:00",
    ) -> None:
        self._object_store = object_store
        self._documents = documents
        self._namespace_id = namespace_id
        self._now = now

    def execute(
        self,
        *,
        source_id: str,
        template_type: str,
        prompt_context: Sequence[Mapping[str, object]] = (),
        style_prefix: str = "",
        outline_override: object = None,
    ) -> SourceTemplateDocumentResult:
        clean_source_id = source_id.strip()
        if not clean_source_id:
            raise SourceTemplateDocumentError("source_id is required")
        template = self._template(template_type)
        outline = self._outline_for(template_type, override=outline_override)
        source = self._source(clean_source_id)
        metadata = dict(source.get("metadata") if isinstance(source.get("metadata"), Mapping) else {})
        structure = metadata.get("content_structure")
        if not isinstance(structure, Mapping) or structure.get("status") != "completed":
            raise SourceTemplateDocumentError("completed content structure is required before template output")
        title = _title(source, fallback=clean_source_id)
        series = metadata.get("series_assignment")
        series_name = _optional_str(series.get("series_name")) if isinstance(series, Mapping) else None
        summary = _optional_str(structure.get("summary")) or _optional_str(structure.get("series_reason")) or title
        key_points = _string_sequence(structure.get("key_points"))
        structured_body = _optional_str(structure.get("structured_body")) or ""
        source_refs = _source_refs(clean_source_id, title, structure=structure, series=series)
        prompt = _template_prompt(
            template_label=_required_template_str(template, "label"),
            instruction=_required_template_str(template, "instruction"),
            series_name=series_name or _optional_str(structure.get("series_candidate")) or "未确认系列",
            style_prefix=style_prefix,
        )
        template_prompt_refs = _prompt_refs(prompt_context)
        markdown = _markdown(
            template_label=_required_template_str(template, "label"),
            title=title,
            series_name=series_name or _optional_str(structure.get("series_candidate")) or "未确认系列",
            prompt=prompt,
            summary=summary,
            key_points=key_points,
            structured_body=structured_body,
            source_id=clean_source_id,
            structure_ref=_optional_str(structure.get("structure_ref")),
            outline=outline,
        )
        document = self._documents.create(
            DocumentDraft(
                title=f"{template['label']}：{title}",
                document_type=_required_template_str(template, "document_type"),
                markdown=markdown,
                source_refs=tuple(source_refs),
                project_id=_optional_str(source.get("project_id")) or "default",
            )
        )
        document_id = _required_str(document, "id")
        output_id = f"template-output-{clean_source_id}-{template_type}"
        output_ref = f"crp://{self._namespace_id}/source-template-outputs/{output_id}.json"
        self._object_store.write(
            "source_template_outputs",
            output_id,
            {
                "schema_version": "1.0.0",
                "id": output_id,
                "source_id": clean_source_id,
                "template_type": template_type,
                "template_label": template["label"],
                "document_id": document_id,
                "document_revision": document["revision"],
                "source_refs": [dict(ref) for ref in source_refs],
                "template_prompt": prompt,
                "template_prompt_refs": [dict(ref) for ref in template_prompt_refs],
                "memory_publication": "not_started",
                "blocked_operations": list(self._BLOCKED_OPERATIONS),
                "created_at": self._now,
                "ref": output_ref,
            },
            expected_revision=None,
        )
        self._update_source_metadata(
            metadata,
            source=source,
            output_id=output_id,
            output_ref=output_ref,
            document=document,
            template_prompt_refs=template_prompt_refs,
        )
        return SourceTemplateDocumentResult(
            status="document_created",
            source_id=clean_source_id,
            template_type=template_type,
            template_label=_required_template_str(template, "label"),
            document_id=document_id,
            document_revision=_required_int(document, "revision"),
            document_type=_required_str(document, "type"),
            title=_required_str(document, "title"),
            source_refs=tuple(source_refs),
            template_prompt=prompt,
            template_prompt_refs=template_prompt_refs,
            output_ref=output_ref,
            memory_publication_state="not_published",
            blocked_operations=self._BLOCKED_OPERATIONS,
        )

    def _source(self, source_id: str) -> Mapping[str, object]:
        source = self._object_store.read("sources", source_id)
        if source is None:
            raise SourceTemplateDocumentError("source not found")
        return source

    def _template(self, template_type: str) -> Mapping[str, object]:
        clean = template_type.strip()
        if clean not in self._TEMPLATES:
            raise SourceTemplateDocumentError("unsupported template_type")
        return self._TEMPLATES[clean]

    def _outline_for(self, template_type: str, *, override: object = None) -> Outline:
        """返回模板的 outline；若提供 override（来自 ProjectSkill），先校验再用。"""
        if override is not None:
            try:
                return Outline.from_payload(override)
            except OutlineError as error:
                raise SourceTemplateDocumentError(str(error)) from error
        template = self._template(template_type)
        outline_payload = template.get("outline")
        if outline_payload is None:
            raise SourceTemplateDocumentError(
                f"template '{template_type}' missing outline definition"
            )
        try:
            return Outline.from_payload(outline_payload)
        except OutlineError as error:
            raise SourceTemplateDocumentError(str(error)) from error

    def _update_source_metadata(
        self,
        metadata: Mapping[str, object],
        *,
        source: Mapping[str, object],
        output_id: str,
        output_ref: str,
        document: Mapping[str, object],
        template_prompt_refs: Sequence[Mapping[str, object]],
    ) -> None:
        updated_metadata = dict(metadata)
        outputs = list(updated_metadata.get("template_outputs")) if isinstance(updated_metadata.get("template_outputs"), list) else []
        outputs = [item for item in outputs if isinstance(item, Mapping) and item.get("output_id") != output_id]
        outputs.append(
            {
                "output_id": output_id,
                "output_ref": output_ref,
                "document_id": _required_str(document, "id"),
                "document_revision": _required_int(document, "revision"),
                "document_type": _required_str(document, "type"),
                "template_prompt_refs": [dict(ref) for ref in template_prompt_refs],
                "created_at": self._now,
            }
        )
        updated_metadata["template_outputs"] = outputs
        updated = dict(source)
        updated["metadata"] = updated_metadata
        self._object_store.write("sources", _required_str(source, "id"), updated, expected_revision=None)




def prepare_source_document_ai_evidence(
    *,
    source: Mapping[str, object],
    source_revision: int,
    template_type: str,
    prompt_context: Sequence[Mapping[str, object]] = (),
    style_prefix: str = "",
    outline_override: object = None,
    document_baseline: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Purely prepare the bounded evidence sent to the Source Document model."""

    source_id = _required_str(source, "id")
    if not isinstance(source_revision, int) or isinstance(source_revision, bool) or source_revision < 1:
        raise SourceTemplateDocumentError("source_revision must be a positive integer")
    clean_template_type = template_type.strip()
    template = CreateSourceTemplateDocument._TEMPLATES.get(clean_template_type)
    if template is None:
        raise SourceTemplateDocumentError("unsupported template_type")
    metadata = dict(source.get("metadata") if isinstance(source.get("metadata"), Mapping) else {})
    structure = metadata.get("content_structure")
    if not isinstance(structure, Mapping) or structure.get("status") != "completed":
        raise SourceTemplateDocumentError("completed content structure is required before provider template output")
    raw_outline = outline_override if outline_override is not None else template.get("outline")
    try:
        Outline.from_payload(raw_outline)
    except OutlineError as error:
        raise SourceTemplateDocumentError(str(error)) from error
    if not isinstance(raw_outline, Sequence) or isinstance(raw_outline, (str, bytes)):
        raise SourceTemplateDocumentError("source document outline must be an array")
    outline = [dict(item) for item in raw_outline if isinstance(item, Mapping)]
    if len(outline) != len(raw_outline):
        raise SourceTemplateDocumentError("source document outline items must be objects")
    title = _title(source, fallback=source_id)
    series = metadata.get("series_assignment")
    series_name = _optional_str(series.get("series_name")) if isinstance(series, Mapping) else None
    resolved_series = series_name or _optional_str(structure.get("series_candidate")) or "未确认系列"
    source_refs = _source_refs(source_id, title, structure=structure, series=series)
    clean_style_prefix = style_prefix.strip()
    if clean_style_prefix and _contains_secret_material(clean_style_prefix):
        raise SourceTemplateDocumentError("source document style must not include secret material")
    clean_prompts = tuple(dict(item) for item in prompt_context if isinstance(item, Mapping))
    prompt = _template_prompt(
        template_label=_required_template_str(template, "label"),
        instruction=_required_template_str(template, "instruction"),
        series_name=resolved_series,
        style_prefix=clean_style_prefix,
    )
    baseline = _normalize_document_baseline(document_baseline)
    return {
        "schema_version": "1.0.0",
        "kind": SOURCE_DOCUMENT_AI_EVIDENCE_KIND,
        "prompt_version": SOURCE_DOCUMENT_AI_PROMPT_VERSION,
        "source_id": source_id,
        "source_revision": source_revision,
        "project_id": _optional_str(source.get("project_id")) or "default",
        "source_title": title,
        "template_type": clean_template_type,
        "template_label": _required_template_str(template, "label"),
        "document_type": _required_template_str(template, "document_type"),
        "template_instruction": _required_template_str(template, "instruction"),
        "series_name": resolved_series,
        "summary": _optional_str(structure.get("summary")) or _optional_str(structure.get("series_reason")) or title,
        "key_points": list(_string_sequence(structure.get("key_points"))),
        "structured_body": _optional_str(structure.get("structured_body")) or "",
        "structure_ref": _optional_str(structure.get("structure_ref")),
        "source_refs": [dict(ref) for ref in source_refs],
        "template_prompt": prompt,
        "prompt_context": [dict(item) for item in clean_prompts],
        "template_prompt_refs": [dict(ref) for ref in _prompt_refs(clean_prompts)],
        "style_prefix": clean_style_prefix,
        "outline": outline,
        "document_baseline": baseline,
        "memory_publication_state": "not_published",
        "blocked_operations": list(SOURCE_DOCUMENT_AI_BLOCKED_OPERATIONS),
    }


def source_document_ai_system_prompt(evidence: Mapping[str, object]) -> str:
    """Build the system message without mixing it into user evidence."""

    _require_ai_evidence(evidence)
    prompt_context = evidence.get("prompt_context")
    clean_prompts = tuple(dict(item) for item in prompt_context if isinstance(item, Mapping)) if isinstance(prompt_context, list) else ()
    return _provider_system_prompt(
        template_label=_required_str(evidence, "template_label"),
        prompt_context=clean_prompts,
        style_prefix=_optional_str(evidence.get("style_prefix")) or "",
    )


def source_document_ai_user_payload(evidence: Mapping[str, object]) -> dict[str, object]:
    """Return the evidence-only user message for ModelGatewayPort."""

    _require_ai_evidence(evidence)
    return {
        "task": "create_editable_source_template_document",
        "source_id": _required_str(evidence, "source_id"),
        "source_revision": _required_int(evidence, "source_revision"),
        "source_title": _required_str(evidence, "source_title"),
        "template_type": _required_str(evidence, "template_type"),
        "template_label": _required_str(evidence, "template_label"),
        "template_instruction": _required_str(evidence, "template_instruction"),
        "series_name": _required_str(evidence, "series_name"),
        "summary": str(evidence.get("summary") or ""),
        "key_points": list(evidence.get("key_points") or ()),
        "structured_body": str(evidence.get("structured_body") or ""),
        "source_refs": [dict(item) for item in evidence.get("source_refs", ()) if isinstance(item, Mapping)],
        "developer_prompt_refs": [dict(item) for item in evidence.get("template_prompt_refs", ()) if isinstance(item, Mapping)],
        "outline": [dict(item) for item in evidence.get("outline", ()) if isinstance(item, Mapping)],
        "output_contract": {
            "type": "json_object",
            "required": ["title", "markdown"],
            "markdown_must_be_editable": True,
            "must_mark_uncertain_content": True,
            "must_not_include_credentials": True,
        },
    }


def normalize_source_document_ai_output(
    *,
    evidence: Mapping[str, object],
    value: Mapping[str, object],
    provider_name: str,
) -> dict[str, object]:
    """Purely validate and normalize one model result into an editable draft."""

    _require_ai_evidence(evidence)
    clean_provider_name = provider_name.strip()
    if not clean_provider_name or _contains_secret_material(clean_provider_name):
        raise SourceTemplateDocumentError("provider_name is invalid")
    if value.get("kind") == SOURCE_DOCUMENT_AI_NORMALIZED_KIND:
        title = _provider_text(value, "title")
        markdown = _provider_required_markdown(value)
        if title is None:
            raise SourceTemplateDocumentError("provider template output requires title")
        return {
            "schema_version": "1.0.0",
            "kind": SOURCE_DOCUMENT_AI_NORMALIZED_KIND,
            "title": title,
            "markdown": markdown,
        }
    provider_title = _provider_text(value, "title") or _required_str(evidence, "source_title")
    provider_markdown = _provider_required_markdown(value)
    try:
        outline = Outline.from_payload(evidence.get("outline"))
    except OutlineError as error:
        raise SourceTemplateDocumentError(str(error)) from error
    markdown = _provider_wrapped_markdown(
        template_label=_required_str(evidence, "template_label"),
        title=provider_title,
        series_name=_required_str(evidence, "series_name"),
        prompt=_required_str(evidence, "template_prompt"),
        provider_markdown=provider_markdown,
        source_id=_required_str(evidence, "source_id"),
        structure_ref=_optional_str(evidence.get("structure_ref")),
        provider_name=clean_provider_name,
        outline=outline,
    )
    return {
        "schema_version": "1.0.0",
        "kind": SOURCE_DOCUMENT_AI_NORMALIZED_KIND,
        "title": provider_title,
        "markdown": markdown,
    }


class ApprovedSourceDocumentDraftWriter:
    """Approval-only durable write seam for one AI-generated editable Document."""

    def __init__(
        self,
        *,
        object_store: ObjectStorePort,
        documents: DocumentRepositoryPort,
        namespace_id: str = "default",
        now: str = "2026-07-02T19:20:00+08:00",
    ) -> None:
        self._object_store = object_store
        self._documents = documents
        self._namespace_id = namespace_id
        self._now = now

    def execute(
        self,
        *,
        evidence: Mapping[str, object],
        generated: Mapping[str, object],
        provider_id: str,
        model_name: str,
    ) -> ApprovedSourceDocumentDraftResult:
        _require_ai_evidence(evidence)
        normalized = normalize_source_document_ai_output(
            evidence=evidence,
            value=generated,
            provider_name=provider_id or "model-gateway",
        )
        generation_id = _source_document_generation_id(evidence, normalized)
        output_id = f"source-document-ai-output-{generation_id[-24:]}"
        output_ref = f"crp://{self._namespace_id}/source-template-outputs/{output_id}.json"
        existing = self._object_store.read("source_template_outputs", output_id)
        if existing is not None:
            return self._replay(existing, evidence=evidence, generation_id=generation_id, output_ref=output_ref)

        source_id = _required_str(evidence, "source_id")
        expected_source_revision = _required_int(evidence, "source_revision")
        source = self._object_store.read("sources", source_id)
        if source is None:
            raise SourceTemplateDocumentError("source not found")
        if self._object_store.revision("sources", source_id) != expected_source_revision:
            raise SourceTemplateDocumentError("Source document AI draft baseline is stale")
        self._verify_document_baseline(evidence.get("document_baseline"))
        source_refs = tuple(dict(item) for item in evidence.get("source_refs", ()) if isinstance(item, Mapping))
        document = self._documents.create(
            DocumentDraft(
                title=f"{_required_str(evidence, 'template_label')}：{_required_str(normalized, 'title')}",
                document_type=_required_str(evidence, "document_type"),
                markdown=_required_str(normalized, "markdown"),
                source_refs=source_refs,
                project_id=_required_str(evidence, "project_id"),
            )
        )
        resulting_source_revision = expected_source_revision + 1
        record = {
            "schema_version": "1.0.0",
            "id": output_id,
            "generation_id": generation_id,
            "prompt_version": SOURCE_DOCUMENT_AI_PROMPT_VERSION,
            "source_id": source_id,
            "source_revision": expected_source_revision,
            "resulting_source_revision": resulting_source_revision,
            "document_baseline": evidence.get("document_baseline"),
            "template_type": _required_str(evidence, "template_type"),
            "template_label": _required_str(evidence, "template_label"),
            "document_id": _required_str(document, "id"),
            "document_revision": _required_int(document, "revision"),
            "document_content_hash": _required_str(document, "content_hash"),
            "source_refs": [dict(ref) for ref in source_refs],
            "template_prompt": _required_str(evidence, "template_prompt"),
            "template_prompt_refs": [dict(item) for item in evidence.get("template_prompt_refs", ()) if isinstance(item, Mapping)],
            "provider_enhanced": True,
            "provider_name": provider_id,
            "model_name": model_name,
            "request_payload_persisted": False,
            "key_material_returned": False,
            "memory_publication": "not_started",
            "blocked_operations": list(SOURCE_DOCUMENT_AI_BLOCKED_OPERATIONS),
            "created_at": self._now,
            "ref": output_ref,
        }
        self._object_store.write("source_template_outputs", output_id, record, expected_revision=0)
        metadata = dict(source.get("metadata") if isinstance(source.get("metadata"), Mapping) else {})
        outputs = [
            dict(item)
            for item in metadata.get("template_outputs", ())
            if isinstance(item, Mapping) and item.get("output_id") != output_id
        ]
        outputs.append(self._source_output_link(
            evidence=evidence,
            document=document,
            output_id=output_id,
            output_ref=output_ref,
            generation_id=generation_id,
            provider_id=provider_id,
        ))
        metadata["template_outputs"] = outputs
        updated_source = dict(source)
        updated_source["metadata"] = metadata
        written_revision = self._object_store.write(
            "sources",
            source_id,
            updated_source,
            expected_revision=expected_source_revision,
        )
        if written_revision != resulting_source_revision:
            raise SourceTemplateDocumentError("Source document AI draft source revision is inconsistent")
        return ApprovedSourceDocumentDraftResult(
            document=self._result(document, evidence=evidence, output_ref=output_ref, provider_id=provider_id),
            generation_id=generation_id,
            receipt_ref=output_ref,
            replayed=False,
        )

    def _verify_document_baseline(self, baseline: object) -> None:
        if baseline is None:
            return
        if not isinstance(baseline, Mapping):
            raise SourceTemplateDocumentError("Source document AI draft document baseline is invalid")
        document_id = _required_str(baseline, "document_id")
        current = self._documents.read(document_id)
        if current is None:
            raise SourceTemplateDocumentError("Source document AI draft document baseline is missing")
        expected_revision = _required_int(baseline, "document_revision")
        if current.get("revision") != expected_revision or current.get("content_hash") != baseline.get("content_hash"):
            raise SourceTemplateDocumentError("Source document AI draft document baseline is stale")

    def _replay(
        self,
        existing: Mapping[str, object],
        *,
        evidence: Mapping[str, object],
        generation_id: str,
        output_ref: str,
    ) -> ApprovedSourceDocumentDraftResult:
        if existing.get("generation_id") != generation_id:
            raise SourceTemplateDocumentError("Source document AI generation identity conflict")
        source_id = _required_str(evidence, "source_id")
        document = self._documents.read(_required_str(existing, "document_id"))
        if (
            document is None
            or document.get("revision") != existing.get("document_revision")
            or document.get("content_hash") != existing.get("document_content_hash")
        ):
            raise SourceTemplateDocumentError("Source document AI draft replay document baseline is stale")
        provider_id = _optional_str(existing.get("provider_name")) or "model-gateway"
        source = self._object_store.read("sources", source_id)
        if source is None:
            raise SourceTemplateDocumentError("source not found")
        current_revision = self._object_store.revision("sources", source_id)
        expected_revision = _required_int(existing, "source_revision")
        resulting_revision = _required_int(existing, "resulting_source_revision")
        output_id = _required_str(existing, "id")
        if current_revision == expected_revision:
            metadata = dict(source.get("metadata") if isinstance(source.get("metadata"), Mapping) else {})
            outputs = [dict(item) for item in metadata.get("template_outputs", ()) if isinstance(item, Mapping)]
            outputs = [item for item in outputs if item.get("output_id") != output_id]
            outputs.append(self._source_output_link(
                evidence=evidence,
                document=document,
                output_id=output_id,
                output_ref=output_ref,
                generation_id=generation_id,
                provider_id=provider_id,
            ))
            metadata["template_outputs"] = outputs
            repaired = dict(source)
            repaired["metadata"] = metadata
            written_revision = self._object_store.write(
                "sources", source_id, repaired, expected_revision=expected_revision
            )
            if written_revision != resulting_revision:
                raise SourceTemplateDocumentError("Source document AI draft replay repair is inconsistent")
        elif current_revision == resulting_revision:
            metadata = source.get("metadata")
            outputs = metadata.get("template_outputs") if isinstance(metadata, Mapping) else None
            linked = next(
                (item for item in outputs if isinstance(item, Mapping) and item.get("output_id") == output_id),
                None,
            ) if isinstance(outputs, list) else None
            if (
                not isinstance(linked, Mapping)
                or linked.get("generation_id") != generation_id
                or linked.get("document_id") != document.get("id")
            ):
                raise SourceTemplateDocumentError("Source document AI draft replay source link is inconsistent")
        else:
            raise SourceTemplateDocumentError("Source document AI draft replay source baseline is stale")
        return ApprovedSourceDocumentDraftResult(
            document=self._result(document, evidence=evidence, output_ref=output_ref, provider_id=provider_id),
            generation_id=generation_id,
            receipt_ref=output_ref,
            replayed=True,
        )

    def _source_output_link(
        self,
        *,
        evidence: Mapping[str, object],
        document: Mapping[str, object],
        output_id: str,
        output_ref: str,
        generation_id: str,
        provider_id: str,
    ) -> dict[str, object]:
        return {
            "output_id": output_id,
            "output_ref": output_ref,
            "generation_id": generation_id,
            "template_type": _required_str(evidence, "template_type"),
            "document_id": _required_str(document, "id"),
            "document_revision": _required_int(document, "revision"),
            "document_type": _required_str(document, "type"),
            "provider_enhanced": True,
            "provider_name": provider_id,
            "template_prompt_refs": [
                dict(item) for item in evidence.get("template_prompt_refs", ()) if isinstance(item, Mapping)
            ],
            "created_at": self._now,
        }

    @staticmethod
    def _result(
        document: Mapping[str, object],
        *,
        evidence: Mapping[str, object],
        output_ref: str,
        provider_id: str,
    ) -> SourceTemplateDocumentResult:
        return SourceTemplateDocumentResult(
            status="document_created",
            source_id=_required_str(evidence, "source_id"),
            template_type=_required_str(evidence, "template_type"),
            template_label=_required_str(evidence, "template_label"),
            document_id=_required_str(document, "id"),
            document_revision=_required_int(document, "revision"),
            document_type=_required_str(document, "type"),
            title=_required_str(document, "title"),
            source_refs=tuple(dict(item) for item in evidence.get("source_refs", ()) if isinstance(item, Mapping)),
            template_prompt=_required_str(evidence, "template_prompt"),
            template_prompt_refs=tuple(dict(item) for item in evidence.get("template_prompt_refs", ()) if isinstance(item, Mapping)),
            output_ref=output_ref,
            memory_publication_state="not_published",
            blocked_operations=SOURCE_DOCUMENT_AI_BLOCKED_OPERATIONS,
            provider_name=provider_id,
            provider_enhanced=True,
        )


class CreateMediaOutputTemplateDocument:
    """Create editable fixed-template output from completed transcript or summary media output."""

    _BLOCKED_OPERATIONS = (
        "model_provider_execution",
        "memory_candidate_auto_creation",
        "long_term_memory_publication",
        "document_overwrite_without_revision",
    )

    def __init__(
        self,
        *,
        object_store: ObjectStorePort,
        documents: DocumentRepositoryPort,
        namespace_id: str = "default",
        now: str = "2026-07-02T19:20:00+08:00",
    ) -> None:
        self._object_store = object_store
        self._documents = documents
        self._namespace_id = namespace_id
        self._now = now

    def execute(
        self,
        *,
        output_id: str,
        template_type: str,
        prompt_context: Sequence[Mapping[str, object]] = (),
        style_prefix: str = "",
        outline_override: object = None,
    ) -> SourceTemplateDocumentResult:
        clean_output_id = output_id.strip()
        if not clean_output_id:
            raise SourceTemplateDocumentError("output_id is required")
        base = CreateSourceTemplateDocument(
            object_store=self._object_store,
            documents=self._documents,
            namespace_id=self._namespace_id,
            now=self._now,
        )
        template = base._template(template_type)
        outline = base._outline_for(template_type, override=outline_override)
        output = self._object_store.read("media_processing_outputs", clean_output_id)
        if output is None:
            raise SourceTemplateDocumentError("media processing output not found")
        if output.get("status") != "completed":
            raise SourceTemplateDocumentError("media processing output must be completed")
        output_kind = _required_str(output, "output_kind")
        if output_kind not in {"transcript", "summary"}:
            raise SourceTemplateDocumentError("media output template requires transcript or summary output")
        source_id = _required_str(output, "source_id")
        source = self._object_store.read("sources", source_id)
        if source is None:
            raise SourceTemplateDocumentError("source not found")
        metadata = dict(source.get("metadata") if isinstance(source.get("metadata"), Mapping) else {})
        title = _media_title(source=source, output=output, fallback=clean_output_id)
        series = metadata.get("series_assignment")
        series_name = _optional_str(series.get("series_name")) if isinstance(series, Mapping) else None
        media_text = _media_output_text(output)
        preview = _required_str(output, "preview")
        summary = _media_summary(output=output, preview=preview)
        key_points = _media_key_points(output)
        source_refs = _media_source_refs(
            source_id=source_id,
            output_id=clean_output_id,
            output_kind=output_kind,
            title=title,
            preview=preview,
            series=series,
        )
        prompt = _template_prompt(
            template_label=_required_template_str(template, "label"),
            instruction=_required_template_str(template, "instruction"),
            series_name=series_name or "未确认系列",
            style_prefix=style_prefix,
        )
        template_prompt_refs = _prompt_refs(prompt_context)
        markdown = _media_markdown(
            template_label=_required_template_str(template, "label"),
            title=title,
            series_name=series_name or "未确认系列",
            prompt=prompt,
            summary=summary,
            key_points=key_points,
            media_text=media_text,
            source_id=source_id,
            output_id=clean_output_id,
            output_kind=output_kind,
            output_ref=_optional_str(output.get("ref")),
            outline=outline,
        )
        document = self._documents.create(
            DocumentDraft(
                title=f"{template['label']}：{title}",
                document_type=_required_template_str(template, "document_type"),
                markdown=markdown,
                source_refs=tuple(source_refs),
                project_id=_optional_str(source.get("project_id")) or "default",
            )
        )
        document_id = _required_str(document, "id")
        template_output_id = f"media-template-output-{clean_output_id}-{template_type}"
        output_ref = f"crp://{self._namespace_id}/media-template-outputs/{template_output_id}.json"
        self._object_store.write(
            "media_template_outputs",
            template_output_id,
            {
                "schema_version": "1.0.0",
                "id": template_output_id,
                "source_id": source_id,
                "media_output_id": clean_output_id,
                "media_output_kind": output_kind,
                "template_type": template_type,
                "template_label": template["label"],
                "document_id": document_id,
                "document_revision": document["revision"],
                "source_refs": [dict(ref) for ref in source_refs],
                "template_prompt": prompt,
                "template_prompt_refs": [dict(ref) for ref in template_prompt_refs],
                "memory_publication": "not_started",
                "blocked_operations": list(self._BLOCKED_OPERATIONS),
                "created_at": self._now,
                "ref": output_ref,
            },
            expected_revision=None,
        )
        self._update_source_metadata(
            metadata,
            source=source,
            output_id=template_output_id,
            output_ref=output_ref,
            document=document,
            media_output_id=clean_output_id,
            template_prompt_refs=template_prompt_refs,
        )
        return SourceTemplateDocumentResult(
            status="document_created",
            source_id=source_id,
            template_type=template_type,
            template_label=_required_template_str(template, "label"),
            document_id=document_id,
            document_revision=_required_int(document, "revision"),
            document_type=_required_str(document, "type"),
            title=_required_str(document, "title"),
            source_refs=tuple(source_refs),
            template_prompt=prompt,
            template_prompt_refs=template_prompt_refs,
            output_ref=output_ref,
            memory_publication_state="not_published",
            blocked_operations=self._BLOCKED_OPERATIONS,
            media_output_id=clean_output_id,
            media_output_kind=output_kind,
        )

    def _update_source_metadata(
        self,
        metadata: Mapping[str, object],
        *,
        source: Mapping[str, object],
        output_id: str,
        output_ref: str,
        document: Mapping[str, object],
        media_output_id: str,
        template_prompt_refs: Sequence[Mapping[str, object]],
    ) -> None:
        updated_metadata = dict(metadata)
        outputs = (
            list(updated_metadata.get("media_template_outputs"))
            if isinstance(updated_metadata.get("media_template_outputs"), list)
            else []
        )
        outputs = [item for item in outputs if isinstance(item, Mapping) and item.get("output_id") != output_id]
        outputs.append(
            {
                "output_id": output_id,
                "output_ref": output_ref,
                "media_output_id": media_output_id,
                "document_id": _required_str(document, "id"),
                "document_revision": _required_int(document, "revision"),
                "document_type": _required_str(document, "type"),
                "template_prompt_refs": [dict(ref) for ref in template_prompt_refs],
                "created_at": self._now,
            }
        )
        updated_metadata["media_template_outputs"] = outputs
        updated = dict(source)
        updated["metadata"] = updated_metadata
        self._object_store.write("sources", _required_str(source, "id"), updated, expected_revision=None)


def serialize_source_template_document_result(result: SourceTemplateDocumentResult) -> dict[str, object]:
    return {
        "status": result.status,
        "source_id": result.source_id,
        "template_type": result.template_type,
        "template_label": result.template_label,
        "document_id": result.document_id,
        "document_revision": result.document_revision,
        "document_type": result.document_type,
        "title": result.title,
        "source_refs": [dict(ref) for ref in result.source_refs],
        "template_prompt": result.template_prompt,
        "template_prompt_refs": [dict(ref) for ref in result.template_prompt_refs],
        "output_ref": result.output_ref,
        "memory_publication_state": result.memory_publication_state,
        "blocked_operations": list(result.blocked_operations),
        "provider_name": result.provider_name,
        "provider_enhanced": result.provider_enhanced,
        "media_output_id": result.media_output_id,
        "media_output_kind": result.media_output_kind,
    }


def _normalize_document_baseline(value: Mapping[str, object] | None) -> dict[str, object] | None:
    if value is None:
        return None
    document_id = _required_str(value, "document_id")
    revision = _required_int(value, "document_revision")
    content_hash = _required_str(value, "content_hash")
    return {
        "document_id": document_id,
        "document_revision": revision,
        "content_hash": content_hash,
        "status": _optional_str(value.get("status")) or "draft",
    }


def _require_ai_evidence(evidence: Mapping[str, object]) -> None:
    if evidence.get("kind") != SOURCE_DOCUMENT_AI_EVIDENCE_KIND:
        raise SourceTemplateDocumentError("Source document AI evidence is invalid")
    if evidence.get("prompt_version") != SOURCE_DOCUMENT_AI_PROMPT_VERSION:
        raise SourceTemplateDocumentError("Source document AI prompt version is invalid")


def _source_document_generation_id(
    evidence: Mapping[str, object],
    generated: Mapping[str, object],
) -> str:
    prompt_refs = [
        {key: item[key] for key in ("id", "revision", "source", "stage_id", "model_profile_id") if key in item}
        for item in evidence.get("template_prompt_refs", ())
        if isinstance(item, Mapping)
    ]
    material = {
        "prompt_version": SOURCE_DOCUMENT_AI_PROMPT_VERSION,
        "source_id": _required_str(evidence, "source_id"),
        "source_revision": _required_int(evidence, "source_revision"),
        "template_type": _required_str(evidence, "template_type"),
        "project_id": _required_str(evidence, "project_id"),
        "prompt_refs": prompt_refs,
        "style_prefix": str(evidence.get("style_prefix") or ""),
        "outline": evidence.get("outline"),
        "document_baseline": evidence.get("document_baseline"),
        "generated": dict(generated),
    }
    digest = hashlib.sha256(
        json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"source-document-ai-{digest}"


def _source_refs(
    source_id: str,
    title: str,
    *,
    structure: Mapping[str, object],
    series: object,
) -> tuple[Mapping[str, object], ...]:
    refs: list[Mapping[str, object]] = [
        {"source_id": source_id, "locator": "source:content", "quote": title},
        {"source_id": source_id, "locator": "source:structure", "quote": _optional_str(structure.get("summary")) or title},
    ]
    if isinstance(series, Mapping):
        series_name = _optional_str(series.get("series_name"))
        series_id = _optional_str(series.get("series_id"))
        if series_name and series_id:
            refs.append({"source_id": source_id, "locator": f"series:{series_id}", "quote": series_name})
    return tuple(refs)


def _template_prompt(*, template_label: str, instruction: str, series_name: str, style_prefix: str = "") -> str:
    lines: list[str] = []
    if style_prefix:
        lines.append(style_prefix)
        lines.append("")
    lines.extend(
        [
            f"你正在为个人 AI 记忆工作台生成{template_label}。",
            f"系列上下文：{series_name}",
            instruction,
            "必须使用已读取正文、结构化摘要、关键点和来源引用；不得编造不存在的事实。",
            "输出必须保留可编辑 Markdown 结构，并标注待确认内容。",
        ]
    )
    return "\n".join(lines)


def _markdown(
    *,
    template_label: str,
    title: str,
    series_name: str,
    prompt: str,
    summary: str,
    key_points: Sequence[str],
    structured_body: str,
    source_id: str,
    structure_ref: str | None,
    outline: Outline,
) -> str:
    full_outline = _with_prompt_series(outline)
    inputs = OutlineRenderInputs(
        template_label=template_label,
        title=title,
        series_name=series_name,
        prompt=prompt,
        summary=summary,
        key_points=tuple(key_points),
        body=structured_body,
        sources=(
            f"- Source: {source_id}",
            f"- Structure: {structure_ref or 'not_recorded'}",
        ),
        uncertain_notes=(),
    )
    return OutlineApplier().render(full_outline, inputs)


def _with_prompt_series(outline: Outline) -> Outline:
    """在业务 outline 前置 prompt 和 series 元信息 section。"""
    prompt_series = Outline.from_payload(
        list(CreateSourceTemplateDocument._DEFAULT_OUTLINE_PROMPT_SERIES)
    )
    return Outline(sections=prompt_series.sections + outline.sections)


def _media_markdown(
    *,
    template_label: str,
    title: str,
    series_name: str,
    prompt: str,
    summary: str,
    key_points: Sequence[str],
    media_text: str,
    source_id: str,
    output_id: str,
    output_kind: str,
    output_ref: str | None,
    outline: Outline,
) -> str:
    full_outline = _with_prompt_series(outline)
    inputs = OutlineRenderInputs(
        template_label=template_label,
        title=title,
        series_name=series_name,
        prompt=prompt,
        summary=summary,
        key_points=tuple(key_points),
        body=media_text,
        sources=(
            f"- Source: {source_id}",
            f"- Media output: {output_id}",
            f"- Output kind: {output_kind}",
            f"- Output ref: {output_ref or 'not_recorded'}",
        ),
        uncertain_notes=(
            "- 请确认视频或音频输出是否完整，特别是转写错误、说话人、省略内容和系列归类。",
            "- 如需进入长期记忆，仍需走 Memory Candidate review / publication。",
        ),
    )
    return OutlineApplier().render(full_outline, inputs)


def _media_source_refs(
    *,
    source_id: str,
    output_id: str,
    output_kind: str,
    title: str,
    preview: str,
    series: object,
) -> tuple[Mapping[str, object], ...]:
    refs: list[Mapping[str, object]] = [
        {"source_id": source_id, "locator": "source:video", "quote": title},
        {"source_id": source_id, "locator": f"media:{output_kind}", "quote": preview},
        {"source_id": source_id, "locator": f"media_output:{output_id}", "quote": output_id},
    ]
    if isinstance(series, Mapping):
        series_name = _optional_str(series.get("series_name"))
        series_id = _optional_str(series.get("series_id"))
        if series_name and series_id:
            refs.append({"source_id": source_id, "locator": f"series:{series_id}", "quote": series_name})
    return tuple(refs)


def _media_title(*, source: Mapping[str, object], output: Mapping[str, object], fallback: str) -> str:
    return _optional_str(output.get("title")) or _title(source, fallback=fallback)


def _media_summary(*, output: Mapping[str, object], preview: str) -> str:
    summary_data = output.get("summary_data")
    if isinstance(summary_data, Mapping):
        summary = (
            _optional_str(summary_data.get("thirty_second_summary"))
            or _optional_str(summary_data.get("core_problem"))
            or _optional_str(summary_data.get("one_sentence_summary"))
        )
        if summary:
            return summary
    return preview


def _media_output_text(output: Mapping[str, object]) -> str:
    for key in ("markdown", "text"):
        value = _optional_str(output.get(key))
        if value:
            return value
    return _optional_str(output.get("preview")) or ""


def _media_key_points(output: Mapping[str, object]) -> tuple[str, ...]:
    summary_data = output.get("summary_data")
    if isinstance(summary_data, Mapping):
        points: list[str] = []
        for key in ("key_points", "key_takeaways", "takeaways", "action_items"):
            points.extend(_string_sequence(summary_data.get(key)))
        chapters = summary_data.get("chapters")
        if isinstance(chapters, Sequence) and not isinstance(chapters, (str, bytes)):
            for chapter in chapters:
                if isinstance(chapter, Mapping):
                    title = _optional_str(chapter.get("title"))
                    summary = _optional_str(chapter.get("summary"))
                    if title and summary:
                        points.append(f"{title}：{summary}")
                    elif title:
                        points.append(title)
        if points:
            return tuple(points[:8])
    preview = _optional_str(output.get("preview"))
    return (preview,) if preview else ()


def _provider_system_prompt(*, template_label: str, prompt_context: Sequence[Mapping[str, object]] = (), style_prefix: str = "") -> str:
    lines: list[str] = []
    if style_prefix:
        lines.append(style_prefix)
        lines.append("")
    lines.extend(
        [
            f"你是个人 AI 记忆工作台的{template_label}生成器。",
            "只返回 JSON object，不要返回 Markdown code fence。",
            "JSON 必须包含 title 和 markdown 字段。",
            "markdown 必须可编辑、可追溯，并明确标注待确认内容。",
            "只能使用用户 payload 中的 summary、key_points、structured_body 和 source_refs。",
            "不得编造不存在的事实，不得输出 API key、Cookie、Authorization header 或任何凭据。",
        ]
    )
    developer_prompts = _provider_prompt_lines(prompt_context)
    if developer_prompts:
        lines.extend(["", "Developer Studio 额外提示词：", *developer_prompts])
    return "\n".join(lines)


def _provider_required_markdown(provider_output: Mapping[str, object]) -> str:
    markdown = _provider_text(provider_output, "markdown")
    if not markdown:
        raise SourceTemplateDocumentError("provider template output requires markdown")
    if _contains_secret_material(markdown):
        raise SourceTemplateDocumentError("provider template output must not include secret material")
    return markdown


def _provider_text(provider_output: Mapping[str, object], key: str) -> str | None:
    value = provider_output.get(key)
    if not isinstance(value, str):
        return None
    clean = value.strip()
    if not clean:
        return None
    if _contains_secret_material(clean):
        raise SourceTemplateDocumentError("provider template output must not include secret material")
    return clean


def _contains_secret_material(value: str) -> bool:
    lowered = value.lower()
    return "sk-" in lowered or "cookie" in lowered or "authorization:" in lowered or "bearer " in lowered


def _provider_wrapped_markdown(
    *,
    template_label: str,
    title: str,
    series_name: str,
    prompt: str,
    provider_markdown: str,
    source_id: str,
    structure_ref: str | None,
    provider_name: str,
    outline: Outline,
) -> str:
    full_outline = _with_prompt_series(outline)
    inputs = OutlineRenderInputs(
        template_label=template_label,
        title=title,
        series_name=series_name,
        prompt=prompt,
        summary="",  # Provider 增强不单独输出 summary，正文即 AI 增强结果
        key_points=(),
        body=provider_markdown,
        sources=(
            f"- Source: {source_id}",
            f"- Structure: {structure_ref or 'not_recorded'}",
            f"- Provider: {provider_name}",
        ),
        uncertain_notes=(
            "- 请确认 Provider 增强内容是否符合原始资料和系列语义。",
            "- 如需进入长期记忆，仍需走 Memory Candidate review / publication。",
        ),
    )
    return OutlineApplier().render(full_outline, inputs)


def _title(source: Mapping[str, object], *, fallback: str) -> str:
    for key in ("title", "name", "display_name"):
        value = source.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    metadata = source.get("metadata")
    if isinstance(metadata, Mapping):
        value = metadata.get("display_name")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return fallback


def _string_sequence(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item)


def _prompt_refs(prompt_context: Sequence[Mapping[str, object]]) -> tuple[Mapping[str, object], ...]:
    refs: list[Mapping[str, object]] = []
    seen: set[str] = set()
    for prompt in prompt_context:
        prompt_id = _optional_str(prompt.get("id"))
        if prompt_id is None or prompt_id in seen:
            continue
        seen.add(prompt_id)
        ref: dict[str, object] = {"id": prompt_id}
        revision = prompt.get("revision")
        if isinstance(revision, int):
            ref["revision"] = revision
        source = _optional_str(prompt.get("source"))
        if source is not None:
            ref["source"] = source
        stage_id = _optional_str(prompt.get("stage_id"))
        if stage_id is not None:
            ref["stage_id"] = stage_id
        model_profile_id = _optional_str(prompt.get("model_profile_id"))
        if model_profile_id is not None:
            ref["model_profile_id"] = model_profile_id
        refs.append(ref)
    return tuple(refs)


def _provider_prompt_lines(prompt_context: Sequence[Mapping[str, object]]) -> tuple[str, ...]:
    lines: list[str] = []
    for prompt in prompt_context:
        prompt_id = _optional_str(prompt.get("id"))
        content = _optional_str(prompt.get("content"))
        if prompt_id is None or content is None:
            continue
        if _contains_secret_material(content):
            continue
        lines.append(f"- {prompt_id}: {content}")
    return tuple(lines)


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _required_str(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise SourceTemplateDocumentError(f"{key} is required")
    return value


def _required_template_str(mapping: Mapping[str, str], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise SourceTemplateDocumentError(f"template {key} is required")
    return value


def _required_int(mapping: Mapping[str, object], key: str) -> int:
    value = mapping.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise SourceTemplateDocumentError(f"{key} must be an integer")
    return value
