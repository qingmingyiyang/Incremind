from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass

from backend.recognition import RecognitionConflict
from core.storage_provider.sqlite_uow import begin_read

from ..transaction_records import TransactionRecords
from .material_origin_admission import BoundMaterialOrigins
from .material_origin_readers import qualified_record_read
from .material_origins import ORIGIN_KIND, SharedMaterialOwner
from .shared_auxiliary import _qualified_commit, current_qualified_commit_scope
from .shared_query import SharedQueryFactory
from .shared_rendered_preparation import _query_current


_TASK_PRIVACY_READS = ContextVar('shared_task_dependency_privacy_reads', default=())


@contextmanager
def task_dependency_scope(service, identity):
    from ..source_graph import LIMIT
    previous = _TASK_PRIVACY_READS.get()
    key = (service, identity)
    if key in previous or len(previous) >= LIMIT:
        raise RecognitionConflict('shared_task_dependency_cycle_or_limit')
    token = _TASK_PRIVACY_READS.set((*previous, key))
    try:
        yield
    finally:
        _TASK_PRIVACY_READS.reset(token)


def _factory(service):
    factory = getattr(service.application.state, 'shared_query_factory', None)
    if not isinstance(factory, SharedQueryFactory):
        raise RecognitionConflict('outcome_source_factory_unavailable')
    return factory


def _issued_query(service, scope):
    factory = _factory(service)
    if scope._admission.application is not service.application or scope._admission.registry is not factory.registry:
        raise RecognitionConflict('outcome_source_execution_owner_changed')
    with factory._lock:
        selected = [query for query in factory._issued_queries if query.origin_admission is scope._admission]
    if len(selected) != 1:
        raise RecognitionConflict('outcome_source_query_not_issued')
    _query_current(selected[0])
    return selected[0]


def _shared_request(service, request):
    if request is None:
        return False
    native = service._native()
    if native.get_request(request['turn_id']) != request:
        raise RecognitionConflict('outcome_source_native_request_changed')
    bundle = native.restore_admission(request)
    selected = [] if bundle is None else [item for item in bundle.items if item.kind == ORIGIN_KIND]
    if not selected:
        return False
    if len(selected) != 1:
        raise RecognitionConflict('outcome_source_origin_changed')
    return True


@contextmanager
def source_scope(service, *, request=None, reader=None):
    service._validate()
    current = current_qualified_commit_scope()
    if reader is not None:
        if isinstance(reader, SourceReadRecords):
            reader.validate(service)
        if current is None:
            if _shared_request(service, request):
                raise RecognitionConflict('outcome_source_scope_required_before_actor_transaction')
            yield None
            return
        current._actor_reader(TransactionRecords(reader))
    if current is not None:
        query = _issued_query(service, current)
        if _shared_request(service, request):
            query.origin_admission.restore(request)
        yield query
        current._validate()
        return
    if not _shared_request(service, request):
        yield None
        return
    query = _factory(service).dispatch(service.application, request)
    _query_current(query)
    query.answer_turns._runtime()
    with _qualified_commit(query.origin_admission):
        query.origin_admission.restore(request)
        yield query


class SourceReadRecords(TransactionRecords):
    def __init__(self, reader, service, query, scope):
        super().__init__(reader)
        self._plain = TransactionRecords(reader)
        self.service, self.query, self.qualification = service, query, scope
        self.origins = BoundMaterialOrigins(query.origin_admission, query.origin_admission.lease, {})

    def validate(self, service=None):
        if service is not None and service is not self.service:
            raise RecognitionConflict('outcome_source_execution_owner_changed')
        current = current_qualified_commit_scope()
        if current is not self.qualification or self.query.origin_admission is not current._admission:
            raise RecognitionConflict('outcome_source_read_scope_expired')
        current._actor_reader(self._plain)
        _query_current(self.query)

    @property
    def connection(self):
        self.validate()
        return self._plain.connection

    @property
    def database_path(self):
        self.validate()
        return self._plain.database_path

    def read(self, collection, identity):
        self.validate()
        return self._plain.read(collection, identity)

    def list(self, collection):
        self.validate()
        return self._plain.list(collection)

    def put(self, collection, identity, payload, *, expected_revision):
        self.validate()
        return self._plain.put(collection, identity, payload, expected_revision=expected_revision)

    def source_reader(self, owner):
        self.validate()
        if isinstance(owner, SharedMaterialOwner):
            if owner is not self.query.material_owner:
                raise RecognitionConflict('outcome_source_owner_changed')
            return TransactionRecords(self.qualification.reader_for(self.origins, self.origins.shared, actor_reader=self._plain))
        if owner.records is not self.service.records:
            raise RecognitionConflict('outcome_source_owner_changed')
        return self._plain


def bind_source_reader(service, reader):
    if isinstance(reader, SourceReadRecords):
        reader.validate(service)
        return reader
    scope = current_qualified_commit_scope()
    view = TransactionRecords(reader)
    if scope is None:
        return view
    scope._actor_reader(view)
    return SourceReadRecords(view, service, _issued_query(service, scope), scope)


@contextmanager
def read_scope(service, *, request=None, reader=None):
    with source_scope(service, request=request, reader=reader) as query:
        if reader is None:
            scope = current_qualified_commit_scope() if query is not None else None
            context = (qualified_record_read(service.records, validate=scope._validate)
                if scope is not None else begin_read(service.records))
        else:
            context = nullcontext(reader)
        with context as transaction:
            yield bind_source_reader(service, transaction)


@dataclass(frozen=True)
class SourceOwnerSelection:
    reader: SourceReadRecords
    project_id: str
    namespace: str | None


def resolve_source_owner(application, binding, *, mode):
    if (mode != 'freshness_read' or type(binding) is not SourceOwnerSelection
            or type(binding.reader) is not SourceReadRecords or binding.reader.service.application is not application):
        raise RecognitionConflict('outcome_source_selection_unavailable')
    binding.reader.validate()
    query = binding.reader.query
    if (binding.project_id != query.origin_admission.lease.project_id
            or binding.namespace not in {None, query.source_namespace}):
        raise RecognitionConflict('outcome_source_namespace_changed')
    return query.material_owner


@contextmanager
def outcome_scope(service, *, project, document_id, reader=None):
    with service._read_scope(reader) as view:
        try:
            fact = service._fact(view, project, document_id)
        except RecognitionConflict as error:
            if str(error) not in {'outcome_native_owner_unavailable', 'outcome_completed_evidence_unavailable'}:
                raise
            fact = None
    if fact is None or not _shared_request(service, fact['native_request']):
        yield None
        return
    current = current_qualified_commit_scope()
    if current is None:
        if reader is not None:
            raise RecognitionConflict('outcome_source_scope_required_before_actor_transaction')
        from backend.security.user_context import USER_ACCESS
        access = USER_ACCESS.get()
        if access is None or access.target_user_id != service.owner['owner_user_id']:
            raise RecognitionConflict('outcome_human_source_actor_unavailable')
        query = _factory(service).query(service.application, access.caller, project)
        _query_current(query)
        query.answer_turns._runtime()
    else:
        query = _issued_query(service, current)
    with _qualified_commit(query.origin_admission) as qualification:
        context = (qualified_record_read(service.records, validate=qualification._validate)
            if reader is None else nullcontext(reader))
        with context as transaction:
            view = bind_source_reader(service, transaction)
            if service._fact(view, project, document_id) != fact:
                raise RecognitionConflict('outcome_input_binding_changed')
            public = view.read('v2_turns', fact['public_turn_id'])
            if public is None:
                raise RecognitionConflict('outcome_public_turn_unavailable')
            if fact['native_request']['desired_outcome'] == 'project.answer':
                from .shared_history import read_model_history_answer
                actual = read_model_history_answer(query, public.object_id, public.revision, reader=view)
                if actual['facts']['request'] != fact['native_request']:
                    raise RecognitionConflict('outcome_completed_evidence_changed')
            elif fact['native_request']['desired_outcome'] == 'project.task':
                from backend.recognition.product_draft_dependencies import product_draft_source
                from backend.recognition import WorkScope
                draft = product_draft_source(view, WorkScope('local-user', project), document_id, 1)
                if draft is None or draft.material_proof is None:
                    raise RecognitionConflict('outcome_shared_task_material_unavailable')
                service.shared_task_sources(view, draft)
            else:
                raise RecognitionConflict('outcome_native_kind_invalid')
        yield query
