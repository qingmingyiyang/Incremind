from __future__ import annotations

import json
import hashlib
import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from backend.agent.memory.context import AgentContext, AgentSeriesScopeAuthority
from backend.agent.schemas.action_plan import AgentActionPlan, AgentTurnResult, ScopeType
from backend.api.container import ApiContainerDep
from backend.api.responses import (
    AgentChatContextRequest,
    AgentChatRequest,
    AgentChatResponse,
    AgentContextUsageRequest,
    AgentContextUsageResponse,
    AgentSessionClearRequest,
    AgentSessionRecoveryRequest,
    AgentSessionRecoveryResponse,
    CitationResponse,
)
from backend.api.series_turn_scope_authority import (
    SeriesTurnScopeAuthorityError,
    build_series_turn_scope_authority,
)
from backend.api.sse import encode_sse_event
from backend.video_summary.infrastructure.rag_models import (
    RAG_EMBEDDING_REQUIRED_MESSAGE,
    RAG_MODEL_DOWNLOAD_MESSAGE,
)
from backend.video_summary.infrastructure.settings import load_settings
from core.product_core.project_series_scope import ProjectSeriesScopeError

LOGGER = logging.getLogger(__name__)
router = APIRouter()


@router.post("/api/agent/chat", response_model=AgentChatResponse)
def agent_chat(request: AgentChatRequest, container: ApiContainerDep) -> AgentChatResponse:
    context_override = _prepare_agent_context(request, container)
    rag_block_message = _resolve_rag_block_message(context_override, container)
    if rag_block_message:
        return _build_rag_block_response(context_override, rag_block_message)

    try:
        result = container.get_agent_graph_service().run_turn(
            session_id=request.session_id,
            user_message=request.message,
            context_override=context_override,
        )
    except Exception as error:
        LOGGER.exception("Agent chat failed")
        raise HTTPException(status_code=503, detail=_format_agent_error(error)) from error
    return AgentChatResponse.from_result(result)


@router.post("/api/agent/chat/stream")
def agent_chat_stream(request: AgentChatRequest, container: ApiContainerDep) -> StreamingResponse:
    context_override = _prepare_agent_context(request, container)

    def event_iterator():
        rag_block_message = _resolve_rag_block_message(context_override, container)
        if rag_block_message:
            yield from _stream_rag_block_message(rag_block_message)
            return
        debug_trace: dict[str, object] | None = {} if _is_agent_debug_enabled(container) else None
        try:
            service = container.get_agent_graph_service()
            for event in service.stream_with_context(
                session_id=request.session_id,
                user_message=request.message,
                context_override=context_override,
                debug_trace=debug_trace,
            ):
                yield encode_sse_event(event.type, event.payload)
            _log_agent_debug_trace(request, debug_trace)
        except Exception as error:
            _log_agent_debug_trace(request, debug_trace)
            LOGGER.exception("Agent chat stream failed")
            yield encode_sse_event("error", {"message": _format_agent_error(error)})

    return StreamingResponse(
        event_iterator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


@router.post("/api/agent/context/usage", response_model=AgentContextUsageResponse)
def get_agent_context_usage(request: AgentContextUsageRequest, container: ApiContainerDep) -> AgentContextUsageResponse:
    context_override = _build_agent_context_override(request.session_id, request.context)
    usage = container.get_agent_context_usage().inspect(
        session_id=request.session_id,
        context_override=context_override,
    )
    return AgentContextUsageResponse(
        session_id=usage.session_id,
        scope_type=usage.scope_type,
        memory_key=usage.memory_key,
        estimated_total_tokens=usage.estimated_total_tokens,
        window_tokens=usage.window_tokens,
        reserved_output_tokens=usage.reserved_output_tokens,
        warning_threshold_tokens=usage.warning_threshold_tokens,
        compact_threshold_tokens=usage.compact_threshold_tokens,
        blocking_threshold_tokens=usage.blocking_threshold_tokens,
        remaining_tokens=usage.remaining_tokens,
        usage_percent=usage.usage_percent,
        level=usage.level,
        sources=[
            {
                "id": source.id,
                "label": source.label,
                "estimated_tokens": source.estimated_tokens,
            }
            for source in usage.sources
        ],
    )


@router.get("/api/agent/memory/status")
def get_agent_memory_status(container: ApiContainerDep) -> dict[str, object]:
    return container.knowledge_memory_progress_tracker.get_snapshot("agent-memory-refresh").to_dict()


@router.post("/api/agent/session/recover", response_model=AgentSessionRecoveryResponse)
def recover_agent_session(request: AgentSessionRecoveryRequest, container: ApiContainerDep) -> AgentSessionRecoveryResponse:
    snapshot = container.agent_session_store.get_snapshot(request.session_id)
    if snapshot is None:
        return AgentSessionRecoveryResponse(
            session_id=request.session_id,
            restored=False,
            messages=[],
        )
    frozen_context = snapshot.context
    return AgentSessionRecoveryResponse(
        session_id=snapshot.session_id,
        restored=True,
        memory_key=snapshot.memory_key,
        updated_at=snapshot.updated_at,
        message_count=snapshot.message_count,
        series_authority=(
            frozen_context.series_authority.model_dump(mode="json")
            if frozen_context.series_authority is not None
            else None
        ),
        messages=[
            {
                "role": message.role,
                "content": message.content,
                "created_at": message.created_at,
                "citations": [CitationResponse.from_model(item) for item in message.citations],
            }
            for message in snapshot.messages
        ],
    )


@router.post("/api/agent/session/clear")
def clear_agent_session(request: AgentSessionClearRequest, container: ApiContainerDep) -> dict[str, object]:
    session_store = getattr(container, "agent_session_store", None)
    if session_store is not None:
        session_store.clear_snapshot(request.session_id)
    return {"status": "cleared", "session_id": request.session_id}


def _build_agent_context_override(session_id: str, request_context) -> AgentContext | None:
    if request_context is None:
        return None
    return AgentContext(
        session_id=session_id,
        scope_type=request_context.scope_type or "series",
        series_id=request_context.series_id,
        series_title=request_context.series_title,
        video_id=request_context.video_id,
        video_title=request_context.video_title,
        selected_tool=request_context.selected_tool,
    )


def _prepare_agent_context(request: AgentChatRequest, container) -> AgentContext:
    """Build the turn context and freeze Series scope before any runner call.

    Series- and video-scoped requests must resolve their one authoritative
    Project membership through the series and freeze the revision-bound
    snapshot before the legacy turn runs.  A session that already carries a
    frozen snapshot for the same series replays it without re-resolving, so
    membership drift or restart can never silently switch projects.
    """
    context_override = _build_agent_context_override(request.session_id, request.context)
    request_context = request.context
    if request_context is None:
        return context_override
    # A client-supplied authority is rejected even when null, so a request
    # cannot smuggle a placeholder across an authority boundary.
    if "series_authority" in request_context.model_fields_set:
        raise HTTPException(
            status_code=400,
            detail={"detail": "AI agent series scope rejected", "reason": "series_turn_authority_client_supplied"},
        )
    scope_type = request_context.scope_type or ScopeType.SERIES.value
    if scope_type not in (ScopeType.SERIES.value, ScopeType.VIDEO.value):
        return context_override
    # Video scope resolves its series the same way: the video is never an
    # independent authority carrier, its series membership is.
    if not request_context.series_id:
        raise HTTPException(
            status_code=400,
            detail={"detail": "AI agent series scope rejected", "reason": "series_turn_series_id_invalid"},
        )
    context_override.series_authority = _replay_or_resolve_series_scope(
        series_id=request_context.series_id,
        asserted_project_id=request_context.project_id,
        session_id=request.session_id,
        container=container,
    )
    return context_override


def _replay_or_resolve_series_scope(
    *,
    series_id: str,
    asserted_project_id: str | None,
    session_id: str,
    container,
) -> AgentSeriesScopeAuthority:
    session_store = getattr(container, "agent_session_store", None)
    if session_store is not None:
        snapshot = session_store.get_snapshot(session_id)
        if (
            snapshot is not None
            and snapshot.context.series_authority is not None
            and snapshot.context.series_id == series_id
        ):
            frozen = snapshot.context.series_authority
            if asserted_project_id is not None and asserted_project_id != frozen.project_id:
                raise HTTPException(
                    status_code=400,
                    detail={"detail": "AI agent series scope rejected", "reason": "series_turn_project_assertion_mismatch"},
                )
            return frozen
    authority = build_series_turn_scope_authority(getattr(container, "root_dir"))
    try:
        facts = authority.freeze_agent_series_scope(
            series_id=series_id,
            asserted_project_id=asserted_project_id,
        )
    except (SeriesTurnScopeAuthorityError, ProjectSeriesScopeError) as error:
        raise HTTPException(
            status_code=400,
            detail={"detail": "AI agent series scope rejected", "reason": str(error)},
        ) from error
    return AgentSeriesScopeAuthority(
        kind="project_series_scope_v1",
        project_id=facts.project_id,
        series_id=facts.series_id,
        object_id=facts.object_id,
        payload_revision=facts.payload_revision,
        storage_revision=facts.storage_revision,
        authority_identity=facts.authority_identity,
        authority_ref=facts.authority_ref,
    )


def _resolve_rag_block_message(context: AgentContext | None, container) -> str | None:
    if context is None or context.scope_type != ScopeType.SERIES.value:
        return None
    rag_model_manager = getattr(container, "rag_model_manager", None)
    if rag_model_manager is None:
        return None
    if rag_model_manager.has_active_download():
        return RAG_MODEL_DOWNLOAD_MESSAGE
    if not rag_model_manager.is_downloaded("embedding"):
        return RAG_EMBEDDING_REQUIRED_MESSAGE
    return None


def _build_rag_block_response(context: AgentContext | None, message: str) -> AgentChatResponse:
    scope_type = ScopeType.SERIES if context is None or context.scope_type == ScopeType.SERIES.value else ScopeType.VIDEO
    return AgentChatResponse.from_result(
        AgentTurnResult(
            assistant_message=message,
            plan=AgentActionPlan(
                scope_type=scope_type,
                reason="rag_model_unavailable",
                tool_calls=[],
                use_answerer=False,
            ),
            tool_results=[],
            citations=[],
        )
    )


def _stream_rag_block_message(message: str):
    yield encode_sse_event("answer_started", {"message": "正在检查 RAG 模型"})
    yield encode_sse_event("answer_delta", {"delta": message})
    yield encode_sse_event("answer_completed", {"message": message, "citations": []})


def _format_agent_error(error: Exception) -> str:
    message = str(error).strip()
    if _is_web_search_timeout(error, message):
        return "联网搜索失败：请求超时，请稍后重试或关闭联网搜索。"
    if _is_unsupported_web_search_error(message):
        return "联网搜索失败：当前模型或供应商不支持联网搜索，请关闭联网搜索或更换支持搜索的模型。"
    if _is_web_search_error(message):
        return f"联网搜索失败：{message}" if message else "联网搜索失败。"
    if _is_model_url_connection_error(message):
        return "url连接错误，请检查拼写或者地址是否可用"
    if "Your request was blocked" in message:
        return "模型请求被上游网关拦截，请检查模型供应商、API 网关或更换官方 API 地址。"
    if "APIError" in message or "OpenAIException" in message or "litellm" in type(error).__module__:
        return f"模型服务调用失败：{message}" if message else "模型服务调用失败。"
    return message or "AI 对话失败。"


def _is_web_search_timeout(error: Exception, message: str) -> bool:
    normalized = message.lower()
    return (
        ("web_search" in normalized or "联网搜索" in message)
        and (isinstance(error, TimeoutError) or "timeout" in normalized or "timed out" in normalized)
    )


def _is_unsupported_web_search_error(message: str) -> bool:
    normalized = message.lower()
    if "web_search_options" not in normalized and "web_search" not in normalized and "联网搜索" not in message:
        return False
    unsupported_markers = (
        "unsupported",
        "not supported",
        "unrecognized",
        "unknown parameter",
        "invalid parameter",
        "does not support",
        "不支持",
    )
    return any(marker in normalized for marker in unsupported_markers)


def _is_web_search_error(message: str) -> bool:
    normalized = message.lower()
    return "web_search" in normalized or "web_search_options" in normalized or "联网搜索" in message


def _is_model_url_connection_error(message: str) -> bool:
    normalized = message.lower()
    connection_markers = (
        "connection error",
        "connecterror",
        "apiconnectionerror",
        "connection refused",
        "name or service not known",
        "nodename nor servname provided",
        "failed to resolve",
        "getaddrinfo failed",
        "temporary failure in name resolution",
        "无法连接",
        "连接失败",
        "拒绝连接",
    )
    return any(marker in normalized for marker in connection_markers)


def _is_agent_debug_enabled(container) -> bool:
    try:
        return bool(load_settings(container.config_path, container.root_dir).debug.mode)
    except Exception:
        return False


def _log_agent_debug_trace(request: AgentChatRequest, debug_trace: dict[str, object] | None) -> None:
    if not debug_trace:
        return
    session_fingerprint = hashlib.sha256(request.session_id.encode("utf-8")).hexdigest()[:16]
    LOGGER.info(
        "Agent debug trace metadata\nsession_fingerprint=%s\nmessage_chars=%s\ntrace=%s",
        session_fingerprint,
        len(request.message),
        json.dumps(_agent_debug_trace_metadata(debug_trace), ensure_ascii=False, sort_keys=True),
    )


def _agent_debug_trace_metadata(debug_trace: dict[str, object]) -> dict[str, object]:
    metadata: dict[str, object] = {}
    for stage, value in debug_trace.items():
        safe_stage = str(stage)[:80]
        if isinstance(value, dict):
            stage_metadata: dict[str, object] = {
                "field_count": len(value),
                "fields": sorted(str(key)[:80] for key in value)[:24],
            }
            prompt_version = value.get("prompt_version")
            if isinstance(prompt_version, str) and prompt_version.startswith("agent-") and len(prompt_version) <= 80:
                stage_metadata["prompt_version"] = prompt_version
            input_metadata = value.get("input")
            if isinstance(input_metadata, dict):
                stage_metadata["input_metrics"] = {
                    str(key)[:80]: metric
                    for key, metric in input_metadata.items()
                    if (
                        isinstance(metric, (int, float, bool))
                        and not isinstance(metric, str)
                        and str(key).endswith(("_count", "_chars"))
                    )
                }
            metadata[safe_stage] = stage_metadata
        elif isinstance(value, list):
            metadata[safe_stage] = {"item_count": len(value)}
        else:
            metadata[safe_stage] = {"value_type": type(value).__name__}
    return metadata
