import base64
from datetime import datetime, timedelta, timezone
import json
from os import utime
from pathlib import Path
from shutil import copyfile
from urllib.parse import quote

import pytest
from asn1crypto import core
from cryptography import x509
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient

from backend.memory_app.v2.android_attestation import (
    ANDROID_KEY_DESCRIPTION, AndroidAttestation, AttestationError, _AppIdentity, _Integers, _KeyDescription, _RootOfTrust,
)
from backend.memory_app.v2.android_mobile import CERTS, INVITES
from backend.shared.deployment import DeploymentLayout


PACKAGE = 'app.incremind.mobile'
SIGNER = b'\xf2' * 32
PROXY = 'test-proof-' + 'j' * 43


class HardwareAuthorization(core.Sequence):
    _fields = [('purpose', _Integers, {'explicit': 1}), ('algorithm', core.Integer, {'explicit': 2}),
        ('digests', _Integers, {'explicit': 5}), ('curve', core.Integer, {'explicit': 10}),
        ('origin', core.Integer, {'explicit': 702}), ('boot', _RootOfTrust, {'explicit': 704})]


class SoftwareAuthorization(core.Sequence):
    _fields = [('app', core.OctetString, {'explicit': 709})]


def _certificate(subject, key, now, *, authority=None, issuer_key=None, extension=None):
    label = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject)])
    is_ca = authority is None
    builder = (x509.CertificateBuilder().subject_name(label).issuer_name(label if is_ca else authority.subject)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=is_ca, path_length=0 if is_ca else None), critical=True)
        .add_extension(x509.KeyUsage(True, False, False, False, False, is_ca, is_ca, False, False), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False))
    if extension is not None:
        builder = builder.add_extension(extension, critical=False)
    return builder.sign(key if is_ca else issuer_key, hashes.SHA256())


def _description(challenge, *, level='tee', digest=SIGNER):
    hardware = HardwareAuthorization({'purpose': [2], 'algorithm': 3, 'digests': [4], 'curve': 1, 'origin': 0,
        'boot': {'boot_key': b'\x01' * 32, 'locked': True, 'state': 'verified', 'boot_hash': b'\x02' * 32}})
    app = _AppIdentity({'packages': [{'name': PACKAGE.encode(), 'version': 1}], 'digests': [digest]})
    software = SoftwareAuthorization({'app': app.dump()})
    desc = _KeyDescription({'version': 400, 'level': level, 'key_version': 400, 'key_level': level,
        'challenge': challenge, 'unique_id': b'', 'software': core.Sequence.load(software.dump()),
        'hardware': core.Sequence.load(hardware.dump())})
    return x509.UnrecognizedExtension(ANDROID_KEY_DESCRIPTION, desc.dump())


def _configure(tmp_path, monkeypatch, now):
    cfg = tmp_path / 'android-mobile'
    cfg.mkdir()
    key = ec.generate_private_key(ec.SECP256R1())
    (cfg / 'client-ca.pem').write_bytes(_certificate('Mobile CA', key, now).public_bytes(serialization.Encoding.PEM))
    (cfg / 'client-ca.key').write_bytes(key.private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    (cfg / 'proxy-token').write_text(PROXY, encoding='ascii')
    (cfg / 'android-package.txt').write_text(PACKAGE, encoding='ascii')
    (cfg / 'android-signer-sha256.txt').write_text(SIGNER.hex(), encoding='ascii')
    root_key = ec.generate_private_key(ec.SECP256R1())
    root_cert = _certificate('Test attestation CA', root_key, now)
    (cfg / 'android-attestation-roots.pem').write_bytes(root_cert.public_bytes(serialization.Encoding.PEM))
    (cfg / 'android-attestation-revocations.json').write_text(json.dumps({'entries': {}}), encoding='ascii')
    monkeypatch.setenv('CHRIPTMAS_ANDROID_MOBILE_DIR', str(cfg))
    return root_key, root_cert


def test_verifies_signed_chain_and_live_revocation(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    issuer, root = _configure(tmp_path, monkeypatch, now)
    cfg = tmp_path / 'android-mobile'
    attest = AndroidAttestation(roots=cfg / 'android-attestation-roots.pem',
        revocations=cfg / 'android-attestation-revocations.json', package=PACKAGE, signing_digest=SIGNER)
    key = ec.generate_private_key(ec.SECP256R1())
    challenge = b'\x22' * 32
    leaf = _certificate('Generated device test key', key, now, authority=root, issuer_key=issuer,
        extension=_description(challenge))
    chain = [base64.b64encode(cert.public_bytes(serialization.Encoding.DER)).decode() for cert in (leaf, root)]
    attested_key, serials = attest.verify(chain, challenge, now)
    assert attested_key.public_numbers() == key.public_key().public_numbers()
    assert serials == [format(leaf.serial_number, 'x'), format(root.serial_number, 'x')]
    attest.revocations.write_text(json.dumps({'entries': {serials[0]: {'status': 'REVOKED'}}}), encoding='ascii')
    with pytest.raises(AttestationError, match='mobile_attestation_revoked'):
        attest.verify(chain, challenge, now)


def test_mobile_certificate_identity_and_revoke(tmp_path_factory, monkeypatch):
    tmp_path = tmp_path_factory.mktemp('m')
    import_root = (tmp_path / 'import-runtime').resolve()
    (import_root / 'config').mkdir(parents=True)
    copyfile(Path(__file__).resolve().parents[3] / 'config/settings.toml.example', import_root / 'config/settings.toml')
    monkeypatch.setenv('CHRIPTMAS_APP_ROOT', str(import_root))
    monkeypatch.setenv('CHRIPTMAS_DEPLOY', 'server')
    monkeypatch.setenv('CHRIPTMAS_SERVER_MASTER_KEY', Fernet.generate_key().decode('ascii'))
    from backend.memory_app.app import create_server_app

    now = datetime.now(timezone.utc)
    root_key, root_cert = _configure(tmp_path, monkeypatch, now)
    app = create_server_app(layout=DeploymentLayout('server', tmp_path / 'users/local-user', tmp_path))
    registry, users = app.state.device_registry, app.state.server_users
    paired = registry.exchange(registry.issue_pairing(user_id='local-user', actor='install')['code'], name='管理')
    admin = registry.authenticate(paired['key'])
    user_id = users.create(admin, name='手机验收')['user_id']
    admin_headers = {'Authorization': 'Bearer ' + paired['key']}
    with TestClient(app, base_url='https://testserver') as client:
        endpoint = f'/api/v2/server/users/{user_id}/mobile-invites'
        assert client.post(endpoint, json={}).status_code == 401
        invite = client.post(endpoint, headers=admin_headers, json={})
        assert invite.status_code == 200
        code = invite.json()['code']
        assert invite.json()['qr'].startswith('data:image/png;base64,')
        challenge = client.post('/api/v2/mobile/challenge', json={'code': code})
        assert challenge.status_code == 200
        challenge_value = base64.urlsafe_b64decode(challenge.json()['challenge'] + '=')
        device_key = ec.generate_private_key(ec.SECP256R1())

        def activate(description):
            leaf = _certificate('Device key', device_key, now, authority=root_cert, issuer_key=root_key,
                extension=description)
            chain = [base64.b64encode(cert.public_bytes(serialization.Encoding.DER)).decode() for cert in (leaf, root_cert)]
            return client.post('/api/v2/mobile/activate', json={'code': code, 'device_name': 'Android',
                'challenge_id': challenge.json()['challenge_id'], 'attestation_chain': chain})

        assert activate(_description(challenge_value, level='software')).status_code == 400
        assert activate(_description(challenge_value, digest=b'\xf1' * 32)).status_code == 400
        assert activate(_description(b'unrelated challenge')).status_code == 400
        result = activate(_description(challenge_value))
        assert result.status_code == 200, result.text
        session = result.json()
        assert set(session) == {'device_id', 'user_id', 'certificate', 'expires_at'}
        assert session['user_id'] == user_id
        assert activate(_description(challenge_value)).status_code == 409
        mobile_headers = {'X-Incremind-Mobile-Proxy': PROXY, 'X-Incremind-Client-Cert': quote(session['certificate'])}
        devices = '/api/v2/devices'
        assert client.get(devices, headers={'X-Incremind-Client-Cert': quote(session['certificate'])}).status_code == 401
        assert client.get(devices, headers=mobile_headers).json()['mobile_session'] == {
            'device_id': session['device_id'], 'user_id': user_id}
        assert 'mobile_session' not in client.get(devices, headers=admin_headers).json()
        assert client.post(devices + '/pair', headers=mobile_headers, json={}).status_code == 403
        assert client.post('/api/v2/native-mcp/keys', headers=mobile_headers, json={}).status_code == 403
        assert client.post('/api/rebuild/workbench/realtime-asr/ticket', headers=mobile_headers, json={}).status_code == 403
        assert client.post(endpoint, headers=mobile_headers, json={}).status_code == 403
        assert client.get(devices, headers={**mobile_headers, 'X-Chriptmas-Target-User': 'local-user'}).status_code == 401
        assert client.post('/api/v2/mobile/renew', headers=mobile_headers, json={}).json() == session
        assert client.post('/api/v2/mobile/renew', headers=admin_headers, json={}).status_code == 401
        revocations = app.state.android_mobile.attestation.revocations
        previous = x509.load_pem_x509_certificate(session['certificate'].encode('ascii'))
        renewal_time = previous.not_valid_after_utc - timedelta(days=29)
        with monkeypatch.context() as timing:
            timing.setattr(registry, 'clock', lambda: renewal_time)
            utime(revocations, times=(renewal_time.timestamp(), renewal_time.timestamp()))
            rotated = client.post('/api/v2/mobile/renew', headers=mobile_headers, json={})
            assert rotated.status_code == 200, rotated.text
            replacement = rotated.json()
            current = x509.load_pem_x509_certificate(replacement['certificate'].encode('ascii'))
            assert replacement['device_id'] == session['device_id'] and replacement['user_id'] == user_id
            assert current.serial_number != previous.serial_number
            assert current.public_key().public_numbers() == previous.public_key().public_numbers()
            assert current.not_valid_after_utc == renewal_time + timedelta(days=90)
            new_headers = {**mobile_headers, 'X-Incremind-Client-Cert': quote(replacement['certificate'])}
            for headers in (mobile_headers, new_headers):
                assert client.get(devices, headers=headers).status_code == 200
                assert client.post('/api/v2/mobile/renew', headers=headers, json={}).json() == replacement
            after_expiry = previous.not_valid_after_utc + timedelta(seconds=1)
            timing.setattr(registry, 'clock', lambda: after_expiry)
            assert client.get(devices, headers=mobile_headers).status_code == 401
            assert client.get(devices, headers=new_headers).status_code == 200
        stored = registry.records.read(CERTS, session['device_id'])
        revocations.write_text(json.dumps({'entries': {
            stored.payload['attestation_serials'][0]: {'status': 'REVOKED'}}}), encoding='ascii')
        assert client.post('/api/v2/mobile/renew', headers=mobile_headers, json={}).status_code == 403
        share = '/api/v2/workbench/share'
        share_headers = {**mobile_headers, 'Idempotency-Key': 'android-test-share'}
        form = {'project_id': (None, 'inbox'), 'text': (None, '移动分享联调样本')}
        first = client.post(share, headers=share_headers, files=form)
        assert first.status_code == 200, first.text
        replay = client.post(share, headers=share_headers, files=form)
        assert replay.status_code == 200
        assert len(first.json()['results']) == 1 and first.json() == replay.json()
        assert client.post(devices + '/logout', headers=mobile_headers, json={}).status_code == 200
        assert client.get(devices, headers=mobile_headers).status_code == 401
        assert len(registry.records.list(CERTS)) == len(registry.records.list(INVITES)) == 1
        device_id = session['device_id']
        revision = next(row['revision'] for row in registry.list_devices(user_id) if row['device_id'] == device_id)
        removed = client.request('DELETE', devices + '/' + device_id, json={
            'expected_revision': revision, 'confirmation': device_id},
            headers={**admin_headers, 'X-Chriptmas-Target-User': user_id})
        assert removed.status_code == 200
        assert not registry.records.list(CERTS) and not registry.records.list(INVITES)
