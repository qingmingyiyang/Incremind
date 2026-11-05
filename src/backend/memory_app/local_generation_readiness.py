from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass
from importlib.util import find_spec
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
from threading import RLock


MODEL_NAME = 'qwen2.5-1.5b-instruct'
_DEPENDENCIES = ('torch', 'transformers', 'tokenizers', 'safetensors', 'jinja2')
_LOCK = RLock()
_ASSETS = OrderedDict()
_RUNTIME = {}


class LocalGenerationError(ValueError):
    pass


@dataclass(frozen=True)
class LocalGenerationReadiness:
    identity: tuple
    value: dict

    def public(self):
        return deepcopy(self.value)

    @property
    def installed(self):
        return self.value['assets_ready']

    @property
    def available(self):
        return self.value['eligible'] and self.value['status'] not in {
            'load_failed', 'inference_failed', 'restart_required'}

    def require_selection(self):
        if not self.value['eligible'] or self.value['status'] == 'restart_required':
            raise LocalGenerationError(self.value['reason_code'])

    def require_available(self):
        self.require_selection()
        if not self.available:
            raise LocalGenerationError(self.value['reason_code'])


def _asset(directory, name):
    if (not isinstance(name, str) or not name or PureWindowsPath(name).drive
            or PurePosixPath(name).is_absolute() or '..' in PurePosixPath(name).parts
            or '\\' in name):
        raise LocalGenerationError('local_model_assets_invalid')
    path = (directory / name).resolve()
    if not path.is_relative_to(directory):
        raise LocalGenerationError('local_model_assets_invalid')
    return path


def _json(directory, name, *, limit=1_048_576):
    path = _asset(directory, name)
    if path.stat().st_size > limit:
        raise LocalGenerationError('local_model_assets_invalid')
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise LocalGenerationError('local_model_assets_invalid')
    return value


def _tokenizer_assets(directory):
    selected = 'tokenizer.json'
    try:
        config_path = _asset(directory, 'tokenizer_config.json')
        config = _json(directory, 'tokenizer_config.json') if config_path.is_file() else {}
        if config.get('tokenizer_class') not in (None, 'Qwen2Tokenizer', 'Qwen2TokenizerFast'):
            raise LocalGenerationError('local_model_tokenizer_invalid')
        versions = config.get('fast_tokenizer_files', [])
        if not isinstance(versions, list) or len(versions) > 256:
            raise LocalGenerationError('local_model_tokenizer_invalid')
        for name in versions:
            _asset(directory, name)
    except (OSError, ValueError, TypeError) as error:
        raise LocalGenerationError('local_model_tokenizer_invalid') from error
    if versions:
        if find_spec('transformers') is None:
            # 依赖缺失时只收集目录内现有文件，资格由真实依赖检查拒绝。
            candidates = dict.fromkeys([*versions, selected, 'vocab.json', 'merges.txt'])
            return [name for name in candidates if _asset(directory, name).is_file()], None
        from transformers.tokenization_utils_base import get_fast_tokenizer_file
        try:
            selected = get_fast_tokenizer_file(versions)
        except (ValueError, TypeError) as error:
            raise LocalGenerationError('local_model_tokenizer_invalid') from error
    try:
        present = _asset(directory, selected).is_file()
    except LocalGenerationError as error:
        raise LocalGenerationError('local_model_tokenizer_invalid') from error
    if not present:
        return ['vocab.json', 'merges.txt'], None
    return [selected, *[name for name in ('vocab.json', 'merges.txt')
        if (directory / name).is_file()]], selected


def _layout(directory):
    files = ['config.json', 'tokenizer_config.json']
    for name in ('chat_template.jinja', 'added_tokens.json', 'special_tokens_map.json',
                 'generation_config.json', 'adapter_config.json'):
        if (directory / name).is_file():
            files.append(name)
    files.extend(sorted(path.relative_to(directory).as_posix()
        for path in (directory / 'chat_templates').glob('*.jinja') if path.is_file()))
    tokenizer_files, _ = _tokenizer_assets(directory)
    files.extend(tokenizer_files)
    if (directory / 'model.safetensors').is_file():
        files.append('model.safetensors')
        return files, None
    index_name = 'model.safetensors.index.json'
    files.append(index_name)
    if not (directory / index_name).is_file():
        return files, None
    index = _json(directory, index_name, limit=16_777_216)
    mapping = index.get('weight_map')
    if (not isinstance(mapping, dict) or not mapping
            or any(not isinstance(key, str) or not key or not isinstance(name, str)
                   or not name.endswith('.safetensors') for key, name in mapping.items())):
        raise LocalGenerationError('local_model_assets_invalid')
    shards = sorted(set(mapping.values()))
    if len(shards) > 256:
        raise LocalGenerationError('local_model_assets_invalid')
    for name in shards:
        _asset(directory, name)
    return [*files, *shards], mapping


def _validate_assets(directory, files, mapping):
    from jinja2 import Environment, TemplateSyntaxError
    from safetensors import safe_open, SafetensorError
    from tokenizers import Tokenizer
    from tokenizers.models import BPE
    import torch
    from transformers import AutoConfig, AutoModelForCausalLM, AutoTokenizer

    config = _json(directory, 'config.json')
    tokenizer = _json(directory, 'tokenizer_config.json')
    # 固定资源名对应官方 1.5B 架构，完整性检查使用同一实际配置。
    dimensions = {'hidden_size': 1536, 'num_hidden_layers': 28, 'num_attention_heads': 12,
        'num_key_value_heads': 2, 'intermediate_size': 8960, 'vocab_size': 151936}
    if ('adapter_config.json' in files or config.get('quantization_config') is not None
            or config.get('model_type') != 'qwen2' or config.get('architectures') != ['Qwen2ForCausalLM']
            or any(type(config.get(key)) is not int or config[key] != value
                   for key, value in dimensions.items()) or config.get('tie_word_embeddings') is not True):
        raise LocalGenerationError('local_model_config_invalid')
    template = tokenizer.get('chat_template')
    if 'chat_template.jinja' in files:
        path = _asset(directory, 'chat_template.jinja')
        if path.stat().st_size > 1_048_576:
            raise LocalGenerationError('local_model_tokenizer_invalid')
        template = path.read_text(encoding='utf-8')
    if isinstance(template, dict):
        template = template.get('default')
    elif isinstance(template, list):
        choices = [item.get('template') for item in template
            if isinstance(item, dict) and item.get('name') == 'default']
        template = choices[0] if len(choices) == 1 else None
    if not isinstance(template, str) or not template.strip():
        raise LocalGenerationError('local_model_tokenizer_invalid')
    try:
        Environment().parse(template)
    except TemplateSyntaxError as error:
        raise LocalGenerationError('local_model_tokenizer_invalid') from error
    try:
        _, tokenizer_file = _tokenizer_assets(directory)
        if tokenizer_file is not None:
            backend = Tokenizer.from_file(str(_asset(directory, tokenizer_file)))
        else:
            backend = Tokenizer(BPE.from_file(str(_asset(directory, 'vocab.json')),
                str(_asset(directory, 'merges.txt'))))
    except Exception as error:
        raise LocalGenerationError('local_model_tokenizer_invalid') from error
    if not 0 < backend.get_vocab_size() <= config['vocab_size']:
        raise LocalGenerationError('local_model_tokenizer_invalid')
    try:
        actual_tokenizer = AutoTokenizer.from_pretrained(directory, local_files_only=True, trust_remote_code=False)
        actual_tokenizer.apply_chat_template([{'role': 'user', 'content': 'readiness'}],
            tokenize=False, add_generation_prompt=True)
    except Exception as error:
        raise LocalGenerationError('local_model_tokenizer_invalid') from error
    tensors = {}
    for name in files:
        if not name.endswith('.safetensors'):
            continue
        try:
            with safe_open(str(_asset(directory, name)), framework='np') as archive:
                keys = set(archive.keys())
                if not keys or set(tensors).intersection(keys):
                    raise LocalGenerationError('local_model_weights_invalid')
                if mapping is not None and keys != {key for key, shard in mapping.items() if shard == name}:
                    raise LocalGenerationError('local_model_weights_invalid')
                tensors.update({key: tuple(archive.get_slice(key).get_shape()) for key in keys})
        except SafetensorError as error:
            raise LocalGenerationError('local_model_weights_invalid') from error
    if mapping is not None and set(tensors) != set(mapping):
        raise LocalGenerationError('local_model_weights_invalid')
    # meta 设备仅建立实际架构的参数形状，不分配或加载完整权重。
    try:
        configured = AutoConfig.from_pretrained(directory, local_files_only=True, trust_remote_code=False)
        with torch.device('meta'):
            skeleton = AutoModelForCausalLM.from_config(configured, trust_remote_code=False)
        skeleton.tie_weights()
    except (KeyError, AttributeError) as error:
        raise LocalGenerationError('local_model_config_invalid') from error
    expected = skeleton.state_dict(keep_vars=True)
    aliases = {}
    for name, value in expected.items():
        aliases.setdefault(id(value), set()).add(name)
    if (set(tensors) - set(expected)
            or any(tuple(expected[name].shape) != shape for name, shape in tensors.items())):
        raise LocalGenerationError('local_model_weights_invalid')
    if any(not names.intersection(tensors) for names in aliases.values()):
        raise LocalGenerationError('local_model_weights_incomplete')


def inspect_local_generation(directory):
    directory = Path(directory).resolve()
    missing, invalid, signature = [], [], []
    reason, asset_error = None, None
    try:
        files, mapping = _layout(directory)
    except LocalGenerationError as error:
        files, mapping = ['config.json', 'tokenizer_config.json'], None
        invalid.append('tokenizer_config.json' if str(error) == 'local_model_tokenizer_invalid'
            else 'model.safetensors.index.json')
        reason = str(error)
    except ImportError:
        files, mapping = ['config.json', 'tokenizer_config.json'], None
        asset_error = 'local_model_runtime_unavailable'
        reason = asset_error
    except (OSError, ValueError, TypeError):
        files, mapping = ['config.json', 'tokenizer_config.json', 'model.safetensors.index.json'], None
        invalid.append('model.safetensors.index.json')
        reason = 'local_model_assets_invalid'
    for name in files:
        try:
            path = _asset(directory, name)
            stat = path.stat()
            if not path.is_file() or stat.st_size == 0:
                invalid.append(name)
            signature.append((name, stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns))
        except FileNotFoundError:
            missing.append(name)
        except (OSError, LocalGenerationError):
            invalid.append(name)
    dependencies = [name for name in _DEPENDENCIES if find_spec(name) is None]
    identity = (str(directory), tuple(signature))
    cache_key = (identity, tuple(dependencies))
    parser_missing = bool(dependencies)
    if not missing and not invalid and not parser_missing and asset_error is None:
        with _LOCK:
            cached = _ASSETS.get(cache_key)
        if cached is None:
            try:
                if _layout(directory) != (files, mapping):
                    raise LocalGenerationError('local_model_assets_changed')
                _validate_assets(directory, files, mapping)
                after = []
                for name in files:
                    stat = _asset(directory, name).stat()
                    after.append((name, stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns))
                if after != signature:
                    raise LocalGenerationError('local_model_assets_changed')
                cached = (True, None)
            except ImportError:
                cached = (False, 'local_model_runtime_unavailable')
            except (OSError, ValueError, TypeError, RuntimeError) as error:
                cached = (False, str(error) if isinstance(error, LocalGenerationError)
                    else 'local_model_assets_invalid')
            with _LOCK:
                _ASSETS[cache_key] = cached
                _ASSETS.move_to_end(cache_key)
                while len(_ASSETS) > 32:
                    _ASSETS.popitem(last=False)
        if not cached[0]:
            asset_error = cached[1]
            if asset_error != 'local_model_runtime_unavailable':
                invalid.extend(files)
            reason = cached[1]
    assets_ready = not missing and not invalid and not parser_missing and asset_error is None
    eligible = assets_ready and not dependencies
    status = 'available'
    if missing:
        status, reason = 'missing_assets', 'local_model_assets_missing'
    elif invalid:
        status, reason = 'invalid_assets', reason or 'local_model_assets_invalid'
    elif dependencies:
        status, reason = 'missing_dependencies', 'local_model_dependencies_missing'
    elif asset_error is not None:
        status, reason = 'runtime_unavailable', asset_error
    with _LOCK:
        runtime = _RUNTIME.get(identity)
        if eligible and any(key[0] == identity[0] and key != identity and value.get('loaded')
                for key, value in _RUNTIME.items()):
            status, reason = 'restart_required', 'local_model_assets_changed_restart_required'
        elif eligible and runtime is not None:
            status, reason = runtime['status'], runtime.get('reason_code')
    return LocalGenerationReadiness(identity, {'model': MODEL_NAME, 'status': status,
        'assets_ready': assets_ready, 'eligible': eligible, 'missing_files': sorted(set(missing)),
        'invalid_files': sorted(set(invalid)), 'missing_dependencies': dependencies, 'reason_code': reason})


def require_current(directory, identity):
    current = inspect_local_generation(directory)
    current.require_available()
    if current.identity != identity:
        raise LocalGenerationError('local_model_assets_changed')
    return current


def mark_runtime(identity, status, *, reason_code=None, loaded=False):
    with _LOCK:
        _RUNTIME[identity] = {'status': status, 'reason_code': reason_code, 'loaded': loaded}


def retry_loading(current):
    current.require_selection()
    with _LOCK:
        previous = _RUNTIME.get(current.identity)
        if previous is not None and previous['status'] in {'load_failed', 'inference_failed'}:
            _RUNTIME[current.identity] = {'status': 'loaded' if previous['loaded'] else 'available',
                'reason_code': None, 'loaded': previous['loaded']}
