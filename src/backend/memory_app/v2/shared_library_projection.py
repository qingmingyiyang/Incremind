from contextlib import contextmanager
import sqlite3
from threading import get_ident

from backend.recognition import RecognitionConflict
from core.document_engine import SQLiteDocumentRepository
from core.storage_provider.connection_scope import connection_scope
from ..source_egress import recognition_service
from ..workspace_query import WorkspaceQuery
from .material_origin_readers import ReadOnlyMaterialRecords, _ReadConnection, qualified_record_read
from .material_read_context import qualified_material_scope
from .shared_auxiliary import _record_database


def _authorize_sql(action, argument, value, _database, _origin):
    if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE):
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_PRAGMA and (argument, value) in {
            ('database_list', None), ('query_only', 'ON')}:
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


class _ProjectionConnection(_ReadConnection):
    def __init__(self, scope):
        self._scope, self._closed = scope, False
        super().__init__(scope.reader.connection, self._validate_lease)

    def _validate_lease(self):
        if self._closed:
            raise RecognitionConflict('shared_library_projection_expired')
        self._scope.validate()

    def execute(self, statement, *args, **kwargs):
        self._validate_lease()
        if not args and not kwargs and statement.strip().upper() in {'BEGIN', 'ROLLBACK'}:
            # 领域读取借用同一只读快照，内层事务清理只释放自己的借用。
            return None
        return super().execute(statement, *args, **kwargs)

    def rollback(self):
        self._validate_lease()

    def close(self):
        self._closed = True

    def __enter__(self):
        self._validate_lease()
        return self

    def __exit__(self, *_error):
        self.close()


class _ProjectionRecords(ReadOnlyMaterialRecords):
    def __init__(self, scope):
        super().__init__(scope.data.records.database_path, validate=scope.validate)
        self._scope = scope

    def _connect(self):
        self._scope.validate()
        return _ProjectionConnection(self._scope)


class _ProjectionScope:
    def __init__(self, admission, data, reader):
        self.admission, self.data, self.reader = admission, data, reader
        self.thread, self.active = get_ident(), True

    def validate(self):
        if not self.active or self.thread != get_ident() or self.reader.closed or not self.reader.connection.in_transaction:
            raise RecognitionConflict('shared_library_projection_expired')

    def material_data(self, registry, actor, lease):
        self.validate()
        if registry is not self.admission.registry or actor != self.admission.actor or lease != self.admission.lease:
            raise RecognitionConflict('material_origin_scope_changed')
        return self.data

    def source_records(self, _owner, _reader):
        raise RecognitionConflict('shared_library_projection_read_only')


def _query(issued, records):
    documents = SQLiteDocumentRepository(records, namespace_id=issued.documents.namespace_id, now=issued.documents.now)
    query = WorkspaceQuery(records, documents, issued.source_store, issued.models, recognition_service(records),
        read_only=True, source_index_records=issued.retrieval_index.source_records, profile_owner=issued.profile_owner,
        receipt_records=issued.receipt_records, execution_owner=issued.execution_owner,
        origin_admission=issued.origin_admission, origin_dispatcher=issued.origin_dispatcher)
    query.origin_factory = issued.origin_factory
    query.source_namespace = issued.source_namespace
    query.material_owner = issued.material_owner
    query.answer_turns = issued.answer_turns
    return query


@contextmanager
def library_projection(issued):
    admission = issued.origin_admission
    registry, actor, lease = admission.registry, admission.actor, admission.lease
    authority = {'project_revision': lease.project_revision, 'member_revision': lease.member_revision,
                 'account_revision': lease.account_revision}
    with connection_scope():
        issued.validate_retrieval_origin()
        with registry.records.begin_read() as central:
            access = registry.authorize(central, actor, lease.project_id, authority=authority)
            with registry.roots.lease(lease.project_id):
                data = registry.roots._data(lease.project_id)
                if data.namespace != lease.data_namespace or data.root_identity != lease.root_identity:
                    raise RecognitionConflict('material_origin_root_changed')
                database = _record_database(data.records, data.root)

                def validate_location():
                    if _record_database(data.records, data.root) != database:
                        raise RecognitionConflict('material_origin_root_changed')

                with qualified_record_read(data.records, validate=validate_location) as reader:
                    marker = reader.read('v2_shared_root', 'identity')
                    if marker is None or marker.payload.get('project_id') != lease.project_id:
                        raise RecognitionConflict('material_origin_root_changed')
                    scope = _ProjectionScope(admission, data, reader)
                    connection = reader.connection
                    connection.set_authorizer(_authorize_sql)
                    try:
                        with qualified_material_scope(scope):
                            yield _query(issued, _ProjectionRecords(scope)), access
                    finally:
                        scope.active = False
                        connection.set_authorizer(None)
        # 旧中央读事务及借用均已结束，发布前重新读取当前资格及资料根。
        admission.validate_execution_owner()
        with lease._current_data(registry, actor) as fresh:
            if (_record_database(fresh.records, fresh.root) != database
                    or fresh.records.read('v2_shared_root', 'identity') != marker):
                raise RecognitionConflict('material_origin_root_changed')
