"""可选依赖缺失时保持真实本机配置和消费者的可恢复降级。"""
import pytest

from backend.memory_app import local_vectors
from tests.memory_app.v2.test_local_vector_consumers_v1 import local_query, private_scores
from tests.memory_app.v2.test_workbench_ask import env
from tests.memory_app.v2.kernel_receipts import wire_receipts


@pytest.mark.parametrize('failure', ['missing', 'old_sentence_transformers', 'old_transformers'])
def test_dependency_metadata_rejects_missing_and_unsupported_versions(monkeypatch, failure):
    minimums = {'sentence-transformers': '6.1', 'transformers': '5.19',
        'torch': '2.7', 'safetensors': '0.7', 'torchvision': '0.22',
        'Pillow': '10.0', 'librosa': '0.11'}
    def version(name):
        if name == 'sentence-transformers' and failure == 'missing':
            raise local_vectors.PackageNotFoundError(name)
        if name == 'sentence-transformers' and failure == 'old_sentence_transformers':
            return '6.0'
        if name == 'transformers' and failure == 'old_transformers':
            return '5.18'
        return minimums[name]
    monkeypatch.setattr(local_vectors, 'package_version', version)
    assert local_vectors.dependencies_available() is False


def test_missing_dependencies_degrade_real_private_consumer_without_network_or_receipt(env, local_query, monkeypatch):
    query, calls = local_query
    # 只替换可选依赖探针，不替换配置读模型或消费者。
    monkeypatch.setattr(local_vectors, 'dependencies_available', lambda: False)
    monkeypatch.setattr('backend.memory_app.v2.embedding_settings.dependencies_available', lambda: False)
    public = query.models.public()['embedding']
    assert public['mode'] == 'local' and public['configured'] is False
    assert public['local']['reason_code'] == 'local_vector_dependencies_missing'
    scores, _ = private_scores(env, query)
    assert scores == {} and calls == []
    assert wire_receipts(env.records) == [] and query._v1_network_calls == []
