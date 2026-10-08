"""Server ASGI identity gate for HTTP, streaming responses and WebSockets."""
from __future__ import annotations

import hmac
import secrets
from urllib.parse import urlsplit

from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse
from starlette.websockets import WebSocketDisconnect

from .device_identity import server_mode, ServerTicketIdentity


def _bearer(connection):
    if len(connection.headers.getlist('authorization')) != 1:
        return None
    value = connection.headers.get('authorization', '')
    return value[7:] if value.startswith('Bearer ') and value.isascii() else None


def same_origin(connection):
    origin = connection.headers.get('origin')
    if origin is None:
        return True
    try:
        parsed = urlsplit(origin)
        base = connection.base_url
        scheme = {'ws': 'http', 'wss': 'https'}.get(base.scheme, base.scheme)
        return (not parsed.username and not parsed.password and not parsed.path and not parsed.query
            and not parsed.fragment and parsed.scheme == scheme and parsed.hostname == base.hostname
            and (parsed.port or (443 if scheme == 'https' else 80)) == (base.port or (443 if scheme == 'https' else 80)))
    except ValueError:
        return False


class ServerDeviceAuth:
    """Separate in-memory local-model identity, never persisted or projected."""
    def __init__(self, registry, *, port=8001):
        self.registry = registry
        self._local_key = secrets.token_urlsafe(32)
        self._user_keys = {'local-user': self._local_key}
        self.configure_port(port)

    def configure_port(self, port):
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError('server_port_invalid')
        self._local_endpoint = f'http://127.0.0.1:{port}/local-model/v1'

    def internal_key_for(self, base_url):
        return self._local_key if base_url == self._local_endpoint else None

    def internal_key_for_user(self, user_id, base_url):
        if base_url != self._local_endpoint:
            return None
        if user_id not in self._user_keys:
            self._user_keys[user_id] = secrets.token_urlsafe(32)
        return self._user_keys[user_id]

    def internal_request(self, connection):
        value = _bearer(connection)
        return (connection.scope['type'] == 'http' and connection.scope.get('method') == 'POST'
            and connection.scope['path'] == '/local-model/v1/chat/completions'
            and not connection.url.query
            and str(connection.base_url).rstrip('/') + '/local-model/v1' == self._local_endpoint
            and isinstance(value, str) and any(hmac.compare_digest(value, key) for key in self._user_keys.values()))

    def internal_target(self, connection):
        if not self.internal_request(connection):
            return None
        value = _bearer(connection)
        return next(user for user, key in self._user_keys.items() if hmac.compare_digest(value, key))


class DeviceAuthenticationMiddleware:
    def __init__(self, app, *, application, registry, auth):
        self.app, self.application, self.registry = app, application, registry
        self.auth = auth

    async def __call__(self, scope, receive, send):
        if scope['type'] not in {'http', 'websocket'}:
            return await self.app(scope, receive, send)
        scope['app'] = self.application
        connection = HTTPConnection(scope)
        if not server_mode(connection):
            return await self.app(scope, receive, send)
        if scope.get('state', {}).get('server_dispatch_authority') is self.auth:
            return await self.app(scope, receive, send)
        path, method = scope['path'], scope.get('method')
        if scope['type'] == 'websocket':
            if not same_origin(connection):
                return await send({'type': 'websocket.close', 'code': 4403})
            if any(name.lower() in {'ticket', 'key', 'device_key', 'api_key', 'authorization'} for name in connection.query_params):
                return await send({'type': 'websocket.close', 'code': 4401})
        if scope['type'] == 'http':
            if method in {'GET', 'HEAD'} and path == '/api/health':
                return await JSONResponse({'status': 'ok'}, headers={'Cache-Control': 'no-store'})(scope, receive, send)
            public = method in {'GET', 'HEAD'} and (path in {'/', '/pair'}
                or path in getattr(self.application.state, 'server_static_paths', ()))
            public = public or method == 'POST' and path == '/api/v2/devices/exchange'
            if method in {'POST', 'PUT', 'PATCH', 'DELETE'} and not same_origin(connection):
                return await JSONResponse({'detail': 'same_origin_required'}, status_code=403)(scope, receive, send)
            if public:
                scope.setdefault('state', {})['server_auth_public'] = True
                return await self.app(scope, receive, send)
            if self.auth.internal_request(connection):
                scope.setdefault('state', {})['server_internal_model'] = True
                scope['state']['server_internal_target'] = self.auth.internal_target(connection)
                return await self.app(scope, receive, send)
        identity = self.registry.authenticate(_bearer(connection))
        if identity is None and scope['type'] == 'websocket':
            consume = getattr(self.application.state, 'server_realtime_ticket_consumer', None)
            if scope['path'] == '/api/rebuild/workbench/realtime-asr/ws' and consume is not None:
                offered = scope.get('subprotocols', [])
                tickets = [value.removeprefix('chriptmas-asr-ticket.') for value in offered
                    if value.startswith('chriptmas-asr-ticket.')]
                if len(tickets) == 1 and 'chriptmas-asr' in offered and 'ticket' not in connection.query_params:
                    ticket_identity = consume(tickets[0], self.registry,
                        users=getattr(self.application.state, 'server_users', None))
                    if isinstance(ticket_identity, ServerTicketIdentity):
                        identity = ticket_identity.caller
                        scope.setdefault('state', {})['server_ticket_target'] = ticket_identity.target_user_id
        if identity is None:
            if scope['type'] == 'websocket':
                return await send({'type': 'websocket.close', 'code': 4401})
            return await JSONResponse({'detail': 'device_unauthorized'}, status_code=401,
                headers={'WWW-Authenticate': 'Bearer', 'Cache-Control': 'no-store'})(scope, receive, send)
        scope.setdefault('state', {})['device_identity'] = identity
        if scope['type'] == 'http':
            return await self.app(scope, receive, send)
        closed = False

        def current():
            if not self.registry.is_current(identity):
                return False
            access = scope.get('state', {}).get('user_access')
            if access is None:
                return True
            row = self.registry.records.read('server_users', access.target_user_id)
            return row is not None and row.payload.get('disabled_at') is None

        async def reject():
            nonlocal closed
            if not closed:
                closed = True
                await send({'type': 'websocket.close', 'code': 4401})

        async def guarded_receive():
            if not current():
                await reject()
                return {'type': 'websocket.disconnect', 'code': 4401}
            message = await receive()
            if not current():
                await reject()
                return {'type': 'websocket.disconnect', 'code': 4401}
            return message

        async def guarded_send(message):
            if not current():
                await reject()
                raise WebSocketDisconnect(code=4401)
            elif not closed:
                await send(message)
        return await self.app(scope, guarded_receive, guarded_send)


def install_device_authentication(application, *, registry, auth=None):
    auth = auth or ServerDeviceAuth(registry)
    application.state.server_device_auth = auth
    application.add_middleware(DeviceAuthenticationMiddleware, application=application, registry=registry, auth=auth)
    return auth
