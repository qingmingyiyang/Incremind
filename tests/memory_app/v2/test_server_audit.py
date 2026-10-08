"""Admin provenance joins the existing business transaction without payload changes."""
import asyncio

import pytest

from core.storage_provider import SQLiteStructuredRecordStore, SQLiteUnitOfWorkConflict
from backend.security.device_identity import DeviceIdentity
from backend.security.user_context import UserAccess, user_context


def access():
    return UserAccess(DeviceIdentity('device-admin', 'local-user', 1), 'user-other', 'admin')


def test_business_and_by_commit_once_without_changing_original_payload_or_commit_return(tmp_path):
    from backend.memory_app.server_audit import AuditedRecordStore
    original = SQLiteStructuredRecordStore(tmp_path / 'actual-authority.sqlite3')
    store = AuditedRecordStore(original, namespace='actual-namespace', user_id='user-other')
    with user_context(access()), store.begin() as tx:
        connection = tx.connection
        record = tx.put('documents', 'document-1', {'body': '原文'}, expected_revision=0)
        committed = tx.commit()
    assert committed == (record,) and tx.closed
    assert original.read('documents', 'document-1').payload == {'body': '原文'}
    writer = store.writer_for('documents', 'document-1', 1)
    assert writer['by'] == 'admin' and writer['actor_user_id'] == 'local-user'
    assert writer['target_user_id'] == 'user-other' and writer['namespace'] == 'actual-namespace'
    assert len(original.list('server_admin_writes')) == 1
    assert store.writer_for('documents', 'document-1', 2) is None


def test_rollback_failed_cas_and_attribution_failure_leave_no_business_or_by(tmp_path):
    from backend.memory_app.server_audit import AuditedRecordStore
    original = SQLiteStructuredRecordStore(tmp_path / 'authority.sqlite3')
    store = AuditedRecordStore(original, namespace='default', user_id='user-other')
    with user_context(access()), store.begin() as tx:
        tx.put('documents', 'rolled-back', {'body': '文本'}, expected_revision=0)
    with pytest.raises(SQLiteUnitOfWorkConflict), user_context(access()), store.begin() as tx:
        tx.put('documents', 'wrong-cas', {'body': '文本'}, expected_revision=7)
    # SQLite failure is injected at the actual attribution write, not the subject.
    with user_context(access()), store.begin() as tx:
        tx.connection.execute("CREATE TRIGGER reject_by BEFORE INSERT ON crp_structured_records WHEN NEW.collection='server_admin_writes' BEGIN SELECT RAISE(ABORT,'audit_failure'); END")
        with pytest.raises(Exception, match='audit_failure'):
            tx.put('documents', 'attribution-fails', {'body': '文本'}, expected_revision=0)
    assert original.list('documents') == ()
    assert original.list('server_admin_writes') == ()


def test_actor_and_target_are_frozen_when_transaction_begins(tmp_path):
    from backend.memory_app.server_audit import AuditedRecordStore
    original = SQLiteStructuredRecordStore(tmp_path / 'authority.sqlite3')
    store = AuditedRecordStore(original, namespace='default', user_id='user-other')
    with user_context(access()):
        tx = store.begin()
    with tx:
        tx.put('documents', 'after-request', {'body': '后台'}, expected_revision=0)
        tx.commit()
    assert store.writer_for('documents', 'after-request', 1)['actor_user_id'] == 'local-user'
    with user_context(UserAccess(access().caller, 'wrong-target', 'admin')):
        with pytest.raises(ValueError, match='admin_target_mismatch'):
            store.begin()
    assert len(original.list('documents')) == 1


def test_background_task_keeps_actor_and_target_after_request_context_returns(tmp_path):
    from backend.memory_app.server_audit import AuditedRecordStore
    store = AuditedRecordStore(SQLiteStructuredRecordStore(tmp_path / 'authority.sqlite3'),
                               namespace='default', user_id='user-other')
    async def scenario():
        gate = asyncio.Event()
        async def job():
            await gate.wait()
            def write():
                with store.begin() as tx:
                    tx.put('documents', 'background', {'body': '后台'}, expected_revision=0)
                    tx.commit()
            await asyncio.to_thread(write)
        with user_context(access()):
            pending = asyncio.create_task(job())
        gate.set()
        await pending
    asyncio.run(scenario())
    assert store.writer_for('documents', 'background', 1)['by'] == 'admin'


def test_central_audit_is_append_only_and_never_contains_request_body_or_secrets(tmp_path):
    from backend.memory_app.server_audit import AdminAudit
    from backend.memory_app.v2.server_users import ServerUsers
    from backend.memory_app.v2.devices import DeviceRegistry
    from backend.security.user_context import authorize_user
    devices = DeviceRegistry(tmp_path / 'server')
    pair = devices.exchange(devices.issue_pairing(user_id='local-user', actor='install')['code'], name='管理电脑')
    admin = devices.authenticate(pair['key'])
    users = ServerUsers(tmp_path, records=devices.records)
    target = users.create(admin, name='目标')
    third = users.create(admin, name='无关')
    def paired_user(user_id):
        value = devices.exchange(devices.issue_pairing(user_id=user_id, actor=admin.device_id)['code'], name='手机')
        return devices.authenticate(value['key'])
    target_identity, third_identity = paired_user(target['user_id']), paired_user(third['user_id'])
    audit = AdminAudit(users)
    intent = audit.intent(authorize_user(users, admin, target['user_id']), method='POST', path='/api/v2/workbench/files')
    audit.outcome(intent, status=201)
    first = audit.records.read('admin_audit', intent)
    assert first.revision == 1 and first.payload['phase'] == 'intent'
    rows = audit.list_for(authorize_user(users, admin))
    assert len(rows) == 2 and {row['phase'] for row in rows} == {'intent', 'outcome'}
    assert audit.list_for(authorize_user(users, target_identity)) == rows
    assert audit.list_for(authorize_user(users, third_identity)) == []
    assert all(set(row).isdisjoint({'body', 'key', 'authorization', 'query'}) for row in rows)
