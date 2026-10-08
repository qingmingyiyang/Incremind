from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
import hashlib
import json
import math
import random
import ssl
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from threading import Lock
from time import monotonic, sleep
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from backend.shared.llm.message_metadata import (
    _dump_messages, _estimate_input_tokens, _build_json_mode_messages,
    _build_prompt_fallback_messages, _build_validation_retry_message,
    _extract_normalized_usage, _first_nonnegative_int, _nonnegative_int, _lookup,
)
from backend.shared.llm.chat_stream import ChatCompletionStreamChunk
from backend.shared.llm.base_url import resolve_openai_compatible_api_base_url
from backend.shared.llm.json_mode import (PartialJSONField, StructuredResponseDecodeError,
    describe_validation_error, validate_json_response)
from backend.shared.llm.model_capabilities import ModelCapabilities, resolve_model_capabilities
from backend.shared.llm.model_transport import ModelTransportError, ModelTransportTimeout, complete_with_owned_transport
from backend.shared.llm.openai_responses import (
    ProviderBackgroundOptions, ResponsesBackgroundInterrupted, ResponsesCompletion, ResponsesError,
)


StructuredResponseT = TypeVar("StructuredResponseT", bound=BaseModel)
CompletionFn = Callable[..., Any]
AsyncCompletionFn = Callable[..., Awaitable[Any]]
ProviderAttemptObserver = Callable[[str, BaseException | None], None]


class ModelRetryControl:
    """One live logical call; policy and current execution checks are injected.

    The product caller must supply its frozen get('retry') implementation. A
    failed attempt is eligible only after its terminal and transport closure;
    no historical UNKNOWN Effect or permission is used as a retry witness.
    """

    def __init__(self, *, policy: Callable[[Mapping[str, object]], Mapping[str, object]],
                 checkpoint: Callable[[], None], clock: Callable[[], float] = monotonic,
                 wait: Callable[[float], None] | None = None,
                 jitter: Callable[[], float] = random.random,
                 wall_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 on_retry: Callable[[Mapping[str, object]], None] | None = None,
                 owned_client_factory=None) -> None:
        self.policy, self.checkpoint, self.clock = policy, checkpoint, clock
        self.wait, self.jitter, self.wall_clock = wait, jitter, wall_clock
        self.on_retry = on_retry
        self.owned_client_factory = owned_client_factory
        self.limits = dict(policy({'kind': 'limits'}))
        self.counts: dict[str, int] = {}
        self.retry_number = 0
        self.phase = 'before_output'
        self.deadline: float | None = None
        self.non_stream = False
        self._provider_failure: BaseException | None = None
        self.closed_witness = None
        self.attempt_usage: dict[str, int] = {}
        self.attempt_cache: dict[str, int] | None = None
        self.terminal_seen = False
        self.usage: dict[str, int] = {}

    def start(self, timeout: float | None) -> None:
        if self.deadline is None:
            limit = float(self.limits['total_timeout'])
            if timeout is not None:
                if not math.isfinite(timeout) or timeout <= 0:
                    raise ValueError('timeout must be positive')
                limit = min(limit, timeout)
            self.deadline = self.clock() + limit

    def remaining(self) -> float:
        assert self.deadline is not None
        remaining = self.deadline - self.clock()
        if remaining <= 0:
            raise TimeoutError('model request deadline exceeded')
        return remaining

    def before_attempt(self) -> float:
        self._provider_failure = None
        self.closed_witness = None
        self.attempt_usage = {}
        self.attempt_cache = None
        self.terminal_seen = False
        self.checkpoint()
        return self.remaining()

    def provider_failed(self, error: BaseException, *, closed: bool = True) -> None:
        if isinstance(error, ModelTransportError) and error.kind == 'provider_close_failed':
            return
        if closed and not _is_consumer_cancellation(error):
            self._provider_failure = error

    def observe(self, chunk: Any) -> None:
        if _lookup(chunk, 'usage') is not None:
            self.attempt_usage = _extract_normalized_usage(chunk)
            self.attempt_cache = _extract_prompt_cache_observation(chunk)
        choices = _lookup(chunk, 'choices')
        if choices:
            reason = _lookup(choices[0], 'finish_reason')
            if reason == 'content_filter':
                raise ModelTransportError('content_filter')
            if reason == 'length':
                raise ModelTransportError('provider_rejected')
            if reason in {'stop', 'tool_calls', 'function_call'}:
                self.terminal_seen = True
            delta = _lookup(choices[0], 'delta')
            if _lookup(delta, 'tool_calls') or _lookup(delta, 'function_call'):
                self.phase = 'tool'
            elif self.phase == 'before_output' and (
                _lookup(delta, 'reasoning_content') or _lookup(delta, 'reasoning')
            ):
                self.phase = 'thinking'

    def recorded_usage(self, usage: Mapping[str, int]) -> dict[str, int]:
        for key, value in usage.items():
            self.usage[key] = self.usage.get(key, 0) + value
        return dict(self.usage)

    def body_started(self) -> None:
        if self.phase != 'tool':
            self.phase = 'body'

    def transport_timeout(self):
        return ModelTransportTimeout(remaining=self.remaining, limits=self.limits,
                                     header_retry=self.counts.get('header', 0) > 0,
                                     owned_client_factory=self.owned_client_factory)

    def retry(self, error: BaseException) -> bool:
        if error is not self._provider_failure:
            return False
        self._provider_failure = None
        self.checkpoint()
        if isinstance(error, ResponsesBackgroundInterrupted):
            # This response may already be accepted. Only its owned adapter
            # may resume GET reads; a new model attempt must not create it again.
            return False
        kind, retry_after = _model_transport_failure(error, self.wall_clock())
        decision = dict(self.policy({'kind': kind, 'phase': self.phase,
                                    'counts': dict(self.counts), 'remaining': self.remaining(),
                                    'jitter': self.jitter(), 'retry_after': retry_after}))
        if decision.get('retry') is not True:
            return False
        budget = str(decision['budget'])
        self.counts[budget] = self.counts.get(budget, 0) + 1
        extra = decision.get('extra_budget')
        if isinstance(extra, str):
            self.counts[extra] = self.counts.get(extra, 0) + 1
        self.non_stream = bool(decision.get('non_stream')) or self.non_stream
        self.retry_number += 1
        delay = float(decision['delay'])
        if self.on_retry is not None:
            self._notify_retry(kind, budget, delay)
        if self.wait is not None:
            self.wait(delay)
        else:
            wake = self.clock() + delay
            while self.clock() < wake:
                self.checkpoint()
                remaining_wait = wake - self.clock()
                if remaining_wait > 0:
                    sleep(min(0.1, remaining_wait))
        self.checkpoint()
        self.remaining()
        return True

    def _notify_retry(self, kind: str, budget: str, delay: float) -> None:
        # Display is not a permission check. Query the same frozen recipe only
        # when observed; failures here never change a real retry decision.
        try:
            display = dict(self.policy({'kind': 'retry_display', 'reason': kind,
                                        'budget': budget, 'counts': dict(self.counts)}))
            if (set(display) != {'reason', 'used', 'limit'}
                    or type(display['reason']) is not str or display['reason'] != kind
                    or kind not in {'server', 'connection', 'timeout', 'stalled',
                                    'rate_limit', 'header_timeout', 'malformed_stream'}
                    or budget not in {'before_output', 'thinking_error', 'thinking_stall',
                                      'header', 'fallback'}
                    or type(display['used']) is not int
                    or display['used'] != self.counts.get(budget)
                    or type(display['limit']) is not int
                    or not 1 <= display['used'] <= display['limit']):
                return
            self.on_retry({'attempt': self.retry_number, 'delay': delay, 'budget': budget,
                           **display})
        except Exception:
            return


def _model_transport_failure(error: BaseException, now: datetime) -> tuple[str, float | None]:
    """Classify safe provider metadata only; no raw exception or headers leave."""
    cause = error
    seen: set[int] = set()
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        if isinstance(cause, ssl.SSLError) or isinstance(getattr(cause, 'reason', None), ssl.SSLError):
            return 'certificate', None
        cause = cause.__cause__ or cause.__context__
    # 原生协议已解析的永久额度事实优先于通用 HTTP 429 分类。
    if isinstance(error, ResponsesError) and error.category == 'usage_limit':
        return 'quota', None
    body = getattr(error, 'body', None)
    response = getattr(error, 'response', None)
    if body is None and callable(getattr(response, 'json', None)):
        try:
            body = response.json()
        except (TypeError, ValueError):
            body = None
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except (TypeError, ValueError):
            body = None
    payload = body.get('error', body) if isinstance(body, Mapping) else {}
    code = str((payload.get('code') or payload.get('type') or '') if isinstance(payload, Mapping) else '').lower()
    code = code or str(getattr(error, 'code', '')).lower()
    if code in {'insufficient_quota', 'quota_exceeded', 'billing_hard_limit_reached', 'usage_limit_reached'}:
        return 'quota', None
    if code in {'content_filter', 'content_policy_violation', 'safety', 'refusal'}:
        return 'content_filter', None
    status = getattr(error, 'status_code', getattr(error, 'code', None))
    if status in {401, 403}:
        return 'authentication', None
    if isinstance(error, (ModelTransportError, ResponsesBackgroundInterrupted)):
        return error.kind, None
    if isinstance(error, json.JSONDecodeError):
        return 'malformed_stream', None
    if type(status) is int and (status == 429 or 500 <= status <= 599):
        headers = getattr(error, 'headers', None) or getattr(response, 'headers', None)
        retry_after = None
        if isinstance(headers, Mapping):
            raw = headers.get('retry-after', headers.get('Retry-After'))
            if raw is not None:
                try:
                    retry_after = float(raw)
                except (TypeError, ValueError):
                    try:
                        instant = parsedate_to_datetime(str(raw))
                        retry_after = max(0, (instant - now).total_seconds())
                    except (TypeError, ValueError, OverflowError):
                        pass
                if retry_after is not None and (not math.isfinite(retry_after) or retry_after < 0):
                    retry_after = None
        return ('rate_limit' if status == 429 else 'server'), retry_after
    if isinstance(error, TimeoutError) or type(error).__name__ in {'APITimeoutError', 'ReadTimeout', 'ConnectTimeout'}:
        return 'timeout', None
    if isinstance(error, (ConnectionError, URLError)) or type(error).__name__ == 'APIConnectionError':
        return 'connection', None
    return 'provider_rejected', None


def _with_model_retries(control: ModelRetryControl, sink: WireAttemptSink | None,
                        timeout: float | None, invoke: Callable[[float], Any]) -> Any:
    if sink is None:
        raise ValueError('model retries require an audited wire attempt sink')
    control.start(timeout)
    while True:
        remaining = control.before_attempt()
        try:
            return invoke(remaining)
        except BaseException as error:
            if not control.retry(error):
                raise


class WireAttemptHandle(Protocol):
    def succeeded(
        self,
        *,
        usage: Mapping[str, int],
        cache_observation: Mapping[str, int] | None,
    ) -> None: ...

    def failed_transport(self, *, error_code: str,
                         usage: Mapping[str, int] | None = None,
                         cache_observation: Mapping[str, int] | None = None) -> None: ...

    def consumer_cancelled(self, *, usage: Mapping[str, int] | None = None,
                           cache_observation: Mapping[str, int] | None = None) -> None: ...

    def invoke_wire(self, handler: Callable[[], Any]) -> Any: ...


class WireAttemptSink(Protocol):
    def begin_model_wire_attempt(self) -> WireAttemptHandle: ...


class EgressLease(Protocol):
    def finish(self, status: str, *, error_code: str | None = None) -> None: ...


EgressGuard = Callable[[str, tuple[str, ...], int], EgressLease]
StructuredModeName = str
StructuredMode = tuple[StructuredModeName, list[dict[str, Any]], dict[str, Any] | type[BaseModel] | None]
STRUCTURED_MODE_SCHEMA = "schema"
STRUCTURED_MODE_JSON_OBJECT = "json_object"
STRUCTURED_MODE_PROMPT = "prompt"
STRUCTURED_RESPONSE_FORMAT_ERROR_MARKERS = (
    "response_format",
    "json_schema",
    "json_object",
    "schema is not supported",
    "not support schema",
)
_STRUCTURED_MODE_CACHE: dict[str, StructuredModeName] = {}
_STRUCTURED_MODE_CACHE_LOCK = Lock()


class _ProviderAttemptTerminal:
    """Finish one authorized wire attempt exactly once."""

    def __init__(
        self,
        lease: EgressLease,
        observer: ProviderAttemptObserver | None,
        wire_attempt: WireAttemptHandle | None,
    ) -> None:
        self._lease = lease
        self._observer = observer
        self._wire_attempt = wire_attempt
        self._lock = Lock()
        self._finished = False

    def invoke(self, handler: Callable[[], Any]) -> Any:
        try:
            if self._wire_attempt is None:
                return handler()
            invoke_wire = getattr(self._wire_attempt, "invoke_wire", None)
            if not callable(invoke_wire):
                raise RuntimeError("model wire attempt Handler is unavailable")
            return invoke_wire(handler)
        except BaseException as error:
            # 接点复验可在 provider handler 前拒绝；幂等收口保留原回执并完成 observer/lease。
            if _is_consumer_cancellation(error):
                self.cancelled(error)
            else:
                self.failed(error, error_code="wire_handler_failed")
            raise

    def succeeded(
        self,
        *,
        usage: Mapping[str, int],
        cache_observation: Mapping[str, int] | None,
    ) -> None:
        self._finish("succeeded", None, None, usage, cache_observation)

    def failed(self, error: BaseException, *, error_code: str) -> None:
        self._finish("failed", error, error_code, None, None)

    def cancelled(self, error: BaseException) -> None:
        self._finish("cancelled", error, "consumer_cancelled", None, None)

    def _finish(
        self,
        status: str,
        error: BaseException | None,
        error_code: str | None,
        usage: Mapping[str, int] | None,
        cache_observation: Mapping[str, int] | None,
    ) -> None:
        with self._lock:
            if self._finished:
                return
            self._finished = True
        wire_error: BaseException | None = None
        if self._wire_attempt is not None:
            try:
                if status == "succeeded":
                    self._wire_attempt.succeeded(
                        usage=usage or {},
                        cache_observation=cache_observation,
                    )
                elif status == "cancelled":
                    self._wire_attempt.consumer_cancelled()
                else:
                    self._wire_attempt.failed_transport(
                        error_code=f"ai.{error_code or 'provider_request_failed'}"
                    )
            except BaseException as attempt_error:
                wire_error = attempt_error
        if self._observer is not None:
            try:
                self._observer(status, error)
            except Exception:
                pass
        try:
            self._lease.finish(status, error_code=error_code)
        finally:
            if wire_error is not None:
                raise wire_error


class LiteLLMCompletionGateway:
    def __init__(
        self,
        *,
        provider: str,
        model: str,
        base_url: str,
        api_key: str | None,
        api_key_provider: Callable[[], str] | None = None,
        anonymous: bool = False,
        reasoning_effort: str | None = None,
        capabilities: ModelCapabilities | None = None,
        completion_fn: CompletionFn | None = None,
        acompletion_fn: AsyncCompletionFn | None = None,
        egress_guard: EgressGuard | None = None,
        egress_purpose: str = "model_completion",
        egress_categories: tuple[str, ...] = ("instructions", "source_excerpt"),
        provider_attempt_observer: ProviderAttemptObserver | None = None,
        context_window_tokens: int = 1_000_000,
        reserved_output_tokens: int = 20_000,
        background_options: ProviderBackgroundOptions | None = None,
    ) -> None:
        normalized_api_key = api_key.strip() if isinstance(api_key, str) else ""
        if normalized_api_key and api_key_provider is not None:
            raise RuntimeError("API Key 注入来源冲突。")
        if not normalized_api_key and api_key_provider is None and not anonymous:
            raise RuntimeError("缺少 API Key，无法调用模型。")
        if (
            not isinstance(context_window_tokens, int)
            or isinstance(context_window_tokens, bool)
            or not isinstance(reserved_output_tokens, int)
            or isinstance(reserved_output_tokens, bool)
            or context_window_tokens < 2
            or reserved_output_tokens < 1
            or reserved_output_tokens >= context_window_tokens
        ):
            raise RuntimeError("模型 Token 预算配置无效。")

        self._provider = provider.strip()
        self._model = _normalize_litellm_model(self._provider, model)
        self._base_url = resolve_openai_compatible_api_base_url(base_url)
        # A keyless gateway is deliberately explicit. The routing runtime is
        # the only production caller allowed to construct it and derives this
        # from the canonical Provider egress manifest (loopback,
        # external=false). Do not manufacture placeholder credentials.
        self._api_key_provider = api_key_provider or (
            (lambda: normalized_api_key) if normalized_api_key else None
        )
        self._anonymous = bool(anonymous)
        self.capabilities = capabilities or resolve_model_capabilities(self._provider, model, self._base_url)
        self._reasoning_effort = _normalize_reasoning_effort(reasoning_effort, self.capabilities.reasoning_efforts)
        self._structured_mode_cache_key = _build_structured_mode_cache_key(
            provider=self._provider,
            base_url=self._base_url,
            model=self._model,
            api_key_identity=(
                "lease" if api_key_provider is not None
                else hashlib.sha256(normalized_api_key.encode("utf-8")).hexdigest()[:12]
                if normalized_api_key else "anonymous"
            ),
        )
        self._structured_mode_cache_key += "|" + repr(self.capabilities)
        self.cache_identity = "|".join(
            [
                type(self).__module__,
                type(self).__qualname__,
                self._provider,
                self._base_url.rstrip("/"),
                self._model,
                self._reasoning_effort or "",
                repr(self.capabilities),
            ]
        )
        self._completion = completion_fn or (
            _anonymous_openai_compatible_completion if self._anonymous
            else _load_litellm_completion()
        )
        if background_options is not None:
            if (not isinstance(background_options, ProviderBackgroundOptions)
                    or not self.capabilities.background_resume
                    or not isinstance(completion_fn, ResponsesCompletion)
                    or not completion_fn.background_resume_capable
                    or completion_fn.api_base != self._base_url.rstrip('/')):
                raise ValueError('provider_background_adapter_invalid')
            self._completion = lambda **request: completion_fn(
                **request, provider_background=background_options)
        self._acompletion = acompletion_fn or (
            _anonymous_openai_compatible_acompletion if self._anonymous
            else _load_litellm_acompletion()
        )
        self._egress_guard = egress_guard
        self._egress_purpose = egress_purpose
        self._egress_categories = egress_categories
        self._provider_attempt_observer = provider_attempt_observer
        self._context_window_tokens = context_window_tokens
        self._reserved_output_tokens = reserved_output_tokens
        self._input_budget_snapshot: dict[str, int] | None = None

    def input_budget_limits(self, *, max_tokens: int | None = None) -> dict[str, int]:
        """Read configured capacity without invoking a provider or egress guard."""
        reserve = max_tokens if type(max_tokens) is int and max_tokens > 0 else self._reserved_output_tokens
        return {"window": self._context_window_tokens, "reserve": reserve}

    def input_budget_snapshot(self) -> dict[str, int] | None:
        """Return only numeric observations from this gateway's latest budget check."""
        return dict(self._input_budget_snapshot) if self._input_budget_snapshot is not None else None

    def _wire_api_key(self) -> str | None:
        if self._anonymous:
            return None
        if self._api_key_provider is None:
            raise RuntimeError("缺少 API Key，无法调用模型。")
        value = self._api_key_provider()
        if not isinstance(value, str) or not value.strip():
            raise RuntimeError("API Key 注入失败。")
        return value.strip()

    def _compatible_request(self, request: dict[str, Any]) -> dict[str, Any]:
        if not self.capabilities.developer_role:
            request["messages"] = [dict(message, role="system") if message.get("role") == "developer"
                                   else message for message in request["messages"]]
        if "max_tokens" in request and self.capabilities.max_tokens_field != "max_tokens":
            request[self.capabilities.max_tokens_field] = request.pop("max_tokens")
        return request

    def _stream_options(self) -> dict[str, Any]:
        return {"stream": True, **({"stream_options": {"include_usage": True}}
                                  if self.capabilities.stream_usage else {})}

    def complete_text(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        temperature: float = 0,
        response_format: dict[str, Any] | type[BaseModel] | None = None,
        max_tokens: int | None = None,
        timeout: float | None = None,
        wire_attempt_sink: WireAttemptSink | None = None,
    ) -> str:
        content, _usage, _prompt_cache_observation = self.complete_text_with_usage(
            messages,
            temperature=temperature,
            response_format=response_format,
            max_tokens=max_tokens,
            timeout=timeout,
            wire_attempt_sink=wire_attempt_sink,
        )
        return content

    def complete_text_with_usage(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        temperature: float = 0,
        response_format: dict[str, Any] | type[BaseModel] | None = None,
        max_tokens: int | None = None,
        timeout: float | None = None,
        wire_attempt_sink: WireAttemptSink | None = None,
        retry_control: ModelRetryControl | None = None,
        _retry_state: ModelRetryControl | None = None,
    ) -> tuple[str, dict[str, int], dict[str, int] | None]:
        """Return response text plus sanitized provider usage observations.

        The third item only contains token counters explicitly reported by the
        provider.  It intentionally carries neither prompts nor raw provider
        payloads, and callers must not infer a cache result when it is ``None``.
        """
        if retry_control is not None:
            return _with_model_retries(retry_control, wire_attempt_sink, timeout,
                lambda remaining: self.complete_text_with_usage(messages,
                    temperature=temperature, response_format=response_format,
                    max_tokens=max_tokens, timeout=remaining,
                    wire_attempt_sink=wire_attempt_sink, _retry_state=retry_control))
        dumped_messages = _dump_messages(messages)
        self._enforce_input_token_budget(dumped_messages, max_tokens=max_tokens)
        request = _build_completion_request(
            model=self._model,
            messages=dumped_messages,
            api_base=self._base_url,
            api_key=self._wire_api_key(),
            temperature=temperature,
            response_format=response_format,
            max_tokens=max_tokens,
            timeout=timeout,
            reasoning_effort=self._reasoning_effort,
        )
        request = self._compatible_request(request)
        lease = self._authorize_egress(request)
        terminal = self._attempt_terminal(lease, wire_attempt_sink)
        def handle_wire():
            try:
                if _retry_state is not None:
                    request['timeout'] = _retry_state.transport_timeout()
                response = self._completion(**request)
            except BaseException as error:
                if _is_consumer_cancellation(error):
                    terminal.cancelled(error)
                else:
                    terminal.failed(error, error_code="provider_request_failed")
                if _retry_state is not None:
                    _retry_state.provider_failed(error)
                if _is_unsupported_reasoning_effort_error(error):
                    raise RuntimeError("此模型不支持思考强度。") from error
                raise
            usage = _extract_normalized_usage(response)
            cache_observation = _extract_prompt_cache_observation(response)
            terminal.succeeded(usage=usage, cache_observation=cache_observation)
            if _retry_state is not None:
                usage = _retry_state.recorded_usage(usage)
            return response, usage, cache_observation

        response, usage, cache_observation = terminal.invoke(handle_wire)
        content = _extract_completion_content(response)
        if content.strip():
            return (
                content.strip(),
                usage,
                cache_observation,
            )
        raise RuntimeError("模型返回缺少 message.content。")

    async def acomplete_text(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        temperature: float = 0,
        response_format: dict[str, Any] | type[BaseModel] | None = None,
        max_tokens: int | None = None,
        timeout: float | None = None,
        wire_attempt_sink: WireAttemptSink | None = None,
    ) -> str:
        dumped_messages = _dump_messages(messages)
        self._enforce_input_token_budget(dumped_messages, max_tokens=max_tokens)
        request = _build_completion_request(
            model=self._model,
            messages=dumped_messages,
            api_base=self._base_url,
            api_key=self._wire_api_key(),
            temperature=temperature,
            response_format=response_format,
            max_tokens=max_tokens,
            timeout=timeout,
            reasoning_effort=self._reasoning_effort,
        )
        request = self._compatible_request(request)
        lease = self._authorize_egress(request)
        terminal = self._attempt_terminal(lease, wire_attempt_sink)
        try:
            response = await self._acompletion(**request)
        except BaseException as error:
            if _is_consumer_cancellation(error):
                terminal.cancelled(error)
            else:
                terminal.failed(error, error_code="provider_request_failed")
            if _is_unsupported_reasoning_effort_error(error):
                raise RuntimeError("此模型不支持思考强度。") from error
            raise
        terminal.succeeded(
            usage=_extract_normalized_usage(response),
            cache_observation=_extract_prompt_cache_observation(response),
        )
        content = _extract_completion_content(response)
        if content.strip():
            return content.strip()
        raise RuntimeError("模型返回缺少 message.content。")

    def stream_text(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        temperature: float = 0,
        response_format: dict[str, Any] | type[BaseModel] | None = None,
        timeout: float | None = None,
        wire_attempt_sink: WireAttemptSink | None = None,
    ) -> Iterator[str]:
        if self._anonymous:
            raise RuntimeError("anonymous local model streaming is unsupported")
        dumped_messages = _dump_messages(messages)
        self._enforce_input_token_budget(dumped_messages, max_tokens=None)
        request = _build_stream_request(
            model=self._model,
            messages=dumped_messages,
            api_base=self._base_url,
            api_key=self._wire_api_key(),
            temperature=temperature,
            response_format=response_format,
            timeout=timeout,
            reasoning_effort=self._reasoning_effort,
        )
        request = self._compatible_request(request)
        lease = self._authorize_egress(request)
        terminal = self._attempt_terminal(lease, wire_attempt_sink)
        try:
            stream = self._completion(
                **request,
                **self._stream_options(),
            )
        except BaseException as error:
            if _is_consumer_cancellation(error):
                terminal.cancelled(error)
            else:
                terminal.failed(error, error_code="provider_request_failed")
            if _is_unsupported_reasoning_effort_error(error):
                raise RuntimeError("此模型不支持思考强度。") from error
            raise
        final_provider_metadata: Any | None = None
        try:
            for chunk in stream:
                if _lookup(chunk, "usage") is not None:
                    final_provider_metadata = chunk
                delta = _extract_stream_delta(chunk)
                if delta:
                    yield delta
        except BaseException as error:
            if _is_consumer_cancellation(error):
                terminal.cancelled(error)
            else:
                terminal.failed(error, error_code="provider_stream_failed")
            _close_sync_stream(stream)
            raise
        else:
            terminal.succeeded(
                usage=_extract_normalized_usage(final_provider_metadata),
                cache_observation=_extract_prompt_cache_observation(
                    final_provider_metadata
                ),
            )

    def stream_text_with_metadata(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        temperature: float = 0,
        response_format: dict[str, Any] | type[BaseModel] | None = None,
        max_tokens: int | None = None,
        timeout: float | None = None,
        wire_attempt_sink: WireAttemptSink | None = None,
        _retry_state: ModelRetryControl | None = None,
    ) -> Iterator[ChatCompletionStreamChunk]:
        if self._anonymous:
            raise RuntimeError("anonymous local model streaming is unsupported")
        dumped_messages = _dump_messages(messages)
        self._enforce_input_token_budget(dumped_messages, max_tokens=max_tokens)
        request = _build_stream_request(
            model=self._model,
            messages=dumped_messages,
            api_base=self._base_url,
            api_key=self._wire_api_key(),
            temperature=temperature,
            response_format=response_format,
            timeout=timeout,
            reasoning_effort=self._reasoning_effort,
        )
        if max_tokens is not None:
            request["max_tokens"] = max_tokens
        request = self._compatible_request(request)
        lease = self._authorize_egress(request)
        terminal = self._attempt_terminal(lease, wire_attempt_sink)
        try:
            if _retry_state is not None:
                request['timeout'] = _retry_state.transport_timeout()
            stream = self._completion(
                **request,
                **self._stream_options(),
            )
            if _retry_state is not None:
                stream = request['timeout'].bind_completion(stream)
        except BaseException as error:
            if _retry_state is not None and isinstance(request.get('timeout'), ModelTransportTimeout):
                error = request['timeout'].header_error(error)
            if _is_consumer_cancellation(error):
                terminal.cancelled(error)
            else:
                terminal.failed(error, error_code="provider_request_failed")
            if _retry_state is not None:
                _retry_state.provider_failed(error)
            if _is_unsupported_reasoning_effort_error(error):
                raise RuntimeError("此模型不支持思考强度。") from error
            raise error
        final_provider_metadata: Any | None = None
        final_usage: dict[str, int] = {}
        close_attempted = False
        try:
            for chunk in stream:
                if _retry_state is not None:
                    _retry_state.observe(chunk)
                delta = _extract_stream_delta(chunk)
                if _lookup(chunk, "usage") is not None:
                    final_provider_metadata = chunk
                    final_usage = _extract_usage(chunk)
                if delta:
                    yield ChatCompletionStreamChunk(delta=delta)
            if _retry_state is not None and not _retry_state.terminal_seen:
                raise ConnectionError('provider stream ended before its terminal event')
            if final_usage:
                yield ChatCompletionStreamChunk(usage=final_usage,
                    cache_observation=_extract_prompt_cache_observation(final_provider_metadata))
        except BaseException as error:
            close_attempted = True
            closed = _close_sync_stream(stream)
            if closed and _retry_state is not None:
                from .model_transport import _provider_closed_witness
                _retry_state.closed_witness = _provider_closed_witness()
            if _is_consumer_cancellation(error):
                terminal.cancelled(error)
            else:
                terminal.failed(error, error_code="provider_stream_failed")
            if _retry_state is not None:
                _retry_state.provider_failed(error, closed=closed)
            raise
        else:
            terminal.succeeded(
                usage=_extract_normalized_usage(final_provider_metadata),
                cache_observation=_extract_prompt_cache_observation(
                    final_provider_metadata
                ),
            )

        finally:
            if not close_attempted:
                _close_sync_stream(stream)

    def test_connection(self) -> str:
        # Reasoning models may consume a tiny output budget before emitting
        # message.content. Keep the diagnostic bounded but allow a real reply.
        return self.complete_text(
            [{"role": "user", "content": "Reply with exactly: ok"}],
            temperature=0,
            max_tokens=256,
            timeout=30,
        )

    async def astream_text(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        temperature: float = 0,
        response_format: dict[str, Any] | type[BaseModel] | None = None,
        timeout: float | None = None,
        wire_attempt_sink: WireAttemptSink | None = None,
    ):
        if self._anonymous:
            raise RuntimeError("anonymous local model streaming is unsupported")
        dumped_messages = _dump_messages(messages)
        self._enforce_input_token_budget(dumped_messages, max_tokens=None)
        request = _build_stream_request(
            model=self._model,
            messages=dumped_messages,
            api_base=self._base_url,
            api_key=self._wire_api_key(),
            temperature=temperature,
            response_format=response_format,
            timeout=timeout,
            reasoning_effort=self._reasoning_effort,
        )
        request = self._compatible_request(request)
        lease = self._authorize_egress(request)
        terminal = self._attempt_terminal(lease, wire_attempt_sink)
        try:
            stream = await self._acompletion(
                **request,
                **self._stream_options(),
            )
        except BaseException as error:
            if _is_consumer_cancellation(error):
                terminal.cancelled(error)
            else:
                terminal.failed(error, error_code="provider_request_failed")
            if _is_unsupported_reasoning_effort_error(error):
                raise RuntimeError("此模型不支持思考强度。") from error
            raise
        final_provider_metadata: Any | None = None
        try:
            async for chunk in stream:
                if _lookup(chunk, "usage") is not None:
                    final_provider_metadata = chunk
                delta = _extract_stream_delta(chunk)
                if delta:
                    yield delta
        except BaseException as error:
            if _is_consumer_cancellation(error):
                terminal.cancelled(error)
            else:
                terminal.failed(error, error_code="provider_stream_failed")
            await _close_async_stream(stream)
            raise
        else:
            terminal.succeeded(
                usage=_extract_normalized_usage(final_provider_metadata),
                cache_observation=_extract_prompt_cache_observation(
                    final_provider_metadata
                ),
            )

    def _authorize_egress(self, request: Mapping[str, object]) -> EgressLease:
        if self._egress_guard is None:
            raise RuntimeError("模型外发政策未配置。")
        redacted = {key: value for key, value in request.items() if key not in {"api_key", "api_base"}}
        payload_bytes = len(json.dumps(redacted, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8"))
        return self._egress_guard(
            self._egress_purpose,
            self._egress_categories,
            payload_bytes,
        )

    def _enforce_input_token_budget(
        self, messages: Sequence[dict[str, Any]], *, max_tokens: int | None,
    ) -> None:
        output_reserve = (
            max_tokens
            if isinstance(max_tokens, int) and not isinstance(max_tokens, bool) and max_tokens > 0
            else self._reserved_output_tokens
        )
        if output_reserve >= self._context_window_tokens:
            raise RuntimeError("模型输出 Token 预算超过上下文窗口。")
        estimated = _estimate_input_tokens(messages)
        if estimated > self._context_window_tokens - output_reserve:
            raise RuntimeError("模型输入超过 Token 硬预算，已在外发前阻止。")
        self._input_budget_snapshot = {"window": self._context_window_tokens,
            "reserve": output_reserve, "estimated_input_tokens": estimated}

    def _attempt_terminal(
        self,
        lease: EgressLease,
        wire_attempt_sink: WireAttemptSink | None,
    ) -> _ProviderAttemptTerminal:
        try:
            wire_attempt = (
                wire_attempt_sink.begin_model_wire_attempt()
                if wire_attempt_sink is not None
                else None
            )
        except BaseException:
            lease.finish("not_sent", error_code="model_attempt_dispatch_failed")
            raise
        return _ProviderAttemptTerminal(
            lease,
            self._provider_attempt_observer,
            wire_attempt,
        )

    def stream_structured_with_usage(
        self, messages, *, response_model, on_delta, validate_current,
        max_tokens=None, timeout=None, field="answer", wire_attempt_sink=None,
        retry_control=None, _retry_state=None,
        observe=None,
    ):
        if retry_control is not None:
            def attempt(remaining):
                if retry_control.non_stream:
                    result, usage, _cache = self.complete_structured_with_usage(
                        messages, response_model=response_model, retries=0,
                        timeout=remaining, max_tokens=max_tokens, max_wire_attempts=1,
                        wire_attempt_sink=wire_attempt_sink, retry_control=retry_control)
                    validate_current()
                    text = getattr(result, field)
                    if isinstance(text, str) and text:
                        retry_control.body_started()
                        on_delta(text)
                    return result, usage
                return self.stream_structured_with_usage(messages,
                    response_model=response_model, on_delta=on_delta,
                    validate_current=validate_current, max_tokens=max_tokens,
                    timeout=remaining, field=field, wire_attempt_sink=wire_attempt_sink,
                    observe=observe,
                    _retry_state=retry_control)
            return _with_model_retries(retry_control, wire_attempt_sink, timeout, attempt)
        modes = _build_structured_request_modes(cache_key=self._structured_mode_cache_key,
            messages=messages, response_model=response_model, validation_error=None,
            supported=self.capabilities.structured_modes)
        # Streams cannot replay format fallbacks after delivering a prefix.
        selected = next((mode for mode in modes if mode[0] == "json_object"),
                        next(mode for mode in modes if mode[0] == "prompt"))
        # 本地 Token 拒绝不登记外发尝试。
        self._enforce_input_token_budget(_dump_messages(selected[1]), max_tokens=max_tokens)
        decoder, usage, cache_observation = PartialJSONField(field, observe=observe), {}, None
        wire = wire_attempt_sink.begin_model_wire_attempt() if wire_attempt_sink is not None else None

        def consume():
            nonlocal usage, cache_observation
            stream = self.stream_text_with_metadata(selected[1], response_format=selected[2],
                max_tokens=max_tokens, timeout=timeout, _retry_state=_retry_state)
            try:
                for chunk in stream:
                    validate_current()
                    if chunk.delta:
                        text = decoder.feed(chunk.delta)
                        if text:
                            if _retry_state is not None:
                                _retry_state.body_started()
                            on_delta(text)
                    if chunk.usage:
                        usage = _extract_normalized_usage({"usage": chunk.usage})
                        cache_observation = chunk.cache_observation
                validate_current()
                if wire is not None:
                    wire.succeeded(usage=usage, cache_observation=cache_observation)
                if _retry_state is not None:
                    usage = _retry_state.recorded_usage(usage)
                return decoder.raw, usage
            except BaseException as error:
                if wire is not None:
                    if _is_consumer_cancellation(error):
                        if _retry_state is not None and _retry_state.attempt_usage:
                            wire.consumer_cancelled(usage=_retry_state.attempt_usage,
                                cache_observation=_retry_state.attempt_cache)
                            _retry_state.recorded_usage(_retry_state.attempt_usage)
                        else:
                            wire.consumer_cancelled()
                    else:
                        if _retry_state is not None and _retry_state.attempt_usage:
                            wire.failed_transport(error_code="ai.provider_stream_failed",
                                usage=_retry_state.attempt_usage,
                                cache_observation=_retry_state.attempt_cache)
                            _retry_state.recorded_usage(_retry_state.attempt_usage)
                        else:
                            wire.failed_transport(error_code="ai.provider_stream_failed")
                raise
            finally:
                stream.close()

        # The durable Handler encloses consumption, while deltas reach the
        # caller immediately. Schema rejection follows the actual wire receipt.
        raw, usage = wire.invoke_wire(consume) if wire is not None else consume()
        try:
            return validate_json_response(raw_text=raw, response_model=response_model), usage
        except (StructuredResponseDecodeError, ValidationError) as error:
            # 只有原 wire 已成功返回后的解码拒绝拥有完成资格；未知用量仍为空。
            code = error.code if type(error) is StructuredResponseDecodeError else 'schema_invalid'
            raise StructuredResponseDecodeError(code, 'model_response_decoding_failed',
                completed_wire=wire is not None, usage=usage if wire is not None else None) from None

    def complete_structured(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        response_model: type[StructuredResponseT],
        temperature: float = 0,
        retries: int = 2,
        timeout: float | None = None,
        max_wire_attempts: int = 9,
        wire_attempt_sink: WireAttemptSink | None = None,
    ) -> StructuredResponseT:
        return self.complete_structured_with_usage(messages, response_model=response_model,
            temperature=temperature, retries=retries, timeout=timeout,
            max_wire_attempts=max_wire_attempts, wire_attempt_sink=wire_attempt_sink)[0]

    def complete_structured_with_usage(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        response_model: type[StructuredResponseT],
        temperature: float = 0,
        retries: int = 2,
        timeout: float | None = None,
        max_tokens: int | None = None,
        max_wire_attempts: int = 9,
        wire_attempt_sink: WireAttemptSink | None = None,
        retry_control: ModelRetryControl | None = None,
    ) -> tuple[StructuredResponseT, dict[str, int], dict[str, int] | None]:
        _validate_wire_attempt_budget(max_wire_attempts)
        validation_error: str | None = None
        last_raw_text = ""
        started_at = monotonic()
        wire_attempts = 0

        for attempt_index in range(retries + 1):
            modes = _build_structured_request_modes(
                cache_key=self._structured_mode_cache_key,
                messages=messages,
                response_model=response_model,
                validation_error=validation_error,
                supported=self.capabilities.structured_modes,
            )
            mode_errors: list[str] = []
            for mode_name, structured_messages, response_format in modes:
                if wire_attempts >= max_wire_attempts:
                    raise RuntimeError("LiteLLM 结构化请求失败: 已达到 wire attempt 预算。")
                wire_attempts += 1
                try:
                    last_raw_text, usage, cache_observation = self.complete_text_with_usage(
                        structured_messages,
                        temperature=temperature,
                        response_format=response_format,
                        timeout=_remaining_timeout(timeout, started_at=started_at),
                        max_tokens=max_tokens,
                        wire_attempt_sink=wire_attempt_sink,
                        retry_control=retry_control,
                    )
                    _remember_structured_mode(self._structured_mode_cache_key, mode_name)
                    break
                except Exception as error:
                    if not _is_response_format_error(error) or response_format is None:
                        raise
                    mode_errors.append(str(error))
            else:
                raise RuntimeError(
                    "LiteLLM 结构化请求失败: 所有 response_format 模式均不可用。"
                    f"{'; '.join(mode_errors)}"
                )
            try:
                validated = validate_json_response(
                    raw_text=last_raw_text,
                    response_model=response_model,
                )
                return validated, usage, cache_observation  # type: ignore[return-value]
            except Exception as error:
                validation_error = describe_validation_error(error)
                if attempt_index == retries:
                    raise RuntimeError(
                        "LiteLLM 结构化请求失败: "
                        f"{validation_error}\n原始输出:\n{last_raw_text}"
                    ) from error

        raise RuntimeError("LiteLLM 结构化请求失败: 未知错误。")

    async def acomplete_structured(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        response_model: type[StructuredResponseT],
        temperature: float = 0,
        retries: int = 2,
        timeout: float | None = None,
        max_wire_attempts: int = 9,
        wire_attempt_sink: WireAttemptSink | None = None,
    ) -> StructuredResponseT:
        _validate_wire_attempt_budget(max_wire_attempts)
        validation_error: str | None = None
        last_raw_text = ""
        started_at = monotonic()
        wire_attempts = 0

        for attempt_index in range(retries + 1):
            modes = _build_structured_request_modes(
                cache_key=self._structured_mode_cache_key,
                messages=messages,
                response_model=response_model,
                validation_error=validation_error,
                supported=self.capabilities.structured_modes,
            )
            mode_errors: list[str] = []
            for mode_name, structured_messages, response_format in modes:
                if wire_attempts >= max_wire_attempts:
                    raise RuntimeError("LiteLLM 结构化请求失败: 已达到 wire attempt 预算。")
                wire_attempts += 1
                try:
                    last_raw_text = await self.acomplete_text(
                        structured_messages,
                        temperature=temperature,
                        response_format=response_format,
                        timeout=_remaining_timeout(timeout, started_at=started_at),
                        wire_attempt_sink=wire_attempt_sink,
                    )
                    _remember_structured_mode(self._structured_mode_cache_key, mode_name)
                    break
                except Exception as error:
                    if not _is_response_format_error(error) or response_format is None:
                        raise
                    mode_errors.append(str(error))
            else:
                raise RuntimeError(
                    "LiteLLM 结构化请求失败: 所有 response_format 模式均不可用。"
                    f"{'; '.join(mode_errors)}"
                )
            try:
                validated = validate_json_response(
                    raw_text=last_raw_text,
                    response_model=response_model,
                )
                return validated  # type: ignore[return-value]
            except Exception as error:
                validation_error = describe_validation_error(error)
                if attempt_index == retries:
                    raise RuntimeError(
                        "LiteLLM 结构化请求失败: "
                        f"{validation_error}\n原始输出:\n{last_raw_text}"
                    ) from error

        raise RuntimeError("LiteLLM 结构化请求失败: 未知错误。")


def _load_litellm_completion() -> CompletionFn:
    try:
        from litellm import completion
    except ModuleNotFoundError as error:
        raise RuntimeError("缺少 litellm 依赖，无法调用模型。") from error
    def complete(**request):
        return complete_with_owned_transport(completion, request)
    return complete


def _load_litellm_acompletion() -> AsyncCompletionFn:
    try:
        from litellm import acompletion
    except ModuleNotFoundError as error:
        raise RuntimeError("缺少 litellm 依赖，无法调用模型。") from error
    return acompletion


def _is_consumer_cancellation(error: BaseException) -> bool:
    return isinstance(error, (GeneratorExit, asyncio.CancelledError)) or not isinstance(
        error, Exception,
    )


def _validate_wire_attempt_budget(value: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError("max_wire_attempts must be a positive integer")


def _close_sync_stream(stream: object) -> bool:
    close = getattr(stream, "close", None)
    if not callable(close):
        return False
    try:
        close()
    except Exception:
        return False
    return True


async def _close_async_stream(stream: object) -> None:
    close = getattr(stream, "aclose", None)
    if not callable(close):
        return
    try:
        await close()
    except BaseException:
        return


def _normalize_litellm_model(provider: str, model: str) -> str:
    normalized_model = model.strip()
    if not normalized_model:
        raise RuntimeError("缺少模型名称，无法调用模型。")
    if "/" in normalized_model:
        return normalized_model
    normalized_provider = provider.strip().lower()
    if not normalized_provider:
        raise RuntimeError("缺少模型类型，无法调用模型。")
    return f"{normalized_provider}/{normalized_model}"


class _AnonymousOpenAICompatibleHTTPError(RuntimeError):
    def __init__(self, status_code: int, *, code: str | None = None,
                 retry_after: str | None = None) -> None:
        self.status_code = status_code
        self.code = code
        self.headers = {'retry-after': retry_after} if retry_after is not None else {}
        super().__init__(f"anonymous local OpenAI-compatible endpoint returned HTTP {status_code}")


class _RejectRedirectHandler(HTTPRedirectHandler):
    """Keep a manifest-approved loopback request on its original endpoint."""

    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def _anonymous_openai_compatible_completion(**request: Any) -> dict[str, Any]:
    """Perform one keyless loopback OpenAI-compatible completion.

    LiteLLM/OpenAI currently reject a missing key before opening a connection.
    This minimal transport is selected only after the model runtime has derived
    ``anonymous=True`` from the canonical egress manifest.  It intentionally
    does not add Authorization or create a synthetic key.
    """
    api_base = request.get("api_base")
    if not isinstance(api_base, str) or not api_base:
        raise RuntimeError("anonymous local model endpoint is unavailable")
    model = request.get("model")
    if not isinstance(model, str) or not model:
        raise RuntimeError("anonymous local model is unavailable")
    body: dict[str, Any] = {
        key: value for key, value in request.items()
        if key in {"messages", "temperature", "response_format", "max_tokens"}
        and value is not None
    }
    # LiteLLM prefixes a provider for its own dispatch.  OpenAI-compatible
    # servers receive the configured model identity, not that prefix.
    body["model"] = model.split("/", 1)[1] if model.startswith("openai/") else model
    timeout = request.get("timeout")
    timeout_seconds = float(timeout) if isinstance(timeout, (int, float)) else None
    encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    target = api_base.rstrip("/") + "/chat/completions"
    try:
        with build_opener(_RejectRedirectHandler()).open(Request(
            target, data=encoded,
            headers={"Content-Type": "application/json"}, method="POST",
        ), timeout=timeout_seconds) as response:
            payload = response.read()
    except HTTPError as error:
        # Keep only classification metadata, never provider prose or headers.
        code = None
        retry_after = error.headers.get('Retry-After') if error.headers is not None else None
        try:
            payload = json.loads(error.read(65536))
            details = payload.get('error', payload) if isinstance(payload, Mapping) else {}
            if isinstance(details, Mapping):
                value = details.get('code') or details.get('type')
                if value in {'insufficient_quota', 'quota_exceeded', 'billing_hard_limit_reached',
                             'usage_limit_reached', 'content_filter', 'content_policy_violation',
                             'safety', 'refusal'}:
                    code = value
        except (ValueError, OSError):
            pass
        finally:
            error.close()
        raise _AnonymousOpenAICompatibleHTTPError(error.code, code=code,
                                                 retry_after=retry_after) from error
    except URLError as error:
        raise ConnectionError("anonymous local model connection failed") from error
    try:
        decoded = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("anonymous local model response is invalid") from error
    if not isinstance(decoded, dict):
        raise RuntimeError("anonymous local model response is invalid")
    return decoded


async def _anonymous_openai_compatible_acompletion(**request: Any) -> dict[str, Any]:
    return await asyncio.to_thread(_anonymous_openai_compatible_completion, **request)






def _disable_default_deepseek_thinking(request: dict[str, Any], api_base: str, reasoning_effort: str | None) -> None:
    """DeepSeek turns thinking on by default, and its hidden reasoning counts against max_tokens.

    Without an explicit effort the short structured calls (routing, insights, links)
    were truncated with finish_reason=length. Only an explicit effort keeps thinking on.
    """
    host = (urlsplit(api_base or "").hostname or "").lower()
    if reasoning_effort is None and (host == "api.deepseek.com" or host.endswith(".deepseek.com")):
        request["extra_body"] = {"thinking": {"type": "disabled"}}


def _build_completion_request(
    *,
    model: str,
    messages: list[dict[str, Any]],
    api_base: str,
    api_key: str | None,
    temperature: float,
    response_format: dict[str, Any] | type[BaseModel] | None,
    max_tokens: int | None,
    timeout: float | None,
    reasoning_effort: str | None,
) -> dict[str, Any]:
    request: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "response_format": response_format,
        # A Turn owns the retry/fallback policy.  Hidden SDK retries would
        # create multiple provider side effects inside one recorded wire
        # attempt and make delivery certainty impossible to audit.
        "max_retries": 0,
    }
    if api_key:
        request["api_key"] = api_key
    if api_base:
        request["api_base"] = api_base
    if max_tokens is not None:
        request["max_tokens"] = max_tokens
    if timeout is not None:
        request["timeout"] = timeout
    if reasoning_effort is not None:
        request["reasoning_effort"] = reasoning_effort
        request["allowed_openai_params"] = ["reasoning_effort"]
    _disable_default_deepseek_thinking(request, api_base, reasoning_effort)
    return request


def _remaining_timeout(timeout: float | None, *, started_at: float) -> float | None:
    if timeout is None:
        return None
    if timeout <= 0:
        raise ValueError("timeout 必须是正数。")
    remaining = timeout - (monotonic() - started_at)
    if remaining <= 0:
        raise TimeoutError("模型结构化请求超过统一 deadline。")
    return remaining


def _build_stream_request(
    *,
    model: str,
    messages: list[dict[str, Any]],
    api_base: str,
    api_key: str | None,
    temperature: float,
    response_format: dict[str, Any] | type[BaseModel] | None,
    timeout: float | None,
    reasoning_effort: str | None,
) -> dict[str, Any]:
    request: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "response_format": response_format,
        "max_retries": 0,
    }
    if api_key:
        request["api_key"] = api_key
    if api_base:
        request["api_base"] = api_base
    if timeout is not None:
        request["timeout"] = timeout
    if reasoning_effort is not None:
        request["reasoning_effort"] = reasoning_effort
        request["allowed_openai_params"] = ["reasoning_effort"]
    _disable_default_deepseek_thinking(request, api_base, reasoning_effort)
    return request


def _normalize_reasoning_effort(value: str | None, supported: tuple[str, ...] = ("low", "medium", "high")) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if not normalized:
        return None
    if normalized == "none":
        return None
    if normalized not in supported:
        raise RuntimeError(f"unsupported reasoning_effort '{value}'")
    return normalized


def _is_unsupported_reasoning_effort_error(error: Exception) -> bool:
    message = str(error).lower()
    return "reasoning_effort" in message and (
        "unsupported" in message
        or "does not support" in message
        or "not support" in message
        or "unsupportedparams" in message
    )


def _build_structured_request_modes(
    *,
    cache_key: str,
    messages: Sequence[dict[str, Any]],
    response_model: type[BaseModel],
    validation_error: str | None,
    supported: tuple[str, ...] = ("schema", "json_object", "prompt"),
) -> list[StructuredMode]:
    modes = [
        (
            STRUCTURED_MODE_SCHEMA,
            _append_validation_retry_messages(messages, validation_error),
            response_model,
        ),
        (
            STRUCTURED_MODE_JSON_OBJECT,
            _build_json_mode_messages(messages=messages, validation_error=validation_error),
            {"type": "json_object"},
        ),
        (
            STRUCTURED_MODE_PROMPT,
            _build_prompt_fallback_messages(
                messages=messages,
                response_model=response_model,
                validation_error=validation_error,
            ),
            None,
        ),
    ]
    modes = [mode for mode in modes if mode[0] in supported]
    cached_mode = _lookup_structured_mode(cache_key)
    if cached_mode is None:
        return modes
    selected = [mode for mode in modes if mode[0] == cached_mode]
    if not selected:
        return modes
    remaining = [mode for mode in modes if mode[0] != cached_mode]
    return [*selected, *remaining]


def _append_validation_retry_messages(
    messages: Sequence[dict[str, Any]],
    validation_error: str | None,
) -> list[dict[str, Any]]:
    structured_messages = _dump_messages(messages)
    if validation_error:
        structured_messages.append(_build_validation_retry_message(validation_error))
    return structured_messages


def _is_response_format_error(error: Exception) -> bool:
    text = str(error).lower()
    return any(marker in text for marker in STRUCTURED_RESPONSE_FORMAT_ERROR_MARKERS)


def _build_structured_mode_cache_key(
    *,
    provider: str,
    base_url: str,
    model: str,
    api_key_identity: str,
) -> str:
    return "|".join([provider.strip(), base_url.rstrip("/"), model.strip(), api_key_identity])


def _lookup_structured_mode(cache_key: str) -> StructuredModeName | None:
    with _STRUCTURED_MODE_CACHE_LOCK:
        return _STRUCTURED_MODE_CACHE.get(cache_key)


def _remember_structured_mode(cache_key: str, mode_name: StructuredModeName) -> None:
    with _STRUCTURED_MODE_CACHE_LOCK:
        _STRUCTURED_MODE_CACHE[cache_key] = mode_name


def clear_structured_mode_cache() -> None:
    with _STRUCTURED_MODE_CACHE_LOCK:
        _STRUCTURED_MODE_CACHE.clear()


def _extract_completion_content(response: Any) -> str:
    choices = _lookup(response, "choices")
    if not isinstance(choices, Sequence) or not choices:
        return ""
    message = _lookup(choices[0], "message")
    tool_call_content = _extract_tool_call_arguments(_lookup(message, "tool_calls"))
    if tool_call_content:
        return tool_call_content
    content = _lookup(message, "content")
    if isinstance(content, str):
        return content
    if isinstance(content, Sequence):
        parts = [part for part in (_extract_text_part(item) for item in content) if part]
        return "".join(parts)
    return ""


def _extract_stream_delta(chunk: Any) -> str:
    choices = _lookup(chunk, "choices")
    if not isinstance(choices, Sequence) or not choices:
        return ""
    delta = _lookup(choices[0], "delta")
    tool_call_content = _extract_tool_call_arguments(_lookup(delta, "tool_calls"))
    if tool_call_content:
        return tool_call_content
    content = _lookup(delta, "content")
    if isinstance(content, str):
        return content
    if isinstance(content, Sequence):
        parts = [part for part in (_extract_text_part(item) for item in content) if part]
        return "".join(parts)
    return ""


def _extract_usage(chunk: Any) -> dict[str, int]:
    normalized = _extract_normalized_usage(chunk)
    legacy: dict[str, int] = {}
    if "input_tokens" in normalized:
        legacy["prompt_tokens"] = normalized["input_tokens"]
    if "output_tokens" in normalized:
        legacy["completion_tokens"] = normalized["output_tokens"]
    if "total_tokens" in normalized:
        legacy["total_tokens"] = normalized["total_tokens"]
    return legacy




def _extract_prompt_cache_observation(response: Any) -> dict[str, int] | None:
    """Return only cache counters explicitly reported by a provider.

    OpenAI Chat and Responses expose cached input tokens in nested usage
    details.  Anthropic exposes root cache counters.  LiteLLM may normalize
    the same root counter names.  DeepSeek explicitly reports a hit/miss pair;
    the pair is accepted only when it exactly partitions ``prompt_tokens``.
    """
    usage = _lookup(response, "usage")
    if usage is None:
        return None

    deepseek_hit = _lookup(usage, "prompt_cache_hit_tokens")
    deepseek_miss = _lookup(usage, "prompt_cache_miss_tokens")
    if deepseek_hit is not None or deepseek_miss is not None:
        hit = _nonnegative_int(deepseek_hit)
        miss = _nonnegative_int(deepseek_miss)
        prompt_tokens = _nonnegative_int(_lookup(usage, "prompt_tokens"))
        if hit is None or miss is None or prompt_tokens is None or hit + miss != prompt_tokens:
            return None
        return {
            "cache_read_input_tokens": hit,
            "cache_miss_input_tokens": miss,
        }

    observation: dict[str, int] = {}
    root_read_tokens = _first_nonnegative_int(
        usage,
        "cache_read_input_tokens",
    )
    root_creation_tokens = _first_nonnegative_int(
        usage,
        "cache_creation_input_tokens",
    )
    nested_read_tokens = _nested_nonnegative_int(
        usage,
        ("prompt_tokens_details", "cached_tokens"),
        ("input_tokens_details", "cached_tokens"),
    )
    nested_creation_tokens = _nested_nonnegative_int(
        usage,
        ("input_tokens_details", "cache_write_tokens"),
    )
    if (
        root_read_tokens is not None
        and nested_read_tokens is not None
        and root_read_tokens != nested_read_tokens
    ) or (
        root_creation_tokens is not None
        and nested_creation_tokens is not None
        and root_creation_tokens != nested_creation_tokens
    ):
        return None
    read_tokens = root_read_tokens if root_read_tokens is not None else nested_read_tokens
    creation_tokens = (
        root_creation_tokens
        if root_creation_tokens is not None
        else nested_creation_tokens
    )
    if read_tokens is not None:
        observation["cache_read_input_tokens"] = read_tokens
    if creation_tokens is not None:
        observation["cache_creation_input_tokens"] = creation_tokens
    return observation or None


def _nested_nonnegative_int(source: Any, *paths: tuple[str, str]) -> int | None:
    for parent_key, child_key in paths:
        parent = _lookup(source, parent_key)
        value = _nonnegative_int(_lookup(parent, child_key))
        if value is not None:
            return value
    return None






def _extract_text_part(item: Any) -> str:
    if isinstance(item, str):
        return item
    text = _lookup(item, "text")
    if isinstance(text, str):
        return text
    return ""


def _extract_tool_call_arguments(tool_calls: Any) -> str:
    if not isinstance(tool_calls, Sequence):
        return ""
    parts: list[str] = []
    for tool_call in tool_calls:
        function_block = _lookup(tool_call, "function")
        arguments = _lookup(function_block, "arguments")
        if isinstance(arguments, str) and arguments.strip():
            parts.append(arguments.strip())
    return "".join(parts)
