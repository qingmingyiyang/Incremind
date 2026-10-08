"""Invalidate existing derived vector rows without knowing product authority."""
import json
import sqlite3
from pathlib import Path

from core.storage_provider.observability import observe_connection


def chunk_cache_namespace(project, entry):
    return json.dumps(['contextual-chunks-v1', project, entry['kind'], entry['id']],
                      ensure_ascii=False, separators=(',', ':'))


def chunk_cache_id(entry, layer, span_slot, index):
    return json.dumps([layer, span_slot, entry.get('item_revision'), index], separators=(',', ':'))


def chunk_cache_parent(namespace):
    try:
        value = json.loads(namespace)
    except (TypeError, ValueError):
        return None
    if (isinstance(value, list) and len(value) == 4 and value[0] == 'contextual-chunks-v1'
            and all(isinstance(part, str) and part for part in value[1:])
            and value[2] in {'document', 'source'}):
        return tuple(value[1:])
    return None


def vector_cache_path(tx):
    """Use the explicitly enlisted main database, including legacy locations."""
    connection = getattr(tx, 'connection', None)
    path = (next((row[2] for row in connection.execute('PRAGMA database_list')
                  if row[1] == 'main'), '') if connection is not None
            else tx.database_path)
    return Path(path).parent / 'recognition-vectors.sqlite3' if path else None


def delete_cached_recognition(connection, project, identity):
    return connection.execute(
        'DELETE FROM recognition_embedding_cache WHERE project_id = ? AND recognition_id = ?',
        (project, identity),
    ).rowcount


def delete_cached_namespace(connection, namespace):
    return connection.execute(
        'DELETE FROM recognition_embedding_cache WHERE project_id = ?', (namespace,),
    ).rowcount


def cached_parents(connection):
    """Read derived identities only, without vector bodies or model partitions."""
    return tuple(connection.execute(
        'SELECT DISTINCT project_id, recognition_id FROM recognition_embedding_cache '
        'ORDER BY project_id, recognition_id'
    ))


class VectorCacheInvalidator:
    """No cached authority; write owners supply explicit changed identities."""
    def __init__(self, database_path):
        self.path = Path(database_path) if database_path is not None else None

    def _apply(self, operation, *, missing=0):
        if self.path is None or not self.path.is_file():
            return missing
        # mode=rw closes the existence-check race without creating a database.
        connection = sqlite3.connect(self.path.resolve().as_uri() + '?mode=rw', uri=True)
        observe_connection(connection)
        try:
            if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='recognition_embedding_cache'").fetchone():
                return missing
            result = operation(connection)
            connection.commit()
            return result
        finally:
            connection.close()

    def parents(self):
        return self._apply(cached_parents, missing=())

    def targets(self, *, recognitions=(), materials=()):
        """Delete a fully qualified caller plan in one derived transaction."""
        materials = tuple(dict.fromkeys(materials))
        if any(kind not in {'document', 'source'} for _, kind, _ in materials):
            raise ValueError('vector_cache_material_kind_invalid')
        namespaces = tuple(chunk_cache_namespace(project, {'kind': kind, 'id': identity})
                           for project, kind, identity in materials)
        identities = tuple(dict.fromkeys(recognitions))
        def invalidate(connection):
            return (sum(delete_cached_recognition(connection, project, identity)
                        for project, identity in identities)
                    + sum(delete_cached_namespace(connection, namespace) for namespace in namespaces))
        return self._apply(invalidate)

    def recognitions(self, project, identities):
        return self._apply(lambda connection: sum(delete_cached_recognition(connection, project, identity)
                                                 for identity in dict.fromkeys(identities)))

    def material(self, project, kind, identity):
        if kind not in {'document', 'source'}:
            raise ValueError('vector_cache_material_kind_invalid')
        namespace = chunk_cache_namespace(project, {'kind':kind, 'id':identity})
        return self._apply(lambda connection: delete_cached_namespace(connection, namespace))

    def project(self, project):
        def invalidate(connection):
            removed = delete_cached_namespace(connection, project)
            namespaces = tuple(row[0] for row in connection.execute('SELECT DISTINCT project_id FROM recognition_embedding_cache'))
            for namespace in namespaces:
                parent = chunk_cache_parent(namespace)
                if parent is not None and parent[0] == project:
                    removed += delete_cached_namespace(connection, namespace)
            return removed
        return self._apply(invalidate)
