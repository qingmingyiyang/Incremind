from __future__ import annotations

import hashlib
from copy import deepcopy
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal

from core.ingestion_core import SourceRegistrarPort
from core.job_runner import JobRepositoryPort
from .ports import ObjectStorePort

from .audio_auto_workflow import AudioAutoWorkflowResult
from .inspiration_system import RecordInspirationFromSource
from .long_audio_chunker import LongAudioChunkedWorkflowResult
from .media_processing_queue import MediaProcessingQueueResult
from .source_content_read import ReadLinkWebContent, ReadSourceTextContent, SourceContentReadResult
from .source_output_memory_candidate import SourceOutputMemoryCandidateResult
from .source_series_assignment import ConfirmSourceSeriesAssignment
from .source_structuring import StructureSourceContent
from .tag_index import IndexSourceTags
from .task_model_map_resolver import TaskModelMapResolver
from .transcript_summary_adapter import TranscriptSummaryResult
from .video_auto_workflow import VideoAutoWorkflowResult
from .workbench_input_classifier import (
    ClassifyWorkbenchInput,
    WorkbenchInputClassificationResult,
    serialize_workbench_input_classification,
)
from .workbench_source_intake import (
    CaptureWorkbenchAudioSource,
    CaptureWorkbenchBookmarkCollection,
    CaptureWorkbenchFileSource,
    CaptureWorkbenchImageSource,
    CaptureWorkbenchLinkSource,
    CaptureWorkbenchTextSource,
    CaptureWorkbenchVideoSource,
)
from .workflow_progression import WorkflowDecisionBoundary, decide_workflow_progression
from .workbench_original_asset import link_workbench_original_asset_to_source


WorkbenchAutoIntakeItemStatus = Literal[
    "queued",
    "completed",
    "completed_pending_series",
    "needs_confirmation",
    "needs_extractor",
    "needs_asr",
    "needs_video_workflow",
    "failed",
]


WorkbenchAutoIntakeStatus = Literal[
    "accepted",
    "needs_confirmation",
    "direct_question",
    "failed",
]


@dataclass(frozen=True, slots=True)
class WorkbenchAutoIntakeItem:
    source_id: str
    source_uri: str
    input_type: str
    workflow: str
    status: WorkbenchAutoIntakeItemStatus
    needs_user_confirmation: bool
    title: str
    content_read_status: str
    structure_status: str
    series_status: str
    series_name: str
    series_confidence: float
    inspiration_status: str
    auto_organization: Mapping[str, object]
    next_step: str


@dataclass(frozen=True, slots=True)
class WorkbenchAutoIntakeResult:
    status: WorkbenchAutoIntakeStatus
    job_id: str
    items: tuple[WorkbenchAutoIntakeItem, ...]
    classification: Mapping[str, object]
    next_ui: str
    direct_question_hint: Mapping[str, object] | None
    error: str | None
    # 阶段 1.5：Memory Router 自动判断 memory_event_type / memory_delta
    memory_event: Mapping[str, object] | None = None


def reduce_workbench_auto_intake_job(
    items: Sequence[WorkbenchAutoIntakeItem],
    *,
    label: str,
    now: str,
) -> dict[str, object]:
    """Reduce synchronous intake outcomes into an honest persistent Job state.

    This deliberately models only work completed by the request.  A required
    extractor or a user confirmation remains pending/waiting rather than being
    represented as fabricated background progress.
    """
    steps: list[dict[str, object]] = []
    failed_items = [item for item in items if item.status == "failed"]
    waiting_items = [
        item
        for item in items
        if item.status in {"needs_confirmation", "completed_pending_series"}
    ]
    completed_count = 0

    for index, item in enumerate(items, start=1):
        if item.status == "completed":
            step_status = "completed"
            step_progress = 100
            completed_at: str | None = now
            completed_count += 1
        elif item.status == "failed":
            step_status = "failed"
            step_progress = 0
            completed_at = now
        elif item in waiting_items:
            step_status = "waiting_user"
            step_progress = 0
            completed_at = None
        else:
            step_status = "pending"
            step_progress = 0
            completed_at = None

        step_error = None
        if item.status == "failed":
            step_error = {
                "code": "workbench_intake_item_failed",
                "message": "资料处理失败，请检查资料后重试。",
                "retryable": True,
                "failed_step": f"orchestrate_{label}_{index}",
                "details": {"item_status": item.status, "next_step": item.next_step},
            }
        steps.append(
            {
                "name": f"orchestrate_{label}_{index}",
                "status": step_status,
                "attempt": 1,
                "started_at": now,
                "completed_at": completed_at,
                "progress": step_progress,
                "input_refs": [item.source_uri] if item.source_uri else [],
                "staged_output_refs": [],
                "log_refs": [],
                "error": step_error,
                "source_id": item.source_id,
                "input_type": item.input_type,
                "workflow": item.workflow,
                "needs_user_confirmation": item.needs_user_confirmation,
                "retry_action": item.next_step if item.status == "failed" else None,
            }
        )

    total = len(items)
    percent = 0 if total == 0 else (completed_count * 100) // total
    if failed_items:
        first_failed_step = str(steps[next(index for index, item in enumerate(items) if item.status == "failed")]["name"])
        status = "failed"
        error: dict[str, object] | None = {
            "code": "workbench_intake_failed",
            "message": "部分资料未能完成处理，请检查失败项后重试。",
            "retryable": True,
            "failed_step": first_failed_step,
            "details": {"failed_items": len(failed_items), "total_items": total, "retry_action": "review_failure_and_retry"},
        }
        message = f"{len(failed_items)} of {total} workbench intake item(s) failed"
    elif waiting_items:
        status = "waiting_user"
        error = None
        message = f"awaiting confirmation for {len(waiting_items)} workbench intake item(s)"
    elif completed_count == total and total > 0:
        status = "completed"
        error = None
        message = f"orchestrated {total} workbench intake item(s)"
    else:
        status = "pending"
        error = None
        message = f"awaiting required workflow for {total - completed_count} workbench intake item(s)"

    return {
        "status": status,
        "attempt": 1,
        "max_attempts": 3,
        "progress": {"current": completed_count, "total": total, "percent": percent, "message": message},
        "steps": steps,
        "error": error,
    }


_HIGH_CONFIDENCE_SERIES_THRESHOLD = 0.82

_CAPTURE_KEY_MAP = {
    "file": "file",
    "document": "file",
    "pdf": "file",
    "image": "image",
    "audio": "audio",
    "meeting_recording": "audio",
    "video": "video",
}


@dataclass(frozen=True, slots=True)
class _FrozenSourceReplay:
    source_id: str
    source: Mapping[str, object]
    asset_id: str
    link_ref: str
    asset_storage_mode: str
    asset_availability: str
    authorization_ready: bool


@dataclass(frozen=True, slots=True)
class _ReplayMediaCaptureResult:
    source_id: str
    source_uri: str
    source_title: str
    asset_id: str
    asset_storage_mode: str
    asset_availability: str


def _frozen_source_snapshot_for_asset(
    object_store: ObjectStorePort,
    original_asset_ref: str,
    *,
    namespace_id: str = "default",
    expected_source_type: str,
    expected_media_type: str,
    expected_title: str,
    expected_display_name: str,
    project_id: str,
) -> _FrozenSourceReplay | None:
    """Read an already-linked Source before deterministic replay capture writes it.

    The asset reference is opaque.  This never opens the asset; it only finds a
    durable Source/asset relation that was created by an earlier admission.
    """
    if not original_asset_ref:
        return None
    candidates: list[_FrozenSourceReplay] = []
    for link in object_store.list("source_asset_links"):
        if not isinstance(link, Mapping) or link.get("asset_ref") != original_asset_ref:
            continue
        if link.get("role") != "original":
            raise ValueError("workbench replay source asset relation is invalid")
        source_id = link.get("source_id")
        link_id = link.get("id")
        asset_id = link.get("asset_id")
        if (
            not isinstance(source_id, str) or not source_id
            or not isinstance(link_id, str) or not link_id
            or not isinstance(asset_id, str) or not asset_id
        ):
            continue
        source = object_store.read("sources", source_id)
        asset = object_store.read("workbench_original_assets", asset_id)
        if not isinstance(source, Mapping):
            continue
        source_uri = source.get("storage_uri")
        if (
            not isinstance(asset, Mapping)
            or asset.get("id") != asset_id
            or asset.get("asset_ref") != original_asset_ref
            or source.get("id") != source_id
            or link.get("source_uri") != source_uri
        ):
            raise ValueError("workbench replay source asset relation is invalid")
        if _source_matches_replay_request(
            source,
            asset_ref=original_asset_ref,
            source_type=expected_source_type,
            media_type=expected_media_type,
            title=expected_title,
            display_name=expected_display_name,
            project_id=project_id,
        ):
            candidates.append(_FrozenSourceReplay(
                source_id=source_id,
                source=deepcopy(dict(source)),
                asset_id=asset_id,
                link_ref=f"crp://{namespace_id}/source-assets/{link_id}",
                asset_storage_mode=str((asset or {}).get("storage_mode") or "stored_original"),
                asset_availability=str((asset or {}).get("availability") or "unknown"),
                authorization_ready=_source_authorization_is_ready(
                    object_store,
                    source,
                    source_id=source_id,
                    asset_ref=original_asset_ref,
                    source_type=expected_source_type,
                    media_type=expected_media_type,
                ),
            ))
        else:
            raise ValueError("workbench replay source is incompatible with the capture request")
    if len(candidates) > 1:
        raise ValueError("workbench replay source authority is ambiguous")
    return candidates[0] if candidates else None


def _replay_media_capture_result(frozen: _FrozenSourceReplay) -> _ReplayMediaCaptureResult:
    """Return capture display fields without touching a frozen replay Source."""
    source = frozen.source
    source_uri = source.get("storage_uri")
    source_title = source.get("title")
    if not isinstance(source_uri, str) or not source_uri or not isinstance(source_title, str) or not source_title:
        raise ValueError("workbench replay source evidence is invalid")
    return _ReplayMediaCaptureResult(
        source_id=frozen.source_id,
        source_uri=source_uri,
        source_title=source_title,
        asset_id=frozen.asset_id,
        asset_storage_mode=frozen.asset_storage_mode,
        asset_availability=frozen.asset_availability,
    )


def _source_authorization_is_ready(
    object_store: ObjectStorePort,
    source: Mapping[str, object],
    *,
    source_id: str,
    asset_ref: str,
    source_type: str,
    media_type: str,
) -> bool:
    metadata = source.get("metadata")
    if not isinstance(metadata, Mapping):
        return False
    authorization = metadata.get({
        "file": "document_authorization" if "document_authorization" in metadata else "file_authorization",
        "image": "image_authorization",
        "audio": "audio_authorization",
        "video": "video_authorization",
    }[source_type])
    if not isinstance(authorization, Mapping):
        return False
    authorization_id = authorization.get("authorization_id")
    reference_field = {
        "file": "file_reference",
        "image": "image_reference",
        "audio": "audio_reference",
        "video": "video_reference",
    }[source_type]
    record = object_store.read("authorized_file_refs", authorization_id) if isinstance(authorization_id, str) else None
    return (
        authorization.get("status") == "authorized"
        and authorization.get(reference_field) == asset_ref
        and authorization.get("media_type") == media_type
        and isinstance(record, Mapping)
        and record.get("id") == authorization_id
        and record.get("status") == "authorized"
        and record.get("source_id") == source_id
        and record.get(reference_field) == asset_ref
        and record.get("media_type") == media_type
    )


def _source_matches_replay_request(
    source: Mapping[str, object],
    *,
    asset_ref: str,
    source_type: str,
    media_type: str,
    title: str,
    display_name: str,
    project_id: str,
) -> bool:
    metadata = source.get("metadata")
    return (
        source.get("type") == source_type
        and source.get("media_type") == media_type
        and source.get("title") == title
        and str(source.get("project_id") or "default") == project_id
        and isinstance(metadata, Mapping)
        and metadata.get("display_name") == display_name
        and metadata.get(
            {
                "file": "file_reference",
                "image": "image_reference",
                "audio": "audio_reference",
                "video": "video_reference",
            }[source_type]
        )
        == asset_ref
    )
class OrchestrateWorkbenchAutoIntake:
    """Orchestrate the unified workbench auto intake pipeline.

    The orchestrator wires the existing deterministic classifier, source capture
    use cases, content readers, structuring and series assignment into one job so
    the front end submits raw input once and receives child jobs plus status.
    """

    _ORCHESTRATOR_VERSION = "workbench-auto-intake-v1"

    def __init__(
        self,
        *,
        object_store: ObjectStorePort,
        source_registrar: SourceRegistrarPort,
        job_repository: JobRepositoryPort,
        fetch_url: Callable[[str], str],
        namespace_id: str = "default",
        project_id: str = "default",
        now: str = "2026-07-03T20:00:00+08:00",
        classify_input: ClassifyWorkbenchInput | None = None,
        max_bytes: int = 262_144,
        enhance_classification: Callable[..., WorkbenchInputClassificationResult] | None = None,
        force_provider_enhancement: bool = False,
        run_document_text_extractor: Callable[[str], SourceContentReadResult] | None = None,
        run_image_ocr: Callable[[str], MediaProcessingQueueResult] | None = None,
        run_audio_auto_workflow: Callable[[str, str | None], AudioAutoWorkflowResult] | None = None,
        run_video_auto_workflow: Callable[[str], VideoAutoWorkflowResult] | None = None,
        prepare_file_source: Callable[[str, str], object] | None = None,
        prepare_video_source: Callable[[str, str], object] | None = None,
        run_long_audio_chunked_workflow: Callable[[str, str | None], LongAudioChunkedWorkflowResult] | None = None,
        audio_summarize_transcript: Callable[..., TranscriptSummaryResult] | None = None,
        audio_create_memory_candidate: Callable[..., SourceOutputMemoryCandidateResult] | None = None,
        task_model_map: Mapping[str, object] | None = None,
        memory_router: object | None = None,
        produce_candidate_jobs: Callable[[str, Sequence[WorkbenchAutoIntakeItem], str], Sequence[Mapping[str, object]]] | None = None,
        admit_content_transform: Callable[[Mapping[str, object], Sequence[WorkbenchAutoIntakeItem]], Mapping[str, object]] | None = None,
        admit_review_intents: Callable[[str, str, Sequence[WorkbenchAutoIntakeItem]], None] | None = None,
    ) -> None:
        self._object_store = object_store
        self._source_registrar = source_registrar
        self._job_repository = job_repository
        self._fetch_url = fetch_url
        self._namespace_id = namespace_id
        self._project_id = project_id
        self._now = now
        self._classify_input = classify_input or ClassifyWorkbenchInput()
        self._max_bytes = max_bytes
        self._enhance_classification = enhance_classification
        self._force_provider_enhancement = force_provider_enhancement
        # 阶段 1.5：自动记忆路由器，判断 memory_event_type 并生成 memory_delta
        from .memory_router import MemoryRouter as _MemoryRouter
        self._memory_router = memory_router or _MemoryRouter()
        # Phase 3: optional media auto-workflow callables. When None, the
        # orchestrator keeps the legacy "needs_extractor/needs_asr/needs_video_workflow"
        # status so behavior is backward-compatible. When provided, the orchestrator
        # triggers the workflow after capture and records the trace in
        # item.auto_organization["media_auto_workflow"].
        self._run_document_text_extractor = run_document_text_extractor
        self._run_image_ocr = run_image_ocr
        self._run_audio_auto_workflow = run_audio_auto_workflow
        self._run_video_auto_workflow = run_video_auto_workflow
        self._prepare_file_source = prepare_file_source
        self._prepare_video_source = prepare_video_source
        # 阶段 4：长音频分段转写工作流。当音频时长超过阈值时，走分段流程。
        self._run_long_audio_chunked_workflow = run_long_audio_chunked_workflow
        # Phase 3 音频链路补齐：转写完成后的 summarize / candidate / structure / tag / series
        # 回调。默认 None 时音频只做转写（向后兼容）。提供后，orchestrator 在 transcribe
        # 成功后自动串联摘要、记忆候选、结构化、标签索引、系列判断。
        self._audio_summarize_transcript = audio_summarize_transcript
        self._audio_create_memory_candidate = audio_create_memory_candidate
        # 3.13 验收第三条：task_model_map 能被 auto-intake 消费。解析为
        # model_profile_refs 写入 item.auto_organization，让用户在资料库看到
        # 「这条记忆用了哪个模型」。
        self._task_model_map = task_model_map
        self._produce_candidate_jobs = produce_candidate_jobs
        self._admit_content_transform = admit_content_transform
        self._admit_review_intents = admit_review_intents

    def execute(
        self,
        *,
        content: str = "",
        media_type: str = "",
        file_name: str = "",
        urls: Sequence[str] | None = None,
        add_to_knowledge_base: bool = True,
        title: str = "",
        classifier_prompt: Mapping[str, object] | None = None,
        child_inputs: Sequence[Mapping[str, object]] | None = None,
        media_duration_ms: int | None = None,
        original_asset_ref: str = "",
    ) -> WorkbenchAutoIntakeResult:
        clean_content = (content or "").strip()
        clean_media_type = (media_type or "").strip().lower()
        clean_file_name = (file_name or "").strip()
        clean_urls = tuple(url.strip() for url in urls or () if isinstance(url, str) and url.strip())
        clean_title = (title or "").strip()
        clean_original_asset_ref = (original_asset_ref or "").strip()

        if not add_to_knowledge_base:
            # 阶段 1.5：legacy 兼容字段。不再走独立的 direct_question 分支，
            # 所有输入都进入 L0 + Memory Router。保留字段仅为兼容旧前端。
            # 旧 hint 不再返回，避免误导用户「未加入知识库」。
            pass

        # 批量文件路径：跳过 classifier，直接走 file_collection 聚合。
        # child_inputs 由调用方（前端批量上传后）显式提供，每个 child 含
        # file_name/media_type/size_bytes/file_reference。
        if child_inputs:
            return self._with_memory_event(
                self._orchestrate_file_collection(
                    child_inputs=child_inputs,
                    clean_title=clean_title,
                ),
                content=clean_content,
                media_type="",
                file_name="",
                urls=(),
            )

        try:
            classification = self._classify_input.execute(
                content=clean_content,
                media_type=clean_media_type,
                file_name=clean_file_name,
                urls=clean_urls,
                classifier_prompt=classifier_prompt,
            )
        except ValueError as error:
            return self._failed_result(str(error), classification_payload={})

        classification = self._maybe_enhance_classification(
            classification=classification,
            content=clean_content,
            media_type=clean_media_type,
            file_name=clean_file_name,
            urls=clean_urls,
            classifier_prompt=classifier_prompt,
        )

        if classification.needs_user_confirmation:
            return self._with_memory_event(
                self._needs_confirmation_result(classification),
                content=clean_content,
                media_type=clean_media_type,
                file_name=clean_file_name,
                urls=clean_urls,
                input_type=classification.input_type,
                intent=classification.intent,
            )

        if classification.child_inputs and classification.input_type == "bookmark_collection":
            return self._with_memory_event(
                self._orchestrate_bookmark_collection(classification, clean_title),
                content=clean_content,
                media_type=clean_media_type,
                file_name=clean_file_name,
                urls=clean_urls,
                input_type=classification.input_type,
                intent=classification.intent,
            )

        return self._with_memory_event(
            self._orchestrate_single(
                classification=classification,
                clean_content=clean_content,
                clean_media_type=clean_media_type,
                clean_file_name=clean_file_name,
                clean_urls=clean_urls,
                clean_title=clean_title,
                media_duration_ms=media_duration_ms,
                original_asset_ref=clean_original_asset_ref,
            ),
            content=clean_content,
            media_type=clean_media_type,
            file_name=clean_file_name,
            urls=clean_urls,
            input_type=classification.input_type,
            intent=classification.intent,
        )

    def _with_memory_event(
        self,
        result: WorkbenchAutoIntakeResult,
        *,
        content: str,
        media_type: str,
        file_name: str,
        urls: Sequence[str] = (),
        input_type: str = "",
        intent: str = "",
    ) -> WorkbenchAutoIntakeResult:
        """阶段 1.5：调用 MemoryRouter，把 memory_event 附加到返回结果。

        所有输入都经过 Memory Router，判断 memory_event_type 并生成 memory_delta。
        失败时静默降级（不阻塞主流程），memory_event 保持 None。
        """
        try:
            router_result = self._memory_router.execute(
                content=content,
                media_type=media_type,
                file_name=file_name,
                urls=urls,
                input_type=input_type,
                intent=intent,
                now=self._now,
            )
            from .memory_router import serialize_memory_router_result
            memory_event = serialize_memory_router_result(router_result)
        except Exception:  # noqa: BLE001 - 路由失败不阻塞主流程
            memory_event = None
        # 用 dataclasses.replace 重建结果，保留原有字段
        import dataclasses
        return dataclasses.replace(result, memory_event=memory_event)

    def _direct_question_result(
        self,
        *,
        clean_content: str,
        clean_media_type: str,
        clean_file_name: str,
    ) -> WorkbenchAutoIntakeResult:
        hint = {
            "route": "direct_question",
            "reason": "未勾选加入知识库，仅返回本地问答结果，不创建 Source、候选或长期记忆。",
            "source_created": False,
            "memory_candidate_created": False,
            "memory_publication_created": False,
        }
        return WorkbenchAutoIntakeResult(
            status="direct_question",
            job_id="",
            items=(),
            classification={
                "content_preview": _safe_preview(clean_content),
                "media_type": clean_media_type,
                "file_name": _basename_only(clean_file_name),
            },
            next_ui="direct_question",
            direct_question_hint=hint,
            error=None,
        )

    def _maybe_enhance_classification(
        self,
        *,
        classification: WorkbenchInputClassificationResult,
        content: str,
        media_type: str,
        file_name: str,
        urls: Sequence[str],
        classifier_prompt: Mapping[str, object] | None,
    ) -> WorkbenchInputClassificationResult:
        """Call the provider enhancer when local confidence is low or input is complex.

        Phase 2 wires the authorized main-model provider into the unified auto-intake
        orchestrator. Enhancement is triggered when:
          - force_provider_enhancement is set (user or settings forced), OR
          - local classifier recommended enhancement, OR
          - child_inputs is non-empty (multi-link bookmark collection), OR
          - confidence < 0.8

        Provider payload only contains redacted preview, basename and urls. On
        ValueError (schema rejected, forbidden secret markers, network error) the
        orchestrator degrades to the local classification and records the reason so
        the UI can surface a blocked/degraded state instead of crashing.
        """
        if self._enhance_classification is None:
            return classification
        should_enhance = (
            self._force_provider_enhancement
            or classification.provider_enhancement_recommended
            or bool(classification.child_inputs)
            or classification.confidence < 0.8
        )
        if not should_enhance:
            return classification
        try:
            return self._enhance_classification(
                local_result=classification,
                content=content,
                media_type=media_type,
                file_name=file_name,
                urls=urls,
                classifier_prompt=classifier_prompt,
            )
        except ValueError:
            # Provider unavailable, schema rejected or forbidden material detected.
            # Degrade to local classification; the needs_confirmation path downstream
            # will surface the low-confidence state to the user.
            return classification

    def _needs_confirmation_result(
        self,
        classification: WorkbenchInputClassificationResult,
    ) -> WorkbenchAutoIntakeResult:
        item = WorkbenchAutoIntakeItem(
            source_id="",
            source_uri="",
            input_type=classification.input_type,
            workflow=classification.auto_workflow or "",
            status="needs_confirmation",
            needs_user_confirmation=True,
            title="",
            content_read_status="not_started",
            structure_status="not_started",
            series_status="not_started",
            series_name="",
            series_confidence=classification.confidence,
            inspiration_status="not_started",
            auto_organization={
                "confidence": classification.confidence,
                "provider_enhancement_recommended": classification.provider_enhancement_recommended,
                "provider_enhancement_reason": classification.provider_enhancement_reason,
                "workflow_steps": list(classification.workflow_steps),
            },
            next_step="await_user_confirmation",
        )
        return WorkbenchAutoIntakeResult(
            status="needs_confirmation",
            job_id="",
            items=(item,),
            classification=serialize_workbench_input_classification(classification),
            next_ui="confirmation_required",
            direct_question_hint=None,
            error=None,
        )

    def _orchestrate_bookmark_collection(
        self,
        classification: WorkbenchInputClassificationResult,
        clean_title: str,
    ) -> WorkbenchAutoIntakeResult:
        child_inputs = tuple(
            child for child in classification.child_inputs
            if isinstance(child, Mapping) and (child.get("raw_input") or child.get("url"))
        )
        if not child_inputs:
            return self._failed_result(
                "bookmark collection has no usable child links",
                classification_payload=serialize_workbench_input_classification(classification),
            )
        collection_title = clean_title or "收藏夹"
        urls = tuple(
            str(child.get("raw_input") or child.get("url"))
            for child in child_inputs
        )
        capture = CaptureWorkbenchBookmarkCollection(
            source_registrar=self._source_registrar,
            job_repository=self._job_repository,
            namespace_id=self._namespace_id,
            now=self._now,
        )
        collection_result = capture.execute(title=collection_title, urls=urls)
        job_id = f"job-intake-{collection_result.source_id}"
        items: list[WorkbenchAutoIntakeItem] = []
        for child_source_id, child_url in zip(collection_result.child_source_ids, urls):
            item = self._organize_link_source(
                source_id=child_source_id,
                url=child_url,
                input_type="webpage",
                workflow="link_auto_organization",
            )
            items.append(item)
        self._write_intake_job(job_id, collection_result.source_id, items, label="bookmark_collection")
        return WorkbenchAutoIntakeResult(
            status="accepted",
            job_id=job_id,
            items=tuple(items),
            classification=serialize_workbench_input_classification(classification),
            next_ui="library_job_status",
            direct_question_hint=None,
            error=None,
        )

    def _orchestrate_file_collection(
        self,
        *,
        child_inputs: Sequence[Mapping[str, object]],
        clean_title: str,
    ) -> WorkbenchAutoIntakeResult:
        """批量文件聚合路径，复用 bookmark_collection 的 parent/child 模式。

        与 ``_orchestrate_bookmark_collection`` 对称：
        - 注册 N 个 ``kind=file`` child source（含 asset reference）
        - 注册 1 个 ``type=collection`` parent source（metadata.collection_type="file_collection"）
        - 写 1 个 parent job（job_type=workbench_auto_intake），steps[] 列出每个 child

        每个 child 走 ``_capture_file_source``，因此 ``run_document_text_extractor`` 等
        media auto workflow 回调同样会在批量场景下触发（若已注入）。
        """
        clean_children: list[dict[str, object]] = []
        for raw in child_inputs:
            if not isinstance(raw, Mapping):
                continue
            file_name = str(raw.get("file_name") or raw.get("display_name") or "").strip()
            file_reference = str(raw.get("file_reference") or raw.get("asset_ref") or "").strip()
            if not file_name or not file_reference:
                continue
            media_type = str(raw.get("media_type") or "application/octet-stream").strip().lower()
            try:
                size_bytes = int(raw.get("size_bytes") or 0)
            except (TypeError, ValueError):
                size_bytes = 0
            if size_bytes < 0:
                size_bytes = 0
            title = str(raw.get("title") or file_name).strip() or file_name
            clean_children.append(
                {
                    "title": title,
                    "file_name": file_name,
                    "media_type": media_type,
                    "size_bytes": size_bytes,
                    "file_reference": file_reference,
                }
            )
        if not clean_children:
            return self._failed_result(
                "file collection has no usable child files (each needs file_name and file_reference)",
                classification_payload={"input_type": "file_collection"},
            )

        if clean_title:
            collection_title = clean_title
        elif len(clean_children) == 1:
            collection_title = str(clean_children[0]["title"])
        else:
            collection_title = "文件批量"

        items: list[WorkbenchAutoIntakeItem] = []
        child_source_ids: list[str] = []
        child_source_uris: list[str] = []
        for child in clean_children:
            capture = CaptureWorkbenchFileSource(
                source_registrar=self._source_registrar,
                job_repository=self._job_repository,
                namespace_id=self._namespace_id,
                now=self._now,
            )
            capture_result = capture.execute(
                title=str(child["title"]),
                display_name=str(child["file_name"]),
                media_type=str(child["media_type"]),
                size_bytes=int(child["size_bytes"]),
                file_reference=str(child["file_reference"]),
            )
            item = WorkbenchAutoIntakeItem(
                source_id=capture_result.source_id,
                source_uri=capture_result.source_uri,
                input_type="file",
                workflow="document_text_extraction",
                status="needs_extractor",
                needs_user_confirmation=False,
                title=capture_result.source_title,
                content_read_status="not_started",
                structure_status="not_started",
                series_status="not_started",
                series_name="",
                series_confidence=0.0,
                inspiration_status="not_started",
                auto_organization={
                    "asset_id": capture_result.asset_id,
                    "asset_storage_mode": capture_result.asset_storage_mode,
                    "asset_availability": capture_result.asset_availability,
                    "memory_publication_state": "not_published",
                    "orchestrator_version": self._ORCHESTRATOR_VERSION,
                    "path_policy": "no_os_absolute_path_in_response",
                },
                next_step="await_document_extractor",
            )
            item = self._maybe_run_media_auto_workflow(
                item,
                workflow_kind="document_text_extraction",
                asset_id=item.auto_organization.get("asset_id"),
            )
            items.append(item)
            child_source_ids.append(item.source_id)
            child_source_uris.append(item.source_uri)

        parent_source_id, parent_source_uri = self._write_file_collection_parent_source(
            collection_title=collection_title,
            child_source_ids=child_source_ids,
            child_source_uris=child_source_uris,
        )
        job_id = f"job-intake-{parent_source_id}"
        self._write_intake_job(job_id, parent_source_id, items, label="file_collection")
        return WorkbenchAutoIntakeResult(
            status="accepted",
            job_id=job_id,
            items=tuple(items),
            classification={
                "input_type": "file_collection",
                "child_count": len(items),
                "collection_type": "file_collection",
            },
            next_ui="library_job_status",
            direct_question_hint=None,
            error=None,
        )

    def _write_file_collection_parent_source(
        self,
        *,
        collection_title: str,
        child_source_ids: list[str],
        child_source_uris: list[str],
    ) -> tuple[str, str]:
        """注册 type=collection 的 parent source，聚合多个 file child source。

        digest 基于 child_source_ids 拼接，与 bookmark_collection（基于 urls）
        的 digest 空间不冲突。metadata.collection_type 标记为 ``file_collection``
        以便下游区分。
        """
        digest_basis = "\n".join(child_source_ids).encode("utf-8")
        digest = hashlib.sha256(digest_basis).hexdigest()
        source_id = f"source-collection-{digest[:12]}"
        source_uri = f"crp://{self._namespace_id}/sources/{source_id}"
        record = {
            "schema_version": "1.0.0",
            "id": source_id,
            "project_id": self._project_id,
            "type": "collection",
            "title": collection_title,
            "capture_mode": "reference_batch",
            "storage_uri": source_uri,
            "original_url": None,
            "content_hash": digest,
            "media_type": "application/vnd.chriptmas.file-collection+json",
            "size_bytes": len(digest_basis),
            "parser_version": None,
            "processing_state": "captured",
            "created_at": self._now,
            "imported_from_legacy": False,
            "trust_status": "user_confirmed",
            "metadata": {
                "collection_type": "file_collection",
                "item_count": len(child_source_ids),
                "child_source_ids": list(child_source_ids),
                "child_source_uris": list(child_source_uris),
                "content_hash_basis": "child_source_ids_concat",
                "remote_fetch": "not_performed",
                "content_snapshot": None,
            },
        }
        self._object_store.write("sources", source_id, record, expected_revision=None)
        return source_id, source_uri

    def _orchestrate_single(
        self,
        *,
        classification: WorkbenchInputClassificationResult,
        clean_content: str,
        clean_media_type: str,
        clean_file_name: str,
        clean_urls: Sequence[str],
        clean_title: str,
        media_duration_ms: int | None = None,
        original_asset_ref: str = "",
    ) -> WorkbenchAutoIntakeResult:
        input_type = classification.input_type
        target_intake = classification.target_intake
        title = clean_title or _derive_title(clean_content, clean_urls, clean_file_name)

        if target_intake == "text_source_intake":
            item = self._capture_and_organize_text(title=title, content=clean_content)
        elif target_intake in {"link_source_intake", "video_link_download_or_link_source_intake"}:
            if target_intake == "video_link_download_or_link_source_intake" and self._admit_content_transform is not None:
                # A URL Source has no local video asset authorization. The reviewed media work item owns this path.
                raise ValueError("video_link_requires_workspace_review")
            url = select_workbench_link_target(content=clean_content, urls=clean_urls)
            if not url:
                return self._failed_result(
                    "link intake requires a url",
                    classification_payload=serialize_workbench_input_classification(classification),
                )
            source_id = self._capture_link_source(title=title, url=url)
            if self._admit_content_transform is not None:
                item = self._defer_link_source(
                    source_id=source_id,
                    url=url,
                    input_type=input_type,
                    workflow=classification.auto_workflow or "link_auto_organization",
                )
            else:
                item = self._organize_link_source(
                    source_id=source_id,
                    url=url,
                    input_type=input_type,
                    workflow=classification.auto_workflow or "link_auto_organization",
                )
        elif target_intake == "file_source_intake":
            item = self._capture_file_source(
                title=title,
                display_name=clean_file_name,
                media_type=clean_media_type,
                input_type=input_type,
                workflow=classification.auto_workflow or "document_text_extraction",
                original_asset_ref=original_asset_ref,
            )
        elif target_intake == "image_source_intake":
            item = self._capture_image_source(
                title=title,
                display_name=clean_file_name,
                media_type=clean_media_type,
                input_type=input_type,
                workflow=classification.auto_workflow or "image_ocr",
                original_asset_ref=original_asset_ref,
            )
        elif target_intake == "audio_source_intake":
            item = self._capture_audio_source(
                title=title,
                display_name=clean_file_name,
                media_type=clean_media_type,
                input_type=input_type,
                workflow=classification.auto_workflow or "audio_auto_workflow",
                media_duration_ms=media_duration_ms,
                original_asset_ref=original_asset_ref,
            )
        elif target_intake == "video_source_intake":
            item = self._capture_video_source(
                title=title,
                display_name=clean_file_name,
                media_type=clean_media_type,
                input_type=input_type,
                workflow=classification.auto_workflow or "video_auto_workflow",
                original_asset_ref=original_asset_ref,
            )
        else:
            return self._failed_result(
                f"unsupported target_intake: {target_intake}",
                classification_payload=serialize_workbench_input_classification(classification),
            )

        job_id = f"job-intake-{item.source_id}"
        transform_queued = self._write_intake_job(
            job_id, item.source_id, [item], label=input_type,
        )
        if transform_queued:
            item = replace(
                item,
                status="queued",
                next_step="await_background_transform",
                auto_organization={
                    **dict(item.auto_organization),
                    "background_transform": {
                        "status": "queued",
                        "job_id": job_id,
                        "execution_version": "effect-v2",
                    },
                },
            )
        return WorkbenchAutoIntakeResult(
            status="accepted",
            job_id=job_id,
            items=(item,),
            classification=serialize_workbench_input_classification(classification),
            next_ui="library_job_status",
            direct_question_hint=None,
            error=None,
        )

    def _capture_and_organize_text(self, *, title: str, content: str) -> WorkbenchAutoIntakeItem:
        capture = CaptureWorkbenchTextSource(
            source_registrar=self._source_registrar,
            job_repository=self._job_repository,
            namespace_id=self._namespace_id,
            now=self._now,
        )
        result = capture.execute(title=title, content=content)
        return self._organize_text_source(
            source_id=result.source_id,
            source_uri=result.source_uri,
            title=result.source_title,
            input_type="direct_idea",
            workflow="text_auto_organization",
        )

    def _capture_link_source(self, *, title: str, url: str) -> str:
        capture = CaptureWorkbenchLinkSource(
            source_registrar=self._source_registrar,
            job_repository=self._job_repository,
            namespace_id=self._namespace_id,
            now=self._now,
        )
        result = capture.execute(title=title, url=url)
        return result.source_id

    def _organize_link_source(
        self,
        *,
        source_id: str,
        url: str,
        input_type: str,
        workflow: str,
    ) -> WorkbenchAutoIntakeItem:
        source = self._object_store.read("sources", source_id)
        source_uri = _required_str(source, "storage_uri") if source else ""
        title = _required_str(source, "title") if source else url
        reader = ReadLinkWebContent(
            self._object_store,
            fetch_url=self._fetch_url,
            namespace_id=self._namespace_id,
            now=self._now,
            max_bytes=self._max_bytes,
        )
        try:
            read_result = reader.execute(source_id=source_id)
        except Exception as error:  # noqa: BLE001 - orchestrator must not crash on reader failure
            return self._failed_item(
                source_id=source_id,
                source_uri=source_uri,
                input_type=input_type,
                workflow=workflow,
                title=title,
                error=f"web content read failed: {error}",
            )
        if read_result.status != "completed":
            return self._item_from_partial_read(
                source_id=source_id,
                source_uri=source_uri,
                input_type=input_type,
                workflow=workflow,
                title=title,
                read_status=read_result.status,
                read_error=read_result.error or "",
            )
        return self._structure_and_assign_series(
            source_id=source_id,
            source_uri=source_uri,
            input_type=input_type,
            workflow=workflow,
            title=title,
            content_read_id=_content_read_id_from_ref(read_result.read_ref),
        )

    def _defer_link_source(
        self,
        *,
        source_id: str,
        url: str,
        input_type: str,
        workflow: str,
    ) -> WorkbenchAutoIntakeItem:
        """Return after recording a URL Source; Core owns the remote read."""
        source = self._object_store.read("sources", source_id)
        return WorkbenchAutoIntakeItem(
            source_id=source_id,
            source_uri=_required_str(source, "storage_uri") if source else "",
            input_type=input_type,
            workflow=workflow,
            status="needs_extractor",
            needs_user_confirmation=False,
            title=_required_str(source, "title") if source else url,
            content_read_status="not_started",
            structure_status="not_started",
            series_status="not_started",
            series_name="",
            series_confidence=0.0,
            inspiration_status="not_started",
            auto_organization={
                "remote_fetch": "deferred_to_content_transform",
                "memory_publication_state": "not_published",
                "orchestrator_version": self._ORCHESTRATOR_VERSION,
            },
            next_step="await_background_transform",
        )

    def _organize_text_source(
        self,
        *,
        source_id: str,
        source_uri: str,
        title: str,
        input_type: str,
        workflow: str,
    ) -> WorkbenchAutoIntakeItem:
        reader = ReadSourceTextContent(
            self._object_store,
            namespace_id=self._namespace_id,
            now=self._now,
            max_bytes=self._max_bytes,
        )
        try:
            read_result = reader.execute(source_id=source_id)
        except Exception as error:  # noqa: BLE001 - orchestrator must not crash on reader failure
            return self._failed_item(
                source_id=source_id,
                source_uri=source_uri,
                input_type=input_type,
                workflow=workflow,
                title=title,
                error=f"text content read failed: {error}",
            )
        if read_result.status != "completed":
            return self._item_from_partial_read(
                source_id=source_id,
                source_uri=source_uri,
                input_type=input_type,
                workflow=workflow,
                title=title,
                read_status=read_result.status,
                read_error=read_result.error or "",
            )
        return self._structure_and_assign_series(
            source_id=source_id,
            source_uri=source_uri,
            input_type=input_type,
            workflow=workflow,
            title=title,
            content_read_id=_content_read_id_from_ref(read_result.read_ref),
        )

    def _structure_and_assign_series(
        self,
        *,
        source_id: str,
        source_uri: str,
        input_type: str,
        workflow: str,
        title: str,
        content_read_id: str,
    ) -> WorkbenchAutoIntakeItem:
        structurer = StructureSourceContent(
            self._object_store,
            namespace_id=self._namespace_id,
            now=self._now,
        )
        try:
            structure_result = structurer.execute(source_id=source_id, content_read_id=content_read_id)
        except Exception as error:  # noqa: BLE001 - orchestrator must not crash on structuring failure
            return self._failed_item(
                source_id=source_id,
                source_uri=source_uri,
                input_type=input_type,
                workflow=workflow,
                title=title,
                error=f"content structuring failed: {error}",
            )

        series_status, series_name = self._assign_series(
            source_id=source_id,
            series_candidate=structure_result.series_candidate,
            series_confidence=structure_result.series_confidence,
        )
        inspiration_status = self._maybe_record_inspiration(
            source_id=source_id,
            content_read_id=content_read_id,
        )
        tag_index_status = self._index_tags(
            source_id=source_id,
            structure_ref=structure_result.structure_ref,
        )
        auto_organization = {
            "content_read_id": content_read_id,
            "structure_ref": structure_result.structure_ref,
            "summary": structure_result.summary,
            "tags": list(structure_result.tags),
            "key_points": list(structure_result.key_points),
            "series_candidate": structure_result.series_candidate,
            "series_confidence": structure_result.series_confidence,
            "series_reason": structure_result.series_reason,
            "tag_index_status": tag_index_status,
            "memory_publication_state": "not_published",
            "orchestrator_version": self._ORCHESTRATOR_VERSION,
            "model_profile_refs": list(self._resolve_model_profile_refs()),
        }
        item_status: WorkbenchAutoIntakeItemStatus = (
            "completed" if series_status == "confirmed"
            else "completed_pending_series" if series_status == "pending_confirmation"
            else "completed"
        )
        return WorkbenchAutoIntakeItem(
            source_id=source_id,
            source_uri=source_uri,
            input_type=input_type,
            workflow=workflow,
            status=item_status,
            needs_user_confirmation=False,
            title=title,
            content_read_status="completed",
            structure_status="completed",
            series_status=series_status,
            series_name=series_name,
            series_confidence=structure_result.series_confidence,
            inspiration_status=inspiration_status,
            auto_organization=auto_organization,
            next_step="library_overview_refresh",
        )

    def _assign_series(
        self,
        *,
        source_id: str,
        series_candidate: str,
        series_confidence: float,
    ) -> tuple[str, str]:
        if not series_candidate or series_candidate == "未归类资料":
            return "pending_confirmation", ""
        if series_confidence < _HIGH_CONFIDENCE_SERIES_THRESHOLD:
            return "pending_confirmation", series_candidate
        assigner = ConfirmSourceSeriesAssignment(
            self._object_store,
            namespace_id=self._namespace_id,
            now=self._now,
        )
        try:
            assignment = assigner.execute(
                source_id=source_id,
                confirm=False,
                series_name=series_candidate,
                reason="高置信度系列候选由系统自动确认，保留用户后续调整入口。",
                confirmed_by="system",
            )
            return "confirmed", assignment.series_name
        except Exception:  # noqa: BLE001 - auto series assignment must not block intake
            return "pending_confirmation", series_candidate

    def _maybe_record_inspiration(self, *, source_id: str, content_read_id: str) -> str:
        recorder = RecordInspirationFromSource(
            self._object_store,
            namespace_id=self._namespace_id,
            now=self._now,
        )
        source = self._object_store.read("sources", source_id)
        if source is None:
            return "skipped"
        metadata = source.get("metadata")
        if not isinstance(metadata, Mapping):
            return "skipped"
        content = metadata.get("content")
        text = content if isinstance(content, str) and content else ""
        try:
            record = recorder.execute(
                source_id=source_id,
                content_read_id=content_read_id,
                text=text,
                project_id=self._project_id,
                theme_hint="灵感",
            )
            return record.status if hasattr(record, "status") else "recorded"
        except Exception:  # noqa: BLE001 - inspiration detection is optional
            return "skipped"

    def _index_tags(self, *, source_id: str, structure_ref: str) -> str:
        indexer = IndexSourceTags(
            self._object_store,
            namespace_id=self._namespace_id,
            now=self._now,
        )
        structure_id = _structure_id_from_ref(structure_ref) or f"structure-{source_id}"
        try:
            result = indexer.execute(source_id=source_id, structure_id=structure_id)
            return "indexed" if result.indexed_tags else "empty"
        except Exception:  # noqa: BLE001 - tag indexing is optional and must not block intake
            return "skipped"

    def _resolve_model_profile_refs(self) -> tuple[dict[str, object], ...]:
        """从 task_model_map 解析 auto-intake 使用的 model_profile 引用列表。

        auto-intake 主要消费 intakeMain / memory / lightweight 三个 use key。
        task_model_map 为 None 或空时返回空 tuple（向后兼容）。
        """
        if not self._task_model_map:
            return ()
        resolver = TaskModelMapResolver(self._task_model_map)
        return resolver.refs_for(("intakeMain", "memory", "lightweight"))

    def _capture_file_source(
        self,
        *,
        title: str,
        display_name: str,
        media_type: str,
        input_type: str,
        workflow: str,
        original_asset_ref: str = "",
    ) -> WorkbenchAutoIntakeItem:
        item = self._capture_media_reference(
            title=title,
            display_name=display_name,
            media_type=media_type,
            input_type=input_type,
            workflow=workflow,
            next_status="needs_extractor",
            next_step="await_document_extractor",
            original_asset_ref=original_asset_ref,
        )
        if (
            original_asset_ref
            and not item.auto_organization.get("frozen_source_authorization_ready")
            and self._prepare_file_source is not None
        ):
            try:
                self._prepare_file_source(item.source_id, original_asset_ref)
            except Exception as error:  # noqa: BLE001 - preparation failure must remain visible and bounded.
                return self._item_with_media_workflow_trace(
                    item,
                    {
                        "workflow_kind": "document_text_extraction",
                        "status": "failed",
                        "triggered": True,
                        "error": str(error) or error.__class__.__name__,
                        "blocked_reason": "file_source_preparation_failed",
                    },
                    completed=False,
                )
        return self._maybe_run_media_auto_workflow(
            item,
            workflow_kind="document_text_extraction",
            asset_id=item.auto_organization.get("asset_id"),
        )

    def _capture_image_source(
        self,
        *,
        title: str,
        display_name: str,
        media_type: str,
        input_type: str,
        workflow: str,
        original_asset_ref: str = "",
    ) -> WorkbenchAutoIntakeItem:
        item = self._capture_media_reference(
            title=title,
            display_name=display_name,
            media_type=media_type,
            input_type=input_type,
            workflow=workflow,
            next_status="needs_extractor",
            next_step="await_ocr_or_vision_provider",
            original_asset_ref=original_asset_ref,
        )
        if (
            original_asset_ref
            and not item.auto_organization.get("frozen_source_authorization_ready")
            and self._prepare_file_source is not None
        ):
            self._prepare_file_source(item.source_id, original_asset_ref)
        if self._admit_content_transform is not None:
            return item
        return self._maybe_run_media_auto_workflow(
            item,
            workflow_kind="image_ocr",
            asset_id=item.auto_organization.get("asset_id"),
        )

    def _capture_audio_source(
        self,
        *,
        title: str,
        display_name: str,
        media_type: str,
        input_type: str,
        workflow: str,
        media_duration_ms: int | None = None,
        original_asset_ref: str = "",
    ) -> WorkbenchAutoIntakeItem:
        item = self._capture_media_reference(
            title=title,
            display_name=display_name,
            media_type=media_type,
            input_type=input_type,
            workflow=workflow,
            next_status="needs_asr",
            next_step="await_asr_provider",
            original_asset_ref=original_asset_ref,
        )
        # 阶段 4：将音频时长写入 audio_asset_refs，供长音频检测（_is_long_audio）使用。
        # 前端上传时会提取音频时长（毫秒），这里持久化为秒，让 orchestrator 能判断是否走分段流程。
        asset_id = item.auto_organization.get("asset_id")
        if isinstance(asset_id, str):
            duration_seconds = (float(media_duration_ms) / 1000.0) if media_duration_ms else 0.0
            self._object_store.write(
                "audio_asset_refs",
                asset_id,
                {
                    "schema_version": "1.0.0",
                    "id": asset_id,
                    "source_id": item.source_id,
                    "duration_seconds": duration_seconds,
                    "media_type": media_type or "audio/mpeg",
                    "status": "available",
                    "is_chunk": False,
                    "created_at": self._now,
                },
                expected_revision=None,
            )
        if (
            original_asset_ref
            and not item.auto_organization.get("frozen_source_authorization_ready")
            and self._prepare_file_source is not None
        ):
            self._prepare_file_source(item.source_id, original_asset_ref)
        if self._admit_content_transform is not None:
            return item
        return self._maybe_run_media_auto_workflow(
            item,
            workflow_kind="audio_auto_workflow",
            asset_id=item.auto_organization.get("asset_id"),
        )

    def _capture_video_source(
        self,
        *,
        title: str,
        display_name: str,
        media_type: str,
        input_type: str,
        workflow: str,
        original_asset_ref: str = "",
    ) -> WorkbenchAutoIntakeItem:
        item = self._capture_media_reference(
            title=title,
            display_name=display_name,
            media_type=media_type,
            input_type=input_type,
            workflow=workflow,
            next_status="needs_video_workflow",
            next_step="await_video_auto_workflow",
            original_asset_ref=original_asset_ref,
        )
        if (
            original_asset_ref
            and not item.auto_organization.get("frozen_source_authorization_ready")
            and self._prepare_video_source is not None
        ):
            try:
                self._prepare_video_source(item.source_id, original_asset_ref)
            except Exception as error:  # noqa: BLE001 - preparation failure must remain visible and bounded.
                return self._item_with_media_workflow_trace(
                    item,
                    {
                        "workflow_kind": "video_auto_workflow",
                        "status": "failed",
                        "triggered": True,
                        "error": str(error) or error.__class__.__name__,
                        "blocked_reason": "video_source_preparation_failed",
                    },
                    completed=False,
                )
        return self._maybe_run_media_auto_workflow(
            item,
            workflow_kind="video_auto_workflow",
            asset_id=item.auto_organization.get("asset_id"),
        )

    def _maybe_run_media_auto_workflow(
        self,
        item: WorkbenchAutoIntakeItem,
        *,
        workflow_kind: str,
        asset_id: object,
    ) -> WorkbenchAutoIntakeItem:
        """Phase 3: trigger the corresponding media auto-workflow after capture.

        When the callable for ``workflow_kind`` is None the item is returned
        unchanged so behavior stays backward-compatible with pre-Phase-3 code
        (status remains needs_extractor / needs_asr / needs_video_workflow and
        the UI surfaces the await_* next_step). When the callable is provided
        the orchestrator triggers it and records the trace in
        ``auto_organization["media_auto_workflow"]``. On success the item is
        upgraded to ``completed``; on blocked/failed the original needs_*
        status is kept so the UI still surfaces the blocked reason.
        """
        callable_: Callable[..., object] | None
        if workflow_kind == "document_text_extraction":
            callable_ = self._run_document_text_extractor
        elif workflow_kind == "image_ocr":
            callable_ = self._run_image_ocr
        elif workflow_kind == "audio_auto_workflow":
            callable_ = self._run_audio_auto_workflow
        elif workflow_kind == "video_auto_workflow":
            callable_ = self._run_video_auto_workflow
        else:
            return item

        # 阶段 4：长音频分段检测优先于 callable_ is None 检查。
        # 当 run_long_audio_chunked_workflow 可用且音频时长超过阈值时，
        # 即使 run_audio_auto_workflow 未配置，也走分段流程。
        if (
            workflow_kind == "audio_auto_workflow"
            and self._run_long_audio_chunked_workflow is not None
            and self._is_long_audio(asset_id)
        ):
            source_id = item.source_id
            audio_asset_id = asset_id if isinstance(asset_id, str) else None
            trace: dict[str, object] = {
                "workflow_kind": workflow_kind,
                "status": "running",
                "triggered": True,
            }
            return self._run_long_audio_chunked_branch(
                item, trace, source_id=source_id, audio_asset_id=audio_asset_id
            )

        if callable_ is None:
            return item

        source_id = item.source_id
        audio_asset_id = asset_id if isinstance(asset_id, str) else None
        trace: dict[str, object] = {
            "workflow_kind": workflow_kind,
            "status": "running",
            "triggered": True,
        }
        try:
            if workflow_kind == "audio_auto_workflow":
                workflow_result = callable_(source_id, audio_asset_id)
            else:
                workflow_result = callable_(source_id)
        except Exception as error:  # noqa: BLE001 - media workflow failures must not crash intake
            trace["status"] = "failed"
            trace["error"] = str(error) or error.__class__.__name__
            trace["blocked_reason"] = "media_workflow_exception"
            return self._item_with_media_workflow_trace(item, trace, completed=False)

        trace.update(self._summarize_media_workflow_result(workflow_result, workflow_kind=workflow_kind))
        completed = str(trace.get("status", "")) == "completed"

        # Phase 3 音频链路补齐：转写成功后自动串联 summarize / candidate / structure / tag / series。
        # 仅在 audio_auto_workflow 且 transcribe 成功且至少一个 post-transcribe 回调可用时触发。
        audio_post: dict[str, object] | None = None
        if (
            workflow_kind == "audio_auto_workflow"
            and completed
            and getattr(workflow_result, "transcript_output_id", None)
            and (self._audio_summarize_transcript or self._audio_create_memory_candidate)
        ):
            audio_post = self._run_audio_post_transcribe(
                workflow_result=workflow_result,
                source_id=source_id,
                project_id=self._project_id,
            )
            extra_steps = audio_post.pop("extra_steps", [])
            if extra_steps:
                existing_steps = list(trace.get("steps", [])) + extra_steps
                trace["steps"] = existing_steps
            trace["audio_post_transcribe"] = audio_post
            # 合并关键字段到 trace 顶层，供 workflow_steps 适配器读取
            for key in (
                "summary_output_id",
                "memory_candidate_id",
                "structure_status",
                "tag_index_status",
                "series_status",
                "series_name",
                "series_confidence",
                "memory_publication_state",
                "summary",
                "tags",
            ):
                if key in audio_post:
                    trace[key] = audio_post[key]

        if audio_post:
            return self._item_with_audio_post_transcribe(item, trace, audio_post, completed=completed)
        return self._item_with_media_workflow_trace(item, trace, completed=completed)

    def _is_long_audio(self, asset_id: object) -> bool:
        """阶段 4：检测是否为长音频（时长超过阈值）。"""
        if not isinstance(asset_id, str):
            return False
        asset = self._object_store.read("audio_asset_refs", asset_id)
        if asset is None:
            return False
        duration = asset.get("duration_seconds")
        if not isinstance(duration, (int, float)):
            return False
        from .long_audio_chunker import LONG_AUDIO_THRESHOLD_SECONDS
        return float(duration) >= LONG_AUDIO_THRESHOLD_SECONDS

    def _run_long_audio_chunked_branch(
        self,
        item: WorkbenchAutoIntakeItem,
        trace: dict[str, object],
        *,
        source_id: str,
        audio_asset_id: str | None,
    ) -> WorkbenchAutoIntakeItem:
        """阶段 4：长音频分段转写分支。

        调用分段工作流，将结果适配为统一 trace 格式，
        并在转写完成后继续走 post-transcribe 链路（摘要/标签/系列/候选）。
        """
        assert self._run_long_audio_chunked_workflow is not None  # 已由调用方检查
        try:
            chunked_result = self._run_long_audio_chunked_workflow(source_id, audio_asset_id)
        except Exception as error:  # noqa: BLE001
            trace["status"] = "failed"
            trace["error"] = str(error) or error.__class__.__name__
            trace["blocked_reason"] = "long_audio_chunked_exception"
            return self._item_with_media_workflow_trace(item, trace, completed=False)

        # 适配 chunked result 到统一 trace
        trace["workflow_kind"] = "audio_auto_workflow"
        trace["is_long_audio"] = True
        trace["is_chunked"] = True
        trace["status"] = "completed" if chunked_result.status in ("completed", "partial_completed") else chunked_result.status
        trace["workflow_id"] = chunked_result.workflow_id
        trace["total_duration_seconds"] = chunked_result.total_duration_seconds
        trace["completed_duration_seconds"] = chunked_result.completed_duration_seconds
        trace["progress"] = chunked_result.progress
        trace["chunk_count"] = chunked_result.chunk_count
        trace["completed_chunk_count"] = chunked_result.completed_chunk_count
        trace["failed_chunk_count"] = chunked_result.failed_chunk_count
        trace["is_partial_result"] = chunked_result.is_partial_result
        trace["chunks"] = [
            {
                "chunk_index": c.chunk_index,
                "status": c.status,
                "start_seconds": c.start_seconds,
                "end_seconds": c.end_seconds,
                "duration_seconds": c.duration_seconds,
                "error": c.error,
                "retry_count": c.retry_count,
            }
            for c in chunked_result.chunks
        ]
        if chunked_result.error:
            trace["error"] = chunked_result.error
        if chunked_result.readiness_reason:
            trace["blocked_reason"] = chunked_result.readiness_reason

        completed = chunked_result.status in ("completed", "partial_completed")
        merged_output_id = chunked_result.merged_transcript_output_id

        # 如果有合并后的转写文本，继续走 post-transcribe 链路
        audio_post: dict[str, object] | None = None
        if (
            completed
            and merged_output_id
            and (self._audio_summarize_transcript or self._audio_create_memory_candidate)
        ):
            # 创建一个适配对象，让 _run_audio_post_transcribe 能读取 transcript_output_id
            adapter = _ChunkedWorkflowAdapter(
                transcript_output_id=merged_output_id,
                source_id=source_id,
            )
            audio_post = self._run_audio_post_transcribe(
                workflow_result=adapter,
                source_id=source_id,
                project_id=self._project_id,
            )
            extra_steps = audio_post.pop("extra_steps", [])
            if extra_steps:
                trace["steps"] = list(trace.get("steps", [])) + extra_steps
            trace["audio_post_transcribe"] = audio_post
            for key in (
                "summary_output_id", "memory_candidate_id", "structure_status",
                "tag_index_status", "series_status", "series_name",
                "series_confidence", "memory_publication_state", "summary", "tags",
            ):
                if key in audio_post:
                    trace[key] = audio_post[key]

        if audio_post:
            return self._item_with_audio_post_transcribe(item, trace, audio_post, completed=completed)
        return self._item_with_media_workflow_trace(item, trace, completed=completed)

    @staticmethod
    def _summarize_media_workflow_result(
        result: object,
        *,
        workflow_kind: str,
    ) -> dict[str, object]:
        """Build a redacted trace dict from a media auto-workflow result.

        Only safe fields are copied: status, workflow_id, steps (name+status+reason),
        next_step, blocked_operations, error. Local paths and provider secrets
        are not propagated; the underlying use cases already redact them.
        """
        trace: dict[str, object] = {"workflow_kind": workflow_kind, "triggered": True}
        status = getattr(result, "status", "unknown")
        trace["status"] = status
        workflow_id = getattr(result, "workflow_id", None) or getattr(result, "job_id", None)
        if workflow_id:
            trace["workflow_id"] = workflow_id
        steps = getattr(result, "steps", None)
        if steps:
            trace["steps"] = [
                {
                    "name": getattr(step, "name", ""),
                    "status": getattr(step, "status", ""),
                    "reason": getattr(step, "reason", None),
                }
                for step in steps
            ]
        next_step = getattr(result, "next_step", None)
        if next_step:
            trace["next_step"] = next_step
        blocked_ops = getattr(result, "blocked_operations", None)
        if blocked_ops:
            trace["blocked_operations"] = list(blocked_ops)
        error = getattr(result, "error", None)
        if error:
            trace["error"] = error
        # Document text extraction returns a SourceContentReadResult which has
        # status "completed" on success; surface content_read fields too.
        read_ref = getattr(result, "read_ref", None)
        if read_ref:
            trace["read_ref"] = read_ref
        char_count = getattr(result, "char_count", None)
        if isinstance(char_count, int):
            trace["char_count"] = char_count
        return trace

    @staticmethod
    def _item_with_media_workflow_trace(
        item: WorkbenchAutoIntakeItem,
        trace: Mapping[str, object],
        *,
        completed: bool,
    ) -> WorkbenchAutoIntakeItem:
        auto_organization = dict(item.auto_organization)
        auto_organization["media_auto_workflow"] = dict(trace)
        if completed:
            new_status: WorkbenchAutoIntakeItemStatus = "completed"
            new_content_read_status = "completed"
            new_next_step = "library_overview_refresh"
        else:
            # Keep the original needs_* status so the UI can surface the
            # blocked reason. The await_* next_step is preserved.
            new_status = item.status
            new_content_read_status = item.content_read_status
            new_next_step = item.next_step
        return WorkbenchAutoIntakeItem(
            source_id=item.source_id,
            source_uri=item.source_uri,
            input_type=item.input_type,
            workflow=item.workflow,
            status=new_status,
            needs_user_confirmation=item.needs_user_confirmation,
            title=item.title,
            content_read_status=new_content_read_status,
            structure_status=item.structure_status,
            series_status=item.series_status,
            series_name=item.series_name,
            series_confidence=item.series_confidence,
            inspiration_status=item.inspiration_status,
            auto_organization=auto_organization,
            next_step=new_next_step,
        )

    @staticmethod
    def _item_with_audio_post_transcribe(
        item: WorkbenchAutoIntakeItem,
        trace: Mapping[str, object],
        audio_post: Mapping[str, object],
        *,
        completed: bool,
    ) -> WorkbenchAutoIntakeItem:
        """Build item with audio post-transcribe results merged into auto_organization.

        Updates item's structure_status, series_status, series_name, series_confidence
        based on the post-transcribe processing results. Keeps needs_user_confirmation
        False (低置信度系列不阻塞，仅标记为 completed_pending_series).
        """
        auto_organization = dict(item.auto_organization)
        auto_organization["media_auto_workflow"] = dict(trace)

        structure_status = str(audio_post.get("structure_status", "not_started"))
        tag_index_status = str(audio_post.get("tag_index_status", "not_started"))
        series_status = str(audio_post.get("series_status", "not_started"))
        series_name = str(audio_post.get("series_name", ""))
        series_confidence = float(audio_post.get("series_confidence", 0.0) or 0.0)
        memory_publication_state = str(audio_post.get("memory_publication_state", "not_started"))

        auto_organization["structure_status"] = structure_status
        auto_organization["tag_index_status"] = tag_index_status
        auto_organization["series_status"] = series_status
        auto_organization["series_name"] = series_name
        auto_organization["series_confidence"] = series_confidence
        auto_organization["memory_publication_state"] = memory_publication_state
        if audio_post.get("summary"):
            auto_organization["summary"] = audio_post["summary"]
        if audio_post.get("tags"):
            auto_organization["tags"] = audio_post["tags"]

        if completed:
            if series_status == "pending_confirmation":
                new_status: WorkbenchAutoIntakeItemStatus = "completed_pending_series"
            else:
                new_status = "completed"
            new_content_read_status = "completed"
            new_next_step = "library_overview_refresh"
        else:
            new_status = item.status
            new_content_read_status = item.content_read_status
            new_next_step = item.next_step
        return WorkbenchAutoIntakeItem(
            source_id=item.source_id,
            source_uri=item.source_uri,
            input_type=item.input_type,
            workflow=item.workflow,
            status=new_status,
            needs_user_confirmation=False,
            title=item.title,
            content_read_status=new_content_read_status,
            structure_status=structure_status if structure_status != "not_started" else item.structure_status,
            series_status=series_status if series_status != "not_started" else item.series_status,
            series_name=series_name or item.series_name,
            series_confidence=series_confidence or item.series_confidence,
            inspiration_status=item.inspiration_status,
            auto_organization=auto_organization,
            next_step=new_next_step,
        )

    def _run_audio_post_transcribe(
        self,
        *,
        workflow_result: AudioAutoWorkflowResult,
        source_id: str,
        project_id: str,
    ) -> dict[str, object]:
        """音频转写完成后的扩展处理链：

        summarize_transcript → create_memory_candidate → structure → tag → series

        默认不自动发布长期记忆（仅创建候选）。
        返回包含 extra_steps / summary_output_id / memory_candidate_id / structure_status /
        tag_index_status / series_status / series_name / series_confidence /
        memory_publication_state / summary / tags 的 dict。
        """
        extra_steps: list[dict[str, object]] = []
        summary_output_id: str | None = None
        memory_candidate_id: str | None = None
        memory_publication_state = "not_started"
        transcript_output_id = workflow_result.transcript_output_id

        # 1. Summarize transcript（复用视频的 SummarizeTranscriptOutput）
        if self._audio_summarize_transcript and transcript_output_id:
            try:
                summary_result = self._audio_summarize_transcript(
                    transcript_output_id=transcript_output_id,
                    source_id=workflow_result.source_id,
                )
                extra_steps.append({
                    "name": "summarize_transcript",
                    "status": summary_result.status,
                    "reason": summary_result.error,
                    "output_id": summary_result.output_id,
                })
                if summary_result.status == "completed":
                    summary_output_id = summary_result.output_id
                    summary_candidate_ids = tuple(getattr(summary_result, "candidate_ids", ()))
                    if getattr(summary_result, "creates_memory_candidate", False) and summary_candidate_ids:
                        memory_candidate_id = summary_candidate_ids[0]
                        memory_publication_state = "candidates_created_not_published"
            except Exception as error:  # noqa: BLE001 - summarize failure must not block intake
                extra_steps.append({
                    "name": "summarize_transcript",
                    "status": "failed",
                    "reason": str(error) or error.__class__.__name__,
                })

        # 2. Create memory candidate（默认不自动发布）
        if summary_output_id and not memory_candidate_id and self._audio_create_memory_candidate:
            try:
                candidate_result = self._audio_create_memory_candidate(
                    output_id=summary_output_id,
                    source_id=workflow_result.source_id,
                    project_id=project_id,
                    target_layer="atom",
                    candidate_type="audio_summary",
                    created_at=self._now,
                )
                extra_steps.append({
                    "name": "create_memory_candidate",
                    "status": candidate_result.status,
                    "candidate_id": candidate_result.candidate_id,
                })
                if candidate_result.status == "candidate_created":
                    memory_candidate_id = candidate_result.candidate_id
                    memory_publication_state = "candidate_created_not_published"
            except Exception as error:  # noqa: BLE001 - candidate creation failure must not block intake
                extra_steps.append({
                    "name": "create_memory_candidate",
                    "status": "failed",
                    "reason": str(error) or error.__class__.__name__,
                })

        # 3. Structure + Tag + Series（把 transcript 写入 source_content_reads，复用文本路径）
        structure_status, tag_index_status, series_status, series_name, series_confidence, summary, tags = (
            self._organize_audio_transcript(
                source_id=source_id,
                transcript_output_id=transcript_output_id or "",
            )
        )
        extra_steps.append({
            "name": "structure_and_index",
            "status": "completed" if structure_status == "completed" else "failed",
        })
        if series_status == "pending_confirmation":
            extra_steps.append({
                "name": "assign_series",
                "status": "confirm_required",
                "reason": "系列置信度不足，待用户确认。",
            })
        elif series_status == "confirmed":
            extra_steps.append({
                "name": "assign_series",
                "status": "completed",
                "reason": series_name or "",
            })

        return {
            "extra_steps": extra_steps,
            "summary_output_id": summary_output_id,
            "memory_candidate_id": memory_candidate_id,
            "structure_status": structure_status,
            "tag_index_status": tag_index_status,
            "series_status": series_status,
            "series_name": series_name,
            "series_confidence": series_confidence,
            "memory_publication_state": memory_publication_state,
            "summary": summary,
            "tags": tags,
        }

    def _organize_audio_transcript(
        self,
        *,
        source_id: str,
        transcript_output_id: str,
    ) -> tuple[str, str, str, str, float, str, list[str]]:
        """把音频 transcript 文本写入 source_content_reads，然后复用 structure/tag/series。

        返回 (structure_status, tag_index_status, series_status, series_name,
        series_confidence, summary, tags)。
        任何步骤失败都不阻塞 intake，仅返回相应失败状态。
        """
        if not transcript_output_id:
            return "not_started", "not_started", "not_started", "", 0.0, "", []

        # 1. 读取 transcript output，拿到 text
        transcript_output = self._object_store.read("media_processing_outputs", transcript_output_id)
        if transcript_output is None:
            return "not_started", "not_started", "not_started", "", 0.0, "", []
        transcript_text = transcript_output.get("text")
        if not isinstance(transcript_text, str) or not transcript_text.strip():
            return "not_started", "not_started", "not_started", "", 0.0, "", []

        # 2. 写入 source_content_reads（status=completed, text=transcript_text）
        content_read_id = f"content-read-{source_id}"
        content_read = {
            "id": content_read_id,
            "source_id": source_id,
            "status": "completed",
            "text": transcript_text,
            "char_count": len(transcript_text),
            "content_kind": "audio_transcript",
            "created_at": self._now,
            "updated_at": self._now,
        }
        self._object_store.write(
            "source_content_reads", content_read_id, content_read, expected_revision=None
        )

        # 3. 调 StructureSourceContent（复用文本路径的结构化逻辑）
        structurer = StructureSourceContent(
            self._object_store,
            namespace_id=self._namespace_id,
            now=self._now,
        )
        try:
            structure_result = structurer.execute(source_id=source_id, content_read_id=content_read_id)
        except Exception:  # noqa: BLE001 - structuring failure must not block intake
            return "failed", "not_started", "not_started", "", 0.0, "", []

        # 4. 调 _assign_series
        series_status, series_name = self._assign_series(
            source_id=source_id,
            series_candidate=structure_result.series_candidate,
            series_confidence=structure_result.series_confidence,
        )

        # 5. 调 _index_tags
        tag_index_status = self._index_tags(
            source_id=source_id,
            structure_ref=structure_result.structure_ref,
        )

        return (
            "completed",
            tag_index_status,
            series_status,
            series_name,
            structure_result.series_confidence,
            structure_result.summary,
            list(structure_result.tags),
        )

    def _capture_media_reference(
        self,
        *,
        title: str,
        display_name: str,
        media_type: str,
        input_type: str,
        workflow: str,
        next_status: WorkbenchAutoIntakeItemStatus,
        next_step: str,
        original_asset_ref: str = "",
    ) -> WorkbenchAutoIntakeItem:
        capture_key = _CAPTURE_KEY_MAP.get(input_type, "file")
        capture_cls = {
            "file": CaptureWorkbenchFileSource,
            "image": CaptureWorkbenchImageSource,
            "audio": CaptureWorkbenchAudioSource,
            "video": CaptureWorkbenchVideoSource,
        }[capture_key]
        kwargs: dict[str, object] = {
            "title": title,
            "display_name": display_name or title,
            "media_type": media_type or "application/octet-stream",
            "size_bytes": 0,
        }
        reference_field = {
            "file": "file_reference",
            "image": "image_reference",
            "audio": "audio_reference",
            "video": "video_reference",
        }[capture_key]
        kwargs[reference_field] = original_asset_ref or display_name or title
        frozen_source = _frozen_source_snapshot_for_asset(
            self._object_store,
            original_asset_ref,
            namespace_id=self._namespace_id,
            expected_source_type=capture_key,
            expected_media_type=media_type or "application/octet-stream",
            expected_title=title,
            expected_display_name=display_name or title,
            project_id=self._project_id,
        )
        source_asset_link = None
        if frozen_source is not None:
            result = _replay_media_capture_result(frozen_source)
        else:
            capture = capture_cls(
                source_registrar=self._source_registrar,
                job_repository=self._job_repository,
                namespace_id=self._namespace_id,
                now=self._now,
            )
            result = capture.execute(**kwargs)
        if original_asset_ref and frozen_source is None:
            source_asset_link = link_workbench_original_asset_to_source(
                object_store=self._object_store,
                namespace_id=self._namespace_id,
                asset_ref=original_asset_ref,
                source_id=result.source_id,
                source_uri=result.source_uri,
                now=self._now,
            )
        return WorkbenchAutoIntakeItem(
            source_id=result.source_id,
            source_uri=result.source_uri,
            input_type=input_type,
            workflow=workflow,
            status=next_status,
            needs_user_confirmation=False,
            title=result.source_title,
            content_read_status="not_started",
            structure_status="not_started",
            series_status="not_started",
            series_name="",
            series_confidence=0.0,
            inspiration_status="not_started",
            auto_organization={
                "asset_id": result.asset_id,
                "asset_storage_mode": result.asset_storage_mode,
                "asset_availability": result.asset_availability,
                "original_asset_id": (
                    source_asset_link.asset_id if source_asset_link else frozen_source.asset_id if frozen_source else None
                ),
                "source_asset_link_ref": (
                    source_asset_link.link_ref if source_asset_link else frozen_source.link_ref if frozen_source else None
                ),
                "replayed_frozen_source": frozen_source is not None,
                "frozen_source_authorization_ready": (
                    frozen_source.authorization_ready if frozen_source else False
                ),
                "memory_publication_state": "not_published",
                "orchestrator_version": self._ORCHESTRATOR_VERSION,
                "path_policy": "no_os_absolute_path_in_response",
            },
            next_step=next_step,
        )

    def _item_from_partial_read(
        self,
        *,
        source_id: str,
        source_uri: str,
        input_type: str,
        workflow: str,
        title: str,
        read_status: str,
        read_error: str,
    ) -> WorkbenchAutoIntakeItem:
        return WorkbenchAutoIntakeItem(
            source_id=source_id,
            source_uri=source_uri,
            input_type=input_type,
            workflow=workflow,
            status="needs_confirmation",
            needs_user_confirmation=True,
            title=title,
            content_read_status=read_status,
            structure_status="not_started",
            series_status="not_started",
            series_name="",
            series_confidence=0.0,
            inspiration_status="not_started",
            auto_organization={
                "read_error": read_error,
                "memory_publication_state": "not_published",
                "orchestrator_version": self._ORCHESTRATOR_VERSION,
            },
            next_step="await_user_confirmation_after_read_failure",
        )

    def _failed_item(
        self,
        *,
        source_id: str,
        source_uri: str,
        input_type: str,
        workflow: str,
        title: str,
        error: str,
    ) -> WorkbenchAutoIntakeItem:
        return WorkbenchAutoIntakeItem(
            source_id=source_id,
            source_uri=source_uri,
            input_type=input_type,
            workflow=workflow,
            status="failed",
            needs_user_confirmation=False,
            title=title,
            content_read_status="failed",
            structure_status="not_started",
            series_status="not_started",
            series_name="",
            series_confidence=0.0,
            inspiration_status="not_started",
            auto_organization={
                "error": error,
                "memory_publication_state": "not_published",
                "orchestrator_version": self._ORCHESTRATOR_VERSION,
            },
            next_step="review_failure_and_retry",
        )

    def _failed_result(
        self,
        error: str,
        *,
        classification_payload: Mapping[str, object],
    ) -> WorkbenchAutoIntakeResult:
        return WorkbenchAutoIntakeResult(
            status="failed",
            job_id="",
            items=(),
            classification=dict(classification_payload),
            next_ui="intake_failed",
            direct_question_hint=None,
            error=error,
        )

    def _write_intake_job(
        self,
        job_id: str,
        source_id: str,
        items: Sequence[WorkbenchAutoIntakeItem],
        *,
        label: str,
    ) -> bool:
        if self._admit_review_intents is not None:
            self._admit_review_intents(job_id, source_id, items)
        log_ref = f"crp://{self._namespace_id}/logs/jobs/{job_id}/orchestrate.jsonl"
        reduction = reduce_workbench_auto_intake_job(items, label=label, now=self._now)
        candidate_jobs = tuple(self._produce_candidate_jobs(job_id, items, self._now)) if self._produce_candidate_jobs else ()
        job = {
            "schema_version": "1.0.0",
            "id": job_id,
            "source_id": source_id,
            "job_type": "workbench_auto_intake",
            "idempotency_key": f"workbench-auto-intake-{source_id}",
            "status": reduction["status"],
            "attempt": reduction["attempt"],
            "max_attempts": reduction["max_attempts"],
            "lease": None,
            "progress": reduction["progress"],
            "steps": reduction["steps"],
            "error": reduction["error"],
            "checkpoint": None,
            "staged_outputs": [
                {
                    "kind": "candidate_job",
                    "object_id": str(child["id"]),
                    "uri": f"crp://{self._namespace_id}/jobs/{child['id']}",
                    "status": str(child.get("status", "pending")),
                }
                for child in candidate_jobs
            ],
            "published_outputs": [],
            "log_refs": [log_ref],
            "created_at": self._now,
            "updated_at": self._now,
        }
        if self._admit_content_transform is not None and any(
            item.status != "failed"
            and item.workflow in {
                "document_text_extraction",
                "video_auto_workflow",
                "image_ocr",
                "audio_auto_workflow",
                "link_auto_organization",
            }
            for item in items
        ):
            admitted = self._admit_content_transform(job, items)
            if admitted:
                return True
        self._job_repository.save(job)
        return False


class _VideoMetadataOnlyCapture:
    """Deprecated placeholder retained for import compatibility; unused after Phase A uses CaptureWorkbenchVideoSource directly."""

    _DEPRECATED = True


def serialize_workbench_auto_intake_result(result: WorkbenchAutoIntakeResult) -> dict[str, object]:
    return {
        "status": result.status,
        "job_id": result.job_id,
        "items": [serialize_workbench_auto_intake_item(item) for item in result.items],
        "classification": dict(result.classification),
        "next_ui": result.next_ui,
        "direct_question_hint": dict(result.direct_question_hint) if result.direct_question_hint else None,
        "error": result.error,
        # 阶段 1.5：Memory Router 自动判断结果
        "memory_event": dict(result.memory_event) if result.memory_event else None,
    }


# ── 统一 intake workflow step contract（阶段 1.5）──────────────────────────
# 9 步自动记忆路径：捕获原始事件 → 判断意图 → 转写/抽取正文 → 生成摘要 →
# 提取原子事实 → 归入场景/系列 → 更新 Persona/Series/Project Skill 候选 →
# 更新项目大脑 → 生成本次记忆变化
# 这是面向产品的契约，把现有 item 内部状态映射成前端可直接渲染的步骤。
# 兼容性：新增字段，旧字段保留不变。

_WORKFLOW_STEP_LABELS = {
    "save_source": "捕获原始事件",
    "classify_intent": "判断意图",
    "extract_content": "转写/抽取正文",
    "generate_summary": "生成摘要",
    "extract_tags": "提取原子事实",
    "assign_series": "归入场景/系列",
    "create_memory_candidate": "更新 Persona / Series / Project Skill 候选",
    "update_project_brain": "更新项目大脑",
    "memory_delta": "生成本次记忆变化",
}

_WORKFLOW_STEP_ORDER = tuple(_WORKFLOW_STEP_LABELS.keys())


def _step(
    step_id: str,
    *,
    status: str,
    message: str = "",
    progress: float = 0.0,
    recoverable: bool = False,
    evidence_refs: tuple[str, ...] = (),
    result_refs: tuple[str, ...] = (),
    error_code: str = "",
    retry_action: str = "",
    chunk_info: Mapping[str, object] | None = None,
) -> dict[str, object]:
    result = {
        "id": step_id,
        "label": _WORKFLOW_STEP_LABELS[step_id],
        "status": status,
        "started_at": "",
        "finished_at": "",
        "progress": progress,
        "message": message,
        "recoverable": recoverable,
        "evidence_refs": list(evidence_refs),
        "result_refs": list(result_refs),
        "error_code": error_code,
        "retry_action": retry_action,
    }
    if chunk_info:
        result["chunk_info"] = dict(chunk_info)
    return result


def _build_workflow_steps(item: WorkbenchAutoIntakeItem) -> list[dict[str, object]]:
    """把 item 内部状态映射成 7 步统一 workflow_steps。

    映射规则按媒体类型（item.workflow）和 item 各 status 字段推导。
    不调用任何后端逻辑，纯本地映射。
    """
    org = item.auto_organization or {}
    media_wf = org.get("media_auto_workflow") or {}
    media_status = media_wf.get("status") if isinstance(media_wf, Mapping) else ""
    media_steps = media_wf.get("steps") if isinstance(media_wf, Mapping) else []
    source_uri = item.source_uri
    content_read_id = org.get("content_read_id", "") if isinstance(org, Mapping) else ""
    structure_ref = org.get("structure_ref", "") if isinstance(org, Mapping) else ""
    summary = org.get("summary", "") if isinstance(org, Mapping) else ""
    tags = org.get("tags", []) if isinstance(org, Mapping) else []
    memory_pub_state = org.get("memory_publication_state", "") if isinstance(org, Mapping) else ""
    workflow = item.workflow or ""
    item_status = item.status or ""

    # 步骤 1：保存原始资料
    save_status = "done" if item.source_id else "failed"
    save_message = "已保存原始资料。" if save_status == "done" else "原始资料保存失败。"
    steps: list[dict[str, object]] = [
        _step(
            "save_source",
            status=save_status,
            message=save_message,
            progress=1.0 if save_status == "done" else 0.0,
            evidence_refs=(source_uri,) if source_uri else (),
        )
    ]

    # 步骤 2：转写/抽取正文
    chunk_info: Mapping[str, object] | None = None
    if isinstance(media_wf, Mapping) and media_wf.get("is_chunked"):
        chunk_info = {
            "is_chunked": True,
            "chunk_count": media_wf.get("chunk_count", 0),
            "completed_chunk_count": media_wf.get("completed_chunk_count", 0),
            "failed_chunk_count": media_wf.get("failed_chunk_count", 0),
            "is_partial_result": media_wf.get("is_partial_result", False),
            "progress": media_wf.get("progress", 0.0),
        }
    extract_status, extract_message, extract_refs, extract_recoverable, extract_retry = _extract_content_step(
        workflow, item.content_read_status, item_status, media_status, media_steps, content_read_id, source_uri, chunk_info=chunk_info
    )
    steps.append(_step(
        "extract_content",
        status=extract_status,
        message=extract_message,
        progress=1.0 if extract_status == "done" else (0.5 if extract_status == "running" else 0.0),
        recoverable=extract_recoverable,
        evidence_refs=(source_uri,) if source_uri else (),
        result_refs=extract_refs,
        error_code="" if extract_status != "failed" else "extract_failed",
        retry_action=extract_retry,
        chunk_info=chunk_info,
    ))

    # 步骤 3：生成摘要
    if workflow in {"text_auto_organization", "link_auto_organization"}:
        if item.structure_status == "completed" and summary:
            summary_status = "done"
            summary_message = "已生成摘要。"
        elif item.structure_status == "completed":
            summary_status = "done"
            summary_message = "已整理内容。"
        elif extract_status == "failed":
            summary_status = "skipped"
            summary_message = "正文抽取未完成，跳过摘要。"
        else:
            summary_status = "skipped"
            summary_message = "未生成摘要。"
    elif workflow == "video_auto_workflow":
        has_summary_step = _media_step_status(media_steps, "summarize_transcript") == "done"
        if has_summary_step:
            summary_status = "done"
            summary_message = "已生成视频摘要。"
        elif media_status == "completed":
            summary_status = "done"
            summary_message = "已整理内容。"
        elif extract_status == "failed":
            summary_status = "skipped"
            summary_message = "转写未完成，跳过摘要。"
        else:
            summary_status = "skipped"
            summary_message = "未生成摘要。"
    elif workflow == "audio_auto_workflow":
        # Phase 3 音频链路补齐：转写完成后自动生成摘要
        has_summary_step = _media_step_status(media_steps, "summarize_transcript") == "done"
        if has_summary_step:
            summary_status = "done"
            summary_message = "已生成音频摘要。"
        elif item.structure_status == "completed" and summary:
            summary_status = "done"
            summary_message = "已整理内容。"
        elif extract_status == "failed":
            summary_status = "skipped"
            summary_message = "转写未完成，跳过摘要。"
        elif extract_status == "done":
            summary_status = "skipped"
            summary_message = "未生成摘要。"
        else:
            summary_status = "skipped"
            summary_message = "未生成摘要。"
    else:
        # 文件/图片：当前不生成摘要
        summary_status = "skipped"
        summary_message = "该类型暂不生成摘要。"
    steps.append(_step(
        "generate_summary",
        status=summary_status,
        message=summary_message,
        progress=1.0 if summary_status == "done" else 0.0,
        evidence_refs=(source_uri,) if source_uri else (),
        result_refs=(structure_ref,) if structure_ref and summary_status == "done" else (),
    ))

    # 步骤 4：提取标签
    if workflow in {"text_auto_organization", "link_auto_organization", "audio_auto_workflow"}:
        tag_index_status = org.get("tag_index_status", "") if isinstance(org, Mapping) else ""
        if tag_index_status == "indexed" and tags:
            tags_status = "done"
            tags_message = f"已提取 {len(tags)} 个标签。"
        elif tag_index_status == "empty" or not tags:
            tags_status = "skipped"
            tags_message = "未识别到标签。"
        elif tag_index_status == "indexed":
            tags_status = "done"
            tags_message = "已提取标签。"
        else:
            tags_status = "skipped"
            tags_message = "未提取标签。"
    else:
        tags_status = "skipped"
        tags_message = "该类型暂不提取标签。"
    steps.append(_step(
        "extract_tags",
        status=tags_status,
        message=tags_message,
        progress=1.0 if tags_status == "done" else 0.0,
        evidence_refs=(source_uri,) if source_uri else (),
    ))

    # 步骤 5：判断项目系列
    if workflow in {"text_auto_organization", "link_auto_organization", "audio_auto_workflow"}:
        if item.series_status == "confirmed":
            series_status = "done"
            series_message = f"已归入「{item.series_name}」。" if item.series_name else "已归入系列。"
        elif item.series_status == "pending_confirmation":
            series_status = "confirm_required"
            series_message = "系列置信度不足，已继续整理，可在资料库确认。" if not item.series_name else f"疑似「{item.series_name}」，待你确认。"
        elif item.series_status == "not_started":
            series_status = "skipped"
            series_message = "未判断系列。"
        else:
            series_status = "skipped"
            series_message = "未判断系列。"
    else:
        series_status = "skipped"
        series_message = "该类型暂不判断系列。"
    steps.append(_step(
        "assign_series",
        status=series_status,
        message=series_message,
        progress=1.0 if series_status == "done" else 0.0,
        recoverable=series_status == "confirm_required",
        evidence_refs=(source_uri,) if source_uri else (),
        retry_action="confirm_series" if series_status == "confirm_required" else "",
    ))

    # 步骤 6：生成记忆候选
    if workflow == "video_auto_workflow":
        candidate_step_status = _media_step_status(media_steps, "create_memoryCandidate") or _media_step_status(media_steps, "memory_candidate")
        if candidate_step_status == "done" or memory_pub_state not in {"not_published", "not_started", ""}:
            cand_status = "done"
            cand_message = "已生成记忆候选。"
        elif media_status == "completed":
            cand_status = "done"
            cand_message = "已生成记忆候选。"
        elif media_status == "blocked":
            cand_status = "failed"
            cand_message = "记忆候选生成受阻。"
            cand_recoverable = True
            cand_retry = "enable_provider"
        else:
            cand_status = "skipped"
            cand_message = "未生成记忆候选。"
            cand_recoverable = False
            cand_retry = ""
        if cand_status != "failed":
            cand_recoverable = False
            cand_retry = ""
    elif workflow == "audio_auto_workflow":
        # Phase 3 音频链路补齐：转写+摘要完成后自动生成候选（不自动发布）
        candidate_step_status = _media_step_status(media_steps, "create_memory_candidate")
        if candidate_step_status == "done" or memory_pub_state in {"candidate_created_not_published", "candidate_created"}:
            cand_status = "done"
            cand_message = "已生成记忆候选，待你审阅。"
        elif summary_status == "failed":
            cand_status = "skipped"
            cand_message = "摘要未生成，跳过记忆候选。"
            cand_recoverable = False
            cand_retry = ""
        elif extract_status == "failed":
            cand_status = "skipped"
            cand_message = "转写未完成，跳过记忆候选。"
            cand_recoverable = False
            cand_retry = ""
        else:
            cand_status = "skipped"
            cand_message = "未生成记忆候选。"
            cand_recoverable = False
            cand_retry = ""
    elif workflow in {"text_auto_organization", "link_auto_organization"}:
        # 文本/链接当前不创建 candidate（已知缺口）
        cand_status = "skipped"
        cand_message = "该类型暂不自动生成记忆候选。"
        cand_recoverable = False
        cand_retry = ""
    else:
        cand_status = "skipped"
        cand_message = "该类型暂不自动生成记忆候选。"
        cand_recoverable = False
        cand_retry = ""
    steps.append(_step(
        "create_memory_candidate",
        status=cand_status,
        message=cand_message,
        progress=1.0 if cand_status == "done" else 0.0,
        recoverable=cand_recoverable if cand_status == "failed" else False,
        evidence_refs=(source_uri,) if source_uri else (),
        retry_action=cand_retry if cand_status == "failed" else "",
    ))

    # 步骤 7：更新项目大脑（自动发布）
    if workflow == "video_auto_workflow":
        if memory_pub_state in {"published", "auto_published"}:
            brain_status = "done"
            brain_message = "已更新项目大脑。"
        elif memory_pub_state in {"skipped", "not_published"} and cand_status == "done":
            brain_status = "skipped"
            brain_message = "记忆候选待审，暂未更新项目大脑。"
        elif cand_status == "skipped":
            brain_status = "skipped"
            brain_message = "无记忆候选，暂未更新项目大脑。"
        else:
            brain_status = "skipped"
            brain_message = "暂未更新项目大脑。"
    elif workflow == "audio_auto_workflow":
        # Phase 3 音频链路补齐：默认不自动发布，仅创建候选
        if memory_pub_state in {"published", "auto_published"}:
            brain_status = "done"
            brain_message = "已更新项目大脑。"
        elif cand_status == "done":
            brain_status = "skipped"
            brain_message = "记忆候选待审，暂未更新项目大脑。"
        else:
            brain_status = "skipped"
            brain_message = "暂未更新项目大脑。"
    else:
        brain_status = "skipped"
        brain_message = "该类型暂不自动更新项目大脑。"
    steps.append(_step(
        "update_project_brain",
        status=brain_status,
        message=brain_message,
        progress=1.0 if brain_status == "done" else 0.0,
        evidence_refs=(source_uri,) if source_uri else (),
    ))

    return steps


def _extract_content_step(
    workflow: str,
    content_read_status: str,
    item_status: str,
    media_status: str,
    media_steps,
    content_read_id: str,
    source_uri: str,
    chunk_info: Mapping[str, object] | None = None,
) -> tuple[str, str, tuple[str, ...], bool, str]:
    """返回 (status, message, result_refs, recoverable, retry_action)。"""
    if workflow in {"text_auto_organization", "link_auto_organization"}:
        if content_read_status == "completed":
            refs = (f"crp://default/source-content-reads/{content_read_id}.json",) if content_read_id else ()
            return "done", "已读取正文。", refs, False, ""
        if content_read_status == "failed":
            return "failed", "正文读取失败。", (), True, "retry_intake"
        if item_status == "needs_confirmation":
            return "failed", "正文读取需要确认。", (), True, "retry_intake"
        return "skipped", "未读取正文。", (), False, ""
    if workflow == "document_text_extraction":
        if media_status == "completed":
            return "done", "已抽取文档正文。", (), False, ""
        if media_status == "blocked":
            return "failed", "文档抽取 Provider 未启用或失败。", (), True, "enable_provider"
        if item_status == "needs_extractor":
            return "failed", "需要启用文档抽取 Provider。", (), True, "enable_provider"
        if item_status == "queued":
            return "running", "文档已进入后台抽取任务。", (), False, ""
        return "skipped", "未抽取文档正文。", (), False, ""
    if workflow == "image_ocr":
        if media_status == "completed":
            return "done", "已完成图片 OCR。", (), False, ""
        if media_status == "blocked" or item_status == "needs_extractor":
            return "failed", "需要启用 OCR Provider。", (), True, "enable_provider"
        return "skipped", "未执行 OCR。", (), False, ""
    if workflow == "audio_auto_workflow":
        # 阶段 4：长音频分段转写
        if chunk_info and chunk_info.get("is_chunked"):
            total = int(chunk_info.get("chunk_count", 0))
            done_count = int(chunk_info.get("completed_chunk_count", 0))
            failed_count = int(chunk_info.get("failed_chunk_count", 0))
            is_partial = bool(chunk_info.get("is_partial_result", False))
            if media_status == "completed" and failed_count == 0:
                return "done", f"已完成分段转写（{done_count}/{total} 段）。", (), False, ""
            if media_status in {"completed", "partial_completed"} and is_partial:
                return "pending", f"已完成部分分段转写（{done_count}/{total} 段），其余 {failed_count} 段等待 Effect 恢复。", (), False, ""
            if media_status == "blocked" or item_status == "needs_asr":
                return "failed", "需要启用 ASR Provider。", (), True, "enable_provider"
            return "skipped", "未执行转写。", (), False, ""
        if media_status == "completed":
            return "done", "已完成音频转写。", (), False, ""
        if media_status == "blocked" or item_status == "needs_asr":
            return "failed", "需要启用 ASR Provider。", (), True, "enable_provider"
        return "skipped", "未执行转写。", (), False, ""
    if workflow == "video_auto_workflow":
        transcribe_status = _media_step_status(media_steps, "transcribe_audio") or _media_step_status(media_steps, "transcription")
        extract_status = _media_step_status(media_steps, "extract_audio") or _media_step_status(media_steps, "audio_extraction")
        if transcribe_status == "done":
            return "done", "已抽取音轨并完成转写。", (), False, ""
        if extract_status == "done" and transcribe_status != "failed":
            return "running", "已抽取音轨，正在转写。", (), False, ""
        if item_status == "queued":
            return "running", "视频已进入后台音轨抽取与转写任务。", (), False, ""
        if media_status == "blocked" or item_status == "needs_video_workflow":
            return "failed", "视频工作流受阻，需启用 Provider 或授权文件。", (), True, "enable_provider"
        return "skipped", "未执行视频转写。", (), False, ""
    return "skipped", "未抽取正文。", (), False, ""


def _media_step_status(media_steps, step_name: str) -> str:
    """从 media_auto_workflow.steps 数组中查找指定步骤的 status。

    返回归一化后的状态值："done" | "running" | "failed" | "skipped" | ""。
    media workflow 内部用 "completed"，统一映射为 "done" 以便后续比较。
    """
    _DONE_EQUIVALENTS = {"completed", "done"}
    if not isinstance(media_steps, Sequence):
        return ""
    for step in media_steps:
        if isinstance(step, Mapping):
            name = step.get("name") or step.get("step") or ""
            if name == step_name:
                status = step.get("status", "")
                if isinstance(status, str):
                    if status in _DONE_EQUIVALENTS:
                        return "done"
                    return status
    return ""


def serialize_workbench_auto_intake_item(item: WorkbenchAutoIntakeItem) -> dict[str, object]:
    workflow_steps = _build_workflow_steps(item)
    progression_mode, progression_reason = _workbench_item_progression(item, workflow_steps)
    return {
        "source_id": item.source_id,
        "source_uri": item.source_uri,
        "input_type": item.input_type,
        "workflow": item.workflow,
        "status": item.status,
        "needs_user_confirmation": item.needs_user_confirmation,
        "progression_mode": progression_mode,
        "progression_reason": progression_reason,
        "title": item.title,
        "content_read_status": item.content_read_status,
        "structure_status": item.structure_status,
        "series_status": item.series_status,
        "series_name": item.series_name,
        "series_confidence": item.series_confidence,
        "inspiration_status": item.inspiration_status,
        "auto_organization": dict(item.auto_organization),
        "next_step": item.next_step,
        "workflow_steps": workflow_steps,
    }


def _workbench_item_progression(
    item: WorkbenchAutoIntakeItem,
    workflow_steps: Sequence[Mapping[str, object]],
) -> tuple[str, str]:
    memory_state = str(item.auto_organization.get("memory_publication_state") or "")
    if memory_state in {
        "candidate_created",
        "candidate_created_not_published",
        "candidates_created_not_published",
        "pending_review",
    }:
        decision = decide_workflow_progression(WorkflowDecisionBoundary.FORMAL_MEMORY_PUBLICATION)
        return decision.mode.value, decision.reason.value
    retry_actions = {
        str(step.get("retry_action") or "")
        for step in workflow_steps
        if isinstance(step, Mapping)
    }
    if "enable_provider" in retry_actions or item.status == "needs_extractor":
        decision = decide_workflow_progression(WorkflowDecisionBoundary.PERMISSION_EXPANSION)
        return decision.mode.value, decision.reason.value
    if item.needs_user_confirmation or any(
        step.get("status") in {"confirm_required", "failed"}
        for step in workflow_steps
        if isinstance(step, Mapping)
    ):
        decision = decide_workflow_progression(WorkflowDecisionBoundary.MATERIAL_AMBIGUITY)
        return decision.mode.value, decision.reason.value
    decision = decide_workflow_progression(WorkflowDecisionBoundary.DETERMINISTIC)
    return decision.mode.value, decision.reason.value


def _content_read_id_from_ref(read_ref: str | None) -> str:
    if not isinstance(read_ref, str) or not read_ref.strip():
        return ""
    marker = "/source-content-reads/"
    if marker not in read_ref:
        return ""
    tail = read_ref.rsplit(marker, 1)[1]
    return tail[:-5] if tail.endswith(".json") else tail


def _structure_id_from_ref(structure_ref: str | None) -> str:
    if not isinstance(structure_ref, str) or not structure_ref.strip():
        return ""
    marker = "/source-structures/"
    if marker not in structure_ref:
        return ""
    tail = structure_ref.rsplit(marker, 1)[1]
    return tail[:-5] if tail.endswith(".json") else tail


def _derive_title(content: str, urls: Sequence[str], file_name: str) -> str:
    if file_name:
        return file_name[:80]
    if urls:
        return str(urls[0])[:80]
    if content:
        return content.strip().split("\n", 1)[0][:80]
    return "工作台输入"


def _extract_first_url(content: str) -> str:
    for token in content.replace("\n", " ").split(" "):
        clean = token.strip(" \r\n\t,，。；;）)]}>\"'")
        try:
            from urllib.parse import urlparse
            parsed = urlparse(clean)
            if parsed.scheme in {"http", "https"} and parsed.netloc:
                return clean
        except ValueError:
            continue
    return ""


def select_workbench_link_target(
    *,
    content: str = "",
    urls: Sequence[str] | None = None,
) -> str:
    """Return the link that auto-intake will send to its link-source effect."""

    clean_urls = tuple(url.strip() for url in urls or () if isinstance(url, str) and url.strip())
    return clean_urls[0] if clean_urls else _extract_first_url((content or "").strip())


def _required_str(source: Mapping[str, object] | None, key: str) -> str:
    if not isinstance(source, Mapping):
        return ""
    value = source.get(key)
    return value if isinstance(value, str) and value else ""


def _safe_preview(value: str, *, limit: int = 4000) -> str:
    clean = (value or "").strip()
    return clean[:limit]


def _basename_only(value: str) -> str:
    if not value:
        return ""
    return value.replace("\\", "/").rsplit("/", 1)[-1]


@dataclass(frozen=True, slots=True)
class WorkbenchAutoIntakeEndpointResponse:
    status_code: int
    body: Mapping[str, Any]
    headers: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class _ChunkedWorkflowAdapter:
    """适配长音频分段工作流结果，让 _run_audio_post_transcribe 能读取 transcript_output_id。"""

    transcript_output_id: str
    source_id: str


class ServeWorkbenchAutoIntakeEndpoint:
    """Serve the unified workbench auto intake endpoint."""

    endpoint_path = "/api/rebuild/workbench/auto-intake"

    def execute(
        self,
        *,
        method: str,
        path: str,
        body: Mapping[str, Any] | None,
        orchestrate: Callable[..., WorkbenchAutoIntakeResult],
    ) -> WorkbenchAutoIntakeEndpointResponse:
        request_path = path.split("?", 1)[0]
        if request_path != self.endpoint_path:
            return self._json_response(404, {"detail": "workbench auto intake endpoint not found"})
        if method.upper() != "POST":
            return self._json_response(
                405,
                {"detail": "workbench auto intake endpoint only supports POST"},
                extra_headers={"Allow": "POST"},
            )
        if not isinstance(body, Mapping):
            return self._json_response(400, {"detail": "request body must be a JSON object"})
        content = body.get("content", "")
        media_type = body.get("media_type", "")
        file_name = body.get("file_name", "")
        urls = body.get("urls", [])
        add_to_knowledge_base = body.get("add_to_knowledge_base", True)
        title = body.get("title", "")
        raw_child_inputs = body.get("child_inputs")
        raw_media_duration_ms = body.get("media_duration_ms")
        original_asset_ref = body.get("original_asset_ref", "")
        if not isinstance(content, str) or not isinstance(media_type, str) or not isinstance(file_name, str):
            return self._json_response(400, {"detail": "content, media_type and file_name must be strings"})
        if not isinstance(title, str):
            return self._json_response(400, {"detail": "title must be a string"})
        if not isinstance(urls, list):
            return self._json_response(400, {"detail": "urls must be an array"})
        if not isinstance(add_to_knowledge_base, bool):
            return self._json_response(400, {"detail": "add_to_knowledge_base must be a boolean"})
        if not isinstance(original_asset_ref, str):
            return self._json_response(400, {"detail": "original_asset_ref must be a string"})
        media_duration_ms: int | None = None
        if raw_media_duration_ms is not None:
            if not isinstance(raw_media_duration_ms, int) or raw_media_duration_ms < 0:
                return self._json_response(400, {"detail": "media_duration_ms must be a non-negative integer"})
            media_duration_ms = raw_media_duration_ms
        child_inputs: tuple[Mapping[str, Any], ...] | None = None
        if raw_child_inputs is not None:
            if not isinstance(raw_child_inputs, list) or not raw_child_inputs:
                return self._json_response(400, {"detail": "child_inputs must be a non-empty array"})
            normalized_children: list[Mapping[str, Any]] = []
            for index, raw_child in enumerate(raw_child_inputs):
                if not isinstance(raw_child, Mapping):
                    return self._json_response(
                        400,
                        {"detail": f"child_inputs[{index}] must be an object"},
                    )
                child_file_name = str(raw_child.get("file_name") or raw_child.get("display_name") or "").strip()
                child_file_reference = str(
                    raw_child.get("file_reference") or raw_child.get("asset_ref") or ""
                ).strip()
                if not child_file_name or not child_file_reference:
                    return self._json_response(
                        400,
                        {
                            "detail": f"child_inputs[{index}] requires file_name and file_reference",
                        },
                    )
                normalized_children.append(raw_child)
            child_inputs = tuple(normalized_children)
        try:
            kwargs: dict[str, Any] = {
                "content": content,
                "media_type": media_type,
                "file_name": file_name,
                "urls": tuple(urls),
                "add_to_knowledge_base": add_to_knowledge_base,
                "title": title,
                "original_asset_ref": original_asset_ref,
            }
            if child_inputs:
                kwargs["child_inputs"] = child_inputs
            if media_duration_ms is not None:
                kwargs["media_duration_ms"] = media_duration_ms
            result = orchestrate(**kwargs)
        except ValueError as error:
            return self._json_response(
                400,
                {
                    "detail": "workbench auto intake rejected",
                    "reason": str(error),
                    "actionable": True,
                },
            )
        queued = any(
            item.status in {"queued", "needs_extractor", "needs_video_workflow", "needs_asr"}
            for item in result.items
        )
        status_code = 202 if result.status == "accepted" and queued else (201 if result.status == "accepted" else 200)
        return self._json_response(status_code, serialize_workbench_auto_intake_result(result))

    def _json_response(
        self,
        status_code: int,
        body: Mapping[str, Any],
        *,
        extra_headers: Mapping[str, str] | None = None,
    ) -> WorkbenchAutoIntakeEndpointResponse:
        headers = {"Content-Type": "application/json", "Cache-Control": "no-store"}
        if extra_headers:
            headers.update(extra_headers)
        return WorkbenchAutoIntakeEndpointResponse(
            status_code=status_code,
            body=body,
            headers=headers,
        )
