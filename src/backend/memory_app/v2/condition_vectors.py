from contextlib import nullcontext
from dataclasses import replace
import sqlite3
from types import SimpleNamespace
from urllib.parse import urlsplit

from backend.recognition import RecognitionConflict, RecognitionError
from backend.recognition_retrieval import RecognitionRetrievalError, SQLiteEmbeddingCache, retrieve
from core.search_and_recall.vector_cache_invalidation import condition_cache_namespace

from ..model_config import ModelConfigurationError
from ..recall_state import is_recall_excluded
from ..retrieval_models import _public_identity, configured_adapter
from ..source_egress import SourceEgressService
from .embedding_settings import local_vectors_ready, vector_policy
from .indexer import ReusedQuestion, with_question_vectors
from .memory_turn import embedding_request
from .privacy import egress_allowed, is_private_project, privacy_revision
from .shared_vector_keys import material_vector_namespace


FIELD = 'method-condition-v1'


@with_question_vectors
def condition_vector_scores(query, project, scope, entries, situation):
    """返回当前认识各项条件与情境的最大相似度。"""
    if query.offline or not entries or not situation.strip():
        return {}
    query.validate_retrieval_origin()
    settings = query.models.public().get('embedding', {})
    if not settings.get('configured') or not settings.get('enabled'):
        return {}
    local_vectors = settings.get('mode') == 'local' and settings.get('provider') == 'local'
    if local_vectors and not local_vectors_ready(query.models):
        return {}
    remote = urlsplit(settings['base_url']).hostname not in {'127.0.0.1', 'localhost', '::1'}
    if (is_private_project(query.records, project)
            or remote and not egress_allowed(query.records, query.models, project, 'embedding')):
        return {}
    authority = SourceEgressService(query.records)
    epoch = privacy_revision(query.records)
    sources, projected, keys, positions = {}, [], {}, {}
    for entry in entries:
        conditions = [(index, value) for index, value in enumerate(entry.get('conditions', ()))
            if isinstance(value, str) and value.strip() and len(value) <= vector_policy()['max_input_chars']]
        if not conditions:
            continue
        try:
            snapshot = authority.snapshot(scope, [{'type': 'recognition', 'id': entry['id'],
                                                   'revision': entry['revision']}])
            authority.require(snapshot, 'embedding')
        except RecognitionError:
            continue
        sources[entry['id']] = snapshot
        for index, value in conditions:
            identity = 'condition-' + str(len(projected))
            projected.append({'id': identity, 'revision': entry['revision'], 'project_id': project,
                              'content': value, 'source_refs': []})
            keys[identity] = SimpleNamespace(id=entry['id'], revision=entry['revision'],
                project_id=condition_cache_namespace(project, index))
            positions[identity] = (entry, index)
    if not projected:
        return {}

    def validate(identities):
        query.validate_retrieval_origin()
        current = query.models.public().get('embedding', {})
        if (privacy_revision(query.records) != epoch or is_private_project(query.records, project)
                or remote and not egress_allowed(query.records, query.models, project, 'embedding')
                or (_public_identity(current) != _public_identity(settings) if local_vectors else current != settings)):
            raise RecognitionConflict('method_condition_vector_authority_changed')
        for identity in identities:
            if is_recall_excluded(query.records, scope, identity):
                raise RecognitionConflict('method_condition_vector_authority_changed')
            authority.validate_snapshot(scope, sources[identity])
            authority.require(sources[identity], 'embedding')

    size = vector_policy()['batch_size']
    result = {}
    cache = None
    try:
        cache = SQLiteEmbeddingCache(str(query.execution_owner.records.database_path.parent / 'recognition-vectors.sqlite3'))
        class ConditionCache:
            def __init__(self):
                self.missing = []

            def key(self, model_id, entry):
                raw = keys[entry.id]
                return SimpleNamespace(id=raw.id, revision=raw.revision,
                    project_id=material_vector_namespace(query, raw.project_id, entry.content, model_id))

            def read(self, *, model_id, entry):
                result = cache.read(model_id=model_id, entry=self.key(model_id, entry))
                if result is None:
                    self.missing.append(entry.id)
                return result

            def write(self, *, model_id, entry, vector):
                validate((keys[entry.id].id,))
                cache.write(model_id=model_id, entry=self.key(model_id, entry), vector=vector)

        for start in range(0, len(projected), size):
            batch = projected[start:start + size]
            identities = tuple(dict.fromkeys(keys[row['id']].id for row in batch))

            def current():
                validate(identities)

            current()
            provider = configured_adapter(query.models, 'embedding', validate_current=current)
            transport = provider.client
            materials = list({(root['type'], root['id'], root['revision']): {**root, 'project_id': project}
                for identity in identities for root in sources[identity]['roots']}.values())

            scoped_cache = ConditionCache()
            class ObservedTransport:
                def post_json(self, **request):
                    if query.origin_admission is not None:
                        from .shared_rendered_preparation import condition_wire_input
                        rendered = condition_wire_input(query, payload=request['payload'], question=situation,
                            conditions=[positions[identity] for identity in scoped_cache.missing])
                    else:
                        rendered = nullcontext(None)
                    with rendered as prepared:
                        return embedding_request(query.execution_owner.records, query.models, project, materials,
                            {'namespace': FIELD, 'inputs': request['payload']['input'], 'materials': materials,
                             'model_key': provider.cache_identity}, current, transport, request,
                            origin_admission=query.origin_admission, rendered_input=prepared)

            observed = ReusedQuestion(replace(provider, client=ObservedTransport()),
                cache_namespace=query.cache_namespace(provider.cache_identity), validate_current=current)
            scored = retrieve(project, situation, batch, limit=len(batch), vector_candidate_limit=len(batch),
                keyword_enabled=False, embedding_provider=observed,
                embedding_cache=scoped_cache)
            current()
            for hit in scored.hits:
                if hit.vector_score is not None:
                    identity = keys[hit.id].id
                    result[identity] = max(result.get(identity, -1.0), hit.vector_score)
        validate(tuple(result))
        return result
    except (RecognitionError, RecognitionRetrievalError, ModelConfigurationError, sqlite3.Error):
        return {}
    finally:
        if cache is not None:
            cache.close()
