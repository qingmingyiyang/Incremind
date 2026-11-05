import os
import json
from pathlib import Path
from shutil import copyfile
from uuid import uuid4
import subprocess

from fastapi.testclient import TestClient
import pytest


@pytest.fixture
def real_app(tmp_path_factory, monkeypatch):
    assigned = Path(os.environ['CHRIPTMAS_APP_ROOT']).resolve(strict=True)
    root = tmp_path_factory.mktemp('r') / 'app'
    root.mkdir()
    assert root.is_absolute() and root.is_relative_to(assigned)
    (root / 'config').mkdir()
    copyfile(Path(__file__).resolve().parents[3] / 'config/settings.toml.example', root / 'config/settings.toml')
    monkeypatch.setenv('CHRIPTMAS_APP_ROOT', str(root))
    monkeypatch.setenv('CHRIPTMAS_DEPLOY', 'desktop')
    monkeypatch.setenv('LOCALAPPDATA', str(root / 'appdata'))
    from backend.memory_app.app import create_app
    application = create_app(runtime_root=root)
    with TestClient(application, base_url='http://127.0.0.1:18121', client=('127.0.0.1', 1234)) as client:
        yield application, client, root


def test_actual_lexicon_changes_preview_confirm_and_revision_recovery(real_app):
    application, client, _root = real_app
    endpoint = '/api/v2/asr/lexicon'
    initial = client.post(endpoint + '/import', json={'text': '旧词\n保留词', 'expected_revision': 0, 'preview': False})
    assert initial.status_code == 200
    before = client.get(endpoint).json()
    original = next(term for term in before['terms'] if term['term'] == '旧词')
    revision = before['revision']
    operations = [{'action': 'rename', 'term': '旧词', 'new_term': '新词', 'weight': 5}]
    preview = client.post(endpoint + '/changes', json={'operations': operations, 'expected_revision': revision,
        'preview': True, 'confirm': False})
    assert preview.status_code == 200 and preview.json()['preview'] is True
    assert preview.json()['changes'] == [{'action': 'rename', 'before': {'term': '旧词', 'weight': 4, 'status': 'active'},
                                        'after': {'term': '新词', 'weight': 5, 'status': 'active'}}]
    assert client.get(endpoint).json() == before
    denied = client.post(endpoint + '/changes', json={'operations': operations, 'expected_revision': revision,
        'preview': False, 'confirm': False})
    assert denied.status_code == 400 and client.get(endpoint).json() == before
    committed = client.post(endpoint + '/changes', json={'operations': operations, 'expected_revision': revision,
        'preview': False, 'confirm': True})
    assert committed.status_code == 200 and committed.json()['preview'] is False
    current = client.get(endpoint).json()
    renamed = next(term for term in current['terms'] if term['term'] == '新词')
    assert renamed['id'] == original['id'] and renamed['weight'] == 5
    assert next(term for term in current['terms'] if term['term'] == '保留词')['weight'] == 4
    stale = client.post(endpoint + '/changes', json={'operations': operations, 'expected_revision': revision,
        'preview': False, 'confirm': True})
    assert stale.status_code == 409 and client.get(endpoint).json() == current
    for operation, expected_weight, expected_status in [
        ({'action': 'set_weight', 'term': '新词', 'weight': 50}, 50, 'active'),
        ({'action': 'retire', 'term': '新词'}, 50, 'retired'),
        ({'action': 'restore', 'term': '新词'}, 50, 'active'),
    ]:
        pending = client.post(endpoint + '/changes', json={'operations': [operation], 'expected_revision': current['revision'],
            'preview': True, 'confirm': False})
        assert pending.status_code == 200 and client.get(endpoint).json() == current
        saved = client.post(endpoint + '/changes', json={'operations': [operation], 'expected_revision': current['revision'],
            'preview': False, 'confirm': True})
        assert saved.status_code == 200
        current = client.get(endpoint).json()
        updated = next(term for term in current['terms'] if term['id'] == original['id'])
        assert updated['term'] == '新词' and updated['weight'] == expected_weight and updated['status'] == expected_status
    assert application.state.asr_lexicon.read() == current
    from backend.memory_app.v2.memory_turn import MemoryTurn
    store = MemoryTurn.store_for(application.state.recognition_models.records)
    with store._connect() as db:
        assert db.execute('SELECT count(*) FROM ai_model_attempt_reservations').fetchone()[0] == 0


def test_actual_web_asr_key_replaces_original_encrypted_owner_and_recovers_cas(real_app):
    application, client, root = real_app
    from core.product_core.cloud_asr_provider_settings import TOKENHUB_ASR_SECRET_REF
    from backend.memory_app.asr_credentials import store_asr_credential
    secret = 'synthetic-' + uuid4().hex
    endpoint = '/api/v2/settings/asr-key'
    before = client.get('/api/v2/settings').json()['model']['asr']
    body = {'api_key': secret, 'command_id': 'cmd-' + uuid4().hex,
        'expected_settings_revision': before['settings_revision'], 'expected_generation': before['secret_generation']}
    stored = client.put(endpoint, json=body)
    assert stored.status_code == 200, stored.text
    assert stored.json()['status'] == 'stored' and secret not in stored.text
    owner = application.state.container.secret_store
    assert owner._path.resolve().is_relative_to(root)
    assert owner.get_snapshot(TOKENHUB_ASR_SECRET_REF).value == secret
    assert secret.encode() not in owner._path.read_bytes()
    after = client.get('/api/v2/settings').json()['model']['asr']
    assert after['has_api_key'] is True and after['secret_generation'] == before['secret_generation'] + 1
    assert after['settings_revision'] == before['settings_revision'] and after['enabled'] == before['enabled']
    assert after['egress_manifest'] == before['egress_manifest'] and secret not in str(after)
    assert client.get(endpoint + '/operations/' + body['command_id']).json()['status'] == 'stored'
    replacement = 'synthetic-' + uuid4().hex
    stale = {**body, 'command_id': 'cmd-' + uuid4().hex, 'api_key': replacement}
    refused = client.put(endpoint, json=stale)
    assert refused.status_code == 409 and refused.json() == {'detail': 'asr_key_revision_conflict'}
    assert owner.get_snapshot(TOKENHUB_ASR_SECRET_REF).value == secret
    state = client.get(endpoint + '/operations/' + stale['command_id'])
    assert state.json()['status'] == 'not_recorded'
    cancelled = client.post(endpoint + '/operations/' + stale['command_id'] + '/cancel', json={})
    assert cancelled.status_code == 200 and cancelled.json()['status'] == 'cancelled'
    replay = client.put(endpoint, json={**stale, 'expected_generation': after['secret_generation']})
    assert replay.status_code == 200 and replay.json()['status'] == 'cancelled'
    assert owner.get_snapshot(TOKENHUB_ASR_SECRET_REF).value == secret
    # 原桌面消费者使用同一个加密 owner，真实替换推进代次并使旧网页修订失效。
    generation = store_asr_credential(application.state.container, replacement)
    assert generation == after['secret_generation'] + 1
    old_revision = client.put(endpoint, json={**body, 'command_id': 'cmd-' + uuid4().hex,
        'expected_generation': after['secret_generation']})
    assert old_revision.status_code == 409
    final = client.put(endpoint, json={**body, 'command_id': 'cmd-' + uuid4().hex, 'expected_generation': generation})
    assert final.status_code == 200 and final.json()['status'] == 'stored'
    assert owner.get_snapshot(TOKENHUB_ASR_SECRET_REF).value == secret
    for row in application.state.recognition_models.records.list('v2_asr_key_commands'):
        assert secret not in str(row.payload) and replacement not in str(row.payload)
    assert client.post('/api/rebuild/security/credentials/capture', json={}).status_code == 403


def test_actual_asr_secret_file_conflict_stays_unknown_and_keeps_old_credential(real_app):
    application, client, _root = real_app
    from core.product_core.cloud_asr_provider_settings import TOKENHUB_ASR_SECRET_REF
    from backend.memory_app.asr_credentials import store_asr_credential
    owner = application.state.container.secret_store
    secret = 'synthetic-' + uuid4().hex
    store_asr_credential(application.state.container, secret)
    row = client.get('/api/v2/settings').json()['model']['asr']
    command = {'api_key': 'synthetic-' + uuid4().hex, 'command_id': 'cmd-' + uuid4().hex,
        'expected_settings_revision': row['settings_revision'], 'expected_generation': row['secret_generation']}
    endpoint = '/api/v2/settings/asr-key'
    # Windows 真实打开句柄保留读取资格并拒绝替换，验证跨文件与 SQLite 的未知结果。
    with owner._path.open('rb'):
        response = client.put(endpoint, json=command)
    assert response.status_code == 503 and response.json() == {'detail': 'asr_key_store_unavailable'}
    assert owner.get_snapshot(TOKENHUB_ASR_SECRET_REF).value == secret
    status = client.get(endpoint + '/operations/' + command['command_id']).json()
    assert status['status'] == 'unknown' and status['current']['secret_generation'] == row['secret_generation']
    assert client.post(endpoint + '/operations/' + command['command_id'] + '/cancel', json={}).json()['status'] == 'unknown'
    assert client.put(endpoint, json=command).json()['status'] == 'unknown'
    actual_generation = store_asr_credential(application.state.container, 'synthetic-' + uuid4().hex)
    changed = client.get(endpoint + '/operations/' + command['command_id']).json()
    assert changed['status'] == 'unknown' and changed['current']['secret_generation'] == actual_generation
    saved = client.put(endpoint, json={**command, 'command_id': 'cmd-' + uuid4().hex, 'expected_generation': actual_generation})
    assert saved.status_code == 200 and saved.json()['status'] == 'stored'
    assert owner.get_snapshot(TOKENHUB_ASR_SECRET_REF).value == command['api_key']


def test_real_server_asr_key_obeys_target_space_and_independent_allocation(tmp_path_factory, monkeypatch):
    assigned = Path(os.environ['CHRIPTMAS_APP_ROOT']).resolve(strict=True)
    root = tmp_path_factory.mktemp('srv') / 'server'
    root.mkdir()
    assert root.is_relative_to(assigned)
    monkeypatch.setenv('CHRIPTMAS_APP_ROOT', str(root))
    monkeypatch.setenv('CHRIPTMAS_DEPLOY', 'server')
    from cryptography.fernet import Fernet
    monkeypatch.setenv('CHRIPTMAS_SERVER_MASTER_KEY', Fernet.generate_key().decode('ascii'))
    from backend.memory_app.app import create_server_app
    from backend.shared.deployment import DeploymentLayout
    application = create_server_app(layout=DeploymentLayout('server', root / 'users/local-user', root), port=18121)
    registry = application.state.device_registry
    paired = registry.exchange(registry.issue_pairing(user_id='local-user', actor='install')['code'], name='synthetic admin')
    actor = registry.authenticate(paired['key'])
    admin = {'Authorization': 'Bearer ' + paired['key']}
    with TestClient(application, base_url='http://127.0.0.1:18121', client=('127.0.0.1', 1234)) as client:
        users = []
        for name in ('synthetic ASR A', 'synthetic ASR B'):
            created = client.post('/api/v2/server/users', headers=admin, json={'name': name})
            assert created.status_code == 201
            identity = created.json()['user_id']
            device = registry.exchange(registry.issue_pairing(user_id=identity, actor=actor.device_id)['code'], name=name)
            users.append((identity, {'Authorization': 'Bearer ' + device['key']}))
        user_a, headers_a = users[0]
        _user_b, headers_b = users[1]
        row = client.get('/api/v2/settings', headers=headers_a).json()['model']['asr']
        secret = 'synthetic-' + uuid4().hex
        endpoint = '/api/v2/settings/asr-key'
        command = {'api_key': secret, 'command_id': 'cmd-' + uuid4().hex,
            'expected_settings_revision': row['settings_revision'], 'expected_generation': row['secret_generation']}
        stored = client.put(endpoint, headers=headers_a, json=command)
        assert stored.status_code == 200 and stored.json()['status'] == 'stored'
        operation = endpoint + '/operations/' + command['command_id']
        assert client.get(operation, headers=headers_b).json()['status'] == 'not_recorded'
        assert client.get(operation, headers={**headers_b, 'X-Chriptmas-Target-User': user_a}).status_code == 403
        assert client.get(operation, headers={**admin, 'X-Chriptmas-Target-User': user_a}).json()['status'] == 'stored'
        assert client.get('/api/v2/settings', headers=headers_b).json()['model']['asr']['has_api_key'] is False
        profile = client.post('/api/v2/server/model-profiles', headers=admin, json={'name': 'synthetic ASR allocation',
            'purpose': 'asr', 'provider': 'openai', 'base_url': 'https://tokenhub.tencentmaas.com/v1/wand/asrproxy/sync_transcribe',
            'model': 'hy-asr-3.0-preview', 'allowed_models': ['hy-asr-3.0-preview'], 'enabled': True,
            'api_key': 'synthetic-' + uuid4().hex})
        assert profile.status_code == 201, profile.text
        allocation_path = '/api/v2/server/users/' + user_a + '/model-allocation'
        current = client.get(allocation_path, headers=admin).json()
        allocated = client.put(allocation_path, headers=admin, json={'mode': 'shared', 'profiles': {'asr': profile.json()['profile_id']},
            'expected_revision': current['revision'], 'expected_user_revision': current['user_revision']})
        assert allocated.status_code == 200, allocated.text
        denied = client.put(endpoint, headers=headers_a, json={**command, 'command_id': 'cmd-' + uuid4().hex,
            'expected_generation': stored.json()['generation']})
        assert denied.status_code == 403 and denied.json() == {'detail': 'server_model_configuration_readonly'}
        assert client.get(operation, headers=headers_a).json()['status'] == 'stored'
        assert client.put(endpoint, json=command).status_code == 401
        assert secret not in str(client.get('/api/v2/settings', headers=headers_a).json())


def test_actual_lexicon_two_edits_preserve_native_draft_and_reconfirm_after_409(real_app):
    _application, client, root = real_app
    endpoint = '/api/v2/asr/lexicon'
    assert client.post(endpoint + '/import', json={'text': '甲词\n乙词', 'expected_revision': 0, 'preview': False}).status_code == 200
    before = client.get(endpoint).json()
    old_operations = [{'action': 'rename', 'term': '甲词', 'new_term': '输入甲', 'weight': 5}]
    assert client.post(endpoint + '/changes', json={'operations': old_operations, 'expected_revision': before['revision'],
        'preview': True, 'confirm': False}).status_code == 200
    retired = client.post(endpoint + '/changes', json={'operations': [{'action': 'retire', 'term': '乙词'}],
        'expected_revision': before['revision'], 'preview': False, 'confirm': True})
    assert retired.status_code == 200
    after_b = client.get(endpoint).json()
    stale = client.post(endpoint + '/changes', json={'operations': old_operations, 'expected_revision': before['revision'],
        'preview': False, 'confirm': True})
    assert stale.status_code == 409 and client.get(endpoint).json() == after_b
    concurrent = client.post(endpoint + '/changes', json={'operations': [{'action': 'set_weight', 'term': '甲词', 'weight': 2}],
        'expected_revision': after_b['revision'], 'preview': False, 'confirm': True})
    assert concurrent.status_code == 200
    after_a = client.get(endpoint).json()
    fixture = root.parent / 'lexicon-draft-versions.json'
    fixture.write_text(json.dumps({'before': before, 'afterB': after_b, 'afterA': after_a}, ensure_ascii=False), encoding='utf8')
    checker = Path(__file__).resolve().parents[2] / 'frontend/lexiconDraft.native.mjs'
    native = subprocess.run(['node', str(checker), str(fixture)], capture_output=True, text=True, encoding='utf8', check=False)
    assert native.returncode == 0, native.stderr
    proposed = json.loads(native.stdout)
    assert proposed['expected_revision'] == after_a['revision']
    preview = client.post(endpoint + '/changes', json={**proposed, 'preview': True, 'confirm': False})
    assert preview.status_code == 200 and client.get(endpoint).json() == after_a
    saved = client.post(endpoint + '/changes', json={**proposed, 'preview': False, 'confirm': True})
    assert saved.status_code == 200
    final = client.get(endpoint).json()
    renamed = next(term for term in final['terms'] if term['term'] == '输入甲')
    assert renamed['weight'] == 5 and renamed['id'] == next(term for term in before['terms'] if term['term'] == '甲词')['id']
    assert next(term for term in final['terms'] if term['term'] == '乙词')['status'] == 'retired'


def test_actual_purpose_tests_refuse_unavailable_conditions_and_keep_durable_identity(real_app):
    application, client, root = real_app
    from backend.memory_app.kernel.memory_turn import MemoryTurn
    endpoint = '/api/v2/settings/purpose-tests'
    model = client.get('/api/v2/settings').json()['model']['vision']
    assert client.patch('/api/v2/settings/vision-mode', json={'mode': 'remote', 'expected_revision': model['mode_revision']}).status_code == 200
    records = application.state.recognition_models.records
    originals = records.list('workspace_items')
    cases, outcomes = [], []
    for purpose, error in [('vision', 'purpose_test_remote_disabled'), ('asr', 'purpose_test_asr_disabled'), ('search', 'purpose_test_remote_disabled')]:
        sample = client.get(endpoint + '/samples/' + purpose)
        assert sample.status_code == 200, sample.text
        sample = sample.json()
        body = {'id': 'test-' + uuid4().hex, 'purpose': purpose, 'project_id': 'default', 'sample_id': sample['id'], 'expected': sample['expected']}
        response = client.post(endpoint, json=body)
        assert response.status_code == 200, response.text
        result = response.json()
        assert result['status'] == 'failed' and result['error'] == error
        assert 'input_proof' not in result and 'text' not in result
        assert client.get(endpoint + '/' + body['id']).json() == result
        assert client.post(endpoint, json=body).json() == result
        assert client.post(endpoint + '/' + body['id'] + '/cancel', json={}).json() == result
        assert not (root / 'model-checks' / body['id']).exists()
        cases.append(body)
        outcomes.append(result)
    second = {**cases[-1], 'id': 'test-' + uuid4().hex}
    repeated = client.post(endpoint, json=second)
    assert repeated.status_code == 200 and repeated.json()['status'] == 'failed'
    fixture = root / 'purpose-command-input.json'
    fixture.write_text(json.dumps({'bodies': [cases[-1], second], 'results': [outcomes[-1], repeated.json()]}), encoding='utf-8')
    native = subprocess.run(['node', str(Path(__file__).resolve().parents[2] / 'frontend/purposeCommands.native.mjs'), str(fixture)],
        capture_output=True, text=True, check=True, timeout=40)
    evidence = json.loads(native.stdout)
    assert evidence['cases'] == 8 and len(set(evidence['pids'])) == 3
    (root / 'purpose-command-storage.json').write_text(json.dumps(evidence), encoding='utf-8')
    never = {**cases[-1], 'id': 'test-' + uuid4().hex}
    assert client.get(endpoint + '/' + never['id']).json()['status'] == 'not_recorded'
    assert client.post(endpoint + '/' + never['id'] + '/cancel', json={}).json()['status'] == 'cancelled'
    assert client.post(endpoint, json=never).json()['status'] == 'cancelled'
    assert records.list('workspace_items') == originals
    store = MemoryTurn.store_for(records)
    with store._connect() as db:
        assert db.execute('SELECT count(*) FROM ai_model_attempt_reservations').fetchone()[0] == 0
    client.portal.call(application.state.memory_daily_jobs.stop)
    before = records.change_stamp()
    assert client.get(endpoint + '/' + cases[-1]['id']).status_code == 200
    assert records.change_stamp() == before


def test_registered_sample_real_file_and_frozen_proof_reject_replaced_catalogue_bytes(real_app):
    from dataclasses import replace
    from datetime import datetime, timezone
    from backend.memory_app.v2.purpose_samples import CATALOG, PROOF, RegisteredImageReference, RegisteredPurposeSample
    from backend.memory_app.kernel.memory_turn import MemoryTurn
    application, _client, root = real_app
    owner = application.state.purpose_tests
    identity = 'test-' + uuid4().hex
    body = {'id': identity, 'purpose': 'vision', 'project_id': 'default', 'sample_id': CATALOG['vision']['id'],
        'expected': owner.expected('vision')}
    assert owner.admit(body, validate=lambda: None) is True
    sample = RegisteredPurposeSample.create(owner.records, root, identity, 'default', 'vision', consume=lambda _size: None, validate=lambda: None)
    turn = 'memory-' + uuid4().hex
    request = sample.freeze('media.image_read', records=owner.records, models=owner.models, project_id='default', materials=(),
        load_text=lambda _item: '', local_only=True, model_purpose='vision', turn_id=turn, session_id='aux-' + turn,
        operation_id='op-' + turn, idempotency_key=turn, created_at=datetime.now(timezone.utc).isoformat(), capabilities=[])
    sample.validate_request(owner.records, owner.models, request, purpose='vision')
    store = MemoryTurn.store_for(owner.records)
    assert store.get_immutable_payload(turn, PROOF) is None
    with pytest.raises(ValueError, match='purpose_test_turn_not_accepted'):
        sample.publish(request, owner.models)
    assert store.claim_turn(request) == (turn, True)
    sample.publish(request, owner.models)
    proof = store.get_immutable_payload(turn, PROOF)
    assert request['input']['refs'] == [{'kind': 'asset', 'object_id': CATALOG['vision']['id'], 'uri': proof[0]}]
    assert len(proof[1]['messages']) == 1 and proof[1]['bytes'] == len(sample.raw)
    replaced = replace(sample, raw=b'private text is not a registered image')
    sample.path.write_bytes(replaced.raw)
    with pytest.raises(ValueError, match='purpose_test_sample_changed'):
        replaced.validate()
    with pytest.raises(ValueError, match='purpose_test_sample_changed'):
        RegisteredImageReference(replaced)
    with pytest.raises(ValueError, match='purpose_test_sample_changed'):
        sample.validate()
    sample.path.write_bytes(sample.raw)
    sample.validate_request(owner.records, owner.models, request, purpose='vision')
    sample.dispose()
    assert not sample.path.exists() and owner.records.list('workspace_items') == ()


def test_actual_asr_wire_validation_rechecks_project_privacy_and_saved_configuration(real_app):
    application, client, root = real_app
    from backend.api.tokenhub_asr_provider import TokenHubAsrError, TokenHubAudioAssetTranscriber
    from backend.memory_app.purpose_test_asr import validate_asr_test
    from backend.memory_app.rebuild_storage import build_rebuild_object_store
    from backend.memory_app.v2.purpose_samples import CATALOG, RegisteredPurposeSample
    owner = application.state.purpose_tests
    created = client.post('/api/v2/projects', json={'name': 'synthetic ASR privacy'})
    assert created.status_code == 200
    project = created.json()['id']
    row = client.get('/api/v2/settings').json()['model']['asr']
    key = client.put('/api/v2/settings/asr-key', json={'api_key': 'synthetic-' + uuid4().hex,
        'command_id': 'cmd-' + uuid4().hex, 'expected_settings_revision': row['settings_revision'],
        'expected_generation': row['secret_generation']})
    assert key.status_code == 200 and key.json()['status'] == 'stored'
    enabled = client.put('/api/rebuild/settings/cloud-asr-provider', json={'enabled': True, 'confirm_enable': True})
    assert enabled.status_code == 200
    consent = client.post('/api/rebuild/settings/cloud-asr-provider/egress-consent',
        json={'manifest_id': enabled.json()['egress_manifest']['manifest_id'], 'confirm': True})
    assert consent.status_code == 200
    body = {'id': 'test-' + uuid4().hex, 'purpose': 'asr', 'project_id': project, 'sample_id': CATALOG['asr']['id'],
        'expected': owner.expected('asr')}
    assert owner.admit(body, validate=lambda: None)
    sample = RegisteredPurposeSample.create(owner.records, root, body['id'], project, 'asr', consume=lambda _size: None, validate=lambda: None)
    objects, storage = build_rebuild_object_store(root)
    provider = TokenHubAudioAssetTranscriber(root, objects, namespace_id=storage.namespace_id,
        secret_store=application.state.container.secret_store, registered_sample=sample,
        validate_wire=lambda: validate_asr_test(sample, application.state.container, validate=lambda: None))
    assert validate_asr_test(sample, application.state.container, validate=lambda: None)['enabled'] is True
    provider._check_wire_consent()
    before = client.get('/api/v2/settings').json()['privacy']
    changed = client.patch('/api/v2/settings/privacy', json={'private_projects': [project], 'expected_revision': before['revision']})
    assert changed.status_code == 200
    with pytest.raises(ValueError, match='purpose_test_remote_disabled'):
        validate_asr_test(sample, application.state.container, validate=lambda: None)
    with pytest.raises(TokenHubAsrError, match='remote_processing_target_changed'):
        provider._check_wire_consent()
    restored = client.patch('/api/v2/settings/privacy', json={'private_projects': [], 'expected_revision': changed.json()['revision']})
    assert restored.status_code == 200
    provider._check_wire_consent()
    disabled = client.put('/api/rebuild/settings/cloud-asr-provider', json={'enabled': False, 'confirm_enable': False})
    assert disabled.status_code == 200
    with pytest.raises(ValueError, match='purpose_test_asr_disabled'):
        validate_asr_test(sample, application.state.container, validate=lambda: None)
    with pytest.raises(TokenHubAsrError, match='remote_processing_target_changed'):
        provider._check_wire_consent()
    assert objects.list('cloud_asr_dispatches') == ()
    sample.dispose()


def test_real_shared_asr_sample_freezes_owned_input_and_refuses_zero_wallet_before_http(tmp_path_factory, monkeypatch):
    assigned = Path(os.environ['CHRIPTMAS_APP_ROOT']).resolve(strict=True)
    root = tmp_path_factory.mktemp('wire') / 'server'
    root.mkdir()
    assert root.is_relative_to(assigned)
    monkeypatch.setenv('CHRIPTMAS_APP_ROOT', str(root))
    monkeypatch.setenv('CHRIPTMAS_DEPLOY', 'server')
    from cryptography.fernet import Fernet
    monkeypatch.setenv('CHRIPTMAS_SERVER_MASTER_KEY', Fernet.generate_key().decode('ascii'))
    from backend.memory_app.app import create_server_app
    from backend.shared.deployment import DeploymentLayout
    application = create_server_app(layout=DeploymentLayout('server', root / 'users/local-user', root), port=18121)
    registry = application.state.device_registry
    paired = registry.exchange(registry.issue_pairing(user_id='local-user', actor='install')['code'], name='synthetic sample admin')
    headers = {'Authorization': 'Bearer ' + paired['key']}
    with TestClient(application, base_url='http://127.0.0.1:18121', client=('127.0.0.1', 1234)) as client:
        profile = client.post('/api/v2/server/model-profiles', headers=headers, json={'name': 'synthetic sample ASR',
            'purpose': 'asr', 'provider': 'tokenhub-asr', 'base_url': 'https://tokenhub.tencentmaas.com/v1',
            'model': 'hy-asr-3.0-preview', 'allowed_models': ['hy-asr-3.0-preview'], 'enabled': True,
            'api_key': 'synthetic-' + uuid4().hex})
        assert profile.status_code == 201, profile.text
        profile = profile.json()
        prices = client.put('/api/v2/server/model-profiles/' + profile['profile_id'] + '/metering-prices', headers=headers,
            json={'expected_revision': 0, 'expected_profile_revision': profile['revision'],
                'models': {'hy-asr-3.0-preview': {'unit': 'audio_seconds', 'rates': {'audio_per_second': '0.00022'}}}})
        assert prices.status_code == 200, prices.text
        allocation_path = '/api/v2/server/users/local-user/model-allocation'
        allocation = client.get(allocation_path, headers=headers).json()
        allocated = client.put(allocation_path, headers=headers, json={'mode': 'shared', 'profiles': {'asr': profile['profile_id']},
            'expected_revision': allocation['revision'], 'expected_user_revision': allocation['user_revision']})
        assert allocated.status_code == 200, allocated.text
        enabled = client.put('/api/rebuild/settings/cloud-asr-provider', headers=headers, json={'enabled': True, 'confirm_enable': True})
        assert enabled.status_code == 200, enabled.text
        consent = client.post('/api/rebuild/settings/cloud-asr-provider/egress-consent', headers=headers,
            json={'manifest_id': enabled.json()['egress_manifest']['manifest_id'], 'confirm': True})
        assert consent.status_code == 200, consent.text
        balance_path = '/api/v2/server/users/local-user/model-balance'
        before = client.get(balance_path, headers=headers).json()
        assert before['balance'] == before['available'] == before['reserved'] == before['spent'] == '0'
        endpoint = '/api/v2/settings/purpose-tests'
        sample = client.get(endpoint + '/samples/asr', headers=headers)
        assert sample.status_code == 200, sample.text
        sample = sample.json()
        body = {'id': 'test-' + uuid4().hex, 'purpose': 'asr', 'project_id': 'default', 'sample_id': sample['id'], 'expected': sample['expected']}
        response = client.post(endpoint, headers=headers, json=body)
        assert response.status_code == 200, response.text
        result = response.json()
        assert result['status'] == 'failed' and result['error'] == 'server_model_balance_insufficient', result
        assert result['receipt_kind'] == 'asr_dispatch' and result['attempt_count'] == 1 and result['turn_id']
        assert client.get(endpoint + '/' + body['id'], headers=headers).json() == result
        assert client.get(balance_path, headers=headers).json() == before
        from backend.memory_app.rebuild_storage import build_rebuild_object_store
        from backend.memory_app.kernel.memory_turn import MemoryTurn
        from backend.memory_app.v2.purpose_samples import PROOF
        user_root = root / 'users/local-user'
        objects, _ = build_rebuild_object_store(user_root)
        assert objects.read('audio_asset_refs', 'sample-audio-' + body['id']) is None
        dispatch = objects.read('cloud_asr_dispatches', result['receipt_id'])
        assert dispatch['status'] == 'failed' and dispatch['transmission'] == 'confirmed_none'
        assert objects.read('workbench_asr_bindings', result['asr']['binding_id'])['registered_sample']['command_id'] == body['id']
        records = application.state.server_context.pool._children['local-user'].application.state.recognition_models.records
        store = MemoryTurn.store_for(records)
        request = store.get_request(result['turn_id'])
        proof = store.get_immutable_payload(result['turn_id'], PROOF)
        assert proof[1]['command_id'] == body['id'] and request['input']['refs'][0]['uri'] == proof[0]
        assert records.list('workspace_items') == () and not (user_root / 'model-checks' / body['id']).exists()


def test_actual_purpose_keys_and_cookie_cas_keep_native_memory_drafts_until_success(real_app):
    application, client, root = real_app
    script = Path(__file__).resolve().parents[2] / 'frontend/credentialDraft.native.mjs'
    evidence = []

    def native(case, phase):
        fixture = root / (case['target'] + '-' + phase + '.json')
        fixture.write_text(json.dumps(case), encoding='utf-8')
        result = subprocess.run(['node', str(script), str(fixture)], capture_output=True, text=True, check=True, timeout=10)
        return json.loads(result.stdout)

    for purpose in ('generation', 'vision', 'embedding', 'rerank', 'search'):
        before = client.get('/api/v2/settings').json()['model'][purpose]
        if purpose in {'vision', 'embedding'} and before['mode'] != 'remote':
            mode = client.patch('/api/v2/settings/' + purpose + '-mode', json={'mode': 'remote', 'expected_revision': before['mode_revision']})
            assert mode.status_code == 200, mode.text
            before = client.get('/api/v2/settings').json()['model'][purpose]
        secret = 'synthetic-' + uuid4().hex
        fields = {'baseUrl': 'https://api.openai.com/v1', 'model': 'synthetic-' + purpose + '-draft', 'key': secret}
        body = {'purpose': purpose, 'base_url': fields['baseUrl'], 'model': fields['model'], 'api_key': secret,
            'enabled': False, 'allow_remote': False, 'expected_revision': before['revision']}
        invalid = client.put('/api/v2/settings/model', json={**body, 'base_url': 'http://api.openai.com/v1'})
        assert invalid.status_code == 400 and secret not in invalid.text
        current = client.put('/api/v2/settings/model', json={**body, 'model': 'synthetic-' + purpose + '-current',
            'api_key': 'synthetic-' + uuid4().hex})
        assert current.status_code == 200, current.text
        stale = client.put('/api/v2/settings/model', json=body)
        assert stale.status_code == 409 and secret not in stale.text
        after = client.get('/api/v2/settings').json()['model'][purpose]
        case = {'kind': 'model', 'target': purpose, 'before': before, 'after': after, 'fields': fields,
            'failures': [invalid.status_code, stale.status_code]}
        reviewed = native(case, 'review')
        assert reviewed['fields'] == fields and reviewed['expected_revision'] == after['revision']
        saved = client.put('/api/v2/settings/model', json={**body, 'expected_revision': reviewed['expected_revision']})
        assert saved.status_code == 200 and secret not in saved.text
        case['saved'] = client.get('/api/v2/settings').json()['model'][purpose]
        assert case['saved']['enabled'] is False and case['saved']['allow_remote'] is False
        assert case['saved']['model'] == fields['model'] and case['saved']['has_api_key'] is True
        assert native(case, 'complete')['checks'] == 10
        evidence.append({'purpose': purpose, 'validation': invalid.status_code, 'cas': stale.status_code, 'saved': saved.status_code})

    endpoint = '/api/v2/settings/platform-sessions'
    for platform in ('bilibili', 'xiaohongshu'):
        before = next(row for row in client.get(endpoint).json()['items'] if row['platform'] == platform)
        content = 'synthetic_session=' + uuid4().hex
        invalid = client.put(endpoint + '/' + platform, json={'content': 'invalid-cookie-input', 'expected_revision': before['revision']})
        assert invalid.status_code == 400
        current = client.put(endpoint + '/' + platform, json={'content': 'synthetic_session=' + uuid4().hex, 'expected_revision': before['revision']})
        assert current.status_code == 200 and current.json()['authentication'] == 'not_checked'
        stale = client.put(endpoint + '/' + platform, json={'content': content, 'expected_revision': before['revision']})
        assert stale.status_code == 409 and content not in stale.text
        after = next(row for row in client.get(endpoint).json()['items'] if row['platform'] == platform)
        case = {'kind': 'cookie', 'target': platform, 'before': before, 'after': after, 'fields': {'content': content},
            'failures': [invalid.status_code, stale.status_code]}
        reviewed = native(case, 'review')
        saved = client.put(endpoint + '/' + platform, json={'content': reviewed['fields']['content'], 'expected_revision': reviewed['expected_revision']})
        assert saved.status_code == 200 and saved.json()['saved'] is True and content not in saved.text
        case['saved'] = next(row for row in client.get(endpoint).json()['items'] if row['platform'] == platform)
        assert native(case, 'complete')['checks'] == 10
        evidence.append({'platform': platform, 'validation': invalid.status_code, 'cas': stale.status_code, 'saved': saved.status_code})
    from backend.memory_app.kernel.memory_turn import MemoryTurn
    with MemoryTurn.store_for(application.state.recognition_models.records)._connect() as db:
        assert db.execute('SELECT count(*) FROM ai_model_attempt_reservations').fetchone()[0] == 0
    (root / 'credential-draft-results.json').write_text(json.dumps({'cases': evidence, 'model_attempts': 0}), encoding='utf-8')
