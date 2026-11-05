import asyncio
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from uuid import NAMESPACE_URL, uuid5

from backend.recognition import WorkScope
from core.storage_provider.persistence_budget import persistence_budget
from core.storage_provider.record_lineage import FACTS, WITNESS_COLLECTIONS, capture_lineage, verify_first_created, verify_lineage
from ..processing_lease import ProcessingLease
from ..server_jobs import retain_until_finished
from ..source_egress import SourceEgressService, recognition_service
from ..transaction_records import TransactionRecords
from ..workspace_contracts import _optional_title, _text
from ..workspace_intake import WorkspaceIntake
from ..workspace_items import WorkspaceItems
from .shared_projects import PROJECTS, SharedProjectError, segment
from .shared_runtime import SharedWriteRecords, shared_read_capability


COMMANDS = 'v2_shared_intake_commands'


def conversation_item(reader, command, *, project, namespace):
    value = command.payload
    if (command.revision != 1 or value.get('kind') != 'conversation' or value.get('action') != 'share'
            or value.get('project_id') != project or value.get('namespace') != namespace):
        raise SharedProjectError('shared_conversation_command_changed', status=409)
    identity = value['item_id']
    item = reader.read('workspace_items', identity)
    if (item is None or item.payload.get('id') != identity or item.payload.get('project_id') != project
            or item.payload.get('input_kind') != 'conversation'):
        raise SharedProjectError('shared_conversation_original_changed', status=409)
    verify_first_created(reader, value['birth'], collection='workspace_items', object_id=identity)
    return item


def _shared_conversation_commands(records, project, namespace, identity):
    fields = {'project_id': project, 'namespace': namespace, 'kind': 'conversation', 'action': 'share'}
    if identity is not None:
        fields['item_id'] = identity
    return records.list_matching(COMMANDS, **fields)


def shared_conversation_items(records, project, namespace, *, identity=None):
    with records.begin_read() as reader:
        result = {}
        for listed in _shared_conversation_commands(records, project, namespace, identity):
            command = reader.read(COMMANDS, listed.object_id)
            if command != listed:
                raise SharedProjectError('shared_conversation_command_changed', status=409)
            item = conversation_item(reader, command, project=project, namespace=namespace)
            if item.object_id in result:
                raise SharedProjectError('shared_conversation_command_changed', status=409)
            result[item.object_id] = item
        return result


@dataclass(frozen=True)
class SharedConversationItemSource:
    command: object

    @property
    def item_id(self):
        return self.command.payload['item_id']

    def read(self, reader, query):
        command = reader.read(COMMANDS, self.command.object_id)
        if command != self.command:
            raise SharedProjectError('shared_conversation_command_changed', status=409)
        return conversation_item(reader, command, project=query.origin_admission.lease.project_id,
            namespace=query.source_namespace)


@dataclass(frozen=True)
class SharedIntakeCommandSource:
    key: str
    actor_user_id: str
    item_id: str
    text: str

    def read(self, reader, query):
        identity = SharedIntake._identity(query, self.key)
        command = SharedIntake._command(reader, identity, query, self.key)
        if (command is None or command.revision != 1 or command.payload.get('actor_user_id') != self.actor_user_id
                or command.payload.get('kind') != 'text' or command.payload.get('input') != {'text': self.text}):
            raise SharedProjectError('shared_intake_command_changed', status=409)
        birth = command.payload.get('birth')
        if (command.payload.get('item_id') != self.item_id or not isinstance(birth, dict)
                or birth.get('collection') != 'workspace_items' or birth.get('object_id') != self.item_id
                or not isinstance(birth.get('fact_id'), str)):
            raise SharedProjectError('shared_intake_item_changed', status=409)
        fact = reader.read(FACTS, birth['fact_id'])
        first = reader.read(WITNESS_COLLECTIONS['workspace_items'], self.item_id)
        if (fact is None or fact.payload.get('origin') != 'created' or fact.payload.get('observed_revision') != 1
                or first is None or first.payload.get('first_fact_id') != birth['fact_id']):
            raise SharedProjectError('shared_intake_item_changed', status=409)
        item = SharedIntake._material(reader, command, query=query, key=self.key)
        if (item.object_id != self.item_id or item.payload.get('input_kind') != 'text'
                or item.payload.get('source_text') != self.text):
            raise SharedProjectError('shared_intake_item_changed', status=409)
        return item


class SharedIntake:
    def __init__(self, factory, context):
        if context.shared_query_factory is not factory or context.shared_projects is not factory.registry:
            raise SharedProjectError('shared_intake_owner_changed', status=409)
        self.factory, self.registry, self.context = factory, factory.registry, context

    def _query(self, application, actor, project, *, query=None):
        if query is None:
            query = self.factory.incoming_query(application, project, scope_kind='shared')
        else:
            with self.factory._lock:
                if query not in self.factory._issued_queries:
                    raise SharedProjectError('shared_intake_owner_changed', status=409)
            admission = query.origin_admission
            if (admission.registry is not self.registry or admission.application is not application
                    or admission.lease.project_id != project or query.origin_factory is not self.factory):
                raise SharedProjectError('shared_intake_owner_changed', status=409)
            query.validate_retrieval_origin()
        if query.origin_admission.actor != actor:
            raise SharedProjectError('shared_intake_actor_changed', status=403)
        return query

    @staticmethod
    def _identity(query, key):
        segment(key)
        admission = query.origin_admission
        return 'intake-' + uuid5(NAMESPACE_URL, '/'.join((
            admission.destination.owner_user_id, admission.destination.namespace, key))).hex

    @staticmethod
    def _command(reader, identity, query, key):
        row = reader.read(COMMANDS, identity)
        if row is not None:
            return row
        # 已保存的旧命令归实际目标空间所有；当前管理员或本人可读取同一原结果。
        previous = [entry for entry in reader.list(COMMANDS)
            if entry.payload.get('execution_owner_user_id') == query.origin_admission.destination.owner_user_id
            and entry.payload.get('project_id') == query.origin_admission.lease.project_id
            and entry.payload.get('namespace') == query.source_namespace and entry.payload.get('command_id') == key]
        if len(previous) > 1:
            raise SharedProjectError('shared_intake_command_conflict', status=409)
        return previous[0] if previous else None

    @staticmethod
    def _material(reader, command, *, query, key):
        admission = query.origin_admission
        payload = command.payload
        if (payload.get('project_id') != admission.lease.project_id
                or payload.get('execution_owner_user_id') != admission.destination.owner_user_id
                or payload.get('command_id') != key or payload.get('namespace') != query.source_namespace):
            raise SharedProjectError('shared_intake_command_changed', status=409)
        segment(payload.get('actor_user_id'))
        verify_lineage(reader, payload['birth'])
        collection = {'text': 'workspace_items', 'inspiration': 'recognition_candidates'}.get(payload.get('kind', 'text'))
        if collection is None:
            raise SharedProjectError('shared_intake_command_changed', status=409)
        item = reader.read(collection, payload['item_id'])
        if (item is None or (item.payload.get('scope', {}) if collection == 'recognition_candidates'
                else item.payload).get('project_id') != payload['project_id']):
            raise SharedProjectError('shared_intake_item_changed', status=409)
        return item

    def _view(self, reader, command, query, key):
        item = self._material(reader, command, query=query, key=key)
        value = {'command_id': key, 'state': 'accepted', 'project_id': query.origin_admission.lease.project_id,
            'scope_kind': 'shared', 'namespace': query.source_namespace,
            'kind': command.payload.get('kind', 'text')}
        if value['kind'] == 'text':
            value['item'] = WorkspaceItems(reader, None, RLock()).public_row(item)
        else:
            value['insight'] = {'id': item.object_id, 'revision': item.revision,
                'content': item.payload['content'], 'state': item.payload['state']}
        return value

    def command(self, application, actor, project, key):
        query = self._query(application, actor, project)
        identity = self._identity(query, key)
        with query.records.begin_read() as reader:
            row = self._command(reader, identity, query, key)
            if row is None or row.payload.get('kind') == 'conversation':
                raise SharedProjectError('shared_intake_not_received', status=404)
            return self._view(reader, row, query, key)

    def _conversation_target(self, query, *, namespace=None):
        admission = query.origin_admission
        lease = admission.lease
        if namespace is not None and namespace != lease.namespace:
            raise SharedProjectError('shared_conversation_destination_changed', status=409)
        authority = {'project_revision': lease.project_revision, 'member_revision': lease.member_revision,
            'account_revision': lease.account_revision}
        with self.registry.records.begin_read() as central:
            self.registry.authorize(central, admission.actor, lease.project_id, action='write', authority=authority)
            owner = central.read(PROJECTS, lease.project_id).payload['owner_user_id']
        return owner, authority

    def conversation_target(self, application, actor, project):
        query = self._query(application, actor, project)
        _, authority = self._conversation_target(query)
        return {'project_id': project, 'scope_kind': 'shared', 'namespace': query.source_namespace,
            'authority': authority}

    @staticmethod
    def _conversation_view(reader, command, query, key):
        admission = query.origin_admission
        lease = admission.lease
        if any(command.payload.get(field) != expected for field, expected in {
                'command_id': key, 'actor_user_id': admission.actor.user_id,
                'device_id': admission.actor.device_id, 'device_revision': admission.actor.revision,
                'execution_owner_user_id': admission.destination.owner_user_id,
                'execution_namespace': admission.destination.namespace}.items()):
            raise SharedProjectError('shared_conversation_command_changed', status=409)
        item = conversation_item(reader, command, project=lease.project_id, namespace=lease.namespace)
        return {'command_id': key, 'state': 'accepted', 'project_id': lease.project_id,
            'scope_kind': 'shared', 'namespace': lease.namespace, 'kind': 'conversation',
            'item': {'id': item.object_id, 'input_kind': 'conversation'}}

    def conversation_command(self, application, actor, project, key, namespace):
        query = self._query(application, actor, project)
        self._conversation_target(query, namespace=namespace)
        with query.records.begin_read() as reader:
            row = reader.read(COMMANDS, self._identity(query, key))
            if row is None:
                return {'state': 'not_accepted'}
            return self._conversation_view(reader, row, query, key)

    async def conversation(self, application, actor, project, body, key, *, prepare):
        query = self._query(application, actor, project)
        owner, authority = self._conversation_target(query, namespace=body['namespace'])
        identity = self._identity(query, key)
        with query.records.begin_read() as reader:
            old = reader.read(COMMANDS, identity)
            if old is not None:
                if old.payload.get('input') != body:
                    raise SharedProjectError('shared_intake_command_conflict', status=409)
                return self._conversation_view(reader, old, query, key)
        if body['authority'] != authority:
            raise SharedProjectError('shared_authority_changed', status=409)
        snapshot = await retain_until_finished(asyncio.to_thread(prepare, query.origin_admission.destination))
        async with self.context.jobs.intake_many((owner,)) as budgets:
            cap = budgets[owner].reserve_up_to(32 * len(snapshot[1].encode('utf-8')) + 65536)
            try:
                return await retain_until_finished(asyncio.to_thread(self._write,
                    query, owner, identity, key, body, cap, 'conversation', snapshot=snapshot, prepare=prepare))
            finally:
                cap.close()

    def item(self, application, actor, project, identity):
        query = self._query(application, actor, project)
        row = WorkspaceItems(query.records, None, RLock()).item_for(segment(identity), project)
        return {'project_id': project, 'scope_kind': 'shared', 'namespace': query.source_namespace,
            'item': WorkspaceItems(query.records, None, RLock()).public_row(row)}

    async def text(self, application, actor, project, body, key, *, query=None):
        return await self._run('text', application, actor, project, body, key, query=query)

    async def inspiration(self, application, actor, project, body, key):
        return await self._run('inspiration', application, actor, project, body, key)

    async def process(self, application, actor, project, identity, body, *, query=None, command_source=None):
        if (not isinstance(body, dict) or set(body) != {'expected_revision'}
                or type(body['expected_revision']) is not int or body['expected_revision'] < 1):
            raise SharedProjectError('shared_intake_fields_invalid')
        query = self._query(application, actor, project, query=query)
        identity = segment(identity)
        with self.registry.records.begin_read() as central:
            self.registry.authorize(central, actor, project, action='write')
            owner_user_id = central.read(PROJECTS, project).payload['owner_user_id']
        async def operation():
            from .shared_organize import SharedIntakeRecords, SharedOrganizeFactory
            # 整理确实写入两份资料根，沿原调度器分别保护执行者与来源所有者容量。
            async with self.context.jobs.intake_many((query.origin_admission.destination.owner_user_id, owner_user_id)) as budgets:
                current = query.records.read('workspace_items', identity)
                if (current is None or current.payload.get('project_id') != project
                        or current.payload.get('input_kind') not in {'text', 'conversation'}):
                    raise SharedProjectError('shared_intake_source_unavailable', status=409)
                qualified_source = command_source
                if current.payload['input_kind'] == 'conversation':
                    commands = _shared_conversation_commands(query.records, project, query.source_namespace, identity)
                    if command_source is not None or len(commands) != 1:
                        raise SharedProjectError('shared_intake_source_unavailable', status=409)
                    qualified_source = SharedConversationItemSource(commands[0])
                    with query.records.begin_read() as reader:
                        if qualified_source.read(reader, query) != current:
                            raise SharedProjectError('shared_intake_source_unavailable', status=409)
                cap = budgets[owner_user_id].reserve_up_to(32 * len(current.payload['source_text'].encode('utf-8')) + 1048576)
                try:
                    records = SharedIntakeRecords(query, owner_user_id, cap,
                        action='organize', identity=identity, title=current.payload['title'], command_source=qualified_source)
                    items = WorkspaceItems(records, ProcessingLease(records, 'workspace_items',
                        'shared-organize-' + identity), RLock())
                    factory = SharedOrganizeFactory(query, budgets[query.origin_admission.destination.owner_user_id])
                    intake = WorkspaceIntake(query.material_owner.root, items, query.models, source_factory=factory)
                    result = await intake.process(identity, {'project_id': project, **body})
                    return {'project_id': project, 'scope_kind': 'shared', 'namespace': query.source_namespace, 'item': result}
                finally:
                    cap.close()
        return await self.context.jobs.submit(query.origin_admission.destination.owner_user_id,
            'intake', operation, retain_running=True)

    async def _run(self, operation, application, actor, project, body, key, *, query=None):
        if not isinstance(body, dict) or not {'text'} <= body.keys() <= {'text', 'title'}:
            raise SharedProjectError('shared_intake_fields_invalid')
        if operation == 'inspiration' and set(body) != {'text'}:
            raise SharedProjectError('shared_intake_fields_invalid')
        text = _text(body['text'], 'text')
        _optional_title(body.get('title'))
        query = self._query(application, actor, project, query=query)
        identity = self._identity(query, key)
        with self.registry.records.begin_read() as central:
            self.registry.authorize(central, actor, project, action='write')
            owner_user_id = central.read(PROJECTS, project).payload['owner_user_id']
        # 容量锁与转让共用稳定用户顺序，统计全部根时尚未持有来源租约。
        async with self.context.jobs.intake_many((owner_user_id,)) as budgets:
            source_cap = budgets[owner_user_id].reserve_up_to(32 * len(text.encode('utf-8')) + 65536)
            try:
                return await retain_until_finished(asyncio.to_thread(self._write,
                    query, owner_user_id, identity, key, deepcopy(body), source_cap, operation))
            finally:
                source_cap.close()

    def _write(self, query, owner_user_id, identity, key, body, cap, operation, *, snapshot=None, prepare=None):
        admission, registry = query.origin_admission, self.registry
        lease = admission.lease
        admission.validate_execution_owner()
        authority = {'project_revision': lease.project_revision, 'member_revision': lease.member_revision,
            'account_revision': lease.account_revision}
        writer = SharedWriteRecords(registry, admission.actor, lease.project_id, authority=authority,
            action='remember', object_kind='insight' if operation == 'inspiration' else 'source', object_id=identity,
            object_title=snapshot[0][:80] if snapshot is not None else body.get('title') or body['text'].strip().splitlines()[0][:80])
        with writer.begin() as tx:
            reader = TransactionRecords(tx)
            capability = shared_read_capability(reader, registry=registry)
            data = capability._data
            current_owner = tx._central.read(PROJECTS, lease.project_id).payload['owner_user_id']
            if (capability.lease_payload() != lease.payload() or current_owner != owner_user_id
                    or data.namespace != lease.data_namespace or data.root_identity != lease.root_identity
                    or Path(data.records.database_path).resolve() != Path(reader.database_path).resolve()):
                raise SharedProjectError('shared_intake_owner_changed', status=409)
            with cap.using_reader(tx._central), persistence_budget(root=data.root,
                    databases=(data.records.database_path,), consume=cap.consume):
                old = (reader.read(COMMANDS, identity) if operation == 'conversation'
                    else self._command(reader, identity, query, key))
                if old is not None:
                    if old.payload.get('input') != body or old.payload.get('kind', 'text') != operation:
                        raise SharedProjectError('shared_intake_command_conflict', status=409)
                    if operation == 'conversation':
                        return self._conversation_view(reader, old, query, key)
                    return self._view(reader, old, query, key)
                items = WorkspaceItems(reader, ProcessingLease(reader, 'workspace_items', 'shared-intake'), RLock())
                def created(transaction, row):
                    extra = {}
                    if operation == 'conversation':
                        writer.object_id = row.object_id
                        extra = {'action': 'share', 'device_id': admission.actor.device_id,
                            'device_revision': admission.actor.revision, 'execution_namespace': admission.destination.namespace}
                        if snapshot[2]:
                            SourceEgressService(data.records, reader=transaction).set_policy(
                                WorkScope('local-user', lease.project_id), 'original_item', row.object_id, row.revision, 0, [])
                    transaction.put(COMMANDS, identity, {'command_id': key, 'project_id': lease.project_id,
                        'kind': operation,
                        'actor_user_id': admission.actor.user_id,
                        'execution_owner_user_id': admission.destination.owner_user_id,
                        'namespace': lease.namespace, 'input': deepcopy(body), 'item_id': row.object_id,
                        'birth': capture_lineage(transaction, row.collection, row.object_id), **extra}, expected_revision=0)
                if operation == 'text':
                    intake = WorkspaceIntake(data.root, items, query.models)
                    asyncio.run(intake.add_text({'project_id': lease.project_id, **body}, stage_created=created))
                elif operation == 'conversation':
                    if prepare(admission.destination, capability) != snapshot:
                        raise SharedProjectError('conversation_source_revision_changed', status=409)
                    title, markdown, _private = snapshot
                    items.create(lease.project_id, 'conversation', title, markdown, stage_created=created)
                else:
                    scope = WorkScope('local-user', lease.project_id)
                    service = recognition_service(reader, source_owner=writer)
                    experience = service.stage_experience(scope=scope, content=_text(body['text'], 'text'),
                        provenance={'kind': 'user_statement', 'actor': admission.actor.user_id})
                    candidate = service.propose(scope=scope, content=_text(body['text'], 'text'),
                        source_experience_ids=[experience])
                    created(reader, reader.read('recognition_candidates', candidate.id))
                command = reader.read(COMMANDS, identity)
                response = (self._conversation_view(reader, command, query, key) if operation == 'conversation'
                    else self._view(reader, command, query, key))
                cap.validate()
                tx.commit()
        return response
