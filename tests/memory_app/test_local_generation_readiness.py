import json
from pathlib import Path
import subprocess
import sys

from fastapi import FastAPI
from fastapi.testclient import TestClient
import numpy as np
import pytest
from safetensors.numpy import save_file
from tokenizers import Tokenizer
from tokenizers.models import WordLevel

from backend.memory_app.local_generation_readiness import inspect_local_generation
from backend.memory_app.local_model import install_local_model_routes
from backend.memory_app.model_config import ModelConfiguration, ModelConfigurationError
from backend.security.secrets import InMemorySecretStore
from core.storage_provider import SQLiteStructuredRecordStore


def incomplete_assets(root):
    directory = root / 'models/qwen2.5-1.5b-instruct'
    directory.mkdir(parents=True)
    config = {'model_type': 'qwen2', 'architectures': ['Qwen2ForCausalLM'], 'hidden_size': 1536,
        'num_hidden_layers': 28, 'num_attention_heads': 12, 'num_key_value_heads': 2,
        'intermediate_size': 8960, 'vocab_size': 151936, 'tie_word_embeddings': True}
    (directory / 'config.json').write_text(json.dumps(config), encoding='utf-8')
    (directory / 'tokenizer_config.json').write_text(json.dumps({'tokenizer_class': 'Qwen2Tokenizer',
        'chat_template': '{{ messages[0].content }}'}), encoding='utf-8')
    Tokenizer(WordLevel({'[UNK]': 0}, unk_token='[UNK]')).save(str(directory / 'tokenizer.json'))
    return directory


def test_single_damaged_asset_is_not_installed_and_mode_cas_preserves_api(tmp_path):
    models = ModelConfiguration(SQLiteStructuredRecordStore(tmp_path / 'models.sqlite3'), tmp_path, InMemorySecretStore())
    models.update('generation', {'model': 'qa', 'base_url': 'https://example.invalid/v1',
        'api_key': 'synthetic-only', 'allow_remote': True, 'expected_revision': 0})
    directory = tmp_path / 'data/models/qwen2.5-1.5b-instruct'
    directory.mkdir(parents=True)
    (directory / 'model.safetensors').write_bytes(b'damaged incomplete file')
    before = models.generation_mode()
    assert not before['local_model_installed'] and before['local_readiness']['status'] == 'missing_assets'
    with pytest.raises(ModelConfigurationError, match='local_model_assets_missing'):
        models.update_generation_mode(mode='local', local_enabled=True,
            local_base_url='http://127.0.0.1:8001/local-model/v1', expected_revision=0)
    assert models.generation_mode() == before and models.snapshot('generation')['api_key'] == 'synthetic-only'
    models.update_generation_mode(mode='api', local_enabled=False,
        local_base_url='http://127.0.0.1:8001/local-model/v1', expected_revision=0)
    with pytest.raises(ModelConfigurationError, match='generation_mode_revision_conflict'):
        models.update_generation_mode(mode='local', local_enabled=True,
            local_base_url='http://127.0.0.1:8001/local-model/v1', expected_revision=0)


@pytest.mark.parametrize('shard', ['part-00001.safetensors', '../outside.safetensors'])
def test_official_shard_index_requires_present_contained_shards(tmp_path, shard):
    directory = incomplete_assets(tmp_path)
    (directory / 'model.safetensors.index.json').write_text(json.dumps({'weight_map': {'model.norm.weight': shard}}), encoding='utf-8')
    state = inspect_local_generation(directory).public()
    assert state['eligible'] is False
    if shard.startswith('..'):
        assert state['status'] == 'invalid_assets'
    else:
        assert state['status'] == 'missing_assets' and shard in state['missing_files']


@pytest.mark.parametrize('failure,expected', [('weights', 'local_model_weights_invalid'),
    ('tokenizer', 'local_model_tokenizer_invalid'), ('template', 'local_model_tokenizer_invalid'),
    ('incomplete', 'local_model_weights_incomplete'), ('adapter', 'local_model_config_invalid')])
def test_real_asset_parsers_reject_corrupt_or_incomplete_assets(tmp_path, failure, expected):
    directory = incomplete_assets(tmp_path)
    save_file({'model.norm.weight': np.zeros((1536,), dtype=np.float32)}, str(directory / 'model.safetensors'))
    if failure == 'weights':
        (directory / 'model.safetensors').write_bytes(b'corrupt safetensors')
    elif failure == 'tokenizer':
        (directory / 'tokenizer.json').write_text('{}', encoding='utf-8')
    elif failure == 'template':
        (directory / 'tokenizer_config.json').write_text(json.dumps({'chat_template': '{% invalid %}'}), encoding='utf-8')
    elif failure == 'adapter':
        (directory / 'adapter_config.json').write_text(json.dumps({'base_model_name_or_path': '../outside'}), encoding='utf-8')
    state = inspect_local_generation(directory).public()
    assert state['assets_ready'] is False and state['eligible'] is False
    assert state['status'] == 'invalid_assets' and state['reason_code'] == expected
    assert inspect_local_generation(directory).public() == state


@pytest.mark.parametrize('keep_default', [True, False])
def test_versioned_tokenizer_uses_actual_selection_and_tracks_file_identity(tmp_path, keep_default):
    from transformers.tokenization_utils_base import get_fast_tokenizer_file
    directory = incomplete_assets(tmp_path)
    name = 'tokenizer.0.0.json'
    Tokenizer(WordLevel({'[UNK]': 0, 'versioned': 1}, unk_token='[UNK]')).save(str(directory / name))
    config_path = directory / 'tokenizer_config.json'
    config = json.loads(config_path.read_text(encoding='utf-8'))
    config['fast_tokenizer_files'] = [name]
    config_path.write_text(json.dumps(config), encoding='utf-8')
    if not keep_default:
        (directory / 'tokenizer.json').unlink()
    save_file({'model.norm.weight': np.zeros((1536,), dtype=np.float32)}, str(directory / 'model.safetensors'))
    assert get_fast_tokenizer_file(config['fast_tokenizer_files']) == name
    before = inspect_local_generation(directory)
    assert before.public()['reason_code'] == 'local_model_weights_incomplete'
    assert name in {entry[0] for entry in before.identity[1]}
    (directory / name).write_text('{}', encoding='utf-8')
    after = inspect_local_generation(directory)
    assert after.identity != before.identity
    assert after.public()['reason_code'] == 'local_model_tokenizer_invalid'


def test_versioned_tokenizer_rejects_sibling_path_before_qualification(tmp_path):
    from transformers import AutoTokenizer
    directory = incomplete_assets(tmp_path)
    sibling = directory.parent / 'tokenizer.0.0.json'
    Tokenizer(WordLevel({'[UNK]': 0, 'sibling': 1}, unk_token='[UNK]')).save(str(sibling))
    config_path = directory / 'tokenizer_config.json'
    config = json.loads(config_path.read_text(encoding='utf-8'))
    config['fast_tokenizer_files'] = ['../tokenizer.0.0.json']
    config_path.write_text(json.dumps(config), encoding='utf-8')
    # 只用专属目录的合成相邻文件证明实际库会消费该路径。
    original = AutoTokenizer.from_pretrained(directory, local_files_only=True, trust_remote_code=False)
    assert original.get_vocab()['sibling'] == 1
    state = inspect_local_generation(directory).public()
    assert state['status'] == 'invalid_assets' and state['reason_code'] == 'local_model_tokenizer_invalid'


def test_actual_meta_config_error_is_reported_through_normal_factory(tmp_path):
    directory = incomplete_assets(tmp_path / 'data')
    config_path = directory / 'config.json'
    config = json.loads(config_path.read_text(encoding='utf-8'))
    config['hidden_act'] = 'readiness-invalid-activation'
    config_path.write_text(json.dumps(config), encoding='utf-8')
    save_file({'model.norm.weight': np.zeros((1536,), dtype=np.float32)}, str(directory / 'model.safetensors'))
    settings = tmp_path / 'config/settings.toml'
    settings.parent.mkdir()
    settings.write_bytes(Path('config/settings.toml.example').read_bytes())
    from backend.memory_app.app import create_app
    from tests.memory_app.test_api import _shutdown
    app = create_app(runtime_root=tmp_path)
    client = TestClient(app, client=('127.0.0.1', 1234))
    try:
        response = client.get('/api/v2/settings?project_id=inbox')
        assert response.status_code == 200
        state = response.json()['model']['generation_mode']['local_readiness']
        assert state['status'] == 'invalid_assets' and state['reason_code'] == 'local_model_config_invalid'
        assert client.get('/local-model/v1/models').json()['data'] == []
        rejected = client.put('/api/v2/settings/generation-mode', json={'mode': 'local', 'local_enabled': True,
            'local_base_url': 'http://127.0.0.1:8001/local-model/v1', 'expected_revision': 0})
        assert rejected.status_code == 400 and rejected.json()['detail'] == 'local_model_config_invalid'
    finally:
        _shutdown(client)


def test_generic_loopback_auxiliary_gateway_keeps_exact_base_url(tmp_path):
    models = ModelConfiguration(SQLiteStructuredRecordStore(tmp_path / 'models.sqlite3'), tmp_path, InMemorySecretStore())
    models.update('generation', {'model': 'local-qa', 'base_url': 'http://127.0.0.1:8765/v1',
        'allow_remote': False, 'enabled': True, 'api_key': 'synthetic-only', 'expected_revision': 0})
    models.update_fast_model(model='local-qa', expected_revision=0, expected_generation_revision=1, expected_mode_revision=0)
    selected = models.for_auxiliary(models.freeze_auxiliary_binding())
    snapshot = selected.snapshot('generation')
    gateway = selected._generation_gateway(snapshot)
    assert snapshot['model'] == 'local-qa' and snapshot['base_url'] == 'http://127.0.0.1:8765/v1'
    assert gateway._base_url == 'http://127.0.0.1:8765/v1'


def persist_legacy_selection(records):
    generation = records.read('recognition_model_config', 'generation')
    mode = records.read('recognition_generation_mode', 'default')
    with records.begin() as tx:
        tx.put('recognition_model_config', 'generation', {'provider': 'openai', 'model': 'qwen2.5-1.5b-instruct',
            'base_url': 'http://127.0.0.1:8001/local-model/v1', 'allow_remote': False, 'enabled': True, 'secret_ref': ''},
            expected_revision=generation.revision if generation else 0)
        tx.put('recognition_generation_mode', 'default', {'mode': 'local', 'local_enabled': True,
            'local_base_url': 'http://127.0.0.1:8001/local-model/v1', 'api_config': {}}, expected_revision=mode.revision if mode else 0)
        tx.commit()


def test_selected_legacy_profile_with_lost_assets_returns_specific_503(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / 'models.sqlite3')
    models = ModelConfiguration(records, tmp_path, InMemorySecretStore())
    persist_legacy_selection(records)
    assert models.local_generation_selected() and not models.local_generation_allowed()
    with pytest.raises(ModelConfigurationError, match='local_model_assets_missing'):
        models.snapshot('generation')
    app = FastAPI()
    install_local_model_routes(app, runtime_root=tmp_path, generation_allowed=models.local_generation_allowed,
        generation_selected=models.local_generation_selected)
    client = TestClient(app, client=('127.0.0.1', 1234))
    response = client.post('/local-model/v1/chat/completions', json={'model': 'qwen2.5-1.5b-instruct',
        'messages': [{'role': 'user', 'content': 'readiness only'}]})
    assert response.status_code == 503 and response.json()['detail'] == 'local_model_assets_missing'
    assert client.get('/local-model/v1/models').json()['data'] == []
    assert models.generation_mode()['local_readiness']['status'] == 'missing_assets'


def test_asset_identity_tracks_actual_directory_and_consumed_optional_files(tmp_path):
    first = incomplete_assets(tmp_path / 'first')
    second = incomplete_assets(tmp_path / 'second')
    before = inspect_local_generation(first)
    assert before.identity != inspect_local_generation(second).identity
    optional = first / 'generation_config.json'
    optional.write_text('{"eos_token_id":151645}', encoding='utf-8')
    after = inspect_local_generation(first)
    assert before.identity != after.identity
    optional.write_text('{"eos_token_id":[151645,151643]}', encoding='utf-8')
    assert after.identity != inspect_local_generation(first).identity
    before_vocabulary = inspect_local_generation(first).identity
    (first / 'vocab.json').write_text('{"[UNK]":0}', encoding='utf-8')
    assert before_vocabulary != inspect_local_generation(first).identity
    assert inspect_local_generation(first).public()['status'] == 'missing_assets'


def test_normal_factory_settings_and_mode_share_actual_readiness(tmp_path):
    config = tmp_path / 'config/settings.toml'
    config.parent.mkdir()
    config.write_bytes(Path('config/settings.toml.example').read_bytes())
    from backend.memory_app.app import create_app
    from tests.memory_app.test_api import _shutdown
    app = create_app(runtime_root=tmp_path)
    client = TestClient(app, client=('127.0.0.1', 1234))
    try:
        models = app.state.recognition_models
        assert models.records is app.state.recognition_records
        before = client.get('/api/v2/settings?project_id=inbox').json()['model']['generation_mode']
        assert before['local_readiness']['status'] == 'missing_assets'
        rejected = client.put('/api/v2/settings/generation-mode', json={'mode': 'local',
            'local_enabled': True, 'local_base_url': 'http://127.0.0.1:8001/local-model/v1', 'expected_revision': before['revision']})
        assert rejected.status_code == 400 and rejected.json()['detail'] == 'local_model_assets_missing'
        assert client.get('/api/v2/settings?project_id=inbox').json()['model']['generation_mode'] == before
        request = {'model': 'qwen2.5-1.5b-instruct', 'messages': [{'role': 'user', 'content': 'readiness only'}]}
        assert client.post('/local-model/v1/chat/completions', json=request).status_code == 403
        assert client.get('/local-model/v1/models').json()['data'] == []
        remote = TestClient(app, client=('192.0.2.1', 1234))
        assert remote.post('/local-model/v1/chat/completions', json=request).status_code == 403
        persist_legacy_selection(models.records)
        lost = client.post('/local-model/v1/chat/completions', json=request)
        assert lost.status_code == 503 and lost.json()['detail'] == 'local_model_assets_missing'
        current = client.get('/api/v2/settings?project_id=inbox').json()['model']['generation_mode']
        assert current['mode'] == 'local' and current['local_readiness']['status'] == 'missing_assets'
        assert not models.local_generation_allowed()
    finally:
        _shutdown(client)


def test_missing_dependencies_are_reported_by_actual_isolated_interpreter(tmp_path):
    root = tmp_path.resolve()
    (root / 'models').mkdir(parents=True, exist_ok=True)
    command = [sys.executable, '-I', '-S', str(Path('tools/native_local_generation_check.py').resolve()),
        '--root', str(root), '--asset-root', str(root / 'models'), '--probe-only']
    result = subprocess.run(command, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    state = json.loads(result.stdout)
    assert set(state['missing_dependencies']) == {'torch', 'transformers', 'tokenizers', 'safetensors', 'jinja2'}
    assert state['eligible'] is False and state['assets_ready'] is False
    directory = incomplete_assets(root)
    name = 'tokenizer.0.0.json'
    Tokenizer(WordLevel({'[UNK]': 0}, unk_token='[UNK]')).save(str(directory / name))
    config_path = directory / 'tokenizer_config.json'
    config = json.loads(config_path.read_text(encoding='utf-8'))
    config['fast_tokenizer_files'] = [name]
    config_path.write_text(json.dumps(config), encoding='utf-8')
    save_file({'model.norm.weight': np.zeros((1536,), dtype=np.float32)}, str(directory / 'model.safetensors'))
    versioned = subprocess.run(command, capture_output=True, text=True, timeout=15)
    assert versioned.returncode == 0, versioned.stderr
    state = json.loads(versioned.stdout)
    assert state['status'] == 'missing_dependencies' and state['missing_files'] == []
    assert state['eligible'] is False and state['assets_ready'] is False
