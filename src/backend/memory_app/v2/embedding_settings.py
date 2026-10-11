"""向量模式和安装进度放在独立旁路，不修改原模型配置。"""
from pathlib import Path
from contextvars import copy_context
from threading import RLock, Thread
from uuid import uuid4

from ..local_vectors import dependencies_available, loaded_dependencies_available, model_directory, ready_or_warm, warm, runtime_status, load, unload, LocalVectorError
from ..local_vector_assets import installed_assets, install_embedding, VectorInstallError, DOWNLOAD_BYTES
from .policies import get, version

_SESSION = uuid4().hex
_INSTALL_LOCK = RLock()


def vector_policy():
    selected = version('vector')
    policy = dict(get('vector', version=selected)())
    policy['model_key'] = f"{policy['model']}:{policy['dims']}:vector{selected}"
    return policy


def server_embedding_profile(model):
    """线协议与缓存使用同一配置身份，服务端拒绝版本错配。"""
    policy = vector_policy()
    return f"{model}:selfhosted-roles@1:{policy['dims']}:vector{version('vector')}"


def local_vectors_ready(models):
    """只有用户明确加载完成才可计算向量。"""
    root = getattr(models, '_local_models_root', None)
    return True if root is None else ready_or_warm(model_directory(root), policy=vector_policy())


def warm_local_vectors(models):
    """兼容旧调用方，只检查状态，不再自动加载。"""
    root = getattr(models, '_local_models_root', None)
    if root is not None:
        warm(model_directory(root), policy=vector_policy())


class EmbeddingSettingsError(ValueError):
    pass


class EmbeddingSettings:
    def __init__(self, records, models_root, *, shared=False, device='cpu', shared_on_ready=None):
        if device not in {'cpu', 'cuda', 'auto'}:
            raise EmbeddingSettingsError('local_vector_device_invalid')
        self.records, self.models_root = records, Path(models_root)
        self.shared, self.device = shared, device
        self.shared_on_ready = shared_on_ready
        self.thread = None
        self._lifecycle_lock, self._closed = RLock(), False

    @property
    def closed(self):
        with self._lifecycle_lock:
            return self._closed

    def begin_close(self):
        with self._lifecycle_lock:
            self._closed = True

    def _notify_ready(self, callback):
        with self._lifecycle_lock:
            if not self._closed and callback is not None:
                callback()

    def _require_open(self):
        if self._closed:
            raise EmbeddingSettingsError('embedding_settings_closed')

    def mode(self, remote):
        row = self.records.read('v2_embedding_mode', 'default')
        return {'mode': row.payload['mode'] if row else
                'remote' if remote.get('configured') and remote.get('enabled') else 'local',
                'revision': row.revision if row else 0}

    def local(self):
        policy = vector_policy()
        install = self.records.read('v2_embedding_install', 'default')
        index = self.records.read('v2_embedding_index', 'default')
        installed = self.installed()
        directory = model_directory(self.models_root)
        before = runtime_status(directory)
        dependencies = loaded_dependencies_available(directory, policy=policy) if installed else False
        runtime = runtime_status(directory)
        if runtime != before:
            # 卸载或重载发生在探测期间时，不把旧编码器的加载事实交给新实例。
            dependencies = dependencies_available() if installed else False
            runtime = runtime_status(directory)
        status = 'ready' if installed and dependencies else 'missing'
        progress = None
        reason = 'local_vector_dependencies_missing' if installed and not dependencies else None
        if install and install.payload.get('status') in {'installing', 'failed'}:
            status = install.payload['status']
            progress = install.payload.get('progress')
            reason = install.payload.get('reason_code')
            if status == 'installing' and install.payload.get('session') != _SESSION:
                status, reason = 'failed', 'embedding_install_interrupted'
        return {'model': policy['model'], 'dims': policy['dims'], 'status': status,
                'progress': progress, 'index': index.payload.get('progress') if index else None,
                'index_reason_code': index.payload.get('reason_code') if index else None,
                'reason_code': reason, 'download_bytes': DOWNLOAD_BYTES,
                **runtime}

    def installed(self):
        return installed_assets(model_directory(self.models_root), model=vector_policy()['model'])

    def project(self, remote):
        selected, local = self.mode(remote), self.local()
        result = {**remote, 'mode': selected['mode'], 'mode_revision': selected['revision'], 'local': local}
        if selected['mode'] == 'server':
            result['model_key'] = server_embedding_profile(remote.get('model', ''))
        if selected['mode'] == 'local':
            policy = vector_policy()
            result.update(provider='local', model=policy['model'],
                base_url='http://127.0.0.1:8001/local-model/v1',
                configured=local['status'] == 'ready', enabled=local['runtime_status'] == 'loaded', allow_remote=False,
                runtime_revision=local['runtime_revision'],
                has_api_key=False, model_key=policy['model_key'])
        return result

    def update_mode(self, *, mode, expected_revision):
        if (not isinstance(mode, str) or mode not in {'local', 'server', 'remote'}
                or type(expected_revision) is not int or expected_revision < 0):
            raise EmbeddingSettingsError('embedding_mode_invalid')
        with self.records.begin() as tx:
            tx.put('v2_embedding_mode', 'default', {'mode': mode}, expected_revision=expected_revision)
            tx.commit()

    def install(self, selected, *, expected_revision, on_ready=None):
        with self._lifecycle_lock:
            self._require_open()
            return self._begin_install(selected, expected_revision=expected_revision, on_ready=on_ready)

    def _begin_install(self, selected, *, expected_revision, on_ready=None):
        if type(expected_revision) is not int or expected_revision < 0:
            raise EmbeddingSettingsError('embedding_install_invalid')
        policy = vector_policy()
        with _INSTALL_LOCK, self.records.begin() as tx:
            mode = tx.read('v2_embedding_mode', 'default')
            if (mode.revision if mode else 0) != expected_revision:
                raise EmbeddingSettingsError('embedding_mode_revision_conflict')
            if (mode.payload['mode'] if mode else selected.get('mode')) != 'local':
                raise EmbeddingSettingsError('embedding_install_requires_local')
            current = tx.read('v2_embedding_install', 'default')
            if (current and current.payload.get('status') == 'installing'
                    and current.payload.get('session') == _SESSION):
                return {'job_id': current.payload['job_id']}
            job_id = uuid4().hex
            tx.put('v2_embedding_install', 'default', {'status': 'installing',
                'progress': {'done': 0, 'total': DOWNLOAD_BYTES}, 'job_id': job_id, 'session': _SESSION},
                expected_revision=current.revision if current else 0)
            tx.commit()
        # 原调用人的上下文随安装任务保留，旁路写入沿用既有归属记录。
        context = copy_context()
        self.thread = Thread(target=context.run, args=(self._install, job_id, on_ready, dict(policy)), name='local-vector-install', daemon=True)
        self.thread.start()
        return {'job_id': job_id}

    def change_runtime(self, selected, *, action, expected_revision, on_ready=None):
        with self._lifecycle_lock:
            self._require_open()
            return self._change_runtime(selected, action=action, expected_revision=expected_revision, on_ready=on_ready)

    def _change_runtime(self, selected, *, action, expected_revision, on_ready=None):
        if type(expected_revision) is not str or action not in {'load', 'unload'}:
            raise EmbeddingSettingsError('embedding_runtime_invalid')
        directory = model_directory(self.models_root)
        if runtime_status(directory)['runtime_revision'] != expected_revision:
            raise EmbeddingSettingsError('embedding_runtime_revision_conflict')
        if action == 'load' and (selected.get('mode') != 'local' or not self.installed()):
            raise EmbeddingSettingsError('local_vector_not_installed')
        try:
            if action == 'load':
                context = copy_context()
                ready = (self.shared_on_ready if self.shared else
                         (lambda: context.run(self._notify_ready, on_ready)) if on_ready else None)
                return load(directory, policy=vector_policy(), expected_revision=expected_revision,
                    device=self.device, on_ready=ready)
            return unload(directory, expected_revision=expected_revision)
        except LocalVectorError as error:
            raise EmbeddingSettingsError(str(error)) from None

    def close(self):
        self.begin_close()
        if self.thread is not None:
            self.thread.join()
        if not self.shared:
            unload(model_directory(self.models_root), wait=True)

    def _update_install(self, job_id, **changes):
        with _INSTALL_LOCK, self.records.begin() as tx:
            row = tx.read('v2_embedding_install', 'default')
            if row is None or row.payload.get('job_id') != job_id or row.payload.get('session') != _SESSION:
                raise EmbeddingSettingsError('embedding_install_owner_changed')
            tx.put('v2_embedding_install', 'default', {**row.payload, **changes}, expected_revision=row.revision)
            tx.commit()

    def _install(self, job_id, on_ready, policy):
        try:
            install_embedding(self.models_root, model=policy['model'],
                progress=lambda value: self._update_install(job_id, progress=value))
        except Exception as error:
            code = str(error) if isinstance(error, VectorInstallError) else 'embedding_install_failed'
            self._update_install(job_id, status='failed', reason_code=code)
        else:
            self._update_install(job_id, status='ready', progress=None, reason_code=None)
            if on_ready is not None and vector_policy() == policy:
                self._notify_ready(on_ready)
