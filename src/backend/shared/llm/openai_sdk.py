import asyncio
import math
from time import monotonic

import httpx
import litellm
from litellm.llms.custom_httpx.http_handler import get_ssl_configuration
from openai import APIConnectionError, APIError, APIStatusError, APITimeoutError, AsyncOpenAI, OpenAI

from .model_transport import ModelTransportError, ModelTransportTimeout, _OwnedHTTPAttempt
from .openai_responses import (
    ResponsesCompletion, ResponsesError, ResponsesProviderError,
    _append_response_text, _provider_error, _text, _usage,
)


class ResponsesIncompleteError(ResponsesError):
    def __init__(self, response, *, output_started):
        if not isinstance(response, dict) or response.get('status') != 'incomplete':
            raise ValueError('subscription_response_invalid')
        details = response.get('incomplete_details')
        reason = details.get('reason') if isinstance(details, dict) else None
        code = reason if isinstance(reason, str) and reason in {'max_output_tokens', 'content_filter'} else None
        super().__init__('subscription_response_incomplete', status_code=200, code=code,
            category='refusal' if code == 'content_filter' else 'incomplete', output_started=output_started)
        # 只保留服务明确报告的计数，正文和未知原因不进入错误元数据。
        self.usage = _usage(response)


class ResponseEventProjection:
    def __init__(self):
        self.text = ''
        self.output_started = False
        self.finished = False

    def accept(self, event):
        kind = event.get('type')
        if self.finished:
            raise ValueError('subscription_response_after_completion')
        if kind == 'response.output_text.delta':
            delta = event.get('delta')
            if not isinstance(delta, str):
                raise ValueError('subscription_response_invalid')
            self.output_started = self.output_started or bool(delta)
            self.text = _append_response_text(self.text, delta)
            return [{'choices': [{'delta': {'content': delta}, 'finish_reason': None}]}]
        if kind == 'response.completed':
            final = event.get('response')
            if _text(final) != self.text:
                raise ValueError('subscription_response_stream_mismatch')
            self.finished = True
            return [{'choices': [{'delta': {}, 'finish_reason': 'stop'}], 'usage': _usage(final)}]
        if kind in {'error', 'response.failed'}:
            raise _provider_error(200, event.get('response') if kind == 'response.failed' else event,
                output_started=self.output_started, message='subscription_response_incomplete')
        if (kind in {'response.function_call_arguments.delta', 'response.custom_tool_call_input.delta'}
                or kind == 'response.output_item.added' and isinstance(event.get('item'), dict)
                and event['item'].get('type') not in {None, 'message', 'reasoning'}):
            self.output_started = True
        if kind == 'response.incomplete':
            raise ResponsesIncompleteError(event.get('response'), output_started=self.output_started)
        if kind == 'response.refusal.delta':
            raise ResponsesError('subscription_response_incomplete', status_code=200,
                category='refusal', output_started=self.output_started)
        return []

    def require_finished(self):
        if not self.finished:
            raise ResponsesError('subscription_response_incomplete', status_code=200,
                category='incomplete' if self.output_started else 'transient',
                output_started=self.output_started)


class OpenAIResponsesCompletion(ResponsesCompletion):
    def prepare_body(self, request):
        body = super().prepare_body(request)
        body['store'] = False
        for message in body['input']:
            if isinstance(message['content'], list):
                parts = []
                for part in message['content']:
                    if part.get('type') == 'text':
                        parts.append({'type': 'input_text', 'text': part['text']})
                    elif part.get('type') == 'image_url' and isinstance(part.get('image_url'), dict):
                        image = part['image_url']
                        parts.append({'type': 'input_image', 'image_url': image['url'],
                            **({'detail': image['detail']} if 'detail' in image else {})})
                    else:
                        raise ValueError('responses_input_part_unsupported')
                message['content'] = parts
        if request.get('reasoning_effort') == 'none' and 'temperature' in request:
            body['temperature'] = request['temperature']
        return body

    @staticmethod
    def _credential(headers):
        credential = headers['Authorization'].removeprefix('Bearer ')
        if not credential.strip():
            raise ValueError('responses_api_key_required')
        return credential

    def _stream(self, body, headers, timeout):
        credential = self._credential(headers)
        if not isinstance(timeout, ModelTransportTimeout):
            if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                    or not math.isfinite(timeout) or timeout <= 0):
                raise ValueError('responses_timeout_invalid')
            duration = float(timeout)
            deadline = monotonic() + duration
            timeout = ModelTransportTimeout(remaining=lambda: _remaining(deadline),
                limits={'header_timeout': duration, 'idle_timeout': duration})
        attempt = None
        if (self.client is not None or litellm.client_session is not None) and timeout.owned_client_factory is None:
            raise ModelTransportError('provider_rejected')
        client = (timeout.owned_client_factory() if timeout.owned_client_factory is not None
            else httpx.Client(verify=get_ssl_configuration(), follow_redirects=False))
        if (not isinstance(client, httpx.Client) or client.is_closed or client is self.client
                or client is litellm.client_session):
            raise ModelTransportError('provider_rejected')
        attempt = _OwnedHTTPAttempt(client, timeout)
        projection = ResponseEventProjection()
        try:
            sdk = OpenAI(api_key=credential, base_url=self.api_base or 'https://api.openai.com/v1',
                max_retries=0, http_client=client, timeout=float(timeout))
            with sdk.responses.with_streaming_response.create(**body) as response:
                _require_event_stream(response.http_response)
                for event in response.parse():
                    for chunk in projection.accept(event.model_dump(exclude_unset=True)):
                        yield chunk
                    if projection.finished:
                        return
                projection.require_finished()
        except APIStatusError as error:
            if attempt is not None:
                attempt.check()
            raise ResponsesProviderError(error.response) from None
        except APITimeoutError:
            attempt.check()
            raise ModelTransportError('header_timeout') from None
        except (APIConnectionError, httpx.RequestError):
            if attempt is not None:
                attempt.check()
            raise ResponsesError('subscription_response_failed', status_code=None,
                category='incomplete' if projection.output_started else 'transient',
                output_started=projection.output_started) from None
        except APIError as error:
            raise _sdk_stream_error(error, projection) from None
        except ResponsesError:
            raise
        except ValueError:
            raise ResponsesError('subscription_response_invalid', status_code=None,
                category='invalid', output_started=projection.output_started) from None
        finally:
            if attempt is not None:
                attempt.close()
                if attempt.failure == 'steer':
                    attempt.check()

    async def acomplete(self, **request):
        body = self.prepare_body(request)
        credential = self._credential({'Authorization': 'Bearer ' + request['api_key']})
        timeout = request.get('timeout', 60)
        if (isinstance(timeout, ModelTransportTimeout) or isinstance(timeout, bool)
                or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0):
            raise ValueError('responses_async_timeout_invalid')
        stream = self._astream(body, credential, float(timeout))
        if request.get('stream'):
            return stream
        text, usage = [], {}
        async for chunk in stream:
            text.append(chunk['choices'][0]['delta'].get('content', ''))
            usage = chunk.get('usage', usage)
        return {'choices': [{'message': {'content': ''.join(text)}, 'finish_reason': 'stop'}], 'usage': usage}

    async def _astream(self, body, credential, timeout):
        projection = ResponseEventProjection()
        try:
            # 总期限覆盖响应头和全部正文；取消由原生异步退出关闭独占连接。
            async with asyncio.timeout(timeout):
                if litellm.aclient_session is not None:
                    raise ModelTransportError('provider_rejected')
                async with httpx.AsyncClient(verify=get_ssl_configuration(), follow_redirects=False) as client:
                    sdk = AsyncOpenAI(api_key=credential, base_url=self.api_base or 'https://api.openai.com/v1',
                        max_retries=0, http_client=client, timeout=timeout)
                    async with sdk.responses.with_streaming_response.create(**body) as response:
                        _require_event_stream(response.http_response)
                        async for event in await response.parse():
                            for chunk in projection.accept(event.model_dump(exclude_unset=True)):
                                yield chunk
                            if projection.finished:
                                return
                        projection.require_finished()
        except APIStatusError as error:
            facts = _provider_error(error.status_code, error.body, output_started=projection.output_started)
            if 'retry-after' in error.response.headers:
                facts.headers = {'retry-after': error.response.headers['retry-after']}
            raise facts from None
        except APITimeoutError:
            raise TimeoutError('model request timed out') from None
        except (APIConnectionError, httpx.RequestError):
            raise ResponsesError('subscription_response_failed', status_code=None,
                category='incomplete' if projection.output_started else 'transient',
                output_started=projection.output_started) from None
        except APIError as error:
            raise _sdk_stream_error(error, projection) from None
        except ResponsesError:
            raise
        except ValueError:
            raise ResponsesError('subscription_response_invalid', status_code=None,
                category='invalid', output_started=projection.output_started) from None


def _require_event_stream(response):
    if response.headers.get('content-type', '').split(';', 1)[0].strip().lower() != 'text/event-stream':
        raise ModelTransportError('provider_rejected')


def _sdk_stream_error(error, projection):
    return _provider_error(200, error.body, output_started=projection.output_started,
        message='subscription_response_incomplete')


def _remaining(deadline):
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise TimeoutError('model request deadline exceeded')
    return remaining
