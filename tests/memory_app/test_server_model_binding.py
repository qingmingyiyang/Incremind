import os
import secrets

from cryptography.fernet import Fernet
import pytest

from backend.memory_app.model_config import ModelConfiguration, ModelConfigurationError, _same_governed_public_configuration
from backend.memory_app.model_binding import public_model_configuration
from backend.memory_app.retrieval_models import _public_identity, _public_from_snapshot
from backend.memory_app.v2.devices import DeviceRegistry
from backend.memory_app.v2.server_users import ServerUsers
from backend.memory_app.v2.server_models import ServerModels
from backend.security.secrets import ServerFileSecretStore
from core.storage_provider.sqlite_uow import SQLiteStructuredRecordStore


@pytest.fixture
def configured(tmp_path):
    previous = os.environ.get('CHRIPTMAS_SERVER_MASTER_KEY')
    previous_root = os.environ.get('CHRIPTMAS_APP_ROOT')
    (tmp_path / 'isolated-app').mkdir()
    os.environ['CHRIPTMAS_SERVER_MASTER_KEY'] = Fernet.generate_key().decode('ascii')
    os.environ['CHRIPTMAS_APP_ROOT'] = str(tmp_path / 'isolated-app')
    try:
        devices = DeviceRegistry(tmp_path / 'server')
        pair = devices.exchange(devices.issue_pairing(user_id='local-user', actor='install')['code'], name='管理')
        admin = devices.authenticate(pair['key'])
        users = ServerUsers(tmp_path, records=devices.records)
        user = users.create(admin, name='普通账号')
        root = users.root_for(user['user_id'])
        owner = ServerModels(users)
        private = secrets.token_urlsafe(24)
        central = secrets.token_urlsafe(24)
        models = ModelConfiguration(SQLiteStructuredRecordStore(root / 'models.sqlite3'), root,
            secrets=ServerFileSecretStore(root / 'private-secrets.json'),
            server_model_access=owner.access_for(user['user_id'], root))
        models.update('generation', {'base_url': 'https://private.example/v1', 'model': 'private',
            'api_key': private, 'allow_remote': True, 'enabled': True, 'expected_revision': 0})
        profile = owner.create_profile(admin, name='服务器模型', purpose='generation', provider='openai',
            base_url='https://shared.example/v1', model='main', allowed_models=['main', 'fast'],
            enabled=True, api_key=central, prices={'main': {'input_per_million': '2',
                'output_per_million': '8', 'cache_read_per_million': '1'},
                'fast': {'input_per_million': '1', 'output_per_million': '3', 'cache_read_per_million': '0.5'}})
        yield owner, models, admin, user, profile, private, central
    finally:
        if previous is None:
            os.environ.pop('CHRIPTMAS_SERVER_MASTER_KEY', None)
        else:
            os.environ['CHRIPTMAS_SERVER_MASTER_KEY'] = previous
        if previous_root is None:
            os.environ.pop('CHRIPTMAS_APP_ROOT', None)
        else:
            os.environ['CHRIPTMAS_APP_ROOT'] = previous_root


def share(state):
    owner, _, admin, user, profile, _, _ = state
    return owner.update_allocation(admin, user['user_id'], expected_revision=0,
        expected_user_revision=user['revision'], mode='shared', profiles={'generation': profile['profile_id']})


def test_shared_projection_preserves_consent_and_private_configuration(configured):
    owner, models, admin, user, _, private, central = configured
    share(configured)
    public = models.public()
    assert public['generation']['base_url'] == 'https://shared.example/v1'
    assert public['generation']['allow_remote'] is True
    assert public['generation']['allowed_models'] == ['main', 'fast']
    assert public['generation']['server_managed'] is True
    assert public['embedding']['configured'] is False
    assert central not in str(public) and private not in str(public)
    assert models.snapshot('generation')['api_key'] == central
    owner.update_allocation(admin, user['user_id'], expected_revision=1,
        expected_user_revision=user['revision'], mode='independent', profiles={})
    assert models.snapshot('generation')['api_key'] == private
    assert models.public()['generation']['server_model_binding']['allocation_revision'] == 2


def test_shared_consent_update_retains_personal_key_and_cannot_edit_transport(configured):
    _, models, _, _, _, _, central = configured
    share(configured)
    original = models.records.read('recognition_model_config', 'generation').payload
    result = models.update('generation', {'allow_remote': False, 'expected_revision': 1})
    assert result['allow_remote'] is False and result['base_url'] == 'https://shared.example/v1'
    assert models.records.read('recognition_model_config', 'generation').payload['secret_ref'] == original['secret_ref']
    assert models.snapshot('generation')['api_key'] == central
    with pytest.raises(ModelConfigurationError, match='readonly'):
        models.update('generation', {'expected_revision': 2, 'api_key': secrets.token_urlsafe(24)})
    with pytest.raises(ModelConfigurationError, match='readonly'):
        models.update_generation_mode(mode='local', local_enabled=True,
            local_base_url='http://127.0.0.1:8001/local-model/v1', expected_revision=0)
    with pytest.raises(ModelConfigurationError, match='readonly'):
        models.select_subscription(model=None, expected_revision=0)


def test_frozen_wire_guard_rejects_profile_change_and_disabled_profile(configured):
    owner, models, admin, _, profile, _, _ = configured
    share(configured)
    snapshot = models.snapshot('generation')
    guard = models._generation_egress_guard(snapshot)
    owner.update_profile(admin, profile['profile_id'], expected_revision=profile['revision'], model='fast')
    with pytest.raises(ModelConfigurationError, match='changed_before_request'):
        guard('memory_generation', ('instructions', 'source_excerpt'), 12)
    owner.update_profile(admin, profile['profile_id'], expected_revision=profile['revision'] + 1, enabled=False)
    assert models.public()['generation']['configured'] is False
    assert models.model_prices('generation')['rates'] is None
    with pytest.raises(ModelConfigurationError):
        models.snapshot('generation')


def test_independent_snapshot_cannot_survive_shared_allocation(configured):
    _, models, _, _, _, _, _ = configured
    public = public_model_configuration(models.public()['generation'])
    guard = models._generation_egress_guard(models.snapshot('generation'))
    share(configured)
    assert not _same_governed_public_configuration(models.public()['generation'], public)
    with pytest.raises(ModelConfigurationError, match='changed_before_request'):
        guard('memory_generation', ('instructions', 'source_excerpt'), 12)


def test_fast_selection_is_allowed_and_freezes_allocation_identity(configured):
    owner, models, admin, _, profile, _, _ = configured
    share(configured)
    with pytest.raises(ModelConfigurationError, match='not_allowed'):
        models.update_fast_model(model='unlisted', expected_revision=0,
            expected_generation_revision=1, expected_mode_revision=0)
    models.update_fast_model(model='fast', expected_revision=0,
        expected_generation_revision=1, expected_mode_revision=0)
    selected = models.for_auxiliary(models.freeze_auxiliary_binding())
    assert selected.snapshot('generation')['model'] == 'fast'
    assert models.model_prices('generation', configuration=selected.public()['generation'])['rates']['input_per_million'] == '1'
    owner.update_profile(admin, profile['profile_id'], expected_revision=1, allowed_models=['main'],
        prices={'main': profile['prices']['main']})
    with pytest.raises(ModelConfigurationError, match='configuration_changed'):
        selected.snapshot('generation')


def test_strict_route_and_retrieval_identity_include_server_binding(configured):
    _, models, _, _, _, _, _ = configured
    share(configured)
    public = models.public()['generation']
    route = public_model_configuration(public)
    assert _same_governed_public_configuration(public, route)
    route.pop('server_model_binding')
    assert not _same_governed_public_configuration(public, route)
    route = public_model_configuration(public)
    route['server_model_binding']['allocation_revision'] = True
    assert not _same_governed_public_configuration(public, route)
    snapshot = models.snapshot('generation')
    assert _public_identity(public) == _public_identity(_public_from_snapshot(snapshot, 'generation'))
    snapshot['server_model_binding']['allocation_revision'] += 1
    assert _public_identity(public) != _public_identity(_public_from_snapshot(snapshot, 'generation'))


def test_shared_prices_are_central_readonly_and_missing_purpose_fails_send(configured):
    _, models, _, _, _, _, _ = configured
    share(configured)
    assert models.model_prices('generation')['source'] == 'server'
    with pytest.raises(ModelConfigurationError, match='readonly'):
        models.update_model_prices('generation', {}, expected_revision=0, expected_configuration_revision=1)
    public = models.update('embedding', {'allow_remote': True, 'enabled': True, 'expected_revision': 0})
    assert public['allow_remote'] is True and public['configured'] is False
    with pytest.raises(ModelConfigurationError, match='unavailable'):
        models.snapshot('embedding')


def test_key_rotation_and_user_disable_revoke_captured_access(configured):
    owner, models, admin, user, profile, _, central = configured
    share(configured)
    original = models.snapshot('generation')
    replacement = secrets.token_urlsafe(24)
    owner.update_profile(admin, profile['profile_id'], expected_revision=1, api_key=replacement)
    current = models.snapshot('generation')
    assert current['api_key'] == replacement and current['api_key'] != central
    with pytest.raises(ValueError, match='binding_changed'):
        models.server_model_access.credential(original['server_model_binding'])
    owner.users.update(admin, user['user_id'], expected_revision=user['revision'], disabled=True)
    with pytest.raises(ModelConfigurationError):
        models.snapshot('generation')


def test_original_local_vector_selection_does_not_use_private_remote_credentials(configured):
    from backend.memory_app.v2.embedding_settings import EmbeddingSettings, vector_policy
    _, models, _, _, _, _, _ = configured
    embeddings = EmbeddingSettings(models.records, models._local_models_root, shared=True)
    embeddings.update_mode(mode='local', expected_revision=0)
    models.bind_embedding(embeddings.project, vector_policy)
    share(configured)
    public = models.public()['embedding']
    assert public['mode'] == 'local' and public['provider'] == 'local'
    assert public['base_url'] == 'http://127.0.0.1:8001/local-model/v1'
    assert 'server_model_binding' not in public
    assert public['configured'] is False
    with pytest.raises(ModelConfigurationError, match='model_not_configured'):
        models.snapshot('embedding')
    embeddings.update_mode(mode='remote', expected_revision=1)
    assert models.public()['embedding']['configured'] is False
    with pytest.raises(ModelConfigurationError, match='unavailable'):
        models.snapshot('embedding')


def test_real_immutable_route_keeps_profile_revision_when_only_price_changes(configured):
    from backend.memory_app.kernel.product_routing import ProductGenerationRouting
    from core.ai_kernel.sqlite_store import SQLiteAITurnStore
    owner, models, admin, _, profile, _, _ = configured
    share(configured)
    store = SQLiteAITurnStore(models.root / 'turns.sqlite3')
    store.claim_turn({'turn_id': 'shared-turn', 'session_id': 'shared-session',
        'operation_id': 'shared-operation', 'idempotency_key': 'shared-key'})
    routing = ProductGenerationRouting(models, store)
    arguments = {'request': {'turn_id': 'shared-turn', 'desired_outcome': 'memory.organize'},
        'project_id': 'inbox', 'project_profile_id': 'default', 'project_profile_revision': 1,
        'boundary_profile_id': 'default', 'boundary_profile_revision': 1, 'capability_ids': ()}
    frozen = routing.acquire(**arguments)
    assert frozen.payload['configuration']['server_model_binding']['profile_revision'] == 1
    assert store.get_immutable_payload('shared-turn', 'recognition-model-routing-snapshot-v1')[1] == frozen.payload
    owner.update_profile(admin, profile['profile_id'], expected_revision=1,
        prices={'main': {'input_per_million': '4', 'output_per_million': '8', 'cache_read_per_million': '1'}})
    with pytest.raises(ValueError, match='immutable payload identity conflict'):
        routing.acquire(**arguments)


def test_existing_personal_prices_remain_available_after_server_binding(configured):
    _, models, _, _, _, _, _ = configured
    original = ModelConfiguration(models.records, models.root, secrets=models.secrets)
    rates = {'input_per_million': '5', 'output_per_million': '9', 'cache_read_per_million': '2'}
    original.update_model_prices('generation', rates, expected_revision=0, expected_configuration_revision=1)
    assert models.model_prices('generation')['rates'] == rates
    share(configured)
    assert models.model_prices('generation')['rates']['input_per_million'] == '2'


def test_unmetered_shared_generation_is_rejected_before_transport(configured):
    _, models, _, _, _, _, _ = configured
    share(configured)
    with pytest.raises(ModelConfigurationError, match='server_model_wire_owner_required'):
        models.complete([{'role': 'user', 'content': 'connection check'}])


def test_legacy_connection_route_rejects_shared_api_with_actual_asgi(configured):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    previous_deploy = os.environ.get('CHRIPTMAS_DEPLOY')
    os.environ['CHRIPTMAS_DEPLOY'] = 'server'
    try:
        from backend.memory_app.app import create_app
    finally:
        if previous_deploy is None:
            os.environ.pop('CHRIPTMAS_DEPLOY', None)
        else:
            os.environ['CHRIPTMAS_DEPLOY'] = previous_deploy
    _, models, _, _, _, _, _ = configured
    share(configured)
    application = create_app(runtime_root=models.root, legacy_app=FastAPI(), model_configuration=models)
    with TestClient(application) as client:
        response = client.post('/api/recognition/settings/test', json={'purpose': 'generation'})
    assert response.status_code == 403
    assert response.json()['detail'] == 'server_model_wire_owner_required'


def test_shared_embedding_profile_keeps_real_local_projection_prices(configured):
    from backend.memory_app.v2.embedding_settings import EmbeddingSettings, vector_policy
    from backend.memory_app.retrieval_models import retrieval_options
    owner, models, admin, user, profile, _, _ = configured
    share(configured)
    embedding = owner.create_profile(admin, name='共享向量', purpose='embedding', provider='openai',
        base_url='https://embedding.example/v1', model='encoder', allowed_models=['encoder'],
        enabled=True, api_key=secrets.token_urlsafe(24), prices={})
    owner.update_allocation(admin, user['user_id'], expected_revision=1,
        expected_user_revision=user['revision'], mode='shared',
        profiles={'generation': profile['profile_id'], 'embedding': embedding['profile_id']})
    embeddings = EmbeddingSettings(models.records, models._local_models_root, shared=True)
    embeddings.update_mode(mode='local', expected_revision=0)
    models.bind_embedding(embeddings.project, vector_policy)
    assert models.public()['embedding']['provider'] == 'local'
    assert models.model_prices('embedding') == {'rates': {'input_per_million': '0',
        'output_per_million': '0', 'cache_read_per_million': '0'},
        'source': 'local', 'revision': 0, 'editable': False}
    models.update('embedding', {'allow_remote': True, 'enabled': True, 'expected_revision': 0})
    embeddings.update_mode(mode='remote', expected_revision=1)
    with pytest.raises(ModelConfigurationError, match='server_model_wire_owner_required'):
        retrieval_options(models, models.root)


def test_persisted_unlisted_fast_model_cannot_become_an_auxiliary_request(configured):
    owner, models, admin, _, profile, _, _ = configured
    share(configured)
    models.update_fast_model(model='fast', expected_revision=0,
        expected_generation_revision=1, expected_mode_revision=0)
    owner.update_profile(admin, profile['profile_id'], expected_revision=1, allowed_models=['main'],
        prices={'main': profile['prices']['main']})
    parent = models._generation_authority_binding()
    with models.records.begin() as tx:
        tx.put('v2_generation_fast_model', 'default', {'model': 'fast', 'binding': parent}, expected_revision=1)
        tx.commit()
    assert models.fast_model()['configured'] is False
    with pytest.raises(ModelConfigurationError, match='server_model_not_allowed'):
        selected = models.for_auxiliary({'model': 'fast', 'selection_revision': 2, 'parent': parent})
        selected.snapshot('generation')
