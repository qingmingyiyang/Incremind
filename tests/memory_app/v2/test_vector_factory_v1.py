from backend.memory_app.v2.embedding_settings import vector_policy
from contextlib import closing
"""完整工厂的公开设置触发；只隔离模型传输、编码与资产 fetch。"""
import json
import time
from functools import partial
from pathlib import Path
from shutil import copyfile
from threading import Event
from types import SimpleNamespace
import sqlite3
import pytest
from fastapi.testclient import TestClient
from backend.memory_app.app import create_app
from backend.memory_app import local_vectors
from backend.memory_app.v2.embedding_settings import EmbeddingSettings
from backend.memory_app.v2.privacy import set_private_project
from backend.memory_app.v2.memory_turn import MemoryTurn
from backend.memory_app.model_costs import attempt_cost
from backend.shared.llm.litellm_gateway import LiteLLMCompletionGateway
from tests.memory_app.v2.test_local_vector_consumers_v1 import marker_assets
from tests.memory_app.v2.test_workbench_ask import add_document

def eventually(read, predicate):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        value = read()
        if predicate(value):
            return value
        time.sleep(.05)
    raise AssertionError('real factory background job did not reach expected state')

@pytest.mark.parametrize('trigger', ['mode', 'install'])
def test_full_factory_public_setting_runs_app_held_index_through_daily_jobs(tmp_path, monkeypatch, trigger):
    def completion(**request):
        raw = json.dumps({'title': 'Synthetic', 'summary': 'alpha', 'facts': [], 'topics': [],
            'todos': [], 'uncertainties': [], 'people': [], 'dates': [], 'suggestions': []})
        return {'choices': [{'message': {'content': raw}, 'finish_reason': 'stop'}],
                'usage': {'prompt_tokens': 3, 'completion_tokens': 2, 'total_tokens': 5}}
    monkeypatch.setattr('backend.memory_app.model_config._load_litellm_completion', completion)
    async def acompletion(**request):
        return completion(**request)
    calls = []
    class Encoder:
        def __init__(self, directory):
            pass
        def encode(self, texts, input_type, *, policy):
            calls.append((tuple(texts), input_type))
            return [[1.0] + [0.0] * 255 for _ in texts], 2 * len(texts)
    monkeypatch.setattr(local_vectors, 'SentenceEncoder', Encoder)
    monkeypatch.setattr(local_vectors, 'dependencies_available', lambda: True)
    monkeypatch.setattr('backend.memory_app.v2.embedding_settings.dependencies_available', lambda: True)
    entered, release = Event(), Event()
    def fetch(root, *, model, progress):
        entered.set()
        assert release.wait(10)
        marker_assets(tmp_path)
        progress({'done': 10, 'total': 10})
    monkeypatch.setattr('backend.memory_app.v2.embedding_settings.install_embedding', fetch)
    # 完整工厂读取实际runtime根配置，只复制仓库公开模板到本轮测试根。
    config = tmp_path / 'config'
    config.mkdir()
    copyfile(Path(__file__).resolve().parents[3] / 'config/settings.toml.example', config / 'settings.toml')
    app = create_app(runtime_root=tmp_path)
    models = app.state.recognition_models
    # 保留真实 Gateway，仅明确注入异步 SDK 传输边界，避免其默认冷加载。
    models._gateway_factory = partial(LiteLLMCompletionGateway, acompletion_fn=acompletion)
    records = app.state.recognition_records
    models.update('generation', {'base_url': 'https://synthetic.invalid/v1', 'model': 'synthetic-generation',
        'api_key': 'test-synthetic-generation', 'enabled': True, 'allow_remote': True, 'expected_revision': 0})
    domains = app.state.workspace_domains
    env = SimpleNamespace(root=tmp_path, records=records, documents=app.state.recognition_documents,
        model=models, domains=domains)
    network = []
    def disconnected(*args, **kwargs):
        network.append(1)
        raise AssertionError('embedding reached external HTTP')
    monkeypatch.setattr('backend.memory_app.retrieval_models.httpx.Client', disconnected)
    try:
        with TestClient(app, base_url='http://127.0.0.1:8020') as client:
            identity, _ = add_document(env, summary='合成概览', body='orchidmarker backup connector is blue.')
            set_private_project(records, 'alpha', True, expected_revision=0)
            facts = {name: records.list(name) for name in ('documents', 'workspace_items', 'recognitions')}
            index = app.state.memory_embedding_index
            assert index.query is domains.query and index.models is models
            assert app.state.memory_daily_jobs.jobs['local_embedding_index'].__self__ is index
            assert not calls
            if trigger == 'mode':
                EmbeddingSettings(records, models._local_models_root).update_mode(mode='remote', expected_revision=0)
                marker_assets(tmp_path)
                response = client.patch('/api/v2/settings/embedding-mode', json={'mode': 'local', 'expected_revision': 1})
                assert response.status_code == 200
            else:
                response = client.post('/api/v2/settings/embedding/install', json={'expected_revision': 0})
                assert response.status_code == 202 and entered.wait(10)
                release.set()
            progress = eventually(lambda: records.read('v2_embedding_index', 'default'),
                lambda row: row is not None and row.payload['progress']['total'] > 0
                    and row.payload['progress']['done'] == row.payload['progress']['total'])
            assert progress.payload['reason_code'] is None
            assert calls and all(kind == 'document' for _, kind in calls)
            with closing(sqlite3.connect(records.database_path.parent / 'recognition-vectors.sqlite3')) as connection:
                assert connection.execute('SELECT COUNT(*) FROM recognition_embedding_cache').fetchone()[0] > 0
            keys = [row for row in records.list('v2_memory_turn_keys')
                if row.payload['identity']['key'].get('namespace') == 'local-index-v1']
            assert keys
            store = MemoryTurn.store_for(records)
            receipts = [store.get(event['data']['receipt_ref']) for row in keys
                for event in store.events_after(row.object_id) if event['type'] == 'model.attempt.terminal']
            assert len(receipts) == len(keys) and all(row['status'] == 'succeeded'
                and row['provider_id'] == 'local' and row['attempt_number'] == 1 for row in receipts)
            assert all(attempt_cost(records, row) == {'currency': 'CNY', 'amount': '0'} for row in receipts)
            assert all(store.get_request(row.object_id)['privacy']['mode'] == 'local_only' for row in keys)
            assert {name: records.list(name) for name in facts} == facts and network == []
    finally:
        release.set()
        local_vectors.worker_for(local_vectors.model_directory(models._local_models_root), policy=vector_policy()).close()
        models.close()
