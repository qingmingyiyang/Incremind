from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
import json
from pathlib import Path
from threading import get_ident

from backend.recognition import RecognitionConflict, WorkScope
from core.ai_kernel.product_admission import (
    ProductAdmissionItem, bundle_from_payload, bundle_to_payload, seal_admission, validate_bundle,
)
from core.ai_kernel.sqlite_store import SQLiteAITurnStore
from ..kernel.memory_turn import MemoryTurn as KernelMemoryTurn
from ..kernel.turn_requests import freeze_product_turn as assemble_product_turn
from ..original_sources import resolve_turn_material
from ..source_egress import SourceEgressService
from .material_origin_admission import BoundMaterialOrigins
from .material_origin_privacy import freeze_origin_materials, validate_origin_privacy
from .material_read_context import qualified_material_scope
from .material_origin_readers import qualified_record_read
from .turn_requests import _INSTRUCTIONS
from .privacy import egress_allowed
from ..privacy_policy import global_egress_allowed


PREPARED = 'v2_shared_auxiliary_admissions'
KEYS = 'v2_memory_turn_keys'
_COMMIT_SCOPE = ContextVar('shared_material_commit_scope', default=None)
_EXTERNAL_ADMISSION = ContextVar('shared_auxiliary_admission', default=None)
_COMMIT_SCOPES = {}


def _record_database(records, root):
    path = Path(records.database_path).resolve(strict=True)
    if not path.is_relative_to(Path(root).resolve(strict=True)) or not path.is_file():
        raise RecognitionConflict('material_origin_commit_database_changed')
    observed = path.stat()
    return path, (observed.st_dev, observed.st_ino)


class _QualifiedCommitScope:
    def __init__(self, admission, central, data, source_reader, validate_source):
        self._admission, self._central, self._data = admission, central, data
        self._source_reader, self._validate_source = source_reader, validate_source
        self._thread, self._token, self._active = get_ident(), object(), True
        destination = admission.destination
        self._actor_database = _record_database(destination.records, destination.root)

    def _validate(self):
        if (not self._active or self._thread != get_ident() or _COMMIT_SCOPE.get() is not self._token
                or _COMMIT_SCOPES.get(self._token) is not self or self._central.closed
                or not self._central.connection.in_transaction or self._source_reader.closed
                or not self._source_reader.connection.in_transaction):
            raise RecognitionConflict('material_origin_commit_scope_expired')
        admission, lease = self._admission, self._admission.lease
        admission.validate_execution_owner()
        admission.destination.validate()
        if _record_database(admission.destination.records, admission.destination.root) != self._actor_database:
            raise RecognitionConflict('material_origin_commit_database_changed')
        admission.registry.authorize(self._central, admission.actor, lease.project_id,
            authority={'project_revision': lease.project_revision, 'member_revision': lease.member_revision,
                'account_revision': lease.account_revision})
        self._validate_source()

    def _actor_reader(self, actor_reader):
        from backend.security.audited_records import _AuditedTransaction
        from backend.recognition.sql_source_identities import CollectingReader
        from core.storage_provider.sqlite_uow import SQLiteStructuredRecordUnitOfWork
        from ..transaction_records import TransactionRecords
        self._validate()
        if not isinstance(actor_reader, TransactionRecords):
            raise RecognitionConflict('material_origin_commit_actor_reader_changed')
        actual, seen = actor_reader._transaction, set()
        while isinstance(actual, (TransactionRecords, CollectingReader)):
            if id(actual) in seen:
                raise RecognitionConflict('material_origin_commit_actor_reader_changed')
            seen.add(id(actual))
            actual = actual._transaction if isinstance(actual, TransactionRecords) else actual._reader
        if (not isinstance(actual, (SQLiteStructuredRecordUnitOfWork, _AuditedTransaction))
                or actual.closed or actual.connection is not actor_reader.connection or not actor_reader.connection.in_transaction
                or Path(actor_reader.database_path).resolve(strict=True)
                    != Path(self._admission.destination.records.database_path).resolve(strict=True)):
            raise RecognitionConflict('material_origin_commit_actor_reader_changed')
        return actor_reader

    def reader_for(self, origins, owner, *, actor_reader):
        self._actor_reader(actor_reader)
        if not isinstance(origins, BoundMaterialOrigins) or origins.admission is not self._admission:
            raise RecognitionConflict('material_origin_commit_owner_changed')
        if owner is origins.shared:
            if (owner.namespace != self._admission.lease.namespace
                    or owner.project_id != self._admission.lease.project_id
                    or Path(owner.records.database_path).resolve(strict=True)
                        != Path(self._data.records.database_path).resolve(strict=True)):
                raise RecognitionConflict('material_origin_commit_owner_changed')
            return self._source_reader
        personal_project = ('me' if owner is origins.profile else
            self._admission.lease.project_id if owner is origins.actor else None)
        if (personal_project is None or owner.namespace != self._admission.personal['namespace']
                or owner.project_id != personal_project
                or Path(owner.records.database_path).resolve(strict=True) != self._actor_database[0]):
            raise RecognitionConflict('material_origin_commit_owner_changed')
        return actor_reader

    def material_data(self, registry, actor, lease):
        if (registry is not self._admission.registry or actor != self._admission.actor
                or lease != self._admission.lease):
            raise RecognitionConflict('material_origin_commit_owner_changed')
        self._validate()
        return self._data

    def source_records(self, owner, reader):
        from backend.recognition.sql_source_identities import CollectingReader
        from ..transaction_records import TransactionRecords
        from .material_origins import SharedMaterialOwner
        from .shared_runtime import _SharedWriteTransaction
        self._validate()
        admission = self._admission
        actual = reader
        while isinstance(actual, (TransactionRecords, CollectingReader, _SharedWriteTransaction)):
            if isinstance(actual, TransactionRecords):
                actual = actual._transaction
            elif isinstance(actual, CollectingReader):
                actual = actual._reader
            else:
                actual = actual.transaction
        if (not isinstance(owner, SharedMaterialOwner) or owner.registry is not admission.registry
                or owner.actor != admission.actor or owner.lease != admission.lease
                or owner.namespace != admission.lease.namespace or actual is not self._source_reader):
            raise RecognitionConflict('material_origin_commit_source_reader_changed')
        owner.validate_current()
        self._validate()
        return self._data.records, self._data.sources

    def validate_sql(self, identities, *, actor_reader):
        from .material_origin_sql import _validate_commit_sql
        self._actor_reader(actor_reader)
        def validate():
            self._actor_reader(actor_reader)
        _validate_commit_sql(identities, {
            self._admission.lease.namespace: self._source_reader.connection,
            self._admission.personal['namespace']: actor_reader.connection}, validate)


@contextmanager
def _commit_scope(admission, central, data, source_reader, validate_source):
    scope = _QualifiedCommitScope(admission, central, data, source_reader, validate_source)
    _COMMIT_SCOPES[scope._token] = scope
    token = _COMMIT_SCOPE.set(scope._token)
    try:
        with qualified_material_scope(scope):
            scope._validate()
            yield scope
    finally:
        scope._active = False
        _COMMIT_SCOPES.pop(scope._token, None)
        _COMMIT_SCOPE.reset(token)


def shared_auxiliary_key(admission, key, materials, purpose):
    from .material_origin_sql import freeze_sql_identities
    proof = []
    origins = BoundMaterialOrigins(admission, admission.lease, {})
    for material in materials:
        if not isinstance(material, dict) or set(material) != {'type', 'id', 'revision', 'project_id', 'namespace'}:
            raise RecognitionConflict('material_origin_scope_changed')
        owner = origins.owner(material['project_id'], material['namespace'])
        scope = WorkScope('local-user', owner.project_id)
        descriptor = {field: material[field] for field in ('type', 'id', 'revision', 'project_id')}
        resolved, roots = resolve_turn_material(owner.records, scope, descriptor)
        proof.append({'namespace': owner.namespace, 'ref': resolved['ref'],
            'snapshot': SourceEgressService(owner.records).snapshot(scope, roots)})
    return json.dumps({'schema_version': 2, 'key': key, 'purpose': purpose, 'origin': admission.lease.payload(),
        'execution': admission.personal, 'materials': list(materials), 'proof': proof,
        'sql_identities': freeze_sql_identities(origins, materials)}, ensure_ascii=False, sort_keys=True)


@contextmanager
def _qualified_commit(admission):
    from .shared_runtime import current_shared_read_capability
    lease, registry, actor = admission.lease, admission.registry, admission.actor
    admission._store()
    current = _COMMIT_SCOPE.get()
    if current is not None:
        existing = _COMMIT_SCOPES.get(current)
        if existing is None or existing._admission is not admission:
            raise RecognitionConflict('material_origin_commit_owner_changed')
        existing._validate()
        yield existing
        existing._validate()
        return
    capability = current_shared_read_capability(registry=registry)
    if capability is not None:
        if capability.actor != actor or capability.lease_payload() != lease.payload():
            raise RecognitionConflict('material_origin_binding_changed')
        with _commit_scope(admission, capability._writer._central, capability._data,
                capability._writer.transaction, capability.validate) as scope:
            yield scope
        return
    authority = {'project_revision': lease.project_revision, 'member_revision': lease.member_revision,
                 'account_revision': lease.account_revision}
    with registry.records.begin() as central:
        registry.authorize(central, actor, lease.project_id, authority=authority)
        with registry.roots.lease(lease.project_id):
            data = registry.roots._data(lease.project_id)
            if data.namespace != lease.data_namespace or tuple(data.root_identity) != lease.root_identity:
                raise RecognitionConflict('material_origin_root_changed')
            admission.destination.validate()
            database = _record_database(data.records, data.root)
            def validate_location():
                from .server_deletion import _directory
                if central.closed or not central.connection.in_transaction:
                    raise RecognitionConflict('material_origin_commit_scope_expired')
                admission.validate_execution_owner()
                registry.authorize(central, actor, lease.project_id, authority=authority)
                observed = _directory(data.root)
                if ((observed.st_dev, observed.st_ino) != lease.root_identity
                        or data.namespace != lease.data_namespace
                        or _record_database(data.records, data.root) != database):
                    raise RecognitionConflict('material_origin_root_changed')
            with qualified_record_read(data.records, validate=validate_location) as reader:
                marker = reader.read('v2_shared_root', 'identity')
                def validate_source():
                    validate_location()
                    if (marker is None or reader.read('v2_shared_root', 'identity') != marker
                            or marker.payload.get('project_id') != lease.project_id):
                        raise RecognitionConflict('material_origin_root_changed')
                with _commit_scope(admission, central, data, reader, validate_source) as scope:
                    yield scope


def current_qualified_commit_scope():
    token = _COMMIT_SCOPE.get()
    if token is None:
        return None
    scope = _COMMIT_SCOPES.get(token)
    if scope is None:
        raise RecognitionConflict('material_origin_commit_scope_expired')
    scope._validate()
    return scope


class _PreparedTransaction:
    def __init__(self, transaction, owner):
        self.transaction, self.owner = transaction, owner

    def __getattr__(self, name):
        return getattr(self.transaction, name)

    def put(self, collection, identity, payload, *, expected_revision):
        if collection == KEYS:
            request = payload['request']
            bundle = self.owner.pending.get(identity)
            if bundle is None:
                raise RecognitionConflict('shared_auxiliary_preparation_missing')
            validate_bundle(request, bundle)
            self.transaction.put(PREPARED, identity, {'request': deepcopy(request), 'bundle': bundle_to_payload(bundle)},
                                 expected_revision=0)
        return self.transaction.put(collection, identity, payload, expected_revision=expected_revision)


class _ActorRecords:
    def __init__(self, admission, *, automatic=None):
        self.admission, self.pending = admission, {}
        self.records = admission.destination.records
        self.automatic = None
        if automatic is not None:
            self.bind_automatic(automatic)

    def bind_automatic(self, binding):
        from .automatic_constraints import AutomaticGenerationBinding
        from .shared_project_constraint_input import SharedProjectConstraintInput
        if (not isinstance(binding, AutomaticGenerationBinding)
                or binding.owner.records is not self.records
                or binding.owner.models is not self.admission.application.state.recognition_models
                or not isinstance(binding.owner.input, SharedProjectConstraintInput)
                or binding.owner.input._shared_origin is not self.admission
                or self.automatic is not None and self.automatic is not binding):
            raise RecognitionConflict('shared_auxiliary_automatic_owner_changed')
        self.automatic = binding

    def __getattr__(self, name):
        return getattr(self.records, name)

    @contextmanager
    def begin(self):
        with _qualified_commit(self.admission), self.records.begin() as transaction:
            yield _PreparedTransaction(transaction, self)


class _AdmittedStore(SQLiteAITurnStore):
    def __init__(self, records, admission):
        self.actor_records, self.origin_admission = records, admission
        super().__init__(admission._store()._path)

    @contextmanager
    def admission_bundle(self, request, bundle):
        validate_bundle(request, bundle)
        if _EXTERNAL_ADMISSION.get() is not None:
            raise RecognitionConflict('nested shared auxiliary admission is forbidden')
        row = self.actor_records.read(PREPARED, request['turn_id'])
        if (row is None or row.payload.get('request') != request
                or row.payload.get('bundle') != bundle_to_payload(bundle)):
            raise RecognitionConflict('shared_auxiliary_preparation_changed')
        # 外层只保存原完整证明；实际注册由 claim 在来源和操作人事务内完成。
        token = _EXTERNAL_ADMISSION.set((self, deepcopy(request), deepcopy(bundle)))
        try:
            yield
        finally:
            _EXTERNAL_ADMISSION.reset(token)

    def claim_turn(self, request):
        # 准入注册仅覆盖原生 claim，模型运行继续使用原执行生命周期。
        self.actor_records.validate_request(request)
        external = _EXTERNAL_ADMISSION.get()
        if external is not None and (external[0] is not self or external[1] != request):
            raise RecognitionConflict('shared_auxiliary_admission_request_changed')
        turn = self.get_request(request['turn_id'])
        if turn is not None:
            origins = self.origin_admission.restore(request)
            return self._claim_with_source_sql(request, origins, bundle=external[2] if external is not None else None)
        row = self.actor_records.read(PREPARED, request['turn_id'])
        if row is None or row.payload.get('request') != request:
            raise RecognitionConflict('shared_auxiliary_preparation_missing')
        bundle = bundle_from_payload(row.payload['bundle'])
        validate_bundle(request, bundle)
        if external is not None and bundle_to_payload(external[2]) != bundle_to_payload(bundle):
            raise RecognitionConflict('shared_auxiliary_preparation_changed')
        origins = self.origin_admission._bind_bundle(request, bundle)
        return self._claim_with_source_sql(request, origins, bundle=bundle)

    def _claim_with_source_sql(self, request, origins, *, bundle=None):
        from ..transaction_records import TransactionRecords
        if (not isinstance(origins, BoundMaterialOrigins) or origins.admission is not self.origin_admission
                or origins.auxiliary is None):
            raise RecognitionConflict('material_origin_auxiliary_changed')
        identities = origins.auxiliary['selection'].get('sql_identities', [])
        # 接纳和重复接纳都在实际中央资格、来源与操作者事务内重核冻结身份。
        with _qualified_commit(self.origin_admission) as scope:
            with qualified_record_read(self.origin_admission.destination.records, validate=scope._validate) as actor_reader:
                reader = TransactionRecords(actor_reader)
                scope.validate_sql(identities, actor_reader=reader)
                if origins.auxiliary is not None and 'search_input' in origins.auxiliary:
                    from .shared_search_input import validate_search_claim_sql
                    validate_search_claim_sql(origins, request, scope, reader)
                from .automatic_constraint_input import constraint_markers
                if constraint_markers(request) and self.actor_records.automatic is None:
                    raise RecognitionConflict('shared_auxiliary_automatic_binding_missing')
                if self.actor_records.automatic is not None:
                    self.actor_records.automatic.validate_current(request, self, reader=reader)
                if bundle is None:
                    return super().claim_turn(request)
                with super().admission_bundle(request, bundle):
                    return super().claim_turn(request)


class _AutomaticSourceInput:
    def __init__(self, turn, binding):
        self.turn, self.binding = turn, binding

    @staticmethod
    def _reader(reader):
        return reader.transaction if isinstance(reader, _PreparedTransaction) else reader

    def prepare(self, request, store, reader, *, producer_identity):
        additions = self.binding.collect(request, store, self._reader(reader), producer_identity=producer_identity)
        items = self.turn.actor_records.pending_items.pop(request['turn_id'])
        bundle = self.turn._seal(request, (*items, *additions))
        self.turn._validate_request(self.turn.actor_records, self.turn.models, request, purpose=self.turn.purpose)
        return bundle

    def stage(self, request, store, bundle, reader):
        return self.binding.stage(request, store, bundle, self._reader(reader))

    def validate_commit(self, request, store, reader):
        return self.binding.validate_commit(request, store, self._reader(reader))

    def __getattr__(self, name):
        return getattr(self.binding, name)


class SharedAuxiliaryTurn(KernelMemoryTurn):
    @staticmethod
    def store_for(records):
        if not isinstance(records, _ActorRecords):
            raise RecognitionConflict('shared_auxiliary_execution_owner_missing')
        return _AdmittedStore(records, records.admission)

    def __init__(self, admission, models, *, materials, key, purpose='generation', project=None, text='',
                 rendered_input=None, automatic=None, _predecessor=None, **kwargs):
        if project is not None and project != admission.lease.project_id:
            raise RecognitionConflict('material_origin_scope_changed')
        if models is not admission.application.state.recognition_models:
            raise RecognitionConflict('shared_auxiliary_model_owner_changed')
        self.origin_admission = admission
        self.text, self.rendered_input = text, rendered_input
        self.rendered_payload, self.rendered_binding = None, None
        if rendered_input is not None:
            from .shared_rendered_preparation import PreparedRenderedWireInput
            from .shared_task_division_vectors import TaskDivisionWireInput
            if (not isinstance(rendered_input, PreparedRenderedWireInput)
                    or rendered_input.query.origin_admission is not admission or purpose not in {'embedding', 'rerank'}
                    or isinstance(rendered_input, TaskDivisionWireInput)
                        and (type(rendered_input) is not TaskDivisionWireInput or purpose != 'embedding')):
                raise RecognitionConflict('shared_rendered_input_owner_changed')
            self.rendered_binding = rendered_input.binding
            self.rendered_payload = deepcopy(self.rendered_binding['wire_payload'])
            key = {'schema_version': 3, 'key': key, 'rendered_input': self.rendered_binding}
        self.actor_records = _ActorRecords(admission)
        self.actor_records.pending_items = {}
        content = None
        if automatic is not None:
            from .automatic_constraints import AutomaticConstraintAdmission
            from .shared_project_constraint_input import SharedProjectConstraintInput
            constraints = getattr(admission, 'constraint_input', None)
            if (purpose != 'generation' or not isinstance(constraints, SharedProjectConstraintInput)
                    or constraints._shared_origin is not admission):
                raise RecognitionConflict('automatic_constraint_shared_owner_missing')
            binding = AutomaticConstraintAdmission(constraints).bind(admission.destination.records, models,
                kind=kwargs['kind'], project=admission.lease.project_id, spec=automatic)
            content = _AutomaticSourceInput(self, binding)
        elif getattr(models, 'product_automatic_constraints', None) is not None and purpose == 'generation':
            from .automatic_constraint_input import PRODUCERS
            if kwargs['kind'] in {row[0] for row in PRODUCERS.values()}:
                raise RecognitionConflict('automatic_constraint_producer_missing')
        self.actor_records.validate_request = lambda request: self._validate_request(
            self.actor_records, models, request, purpose=purpose)
        if content is not None:
            self.actor_records.bind_automatic(content.binding)
        self.qualified_materials = deepcopy(list(materials))
        if _predecessor is None:
            key = shared_auxiliary_key(admission, key, self.qualified_materials, purpose)
        elif (not isinstance(_predecessor, SharedAuxiliaryTurn) or _predecessor.origin_admission is not admission
                or _predecessor.models is not models or _predecessor.qualified_materials != self.qualified_materials
                or purpose != 'generation' or kwargs['kind'] != 'memory.propose_insights'
                or not key.startswith(_predecessor._identity['key'] + ':retry-of:')):
            raise RecognitionConflict('shared_auxiliary_retry_owner_changed')
        super().__init__(self.actor_records, models, materials=self.qualified_materials, key=key,
            project=admission.lease.project_id, purpose=purpose, freeze_request=self._freeze,
            validate_request=self._validate_request, remote_allowed=self._remote_allowed, admission=content, **kwargs)

    def _new_attempt(self, key, *, retry_token=None):
        # 后继沿原共享 owner 重新准备；已限定的原身份不再追加第二份来源键。
        spec = self.admission.binding.spec if self.admission is not None else None
        return SharedAuxiliaryTurn(self.origin_admission, self.models, kind=self._identity['kind'],
            project=self._identity['project'], key=key, materials=self.qualified_materials,
            validate=self.validate_domain, purpose=self.purpose, text=self.text,
            automatic=spec, retry_token=retry_token, _predecessor=self)

    def _seal(self, request, items):
        bundle = seal_admission(request, items, self.origin_admission._store())
        self.actor_records.pending[request['turn_id']] = bundle
        return bundle

    def _remote_allowed(self, records, models, project, purpose):
        self.origin_admission.lease.validate(self.origin_admission.registry, self.origin_admission.actor)
        with self.origin_admission.lease.current(self.origin_admission.registry, self.origin_admission.actor) as source:
            return (global_egress_allowed(models, purpose)
                    and egress_allowed(source.records, models, project, purpose))

    def _freeze(self, kind, *, records, models, project_id, materials, load_text, **options):
        selected_proof = {}
        rendered_capsule = None
        reference = self.origin_admission._reference({'turn_id': options['turn_id']})
        origins = BoundMaterialOrigins(self.origin_admission, self.origin_admission.lease, reference)
        def freeze(_records, _models, project, selected, **privacy_options):
            nonlocal rendered_capsule
            from .material_origin_sql import freeze_sql_identities
            # 请求 ID 已由原辅助 Turn 分配；材料允许集合在实际生成前再次核验。
            allowed, privacy, selection = freeze_origin_materials(origins, models, project, selected, **privacy_options)
            selected_proof.update(selection)
            if self.rendered_input is None:
                selected_proof.update(schema_version=2, sql_identities=freeze_sql_identities(origins, selection['origin_materials']))
            else:
                rendered_capsule = self.rendered_input.capsule(turn_id=options['turn_id'], reference=reference,
                    materials=selection['origin_materials'])
                selected_proof.update(schema_version=3, rendered_input_ref=deepcopy(reference),
                    sql_identities=freeze_sql_identities(origins, selection['origin_materials'],
                        query=self.rendered_input.query, prepared_input=self.rendered_input))
                origins.auxiliary = {'purpose': options['model_purpose'], 'materials': deepcopy(selection['origin_materials']),
                    'selection': deepcopy(selected_proof), 'rendered_input': deepcopy(rendered_capsule)}
            return allowed, privacy
        def validate(_records, _models, request, *, purpose):
            validate_origin_privacy(origins, models, project_id, request['privacy'],
                                    purpose=purpose, selection=selected_proof)
        request = assemble_product_turn(kind, records=records, models=models, project_id=project_id,
            materials=materials, load_text=load_text, freeze_materials=freeze,
            validate_request=validate, instruction=_INSTRUCTIONS.get(kind, self.text), text=self.text, **options)
        items = self.origin_admission.prepare(request)
        sources = [item for item in items if item.kind == 'material-origin-v1']
        if len(sources) != 1:
            raise RecognitionConflict('material_origin_binding_missing')
        item = sources[0]
        payload = {**item.payload, 'auxiliary': {'purpose': options['model_purpose'],
            'materials': deepcopy(selected_proof['origin_materials']), 'selection': deepcopy(selected_proof),
            **({'rendered_input': deepcopy(rendered_capsule)} if rendered_capsule is not None else {})}}
        items = tuple(ProductAdmissionItem(item.kind, item.ref, item.scope, payload)
            if entry.kind == item.kind else entry for entry in items)
        if self.admission is not None:
            self.actor_records.pending_items[request['turn_id']] = items
            return request
        self._seal(request, items)
        self._validate_request(records, models, request, purpose=options['model_purpose'])
        return request

    def _validate_request(self, records, models, request, *, purpose):
        if self.origin_admission._store().get_request(request['turn_id']) is not None:
            origins = self.origin_admission.restore(request)
        else:
            row = records.read(PREPARED, request['turn_id'])
            bundle = (bundle_from_payload(row.payload['bundle']) if row is not None
                      else records.pending.get(request['turn_id']))
            if bundle is None or row is not None and row.payload.get('request') != request:
                raise RecognitionConflict('shared_auxiliary_preparation_missing')
            validate_bundle(request, bundle)
            origins = self.origin_admission._bind_bundle(request, bundle)
        auxiliary = origins.auxiliary
        if (auxiliary is None or auxiliary['purpose'] != purpose
                or auxiliary['materials'] != self.qualified_materials):
            raise RecognitionConflict('material_origin_auxiliary_changed')
        if self.rendered_payload is not None:
            from .shared_rendered_input import read_rendered_capsule
            capsule = read_rendered_capsule(request, auxiliary, reference=origins.reference)
            if ({key: capsule[key] for key in ('purpose', 'wire_payload', 'items')} != self.rendered_binding
                    or capsule['wire_payload'] != self.rendered_payload):
                raise RecognitionConflict('shared_rendered_input_changed')
        validate_origin_privacy(origins, models, request['scope']['project_id'], request['privacy'],
                                purpose=purpose, selection=auxiliary['selection'])


def shared_auxiliary_factory(admission):
    def create(records, models, **options):
        if records is not admission.destination.records:
            raise RecognitionConflict('shared_auxiliary_execution_owner_changed')
        return SharedAuxiliaryTurn(admission, models, **options)
    return create
