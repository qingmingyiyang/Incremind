import asyncio
from sys import maxsize
from uuid import UUID

from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, ValidationError

from backend.recognition import RecognitionConflict, RecognitionError, WorkScope
from backend.recognition.restructuring import RestructureProposalService
from backend.security.user_context import authorize_user, user_context
from core.storage_provider.persistence_budget import persistence_budget
from ..server_jobs import QuotaError, retain_until_finished
from ..source_egress import SourceEgressService, recognition_service
from ..transaction_records import TransactionRecords
from .insight_validity import COLLECTION as VALIDITY, close
from .library_merge import MergeInput, _identity
from .projects import assign_scene, scene_of
from .shared_insight_maintenance import _eligible_replacement, _pending_candidate
from .shared_merge_privacy import OPERATIONS, PRIVACY, graph_private, pending_merge_private
from .shared_projects import PROJECTS, SharedProjectError, revision, segment
from .shared_runtime import SharedWriteRecords, shared_read_capability


class ActiveInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    operation_id: StrictStr
    expected_revisions: dict[StrictStr, StrictInt] = Field(min_length=2, max_length=12)
    content: StrictStr = Field(min_length=1, max_length=20000)
    conditions: list[StrictStr] = Field(max_length=64)


class SharedInsightMerges:
    def __init__(self, library, context):
        self.library, self.context, self.registry = library, context, library.registry

    @staticmethod
    def _command(reader, project, namespace, actor, identity):
        row = reader.read(OPERATIONS, identity)
        if row is not None and (row.payload['project_id'] != project or row.payload['namespace'] != namespace
                or row.payload['actor_user_id'] != actor.user_id or row.payload['device_id'] != actor.device_id
                or row.payload['device_revision'] != actor.revision or row.payload['operation_id'] != identity):
            raise RecognitionConflict('shared_merge_command_changed')
        return row

    @staticmethod
    def _view(row, service, access):
        value = row.payload
        result = {key: value[key] for key in ('operation_id', 'project_id', 'namespace', 'state')}
        result.update(revision=row.revision, can_write=True, authority=access.authority, proposal=None)
        if value['state'] == 'applied':
            result['result_id'] = value['result_id']
        if value['state'] == 'prepared':
            scope = WorkScope('local-user', value['project_id'])
            result['proposal'] = RestructureProposalService(service).get_in_uow(
                service.records, scope=scope, proposal_id=value['proposal_id'])
        return result

    def read(self, child, actor, project, namespace, *, operation=None):
        with self.library._projection(child, actor, project) as (query, library, access):
            if not library.can_write:
                raise SharedProjectError('shared_write_forbidden', status=403)
            if query.source_namespace != namespace:
                raise RecognitionConflict('shared_merge_owner_changed')
            if operation is not None:
                row = self._command(query.records, project, namespace, actor, _identity(operation))
                if row is None:
                    raise SharedProjectError('shared_merge_not_found', status=404)
                return self._view(row, query.service, access)
            scope = WorkScope('local-user', project)
            items = [{**item, 'merge_eligible': True} for item in library.insights(project)
                if item['kind'] == 'recognition'
                and _eligible_replacement(query.records, query.service, scope, query.records.read('recognitions', item['id']))]
            return {'items': items, 'project_id': project, 'namespace': namespace,
                'authority': access.authority, 'can_write': True}

    async def mutate(self, actor, project, namespace, authority, operation, action, values):
        segment(project)
        _identity(operation)
        if not isinstance(namespace, str) or not namespace or not isinstance(authority, dict):
            raise RecognitionError('shared_merge_scope_invalid')
        with self.registry.records.begin_read() as central:
            self.registry.authorize(central, actor, project, action='write', authority=authority)
            owner = central.read(PROJECTS, project).payload['owner_user_id']
        async with self.context.jobs.intake_many((owner,)) as budgets:
            cap = budgets[owner].reserve_up_to(maxsize)
            try:
                return await retain_until_finished(asyncio.to_thread(
                    self._write, actor, project, namespace, authority, operation, action, values, owner, cap))
            finally:
                cap.close()

    def _write(self, actor, project, namespace, authority, operation, action, values, owner, cap):
        writer = SharedWriteRecords(self.registry, actor, project, authority=authority,
            action='merge', object_kind='insight', object_id=operation, object_title='合并认识')
        with writer.begin() as tx:
            reader = TransactionRecords(tx)
            source = shared_read_capability(reader, registry=self.registry)
            if source.namespace != namespace or tx._central.read(PROJECTS, project).payload['owner_user_id'] != owner:
                raise RecognitionConflict('shared_merge_owner_changed')
            data = source._data
            with cap.using_reader(tx._central), persistence_budget(root=data.root,
                    databases=(data.records.database_path,), consume=cap.consume):
                row = self._command(reader, project, namespace, actor, operation)
                service = recognition_service(reader, source_owner=writer)
                scope = WorkScope('local-user', project)
                egress = SourceEgressService(data.records, reader=tx)
                if action == 'review':
                    if row is None or row.payload['state'] != 'prepared':
                        raise RecognitionConflict('shared_merge_command_changed')
                    self._review(reader, scope, service, egress, row, actor, values)
                elif row is not None:
                    if row.payload['action'] != action or row.payload['inputs'] != values:
                        raise RecognitionConflict('shared_merge_command_changed')
                    return self._view(row, service, tx.access)
                else:
                    value = {'operation_id': operation, 'project_id': project, 'namespace': namespace,
                        'actor_user_id': actor.user_id, 'device_id': actor.device_id,
                        'device_revision': actor.revision, 'action': action, 'inputs': values}
                    if action == 'active':
                        result = self._active(reader, scope, service, egress, writer, actor, operation, values)
                        value.update(state='applied', result_id=result)
                    elif action == 'prepare':
                        proposal_id = self._prepare(reader, scope, service, operation, values)
                        value.update(state='prepared', proposal_id=proposal_id)
                    else:
                        value['state'] = 'cancelled'
                    row = tx.put(OPERATIONS, operation, value, expected_revision=0)
                result = self._view(row, service, tx.access)
                cap.validate()
                tx.commit()
                return result

    @staticmethod
    def _active_row(reader, scope, service, identity, expected):
        segment(identity)
        revision(expected)
        row = reader.read('recognitions', identity)
        if row is None or row.revision != expected or not _eligible_replacement(reader, service, scope, row):
            raise RecognitionConflict('shared_merge_inputs_changed')
        return row

    def _active(self, reader, scope, service, egress, writer, actor, operation, values):
        revisions = values['expected_revisions']
        rows = [self._active_row(reader, scope, service, identity, expected) for identity, expected in revisions.items()]
        refs = [{'type': 'recognition', 'id': row.object_id, 'revision': row.revision} for row in rows]
        private = graph_private(egress, scope, refs)
        scenes = [scene_of(reader, 'recognition', row.object_id) for row in rows]
        saved = service.merge(scope=scope, recognition_ids=list(revisions), expected_revisions=revisions,
            recognition_id='recognition-merge-' + UUID(operation).hex, reviewer=actor.user_id,
            content=values['content'], conditions=values['conditions'])
        current = reader.read('recognitions', saved.id)
        writer.object_id, writer.object_title = saved.id, current.payload['content'].splitlines()[0][:80]
        at = current.payload['created_at']
        reader.put(VALIDITY, saved.id, {'valid_from': at, 'valid_until': None, 'superseded_by': None}, expected_revision=0)
        for row in rows:
            close(reader, row.object_id, saved.id, at)
        if scenes[0] and all(scene == scenes[0] for scene in scenes):
            assign_scene(reader, 'recognition', saved.id, scope.project_id, scenes[0]['scene'])
        if private:
            egress.set_policy(scope, 'recognition', saved.id, current.revision, 0, [])
        return saved.id

    def _pending_rows(self, reader, scope, service, values):
        candidate = reader.read('recognition_candidates', segment(values['candidate_id']))
        if (not _pending_candidate(reader, scope, candidate, values['candidate_revision'])
                or reader.read('v2_candidate_merges', values['candidate_id']) is not None
                or values['candidate_id'] == values['target_id']):
            raise RecognitionConflict('shared_merge_inputs_changed')
        target = self._active_row(reader, scope, service, values['target_id'], values['target_revision'])
        return candidate, target

    def _prepare(self, reader, scope, service, operation, values):
        candidate, target = self._pending_rows(reader, scope, service, values)
        authority = RestructureProposalService(service, allow_private_manual=True)
        snapshot = authority.capture(scope=scope, recognition_ids=[candidate.object_id, target.object_id],
            expected_revisions={candidate.object_id: candidate.revision, target.object_id: target.revision}, pending_output=True)
        proposal_id = 'library-merge-' + UUID(operation).hex
        authority.save_in_uow(reader, scope=scope, proposal_id=proposal_id, snapshot=snapshot, operation='merge',
            outputs=[{'content': values['content'], 'conditions': values['conditions'],
                'source_experience_ids': [item['id'] for item in snapshot['experiences']],
                'source_recognition_ids': [item['id'] for item in snapshot['recognitions']
                    if item['id'] not in snapshot['target_recognition_ids']]}],
            reason='成员预览合并待确认认识与已有认识', step_metadata={'source': 'shared-library-review'})
        return proposal_id

    def _review(self, reader, scope, service, egress, command, actor, values):
        proposal_id = command.payload['proposal_id']
        proposal = reader.read('recognition_restructure_proposals', proposal_id)
        if proposal is None:
            raise RecognitionConflict('shared_merge_proposal_changed')
        decision, expected = values['decision'], values['expected_revision']
        pending = proposal.payload['state'] == 'pending'
        private = False
        if pending and decision == 'approve':
            candidate, target = self._pending_rows(reader, scope, service, command.payload['inputs'])
            refs = [{'type': 'recognition', 'id': target.object_id, 'revision': target.revision}]
            for kind, field in [('experience', 'source_experience'), ('recognition', 'source_recognition')]:
                refs.extend({'type': kind, 'id': identity, 'revision': candidate.payload[field + '_revisions'][identity]}
                    for identity in candidate.payload[field + '_ids'])
            private = graph_private(egress, scope, refs) or pending_merge_private(reader, egress, scope, candidate)
        reviewed = RestructureProposalService(service, allow_private_manual=True).review(scope=scope, proposal_id=proposal_id,
            expected_revision=expected, decision=decision, reviewer=actor.user_id)
        if pending and decision == 'approve':
            result_id, = reviewed['result_candidate_ids']
            reader.put(PRIVACY, result_id, {'project_id': scope.project_id, 'proposal_id': proposal_id,
                'operation_id': command.object_id, 'target_id': target.object_id,
                'target_revision': target.revision, 'private': private, 'parent_candidate_id': candidate.object_id,
                'parent_merge': reader.read(PRIVACY, candidate.object_id) is not None}, expected_revision=0)


def install_shared_insight_merges(router, *, library, context, identity, execute):
    owner = SharedInsightMerges(library, context)
    base = '/{project_id}/library/insight-merges'

    async def read(project, namespace, request, operation=None):
        actor = identity(request)
        async def run():
            access = authorize_user(context.users, actor, actor.user_id)
            with user_context(access):
                async with context.pool.lease(actor.user_id) as child:
                    return await retain_until_finished(asyncio.to_thread(
                        owner.read, child, actor, project, namespace, operation=operation))
        return await execute(run)

    async def write(project, request, action, operation=None):
        actor = identity(request)
        async def run():
            try:
                body = await request.json()
                if not isinstance(body, dict):
                    raise ValueError()
                namespace, authority = body.pop('namespace'), body.pop('authority')
                if action in {'active', 'prepare'}:
                    model = ActiveInput if action == 'active' else MergeInput
                    values = model.model_validate(body).model_dump(mode='json')
                    if action == 'prepare' and values['project_id'] != project:
                        raise ValueError()
                    command = values['operation_id']
                else:
                    command, values = operation, body
                    if (action == 'cancel' and (values != {'expected_revision': 0}
                            or type(values['expected_revision']) is not int)
                            or action == 'review' and (set(values) != {'expected_revision', 'decision'}
                                or values['decision'] not in ('approve', 'reject'))):
                        raise ValueError()
                    if action == 'review':
                        revision(values['expected_revision'])
            except (ValueError, KeyError, ValidationError):
                raise SharedProjectError('shared_merge_input_invalid') from None
            try:
                return await owner.mutate(actor, project, namespace, authority, command, action, values)
            except QuotaError as error:
                raise SharedProjectError(str(error), status=429) from error
        return await execute(run)

    @router.get(base + '/targets')
    async def targets(project_id: str, namespace: str, request: Request):
        return await read(project_id, namespace, request)

    @router.get(base + '/{operation_id}')
    async def status(project_id: str, operation_id: str, namespace: str, request: Request):
        return await read(project_id, namespace, request, operation_id)

    @router.post(base + '/active')
    async def active(project_id: str, request: Request):
        return await write(project_id, request, 'active')

    @router.post(base + '/prepare')
    async def prepare(project_id: str, request: Request):
        return await write(project_id, request, 'prepare')

    @router.post(base + '/{operation_id}/cancel')
    async def cancel(project_id: str, operation_id: str, request: Request):
        return await write(project_id, request, 'cancel', operation_id)

    @router.post(base + '/{operation_id}/review')
    async def review(project_id: str, operation_id: str, request: Request):
        return await write(project_id, request, 'review', operation_id)
