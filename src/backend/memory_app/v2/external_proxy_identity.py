import json
import re

from fastapi import HTTPException
from starlette.datastructures import Headers
from starlette.responses import JSONResponse

from backend.security.user_context import USER_ACCESS
from ..server_audit import AuditedRecordStore
from .external_proxy_transport import _headers, ProxyTransportError
from .external_server_identity import ServerMemoryLease


_PATHS = frozenset('/api/v2/external-agent/proxy/' + client + '/v1/' + protocol
    for client in ('claude', 'codex') for protocol in ('messages', 'responses', 'chat/completions'))
_SLOT = 'external_proxy_request_credentials'


class _Credentials:
    def __init__(self, key, headers, authority):
        self.key, self.headers, self.authority = key, headers, authority

    def __repr__(self):
        return 'ProxyRequestCredentials()'

    def close(self):
        self.key, self.headers = '', {}


class ProxyDeviceHeaders:
    """仅在原设备认证之前分离两套认证头，所有授权仍由原分发器完成。"""
    def __init__(self, app, *, authority):
        self.app, self.authority = app, authority

    async def __call__(self, scope, receive, send):
        if (scope['type'] != 'http' or scope.get('method') != 'POST'
                or scope['path'] not in _PATHS):
            return await self.app(scope, receive, send)
        headers = Headers(scope=scope)
        keys = headers.getlist('x-chriptmas-device-key')
        if (len(keys) != 1 or not re.fullmatch(r'[A-Za-z0-9_-]{43}', keys[0])
                or len(headers.getlist('authorization')) > 1
                or len(headers.getlist('x-api-key')) > 1):
            return await JSONResponse({'detail': 'external_proxy_credentials_invalid'}, status_code=401)(scope, receive, send)
        if scope.get('scheme') != 'https':
            return await JSONResponse({'detail': 'external_proxy_https_required'}, status_code=400)(scope, receive, send)
        if scope.get('query_string'):
            return await JSONResponse({'detail': 'external_proxy_query_forbidden'}, status_code=400)(scope, receive, send)
        try:
            upstream = _headers(headers.items())
            if any(keys[0] in value for value in upstream.values()):
                raise ProxyTransportError('external_proxy_credentials_invalid')
        except ProxyTransportError:
            return await JSONResponse({'detail': 'external_proxy_credentials_invalid'}, status_code=400)(scope, receive, send)
        credentials = _Credentials(keys[0], upstream, self.authority)
        state = scope.setdefault('state', {})
        state[_SLOT] = credentials
        # 原模型 Authorization 只保存在本次 owner；原认证中间件只看到设备 Bearer。
        scope['headers'] = [(name, value) for name, value in scope['headers']
            if name.lower() not in {b'authorization', b'x-chriptmas-device-key'}]
        scope['headers'].append((b'authorization', ('Bearer ' + keys[0]).encode('ascii')))
        try:
            await self.app(scope, receive, send)
        finally:
            state.pop(_SLOT, None)
            credentials.close()


def proxy_identity(request, *, application, records, workspace, runtime_root):
    state = application.state
    layout = getattr(state, 'deployment', None)
    if layout is None and getattr(state, 'server_context', None) is not None:
        raise HTTPException(403, 'external_proxy_user_domain_invalid')
    if layout is None or layout.mode == 'desktop':
        return None, request.headers, ()
    context = getattr(state, 'server_context', None)
    scope = request.scope.get('state', {})
    access = USER_ACCESS.get()
    credentials = scope.get(_SLOT)
    external = getattr(state, 'external_context', None)
    try:
        if (context is None or not isinstance(credentials, _Credentials)
                or credentials.authority is not context.auth
                or scope.get('server_dispatch_authority') is not context.auth
                or access is None or scope.get('user_access') is not access
                or scope.get('device_identity') is not access.caller
                or state.server_user_id != access.target_user_id
                or not isinstance(records, AuditedRecordStore) or records.user_id != state.server_user_id
                or state.recognition_records is not records or workspace.query.records is not records
                or external.records is not records or external.documents is not workspace.query.documents
                or external.owner_id != 'local-user'
                or state.recognition_runtime_root != runtime_root
                or context.users.root_for(state.server_user_id) != runtime_root
                or context.registry is not state.device_registry):
            raise ValueError('external_proxy_user_domain_invalid')
        lease = ServerMemoryLease(state, access.caller, access, credentials.key)
    except (AttributeError, ValueError):
        raise HTTPException(403, 'external_proxy_user_domain_invalid') from None
    return lease, Headers(credentials.headers), (credentials.key,)


def validate_proxy_body(raw, payload, secrets):
    # 成熟 JSON 解析器解码后再比较，防止转义后的设备钥匙进入上游正文。
    if (not isinstance(payload, dict)
            or any(value.encode('ascii') in raw or value in json.dumps(payload, ensure_ascii=False)
                for value in secrets)):
        raise HTTPException(400, 'external_proxy_body_invalid')
