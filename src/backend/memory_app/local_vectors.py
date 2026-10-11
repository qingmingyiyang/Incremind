"""本机文字编码共用一个工作线程，查询优先于后台资料批次。"""
from collections import deque
from concurrent.futures import Future
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, version as package_version
import gc
import logging
import math
import os
import sys
from pathlib import Path
from threading import Condition, RLock, Thread
from uuid import uuid4


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
    def __init__(self, directory, *, device='cpu'):
        import torch
        from sentence_transformers import SentenceTransformer
        if device not in {'cpu', 'cuda', 'auto'}:
            raise LocalVectorError('local_vector_device_invalid')
        if device == 'cuda' and not torch.cuda.is_available():
            raise LocalVectorError('local_vector_cuda_unavailable')
        selected = 'cuda' if device != 'cpu' and torch.cuda.is_available() else 'cpu'
        if selected == 'cpu':
            torch.set_num_threads(min(8, os.cpu_count() or 4))
        self.device = selected
        self.model = SentenceTransformer(str(directory), device=selected, local_files_only=True,
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

    def preload(self):
        """仅显式生命周期加载调用，不编码资料。"""
        request = _Request((), 'load', dict(self.policy))
        with self.condition:
            if self.closed:
                raise LocalVectorError('local_vector_worker_closed')
            if self.encoder is not None:
                return
            self.pending += 1
            self.queries.appendleft(request)
            if self.thread is None:
                self.thread = Thread(target=self._run, name='local-text-vectors', daemon=True)
                self.thread.start()
            self.condition.notify()
        request.future.result()

    def _run(self):
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.closed or self.queries or self.documents)
                if self.closed:
                    return
                request = (self.queries or self.documents).popleft()
            try:
                if self.encoder is None:
                    encoder = self.loader(self.directory)
                    with self.condition:
                        if self.closed:
                            del encoder
                            raise LocalVectorError('local_vector_worker_closed')
                        self.encoder = encoder
                    del encoder
                if request.input_type == 'load':
                    with self.condition:
                        if self.closed:
                            raise LocalVectorError('local_vector_worker_closed')
                        self._finish(request, result=None)
                    continue
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
                # Future 不保留模型调用栈，避免卸载后仍被异常链引用权重。
                code = (str(error) if isinstance(error, LocalVectorError) else
                        'local_vector_load_failed' if request.input_type == 'load' else 'local_vector_encode_failed')
                request.future.set_exception(LocalVectorError(code))
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
_OWNERS = {}
_SESSION = uuid4().hex


@dataclass
class _Owner:
    state: str = 'unloaded'
    generation: int = 0
    worker: object = None
    reason: str | None = None
    thread: object = None
    device: str | None = None
    requested_device: str | None = None


def _owner(directory):
    identity = os.path.normcase(str(Path(directory).resolve()))
    return _OWNERS.setdefault(identity, _Owner())


def runtime_status(directory):
    with _LOCK:
        owner = _owner(directory)
        return {'runtime_status': owner.state, 'runtime_reason_code': owner.reason,
                'runtime_revision': f'{_SESSION}:{owner.generation}', 'device': owner.device}


def loaded(directory):
    return runtime_status(directory)['runtime_status'] == 'loaded'


def loaded_dependencies_available(directory, *, policy):
    """当前目录与策略的编码器已加载成功时，其依赖已经实际可用。"""
    with _LOCK:
        owner = _owner(directory)
        if (owner.state == 'loaded' and owner.worker is not None
                and owner.worker.policy == policy):
            worker = owner.worker
            with worker.condition:
                return (not worker.closed and worker.encoder is not None
                        and worker.thread is not None and worker.thread.is_alive())
    # 卸载、重载前、换策略或新进程均重新探测，不永久缓存包版本。
    return dependencies_available()


def worker_for(directory, *, policy):
    # 查询和索引只能使用明确加载完成的 owner，不能偷偷重建线程。
    with _LOCK:
        owner = _owner(directory)
        if owner.state != 'loaded' or owner.worker is None:
            raise LocalVectorError('local_vector_not_loaded')
        return owner.worker


def load(directory, *, policy, on_ready=None, expected_revision=None, device='cpu'):
    if device not in {'cpu', 'cuda', 'auto'}:
        raise LocalVectorError('local_vector_device_invalid')
    with _LOCK:
        owner = _owner(directory)
        if expected_revision is not None and runtime_status(directory)['runtime_revision'] != expected_revision:
            raise LocalVectorError('embedding_runtime_revision_conflict')
        if owner.state in {'loading', 'loaded'}:
            if owner.requested_device != device and owner.device != device:
                raise LocalVectorError('local_vector_device_requires_unload')
            return runtime_status(directory)
        if owner.state == 'unloading':
            raise LocalVectorError('local_vector_unloading')
        if not (Path(directory) / 'model.safetensors').is_file():
            raise LocalVectorError('local_vector_not_installed')
        if not dependencies_available():
            raise LocalVectorError('local_vector_dependencies_missing')
        owner.generation += 1
        generation = owner.generation
        owner.state, owner.reason = 'loading', None
        owner.requested_device, owner.device = device, None
        worker = VectorWorker(directory, policy=policy,
            loader=lambda path: SentenceEncoder(path, device=device))
        owner.worker = worker

        def run():
            reason = None
            try:
                worker.preload()
            except Exception as error:
                reason = str(error) if isinstance(error, LocalVectorError) else 'local_vector_load_failed'
            if reason is not None:
                # 先离开 except 清掉异常栈，再关闭线程与回收权重引用。
                worker.close()
                _release_memory(device)
                with _LOCK:
                    if owner.generation == generation and owner.state == 'loading':
                        owner.worker = None
                        owner.state, owner.reason = 'failed', reason
                return
            with _LOCK:
                if owner.generation != generation or owner.state != 'loading':
                    return
                owner.device = getattr(worker.encoder, 'device', device)
                owner.state = 'loaded'
            if on_ready is not None:
                try:
                    on_ready()
                except Exception as error:
                    # 索引调度失败不改变已加载状态，也不记录路径或异常正文。
                    logging.getLogger(__name__).warning(
                        'local_vector_index_schedule_failed exception_type=%s', type(error).__name__)

        owner.thread = Thread(target=run, name='local-vector-load', daemon=True)
        owner.thread.start()
        return runtime_status(directory)


def _release_memory(device):
    gc.collect()
    torch = sys.modules.get('torch')
    if torch is not None and device in {'cuda', 'auto'} and torch.cuda.is_initialized():
        torch.cuda.empty_cache()


def unload(directory, *, wait=False, expected_revision=None):
    with _LOCK:
        owner = _owner(directory)
        if expected_revision is not None and runtime_status(directory)['runtime_revision'] != expected_revision:
            raise LocalVectorError('embedding_runtime_revision_conflict')
        if owner.state == 'unloaded':
            return runtime_status(directory)
        if owner.state == 'unloading':
            thread = owner.thread
        else:
            owner.generation += 1
            owner.state, owner.reason = 'unloading', None
            worker, previous = owner.worker, owner.thread

            def run():
                if worker is not None:
                    worker.close()
                # 旧 loader 也退出后才开放重载，避免同目录两个编码器共存。
                if previous is not None:
                    previous.join()
                _release_memory(owner.requested_device)
                with _LOCK:
                    owner.worker = None
                    owner.device, owner.requested_device = None, None
                    owner.state = 'unloaded'

            thread = Thread(target=run, name='local-vector-unload', daemon=True)
            owner.thread = thread
            thread.start()
    if wait:
        thread.join()
    return runtime_status(directory)


def encode_request(directory, body, *, policy):
    """整次请求先校验，再按角色进入同一个 owner 队列，复验代际后交付。"""
    texts, default_role = validate_request(body, policy=policy)
    roles = body.get('input_types')
    if roles is None:
        roles = [default_role] * len(texts)
    if (not isinstance(roles, list) or len(roles) != len(texts)
            or any(not isinstance(role, str) or role not in {'query', 'document'} for role in roles)
            or ('input_type' in body and any(role != default_role for role in roles))):
        raise LocalVectorError('invalid_local_vector_request')
    groups = [(role, [index for index, value in enumerate(roles) if value == role])
              for role in ('query', 'document') if role in roles]
    before = runtime_status(directory)
    worker = worker_for(directory, policy=policy)
    # 两个角色一起接纳，避免队列满时只接收半个混合请求。
    with worker.condition:
        if worker.closed:
            raise LocalVectorError('local_vector_worker_closed')
        if worker.pending + len(groups) > worker.limit:
            raise LocalVectorError('local_vector_queue_full')
        pending = [(indices, worker.submit([texts[index] for index in indices], role, policy=policy))
                   for role, indices in groups]
    data, tokens = [None] * len(texts), 0
    for indices, future in pending:
        result = future.result()
        tokens += result['usage']['prompt_tokens']
        for index, item in zip(indices, result['data']):
            data[index] = {**item, 'index': index}
    if runtime_status(directory) != before:
        raise LocalVectorError('local_vector_runtime_changed')
    return {'object': 'list', 'model': policy['model'], 'data': data,
            'usage': {'prompt_tokens': tokens, 'total_tokens': tokens}}


def warm(directory, *, policy):
    """兼容入口只报告可用性，预热不再隐式加载。"""
    return loaded(directory)


def ready_or_warm(directory, *, policy):
    """仅报告显式加载状态。"""
    return loaded(directory)
