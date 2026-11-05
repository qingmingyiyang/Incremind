import json
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from mcp.server.auth.handlers.authorize import AuthorizationHandler
from mcp.server.auth.handlers.metadata import MetadataHandler
from mcp.server.auth.handlers.revoke import RevocationHandler
from mcp.server.auth.handlers.token import TokenHandler
from mcp.server.auth.middleware.client_auth import ClientAuthenticator
from mcp.server.auth.routes import build_metadata, create_protected_resource_routes, cors_middleware
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from pydantic import AnyHttpUrl
from starlette.datastructures import FormData
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route, Router

from .native_mcp_scope import NativeMcpError, SCOPES, validate_scopes


def _safe_oauth_response(response):
    codes = {'invalid_request', 'invalid_client', 'invalid_grant', 'unauthorized_client',
        'unsupported_grant_type', 'invalid_scope', 'access_denied', 'unsupported_response_type',
        'server_error', 'temporarily_unavailable', 'invalid_client_metadata', 'invalid_redirect_uri'}
    if 300 <= response.status_code < 400 and 'location' in response.headers:
        target = urlsplit(response.headers['location'])
        params = parse_qsl(target.query, keep_blank_values=True)
        if not any(key == 'error' for key, _ in params):
            return response
        # SDK 已验证重定向目标；仅保留协议错误码和原 state。
        code = next(value for key, value in params if key == 'error')
        safe = [(key, value) for key, value in params if key not in {'error', 'error_description', 'error_uri'}]
        safe += [('error', code if code in codes else 'server_error'),
            ('error_description', 'OAuth request rejected')]
        response.headers['location'] = urlunsplit(target._replace(query=urlencode(safe)))
        return response
    if response.status_code >= 400:
        data = json.loads(response.body)
        code = data.get('error')
        result = {'error': code if code in codes else 'server_error', 'error_description': 'OAuth request rejected'}
        if isinstance(data.get('state'), str):
            result['state'] = data['state']
        headers = {key: value for key, value in response.headers.items() if key not in {'content-length', 'content-type'}}
        return JSONResponse(result, status_code=response.status_code, headers=headers)
    return response


class NativeGateway:
    def __init__(self, app, native, oauth):
        self.app, self.native, self.oauth = app, native, oauth
        self.native.original_application = app
        self.router = None

    def configure(self):
        with self.oauth.records.begin_read() as tx:
            config, origin = self.native.credentials.configuration(tx)
        self.config_revision = config.revision
        options = ClientRegistrationOptions(enabled=False, valid_scopes=list(SCOPES))
        metadata = build_metadata(AnyHttpUrl(origin), None, options, RevocationOptions(enabled=True))
        metadata.token_endpoint_auth_methods_supported = ['none']
        metadata.revocation_endpoint_auth_methods_supported = ['none']
        authenticator = ClientAuthenticator(self.oauth)
        authorize = AuthorizationHandler(self.oauth)
        token = TokenHandler(self.oauth, authenticator)
        revoke = RevocationHandler(self.oauth, authenticator)

        async def protocol(request):
            params = request.query_params if request.method == 'GET' else await request.form()
            if any(len(params.getlist(key)) != 1 for key in params):
                return self._oauth_error('invalid_request')
            resource = params.get('resource')
            if request.url.path in {'/authorize', '/token'} and resource != origin + '/mcp':
                return self._oauth_error('invalid_request')
            if 'scope' in params:
                try:
                    validate_scopes(params['scope'].split(' '))
                except NativeMcpError:
                    return self._oauth_error('invalid_scope')
            if request.url.path == '/authorize':
                return _safe_oauth_response(await authorize.handle(request))
            if request.url.path == '/token':
                return _safe_oauth_response(await token.handle(request))
            # SDK 对 public none 仍要求可空字段；仅补 SDK 同一 Request 的已解析表单。
            client = await self.oauth.get_client(params.get('client_id', ''))
            if client is not None and client.token_endpoint_auth_method == 'none' and 'client_secret' not in params:
                request._form = FormData([*params.multi_items(), ('client_secret', None)])
            return _safe_oauth_response(await revoke.handle(request))

        routes = [Route('/.well-known/oauth-authorization-server',
                cors_middleware(MetadataHandler(metadata).handle, ['GET', 'OPTIONS']), methods=['GET', 'OPTIONS']),
            Route('/authorize', protocol, methods=['GET', 'POST']),
            Route('/token', cors_middleware(protocol, ['POST', 'OPTIONS']), methods=['POST', 'OPTIONS']),
            Route('/revoke', cors_middleware(protocol, ['POST', 'OPTIONS']), methods=['POST', 'OPTIONS'])]
        routes += create_protected_resource_routes(AnyHttpUrl(origin + '/mcp'), [AnyHttpUrl(origin)],
            scopes_supported=list(SCOPES), resource_name='Chriptmas Memory')
        self.router = Router(routes)

    @staticmethod
    def _oauth_error(code):
        return JSONResponse({'error': code}, status_code=400, headers={'Cache-Control': 'no-store'})

    async def __call__(self, scope, receive, send):
        if scope['type'] not in {'http', 'websocket'}:
            return await self.app(scope, receive, send)
        from starlette.requests import HTTPConnection
        request = HTTPConnection(scope)
        path, method = scope['path'], scope.get('method')
        public = {'/mcp': {'GET', 'POST', 'OPTIONS', 'DELETE'},
            '/.well-known/oauth-protected-resource/mcp': {'GET'},
            '/.well-known/oauth-authorization-server': {'GET', 'OPTIONS'}, '/authorize': {'GET', 'POST'},
            '/token': {'POST', 'OPTIONS'}, '/revoke': {'POST', 'OPTIONS'}}
        values = request.headers.getlist('authorization')
        presented = [value[7:] for value in values if value.startswith('Bearer ')]
        presented += request.headers.getlist('x-chriptmas-device-key')
        restricted = any(self.native.credentials.is_restricted_token(value) for value in presented)
        if scope['type'] == 'websocket':
            if restricted:
                return await send({'type': 'websocket.close', 'code': 4403})
            return await self.app(scope, receive, send)
        if path not in public:
            if restricted:
                return await JSONResponse({'detail': 'native_mcp_scope_forbidden'}, status_code=403)(scope, receive, send)
            return await self.app(scope, receive, send)
        if method not in public[path]:
            return await JSONResponse({'detail': 'method_not_allowed'}, status_code=405)(scope, receive, send)
        try:
            with self.oauth.records.begin_read() as tx:
                _, origin = self.native.credentials.configuration(tx)
        except NativeMcpError:
            return await JSONResponse({'detail': 'native_mcp_not_configured'}, status_code=503)(scope, receive, send)
        if request.headers.getlist('host') != [urlsplit(origin).netloc]:
            return await JSONResponse({'detail': 'native_mcp_host_invalid'}, status_code=421)(scope, receive, send)
        if len(values) > 1:
            return await JSONResponse({'detail': 'native_mcp_credential_invalid'}, status_code=401)(scope, receive, send)
        if path == '/mcp':
            origins = request.headers.getlist('origin')
            if len(origins) > 1 or origins and origins[0] not in {origin, 'https://chatgpt.com'}:
                return await JSONResponse({'detail': 'native_mcp_origin_invalid'}, status_code=403)(scope, receive, send)
            if scope.get('query_string'):
                return await JSONResponse({'detail': 'native_mcp_query_forbidden'}, status_code=400)(scope, receive, send)
            return await self.native.handle(scope, receive, send)
        with self.oauth.records.begin_read() as tx:
            config, _ = self.native.credentials.configuration(tx)
        if self.router is None or self.config_revision != config.revision:
            self.configure()
        if len(scope.get('query_string', b'')) > self.oauth.policy['body_bytes']:
            return await JSONResponse({'error': 'invalid_request'}, status_code=413)(scope, receive, send)
        if method == 'POST':
            request = Request(scope, receive)
            chunks, size = [], 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > self.oauth.policy['body_bytes']:
                    return await JSONResponse({'error': 'invalid_request'}, status_code=413)(scope, receive, send)
                chunks.append(chunk)
            body = b''.join(chunks)

            original_receive, delivered = receive, False

            async def received():
                nonlocal delivered
                if delivered:
                    return await original_receive()
                delivered = True
                return {'type': 'http.request', 'body': body, 'more_body': False}

            receive = received
        return await self.router(scope, receive, send)


def install_native_mcp_routes(application, *, records):
    from fastapi import APIRouter, HTTPException, Request
    from fastapi.routing import APIRoute
    from mcp.server.auth.handlers.register import RegistrationHandler
    from ..workspace_contracts import _json
    from backend.security.user_context import USER_ACCESS
    from core.storage_provider import SQLiteUnitOfWorkConflict
    from .native_mcp_auth import OAUTH_CLIENTS, provisioning
    from .native_mcp_credentials import RESTRICTIONS, PREFIX
    from .native_mcp_human import HumanReview

    async def rejected(request, error):
        code = str(error)
        status = 403 if code in {'native_mcp_human_required', 'native_mcp_target_forbidden'} else 400 if code in {
            'native_mcp_request_invalid', 'native_mcp_scope_invalid', 'native_mcp_configuration_invalid',
            'native_mcp_human_action_invalid', 'native_mcp_decision_invalid'} else 409
        return JSONResponse({'detail': code}, status_code=status, headers={'Cache-Control': 'no-store'})

    class NativeRoute(APIRoute):
        def get_route_handler(self):
            handler = super().get_route_handler()

            async def invoke(request):
                try:
                    return await handler(request)
                except NativeMcpError as error:
                    return await rejected(request, error)
                except SQLiteUnitOfWorkConflict:
                    return JSONResponse({'detail': 'native_mcp_revision_conflict'}, status_code=409,
                        headers={'Cache-Control': 'no-store'})

            return invoke

    router = APIRouter(prefix='/api/v2/native-mcp', route_class=NativeRoute)

    def owner():
        context = getattr(application.state, 'server_context', None)
        value = getattr(context, 'native_mcp', None)
        if value is None:
            raise HTTPException(409, 'native_mcp_server_required')
        return value

    def access():
        value = USER_ACCESS.get()
        if value is None:
            raise HTTPException(401, 'native_mcp_human_required')
        with owner().credentials.records.begin_read() as tx:
            owner().credentials.full_caller(tx, value.caller)
            owner().credentials._target(tx, value.caller, value.target_user_id)
        return value

    async def body(request, fields):
        result = await _json(request)
        if not isinstance(result, dict) or set(result) != fields:
            raise HTTPException(400, 'native_mcp_request_invalid')
        return result

    @router.get('')
    def settings():
        context = getattr(application.state, 'server_context', None)
        if getattr(context, 'native_mcp', None) is None:
            return {'available': False, 'reason': 'native_mcp_server_required'}
        selected = access()
        runtime = owner()
        with runtime.credentials.records.begin_read() as tx:
            caller = runtime.credentials.full_caller(tx, selected.caller)
            canonical = tx.read('v2_native_mcp_configuration', 'canonical')
            keys = []
            for restriction in tx.list(RESTRICTIONS):
                value = runtime.credentials._restriction(restriction)
                device = tx.read('server_devices', restriction.object_id)
                if (value.actor_user_id != selected.caller.user_id or value.target_user_id != selected.target_user_id
                        or value.family_id is not None or device is None):
                    continue
                keys.append({'device_id': device.object_id, 'client': value.client, 'scopes': value.scopes,
                    'expires_at': value.expires_at, 'revision': restriction.revision, 'device_revision': device.revision,
                    'revoked': device.payload['revoked_at'] is not None})
            clients = [{'client_id': row.object_id, 'redirect_uris': row.payload['client']['redirect_uris'],
                'scopes': row.payload['client'].get('scope'), 'revision': row.revision} for row in tx.list(OAUTH_CLIENTS)
                if row.payload['actor_user_id'] == selected.caller.user_id]
        preference = records.read('v2_native_mcp_memory_preferences', 'chatgpt')
        return {'available': True, 'origin': canonical.payload['origin'] if canonical else None,
            'config_revision': canonical.revision if canonical else 0,
            'can_configure': caller.payload['role'] == 'admin', 'target_user_id': selected.target_user_id,
            'chatgpt': {'enabled': preference.payload['enabled'] if preference else False,
                'revision': preference.revision if preference else 0}, 'keys': keys, 'oauth_clients': clients}

    @router.put('/configuration')
    async def configure(request: Request):
        data = await body(request, {'origin', 'expected_revision'})
        return owner().credentials.configure(access().caller, **data)

    @router.put('/chatgpt')
    async def preference(request: Request):
        access()
        data = await body(request, {'enabled', 'expected_revision'})
        if type(data['enabled']) is not bool or type(data['expected_revision']) is not int:
            raise HTTPException(400, 'native_mcp_request_invalid')
        with records.begin() as tx:
            saved = tx.put('v2_native_mcp_memory_preferences', 'chatgpt', {'enabled': data['enabled']},
                expected_revision=data['expected_revision'])
            tx.commit()
        return {'enabled': saved.payload['enabled'], 'revision': saved.revision}

    @router.post('/keys')
    async def issue(request: Request):
        selected = access()
        data = await body(request, {'client', 'scopes', 'expires_at', 'name'})
        if data['client'] == 'chatgpt':
            raise HTTPException(400, 'native_mcp_oauth_required')
        return owner().credentials.issue(actor=selected.caller, target=selected.target_user_id, **data)

    @router.post('/keys/{device_id}/revoke')
    async def revoke(device_id: str, request: Request):
        selected = access()
        data = await body(request, {'expected_revision'})
        if not device_id.startswith(PREFIX):
            raise HTTPException(404, 'native_mcp_key_not_found')
        runtime = owner()
        with runtime.credentials.records.begin() as tx:
            runtime.credentials.full_caller(tx, selected.caller)
            scope = tx.read(RESTRICTIONS, device_id)
            if (scope is None or scope.payload['actor_user_id'] != selected.caller.user_id
                    or scope.payload['target_user_id'] != selected.target_user_id):
                raise HTTPException(404, 'native_mcp_key_not_found')
            result = runtime.credentials.registry.revoke_enlisted(tx, selected.caller.user_id,
                device_id, expected_revision=data['expected_revision'])
            tx.commit()
            return result

    @router.post('/oauth-clients')
    async def client(request: Request):
        selected = access()
        runtime = owner()
        data = await request.json()
        if not isinstance(data, dict) or not isinstance(data.get('scope'), str):
            raise HTTPException(400, 'native_mcp_scope_invalid')
        validate_scopes(data['scope'].split(' '))
        options = ClientRegistrationOptions(enabled=True, valid_scopes=list(SCOPES))
        with provisioning(selected.caller):
            return _safe_oauth_response(await RegistrationHandler(runtime.oauth, options).handle(request))

    @router.get('/consent/{request_id}')
    def consent(request_id: str):
        selected = access()
        return {**owner().oauth.pending(selected.caller, request_id), 'target_user_id': selected.target_user_id}

    @router.post('/consent/{request_id}')
    async def decision(request_id: str, request: Request):
        selected = access()
        data = await body(request, {'expected_revision', 'approve'})
        return owner().oauth.decide(selected.caller, request_id, target=selected.target_user_id, **data)

    review = None

    def reviewer():
        nonlocal review
        if review is None:
            review = HumanReview(owner().credentials, application.state.recognition_service)
        return review

    @router.get('/review/{candidate_id}')
    def candidate(candidate_id: str, project_id: str):
        selected = access()
        reviewer()._authorize(selected.caller, selected.target_user_id)
        row = records.read('recognition_candidates', candidate_id)
        if row is None:
            raise HTTPException(404, 'native_mcp_candidate_unavailable')
        with records.begin_read() as tx:
            row = reviewer()._candidate(tx, project=project_id, candidate_id=candidate_id, revision=row.revision)
        return {'candidate_id': row.object_id, 'project_id': project_id, 'revision': row.revision,
            'content': row.payload['content'], 'conditions': row.payload.get('conditions'),
            'target_user_id': selected.target_user_id}

    @router.post('/review/{candidate_id}/prepare')
    async def prepare(candidate_id: str, request: Request):
        selected = access()
        data = await body(request, {'project', 'revision', 'action'})
        return reviewer().prepare(records, actor=selected.caller, target=selected.target_user_id,
            candidate_id=candidate_id, **data)

    @router.post('/review/{candidate_id}/commit')
    async def commit(candidate_id: str, request: Request):
        selected = access()
        data = await body(request, {'project', 'revision', 'action', 'nonce'})
        return reviewer().commit(records, actor=selected.caller, target=selected.target_user_id,
            candidate_id=candidate_id, **data)

    application.include_router(router)
