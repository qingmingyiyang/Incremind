"""受控上游的原始 HTTP 转发；不保存凭据、请求、交付或对话。"""
from collections.abc import Mapping
from contextlib import asynccontextmanager
import ipaddress
from types import MappingProxyType

import anyio
import httpx


_ENDPOINTS = {
    'chat_completions': 'https://api.openai.com/v1/chat/completions',
    'responses': 'https://api.openai.com/v1/responses',
    'messages': 'https://api.anthropic.com/v1/messages',
}
_REQUEST_HEADERS = frozenset({'authorization', 'x-api-key', 'anthropic-version',
    'anthropic-beta', 'openai-organization', 'openai-project', 'accept'})
_RESPONSE_HEADERS = frozenset({'content-type', 'content-encoding', 'cache-control',
    'retry-after'})


class ProxyTransportError(ValueError):
    """固定错误码；不投影客户端凭据或 HTTPX 原错误。"""


def _endpoint(value):
    try:
        url = httpx.URL(value)
        if (not isinstance(value, str) or not url.host or url.userinfo
                or url.query or url.fragment or url.scheme not in {'http', 'https'}):
            raise ValueError
        if url.scheme == 'http' and not ipaddress.ip_address(url.host).is_loopback:
            raise ValueError
        return url
    except (TypeError, ValueError, httpx.InvalidURL):
        raise ProxyTransportError('external_proxy_endpoint_invalid') from None


def _headers(values):
    # 不复制 request.headers 全集，设备身份、cookies 和路由头留在本机。
    try:
        rows = values.items() if isinstance(values, Mapping) else values
        result = {}
        for name, value in rows:
            if not isinstance(name, str) or not isinstance(value, str):
                raise ValueError
            key = name.lower()
            if key not in _REQUEST_HEADERS:
                continue
            if (key in result or not value.isascii()
                    or any(ord(char) < 32 or ord(char) == 127 for char in value)):
                raise ValueError
            result[key] = value
        result['content-type'] = 'application/json'
        result['accept-encoding'] = 'identity'
        return result
    except (TypeError, ValueError):
        raise ProxyTransportError('external_proxy_headers_invalid') from None


class _Relay:
    def __init__(self, response):
        self.status_code = response.status_code
        self.headers = {key: value for key, value in response.headers.items()
                        if key in _RESPONSE_HEADERS}
        self._response = response

    async def iter_bytes(self):
        """保持 SSE、JSON、错误和压缩字节；记录由独立调用方旁观。"""
        try:
            async for chunk in self._response.aiter_raw():
                yield chunk
        except httpx.HTTPError:
            raise ProxyTransportError('external_proxy_stream_failed') from None


class ProxyTransport:
    def __init__(self, client, *, endpoints=None):
        if not isinstance(client, httpx.AsyncClient):
            raise TypeError('external_proxy_client_invalid')
        if endpoints is not None and (not isinstance(endpoints, Mapping)
                or not endpoints or not set(endpoints) <= set(_ENDPOINTS)):
            raise ProxyTransportError('external_proxy_endpoint_invalid')
        self._endpoints = MappingProxyType({key: _endpoint(value)
            for key, value in {**_ENDPOINTS, **(endpoints or {})}.items()})
        self._client = client

    @asynccontextmanager
    async def open(self, protocol, body, headers, *, before_send=None):
        """只有部署方能定上游；禁止重定向、自动重试及客户端默认认证。"""
        if not isinstance(protocol, str) or protocol not in self._endpoints:
            raise ProxyTransportError('external_proxy_protocol_invalid')
        if not isinstance(body, bytes):
            raise ProxyTransportError('external_proxy_body_invalid')
        request_headers = _headers(headers)
        if before_send is not None:
            try:
                body = before_send()
            except Exception:
                raise ProxyTransportError('external_proxy_before_send_failed') from None
            if not isinstance(body, bytes):
                raise ProxyTransportError('external_proxy_body_invalid')
        # 独立 Request 不继承共享 client 的认证、cookies 或默认私密头。
        request = httpx.Request('POST', self._endpoints[protocol],
            content=body, headers=request_headers)
        try:
            response = await self._client.send(request, stream=True,
                follow_redirects=False, auth=None)
        except httpx.HTTPError:
            raise ProxyTransportError('external_proxy_upstream_failed') from None
        primary_failed = False
        try:
            yield _Relay(response)
        except BaseException:
            primary_failed = True
            raise
        finally:
            # ASGI 断流的取消作用域不能跳过上游释放。
            try:
                with anyio.CancelScope(shield=True):
                    await response.aclose()
            except httpx.HTTPError:
                # 清理异常不覆盖原取消或流失败，也不泄露上游诊断正文。
                if not primary_failed:
                    raise ProxyTransportError('external_proxy_close_failed') from None
