"""Server device authority uses real SQLite transactions and random credentials."""
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest


def registry(tmp_path, *, now=None):
    from backend.memory_app.v2.devices import DeviceRegistry
    return DeviceRegistry(tmp_path / 'server', clock=now)


def test_pairing_returns_one_32_byte_key_and_persists_only_digests(tmp_path):
    devices = registry(tmp_path)
    pairing = devices.issue_pairing(user_id='local-user', actor='install')
    paired = devices.exchange(pairing['code'], name='手机')
    assert len(base64.urlsafe_b64decode(paired['key'] + '=')) == 32
    assert paired['device']['user_id'] == 'local-user'
    assert devices.authenticate(paired['key']).device_id == paired['device']['device_id']
    public = devices.list_devices('local-user')
    assert 'key' not in public[0] and 'key_hash' not in public[0]
    records = devices.records.list_all()
    assert all(paired['key'] not in str(row.payload) and pairing['code'] not in str(row.payload) for row in records)
    assert any(len(row.payload.get('key_hash', '')) == 64 for row in records)


def test_pair_code_expires_at_ten_minutes_and_has_one_atomic_consumer(tmp_path):
    from backend.memory_app.v2.devices import DeviceError
    clock = [datetime(2026, 10, 5, tzinfo=timezone.utc)]
    devices = registry(tmp_path, now=lambda: clock[0])
    pairing = devices.issue_pairing(user_id='local-user', actor='install')
    assert datetime.fromisoformat(pairing['expires_at']) == clock[0] + timedelta(minutes=10)
    devices.exchange(pairing['code'], name='第一台')
    with pytest.raises(DeviceError, match='pairing_unavailable'):
        devices.exchange(pairing['code'], name='第二台')
    expired = devices.issue_pairing(user_id='local-user', actor='install')
    clock[0] += timedelta(minutes=10)
    with pytest.raises(DeviceError, match='pairing_unavailable'):
        devices.exchange(expired['code'], name='过期')
    assert len(devices.list_devices('local-user')) == 1


def test_two_independent_instances_cannot_consume_one_code_twice(tmp_path):
    from backend.memory_app.v2.devices import DeviceError
    first, second = registry(tmp_path), registry(tmp_path)
    code = first.issue_pairing(user_id='local-user', actor='install')['code']
    def consume(owner):
        try:
            owner.exchange(code, name='手机')
            return 'paired'
        except DeviceError:
            return 'unavailable'
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(consume, (first, second)))
    assert sorted(outcomes) == ['paired', 'unavailable']
    assert len(first.list_devices('local-user')) == 1


def test_revocation_invalidates_another_instance_and_cannot_cross_user(tmp_path):
    from backend.memory_app.v2.devices import DeviceError
    first, second = registry(tmp_path), registry(tmp_path)
    paired = first.exchange(first.issue_pairing(user_id='local-user', actor='install')['code'], name='电脑')
    before = second.authenticate(paired['key'])
    assert before.user_id == 'local-user'
    with pytest.raises(DeviceError, match='device_not_found'):
        first.revoke('another-user', before.device_id, expected_revision=before.revision)
    first.revoke('local-user', before.device_id, expected_revision=before.revision)
    assert second.authenticate(paired['key']) is None
    assert first.list_devices('local-user')[0]['revoked_at'] is not None


def test_revoked_issuer_cannot_issue_or_finish_previously_issued_pairing(tmp_path):
    from backend.memory_app.v2.devices import DeviceError
    devices = registry(tmp_path)
    paired = devices.exchange(devices.issue_pairing(user_id='local-user', actor='install')['code'], name='电脑')
    identity = devices.authenticate(paired['key'])
    pending = devices.issue_pairing(user_id=identity.user_id, actor=identity.device_id)
    devices.revoke(identity.user_id, identity.device_id, expected_revision=identity.revision)
    with pytest.raises(DeviceError, match='device_unauthorized'):
        devices.issue_pairing(user_id=identity.user_id, actor=identity.device_id)
    with pytest.raises(DeviceError, match='pairing_unavailable'):
        devices.exchange(pending['code'], name='迟到')
    assert len(devices.list_devices(identity.user_id)) == 1


@pytest.mark.parametrize('code,name', [('', '手机'), ('arbitrary', '手机'), ('\n', '手机')])
def test_invalid_exchange_is_controlled_and_creates_no_device(tmp_path, code, name):
    from backend.memory_app.v2.devices import DeviceError
    devices = registry(tmp_path)
    with pytest.raises(DeviceError, match='pairing_unavailable'):
        devices.exchange(code, name=name)
    assert devices.list_devices('local-user') == []


def test_presence_does_not_change_fact_cas_or_invalidate_issued_pairing(tmp_path):
    devices = registry(tmp_path)
    paired = devices.exchange(devices.issue_pairing(user_id='local-user', actor='install')['code'], name='电脑')
    identity = devices.authenticate(paired['key'])
    pending = devices.issue_pairing(user_id=identity.user_id, actor=identity.device_id)
    for _ in range(3):
        assert devices.authenticate(paired['key']).revision == identity.revision
    assert devices.exchange(pending['code'], name='手机')['device']['user_id'] == identity.user_id
    devices.revoke(identity.user_id, identity.device_id, expected_revision=identity.revision)
    assert devices.authenticate(paired['key']) is None


def test_presence_and_revocation_are_serialized_by_the_real_owner(tmp_path):
    devices, other = registry(tmp_path), registry(tmp_path)
    paired = devices.exchange(devices.issue_pairing(user_id='local-user', actor='install')['code'], name='电脑')
    identity = devices.authenticate(paired['key'])
    with ThreadPoolExecutor(max_workers=2) as pool:
        auth = pool.submit(other.authenticate, paired['key'])
        revoked = pool.submit(devices.revoke, identity.user_id, identity.device_id, expected_revision=identity.revision)
        auth.result()
        revoked.result()
    assert other.authenticate(paired['key']) is None
