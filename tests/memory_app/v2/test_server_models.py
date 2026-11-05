from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import os
import sqlite3

import pytest
from cryptography.fernet import Fernet

from backend.memory_app.v2.devices import DeviceRegistry
from backend.memory_app.v2.server_users import ServerUsers
from backend.security.device_identity import DeviceIdentity
from backend.security.secrets import ServerFileSecretStore, build_server_model_secret_store
from backend.memory_app.v2.server_models import (
    ALLOCATIONS, PROFILES, SERVER_MODEL_PURPOSES, ServerModelError, ServerModels,
    validate_server_model_binding,
)


@pytest.fixture
def server_master_key(tmp_path):
    configured = {'CHRIPTMAS_SERVER_MASTER_KEY': Fernet.generate_key().decode('ascii'),
        'CHRIPTMAS_DEPLOY': 'server', 'CHRIPTMAS_APP_ROOT': str(tmp_path)}
    previous = {name: os.environ.get(name) for name in configured}
    os.environ.update(configured)
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


@pytest.fixture
def owners(tmp_path, server_master_key):
    registry = DeviceRegistry(tmp_path / 'server')
    pair = registry.exchange(registry.issue_pairing(user_id='local-user', actor='install')['code'], name='管理设备')
    actor = registry.authenticate(pair['key'])
    users = ServerUsers(tmp_path, records=registry.records)
    user = users.create(actor, name='账号甲')
    other = users.create(actor, name='账号乙')
    models = ServerModels(users)
    return users, registry, actor, user, other, models


def profile_data(**changes):
    return {'name': '共享生成', 'purpose': 'generation', 'provider': 'openai',
            'base_url': 'https://provider.example/v1', 'model': 'writer',
            'allowed_models': ['writer', 'helper'], 'enabled': True,
            'api_key': 'synthetic-central-key', **changes}


def allocate(models, actor, user, profile, *, revision=0):
    return models.update_allocation(actor, user['user_id'], expected_revision=revision,
        expected_user_revision=user['revision'], mode='shared',
        profiles={'generation': profile['profile_id']})


def test_central_profile_uses_real_encryption_and_never_projects_secret_references(owners):
    users, _, actor, user, _, models = owners
    profile = models.create_profile(actor, **profile_data())
    assert profile['revision'] == 1 and profile['has_api_key'] and profile['configured']
    assert models.list_profiles(actor) == [profile]
    public = json.dumps(profile)
    assert 'synthetic-central-key' not in public and 'secret_ref' not in public
    encrypted = (users.server_root / 'server' / 'model-secrets.json').read_text(encoding='utf-8')
    assert 'synthetic-central-key' not in encrypted
    assert not (users.root_for(user['user_id']) / 'secrets.json').exists()
    allocate(models, actor, user, profile)
    access = models.access_for(user['user_id'], users.root_for(user['user_id']))
    selection = access.selection('generation')
    assert selection['configuration']['base_url'] == 'https://provider.example/v1'
    assert access.credential(selection['server_model_binding']).value == 'synthetic-central-key'
    assert 'secret_ref' not in json.dumps(selection)


def test_legacy_account_is_independent_revision_zero_with_exact_typed_binding(owners):
    users, _, _, user, _, models = owners
    access = models.access_for(user['user_id'], users.root_for(user['user_id']))
    selected = access.selection('generation')
    assert selected == {'mode': 'independent', 'purpose': 'generation', 'configuration': None,
        'server_model_binding': {'schema_version': 1, 'user_id': user['user_id'],
            'allocation_revision': 0, 'mode': 'independent', 'purpose': 'generation',
            'profile_id': None, 'profile_revision': 0, 'credential_generation': 0}}
    assert access.validate_binding(selected['server_model_binding']) == selected
    assert models.records.list(ALLOCATIONS) == ()
    invalid = {**selected['server_model_binding'], 'allocation_revision': False}
    with pytest.raises(ServerModelError, match='server_model_binding_invalid'):
        validate_server_model_binding(invalid)
    with pytest.raises(ServerModelError, match='server_model_independent'):
        access.credential(selected['server_model_binding'])


def test_only_fresh_real_admin_can_change_profiles_or_allocations(owners):
    users, registry, actor, user, _, models = owners
    pair = registry.exchange(registry.issue_pairing(user_id=user['user_id'], actor=actor.device_id)['code'], name='普通设备')
    ordinary = registry.authenticate(pair['key'])
    with pytest.raises(ServerModelError, match='server_model_forbidden'):
        models.create_profile(ordinary, **profile_data())
    with pytest.raises(ServerModelError, match='server_model_unauthorized'):
        models.list_profiles(DeviceIdentity(actor.device_id, actor.user_id, actor.revision + 1))
    profile = models.create_profile(actor, **profile_data())
    with pytest.raises(ServerModelError, match='server_model_forbidden'):
        allocate(models, ordinary, user, profile)
    registry.revoke(actor.user_id, actor.device_id, expected_revision=actor.revision)
    with pytest.raises(ServerModelError, match='server_model_unauthorized'):
        models.update_profile(actor, profile['profile_id'], expected_revision=1, enabled=False)
    assert users.records.read(PROFILES, profile['profile_id']).revision == 1


def test_allocation_and_user_cas_prevent_stale_assignment_and_cross_account_binding(owners):
    users, _, actor, user, other, models = owners
    profile = models.create_profile(actor, **profile_data())
    changed = users.update(actor, user['user_id'], expected_revision=user['revision'], storage_limit_mb=20)
    with pytest.raises(ServerModelError, match='server_model_user_revision_conflict'):
        allocate(models, actor, user, profile)
    assert models.records.list(ALLOCATIONS) == ()
    allocate(models, actor, changed, profile)
    with pytest.raises(ServerModelError, match='server_model_revision_conflict'):
        allocate(models, actor, changed, profile)
    source = models.access_for(user['user_id'], users.root_for(user['user_id']))
    target = models.access_for(other['user_id'], users.root_for(other['user_id']))
    binding = source.selection('generation')['server_model_binding']
    with pytest.raises(ServerModelError, match='server_model_binding_invalid'):
        target.credential(binding)
    with pytest.raises(ServerModelError, match='server_model_user_root_invalid'):
        models.access_for(user['user_id'], users.root_for(other['user_id']))


def test_rotation_and_mode_changes_invalidate_frozen_bindings_without_refreezing(owners):
    users, _, actor, user, _, models = owners
    profile = models.create_profile(actor, **profile_data())
    access = models.access_for(user['user_id'], users.root_for(user['user_id']))
    independent = access.selection('generation')['server_model_binding']
    allocate(models, actor, user, profile)
    with pytest.raises(ServerModelError, match='server_model_binding_changed'):
        access.validate_binding(independent)
    old = access.selection('generation')['server_model_binding']
    models.update_profile(actor, profile['profile_id'], expected_revision=1, api_key='synthetic-rotated-key')
    with pytest.raises(ServerModelError, match='server_model_binding_changed'):
        access.credential(old)
    current = access.selection('generation')['server_model_binding']
    assert access.credential(current).value == 'synthetic-rotated-key'
    models.update_allocation(actor, user['user_id'], expected_revision=1,
        expected_user_revision=user['revision'], mode='independent', profiles={})
    with pytest.raises(ServerModelError, match='server_model_binding_changed'):
        access.validate_binding(current)
    assert access.selection('generation')['server_model_binding']['allocation_revision'] == 2


@pytest.mark.parametrize('reason', ['disabled_user', 'deleting_user', 'disabled_profile', 'missing_key', 'missing_purpose'])
def test_disabled_deleted_or_unavailable_shared_credential_cannot_send(owners, reason):
    users, _, actor, user, _, models = owners
    profile = models.create_profile(actor, **profile_data())
    allocate(models, actor, user, profile)
    access = models.access_for(user['user_id'], users.root_for(user['user_id']))
    binding = access.selection('generation')['server_model_binding']
    if reason == 'disabled_user':
        users.update(actor, user['user_id'], expected_revision=1, disabled=True)
    elif reason == 'deleting_user':
        with users.records.begin() as tx:
            tx.put('server_user_deletions', user['user_id'], {'state': 'deleting'}, expected_revision=0)
            tx.commit()
    elif reason == 'disabled_profile':
        models.update_profile(actor, profile['profile_id'], expected_revision=1, enabled=False)
    elif reason == 'missing_key':
        models.update_profile(actor, profile['profile_id'], expected_revision=1, clear_api_key=True)
    else:
        with pytest.raises(ServerModelError, match='server_model_purpose_unavailable'):
            access.selection('embedding')
        return
    with pytest.raises(ServerModelError):
        access.credential(binding)


def test_two_real_central_owners_serialize_allocation_cas(owners):
    users, _, actor, user, _, models = owners
    profile = models.create_profile(actor, **profile_data())
    second = ServerModels(ServerUsers(users.server_root))
    def save(owner):
        try:
            return allocate(owner, actor, user, profile)['revision']
        except ServerModelError as error:
            return str(error)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save, [models, second]))
    assert sorted(results, key=str) == [1, 'server_model_revision_conflict']


@pytest.mark.parametrize('changes', [
    {'purpose': 'other'}, {'base_url': 'http://provider.example/v1'},
    {'base_url': 'https://secret:password@provider.example/v1'},
    {'base_url': 'https://provider.example/v1?key=private'},
    {'enabled': 1}, {'allowed_models': ['helper']},
    {'allowed_models': ['writer', 'writer']}, {'api_key': False},
])
def test_invalid_profile_inputs_do_not_write_central_records(owners, changes):
    _, _, actor, _, _, models = owners
    with pytest.raises(ServerModelError):
        models.create_profile(actor, **profile_data(**changes))
    assert models.records.list(PROFILES) == ()


def test_public_factory_is_central_and_supports_six_credential_purposes(owners):
    users, _, actor, _, _, models = owners
    store = build_server_model_secret_store(users.server_root)
    assert store._path == users.server_root / 'server' / 'model-secrets.json'
    assert SERVER_MODEL_PURPOSES == ('generation', 'embedding', 'rerank', 'vision', 'search', 'asr')
    for purpose in SERVER_MODEL_PURPOSES:
        profile = models.create_profile(actor, **profile_data(name=purpose, purpose=purpose))
        assert profile['purpose'] == purpose
    altered = deepcopy(models.list_profiles(actor))
    altered[0]['allowed_models'].append('another')
    assert all('another' not in item['allowed_models'] for item in models.list_profiles(actor))


def test_public_selection_projects_unavailable_purposes_without_personal_fallback(owners):
    users, _, actor, user, _, models = owners
    profile = models.create_profile(actor, **profile_data())
    allocate(models, actor, user, profile)
    access = models.access_for(user['user_id'], users.root_for(user['user_id']))
    missing = access.public_selection('embedding')
    assert missing == {'mode': 'shared', 'purpose': 'embedding', 'configuration': None,
        'server_model_binding': None, 'available': False,
        'unavailable_reason': 'server_model_purpose_unavailable'}
    models.update_profile(actor, profile['profile_id'], expected_revision=1, enabled=False)
    disabled = access.public_selection('generation')
    assert not disabled['available'] and disabled['server_model_binding'] is None
    assert disabled['configuration']['profile_id'] == profile['profile_id']
    assert disabled['unavailable_reason'] == 'server_model_profile_disabled'
    models.update_profile(actor, profile['profile_id'], expected_revision=2, enabled=True, clear_api_key=True)
    assert access.public_selection('generation')['unavailable_reason'] == 'server_model_credential_unavailable'
    users.update(actor, user['user_id'], expected_revision=1, disabled=True)
    with pytest.raises(ServerModelError, match='server_model_user_disabled'):
        access.public_selection('generation')


def test_central_prices_are_admin_only_per_model_and_invalidate_profile_binding(owners):
    users, registry, actor, user, _, models = owners
    rates = {'input_per_million': '2.5', 'output_per_million': 8, 'cache_read_per_million': '0.04'}
    profile = models.create_profile(actor, **profile_data(prices={'writer': rates}))
    assert profile['prices']['writer'] == {**rates, 'output_per_million': '8'}
    assert 'helper' not in profile['prices']
    allocate(models, actor, user, profile)
    access = models.access_for(user['user_id'], users.root_for(user['user_id']))
    binding = access.selection('generation')['server_model_binding']
    pair = registry.exchange(registry.issue_pairing(user_id=user['user_id'], actor=actor.device_id)['code'], name='用户设备')
    ordinary = registry.authenticate(pair['key'])
    with pytest.raises(ServerModelError, match='server_model_forbidden'):
        models.update_profile(ordinary, profile['profile_id'], expected_revision=1, prices={'writer': rates})
    models.update_profile(actor, profile['profile_id'], expected_revision=1,
        prices={'writer': {**rates, 'output_per_million': '9'}})
    with pytest.raises(ServerModelError, match='server_model_binding_changed'):
        access.validate_binding(binding)
    with pytest.raises(ServerModelError, match='server_model_prices_invalid'):
        models.update_profile(actor, profile['profile_id'], expected_revision=2, prices={'unallowed': rates})
    assert models.get_profile(actor, profile['profile_id'])['revision'] == 2


def test_failed_actual_sqlite_publication_leaves_no_usable_orphan_secret(owners):
    users, _, actor, user, _, models = owners
    with sqlite3.connect(users.records.database_path) as connection:
        connection.execute("CREATE TRIGGER refuse_central_profile BEFORE INSERT ON crp_structured_records "
            "WHEN NEW.collection='server_model_profiles' BEGIN SELECT RAISE(ABORT,'profile_publication_refused'); END")
    with pytest.raises(sqlite3.IntegrityError, match='profile_publication_refused'):
        models.create_profile(actor, **profile_data())
    assert models.list_profiles(actor) == []
    encrypted = json.loads((users.server_root / 'server' / 'model-secrets.json').read_text(encoding='utf-8'))
    assert len(encrypted['records']) == 1
    orphan = next(iter(encrypted['records']))
    with pytest.raises(ServerModelError, match='server_model_profile_not_found'):
        models.update_allocation(actor, user['user_id'], expected_revision=0,
            expected_user_revision=1, mode='shared', profiles={'generation': orphan})
    assert models.records.list(ALLOCATIONS) == ()


def test_changed_real_secret_generation_never_becomes_an_automatically_accepted_binding(owners):
    users, _, actor, user, _, models = owners
    profile = models.create_profile(actor, **profile_data())
    allocate(models, actor, user, profile)
    access = models.access_for(user['user_id'], users.root_for(user['user_id']))
    binding = access.selection('generation')['server_model_binding']
    reference = models.records.read(PROFILES, profile['profile_id']).payload['secret_ref']
    actual = build_server_model_secret_store(users.server_root)
    actual.set(reference, 'synthetic-outside-owner-change')
    with pytest.raises(ServerModelError, match='server_model_credential_unavailable'):
        access.credential(binding)
    assert access.public_selection('generation')['server_model_binding'] is None


def test_actual_server_fernet_failure_preserves_profile_pointer_and_encrypted_file(owners):
    users, _, actor, user, _, _ = owners
    path = users.server_root / 'server' / 'model-secrets.json'
    models = ServerModels(users, secrets=ServerFileSecretStore(path))
    profile = models.create_profile(actor, **profile_data())
    assert json.loads(path.read_text(encoding='utf-8'))['encryption'] == 'fernet-v1'
    allocate(models, actor, user, profile)
    access = models.access_for(user['user_id'], users.root_for(user['user_id']))
    binding = access.selection('generation')['server_model_binding']
    previous = path.read_bytes()
    original_key = os.environ['CHRIPTMAS_SERVER_MASTER_KEY']
    try:
        os.environ['CHRIPTMAS_SERVER_MASTER_KEY'] = 'invalid-master-key'
        with pytest.raises(ServerModelError, match='server_model_secret_unavailable'):
            models.update_profile(actor, profile['profile_id'], expected_revision=1, api_key='synthetic-not-saved')
        assert path.read_bytes() == previous
        assert models.records.read(PROFILES, profile['profile_id']).revision == 1
    finally:
        os.environ['CHRIPTMAS_SERVER_MASTER_KEY'] = original_key
    assert access.credential(binding).value == 'synthetic-central-key'


def test_allocation_binding_reuses_real_central_transaction_for_budget_admission(owners):
    users, _, actor, user, _, models = owners
    profile = models.create_profile(actor, **profile_data())
    allocate(models, actor, user, profile)
    access = models.access_for(user['user_id'], users.root_for(user['user_id']))
    binding = access.selection('generation')['server_model_binding']
    with users.records.begin() as tx:
        assert access.validate_binding(binding, reader=tx)['mode'] == 'shared'
        tx.put('server_model_test_admissions', 'admission', {'binding': binding}, expected_revision=0)
        tx.commit()
    assert users.records.read('server_model_test_admissions', 'admission').payload['binding'] == binding


def test_real_server_routes_apply_original_device_authority_strict_bodies_and_no_secret_echo(owners):
    from fastapi.testclient import TestClient
    from backend.memory_app.app import create_server_app
    from backend.shared.deployment import DeploymentLayout
    from backend.memory_app.v2.server_model_routes import install_server_model_routes
    users, registry, actor, user, _, models = owners
    application = create_server_app(layout=DeploymentLayout('server', users.server_root / 'users/local-user', users.server_root), port=8092)
    install_server_model_routes(application, models=models)
    issued = registry.exchange(registry.issue_pairing(user_id='local-user', actor=actor.device_id)['code'], name='管理浏览器')
    ordinary = registry.exchange(registry.issue_pairing(user_id=user['user_id'], actor=actor.device_id)['code'], name='普通浏览器')
    admin_headers = {'Authorization': 'Bearer ' + issued['key']}
    user_headers = {'Authorization': 'Bearer ' + ordinary['key']}
    with TestClient(application, base_url='http://127.0.0.1:8092') as client:
        assert client.get('/api/v2/server/model-profiles').status_code == 401
        assert client.get('/api/v2/server/model-profiles', headers=user_headers).status_code == 403
        created = client.post('/api/v2/server/model-profiles', json=profile_data(), headers=admin_headers)
        assert created.status_code == 201
        profile = created.json()
        assert 'secret_ref' not in created.text and 'synthetic-central-key' not in created.text
        path = '/api/v2/server/users/' + user['user_id'] + '/model-allocation'
        assigned = client.put(path, headers=admin_headers, json={'mode': 'shared',
            'profiles': {'generation': profile['profile_id']}, 'expected_revision': 0, 'expected_user_revision': 1})
        assert assigned.status_code == 200 and assigned.json()['revision'] == 1
        assert client.get(path, headers=admin_headers).json() == assigned.json()
        changed = client.put('/api/v2/server/model-profiles/' + profile['profile_id'],
            headers=admin_headers, json={'expected_revision': 1, 'api_key': 'synthetic-http-rotation'})
        assert changed.status_code == 200 and 'synthetic-http-rotation' not in changed.text
        assert client.put(path, headers=user_headers, json={'mode': 'independent', 'profiles': {},
            'expected_revision': 1, 'expected_user_revision': 1}).status_code == 403
        assert client.post('/api/v2/server/model-profiles', headers=admin_headers,
            json={**profile_data(), 'secret_ref': 'arbitrary'}).status_code == 400
        assert client.put('/api/v2/server/model-profiles/' + profile['profile_id'],
            headers=admin_headers, json={'expected_revision': 1, 'enabled': False}).status_code == 409
        assert application.state.server_user_pool.loaded_user_ids == ()
