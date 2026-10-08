from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import re
from typing import Literal, Protocol
from urllib.parse import urlparse


WorkbenchClassifiedInputType = Literal[
    "link",
    "webpage",
    "file",
    "document",
    "pdf",
    "image",
    "audio",
    "meeting_recording",
    "video",
    "bookmark_collection",
    "direct_idea",
]


@dataclass(frozen=True, slots=True)
class WorkbenchInputClassificationResult:
    status: str
    input_type: WorkbenchClassifiedInputType
    input_type_label: str
    intent: str
    intent_label: str
    route: str
    confidence: float
    needs_user_confirmation: bool
    target_intake: str
    workflow_steps: tuple[str, ...]
    structured_output_plan: tuple[str, ...]
    memory_layer_update_plan: tuple[str, ...]
    suggested_next_actions: tuple[str, ...]
    media_required_capability: str | None
    auto_workflow: str | None
    child_inputs: tuple[dict[str, object], ...]
    provider_enhancement_recommended: bool
    provider_enhancement_reason: str
    recommended_provider_role: str
    provider_boundary: str
    privacy_boundary: str
    classifier_version: str
    classifier_prompt_id: str
    classifier_prompt_revision: int
    classifier_prompt_source: str


class WorkbenchInputClassificationProviderPort(Protocol):
    def complete_json(self, *, system_prompt: str, user_payload: Mapping[str, object]) -> Mapping[str, object]:
        """Return safe JSON classification output from an authorized Provider."""


class ClassifyWorkbenchInput:
    """Classify workbench input before routing it to the intake workflow.

    This is intentionally deterministic. It gives the UI and backend one shared
    contract now, while leaving room for a provider-enhanced classifier later.
    """

    _VERSION = "workbench-input-classifier-local-v1"

    def execute(
        self,
        *,
        content: str = "",
        media_type: str = "",
        file_name: str = "",
        urls: Sequence[str] | None = None,
        classifier_prompt: Mapping[str, object] | None = None,
    ) -> WorkbenchInputClassificationResult:
        clean_content = content.strip()
        clean_media_type = media_type.strip().lower()
        clean_file_name = file_name.strip()
        clean_urls = tuple(url.strip() for url in urls or () if isinstance(url, str) and url.strip())
        prompt_meta = _classifier_prompt_meta(classifier_prompt)

        if clean_media_type or clean_file_name:
            return self._classify_file(media_type=clean_media_type, file_name=clean_file_name, prompt_meta=prompt_meta)

        extracted_urls = clean_urls or _extract_urls(clean_content)
        if len(extracted_urls) > 1:
            child_inputs = tuple(
                serialize_workbench_input_classification(self._classify_url(url, prompt_meta=prompt_meta)) | {"raw_input": url}
                for url in extracted_urls
            )
            return self._result(
                input_type="bookmark_collection",
                input_type_label="收藏夹",
                intent="knowledge_supplement",
                intent_label="知识补充",
                route="bookmark_collection_intake",
                confidence=0.88,
                target_intake="bookmark_collection_source_intake",
                workflow_steps=(
                    "save_original_links",
                    "read_web_pages",
                    "structure_collection",
                    "classify_series",
                    "update_memory_layers",
                ),
                structured_output_plan=("链接清单", "网页摘要", "共同主题", "待确认条目"),
                memory_layer_update_plan=("atom", "series_overview", "raw_material_index"),
                suggested_next_actions=("保存收藏夹", "批量读取网页", "生成系列候选"),
                media_required_capability=None,
                auto_workflow="bookmark_collection_web_content_read",
                child_inputs=child_inputs,
                provider_enhancement_recommended=True,
                provider_enhancement_reason=(
                    "一次输入包含多个链接；本地分类器已拆分子项，建议主识别模型进一步判断每个链接的主题和后续工作流。"
                ),
                prompt_meta=prompt_meta,
            )

        if len(extracted_urls) == 1 and _is_http_url(extracted_urls[0]):
            return self._classify_url(extracted_urls[0], prompt_meta=prompt_meta)

        if clean_content:
            intent = classify_direct_text_intent(clean_content)
            return self._result(
                input_type="direct_idea",
                input_type_label="用户直接输入的想法",
                intent=str(intent["intent"]),
                intent_label=str(intent["label"]),
                route=str(intent["route"]),
                confidence=float(intent["confidence"]),
                target_intake="text_source_intake",
                workflow_steps=(
                    "save_original_text",
                    "read_text_content",
                    "structure_content",
                    "classify_series",
                    "update_memory_layers",
                ),
                structured_output_plan=tuple(str(item) for item in intent["structured_output_plan"]),
                memory_layer_update_plan=tuple(str(item) for item in intent["memory_layer_update_plan"]),
                suggested_next_actions=tuple(str(item) for item in intent["suggested_next_actions"]),
                media_required_capability=None,
                auto_workflow="text_auto_organization",
                prompt_meta=prompt_meta,
            )

        raise ValueError("input classifier requires content, url, media_type or file_name")

    def _classify_url(
        self,
        url: str,
        *,
        prompt_meta: Mapping[str, object] | None = None,
    ) -> WorkbenchInputClassificationResult:
        host = urlparse(url).netloc.lower()
        if _looks_like_video_url(url, host):
            return self._result(
                input_type="video",
                input_type_label="视频链接",
                intent="knowledge_supplement",
                intent_label="知识补充",
                route="video_link_workflow",
                confidence=0.86,
                target_intake="video_link_download_or_link_source_intake",
                workflow_steps=(
                    "save_original_link",
                    "download_or_register_video",
                    "extract_audio",
                    "transcribe_audio",
                    "summarize_transcript",
                    "update_memory_layers",
                ),
                structured_output_plan=("视频信息", "转写正文", "摘要", "关键内容", "标签"),
                memory_layer_update_plan=("atom", "scenario", "series_overview", "raw_material_index"),
                suggested_next_actions=("保存视频链接", "下载或登记视频", "启动视频自动工作流"),
                media_required_capability="video_download_or_audio_extraction",
                auto_workflow="video_auto_workflow",
                prompt_meta=prompt_meta,
            )
        return self._result(
            input_type="webpage",
            input_type_label="链接/网页",
            intent="knowledge_supplement",
            intent_label="知识补充",
            route="webpage_intake",
            confidence=0.9,
            target_intake="link_source_intake",
            workflow_steps=(
                "save_original_link",
                "read_web_content",
                "structure_content",
                "classify_series",
                "update_memory_layers",
            ),
            structured_output_plan=("网页摘要", "正文结构", "标签", "关键字段"),
            memory_layer_update_plan=("atom", "series_overview", "raw_material_index"),
            suggested_next_actions=("保存链接", "读取网页正文", "生成系列候选"),
            media_required_capability="web_content_read",
            auto_workflow="link_auto_organization",
            prompt_meta=prompt_meta,
        )

    def _classify_file(
        self,
        *,
        media_type: str,
        file_name: str,
        prompt_meta: Mapping[str, object] | None = None,
    ) -> WorkbenchInputClassificationResult:
        name = file_name.lower()
        if media_type == "application/pdf" or name.endswith(".pdf"):
            return self._file_result(
                input_type="pdf",
                label="PDF",
                target_intake="file_source_intake",
                media_required_capability="document_text_extraction",
                auto_workflow="document_text_extraction",
                prompt_meta=prompt_meta,
            )
        if _is_document(media_type, name):
            return self._file_result(
                input_type="document",
                label="文件/文档",
                target_intake="file_source_intake",
                media_required_capability="document_text_extraction",
                auto_workflow="document_text_extraction",
                prompt_meta=prompt_meta,
            )
        if media_type.startswith("image/"):
            return self._file_result(
                input_type="image",
                label="图片",
                target_intake="image_source_intake",
                media_required_capability="ocr",
                auto_workflow="image_ocr",
                prompt_meta=prompt_meta,
            )
        if media_type.startswith("audio/"):
            input_type: WorkbenchClassifiedInputType = (
                "meeting_recording" if _looks_like_meeting_recording(name) else "audio"
            )
            return self._file_result(
                input_type=input_type,
                label="会议录音" if input_type == "meeting_recording" else "音频/录音",
                target_intake="audio_source_intake",
                media_required_capability="asr",
                auto_workflow="audio_auto_workflow",
                prompt_meta=prompt_meta,
            )
        if media_type.startswith("video/"):
            return self._file_result(
                input_type="video",
                label="视频",
                target_intake="video_source_intake",
                media_required_capability="video_audio_extraction",
                auto_workflow="video_auto_workflow",
                prompt_meta=prompt_meta,
            )
        return self._file_result(
            input_type="file",
            label="文件",
            target_intake="file_source_intake",
            media_required_capability="document_text_extraction",
            auto_workflow="document_text_extraction",
            confidence=0.72,
            needs_user_confirmation=True,
            prompt_meta=prompt_meta,
        )

    def _file_result(
        self,
        *,
        input_type: WorkbenchClassifiedInputType,
        label: str,
        target_intake: str,
        media_required_capability: str | None,
        auto_workflow: str | None,
        confidence: float = 0.88,
        needs_user_confirmation: bool = False,
        prompt_meta: Mapping[str, object] | None = None,
    ) -> WorkbenchInputClassificationResult:
        return self._result(
            input_type=input_type,
            input_type_label=label,
            intent="knowledge_supplement",
            intent_label="知识补充",
            route=f"{input_type}_intake",
            confidence=confidence,
            needs_user_confirmation=needs_user_confirmation,
            target_intake=target_intake,
            workflow_steps=(
                "save_original_asset",
                "extract_readable_content",
                "structure_content",
                "classify_series",
                "update_memory_layers",
            ),
            structured_output_plan=("原档索引", "可读正文", "摘要", "标签", "关键字段"),
            memory_layer_update_plan=("atom", "series_overview", "raw_material_index"),
            suggested_next_actions=("保存原档", "提取可读内容", "生成系列候选"),
            media_required_capability=media_required_capability,
            auto_workflow=auto_workflow,
            provider_enhancement_recommended=confidence < 0.8,
            provider_enhancement_reason="本地文件类型识别置信度较低，建议主识别模型结合文件名和抽取正文确认工作流。"
            if confidence < 0.8
            else "",
            prompt_meta=prompt_meta,
        )

    def _result(
        self,
        *,
        input_type: WorkbenchClassifiedInputType,
        input_type_label: str,
        intent: str,
        intent_label: str,
        route: str,
        confidence: float,
        target_intake: str,
        workflow_steps: tuple[str, ...],
        structured_output_plan: tuple[str, ...],
        memory_layer_update_plan: tuple[str, ...],
        suggested_next_actions: tuple[str, ...],
        media_required_capability: str | None,
        auto_workflow: str | None,
        needs_user_confirmation: bool = False,
        child_inputs: tuple[dict[str, object], ...] = (),
        provider_enhancement_recommended: bool | None = None,
        provider_enhancement_reason: str = "",
        prompt_meta: Mapping[str, object] | None = None,
    ) -> WorkbenchInputClassificationResult:
        should_enhance = confidence < 0.8 if provider_enhancement_recommended is None else provider_enhancement_recommended
        clean_prompt_meta = _classifier_prompt_meta(prompt_meta)
        return WorkbenchInputClassificationResult(
            status="classified",
            input_type=input_type,
            input_type_label=input_type_label,
            intent=intent,
            intent_label=intent_label,
            route=route,
            confidence=round(confidence, 2),
            needs_user_confirmation=needs_user_confirmation,
            target_intake=target_intake,
            workflow_steps=workflow_steps,
            structured_output_plan=structured_output_plan,
            memory_layer_update_plan=memory_layer_update_plan,
            suggested_next_actions=suggested_next_actions,
            media_required_capability=media_required_capability,
            auto_workflow=auto_workflow,
            child_inputs=child_inputs,
            provider_enhancement_recommended=should_enhance,
            provider_enhancement_reason=provider_enhancement_reason
            or (
                "本地快速分类置信度低于 0.80，建议调用主识别模型增强分类和整理。"
                if should_enhance
                else ""
            ),
            recommended_provider_role="intake_main_model" if should_enhance or child_inputs else "lightweight_task_model",
            provider_boundary="local_rule_classifier_no_remote_provider",
            privacy_boundary="Do not include credentials, secrets or raw local paths in classifier payloads.",
            classifier_version=self._VERSION,
            classifier_prompt_id=str(clean_prompt_meta["id"]),
            classifier_prompt_revision=int(clean_prompt_meta["revision"]),
            classifier_prompt_source=str(clean_prompt_meta["source"]),
        )


class EnhanceWorkbenchInputClassification:
    """Use an authorized JSON Provider to refine low-confidence or multi-item classification."""

    _VERSION = "workbench-input-classifier-provider-v1"
    _ALLOWED_TYPES = {
        "link",
        "webpage",
        "file",
        "document",
        "pdf",
        "image",
        "audio",
        "meeting_recording",
        "video",
        "bookmark_collection",
        "direct_idea",
    }

    def execute(
        self,
        *,
        local_result: WorkbenchInputClassificationResult,
        provider: WorkbenchInputClassificationProviderPort,
        provider_name: str,
        content: str = "",
        media_type: str = "",
        file_name: str = "",
        urls: Sequence[str] | None = None,
        classifier_prompt: Mapping[str, object] | None = None,
    ) -> WorkbenchInputClassificationResult:
        clean_provider_name = provider_name.strip()
        if not clean_provider_name:
            raise ValueError("provider_name is required")
        prompt_meta = (
            _classifier_prompt_meta(classifier_prompt)
            if classifier_prompt is not None
            else {
                "id": local_result.classifier_prompt_id,
                "revision": local_result.classifier_prompt_revision,
                "source": local_result.classifier_prompt_source,
            }
        )
        provider_output = provider.complete_json(
            system_prompt=_provider_system_prompt(classifier_prompt),
            user_payload={
                "task": "classify_workbench_input",
                "privacy_boundary": (
                    "The payload is redacted. Do not request or return credentials, secrets, "
                    "authorization material or raw absolute local paths."
                ),
                "content_preview": _safe_preview(content),
                "media_type": media_type[:160],
                "file_name": _safe_preview(_basename_only(file_name), limit=240),
                "urls": [_safe_preview(url, limit=500) for url in urls or ()],
                "local_classification": serialize_workbench_input_classification(local_result),
                "classifier_prompt": {
                    "id": prompt_meta["id"],
                    "revision": prompt_meta["revision"],
                    "source": prompt_meta["source"],
                },
                "required_output_json_shape": {
                    "input_type": "one supported input type",
                    "intent": "knowledge_supplement|question|inspiration|project_progress|review",
                    "route": "workflow route string",
                    "confidence": "0.0-1.0",
                    "child_inputs": "array for multiple links/items",
                    "workflow_steps": "array of workflow step names",
                    "reason": "short explanation",
                },
            },
        )
        _reject_forbidden_provider_output(provider_output)
        return self._merge(
            local_result=local_result,
            output=provider_output,
            provider_name=clean_provider_name,
            prompt_meta=prompt_meta,
        )

    def _merge(
        self,
        *,
        local_result: WorkbenchInputClassificationResult,
        output: Mapping[str, object],
        provider_name: str,
        prompt_meta: Mapping[str, object],
    ) -> WorkbenchInputClassificationResult:
        input_type = _provider_str(output.get("input_type")) or local_result.input_type
        if input_type not in self._ALLOWED_TYPES:
            input_type = local_result.input_type
        intent = _provider_str(output.get("intent")) or local_result.intent
        route = _provider_str(output.get("route")) or local_result.route
        confidence = _provider_confidence(output.get("confidence"), fallback=local_result.confidence)
        workflow_steps = _provider_str_tuple(output.get("workflow_steps")) or local_result.workflow_steps
        structured_output_plan = _provider_str_tuple(output.get("structured_output_plan")) or local_result.structured_output_plan
        memory_layer_update_plan = (
            _provider_str_tuple(output.get("memory_layer_update_plan")) or local_result.memory_layer_update_plan
        )
        suggested_next_actions = _provider_str_tuple(output.get("suggested_next_actions")) or (
            local_result.suggested_next_actions
        )
        child_inputs = _provider_child_inputs(output.get("child_inputs")) or local_result.child_inputs
        reason = _provider_str(output.get("reason")) or _provider_str(output.get("provider_reason")) or (
            "Provider 已增强前置输入分类。"
        )
        return WorkbenchInputClassificationResult(
            status="provider_enhanced",
            input_type=input_type,  # type: ignore[arg-type]
            input_type_label=_input_type_label(input_type, fallback=local_result.input_type_label),
            intent=intent,
            intent_label=_intent_label(intent, fallback=local_result.intent_label),
            route=route,
            confidence=confidence,
            needs_user_confirmation=confidence < 0.72,
            target_intake=_target_intake(input_type, fallback=local_result.target_intake),
            workflow_steps=workflow_steps,
            structured_output_plan=structured_output_plan,
            memory_layer_update_plan=memory_layer_update_plan,
            suggested_next_actions=suggested_next_actions,
            media_required_capability=_provider_str(output.get("media_required_capability"))
            or local_result.media_required_capability,
            auto_workflow=_provider_str(output.get("auto_workflow")) or local_result.auto_workflow,
            child_inputs=child_inputs,
            provider_enhancement_recommended=confidence < 0.72,
            provider_enhancement_reason=reason,
            recommended_provider_role="intake_main_model",
            provider_boundary=f"provider_enhanced_by:{provider_name}",
            privacy_boundary=(
                "Provider payload is redacted and must not include credentials, secrets, "
                "authorization material or raw absolute local paths."
            ),
            classifier_version=self._VERSION,
            classifier_prompt_id=str(prompt_meta["id"]),
            classifier_prompt_revision=int(prompt_meta["revision"]),
            classifier_prompt_source=str(prompt_meta["source"]),
        )


def classify_direct_text_intent(content: str) -> dict[str, object]:
    text = content.strip().lower()
    if _contains_any(text, ("灵感", "想法", "点子", "idea", "inspiration")):
        return _direct_text_intent(
            intent="inspiration",
            label="灵感",
            route="inspiration_material",
            confidence=0.88,
            feedback="已识别为灵感或想法，后续适合保留原文、提炼可行动假设和可能关联的系列。",
            structured_output_plan=("原始想法", "可行动假设", "关联标签", "待确认问题"),
            memory_layer_update_plan=("atom", "scenario"),
            suggested_next_actions=("生成灵感卡片", "创建待审原子记忆", "等待用户确认系列"),
        )
    if _contains_any(text, ("复盘", "回顾", "总结", "反思", "review")):
        return _direct_text_intent(
            intent="review",
            label="复盘",
            route="review_material",
            confidence=0.86,
            feedback="已识别为复盘材料，后续适合抽取事件、判断、反复模式和下一步。",
            structured_output_plan=("事件摘要", "判断依据", "反复模式", "下一步入口"),
            memory_layer_update_plan=("atom", "scenario", "series_overview"),
            suggested_next_actions=("生成复盘摘要", "创建待审场景记忆", "关联到项目或系列"),
        )
    if _contains_any(text, ("下一步", "推进", "任务", "计划", "实现", "修复", "完成", "todo", "deadline")):
        return _direct_text_intent(
            intent="project_progress",
            label="项目推进",
            route="project_progress_material",
            confidence=0.84,
            feedback="已识别为项目推进材料，后续适合拆出任务、阻塞、责任对象和项目技能更新点。",
            structured_output_plan=("任务摘要", "当前状态", "阻塞事项", "验收标准"),
            memory_layer_update_plan=("scenario", "project_skill"),
            suggested_next_actions=("生成项目推进条目", "创建项目技能候选", "关联到当前项目"),
        )
    if _contains_any(text, ("?", "？", "为什么", "怎么", "如何", "能不能", "是否", "什么", "哪", "吗")):
        return _direct_text_intent(
            intent="question",
            label="问答",
            route="qa_material",
            confidence=0.82,
            feedback="已识别为问答材料，后续适合进入召回、回答生成和回答文档草稿。",
            structured_output_plan=("问题", "上下文", "待召回证据", "回答草稿"),
            memory_layer_update_plan=("atom", "scenario"),
            suggested_next_actions=("准备问答证据", "生成本地回答", "生成回答文档或待审记忆候选"),
        )
    if _contains_any(text, ("知识", "资料", "概念", "方法", "学习", "补充", "原则", "定义")):
        return _direct_text_intent(
            intent="knowledge_supplement",
            label="知识补充",
            route="knowledge_material",
            confidence=0.84,
            feedback="已识别为知识补充，后续适合抽取摘要、标签、关键字段和系列归类。",
            structured_output_plan=("摘要", "详细摘要", "标签", "关键字段"),
            memory_layer_update_plan=("atom", "series_overview"),
            suggested_next_actions=("生成结构化正文", "创建待审原子记忆", "自动建议系列"),
        )
    return _direct_text_intent(
        intent="inspiration",
        label="灵感",
        route="inspiration_material",
        confidence=0.76,
        feedback="已识别为灵感或想法，后续适合保留原文、提炼可行动假设和可能关联的系列。",
        structured_output_plan=("原始想法", "可行动假设", "关联标签", "待确认问题"),
        memory_layer_update_plan=("atom", "scenario"),
        suggested_next_actions=("生成灵感卡片", "创建待审原子记忆", "等待用户确认系列"),
    )


def serialize_workbench_input_classification(
    result: WorkbenchInputClassificationResult,
) -> dict[str, object]:
    return {
        "status": result.status,
        "input_type": result.input_type,
        "input_type_label": result.input_type_label,
        "intent": result.intent,
        "intent_label": result.intent_label,
        "route": result.route,
        "confidence": result.confidence,
        "needs_user_confirmation": result.needs_user_confirmation,
        "target_intake": result.target_intake,
        "workflow_steps": list(result.workflow_steps),
        "structured_output_plan": list(result.structured_output_plan),
        "memory_layer_update_plan": list(result.memory_layer_update_plan),
        "suggested_next_actions": list(result.suggested_next_actions),
        "media_required_capability": result.media_required_capability,
        "auto_workflow": result.auto_workflow,
        "child_inputs": list(result.child_inputs),
        "provider_enhancement_recommended": result.provider_enhancement_recommended,
        "provider_enhancement_reason": result.provider_enhancement_reason,
        "recommended_provider_role": result.recommended_provider_role,
        "provider_boundary": result.provider_boundary,
        "privacy_boundary": result.privacy_boundary,
        "classifier_version": result.classifier_version,
        "classifier_prompt_id": result.classifier_prompt_id,
        "classifier_prompt_revision": result.classifier_prompt_revision,
        "classifier_prompt_source": result.classifier_prompt_source,
    }


def _classifier_prompt_meta(classifier_prompt: Mapping[str, object] | None) -> dict[str, object]:
    if not isinstance(classifier_prompt, Mapping):
        return {"id": "pt-input-understanding", "revision": 0, "source": "product_core_default"}
    prompt_id = _provider_str(classifier_prompt.get("id")) or "pt-input-understanding"
    source = _provider_str(classifier_prompt.get("source")) or "developer_studio"
    revision_value = classifier_prompt.get("revision")
    revision = revision_value if isinstance(revision_value, int) and not isinstance(revision_value, bool) else 0
    return {"id": prompt_id, "revision": revision, "source": source}


def _classifier_prompt_content(classifier_prompt: Mapping[str, object] | None) -> str:
    if not isinstance(classifier_prompt, Mapping):
        return ""
    content = classifier_prompt.get("content")
    if not isinstance(content, str) or not content.strip():
        return ""
    return _safe_preview(content, limit=2400)


def _provider_system_prompt(classifier_prompt: Mapping[str, object] | None = None) -> str:
    custom = _classifier_prompt_content(classifier_prompt)
    base = (
        "你是 Chriptmas OS 的前置输入分类器。你只返回 JSON object。"
        "目标是判断输入类型、意图和后续工作流。支持 input_type: "
        "link, webpage, file, document, pdf, image, audio, meeting_recording, video, bookmark_collection, direct_idea。"
        "支持 intent: knowledge_supplement, question, inspiration, project_progress, review。"
        "如果一次输入包含多个链接或多个材料，必须返回 child_inputs 数组，并让每个子项独立包含 input_type、route、intent、raw_input。"
        "不要返回凭据、密钥、认证材料或本地绝对路径。"
    )
    if not custom:
        return base
    return f"{base}\n\nDeveloper Studio 输入理解提示词：\n{custom}"


def _safe_preview(value: str, *, limit: int = 4000) -> str:
    clean = value.strip()
    clean = re.sub(r"(?i)(sk-[A-Za-z0-9_-]{8,})", "[REDACTED_API_KEY]", clean)
    clean = re.sub(r"(?i)(api[_-]?key|token|password|authorization|cookie)\s*[:=]\s*\S+", "[SENSITIVE_VALUE_REMOVED]", clean)
    clean = re.sub(r"[A-Za-z]:\\[^\s]+", "[REDACTED_LOCAL_PATH]", clean)
    clean = re.sub(r"/(?:Users|home|mnt|Volumes)/[^\s]+", "[REDACTED_LOCAL_PATH]", clean)
    return clean[:limit]


def _basename_only(value: str) -> str:
    if not value:
        return ""
    return value.replace("\\", "/").rsplit("/", 1)[-1]


def _reject_forbidden_provider_output(output: Mapping[str, object]) -> None:
    encoded = str(output)
    lowered = encoded.lower()
    if any(marker in lowered for marker in ("api_key", "apikey", "cookie", "authorization", "password")):
        raise ValueError("provider classifier output includes forbidden secret markers")
    if re.search(r"(?i)sk-[A-Za-z0-9_-]{8,}", encoded):
        raise ValueError("provider classifier output includes forbidden secret material")
    if re.search(r"[A-Za-z]:\\", encoded):
        raise ValueError("provider classifier output includes forbidden local path")


def _provider_str(value: object) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else ""


def _provider_confidence(value: object, *, fallback: float) -> float:
    if isinstance(value, (int, float)):
        return round(max(0.0, min(1.0, float(value))), 2)
    if isinstance(value, str):
        try:
            return round(max(0.0, min(1.0, float(value))), 2)
        except ValueError:
            return fallback
    return fallback


def _provider_str_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item.strip() for item in value if isinstance(item, str) and item.strip())


def _provider_child_inputs(value: object) -> tuple[dict[str, object], ...]:
    if not isinstance(value, list):
        return ()
    children: list[dict[str, object]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        child = {str(key): val for key, val in item.items() if isinstance(key, str)}
        raw_input = _provider_str(child.get("raw_input")) or _provider_str(child.get("url"))
        if raw_input:
            child["raw_input"] = _safe_preview(raw_input, limit=500)
        children.append(child)
    return tuple(children)


def _input_type_label(input_type: str, *, fallback: str) -> str:
    labels = {
        "link": "链接/网页",
        "webpage": "链接/网页",
        "file": "文件",
        "document": "文件/文档",
        "pdf": "PDF",
        "image": "图片",
        "audio": "音频/录音",
        "meeting_recording": "会议录音",
        "video": "视频",
        "bookmark_collection": "收藏夹",
        "direct_idea": "用户直接输入的想法",
    }
    return labels.get(input_type, fallback)


def _intent_label(intent: str, *, fallback: str) -> str:
    labels = {
        "knowledge_supplement": "知识补充",
        "question": "问答",
        "inspiration": "灵感",
        "project_progress": "项目推进",
        "review": "复盘",
    }
    return labels.get(intent, fallback)


def _target_intake(input_type: str, *, fallback: str) -> str:
    targets = {
        "link": "link_source_intake",
        "webpage": "link_source_intake",
        "bookmark_collection": "bookmark_collection_source_intake",
        "image": "image_source_intake",
        "audio": "audio_source_intake",
        "meeting_recording": "audio_source_intake",
        "video": "video_source_intake",
        "file": "file_source_intake",
        "document": "file_source_intake",
        "pdf": "file_source_intake",
        "direct_idea": "text_source_intake",
    }
    return targets.get(input_type, fallback)


def _direct_text_intent(
    *,
    intent: str,
    label: str,
    route: str,
    confidence: float,
    feedback: str,
    structured_output_plan: tuple[str, ...],
    memory_layer_update_plan: tuple[str, ...],
    suggested_next_actions: tuple[str, ...],
) -> dict[str, object]:
    return {
        "intent": intent,
        "label": label,
        "route": route,
        "confidence": confidence,
        "feedback": feedback,
        "structured_output_plan": structured_output_plan,
        "memory_layer_update_plan": memory_layer_update_plan,
        "suggested_next_actions": suggested_next_actions,
    }


def _contains_any(text: str, tokens: tuple[str, ...]) -> bool:
    return any(token in text for token in tokens)


def _extract_urls(text: str) -> tuple[str, ...]:
    candidates = []
    for token in text.replace("\n", " ").replace("\t", " ").split(" "):
        clean = token.strip(" \r\n\t,，。；;）)]}>\"'")
        if _is_http_url(clean):
            candidates.append(clean)
    return tuple(candidates)


def _is_http_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _looks_like_video_url(url: str, host: str) -> bool:
    lowered = url.lower()
    return any(
        token in lowered or token in host
        for token in ("bilibili.com", "youtube.com", "youtu.be", "vimeo.com", "/video/", "video")
    )


def _is_document(media_type: str, name: str) -> bool:
    if media_type in {
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.ms-powerpoint",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "text/markdown",
        "text/plain",
        "text/csv",
    }:
        return True
    return name.endswith((".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".md", ".txt", ".csv"))


def _looks_like_meeting_recording(name: str) -> bool:
    return _contains_any(name, ("meeting", "会议", "录会", "访谈", "interview", "call"))
