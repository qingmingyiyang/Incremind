import asyncio
from contextlib import contextmanager
from copy import deepcopy
from threading import RLock
from uuid import NAMESPACE_URL, uuid5

from backend.recognition import WorkScope
from backend.recognition.external_input_dependencies import COLLECTION as DEPENDENCIES
from backend.shared.secret_detection import redact_secrets
from core.storage_provider.record_lineage import capture_lineage, verify_first_created
from starlette.concurrency import run_in_threadpool

from ..source_egress import recognition_service
from ..server_jobs import retain_until_finished
from ..transaction_records import TransactionRecords
from ..workspace_contracts import _now
from ..workspace_intake import WorkspaceIntake
from ..workspace_items import WorkspaceItems
from .external_agent_guard import _WRITE_RESERVATIONS
from .external_context import SHARED_ARCHIVE_VERSION, ExternalContextError, _encoded
from .material_origin_readers import qualified_record_read
from .mcp_intake import INTAKES
from .projects import assign_scene
from .shared_auxiliary import _qualified_commit
from .shared_intake import COMMANDS, SharedIntake
from .shared_mcp_materials import _delivery
from .shared_organize import SharedIntakeRecords
from .shared_projects import PROJECTS


OPERATIONS = 'v2_shared_mcp_intake_operations'


class _Replay(Exception):
    pass


class _RedactingItems(WorkspaceItems):
    def create(self, project_id, kind, title, source_text, **extra):
        return super().create(project_id, kind, redact_secrets(title), redact_secrets(source_text), **extra)


class _McpIntakeRecords(SharedIntakeRecords):
    def __init__(self, producer, operation, query, source_owner, cap):
        self.producer, self.operation = producer, operation
        title = operation.input.get('text', '外部原件').splitlines()[0][:80]
        super().__init__(query, source_owner, cap, action=operation.tool, identity=operation.command, title=title)

    @contextmanager
    def begin(self):
        op, producer = self.operation, self.producer
        with super().begin() as tx, _qualified_commit(self.query.origin_admission), producer.api.records.begin() as actor_tx:
            producer._qualified(op, self.query, tx._central, tx, actor_tx)
            producer._operation(actor_tx, op)
            if producer._command(tx, self.query, op) is not None:
                raise _Replay()
            producer._validate_evidence(op, self.query, tx, actor_tx)
            yield tx


class _Operation:
    def __init__(self, query, native, tool, client, arguments, scene):
        admission = query.origin_admission
        self.native, self.tool, self.client, self.scene = native, tool, client, scene
        self.project = admission.lease.project_id
        self.namespace = query.source_namespace
        self.kind = 'text' if tool == 'remember' else 'inspiration'
        self.input = {key: redact_secrets(arguments[key]) for key in ('text', 'url') if key in arguments}
        if tool == 'propose_insight':
            self.input['conditions'] = [redact_secrets(value) for value in arguments.get('conditions', [])]
        if scene is not None:
            self.input['scene'] = scene
        self.references = deepcopy(arguments.get('evidence_ids', []))
        self.evidence = []
        key = '/'.join((admission.personal['namespace'], admission.actor.user_id, self.namespace,
            client, arguments['idempotency_key']))
        self.command = 'mcp-command-' + uuid5(NAMESPACE_URL, key).hex
        self.receipt = 'external-intake-' + uuid5(NAMESPACE_URL, 'receipt/' + key).hex
        self.source_id = SharedIntake._identity(query, self.command)
        self.fact = {'schema_version': 1, 'namespace': self.namespace, 'project_id': self.project,
            'actor_user_id': admission.actor.user_id, 'execution_owner_user_id': admission.destination.owner_user_id,
            'command_id': self.command, 'client': client, 'tool': tool,
            'evidence_ids': self.references,
            'input_id': uuid5(NAMESPACE_URL, _encoded({'input': self.input, 'references': self.references})).hex}


class SharedMcpIntake:
    def __init__(self, shared):
        self.shared, self.api, self.authority = shared, shared.context, shared.authority

    async def run(self, client, tool, arguments, project, scene):
        native, actor = self.authority.current(client, tool)
        query = await run_in_threadpool(self.authority.factory.incoming_query, self.authority.application,
            project, scope_kind='shared')
        if query.origin_admission.actor != actor:
            raise ExternalContextError('external_context_owner_changed')
        op = _Operation(query, native, tool, client, arguments, scene)
        recovered, owner = await run_in_threadpool(self._admit, op, query, False)
        if recovered is not None:
            return recovered
        op.evidence = await run_in_threadpool(self._evidence, op)
        recovered, owner = await run_in_threadpool(self._admit, op, query, True)
        if recovered is not None:
            return recovered
        context = self.authority.application.state.server_context
        maximum = 32 * len(_encoded({'input': op.input, 'evidence': op.evidence}).encode('utf-8')) + 65536
        if 'url' in op.input:
            maximum += 32 * 1048576
        async with context.jobs.intake_many((owner,)) as budgets:
            cap = budgets[owner].reserve_up_to(maximum)
            try:
                await retain_until_finished(asyncio.to_thread(self._write, op, query, owner, cap))
            finally:
                cap.close()
        result, _ = await run_in_threadpool(self._admit, op, query, False)
        if result is None:
            raise ExternalContextError('external_context_binding_invalid')
        return result

    def _qualified(self, op, query, central, source, actor_reader):
        lease = query.origin_admission.lease
        actor = self.authority.validate(central, op.native, op.client, op.tool)
        if actor != query.origin_admission.actor:
            raise ExternalContextError('external_context_owner_changed')
        self.authority.registry.authorize(central, actor, op.project, action='write',
            authority={'project_revision': lease.project_revision, 'member_revision': lease.member_revision,
                'account_revision': lease.account_revision})
        self.authority.require_external(actor_reader, op.client, project=op.project, source_reader=source)
        query.validate_retrieval_origin()

    def _operation(self, tx, op, *, required=True):
        row = tx.read(OPERATIONS, op.receipt)
        reservation = tx.read(_WRITE_RESERVATIONS, op.receipt)
        if row is None:
            if required or reservation is not None:
                raise ExternalContextError('external_context_binding_invalid')
            return None
        if row.revision != 1 or _encoded(row.payload) != _encoded(op.fact):
            raise ExternalContextError('external_context_binding_invalid')
        self.api.guard._validate_reservation(tx, reservation, 'receipt_id', op.receipt, op.fact)
        return row

    def _command(self, tx, query, op):
        command = SharedIntake._command(tx, op.source_id, query, op.command)
        if command is None:
            return None
        value = command.payload
        if (command.revision != 1 or value.get('kind') != op.kind or value.get('external_tool') != op.tool
                or value.get('actor_user_id') != op.fact['actor_user_id']
                or value.get('input_id') != op.fact['input_id'] or value.get('input') != op.input
                or type(value.get('object_revision')) is not int or value['object_revision'] != 1):
            raise ExternalContextError('external_context_binding_invalid')
        row = SharedIntake._material(tx, command, query=query, key=op.command)
        verify_first_created(tx, value['birth'], collection=row.collection, object_id=row.object_id)
        return command

    def _admit(self, op, query, reserve):
        with (self.authority.registry.records.begin() as central, self.api.records.begin() as tx,
                qualified_record_read(query.records, validate=query.validate_retrieval_origin) as source):
            self._qualified(op, query, central, source, tx)
            owner = central.read(PROJECTS, op.project).payload['owner_user_id']
            prior = self._operation(tx, op, required=False)
            command = self._command(source, query, op)
            if command is not None:
                if prior is None:
                    raise ExternalContextError('external_context_binding_invalid')
                result = self._receipt(tx, op, command)
                tx.commit()
                return result, owner
            if tx.read(INTAKES, op.receipt) is not None:
                raise ExternalContextError('external_context_binding_invalid')
            if reserve and prior is None:
                # 实际来源和操作人各有原事务；来源命令用于恢复已提交而未写回执的结果。
                self.api.guard._reserve_once(tx, _WRITE_RESERVATIONS, 'receipt_id', op.receipt, op.fact)
                tx.put(OPERATIONS, op.receipt, op.fact, expected_revision=0)
                tx.commit()
            return None, owner

    def _receipt(self, tx, op, command):
        current = tx.read(INTAKES, op.receipt)
        payload = {'schema_version': 2, 'owner_id': self.api.owner_id, **{key: op.fact[key]
            for key in ('client', 'tool', 'project_id', 'namespace', 'actor_user_id', 'execution_owner_user_id', 'command_id')},
            'object_id': command.payload['item_id'], 'object_revision': command.payload['object_revision'],
            'created_at': current.payload['created_at'] if current is not None else _now()}
        if op.scene is not None:
            payload['scene'] = op.scene
        if op.references:
            payload['evidence_ids'] = op.references
            payload['material_refs'] = command.payload['evidence_materials']
        if op.tool == 'remember':
            payload['material_refs'] = [{'type': 'original_item', 'id': payload['object_id'],
                'revision': payload['object_revision'], 'project_id': op.project, 'namespace': op.namespace}]
        if current is None:
            tx.put(INTAKES, op.receipt, payload, expected_revision=0)
        elif current.revision != 1 or _encoded(current.payload) != _encoded(payload):
            raise ExternalContextError('external_context_binding_invalid')
        result = {'receipt_id': op.receipt, 'project_id': op.project, 'namespace': op.namespace,
            'scope_kind': 'shared', 'client': op.client, 'revision': payload['object_revision']}
        if op.tool == 'remember':
            return {**result, 'item_id': payload['object_id'], 'state': 'staged', 'verified': False}
        return {**result, 'candidate_id': payload['object_id'], 'state': 'pending'}

    def _evidence(self, op):
        result = []
        for reference in op.references:
            proof, request = self.api.delivered_proof(reference['turn_id'], reference['id'],
                client=op.client, _shared_tool='propose_insight')
            if (request.get('scope_kind') != 'shared'
                    or (proof['material']['project_id'], proof['material'].get('namespace')) != (op.project, op.namespace)):
                raise ExternalContextError('external_context_evidence_invalid')
            result.append(proof)
        return result

    def _validate_evidence(self, op, query, source, actor_reader):
        archives = {}
        for reference, proof in zip(op.references, op.evidence, strict=True):
            turn = reference['turn_id']
            if turn not in archives:
                ref, archive, outcome = self.api._completed(turn)
                if archive['schema_version'] != SHARED_ARCHIVE_VERSION or archive['shared']['native'] != op.native:
                    raise ExternalContextError('external_context_evidence_invalid')
                _delivery(actor_reader, self.api, turn, ref, outcome)
                self.shared.materials._validate_current_in_source(archive, query, source, actor_reader)
                archives[turn] = archive
            if archives[turn]['mapping'].get(reference['id']) != proof:
                raise ExternalContextError('external_context_evidence_invalid')

    def _created(self, op, writer, tx, row):
        if op.scene is not None:
            assign_scene(TransactionRecords(tx), 'item' if op.tool == 'remember' else 'candidate',
                row.object_id, op.project, op.scene)
        tx.put(COMMANDS, op.source_id, {'command_id': op.command, 'project_id': op.project, 'kind': op.kind,
            'actor_user_id': op.fact['actor_user_id'], 'execution_owner_user_id': op.fact['execution_owner_user_id'],
            'namespace': op.namespace, 'input': op.input, 'input_id': op.fact['input_id'],
            'external_tool': op.tool, 'item_id': row.object_id, 'object_revision': row.revision,
            'evidence_materials': [proof['material'] for proof in op.evidence],
            'birth': capture_lineage(tx, row.collection, row.object_id)}, expected_revision=0)
        writer.cap.validate()

    def _write(self, op, query, owner, cap):
        writer = _McpIntakeRecords(self, op, query, owner, cap)
        try:
            if op.tool == 'remember':
                items = _RedactingItems(writer, None, RLock())
                intake = WorkspaceIntake(query.material_owner.root, items, query.models)
                def created(tx, row):
                    self._created(op, writer, tx, row)
                if 'url' in op.input:
                    asyncio.run(intake.add_link({'project_id': op.project, 'url': op.input['url']}, stage_created=created))
                else:
                    asyncio.run(intake.add_text({'project_id': op.project, 'text': op.input['text']}, stage_created=created))
                return
            with writer.begin() as tx:
                scope = WorkScope(self.api.owner_id, op.project)
                enlisted = TransactionRecords(tx)
                service = recognition_service(enlisted, source_owner=writer)
                content = op.input['text']
                experience = service.stage_experience(scope=scope, content=content,
                    provenance={'kind': 'user_statement', 'actor': op.fact['actor_user_id']},
                    **({'experience_id': 'experience-external-v1-' + uuid5(NAMESPACE_URL, op.command).hex} if op.evidence else {}))
                if op.evidence:
                    proofs = {_encoded(proof['material']): proof for proof in op.evidence}
                    tx.put(DEPENDENCIES, experience, {'schema_version': 3, 'owner_id': scope.user_id,
                        'client': op.client, 'scope': {'user_id': scope.user_id, 'project_id': scope.project_id},
                        'experience_id': experience, 'experience_revision': 1,
                        'ownexperience_identity': capture_lineage(tx, 'recognition_experiences', experience),
                        'namespace': op.namespace, 'actor_user_id': op.fact['actor_user_id'],
                        'references': [{'proof': {key: proof[key] for key in ('material', 'snapshot', 'usage_kind')},
                            'sql_identities': proof['sql_identities']} for proof in proofs.values()]}, expected_revision=0)
                candidate = service.propose_in_uow(tx, scope=scope, content=content,
                    source_experience_ids=[experience], conditions=op.input['conditions'])
                self._created(op, writer, tx, tx.read('recognition_candidates', candidate.id))
                tx.commit()
        except _Replay:
            return
