"""桌面代理编排；原始转发、交接资格和录制各由既有 owner 承担。"""
import ipaddress
import json
import logging
import sys
from types import SimpleNamespace

import httpx
import anyio
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from ..kernel.ai_runtime import get_or_build_ai_runtime
from .external_agent_settings import external_agent_settings
from .external_proxy_handoff import ProxyHandoff
from .external_proxy_protocol import insert_context, last_user_text
from .external_proxy_recording import ConversationRecorder, _json
from .external_proxy_settings import external_proxy_settings
from .external_proxy_transport import ProxyTransport, ProxyTransportError
from .route import resolve_scope_project


_LOG = logging.getLogger(__name__)


class _ProxyResponse(StreamingResponse):
    """响应头发送失败也归同一 owner 清理，不依赖正文迭代器开始。"""
    def __init__(self, content, *, close, **kwargs):
        super().__init__(content, **kwargs)
        self._close = close

    async def __call__(self, scope, receive, send):
        error = (None, None, None)
        try:
            await super().__call__(scope, receive, send)
        except BaseException:
            error = sys.exc_info()
            raise
        finally:
            with anyio.CancelScope(shield=True):
                try:
                    await self.body_iterator.aclose()
                finally:
                    await self._close(error)


def _credentials(headers):
    values = []
    authorization = headers.get('authorization', '')
    scheme, _, value = authorization.partition(' ')
    if authorization:
        values.append(authorization)
    if value:
        values.append(value)
    if headers.get('x-api-key'):
        values.append(headers['x-api-key'])
    return tuple(values)


def install_external_proxy_routes(application, *, runtime_root, records, workspace):
    router = APIRouter(prefix='/api/v2/external-agent/proxy')

    def prepare(client, text, credentials):
        try:
            setting = external_agent_settings(records)
            if not setting['allow_remote'] or not setting['clients'][client]:
                return None
            runtime = get_or_build_ai_runtime(SimpleNamespace(app=application), SimpleNamespace(root_dir=runtime_root))
            context = application.state.external_context
            return ProxyHandoff(workspace.query, context, runtime=runtime,
                runner=application.state.ai_turn_runner).prepare(client, text, credentials=credentials)
        except Exception:
            _LOG.warning('external_proxy_injection_unavailable')
            return None

    async def proxy(request: Request, client: str, protocol: str):
        layout = getattr(application.state, 'deployment', None)
        if layout is not None and layout.mode != 'desktop':
            raise HTTPException(503, 'external_proxy_user_domains_unavailable')
        try:
            local = request.client is not None and ipaddress.ip_address(request.client.host).is_loopback
        except ValueError:
            local = False
        if not local:
            raise HTTPException(403, 'external_proxy_loopback_required')
        if client not in {'claude', 'codex'}:
            raise HTTPException(404, 'external_proxy_client_unknown')
        raw = await request.body()
        credentials = _credentials(request.headers)
        payload, text, project = None, None, None
        try:
            payload = _json(raw.decode('utf-8'))
            text = last_user_text(protocol, payload)
            if text:
                project = resolve_scope_project(records, text, 'default')
        except Exception:
            pass
        prepared = await run_in_threadpool(prepare, client, text, credentials) if text else None
        def before_send():
            if prepared is not None:
                try:
                    prepared.validate()
                    return json.dumps(insert_context(protocol, payload, prepared.text),
                        ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
                except Exception:
                    _LOG.warning('external_proxy_injection_unavailable')
            return raw

        recorder = None
        if project is not None:
            try:
                if external_proxy_settings(records)['record_conversations'][client]:
                    recorder = ConversationRecorder(protocol, payload, credentials=credentials)
            except Exception:
                _LOG.warning('external_proxy_recording_unavailable')
        transport = getattr(application.state, 'external_proxy_transport', None)
        http_client = None
        if transport is None:
            http_client = httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(None, connect=10.0))
            transport = ProxyTransport(http_client)
        manager = transport.open(protocol, raw, request.headers, before_send=before_send)
        try:
            relay = await manager.__aenter__()
        except BaseException as error:
            if recorder:
                recorder.close()
            if http_client:
                with anyio.CancelScope(shield=True):
                    try:
                        await http_client.aclose()
                    except httpx.HTTPError:
                        _LOG.warning('external_proxy_cleanup_failed')
            if isinstance(error, ProxyTransportError):
                raise HTTPException(502, 'external_proxy_upstream_failed') from None
            raise

        complete, closed = False, False
        stream_error = (None, None, None)

        async def stream():
            nonlocal complete, stream_error
            try:
                async for chunk in relay.iter_bytes():
                    if recorder:
                        recorder.feed(chunk)
                    yield chunk
                complete = True
            except BaseException:
                stream_error = sys.exc_info()
                raise

        async def close(error):
            nonlocal closed
            if closed:
                return
            closed = True
            primary = error if error[0] is not None else stream_error
            try:
                with anyio.CancelScope(shield=True):
                    await manager.__aexit__(*primary)
                if complete and primary[0] is None and recorder:
                    try:
                        recorded = recorder.finish(status_code=relay.status_code,
                            content_type=relay.headers.get('content-type', ''),
                            content_encoding=relay.headers.get('content-encoding', ''))
                        if (recorded and external_proxy_settings(records)['record_conversations'][client]
                                and resolve_scope_project(records, text, 'default') == project):
                            await workspace.intake.add_text({'project_id':project,
                                'text':recorded, 'title':client+' 对话'})
                    except Exception:
                        _LOG.warning('external_proxy_recording_unavailable')
            finally:
                if recorder:
                    recorder.close()
                if http_client:
                    with anyio.CancelScope(shield=True):
                        try:
                            await http_client.aclose()
                        except httpx.HTTPError:
                            if primary[0] is None:
                                raise ProxyTransportError('external_proxy_close_failed') from None

        return _ProxyResponse(stream(), close=close, status_code=relay.status_code, headers=relay.headers)

    @router.post('/{client}/v1/chat/completions')
    async def chat(request: Request, client: str):
        return await proxy(request, client, 'chat_completions')

    @router.post('/{client}/v1/messages')
    async def messages(request: Request, client: str):
        return await proxy(request, client, 'messages')

    @router.post('/{client}/v1/responses')
    async def responses(request: Request, client: str):
        return await proxy(request, client, 'responses')

    application.include_router(router)
