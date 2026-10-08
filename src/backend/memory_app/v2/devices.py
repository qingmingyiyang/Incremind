"""One server authority for device credentials, pairing and observed presence."""
from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import hashlib
import re
import secrets
from pathlib import Path
from uuid import uuid4
from io import BytesIO
import ipaddress

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from core.storage_provider import SQLiteStructuredRecordStore, SQLiteUnitOfWorkConflict
from backend.security.device_identity import DeviceIdentity, server_mode, server_identity


DEVICES = 'server_devices'
PAIRINGS = 'server_pairings'
PRESENCE = 'server_device_presence'
USERS = 'server_users'
_TOKEN = re.compile(r'[A-Za-z0-9_-]{43}\Z')
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._~-]{0,127}\Z')


class DeviceError(ValueError):
    """Fixed public code; credentials and input values never enter errors."""


def _digest(token):
    if not isinstance(token, str) or not _TOKEN.fullmatch(token):
        return None
    return hashlib.sha256(token.encode('ascii')).hexdigest()


def _credential():
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode('ascii').rstrip('=')


class DeviceRegistry:
    def __init__(self, directory: Path, *, clock=None):
        self.records = SQLiteStructuredRecordStore(Path(directory) / 'devices.sqlite3')
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _now(self):
        return self.clock().astimezone(timezone.utc)

    def _issuer(self, tx, user_id, actor):
        from .server_users import enabled_user
        from backend.security.user_context import user_action_allowed
        target = enabled_user(tx, user_id)
        if actor in {'install', 'desktop'}:
            return user_id == 'local-user' and (target is not None or tx.read(USERS, user_id) is None)
        row = tx.read(DEVICES, actor) if isinstance(actor, str) and _ID.fullmatch(actor) else None
        caller = enabled_user(tx, row.payload['user_id']) if row else None
        return bool(target and caller and row.payload['revoked_at'] is None
            and user_action_allowed(caller.payload.get('role'), row.payload['user_id'], user_id, 'pair_device'))

    def issue_pairing(self, *, user_id, actor):
        if not isinstance(user_id, str) or not _ID.fullmatch(user_id):
            raise DeviceError('device_unauthorized')
        now, code = self._now(), _credential()
        with self.records.begin() as tx:
            if not self._issuer(tx, user_id, actor):
                raise DeviceError('device_unauthorized')
            if actor == 'install':
                from .server_users import ensure_install_user, UserError
                try:
                    ensure_install_user(tx, now)
                except UserError:
                    raise DeviceError('device_unauthorized')
            tx.put(PAIRINGS, 'pair-' + _digest(code), {'user_id': user_id, 'issuer': actor,
                'created_at': now.isoformat(), 'expires_at': (now + timedelta(minutes=10)).isoformat(),
                'consumed_at': None, 'device_id': None}, expected_revision=0)
            tx.commit()
        return {'code': code, 'expires_at': (now + timedelta(minutes=10)).isoformat()}

    def exchange(self, code, *, name):
        digest = _digest(code)
        if digest is None:
            raise DeviceError('pairing_unavailable')
        if not isinstance(name, str) or not name.strip() or len(name) > 80 or any(ord(c) < 32 for c in name):
            raise DeviceError('device_name_invalid')
        now, key, device_id = self._now(), _credential(), 'device-' + uuid4().hex
        with self.records.begin() as tx:
            row = tx.read(PAIRINGS, 'pair-' + digest)
            if (row is None or row.payload['consumed_at'] is not None
                    or datetime.fromisoformat(row.payload['expires_at']) <= now
                    or not self._issuer(tx, row.payload['user_id'], row.payload['issuer'])):
                raise DeviceError('pairing_unavailable')
            device = tx.put(DEVICES, device_id, {'device_id': device_id,
                'user_id': row.payload['user_id'], 'name': name.strip(), 'key_hash': _digest(key),
                'created_at': now.isoformat(), 'last_seen_at': now.isoformat(), 'revoked_at': None}, expected_revision=0)
            tx.put(PAIRINGS, row.object_id, {**row.payload, 'consumed_at': now.isoformat(),
                'device_id': device_id}, expected_revision=row.revision)
            tx.commit()
        return {'device': self._public(device), 'key': key}

    def authenticate(self, key):
        digest = _digest(key)
        if digest is None:
            return None
        with self.records.begin() as tx:
            matches = [row for row in tx.list(DEVICES) if row.payload.get('key_hash') == digest]
            if len(matches) != 1 or matches[0].payload['revoked_at'] is not None:
                return None
            device = matches[0]
            user = tx.read(USERS, device.payload['user_id'])
            if user is None and device.payload['user_id'] != 'local-user' or user and user.payload.get('disabled_at') is not None:
                return None
            current = tx.read(PRESENCE, device.object_id)
            tx.put(PRESENCE, device.object_id, {'last_seen_at': self._now().isoformat()},
                expected_revision=current.revision if current else 0)
            tx.commit()
        return DeviceIdentity(device.object_id, device.payload['user_id'], device.revision)

    def is_current(self, identity):
        if not isinstance(identity, DeviceIdentity):
            return False
        row = self.records.read(DEVICES, identity.device_id)
        user = self.records.read(USERS, identity.user_id)
        active = (user is not None and user.payload.get('disabled_at') is None) or user is None and identity.user_id == 'local-user'
        return bool(active and row and row.revision == identity.revision
            and row.payload['user_id'] == identity.user_id and row.payload['revoked_at'] is None)

    def identity_for_device(self, device_id, user_id):
        if not isinstance(device_id, str) or not _ID.fullmatch(device_id) or not isinstance(user_id, str) or not _ID.fullmatch(user_id):
            return None
        row = self.records.read(DEVICES, device_id)
        if row is None or row.payload['user_id'] != user_id or row.payload['revoked_at'] is not None:
            return None
        identity = DeviceIdentity(device_id, user_id, row.revision)
        return identity if self.is_current(identity) else None

    def _public(self, row, presence=None):
        value = {key: value for key, value in row.payload.items() if key != 'key_hash'}
        if presence is not None:
            value['last_seen_at'] = presence.payload['last_seen_at']
        return {**value, 'revision': row.revision}

    def list_devices(self, user_id):
        with self.records.begin() as tx:
            return [self._public(row, tx.read(PRESENCE, row.object_id))
                for row in tx.list(DEVICES) if row.payload['user_id'] == user_id]

    def revoke(self, user_id, device_id, *, expected_revision):
        if not isinstance(device_id, str) or not _ID.fullmatch(device_id):
            raise DeviceError('device_not_found')
        if type(expected_revision) is not int or expected_revision < 1:
            raise DeviceError('device_revision_invalid')
        try:
            with self.records.begin() as tx:
                row = tx.read(DEVICES, device_id)
                if row is None or row.payload['user_id'] != user_id:
                    raise DeviceError('device_not_found')
                result = tx.put(DEVICES, device_id, {**row.payload, 'revoked_at': self._now().isoformat()},
                    expected_revision=expected_revision)
                tx.commit()
            return self._public(result)
        except SQLiteUnitOfWorkConflict:
            raise DeviceError('device_revision_conflict') from None


def _response(status, value):
    return JSONResponse(value, status_code=status, headers={'Cache-Control': 'no-store'})


def _desktop_local(request):
    host = request.client.host if request.client is not None else ''
    if host in {'localhost', 'testclient'}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _request_owner(request):
    if server_mode(request):
        identity = server_identity(request)
        if identity is None:
            raise DeviceError('device_unauthorized')
        from backend.security.user_context import USER_ACCESS
        access = USER_ACCESS.get()
        return (access.target_user_id if access is not None else identity.user_id), identity.device_id
    if not _desktop_local(request):
        raise DeviceError('device_unauthorized')
    return 'local-user', 'desktop'


def _qr(url):
    import qrcode
    output = BytesIO()
    qrcode.make(url, box_size=6, border=4).save(output, format='PNG')
    return 'data:image/png;base64,' + base64.b64encode(output.getvalue()).decode('ascii')


async def _body(request, fields):
    try:
        body = await request.json()
    except ValueError:
        raise DeviceError('device_request_invalid') from None
    if not isinstance(body, dict) or set(body) != fields:
        raise DeviceError('device_request_invalid')
    return body


def install_device_routes(application, *, registry):
    router = APIRouter(prefix='/api/v2/devices')

    @router.get('')
    def devices(request: Request):
        user_id, actor = _request_owner(request)
        items = registry.list_devices(user_id)
        mode = 'server' if server_mode(request) else 'desktop'
        if mode == 'desktop':
            items.insert(0, {'device_id': 'desktop', 'user_id': 'local-user', 'name': '这台电脑',
                'created_at': None, 'last_seen_at': None, 'revoked_at': None, 'revision': 0})
        return _response(200, {'mode': mode, 'device_id': actor, 'items': items})

    @router.post('/pair')
    async def pairing(request: Request):
        user_id, actor = _request_owner(request)
        await _body(request, set())
        result = registry.issue_pairing(user_id=user_id, actor=actor)
        origin = str(request.base_url).rstrip('/')
        url = origin + '/pair#code=' + result['code']
        return _response(200, {'expires_at': result['expires_at'], 'url': url, 'qr': _qr(url)})

    @router.post('/exchange')
    async def exchange(request: Request):
        body = await _body(request, {'code', 'name'})
        return _response(201, registry.exchange(body['code'], name=body['name']))

    @router.post('/{device_id}/revoke')
    async def revoke(device_id: str, request: Request):
        user_id, _ = _request_owner(request)
        body = await _body(request, {'expected_revision'})
        return _response(200, registry.revoke(user_id, device_id, expected_revision=body['expected_revision']))

    async def device_error(_request, error):
        status = {'pairing_unavailable': 401, 'device_unauthorized': 401,
            'device_not_found': 404, 'device_revision_conflict': 409}.get(str(error), 400)
        return _response(status, {'detail': str(error)})
    application.add_exception_handler(DeviceError, device_error)
    application.include_router(router)
