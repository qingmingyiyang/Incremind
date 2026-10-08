"""原服务器工厂和设备认证的技能消费者；只使用本次临时身份与手写方法。"""
import asyncio
from dataclasses import dataclass, field
import io
import json
import os
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import zipfile

from cryptography.fernet import Fernet
import httpx
import pytest

from backend.recognition import WorkScope
from backend.security.user_context import USER_ACCESS, authorize_user, user_context
from backend.shared.deployment import DeploymentLayout
from tests.memory_app.v2.test_server_product_composition import environment
from tests.memory_app.v2.test_skill_exports import draft
from tests.memory_app.v2.test_skill_exports_full_factory import (
    BASE, STARTUP_ENVIRONMENT, process_memory, standard_event_loop,
)


@dataclass(frozen=True)
class UserDevice:
    user_id: str
    device_id: str
    key: str = field(repr=False)


def headers(device, target=None):
    result = {'Authorization': 'Bearer ' + device.key}
    if target is not None:
        result['X-Chriptmas-Target-User'] = target
    return result


def manual_document():
    document = draft()
    return {**document, 'name': 'verify-delivery', 'description': '准备成果时核对交付限制和来源。',
        'trigger': '准备成果时先核对交付条件。',
        'steps': [{'text': '核对交付限制和来源。', 'sources': [1]}],
        'validation': ['核对限制和来源均有依据。']}


class ASGIClient:
    """只适配原 ASGI 运输；请求仍进入原认证、用户池和产品路由。"""
    def __init__(self, application, loop):
        self.loop = loop
        self.client = httpx.AsyncClient(base_url='http://127.0.0.1:8018', trust_env=False,
            timeout=30, transport=httpx.ASGITransport(app=application, client=('192.0.2.17', 32017)))

    def request(self, method, path, **kwargs):
        return self.loop.run_until_complete(asyncio.wait_for(self.client.request(method, path, **kwargs), 30))

    def get(self, path, **kwargs):
        return self.request('GET', path, **kwargs)

    def post(self, path, **kwargs):
        return self.request('POST', path, **kwargs)

    def patch(self, path, **kwargs):
        return self.request('PATCH', path, **kwargs)

    def close(self):
        self.loop.run_until_complete(asyncio.wait_for(self.client.aclose(), 30))


@pytest.fixture
def original_server_factory(tmp_path, monkeypatch):
    assert os.name == 'nt'
    requested = os.environ.get('CHRIPTMAS_SKILL_SERVER_TEST_ROOT')
    root = tmp_path if requested is None else Path(requested).resolve()
    if requested is not None:
        assert root.is_absolute() and not root.exists()
        root.mkdir()
    for key in STARTUP_ENVIRONMENT:
        monkeypatch.delenv(key, raising=False)
    for key, value in environment(root, tmp_path).items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv('CHRIPTMAS_SKILL_TEST_PORT', '8018')
    monkeypatch.setenv('LITELLM_LOCAL_MODEL_COST_MAP', 'True')
    # 原 server secret store 接收本次随机测试值，不读取或输出任何既有密钥。
    monkeypatch.setenv('CHRIPTMAS_SERVER_MASTER_KEY', Fernet.generate_key().decode('ascii'))
    binaries, authentication = root / 'empty-bin', root / 'empty-auth'
    binaries.mkdir()
    authentication.mkdir()
    monkeypatch.setenv('PATH', str(binaries))
    monkeypatch.setenv('CODEX_HOME', str(authentication))
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(authentication))
    loop = standard_event_loop()
    audit = {'active': True, 'spawns': 0, 'blocked_connections': []}
    wire = {'calls': 0}
    application, client, lifetime = None, None, None
    entered = False
    children = []

    def observe(event, arguments):
        if not audit['active']:
            return
        if event == 'subprocess.Popen':
            audit['spawns'] += 1
            raise RuntimeError('服务器技能消费者不启动原生客户端')
        if event == 'socket.connect':
            address = arguments[1]
            audit['blocked_connections'].append(address[1] if isinstance(address, tuple) else None)
            raise RuntimeError('原 ASGI 运输不允许真实网络连接')

    sys.addaudithook(observe)
    try:
        from backend.memory_app.app import create_server_app
        layout = DeploymentLayout('server', root / 'users/local-user', root)
        application = create_server_app(layout=layout, port=8018)
        assert application.state.server_user_pool.loaded_user_ids == ()
        assert application.state.server_context.layout.server_root == root
        lifetime = application.router.lifespan_context(application)
        loop.run_until_complete(asyncio.wait_for(lifetime.__aenter__(), 40))
        entered = True
        client = ASGIClient(application, loop)
        actual = SimpleNamespace(app=application, client=client, loop=loop, root=root,
            registry=application.state.device_registry, users=application.state.server_users,
            audit=audit, wire=wire, children=children, monkeypatch=monkeypatch)
        # 只有原 bootstrap 配对权威签发临时管理员；原公开兑换接口仍执行。
        issued = actual.registry.issue_pairing(user_id='local-user', actor='install')
        exchanged = client.post('/api/v2/devices/exchange', json={'code': issued['code'], 'name': '测试管理员'})
        assert exchanged.status_code == 201
        paired = exchanged.json()
        actual.admin = UserDevice('local-user', paired['device']['device_id'], paired['key'])
        yield actual
    finally:
        try:
            active_before_shutdown = [child.state.ai_turn_runner.active_turn_ids for child in children]
            try:
                try:
                    if client is not None:
                        client.close()
                finally:
                    if entered:
                        loop.run_until_complete(asyncio.wait_for(lifetime.__aexit__(None, None, None), 20))
            finally:
                shutdowns = [child.state.ai_turn_runner.shutdown(timeout_seconds=5) for child in children]
            assert all(ids == () for ids in active_before_shutdown) and all(ids == () for ids in shutdowns)
            if entered:
                assert application.state.server_user_pool.loaded_user_ids == ()
            for child in children:
                assert child.state.recognition_models.subscriptions.client.is_closed
            assert audit['spawns'] == 0 and audit['blocked_connections'] == [] and wire['calls'] == 0
            assert not tuple(authentication.iterdir()) and not tuple(binaries.iterdir())
        finally:
            audit['active'] = False
            if not loop.is_closed():
                loop.close()
            (root / 'process-memory.json').write_text(json.dumps({**process_memory(),
                'data_root': str(root), 'spawn_calls': audit['spawns'], 'configured_port': 8018,
                'allowed_connection_ports': [], 'blocked_connection_ports': audit['blocked_connections'],
                'wire_calls': wire['calls'], 'remaining_children':
                    list(application.state.server_user_pool.loaded_user_ids) if application is not None else [],
            }), encoding='utf-8')


def create_user(actual, name):
    created = actual.client.post('/api/v2/server/users', headers=headers(actual.admin), json={'name': name})
    assert created.status_code == 201
    user_id = created.json()['user_id']
    issued = actual.registry.issue_pairing(user_id=user_id, actor=actual.admin.device_id)
    response = actual.client.post('/api/v2/devices/exchange', json={'code': issued['code'], 'name': name + '测试设备'})
    assert response.status_code == 201
    paired = response.json()
    return UserDevice(user_id, paired['device']['device_id'], paired['key'])


def confirmed_method(actual, device, content):
    own = headers(device)
    settings = actual.client.get('/api/recognition/settings', headers=own)
    assert settings.status_code == 200
    child = actual.app.state.server_user_pool._children[device.user_id].application
    actual.children.append(child)
    expected = actual.users.root_for(device.user_id).resolve()
    assert child.state.server_user_id == device.user_id
    assert child.state.server_context is actual.app.state.server_context
    assert child.state.recognition_runtime_root.resolve() == expected
    assert Path(child.state.container.root_dir).resolve() == expected
    assert child.state.recognition_records.user_id == device.user_id
    assert child.state.recognition_records.database_path.is_relative_to(expected)
    assert child.state.recognition_documents.records is child.state.recognition_records

    def no_provider(**request):
        actual.wire['calls'] += 1
        raise RuntimeError('模型关闭的手写方法不调用提供方')

    # 仅外部 wire 保底拒绝；原模型配置、网关和资格判断不替换。
    actual.monkeypatch.setattr(child.state.recognition_models, '_completion_fn', no_provider)
    saved = actual.client.request('PUT', '/api/recognition/settings', headers=own,
        json={'purpose': 'generation', 'allow_remote': False,
            'expected_revision': settings.json()['generation']['revision']})
    assert saved.status_code == 200
    assert child.state.recognition_models.public()['generation']['allow_remote'] is False
    evidence = actual.client.post('/api/recognition/experiences', headers=own,
        json={'project_id': 'alpha', 'content': '用户明确提出的手写方法依据。'})
    assert evidence.status_code == 200
    identity = actual.registry.authenticate(device.key)
    assert identity is not None and identity.user_id == device.user_id
    # 复用原 test_api._published 的真实 propose 前提，确认仍由本人 HTTP 执行。
    with user_context(authorize_user(actual.users, identity)):
        candidate = child.state.recognition_service.propose(scope=WorkScope('local-user', 'alpha'),
            content=content, conditions=['准备成果时'], source_experience_ids=[evidence.json()['id']])
    assert USER_ACCESS.get() is None
    confirmed = actual.client.post('/api/v2/library/insights/' + candidate.id + '/confirm', headers=own,
        json={'project_id': 'alpha', 'expected_revision': candidate.revision})
    assert confirmed.status_code == 200
    method = confirmed.json()
    assert method['state'] == 'active' and method['conditions'] == ['准备成果时']
    return child, method


def manual_export(actual, device, method):
    own = headers(device)
    methods = actual.client.get(BASE + '/methods', headers=own)
    assert methods.status_code == 200
    assert [item['id'] for item in methods.json()['items']] == [method['id']]
    created = actual.client.post(BASE, headers=own, json={'sources': [
        {'id': method['id'], 'revision': method['revision']}], 'document': manual_document()})
    assert created.status_code == 200
    saved = created.json()
    assert saved['reviewed'] is False and saved['needs_update'] is False
    return saved


def test_original_server_factory_own_method_manual_review_zip_freezes_user_sources(original_server_factory):
    actual = original_server_factory
    device = create_user(actual, '技能本人')
    child, method = confirmed_method(actual, device, '交付前核对限制和来源。')
    saved = manual_export(actual, device, method)
    path, own = BASE + '/' + saved['id'], headers(device)
    assert actual.client.post(path + '/download', headers=own,
        json={'expected_revision': saved['revision']}).status_code == 409
    assert actual.client.post(path + '/review', headers=own,
        json={'expected_revision': saved['revision'] + 1}).status_code == 409
    reviewed = actual.client.post(path + '/review', headers=own,
        json={'expected_revision': saved['revision']})
    assert reviewed.status_code == 200 and reviewed.json()['reviewed'] is True
    frozen = child.state.recognition_records.read('v2_skill_exports', saved['id']).payload['sources']
    assert frozen[0]['id'] == method['id'] and frozen[0]['revision'] == method['revision']
    from backend.memory_app.source_egress import SourceEgressService
    snapshot = SourceEgressService(child.state.recognition_records).snapshot(WorkScope('local-user', 'alpha'),
        [{'type': 'recognition', 'id': method['id'], 'revision': method['revision']}])
    assert frozen[0]['source_snapshot'] == snapshot
    archive = actual.client.post(path + '/download', headers=own,
        json={'expected_revision': reviewed.json()['revision']})
    assert archive.status_code == 200 and archive.headers['content-type'] == 'application/zip'
    with zipfile.ZipFile(io.BytesIO(archive.content)) as package:
        assert set(package.namelist()) == {'verify-delivery/SKILL.md', 'verify-delivery/references/methods.md'}
        assert all(part in package.read('verify-delivery/references/methods.md').decode('utf-8')
            for part in (method['id'], '修订：' + str(method['revision']), '交付前核对限制和来源。'))
    state = actual.client.get(BASE, headers=own)
    assert state.status_code == 200 and state.json()['generation_available'] is False
    assert state.json()['local_folder']['available'] is False
    current = child.state.recognition_records.read('v2_skill_exports', saved['id'])
    preferences = child.state.recognition_records.list('v2_skill_export_preferences')
    forbidden = actual.root / 'forbidden-server-folder'
    from backend.memory_app.v2.skill_exports import SkillExports
    from backend.memory_app.v2.skill_folder import write_reviewed_folder
    codes = {SkillExports.export_folder.__code__: 'service', write_reviewed_folder.__code__: 'writer'}
    calls = {'service': 0, 'writer': 0}

    def observe_call(frame, event, arg):
        if event == 'call' and frame.f_code in codes:
            calls[codes[frame.f_code]] += 1

    previous, previous_threads = sys.getprofile(), threading.getprofile()
    try:
        # 观察原函数入口，不替换或包装被测服务与真实文件写入函数。
        threading.setprofile_all_threads(observe_call)
        refused = actual.client.post(path + '/folder', headers=own, json={
            'expected_revision': current.revision, 'directory': str(forbidden),
            'confirm_first_export': True, 'expected_confirmation_revision': 0})
    finally:
        threading.setprofile_all_threads(previous_threads)
        sys.setprofile(previous)
    assert refused.status_code == 503 and refused.json()['detail'] == 'skill_user_domain_unavailable'
    assert calls == {'service': 0, 'writer': 0} and not forbidden.exists()
    assert child.state.recognition_records.read('v2_skill_exports', saved['id']) == current
    assert child.state.recognition_records.list('v2_skill_export_preferences') == preferences
    assert child.state.recognition_records.list('v2_memory_turn_keys') == () and actual.wire['calls'] == 0


def test_original_server_factory_two_users_cannot_read_or_mutate_methods_or_skill_exports(original_server_factory):
    actual = original_server_factory
    one, two = create_user(actual, '技能甲'), create_user(actual, '技能乙')
    assert actual.client.get(BASE + '/methods').status_code == 401
    assert actual.client.get(BASE + '/methods', headers={'Authorization': 'Bearer invalid-test-device'}).status_code == 401
    assert actual.client.get(BASE + '/methods', headers=headers(one, two.user_id)).status_code == 403
    assert actual.client.get(BASE + '/methods', headers=headers(two, one.user_id)).status_code == 403
    assert actual.app.state.server_user_pool.loaded_user_ids == ()
    child_one, method_one = confirmed_method(actual, one, '交付前核对限制。')
    child_two, method_two = confirmed_method(actual, two, '交付前核对来源。')
    assert child_one.state.recognition_records.database_path != child_two.state.recognition_records.database_path
    assert child_one.state.recognition_models is not child_two.state.recognition_models
    assert child_one.state.recognition_records.read('recognitions', method_two['id']) is None
    assert child_two.state.recognition_records.read('recognitions', method_one['id']) is None
    # 原已加载 child 自身也经过真实设备认证；缺调度/USER_ACCESS不能取得导出资格。
    direct = ASGIClient(child_one, actual.loop)
    try:
        assert direct.get(BASE + '/methods', headers=headers(one)).status_code == 503
    finally:
        direct.close()
    export_one = manual_export(actual, one, method_one)
    export_two = manual_export(actual, two, method_two)
    before_one = child_one.state.recognition_records.read('v2_skill_exports', export_one['id'])
    before_two = child_two.state.recognition_records.read('v2_skill_exports', export_two['id'])
    for caller, foreign, foreign_method in ((one, export_two, method_two), (two, export_one, method_one)):
        own, path = headers(caller), BASE + '/' + foreign['id']
        assert actual.client.get(path, headers=own).status_code == 404
        assert actual.client.patch(path, headers=own, json={
            'expected_revision': foreign['revision'], 'document': manual_document()}).status_code == 404
        for action in ('review', 'download'):
            assert actual.client.post(path + '/' + action, headers=own,
                json={'expected_revision': foreign['revision']}).status_code == 404
        assert actual.client.post(BASE, headers=own, json={'sources': [
            {'id': foreign_method['id'], 'revision': foreign_method['revision']}], 'document': manual_document()}).status_code == 409
    for caller, exported in ((one, export_one), (two, export_two)):
        listed = actual.client.get(BASE, headers=headers(caller))
        assert listed.status_code == 200 and [item['id'] for item in listed.json()['items']] == [exported['id']]
    assert child_one.state.recognition_records.read('v2_skill_exports', export_one['id']) == before_one
    assert child_two.state.recognition_records.read('v2_skill_exports', export_two['id']) == before_two
    assert child_one.state.recognition_records.list('v2_memory_turn_keys') == ()
    assert child_two.state.recognition_records.list('v2_memory_turn_keys') == () and actual.wire['calls'] == 0
