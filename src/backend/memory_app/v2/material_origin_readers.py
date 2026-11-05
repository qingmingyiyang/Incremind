from collections.abc import Iterator
from contextlib import contextmanager
import sqlite3

from backend.recognition import RecognitionConflict
from core.storage_provider import SQLiteStructuredRecordStore
from core.storage_provider.sqlite_uow import SQLiteStructuredRecordUnitOfWork
from core.document_engine import SQLiteDocumentRepository


def _readonly_connection(database_path):
    connection = sqlite3.connect(database_path.as_uri() + '?mode=ro', uri=True, check_same_thread=False)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA query_only=ON')
        return connection
    except BaseException:
        connection.close()
        raise


@contextmanager
def qualified_record_read(records, *, validate):
    validate()
    connection = _readonly_connection(records.database_path)
    try:
        validate()
        connection.execute('BEGIN')
        with SQLiteStructuredRecordUnitOfWork(connection, read_only=True) as reader:
            try:
                yield reader
            finally:
                validate()
    finally:
        connection.close()


def qualified_actor_reader(origins, reader):
    if reader is None:
        return None
    from ..transaction_records import TransactionRecords
    from .shared_auxiliary import current_qualified_commit_scope
    scope = current_qualified_commit_scope()
    if scope is None or scope._admission is not origins.admission:
        raise RecognitionConflict('material_origin_actor_scope_required_before_transaction')
    return scope._actor_reader(TransactionRecords(reader))


class ReadOnlyMaterialRecords(SQLiteStructuredRecordStore):
    def __init__(self, database_path, *, validate):
        super().__init__(database_path)
        self._validate = validate

    def begin(self):
        self._validate()
        raise RecognitionConflict('shared_material_read_only')

    def _connect(self):
        self._validate()
        connection = _readonly_connection(self.database_path)
        try:
            self._validate()
        except BaseException:
            connection.close()
            raise
        return _ReadConnection(connection, self._validate)


class _ReadCursor:
    def __init__(self, cursor, validate):
        self._cursor, self._validate = cursor, validate

    def _read(self, operation, *args, **kwargs):
        self._validate()
        result = operation(*args, **kwargs)
        self._validate()
        return result

    def fetchone(self):
        return self._read(self._cursor.fetchone)

    def fetchmany(self, *args):
        return self._read(self._cursor.fetchmany, *args)

    def fetchall(self):
        return self._read(self._cursor.fetchall)

    def execute(self, *args, **kwargs):
        self._read(self._cursor.execute, *args, **kwargs)
        return self

    def __iter__(self):
        return self

    def __next__(self):
        return self._read(next, self._cursor)

    def close(self):
        return self._cursor.close()

    def __getattr__(self, name):
        if name in {'description', 'rowcount', 'lastrowid', 'arraysize'}:
            self._validate()
            return getattr(self._cursor, name)
        raise RecognitionConflict('shared_material_read_only')


class _ReadConnection:
    def __init__(self, connection, validate):
        self._connection, self._validate = connection, validate

    def execute(self, statement, *args, **kwargs):
        # 事务清理不读取正文，失权后仍允许释放原生资源。
        cleanup = statement.strip().upper() in {'COMMIT', 'ROLLBACK'}
        if not cleanup:
            self._validate()
        cursor = self._connection.execute(statement, *args, **kwargs)
        if not cleanup:
            self._validate()
        return _ReadCursor(cursor, self._validate)

    def cursor(self, *args, **kwargs):
        self._validate()
        return _ReadCursor(self._connection.cursor(*args, **kwargs), self._validate)

    def close(self):
        return self._connection.close()

    def rollback(self):
        return self._connection.rollback()

    @property
    def in_transaction(self):
        return self._connection.in_transaction

    def __enter__(self):
        self._validate()
        self._connection.__enter__()
        return self

    def __exit__(self, *args):
        return self._connection.__exit__(*args)


def _guarded_value(value, validate):
    if isinstance(value, Iterator):
        def iterate():
            while True:
                validate()
                try:
                    item = next(value)
                except StopIteration:
                    return
                validate()
                yield item
        return iterate()
    return value


class ReadOnlyMaterialFacade:
    def __init__(self, source, *, validate):
        self._source, self._validate = source, validate

    def __getattr__(self, name):
        self._validate()
        value = getattr(self._source, name)
        if not callable(value):
            return value
        def read(*args, **kwargs):
            self._validate()
            result = value(*args, **kwargs)
            self._validate()
            return _guarded_value(result, self._validate)
        return read


class ReadOnlyMaterialSources(ReadOnlyMaterialFacade):
    _READS = frozenset({'read', 'read_including_deleted', 'list', 'list_including_deleted',
        'collection_names', 'revision', 'incarnation', 'attribution', 'asset_location',
        'is_revision_conflict', '_collection_path', '_object_path', '_meta_path', '_object_paths',
        '_guard_source_authority', '_source_published'})
    _PATHS = frozenset({'root', 'namespace_id', 'vector_cache_path', 'authority_identity', 'sqlite_active'})

    def __getattr__(self, name):
        if name not in self._READS | self._PATHS:
            self._validate()
            raise RecognitionConflict('shared_material_read_only')
        return super().__getattr__(name)


class ReadOnlyMaterialDocuments(SQLiteDocumentRepository):
    _READS = frozenset({'read', 'list', 'revision', 'revisions', 'markdown', 'records', 'namespace_id', 'now'})

    def __init__(self, source, *, validate):
        self._validate = validate
        super().__init__(source.records, namespace_id=source.namespace_id, now=source.now)

    def __getattribute__(self, name):
        if name.startswith('_'):
            return super().__getattribute__(name)
        validate = super().__getattribute__('_validate')
        validate()
        if name not in super().__getattribute__('_READS'):
            raise RecognitionConflict('shared_material_read_only')
        value = super().__getattribute__(name)
        if not callable(value):
            return value
        def read(*args, **kwargs):
            validate()
            result = value(*args, **kwargs)
            validate()
            return _guarded_value(result, validate)
        return read
