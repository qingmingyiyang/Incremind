"""派生格式用官方 safetensors API；小合成权重不代签真实模型兼容。"""
from backend.memory_app.v2.embedding_settings import vector_policy
import json
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import pytest

from backend.memory_app.local_vector_assets import (
    FILES, VectorInstallError, derive_text_assets, installed_assets, _safe_path,
)
from tests.memory_app.v2.test_local_vector_consumers_v1 import marker_assets


@pytest.fixture(scope='module', autouse=True)
def own_source_provenance():
    owned_source = Path(__file__).resolve().parents[2] / 'src'
    for name, module in tuple(sys.modules.items()):
        if name.split('.')[0] in {'backend', 'core'} and getattr(module, '__file__', None):
            assert Path(module.__file__).resolve().is_relative_to(owned_source), name
    print(json.dumps({'stage': 'assets_source_provenance', 'source': str(owned_source),
        'assets_file': sys.modules['backend.memory_app.local_vector_assets'].__file__,
        'vectors_file': sys.modules['backend.memory_app.local_vectors'].__file__}))


@pytest.mark.parametrize('manifest', [[], None, {'files': []}, {'files': {}},
    {'model': 'other', 'files': {}}, {'files': {'model.safetensors': 'bad'}}])
def test_invalid_manifest_is_missing_instead_of_crashing(tmp_path, manifest):
    directory = marker_assets(tmp_path)
    (directory / 'embedding-manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    assert not installed_assets(directory, model=vector_policy()['model'])


def test_installed_metadata_requires_all_files_and_exact_sizes(tmp_path):
    directory = marker_assets(tmp_path)
    assert installed_assets(directory, model=vector_policy()['model'])
    (directory / 'tokenizer.json').write_bytes(b'changed synthetic bytes')
    assert not installed_assets(directory, model=vector_policy()['model'])


def synthetic_source(tmp_path):
    import torch
    import safetensors
    from safetensors.torch import save_file
    print(json.dumps({'stage': 'assets_official_format', 'torch_file': torch.__file__,
        'torch_version': torch.__version__, 'safetensors_file': safetensors.__file__,
        'safetensors_version': safetensors.__version__}))
    source = tmp_path / 'source'
    source.mkdir()
    for name in FILES:
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{}', encoding='utf-8')
    (source / 'config.json').write_text(json.dumps({'model_type': 'embedding_gemma2',
        'vision_config': {'synthetic': True}, 'audio_config': {'synthetic': True}}), encoding='utf-8')
    save_file({name: torch.tensor([[1.0, 2.0], [3.0, 4.0]], dtype=torch.float32) for name in (
        'language_model.synthetic', 'vision_tower.synthetic', 'audio_tower.synthetic')},
        str(source / 'model.safetensors'), metadata={'format': 'pt'})
    destination = tmp_path / 'text'
    destination.mkdir()
    return source, destination


def test_official_format_derivation_keeps_only_text_and_marks_derivative(tmp_path):
    from safetensors import safe_open
    source, destination = synthetic_source(tmp_path)
    before = {path.relative_to(source).as_posix(): (path.stat().st_size, path.stat().st_mtime_ns)
              for path in source.rglob('*') if path.is_file()}
    manifest = derive_text_assets(source, destination, expected_count=1, expected_bytes=16, model=vector_policy()['model'])
    with safe_open(str(destination / 'model.safetensors'), framework='pt', device='cpu') as stream:
        assert stream.keys() == ['language_model.synthetic']
        assert stream.get_tensor('language_model.synthetic').tolist() == [[1.0, 2.0], [3.0, 4.0]]
    config = json.loads((destination / 'config.json').read_text(encoding='utf-8'))
    assert config['vision_config'] is None and config['audio_config'] is None
    assert manifest['derivation'] == 'text-only@1'
    assert manifest['tensor_count'] == 1 and manifest['tensor_bytes'] == 16
    assert manifest['source']['tensor_count'] == 3
    assert set(manifest['files']) == {*FILES, 'model.safetensors'}
    assert {path.relative_to(source).as_posix(): (path.stat().st_size, path.stat().st_mtime_ns)
            for path in source.rglob('*') if path.is_file()} == before


@pytest.mark.parametrize('count, size', [(2, 16), (1, 32)])
def test_derivation_rejects_wrong_text_parameter_contract_before_publish(tmp_path, count, size):
    source, destination = synthetic_source(tmp_path)
    with pytest.raises(VectorInstallError, match='embedding_text_parameters_invalid'):
        derive_text_assets(source, destination, expected_count=count, expected_bytes=size, model=vector_policy()['model'])
    assert list(destination.iterdir()) == []


def test_derivation_rejects_source_checksum_before_publish(tmp_path):
    source, destination = synthetic_source(tmp_path)
    proof = {'kind': 'synthetic', 'files': {'model.safetensors': {
        'size': (source / 'model.safetensors').stat().st_size, 'sha256': '0' * 64}}}
    with pytest.raises(VectorInstallError, match='embedding_source_checksum_failed'):
        derive_text_assets(source, destination, source_info=proof, expected_count=1, expected_bytes=16, model=vector_policy()['model'])
    assert list(destination.iterdir()) == []


def test_actual_windows_junction_is_rejected_without_reading_target():
    owned = Path('D:/CAQ-20261008-dispatch-V1').resolve()
    root = (owned / uuid4().hex).resolve()
    assert root.is_relative_to(owned) and root != owned
    target, link = root / 'target', root / 'link'
    target.mkdir(parents=True)
    try:
        created = subprocess.run(['cmd.exe', '/c', 'mklink', '/J', str(link), str(target)],
            capture_output=True, check=True)
        assert created.returncode == 0 and link.is_junction()
        with pytest.raises(VectorInstallError, match='embedding_asset_link_forbidden'):
            _safe_path(link / 'model.safetensors')
    finally:
        # 只移除自己创建的目录联接和空目录，不跟随目标删除。
        if link.is_junction():
            link.rmdir()
        target.rmdir()
        root.rmdir()
