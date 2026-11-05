import os
import asyncio
from contextvars import copy_context
from pathlib import Path
import subprocess
import sys

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
import pytest

from backend.memory_app.v2.devices import DeviceRegistry
from backend.memory_app.v2.server_users import ServerUsers
from backend.security.user_context import authorize_user, user_context, UserError
from backend.shared.deployment import DeploymentLayout
from backend.shared.server_resources import resource_context


@pytest.fixture
def server(tmp_path_factory, request):
    root = tmp_path_factory.mktemp('srv')
    values = {'CHRIPTMAS_DEPLOY': 'server', 'CHRIPTMAS_APP_ROOT': str(root),
              'CHRIPTMAS_SERVER_MASTER_KEY': Fernet.generate_key().decode('ascii')}
    old = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        from backend.memory_app.app import create_server_app
        registry = DeviceRegistry(root / 'server')
        paired = registry.exchange(registry.issue_pairing(user_id='local-user', actor='install')['code'], name='管理员')
        users = ServerUsers(root, records=registry.records)
        admin = registry.authenticate(paired['key'])
        user = users.create(admin, name='账号甲')
        ordinary = registry.exchange(registry.issue_pairing(user_id=user['user_id'], actor=admin.device_id)['code'], name='成员')
        if getattr(request, 'param', None) == 'interrupted':
            seed_interrupted_reservation(users, registry, admin, user)
        app = create_server_app(layout=DeploymentLayout('server', root / 'users/local-user', root), port=8090)
        with TestClient(app) as client:
            yield client, app, users, registry, admin, paired, user, ordinary
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def seed_interrupted_reservation(users, registry, admin, user):
    from backend.memory_app.v2.server_model_balance import ServerModelBalances, RESERVATIONS
    from backend.memory_app.v2.server_models import ServerModels
    models = ServerModels(users)
    selected = models.create_profile(admin, name='恢复检查模型', purpose='generation', provider='openai',
        base_url='https://api.deepseek.com/v1', model='deepseek-flash', allowed_models=['deepseek-flash'],
        enabled=True, api_key=Fernet.generate_key().decode('ascii'), prices={'deepseek-flash': {
            'input_per_million': '1', 'output_per_million': '2', 'cache_read_per_million': '0'}})
    models.update_allocation(admin, user['user_id'], mode='shared', profiles={'generation': selected['profile_id']},
        expected_revision=0, expected_user_revision=1)
    ServerModelBalances(users, models).adjust(admin, user['user_id'], amount='20',
        expected_revision=0, expected_user_revision=1, reason='恢复检查额度')
    environment = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[3] / 'src'),
        'CHRIPTMAS_BALANCE_PAIRING': registry.issue_pairing(user_id=user['user_id'], actor=admin.device_id)['code']}
    child = subprocess.Popen([sys.executable, str(Path(__file__).parent / 'fixtures/server_balance_child.py'),
        str(users.server_root), user['user_id'], 'qa-startup-recovery'], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=environment)
    try:
        assert child.stdout.readline().strip() == 'reserved'
        child.communicate(input='finished\n', timeout=30)
        assert child.returncode == 0
        assert users.records.read(RESERVATIONS, 'qa-startup-recovery').payload['status'] == 'reserved'
    finally:
        if child.poll() is None:
            child.terminate()
            child.communicate(timeout=15)


@pytest.mark.parametrize('server', ['interrupted'], indirect=True)
def test_server_composition_recovers_actual_exited_process_reservation_before_wallet_reads(server):
    from backend.memory_app.v2.server_model_balance import RESERVATIONS
    _, app, users, _, _, _, _, _ = server
    assert app.state.server_context.model_balances.users.records.database_path == users.records.database_path
    recovered = users.records.read(RESERVATIONS, 'qa-startup-recovery').payload
    assert recovered['status'] == 'pending_review' and recovered['pending_reason'] == 'process_interrupted'
    assert recovered['charged_amount'] is None


def headers(paired):
    return {'Authorization': 'Bearer ' + paired['key']}


def profile(client, paired, *, official=False):
    model = 'deepseek-flash' if official else 'writer'
    response = client.post('/api/v2/server/model-profiles', headers=headers(paired), json={
        'name': '共享生成', 'purpose': 'generation', 'provider': 'openai',
        'base_url': 'https://api.deepseek.com/v1' if official else 'https://provider.example/v1',
        'model': model, 'allowed_models': [model],
        'enabled': True, 'api_key': 'synthetic-central-integration-key',
        'prices': {model: {'input_per_million': '1', 'output_per_million': '2', 'cache_read_per_million': '0'}}})
    assert response.status_code == 201, response.text
    return response.json()


def allocate(client, paired, user, selected, *, revision=0):
    return client.put('/api/v2/server/users/' + user['user_id'] + '/model-allocation', headers=headers(paired),
        json={'mode': 'shared', 'profiles': {'generation': selected['profile_id']},
              'expected_revision': revision, 'expected_user_revision': user['revision']})


def test_real_central_model_routes_cas_permissions_and_target_audit(server):
    client, app, users, registry, admin, paired, user, ordinary = server
    assert app.state.server_context.models.users.records.database_path == users.records.database_path
    selected = profile(client, paired)
    assert 'synthetic-central-integration-key' not in str(selected)
    assert client.get('/api/v2/server/model-profiles', headers=headers(ordinary)).status_code == 403
    denied = allocate(client, ordinary, user, selected)
    assert denied.status_code == 403
    allocated = allocate(client, paired, user, selected)
    assert allocated.status_code == 200, allocated.text
    assert allocate(client, paired, user, selected).status_code == 409
    path = '/api/v2/server/model-profiles/' + selected['profile_id']
    updated = client.put(path, headers=headers(paired), json={'expected_revision': 1, 'name': '共享新名称'})
    assert updated.status_code == 200
    assert client.put(path, headers=headers(paired), json={'expected_revision': 1, 'name': '旧修订'}).status_code == 409
    history = client.get('/api/v2/server/audit', headers=headers(ordinary)).json()['items']
    intents = [row for row in history if row['phase'] == 'intent' and row['method'] == 'PUT'
        and row['path'] == '/api/v2/server/users/{user_id}/model-allocation']
    assert len(intents) == 2
    assert all(row['actor_user_id'] == 'local-user' and row['target_user_id'] == user['user_id'] for row in intents)
    assert {row['status'] for row in history if row['phase'] == 'outcome'
        and row['intent_id'] in {item['audit_id'] for item in intents}} == {200, 409}


def test_shared_asr_rejects_private_key_and_frozen_dispatch_but_can_disable(server):
    from backend.memory_app.v2.server_models import ServerModelError
    from backend.api.tokenhub_asr_provider import freeze_workbench_asr_binding, TokenHubAudioAssetTranscriber, TokenHubAsrError, tokenhub_egress_manifest
    from backend.api.rebuild_storage_runtime import build_rebuild_object_store
    from backend.security.provider_egress import ProviderEgressPolicyStore
    from core.product_core.cloud_asr_provider_settings import TOKENHUB_ASR_SECRET_REF, SaveCloudAsrProviderSettings
    client, app, users, registry, admin, paired, user, ordinary = server
    assert client.get('/api/v2/settings', headers=headers(ordinary)).status_code == 200
    root = users.root_for(user['user_id'])
    child = app.state.server_user_pool._children[user['user_id']].application
    access = authorize_user(users, registry.authenticate(ordinary['key']))
    store, storage = build_rebuild_object_store(root)
    with user_context(access), resource_context(app.state.server_context.resources):
        child.state.container.secret_store.set(TOKENHUB_ASR_SECRET_REF, 'synthetic-private-asr-key')
        SaveCloudAsrProviderSettings(store, now='2026-10-24T00:00:00Z').execute(enabled=True, confirm_enable=True)
        manifest = tokenhub_egress_manifest(root)
        ProviderEgressPolicyStore(root).grant(manifest, manifest_id=manifest.manifest_id, confirm=True)
        frozen = freeze_workbench_asr_binding(runtime_root=root, object_store=store,
            job_id='qa-asr', source_id='qa-source', project_id='default', secret_store=child.state.container.secret_store)
        transcriber = TokenHubAudioAssetTranscriber(root, store, namespace_id=storage.namespace_id,
            secret_store=child.state.container.secret_store)
    assert store.read('workbench_asr_bindings', frozen)['provider'] == 'tokenhub-asr'
    assert allocate(client, paired, user, profile(client, paired)).status_code == 200
    url = '/api/rebuild/settings/cloud-asr-provider'
    public = client.get(url, headers=headers(ordinary))
    assert public.status_code == 200, public.text
    assert public.json()['server_managed'] is True
    assert public.json()['status'] == 'unavailable' and public.json()['has_api_key'] is False
    assert public.json()['unavailable_reason'] == 'server_model_purpose_unavailable'
    assert client.put(url, headers=headers(ordinary), json={'enabled': True, 'confirm_enable': True}).status_code == 403
    async def denied_job():
        with pytest.raises(TokenHubAsrError, match='^server_model_purpose_unavailable$'):
            freeze_workbench_asr_binding(runtime_root=root, object_store=store, job_id='qa-new',
                source_id='qa-source', project_id='default', secret_store=child.state.container.secret_store)
        with pytest.raises(ServerModelError, match='^server_model_purpose_unavailable$'):
            transcriber.execute(audio_asset_id='qa-asset', execution_ref='qa-execution', binding_id=frozen)
    async def submit_denied_job():
        return await app.state.server_context.jobs.submit(user['user_id'], 'qa-unassigned-asr', denied_job)
    with resource_context(app.state.server_context.resources):
        client.portal.start_task_soon(submit_denied_job).result(timeout=30)
    with pytest.raises(TokenHubAsrError, match='^server_asr_identity_required$'):
        TokenHubAudioAssetTranscriber(root, store, namespace_id=storage.namespace_id,
            secret_store=child.state.container.secret_store)
    disabled = client.put(url, headers=headers(ordinary), json={'enabled': False, 'confirm_enable': False})
    assert disabled.status_code == 200 and disabled.json()['enabled'] is False
    assert client.delete(url + '/egress-consent', headers=headers(ordinary)).status_code == 200
    with resource_context(app.state.server_context.resources):
        with pytest.raises(UserError, match='user_unauthorized'):
            app.state.server_context.resources.server_model_access_for(root)
    with user_context(access), resource_context(app.state.server_context.resources):
        with pytest.raises(UserError, match='user_unauthorized'):
            app.state.server_context.resources.server_model_access_for(users.root_for('local-user'))


def test_shared_transport_prices_and_subscription_writes_are_forbidden(server):
    client, app, users, registry, admin, paired, user, ordinary = server
    assert client.post('/api/v2/settings/subscriptions/login', headers=headers(ordinary),
        json={'expected_revision': 0}).status_code == 409
    assert allocate(client, paired, user, profile(client, paired)).status_code == 200
    public = client.get('/api/v2/settings', headers=headers(ordinary))
    assert public.status_code == 200, public.text
    model = public.json()['model']
    generation = model['generation']
    revision = generation['revision']
    requests = [
        ('PUT', '/api/v2/settings/model', {'purpose': 'generation', 'base_url': 'https://other.example/v1',
            'model': 'changed', 'api_key': 'synthetic-other-key', 'expected_revision': revision}),
        ('PATCH', '/api/v2/settings/model-prices', {'purpose': 'generation',
            'rates': {'input_per_million': '3', 'output_per_million': '4', 'cache_read_per_million': '0'},
            'expected_revision': generation['pricing']['revision'], 'expected_configuration_revision': revision}),
        ('POST', '/api/v2/settings/subscriptions/login', {'expected_revision': 0}),
        ('POST', '/api/v2/settings/subscriptions/logout', {'expected_revision': 0}),
        ('DELETE', '/api/v2/settings/subscriptions/login/unknown', None),
        ('PATCH', '/api/v2/settings/subscriptions/selection', {'expected_revision': 0, 'model': None}),
        ('PUT', '/api/recognition/settings', {'purpose': 'generation', 'base_url': 'https://other.example/v1',
            'model': 'changed', 'api_key': 'synthetic-other-key', 'expected_revision': revision}),
    ]
    for method, url, data in requests:
        response = client.request(method, url, headers=headers(ordinary), json=data)
        assert response.status_code == 403, (url, response.status_code, response.text)
        assert response.json()['detail'] == 'server_model_configuration_readonly'
    fast = {'model': 'writer', 'expected_revision': generation['fast_model']['revision'],
        'expected_generation_revision': revision, 'expected_mode_revision': model['generation_mode']['revision']}
    allowed = client.patch('/api/v2/settings/fast-model', headers=headers(ordinary), json=fast)
    assert allowed.status_code == 200 and allowed.json()['model'] == 'writer' and allowed.json()['configured']
    child = app.state.server_user_pool._children[user['user_id']].application
    with user_context(authorize_user(users, registry.authenticate(ordinary['key']))), resource_context(app.state.server_context.resources):
        frozen = child.state.recognition_models.freeze_auxiliary_binding()
    assert frozen['model'] == 'writer'
    denied = client.patch('/api/v2/settings/fast-model', headers=headers(ordinary),
        json={**fast, 'model': 'outside-admin-list', 'expected_revision': allowed.json()['revision']})
    assert denied.status_code == 403 and denied.json()['detail'] == 'server_model_not_allowed'
    provider = client.get('/api/v2/settings/provider-store', headers=headers(ordinary)).json()
    privacy = client.put('/api/v2/settings/provider-store', headers=headers(ordinary), json={
        'enabled': False, 'expected_revision': provider['revision'],
        'expected_generation_revision': provider['generation_revision'], 'expected_mode_revision': provider['mode_revision']})
    assert privacy.status_code == 200 and privacy.json()['enabled'] is False


def test_real_balance_routes_reconciliation_and_target_audit(server):
    client, app, users, registry, admin, paired, user, ordinary = server
    balances = app.state.server_context.model_balances
    assert balances.models is app.state.server_context.models
    url = '/api/v2/server/users/' + user['user_id'] + '/model-balance'
    assert client.get(url, headers=headers(ordinary)).json()['balance'] == '0'
    change = {'amount': '20', 'expected_revision': 0, 'expected_user_revision': 1, 'reason': '测试分配'}
    assert client.post(url + '/adjust', headers=headers(ordinary), json=change).status_code == 403
    assert client.post(url + '/adjust', headers=headers(paired), json=change).status_code == 200
    assert client.post(url + '/adjust', headers=headers(paired), json=change).status_code == 409
    assert allocate(client, paired, user, profile(client, paired, official=True)).status_code == 200
    binding = app.state.server_context.models.access_for(user['user_id'], users.root_for(user['user_id'])).selection('generation')['server_model_binding']
    balances.reserve(actor=registry.authenticate(ordinary['key']), user_id=user['user_id'],
        user_root=users.root_for(user['user_id']), binding=binding, attempt_id='qa-interrupted',
        model='deepseek-flash', max_output_tokens=100)
    pending = balances.mark_unknown('qa-interrupted', reason_code='process_interrupted')
    listed = client.get(url + '/attempts', headers=headers(ordinary))
    assert listed.status_code == 200 and listed.json()['items'][0]['status'] == 'pending_review'
    reconcile_url = url + '/attempts/qa-interrupted/reconcile'
    reconcile = {'amount': '0', 'expected_revision': pending['revision'], 'expected_user_revision': 1, 'reason': '未执行外部请求'}
    assert client.post(reconcile_url, headers=headers(ordinary), json=reconcile).status_code == 403
    reconciled = client.post(reconcile_url, headers=headers(paired), json=reconcile)
    assert reconciled.status_code == 200 and reconciled.json()['charged_amount'] == '0'
    assert client.post(reconcile_url, headers=headers(paired), json=reconcile).status_code == 409
    history = client.get('/api/v2/server/audit', headers=headers(ordinary)).json()['items']
    for route in ('/api/v2/server/users/{user_id}/model-balance/adjust',
                  '/api/v2/server/users/{user_id}/model-balance/attempts/{attempt_id}/reconcile'):
        intents = [row for row in history if row['phase'] == 'intent' and row['method'] == 'POST' and row['path'] == route]
        assert len(intents) == 2 and all(row['target_user_id'] == user['user_id'] for row in intents)
        assert {row['status'] for row in history if row['phase'] == 'outcome'
            and row['intent_id'] in {item['audit_id'] for item in intents}} == {200, 409}


def test_delete_waits_for_actual_job_and_clears_pending_financial_records(server):
    from backend.memory_app.v2.server_model_balance import BALANCES, ENTRIES, RESERVATIONS
    from backend.memory_app.v2.server_models import ALLOCATIONS, PROFILES
    client, app, users, registry, admin, paired, user, ordinary = server
    selected = profile(client, paired, official=True)
    assert allocate(client, paired, user, selected).status_code == 200
    balances = app.state.server_context.model_balances
    balances.adjust(admin, user['user_id'], amount='20', expected_revision=0, expected_user_revision=1, reason='测试分配')
    binding = app.state.server_context.models.access_for(user['user_id'], users.root_for(user['user_id'])).selection('generation')['server_model_binding']
    balances.reserve(actor=registry.authenticate(ordinary['key']), user_id=user['user_id'],
        user_root=users.root_for(user['user_id']), binding=binding, attempt_id='qa-delete-interrupted',
        model='deepseek-flash', max_output_tokens=100)
    balances.mark_unknown('qa-delete-interrupted', reason_code='process_interrupted')
    started, release = asyncio.Event(), asyncio.Event()
    job_tokens = []
    async def actual_job():
        assert app.state.server_context.jobs.active_for(user['user_id']) > 0
        root = users.root_for(user['user_id'])
        job_tokens.extend(app.state.server_user_pool._model_balance_jobs)
        assert any(app.state.server_user_pool.model_balance_job_current(token, user['user_id'], root)
                   for token in job_tokens)
        held = balances.reserve(actor=None, user_id=user['user_id'], user_root=root, binding=binding,
            attempt_id='qa-background-job', model='deepseek-flash', max_output_tokens=100)
        assert held['status'] == 'reserved'
        balances.mark_unknown('qa-background-job', reason_code='process_interrupted')
        started.set()
        await release.wait()
        return 'finished'
    async def submit():
        return await app.state.server_context.jobs.submit(user['user_id'], 'qa-held-job', actual_job)
    future = client.portal.start_task_soon(submit)
    url = '/api/v2/server/users/' + user['user_id']
    try:
        client.portal.call(asyncio.wait_for, started.wait(), 30)
        disabled = client.patch(url, headers=headers(paired), json={'disabled': True, 'expected_revision': 1})
        assert disabled.status_code == 200
        body = {'expected_revision': disabled.json()['revision'], 'confirmation': user['user_id']}
        blocked = client.request('DELETE', url, headers=headers(paired), json=body)
        assert blocked.status_code == 409 and blocked.json()['detail'] == 'user_delete_busy'
    finally:
        client.portal.call(release.set)
        assert future.result(timeout=30) == 'finished'
    assert job_tokens and not any(app.state.server_user_pool.model_balance_job_current(token,
        user['user_id'], users.root_for(user['user_id'])) for token in job_tokens)
    deleted = client.request('DELETE', url, headers=headers(paired), json=body)
    assert deleted.status_code == 200, deleted.text
    for collection in (BALANCES, ENTRIES, RESERVATIONS, ALLOCATIONS):
        assert not [row for row in users.records.list(collection) if row.payload['user_id'] == user['user_id']]
    assert users.records.read(PROFILES, selected['profile_id']) is not None


def test_independent_background_asr_requires_live_job_scope_and_exact_subject(server):
    from backend.api.tokenhub_asr_provider import freeze_workbench_asr_binding, tokenhub_egress_manifest, TokenHubAudioAssetTranscriber
    from backend.api.rebuild_storage_runtime import build_rebuild_object_store
    from backend.security.provider_egress import ProviderEgressPolicyStore
    from core.product_core.cloud_asr_provider_settings import TOKENHUB_ASR_SECRET_REF, SaveCloudAsrProviderSettings
    client, app, users, registry, admin, paired, user, ordinary = server
    assert client.get('/api/v2/settings', headers=headers(ordinary)).status_code == 200
    root = users.root_for(user['user_id'])
    context = app.state.server_context
    child = app.state.server_user_pool._children[user['user_id']].application
    store, storage = build_rebuild_object_store(root)
    with user_context(authorize_user(users, registry.authenticate(ordinary['key']))), resource_context(context.resources):
        child.state.container.secret_store.set(TOKENHUB_ASR_SECRET_REF, 'synthetic-background-asr-key')
        SaveCloudAsrProviderSettings(store, now='2026-10-24T00:00:00Z').execute(enabled=True, confirm_enable=True)
        manifest = tokenhub_egress_manifest(root)
        ProviderEgressPolicyStore(root).grant(manifest, manifest_id=manifest.manifest_id, confirm=True)
    copied, transcribers = [], []
    async def actual_job():
        copied.append(copy_context())
        transcribers.append(TokenHubAudioAssetTranscriber(root, store, namespace_id=storage.namespace_id,
            secret_store=child.state.container.secret_store))
        current = context.resources.server_model_access_for(root)
        assert current.user_id == user['user_id'] and current.selection('asr')['mode'] == 'independent'
        with user_context(authorize_user(users, admin)):
            with pytest.raises(UserError, match='user_unauthorized'):
                context.resources.server_model_access_for(root)
        return freeze_workbench_asr_binding(runtime_root=root, object_store=store,
            job_id='qa-background-asr', source_id='qa-background-source', project_id='default',
            secret_store=child.state.container.secret_store)
    async def submit():
        return await context.jobs.submit(user['user_id'], 'qa-independent-asr', actual_job)
    with resource_context(context.resources):
        future = client.portal.start_task_soon(submit)
        frozen = future.result(timeout=30)
    assert store.read('workbench_asr_bindings', frozen)['provider'] == 'tokenhub-asr'
    with pytest.raises(UserError, match='user_unauthorized'):
        copied[0].run(context.resources.server_model_access_for, root)
    with pytest.raises(UserError, match='user_unauthorized'):
        copied[0].run(transcribers[0]._check_server_model)
    with pytest.raises(UserError, match='user_unauthorized'):
        copied[0].run(transcribers[0]._check_wire_consent)
    assert app.state.server_user_pool._model_balance_jobs == {}


def test_asr_transcriber_revalidates_revoked_device_before_execution_and_wire(server):
    from backend.api.tokenhub_asr_provider import TokenHubAudioAssetTranscriber
    from backend.api.rebuild_storage_runtime import build_rebuild_object_store
    client, app, users, registry, admin, paired, user, ordinary = server
    assert client.get('/api/v2/settings', headers=headers(ordinary)).status_code == 200
    root = users.root_for(user['user_id'])
    child = app.state.server_user_pool._children[user['user_id']].application
    store, storage = build_rebuild_object_store(root)
    access = authorize_user(users, registry.authenticate(ordinary['key']))
    with user_context(access), resource_context(app.state.server_context.resources):
        transcriber = TokenHubAudioAssetTranscriber(root, store, namespace_id=storage.namespace_id,
            secret_store=child.state.container.secret_store)
        transcriber._check_server_model()
        registry.revoke(user['user_id'], ordinary['device']['device_id'],
            expected_revision=ordinary['device']['revision'])
        with pytest.raises(UserError, match='user_unauthorized'):
            transcriber._check_server_model()
        with pytest.raises(UserError, match='user_unauthorized'):
            transcriber._check_wire_consent()
        with pytest.raises(UserError, match='user_unauthorized'):
            transcriber.execute(audio_asset_id='qa-unused', execution_ref='qa-unused', binding_id='qa-unused')
