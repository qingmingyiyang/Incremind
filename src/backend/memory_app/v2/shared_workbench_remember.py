import asyncio
from contextlib import contextmanager
from copy import deepcopy
from uuid import uuid4

from fastapi import HTTPException

from backend.recognition import RecognitionConflict, WorkScope
from backend.security.user_context import USER_ACCESS, UserError
from core.document_engine import SQLiteDocumentRepository
from core.storage_provider.record_lineage import RecordLineageError
from ..source_egress import recognition_service
from ..transaction_records import TransactionRecords
from ..workbench_identity_binding import execution_owner
from ..workspace_contracts import _now, _text
from .insights import insight_view
from .material_origin_admission import BoundMaterialOrigins
from .shared_auxiliary import _qualified_commit
from .shared_intake import SharedIntake, SharedIntakeCommandSource
from .shared_intake_review import review_shared_intake, validate_organized_source
from .shared_projects import SharedProjectError
from .turn_execution import REQUESTS, _REQUEST_KEY
from .turn_message_order import ACCEPTANCES
from .workbench_receipts import _safe_error


COMMANDS = 'v2_workbench_shared_remember_commands'
TURNS = 'v2_workbench_shared_remember_turns'


def _command(reader, key, owner):
    row = reader.read(COMMANDS, key)
    if row is None:
        return None, None, None
    value = row.payload
    accepted = reader.read(ACCEPTANCES, value['turn_id'])
    request = reader.read(REQUESTS, key)
    selection = value.get('selection')
    caller = value.get('caller')
    if (set(value) != {'owner', 'caller', 'project_id', 'turn_id', 'thread_id', 'selection'}
            or not isinstance(caller, dict) or set(caller) != {'user_id', 'device_id'}
            or any(not isinstance(field, str) or not field for field in caller.values())
            or row.revision != (1 if selection is None else 2)
            or selection is not None and (not isinstance(selection, dict) or set(selection) != {'namespace', 'route'})
            or accepted is None or accepted.revision != 1 or request is None
            or value['owner'] != owner or accepted.payload['owner'] != owner
            or accepted.payload['actor']['user_id'] != caller['user_id']
            or accepted.payload['public_id'] != value['turn_id']
            or accepted.payload['request_key'] != key or accepted.payload['body'] != request.payload['body']
            or accepted.payload['accepted_at'] != request.payload['created_at']
            or accepted.payload['requested_project_id'] != value['project_id']
            or accepted.payload['thread_id'] != value['thread_id']
            or request.payload['body'].get('scope_kind') != 'shared'
            or request.payload['body'].get('project_id') != value['project_id']
            or request.payload['body'].get('intent') not in {None, 'auto', 'remember'}):
        raise RecognitionConflict('shared_remember_command_changed')
    return row, accepted.payload, request.payload


def read_shared_remember(reader, row, *, owner_user_id, namespace_id):
    marker = reader.read(TURNS, row.object_id)
    accepted = reader.read(ACCEPTANCES, row.object_id)
    if marker is None:
        if accepted is not None and accepted.payload.get('body', {}).get('scope_kind') == 'shared':
            raise RecognitionConflict('shared_remember_turn_changed')
        return None
    value = marker.payload
    owner = {'owner_user_id': owner_user_id, 'namespace_id': namespace_id, 'origin_kind': 'personal'}
    command, acceptance, request = _command(reader, value['key'], owner)
    payload = row.payload
    memory = payload.get('receipt', {}).get('remember', {})
    if (marker.revision != 1 or set(value) != {'key', 'namespace', 'source_key', 'item_id'}
            or command is None or command.payload['selection'] is None
            or value['namespace'] != command.payload['selection']['namespace']
            or value['source_key'] != 'wb-' + row.object_id
            or command.payload['turn_id'] != row.object_id or request['state'] != 'completed'
            or request['result']['thread_id'] != payload['thread_id']
            or request['result']['turn']['id'] != row.object_id
            or request['result']['turn']['scope_kind'] != 'shared'
            or request['result']['turn']['intent'] != 'remember'
            or request['result']['turn']['receipt']['remember']['item_id'] != value['item_id']
            or payload['intent'] != 'remember' or payload.get('parent_turn_id') is not None
            or payload['project_id'] != command.payload['project_id']
            or payload['thread_id'] != acceptance['thread_id']
            or payload['user_text'] != request['result']['turn']['user_text']
            or payload['item_id'] != value['item_id'] or memory.get('item_id') != value['item_id']):
        raise RecognitionConflict('shared_remember_turn_changed')
    return {**value, 'project_id': payload['project_id'], 'caller': command.payload['caller']}


class SharedWorkbenchRemember:
    def __init__(self, application, records, documents, instance):
        self.application, self.records, self.documents, self.instance = application, records, documents, instance

    def _access(self):
        state, access = self.application.state, USER_ACCESS.get()
        context = getattr(state, 'server_context', None)
        if (context is None or access is None or access.target_user_id != getattr(state, 'server_user_id', None)
                or state.recognition_records is not self.records or state.recognition_documents is not self.documents):
            raise HTTPException(403, 'shared_remember_owner_changed')
        return context, access

    @staticmethod
    def _caller(access):
        return {'user_id': access.caller.user_id, 'device_id': access.caller.device_id}

    def _owner(self):
        return execution_owner(self.application, self.records, self.documents)

    def _authorized(self, value):
        if value['caller'] != self._caller(self._access()[1]):
            raise HTTPException(403, 'shared_remember_owner_changed')

    @staticmethod
    def is_submission(body):
        return (body.get('scope_kind') == 'shared' and 'text' in body
            and body.get('intent') in {None, 'auto', 'remember'})

    @staticmethod
    def validate_key(key):
        if not isinstance(key, str) or not _REQUEST_KEY.fullmatch(key):
            raise HTTPException(400, 'invalid_idempotency_key')

    def reserve(self, tx, body, key, acceptance):
        if key is None or not self.is_submission(body):
            return
        tx.put(COMMANDS, key, {'owner': self._owner(), 'caller': self._caller(self._access()[1]),
            'project_id': acceptance['requested_project_id'], 'turn_id': acceptance['public_id'],
            'thread_id': acceptance['thread_id'], 'selection': None}, expected_revision=0)

    def replay(self, tx, body, key):
        if tx.read(COMMANDS, key) is None:
            if body.get('scope_kind') == 'shared' and body.get('intent') == 'remember':
                raise RecognitionConflict('shared_remember_command_changed')
            return None
        row, acceptance, request = _command(tx, key, self._owner())
        if row.payload['selection'] is None and body.get('intent') != 'remember':
            return None
        self._authorized(row.payload)
        # 恢复的操作只写同一个原件，不会再次执行意图判断。
        if (request['state'] != 'completed'
                and (request['state'] != 'running' or request['instance'] != self.instance)):
            if tx.read('v2_turns', acceptance['public_id']) is not None:
                raise RecognitionConflict('shared_remember_turn_changed')
            return acceptance
        return None

    def selection(self, key):
        if key is None:
            return None
        with self.records.begin_read() as tx:
            if tx.read(COMMANDS, key) is None:
                return None
            row, _, _ = _command(tx, key, self._owner())
            if row.payload['selection'] is None:
                return None
            self._authorized(row.payload)
            return deepcopy(row.payload['selection'])

    def discover(self, key, project):
        self.validate_key(key)
        self._access()
        with self.records.begin_read() as tx:
            row, acceptance, request = _command(tx, key, self._owner())
            if (row is None or row.payload['project_id'] != project
                    or row.payload['selection'] is None and request['body'].get('intent') != 'remember'):
                raise HTTPException(404, 'shared_remember_not_received')
            self._authorized(row.payload)
            value = {'project_id': project, 'scope_kind': 'shared'}
            if request['state'] != 'completed':
                body = request['body']
                if request['state'] == 'running' and request['instance'] == self.instance:
                    return {**value, 'state': 'preparing', 'recoverable': False}
                if request['state'] == 'running':
                    return {**value, 'state': 'preparing', 'recoverable': True, 'request': body}
                return {**value, 'state': 'failed', 'error': request.get('error', 'turn_interrupted'),
                    'recoverable': True, 'request': body}
            current = tx.read('v2_turns', acceptance['public_id'])
            if current is None or current.payload['intent'] != 'remember':
                raise HTTPException(404, 'shared_remember_not_received')
            binding = self.read_turn(tx, current)
        return {**value, 'state': 'accepted', 'result': self.result(current, binding=binding)}

    def intake(self):
        context, access = self._access()
        return SharedIntake(context.shared_query_factory, context), access

    def query(self, project, *, namespace=None, write=True):
        intake, access = self.intake()
        try:
            query = intake._query(self.application, access.caller, project)
            if write:
                with intake.registry.records.begin_read() as central:
                    intake.registry.authorize(central, access.caller, project, action='write')
        except SharedProjectError as error:
            raise HTTPException(error.status, str(error)) from None
        if namespace is not None and query.source_namespace != namespace:
            raise RecognitionConflict('shared_remember_namespace_changed')
        return query

    @staticmethod
    @contextmanager
    def qualified(query):
        try:
            with _qualified_commit(query.origin_admission) as scope:
                yield scope
        except SharedProjectError as error:
            raise HTTPException(error.status, str(error)) from None

    @staticmethod
    def qualify(scope, query, tx, *, write=True):
        admission = query.origin_admission
        if write:
            admission.registry.authorize(scope._central, admission.actor, admission.lease.project_id, action='write')
        origins = BoundMaterialOrigins(admission, admission.lease, {})
        return scope.reader_for(origins, origins.shared, actor_reader=TransactionRecords(tx))

    async def capture(self, query, *, turn_id, request_key, text, route):
        self.validate_key(request_key)
        intake, access = self.intake()
        source_key = 'wb-' + turn_id
        with self.qualified(query) as scope, self.records.begin() as tx:
            source_reader = self.qualify(scope, query, tx)
            command, acceptance, request = _command(tx, request_key, self._owner())
            if (command is None or acceptance['public_id'] != turn_id or _text(request['body']['text'], 'text') != text
                    or request['state'] != 'running' or request['instance'] != self.instance):
                raise RecognitionConflict('shared_remember_command_changed')
            self._authorized(command.payload)
            if not acceptance['new_thread']:
                from .turn_logical_scope import TurnLogicalScope
                reader = TransactionRecords(tx)
                logical = TurnLogicalScope(self.application)
                existing = [row for row in reader.list('v2_turns')
                    if row.payload.get('thread_id') == acceptance['thread_id']]
                if not existing or any(logical.read(row, reader=reader) != 'shared' for row in existing):
                    raise HTTPException(409, 'shared_remember_thread_changed')
            selection = {'namespace': query.source_namespace, 'route': route}
            if command.payload['selection'] is None:
                tx.put(COMMANDS, request_key, {**command.payload, 'selection': selection}, expected_revision=command.revision)
            elif command.payload['selection'] != selection:
                raise RecognitionConflict('shared_remember_selection_changed')
            source = intake._command(source_reader, intake._identity(query, source_key), query, source_key)
            if source is not None:
                proof = SharedIntakeCommandSource(source_key, access.caller.user_id, source.payload['item_id'], text)
                proof.read(source_reader, query)
                item = intake._view(source_reader, source, query, source_key)['item']
            else:
                item = None
            tx.commit()
        if item is None:
            try:
                value = await intake.text(self.application, access.caller, command.payload['project_id'],
                    {'text': text}, source_key, query=query)
            except SharedProjectError as error:
                raise HTTPException(error.status, str(error)) from None
            if value['namespace'] != query.source_namespace:
                raise RecognitionConflict('shared_remember_namespace_changed')
            item = value['item']
        return item, {'key': request_key, 'source_key': source_key, 'namespace': query.source_namespace,
            'item_id': item['id']}

    def _command_source(self, binding, text):
        actor = binding['caller']['user_id'] if 'caller' in binding else self._access()[1].caller.user_id
        return SharedIntakeCommandSource(binding['source_key'], actor, binding['item_id'], text)

    def _source(self, reader, query, binding, *, text):
        if query.source_namespace != binding['namespace']:
            raise RecognitionConflict('shared_remember_source_changed')
        try:
            return self._command_source(binding, text).read(reader, query)
        except (SharedProjectError, RecordLineageError):
            raise RecognitionConflict('shared_remember_source_changed') from None

    def bind_turn(self, tx, scope, query, turn, binding):
        reader = self.qualify(scope, query, tx)
        self._source(reader, query, binding, text=turn.payload['user_text'])
        tx.put(TURNS, turn.object_id, binding, expected_revision=0)

    def read_turn(self, reader, row):
        owner = self._owner()
        return read_shared_remember(reader, row, owner_user_id=owner['owner_user_id'], namespace_id=owner['namespace_id'])

    def result(self, row, *, binding=None):
        payload = row.payload
        receipt = deepcopy(payload['receipt'])
        memory = receipt['remember']
        candidate = memory.pop('insight_id', None)
        if memory['state'] == 'processing' and payload['instance'] != self.instance:
            memory.update(state='failed', error='interrupted')
        if binding is not None:
            self._read_source_links(row, binding, memory, candidate)
        return {'thread_id': payload['thread_id'], 'turn': {'id': row.object_id, 'thread_id': payload['thread_id'],
            'intent': 'remember', 'scope_kind': 'shared', 'user_text': payload['user_text'],
            'created_at': payload['created_at'], 'receipt': receipt}}

    def _read_source_links(self, row, binding, memory, candidate):
        memory.update(sources_available=False, insights=[])
        projected = None
        try:
            query = self.query(binding['project_id'], namespace=binding['namespace'], write=False)
            with self.qualified(query) as scope, self.records.begin_read() as tx:
                reader = self.qualify(scope, query, tx, write=False)
                self._source(reader, query, binding, text=row.payload['user_text'])
                if candidate is not None:
                    service = recognition_service(reader, source_owner=query.material_owner)
                    projected = insight_view(reader, WorkScope('local-user', binding['project_id']),
                        candidate, service=service, source_owner=query.material_owner)
        except HTTPException as error:
            if error.status_code not in {403, 404}:
                raise
            return
        except (SharedProjectError, RecognitionConflict, RecordLineageError, UserError):
            # 个人旧回执不授予已失效的共享原件读取资格。
            return
        memory.update(sources_available=True, insights=[projected] if projected is not None else [])

    def _save(self, tx, row, memory, *, run_id=None):
        now = _now()
        saved = tx.put('v2_turns', row.object_id, {**row.payload, 'receipt': {'remember': memory}, 'updated_at': now,
            **({'run_id': run_id, 'instance': self.instance} if run_id is not None else {})}, expected_revision=row.revision)
        thread = tx.read('v2_threads', row.payload['thread_id'])
        tx.put('v2_threads', thread.object_id, {**thread.payload, 'updated_at': now}, expected_revision=thread.revision)
        tx.commit()
        return saved

    def retry(self, row):
        with self.records.begin_read() as tx:
            binding = self.read_turn(tx, row)
        query = self.query(binding['project_id'], namespace=binding['namespace'])
        with self.qualified(query) as scope, self.records.begin() as tx:
            current = tx.read('v2_turns', row.object_id)
            if current != row:
                raise HTTPException(409, 'turn_not_retryable')
            self._source(self.qualify(scope, query, tx), query, binding, text=row.payload['user_text'])
            run_id = uuid4().hex
            memory = {**row.payload['receipt']['remember'], 'state': 'processing', 'error': None}
            return self._save(tx, row, memory, run_id=run_id), run_id

    def _progress(self, query, binding, turn_id, run_id, *, persist=True, candidate=None):
        with self.qualified(query) as scope, self.records.begin() as tx:
            row = tx.read('v2_turns', turn_id)
            if row is None or row.payload['run_id'] != run_id:
                raise RecognitionConflict('shared_remember_run_changed')
            reader = self.qualify(scope, query, tx)
            item = self._source(reader, query, binding, text=row.payload['user_text'])
            memory = dict(row.payload['receipt']['remember'])
            payload = item.payload
            status, document_id = payload['status'], payload.get('document_id')
            if status == 'failed':
                memory.update(state='failed', error=payload.get('error') or 'processing_failed')
            if status == 'confirmed':
                validate_organized_source(reader, query, item.object_id)
                document = SQLiteDocumentRepository(reader, namespace_id=query.material_owner.lease.data_namespace).read(document_id)
                if (document is None or document['project_id'] != binding['project_id'] or not any(
                        ref.get('locator') == 'workspace://' + item.object_id and ref.get('source_id') == item.object_id
                        for ref in document['source_refs'])):
                    raise RecognitionConflict('shared_remember_archive_changed')
                memory.update(document_id=document_id, progress={'done': 4, 'total': 4})
            elif status == 'ready':
                memory.update(progress={'done': 3, 'total': 4})
            if candidate is not None:
                if (status != 'confirmed' or candidate['document_id'] != document_id
                        or candidate['document_revision'] != document['revision']
                        or candidate['candidate_id'] != payload.get('candidate_id')
                        or candidate['experience_id'] != payload.get('experience_id')):
                    raise RecognitionConflict('shared_remember_insight_changed')
                pending = reader.read('recognition_candidates', candidate['candidate_id'])
                if (pending is None or pending.payload.get('scope') != {'user_id': 'local-user',
                        'project_id': binding['project_id']} or candidate['experience_id'] not in
                        pending.payload.get('source_experience_ids', ())):
                    raise RecognitionConflict('shared_remember_insight_changed')
                memory.update(state='done', error=None, insight_id=pending.object_id)
            if persist:
                self._save(tx, row, memory)
            return item

    def fail(self, turn_id, run_id, code):
        with self.records.begin() as tx:
            row = tx.read('v2_turns', turn_id)
            if row is not None and row.payload['run_id'] == run_id:
                self.read_turn(tx, row)
                memory = {**row.payload['receipt']['remember'], 'state': 'failed', 'error': code}
                self._save(tx, row, memory)

    async def run(self, turn_id, run_id):
        with self.records.begin_read() as tx:
            row = tx.read('v2_turns', turn_id)
            if row is None or row.payload['run_id'] != run_id:
                return
            binding = self.read_turn(tx, row)
        context, access = self._access()
        project = binding['project_id']
        stage = 'processing_failed'
        try:
            async with context.pool.lease(access.target_user_id) as child:
                if child is not self.application:
                    raise RecognitionConflict('shared_remember_owner_changed')
                with context.pool.request_scope(access.target_user_id, child, access):
                    query = self.query(project, namespace=binding['namespace'])
                    intake, _ = self.intake()
                    command_source = self._command_source(binding, row.payload['user_text'])
                    item = self._progress(query, binding, turn_id, run_id, persist=False)
                    if item.payload['status'] in {'staged', 'failed', 'processing'}:
                        await intake.process(child, access.caller, project, item.object_id,
                            {'expected_revision': item.revision}, query=query, command_source=command_source)
                        item = self._progress(query, binding, turn_id, run_id)
                    if item.payload['status'] == 'failed':
                        return
                    stage = 'confirmation_failed'
                    if item.payload['status'] == 'ready':
                        await review_shared_intake(intake, child, access.caller, project, item.object_id,
                            {'expected_revision': item.revision}, operation='confirm', query=query, command_source=command_source)
                        item = self._progress(query, binding, turn_id, run_id)
                    if item.payload['status'] != 'confirmed':
                        raise RecognitionConflict('shared_remember_archive_changed')
                    stage = 'insight_generation_failed'
                    result = await review_shared_intake(intake, child, access.caller, project, item.object_id,
                        {'expected_revision': item.revision}, operation='recognition', query=query, command_source=command_source)
                    self._progress(query, binding, turn_id, run_id, candidate=result['recognition'])
        except asyncio.CancelledError:
            self.fail(turn_id, run_id, 'interrupted')
            raise
        except (SharedProjectError, RecognitionConflict, RecordLineageError, UserError):
            self.fail(turn_id, run_id, 'source_changed_retry')
        except Exception as error:
            self.fail(turn_id, run_id, _safe_error(error, stage))
