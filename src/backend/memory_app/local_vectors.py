"""本机文字编码共用一个工作线程，查询优先于后台资料批次。"""
from collections import deque
from concurrent.futures import Future
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, version as package_version
import math
import os
from pathlib import Path
from threading import Condition, RLock, Thread


SUPPORTED_MODEL = 'google/embeddinggemma-2'


class LocalVectorError(ValueError):
    pass


def model_directory(models_root):
    return Path(models_root) / 'google-embeddinggemma-2'


def dependencies_available():
    try:
        minimums = {'sentence-transformers': (6, 1), 'transformers': (5, 19),
                    'torch': (2, 7), 'safetensors': (0, 7), 'torchvision': (0, 22),
                    'Pillow': (10, 0), 'librosa': (0, 11)}
        for name, minimum in minimums.items():
            installed = tuple(int(part) for part in package_version(name).split('.')[:2])
            if installed < minimum:
                return False
    except (PackageNotFoundError, ValueError):
        return False
    return True


def validate_request(body, *, policy):
    if (not isinstance(body, dict) or not isinstance(body.get('model'), str)
            or body['model'] not in {policy['model'], policy.get('model_key', policy['model'])}):
        raise LocalVectorError('invalid_local_vector_request')
    texts = body.get('input')
    texts = [texts] if isinstance(texts, str) else texts
    input_type = body.get('input_type', 'document')
    if (not isinstance(texts, list) or not texts or not isinstance(input_type, str)
            or input_type not in {'query', 'document'}
            or any(not isinstance(text, str) or not text.strip() for text in texts)):
        raise LocalVectorError('invalid_local_vector_request')
    if len(texts) > policy['max_items'] or any(len(text) > policy['max_input_chars'] for text in texts):
        raise LocalVectorError('local_vector_input_too_large')
    return tuple(texts), input_type


class SentenceEncoder:
    def __init__(self, directory):
        import torch
        from sentence_transformers import SentenceTransformer
        torch.set_num_threads(min(8, os.cpu_count() or 4))
        self.model = SentenceTransformer(str(directory), device='cpu', local_files_only=True,
            config_kwargs={'vision_config': None, 'audio_config': None},
            model_kwargs={'dtype': torch.float32})

    def encode(self, texts, input_type, *, policy):
        if input_type == 'query':
            inputs = [policy['query_prefix'] + text for text in texts]
        else:
            inputs = []
            for text in texts:
                title, separator, content = text.partition('\n')
                inputs.append(policy['document_prefix'].format(
                    title=title if separator else 'none', content=content if separator else text))
        vectors = self.model.encode(inputs, batch_size=policy['batch_size'],
            truncate_dim=policy['dims'], normalize_embeddings=True, show_progress_bar=False)
        tokens = sum(len(self.model.tokenizer.encode(text)) for text in inputs)
        return [[float(value) for value in vector] for vector in vectors], tokens


@dataclass
class _Request:
    texts: tuple
    input_type: str
    policy: dict
    future: Future = field(default_factory=Future)
    vectors: list = field(default_factory=list)
    tokens: int = 0
    accounted: bool = True


class VectorWorker:
    def __init__(self, directory, *, policy, loader=None, queue_limit=None):
        self.directory, self.loader = Path(directory), loader or SentenceEncoder
        self.policy = dict(policy)
        # 本版本固定资产家族，不能把已有文字权重标成另一模型。
        if self.policy['model'] != SUPPORTED_MODEL:
            raise LocalVectorError('local_vector_configuration_invalid')
        self.limit = self.policy['queue_limit'] if queue_limit is None else queue_limit
        self.condition = Condition()
        self.queries, self.documents = deque(), deque()
        self.thread, self.encoder = None, None
        self.closed, self.pending = False, 0

    def submit(self, texts, input_type='document', *, policy=None):
        # 每次提交冻结选择；工作线程不依赖调用线程的策略上下文。
        selected = dict(self.policy if policy is None else policy)
        if selected['model'] != self.policy['model']:
            raise LocalVectorError('local_vector_configuration_invalid')
        texts, input_type = validate_request({'model': selected['model'],
            'input': list(texts), 'input_type': input_type}, policy=selected)
        request = _Request(texts, input_type, selected)
        with self.condition:
            if self.closed:
                raise LocalVectorError('local_vector_worker_closed')
            if self.pending >= self.limit:
                raise LocalVectorError('local_vector_queue_full')
            self.pending += 1
            (self.queries if input_type == 'query' else self.documents).append(request)
            if self.thread is None:
                self.thread = Thread(target=self._run, name='local-text-vectors', daemon=True)
                self.thread.start()
            self.condition.notify()
        return request.future

    def _run(self):
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.closed or self.queries or self.documents)
                if self.closed:
                    return
                request = (self.queries or self.documents).popleft()
            try:
                if self.encoder is None:
                    self.encoder = self.loader(self.directory)
                start = len(request.vectors)
                batch = request.texts[start:start + request.policy['batch_size']]
                vectors, tokens = self.encoder.encode(batch, request.input_type, policy=request.policy)
                if (len(vectors) != len(batch) or type(tokens) is not int or tokens < 0
                        or any(len(vector) != request.policy['dims']
                            or any(not math.isfinite(value) for value in vector) for vector in vectors)):
                    raise LocalVectorError('local_vector_invalid_output')
                request.vectors.extend(vectors)
                request.tokens += tokens
                with self.condition:
                    if len(request.vectors) < len(request.texts) and not self.closed:
                        (self.queries if request.input_type == 'query' else self.documents).appendleft(request)
                        continue
                with self.condition:
                    if self.closed:
                        raise LocalVectorError('local_vector_worker_closed')
                    response = {'object': 'list', 'model': request.policy['model'],
                        'data': [{'object': 'embedding', 'index': index, 'embedding': vector}
                                 for index, vector in enumerate(request.vectors)],
                        'usage': {'prompt_tokens': request.tokens, 'total_tokens': request.tokens}}
                    self._finish(request, result=response)
            except Exception as error:
                with self.condition:
                    self._finish(request, error=error)

    def _finish(self, request, *, result=None, error=None):
        if request.accounted:
            self.pending -= 1
            request.accounted = False
        if not request.future.done():
            if error is not None:
                request.future.set_exception(error)
            else:
                request.future.set_result(result)

    def close(self):
        with self.condition:
            self.closed = True
            for request in (*self.queries, *self.documents):
                self._finish(request, error=LocalVectorError('local_vector_worker_closed'))
            self.queries.clear()
            self.documents.clear()
            self.condition.notify_all()
        if self.thread is not None:
            self.thread.join()
        self.encoder = None


_LOCK = RLock()
_WORKERS = {}


def worker_for(directory, *, policy):
    identity = str(Path(directory).resolve())
    with _LOCK:
        if identity not in _WORKERS or _WORKERS[identity].closed:
            _WORKERS[identity] = VectorWorker(directory, policy=policy)
        return _WORKERS[identity]


def encode_request(directory, body, *, policy):
    texts, input_type = validate_request(body, policy=policy)
    if not (Path(directory) / 'model.safetensors').is_file():
        raise LocalVectorError('local_vector_not_installed')
    if not dependencies_available():
        raise LocalVectorError('local_vector_dependencies_missing')
    return worker_for(directory, policy=policy).submit(texts, input_type, policy=policy).result()
