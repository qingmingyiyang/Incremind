"""官方权重的文字派生安装；临时目录发布后只保留文字张量。"""
import hashlib
import json
from math import prod
from pathlib import Path
import shutil
from urllib.request import urlopen
from uuid import uuid4

from .local_vectors import model_directory, SUPPORTED_MODEL


FILES = ('config.json', 'config_sentence_transformers.json', 'modules.json',
         'sentence_bert_config.json', 'tokenizer_config.json', 'tokenizer.json',
         'tokenizer.model', 'processor_config.json', 'preprocessor_config.json',
         'chat_template.jinja', '1_Pooling/config.json', '2_Normalize/config.json')
TEXT_PARAMETERS = 413
TEXT_BYTES = 542005296
DOWNLOAD_BYTES = 1525787092


class VectorInstallError(ValueError):
    pass


def _safe_path(path):
    path = Path(path).absolute()
    for part in (path, *path.parents):
        if part.is_symlink() or part.is_junction():
            raise VectorInstallError('embedding_asset_link_forbidden')
    return path


def _digest(path):
    # 安装内部校验属于被测功能，不是执行者手工计算文件摘要。
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def installed_assets(directory, *, model):
    try:
        directory = _safe_path(directory)
        manifest = json.loads((directory / 'embedding-manifest.json').read_text(encoding='utf-8'))
        if not isinstance(manifest, dict) or not isinstance(manifest.get('files'), dict):
            return False
        return (manifest['model'] == model
                and manifest['derivation'] == 'text-only@1'
                and manifest['tensor_count'] == TEXT_PARAMETERS
                and set(manifest['files']) == {*FILES, 'model.safetensors'}
                and all(isinstance(info, dict) and type(info.get('size')) is int and info['size'] > 0
                        and _safe_path(directory / name).stat().st_size == info['size']
                        for name, info in manifest['files'].items()))
    except (OSError, ValueError, KeyError, TypeError):
        return False


def derive_text_assets(source, destination, *, model, source_info=None,
                       expected_count=TEXT_PARAMETERS, expected_bytes=TEXT_BYTES):
    """参数合同用于小合成权重测试，产品入口使用官方文字参数数量和字节数。"""
    from safetensors import safe_open
    from safetensors.torch import save_file

    source, destination = _safe_path(source), _safe_path(destination)
    config = json.loads(_safe_path(source / 'config.json').read_text(encoding='utf-8'))
    if config.get('model_type') != 'embedding_gemma2':
        raise VectorInstallError('embedding_source_model_invalid')
    weight = _safe_path(source / 'model.safetensors')
    info = source_info or {'kind': 'local', 'files': {}}
    if info.get('weight_size') is not None and weight.stat().st_size != info['weight_size']:
        raise VectorInstallError('embedding_source_size_invalid')
    tensors = {}
    with safe_open(str(weight), framework='pt', device='cpu') as stream:
        names = [name for name in stream.keys() if name.startswith('language_model.')]
        if len(names) != expected_count:
            raise VectorInstallError('embedding_text_parameters_invalid')
        metadata_bytes = 0
        for name in names:
            view = stream.get_slice(name)
            width = {'BF16': 2, 'F16': 2, 'F32': 4, 'F64': 8,
                     'I64': 8, 'I32': 4, 'I16': 2, 'I8': 1, 'U8': 1, 'BOOL': 1}.get(view.get_dtype())
            if width is None:
                raise VectorInstallError('embedding_source_dtype_invalid')
            metadata_bytes += prod(view.get_shape()) * width
        if metadata_bytes != expected_bytes:
            raise VectorInstallError('embedding_text_parameters_invalid')
        for name in names:
            tensors[name] = stream.get_tensor(name)
        source_tensor_count = len(stream.keys())
    tensor_bytes = sum(tensor.numel() * tensor.element_size() for tensor in tensors.values())
    if len(tensors) != expected_count or tensor_bytes != expected_bytes:
        raise VectorInstallError('embedding_text_parameters_invalid')
    for name in ('model.safetensors', *FILES):
        path = _safe_path(source / name)
        expected = info.get('files', {}).get(name)
        if not path.is_file():
            raise VectorInstallError('embedding_source_file_missing')
        if expected and (path.stat().st_size != expected['size']
                         or expected.get('sha256') and _digest(path) != expected['sha256']):
            raise VectorInstallError('embedding_source_checksum_failed')
    for name in FILES:
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / name, target)
    config.update(vision_config=None, audio_config=None)
    (destination / 'config.json').write_text(json.dumps(config, indent=2) + '\n', encoding='utf-8')
    save_file(tensors, str(destination / 'model.safetensors'), metadata={'format': 'pt'})
    del tensors
    manifest = {'schema_version': 1, 'model': model,
        'derivation': 'text-only@1', 'tensor_count': expected_count, 'tensor_bytes': tensor_bytes,
        'source': {**info, 'weight_size': weight.stat().st_size, 'tensor_count': source_tensor_count},
        'files': {name: {'size': (destination / name).stat().st_size,
                         'sha256': _digest(destination / name)} for name in (*FILES, 'model.safetensors')}}
    (destination / 'embedding-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    return manifest


def _download(source, progress, *, model):
    from huggingface_hub import HfApi, hf_hub_url
    info = HfApi(token=False).model_info(model, files_metadata=True)
    files = {item.rfilename: {'size': item.size,
            'sha256': item.lfs.sha256 if item.lfs else None} for item in info.siblings
             if item.rfilename in {*FILES, 'model.safetensors'}}
    if set(files) != {*FILES, 'model.safetensors'} or any(type(value['size']) is not int for value in files.values()):
        raise VectorInstallError('embedding_source_metadata_missing')
    total, done = sum(value['size'] for value in files.values()), 0
    for name, metadata in files.items():
        target = source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with urlopen(hf_hub_url(model, name, revision=info.sha), timeout=60) as response, target.open('xb') as stream:
            while block := response.read(1024 * 1024):
                stream.write(block)
                done += len(block)
                if done > total:
                    raise VectorInstallError('embedding_source_size_invalid')
                progress({'done': done, 'total': total})
        if target.stat().st_size != metadata['size']:
            raise VectorInstallError('embedding_source_size_invalid')
    return {'kind': 'huggingface', 'repository': model, 'revision': info.sha, 'files': files}


def install_embedding(models_root, *, model, source=None, progress=lambda value: None):
    if model != SUPPORTED_MODEL:
        raise VectorInstallError('embedding_model_unsupported')
    root = _safe_path(models_root)
    destination = model_directory(root)
    if installed_assets(destination, model=model):
        return json.loads((destination / 'embedding-manifest.json').read_text(encoding='utf-8'))
    if destination.exists():
        raise VectorInstallError('embedding_install_directory_incomplete')
    root.mkdir(parents=True, exist_ok=True)
    staging = root / ('.embedding-staging-' + uuid4().hex)
    staging.mkdir()
    try:
        derived = staging / 'text'
        derived.mkdir()
        if source is None:
            raw = staging / 'source'
            raw.mkdir()
            info = _download(raw, progress, model=model)
        else:
            raw, info = _safe_path(source), {'kind': 'local', 'weight_size': 1488915288, 'files': {}}
        manifest = derive_text_assets(raw, derived, model=model, source_info=info)
        # 只发布已经完整校验的文字目录，完整三模态来源始终留在私有临时目录。
        derived.rename(destination)
        return manifest
    finally:
        verified = _safe_path(staging)
        if verified.parent.resolve() != root.resolve() or not verified.name.startswith('.embedding-staging-'):
            raise VectorInstallError('embedding_cleanup_path_invalid')
        shutil.rmtree(verified)
