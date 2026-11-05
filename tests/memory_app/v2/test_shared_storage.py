import asyncio
import os
import subprocess

import pytest

from backend.memory_app.server_jobs import QuotaError, ServerJobScheduler
from backend.memory_app.v2.devices import DeviceRegistry
from backend.memory_app.v2.server_users import ServerUsers
from backend.memory_app.v2.shared_projects import MEMBERS, OPERATIONS, PROJECTS, SharedProjects, member_id
from backend.memory_app.v2.shared_runtime import SharedDataRoots
from backend.memory_app.v2.shared_storage import SharedStorage
from backend.security.user_context import UserError


def setup(tmp_path):
    devices = DeviceRegistry(tmp_path / 'server')
    pairing = devices.issue_pairing(user_id='local-user', actor='install')
    admin = devices.authenticate(devices.exchange(pairing['code'], name='容量管理设备')['key'])
    users = ServerUsers(tmp_path, records=devices.records)
    actors = []
    for label in ('甲', '乙'):
        user = users.create(admin, name=label)
        pairing = devices.issue_pairing(user_id=user['user_id'], actor=admin.device_id)
        actors.append(devices.authenticate(devices.exchange(pairing['code'], name=label)['key']))
    registry = SharedProjects(users, SharedDataRoots(tmp_path))
    jobs = ServerJobScheduler(users)
    storage = SharedStorage(registry, jobs)
    projects = [registry.create(actor, project_name=label, operation_id='create-' + label)['project']
                for actor, label in zip(actors, ('project-a', 'project-b'))]
    registry.invite(actors[0], projects[0]['id'], user_id=actors[1].user_id, role='member',
                    authority=projects[0]['authority'], operation_id='invite-b')
    invitation = registry.invitations(actors[1])[0]
    registry.accept(actors[1], invitation['id'], expected_revision=invitation['revision'], operation_id='accept-b')
    return users, admin, actors, registry, jobs, storage, projects


def transfer(registry, actor, project, target, operation):
    detail = registry.detail(actor, project['id'])
    member = next(row for row in detail['members'] if row['user_id'] == target.user_id)
    return registry.change_member(actor, project['id'], user_id=target.user_id, action='transfer',
        expected_revision=member['revision'], authority=detail['project']['authority'], operation_id=operation)


def test_actual_owned_roots_count_once_and_transfer_refusal_preserves_original_rows(tmp_path):
    users, admin, (a, b), registry, jobs, _, projects = setup(tmp_path)
    roots = [registry.roots._data(project['id']).root for project in projects]
    baseline = [jobs.storage_bytes(actor.user_id) for actor in (a, b)]
    for index, root in enumerate(roots):
        (root / 'original.txt').write_bytes(bytes([65 + index]) * 700000)
    assert [jobs.storage_bytes(actor.user_id) for actor in (a, b)] == [value + 700000 for value in baseline]
    users.update(admin, b.user_id, expected_revision=users.get(b.user_id)['revision'], storage_limit_mb=1)
    before = {collection: registry.records.list(collection) for collection in (PROJECTS, MEMBERS, OPERATIONS)}
    with pytest.raises(QuotaError, match='storage_quota_exceeded'):
        transfer(registry, a, projects[0], b, 'transfer-over-quota')
    assert {collection: registry.records.list(collection) for collection in before} == before
    assert (roots[0] / 'original.txt').read_bytes() == b'A' * 700000
    assert registry.records.read(OPERATIONS, 'transfer-over-quota') is None
    users.update(admin, b.user_id, expected_revision=users.get(b.user_id)['revision'], storage_limit_mb=8)
    transfer(registry, admin, projects[0], b, 'transfer-accepted')
    assert registry.records.read(PROJECTS, projects[0]['id']).payload['owner_user_id'] == b.user_id
    assert registry.records.read(MEMBERS, member_id(projects[0]['id'], a.user_id)).payload['role'] == 'member'
    assert jobs.storage_bytes(a.user_id) == jobs._root_bytes(users.root_for(a.user_id))
    assert jobs.storage_bytes(b.user_id) == sum(jobs._root_bytes(root)
        for root in (users.root_for(b.user_id), *roots))
    restored = SharedProjects(users, SharedDataRoots(tmp_path))
    reopened_jobs = ServerJobScheduler(users)
    SharedStorage(restored, reopened_jobs)
    assert reopened_jobs.storage_bytes(b.user_id) == jobs.storage_bytes(b.user_id)
    received = registry.records.read(OPERATIONS, 'transfer-accepted')
    users.update(admin, b.user_id, expected_revision=users.get(b.user_id)['revision'], storage_limit_mb=0)
    result = registry.change_member(admin, projects[0]['id'], user_id=b.user_id, action='transfer',
        expected_revision=received.payload['inputs']['revision'],
        authority=registry.detail(admin, projects[0]['id'])['project']['authority'], operation_id='transfer-accepted')
    assert result['state'] == 'completed' and registry.records.read(OPERATIONS, 'transfer-accepted') == received


def test_actual_dual_intake_serializes_reverse_order_and_releases_cancelled_waiter(tmp_path):
    users, _, (a, b), registry, jobs, _, projects = setup(tmp_path)
    async def scenario():
        ids = (a.user_id, b.user_id)
        entered = asyncio.Event()
        async def waiting():
            async with jobs.intake_many(reversed(ids)):
                entered.set()
        async with jobs.intake_many(ids) as budgets:
            assert set(budgets) == set(ids)
            waiter = asyncio.create_task(waiting())
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(entered.wait(), .1)
            with pytest.raises(QuotaError, match='storage_admission_in_progress'):
                await asyncio.to_thread(transfer, registry, a, projects[0], b, 'transfer-during-intake')
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            for actor in (a, b):
                budgets[actor.user_id](64)
            assert registry.records.read(OPERATIONS, 'transfer-during-intake') is None
        async with jobs.intake_many(reversed(ids)) as budgets:
            budgets[a.user_id](16)
            budgets[b.user_id](16)
        transfer(registry, a, projects[0], b, 'transfer-after-intake')
        assert registry.records.read(PROJECTS, projects[0]['id']).payload['owner_user_id'] == b.user_id
        await jobs.close()
    asyncio.run(scenario())


def test_finite_cap_consumes_in_actual_actor_transaction_with_original_central_reader(tmp_path):
    users, admin, (a, b), registry, jobs, _, projects = setup(tmp_path)
    users.update(admin, a.user_id, expected_revision=users.get(a.user_id)['revision'], storage_limit_mb=1)
    data = registry.roots._data(projects[0]['id'])
    (data.root / 'original.txt').write_bytes(b'C' * 700000)
    from core.storage_provider import SQLiteStructuredRecordStore
    actor_records = SQLiteStructuredRecordStore(users.root_for(a.user_id) / 'actor.sqlite3')
    async def scenario():
        async with jobs.intake_many((b.user_id, a.user_id)) as budgets:
            with pytest.raises(QuotaError, match='storage_quota_exceeded'):
                budgets[a.user_id](400000)
            cap = budgets[a.user_id].reserve_up_to(4096)
            with users.records.begin() as central, cap.using_reader(central), actor_records.begin() as tx:
                cap.consume(64)
                tx.put('quota_probe', 'actual', {'bytes': 64}, expected_revision=0)
                tx.commit()
            cap.close()
        assert actor_records.read('quota_probe', 'actual').payload['bytes'] == 64
        with pytest.raises(QuotaError, match='intake_admission_expired'):
            cap.consume(1)
        await jobs.close()
    asyncio.run(scenario())


def test_actual_source_binding_and_link_refuse_storage_counting(tmp_path):
    _, _, (a, _), registry, jobs, _, projects = setup(tmp_path)
    data = registry.roots._data(projects[0]['id'])
    with data.records.begin() as tx:
        original = tx.read('v2_shared_root', 'identity')
        tx.put('v2_shared_root', 'identity', {'project_id': projects[1]['id']}, expected_revision=original.revision)
        tx.commit()
    with pytest.raises(ValueError, match='shared_root_binding_invalid'):
        jobs.storage_bytes(a.user_id)
    with data.records.begin() as tx:
        original = tx.read('v2_shared_root', 'identity')
        tx.put('v2_shared_root', 'identity', {'project_id': projects[0]['id']}, expected_revision=original.revision)
        tx.commit()
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'original.txt').write_text('不可计入的其他资料', encoding='utf8')
    link = data.root / 'linked-originals'
    if os.name == 'nt':
        subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J', str(link), str(outside)],
                       check=True, capture_output=True)
        assert link.is_junction()
    else:
        link.symlink_to(outside, target_is_directory=True)
        assert link.is_symlink()
    with pytest.raises((QuotaError, UserError), match='(user_storage_invalid|user_delete_path_invalid)'):
        jobs.storage_bytes(a.user_id)
    assert (outside / 'original.txt').read_text(encoding='utf8') == '不可计入的其他资料'
