"""Real ASGI identity, target routing, SQLite attribution and admission boundaries."""
import asyncio
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi import APIRouter
from fastapi.testclient import TestClient
from fastapi.responses import StreamingResponse

from core.storage_provider import SQLiteStructuredRecordStore
from backend.shared.deployment import DeploymentLayout
from backend.security.user_context import USER_ACCESS
from backend.memory_app.server_audit import AuditedRecordStore
from backend.memory_app.v2.devices import DeviceRegistry
from backend.memory_app.v2.server_users import ServerUsers


def fixture(tmp_path):
    devices = DeviceRegistry(tmp_path / 'server')
    admin_pair = devices.exchange(devices.issue_pairing(user_id='local-user', actor='install')['code'], name='管理员')
    admin = devices.authenticate(admin_pair['key'])
    users = ServerUsers(tmp_path, records=devices.records)
    target = users.create(admin, name='乙')
    pair = devices.exchange(devices.issue_pairing(user_id=target['user_id'], actor=admin.device_id)['code'], name='乙手机')
    roots = []
    def factory(root, user_id, context):
        roots.append((root, user_id))
        app = FastAPI()
        records = AuditedRecordStore(SQLiteStructuredRecordStore(root / 'records.sqlite3'), namespace='business', user_id=user_id)
        app.state.recognition_records = records
        @app.get('/api/test/space')
        def space():
            access = USER_ACCESS.get()
            return {'user_id': user_id, 'caller': access.caller.user_id}
        @app.put('/api/test/objects/{object_id}')
        def write(object_id: str):
            with records.begin() as tx:
                record = tx.put('objects', object_id, {'title': '合成'}, expected_revision=0)
                tx.commit()
            return {'revision': record.revision, 'by': records.writer_for('objects', object_id, record.revision)}
        @app.get('/api/test/stream')
        def stream():
            async def body():
                assert USER_ACCESS.get().target_user_id == user_id
                yield user_id + '\n'
                await asyncio.sleep(0)
                yield USER_ACCESS.get().caller.user_id
            return StreamingResponse(body())
        @app.put('/api/rebuild/settings/local-ocr-provider')
        def host_command():
            return {'changed': True}
        @app.post('/api/v2/workbench/turns')
        async def ask(request: Request):
            assert (await request.json())['intent'] == 'ask'
            return {'read': True}
        return app
    from backend.memory_app.server_runtime import create_server_application
    layout = DeploymentLayout('server', tmp_path / 'users/local-user', tmp_path)
    app = create_server_application(layout, child_factory=factory, port=8765)
    return app, users, devices, admin, admin_pair, target, pair, roots


def headers(pair, target=None):
    result = {'Authorization': 'Bearer ' + pair['key']}
    if target is not None:
        result['X-Chriptmas-Target-User'] = target
    return result


def test_real_gate_routes_caller_and_target_separately_and_children_load_on_demand(tmp_path):
    app, users, devices, admin, admin_pair, target, pair, roots = fixture(tmp_path)
    assert roots == [] and app.state.server_user_pool.loaded_user_ids == ()
    with TestClient(app) as client:
        assert client.get('/api/test/space').status_code == 401
        assert client.get('/api/test/space', headers=headers(pair, 'local-user')).status_code == 403
        assert roots == []
        own = client.get('/api/test/space', headers=headers(pair)).json()
        assert own == {'user_id': target['user_id'], 'caller': target['user_id']}
        viewed = client.get('/api/test/space', headers=headers(admin_pair, target['user_id'])).json()
        assert viewed == {'user_id': target['user_id'], 'caller': 'local-user'}
        assert roots == [(users.root_for(target['user_id']), target['user_id'])]
        assert client.get('/api/test/stream', headers=headers(admin_pair, target['user_id'])).text == target['user_id'] + '\nlocal-user'
        assert USER_ACCESS.get() is None
        users.update(admin, target['user_id'], expected_revision=1, disabled=True)
        assert client.get('/api/test/space', headers=headers(pair)).status_code == 401


def test_admin_intent_is_durable_before_business_and_by_is_visible_to_visited_user(tmp_path):
    app, users, _, _, admin_pair, target, pair, _ = fixture(tmp_path)
    with TestClient(app) as client:
        response = client.put('/api/test/objects/synthetic-id', headers=headers(admin_pair, target['user_id']))
        assert response.status_code == 200
        by = response.json()['by']
        assert by['by'] == 'admin' and by['actor_user_id'] == 'local-user' and by['target_user_id'] == target['user_id']
        history = client.get('/api/v2/server/audit', headers=headers(pair)).json()['items']
        assert any(row['phase'] == 'intent' and row['path'] == '/api/test/objects/{object_id}' for row in history)
        assert all('synthetic-id' not in row['path'] for row in history if row['phase'] == 'intent')
        with users.records.begin() as tx:
            tx.connection.execute("CREATE TRIGGER reject_audit BEFORE INSERT ON crp_structured_records WHEN NEW.collection='admin_audit' BEGIN SELECT RAISE(ABORT,'blocked'); END")
            tx.commit()
        blocked = client.put('/api/test/objects/not-written', headers=headers(admin_pair, target['user_id']))
        assert blocked.status_code == 503
        business = SQLiteStructuredRecordStore(users.root_for(target['user_id']) / 'records.sqlite3')
        assert business.read('objects', 'not-written') is None


def test_host_command_setting_is_admin_only_and_quota_pauses_new_intake_without_blocking_reads(tmp_path):
    app, users, _, admin, admin_pair, target, pair, _ = fixture(tmp_path)
    with TestClient(app) as client:
        assert client.put('/api/rebuild/settings/local-ocr-provider', headers=headers(pair), json={'command': ['synthetic']}).status_code == 403
        assert client.put('/api/rebuild/settings/local-ocr-provider', headers=headers(admin_pair, target['user_id']), json={'command': ['synthetic']}).status_code == 200
        users.update(admin, target['user_id'], expected_revision=1, storage_limit_mb=0)
        assert client.post('/api/v2/workbench/files', headers=headers(pair)).status_code == 429
        assert client.get('/api/test/space', headers=headers(pair)).status_code == 200
        asked = client.post('/api/v2/workbench/turns', headers=headers(pair), json={'intent':'ask','text':'读取已有资料'})
        assert asked.status_code == 200 and asked.json() == {'read':True}


def test_users_api_uses_real_owner_cas_names_and_pairing_belongs_to_target(tmp_path):
    app, users, devices, _, admin_pair, target, pair, _ = fixture(tmp_path)
    with TestClient(app) as client:
        assert client.post('/api/v2/server/users', headers=headers(pair), json={'name': '无权'}).status_code == 403
        made = client.post('/api/v2/server/users', headers=headers(admin_pair), json={'name': '丙'})
        assert made.status_code == 201 and made.json()['role'] == 'user'
        assert client.post('/api/v2/server/users', headers=headers(admin_pair), json={'name': '丙'}).status_code == 409
        assert client.patch('/api/v2/server/users/' + target['user_id'], headers=headers(admin_pair), json={'expected_revision': 9, 'disabled': True}).status_code == 409
        issued = client.post('/api/v2/server/users/' + target['user_id'] + '/pair', headers=headers(admin_pair), json={})
        assert issued.status_code == 200 and '#code=' in issued.json()['url']
        code = issued.json()['url'].split('#code=')[1]
        registered = devices.exchange(code, name='乙新设备')
        assert registered['device']['user_id'] == target['user_id']


def test_audit_failure_before_first_admin_visit_initializes_no_child_or_business(tmp_path):
    app,users,_,_,admin_pair,target,_,roots=fixture(tmp_path)
    with users.records.begin() as tx:
        tx.connection.execute("CREATE TRIGGER reject_first_audit BEFORE INSERT ON crp_structured_records WHEN NEW.collection='admin_audit' BEGIN SELECT RAISE(ABORT,'blocked'); END")
        tx.commit()
    with TestClient(app) as client:
        response=client.put('/api/test/objects/never-written',headers=headers(admin_pair,target['user_id']))
        assert response.status_code==503
        assert roots==[] and app.state.server_user_pool.loaded_user_ids==()


def test_disabled_user_keys_stop_while_current_admin_keeps_highest_space_permission(tmp_path):
    app,users,devices,admin,admin_pair,target,pair,_=fixture(tmp_path)
    second=devices.exchange(devices.issue_pairing(user_id=target['user_id'],actor=admin.device_id)['code'],name='second-device')
    with TestClient(app) as client:
        users.update(admin,target['user_id'],expected_revision=1,disabled=True)
        for paired in (pair,second):
            assert client.get('/api/test/space',headers=headers(paired)).status_code==401
        viewed=client.get('/api/test/space',headers=headers(admin_pair,target['user_id']))
        assert viewed.status_code==200
        written=client.put('/api/test/objects/admin-maintains-disabled-space',headers=headers(admin_pair,target['user_id']))
        assert written.status_code==200 and written.json()['by']['by']=='admin'


def test_included_router_audit_uses_public_effective_template_without_object_value(tmp_path):
    app,users,_,_,admin_pair,target,pair,_=fixture(tmp_path)
    pool=app.state.server_user_pool
    original=pool.factory
    def factory(root,user_id):
        child=original(root,user_id)
        outer=APIRouter(prefix='/api/nested')
        inner=APIRouter(prefix='/objects')
        @inner.get('/{object_id}')
        def read(object_id:str): return {'read':True}
        outer.include_router(inner)
        child.include_router(outer)
        return child
    pool.factory=factory
    with TestClient(app) as client:
        assert client.get('/api/nested/objects/private-object-name',headers=headers(admin_pair,target['user_id'])).status_code==200
        history=client.get('/api/v2/server/audit',headers=headers(pair)).json()['items']
        intents=[row for row in history if row['phase']=='intent']
        assert any(row['path']=='/api/nested/objects/{object_id}' for row in intents)
        assert all('private-object-name' not in row['path'] for row in intents)
