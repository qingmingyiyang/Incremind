import os
from functools import partial
import sys
import inspect
from time import monotonic, sleep
from concurrent.futures import ThreadPoolExecutor

import pytest
from cryptography.fernet import Fernet

from backend.memory_app.model_config import ModelConfiguration
from backend.memory_app.v2.provider_connections import ProviderConnections
from backend.providers import CONNECTIONS, ProviderRegistry
from backend.security.secrets import ServerFileSecretStore
from core.storage_provider import SQLiteStructuredRecordStore, SQLiteUnitOfWorkConflict


@pytest.fixture
def owner(tmp_path):
    previous = os.environ.get('CHRIPTMAS_SERVER_MASTER_KEY')
    os.environ['CHRIPTMAS_SERVER_MASTER_KEY'] = Fernet.generate_key().decode('ascii')
    secrets = ServerFileSecretStore(tmp_path / 'secrets.json')
    models = ModelConfiguration(SQLiteStructuredRecordStore(tmp_path / 'models.sqlite3'), tmp_path, secrets)
    registry = ProviderRegistry(tmp_path, connection_owner=models)
    models.bind_provider_connections(registry)
    try:
        yield models, registry, ProviderConnections(registry, models)
    finally:
        if previous is None:
            os.environ.pop('CHRIPTMAS_SERVER_MASTER_KEY', None)
        else:
            os.environ['CHRIPTMAS_SERVER_MASTER_KEY'] = previous


def configuration(**changes):
    return {'name': '独立连接', 'base_url': 'https://provider.example/v1', 'models': ['main', 'fast'], **changes}


def test_connections_are_separate_from_active_json_and_do_not_enable_egress(owner):
    models, registry, domain = owner
    key = Fernet.generate_key().decode('ascii')
    saved = domain.save('first', {**configuration(), 'api_key': key, 'expected_revision': 0, 'operation_id': 'first-save'})
    domain.save('second', {**configuration(name='第二个连接'), 'api_key': key, 'expected_revision': 0, 'operation_id': 'second-save'})
    assert saved['revision'] == 1 and saved['has_api_key'] and saved['uses'] == []
    assert len(domain.list()['items']) == 2
    assert not (models.root / 'library/global/providers/providers.json').exists()
    assert models.records.read('recognition_model_config', 'generation') is None
    assert not models.public()['generation']['allow_remote']
    assert key not in str(domain.list()) and 'secret_ref' not in str(domain.list())
    assert key not in (models.root / 'secrets.json').read_text(encoding='utf-8')
    assert registry.connections() == domain.list()['items']


def test_connection_cas_preserves_winner_and_cleans_only_failed_new_credential(owner):
    models, registry, _ = owner
    initial = Fernet.generate_key().decode('ascii')
    registry.save_connection('first', configuration(), api_key=initial, expected_revision=0)
    original = models.records.read(CONNECTIONS, 'first').payload['secret_ref']

    def save(name):
        try:
            return registry.save_connection('first', configuration(name=name),
                api_key=Fernet.generate_key().decode('ascii'), expected_revision=1)
        except SQLiteUnitOfWorkConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(save, ['甲', '乙']))
    assert sum(item is not None for item in results) == 1
    assert models.records.read(CONNECTIONS, 'first').revision == 2
    current = models.records.read(CONNECTIONS, 'first').payload['secret_ref']
    assert models.secrets.has_secret(current) and not models.secrets.has_secret(original)
    assert models.secrets.get_generation(current) == 1
    with pytest.raises(SQLiteUnitOfWorkConflict):
        registry.disconnect_connection('first', expected_revision=1)
    assert registry.connections()[0]['enabled']


def test_referenced_api_credential_is_retained_until_last_reference_is_replaced(owner):
    models, registry, _ = owner
    registry.save_connection('first', configuration(), api_key=Fernet.generate_key().decode('ascii'), expected_revision=0)
    ref = models.records.read(CONNECTIONS, 'first').payload['secret_ref']
    with models.records.begin() as tx:
        tx.put('recognition_generation_mode', 'default', {'mode': 'local', 'api_config': {'secret_ref': ref}}, expected_revision=0)
        tx.commit()
    registry.disconnect_connection('first', expected_revision=1)
    assert models.secrets.has_secret(ref)
    with models.records.begin() as tx:
        tx.put('recognition_generation_mode', 'default', {'mode': 'local', 'api_config': {}}, expected_revision=1)
        tx.commit()
    registry.release_connection_secret(ref)
    assert not models.secrets.has_secret(ref)


@pytest.mark.parametrize('base', ['http://provider.example/v1', 'https://user:password@provider.example',
    'https://provider.example/v1?q=x', 'http://127.0.0.1:8001/v1', 'https://localhost/v1', 'https://[::1]/v1'])
def test_connection_endpoint_uses_original_strict_egress_validation(owner, base):
    _, registry, _ = owner
    with pytest.raises(ValueError, match='^provider_connection_endpoint_invalid$'):
        registry.save_connection('first', configuration(base_url=base),
            api_key=Fernet.generate_key().decode('ascii'), expected_revision=0)
    assert registry.connections() == []


def test_connection_endpoint_change_requires_fresh_key_and_preserves_previous(owner):
    models, registry, _ = owner
    registry.save_connection('first', configuration(), api_key=Fernet.generate_key().decode('ascii'), expected_revision=0)
    old = models.records.read(CONNECTIONS, 'first')
    with pytest.raises(ValueError, match='^provider_connection_new_key_required$'):
        registry.save_connection('first', configuration(base_url='https://other.example/v1'), api_key='', expected_revision=1)
    assert models.records.read(CONNECTIONS, 'first') == old
    assert models.secrets.has_secret(old.payload['secret_ref'])


def test_unknown_save_keeps_acceptance_after_later_change_and_rejects_late_replay(owner):
    models, registry, domain = owner
    body = {**configuration(), 'api_key': Fernet.generate_key().decode('ascii'),
        'expected_revision': 0, 'operation_id': 'save-unknown'}
    saved = domain.save('first', body)
    ref = models.records.read(CONNECTIONS, 'first').payload['secret_ref']
    assert domain.list()['items'][0]['last_operation_id'] == 'save-unknown'
    proof = registry.connection_operation('save-unknown')
    assert proof['status'] == 'accepted' and proof['result_revision'] == saved['revision']
    domain.save('first', {**body, 'name': '后续修改', 'api_key': '', 'expected_revision': 1, 'operation_id': 'save-next'})
    with pytest.raises(ValueError, match='^provider_connection_operation_already_recorded$'):
        domain.save('first', body)
    with pytest.raises(ValueError, match='^provider_connection_operation_already_recorded$'):
        domain.save('first', {**body, 'api_key': Fernet.generate_key().decode('ascii')})
    assert registry.connection_operation('save-unknown') == proof
    assert models.records.read(CONNECTIONS, 'first').revision == 2
    assert models.records.read(CONNECTIONS, 'first').payload['name'] == '后续修改'
    assert models.secrets.get_generation(ref) == 1
    disconnected = domain.disconnect('first', {'expected_revision': 2, 'operation_id': 'disconnect-unknown'})
    assert registry.connection_operation('disconnect-unknown')['result_revision'] == disconnected['revision']
    assert registry.connections()[0]['revision'] == 3


def test_unknown_operation_cancel_fences_late_save_and_late_disconnect(owner):
    models, registry, domain = owner
    cancelled = registry.connection_operation('save-cancelled', cancel=True)
    assert cancelled['status'] == 'cancelled'
    with pytest.raises(ValueError, match='^provider_connection_operation_already_recorded$'):
        domain.save('first', {**configuration(), 'api_key': Fernet.generate_key().decode('ascii'),
            'expected_revision': 0, 'operation_id': 'save-cancelled'})
    assert registry.connections() == []
    saved = domain.save('first', {**configuration(), 'api_key': Fernet.generate_key().decode('ascii'),
        'expected_revision': 0, 'operation_id': 'save-real'})
    assert registry.connection_operation('save-real', cancel=True)['status'] == 'accepted'
    registry.connection_operation('disconnect-cancelled', cancel=True)
    with pytest.raises(ValueError, match='^provider_connection_operation_already_recorded$'):
        domain.disconnect('first', {'expected_revision': saved['revision'], 'operation_id': 'disconnect-cancelled'})
    assert models.records.read(CONNECTIONS, 'first').revision == 1


@pytest.mark.parametrize('purpose', ['generation', 'embedding', 'rerank', 'vision', 'search'])
def test_select_connection_keeps_purpose_consent_and_revalidates_disconnect(owner, purpose):
    models, registry, domain = owner
    saved = registry.save_connection('first', configuration(), api_key=Fernet.generate_key().decode('ascii'), expected_revision=0)
    selected = domain.select('first', {'purpose': purpose, 'model': 'main', 'expected_revision': saved['revision'],
        'expected_configuration_revision': 0, 'expected_mode_revision': 0, 'operation_id': 'select-' + purpose})
    assert selected['allow_remote'] is False and selected['enabled'] is False
    assert selected['provider_connection'] == {'id': 'first', 'revision': 1, 'available': True}
    assert models.snapshot(purpose)['model'] == 'main'
    domain.disconnect('first', {'expected_revision': 1, 'operation_id': 'disconnect-selected'})
    assert models.public_for_purpose(purpose)['provider_connection']['available'] is False
    with pytest.raises(ValueError, match='^provider_connection_changed$'):
        models.snapshot(purpose)


def test_purpose_edit_cannot_reuse_bound_key_for_another_model_and_preserves_owner_credential(owner):
    models, registry, domain = owner
    registry.save_connection('first', configuration(), api_key=Fernet.generate_key().decode('ascii'), expected_revision=0)
    domain.select('first', {'purpose': 'generation', 'model': 'main', 'expected_revision': 1,
        'expected_configuration_revision': 0, 'expected_mode_revision': 0, 'operation_id': 'select-generation'})
    bound_ref = models.records.read(CONNECTIONS, 'first').payload['secret_ref']
    with pytest.raises(ValueError, match='^provider_connection_changed$'):
        models.update('generation', {'model': 'other', 'expected_revision': 1})
    models.update('generation', {'model': 'other', 'api_key': Fernet.generate_key().decode('ascii'), 'expected_revision': 1})
    assert models.secrets.has_secret(bound_ref)
    assert models.public()['generation']['model'] == 'other'


def test_trial_configuration_keeps_draft_key_only_in_memory_and_closes_writes(owner):
    from backend.memory_app.provider_trial_config import ProviderTrialConfiguration
    models, registry, _ = owner
    models.update('generation', {'base_url': 'https://provider.example/v1', 'model': 'main',
        'api_key': Fernet.generate_key().decode('ascii'), 'enabled': True, 'allow_remote': True, 'expected_revision': 0})
    key = Fernet.generate_key().decode('ascii')
    before = models.records.read('recognition_model_config', 'generation')
    view = ProviderTrialConfiguration(models, target='draft', registry=registry,
        draft={'base_url': 'https://other.example/v1', 'model': 'unsaved', 'api_key': key,
            'connection_id': None, 'expected_connection_revision': None})
    assert view.snapshot('generation')['api_key'] == key
    assert key not in str(view.public()) and key not in (models.root / 'secrets.json').read_text(encoding='utf-8')
    assert models.records.read('recognition_model_config', 'generation') == before
    with pytest.raises(ValueError, match='^provider_trial_readonly$'):
        view.update('generation', {})
    view.dispose()
    with pytest.raises(ValueError, match='^provider_trial_closed$'):
        view.snapshot('generation')


def test_selection_operation_keeps_original_acceptance_and_fences_late_overwrite(owner):
    models, registry, domain = owner
    registry.save_connection('first', configuration(), api_key=Fernet.generate_key().decode('ascii'), expected_revision=0)
    selection = {'purpose': 'generation', 'model': 'main', 'expected_revision': 1,
        'expected_configuration_revision': 0, 'expected_mode_revision': 0, 'operation_id': 'choose-first'}
    domain.select('first', selection)
    proof = registry.connection_operation('choose-first')
    domain.select('first', {**selection, 'model': 'fast', 'expected_configuration_revision': 1, 'operation_id': 'choose-next'})
    with pytest.raises(ValueError, match='^provider_connection_operation_already_recorded$'):
        domain.select('first', {**selection, 'expected_configuration_revision': 2})
    assert registry.connection_operation('choose-first') == proof
    assert models.records.read('recognition_model_config', 'generation').revision == 2
    assert models.snapshot('generation')['model'] == 'fast'
    registry.connection_operation('choose-cancelled', cancel=True)
    with pytest.raises(ValueError, match='^provider_connection_operation_already_recorded$'):
        domain.select('first', {**selection, 'expected_configuration_revision': 2, 'operation_id': 'choose-cancelled'})
    assert models.snapshot('generation')['model'] == 'fast'


def test_trial_private_project_records_real_denial_without_sending_or_saving_draft(owner):
    from backend.memory_app.v2.model_trials import ModelTrials, TRIALS
    from backend.memory_app.v2.privacy import set_private_project
    models, registry, _ = owner
    models.update('generation', {'base_url': 'https://provider.example/v1', 'model': 'main',
        'api_key': Fernet.generate_key().decode('ascii'), 'enabled': True, 'allow_remote': True, 'expected_revision': 0})
    set_private_project(models.records, 'default', True, 0)
    trial = ModelTrials(models.records, models, registry)
    key = Fernet.generate_key().decode('ascii')
    body = {'trial_id': 'trial-private', 'project_id': 'default', 'text': '检查当前输入', 'targets': ['draft'],
        'draft': {'base_url': 'https://other.example/v1', 'model': 'draft', 'api_key': key,
            'connection_id': None, 'expected_connection_revision': None},
        'expected_generation_revision': 1, 'expected_mode_revision': 0, 'expected_fast_revision': 0}
    result = trial.start(body)
    assert result['status'] == 'failed' and result['results'][0]['attempts'] == 0
    assert result['results'][0]['cost'] is None and result['results'][0]['usage'] is None
    assert key not in str(models.records.list(TRIALS))
    assert key not in (models.root / 'secrets.json').read_text(encoding='utf-8')
    assert trial.get('trial-private')['status'] == 'failed'
    with pytest.raises(ValueError, match='^provider_trial_already_recorded$'):
        trial.start(body)
    assert trial.cancel_unaccepted('trial-private')['status'] == 'failed'


def test_governed_trial_routes_once_and_rechecks_real_consent_before_default_gateway(owner):
    from pydantic import RootModel
    from backend.memory_app.provider_trial_config import ProviderTrialConfiguration
    from backend.memory_app.v2.memory_turn import MemoryTurn
    from backend.memory_app.v2.turn_requests import freeze_product_turn
    models, registry, _ = owner
    models.update('generation', {'base_url': 'https://provider.example/v1', 'model': 'main',
        'api_key': Fernet.generate_key().decode('ascii'), 'enabled': True, 'allow_remote': True, 'expected_revision': 0})
    view = ProviderTrialConfiguration(models, target='main', registry=registry)
    turn, changed, active, sockets = None, False, [False], []
    def observe(event, arguments):
        if active[0] and event == 'socket.connect':
            sockets.append(arguments)
    sys.addaudithook(observe)
    def validate():
        nonlocal changed
        if turn is not None and not changed and any(event['type'] == 'model.routed' for event in turn.store.events_after(turn.turn_id)):
            models.update('generation', {'allow_remote': False, 'expected_revision': 1})
            changed = True
        view.public_for_purpose('generation')
    try:
        turn = MemoryTurn(models.records, view, kind='project.answer', project='default', key='governed-real-denial',
            materials=(), validate=validate, freeze_request=partial(freeze_product_turn, text='检查当前输入'))
        active[0] = True
        with pytest.raises(ValueError, match='^provider_trial_configuration_changed$'):
            turn.generate([{'role': 'user', 'content': '检查当前输入'}], response_model=RootModel[str], max_tokens=256, governed=True)
    finally:
        active[0] = False
        view.dispose()
    assert changed and sockets == []
    events = tuple(turn.store.events_after(turn.turn_id))
    assert sum(event['type'] == 'model.routed' for event in events) == 1
    assert sum(event['type'] == 'model.requested' for event in events) == 1
    assert not any(event['type'] == 'model.attempt.dispatched' for event in events)


def test_fast_trial_requires_explicit_configuration_and_rechecks_complete_frozen_choice(owner):
    from backend.memory_app.provider_trial_config import ProviderTrialConfiguration
    models, registry, _ = owner
    models.update('generation', {'base_url': 'https://provider.example/v1', 'model': 'main',
        'api_key': Fernet.generate_key().decode('ascii'), 'enabled': True, 'allow_remote': True, 'expected_revision': 0})
    with pytest.raises(ValueError, match='^provider_trial_fast_unconfigured$'):
        ProviderTrialConfiguration(models, target='fast', registry=registry)
    models.update_fast_model(model='fast', expected_revision=0, expected_generation_revision=1, expected_mode_revision=0)
    main = ProviderTrialConfiguration(models, target='main', registry=registry)
    fast = ProviderTrialConfiguration(models, target='fast', registry=registry)
    assert main.snapshot('generation')['model'] == 'main'
    assert fast.snapshot('generation')['model'] == 'fast'
    models.update_fast_model(model=None, expected_revision=1, expected_generation_revision=1, expected_mode_revision=0)
    assert main.snapshot('generation')['model'] == 'main'
    with pytest.raises(ValueError, match='^provider_trial_configuration_changed$'):
        fast.snapshot('generation')


def test_connection_selection_rejects_real_shared_allocation_changed_while_waiting_for_lock(owner):
    from backend.memory_app.provider_connection_config import select_provider_connection
    from backend.memory_app.v2.devices import DeviceRegistry
    from backend.memory_app.v2.server_users import ServerUsers
    from backend.memory_app.v2.server_models import ServerModels
    original, _, _ = owner
    root = original.root / 'actual-server'
    devices = DeviceRegistry(root / 'server')
    paired = devices.exchange(devices.issue_pairing(user_id='local-user', actor='install')['code'], name='管理设备')
    admin = devices.authenticate(paired['key'])
    users = ServerUsers(root, records=devices.records)
    user = users.create(admin, name='分配检查账号')
    central = ServerModels(users)
    profile = central.create_profile(admin, name='共享生成', purpose='generation', provider='openai',
        base_url='https://provider.example/v1', model='main', allowed_models=['main'], enabled=True,
        api_key=Fernet.generate_key().decode('ascii'))
    user_root = users.root_for(user['user_id'])
    user_root.mkdir(parents=True, exist_ok=True)
    models = ModelConfiguration(SQLiteStructuredRecordStore(user_root / 'recognition.sqlite3'), user_root,
        ServerFileSecretStore(user_root / 'secrets.json'), server_model_access=central.access_for(user['user_id'], user_root))
    registry = ProviderRegistry(user_root, connection_owner=models)
    models.bind_provider_connections(registry)
    domain = ProviderConnections(registry, models)
    registry.save_connection('first', configuration(), api_key=Fernet.generate_key().decode('ascii'), expected_revision=0)
    lines, start = inspect.getsourcelines(select_provider_connection)
    lock_line = start + next(index for index, line in enumerate(lines) if 'with models._lock' in line)
    with ThreadPoolExecutor(max_workers=1) as workers:
        with models._lock:
            future = workers.submit(domain.select, 'first', {'purpose': 'generation', 'model': 'main',
                'expected_revision': 1, 'expected_configuration_revision': 0, 'expected_mode_revision': 0, 'operation_id': 'selection-lock-race'})
            deadline, waiting = monotonic() + 10, False
            while monotonic() < deadline and not waiting:
                for frame in sys._current_frames().values():
                    while frame is not None:
                        if frame.f_code.co_filename == select_provider_connection.__code__.co_filename and frame.f_lineno == lock_line:
                            waiting = True
                            break
                        frame = frame.f_back
                if not waiting:
                    sleep(.01)
            assert waiting
            central.update_allocation(admin, user['user_id'], mode='shared', profiles={'generation': profile['profile_id']},
                expected_revision=0, expected_user_revision=1)
        with pytest.raises(ValueError, match='^server_model_configuration_readonly$'):
            future.result(timeout=10)
    assert models.records.read('recognition_model_config', 'generation') is None
    assert models.records.read('v2_provider_connection_uses', 'generation') is None
    assert registry.connection_operation('selection-lock-race')['status'] == 'not_recorded'


def test_retained_api_profile_reports_disconnected_connection_as_unconfigured(owner):
    models, registry, domain = owner
    registry.save_connection('first', configuration(), api_key=Fernet.generate_key().decode('ascii'), expected_revision=0)
    domain.select('first', {'purpose': 'generation', 'model': 'main', 'expected_revision': 1,
        'expected_configuration_revision': 0, 'expected_mode_revision': 0, 'operation_id': 'choose-archive'})
    models.update_generation_mode(mode='api', local_enabled=False, local_base_url='http://127.0.0.1:8001/local-model/v1', expected_revision=0)
    assert models.generation_mode()['api_configured']
    registry.disconnect_connection('first', expected_revision=1)
    assert models.generation_mode()['mode'] == 'api'
    assert not models.generation_mode()['api_configured']
