from contextlib import contextmanager
from contextvars import ContextVar
from urllib.parse import urlsplit
from uuid import uuid4

from mcp.server.auth.provider import (AccessToken, AuthorizationCode, AuthorizationParams,
    AuthorizeError, RefreshToken, RegistrationError, TokenError, construct_redirect_uri)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from backend.security.device_identity import DeviceIdentity
from .devices import DEVICES, _credential, _digest
from .native_mcp_credentials import FAMILIES, RESTRICTIONS
from .native_mcp_scope import NativeMcpError, SCOPES, validate_scopes
from .policies import get


OAUTH_CLIENTS = 'v2_native_mcp_oauth_clients'
PENDING = 'v2_native_mcp_oauth_pending'
CODES = 'v2_native_mcp_oauth_codes'
REFRESH = 'v2_native_mcp_oauth_refresh'
PROVISION_ACTOR = ContextVar('native_mcp_provision_actor', default=None)


@contextmanager
def provisioning(actor):
    token = PROVISION_ACTOR.set(actor)
    try:
        yield
    finally:
        PROVISION_ACTOR.reset(token)


class NativeOAuth:
    def __init__(self, credentials):
        self.credentials, self.records = credentials, credentials.records
        self.policy = get('native_mcp_auth', version='@1')()

    def _client(self, tx, client_id):
        row = tx.read(OAUTH_CLIENTS, client_id)
        if row is None:
            return None
        config, _ = self.credentials.configuration(tx)
        if row.payload['config_revision'] != config.revision:
            return None
        return OAuthClientInformationFull.model_validate(row.payload['client'])

    async def get_client(self, client_id):
        with self.records.begin_read() as tx:
            return self._client(tx, client_id)

    async def register_client(self, client_info):
        actor = PROVISION_ACTOR.get()
        if (client_info.token_endpoint_auth_method != 'none' or client_info.client_secret is not None
                or set(client_info.grant_types or []) != {'authorization_code', 'refresh_token'}
                or client_info.response_types != ['code']):
            raise RegistrationError('invalid_client_metadata', 'native_mcp_public_client_required')
        validate_scopes((client_info.scope or '').split())
        redirects = client_info.redirect_uris or []
        if not redirects or len(redirects) > 4:
            raise RegistrationError('invalid_redirect_uri')
        for redirect in redirects:
            parsed = urlsplit(str(redirect))
            if (parsed.fragment or parsed.username or parsed.password or parsed.scheme != 'https'
                    or parsed.hostname != 'chatgpt.com'
                    or not (parsed.path.startswith('/connector/oauth/') and len(parsed.path.split('/')) == 4
                        and parsed.path.split('/')[-1])):
                raise RegistrationError('invalid_redirect_uri', 'native_mcp_exact_chatgpt_callback_required')
        with self.records.begin() as tx:
            self.credentials.full_caller(tx, actor)
            config, _ = self.credentials.configuration(tx)
            if len(tx.list(OAUTH_CLIENTS)) >= self.policy['clients_global']:
                raise RegistrationError('invalid_client_metadata', 'native_mcp_client_limit')
            tx.put(OAUTH_CLIENTS, client_info.client_id, {'client': client_info.model_dump(mode='json'),
                'actor_user_id': actor.user_id, 'config_revision': config.revision}, expected_revision=0)
            tx.commit()

    def _cleanup(self, tx):
        now = self.credentials.now()
        for collection in (PENDING, CODES):
            for row in tx.list(collection):
                if row.payload['expires_at'] <= now:
                    tx.delete(collection, row.object_id, expected_revision=row.revision)
        for row in tx.list(REFRESH):
            if row.payload['expires_at'] <= now:
                tx.delete(REFRESH, row.object_id, expected_revision=row.revision)

    async def authorize(self, client, params):
        try:
            scopes = validate_scopes(params.scopes or [])
        except NativeMcpError:
            raise AuthorizeError('invalid_scope', 'native_mcp_scope_invalid') from None
        pending_id = 'consent-' + uuid4().hex
        with self.records.begin() as tx:
            self._cleanup(tx)
            config, origin = self.credentials.configuration(tx)
            actual = self._client(tx, client.client_id)
            if (actual != client or params.resource != origin + '/mcp'
                    or str(params.redirect_uri) not in {str(uri) for uri in client.redirect_uris}
                    or not set(scopes).issubset(set((client.scope or '').split()))):
                raise AuthorizeError('invalid_request', 'native_mcp_authorization_invalid')
            rows = tx.list(PENDING)
            if (len(rows) >= self.policy['pending_global'] or sum(
                    row.payload['client_id'] == client.client_id for row in rows) >= self.policy['pending_per_client']):
                raise AuthorizeError('temporarily_unavailable', 'native_mcp_pending_limit')
            tx.put(PENDING, pending_id, {'client_id': client.client_id, 'config_revision': config.revision,
                'params': params.model_dump(mode='json'), 'scopes': scopes,
                'expires_at': self.credentials.now() + self.policy['pending_seconds']}, expected_revision=0)
            tx.commit()
        return origin + '/#native-mcp-consent=' + pending_id

    def pending(self, actor, pending_id):
        with self.records.begin_read() as tx:
            self.credentials.full_caller(tx, actor)
            row = tx.read(PENDING, pending_id)
            config, _ = self.credentials.configuration(tx)
            if (row is None or row.payload['expires_at'] <= self.credentials.now()
                    or row.payload['config_revision'] != config.revision):
                raise NativeMcpError('native_mcp_consent_unavailable')
            return {'request_id': row.object_id, 'revision': row.revision,
                'client_id': row.payload['client_id'], 'scopes': row.payload['scopes'],
                'expires_at': row.payload['expires_at']}

    def decide(self, actor, pending_id, *, expected_revision, target, approve):
        with self.records.begin() as tx:
            self.credentials.full_caller(tx, actor)
            self.credentials._target(tx, actor, target)
            row = tx.read(PENDING, pending_id)
            config, _ = self.credentials.configuration(tx)
            if (row is None or row.revision != expected_revision
                    or row.payload['expires_at'] <= self.credentials.now()
                    or row.payload['config_revision'] != config.revision
                    or self._client(tx, row.payload['client_id']) is None):
                raise NativeMcpError('native_mcp_consent_unavailable')
            params = AuthorizationParams.model_validate(row.payload['params'])
            tx.delete(PENDING, pending_id, expected_revision=row.revision)
            if approve is True:
                code = _credential()
                tx.put(CODES, 'code-' + _digest(code), {'client_id': row.payload['client_id'],
                    'actor_user_id': actor.user_id, 'issuer_device_id': actor.device_id,
                    'issuer_device_revision': actor.revision, 'target_user_id': target,
                    'config_revision': config.revision, 'params': row.payload['params'],
                    'scopes': row.payload['scopes'], 'consumed': False,
                    'expires_at': self.credentials.now() + self.policy['code_seconds']}, expected_revision=0)
                redirect = construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)
            elif approve is False:
                redirect = construct_redirect_uri(str(params.redirect_uri), error='access_denied', state=params.state)
            else:
                raise NativeMcpError('native_mcp_decision_invalid')
            tx.commit()
            return {'redirect_uri': redirect}

    def _grant(self, tx, value, client_id):
        config, origin = self.credentials.configuration(tx)
        actor = DeviceIdentity(value['issuer_device_id'], value['actor_user_id'], value['issuer_device_revision'])
        self.credentials.full_caller(tx, actor)
        self.credentials._target(tx, actor, value['target_user_id'])
        if (value['client_id'] != client_id or self._client(tx, client_id) is None
                or value['config_revision'] != config.revision
                or value['params']['resource'] != origin + '/mcp'):
            raise NativeMcpError('native_mcp_grant_invalid')
        return actor

    async def load_authorization_code(self, client, authorization_code):
        digest = _digest(authorization_code)
        if digest is None:
            return None
        try:
            with self.records.begin_read() as tx:
                row = tx.read(CODES, 'code-' + digest)
                if row is None or row.payload['consumed'] or row.payload['expires_at'] <= self.credentials.now():
                    return None
                self._grant(tx, row.payload, client.client_id)
                params = AuthorizationParams.model_validate(row.payload['params'])
                return AuthorizationCode(code=authorization_code, scopes=row.payload['scopes'],
                    expires_at=row.payload['expires_at'], client_id=client.client_id,
                    code_challenge=params.code_challenge, redirect_uri=params.redirect_uri,
                    redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly, resource=params.resource)
        except NativeMcpError:
            return None

    def _mint(self, tx, *, grant, actor, scopes, family_id=None):
        now = self.credentials.now()
        family_id = family_id or 'family-' + uuid4().hex
        result = self.credentials.issue_enlisted(tx, actor=actor, target=grant['target_user_id'],
            client='chatgpt', scopes=scopes, expires_at=now + self.policy['access_seconds'],
            family_id=family_id, name='ChatGPT')
        prior = tx.read(FAMILIES, family_id)
        refresh = _credential()
        value = {**grant, 'device_id': result['device']['device_id'], 'scopes': scopes,
            'expires_at': now + self.policy['refresh_seconds'] if prior is None else prior.payload['expires_at'],
            'revoked_at': None}
        tx.put(FAMILIES, family_id, value, expected_revision=prior.revision if prior else 0)
        tx.put(REFRESH, 'refresh-' + _digest(refresh), {'family_id': family_id,
            'client_id': grant['client_id'], 'spent': False, 'expires_at': value['expires_at']}, expected_revision=0)
        return OAuthToken(access_token=result['key'], token_type='Bearer',
            expires_in=self.policy['access_seconds'], refresh_token=refresh, scope=' '.join(scopes))

    async def exchange_authorization_code(self, client, authorization_code):
        try:
            with self.records.begin() as tx:
                row = tx.read(CODES, 'code-' + _digest(authorization_code.code))
                if row is None or row.payload['consumed'] or row.payload['expires_at'] <= self.credentials.now():
                    raise NativeMcpError('native_mcp_grant_invalid')
                actor = self._grant(tx, row.payload, client.client_id)
                params = AuthorizationParams.model_validate(row.payload['params'])
                if (authorization_code.resource != params.resource or authorization_code.client_id != client.client_id
                        or authorization_code.code_challenge != params.code_challenge
                        or authorization_code.redirect_uri != params.redirect_uri):
                    raise NativeMcpError('native_mcp_grant_invalid')
                tx.put(CODES, row.object_id, {**row.payload, 'consumed': True}, expected_revision=row.revision)
                result = self._mint(tx, grant=row.payload, actor=actor, scopes=row.payload['scopes'])
                tx.commit()
                return result
        except NativeMcpError:
            raise TokenError('invalid_grant', 'native_mcp_grant_invalid') from None

    def _revoke_family(self, tx, family):
        device = tx.read(DEVICES, family.payload['device_id'])
        if device is not None and device.payload['revoked_at'] is None:
            self.credentials.registry.revoke_enlisted(tx, device.payload['user_id'], device.object_id,
                expected_revision=device.revision)
        if family.payload['revoked_at'] is None:
            tx.put(FAMILIES, family.object_id, {**family.payload, 'revoked_at': self.credentials.now()},
                expected_revision=family.revision)

    def _family(self, tx, row, client_id):
        family = tx.read(FAMILIES, row.payload['family_id'])
        if (family is None or family.payload['revoked_at'] is not None
                or family.payload['expires_at'] <= self.credentials.now()
                or row.payload['client_id'] != client_id):
            raise NativeMcpError('native_mcp_grant_invalid')
        self._grant(tx, family.payload, client_id)
        device = tx.read(DEVICES, family.payload['device_id'])
        if device is None or device.payload['revoked_at'] is not None:
            raise NativeMcpError('native_mcp_grant_invalid')
        return family

    async def load_refresh_token(self, client, refresh_token):
        digest = _digest(refresh_token)
        if digest is None:
            return None
        try:
            with self.records.begin() as tx:
                row = tx.read(REFRESH, 'refresh-' + digest)
                if row is None or row.payload['client_id'] != client.client_id:
                    return None
                family = tx.read(FAMILIES, row.payload['family_id'])
                if row.payload['spent']:
                    if family is not None:
                        self._revoke_family(tx, family)
                        tx.commit()
                    return None
                family = self._family(tx, row, client.client_id)
                return RefreshToken(token=refresh_token, client_id=client.client_id,
                    scopes=family.payload['scopes'], expires_at=family.payload['expires_at'])
        except NativeMcpError:
            return None

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        validate_scopes(scopes)
        try:
            with self.records.begin() as tx:
                row = tx.read(REFRESH, 'refresh-' + _digest(refresh_token.token))
                if row is None:
                    raise NativeMcpError('native_mcp_grant_invalid')
                family = self._family(tx, row, client.client_id)
                if row.payload['spent']:
                    self._revoke_family(tx, family)
                    tx.commit()
                    raise NativeMcpError('native_mcp_grant_invalid')
                if not set(scopes).issubset(set(family.payload['scopes'])):
                    raise NativeMcpError('native_mcp_scope_invalid')
                siblings = [item for item in tx.list(REFRESH) if item.payload['family_id'] == family.object_id]
                if len(siblings) >= self.policy['refresh_tombstones_per_family']:
                    self._revoke_family(tx, family)
                    tx.commit()
                    raise NativeMcpError('native_mcp_refresh_limit')
                actor = self._grant(tx, family.payload, client.client_id)
                device = tx.read(DEVICES, family.payload['device_id'])
                self.credentials.registry.revoke_enlisted(tx, device.payload['user_id'], device.object_id,
                    expected_revision=device.revision)
                tx.put(REFRESH, row.object_id, {**row.payload, 'spent': True}, expected_revision=row.revision)
                result = self._mint(tx, grant=family.payload, actor=actor, scopes=scopes, family_id=family.object_id)
                tx.commit()
                return result
        except NativeMcpError:
            raise TokenError('invalid_grant', 'native_mcp_grant_invalid') from None

    async def load_access_token(self, token):
        try:
            lease = self.credentials.authenticate(token)
            if lease.restriction.family_id is None:
                return None
            row = self.records.read(FAMILIES, lease.restriction.family_id)
            return AccessToken(token=token, client_id=row.payload['client_id'], scopes=lease.restriction.scopes,
                expires_at=lease.restriction.expires_at, resource=lease.restriction.resource)
        except NativeMcpError:
            return None

    async def verify_token(self, token):
        return await self.load_access_token(token)

    async def revoke_token(self, token):
        with self.records.begin() as tx:
            if isinstance(token, RefreshToken):
                row = tx.read(REFRESH, 'refresh-' + _digest(token.token))
                family = tx.read(FAMILIES, row.payload['family_id']) if row else None
            else:
                matches = [row for row in tx.list(DEVICES) if row.payload.get('key_hash') == _digest(token.token)]
                restriction = tx.read(RESTRICTIONS, matches[0].object_id) if len(matches) == 1 else None
                family = tx.read(FAMILIES, restriction.payload['family_id']) if restriction else None
            if family is not None and family.payload['client_id'] == token.client_id:
                self._revoke_family(tx, family)
                tx.commit()


class NativeRelatedOwner:
    collections = frozenset({RESTRICTIONS, FAMILIES, OAUTH_CLIENTS, PENDING, CODES, REFRESH})

    def __init__(self, oauth):
        self.oauth = oauth

    def validate_related(self, tx, *, user_id, device_ids):
        for row in tx.list(RESTRICTIONS):
            self.oauth.credentials._restriction(row)
        required = {FAMILIES: {'actor_user_id', 'target_user_id', 'device_id', 'client_id', 'revoked_at'},
            OAUTH_CLIENTS: {'actor_user_id', 'client'}, PENDING: {'client_id', 'expires_at'},
            CODES: {'actor_user_id', 'target_user_id', 'issuer_device_id', 'client_id'},
            REFRESH: {'family_id', 'client_id', 'spent', 'expires_at'}}
        for collection, fields in required.items():
            for row in tx.list(collection):
                if not fields.issubset(row.payload):
                    raise NativeMcpError('native_mcp_related_data_invalid')

    def cleanup_related(self, tx, *, user_id, device_ids):
        self.validate_related(tx, user_id=user_id, device_ids=device_ids)
        clients = {row.object_id for row in tx.list(OAUTH_CLIENTS) if row.payload['actor_user_id'] == user_id}
        families = {row.object_id for row in tx.list(FAMILIES) if
            user_id in (row.payload['actor_user_id'], row.payload['target_user_id'])
            or row.payload['device_id'] in device_ids or row.payload['client_id'] in clients}
        for row in tx.list(FAMILIES):
            if row.object_id in families:
                self.oauth._revoke_family(tx, row)
        for row in tx.list(RESTRICTIONS):
            value = row.payload
            if (user_id in (value['actor_user_id'], value['target_user_id'])
                    or value['family_id'] in families or value['issuer_device_id'] in device_ids):
                device = tx.read(DEVICES, row.object_id)
                if device is not None and device.payload['revoked_at'] is None:
                    self.oauth.credentials.registry.revoke_enlisted(tx, device.payload['user_id'], device.object_id,
                        expected_revision=device.revision)
                tx.delete(RESTRICTIONS, row.object_id, expected_revision=row.revision)
        for collection in (REFRESH, CODES, PENDING, FAMILIES, OAUTH_CLIENTS):
            for row in tx.list(collection):
                value = row.payload
                if (row.object_id in clients or row.object_id in families or value.get('family_id') in families
                        or value.get('client_id') in clients
                        or user_id in (value.get('actor_user_id'), value.get('target_user_id'))
                        or value.get('issuer_device_id') in device_ids):
                    tx.delete(collection, row.object_id, expected_revision=row.revision)
