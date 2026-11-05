from dataclasses import dataclass
from urllib.parse import urlsplit

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, ValidationError

from backend.security.device_identity import DeviceIdentity
from backend.security.user_context import user_action_allowed
from .devices import DEVICES, _digest
from .server_users import current_caller, enabled_user, UserError
from .native_mcp_scope import NativeMcpError, SCOPES, validate_scopes


RESTRICTIONS = 'v2_native_mcp_device_scopes'
CONFIG = 'v2_native_mcp_configuration'
FAMILIES = 'v2_native_mcp_oauth_families'
CLIENTS = frozenset({'claude', 'codex', 'deepseek-harness', 'chatgpt'})
PREFIX = 'native-mcp-device-'


class Restriction(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    device_id: str
    actor_user_id: str
    issuer_device_id: str
    target_user_id: str
    client: str
    scopes: list[str]
    expires_at: int = Field(gt=0)
    resource: str
    config_revision: int = Field(gt=0)
    family_id: str | None


@dataclass(frozen=True)
class NativeLease:
    owner: object
    identity: DeviceIdentity
    restriction_revision: int
    restriction: Restriction
    tool: str | None = None

    def validate(self):
        with self.owner.records.begin_read() as tx:
            return self.owner.validate_enlisted(tx, self)


class NativeCredentials:
    def __init__(self, registry, *, allow_loopback=False):
        self.registry, self.records = registry, registry.records
        self.allow_loopback = allow_loopback

    def now(self):
        return int(self.registry._now().timestamp())

    def configuration(self, tx):
        row = tx.read(CONFIG, 'canonical')
        if row is None or set(row.payload) != {'origin'}:
            raise NativeMcpError('native_mcp_not_configured')
        origin = row.payload['origin']
        if not isinstance(origin, str):
            raise NativeMcpError('native_mcp_configuration_invalid')
        try:
            canonical = AnyHttpUrl(origin)
            parsed = urlsplit(origin)
            parsed.port
        except ValueError:
            raise NativeMcpError('native_mcp_configuration_invalid') from None
        if (parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment
                or parsed.path or not parsed.hostname or origin != origin.rstrip('/')
                or str(canonical).rstrip('/') != origin
                or (parsed.scheme != 'https' and not (self.allow_loopback and parsed.scheme == 'http'
                    and parsed.hostname in {'localhost', '127.0.0.1', '[::1]', '::1'}))):
            raise NativeMcpError('native_mcp_configuration_invalid')
        return row, origin

    def configure(self, actor, *, origin, expected_revision):
        from .server_users import UserError
        with self.records.begin() as tx:
            caller = self.full_caller(tx, actor)
            if type(expected_revision) is not int or expected_revision < 0:
                raise NativeMcpError('native_mcp_request_invalid')
            if caller.payload['role'] != 'admin':
                raise UserError('user_forbidden')
            if isinstance(origin, str):
                origin = origin.rstrip('/')
            row = tx.put(CONFIG, 'canonical', {'origin': origin}, expected_revision=expected_revision)
            self.configuration(tx)
            tx.commit()
            return {'origin': origin, 'revision': row.revision}

    def full_caller(self, tx, identity):
        if not isinstance(identity, DeviceIdentity) or identity.device_id.startswith(PREFIX):
            raise NativeMcpError('native_mcp_human_required')
        if tx.read(RESTRICTIONS, identity.device_id) is not None:
            raise NativeMcpError('native_mcp_human_required')
        try:
            return current_caller(tx, identity)
        except UserError:
            raise NativeMcpError('native_mcp_credential_invalid') from None

    def _target(self, tx, identity, target):
        try:
            caller = current_caller(tx, identity)
        except UserError:
            raise NativeMcpError('native_mcp_credential_invalid') from None
        if (enabled_user(tx, target) is None
                or not user_action_allowed(caller.payload['role'], identity.user_id, target, 'space')):
            raise NativeMcpError('native_mcp_target_forbidden')
        return caller

    def issue_enlisted(self, tx, *, actor, target, client, scopes, expires_at, family_id=None, name):
        self.full_caller(tx, actor)
        self._target(tx, actor, target)
        if not isinstance(client, str) or client not in CLIENTS or type(expires_at) is not int or expires_at <= self.now():
            raise NativeMcpError('native_mcp_credential_invalid')
        scopes = validate_scopes(scopes)
        config, origin = self.configuration(tx)
        result = self.registry.issue_native_mcp_device(tx, actor=actor, name=name)
        device = result['device']
        value = Restriction(device_id=device['device_id'], actor_user_id=actor.user_id,
            issuer_device_id=actor.device_id, target_user_id=target, client=client, scopes=scopes,
            expires_at=expires_at, resource=origin + '/mcp', config_revision=config.revision, family_id=family_id)
        tx.put(RESTRICTIONS, device['device_id'], value.model_dump(), expected_revision=0)
        return result

    def issue(self, *, actor, target, client, scopes, expires_at, name):
        with self.records.begin() as tx:
            result = self.issue_enlisted(tx, actor=actor, target=target, client=client, scopes=scopes,
                expires_at=expires_at, name=name)
            tx.commit()
            return result

    def _restriction(self, row):
        try:
            value = Restriction.model_validate(row.payload)
        except ValidationError:
            raise NativeMcpError('native_mcp_credential_invalid') from None
        validate_scopes(value.scopes)
        if (value.device_id != row.object_id or not row.object_id.startswith(PREFIX)
                or value.client not in CLIENTS):
            raise NativeMcpError('native_mcp_credential_invalid')
        return value

    def validate_enlisted(self, tx, lease):
        if lease.owner is not self:
            raise NativeMcpError('native_mcp_credential_invalid')
        row = tx.read(RESTRICTIONS, lease.identity.device_id)
        if row is None or row.revision != lease.restriction_revision:
            raise NativeMcpError('native_mcp_credential_invalid')
        value = self._restriction(row)
        config, origin = self.configuration(tx)
        if (value != lease.restriction or value.actor_user_id != lease.identity.user_id
                or value.expires_at <= self.now() or value.resource != origin + '/mcp'
                or value.config_revision != config.revision):
            raise NativeMcpError('native_mcp_credential_invalid')
        self._target(tx, lease.identity, value.target_user_id)
        if value.family_id is not None:
            family = tx.read(FAMILIES, value.family_id)
            if (family is None or family.payload.get('revoked_at') is not None
                    or family.payload.get('device_id') != value.device_id
                    or family.payload.get('actor_user_id') != value.actor_user_id
                    or family.payload.get('target_user_id') != value.target_user_id
                    or family.payload.get('config_revision') != value.config_revision):
                raise NativeMcpError('native_mcp_credential_invalid')
        if lease.tool is not None and not any(lease.tool in SCOPES[scope] for scope in value.scopes):
            raise NativeMcpError('native_mcp_insufficient_scope')
        return value

    def authenticate(self, token, *, tool=None):
        digest = _digest(token)
        if digest is None:
            raise NativeMcpError('native_mcp_credential_invalid')
        with self.records.begin_read() as tx:
            matches = [row for row in tx.list(DEVICES) if row.payload.get('key_hash') == digest]
            if len(matches) != 1:
                raise NativeMcpError('native_mcp_credential_invalid')
            device = matches[0]
            row = tx.read(RESTRICTIONS, device.object_id)
            if row is None:
                raise NativeMcpError('native_mcp_credential_invalid')
            lease = NativeLease(self, DeviceIdentity(device.object_id, device.payload['user_id'], device.revision),
                row.revision, self._restriction(row), tool)
            self.validate_enlisted(tx, lease)
            return lease

    def is_restricted_token(self, token):
        digest = _digest(token)
        if digest is None:
            return False
        with self.records.begin_read() as tx:
            return any(row.payload.get('key_hash') == digest and
                (row.object_id.startswith(PREFIX) or tx.read(RESTRICTIONS, row.object_id) is not None)
                for row in tx.list(DEVICES))
