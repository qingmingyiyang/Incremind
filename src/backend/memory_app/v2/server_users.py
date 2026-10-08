"""One server user owner; identity never changes when selecting another space."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import re
from uuid import uuid4

from core.storage_provider import SQLiteStructuredRecordStore, SQLiteUnitOfWorkConflict
from backend.security.device_identity import DeviceIdentity
from backend.security.user_context import UserError, user_action_allowed

USERS = 'server_users'
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._~-]{0,127}\Z')
_UNSET = object()


def enabled_user(tx, user_id):
    row = tx.read(USERS, user_id)
    return row if row is not None and row.payload.get('disabled_at') is None else None


def ensure_install_user(tx, now):
    """Called inside the install pairing transaction; preserve the seed verbatim."""
    row = tx.read(USERS, 'local-user')
    if row is None:
        row = tx.put(USERS, 'local-user', {'user_id': 'local-user', 'name': '本机',
            'role': 'admin', 'disabled_at': None, 'storage_limit_mb': None,
            'job_minutes_per_day': None, 'created_at': now.isoformat()}, expected_revision=0)
    if row.payload.get('role') != 'admin' or row.payload.get('disabled_at') is not None:
        raise UserError('user_unauthorized')
    return row


def current_caller(tx, identity):
    if not isinstance(identity, DeviceIdentity):
        raise UserError('user_unauthorized')
    device = tx.read('server_devices', identity.device_id)
    user = enabled_user(tx, identity.user_id)
    if (user is None or device is None or device.payload.get('user_id') != identity.user_id
            or device.payload.get('revoked_at') is not None or device.revision != identity.revision):
        raise UserError('user_unauthorized')
    return user


def _public(row):
    return {**row.payload, 'revision': row.revision}


class ServerUsers:
    def __init__(self, server_root, *, records=None, clock=None):
        self.server_root = Path(server_root).resolve()
        self.records = records or SQLiteStructuredRecordStore(self.server_root / 'server' / 'devices.sqlite3')
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def get(self, user_id):
        if not isinstance(user_id, str) or not _ID.fullmatch(user_id):
            raise UserError('user_not_found')
        row = self.records.read(USERS, user_id)
        if row is None:
            raise UserError('user_not_found')
        return _public(row)

    def root_for(self, user_id):
        self.get(user_id)
        root = (self.server_root / 'users' / user_id).resolve()
        if not root.is_relative_to(self.server_root / 'users') or root == self.server_root / 'users':
            raise UserError('user_root_invalid')
        return root

    def _admin(self, tx, actor):
        caller = current_caller(tx, actor)
        if not user_action_allowed(caller.payload['role'], actor.user_id, actor.user_id, 'manage_users'):
            raise UserError('user_forbidden')
        return caller

    def authorize_claim(self, identity, target_user_id=None, *, action='space'):
        with self.records.begin() as tx:
            caller = current_caller(tx, identity)
            target = identity.user_id if target_user_id is None else target_user_id
            if not user_action_allowed(caller.payload['role'], identity.user_id, target, action):
                raise UserError('user_forbidden')
            if not isinstance(target, str) or not _ID.fullmatch(target) or tx.read(USERS, target) is None:
                raise UserError('user_not_found')
            return target, 'admin' if target != identity.user_id else None

    def can(self, identity, *, action):
        try:
            self.authorize_claim(identity, action=action)
            return True
        except UserError as error:
            if str(error) == 'user_forbidden':
                return False
            raise

    def list_for(self, actor):
        with self.records.begin() as tx:
            self._admin(tx, actor)
            return [_public(row) for row in tx.list(USERS)]

    def create(self, actor, *, name):
        if (not isinstance(name, str) or not name.strip() or len(name) > 80
                or any(ord(char) < 32 for char in name)):
            raise UserError('user_name_invalid')
        name = name.strip()
        with self.records.begin() as tx:
            self._admin(tx, actor)
            if any(row.payload['name'] == name for row in tx.list(USERS)):
                raise UserError('user_name_conflict')
            user_id = 'user-' + uuid4().hex
            root = self.server_root / 'users' / user_id
            root.parent.mkdir(parents=True, exist_ok=True)
            if root.parent.resolve() != self.server_root / 'users':
                raise UserError('user_root_invalid')
            root.mkdir()
            config = root / 'config' / 'settings.toml'
            config.parent.mkdir()
            config.write_bytes((Path(__file__).resolve().parents[4] / 'config' / 'settings.toml.example').read_bytes())
            row = tx.put(USERS, user_id, {'user_id': user_id, 'name': name, 'role': 'user',
                'disabled_at': None, 'storage_limit_mb': None, 'job_minutes_per_day': None,
                'created_at': self.clock().astimezone(timezone.utc).isoformat()}, expected_revision=0)
            tx.commit()
            return _public(row)

    def update(self, actor, user_id, *, expected_revision, disabled=_UNSET,
               storage_limit_mb=_UNSET, job_minutes_per_day=_UNSET):
        if type(expected_revision) is not int or expected_revision < 1:
            raise UserError('user_revision_invalid')
        for value in (storage_limit_mb, job_minutes_per_day):
            if value is not _UNSET and value is not None and (type(value) is not int or value < 0):
                raise UserError('user_quota_invalid')
        if disabled is not _UNSET and type(disabled) is not bool:
            raise UserError('user_request_invalid')
        self.get(user_id)
        try:
            with self.records.begin() as tx:
                self._admin(tx, actor)
                row = tx.read(USERS, user_id)
                payload = dict(row.payload)
                if disabled is not _UNSET:
                    payload['disabled_at'] = self.clock().astimezone(timezone.utc).isoformat() if disabled else None
                for field, value in (('storage_limit_mb', storage_limit_mb), ('job_minutes_per_day', job_minutes_per_day)):
                    if value is not _UNSET:
                        payload[field] = value
                result = tx.put(USERS, user_id, payload, expected_revision=expected_revision)
                tx.commit()
            return _public(result)
        except SQLiteUnitOfWorkConflict:
            raise UserError('user_revision_conflict') from None
