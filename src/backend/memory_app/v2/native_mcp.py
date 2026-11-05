from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
import json
from urllib.parse import urlsplit

import httpx
from jsonschema import Draft202012Validator
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.version import SUPPORTED_PROTOCOL_VERSIONS
from starlette.requests import Request
from starlette.responses import JSONResponse
from pydantic import ValidationError

from ..mcp import _error, _input_schema, _tools, _SAFE_ERRORS
from ..server_jobs import retain_until_finished
from .native_mcp_scope import NativeMcpError, native_request
from .native_mcp_scope import SCOPES
from .policies import get


_INVOCATION = ContextVar('native_mcp_http_invocation', default=None)
_PATH = '/api/v2/external-agent/mcp/'


@dataclass(frozen=True)
class _Invocation:
    authority: object
    lease: object
    token: str = field(repr=False)


class NativeRuntime:
    def __init__(self, registry, *, allow_loopback=False):
        from .native_mcp_auth import NativeOAuth, NativeRelatedOwner
        from .native_mcp_credentials import NativeCredentials
        self.credentials = NativeCredentials(registry, allow_loopback=allow_loopback)
        self.oauth = NativeOAuth(self.credentials)
        self.native = NativeMcp(self.credentials, None)
        self.related_owner = NativeRelatedOwner(self.oauth)


class NativeMcp:
    def __init__(self, credentials, original_application):
        self.credentials = credentials
        self.original_application = original_application
        self.authority = object()
        self.server = Server('chriptmas-memory', version='1.0.0')
        self.tools = {tool.name: tool for tool in _tools(shared=True)}
        from .native_mcp_cards import install_cards
        install_cards(self)
        self.manager = None
        self.origin = None

        @self.server.list_tools()
        async def list_tools():
            self._invocation().lease.validate()
            return list(self.tools.values())

        @self.server.call_tool(validate_input=False)
        async def call_tool(name, arguments):
            invocation = self._invocation()
            if name not in self.tools or not Draft202012Validator(_input_schema(name, shared=True)).is_valid(arguments):
                return _error('external_agent_arguments_invalid')
            if name == 'read' and 'window' in arguments and arguments['window']['start'] >= arguments['window']['end']:
                return _error('external_agent_arguments_invalid')
            if invocation.token in json.dumps(arguments, ensure_ascii=False, allow_nan=False):
                return _error('native_mcp_credential_in_arguments')
            lease = replace(invocation.lease, tool=name)
            try:
                lease.validate()
                transport = httpx.ASGITransport(app=self._original)
                with native_request(lease):
                    async with httpx.AsyncClient(transport=transport, base_url=self.origin,
                            follow_redirects=False, trust_env=False) as client:
                        response = await retain_until_finished(client.post(_PATH + name,
                            headers={'Authorization': 'Bearer ' + invocation.token,
                                'x-chriptmas-target-user': lease.restriction.target_user_id},
                            json={'client': lease.restriction.client, 'arguments': arguments}))
                lease.validate()
            except NativeMcpError as error:
                return _error(str(error))
            data = response.json()
            if response.status_code != 200:
                code = data.get('detail') if isinstance(data, dict) else None
                return _error(code if code in _SAFE_ERRORS else 'native_mcp_tool_rejected')
            if not Draft202012Validator(self.tools[name].outputSchema).is_valid(data):
                return _error('external_agent_backend_failed')
            from .native_mcp_cards import card_metadata
            return types.CallToolResult(content=[types.TextContent(type='text',
                text=json.dumps(data, ensure_ascii=False, allow_nan=False))], structuredContent=data,
                _meta=card_metadata(self, invocation, name, data))

    def _invocation(self):
        invocation = _INVOCATION.get()
        if not isinstance(invocation, _Invocation) or invocation.authority is not self.authority:
            raise NativeMcpError('native_mcp_invocation_invalid')
        return invocation

    async def _original(self, scope, receive, send):
        invocation = self._invocation()
        if (scope['type'] != 'http' or scope['method'] != 'POST'
                or scope['path'] not in {_PATH + name for name in self.tools}):
            raise NativeMcpError('native_mcp_invocation_invalid')
        invocation.lease.validate()
        scope = {**scope, 'state': {**scope.get('state', {}), 'native_mcp_authority': self.authority}}
        await self.original_application(scope, receive, send)

    @asynccontextmanager
    async def lifespan(self):
        self.manager = StreamableHTTPSessionManager(self.server, stateless=True, json_response=True,
            security_settings=TransportSecuritySettings(enable_dns_rebinding_protection=True,
                allowed_hosts=[], allowed_origins=[]))
        try:
            async with self.manager.run():
                yield
        finally:
            self.manager = None

    def challenge(self, *, scope=None, invalid=False):
        value = 'Bearer resource_metadata="' + self.origin + '/.well-known/oauth-protected-resource/mcp"'
        if invalid:
            value += ', error="invalid_token"'
        if scope is not None:
            value += ', error="insufficient_scope", scope="' + scope + '"'
        return value

    async def handle(self, scope, receive, send):
        if self.manager is None:
            return await JSONResponse({'detail': 'native_mcp_not_configured'}, status_code=503)(scope, receive, send)
        request = Request(scope)
        with self.credentials.records.begin_read() as tx:
            _, self.origin = self.credentials.configuration(tx)
        self.manager.security_settings.allowed_hosts = [urlsplit(self.origin).netloc]
        self.manager.security_settings.allowed_origins = [self.origin, 'https://chatgpt.com']
        values = request.headers.getlist('authorization')
        token = values[0][7:] if len(values) == 1 and values[0].startswith('Bearer ') else None
        try:
            if token is None:
                raise NativeMcpError('native_mcp_credential_invalid')
            lease = self.credentials.authenticate(token)
        except NativeMcpError:
            return await JSONResponse({'detail': 'native_mcp_credential_invalid'}, status_code=401,
                headers={'WWW-Authenticate': self.challenge(invalid=True), 'Cache-Control': 'no-store'})(scope, receive, send)
        versions = request.headers.getlist('mcp-protocol-version')
        if len(versions) > 1 or versions and versions[0] not in SUPPORTED_PROTOCOL_VERSIONS:
            return await JSONResponse({'detail': 'native_mcp_protocol_invalid'}, status_code=400)(scope, receive, send)
        if scope['method'] == 'POST':
            chunks, size = [], 0
            request = Request(scope, receive)
            async for chunk in request.stream():
                size += len(chunk)
                if size > get('native_mcp_auth', version='@1')()['tool_body_bytes']:
                    return await JSONResponse({'detail': 'native_mcp_request_too_large'}, status_code=413)(scope, receive, send)
                chunks.append(chunk)
            body = b''.join(chunks)
            try:
                message = types.JSONRPCMessage.model_validate_json(body).root
                payload = message.model_dump(by_alias=True, mode='json', exclude_none=True)
                if isinstance(message, types.JSONRPCRequest):
                    types.ClientRequest.model_validate(payload)
                elif isinstance(message, types.JSONRPCNotification):
                    types.ClientNotification.model_validate(payload)
                else:
                    # 无状态七工具不签发服务端请求，没有可接收响应的会话。
                    return await JSONResponse({'detail': 'native_mcp_request_invalid'}, status_code=400)(scope, receive, send)
            except ValidationError:
                return await JSONResponse({'detail': 'native_mcp_request_invalid'}, status_code=400)(scope, receive, send)
            if isinstance(message, types.JSONRPCRequest) and message.method == 'tools/call':
                try:
                    call = types.CallToolRequestParams.model_validate(message.params)
                    if call.name not in self.tools:
                        return await JSONResponse({'detail': 'native_mcp_request_invalid'}, status_code=400)(scope, receive, send)
                    replace(lease, tool=call.name).validate()
                except ValidationError:
                    return await JSONResponse({'detail': 'native_mcp_request_invalid'}, status_code=400)(scope, receive, send)
                except NativeMcpError as error:
                    required = next((item for item, tools in SCOPES.items() if call.name in tools), None)
                    return await JSONResponse({'detail': str(error)}, status_code=403,
                        headers={'WWW-Authenticate': self.challenge(scope=required), 'Cache-Control': 'no-store'})(scope, receive, send)

            original_receive, delivered = receive, False

            async def received():
                nonlocal delivered
                if delivered:
                    return await original_receive()
                delivered = True
                return {'type': 'http.request', 'body': body, 'more_body': False}

            receive = received
        context = _INVOCATION.set(_Invocation(self.authority, lease, token))
        try:
            await self.manager.handle_request(scope, receive, send)
        finally:
            _INVOCATION.reset(context)
