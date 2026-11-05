import base64
from datetime import datetime, timedelta
import hmac
import os
from pathlib import Path
import re
from urllib.parse import unquote_to_bytes
from uuid import uuid4

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from starlette.datastructures import Headers

from .android_attestation import AndroidAttestation
from .devices import DEVICES, PRESENCE, _credential, _digest, _qr
from .server_users import enabled_user


INVITES = 'server_mobile_invites'
CERTS = 'server_mobile_certificates'
PROXY_HEADER = 'x-incremind-mobile-proxy'
CERT_HEADER = 'x-incremind-client-cert'
BOOTSTRAP = frozenset({'/api/v2/mobile/challenge', '/api/v2/mobile/activate'})
RENEW = '/api/v2/mobile/renew'
_LIFETIME = timedelta(days=90)
_RENEW_WINDOW = timedelta(days=30)


class MobileError(ValueError):
    pass


def _expires(value):
    return datetime.fromisoformat(value)


def _spki(public_key):
    return public_key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)


def _denied():
    raise PermissionError('mobile_device_unauthorized')


def delete_mobile_device_related(tx, device_id):
    certificate = tx.read(CERTS, device_id)
    if certificate is not None:
        tx.delete(CERTS, device_id, expected_revision=certificate.revision)
    for invite in tx.list(INVITES):
        if device_id in (invite.payload.get('issuer'), invite.payload.get('device_id')):
            tx.delete(INVITES, invite.object_id, expected_revision=invite.revision)


class AndroidMobileRelatedOwner:
    collections = frozenset({INVITES, CERTS})

    def validate_related(self, tx, *, user_id, device_ids):
        for collection in self.collections:
            for row in tx.list(collection):
                if not isinstance(row.payload.get('user_id'), str):
                    raise MobileError('mobile_related_identity_invalid')

    def cleanup_related(self, tx, *, user_id, device_ids):
        self.validate_related(tx, user_id=user_id, device_ids=device_ids)
        for collection in self.collections:
            for row in tx.list(collection):
                if (row.payload['user_id'] == user_id or row.object_id in device_ids
                        or row.payload.get('issuer') in device_ids):
                    tx.delete(collection, row.object_id, expected_revision=row.revision)


class AndroidMobile:
    bootstrap_paths = BOOTSTRAP

    @staticmethod
    def permits(path, method):
        if path.startswith('/api/v2/server/'):
            return path == '/api/v2/server/users' and method == 'GET'
        return not (path.startswith(('/api/v2/native-mcp/', '/api/rebuild/workbench/realtime-asr/')) or path in BOOTSTRAP
            or path in {'/api/v2/devices/pair', '/api/v2/auth/login', '/api/v2/devices/exchange'})

    @classmethod
    def configured(cls, users, registry):
        location = os.environ.get('CHRIPTMAS_ANDROID_MOBILE_DIR')
        return cls(users, registry, Path(location).resolve(strict=True)) if location else None

    def __init__(self, users, registry, directory):
        self.users, self.registry = users, registry
        self.ca = x509.load_pem_x509_certificate((directory / 'client-ca.pem').read_bytes())
        self.key = serialization.load_pem_private_key((directory / 'client-ca.key').read_bytes(), password=None)
        self.proxy = (directory / 'proxy-token').read_text('ascii').strip()
        package = (directory / 'android-package.txt').read_text('ascii').strip()
        signing_digest = (directory / 'android-signer-sha256.txt').read_text('ascii').strip()
        if (_spki(self.ca.public_key()) != _spki(self.key.public_key())
                or not self.ca.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
                or not re.fullmatch(r'[A-Za-z0-9_-]{43,128}', self.proxy)
                or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+', package)
                or not re.fullmatch(r'[A-Fa-f0-9]{64}', signing_digest)):
            raise ValueError('mobile_configuration_invalid')
        self.ca.verify_directly_issued_by(self.ca)
        self.attestation = AndroidAttestation(roots=directory / 'android-attestation-roots.pem',
            revocations=directory / 'android-attestation-revocations.json', package=package,
            signing_digest=bytes.fromhex(signing_digest))

    def issue_invite(self, actor, user_id):
        now, code = self.registry._now(), _credential()
        expires = (now + timedelta(minutes=10)).isoformat()
        with self.registry.records.begin() as tx:
            self.users._admin(tx, actor)
            user = enabled_user(tx, user_id)
            if user is None or user.payload['role'] != 'user':
                raise MobileError('mobile_invite_user_invalid')
            for row in tx.list(INVITES):
                if _expires(row.payload['expires_at']) <= now:
                    tx.delete(INVITES, row.object_id, expected_revision=row.revision)
            tx.put(INVITES, _digest(code), {'user_id': user_id, 'issuer': actor.device_id,
                'expires_at': expires, 'challenge_id': None, 'challenge': None, 'used': False}, expected_revision=0)
            tx.commit()
        return {'code': code, 'expires_at': expires, 'qr': _qr('incremind-mobile:v1:' + code)}

    def _invite(self, tx, code, now):
        identity = _digest(code)
        row = tx.read(INVITES, identity) if identity else None
        if (row is None or row.payload['used'] or _expires(row.payload['expires_at']) <= now
                or not self.registry._issuer(tx, row.payload['user_id'], row.payload['issuer'])):
            raise MobileError('mobile_invite_unavailable')
        return row

    def challenge(self, code):
        now = self.registry._now()
        with self.registry.records.begin() as tx:
            row = self._invite(tx, code, now)
            payload = row.payload
            if payload['challenge_id'] is None:
                payload = {**payload, 'challenge_id': uuid4().hex, 'challenge': _credential()}
                tx.put(INVITES, row.object_id, payload, expected_revision=row.revision)
                tx.commit()
            return {field: payload[field] for field in ('challenge_id', 'challenge')}

    def _sign(self, device_id, user_id, key, now):
        expires_at = min(now + _LIFETIME, self.ca.not_valid_after_utc)
        if not self.ca.not_valid_before_utc <= now or expires_at <= now + _RENEW_WINDOW:
            raise MobileError('mobile_ca_expiring')
        cert = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, device_id)]))
            .issuer_name(self.ca.subject).public_key(key).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5)).not_valid_after(expires_at)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(True, False, False, False, False, False, False, False, False), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=True)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(self.ca.public_key()), critical=False)
            .sign(self.key, hashes.SHA256()))
        return {'device_id': device_id, 'user_id': user_id,
            'certificate': cert.public_bytes(serialization.Encoding.PEM).decode('ascii'),
            'expires_at': cert.not_valid_after_utc.isoformat()}

    def activate(self, *, code, challenge_id, device_name, attestation_chain):
        if (not isinstance(challenge_id, str) or not re.fullmatch(r'[a-f0-9]{32}', challenge_id)
                or not isinstance(device_name, str) or not 1 <= len(device_name.strip()) <= 80
                or any(ord(char) < 32 for char in device_name)
                or not isinstance(attestation_chain, list) or not 2 <= len(attestation_chain) <= 10
                or not all(isinstance(cert, str) and 1 <= len(cert) <= 16_384 for cert in attestation_chain)):
            raise MobileError('mobile_request_invalid')
        with self.registry.records.begin_read() as tx:
            pending = self._invite(tx, code, self.registry._now())
            if challenge_id != pending.payload['challenge_id']:
                raise MobileError('mobile_invite_unavailable')
            challenge = base64.urlsafe_b64decode(pending.payload['challenge'] + '=')
        key, serials = self.attestation.verify(attestation_chain, challenge, self.registry._now())
        with self.registry.records.begin() as tx:
            now = self.registry._now()
            row = self._invite(tx, code, now)
            if row.revision != pending.revision:
                raise MobileError('mobile_invite_unavailable')
            identity, user_id = 'mobile-' + uuid4().hex, row.payload['user_id']
            signed = self._sign(identity, user_id, key, now)
            tx.put(DEVICES, identity, {'device_id': identity, 'user_id': user_id, 'name': device_name.strip(),
                'key_hash': None, 'created_at': now.isoformat(), 'last_seen_at': now.isoformat(), 'revoked_at': None},
                expected_revision=0)
            tx.put(CERTS, identity, {**signed, 'attestation_serials': serials}, expected_revision=0)
            tx.put(INVITES, row.object_id, {**row.payload, 'used': True, 'device_id': identity}, expected_revision=row.revision)
            tx.commit()
        return signed

    def identity(self, scope):
        headers = Headers(scope=scope)
        proxies, certs = headers.getlist(PROXY_HEADER), headers.getlist(CERT_HEADER)
        if not proxies and not certs:
            return None
        if (len(proxies) != 1 or len(certs) != 1 or scope['scheme'] not in {'https', 'wss'}
                or not proxies[0].isascii() or not hmac.compare_digest(proxies[0], self.proxy)
                or len(certs[0]) > 8192 or any(headers.getlist(header) for header in (
                    'authorization', 'x-chriptmas-device-key', 'x-chriptmas-target-user'))):
            _denied()
        try:
            cert = x509.load_pem_x509_certificate(unquote_to_bytes(certs[0]))
            cert.verify_directly_issued_by(self.ca)
            device_id = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
        except (ValueError, TypeError, InvalidSignature, IndexError):
            _denied()
        now = self.registry._now()
        if not cert.not_valid_before_utc <= now < min(cert.not_valid_after_utc, self.ca.not_valid_after_utc):
            _denied()
        saved = self.registry.records.read(CERTS, device_id)
        if saved is None:
            _denied()
        current = x509.load_pem_x509_certificate(saved.payload['certificate'].encode('ascii'))
        if _spki(current.public_key()) != _spki(cert.public_key()):
            _denied()
        identity = self.registry.identity_for_device(device_id, saved.payload['user_id'])
        if identity is None:
            _denied()
        self._observe(identity, now)
        # 内部代理凭据和证书不会进入产品路由或下游请求。
        scope['headers'] = [(name, value) for name, value in scope['headers']
            if name.lower() not in {PROXY_HEADER.encode('ascii'), CERT_HEADER.encode('ascii')}]
        return identity

    def _observe(self, identity, now):
        # 复用设备最近活动字段，合并同一页面连续发出的请求。
        presence = self.registry.records.read(PRESENCE, identity.device_id)
        if presence is not None and now - _expires(presence.payload['last_seen_at']) < timedelta(minutes=1):
            return
        with self.registry.records.begin() as tx:
            device = tx.read(DEVICES, identity.device_id)
            if (device is None or device.revision != identity.revision
                    or device.payload['revoked_at'] is not None or enabled_user(tx, identity.user_id) is None):
                _denied()
            current = tx.read(PRESENCE, identity.device_id)
            if current is None or now - _expires(current.payload['last_seen_at']) >= timedelta(minutes=1):
                tx.put(PRESENCE, identity.device_id, {'last_seen_at': now.isoformat()},
                    expected_revision=current.revision if current else 0)
                tx.commit()

    def renew(self, identity):
        now = self.registry._now()
        with self.registry.records.begin() as tx:
            device, user = tx.read(DEVICES, identity.device_id), enabled_user(tx, identity.user_id)
            saved = tx.read(CERTS, identity.device_id)
            if (device is None or user is None or device.revision != identity.revision
                    or device.payload['revoked_at'] is not None or saved is None
                    or device.payload['user_id'] != identity.user_id or saved.payload['user_id'] != identity.user_id):
                raise MobileError('mobile_device_unauthorized')
            serials = saved.payload['attestation_serials']
            self.attestation.assert_not_revoked(serials, now)
            if _expires(saved.payload['expires_at']) - now > _RENEW_WINDOW:
                return {key: saved.payload[key] for key in ('device_id', 'user_id', 'certificate', 'expires_at')}
            cert = x509.load_pem_x509_certificate(saved.payload['certificate'].encode('ascii'))
            signed = self._sign(identity.device_id, identity.user_id, cert.public_key(), now)
            tx.put(CERTS, identity.device_id, {**signed, 'attestation_serials': serials}, expected_revision=saved.revision)
            tx.commit()
        return signed
