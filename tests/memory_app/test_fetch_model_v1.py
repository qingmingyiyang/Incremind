"""安装CLI协议；仅此组隔离资产安装边界，真实资产owner另有集成验收。"""
from pathlib import Path
import runpy
import pytest
from backend.memory_app import local_vector_assets


@pytest.mark.parametrize('with_source', [False, True], ids=['default_huggingface', 'read_only_from'])
def test_fetch_model_cli_preserves_root_model_source_and_progress(tmp_path, monkeypatch, capsys, with_source):
    source = tmp_path / 'unified-source'
    source.mkdir()
    sentinel = source / 'untouched.txt'
    sentinel.write_text('synthetic source stays here', encoding='utf-8')
    before = sentinel.read_bytes(), sentinel.stat().st_mtime_ns
    calls = []
    def install(models_root, *, model, source, progress):
        calls.append((models_root, model, source))
        progress({'done': 2, 'total': 3})
    monkeypatch.setattr(local_vector_assets, 'install_embedding', install)
    tool = runpy.run_path(str(Path(__file__).resolve().parents[2] / 'tools/fetch_model.py'))
    target = tmp_path / 'isolated-app'
    argv = ['embedding', '--root', str(target)]
    if with_source:
        argv += ['--from', str(source)]
    tool['main'](argv)
    assert calls == [(target / 'data/models', 'google/embeddinggemma-2', source if with_source else None)]
    assert capsys.readouterr().out == '2 / 3\n'
    assert (sentinel.read_bytes(), sentinel.stat().st_mtime_ns) == before
    assert not target.exists()


@pytest.mark.parametrize('invalid', ['model', 'exclusive_source'])
def test_fetch_model_cli_rejects_invalid_selection_before_install(tmp_path, monkeypatch, invalid):
    calls = []
    monkeypatch.setattr(local_vector_assets, 'install_embedding', lambda *args, **kwargs: calls.append(1))
    tool = runpy.run_path(str(Path(__file__).resolve().parents[2] / 'tools/fetch_model.py'))
    argv = ['wrong-model' if invalid == 'model' else 'embedding', '--root', str(tmp_path / 'app')]
    if invalid == 'exclusive_source':
        argv += ['--from', str(tmp_path / 'source'), '--source', 'huggingface']
    with pytest.raises(SystemExit) as error:
        tool['main'](argv)
    assert error.value.code == 2
    assert calls == []
    assert not (tmp_path / 'app').exists()


def test_fetch_model_cli_real_installer_rejects_tiny_source_without_partial_publish(tmp_path, monkeypatch):
    from safetensors import safe_open
    from tests.memory_app.test_vector_assets_v1 import synthetic_source
    source, _ = synthetic_source(tmp_path)
    before = {path.relative_to(source).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
              for path in source.rglob('*') if path.is_file()}
    # 官方格式确认合成来源有效；产品--from首先按完整来源字节数拒绝，未进入参数派生。
    with safe_open(str(source / 'model.safetensors'), framework='pt', device='cpu') as stream:
        assert set(stream.keys()) == {'language_model.synthetic', 'vision_tower.synthetic', 'audio_tower.synthetic'}
    downloads = []
    def no_download(*args, **kwargs):
        downloads.append(1)
        raise AssertionError('read-only --from reached download')
    monkeypatch.setattr(local_vector_assets, '_download', no_download)
    original_contract = local_vector_assets.TEXT_PARAMETERS, local_vector_assets.TEXT_BYTES, dict(local_vector_assets.derive_text_assets.__kwdefaults__)
    tool = runpy.run_path(str(Path(__file__).resolve().parents[2] / 'tools/fetch_model.py'))
    assert tool['install_embedding'] is local_vector_assets.install_embedding
    target = tmp_path / 'isolated-app'
    with pytest.raises(local_vector_assets.VectorInstallError, match='^embedding_source_size_invalid$'):
        tool['main'](['embedding', '--root', str(target), '--from', str(source)])
    models_root = target / 'data/models'
    assert models_root.is_dir() and list(models_root.iterdir()) == []
    assert downloads == []
    assert {path.relative_to(source).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
            for path in source.rglob('*') if path.is_file()} == before
    assert (local_vector_assets.TEXT_PARAMETERS, local_vector_assets.TEXT_BYTES,
            local_vector_assets.derive_text_assets.__kwdefaults__) == original_contract
