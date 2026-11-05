import asyncio
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from backend.recognition import RecognitionConflict, WorkScope
from backend.security.audited_records import AuditedRecordStore
from backend.security.user_context import USER_ACCESS, UserAccess, UserError, user_action_allowed
from core.storage_provider.persistence_budget import persistence_budget
from core.storage_provider.record_lineage import capture_lineage

from ..original_sources import CONVERSATION_ARCHIVES as COPIES
from ..original_sources import TurnMaterialRevisionConflict, conversation_archive_item, original, resolve_turn_material
from ..server_jobs import QuotaError, retain_until_finished
from ..source_egress import POLICY_COLLECTIONS, SourceEgressService
from ..transaction_records import TransactionRecords
from ..workspace_contracts import _now, _project
from .conversation_share_snapshot import ConversationSnapshotRecords, read_snapshot, snapshot_markdown
from .material_origins import SharedProjectLease
from .privacy import is_private_project
from .server_deletion import _directory
from .server_users import DELETIONS, USERS, current_caller, enabled_user
from .shared_extraction import PersonalDestination
from .shared_projects import SharedProjectError, revision, segment
from .turn_logical_scope import TurnLogicalScope, logical_scope


SHARES = 'shared_turns'
COMMANDS = 'shared_turn_commands'


def _conflict(code):
    return SharedProjectError(code, status=409)


def _command_id(owner, key):
    if not isinstance(key, str) or len(key) > 64:
        raise SharedProjectError('conversation_command_invalid')
    return segment(owner + '--' + segment(key))


def _source_revision(value):
    if not isinstance(value, dict) or set(value) != {'turn_revision', 'content'}:
        raise SharedProjectError('conversation_source_revision_invalid')
    content = value['content']
    if not isinstance(content, dict) or set(content) != {'id', 'revision'}:
        raise SharedProjectError('conversation_source_revision_invalid')
    revision(value['turn_revision'])
    revision(content['revision'])
    segment(content['id'])


def _current_space(tx, access):
    if not isinstance(access, UserAccess) or USER_ACCESS.get() != access:
        raise UserError('user_unauthorized')
    actor, owner = access.caller, access.target_user_id
    caller = current_caller(tx, actor)
    if (not user_action_allowed(caller.payload['role'], actor.user_id, owner, 'space')
            or tx.read(USERS, owner) is None or tx.read(DELETIONS, owner) is not None
            or access.by != ('admin' if actor.user_id != owner else None)):
        raise UserError('user_forbidden')
    return owner


def _participant(tx, access, identity, side=None):
    owner = _current_space(tx, access)
    row = tx.read(SHARES, segment(identity))
    fields = {'sent': ('from_user_id',), 'inbox': ('to_user_id',),
        None: ('from_user_id', 'to_user_id')}[side]
    if row is None or owner not in tuple(row.payload[field] for field in fields):
        raise SharedProjectError('conversation_share_forbidden', status=403)
    return row


def _copy_item(reader, receipt, *, project, namespace):
    try:
        return conversation_archive_item(reader, receipt, project=project, namespace=namespace)
    except RecognitionConflict as error:
        raise _conflict('conversation_archive_binding_changed') from error


def conversation_originals(records, project, namespace, *, identity=None):
    fields = {'project_id': project}
    if identity is not None:
        fields['item_id'] = identity
    candidates = records.list_matching(COPIES, **fields)
    if not candidates:
        return {}
    with records.begin_read() as transaction:
        return {item.object_id: item for candidate in candidates
            if (receipt := transaction.read(COPIES, candidate.object_id)) == candidate
            and (item := _copy_item(transaction, receipt, project=project, namespace=namespace)) is not None}


def _protect_archive_item(records, reader, item):
    identity, project = item.object_id, item.payload['project_id']
    previous = reader.read(POLICY_COLLECTIONS['original_item'], identity)
    if previous is None or previous.payload.get('allowed_purposes') != []:
        SourceEgressService(records, reader=reader).set_policy(WorkScope('local-user', project),
            'original_item', identity, item.revision, previous.revision if previous else 0, [])


def protect_conversation_archive(records, item):
    if not isinstance(records, AuditedRecordStore):
        return False
    project = item.payload['project_id']
    copies = records.list_matching(COPIES, project_id=project, item_id=item.object_id)
    if not copies:
        return False
    with records.begin() as transaction:
        copy = transaction.read(COPIES, copies[0].object_id)
        if len(copies) != 1 or copy != copies[0] or _copy_item(
                transaction, copy, project=project, namespace=records.namespace) != item:
            raise RecognitionConflict('conversation_archive_binding_changed')
        _protect_archive_item(records, transaction, item)
        transaction.commit()
    return True


def _target_stamp(registry, application, user_id):
    state = application.state
    root = registry.users.server_root / 'users' / user_id
    records = state.recognition_records
    path = Path(records.database_path).resolve(strict=True)
    if (getattr(state, 'server_user_id', None) != user_id or state.recognition_runtime_root != root
            or state.server_context.users is not registry.users or getattr(records, 'user_id', None) != user_id
            or not path.is_relative_to(root) or not path.is_file()):
        raise _conflict('conversation_archive_destination_changed')
    root_stat, db_stat = _directory(root), path.stat()
    return {'namespace': state.recognition_documents.namespace_id,
            'root_identity': [root_stat.st_dev, root_stat.st_ino],
            'database_identity': [db_stat.st_dev, db_stat.st_ino]}


class ConversationShares:
    collections = frozenset({SHARES, COMMANDS})

    def __init__(self, registry, context, library, intake):
        self.registry, self.context, self.library = registry, context, library
        self.records, self.intake = registry.records, intake

    def _command(self, tx, access, action, key, request):
        owner = _current_space(tx, access)
        identity = _command_id(owner, key)
        previous = tx.read(COMMANDS, identity)
        if previous is None:
            return identity, None
        value = previous.payload
        if (value['owner_user_id'] != owner or value['key'] != key
                or value['action'] != action or value['request'] != request):
            raise _conflict('conversation_command_conflict')
        return identity, _participant(tx, access, value['share_id'])

    @staticmethod
    def _record_command(tx, identity, access, action, key, request, share_id):
        attribution = ({'by': 'admin', 'actor_user_id': access.caller.user_id,
            'actor_device_id': access.caller.device_id} if access.by == 'admin' else {})
        tx.put(COMMANDS, identity, {'owner_user_id': access.target_user_id, 'key': key,
            'action': action, 'request': request, 'share_id': share_id, **attribution}, expected_revision=0)

    @staticmethod
    def _state(value):
        if value['revoked_at'] is not None:
            return 'revoked'
        if value['filed_at'] is not None:
            return 'filed'
        return 'filing' if value['filing'] is not None else 'received'

    def _view(self, tx, row, access, *, content=True):
        value = row.payload
        owner = access.target_user_id
        sender = value['from_user_id'] == owner
        counterpart = value['to_user_id'] if sender else value['from_user_id']
        user = tx.read(USERS, counterpart)
        state = self._state(value)
        result = {'id': row.object_id, 'revision': row.revision, 'state': state,
            'direction': 'sent' if sender else 'inbox', 'user_id': counterpart,
            'user_name': user.payload['name'] if user is not None else None,
            'created_at': value['created_at'],
            'revoked_at': value['revoked_at'], 'filed_at': value['filed_at'],
            'can_revoke': sender and state in {'received', 'filing'},
            'can_file': not sender and state == 'received'}
        if sender or state != 'revoked':
            result['title'] = value['snapshot']['question']
        if sender:
            result['source'] = {key: value['source'][key] for key in ('turn_id', 'project_id', 'scope_kind')}
        elif value['archive'] is not None:
            result['archive'] = dict(value['archive'])
        elif state == 'filing':
            pending = tx.read(COMMANDS, value['filing']['command_id'])
            if (pending is None or pending.payload['share_id'] != row.object_id
                    or pending.payload['owner_user_id'] != owner or pending.payload['action'] != 'file'):
                raise _conflict('conversation_archive_binding_changed')
            result['filing'] = {'command_key': pending.payload['key'],
                'project_id': pending.payload['request']['project_id'],
                'expected_revision': pending.payload['request']['expected_revision']}
        if content and state != 'revoked':
            result['snapshot'] = value['snapshot']
            if not sender:
                result['citation_details'] = self._citation_details(tx, access.caller, value['anchors'])
        return result

    def _citation_details(self, tx, actor, anchors):
        eligible = {}
        for anchor in anchors:
            if anchor is None or anchor['project_id'] in eligible:
                continue
            try:
                self.registry.authorize(tx, actor, anchor['project_id'])
            except SharedProjectError:
                eligible[anchor['project_id']] = False
            else:
                eligible[anchor['project_id']] = True
        return [index for index, anchor in enumerate(anchors) if anchor is not None and eligible[anchor['project_id']]]

    def list(self, access, *, side, turn_id=None):
        if side not in {'sent', 'inbox'} or turn_id is not None and side != 'sent':
            raise SharedProjectError('conversation_share_filter_invalid')
        if turn_id is not None:
            segment(turn_id)
        with self.records.begin_read() as tx:
            owner = _current_space(tx, access)
            field = 'from_user_id' if side == 'sent' else 'to_user_id'
            candidates = self.records.list_matching(SHARES, **{field: owner})
            rows = [row for candidate in candidates if (row := tx.read(SHARES, candidate.object_id)) is not None
                and row.payload[field] == owner and (turn_id is None or row.payload['source']['turn_id'] == turn_id)
                and (side == 'sent' or self._state(row.payload) in {'received', 'filing'})]
            return {'items': [self._view(tx, row, access, content=False) for row in
                sorted(rows, key=lambda row: (row.payload['created_at'], row.object_id), reverse=True)]}

    def detail(self, access, identity):
        with self.records.begin_read() as tx:
            row = _participant(tx, access, identity)
            if row.payload['to_user_id'] == access.target_user_id and row.payload['revoked_at'] is not None:
                raise SharedProjectError('conversation_share_unavailable', status=404)
            return self._view(tx, row, access)

    def _command_status(self, access, key):
        with self.records.begin_read() as tx:
            owner = _current_space(tx, access)
            row = tx.read(COMMANDS, _command_id(owner, key))
            if row is None:
                return {'state': 'not_accepted'}, None
            value = row.payload
            if value['owner_user_id'] != owner or value['key'] != key:
                raise _conflict('conversation_command_conflict')
            share = _participant(tx, access, value['share_id'])
            return {'action': value['action'], 'share': self._view(tx, share, access)}, value['request']

    async def command(self, access, key):
        result, request = await retain_until_finished(asyncio.to_thread(self._command_status, access, key))
        if result.get('action') != 'file' or result['share']['state'] != 'filing':
            return result
        async with self.context.pool.lease(access.target_user_id) as child:
            recovered = await retain_until_finished(asyncio.to_thread(
                self._recover_archive, child, access, request['share_id'], key, request))
        return {'action': 'file', 'share': recovered} if recovered is not None else result

    def _snapshot(self, application, destination, turn_id, project, kind, *, central, actor, selected=()):
        destination.validate()
        with destination.records.begin_read() as transaction:
            evidence = {} if kind == 'shared' else None
            snapshot, anchors, source_revision = read_snapshot(application, destination,
                ConversationSnapshotRecords(transaction), segment(turn_id), _project(project), logical_scope(kind),
                evidence=evidence)
            if snapshot['citations'] and evidence is not None and 'shared_answer_leases' in evidence:
                if not self._shared_answer_readable(central, actor, project, evidence['shared_answer_leases']):
                    if selected:
                        raise _conflict('conversation_source_revision_changed')
                    snapshot, anchors = {**snapshot, 'citations': []}, []
        destination.validate()
        return snapshot, anchors, source_revision

    def _shared_answer_readable(self, central, actor, project, leases):
        if not leases:
            return False
        try:
            self.registry.authorize(central, actor, project)
            with self.registry.roots.lease(project):
                data = self.registry.roots._data(project)
                return all(lease.project_id == project and lease.data_namespace == data.namespace
                    and lease.root_identity == tuple(data.root_identity) for lease in leases)
        except (SharedProjectError, OSError):
            return False

    def _destination(self, child, access):
        return PersonalDestination.from_application(self.registry, access.caller, child,
            target_user_id=access.target_user_id)

    def _preview(self, child, access, turn_id, project, kind):
        destination = self._destination(child, access)
        with self.records.begin_read() as central:
            snapshot, _, source_revision = self._snapshot(child, destination, turn_id, project, kind,
                central=central, actor=access.caller)
        return {'snapshot': snapshot, 'source_revision': source_revision}

    async def preview(self, access, turn_id, project, kind):
        async with self.context.pool.lease(access.target_user_id) as child:
            return await retain_until_finished(asyncio.to_thread(self._preview, child, access, turn_id, project, kind))

    async def project_target(self, access, project):
        async with self.context.pool.lease(access.target_user_id) as child:
            return await retain_until_finished(asyncio.to_thread(
                self.intake.conversation_target, child, access.caller, segment(project)))

    async def project_command(self, access, project, key, namespace):
        _command_id(access.target_user_id, key)
        async with self.context.pool.lease(access.target_user_id) as child:
            return await retain_until_finished(asyncio.to_thread(
                self.intake.conversation_command, child, access.caller, segment(project), key, namespace))

    async def share_project(self, access, project, key, request):
        _command_id(access.target_user_id, key)
        segment(request['turn_id'])
        _project(request['project_id'])
        _source_revision(request['source_revision'])
        ordinals = request['include_citations']
        if (request['scope_kind'] not in ('personal', 'shared') or not isinstance(ordinals, list)
                or any(type(index) is not int or index < 0 for index in ordinals) or ordinals != sorted(set(ordinals))
                or not isinstance(request['namespace'], str) or not request['namespace']
                or not isinstance(request['authority'], dict)):
            raise SharedProjectError('conversation_share_request_invalid')
        async with self.context.pool.lease(access.target_user_id) as child:
            return await self.intake.conversation(child, access.caller, segment(project), request, key,
                prepare=lambda destination, target=None: self._project_snapshot(child, destination, access, request, target))

    @contextmanager
    def _project_source(self, actor, project, target):
        if target is not None:
            lease = SharedProjectLease.from_payload(target.lease_payload())
            if target.actor != actor:
                raise _conflict('conversation_source_revision_changed')
            if lease.project_id == project:
                yield lease, target.records
                target.validate(registry=self.registry)
                return
        lease = SharedProjectLease.acquire(self.registry, actor, project)
        with lease.current(self.registry, actor) as source:
            yield lease, source.records
            source.validate_current()

    def _project_snapshot(self, child, destination, access, request, target=None):
        destination.validate_application(self.registry, child)
        turn_id, project, kind = (request[field] for field in ('turn_id', 'project_id', 'scope_kind'))
        ordinals = request['include_citations']
        with destination.records.begin_read() as transaction:
            reader = ConversationSnapshotRecords(transaction)
            evidence = {}
            snapshot, anchors, source_revision = read_snapshot(child, destination, reader, turn_id, project, kind,
                evidence=evidence)
            if source_revision != request['source_revision']:
                raise _conflict('conversation_source_revision_changed')
            if any(index >= len(anchors) for index in ordinals):
                raise SharedProjectError('conversation_citation_invalid')
            private = not evidence.get('unassisted_empty_answer', False)
            if kind == 'shared':
                row = reader.read('v2_turns', turn_id)
                with self._project_source(access.caller, project, target) as (lease, source_reader):
                    private = private or is_private_project(source_reader, project)
                    if row.payload['intent'] == 'remember':
                        from .shared_workbench_remember import read_shared_remember
                        source = read_shared_remember(reader, row, owner_user_id=destination.owner_user_id,
                            namespace_id=destination.namespace)
                        qualified = source is not None and source['namespace'] == lease.namespace
                    else:
                        historical = []
                        actual_kind = TurnLogicalScope(child).read(row, reader=reader, shared_leases=historical)
                        qualified = (actual_kind == 'shared' and bool(historical)
                            and all((old.namespace, old.root_identity) == (lease.namespace, lease.root_identity)
                                for old in historical))
                    if not qualified:
                        raise _conflict('conversation_source_revision_changed')
                    for index in ordinals:
                        anchor = anchors[index]
                        if anchor is None:
                            if index in evidence.get('static_ordinals', ()):
                                continue
                            raise SharedProjectError('conversation_citation_static', status=403)
                        self._require_citation(source_reader, project, lease.namespace, anchor)
            else:
                private = private or is_private_project(reader, project)
        destination.validate()
        snapshot['citations'] = [snapshot['citations'][index] for index in ordinals]
        return snapshot['question'].strip().splitlines()[0][:200], snapshot_markdown(snapshot), private

    async def send(self, access, key, request):
        turn_id = segment(request['turn_id'])
        project, kind = _project(request['project_id']), request['scope_kind']
        recipient = segment(request['to_user_id'])
        _source_revision(request['source_revision'])
        ordinals = request['include_citations']
        if (kind not in ('personal', 'shared') or recipient == access.target_user_id or not isinstance(ordinals, list)
                or any(type(index) is not int or index < 0 for index in ordinals)
                or ordinals != sorted(set(ordinals))):
            raise SharedProjectError('conversation_share_request_invalid')
        with self.records.begin_read() as central:
            _, existing = self._command(central, access, 'send', key, request)
            if existing is not None:
                return self._view(central, existing, access)
        async with self.context.pool.lease(access.target_user_id) as child:
            return await retain_until_finished(asyncio.to_thread(
                self._send, child, access, key, request, turn_id, project, kind, recipient, ordinals))

    def _send(self, child, access, key, request, turn_id, project, kind, recipient, ordinals):
        destination = self._destination(child, access)
        with self.records.begin() as central:
            command, existing = self._command(central, access, 'send', key, request)
            if existing is not None:
                return self._view(central, existing, access)
            if enabled_user(central, recipient) is None:
                raise SharedProjectError('conversation_recipient_unavailable', status=403)
            snapshot, anchors, source_revision = self._snapshot(child, destination, turn_id, project, kind,
                central=central, actor=access.caller, selected=ordinals)
            if source_revision != request['source_revision']:
                raise _conflict('conversation_source_revision_changed')
            if any(index >= len(anchors) for index in ordinals):
                raise SharedProjectError('conversation_citation_invalid')
            snapshot['citations'] = [snapshot['citations'][index] for index in ordinals]
            identity = 'share-' + uuid4().hex
            row = central.put(SHARES, identity, {'from_user_id': access.target_user_id, 'to_user_id': recipient,
                'source': {'turn_id': turn_id, 'project_id': project, 'scope_kind': kind,
                    'revision': source_revision}, 'snapshot': snapshot, 'anchors': [anchors[index] for index in ordinals],
                'created_at': _now(), 'revoked_at': None, 'filed_at': None, 'filing': None, 'archive': None},
                expected_revision=0)
            self._record_command(central, command, access, 'send', key, request, identity)
            result = self._view(central, row, access)
            central.commit()
            return result

    def _copy(self, reader, row, namespace):
        receipt = reader.read(COPIES, row.object_id)
        if receipt is None:
            return None
        value = row.payload
        filing = value['filing']
        item = _copy_item(reader, receipt, project=filing['project_id'], namespace=namespace)
        if (item is None or receipt.payload.get('command_id') != filing['command_id']
                or item.payload.get('source_text') != snapshot_markdown(value['snapshot'])):
            raise _conflict('conversation_archive_binding_changed')
        return {'item_id': item.object_id, 'project_id': filing['project_id']}

    def _finish_archive(self, central, row, archive):
        return central.put(SHARES, row.object_id, {**row.payload, 'filing': None,
            'archive': archive, 'filed_at': _now()}, expected_revision=row.revision)

    def _check_filing(self, child, row):
        current = _target_stamp(self.registry, child, row.payload['to_user_id'])
        if current != row.payload['filing']['destination']:
            raise _conflict('conversation_archive_destination_changed')
        return current['namespace']

    def _recover_archive(self, child, access, identity, key, request):
        with self.records.begin() as central:
            row = _participant(central, access, identity, 'inbox')
            _, old = self._command(central, access, 'file', key, request)
            if old is None:
                if row.payload['revoked_at'] is not None or self._state(row.payload) != 'received':
                    raise _conflict('conversation_share_cannot_file')
                if row.revision != request['expected_revision']:
                    raise _conflict('conversation_share_revision_changed')
                return None
            if old != row:
                raise _conflict('conversation_command_conflict')
            if row.payload['filing'] is None:
                return self._view(central, row, access)
            namespace = self._check_filing(child, row)
            with child.state.recognition_records.begin_read() as transaction:
                archive = self._copy(transaction, row, namespace)
            if archive is None:
                return None
            updated = self._finish_archive(central, row, archive)
            result = self._view(central, updated, access)
            central.commit()
            return result

    async def archive(self, access, identity, key, *, project, expected_revision):
        project = _project(project)
        request = {'share_id': segment(identity), 'project_id': project, 'expected_revision': revision(expected_revision)}
        owner = access.target_user_id
        async with self.context.pool.lease(owner) as child:
            recovered = await retain_until_finished(asyncio.to_thread(
                self._recover_archive, child, access, identity, key, request))
            if recovered is not None:
                return recovered
            try:
                async with self.context.jobs.intake_many((owner,)) as budgets:
                    return await retain_until_finished(asyncio.to_thread(
                        self._archive, child, access, identity, key, request, budgets[owner]))
            except QuotaError:
                recovered = await retain_until_finished(asyncio.to_thread(
                    self._recover_archive, child, access, identity, key, request))
                if recovered is None:
                    raise
                return recovered

    def _reserve_archive(self, child, access, identity, key, request):
        with self.records.begin() as central:
            row = _participant(central, access, identity, 'inbox')
            command, old = self._command(central, access, 'file', key, request)
            if old is not None:
                if old != row:
                    raise _conflict('conversation_command_conflict')
                return row
            if row.revision != request['expected_revision'] or self._state(row.payload) != 'received':
                raise _conflict('conversation_share_revision_changed')
            project = request['project_id']
            _require_project(child.state.recognition_records, project)
            destination = _target_stamp(self.registry, child, access.target_user_id)
            updated = central.put(SHARES, identity, {**row.payload, 'filing': {'command_id': command,
                'project_id': project, 'destination': destination}}, expected_revision=row.revision)
            self._record_command(central, command, access, 'file', key, request, identity)
            central.commit()
            return updated

    def _archive(self, child, access, identity, key, request, budget):
        destination = self._destination(child, access)
        with self.records.begin_read() as central:
            observed = _participant(central, access, identity, 'inbox')
            body = snapshot_markdown(observed.payload['snapshot'])
        cap = budget.reserve_up_to(32 * len(body.encode('utf-8')) + 65536)
        try:
            reserved = self._reserve_archive(child, access, identity, key, request)
            with self.records.begin() as central:
                row = _participant(central, access, identity, 'inbox')
                if row != reserved or row.payload['filing'] is None:
                    if row.payload['filed_at'] is not None or row.payload['revoked_at'] is not None:
                        return self._view(central, row, access)
                    raise _conflict('conversation_share_revision_changed')
                namespace = self._check_filing(child, row)
                project, command = request['project_id'], row.payload['filing']['command_id']
                with cap.using_reader(central), persistence_budget(root=destination.root,
                        databases=(destination.records.database_path,), consume=cap.consume):
                    with destination.records.begin() as transaction:
                        _require_project(transaction, project)
                        archive = self._copy(transaction, row, namespace)
                        if archive is None:
                            items = destination.workspace.items.with_records(TransactionRecords(transaction))
                            def created(reader, item):
                                reader.put(COPIES, identity, {'schema_version': 1, 'share_id': identity,
                                    'command_id': command, 'project_id': project, 'namespace': namespace,
                                    'item_id': item.object_id, 'birth': capture_lineage(reader, item.collection, item.object_id)},
                                    expected_revision=0)
                                _protect_archive_item(destination.records, reader, item)
                            title = row.payload['snapshot']['question'].strip().splitlines()[0][:200]
                            items.create(project, 'conversation', title, body, stage_created=created)
                            archive = self._copy(transaction, row, namespace)
                            if archive is None:
                                raise _conflict('conversation_archive_binding_changed')
                            self._check_filing(child, row)
                            cap.validate()
                            transaction.commit()
                updated = self._finish_archive(central, row, archive)
                result = self._view(central, updated, access)
                central.commit()
                return result
        finally:
            cap.close()

    async def revoke(self, access, identity, key, *, expected_revision):
        request = {'share_id': segment(identity), 'expected_revision': revision(expected_revision)}
        with self.records.begin_read() as central:
            row = _participant(central, access, identity, 'sent')
            _, existing = self._command(central, access, 'revoke', key, request)
            if existing is not None:
                return self._view(central, existing, access)
            needs_check = row.payload['filing'] is not None
        if needs_check:
            async with self.context.pool.lease(row.payload['to_user_id']) as child:
                return await retain_until_finished(asyncio.to_thread(self._revoke, child, access, identity, key, request))
        return await retain_until_finished(asyncio.to_thread(self._revoke, None, access, identity, key, request))

    def _revoke(self, child, access, identity, key, request):
        with self.records.begin() as central:
            row = _participant(central, access, identity, 'sent')
            command, existing = self._command(central, access, 'revoke', key, request)
            if existing is not None:
                return self._view(central, existing, access)
            if row.revision != request['expected_revision'] or row.payload['revoked_at'] is not None:
                raise _conflict('conversation_share_revision_changed')
            if row.payload['filed_at'] is not None:
                raise _conflict('conversation_share_already_filed')
            if row.payload['filing'] is not None:
                if child is None:
                    raise _conflict('conversation_archive_requires_check')
                namespace = self._check_filing(child, row)
                # 中央写锁覆盖原件存在检查与撤回，阻止归档继续沿旧预约写入。
                with child.state.recognition_records.begin_read() as transaction:
                    archive = self._copy(transaction, row, namespace)
                if archive is not None:
                    self._finish_archive(central, row, archive)
                    central.commit()
                    raise _conflict('conversation_share_already_filed')
            updated = central.put(SHARES, identity, {**row.payload, 'revoked_at': _now(), 'filing': None},
                expected_revision=row.revision)
            self._record_command(central, command, access, 'revoke', key, request, identity)
            result = self._view(central, updated, access)
            central.commit()
            return result

    def _citation(self, access, identity, ordinal):
        with self.records.begin_read() as central:
            row = _participant(central, access, identity, 'inbox')
            if row.payload['revoked_at'] is not None:
                raise SharedProjectError('conversation_share_unavailable', status=404)
            anchors = row.payload['anchors']
            if not 0 <= ordinal < len(anchors) or anchors[ordinal] is None:
                raise SharedProjectError('conversation_citation_static', status=403)
            return anchors[ordinal]

    async def drill(self, access, identity, ordinal):
        async with self.context.pool.lease(access.target_user_id) as child:
            return await retain_until_finished(asyncio.to_thread(self._drill, child, access, identity, ordinal))

    @staticmethod
    def _require_citation(reader, project, namespace, anchor):
        from .material_origin_sql import _identity as current_sql_identity

        material = anchor['material']
        if namespace != anchor['namespace'] or project != anchor['project_id']:
            raise SharedProjectError('conversation_citation_static', status=403)
        scope = WorkScope('local-user', project)
        try:
            resolve_turn_material(reader, scope, material)
        except TurnMaterialRevisionConflict as error:
            raise _conflict('conversation_citation_changed') from error
        if material['type'] == 'original_source':
            source = original(reader, scope, material['type'], material['id'])
            if source.payload['_original_incarnation'] != anchor['incarnation']:
                raise _conflict('conversation_citation_changed')
        else:
            frozen = anchor['sql_identity']
            if current_sql_identity(reader, namespace, frozen['collection'],
                    frozen['object_id']) != frozen:
                raise _conflict('conversation_citation_changed')

    def _drill(self, child, access, identity, ordinal):
        anchor = self._citation(access, identity, ordinal)
        project, material = anchor['project_id'], anchor['material']
        with self.library._projection(child, access.caller, project) as (query, reader, _):
            self._require_citation(query.records, project, query.source_namespace, anchor)
            layer = anchor['layer']
            result = {'project_id': project, 'namespace': query.source_namespace, 'scope_kind': 'shared',
                'layer': layer, 'id': material['id']}
            if layer == 'source':
                source_view = reader.full_source(project, material['id'])
                result.update({key: source_view[key] for key in ('title', 'kind', 'text')})
            else:
                view = reader.drill(project, layer, material['id'])
                selected = view.get(layer)
                if selected is None:
                    raise _conflict('conversation_citation_changed')
                result.update(title=selected.get('title'), text=selected['markdown' if layer == 'note' else 'text'])
                if view.get('source_project_id', project) == project:
                    result['originals'] = [{key: source[key] for key in ('id', 'title', 'kind')}
                        | {'text_url': '/api/v2/server/shared-projects/' + segment(project)
                            + '/library/sources/' + segment(source['id']) + '/text'} for source in view['sources']]
        if self._citation(access, identity, ordinal) != anchor:
            raise _conflict('conversation_citation_changed')
        return result

    def validate_related(self, tx, *, user_id, device_ids):
        for row in tx.list(COMMANDS):
            value = row.payload
            share = tx.read(SHARES, value['share_id'])
            action = value['action']
            field = 'to_user_id' if action == 'file' else 'from_user_id'
            if (share is None or action not in {'send', 'file', 'revoke'}
                    or value['owner_user_id'] != share.payload[field]
                    or _command_id(value['owner_user_id'], value['key']) != row.object_id):
                raise _conflict('conversation_share_related_data_invalid')

    def cleanup_related(self, tx, *, user_id, device_ids):
        self.validate_related(tx, user_id=user_id, device_ids=device_ids)
        shares = [row for row in tx.list(SHARES)
            if user_id in (row.payload['from_user_id'], row.payload['to_user_id'])]
        ids = {row.object_id for row in shares}
        for row in tx.list(COMMANDS):
            if row.payload['share_id'] in ids:
                tx.delete(COMMANDS, row.object_id, expected_revision=row.revision)
        for row in shares:
            tx.delete(SHARES, row.object_id, expected_revision=row.revision)


def _require_project(reader, project):
    if project not in {'default', 'inbox', 'me'} and reader.read('v2_projects', project) is None:
        raise SharedProjectError('conversation_archive_project_unavailable', status=404)
