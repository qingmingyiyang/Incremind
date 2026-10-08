"""Derived chunk vectors using the existing cache and governed embedding Turn."""
from dataclasses import replace
from types import SimpleNamespace
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from urllib.parse import urlsplit

from backend.recognition import RecognitionConflict
from backend.recognition_retrieval import SQLiteEmbeddingCache, retrieve
from backend.recognition_retrieval.cache_invalidation import chunk_cache_namespace, chunk_cache_id
from ..retrieval_models import configured_adapter, _public_identity
from ..source_egress import SourceEgressService
from ..workspace_contracts import _COLLECTION
from .memory_turn import embedding_request
from .privacy import egress_allowed, is_private_project, privacy_revision


_INPUT_VALIDATION = ContextVar('embedding_input_validation', default=None)


@contextmanager
def embedding_input_validation(callback):
    """仅本轮可选向量输入使用纯校验，嵌套或异常后恢复原作用域。"""
    if callback is not None and not callable(callback):
        raise TypeError('embedding_input_validator_invalid')
    token = _INPUT_VALIDATION.set(callback)
    try:
        yield
    finally:
        _INPUT_VALIDATION.reset(token)


def chunk_vector_scores(query, project, entry, scope, snapshot, chunks, *,
                        layer, span_start, summary, question: str, span_slot=0, chunk_inventory=None) -> dict[int, float]:
    """Rank internal chunks without changing their parent's citation authority.

    An unavailable provider or changed authority discards this optional vector
    path. The caller retains its separately authorized lexical path.
    """
    public = getattr(query.models, 'public', None)
    if not callable(public) or snapshot is None or not chunks:
        return {}
    cache = None
    input_validation = _INPUT_VALIDATION.get()

    def validate_input(value):
        if input_validation is not None:
            input_validation(deepcopy(value))

    try:
        configured = public().get('embedding', {})
        if not configured.get('configured') or not configured.get('enabled'):
            return {}
        remote = urlsplit(str(configured.get('base_url', ''))).hostname not in {
            'localhost', '127.0.0.1', '::1'}
        local_vectors = configured.get('mode') == 'local' and configured.get('provider') == 'local'
        authority = SourceEgressService(query.records)
        frozen_privacy = privacy_revision(query.records)
        prefix = str(summary)[:120]

        def validate():
            if (scope.project_id != project or (not local_vectors and is_private_project(query.records, project))
                    or privacy_revision(query.records) != frozen_privacy
                    or (remote and not egress_allowed(query.records, query.models, project, 'embedding'))
                    or (_public_identity(public().get('embedding', {})) != _public_identity(configured)
                        if local_vectors else public().get('embedding', {}) != configured)):
                raise RecognitionConflict('chunk_vector_authority_changed')
            authority.validate_snapshot(scope, snapshot)
            if not local_vectors:
                authority.require(snapshot, 'embedding')
            material = query.retrieval_index.vector_material(project, entry, layer)
            if material is None:
                raise RecognitionConflict('chunk_vector_parent_changed')
            current, current_prefix, content_length, indexed_chunks = material
            if any(current.get(key) != entry.get(key) for key in ('revision', 'item_revision', 'title', 'item_id')):
                raise RecognitionConflict('chunk_vector_parent_changed')
            if query.original_snapshot(scope, current, authority) != snapshot:
                raise RecognitionConflict('chunk_vector_original_changed')
            if prefix != current_prefix:
                raise RecognitionConflict('chunk_vector_summary_changed')
            for chunk in chunks:
                left, right = span_start + chunk.start, span_start + chunk.end
                if (not 0 <= left < right <= content_length
                        or query.retrieval_index.indexed_slice(indexed_chunks, left, right) != chunk.text):
                    raise RecognitionConflict('chunk_vector_text_changed')

        validate()
        materials = []
        if entry['kind'] == 'document':
            materials.append({'type': 'document', 'id': entry['id'],
                              'revision': entry['revision'], 'project_id': project})
        for root in snapshot['roots']:
            material = {**root, 'project_id': project}
            if material not in materials:
                materials.append(material)
        if not materials:
            return {}
        provider = configured_adapter(query.models, 'embedding', validate_current=validate)
        original = provider.client

        class ObservedTransport:
            def post_json(self, **request):
                # 原 MemoryTurn 保存 key 前核对提供方实际准备发送的输入。
                validate_input(request['payload']['input'])
                return embedding_request(query.records, query.models, project, materials,
                    {'namespace': 'contextual-chunks-v1', 'embedding': request['payload']['input'],
                     'materials': materials, 'configuration_revision': configured.get('revision'),
                     **({'mode_revision': configured['mode_revision']} if local_vectors else {})},
                    validate, original, request)

        provider = replace(provider, client=ObservedTransport())
        namespace = chunk_cache_namespace(project, entry)
        def chunk_id(own_layer, own_slot, index):
            return chunk_cache_id(entry, own_layer, own_slot, index)

        projected = [{'id': chunk_id(layer, span_slot, index),
                      'project_id': project, 'revision': entry['revision'], 'source_refs': [],
                      'content': entry['title'] + '\n' + prefix + '\n' + chunk.text}
                     for index, chunk in enumerate(chunks)]
        # 先核原全文片段和问题，再打开派生缓存；不修改原资料。
        validate_input(projected)
        validate_input(question)
        cache = SQLiteEmbeddingCache(str(query.records.database_path.parent / 'recognition-vectors.sqlite3'))
        indexes = {item['id']: index for index, item in enumerate(projected)}
        # 本机前台只消费后台已经写入的四键缓存，缺失条目留给关键词路径。
        model_key = provider.cache_identity
        cached_vectors = {}
        if local_vectors:
            cached_vectors = {item['id']: cache.read(model_id=model_key,
                entry=SimpleNamespace(project_id=namespace, id=item['id'], revision=entry['revision']))
                for item in projected}
        cached_ids = {identity for identity, vector in cached_vectors.items() if vector is not None}
        class ScopedCache:
            def read(self, *, model_id, entry):
                if local_vectors:
                    if (model_id != model_key or entry.project_id != project
                            or entry.revision != projected[0]['revision'] or entry.id not in cached_ids):
                        raise RecognitionConflict('chunk_vector_cache_snapshot_changed')
                    return cached_vectors[entry.id]
                return cache.read(model_id=model_id, entry=replace(entry, project_id=namespace))

            def write(self, *, model_id, entry, vector):
                if local_vectors:
                    # 即使缓存被并发清理，也不把前台请求变成后台补算。
                    raise RecognitionConflict('local_vector_foreground_cache_write')
                validate()
                validate_input(projected)
                cache.write(model_id=model_id, entry=replace(entry, project_id=namespace), vector=vector)

        result = retrieve(project, question, projected, limit=len(projected),
                          vector_candidate_limit=len(projected), keyword_enabled=False,
                          embedding_provider=provider, embedding_cache=ScopedCache(),
                          embedding_allowed_ids=cached_ids if local_vectors else None)
        # retrieve deliberately degrades provider errors, including authority
        # exceptions: recheck independently before accepting even cached scores.
        validate()
        if result.trace.get('vector', {}).get('status') != 'used':
            return {}
        return {indexes[hit.id]: hit.vector_score for hit in result.hits}
    except Exception:
        # Optional retrieval failure never permits an ungoverned retry. No
        # provider error text, input text or credentials are persisted here.
        return {}
    finally:
        if cache is not None:
            cache.close()
