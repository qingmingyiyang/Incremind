from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from threading import Barrier

from argon2 import PasswordHasher, profiles
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from backend.memory_app.server_runtime import create_server_application
from backend.memory_app.v2.devices import DeviceRegistry
from backend.memory_app.v2.server_accounts import ServerAccounts, CREDENTIALS, NAMES, LOGIN_DEVICES, ATTEMPTS
from backend.memory_app.v2.server_users import ServerUsers
from backend.security.user_context import UserError
from backend.shared.deployment import DeploymentLayout

PASSWORD = ' 空白与中文完整保留 strong password '


def owners(root):
    registry = DeviceRegistry(root / 'server')
    paired = registry.exchange(registry.issue_pairing(user_id='local-user', actor='install')['code'], name='管理员')
    users = ServerUsers(root, records=registry.records)
    return users, registry, registry.authenticate(paired['key']), paired


@pytest.mark.parametrize('password', ['x' * 14, 'x' * 257, None, '\ud800' * 15])
def test_password_boundaries_reject_before_hash(password):
    from backend.memory_app.v2.server_accounts import prepare_credentials
    with pytest.raises(UserError, match='^login_password_invalid$'):
        prepare_credentials('member', password)


@pytest.mark.parametrize('size', [15, 256])
def test_password_exact_accepted_boundaries_use_real_argon(size):
    from backend.memory_app.v2.server_accounts import prepare_credentials
    name, encoded = prepare_credentials('Member', ' ' * size)
    assert name == 'member' and PasswordHasher().verify(encoded, ' ' * size)


def test_simultaneous_real_password_verification_is_bounded(tmp_path):
    users, registry, admin, _ = owners(tmp_path)
    users.create(admin, name='成员', login_name='member', password=PASSWORD)
    start = Barrier(8)
    def login(_):
        start.wait(timeout=10)
        try:
            ServerAccounts(users, registry).login('member', PASSWORD)
            return 'accepted'
        except UserError as error:
            return str(error)
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(login, range(8)))
    assert 'accepted' in results and 'login_rate_limited' in results
    assert set(results) == {'accepted', 'login_rate_limited'}


def test_real_argon_parameters_unicode_and_safe_public_fields(tmp_path):
    users, registry, admin, _ = owners(tmp_path)
    row = users.create(admin, name='中文名称', login_name='Alice.A', password=PASSWORD)
    credential = users.records.read(CREDENTIALS, row['user_id'])
    hasher = PasswordHasher.from_parameters(profiles.RFC_9106_LOW_MEMORY)
    assert hasher.verify(credential.payload['password_hash'], PASSWORD)
    assert not hasher.check_needs_rehash(credential.payload['password_hash'])
    assert row['login_name'] == 'alice.a' and row['login_revision'] == 1
    assert 'password' not in str(row) and 'argon2' not in str(users.list_for(admin))
    accounts = ServerAccounts(users, registry)
    result = accounts.login('ALICE.A', PASSWORD)
    assert registry.authenticate(result['key']).user_id == row['user_id']
    with pytest.raises(UserError, match='login_failed'):
        accounts.login('Alice.A', PASSWORD.strip())


def test_unique_name_atomic_user_creation_and_password_pairs(tmp_path):
    users, registry, admin, _ = owners(tmp_path)
    users.create(admin, name='一', login_name='same', password=PASSWORD)
    for data, error in (({'login_name': 'SAME', 'password': PASSWORD}, 'login_name_conflict'),
                        ({'login_name': 'second'}, 'user_request_invalid'),
                        ({'login_name': '中文用户', 'password': PASSWORD}, 'login_name_invalid'),
                        ({'login_name': 'short', 'password': 'x' * 14}, 'login_password_invalid')):
        with pytest.raises(UserError, match=error):
            users.create(admin, name='未创建', **data)
    assert len(users.list_for(admin)) == 2
    assert len(list((tmp_path / 'users').iterdir())) == 1
    assert len(registry.records.list(CREDENTIALS)) == 1


def test_reset_dual_cas_revokes_login_but_preserves_explicit_pairing(tmp_path):
    users, registry, admin, _ = owners(tmp_path)
    user = users.create(admin, name='成员', login_name='member', password=PASSWORD)
    accounts = ServerAccounts(users, registry)
    logged = accounts.login('member', PASSWORD)
    paired = registry.exchange(registry.issue_pairing(user_id=user['user_id'], actor=admin.device_id)['code'], name='配对')
    result = accounts.reset(admin, user['user_id'], login_name='new.member', password=PASSWORD + '2',
                            expected_revision=1, expected_login_revision=1)
    assert result['login_revision'] == 2 and result['login_name'] == 'new.member'
    assert registry.authenticate(logged['key']) is None
    assert registry.authenticate(paired['key']) is not None
    assert accounts.login('NEW.MEMBER', PASSWORD + '2')['key']
    for revisions, error in (((0, 2), 'user_revision_invalid'), ((2, 2), 'user_revision_conflict'),
                              ((1, 1), 'login_revision_conflict')):
        with pytest.raises(UserError, match=error):
            accounts.reset(admin, user['user_id'], login_name='new.member', password=PASSWORD,
                           expected_revision=revisions[0], expected_login_revision=revisions[1])


def test_real_concurrent_reset_has_one_cas_winner(tmp_path):
    users, registry, admin, _ = owners(tmp_path)
    user = users.create(admin, name='成员', login_name='member', password=PASSWORD)
    def reset(index):
        try:
            return ServerAccounts(ServerUsers(tmp_path), DeviceRegistry(tmp_path / 'server')).reset(
                admin, user['user_id'], login_name='member', password=PASSWORD + str(index),
                expected_revision=1, expected_login_revision=1)['login_revision']
        except UserError as error:
            return str(error)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(reset, (1, 2)))
    assert 2 in results and 'login_revision_conflict' in results


def test_existing_admin_can_configure_login_and_permanent_device_delete_cleans_index(tmp_path):
    users, registry, admin, paired = owners(tmp_path)
    accounts = ServerAccounts(users, registry)
    configured = accounts.reset(admin, 'local-user', login_name='admin', password=PASSWORD,
                                expected_revision=1, expected_login_revision=0)
    assert configured['role'] == 'admin' and configured['login_revision'] == 1
    logged = accounts.login('admin', PASSWORD)
    device = logged['device']
    revoked = registry.revoke('local-user', device['device_id'], expected_revision=device['revision'])
    registry.delete('local-user', device['device_id'], expected_revision=revoked['revision'],
        confirmation=device['device_id'], actor_device_id=admin.device_id, actor_identity=admin)
    assert users.records.read(LOGIN_DEVICES, device['device_id']) is None
    assert users.records.read(CREDENTIALS, 'local-user') is not None
    assert registry.authenticate(paired['key']) == admin


def test_disabled_unknown_wrong_password_and_deletion_marker_fail_uniformly(tmp_path):
    users, registry, admin, _ = owners(tmp_path)
    user = users.create(admin, name='成员', login_name='member', password=PASSWORD)
    accounts = ServerAccounts(users, registry)
    for name, password in (('unknown', PASSWORD), ('member', PASSWORD + 'wrong')):
        with pytest.raises(UserError, match='^login_failed$'):
            accounts.login(name, password)
    users.update(admin, user['user_id'], expected_revision=1, disabled=True)
    with pytest.raises(UserError, match='^login_failed$'):
        accounts.login('member', PASSWORD)
    users.update(admin, user['user_id'], expected_revision=2, disabled=False)
    with users.records.begin() as tx:
        tx.put('server_user_deletions', user['user_id'], {'user_id': user['user_id']}, expected_revision=0)
        tx.commit()
    with pytest.raises(UserError, match='^login_failed$'):
        accounts.login('member', PASSWORD)
    assert registry.records.list(LOGIN_DEVICES) == ()


def test_persistent_rate_limit_survives_new_owner_and_expires(tmp_path):
    users, registry, admin, _ = owners(tmp_path)
    users.create(admin, name='成员', login_name='member', password=PASSWORD)
    now = datetime(2026, 10, 24, tzinfo=timezone.utc)
    registry.clock = lambda: now
    accounts = ServerAccounts(users, registry)
    for _ in range(10):
        with pytest.raises(UserError, match='^login_failed$'):
            accounts.login('member', PASSWORD + 'bad')
    other = DeviceRegistry(tmp_path / 'server', clock=lambda: now)
    with pytest.raises(UserError, match='^login_rate_limited$'):
        ServerAccounts(ServerUsers(tmp_path), other).login('MEMBER', PASSWORD)
    now += timedelta(minutes=16)
    assert ServerAccounts(users, other).login('member', PASSWORD)['key']
    assert len(users.records.list(ATTEMPTS)) == 2


def test_actual_server_http_login_admin_controls_logout_and_origin(tmp_path):
    users, registry, admin, paired = owners(tmp_path)
    layout = DeploymentLayout('server', tmp_path / 'users/local-user', tmp_path)
    app = create_server_application(layout, child_factory=lambda root, user, context: FastAPI(), port=8091)
    headers = {'Authorization': 'Bearer ' + paired['key']}
    with TestClient(app) as client:
        result = client.post('/api/v2/server/users', headers=headers,
            json={'name': '新账号', 'login_name': 'account', 'password': PASSWORD})
        assert result.status_code == 201, result.text
        user = result.json()
        assert client.post('/api/v2/auth/login', headers={'Origin': 'https://other.example'},
            json={'login_name': 'account', 'password': PASSWORD}).status_code == 403
        login = client.post('/api/v2/auth/login', json={'login_name': 'ACCOUNT', 'password': PASSWORD})
        assert login.status_code == 200, login.text
        ordinary = {'Authorization': 'Bearer ' + login.json()['key']}
        assert client.post('/api/v2/server/users', headers=ordinary, json={'name': '越权'}).status_code == 403
        request = {'login_name': 'account', 'password': PASSWORD + '2', 'expected_revision': 1, 'expected_login_revision': 1}
        assert client.put('/api/v2/server/users/' + user['user_id'] + '/login', headers=ordinary, json=request).status_code == 403
        assert client.put('/api/v2/server/users/' + user['user_id'] + '/login', headers=headers, json=request).status_code == 200
        assert client.get('/api/v2/server/users', headers=ordinary).status_code == 401
        listings = client.get('/api/v2/server/users', headers=headers).json()
        assert listings['caller']['login_configured'] is False
        assert 'password' not in str(listings) and 'argon2' not in str(listings)
        logged = client.post('/api/v2/auth/login', json={'login_name': 'account', 'password': PASSWORD + '2'}).json()
        new_headers = {'Authorization': 'Bearer ' + logged['key']}
        history = client.get('/api/v2/server/audit', headers=new_headers)
        assert history.status_code == 200
        intents = [row for row in history.json()['items'] if row['phase'] == 'intent'
            and row['path'] == '/api/v2/server/users/{user_id}/login']
        assert len(intents) == 1
        assert intents[0]['actor_user_id'] == 'local-user'
        assert intents[0]['target_user_id'] == user['user_id']
        outcomes = [row for row in history.json()['items'] if row['phase'] == 'outcome'
            and row['intent_id'] == intents[0]['audit_id']]
        assert len(outcomes) == 1 and outcomes[0]['status'] == 200
        assert PASSWORD not in history.text and 'password_hash' not in history.text
        assert client.post('/api/v2/devices/logout', headers=new_headers, json={}).json() == {'revoked': True}
        assert client.get('/api/v2/server/users', headers=new_headers).status_code == 401
        assert client.post('/api/v2/auth/register', json={}).status_code == 401
        for name, password in (('unknown', PASSWORD), ('account', PASSWORD)):
            failed = client.post('/api/v2/auth/login', json={'login_name': name, 'password': password})
            assert failed.status_code == 401 and failed.json() == {'detail': 'login_failed'}


def test_actual_server_delete_and_reset_race_cleans_credentials_and_devices(tmp_path):
    users, registry, admin, paired = owners(tmp_path)
    user = users.create(admin, name='待删除', login_name='delete.user', password=PASSWORD)
    ServerAccounts(users, registry).login('delete.user', PASSWORD)
    users.update(admin, user['user_id'], expected_revision=1, disabled=True)
    app = create_server_application(DeploymentLayout('server', tmp_path / 'users/local-user', tmp_path),
        child_factory=lambda root, user_id, context: FastAPI(), port=8091)
    headers = {'Authorization': 'Bearer ' + paired['key']}
    url = '/api/v2/server/users/' + user['user_id']
    with TestClient(app) as client:
        def reset():
            return client.put(url + '/login', headers=headers, json={'login_name': 'delete.user',
                'password': PASSWORD + 'new', 'expected_revision': 2, 'expected_login_revision': 1})
        def delete():
            return client.request('DELETE', url, headers=headers,
                json={'expected_revision': 2, 'confirmation': user['user_id']})
        with ThreadPoolExecutor(max_workers=2) as executor:
            reset_future, deleted_future = executor.submit(reset), executor.submit(delete)
            reset_result, deleted = reset_future.result(), deleted_future.result()
        assert reset_result.status_code in {200, 404, 409}, reset_result.text
        assert deleted.status_code in {200, 409}, deleted.text
        if deleted.status_code == 409:
            assert deleted.json()['detail'] == 'user_delete_busy'
            deleted = delete()
        assert deleted.status_code == 200, deleted.text
        assert users.records.read('server_users', user['user_id']) is None
        assert users.records.read(CREDENTIALS, user['user_id']) is None
        assert users.records.read(NAMES, 'name-delete.user') is None
        assert not [row for row in users.records.list(LOGIN_DEVICES) if row.payload['user_id'] == user['user_id']]
        assert not [row for row in registry.list_devices(user['user_id'])]
        assert client.post('/api/v2/auth/login', json={'login_name': 'delete.user', 'password': PASSWORD + 'new'}).status_code == 401
