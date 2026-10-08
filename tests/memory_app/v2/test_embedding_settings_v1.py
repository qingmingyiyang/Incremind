"""真实配置/旁路 CAS 与安装状态；网络和权重仅作为独立边界。"""
from backend.memory_app.v2.embedding_settings import vector_policy
import pytest

from core.storage_provider import SQLiteStructuredRecordStore, SQLiteUnitOfWorkConflict
from backend.memory_app.model_config import ModelConfiguration
from backend.memory_app.v2.embedding_settings import EmbeddingSettings, EmbeddingSettingsError
from backend.memory_app.local_vector_assets import VectorInstallError
from backend.security.secrets import InMemorySecretStore


@pytest.fixture
def settings(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / 'settings.sqlite3')
    models = ModelConfiguration(records, tmp_path, InMemorySecretStore())
    service = EmbeddingSettings(records, tmp_path / 'data' / 'models')
    models.bind_embedding(service.project, vector_policy)
    try:
        yield records, models, service
    finally:
        service.close()
        models.close()


def configure_remote(models, revision=0):
    return models.update('embedding', {'base_url': 'https://synthetic.invalid/v1',
        'model': 'synthetic-remote', 'api_key': 'test-synthetic-vector',
        'enabled': True, 'allow_remote': True, 'expected_revision': revision})


def test_default_local_and_existing_enabled_remote_are_selected_without_mode_write(settings):
    records, models, _ = settings
    assert models.public()['embedding']['mode'] == 'local'
    configure_remote(models)
    assert models.public()['embedding']['mode'] == 'remote'
    assert records.list('v2_embedding_mode') == ()


def test_mode_preserves_remote_configuration_and_keeps_independent_revisions(settings):
    records, models, service = settings
    configure_remote(models)
    before = records.read('recognition_model_config', 'embedding')
    service.update_mode(mode='local', expected_revision=0)
    assert records.read('recognition_model_config', 'embedding') == before
    configure_remote(models, revision=1)
    public = models.public()['embedding']
    assert public['mode'] == 'local' and public['revision'] == 2 and public['mode_revision'] == 1
    with pytest.raises(SQLiteUnitOfWorkConflict):
        service.update_mode(mode='remote', expected_revision=0)
    service.update_mode(mode='remote', expected_revision=1)
    restored = models.snapshot('embedding')
    assert restored['model'] == 'synthetic-remote'
    assert restored['base_url'] == 'https://synthetic.invalid/v1'
    assert restored['api_key'] == 'test-synthetic-vector'
    assert restored['revision'] == 2 and restored['mode_revision'] == 2


def test_install_failure_progress_and_retry_are_true_revisioned_jobs(settings, monkeypatch):
    records, models, service = settings
    calls = []
    def fake_fetch(root, *, model, progress):
        calls.append(root)
        progress({'done': 10, 'total': 100})
        if len(calls) == 1:
            raise VectorInstallError('embedding_source_checksum_failed')
    monkeypatch.setattr('backend.memory_app.v2.embedding_settings.install_embedding', fake_fetch)
    first = service.install(models.public()['embedding'], expected_revision=0)
    service.close()
    failed = records.read('v2_embedding_install', 'default')
    assert failed.payload['status'] == 'failed'
    assert failed.payload['reason_code'] == 'embedding_source_checksum_failed'
    assert failed.payload['progress'] == {'done': 10, 'total': 100}
    assert service.local()['status'] == 'failed'
    second = service.install(models.public()['embedding'], expected_revision=0)
    service.close()
    assert second['job_id'] != first['job_id'] and len(calls) == 2
    ready = records.read('v2_embedding_install', 'default')
    assert ready.revision > failed.revision and ready.payload['status'] == 'ready'
    assert ready.payload['reason_code'] is None and ready.payload['progress'] is None


def test_interrupted_prior_session_projects_failed_and_retries_without_overwriting_facts(settings, monkeypatch):
    records, models, service = settings
    with records.begin() as tx:
        tx.put('v2_embedding_install', 'default', {'status': 'installing', 'session': 'prior-process',
            'job_id': 'prior-job', 'progress': {'done': 7, 'total': 100}}, expected_revision=0)
        tx.commit()
    state = service.local()
    assert state['status'] == 'failed' and state['reason_code'] == 'embedding_install_interrupted'
    monkeypatch.setattr('backend.memory_app.v2.embedding_settings.install_embedding', lambda root, *, model, progress: None)
    job = service.install(models.public()['embedding'], expected_revision=0)
    service.close()
    assert job['job_id'] != 'prior-job'
    assert records.read('v2_embedding_install', 'default').payload['status'] == 'ready'


@pytest.mark.parametrize('revision', [True, -1, '0'])
def test_install_invalid_revision_never_creates_job(settings, revision):
    records, models, service = settings
    with pytest.raises(EmbeddingSettingsError, match='embedding_install_invalid'):
        service.install(models.public()['embedding'], expected_revision=revision)
    assert records.list('v2_embedding_install') == ()
