"""Server users and centralized access exercise the real shared SQLite owner."""
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor

import pytest


def owners(tmp_path):
    from backend.memory_app.v2.devices import DeviceRegistry
    from backend.memory_app.v2.server_users import ServerUsers
    devices = DeviceRegistry(tmp_path / 'server')
    pairing = devices.issue_pairing(user_id='local-user', actor='install')
    paired = devices.exchange(pairing['code'], name='管理电脑')
    users = ServerUsers(tmp_path, records=devices.records)
    return users, devices, devices.authenticate(paired['key']), paired


def test_seed_is_one_admin_and_new_user_gets_an_independent_initialized_root(tmp_path):
    users, devices, admin, _ = owners(tmp_path)
    original = users.get('local-user')
    assert original['role'] == 'admin' and original['disabled_at'] is None
    user = users.create(admin, name='第二位')
    assert user['role'] == 'user' and user['revision'] == 1
    root = users.root_for(user['user_id'])
    assert root == tmp_path / 'users' / user['user_id']
    assert (root / 'config' / 'settings.toml').is_file()
    assert users.root_for('local-user') != root
    devices.issue_pairing(user_id='local-user', actor='install')
    assert users.get('local-user') == original
    assert [row.payload['name'] for row in devices.records.list('server_users')] == ['本机', '第二位']


def test_duplicate_name_is_serialized_between_two_real_owners(tmp_path):
    from backend.memory_app.v2.server_users import ServerUsers, UserError
    users, _, admin, _ = owners(tmp_path)
    other = ServerUsers(tmp_path)
    def create(owner):
        try:
            owner.create(admin, name='同名')
            return 'created'
        except UserError as error:
            return str(error)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(create, (users, other)))
    assert sorted(results) == ['created', 'user_name_conflict']
    assert len(users.list_for(admin)) == 2


def test_caller_and_target_remain_distinct_and_permissions_revalidate(tmp_path):
    from backend.security.user_context import authorize_user
    from backend.memory_app.v2.server_users import UserError
    users, devices, admin, _ = owners(tmp_path)
    target = users.create(admin, name='普通用户')
    paired = devices.exchange(devices.issue_pairing(user_id=target['user_id'], actor=admin.device_id)['code'], name='手机')
    ordinary = devices.authenticate(paired['key'])
    access = authorize_user(users, admin, target['user_id'])
    assert access.caller == admin and access.target_user_id == target['user_id']
    assert access.by == 'admin' and admin.user_id == 'local-user'
    assert authorize_user(users, ordinary).by is None
    with pytest.raises(UserError, match='user_forbidden'):
        authorize_user(users, ordinary, 'local-user')
    with pytest.raises(UserError, match='user_not_found'):
        authorize_user(users, admin, 'unknown')
    users.update(admin, target['user_id'], expected_revision=target['revision'], disabled=True)
    assert devices.authenticate(paired['key']) is None
    assert not devices.is_current(ordinary)
    assert devices.identity_for_device(ordinary.device_id, ordinary.user_id) is None
    with pytest.raises(UserError, match='user_unauthorized'):
        authorize_user(users, ordinary)
    assert authorize_user(users, admin, target['user_id']).by == 'admin'


def test_disable_invalidates_every_key_and_pending_pairing_and_uses_cas(tmp_path):
    from backend.memory_app.v2.devices import DeviceError
    from backend.memory_app.v2.server_users import UserError
    users, devices, admin, _ = owners(tmp_path)
    target = users.create(admin, name='暂停用户')
    pairings = [devices.issue_pairing(user_id=target['user_id'], actor=admin.device_id) for _ in range(3)]
    keys = [devices.exchange(value['code'], name='手机')['key'] for value in pairings[:2]]
    users.update(admin, target['user_id'], expected_revision=1, disabled=True,
                 storage_limit_mb=0, job_minutes_per_day=0)
    assert all(devices.authenticate(key) is None for key in keys)
    with pytest.raises(DeviceError, match='pairing_unavailable'):
        devices.exchange(pairings[2]['code'], name='迟到')
    with pytest.raises(UserError, match='user_revision_conflict'):
        users.update(admin, target['user_id'], expected_revision=1, disabled=False)
    result = users.get(target['user_id'])
    assert result['storage_limit_mb'] == result['job_minutes_per_day'] == 0


def test_ordinary_user_cannot_manage_users_and_invalid_names_make_no_root(tmp_path):
    from backend.memory_app.v2.server_users import UserError
    users, devices, admin, _ = owners(tmp_path)
    target = users.create(admin, name='成员')
    pair = devices.exchange(devices.issue_pairing(user_id=target['user_id'], actor=admin.device_id)['code'], name='手机')
    ordinary = devices.authenticate(pair['key'])
    for operation in (lambda: users.create(ordinary, name='不允许'),
                      lambda: users.update(ordinary, 'local-user', expected_revision=1, disabled=True),
                      lambda: users.list_for(ordinary)):
        with pytest.raises(UserError, match='user_forbidden'):
            operation()
    for name in ('', '  ', 'a\n', 'x' * 81):
        with pytest.raises(UserError, match='user_name_invalid'):
            users.create(admin, name=name)
    assert len(users.list_for(admin)) == 2


def test_old_device_identity_cannot_be_reused_after_real_fact_cas(tmp_path):
    from backend.memory_app.v2.server_users import UserError
    from backend.security.user_context import authorize_user
    users, devices, old_identity, paired = owners(tmp_path)
    with devices.records.begin() as tx:
        row = tx.read('server_devices', old_identity.device_id)
        tx.put(row.collection, row.object_id, {**row.payload, 'name': '新名称'}, expected_revision=row.revision)
        tx.commit()
    assert not devices.is_current(old_identity)
    with pytest.raises(UserError, match='user_unauthorized'):
        authorize_user(users, old_identity)
    assert devices.authenticate(paired['key']).revision == old_identity.revision + 1
