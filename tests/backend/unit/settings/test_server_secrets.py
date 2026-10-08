import json
import os
from pathlib import Path

import pytest
from cryptography.fernet import Fernet


@pytest.fixture
def master(monkeypatch):
    monkeypatch.delenv('CREDENTIALS_DIRECTORY', raising=False)
    monkeypatch.setenv('CHRIPTMAS_SERVER_MASTER_KEY', Fernet.generate_key().decode('ascii'))


def test_server_secret_store_encrypts_and_preserves_epochs_after_reopen(tmp_path, master):
    from backend.security.secrets import ServerFileSecretStore
    path = tmp_path / 'user/secrets.json'
    store = ServerFileSecretStore(path)
    store.set('provider:test', 'synthetic-private-value')
    assert 'synthetic-private-value' not in path.read_text('utf8')
    reopened = ServerFileSecretStore(path)
    assert reopened.get_snapshot('provider:test').value == 'synthetic-private-value'
    assert reopened.get_generation('provider:test') == 1
    reopened.set('provider:test', 'synthetic-next-value')
    reopened.delete('provider:test')
    assert store.get_snapshot('provider:test').value == ''
    assert store.get_generation('provider:test') == 3
    store.replace_many({'provider:test': 'synthetic-third-value', 'other': 'synthetic-other'})
    assert reopened.get_generation('provider:test') == 4
    assert reopened.get_snapshot('other').value == 'synthetic-other'
    assert json.loads(path.read_text('utf8'))['encryption'] == 'fernet-v1'


def test_missing_master_refuses_write_without_creating_a_plaintext_file(tmp_path, monkeypatch):
    from backend.security.secrets import ServerFileSecretStore
    monkeypatch.delenv('CHRIPTMAS_SERVER_MASTER_KEY', raising=False)
    monkeypatch.delenv('CREDENTIALS_DIRECTORY', raising=False)
    path = tmp_path / 'absent/secrets.json'
    store = ServerFileSecretStore(path)
    assert not store.has_secret('new')
    assert store.get_generation('new') == 0
    assert not path.parent.exists()
    with pytest.raises(RuntimeError, match='server_master_key_required'):
        store.set('new', 'synthetic-private-value')
    assert not path.exists()


def test_wrong_master_and_modified_ciphertext_fail_closed(tmp_path, master, monkeypatch):
    from backend.security.secrets import ServerFileSecretStore
    path = tmp_path / 'secrets.json'
    store = ServerFileSecretStore(path)
    store.set('test', 'synthetic-private-value')
    before = path.read_bytes()
    monkeypatch.setenv('CHRIPTMAS_SERVER_MASTER_KEY', Fernet.generate_key().decode('ascii'))
    with pytest.raises(RuntimeError, match='server_secret_decryption_failed'):
        store.get_snapshot('test')
    with pytest.raises(RuntimeError, match='server_secret_decryption_failed'):
        store.set('other', 'synthetic-next-value')
    assert path.read_bytes() == before
    payload = json.loads(path.read_text('utf8'))
    payload['records']['test']['ciphertext'] = 'invalid-ciphertext'
    path.write_text(json.dumps(payload), encoding='utf8')
    with pytest.raises(RuntimeError, match='server_secret_decryption_failed'):
        store.get_snapshot('test')


def test_systemd_credential_file_supplies_master_and_invalid_key_has_safe_error(tmp_path, monkeypatch):
    from backend.security.secrets import ServerFileSecretStore
    monkeypatch.delenv('CHRIPTMAS_SERVER_MASTER_KEY', raising=False)
    credential = tmp_path / 'credentials'
    credential.mkdir()
    (credential / 'chriptmas-master-key').write_bytes(Fernet.generate_key())
    monkeypatch.setenv('CREDENTIALS_DIRECTORY', str(credential))
    store = ServerFileSecretStore(tmp_path / 'user/secrets.json')
    store.set('test', 'synthetic-private-value')
    assert store.has_secret('test')
    monkeypatch.setenv('CHRIPTMAS_SERVER_MASTER_KEY', 'invalid-key')
    with pytest.raises(RuntimeError, match='server_master_key_invalid') as failure:
        store.set('test', 'synthetic-next-value')
    assert 'invalid-key' not in str(failure.value)


def test_non_windows_factories_and_model_configuration_use_user_root(tmp_path, master, monkeypatch):
    from backend.security import secrets
    from backend.memory_app.model_config import ModelConfiguration
    from core.storage_provider import SQLiteStructuredRecordStore
    monkeypatch.setattr(secrets, '_is_windows', lambda: False)
    for factory in [secrets.build_secret_store, secrets.build_model_secret_store]:
        store = factory(tmp_path / 'user')
        assert isinstance(store, secrets.ServerFileSecretStore)
        assert store._path == tmp_path / 'user/secrets.json'
    models = ModelConfiguration(SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3'), tmp_path / 'user')
    assert isinstance(models.secrets, secrets.ServerFileSecretStore)
    row = models.update('vision', {'base_url': 'https://vision.invalid/v1', 'model': 'synthetic',
        'api_key': 'synthetic-private-value', 'allow_remote': False, 'expected_revision': 0})
    assert row['has_api_key'] and 'synthetic-private-value' not in json.dumps(row)


def test_server_writer_creates_private_temp_file_before_publish(tmp_path, master, monkeypatch):
    from backend.security import secrets
    observed = []
    modes = []
    open_file = secrets.os.open
    chmod = secrets.os.chmod
    def private_open(path, flags, mode=0o777, **kwargs):
        modes.append((flags, mode))
        return open_file(path, flags, mode, **kwargs)
    def private_chmod(path, mode, **kwargs):
        assert mode == 0o600
        return chmod(path, mode, **kwargs)
    monkeypatch.setattr(secrets.os, 'open', private_open)
    monkeypatch.setattr(secrets.os, 'chmod', private_chmod)
    original = secrets._restrict_secret_file
    def restrict(descriptor, path):
        observed.append(Path(path))
        original(descriptor, path)
    monkeypatch.setattr(secrets, '_restrict_secret_file', restrict)
    path = tmp_path / 'user/secrets.json'
    secrets.ServerFileSecretStore(path).set('test', 'synthetic-private-value')
    assert len(observed) == 1 and observed[0].parent == path.parent
    assert observed[0] != path and not observed[0].exists()
    assert modes == [(os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)]
    if os.name != 'nt':
        import stat
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    # POSIX mode enforcement is checked on the Linux host in centralized QA.


def test_windows_model_factory_preserves_dpapi_location(tmp_path):
    from backend.security.secrets import build_model_secret_store, DPAPIFileSecretStore
    if os.name == 'nt':
        store = build_model_secret_store(tmp_path)
        assert isinstance(store, DPAPIFileSecretStore)
    else:
        from backend.security.secrets import ServerFileSecretStore
        store = build_model_secret_store(tmp_path)
        assert isinstance(store, ServerFileSecretStore)
    assert store._path == tmp_path / 'secrets.json'


def test_missing_master_settings_save_has_controlled_refusal_and_no_state(tmp_path, monkeypatch, caplog):
    from backend.security import secrets
    from backend.memory_app.app import create_app
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    monkeypatch.setattr(secrets, '_is_windows', lambda: False)
    monkeypatch.delenv('CHRIPTMAS_SERVER_MASTER_KEY', raising=False)
    monkeypatch.delenv('CREDENTIALS_DIRECTORY', raising=False)
    app = create_app(runtime_root=tmp_path, legacy_app=FastAPI())
    client = TestClient(app, raise_server_exceptions=False)
    response = client.put('/api/recognition/settings', json={
        'purpose': 'vision', 'base_url': 'https://vision.invalid/v1', 'model': 'synthetic',
        'api_key': 'synthetic-private-value', 'allow_remote': False, 'expected_revision': 0})
    assert response.status_code == 503
    assert response.json() == {'detail': 'server_master_key_required'}
    assert app.state.recognition_records.read('recognition_model_config', 'vision') is None
    assert not (tmp_path / 'secrets.json').exists()
    assert 'synthetic-private-value' not in response.text + caplog.text


def test_parallel_store_instances_preserve_every_credential_epoch(tmp_path, master):
    from backend.security.secrets import ServerFileSecretStore
    from concurrent.futures import ThreadPoolExecutor
    path = tmp_path / 'secrets.json'
    def save(_index):
        ServerFileSecretStore(path).set('shared', 'synthetic-private-value')
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(save, range(12)))
    assert ServerFileSecretStore(path).get_generation('shared') == 12


def test_unrecognized_plaintext_format_is_never_upgraded_or_overwritten(tmp_path, master):
    from backend.security.secrets import ServerFileSecretStore
    path = tmp_path / 'secrets.json'
    path.write_text(json.dumps({'test': 'synthetic-private-value'}), encoding='utf8')
    before = path.read_bytes()
    store = ServerFileSecretStore(path)
    with pytest.raises(RuntimeError, match='server_secret_file_invalid'):
        store.set('other', 'synthetic-other-value')
    assert path.read_bytes() == before


def test_missing_master_cannot_delete_or_replace_an_existing_store(tmp_path, master, monkeypatch):
    from backend.security.secrets import ServerFileSecretStore
    store = ServerFileSecretStore(tmp_path / 'secrets.json')
    store.set('test', 'synthetic-private-value')
    before = store._path.read_bytes()
    monkeypatch.delenv('CHRIPTMAS_SERVER_MASTER_KEY')
    for change in [lambda: store.delete('test'), lambda: store.replace_many({'test': None})]:
        with pytest.raises(RuntimeError, match='server_master_key_required'):
            change()
        assert store._path.read_bytes() == before


def _child_environment():
    environment = {key: os.environ[key] for key in ['PATH', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP']
                   if key in os.environ}
    environment.update(PYTHONPATH=str(Path('src').resolve()),
                       CHRIPTMAS_SERVER_MASTER_KEY=os.environ['CHRIPTMAS_SERVER_MASTER_KEY'])
    return environment


def test_cross_process_writes_preserve_epochs(tmp_path, master):
    import subprocess
    import sys
    from backend.security.secrets import ServerFileSecretStore
    path = tmp_path / 'secrets.json'
    script = """
import sys
from pathlib import Path
from backend.security.secrets import ServerFileSecretStore
store = ServerFileSecretStore(Path(sys.argv[1]))
print('ready', flush=True)
sys.stdin.readline()
for _ in range(4):
    store.set('shared', 'synthetic-private-value')
"""
    children = [subprocess.Popen([sys.executable, '-c', script, str(path)],
        env=_child_environment(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True) for _ in range(2)]
    try:
        for child in children:
            assert child.stdout.readline().strip() == 'ready'
        for child in children:
            child.stdin.write('continue\n')
            child.stdin.flush()
        for child in children:
            _output, errors = child.communicate(timeout=15)
            assert child.returncode == 0, errors
        assert ServerFileSecretStore(path).get_generation('shared') == 8
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=5)


def test_cross_process_lock_timeout_never_overwrites_credentials(tmp_path, master):
    import subprocess
    import sys
    from backend.security.secrets import ServerFileSecretStore
    path = tmp_path / 'secrets.json'
    store = ServerFileSecretStore(path)
    store.set('test', 'synthetic-private-value')
    before = path.read_bytes()
    script = """
import sys
from pathlib import Path
from backend.shared.interprocess_lock import interprocess_file_lock
with interprocess_file_lock(Path(sys.argv[1])):
    print('locked', flush=True)
    sys.stdin.readline()
"""
    child = subprocess.Popen([sys.executable, '-c', script, str(path)],
        env=_child_environment(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == 'locked'
        with pytest.raises(RuntimeError, match='server_secret_store_busy'):
            store.set('test', 'synthetic-next-value')
        assert path.read_bytes() == before
        child.stdin.write('release\n')
        child.stdin.flush()
        _output, errors = child.communicate(timeout=10)
        assert child.returncode == 0, errors
        assert store.get_snapshot('test').value == 'synthetic-private-value'
        assert store.get_generation('test') == 1
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=5)


def test_windows_factories_keep_legacy_path_and_dpapi_tombstones(tmp_path, monkeypatch):
    from backend.security.secrets import build_secret_store, DPAPIFileSecretStore
    if os.name != 'nt':
        with pytest.raises(RuntimeError, match='Windows'):
            DPAPIFileSecretStore(tmp_path / 'secrets.json')
        return
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'localappdata'))
    first = build_secret_store(tmp_path / 'user')
    second = build_secret_store(tmp_path / 'user')
    assert isinstance(first, DPAPIFileSecretStore)
    assert first._path == second._path
    assert first._path.parent == tmp_path / 'localappdata/Chriptmas_Replay/secrets'
    first.set('test', 'synthetic-private-value')
    second.delete('test')
    assert first.get_snapshot('test').value == ''
    assert first.get_generation('test') == 2
    first.set('test', 'synthetic-next-value')
    assert second.get_generation('test') == 3


@pytest.mark.parametrize('failure_kind', ['master', 'ciphertext'])
def test_settings_read_decryption_errors_have_safe_existing_shape(tmp_path, master, monkeypatch, caplog, failure_kind):
    from backend.security import secrets
    from backend.memory_app.app import create_app
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    monkeypatch.setattr(secrets, '_is_windows', lambda: False)
    app = create_app(runtime_root=tmp_path, legacy_app=FastAPI())
    client = TestClient(app)
    saved = client.put('/api/recognition/settings', json={
        'purpose': 'vision', 'base_url': 'https://vision.invalid/v1', 'model': 'synthetic',
        'api_key': 'synthetic-private-value', 'allow_remote': False, 'expected_revision': 0})
    assert saved.status_code == 200
    if failure_kind == 'master':
        monkeypatch.setenv('CHRIPTMAS_SERVER_MASTER_KEY', Fernet.generate_key().decode('ascii'))
    else:
        path = tmp_path / 'secrets.json'
        payload = json.loads(path.read_text('utf8'))
        next(iter(payload['records'].values()))['ciphertext'] = 'invalid-ciphertext'
        path.write_text(json.dumps(payload), encoding='utf8')
    response = client.get('/api/v2/settings')
    assert response.status_code == 503
    assert response.json() == {'detail': 'server_secret_decryption_failed'}
    assert 'synthetic-private-value' not in saved.text + response.text + caplog.text


def test_failed_private_file_setup_preserves_existing_ciphertext(tmp_path, master, monkeypatch):
    from backend.security import secrets
    path = tmp_path / 'secrets.json'
    store = secrets.ServerFileSecretStore(path)
    store.set('test', 'synthetic-private-value')
    before = path.read_bytes()
    chmod = secrets.os.chmod
    def unavailable_mode(path, mode, **kwargs):
        if str(path).endswith('.tmp'):
            raise PermissionError('synthetic-private-value')
        return chmod(path, mode, **kwargs)
    monkeypatch.setattr(secrets.os, 'chmod', unavailable_mode)
    if hasattr(secrets.os, 'fchmod'):
        opened = secrets.os.open
        descriptors = {}
        fchmod = secrets.os.fchmod
        def record_open(path, *args, **kwargs):
            descriptor = opened(path, *args, **kwargs)
            descriptors[descriptor] = str(path)
            return descriptor
        def unavailable_descriptor_mode(descriptor, mode):
            if descriptors.get(descriptor, '').endswith('.tmp'):
                raise PermissionError('synthetic-private-value')
            return fchmod(descriptor, mode)
        monkeypatch.setattr(secrets.os, 'open', record_open)
        monkeypatch.setattr(secrets.os, 'fchmod', unavailable_descriptor_mode)
    with pytest.raises(secrets.ServerSecretStoreError) as failure:
        store.set('test', 'synthetic-next-value')
    assert str(failure.value) == 'server_secret_store_unavailable'
    assert 'synthetic-private-value' not in repr(failure.value)
    assert path.read_bytes() == before
    assert not list(tmp_path.glob('*.tmp'))


def test_full_backup_preserves_encrypted_user_credentials_without_the_external_master(tmp_path, master):
    from backend.security.secrets import ServerFileSecretStore
    from backend.memory_app.backup import backup_runtime, restore_runtime
    source = tmp_path / 'server'
    user = source / 'users/local-user'
    store = ServerFileSecretStore(user / 'secrets.json')
    store.set('test', 'synthetic-private-value')
    store.set('test', 'synthetic-next-value')
    snapshot = backup_runtime(source, tmp_path / 'backups')
    restored = tmp_path / 'restored'
    restore_runtime(snapshot.snapshot_root, restored)
    reopened = ServerFileSecretStore(restored / 'users/local-user/secrets.json')
    assert reopened.get_snapshot('test').value == 'synthetic-next-value'
    assert reopened.get_generation('test') == 2
    assert reopened._path.read_bytes() == store._path.read_bytes()
    assert not list(restored.rglob('chriptmas-master-key'))
    assert 'synthetic-next-value' not in reopened._path.read_text('utf8')


def test_restore_immediately_restricts_only_known_credential_files_without_master(tmp_path, master, monkeypatch):
    from backend.security import secrets
    from backend.memory_app.backup import backup_runtime, restore_runtime
    source = tmp_path / 'server'
    known = ['secrets.json', 'users/local-user/secrets.json', 'users/second-user/secrets.json']
    for relative in known:
        secrets.ServerFileSecretStore(source / relative).set('test', 'synthetic-private-value')
    ordinary = source / 'users/local-user/workspace/secrets.json'
    ordinary.parent.mkdir()
    ordinary.write_text('synthetic original bytes', encoding='utf8')
    snapshot = backup_runtime(source, tmp_path / 'backups')
    observed = []
    opened = secrets.os.open
    def record_open(path, flags, *args, **kwargs):
        if flags == os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0):
            observed.append(Path(path))
        return opened(path, flags, *args, **kwargs)
    monkeypatch.setattr(secrets.os, 'open', record_open)
    monkeypatch.delenv('CHRIPTMAS_SERVER_MASTER_KEY')
    monkeypatch.delenv('CREDENTIALS_DIRECTORY', raising=False)
    target = tmp_path / 'restored'
    restore_runtime(snapshot.snapshot_root, target)
    assert set(observed) == {target / relative for relative in known}
    for relative in known:
        assert (target / relative).read_bytes() == (source / relative).read_bytes()
        if os.name != 'nt':
            import stat
            assert stat.S_IMODE((target / relative).stat().st_mode) == 0o600
    assert (target / 'users/local-user/workspace/secrets.json').read_bytes() == ordinary.read_bytes()


def test_restore_rejects_permission_failure_instead_of_reporting_success(tmp_path, master, monkeypatch):
    from backend.security import secrets
    from backend.memory_app.backup import backup_runtime, restore_runtime
    from core.storage_provider.vault_backup_restore import VaultBackupRestoreError
    source = tmp_path / 'user'
    secrets.ServerFileSecretStore(source / 'secrets.json').set('test', 'synthetic-private-value')
    snapshot = backup_runtime(source, tmp_path / 'backups')
    def reject_mode(*_args, **_kwargs):
        raise PermissionError('synthetic-private-value')
    monkeypatch.setattr(secrets.os, 'chmod', reject_mode)
    if hasattr(secrets.os, 'fchmod'):
        monkeypatch.setattr(secrets.os, 'fchmod', reject_mode)
    with pytest.raises(VaultBackupRestoreError, match='restored credential permissions unavailable') as failure:
        restore_runtime(snapshot.snapshot_root, tmp_path / 'restored')
    assert 'synthetic-private-value' not in repr(failure.value)
