"""使用原检索投影、四键缓存和 MemoryTurn 补算本机文字向量。"""
from threading import Lock
from types import SimpleNamespace
from itertools import groupby

from backend.recognition import WorkScope, RecognitionConflict
from backend.recognition_retrieval import SQLiteEmbeddingCache
from backend.recognition_retrieval.cache_invalidation import chunk_cache_namespace, chunk_cache_id
from ..retrieval_models import configured_adapter, _public_identity
from ..source_egress import SourceEgressService
from .embedding_settings import vector_policy
from .memory_turn import embedding_request


class EmbeddingIndex:
    def __init__(self, query):
        self.query = query
        self.records, self.models = query.records, query.models
        self.lock = Lock()

    def _work(self, configured):
        authority = SourceEgressService(self.records)
        projects = {row.object_id for row in self.records.list('v2_projects')}
        projects.update(row.payload.get('scope', {}).get('project_id') for row in self.records.list('recognitions'))
        projects.update(row.payload.get('project_id') for row in self.records.list('documents'))
        for project in sorted(value for value in projects if isinstance(value, str) and value):
            scope = WorkScope('local-user', project)
            for entry in self.query.service.retrieval_entries(scope=scope):
                snapshot = authority.snapshot(scope, [{'type': 'recognition', 'id': entry['id'],
                                                        'revision': entry['revision']}])
                cache_entry = SimpleNamespace(project_id=project, id=entry['id'], revision=entry['revision'])
                yield project, cache_entry, entry['content'], snapshot, None
            entries, _, documents, _, _ = self.query.retrieval_index.indexed_entries(project)
            for entry in entries:
                if entry['kind'] != 'document' or entry['id'] not in documents:
                    continue
                snapshot = self.query.original_snapshot(scope, entry, authority)
                if snapshot is None:
                    continue
                projection = documents[entry['id']]
                namespace = chunk_cache_namespace(project, entry)
                for slot, span in enumerate(projection['spans']):
                    if span['layer'] == 'L2':
                        continue
                    for index, chunk in enumerate(span['chunks']):
                        identity = chunk_cache_id(entry, span['layer'], slot, index)
                        text = entry['title'] + '\n' + projection['summary'][:120] + '\n' + chunk['text']
                        cache_entry = SimpleNamespace(project_id=namespace, id=identity, revision=entry['revision'])
                        yield project, cache_entry, text, snapshot, entry

    def _progress(self, done, total, model_key, *, oversized=0, reason_code=None):
        with self.records.begin() as tx:
            row = tx.read('v2_embedding_index', 'default')
            saved = tx.put('v2_embedding_index', 'default', {'model_key': model_key,
                   'progress': {'done': done, 'total': total}, 'oversized': oversized,
                   'reason_code': reason_code or ('embedding_index_input_too_large' if oversized else None)},
                   expected_revision=row.revision if row else 0)
            tx.commit()
            return saved.revision

    def run(self):
        configured = self.models.public().get('embedding', {})
        if (configured.get('mode') != 'local' or not configured.get('configured')
                or not self.lock.acquire(blocking=False)):
            return 0
        cache = None
        done, total, oversized = 0, 0, 0
        model_key = configured.get('model_key')
        try:
            work = tuple(self._work(configured))
            total = len(work)
            cache = SQLiteEmbeddingCache(str(self.records.database_path.parent / 'recognition-vectors.sqlite3'))
            provider = configured_adapter(self.models, 'embedding')
            model_key = provider.cache_identity
            done = sum(cache.read(model_id=model_key, entry=item[1]) is not None for item in work)
            oversized = sum(len(item[2]) > vector_policy()['max_input_chars'] for item in work)
            # 一次实际本机后台执行使用已经提交的进度修订，不改缓存身份或旧回执。
            run_revision = self._progress(done, len(work), model_key, oversized=oversized)
            computed = 0
            pending = [item for item in work if len(item[2]) <= vector_policy()['max_input_chars']
                       and cache.read(model_id=model_key, entry=item[1]) is None]
            for project, items in groupby(pending, key=lambda item: item[0]):
                items = tuple(items)
                for start in range(0, len(items), vector_policy()['batch_size']):
                    batch = items[start:start + vector_policy()['batch_size']]
                    self._encode_batch(cache, provider, configured, project, batch, run_revision)
                    computed += len(batch)
                    done += len(batch)
                    self._progress(done, len(work), model_key, oversized=oversized)
            return computed
        except Exception:
            # 中断不补成功数；下次按真实四键缓存接着补算。
            self._progress(done, total, model_key, oversized=oversized,
                reason_code='embedding_index_interrupted')
            raise
        finally:
            if cache is not None:
                cache.close()
            self.lock.release()

    def _encode_batch(self, cache, provider, configured, project, batch, run_revision):
        authority = SourceEgressService(self.records)
        def validate():
            if _public_identity(self.models.public().get('embedding', {})) != _public_identity(configured):
                raise RecognitionConflict('embedding_index_configuration_changed')
            scope = WorkScope('local-user', project)
            for _, _, _, snapshot, parent in batch:
                authority.validate_snapshot(scope, snapshot)
                if parent is not None:
                    fresh = self.query.retrieval_index.vector_material(project, parent, 'L1')
                    if fresh is None or fresh[0]['revision'] != parent['revision']:
                        raise RecognitionConflict('embedding_index_parent_changed')
        validate()
        # 批次资格也在真实编码请求两侧校验，使变更进入原失败回执。
        provider = configured_adapter(self.models, 'embedding', validate_current=validate)
        materials = {}
        for _, _, _, snapshot, parent in batch:
            roots = [{**root, 'project_id': project} for root in snapshot['roots']]
            if parent is not None:
                roots.insert(0, {'type': 'document', 'id': parent['id'],
                                'revision': parent['revision'], 'project_id': project})
            for root in roots:
                materials[(root['type'], root['id'], root['revision'], root['project_id'])] = root
        request = {'endpoint': provider.endpoint, 'payload': {
            'model': provider.model, 'input': [item[2] for item in batch], 'input_type': 'document'}}
        response = embedding_request(self.records, self.models, project, list(materials.values()),
            {'namespace': 'local-index-v1', 'index_run_revision': run_revision, 'entries': [
                {'id': item[1].id, 'revision': item[1].revision, 'project': item[1].project_id} for item in batch],
             'model_key': provider.cache_identity}, validate, provider.client, request)
        if len(response.get('data', ())) != len(batch):
            raise RecognitionConflict('embedding_index_response_invalid')
        for index, item in enumerate(batch):
            validate()
            cache.write(model_id=provider.cache_identity, entry=item[1], vector=response['data'][index]['embedding'])
