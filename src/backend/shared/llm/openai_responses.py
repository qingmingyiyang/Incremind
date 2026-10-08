"""Native OAuth Responses transport behind the existing completion gateway.

No retries or provider fallback: one gateway wire attempt creates one response.
Authentication, source authorization and result validation belong to the caller.
"""
import json
import math
import re
from dataclasses import dataclass
from typing import Callable, Mapping
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel
from .model_transport import ModelTransportError, ModelTransportTimeout, _OwnedHTTPAttempt
from .model_capabilities import ModelCapabilities


@dataclass(frozen=True)
class ProviderBackgroundOptions:
    """Explicit active-owner checkpoints and frozen, external GET budget decision."""
    checkpoint: Callable[[], None]
    resume: Callable[[Mapping[str, object]], bool]
    observe: Callable[[Mapping[str, object]], None] | None = None

    def __post_init__(self):
        if (not callable(self.checkpoint) or not callable(self.resume)
                or self.observe is not None and not callable(self.observe)):
            raise ValueError('provider_background_options_invalid')


@dataclass(frozen=True)
class ProviderResponseCursor:
    """Provider identity and cursor supplied by a separately validated owner."""
    response_id: str
    sequence_number: int

    def __post_init__(self):
        if (type(self.response_id) is not str
                or not re.fullmatch(r'resp_[A-Za-z0-9_-]{1,200}', self.response_id)
                or type(self.sequence_number) is not int
                or not 0 <= self.sequence_number <= 9_007_199_254_740_991):
            raise ValueError('provider_response_cursor_invalid')


@dataclass(frozen=True, kw_only=True)
class ProviderResumeOnlyOptions(ProviderBackgroundOptions):
    """Resume an owned response; this metadata supplies no execution authority."""
    cursor: ProviderResponseCursor

    def __post_init__(self):
        super().__post_init__()
        if type(self.cursor) is not ProviderResponseCursor:
            raise ValueError('provider_response_cursor_invalid')
        self.cursor.__post_init__()


class ResponsesBackgroundInterrupted(RuntimeError):
    """A response may already exist; never create another model response."""
    def __init__(self, kind='connection'):
        if kind not in {'connection', 'timeout', 'stalled', 'header_timeout', 'rate_limit', 'server'}:
            raise ValueError('provider_background_interruption_invalid')
        self.kind = kind
        super().__init__('provider_background_interrupted')


class ResponsesError(ValueError):
    """Safe protocol facts for the Turn's retry policy; never retains provider text."""

    def __init__(self, message, *, status_code, code=None, category="unknown", output_started=False):
        super().__init__(message)
        self.status_code, self.code, self.category = status_code, code, category
        self.output_started = output_started
        self.retryable = not output_started and category in {"capacity", "transient", "rate_limit"}


class ResponsesProviderError(ResponsesError):
    """保留原安全分类事实、永久错误码与退避头，不保存服务商正文。"""
    def __init__(self, response):
        facts = _provider_error(response.status_code, None)
        super().__init__(str(facts), status_code=facts.status_code, code=facts.code,
                         category=facts.category, output_started=facts.output_started)
        self.headers = {'retry-after': response.headers['retry-after']} if 'retry-after' in response.headers else {}
        try:
            raw = bytearray()
            for chunk in response.iter_bytes():
                if len(raw) + len(chunk) > 1_000_000:
                    return
                raw.extend(chunk)
            value = json.loads(raw)
        except (TypeError, ValueError):
            return
        facts = _provider_error(response.status_code, value)
        super().__init__(str(facts), status_code=facts.status_code, code=facts.code,
                         category=facts.category, output_started=facts.output_started)
        error = value.get('error', value) if isinstance(value, dict) else None
        code = (error.get('code') or error.get('type')) if isinstance(error, dict) else None
        if isinstance(code, str) and code in {'insufficient_quota', 'quota_exceeded', 'billing_hard_limit_reached',
                    'usage_limit_reached', 'content_filter', 'content_policy_violation', 'safety', 'refusal'}:
            self.code = code


_ERROR_CATEGORIES = {
    "subscription_sharing_usage_limit_exceeded": "usage_limit", "insufficient_quota": "usage_limit",
    "subscription_sharing_user_not_eligible": "authorization", "subscription_sharing_invalid_user": "authorization",
    "chatpass_v2_scope_not_authorized": "authorization", "chatpass_v2_invalid_authorization_context": "authorization",
    "subscription_sharing_unsupported_capability": "unsupported", "subscription_sharing_route_not_supported": "unsupported",
    "subscription_sharing_usage_unavailable": "transient", "subscription_sharing_user_unavailable": "transient",
    "server_error": "transient", "rate_limit_exceeded": "rate_limit", "content_filter": "refusal",
}


def _provider_error(status_code, body, *, output_started=False, message="subscription_response_failed"):
    value = body if isinstance(body, dict) else {}
    value = value.get("error", value)
    value = value if isinstance(value, dict) else {}
    raw_code = value.get("code")
    code = raw_code if isinstance(raw_code, str) and raw_code in _ERROR_CATEGORIES else None
    category = _ERROR_CATEGORIES.get(code, "unknown")
    if status_code in (401, 403) and category in {"transient", "rate_limit", "capacity"}:
        category = "authorization"
    if category == "unknown":
        detail = body.get("detail") if isinstance(body, dict) else None
        text = value.get("message", detail)
        if status_code in (401, 403):
            category = "authorization"
        elif isinstance(text, str) and "model is at capacity" in text.lower():
            category = "capacity"
        elif status_code == 429:
            category = "rate_limit"
        elif 500 <= status_code <= 599:
            category = "transient"
    return ResponsesError(message, status_code=status_code, code=code, category=category, output_started=output_started)


def _failure_body(response):
    # Read a bounded diagnostic only to classify it; never retain or expose it.
    body = bytearray()
    for chunk in response.iter_bytes():
        if len(body) + len(chunk) > 65_536:
            return None
        body.extend(chunk)
    try:
        return json.loads(body)
    except (ValueError, UnicodeError):
        return None


class ResponsesCompletion:
    def __init__(self, *, client=None, api_base=None, capabilities=None):
        self.client = client
        self.api_base = None
        self.capabilities = capabilities or ModelCapabilities()
        if not isinstance(self.capabilities, ModelCapabilities):
            raise ValueError('provider_background_capability_invalid')
        if api_base is not None:
            if not isinstance(api_base, str):
                raise ValueError('responses_api_base_invalid')
            url = urlsplit(api_base)
            local = url.hostname in {'localhost', '127.0.0.1', '::1'}
            if (not url.hostname or url.username or url.password or url.query or url.fragment
                    or url.scheme not in ({'https', 'http'} if local else {'https'})):
                raise ValueError('responses_api_base_invalid')
            self.api_base = api_base.rstrip('/')

    @property
    def background_resume_capable(self):
        return self.api_base is not None and self.capabilities.background_resume

    def __call__(self, **request):
        body = {"model": str(request["model"]).removeprefix("openai/"),
                "input": [{"role": "developer" if m["role"] == "system" else m["role"], "content": m["content"]}
                          for m in request["messages"]], "stream": True}
        if self.api_base is None:
            body['store'] = False
        maximum = request.get("max_completion_tokens", request.get("max_tokens"))
        if maximum is not None:
            body["max_output_tokens"] = maximum
        fmt = request.get("response_format")
        if fmt:
            if isinstance(fmt, type) and issubclass(fmt, BaseModel):
                from openai.lib._parsing._responses import type_to_text_format_param
                fmt = type_to_text_format_param(fmt)
            else:
                if not isinstance(fmt, dict):
                    raise TypeError('unsupported_response_format')
                if fmt.get("type") == "json_schema":
                    fmt = {"type": "json_schema", **fmt["json_schema"]}
            body["text"] = {"format": fmt}
        if request.get("reasoning_effort"):
            body["reasoning"] = {"effort": request["reasoning_effort"]}
        headers = {"Authorization": "Bearer " + request["api_key"]}
        timeout = request.get("timeout", 60)
        options = request.get('provider_background')
        if options is not None:
            if (not self.background_resume_capable or not isinstance(options, ProviderBackgroundOptions)
                    or not isinstance(timeout, ModelTransportTimeout)):
                raise ModelTransportError('provider_rejected')
            body.update(store=True, background=True)
            stream = self._background_stream(body, headers, timeout, options)
        else:
            stream = self._stream(body, headers, timeout)
        if request.get("stream"):
            return stream
        text, usage = [], {}
        for chunk in stream:
            text.append(chunk["choices"][0]["delta"].get("content", ""))
            usage = chunk.get("usage", usage)
        return {"choices": [{"message": {"content": "".join(text)}, "finish_reason": "stop"}], "usage": usage}

    def _stream(self, body, headers, timeout):
        response, output_started = None, False
        attempt = None
        if isinstance(timeout, ModelTransportTimeout):
            if self.client is not None and timeout.owned_client_factory is None:
                raise ModelTransportError('provider_rejected')
            client = (timeout.owned_client_factory() if timeout.owned_client_factory is not None
                else httpx.Client(trust_env=False, follow_redirects=False))
            if not isinstance(client, httpx.Client) or client.is_closed or client is self.client:
                raise ModelTransportError('provider_rejected')
            attempt = _OwnedHTTPAttempt(client, timeout)
        else:
            client = self.client or httpx.Client(trust_env=False, follow_redirects=False)
        accumulated = ""
        try:
            with client.stream("POST", (self.api_base or 'https://api.openai.com/v1') + '/responses', json=body, headers=headers,
                               timeout=timeout, follow_redirects=False) as response:
                if isinstance(timeout, ModelTransportTimeout):
                    timeout.bind_response(response)
                if response.status_code != 200:
                    if isinstance(timeout, ModelTransportTimeout):
                        raise ResponsesProviderError(response)
                    raise _provider_error(response.status_code, _failure_body(response))
                content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if content_type and content_type != "text/event-stream":
                    raise ValueError("subscription_response_invalid_stream")
                for event in _events(response):
                    kind = event.get("type")
                    if kind == "response.output_text.delta":
                        delta = event.get("delta")
                        if not isinstance(delta, str):
                            raise ValueError("subscription_response_invalid")
                        output_started = output_started or bool(delta)
                        accumulated = _append_response_text(accumulated, delta)
                        yield {"choices": [{"delta": {"content": delta}, "finish_reason": None}]}
                    elif kind == "response.completed":
                        final = event.get("response")
                        text = _text(final)
                        if text != accumulated:
                            raise ValueError("subscription_response_stream_mismatch")
                        yield {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": _usage(final)}
                        return
                    elif kind in {"error", "response.failed"}:
                        failure = event.get("response") if kind == "response.failed" else event
                        raise _provider_error(200, failure, output_started=output_started,
                                              message="subscription_response_incomplete")
                    elif (kind in {"response.function_call_arguments.delta", "response.custom_tool_call_input.delta"}
                          or kind == "response.output_item.added" and isinstance(event.get("item"), dict) and event["item"].get("type")
                          not in {None, "message", "reasoning"}):
                        output_started = True
                    elif kind in {"response.incomplete", "response.refusal.delta"}:
                        if kind == 'response.refusal.delta' and isinstance(timeout, ModelTransportTimeout):
                            raise ModelTransportError('content_filter')
                        raise ValueError("subscription_response_incomplete")
                if isinstance(timeout, ModelTransportTimeout) and not accumulated:
                    raise ConnectionError('subscription_response_ended_before_output')
                raise ValueError("subscription_response_incomplete")
        except (ResponsesError, ResponsesProviderError):
            raise
        except (httpx.ReadTimeout, httpx.ReadError, httpx.RemoteProtocolError, ModelTransportError, json.JSONDecodeError) as error:
            if attempt is not None:
                attempt.check()
            if (accumulated or output_started) and isinstance(timeout, ModelTransportTimeout):
                # 保留正文或工具输出阶段，逻辑非流式调用也不能重发已生成内容。
                raise ResponsesError('subscription_response_incomplete',
                    status_code=response.status_code if response is not None else None,
                    category='incomplete', output_started=output_started) from None
            if isinstance(timeout, ModelTransportTimeout):
                raise timeout.header_error(error) from None
            if isinstance(error, httpx.RequestError):
                raise ResponsesError("subscription_response_failed", status_code=response.status_code if response is not None else None,
                                     category="transient", output_started=output_started) from None
            if isinstance(error, json.JSONDecodeError):
                raise ResponsesError("subscription_response_invalid", status_code=response.status_code if response is not None else None,
                                     category="invalid", output_started=output_started) from None
            raise
        except httpx.RequestError as error:
            if isinstance(error, httpx.ReadTimeout) and isinstance(timeout, ModelTransportTimeout):
                raise timeout.header_error(error) from None
            raise ResponsesError("subscription_response_failed", status_code=response.status_code if response is not None else None,
                                 category="transient", output_started=output_started) from None
        except ValueError as error:
            # JSON consumers still use the native stream, so its output phase
            # must survive EOF/parser failures before text reaches the caller.
            message = str(error)
            if not message.startswith("subscription_response_"):
                message = "subscription_response_invalid"
            raise ResponsesError(message, status_code=response.status_code if response is not None else None,
                                 category="incomplete" if message == "subscription_response_incomplete" else "invalid",
                                 output_started=output_started) from None
        finally:
            if attempt is not None:
                attempt.close()
            elif self.client is None:
                try:
                    client.close()
                except Exception:
                    if isinstance(timeout, ModelTransportTimeout):
                        raise ModelTransportError('provider_close_failed') from None
                    raise

    def _background_stream(self, body, headers, timeout, options):
        identity, sequence, last_event, accumulated = None, None, None, ''
        resume_only = isinstance(options, ProviderResumeOnlyOptions)
        if resume_only:
            options.__post_init__()
            identity, sequence = options.cursor.response_id, options.cursor.sequence_number
        clients = []
        while True:
            options.checkpoint()
            if self.client is not None and timeout.owned_client_factory is None:
                raise ModelTransportError('provider_rejected')
            client, owned = None, False
            try:
                client = (timeout.owned_client_factory() if timeout.owned_client_factory is not None
                    else httpx.Client(trust_env=False, follow_redirects=False))
                if (not isinstance(client, httpx.Client) or client.is_closed or client is self.client
                        or any(client is previous for previous in clients)):
                    raise ModelTransportError('provider_rejected')
                owned = True
                clients.append(client)
                attempt = _OwnedHTTPAttempt(client, timeout)
            except Exception:
                if owned:
                    try:
                        client.close()
                    except Exception:
                        raise ModelTransportError('provider_close_failed') from None
                if identity is not None:
                    raise ResponsesBackgroundInterrupted('connection') from None
                raise
            failure = None
            checkpoint_failed = False
            try:
                method = 'POST' if identity is None else 'GET'
                address = self.api_base + '/responses'
                if identity is not None:
                    address += '/' + identity + '?stream=true&starting_after=' + str(sequence)
                with client.stream(method, address, headers=headers, timeout=timeout,
                        follow_redirects=False, **({'json': body} if method == 'POST' else {})) as response:
                    attempt.check()
                    if response.status_code != 200:
                        raise ResponsesProviderError(response)
                    content_type = response.headers.get('content-type', '').split(';', 1)[0].strip().lower()
                    if content_type != 'text/event-stream':
                        raise ModelTransportError('provider_rejected')
                    for event in _events(response):
                        checkpoint_failed = True
                        options.checkpoint()
                        checkpoint_failed = False
                        number = event.get('sequence_number')
                        if type(number) is not int or not 0 <= number <= 9_007_199_254_740_991:
                            raise ModelTransportError('provider_rejected')
                        value = event.get('response')
                        returned = value.get('id') if isinstance(value, dict) else None
                        if identity is None:
                            if (event.get('type') != 'response.created' or number != 0
                                    or not isinstance(returned, str)
                                    or not re.fullmatch(r'resp_[A-Za-z0-9_-]{1,200}', returned)):
                                raise ModelTransportError('provider_rejected')
                            identity = returned
                        elif returned is not None and returned != identity:
                            raise ModelTransportError('provider_rejected')
                        if sequence is not None and number <= sequence:
                            if number == sequence and event == last_event:
                                continue
                            raise ModelTransportError('provider_rejected')
                        if sequence is not None and number != sequence + 1:
                            raise ModelTransportError('provider_rejected')
                        kind = event.get('type')
                        if kind == 'response.output_text.delta':
                            delta = event.get('delta')
                            if not isinstance(delta, str):
                                raise ModelTransportError('provider_rejected')
                            try:
                                accumulated = _append_response_text(accumulated, delta)
                            except ValueError:
                                raise ModelTransportError('provider_rejected') from None
                            sequence, last_event = number, event
                            _observe_response_checkpoint(options, identity, number)
                            if not resume_only:
                                yield {'choices': [{'delta': {'content': delta}, 'finish_reason': None}]}
                        elif kind == 'response.completed':
                            text = _text(value)
                            if returned != identity or (not text.endswith(accumulated) if resume_only else text != accumulated):
                                raise ModelTransportError('provider_rejected')
                            if resume_only:
                                text = _append_response_text('', text)
                            usage = _usage(value)
                            _observe_response_checkpoint(options, identity, number)
                            if resume_only:
                                # A new parser has no prior raw JSON. Only the
                                # provider's complete output can rebuild it.
                                yield {'choices': [{'delta': {'content': text}, 'finish_reason': None}]}
                            yield {'choices': [{'delta': {}, 'finish_reason': 'stop'}], 'usage': usage}
                            return
                        elif kind in {'error', 'response.failed', 'response.incomplete', 'response.refusal.delta'}:
                            raise ModelTransportError('content_filter' if kind == 'response.refusal.delta' else 'provider_rejected')
                        else:
                            sequence, last_event = number, event
                            _observe_response_checkpoint(options, identity, number)
                    failure = 'connection'
            except (httpx.TransportError, ConnectionError, TimeoutError, ModelTransportError, ResponsesProviderError) as error:
                if checkpoint_failed:
                    raise
                if isinstance(error, ModelTransportError):
                    if error.kind not in {'connection', 'timeout', 'stalled', 'header_timeout'}:
                        raise
                    failure = error.kind
                elif isinstance(error, ResponsesProviderError):
                    if error.status_code not in {429, 500, 502, 503, 504} or error.code:
                        raise ModelTransportError('provider_rejected') from None
                    failure = 'rate_limit' if error.status_code == 429 else 'server'
                else:
                    failure = 'timeout' if isinstance(error, (httpx.TimeoutException, TimeoutError)) else 'connection'
            finally:
                # Closing the previous response/socket and joining its timers is
                # mandatory even for cancel, malformed events or checkpoint failure.
                attempt.close()
            options.checkpoint()
            try:
                remaining = timeout.remaining()
            except TimeoutError:
                raise ResponsesBackgroundInterrupted('timeout') from None
            if (identity is None or sequence is None or not math.isfinite(remaining) or remaining <= 0):
                raise ResponsesBackgroundInterrupted(failure)
            try:
                permitted = options.resume({'kind': failure, 'sequence': sequence, 'remaining': remaining})
            except Exception:
                raise ResponsesBackgroundInterrupted(failure) from None
            if type(permitted) is not bool or not permitted:
                raise ResponsesBackgroundInterrupted(failure)


def _observe_response_checkpoint(options, identity, sequence):
    if options.observe is not None:
        try:
            options.observe({'response_id': identity, 'sequence_number': sequence})
        except Exception:
            # Creation is already accepted. An owner/storage failure must not
            # be reclassified as a transport retry that creates another POST.
            raise ResponsesBackgroundInterrupted('connection') from None


def _append_response_text(current, delta):
    value = current + delta
    if len(value) > 8_000_000:
        raise ValueError('subscription_response_too_large')
    return value


def _text(value):
    if not isinstance(value, dict) or value.get("status") != "completed":
        raise ValueError("subscription_response_incomplete")
    output = value.get("output")
    if not isinstance(output, list):
        raise ValueError("subscription_response_invalid")
    pieces = []
    for item in output:
        if item.get("type") == "reasoning":
            continue
        if item.get("type") != "message" or not isinstance(item.get("content"), list):
            raise ValueError("subscription_response_unsupported_output")
        for part in item["content"]:
            if part.get("type") != "output_text" or not isinstance(part.get("text"), str):
                raise ValueError("subscription_response_invalid")
            pieces.append(part["text"])
    if not pieces:
        raise ValueError("subscription_response_empty")
    return "".join(pieces)


def _usage(value):
    usage = value.get("usage") or {}
    return {"prompt_tokens": usage.get("input_tokens", 0), "completion_tokens": usage.get("output_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
            "prompt_tokens_details": {"cached_tokens": (usage.get("input_tokens_details") or {}).get("cached_tokens", 0)}}


def _events(response):
    data, size = [], 0
    for line in response.iter_lines():
        if len(line) > 1_000_000:
            raise ValueError("subscription_response_event_too_large")
        if not line:
            if data:
                value = json.loads("\n".join(data))
                if not isinstance(value, dict):
                    raise ValueError("subscription_response_invalid")
                yield value
            data, size = [], 0
        elif line.startswith("data:"):
            field = line[5:].removeprefix(" ")
            size += len(field)
            if size > 1_000_000:
                raise ValueError("subscription_response_event_too_large")
            data.append(field)
