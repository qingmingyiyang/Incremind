from collections import OrderedDict
from contextlib import contextmanager
from threading import RLock
from weakref import WeakSet

from backend.recognition import RecognitionConflict
from core.storage_provider.source_retrieval_index import index_store
from ..workspace_query import WorkspaceQuery
from ..source_egress import recognition_service
from .material_origin_admission import SharedOriginAdmission
from .material_origin_readers import ReadOnlyMaterialRecords
from .material_origins import ORIGIN_KIND, SharedMaterialOwner, SharedProjectLease
from .material_read_context import current_material_data
from .shared_project_constraint_input import SharedProjectConstraintInput, require_shared_reader_scope
from .shared_projects import PROJECTS


class SharedQueryFactory:
    def __init__(self, registry, devices):
        self.registry, self.devices = registry, devices
        self._queries, self._lock = OrderedDict(), RLock()
        self._issued_queries = WeakSet()

    def _issue(self, query):
        with self._lock:
            self._issued_queries.add(query)
        return query

    def query(self, application, actor, project_id, *, lease=None):
        if lease is not None:
            if lease.project_id != project_id:
                raise RecognitionConflict('material_origin_scope_changed')
            cached = None
            with self._lock:
                for key, query in self._queries.items():
                    issued = query.origin_admission
                    if issued.application is application and issued.actor == actor and issued.lease == lease:
                        self._queries.move_to_end(key)
                        cached = query
                        break
            if cached is not None:
                issued, personal = cached.origin_admission, cached.origin_admission.destination.workspace.query
                if (cached.execution_owner is not personal or cached.profile_owner is not personal
                        or cached.receipt_records is not issued.destination.records
                        or personal.records is not issued.destination.records or personal.execution_owner is not personal):
                    raise RecognitionConflict('material_origin_execution_owner_changed')
                cached.validate_retrieval_origin()
                return self._issue(cached)
            if current_material_data(self.registry, actor, lease) is not None:
                raise RecognitionConflict('material_origin_qualified_query_unavailable')
        admission = SharedOriginAdmission(self.registry, actor, application, project_id, lease=lease)
        admission.validate_execution_owner()
        admission.lease.validate(self.registry, actor)
        personal = admission.destination.workspace.query
        if (personal.models is not application.state.recognition_models
                or personal.records is not admission.destination.records
                or personal.execution_owner is not personal):
            raise RecognitionConflict('material_origin_execution_owner_changed')
        key = (id(application), actor.user_id, actor.device_id, actor.revision, admission.lease,
            admission.destination.namespace, admission.destination.root_identity)
        with self._lock:
            query = self._queries.get(key)
            if query is not None:
                self._queries.move_to_end(key)
        if query is not None:
            query.validate_retrieval_origin()
            return self._issue(query)
        source = SharedMaterialOwner(self.registry, actor, admission.lease)
        source_index = ReadOnlyMaterialRecords(index_store(source.sources).database_path,
            validate=source.validate_current)
        query = WorkspaceQuery(source.records, source.documents, source.sources, personal.models,
            recognition_service(source.records), read_only=True, source_index_records=source_index,
            profile_owner=personal, receipt_records=personal.records, execution_owner=personal,
            origin_admission=admission, source_readonly=True)
        query.answer_turns = personal.answer_turns
        query.outcome_freshness = personal.outcome_freshness
        query.origin_factory = self
        query.source_namespace = source.namespace
        query.material_owner = source
        query.project_constraint_input = SharedProjectConstraintInput(query, personal.project_constraint_input)
        admission.constraint_input = query.project_constraint_input
        # 缓存锁内只处理对象引用，来源租约和实际数据库读取保持在锁外。
        with self._lock:
            published = self._queries.setdefault(key, query)
            self._queries.move_to_end(key)
            while len(self._queries) > 128:
                self._queries.popitem(last=False)
        published.validate_retrieval_origin()
        return self._issue(published)

    def dispatch(self, application, request):
        from backend.security.user_context import USER_ACCESS
        bundle = application.state.ai_turn_store.restore_admission(request)
        items = [] if bundle is None else [item for item in bundle.items if item.kind == ORIGIN_KIND]
        if len(items) != 1:
            raise RecognitionConflict('material_origin_binding_missing')
        payload = items[0].payload
        if not isinstance(payload, dict) or not isinstance(payload.get('shared'), dict):
            raise RecognitionConflict('material_origin_binding_invalid')
        lease = SharedProjectLease.from_payload(payload['shared'].get('lease'))
        access = USER_ACCESS.get()
        if access is not None and (access.target_user_id != getattr(application.state, 'server_user_id', None)
                or access.caller.user_id != lease.actor_user_id or access.caller.device_id != lease.device_id
                or access.caller.revision != lease.device_revision):
            raise RecognitionConflict('material_origin_execution_caller_changed')
        actor = self.devices.identity_for_device(lease.device_id, lease.actor_user_id)
        if actor is None or actor.revision != lease.device_revision:
            raise RecognitionConflict('material_origin_identity_changed')
        query = self.query(application, actor, lease.project_id, lease=lease)
        query.origin_admission.restore(request)
        return query

    def bind_personal_dispatcher(self, application):
        query = application.state.workspace_domains.query
        if query.execution_owner is not query or query.origin_admission is not None:
            raise RecognitionConflict('material_origin_execution_owner_changed')
        existing = getattr(application.state, 'shared_query_factory', None)
        if existing is not None and existing is not self:
            raise RecognitionConflict('material_origin_execution_owner_changed')
        origins = application.state.workbench_route_origins
        examples = application.state.workbench_route_examples
        if (origins.application is not application or origins.records is not query.records
                or examples.records is not query.records or examples.models is not query.models
                or examples.origin_for is not origins):
            raise RecognitionConflict('material_origin_execution_owner_changed')
        if existing is None:
            if origins.shared_origins_for is not None:
                raise RecognitionConflict('material_origin_execution_owner_changed')
            origins.shared_origins_for = lambda request: self.dispatch(application, request).origin_admission.restore(request)
        application.state.shared_query_factory = self
        query.origin_dispatcher = lambda request: self.dispatch(application, request)
        query.project_constraint_input.shared_origin_resolver = self.resolve_constraints
        from .shared_mcp_context import install_shared_mcp
        install_shared_mcp(application, self)

    def incoming_query(self, application, project_id, *, scope_kind='personal'):
        from backend.security.user_context import USER_ACCESS
        from .turn_logical_scope import logical_scope
        logical_scope(scope_kind)
        personal = application.state.workspace_domains.query
        if getattr(application.state, 'shared_query_factory', None) is not self:
            raise RecognitionConflict('material_origin_execution_owner_changed')
        if scope_kind == 'personal':
            return personal
        with self.registry.records.begin_read() as central:
            shared = central.read(PROJECTS, project_id)
        if shared is None:
            raise RecognitionConflict('material_origin_scope_changed')
        access = USER_ACCESS.get()
        if access is None or access.target_user_id != application.state.server_user_id:
            raise RecognitionConflict('material_origin_execution_caller_changed')
        return self.query(application, access.caller, project_id)

    @contextmanager
    def publication_scope(self, application, public_id):
        from .shared_auxiliary import _qualified_commit
        request = application.state.ai_turn_store.get_request(public_id)
        if request is None or not any(ref.get('uri', '').startswith('crp://material-origin/')
                for ref in request['input']['refs']):
            yield None
            return
        query = self.dispatch(application, request)
        with _qualified_commit(query.origin_admission) as qualification:
            yield qualification

    def resolve_constraints(self, application, request, bundle, *, reader):
        if getattr(application.state, 'shared_query_factory', None) is not self or bundle is None:
            raise RecognitionConflict('project constraints shared factory unavailable')
        items = [item for item in bundle.items if item.kind == ORIGIN_KIND]
        if len(items) != 1:
            raise RecognitionConflict('material_origin_binding_missing')
        lease = SharedProjectLease.from_payload(items[0].payload.get('shared', {}).get('lease'))
        actor = self.devices.identity_for_device(lease.device_id, lease.actor_user_id)
        if actor is None or actor.revision != lease.device_revision:
            raise RecognitionConflict('material_origin_identity_changed')
        require_shared_reader_scope(reader, registry=self.registry, actor=actor, lease=lease)
        query = self.query(application, actor, lease.project_id, lease=lease)
        query.origin_admission._bind_bundle(request, bundle)
        return query.project_constraint_input.validate(request, reader=reader)
