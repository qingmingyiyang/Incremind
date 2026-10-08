"""向量模式和安装进度放在独立旁路，不修改原模型配置。"""
from pathlib import Path
from contextvars import copy_context
from threading import RLock, Thread
from uuid import uuid4

from ..local_vectors import dependencies_available, model_directory
from ..local_vector_assets import installed_assets, install_embedding, VectorInstallError, DOWNLOAD_BYTES
from .policies import get, version

_SESSION = uuid4().hex
_INSTALL_LOCK = RLock()


def vector_policy():
    selected = version('vector')
    policy = dict(get('vector', version=selected)())
    policy['model_key'] = f"{policy['model']}:{policy['dims']}:vector{selected}"
    return policy


class EmbeddingSettingsError(ValueError):
    pass


class EmbeddingSettings:
    def __init__(self, records, models_root):
        self.records, self.models_root = records, Path(models_root)
        self.thread = None

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
        dependencies = dependencies_available() if installed else False
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
                'reason_code': reason, 'download_bytes': DOWNLOAD_BYTES}

    def installed(self):
        return installed_assets(model_directory(self.models_root), model=vector_policy()['model'])

    def project(self, remote):
        selected, local = self.mode(remote), self.local()
        result = {**remote, 'mode': selected['mode'], 'mode_revision': selected['revision'], 'local': local}
        if selected['mode'] == 'local':
            policy = vector_policy()
            result.update(provider='local', model=policy['model'],
                base_url='http://127.0.0.1:8001/local-model/v1',
                configured=local['status'] == 'ready', enabled=True, allow_remote=False,
                has_api_key=False, model_key=policy['model_key'])
        return result

    def update_mode(self, *, mode, expected_revision):
        if (not isinstance(mode, str) or mode not in {'local', 'remote'}
                or type(expected_revision) is not int or expected_revision < 0):
            raise EmbeddingSettingsError('embedding_mode_invalid')
        with self.records.begin() as tx:
            tx.put('v2_embedding_mode', 'default', {'mode': mode}, expected_revision=expected_revision)
            tx.commit()

    def install(self, selected, *, expected_revision, on_ready=None):
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

    def close(self):
        if self.thread is not None:
            self.thread.join()

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
                on_ready()
