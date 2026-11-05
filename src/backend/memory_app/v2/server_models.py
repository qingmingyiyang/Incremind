from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import re
from urllib.parse import urlsplit
from uuid import uuid4

from core.storage_provider import SQLiteUnitOfWorkConflict
from backend.security.secrets import build_server_model_secret_store
from backend.security.user_context import UserError
from backend.shared.llm.model_prices import validate_rates
from .server_users import DELETIONS, USERS


PROFILES = 'server_model_profiles'
ALLOCATIONS = 'server_model_allocations'
METERING_PRICES = 'server_model_metering_prices'
SERVER_MODEL_PURPOSES = ('generation', 'embedding', 'rerank', 'vision', 'search', 'asr')
MODEL_PRICE_CURRENCY = 'CNY'
MODEL_PRICE_UNIT = 'million_tokens'
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._~-]{0,127}\Z')
_BINDING_FIELDS = frozenset({'schema_version', 'user_id', 'allocation_revision', 'mode',
    'purpose', 'profile_id', 'profile_revision', 'credential_generation'})
_PROFILE_FIELDS = frozenset({'name', 'purpose', 'provider', 'base_url', 'model', 'allowed_models', 'enabled', 'prices'})
_CHANGES = _PROFILE_FIELDS | {'api_key', 'clear_api_key'}


class ServerModelError(ValueError):
    pass


def _fail(code):
    raise ServerModelError(code)


def _revision(value, *, zero=False, code='server_model_revision_invalid'):
    if type(value) is not int or value < (0 if zero else 1):
        _fail(code)
    return value


def _identity(value):
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def _purpose(value):
    if not isinstance(value, str) or value not in SERVER_MODEL_PURPOSES:
        _fail('server_model_purpose_invalid')
    return value


def _text(value, maximum):
    if (not isinstance(value, str) or not value.strip() or len(value) > maximum
            or any(ord(char) < 32 for char in value)):
        _fail('server_model_profile_invalid')
    return value.strip()


def _profile_fields(data):
    if set(data) != _PROFILE_FIELDS:
        _fail('server_model_profile_invalid')
    result = dict(data)
    result['name'] = _text(result['name'], 80)
    result['purpose'] = _purpose(result['purpose'])
    result['provider'] = _text(result['provider'], 128)
    if not _identity(result['provider']):
        _fail('server_model_profile_invalid')
    result['base_url'] = _text(result['base_url'], 2048).rstrip('/')
    try:
        address = urlsplit(result['base_url'])
        if (address.scheme != 'https' or not address.hostname or address.username or address.password
                or address.query or address.fragment or address.port is not None and not 1 <= address.port <= 65535):
            _fail('server_model_endpoint_invalid')
    except ValueError:
        _fail('server_model_endpoint_invalid')
    result['model'] = _text(result['model'], 200)
    allowed = result['allowed_models']
    if not isinstance(allowed, list) or not 1 <= len(allowed) <= 100:
        _fail('server_model_profile_invalid')
    allowed = [_text(model, 200) for model in allowed]
    if len(set(allowed)) != len(allowed) or result['model'] not in allowed or type(result['enabled']) is not bool:
        _fail('server_model_profile_invalid')
    result['allowed_models'] = allowed
    prices = result['prices']
    if not isinstance(prices, dict) or set(prices) - set(allowed):
        _fail('server_model_prices_invalid')
    try:
        result['prices'] = {model: validate_rates(rates) for model, rates in prices.items()}
    except ValueError:
        _fail('server_model_prices_invalid')
    return result


def validate_server_model_binding(value):
    if not isinstance(value, Mapping) or set(value) != _BINDING_FIELDS:
        _fail('server_model_binding_invalid')
    binding = dict(value)
    if (type(binding['schema_version']) is not int or binding['schema_version'] != 1
            or not _identity(binding['user_id']) or not isinstance(binding['mode'], str)
            or binding['mode'] not in {'independent', 'shared'}):
        _fail('server_model_binding_invalid')
    if binding['purpose'] not in SERVER_MODEL_PURPOSES:
        _fail('server_model_binding_invalid')
    for key in ('allocation_revision', 'profile_revision', 'credential_generation'):
        _revision(binding[key], zero=True, code='server_model_binding_invalid')
    if binding['mode'] == 'independent':
        if binding['profile_id'] is not None or binding['profile_revision'] != 0 or binding['credential_generation'] != 0:
            _fail('server_model_binding_invalid')
    elif (not _identity(binding['profile_id']) or binding['allocation_revision'] < 1
            or binding['profile_revision'] < 1 or binding['credential_generation'] < 1):
        _fail('server_model_binding_invalid')
    return binding


class ServerModels:
    def __init__(self, users, *, secrets=None, clock=None):
        self.users, self.records = users, users.records
        self.server_root = users.server_root
        self._secrets = secrets if secrets is not None else build_server_model_secret_store(self.server_root)
        self._secret_path = self.server_root / 'server' / 'model-secrets.json'
        if Path(getattr(self._secrets, '_path', '')).resolve() != self._secret_path:
            _fail('server_model_secret_store_invalid')
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _admin(self, tx, actor):
        try:
            return self.users._admin(tx, actor)
        except UserError as error:
            _fail('server_model_forbidden' if str(error) == 'user_forbidden' else 'server_model_unauthorized')

    def _user(self, tx, user_id, *, enabled=False):
        if not _identity(user_id):
            _fail('server_model_user_not_found')
        row = tx.read(USERS, user_id)
        if row is None:
            _fail('server_model_user_not_found')
        if tx.read(DELETIONS, user_id) is not None:
            _fail('server_model_user_deleting')
        if enabled and row.payload.get('disabled_at') is not None:
            _fail('server_model_user_disabled')
        return row

    def _profile(self, tx, profile_id):
        if not _identity(profile_id):
            _fail('server_model_profile_not_found')
        row = tx.read(PROFILES, profile_id)
        if row is None:
            _fail('server_model_profile_not_found')
        return row

    def _secret(self, profile):
        if not profile.get('secret_ref'):
            return None
        if self._secret_path.is_symlink() or self._secret_path.parent.resolve() != self._secret_path.parent:
            _fail('server_model_secret_unavailable')
        try:
            return self._secrets.get_snapshot(profile['secret_ref'])
        except Exception:
            _fail('server_model_secret_unavailable')

    def _public_profile(self, row):
        profile = row.payload
        secret = self._secret(profile)
        has_key = bool(secret is not None and secret.value
            and secret.generation == profile['credential_generation'])
        return {key: deepcopy(profile[key]) for key in (*_PROFILE_FIELDS, 'profile_id', 'created_at', 'updated_at')} | {
            'revision': row.revision, 'credential_generation': profile['credential_generation'],
            'has_api_key': has_key, 'configured': has_key and profile['enabled']}

    def list_profiles(self, actor):
        with self.records.begin_read() as tx:
            self._admin(tx, actor)
            return [self._public_profile(row) for row in tx.list(PROFILES)]

    def get_profile(self, actor, profile_id):
        with self.records.begin_read() as tx:
            self._admin(tx, actor)
            return self._public_profile(self._profile(tx, profile_id))

    @staticmethod
    def _metering_identity(profile):
        return {key: deepcopy(profile[key]) for key in
                ('purpose', 'provider', 'base_url', 'model', 'allowed_models')}

    def metering_prices(self, actor, profile_id):
        with self.records.begin_read() as tx:
            self._admin(tx, actor)
            profile = self._profile(tx, profile_id)
            row = tx.read(METERING_PRICES, profile_id)
            current = row is not None and row.payload['configuration'] == self._metering_identity(profile.payload)
            return {'profile_id': profile_id, 'profile_revision': profile.revision,
                'revision': row.revision if row else 0, 'currency': 'CNY',
                'models': deepcopy(row.payload['models']) if current else {}, 'current': current,
                'configuration': self._metering_identity(profile.payload)}

    def update_metering_prices(self, actor, profile_id, *, models, expected_revision, expected_profile_revision):
        from .policies.shared_metering import validate_price
        _revision(expected_revision, zero=True)
        _revision(expected_profile_revision)
        if not isinstance(models, dict):
            _fail('server_model_metering_price_invalid')
        try:
            prices = {model: validate_price(price) for model, price in models.items()}
        except ValueError:
            _fail('server_model_metering_price_invalid')
        try:
            with self.records.begin() as tx:
                self._admin(tx, actor)
                profile = self._profile(tx, profile_id)
                if profile.revision != expected_profile_revision:
                    _fail('server_model_revision_conflict')
                units = {'embedding': {'input_tokens'}, 'rerank': {'input_tokens'},
                    'search': {'search_tokens_calls', 'search_requests'}, 'asr': {'audio_seconds'}}.get(profile.payload['purpose'])
                if (units is None or set(prices) - set(profile.payload['allowed_models'])
                        or any(price['unit'] not in units for price in prices.values())):
                    _fail('server_model_metering_price_invalid')
                tx.put(METERING_PRICES, profile_id, {'configuration': self._metering_identity(profile.payload),
                    'models': prices}, expected_revision=expected_revision)
                tx.commit()
        except SQLiteUnitOfWorkConflict:
            _fail('server_model_revision_conflict')
        return self.metering_prices(actor, profile_id)

    def metering_price(self, reader, profile_id, model):
        profile = self._profile(reader, profile_id)
        row = reader.read(METERING_PRICES, profile_id)
        if (row is None or row.payload['configuration'] != self._metering_identity(profile.payload)
                or model not in row.payload['models']):
            _fail('server_model_metering_price_unavailable')
        return {'price': deepcopy(row.payload['models'][model]), 'revision': row.revision}

    def _new_secret(self, data):
        value = data.get('api_key', '')
        clear = data.get('clear_api_key', False)
        if (not isinstance(value, str) or len(value) > 8192 or type(clear) is not bool
                or value.strip() and clear or any(ord(char) < 32 for char in value)):
            _fail('server_model_key_invalid')
        if not value.strip():
            return None
        reference = 'server-model-' + uuid4().hex
        if self._secret_path.is_symlink() or self._secret_path.parent.resolve() != self._secret_path.parent:
            _fail('server_model_secret_unavailable')
        try:
            # 新slot只有经profile指针提交后才取得资格，孤立slot不能用于发送。
            self._secrets.set(reference, value.strip())
            snapshot = self._secrets.get_snapshot(reference)
            if snapshot.value != value.strip() or type(snapshot.generation) is not int or snapshot.generation < 1:
                _fail('server_model_secret_unavailable')
        except Exception:
            _fail('server_model_secret_unavailable')
        return reference, snapshot.generation

    def create_profile(self, actor, **data):
        if set(data) - (_PROFILE_FIELDS | {'api_key'}):
            _fail('server_model_profile_invalid')
        fields = _profile_fields({key: data[key] for key in _PROFILE_FIELDS if key in data}
            | ({} if 'prices' in data else {'prices': {}}))
        now = self.clock().astimezone(timezone.utc).isoformat()
        profile_id = 'model-' + uuid4().hex
        with self.records.begin() as tx:
            self._admin(tx, actor)
            secret = self._new_secret(data)
            row = tx.put(PROFILES, profile_id, {**fields, 'profile_id': profile_id,
                'secret_ref': secret[0] if secret else '', 'credential_generation': secret[1] if secret else 0,
                'created_at': now, 'updated_at': now}, expected_revision=0)
            public = self._public_profile(row)
            # 只有秘密已持久化且真实SQLite提交成功，才能返回配置成功。
            tx.commit()
        return public

    def update_profile(self, actor, profile_id, *, expected_revision, **changes):
        _revision(expected_revision)
        if not changes or set(changes) - _CHANGES:
            _fail('server_model_profile_invalid')
        try:
            with self.records.begin() as tx:
                self._admin(tx, actor)
                current = self._profile(tx, profile_id)
                if current.revision != expected_revision:
                    _fail('server_model_revision_conflict')
                payload = dict(current.payload)
                fields = _profile_fields({key: changes.get(key, payload[key]) for key in _PROFILE_FIELDS})
                if fields['purpose'] != payload['purpose']:
                    _fail('server_model_profile_purpose_immutable')
                secret = self._new_secret(changes)
                if secret is not None:
                    payload.update(secret_ref=secret[0], credential_generation=secret[1])
                elif changes.get('clear_api_key') is True:
                    payload.update(secret_ref='', credential_generation=0)
                payload.update(fields, updated_at=self.clock().astimezone(timezone.utc).isoformat())
                row = tx.put(PROFILES, profile_id, payload, expected_revision=expected_revision)
                public = self._public_profile(row)
                tx.commit()
            return public
        except SQLiteUnitOfWorkConflict:
            _fail('server_model_revision_conflict')

    def _allocation(self, tx, user_id):
        row = tx.read(ALLOCATIONS, user_id)
        if row is None:
            return {'user_id': user_id, 'mode': 'independent', 'profiles': {}, 'revision': 0}
        return {**deepcopy(row.payload), 'revision': row.revision}

    def allocation(self, actor, user_id):
        with self.records.begin_read() as tx:
            self._admin(tx, actor)
            user = self._user(tx, user_id)
            return {**self._allocation(tx, user_id), 'user_revision': user.revision}

    def update_allocation(self, actor, user_id, *, expected_revision, expected_user_revision, mode, profiles):
        _revision(expected_revision, zero=True)
        _revision(expected_user_revision)
        if (not isinstance(mode, str) or mode not in {'independent', 'shared'} or not isinstance(profiles, dict)
                or any(not isinstance(purpose, str) or purpose not in SERVER_MODEL_PURPOSES
                       or not _identity(profile_id) for purpose, profile_id in profiles.items())
                or mode == 'independent' and profiles or mode == 'shared' and not profiles):
            _fail('server_model_allocation_invalid')
        try:
            with self.records.begin() as tx:
                self._admin(tx, actor)
                user = self._user(tx, user_id)
                if user.revision != expected_user_revision:
                    _fail('server_model_user_revision_conflict')
                current = self._allocation(tx, user_id)
                if current['revision'] != expected_revision:
                    _fail('server_model_revision_conflict')
                for purpose, profile_id in profiles.items():
                    profile = self._profile(tx, profile_id)
                    if profile.payload['purpose'] != purpose:
                        _fail('server_model_allocation_purpose_mismatch')
                    if not self._public_profile(profile)['configured']:
                        _fail('server_model_profile_unavailable')
                saved = tx.put(ALLOCATIONS, user_id, {'user_id': user_id, 'mode': mode,
                    'profiles': dict(profiles)}, expected_revision=expected_revision)
                tx.commit()
                return {**deepcopy(saved.payload), 'revision': saved.revision, 'user_revision': user.revision}
        except SQLiteUnitOfWorkConflict:
            _fail('server_model_revision_conflict')

    def _root(self, user_id, user_root):
        if not _identity(user_id):
            _fail('server_model_user_not_found')
        expected = self.server_root / 'users' / user_id
        if (expected.resolve() != expected or Path(user_root).resolve() != expected
                or not expected.is_dir()):
            _fail('server_model_user_root_invalid')
        return expected

    def access_for(self, user_id, user_root):
        root = self._root(user_id, user_root)
        with self.records.begin_read() as tx:
            self._user(tx, user_id, enabled=True)
        return ServerModelAccess(self, user_id, root)


@dataclass(frozen=True)
class ServerModelAccess:
    _owner: ServerModels = field(repr=False)
    user_id: str
    user_root: Path

    def _selection(self, purpose, reader):
        _purpose(purpose)
        self._owner._root(self.user_id, self.user_root)
        self._owner._user(reader, self.user_id, enabled=True)
        allocation = self._owner._allocation(reader, self.user_id)
        binding = {'schema_version': 1, 'user_id': self.user_id,
            'allocation_revision': allocation['revision'], 'mode': allocation['mode'], 'purpose': purpose,
            'profile_id': None, 'profile_revision': 0, 'credential_generation': 0}
        configuration = None
        if allocation['mode'] == 'shared':
            profile_id = allocation['profiles'].get(purpose)
            if profile_id is None:
                _fail('server_model_purpose_unavailable')
            row = self._owner._profile(reader, profile_id)
            if row.payload['purpose'] != purpose:
                _fail('server_model_allocation_purpose_mismatch')
            configuration = self._owner._public_profile(row)
            if not configuration['enabled']:
                _fail('server_model_profile_disabled')
            if not configuration['has_api_key']:
                _fail('server_model_credential_unavailable')
            binding.update(profile_id=profile_id, profile_revision=row.revision,
                credential_generation=row.payload['credential_generation'])
        validate_server_model_binding(binding)
        return {'mode': allocation['mode'], 'purpose': purpose, 'configuration': configuration,
            'server_model_binding': binding}

    def selection(self, purpose, *, reader=None):
        if reader is not None:
            return self._selection(purpose, reader)
        with self._owner.records.begin_read() as tx:
            return self._selection(purpose, tx)

    def public_selection(self, purpose, *, reader=None):
        if reader is None:
            with self._owner.records.begin_read() as tx:
                return self.public_selection(purpose, reader=tx)
        try:
            selection = self._selection(purpose, reader)
        except ServerModelError as error:
            code = str(error)
            if code not in {'server_model_purpose_unavailable', 'server_model_profile_disabled',
                            'server_model_credential_unavailable'}:
                raise
            allocation = self._owner._allocation(reader, self.user_id)
            profile_id = allocation['profiles'].get(purpose)
            configuration = (self._owner._public_profile(self._owner._profile(reader, profile_id))
                if profile_id is not None else None)
            return {'mode': allocation['mode'], 'purpose': purpose, 'configuration': configuration,
                'server_model_binding': None, 'available': False, 'unavailable_reason': code}
        return {**selection, 'available': True, 'unavailable_reason': None}

    def validate_binding(self, binding, *, reader=None):
        expected = validate_server_model_binding(binding)
        if expected['user_id'] != self.user_id:
            _fail('server_model_binding_invalid')
        current = self.selection(expected['purpose'], reader=reader)
        if current['server_model_binding'] != expected:
            _fail('server_model_binding_changed')
        return current

    def credential(self, binding):
        # 只在可信发送接点调用；公开路由没有凭据读取入口。
        with self._owner.records.begin() as tx:
            current = self.validate_binding(binding, reader=tx)
            if current['mode'] != 'shared':
                _fail('server_model_independent')
            row = self._owner._profile(tx, current['server_model_binding']['profile_id'])
            snapshot = self._owner._secret(row.payload)
            if snapshot is None or not snapshot.value or snapshot.generation != binding['credential_generation']:
                _fail('server_model_binding_changed')
            return snapshot
