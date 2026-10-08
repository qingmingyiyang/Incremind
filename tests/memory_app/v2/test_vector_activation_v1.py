"""独立activation契约，真实配置/线程/安装旁路，传输与线程调度外围受控。"""
from threading import Event, Thread
import pytest
from core.storage_provider import SQLiteStructuredRecordStore
from backend.memory_app import local_vectors, local_vector_assets
from backend.memory_app.v2 import embedding_settings, policies


def test_active_vector_default_uses_real_registry_model_dimension_and_version():
    assert policies.ACTIVE['vector'] == '@1'
    chosen = policies.version('vector')
    raw = policies.get('vector')()
    selected = embedding_settings.vector_policy()
    assert chosen == '@1'
    assert selected['model'] == raw['model'] == 'google/embeddinggemma-2'
    assert selected['dims'] == raw['dims'] == 256
    assert selected['model_key'] == f"{raw['model']}:{raw['dims']}:vector{chosen}"
    assert 'model_key' not in raw


@pytest.mark.parametrize('owner', ['worker', 'installer'])
def test_unknown_vector_model_is_rejected_before_load_write_or_download(tmp_path, monkeypatch, owner):
    calls = []
    policy = {**embedding_settings.vector_policy(), 'model': 'synthetic/unsupported-model'}
    root = tmp_path / 'models'
    def forbidden(*args, **kwargs):
        calls.append(1)
        raise AssertionError('unsupported model reached load or download')
    monkeypatch.setattr(local_vector_assets, '_download', forbidden)
    if owner == 'worker':
        worker = None
        try:
            with pytest.raises(local_vectors.LocalVectorError, match='^local_vector_configuration_invalid$'):
                worker = local_vectors.VectorWorker(root, policy=policy, loader=forbidden)
        finally:
            if worker is not None:
                worker.close()
                assert worker.thread is None or not worker.thread.is_alive()
                assert worker.pending == 0
    else:
        with pytest.raises(local_vector_assets.VectorInstallError, match='^embedding_model_unsupported$'):
            local_vector_assets.install_embedding(root, model=policy['model'])
    assert calls == []
    assert not root.exists()


def test_real_install_thread_freezes_policy_before_scheduler_release(tmp_path, monkeypatch):
    records = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    owner = embedding_settings.EmbeddingSettings(records, tmp_path / 'models')
    entered, release, fetched = Event(), Event(), Event()
    calls, callbacks = [], []
    original_policy = embedding_settings.vector_policy()
    class ObservedThread(Thread):
        def run(self):
            # 只在实际线程的target入口施加调度闸门，目标/参数/执行方法仍完全委托。
            entered.set()
            assert release.wait(10)
            return super().run()
    monkeypatch.setattr(embedding_settings, 'Thread', ObservedThread)
    def fetch(root, *, model, progress):
        calls.append((root, model))
        progress({'done': 1, 'total': 1})
        fetched.set()
    monkeypatch.setattr(embedding_settings, 'install_embedding', fetch)
    try:
        result = owner.install({'mode': 'local'}, expected_revision=0, on_ready=lambda: callbacks.append(1))
        assert result['job_id'] and entered.wait(10)
        # 在真实线程尚未运行target时改变读取器，安装必须仍使用调用时冻结选择。
        monkeypatch.setattr(embedding_settings, 'vector_policy', lambda: {**original_policy, 'model': 'synthetic/later-selection'})
        release.set()
        assert fetched.wait(10)
        owner.thread.join(10)
        assert not owner.thread.is_alive()
        assert calls == [(owner.models_root, original_policy['model'])]
        row = records.read('v2_embedding_install', 'default')
        assert row.payload['status'] == 'ready' and row.payload['job_id'] == result['job_id']
        # 旧资产任务完成不意味着新选择可索引；变化后的策略不可接旧on_ready。
        assert callbacks == []
    finally:
        release.set()
        owner.close()
    assert owner.thread is not None and not owner.thread.is_alive()
