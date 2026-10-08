"""Actual intake serialization budgets uploaded bytes and preserves read admission."""
import asyncio
from io import BytesIO
from threading import RLock
import wave

import pytest
from starlette.datastructures import UploadFile
from core.storage_provider import SQLiteStructuredRecordStore
from backend.memory_app.workspace_items import WorkspaceItems
from backend.memory_app.workspace_intake import WorkspaceIntake
from backend.memory_app.processing_lease import ProcessingLease
from backend.memory_app.server_jobs import ServerJobScheduler, QuotaError
from backend.memory_app.v2.devices import DeviceRegistry
from backend.memory_app.v2.server_users import ServerUsers


def fixture(tmp_path, limit):
    devices = DeviceRegistry(tmp_path/'server')
    pair = devices.exchange(devices.issue_pairing(user_id='local-user',actor='install')['code'],name='admin')
    admin = devices.authenticate(pair['key'])
    users = ServerUsers(tmp_path,records=devices.records)
    user = users.create(admin, name='乙')
    users.update(admin, user['user_id'], expected_revision=1, storage_limit_mb=limit)
    root = users.root_for(user['user_id'])
    (root/'workspace').mkdir()
    records = SQLiteStructuredRecordStore(root/'records.sqlite3')
    items = WorkspaceItems(records, ProcessingLease(records,'workspace_items','instance-one'),RLock())
    jobs = ServerJobScheduler(users)
    owner = WorkspaceIntake(root, items, object(), admission=lambda: jobs.intake(user['user_id']))
    return jobs, owner, records


def test_zero_quota_refuses_real_remember_before_any_item_and_with_items_preserves_guard(tmp_path):
    jobs, owner, records = fixture(tmp_path,0)
    async def scenario():
        for actual in (owner,owner.with_items(owner.items,owner.models)):
            with pytest.raises(QuotaError,match='storage_quota_exceeded'):
                await actual.add_text({'project_id':'default','text':'合成记住'})
        assert records.list('workspace_items') == ()
        await jobs.close()
    asyncio.run(scenario())


def test_concurrent_actual_uploads_share_user_admission_and_budget_known_bytes(tmp_path):
    jobs, owner, records = fixture(tmp_path,1)
    async def scenario():
        files = []
        for name in ('one.wav','two.wav'):
            data = BytesIO()
            with wave.open(data,'wb') as output:
                output.setnchannels(1); output.setsampwidth(2); output.setframerate(16000)
                output.writeframes(b'\0'*699956)
            assert data.tell() == 700000
            data.seek(0)
            files.append(UploadFile(file=data,filename=name))
        results = await asyncio.gather(*(owner.add_file('default',file) for file in files),return_exceptions=True)
        assert sum(isinstance(value,QuotaError) for value in results)==1
        assert sum(isinstance(value,dict) for value in results)==1
        assert len(records.list('workspace_items')) == 1
        assert sum(path.stat().st_size for path in owner.root.glob('*.wav')) == 700000
        assert jobs.storage_bytes(owner.runtime_root.name) <= 1048576
        await jobs.close()
    asyncio.run(scenario())
