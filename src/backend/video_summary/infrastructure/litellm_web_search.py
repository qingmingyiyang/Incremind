from __future__ import annotations

from collections.abc import Mapping, Sequence
import json
from typing import Any, Callable

from backend.shared.llm.base_url import resolve_openai_compatible_api_base_url
from backend.shared.llm.litellm_gateway import EgressGuard
from backend.shared.llm.prompt_contracts import (
    prompt_messages,
    redact_private_prompt_text,
    redaction_boundary_clause,
    untrusted_envelope_clause,
)


VIDEO_WEB_SEARCH_PROMPT_VERSION = "video-summary-web-search-v3"
VIDEO_WEB_SEARCH_MAX_TIMEOUT_SECONDS = 120


class LiteLLMNativeWebSearchGateway:
    def __init__(
        self,
        *,
        provider: str,
        model: str,
        base_url: str,
        api_key: str | None = None,
        api_key_provider: Callable[[], str] | None = None,
        search_context_size: str,
        completion_fn=None,
        egress_guard: EgressGuard | None = None,
        messages_factory: Callable[[str], list[dict[str, str]]] | None = None,
    ) -> None:
        normalized_api_key = api_key.strip() if isinstance(api_key, str) else ""
        if normalized_api_key and api_key_provider is not None:
            raise RuntimeError("联网搜索 API Key 注入来源冲突。")
        if not normalized_api_key and api_key_provider is None:
            raise RuntimeError("缺少 API Key，无法调用联网搜索。")
        normalized_model = model.strip()
        if not normalized_model:
            raise RuntimeError("缺少模型名称，无法调用联网搜索。")

        normalized_provider = provider.strip().lower()
        if not normalized_provider:
            raise RuntimeError("缺少模型类型，无法调用联网搜索。")
        self._model = normalized_model if "/" in normalized_model else f"{normalized_provider}/{normalized_model}"
        self._base_url = resolve_openai_compatible_api_base_url(base_url)
        self._api_key_provider = api_key_provider or (lambda: normalized_api_key)
        self._search_context_size = search_context_size.strip() or "medium"
        self._completion = completion_fn or _load_litellm_completion()
        self._egress_guard = egress_guard
        self._messages_factory = messages_factory or build_web_search_messages

    def search(self, query: str, *, max_results: int, timeout_seconds: int) -> list[dict[str, object]]:
        normalized_query = query.strip()
        if not normalized_query:
            raise ValueError("联网搜索 query 不能为空。")
        if max_results <= 0:
            raise ValueError("联网搜索 max_results 必须是正数。")
        bounded_timeout = max(1, min(int(timeout_seconds), VIDEO_WEB_SEARCH_MAX_TIMEOUT_SECONDS))
        request = {
            "model": self._model,
            "messages": self._messages_factory(normalized_query),
            "api_base": self._base_url,
            "api_key": self._wire_api_key(),
            "temperature": 0,
            "timeout": bounded_timeout,
            "web_search_options": {
                "search_context_size": self._search_context_size,
            },
        }
        lease = self._authorize_egress(request)
        try:
            response = self._completion(**request)
        except Exception:
            lease.finish("failed", error_code="provider_request_failed")
            raise
        lease.finish("succeeded")
        content = _extract_message_content(response)
        results = _extract_url_citations(response, fallback_text=content)
        if not results:
            raise RuntimeError("联网搜索未返回可引用来源。")
        return results[:max_results]

    def _wire_api_key(self) -> str:
        value = self._api_key_provider()
        if not isinstance(value, str) or not value.strip():
            raise RuntimeError("联网搜索 API Key 注入失败。")
        return value.strip()

    def _authorize_egress(self, request: Mapping[str, object]):
        if self._egress_guard is None:
            raise RuntimeError("模型外发政策未配置。")
        redacted = {key: value for key, value in request.items() if key not in {"api_key", "api_base"}}
        payload_bytes = len(json.dumps(redacted, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8"))
        return self._egress_guard("web_search", ("instructions", "source_excerpt"), payload_bytes)


def build_web_search_messages(query: str) -> list[dict[str, str]]:
    return prompt_messages(
        system=(
            "你是视频知识工作流的只读联网检索助手。你的职责是围绕一个明确查询寻找当前网页证据，并返回带原始 URL 引用的"
            "简洁中文结果；你没有修改资料、发布内容、调用其他工具、写笔记或更新长期记忆的权限。\n"
            + untrusted_envelope_clause(
                fields="query",
                content_noun="查询文本",
            )
            + "\n"
            "区分网页明确事实、来源观点和你的归纳；不要编造来源、URL、"
            "发布日期或网页未支持的结论。找不到可引用来源时不要给无来源答案。\n"
            + redaction_boundary_clause()
            + "回答应聚焦与 query 最相关的信息，避免复制"
            "大段受版权保护正文。必须让 Provider 返回原生 URL citations；调用方会在没有可引用来源时拒绝结果。"
        ),
        payload={
            "schema_version": "1.0",
            "prompt_version": VIDEO_WEB_SEARCH_PROMPT_VERSION,
            "data_class": "untrusted_web_search_query",
            "query": redact_private_prompt_text(query),
        },
    )


def _load_litellm_completion():
    try:
        from litellm import completion
    except ModuleNotFoundError as error:
        raise RuntimeError("缺少 litellm 依赖，无法调用联网搜索。") from error
    return completion


def _extract_message_content(response: Any) -> str:
    choices = _lookup(response, "choices")
    if not isinstance(choices, Sequence) or not choices:
        return ""
    message = _lookup(choices[0], "message")
    content = _lookup(message, "content")
    if isinstance(content, str):
        return content.strip()
    return ""


def _extract_url_citations(response: Any, *, fallback_text: str) -> list[dict[str, object]]:
    choices = _lookup(response, "choices")
    if not isinstance(choices, Sequence) or not choices:
        return []
    message = _lookup(choices[0], "message")
    annotations = _lookup(message, "annotations")
    if not isinstance(annotations, Sequence):
        return []

    results: list[dict[str, object]] = []
    seen_urls: set[str] = set()
    for annotation in annotations:
        url_citation = _lookup(annotation, "url_citation")
        if url_citation is None:
            continue
        url = _as_str(_lookup(url_citation, "url"))
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        title = _as_str(_lookup(url_citation, "title")) or url
        cited_text = _slice_text(
            fallback_text,
            start=_lookup(url_citation, "start_index"),
            end=_lookup(url_citation, "end_index"),
        )
        results.append(
            {
                "title": title,
                "url": url,
                "text": cited_text or fallback_text,
                "snippet": cited_text or fallback_text,
            }
        )
    return results


def _slice_text(text: str, *, start: object, end: object) -> str:
    if not isinstance(start, int) or not isinstance(end, int):
        return ""
    if start < 0 or end <= start:
        return ""
    return text[start:end].strip()


def _as_str(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _lookup(source: Any, key: str) -> Any:
    if isinstance(source, Mapping):
        return source.get(key)
    return getattr(source, key, None)
