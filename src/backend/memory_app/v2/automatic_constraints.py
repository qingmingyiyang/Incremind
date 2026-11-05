from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from threading import get_ident
from weakref import WeakKeyDictionary

from backend.recognition import WorkScope
from backend.security.audited_records import _AuditedTransaction
from core.ai_kernel.product_admission import MANIFEST_KIND, ProductAdmissionItem, bundle_from_payload, bundle_to_payload, seal_admission
from core.ai_kernel.sqlite_store import SQLiteAITurnStore
from core.storage_provider.sqlite_uow import SQLiteStructuredRecord, SQLiteStructuredRecordUnitOfWork
from ..transaction_records import TransactionRecords
from .automatic_constraint_input import (
    KIND, OBJECTS, automatic_bundle, constraint_markers, frozen_messages, validate_automatic_descriptor,
    validate_automatic_spec, validate_producer_identity,
)
from .project_constraint_input import ProjectConstraintInput
from .project_constraints import ProjectConstraintScopes
from .shared_project_constraint_input import SharedProjectConstraintInput


PREPARED = 'v2_automatic_constraint_admissions'


class _SceneReader:
    def __init__(self, reader):
        self.reader, self.assignments, self.dependencies = reader, {}, {}

    def read(self, collection, identity):
        row = self.reader.read(collection, identity)
        if collection.startswith('v2_scene_assignments_'):
            self.assignments[(collection, identity)] = {'collection': collection, 'id': identity,
                'revision': row.revision if row is not None else 0,
                'payload': deepcopy(row.payload) if row is not None else None}
        elif collection in {*OBJECTS.values(), 'recognition_experiences'} and row is not None:
            self.dependencies[(collection, identity)] = {'collection': collection, 'id': identity,
                'revision': row.revision}
            if collection == 'recognitions':
                # 原场景读取器会沿父认识递归；每层别名的领域资格同样冻结真实修订。
                for alias in self.reader.list('recognition_candidates'):
                    if (alias.payload.get('scope') == row.payload.get('scope')
                            and alias.payload.get('state') == 'published'
                            and alias.payload.get('recognition_id') == identity):
                        self.dependencies[('recognition_candidates', alias.object_id)] = {
                            'collection': 'recognition_candidates', 'id': alias.object_id, 'revision': alias.revision}
        return row

    def list(self, collection):
        return self.reader.list(collection)


class AutomaticConstraintAdmission:
    def __init__(self, constraint_input):
        if not isinstance(constraint_input, ProjectConstraintInput):
            raise ValueError('automatic_constraint_owner_invalid')
        self.input = constraint_input
        self.records, self.application = constraint_input.records, constraint_input.application
        self.models = self.application.state.recognition_models
        self._births = WeakKeyDictionary()
        self._scopes = None

    @property
    def scopes(self):
        # 服务器子工厂在路由安装后绑定执行 owner；首次实际使用才核验其身份。
        owner = self._owner()
        if self._scopes is None:
            shared = isinstance(self.input, SharedProjectConstraintInput)
            source_records = self.input.query.records if shared else self.records
            namespace = self.input.query.documents.namespace_id if shared else owner['namespace_id']
            self._scopes = ProjectConstraintScopes(source_records, namespace=namespace,
                owner_user_id=owner['owner_user_id'], origin_kind='shared' if shared else 'personal',
                validate_owner=self._owner)
        return self._scopes

    def _owner(self):
        state, owner = self.application.state, self.input._owner()
        if (state.recognition_models is not self.models or self.models.records is not self.records
                or (isinstance(self.input, SharedProjectConstraintInput) and (
                    self.input.personal is not state.project_constraint_input
                    or self.input.query.project_constraint_input is not self.input))
                or not isinstance(self.input, SharedProjectConstraintInput)
                    and state.project_constraint_input is not self.input):
            raise ValueError('automatic_constraint_owner_changed')
        return owner

    def bind(self, records, models, *, kind, project, spec):
        self._owner()
        if records is not self.records or models is not self.models:
            raise ValueError('automatic_constraint_owner_changed')
        return AutomaticGenerationBinding(self, kind, project, validate_automatic_spec(spec, kind))

    def _store(self, store):
        self._owner()
        if (not isinstance(store, SQLiteAITurnStore)
                or Path(store._path).resolve(strict=True)
                != (self.input.root / '.rebuild-data' / 'ai-turns.sqlite3').resolve(strict=True)):
            raise ValueError('automatic_constraint_native_owner_changed')

    @contextmanager
    def _reader(self, reader=None, *, write=False):
        self._owner()
        if reader is None:
            if write:
                raise ValueError('automatic_constraint_writer_required')
            with self.records.begin_read() as tx:
                with self._reader(tx) as view:
                    yield view
            return
        view = TransactionRecords(reader)
        actual = view._transaction
        if isinstance(actual, _AuditedTransaction):
            actual = actual.transaction
        if (not isinstance(actual, SQLiteStructuredRecordUnitOfWork) or actual.closed
                or not actual.connection.in_transaction or write and actual._read_only
                or Path(view.database_path).resolve(strict=True)
                    != Path(self.records.database_path).resolve(strict=True)):
            raise ValueError('automatic_constraint_reader_changed')
        try:
            yield view
        finally:
            self._owner()

    @contextmanager
    def _source_reader(self, actor_reader):
        if isinstance(self.input, SharedProjectConstraintInput):
            with self.input._source_reader(actor_reader) as reader:
                yield TransactionRecords(reader)
        else:
            yield actor_reader

    def read_frozen(self, request, store, *, reader=None):
        self._store(store)
        native = store.get_request(request['turn_id'])
        with self._reader(reader) as view:
            staged = view.read(PREPARED, request['turn_id'])
            if native is not None and native != request:
                raise ValueError('automatic_constraint_native_request_changed')
            if not constraint_markers(request):
                if staged is not None or store.get_immutable_payload(request['turn_id'], KIND) is not None:
                    raise ValueError('automatic_constraint_marker_changed')
                original = view.read('v2_memory_turn_keys', request['turn_id'])
                identity = original.payload.get('identity') if original is not None else None
                if native is None and (original is None or original.payload.get('request') != request
                        or not isinstance(identity, dict) or set(identity) != {'kind', 'project', 'key'}
                        or identity['kind'] != request['desired_outcome']
                        or identity['project'] != request['scope']['project_id']):
                    raise ValueError('automatic_constraint_legacy_unverifiable')
                # 原请求资格保留，旧范围不可核验；这里不生成或补存任何新证明。
                return None
            if (staged is None or staged.revision != 1 or set(staged.payload) != {'request', 'bundle'}
                    or staged.payload['request'] != request):
                raise ValueError('automatic_constraint_preparation_missing')
            admitted = (store.restore_admission(request) if native is not None
                        else bundle_from_payload(staged.payload['bundle']))
            automatic_bundle(request, admitted)
            if bundle_to_payload(admitted) != staged.payload['bundle']:
                raise ValueError('automatic_constraint_preparation_changed')
            return admitted

    def historical_messages(self, request, store, messages, *, reader=None):
        admitted = self.read_frozen(request, store, reader=reader)
        return frozen_messages(None if admitted is None else automatic_bundle(request, admitted), messages)

    def validate_saved(self, request, *, reader=None, spec=None):
        from ..kernel.memory_turn import MemoryTurn
        store = MemoryTurn.store_for(self.records)
        admitted = self.read_frozen(request, store, reader=reader)
        if admitted is None:
            return None
        descriptor = next(item for item in admitted.items if item.kind == KIND).payload['automatic_descriptor']
        if spec is not None and spec != descriptor['spec']:
            raise ValueError('automatic_constraint_spec_changed')
        binding = self.bind(self.records, self.models, kind=request['desired_outcome'],
            project=request['scope']['project_id'], spec=descriptor['spec'])
        return binding.validate_current(request, store, reader=reader)


class AutomaticGenerationBinding:
    def __init__(self, owner, kind, project, spec):
        self.owner, self.kind, self.project, self.spec = owner, kind, project, spec

    def _request(self, request):
        if request['desired_outcome'] != self.kind or request['scope']['project_id'] != self.project:
            raise ValueError('automatic_constraint_request_changed')

    def _descriptor(self, reader, request, producer_identity, producer_previous=None):
        self._request(request)
        project = reader.read('v2_projects', self.project)
        if project is None:
            raise ValueError('automatic_constraint_project_missing')
        scenes, resolutions, owners = _SceneReader(reader), [], []
        object_source = None
        for anchor in self.spec['anchors']:
            row = scenes.read(OBJECTS[anchor['type']], anchor['id'])
            if row is None or row.payload.get('project_id') != self.project:
                raise ValueError('automatic_constraint_anchor_changed')
            collection = 'v2_scene_assignments_' + anchor['type']
            direct = scenes.read(collection, anchor['id'])
            assignment = deepcopy(direct.payload) if direct is not None else None
            source = {'collection': collection, 'id': anchor['id']}
            if anchor['type'] in {'recognition', 'candidate'}:
                from .insights import _scene_assignment
                assignment = _scene_assignment(scenes, WorkScope('local-user', self.project), row, anchor['type'])
            elif anchor['type'] == 'document' and direct is None:
                items = [item for item in reader.list('workspace_items')
                    if item.payload.get('document_id') == anchor['id']
                    and item.payload.get('project_id') == self.project and item.payload.get('status') == 'confirmed']
                if len(items) > 1:
                    raise ValueError('automatic_constraint_document_owner_ambiguous')
                if items:
                    item = scenes.read('workspace_items', items[0].object_id)
                    inherited = scenes.read('v2_scene_assignments_item', item.object_id)
                    assignment = deepcopy(inherited.payload) if inherited is not None else None
                    source = {'collection': 'v2_scene_assignments_item', 'id': item.object_id}
                    owners.append({'id': item.object_id, 'revision': item.revision, 'document_id': anchor['id']})
            if assignment is not None and assignment.get('project_id') != self.project:
                raise ValueError('automatic_constraint_assignment_changed')
            resolutions.append({**anchor, 'assignment': deepcopy(assignment)})
            if self.spec['mode'] == 'object':
                object_source = source
        scene = (resolutions[0]['assignment'] or {}).get('scene') if object_source is not None else self.spec['scene']
        if self.spec['mode'] == 'scope' and scene is not None and any(
                row['assignment'] is None or row['assignment']['scene'] != scene for row in resolutions):
            raise ValueError('automatic_constraint_scope_changed')
        shared = self.owner.input
        source = ({'namespace': shared.query.source_namespace, 'lease': shared._shared_origin.lease.payload()}
                  if isinstance(shared, SharedProjectConstraintInput) else None)
        return {'schema_version': 2 if producer_previous is not None else 1,
            'spec': deepcopy(self.spec), 'producer_identity': deepcopy(producer_identity),
            'project_revision': project.revision, 'scene': scene, 'scene_source': object_source,
            'scene_owners': owners, 'assignments': [scenes.assignments[key] for key in sorted(scenes.assignments)],
            'dependencies': [scenes.dependencies[key] for key in sorted(scenes.dependencies)],
            'resolved_scenes': resolutions, 'materials': deepcopy(request['privacy']['material_refs']), 'source': source,
            **({'producer_previous': deepcopy(producer_previous)} if producer_previous is not None else {})}

    def _snapshot(self, request, reader, producer_identity, producer_previous=None):
        with self.owner._source_reader(reader) as source:
            descriptor = self._descriptor(source, request, producer_identity, producer_previous)
            bundle = self.owner.scopes.prepare_automatic_bundle(WorkScope('local-user', self.project),
                scene=descriptor['scene'], reader=source)
        return descriptor, bundle

    def collect(self, request, store, reader, *, producer_identity, previous_producer=None):
        # 只收集未封装的条目；现有个人或共享 owner 负责唯一一次 seal 和原生 claim。
        self.owner._store(store)
        self._request(request)
        validate_producer_identity(producer_identity, request, self.spec)
        collection, identity = self._producer_location(request, producer_identity)
        with self.owner._reader(reader, write=True) as view:
            current = view.read(collection, identity)
            previous = None
            if previous_producer is not None:
                if (collection != 'workspace_organize_steps' or not isinstance(previous_producer, SQLiteStructuredRecord)
                        or current != previous_producer or previous_producer.collection != collection
                        or previous_producer.object_id != identity
                        or not isinstance(current.payload.get('turn_id'), str)
                        or current.payload['turn_id'] == request['turn_id']):
                    raise ValueError('automatic_constraint_previous_producer_changed')
                # 重试资格仍由原 Organize owner 检查；这里只冻结本次 CAS 替换的真实旧行。
                previous = {'collection': collection, 'id': identity, 'revision': current.revision,
                            'payload': deepcopy(current.payload)}
            if (constraint_markers(request) or view.read(PREPARED, request['turn_id']) is not None
                    or store.get_request(request['turn_id']) is not None
                    or view.read('v2_memory_turn_keys', request['turn_id']) is not None
                    or current is not None and previous is None):
                raise ValueError('automatic_constraint_birth_already_exists')
            descriptor, bundle = self._snapshot(request, view, producer_identity, previous)
            validate_automatic_descriptor(descriptor, request)
            scope = {**self.owner._owner(), 'turn_id': request['turn_id'], 'project_id': self.project}
            reference = {'kind': 'session_event', 'object_id': request['turn_id'],
                'uri': f"crp://{scope['namespace_id']}/project-constraints/{request['turn_id']}"}
            item = ProductAdmissionItem(KIND, reference, scope,
                {'native_turn_id': request['turn_id'], 'automatic_descriptor': descriptor, 'bundle': bundle})
            actual = self._transaction(view)
            births = self.owner._births.setdefault(actual, {})
            if request['turn_id'] in births:
                raise ValueError('automatic_constraint_birth_already_collected')
            births[request['turn_id']] = {'thread': get_ident(), 'item': deepcopy(item),
                'write_offset': len(actual._records), 'previous_revision': current.revision if current else 0}
            return (item,)

    def prepare(self, request, store, reader, *, producer_identity, previous_producer=None):
        return seal_admission(request, self.collect(request, store, reader,
            producer_identity=producer_identity, previous_producer=previous_producer), store)

    def admission(self, request, store, *, reader=None):
        return self.owner.read_frozen(request, store, reader=reader)

    @staticmethod
    def _transaction(reader):
        actual = TransactionRecords(reader)._transaction
        return actual.transaction if isinstance(actual, _AuditedTransaction) else actual

    @staticmethod
    def _producer_location(request, producer):
        return producer['collection'], (request['turn_id'] if producer['collection'] == 'v2_memory_turn_keys'
                                        else producer['key'])

    def _require_birth_uow(self, reader, request, admitted):
        actual = self._transaction(reader)
        birth = self.owner._births.get(actual, {}).get(request['turn_id'])
        item = next(item for item in admitted.items if item.kind == KIND)
        if birth is None or birth['thread'] != get_ident() or birth['item'] != item:
            raise ValueError('automatic_constraint_birth_uow_required')
        producer = item.payload['automatic_descriptor']['producer_identity']
        collection, identity = self._producer_location(request, producer)
        current = reader.read(collection, identity)
        # 原 UOW 只记录本次实际 put/delete；同库旧行和后续同正文更新都不代表出生。
        writes = [row for row in actual._records[birth['write_offset']:]
                  if row.collection == collection and row.object_id == identity]
        if current is None or current.revision != birth['previous_revision'] + 1 or writes != [current]:
            raise ValueError('automatic_constraint_producer_birth_required')

    def _producer(self, reader, request, producer):
        if producer['collection'] == 'v2_memory_turn_keys':
            row = reader.read('v2_memory_turn_keys', request['turn_id'])
            if (row is None or row.payload.get('request') != request
                    or row.payload.get('identity') != {key: value for key, value in producer.items() if key != 'collection'}):
                raise ValueError('automatic_constraint_producer_changed')
        else:
            row = reader.read('workspace_organize_steps', producer['key'])
            if row is None or row.payload.get('turn_id') != request['turn_id']:
                raise ValueError('automatic_constraint_producer_changed')

    def _validate(self, request, admitted, reader):
        bundle = automatic_bundle(request, admitted)
        item = next(item for item in admitted.items if item.kind == KIND)
        descriptor = item.payload['automatic_descriptor']
        if item.scope != {**self.owner._owner(), 'turn_id': request['turn_id'], 'project_id': self.project}:
            raise ValueError('automatic_constraint_owner_changed')
        if descriptor['spec'] != self.spec:
            raise ValueError('automatic_constraint_spec_changed')
        if isinstance(self.owner.input, SharedProjectConstraintInput):
            self.owner.input._shared_origin._bind_bundle(request, admitted)
        self._producer(reader, request, descriptor['producer_identity'])
        current_descriptor, current_bundle = self._snapshot(request, reader, descriptor['producer_identity'],
            descriptor.get('producer_previous'))
        if current_descriptor != descriptor:
            raise ValueError('automatic_constraint_descriptor_changed')
        if current_bundle != bundle:
            raise ValueError('automatic_constraint_bundle_changed')
        return bundle

    def stage(self, request, store, admitted, reader):
        self.owner._store(store)
        self._request(request)
        if store.get_request(request['turn_id']) is not None:
            raise ValueError('automatic_constraint_birth_already_exists')
        with self.owner._reader(reader, write=True) as view:
            automatic_bundle(request, admitted)
            self._require_birth_uow(view, request, admitted)
            self._validate(request, admitted, view)
            payload = {'request': deepcopy(request), 'bundle': bundle_to_payload(admitted)}
            prior = view.read(PREPARED, request['turn_id'])
            if prior is not None:
                if prior.revision != 1 or prior.payload != payload:
                    raise ValueError('automatic_constraint_preparation_changed')
                return prior
            return view.put(PREPARED, request['turn_id'], payload, expected_revision=0)

    def validate_current(self, request, store, *, reader=None):
        self._request(request)
        with self.owner._reader(reader) as view:
            admitted = self.owner.read_frozen(request, store, reader=view)
            return None if admitted is None else self._validate(request, admitted, view)

    def validate_commit(self, request, store, reader):
        with self.owner._reader(reader, write=True) as view:
            return self.validate_current(request, store, reader=view)

    def messages(self, request, store, messages, *, reader=None):
        return frozen_messages(self.validate_current(request, store, reader=reader), messages)

    def base_request(self, request, store):
        copied = deepcopy(request)
        admitted = self.admission(request, store)
        if admitted is not None:
            removed = [item.ref for item in admitted.items]
            removed.append({'kind': 'session_event', 'object_id': request['turn_id'],
                'uri': f"crp://session/{request['turn_id']}/{MANIFEST_KIND}"})
            copied['input']['refs'] = [ref for ref in copied['input']['refs'] if ref not in removed]
        return copied

    def preview_messages(self, messages):
        with self.owner._reader() as reader:
            request = {'desired_outcome': self.kind, 'scope': {'project_id': self.project},
                       'privacy': {'material_refs': []}}
            _, bundle = self._snapshot(request, reader, None)
        return frozen_messages(bundle, messages)

    def input_budget(self, selected_models, messages, *, max_tokens, response_model=None, limit=None):
        public = selected_models.public()['generation']
        self.owner.input.input_budget(selected_models, messages, revision=public['revision'],
            max_tokens=max_tokens, response_model=response_model, limit=limit)

    def record_input(self, request, store, *, model_request_id, messages, response_model, max_tokens):
        self.validate_current(request, store)
        if self.admission(request, store) is None:
            return None
        if store.get_request(request['turn_id']) != request:
            raise ValueError('automatic_constraint_native_request_changed')
        events = tuple(store.events_after(request['turn_id']))
        requested = [event for event in events if event['type'] == 'model.requested'
            and event['correlation'].get('model_request_id') == model_request_id]
        if (not isinstance(model_request_id, str) or not model_request_id or len(requested) != 1
                or any(event['type'] in {'turn.completed', 'turn.failed', 'turn.cancelled'}
                    or event['type'] in {'model.completed', 'model.failed'}
                        and event['correlation'].get('model_request_id') == model_request_id for event in events)):
            raise ValueError('automatic_constraint_model_request_unavailable')
        return store.get_or_create_immutable_payload(request['turn_id'],
            'automatic-constraints-model-input-' + model_request_id, {'turn_id': request['turn_id'],
                'model_request_id': model_request_id, 'messages': deepcopy(messages), 'max_tokens': max_tokens,
                'response_schema': response_model.model_json_schema() if response_model is not None else None,
                'constraints_ref': constraint_markers(request)[0]})

    def validate_recorded_input(self, request, store, messages, *, response_model, max_tokens):
        if self.admission(request, store) is None:
            return
        completed = [event for event in store.events_after(request['turn_id']) if event['type'] == 'model.completed']
        if not completed:
            raise ValueError('automatic_constraint_model_input_unavailable')
        for event in completed:
            identity = event['correlation']['model_request_id']
            saved = store.get_immutable_payload(request['turn_id'], 'automatic-constraints-model-input-' + identity)
            expected = {'turn_id': request['turn_id'], 'model_request_id': identity,
                'messages': messages, 'max_tokens': max_tokens,
                'response_schema': response_model.model_json_schema() if response_model is not None else None,
                'constraints_ref': constraint_markers(request)[0]}
            if saved is None or saved[1] != expected:
                raise ValueError('automatic_constraint_model_input_changed')
