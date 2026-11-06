"""Daily fallback cleanup using current domain authority and existing indexes."""
from backend.recognition import RecognitionError, WorkScope
from backend.recognition_retrieval import SQLiteEmbeddingCache
from backend.recognition_retrieval.cache_invalidation import chunk_cache_parent, chunk_cache_id
from core.search_and_recall.vector_cache_invalidation import condition_cache_parent
from ..source_egress import SourceEgressService
from ..retrieval_models import configured_adapter
import re
from .privacy import is_private_project
from .shared_vector_keys import contextual_cache_prefix, indexed_summary


class CacheMaintenance:
    def __init__(self, query):
        self.query = query
        self.path = query.records.database_path.parent / 'recognition-vectors.sqlite3'

    def run(self):
        if not self.path.is_file():
            return 0
        authority = SourceEgressService(self.query.records)
        scopes = {}
        for row in self.query.records.list('recognitions'):
            raw = row.payload.get('scope', {})
            scope = WorkScope(raw['user_id'], raw.get('project_id'))
            scopes.setdefault(scope.project_id, set()).add(scope)
        cache = SQLiteEmbeddingCache(str(self.path))
        try:
            configured = self.query.models.public().get("embedding", {})
            managed = (configured.get("mode") == "local" and configured.get("provider") == "local"
                       and configured.get("configured") and configured.get("enabled"))
            provider = configured_adapter(self.query.models, "embedding") if managed else None
            model_key = provider.cache_identity if provider is not None else None
            # 本机修订是配置:模式两段；同端点外接修订只有一段，不属于本机管理域。
            model_pattern = (re.escape(provider.endpoint) + r"\|[^|]+:[1-9][0-9]*:vector@[0-9]+\|[0-9]+:[0-9]+"
                             if provider is not None else None)
            removed = 0
            for namespace in cache.namespaces():
                parent = chunk_cache_parent(namespace)
                condition = condition_cache_parent(namespace)
                if parent is not None:
                    current = self._chunks(*parent, authority)
                elif condition is not None:
                    project, slot = condition
                    current = self._recognitions(project, scopes.get(project, ()), authority, condition_slot=slot)
                else:
                    current = self._recognitions(namespace, scopes.get(namespace, ()), authority)
                removed += cache.purge_invalid(project_id=namespace, current_revisions=current,
                                               current_model_id=model_key, model_id_pattern=model_pattern)
            return removed
        finally:
            cache.close()

    def _recognitions(self, project, scopes, authority, *, condition_slot=None):
        configured = self.query.models.public().get('embedding', {})
        local = configured.get('mode') == 'local' and configured.get('provider') == 'local'
        if not local and is_private_project(self.query.records, project):
            return {}
        current = {}
        for scope in scopes:
            for entry in self.query.service.retrieval_entries(scope=scope):
                if condition_slot is not None and condition_slot >= len(entry['conditions']):
                    continue
                try:
                    snapshot = authority.snapshot(scope, [{'type':'recognition', 'id':entry['id'], 'revision':entry['revision']}])
                    if not local:
                        authority.require(snapshot, 'embedding')
                except RecognitionError:
                    continue
                current[entry['id']] = entry['revision']
        return current

    def _chunks(self, project, kind, identity, authority):
        configured = self.query.models.public().get('embedding', {})
        local = configured.get('mode') == 'local' and configured.get('provider') == 'local'
        if not local and is_private_project(self.query.records, project):
            return {}
        entries, _, documents, sources, originals = self.query.retrieval_index.indexed_entries(
            project, selected={'kind':kind, 'id':identity})
        entry = next((value for value in entries if value['kind'] == kind and value['id'] == identity), None)
        if entry is None:
            return {}
        try:
            scope = WorkScope('local-user', project)
            snapshot = self.query.original_snapshot(scope, entry, authority)
            if snapshot is None:
                return {}
            if not local:
                authority.require(snapshot, 'embedding')
        except RecognitionError:
            return {}
        context_prefix = None
        if kind == 'document':
            summary = documents[identity]['summary']
            context_prefix = contextual_cache_prefix(summary, indexed_summary(self.query.records, project, entry, summary))
            spans = [(span['layer'], span['chunks']) for span in documents[identity]['spans']]
            original = originals.get(entry.get('item_id'))
            if original and original['length']:
                spans.append(('L0', original['chunks']))
        else:
            source = next(value for value in sources if value['source_id'] == identity)
            spans = [('L0', source['chunks'])]
        current = {chunk_cache_id(entry, layer, slot, index, context_prefix=context_prefix):entry['revision']
                   for slot, (layer, chunks) in enumerate(spans) if layer != 'L2'
                   for index in range(len(chunks))}
        if kind == 'document' and documents[identity]['summary'] and any(layer == 'L2' for layer, _ in spans):
            from .topic_groups import summary_cache_entry
            current[summary_cache_entry(project, entry).id] = entry['revision']
        return current
