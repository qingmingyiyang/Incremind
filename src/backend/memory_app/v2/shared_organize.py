from contextlib import contextmanager
from copy import deepcopy
import json
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from backend.recognition import RecognitionConflict
from core.ai_kernel.product_admission import ProductAdmissionItem, bundle_from_payload, bundle_to_payload, seal_admission, validate_bundle
from core.storage_provider.persistence_budget import persistence_budget
from ..kernel.turn_requests import freeze_product_turn
from ..server_jobs import _IntakeBudget
from ..transaction_records import TransactionRecords
from .automatic_constraints import AutomaticConstraintAdmission
from .material_origin_admission import BoundMaterialOrigins
from .material_origin_privacy import freeze_origin_materials, validate_origin_privacy
from .material_origin_readers import qualified_record_read
from .material_origin_sql import freeze_sql_identities
from .shared_auxiliary import PREPARED, _ActorRecords, _AdmittedStore, _qualified_commit, current_qualified_commit_scope
from .shared_rendered_preparation import _query_current
from .shared_projects import PROJECTS, SharedProjectError
from .shared_runtime import SharedWriteRecords, shared_read_capability


class _OrganizationAdmission:
    def __init__(self, source, binding):
        self.source, self.binding = source, binding

    def prepare(self, request, store, reader, *, producer_identity, previous_producer=None):
        additions = self.binding.collect(request, store, reader,
            producer_identity=producer_identity, previous_producer=previous_producer)
        items = self.source._pending_items.pop(request['turn_id'])
        bundle = seal_admission(request, (*items, *additions), store)
        self.source.actor_records.pending[request['turn_id']] = bundle
        return bundle

    def stage(self, request, store, bundle, reader):
        self.binding.stage(request, store, bundle, reader)
        reader.put(PREPARED, request['turn_id'], {'request': deepcopy(request),
            'bundle': bundle_to_payload(bundle)}, expected_revision=0)

    def __getattr__(self, name):
        operation = getattr(self.binding, name)
        if not callable(operation):
            return operation
        def qualified(*args, **kwargs):
            scope = current_qualified_commit_scope()
            if scope is None and (kwargs.get('reader') is not None or name == 'validate_commit'):
                raise RecognitionConflict('shared_organize_source_scope_required')
            with _qualified_commit(self.source.admission):
                return operation(*args, **kwargs)
        return qualified


class SharedOrganizeSource:
    def __init__(self, query, items, *, item_id, project_id, run_id, actor_budget):
        _query_current(query)
        admission = query.origin_admission
        if (project_id != admission.lease.project_id or not isinstance(items.records, SharedIntakeRecords)
                or items.records.registry is not admission.registry
                or items.records.actor != admission.actor or items.records.project_id != project_id
                or not isinstance(actor_budget, _IntakeBudget)
                or actor_budget.scheduler is not admission.application.state.server_context.jobs
                or actor_budget.user_id != admission.destination.owner_user_id or not actor_budget.active):
            raise RecognitionConflict('shared_organize_owner_changed')
        self.query, self.admission, self.items = query, admission, items
        self.item_id, self.project_id, self.run_id, self.actor_budget = item_id, project_id, run_id, actor_budget
        self.records, self.models = admission.destination.records, query.models
        self.storage_key = item_id + '-shared-' + uuid5(NAMESPACE_URL,
            '/'.join((query.source_namespace, admission.personal['namespace'], item_id))).hex
        self.actor_records = _ActorRecords(admission)
        self.actor_records.validate_request = self.validate_request
        self.store = _AdmittedStore(self.actor_records, admission)
        self._pending_items = {}

    def validate(self):
        _query_current(self.query)
        self.admission.validate_execution_owner()
        if not self.actor_budget.active:
            raise RecognitionConflict('shared_organize_admission_expired')

    def constraints(self):
        self.validate()
        binding = AutomaticConstraintAdmission(self.query.project_constraint_input).bind(self.records, self.models,
            kind='memory.organize', project=self.project_id,
            spec={'producer': 'item-organize', 'mode': 'object', 'scene': None,
                  'anchors': [{'type': 'item', 'id': self.item_id}]})
        self.actor_records.bind_automatic(binding)
        return _OrganizationAdmission(self, binding)

    @contextmanager
    def source_reader(self, actor_reader=None):
        if actor_reader is not None:
            scope = current_qualified_commit_scope()
            if scope is None or scope._admission is not self.admission:
                raise RecognitionConflict('shared_organize_source_scope_required')
            origins = BoundMaterialOrigins(self.admission, self.admission.lease, {})
            yield scope.reader_for(origins, origins.shared, actor_reader=TransactionRecords(actor_reader))
            scope._actor_reader(TransactionRecords(actor_reader))
            return
        self.validate()
        with _qualified_commit(self.admission) as scope:
            origins = BoundMaterialOrigins(self.admission, self.admission.lease, {})
            with qualified_record_read(self.records, validate=scope._validate) as transaction:
                yield scope.reader_for(origins, origins.shared, actor_reader=TransactionRecords(transaction))

    def item(self, actor_reader=None):
        with self.source_reader(actor_reader) as reader:
            command = self.items.records.command_source
            row = (command.read(reader, self.query) if command is not None
                   else reader.read('workspace_items', self.item_id))
            if row is None or row.payload.get('project_id') != self.project_id:
                raise RecognitionConflict('shared_organize_source_changed')
            return row

    @contextmanager
    def write(self):
        self.validate()
        row = self.item()
        cap = self.actor_budget.reserve_up_to(32 * len(str(row.payload.get('source_text', '')).encode('utf-8')) + 1048576)
        try:
            with _qualified_commit(self.admission) as scope, self.records.begin() as transaction:
                with cap.using_reader(scope._central), persistence_budget(root=self.admission.destination.root,
                        databases=(self.records.database_path,), consume=cap.consume):
                    self.guard(transaction)
                    yield transaction
        finally:
            cap.close()

    def guard(self, actor_reader):
        with self.source_reader(actor_reader) as reader:
            command = self.items.records.command_source
            if command is not None:
                command.read(reader, self.query)
            self.items.processing_lease._owned(reader, self.item_id, self.project_id, self.run_id)

    def freeze(self, kind, *, records, models, project_id, materials, load_text, **options):
        self.validate()
        if records is not self.records or models is not self.models or project_id != self.project_id or kind != 'memory.organize':
            raise RecognitionConflict('shared_organize_owner_changed')
        row = self.item()
        expected = [{'type': 'original_item', 'id': self.item_id, 'revision': row.revision, 'project_id': self.project_id}]
        if list(materials) != expected:
            raise RecognitionConflict('shared_organize_source_changed')
        expected_messages = self.messages(row)
        if json.loads(load_text(materials[0])) != expected_messages:
            raise RecognitionConflict('shared_organize_messages_changed')
        reference = self.admission._reference({'turn_id': options['turn_id']})
        origins = BoundMaterialOrigins(self.admission, self.admission.lease, reference)
        selected = [{**material, 'namespace': self.query.source_namespace} for material in materials]
        selection = {}
        def freeze_materials(_records, _models, project, values, **privacy_options):
            if list(values) != list(materials):
                raise RecognitionConflict('shared_organize_source_changed')
            allowed, privacy, actual = freeze_origin_materials(origins, models, project, selected, **privacy_options)
            selection.update(actual, schema_version=2, sql_identities=freeze_sql_identities(origins, actual['origin_materials']))
            return allowed, privacy
        def validate(_records, _models, request, *, purpose):
            validate_origin_privacy(origins, models, project_id, request['privacy'], purpose=purpose, selection=selection)
        request = freeze_product_turn(kind, records=records, models=models, project_id=project_id,
            materials=materials, load_text=load_text, freeze_materials=freeze_materials, validate_request=validate,
            instruction='整理以下原件。', **options)
        items = self.admission.prepare(request)
        sources = [item for item in items if item.kind == 'material-origin-v1']
        if len(sources) != 1:
            raise RecognitionConflict('material_origin_binding_missing')
        original = sources[0]
        payload = {**original.payload, 'auxiliary': {'purpose': 'generation',
            'materials': deepcopy(selection['origin_materials']), 'selection': deepcopy(selection)}}
        self._pending_items[request['turn_id']] = tuple(
            ProductAdmissionItem(item.kind, item.ref, item.scope, payload) if item is original else item for item in items)
        return request

    @staticmethod
    def messages(row):
        from ..workspace_generation import _PROMPT
        if row.payload.get('input_kind') not in {'text', 'conversation'}:
            raise RecognitionConflict('shared_organize_source_kind_unavailable')
        return [{'role': 'system', 'content': _PROMPT}, {'role': 'user', 'content': row.payload['source_text']}]

    def validate_request(self, request, *, actor_reader=None):
        self.validate()
        if self.store.get_request(request['turn_id']) is not None:
            origins = self.admission.restore(request)
        else:
            row = (self.records if actor_reader is None else actor_reader).read(PREPARED, request['turn_id'])
            bundle = bundle_from_payload(row.payload['bundle']) if row is not None else self.actor_records.pending.get(request['turn_id'])
            if bundle is None or row is not None and row.payload.get('request') != request:
                raise RecognitionConflict('shared_organize_preparation_missing')
            validate_bundle(request, bundle)
            origins = self.admission._bind_bundle(request, bundle)
        auxiliary = origins.auxiliary
        if (auxiliary is None or auxiliary['purpose'] != 'generation'
                or request['desired_outcome'] != 'memory.organize'
                or len(auxiliary['materials']) != 1
                or auxiliary['materials'][0].get('type') != 'original_item'
                or auxiliary['materials'][0].get('project_id') != self.project_id
                or auxiliary['materials'][0].get('id') != self.item_id
                or auxiliary['materials'][0].get('namespace') != self.query.source_namespace):
            raise RecognitionConflict('shared_organize_source_changed')
        validate_origin_privacy(origins, self.models, self.project_id, request['privacy'],
            selection=auxiliary['selection'])
        expected_text = '整理以下原件。\n\n' + json.dumps(self.messages(self.item(actor_reader)), ensure_ascii=False)
        if request['input']['text'] != expected_text:
            raise RecognitionConflict('shared_organize_messages_changed')
        if actor_reader is not None:
            self.guard(actor_reader)


class SharedIntakeRecords(SharedWriteRecords):
    def __init__(self, query, owner_user_id, cap, *, action, identity, title, command_source=None):
        from .shared_intake import SharedConversationItemSource, SharedIntakeCommandSource
        if command_source is not None and (type(command_source) not in {SharedIntakeCommandSource, SharedConversationItemSource}
                or command_source.item_id != identity):
            raise SharedProjectError('shared_intake_command_changed', status=409)
        admission, lease = query.origin_admission, query.origin_admission.lease
        super().__init__(admission.registry, admission.actor, lease.project_id,
            authority={'project_revision': lease.project_revision, 'member_revision': lease.member_revision,
                'account_revision': lease.account_revision},
            action=action, object_kind='source', object_id=identity, object_title=title)
        self.query, self.owner_user_id, self.cap = query, owner_user_id, cap
        self.command_source = command_source
        self._database_path = Path(query.records.database_path)

    @property
    def database_path(self):
        return self._database_path

    @contextmanager
    def begin(self):
        with super().begin() as tx:
            capability = shared_read_capability(TransactionRecords(tx), registry=self.registry)
            data, lease = capability._data, self.query.origin_admission.lease
            project = tx._central.read(PROJECTS, self.project_id)
            if (capability.lease_payload() != lease.payload() or project.payload['owner_user_id'] != self.owner_user_id
                    or data.namespace != lease.data_namespace or data.root_identity != lease.root_identity
                    or Path(next(row[2] for row in tx.connection.execute('PRAGMA database_list') if row[1] == 'main')).resolve()
                        != self._database_path.resolve()):
                raise SharedProjectError('shared_intake_owner_changed', status=409)
            with self.cap.using_reader(tx._central), persistence_budget(root=data.root,
                    databases=(self.database_path,), consume=self.cap.consume):
                if self.command_source is not None:
                    self.command_source.read(TransactionRecords(tx), self.query)
                yield tx


class SharedOrganizeFactory:
    def __init__(self, query, actor_budget):
        _query_current(query)
        self.query, self.actor_budget = query, actor_budget

    def create(self, intake, *, item_id, project_id, source, validate_current, lease, run_id):
        from .organize_turns import OrganizeTurns
        context = SharedOrganizeSource(self.query, intake.items, item_id=item_id,
            project_id=project_id, run_id=run_id, actor_budget=self.actor_budget)
        return OrganizeTurns(root=context.admission.destination.root, records=context.records,
            models=context.models, item_id=item_id, project_id=project_id, source=source,
            validate_current=validate_current, lease=lease, run_id=run_id, source_context=context)

    def publish(self, organize, intake, *, item_id, project_id, run_id, source, draft, processing_seconds, metadata):
        from ..workspace_generation import _draft, _strip_fence, _ground_local_draft, _is_local_model
        from .organize_turns import STEPS
        from core.storage_provider.record_lineage import capture_lineage
        if (organize.source_context is None or organize.source_context.query is not self.query
                or organize.item_id != item_id or organize.project_id != project_id
                or organize.run_id != run_id or organize.source != source
                or len(organize.generation_requests) != 1):
            raise RecognitionConflict('shared_organize_result_changed')
        request = next(iter(organize.generation_requests.values()))
        with intake.items.records.begin() as tx:
            with _qualified_commit(self.query.origin_admission):
                with organize.records.begin() as actor_reader:
                    organize.validate_commit(actor_reader)
                    step = actor_reader.read(STEPS, 'organize-' + organize.storage_key + '-1')
                    immutable = organize.store.get_immutable_payload(request['turn_id'], 'organize-model-result-v1')
                    events = tuple(organize.store.events_after(request['turn_id']))
                    if (step is None or step.payload.get('turn_id') != request['turn_id']
                            or step.payload.get('rejected') or 'output' not in step.payload
                            or not events or events[-1]['type'] != 'turn.completed'
                            or not any(event['type'] == 'model.completed' for event in events)
                            or immutable is None or events[-1]['data'].get('payload_ref') != immutable[0]
                            or immutable[1] != {key: step.payload[key] for key in ('output', 'metadata', 'rejected')}):
                        raise RecognitionConflict('shared_organize_result_unavailable')
                    messages = organize.constraint_admission.messages(request, organize.store,
                        organize.source_context.messages(organize.source_item(actor_reader)))
                    organize.constraint_admission.validate_recorded_input(request, organize.store, messages,
                        response_model=None, max_tokens=step.payload['inputs']['max_tokens'])
                    actual = json.loads(_strip_fence(step.payload['output']))
                    if _is_local_model(organize.models):
                        actual = _ground_local_draft(actual, source)
                    if _draft(actual, source) != draft:
                        raise RecognitionConflict('shared_organize_result_changed')
                    row = tx.read('workspace_items', item_id)
                    intake.items.processing_lease._owned(tx, item_id, project_id, run_id)
                    if row is None or row.payload.get('source_text') != source:
                        raise RecognitionConflict('shared_organize_source_changed')
                    original = capture_lineage(tx, 'workspace_items', item_id)
                    value = {'item_id': item_id, 'project_id': project_id, 'source_namespace': self.query.source_namespace,
                        'source_birth': original, 'source_revision': row.revision, 'source_text': source,
                        'draft': deepcopy(draft), 'execution_owner_user_id': self.query.origin_admission.destination.owner_user_id,
                        'producer': {'turn_id': request['turn_id'], 'desired_outcome': request['desired_outcome'],
                            'namespace_id': self.query.origin_admission.destination.namespace,
                            'stage_key': step.object_id, 'stage_revision': step.revision, 'result_ref': immutable[0]}}
                    previous = tx.read('v2_shared_organize_results', item_id)
                    tx.put('v2_shared_organize_results', item_id, value,
                        expected_revision=previous.revision if previous is not None else 0)
                    from ..transaction_records import TransactionRecords
                    result = intake.items.with_records(TransactionRecords(tx)).update(item_id, project_id,
                        {'processing'}, expected_run_id=run_id, source_text=source, draft=draft,
                        title=draft['title'], status='ready', processing_seconds=processing_seconds)
                    tx.commit()
        return result
