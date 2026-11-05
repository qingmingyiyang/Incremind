import json

import pytest
from fastapi.testclient import TestClient

from backend.memory_app.v2.external_proxy_transport import _headers
from backend.shared.deployment import DeploymentLayout


@pytest.fixture(scope='module')
def server(tmp_path_factory):
    from backend.memory_app.app import create_server_app
    root = tmp_path_factory.mktemp('proxy-server')
    app = create_server_app(layout=DeploymentLayout('server', root / 'users/local-user', root), port=18103)
    registry, users = app.state.device_registry, app.state.server_users
    admin_pair = registry.exchange(registry.issue_pairing(user_id='local-user', actor='install')['code'], name='代理审查管理员')
    admin = registry.authenticate(admin_pair['key'])
    first, second = users.create(admin, name='代理甲'), users.create(admin, name='代理乙')
    pair = registry.exchange(registry.issue_pairing(user_id=first['user_id'], actor=admin.device_id)['code'], name='代理客户端')
    with TestClient(app, base_url='https://testserver', client=('198.51.100.21', 53100)) as http:
        yield app, http, admin_pair, admin, pair, first, second


def route(client='codex'):
    return '/api/v2/external-agent/proxy/' + client + '/v1/' + ('messages' if client == 'claude' else 'responses')


@pytest.mark.parametrize('device_headers', [[], [('X-Chriptmas-Device-Key', '')],
    [('X-Chriptmas-Device-Key', 'a' * 43), ('X-Chriptmas-Device-Key', 'b' * 43)]])
def test_exact_proxy_route_requires_one_nonempty_device_header(server, device_headers):
    _, http, *_ = server
    response = http.post(route(), headers=device_headers, json={})
    assert response.status_code == 401
    assert response.json() == {'detail': 'external_proxy_credentials_invalid'}


@pytest.mark.parametrize('client', ['claude', 'codex'])
def test_actual_server_enters_selected_child_then_requires_separate_model_credentials(server, client):
    app, http, _, _, pair, first, _ = server
    response = http.post(route(client), headers={'X-Chriptmas-Device-Key': pair['key']}, json={})
    assert response.status_code == 401
    assert response.json() == {'detail': 'external_proxy_model_credentials_required'}
    child = app.state.server_user_pool._children[first['user_id']].application
    assert child.state.server_user_id == first['user_id']
    assert child.state.recognition_runtime_root == app.state.server_users.root_for(first['user_id'])
    assert child.state.external_context.records is child.state.recognition_records
    assert child.state.external_context.owner_id == 'local-user'
    assert child.state.external_context.records.user_id == first['user_id']


def test_actual_admin_target_and_normal_user_cross_space_denial(server):
    app, http, admin_pair, _, pair, first, second = server
    target = {'X-Chriptmas-Target-User': second['user_id']}
    denied = http.post(route(), headers={'X-Chriptmas-Device-Key': pair['key'], **target}, json={})
    assert denied.status_code == 403
    accepted_identity = http.post(route(), headers={'X-Chriptmas-Device-Key': admin_pair['key'], **target}, json={})
    assert accepted_identity.status_code == 401
    assert accepted_identity.json()['detail'] == 'external_proxy_model_credentials_required'
    first_app = app.state.server_user_pool._children[first['user_id']].application
    second_app = app.state.server_user_pool._children[second['user_id']].application
    assert first_app.state.recognition_records is not second_app.state.recognition_records
    assert first_app.state.recognition_runtime_root != second_app.state.recognition_runtime_root


@pytest.mark.parametrize('client,header', [('claude', 'x-api-key'), ('codex', 'authorization')])
def test_model_header_is_separate_and_device_key_never_enters_body_or_error(server, client, header):
    _, http, _, _, pair, *_ = server
    headers = {'X-Chriptmas-Device-Key': pair['key'], header: 'synthetic-model-credential'}
    body = {'model': 'unused', 'input': pair['key'], 'messages': [{'role': 'user', 'content': pair['key']}]}
    escaped = json.dumps(body).replace(pair['key'][0], '\\u%04x' % ord(pair['key'][0]))
    for raw in (json.dumps(body), escaped):
        response = http.post(route(client), headers=headers, content=raw)
        assert response.status_code == 400
        assert response.json() == {'detail': 'external_proxy_body_invalid'}
        assert pair['key'] not in response.text and 'synthetic-model-credential' not in response.text


@pytest.mark.parametrize('header', ['authorization', 'x-api-key'])
def test_device_key_as_model_credential_and_duplicate_model_auth_are_rejected(server, header):
    _, http, _, _, pair, *_ = server
    response = http.post(route(), headers={'X-Chriptmas-Device-Key': pair['key'], header: pair['key']}, json={})
    assert response.status_code == 400
    duplicate = http.post(route(), headers=[('X-Chriptmas-Device-Key', pair['key']),
        (header, 'first-synthetic-model'), (header, 'second-synthetic-model')], json={})
    assert duplicate.status_code == 401


def test_server_plain_http_queries_unknown_paths_and_revoked_device_cannot_forward(server):
    app, http, _, admin, pair, first, _ = server
    headers = {'X-Chriptmas-Device-Key': pair['key']}
    assert http.post('http://testserver' + route(), headers=headers, json={}).status_code == 400
    assert http.post(route() + '?project=alpha', headers=headers, json={}).status_code == 400
    assert http.post('/api/v2/settings/external-proxy', headers=headers, json={}).status_code == 401
    fresh = app.state.device_registry.exchange(app.state.device_registry.issue_pairing(
        user_id=first['user_id'], actor=admin.device_id)['code'], name='代理已作废设备')
    app.state.device_registry.revoke(first['user_id'], fresh['device']['device_id'],
        expected_revision=fresh['device']['revision'])
    revoked = http.post(route(), headers={'X-Chriptmas-Device-Key': fresh['key']}, json={})
    assert revoked.status_code == 401


def test_real_header_filter_keeps_model_credentials_and_discards_application_identity():
    result = _headers([('Authorization', 'Bearer synthetic-model-only'),
        ('X-Api-Key', 'synthetic-api-only'), ('X-Chriptmas-Device-Key', 'synthetic-device-only'),
        ('X-Chriptmas-Target-User', 'user-synthetic'), ('Cookie', 'synthetic-cookie')])
    assert result['authorization'] == 'Bearer synthetic-model-only'
    assert result['x-api-key'] == 'synthetic-api-only'
    assert not any(name.startswith('x-chriptmas-') or name == 'cookie' for name in result)
    assert 'synthetic-device-only' not in json.dumps(result)


def test_real_server_lease_rechecks_device_and_account_before_send_or_recording(server):
    from backend.memory_app.v2.external_server_identity import ServerMemoryLease
    from backend.security.user_context import authorize_user
    app, http, _, admin, _, first, _ = server
    registry, users = app.state.device_registry, app.state.server_users
    fresh = registry.exchange(registry.issue_pairing(user_id=first['user_id'], actor=admin.device_id)['code'], name='代理短期身份')
    assert http.post(route(), headers={'X-Chriptmas-Device-Key': fresh['key']}, json={}).status_code == 401
    state = app.state.server_user_pool._children[first['user_id']].application.state
    identity = registry.authenticate(fresh['key'])
    lease = ServerMemoryLease(state, identity, authorize_user(users, identity), fresh['key'])
    lease.validate()
    registry.revoke(first['user_id'], fresh['device']['device_id'], expected_revision=fresh['device']['revision'])
    with pytest.raises(ValueError, match='external_server_identity_unavailable'):
        lease.validate()
    lease.close()
    temporary = users.create(admin, name='代理停用检查')
    pair = registry.exchange(registry.issue_pairing(user_id=temporary['user_id'], actor=admin.device_id)['code'], name='代理停用身份')
    assert http.post(route(), headers={'X-Chriptmas-Device-Key': pair['key']}, json={}).status_code == 401
    child = app.state.server_user_pool._children[temporary['user_id']].application
    identity = registry.authenticate(pair['key'])
    lease = ServerMemoryLease(child.state, identity, authorize_user(users, identity), pair['key'])
    users.update(admin, temporary['user_id'], expected_revision=temporary['revision'], disabled=True)
    with pytest.raises(ValueError, match='external_server_identity_unavailable'):
        lease.validate()
    lease.close()


def test_real_selected_child_private_project_never_prepares_handoff(server):
    from types import SimpleNamespace
    from backend.memory_app.kernel.ai_runtime import get_or_build_ai_runtime
    from backend.memory_app.v2.external_agent_settings import external_agent_settings, replace_external_agent_settings
    from backend.memory_app.v2.external_context import DELIVERIES
    from backend.memory_app.v2.external_proxy_handoff import ProxyHandoff
    from backend.memory_app.v2.privacy import set_private_project
    from backend.security.user_context import authorize_user, user_context
    app, http, _, admin, pair, first, second = server
    assert http.post(route(), headers={'X-Chriptmas-Device-Key': pair['key']}, json={}).status_code == 401
    child = app.state.server_user_pool._children[first['user_id']].application
    state, records = child.state, child.state.recognition_records
    before = records.list(DELIVERIES)
    with user_context(authorize_user(app.state.server_users, admin, first['user_id'])):
        current = external_agent_settings(records)
        replace_external_agent_settings(records, {key: value for key, value in current.items()
            if key != 'revision'} | {'allow_remote': True}, expected_revision=current['revision'])
        set_private_project(records, 'default', True, expected_revision=0)
        runtime = get_or_build_ai_runtime(SimpleNamespace(app=child), SimpleNamespace(root_dir=state.recognition_runtime_root))
        handoff = ProxyHandoff(state.workspace_domains.query, state.external_context,
            runtime=runtime, runner=state.ai_turn_runner)
        assert handoff.prepare('codex', '检查私密项目的内容') is None
    assert records.list(DELIVERIES) == before
    other = app.state.server_user_pool._children[second['user_id']].application
    assert external_agent_settings(other.state.recognition_records)['allow_remote'] is False


def test_recording_waiting_for_real_intake_lock_rechecks_revoked_device_before_write(server):
    import asyncio
    from backend.memory_app.v2.external_proxy import _record_conversation
    from backend.memory_app.v2.external_proxy_settings import external_proxy_settings, replace_external_proxy_settings
    from backend.memory_app.v2.external_server_identity import ServerMemoryLease
    from backend.security.user_context import authorize_user, user_context
    from backend.shared.server_resources import resource_context
    app, http, _, admin, _, first, _ = server
    registry, users = app.state.device_registry, app.state.server_users
    pair = registry.exchange(registry.issue_pairing(user_id=first['user_id'], actor=admin.device_id)['code'], name='代理等待录制')
    assert http.post(route(), headers={'X-Chriptmas-Device-Key': pair['key']}, json={}).status_code == 401
    state = app.state.server_user_pool._children[first['user_id']].application.state
    records, identity = state.recognition_records, registry.authenticate(pair['key'])
    access = authorize_user(users, identity)
    lease = ServerMemoryLease(state, identity, access, pair['key'])
    before = records.list('workspace_items')
    async def scenario():
        with user_context(access), resource_context(state.server_context.resources):
            current = external_proxy_settings(records)
            replace_external_proxy_settings(records, {'record_conversations': {'claude': False, 'codex': True}},
                expected_revision=current['revision'])
            async with state.workspace_domains.intake.admission():
                task = asyncio.create_task(_record_conversation(state.workspace_domains, records,
                    client='codex', project='default', text='检查录制等待',
                    recorded='用户请求检查录制等待，助手提供完整的合成回答。', lease=lease))
                await asyncio.sleep(0)
                assert not task.done()
                registry.revoke(first['user_id'], pair['device']['device_id'], expected_revision=pair['device']['revision'])
            with pytest.raises(ValueError, match='external_server_identity_unavailable'):
                await task
    try:
        asyncio.run(scenario())
    finally:
        lease.close()
    assert records.list('workspace_items') == before
