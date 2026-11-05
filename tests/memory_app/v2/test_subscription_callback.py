import asyncio
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from urllib.parse import urlencode
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from starlette.requests import Request

from backend.memory_app.chatgpt_subscription import ChatGPTSubscriptions, SubscriptionError, _callback_parameters
from backend.memory_app.model_config import ModelConfiguration
from backend.memory_app.v2.subscriptions import install_subscription_routes, _login_owner
from backend.memory_app.v2.devices import DeviceRegistry
from backend.memory_app.v2.server_users import ServerUsers
from backend.memory_app.v2.server_models import ServerModels
from backend.memory_app.server_runtime import ServerContext
from backend.security.device_auth import ServerDeviceAuth
from backend.security.user_context import authorize_user, user_context
from backend.security.secrets import InMemorySecretStore
from backend.shared.deployment import DeploymentLayout
from core.storage_provider import SQLiteStructuredRecordStore


@pytest.fixture
def domain(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / 'subscription.sqlite3')
    # 保留真实 HTTP 传输，仅观察请求次数，不提供供应商响应。
    requests = []
    client = httpx.Client(timeout=15, follow_redirects=False, trust_env=False,
                         event_hooks={'request': [lambda request: requests.append(request.method)]})
    service = ChatGPTSubscriptions(records, InMemorySecretStore(), client=client)
    yield service
    service.close()
    client.close()
    assert requests == []


def pending(service, *, owner=('local-user', 'desktop')):
    # 仅建立待验证的域状态；不签发授权码、令牌或替代供应商响应。
    identity = uuid4().hex
    state = secrets.token_urlsafe(32)
    entry = {'state': 'pending', 'owner': owner, 'revision': 0,
             'expires_at': time.time() + 600, 'oauth_state': state,
             'redirect_uri': 'http://127.0.0.1:18104/auth/callback'}
    service._attempts[identity] = entry
    return identity, entry


def callback(entry, **values):
    return entry['redirect_uri'] + '?' + urlencode({'state': entry['oauth_state'], **values})


@pytest.mark.parametrize('changed', [
    'http://localhost:18104/auth/callback',
    'http://127.0.0.1:18105/auth/callback',
    'https://127.0.0.1:18104/auth/callback',
    'http://127.0.0.1:18104/callback',
    'http://127.0.0.1:18104/x/../auth/callback',
    'http://user@127.0.0.1:18104/auth/callback',
    'https://example.invalid/auth/callback',
])
def test_callback_requires_this_exact_redirect(domain, changed):
    _, entry = pending(domain)
    with pytest.raises(SubscriptionError, match='subscription_callback_invalid'):
        domain._claim_callback(entry, changed + '?state=' + entry['oauth_state'] + '&error=access_denied')
    assert not entry.get('callback_received')
    assert domain.records.list('v2_subscription_profiles') == ()


@pytest.mark.parametrize('suffix', [
    '&state=duplicate', '&error=duplicate', '&code=untrusted', '&unexpected=value',
    '&iss=https%3A%2F%2Fexample.invalid', '#fragment', '#', '&scope=%FF',
])
def test_callback_rejects_ambiguous_parameters_without_consumption(domain, suffix):
    _, entry = pending(domain)
    with pytest.raises(SubscriptionError, match='subscription_callback_invalid'):
        domain._claim_callback(entry, callback(entry, error='access_denied') + suffix)
    assert not entry.get('callback_received')


def test_callback_wrong_state_whitespace_size_and_missing_outcome(domain):
    _, entry = pending(domain)
    for value in (entry['redirect_uri'] + '?state=wrong&error=access_denied',
                  callback(entry, error='access_denied') + '\n',
                  'x' * 16001, callback(entry)):
        with pytest.raises(SubscriptionError, match='subscription_callback_invalid'):
            domain._claim_callback(entry, value)
    assert not entry.get('callback_received')


def test_callback_preserves_issued_client_id_and_scope_parameters():
    state = secrets.token_urlsafe(32)
    redirect = 'http://127.0.0.1:18104/auth/callback'
    values = {'state': state, 'code': secrets.token_urlsafe(20), 'client_id': 'oaiapp_example',
              'scope': 'openid chatgpt.tokens.use.direct'}
    result = _callback_parameters(redirect + '?' + urlencode(values), redirect, state)
    assert result == {key: [value] for key, value in values.items()}


def test_callback_claim_is_single_use_across_concurrent_delivery(domain):
    _, entry = pending(domain)
    address = callback(entry, error='access_denied')
    def claim():
        try:
            domain._claim_callback(entry, address)
            return 'accepted'
        except SubscriptionError as error:
            return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: claim(), range(2)))
    assert sorted(results) == ['accepted', 'subscription_callback_consumed']


def test_callback_expired_cancelled_and_revision_changes_do_not_claim(domain):
    identity, entry = pending(domain)
    address = callback(entry, error='access_denied')
    entry['expires_at'] = time.time() - 1
    with pytest.raises(SubscriptionError, match='subscription_login_expired'):
        domain._claim_callback(entry, address)
    assert 'oauth_state' not in entry
    identity, entry = pending(domain)
    address = callback(entry, error='access_denied')
    entry['revision'] = 1
    with pytest.raises(SubscriptionError, match='subscription_revision_conflict'):
        domain._claim_callback(entry, address)
    assert 'oauth_state' not in entry
    identity, entry = pending(domain)
    address = callback(entry, error='access_denied')
    domain.cancel(identity, owner=entry['owner'])
    with pytest.raises(SubscriptionError, match='subscription_callback_consumed'):
        domain._claim_callback(entry, address)
    assert 'oauth_state' not in entry and 'finish' not in entry


def test_attempt_owner_is_required_for_status_complete_and_cancel(domain):
    identity, entry = pending(domain, owner=('space-a', 'device-a'))
    for owner in (None, ('space-b', 'device-a'), ('space-a', 'device-b')):
        assert domain.status(owner=owner)['login'] is None
        for action in (lambda: domain.attempt(identity, owner=owner),
                       lambda: domain.cancel(identity, owner=owner),
                       lambda: domain.complete(identity, callback(entry, error='access_denied'), owner=owner)):
            with pytest.raises(SubscriptionError, match='subscription_login_not_found'):
                action()
    assert domain.status(owner=entry['owner'])['login']['attempt_id'] == identity
    assert not entry.get('callback_received')


def test_pending_status_preserves_issued_url_only_for_its_owner(domain):
    identity, entry = pending(domain, owner=('space-a', 'device-a'))
    # 仅验证内存尝试的读模型，不表示已经生成或兑换真实 OAuth 授权。
    issued_url = 'opaque-issued-authorization-url'
    entry['authorization_url'] = issued_url
    restored = domain.status(owner=entry['owner'])['login']
    assert restored['attempt_id'] == identity
    assert restored['authorization_url'] == issued_url
    for other in (None, ('space-b', 'device-a'), ('space-a', 'device-b')):
        assert domain.status(owner=other)['login'] is None
    entry['callback_received'] = True
    assert 'authorization_url' not in domain.status(owner=entry['owner'])['login']
    entry['callback_received'] = False
    domain._claim_callback(entry, callback(entry, error='access_denied'))
    assert entry['callback_received'] is True
    assert 'authorization_url' not in entry
    entry['expires_at'] = time.time() - 1
    assert domain.status(owner=entry['owner'])['login'] is None
    entry['expires_at'] = time.time() + 600
    domain.cancel(identity, owner=entry['owner'])
    assert domain.status(owner=entry['owner'])['login'] is None
    assert 'authorization_url' not in entry
    assert domain.records.list('v2_subscription_profiles') == ()


def test_oauth_denial_uses_original_finish_without_credentials(domain):
    identity, entry = pending(domain)
    params = domain._claim_callback(entry, callback(entry, error='access_denied'))
    domain._finish_login(entry, params, {}, secrets.token_urlsafe(32),
                         secrets.token_urlsafe(32), entry['redirect_uri'])
    assert domain.attempt(identity, owner=entry['owner'])['error'] == 'subscription_login_denied'
    assert not domain.status(owner=entry['owner'])['connected']
    assert domain.records.list('v2_subscription_profiles') == ()
    assert 'oauth_state' not in entry and 'validate_owner' not in entry


def test_async_completion_runs_original_denial_and_releases_pending_owner(domain):
    identity, entry = pending(domain)
    entry['finish'] = partial(domain._finish_login, entry, previous={}, nonce=secrets.token_urlsafe(32),
                              verifier=secrets.token_urlsafe(32), redirect=entry['redirect_uri'])
    assert domain.has_pending_login()
    result = domain.complete(identity, callback(entry, error='access_denied'), owner=entry['owner'])
    assert result['state'] in {'pending', 'failed'}
    deadline = time.monotonic() + 3
    while domain.attempt(identity, owner=entry['owner'])['state'] == 'pending' and time.monotonic() < deadline:
        time.sleep(0.01)
    assert domain.attempt(identity, owner=entry['owner'])['error'] == 'subscription_login_denied'
    assert not domain.has_pending_login()
    assert domain.records.list('v2_subscription_profiles') == ()


def test_expired_and_cancelled_attempts_do_not_retain_user_application(domain):
    identity, entry = pending(domain)
    assert domain.has_pending_login()
    entry['expires_at'] = time.time() - 1
    assert not domain.has_pending_login()
    domain.cancel(identity, owner=entry['owner'])
    assert not domain.has_pending_login()


def test_real_asgi_callback_errors_are_bounded_and_do_not_echo_body(domain, tmp_path):
    models = ModelConfiguration(domain.records, tmp_path, secrets=domain.secrets, subscriptions=domain)
    application = FastAPI()
    application.state.deployment = DeploymentLayout('desktop', tmp_path)
    install_subscription_routes(application, models=models)
    identity, entry = pending(domain)
    address = callback(entry, error='access_denied')
    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application, client=('127.0.0.1', 1234)),
                                     base_url='http://127.0.0.1:18104') as client:
            status = await client.get('/api/v2/settings/subscriptions')
            assert status.status_code == 200
            assert status.headers['cache-control'] == 'no-store'
            endpoint = '/api/v2/settings/subscriptions/login/' + identity + '/complete'
            for body in ({'callback_url': address, 'extra': address}, {'callback_url': [address]}):
                response = await client.post(endpoint, json=body)
                assert response.status_code == 400
                assert response.json() == {'detail': 'subscription_callback_invalid'}
                assert address not in response.text and entry['oauth_state'] not in response.text
            async def chunks():
                yield b'{"callback_url":"'
                yield b'x' * 21000
                yield b'"}'
            response = await client.post(endpoint, content=chunks())
            assert response.status_code == 413
            assert response.json() == {'detail': 'subscription_callback_too_large'}
            response = await client.post(endpoint, json={'callback_url': 'https://example.invalid/?code=not-a-grant'})
            assert response.status_code == 400 and 'not-a-grant' not in response.text
            assert not entry.get('callback_received')
    asyncio.run(check())


@pytest.mark.parametrize('change', ['device', 'user', 'allocation', 'generation', 'mode', 'lock_order', 'pending_work'])
def test_live_server_owner_rejects_real_authority_changes(tmp_path, change):
    root = tmp_path.resolve()
    registry = DeviceRegistry(root / 'server')
    users = ServerUsers(root, records=registry.records)
    administrator = registry.exchange(registry.issue_pairing(user_id='local-user', actor='install')['code'], name='administrator')
    admin = registry.authenticate(administrator['key'])
    user = users.create(admin, name='callback owner')
    pairing = registry.issue_pairing(user_id=user['user_id'], actor=admin.device_id)
    issued = registry.exchange(pairing['code'], name='callback device')
    identity = registry.authenticate(issued['key'])
    access = authorize_user(users, identity)
    user_root = users.root_for(user['user_id'])
    layout = DeploymentLayout('server', user_root, root)
    server_models = ServerModels(users)
    models = ModelConfiguration(SQLiteStructuredRecordStore(user_root / 'models.sqlite3'), user_root,
        secrets=InMemorySecretStore(), server_model_access=server_models.access_for(user['user_id'], user_root))
    requests = []
    models.subscriptions.client.event_hooks['request'].append(lambda request: requests.append(request.method))
    context = ServerContext(layout, users, registry, ServerDeviceAuth(registry, port=18104), None, None, models=server_models)
    application = FastAPI()
    application.state.deployment = layout
    application.state.device_registry = registry
    application.state.server_context = context
    application.state.server_user_id = user['user_id']
    application.state.recognition_runtime_root = user_root
    application.state.recognition_models = models
    request = Request({'type': 'http', 'app': application, 'client': ('127.0.0.1', 1234),
        'headers': [], 'state': {'device_identity': identity, 'user_access': access,
                                 'server_dispatch_authority': context.auth}})
    try:
        with user_context(access):
            owner, validate = _login_owner(request, models)
        _, entry = pending(models.subscriptions, owner=owner)
        entry['validate_owner'] = validate
        address = callback(entry, error='access_denied')
        validate()
        if change == 'pending_work':
            from backend.memory_app.app import create_server_app
            server = create_server_app(layout=DeploymentLayout('server', root, root), port=18104)
            pool = server.state.server_user_pool
            assert pool._pending_work(application) == ((), True)
            assert pool._pending_work(application, detached_only=True) == ((), False)
            entry['expires_at'] = time.time() - 1
            assert pool._pending_work(application) == ((), False)
            entry['expires_at'] = time.time() + 600
            assert pool._pending_work(application) == ((), True)
            asyncio.run(pool.close())
            assert pool._pending_work(application) == ((), False)
            return
        if change == 'lock_order':
            with ThreadPoolExecutor(max_workers=1) as pool:
                with models._lock:
                    result = pool.submit(models.subscriptions._claim_callback, entry, address).result(timeout=3)
            assert result['error'] == ['access_denied']
            return
        if change == 'device':
            registry.revoke(user['user_id'], identity.device_id, expected_revision=identity.revision)
        elif change == 'user':
            users.update(admin, user['user_id'], expected_revision=user['revision'], disabled=True)
        elif change == 'allocation':
            server_models.update_allocation(admin, user['user_id'], expected_revision=0,
                expected_user_revision=user['revision'], mode='independent', profiles={})
        elif change == 'generation':
            models.update('generation', {'base_url': 'https://example.invalid/v1', 'model': 'not-invoked',
                'api_key': '', 'enabled': False, 'allow_remote': False, 'expected_revision': 0})
        else:
            models.update_generation_mode(mode='api', local_enabled=False,
                local_base_url='http://127.0.0.1:8001/local-model/v1', expected_revision=0)
        with pytest.raises(SubscriptionError):
            models.subscriptions._validate_login(entry)
        with pytest.raises(SubscriptionError):
            models.subscriptions._claim_callback(entry, address)
        assert entry['state'] == 'failed'
        assert 'oauth_state' not in entry and 'validate_owner' not in entry
        assert not models.subscriptions.status(owner=owner)['connected']
    finally:
        models.close()
        assert requests == []
