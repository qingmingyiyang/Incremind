from contextlib import contextmanager, nullcontext
from copy import deepcopy
from pathlib import Path

from backend.recognition import RecognitionConflict, WorkScope
from backend.security.audited_records import _AuditedTransaction
from core.storage_provider.sqlite_uow import SQLiteStructuredRecordUnitOfWork
from ..constraints import ProjectConstraintService
from ..transaction_records import TransactionRecords
from .material_origin_admission import BoundMaterialOrigins
from .material_origin_readers import qualified_record_read
from .material_read_context import current_material_data
from .policies import get
from .project_constraint_input import KIND, ProjectConstraintInput, constraint_markers
from .shared_auxiliary import _qualified_commit


def require_shared_reader_scope(reader, *, registry, actor, lease):
    if reader is None:
        return
    view = reader if isinstance(reader, TransactionRecords) else TransactionRecords(reader)
    actual = view._transaction
    if isinstance(actual, _AuditedTransaction):
        actual = actual.transaction
    if not isinstance(actual, SQLiteStructuredRecordUnitOfWork) or actual.closed or not actual.connection.in_transaction:
        raise RecognitionConflict('project constraints reader changed')
    if not actual._read_only and current_material_data(registry, actor, lease) is None:
        raise RecognitionConflict('project constraints shared commit scope unavailable')


class SharedProjectConstraintInput(ProjectConstraintInput):
    def __init__(self, query, personal):
        self.query, self.personal = query, personal
        self._shared_origin = query.origin_admission
        self.application = personal.application
        self.records, self.documents, self.root = personal.records, personal.documents, personal.root
        self.physical, self.installed_at = deepcopy(personal.physical), personal.installed_at

    def _assert_resources(self):
        super()._assert_resources()
        admission, query = self._shared_origin, self.query
        if (query.execution_owner is not admission.destination.workspace.query
                or query.execution_owner.project_constraint_input is not self.personal
                or self.application is not admission.application
                or self.records is not admission.destination.records
                or getattr(self.application.state, 'shared_query_factory', None) is not query.origin_factory):
            raise RecognitionConflict('project constraints shared owner changed')
        query.validate_retrieval_origin()

    @contextmanager
    def _source_reader(self, reader):
        admission = self._shared_origin
        view = None if reader is None else reader if isinstance(reader, TransactionRecords) else TransactionRecords(reader)
        require_shared_reader_scope(view, registry=admission.registry, actor=admission.actor, lease=admission.lease)
        with _qualified_commit(admission) as qualification:
            context = (qualified_record_read(self.records, validate=qualification._validate)
                if view is None else nullcontext(view))
            with context as transaction:
                actor_reader = transaction if isinstance(transaction, TransactionRecords) else TransactionRecords(transaction)
                origins = BoundMaterialOrigins(admission, admission.lease, {})
                source_reader = qualification.reader_for(origins, origins.shared, actor_reader=actor_reader)
                yield source_reader
                qualification._actor_reader(actor_reader)

    def snapshot(self, project, *, reader=None):
        if reader is self.records:
            reader = None
        admission = self._shared_origin
        # 先检查真实写事务资格，后续完整资源检查才可借读来源。
        require_shared_reader_scope(reader, registry=admission.registry, actor=admission.actor, lease=admission.lease)
        owner = self._owner()
        if project != self._shared_origin.lease.project_id:
            raise RecognitionConflict('project constraints shared scope changed')
        self.query.answer_turns._runtime()
        with self._source_reader(reader) as source_reader:
            rows = ProjectConstraintService(TransactionRecords(source_reader)).active(WorkScope('local-user', project))
            items = [{key: deepcopy(row[key]) for key in ('id', 'revision', 'content', 'valid_from', 'valid_until')}
                for row in rows]
            path = Path(self.query.records.database_path).resolve(strict=True)
            file, directory = path.stat(), path.parent.stat()
            source_physical = {'path': str(path), 'file': [file.st_dev, file.st_ino],
                'directory': [directory.st_dev, directory.st_ino]}
            if self._owner() != owner:
                raise RecognitionConflict('project constraints owner changed')
        return {'version': 'project-constraints@1', 'owner': owner,
            'source': {'namespace': self.query.source_namespace, 'lease': self._shared_origin.lease.payload()},
            'physical': {'actor': deepcopy(self.physical), 'shared': source_physical},
            'project_id': project, 'items': items, 'prompt': get('constraint_scope', version='@1')().prompt(items)}

    def augment(self, request, store, items):
        if not self.eligible(request):
            return tuple(items)
        if any(item.kind == KIND for item in items):
            raise RecognitionConflict('project constraints already prepared')
        return (*items, *super().augment(request, store, ()))

    def validate(self, request, *, reader=None):
        if reader is self.records:
            reader = None
        admission = self._shared_origin
        require_shared_reader_scope(reader, registry=admission.registry, actor=admission.actor, lease=admission.lease)
        if self.eligible(request) and not constraint_markers(request):
            raise RecognitionConflict('project constraints shared admission unavailable')
        return super().validate(request, reader=reader)
