from contextlib import asynccontextmanager
from copy import deepcopy
import ipaddress
import secrets
import socket
import sys
import threading
import time
from uuid import uuid4

import anyio
from fastapi import FastAPI, HTTPException, Request
import httpx
from httpx_sse import EventSource
import uvicorn

from .external_proxy_recording import _json
from .external_proxy_transport import ProxyTransport
from .server_models import validate_server_model_binding


class SharedExternalGateway:
    def __init__(self, *, access, balances, actor, user_root, selection, validate, task_id):
        binding = validate_server_model_binding(selection['server_model_binding'])
        if binding['mode'] != 'shared' or binding['user_id'] != access.user_id:
            raise ValueError('external_shared_binding_invalid')
        self._access, self._balances = access, balances
        self._actor, self._root = actor, user_root
        self._task_id = task_id
        self._selection = deepcopy(selection)
        self._validate = validate
        self._token = secrets.token_urlsafe(32)
        self._closed = False
        self._expires = time.monotonic() + 1320
        self._server = self._thread = self._socket = None
        self.base_url = None
        self._ready = threading.Event()

    def __repr__(self):
        return 'SharedExternalGateway()'

    @property
    def credential(self):
        self.validate()
        return self._token

    def validate(self):
        if self._closed or time.monotonic() >= self._expires:
            raise ValueError('external_shared_lease_expired')
        self._validate()
        self._access.validate_binding(self._selection['server_model_binding'])

    def start(self):
        if self._server is not None:
            raise ValueError('external_shared_lease_started')
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(('127.0.0.1', 0))
        listener.listen(16)
        self._socket = listener
        self.base_url = 'http://127.0.0.1:' + str(listener.getsockname()[1]) + '/v1'

        @asynccontextmanager
        async def lifespan(application):
            self._ready.set()
            yield

        application = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
        application.add_api_route('/v1/chat/completions', self._request, methods=['POST'])
        self._server = uvicorn.Server(uvicorn.Config(application, access_log=False,
            log_config=None, log_level='critical', timeout_graceful_shutdown=1))
        self._thread = threading.Thread(target=self._server.run,
            kwargs={'sockets': [listener]}, name='external-shared-gateway', daemon=True)
        self._thread.start()
        if not self._ready.wait(5) or not self._thread.is_alive():
            self.close()
            raise ValueError('external_shared_gateway_unavailable')
        return self

    def revoke(self):
        self._closed = True
        self._token = ''

    @property
    def cleanup_complete(self):
        return self._socket is None and (self._thread is None or not self._thread.is_alive())

    def close(self):
        self.revoke()
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(2)
            if self._thread.is_alive():
                raise ValueError('external_shared_gateway_cleanup_incomplete')
        if self._socket is not None:
            self._socket.close()
            self._socket = None

    async def _request(self, request: Request):
        if (request.client is None or not ipaddress.ip_address(request.client.host).is_loopback
                or len(request.headers.getlist('authorization')) != 1
                or not secrets.compare_digest(request.headers['authorization'].encode('utf-8'),
                    ('Bearer ' + self._token).encode('utf-8'))
                or not self._token):
            raise HTTPException(403, 'external_shared_lease_invalid')
        raw = bytearray()
        async for part in request.stream():
            raw.extend(part)
            if len(raw) > 2 * 1024 * 1024:
                raise HTTPException(413, 'external_shared_request_too_large')
        attempt = 'external-' + uuid4().hex
        reserved, entered, completed, closed = False, False, False, False
        client = manager = relay = None
        observed = bytearray()

        async def stream():
            nonlocal completed, observed
            async for chunk in relay.iter_bytes():
                if observed is not None:
                    if len(observed) + len(chunk) > 8 * 1024 * 1024:
                        observed = None
                    else:
                        observed.extend(chunk)
                yield chunk
            completed = True

        async def close(error):
            nonlocal closed
            if closed:
                return
            closed = True
            failure = None
            with anyio.CancelScope(shield=True):
                # 每个已创建资源均尝试清理，运输清理失败仍必须核算原预留。
                try:
                    if entered:
                        await manager.__aexit__(*error)
                except BaseException as caught:
                    failure = caught
                try:
                    if client is not None:
                        await client.aclose()
                except BaseException as caught:
                    if failure is None:
                        failure = caught
                if reserved:
                    try:
                        usage = (self._usage(observed, relay.headers)
                            if completed and relay.status_code == 200 else None)
                        if usage is None:
                            reason = 'usage_unavailable' if entered else 'transport_failed'
                            self._balances.mark_unknown(attempt, reason_code=reason)
                        else:
                            self._balances.settle(attempt, usage=usage['usage'],
                                cache_observation=usage['cache_observation'], terminal_status='succeeded')
                    except BaseException as caught:
                        if failure is None:
                            failure = caught
                        try:
                            self._balances.mark_unknown(attempt, reason_code='settlement_unavailable')
                        except BaseException:
                            # 仍保留首个真实错误，不能把未核算预留描述为成功。
                            pass
            if failure is not None:
                raise failure

        try:
            payload = _json(raw.decode('utf-8'))
            config = self._selection['configuration']
            binding = self._selection['server_model_binding']
            if (not isinstance(payload, dict) or payload.get('model') != config['model']
                    or not isinstance(payload.get('messages'), list)
                    or 'max_completion_tokens' in payload and 'max_tokens' in payload):
                raise ValueError('external_shared_model_invalid')
            self.validate()
            self._reserve(payload, attempt)
            reserved = True
            credential = self._access.credential(binding)
            # 每次物理发送使用冻结授权；外部进程只能持有本机短凭据。
            upstream = config['base_url'].rstrip('/') + '/chat/completions'
            client = httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(120, connect=10))
            transport = ProxyTransport(client, endpoints={'chat_completions': upstream})
            manager = transport.open('chat_completions', bytes(raw),
                {'authorization': 'Bearer ' + credential.value}, before_send=self._before_send(bytes(raw)))
            relay = await manager.__aenter__()
            entered = True
            from .external_proxy import _ProxyResponse
            return _ProxyResponse(stream(), close=close, status_code=relay.status_code, headers=relay.headers)
        except BaseException as failure:
            await close(sys.exc_info())
            if isinstance(failure, Exception):
                raise HTTPException(403, 'external_shared_send_rejected') from None
            raise

    def _before_send(self, raw):
        def check():
            self.validate()
            return raw
        return check

    def _reserve(self, payload, attempt):
        binding = self._selection['server_model_binding']
        return self._balances.reserve(actor=self._actor, user_id=binding['user_id'],
            user_root=self._root, binding=binding, attempt_id=attempt,
            model=self._selection['configuration']['model'],
            max_output_tokens=payload.get('max_completion_tokens', payload.get('max_tokens')),
            protocol='chat_completions', external_task_id=self._task_id)

    @staticmethod
    def _usage(raw, headers):
        if raw is None or headers.get('content-encoding', 'identity') not in {'identity', ''}:
            return None
        try:
            response = httpx.Response(200, content=bytes(raw), headers=headers)
            if 'text/event-stream' in headers.get('content-type', ''):
                events = list(EventSource(response).iter_sse())
                if not events or events[-1].data != '[DONE]':
                    return None
                candidates = []
                for event in events:
                    if not event.data or event.data == '[DONE]':
                        continue
                    value = _json(event.data)
                    if not isinstance(value, dict):
                        return None
                    candidates.append(value.get('usage'))
                candidates = [row for row in candidates if row is not None]
                if not candidates or any(row != candidates[-1] for row in candidates):
                    return None
                row = candidates[-1]
            else:
                value = _json(response.content.decode('utf-8'))
                row = value.get('usage') if isinstance(value, dict) else None
            if not isinstance(row, dict):
                return None
            result = {'input_tokens': row['prompt_tokens'], 'output_tokens': row['completion_tokens']}
            if any(type(value) is not int or value < 0 for value in result.values()):
                return None
            if 'total_tokens' in row:
                total = row['total_tokens']
                if type(total) is not int or total != result['input_tokens'] + result['output_tokens']:
                    return None
                result['total_tokens'] = total
            cache = {'cache_read_input_tokens': row.get('prompt_cache_hit_tokens'),
                'cache_miss_input_tokens': row.get('prompt_cache_miss_tokens'),
                'cache_creation_input_tokens': None}
            for value in cache.values():
                if value is not None and (type(value) is not int or not 0 <= value <= result['input_tokens']):
                    return None
            if (cache['cache_read_input_tokens'] is not None and cache['cache_miss_input_tokens'] is not None
                    and cache['cache_read_input_tokens'] + cache['cache_miss_input_tokens'] != result['input_tokens']):
                return None
            return {'usage': result, 'cache_observation': cache}
        except (ValueError, KeyError, TypeError, httpx.HTTPError):
            return None
