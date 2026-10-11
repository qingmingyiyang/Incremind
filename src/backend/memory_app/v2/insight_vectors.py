"""Meaning-based similarity for published insights through the governed vector path."""
from dataclasses import replace
from types import SimpleNamespace
from urllib.parse import urlsplit

from backend.recognition import RecognitionConflict, RecognitionError
from backend.recognition_retrieval import SQLiteEmbeddingCache, retrieve
from ..retrieval_models import configured_adapter, _public_identity
from ..source_egress import SourceEgressService
from .contextual_chunk_vectors import ReusedQuestion
from .embedding_settings import local_vectors_ready
from .memory_turn import embedding_request
from .privacy import egress_allowed, is_private_project, privacy_revision


def insight_vector_scores(query, project, scope, entries, question) -> dict[str, float]:
    """Return {insight id: similarity to the question}, or {} to stay keyword-only.

    Local vectors encode only the question now; insights use the vectors the
    background index already cached (EmbeddingIndex keys them by project, id,
    revision and model), so an unindexed insight simply stays keyword-only.
    Configured remote vectors follow the egress rules and fill the cache, like
    contextual chunk vectors. Any failure returns {} and never retries.
    """
    public = getattr(query.models, 'public', None)
    if not callable(public) or not entries:
        return {}
    cache = None
    try:
        configured = public().get('embedding', {})
        if not configured.get('configured') or not configured.get('enabled'):
            return {}
        remote = urlsplit(str(configured.get('base_url', ''))).hostname not in {'localhost', '127.0.0.1', '::1'}
        local_vectors = configured.get('mode') == 'local' and configured.get('provider') == 'local'
        if not local_vectors and is_private_project(query.records, project):
            return {}
        if local_vectors and not local_vectors_ready(query.models):
            # Loading takes about a minute; this question stays keyword-only meanwhile.
            return {}
        authority = SourceEgressService(query.records)
        frozen_privacy = privacy_revision(query.records)
        snapshots = {}
        for entry in entries:
            try:
                snapshot = authority.snapshot(scope, [{'type': 'recognition', 'id': entry['id'],
                                                       'revision': entry['revision']}])
                if not local_vectors:
                    authority.require(snapshot, 'embedding')
            except RecognitionError:
                continue
            snapshots[entry['id']] = snapshot
        if not snapshots:
            return {}

        def validate():
            if (privacy_revision(query.records) != frozen_privacy
                    or (not local_vectors and is_private_project(query.records, project))
                    or (remote and not egress_allowed(query.records, query.models, project, 'embedding'))
                    or (_public_identity(public().get('embedding', {})) != _public_identity(configured)
                        if local_vectors else public().get('embedding', {}) != configured)):
                raise RecognitionConflict('insight_vector_authority_changed')
            for snapshot in snapshots.values():
                authority.validate_snapshot(scope, snapshot)
                if not local_vectors:
                    authority.require(snapshot, 'embedding')

        validate()
        materials = []
        for snapshot in snapshots.values():
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
                return embedding_request(query.records, query.models, project, materials,
                    {'namespace': 'insight-recall-v1', 'embedding': request['payload']['input'],
                     'materials': materials, 'configuration_revision': configured.get('revision'),
                     **({'mode_revision': configured['mode_revision']} if local_vectors else {})},
                    validate, original, request)

        provider = ReusedQuestion(replace(provider, client=ObservedTransport()))
        # Same text and key the background index writes for an insight.
        projected = [{'id': entry['id'], 'revision': entry['revision'], 'project_id': project,
                      'content': entry['content'], 'source_refs': []}
                     for entry in entries if entry['id'] in snapshots]
        cache = SQLiteEmbeddingCache(str(query.records.database_path.parent / 'recognition-vectors.sqlite3'))
        model_key = provider.cache_identity
        allowed = None
        cached_vectors = {}
        if local_vectors:
            # 冻结本轮已读向量，清扫缓存不能让后续检索转为现场计算认识正文。
            for item in projected:
                identity = (project, item['id'], item['revision'])
                vector = cache.read(model_id=model_key,
                    entry=SimpleNamespace(project_id=project, id=item['id'], revision=item['revision']))
                if vector is not None:
                    cached_vectors[identity] = vector
            allowed = {identity for _, identity, _ in cached_vectors}
            if not allowed:
                return {}

        class ScopedCache:
            def read(self, *, model_id, entry):
                if local_vectors:
                    return (cached_vectors.get((entry.project_id, entry.id, entry.revision))
                            if model_id == model_key else None)
                return cache.read(model_id=model_id, entry=entry)

            def write(self, *, model_id, entry, vector):
                if local_vectors:
                    # The question turn never becomes background indexing.
                    raise RecognitionConflict('local_vector_foreground_cache_write')
                validate()
                cache.write(model_id=model_id, entry=entry, vector=vector)

        result = retrieve(project, question, projected, limit=len(projected),
                          vector_candidate_limit=len(projected), keyword_enabled=False,
                          embedding_provider=provider, embedding_cache=ScopedCache(),
                          embedding_allowed_ids=allowed)
        # retrieve degrades provider and authority errors alike; recheck before trusting scores.
        validate()
        if result.trace.get('vector', {}).get('status') != 'used':
            return {}
        return {hit.id: hit.vector_score for hit in result.hits if hit.vector_score is not None}
    except Exception:
        # Optional recall never permits an ungoverned retry or stores input text.
        return {}
    finally:
        if cache is not None:
            cache.close()
