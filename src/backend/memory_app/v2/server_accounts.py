from datetime import timedelta
from argon2.exceptions import VerifyMismatchError, VerificationError, InvalidHashError
from fastapi import Request
from starlette.concurrency import run_in_threadpool

from backend.security.user_context import UserError
from core.storage_provider import SQLiteUnitOfWorkConflict
from .server_users import USERS, DELETIONS, enabled_user
from .devices import DEVICES
from .server_credentials import (CREDENTIALS, NAMES, LOGIN_DEVICES, ATTEMPTS, _NAME, _HASHER,
                                 _ARGON, prepare_credentials, store_credentials, login_public)


class ServerAccounts:
    def __init__(self, users, registry):
        self.users, self.registry = users, registry

    def reset(self, actor, user_id, *, login_name, password, expected_revision, expected_login_revision):
        if type(expected_revision) is not int or expected_revision < 1:
            raise UserError('user_revision_invalid')
        if type(expected_login_revision) is not int or expected_login_revision < 0:
            raise UserError('login_revision_invalid')
        # 哈希前先校验管理员身份，昂贵计算期间不持有 SQLite 写锁。
        with self.users.records.begin() as tx:
            self.users._admin(tx, actor)
        name, encoded = prepare_credentials(login_name, password)
        try:
            with self.users.records.begin() as tx:
                self.users._admin(tx, actor)
                row = tx.read(USERS, user_id)
                if row is None:
                    raise UserError('user_not_found')
                if tx.read(DELETIONS, user_id) is not None:
                    raise UserError('user_deletion_in_progress')
                if row.revision != expected_revision:
                    raise UserError('user_revision_conflict')
                store_credentials(tx, user_id, name, encoded, expected_login_revision)
                for issued in tx.list(LOGIN_DEVICES):
                    if issued.payload['user_id'] == user_id:
                        device = tx.read(DEVICES, issued.object_id)
                        if device is not None and device.payload['revoked_at'] is None:
                            tx.put(DEVICES, device.object_id, {**device.payload,
                                'revoked_at': self.registry._now().isoformat()}, expected_revision=device.revision)
                result = {**row.payload, 'revision': row.revision, **login_public(tx, user_id)}
                tx.commit()
                return result
        except SQLiteUnitOfWorkConflict:
            raise UserError('login_revision_conflict') from None

    def _reserve(self, login_name):
        now = self.registry._now()
        with self.users.records.begin() as tx:
            bucket = 'name-' + login_name if tx.read(NAMES, 'name-' + login_name) else 'unknown'
            for key, seconds, limit in (('global', 60, 60), (bucket, 900, 10)):
                row = tx.read(ATTEMPTS, key)
                value = row.payload if row else {}
                recent = value.get('until', '') > now.isoformat()
                count = value.get('count', 0) if recent else 0
                if count >= limit:
                    raise UserError('login_rate_limited')
                tx.put(ATTEMPTS, key, {'count': count + 1,
                    'until': value['until'] if recent else (now + timedelta(seconds=seconds)).isoformat()},
                    expected_revision=row.revision if row else 0)
            tx.commit()

    def login(self, login_name, password, *, device_name='网页'):
        if (not isinstance(login_name, str) or not _NAME.fullmatch(login_name)
                or not isinstance(password, str) or not 15 <= len(password) <= 256):
            raise UserError('login_failed')
        try:
            password.encode('utf-8')
        except UnicodeEncodeError:
            raise UserError('login_failed') from None
        name = login_name.casefold()
        self._reserve(name)
        if not _ARGON.acquire(blocking=False):
            raise UserError('login_rate_limited')
        try:
            with self.users.records.begin() as tx:
                index = tx.read(NAMES, 'name-' + name)
                row = tx.read(CREDENTIALS, index.payload['user_id']) if index else None
            if row is None:
                _HASHER.hash(password)
                raise UserError('login_failed')
            try:
                _HASHER.verify(row.payload['password_hash'], password)
            except (VerifyMismatchError, VerificationError, InvalidHashError):
                raise UserError('login_failed') from None
            with self.users.records.begin() as tx:
                current = tx.read(CREDENTIALS, row.object_id)
                if (current is None or current.revision != row.revision
                        or enabled_user(tx, row.object_id) is None):
                    raise UserError('login_failed')
                result = self.registry.issue_login_device(tx, user_id=row.object_id,
                    login_revision=row.revision, name=device_name)
                tx.commit()
                return result
        finally:
            _ARGON.release()


def install_account_routes(application, *, accounts):
    @application.post('/api/v2/auth/login')
    async def login(request: Request):
        from .server_control import body, response
        from backend.security.device_identity import server_mode
        if not server_mode(request):
            return response(404, {'detail': 'Not Found'})
        data = await body(request, {'login_name', 'password', 'device_name'}, {'login_name', 'password'})
        return response(200, await run_in_threadpool(accounts.login, **data))
