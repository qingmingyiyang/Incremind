import asyncio
from contextlib import contextmanager
from sys import maxsize

from fastapi import HTTPException, Request

from backend.recognition import RecognitionConflict, WorkScope
from backend.security.user_context import authorize_user, user_context
from core.storage_provider.persistence_budget import persistence_budget
from ..recall_state import is_recall_excluded, preference
from ..relations import RelationProposalService
from ..server_jobs import QuotaError, retain_until_finished
from ..source_egress import recognition_service
from ..transaction_records import TransactionRecords
from .insights import insight_view, resolve_insight
from .insight_validity import read_validity
from .links import InsightLinks
from .recall_preferences import set_preference
from .shared_projects import PROJECTS, SharedProjectError, revision, segment
from .shared_runtime import SharedWriteRecords, shared_read_capability
from .usage import UsageService, read_usage_count


_MANUAL_SUPERSESSION = '人工确认取代'
_TARGET_FIELDS = {'target_id', 'target_revision'}


def _target_fields(body):
    segment(body['target_id'])
    revision(body['target_revision'])


def _scope_row(row, scope):
    return row is not None and row.payload.get('scope') == {'user_id': scope.user_id, 'project_id': scope.project_id}


def _pending_candidate(reader, scope, row, expected_revision):
    return (_scope_row(row, scope) and row.revision == expected_revision and row.payload['state'] == 'pending'
        and reader.read('v2_candidate_fade', row.object_id) is None)


def _eligible_replacement(reader, service, scope, row):
    if (not _scope_row(row, scope) or row.payload['state'] != 'active'
            or is_recall_excluded(reader, scope, row.object_id)):
        return False
    validity = read_validity(reader, row.object_id)
    return (validity is not None and validity['valid_until'] is None and validity['superseded_by'] is None
        and service._qualified_recognition(reader, row).authorized)


def _replacement_target(reader, service, scope, candidate_id, body):
    if body['target_id'] == candidate_id:
        return None
    row = reader.read('recognitions', body['target_id'])
    return row if row is not None and row.revision == body['target_revision'] and _eligible_replacement(
        reader, service, scope, row) else None


def _relation_id(proposal_id):
    return 'relation-' + proposal_id.removeprefix('relation-proposal-')


def _replacement_facts(edge, revision):
    relation = {field: edge.payload[field] for field in ('id', 'from_id', 'to_id', 'relation')}
    return {'relation': relation, 'target': {'id': relation['to_id'], 'revision': revision,
        'superseded_by': relation['from_id']}}


def recall_preference_revision(reader, row, *, kind):
    scope = row.payload['scope']
    if kind == 'recognition':
        return preference(reader, WorkScope(scope['user_id'], scope['project_id']), row.object_id)['recall_preference_revision']
    faded = reader.read('v2_candidate_fade', row.object_id)
    return faded.revision if faded is not None else 0


class SharedInsightMaintenance:
    def __init__(self, library, context):
        if context.shared_query_factory is not library.factory or context.shared_projects is not library.registry:
            raise SharedProjectError('shared_insight_owner_changed', status=409)
        self.library, self.context, self.registry = library, context, library.registry

    async def mutate(self, actor, project, identity, body, *, action):
        segment(project)
        segment(identity)
        fields = {'namespace', 'authority', 'expected_revision'} | {
            'edit': {'text', 'conditions'}, 'forget': {'forgotten', 'preference_revision'},
            'supersede': _TARGET_FIELDS,
        }[action]
        if not isinstance(body, dict) or set(body) != fields:
            raise SharedProjectError('shared_insight_fields_invalid')
        revision(body['expected_revision'])
        if not isinstance(body['namespace'], str) or not body['namespace']:
            raise SharedProjectError('shared_insight_namespace_required')
        if action == 'forget' and (type(body['forgotten']) is not bool
                or type(body['preference_revision']) is not int or body['preference_revision'] < 0):
            raise SharedProjectError('shared_insight_recall_invalid')
        if action == 'supersede':
            _target_fields(body)
        with self.registry.records.begin_read() as central:
            self.registry.authorize(central, actor, project, action='write', authority=body['authority'])
            owner = central.read(PROJECTS, project).payload['owner_user_id']
        async with self.context.jobs.intake_many((owner,)) as budgets:
            # 容量属于实际来源所有者；预留其可用空间，原存储逐笔消耗真实字节。
            cap = budgets[owner].reserve_up_to(maxsize)
            try:
                return await retain_until_finished(asyncio.to_thread(
                    self._write, actor, project, identity, body, action=action, owner=owner, cap=cap))
            finally:
                cap.close()

    def _write(self, actor, project, identity, body, *, action, owner, cap):
        operation = action if action != 'forget' or body['forgotten'] else 'restore'
        writer = SharedWriteRecords(self.registry, actor, project, authority=body['authority'],
            action=operation, object_kind='insight', object_id=identity, object_title='认识维护')
        with writer.begin() as tx:
            reader = TransactionRecords(tx)
            source = shared_read_capability(reader, registry=self.registry)
            if (source.namespace != body['namespace']
                    or tx._central.read(PROJECTS, project).payload['owner_user_id'] != owner):
                raise SharedProjectError('shared_insight_owner_changed', status=409)
            data = source._data
            with cap.using_reader(tx._central), persistence_budget(root=data.root,
                    databases=(data.records.database_path,), consume=cap.consume):
                scope = WorkScope('local-user', project)
                row = resolve_insight(reader, scope, identity)
                if row is None:
                    raise SharedProjectError('shared_insight_not_found', status=404)
                if row.object_id != identity or row.revision != body['expected_revision']:
                    raise RecognitionConflict('shared_insight_revision_changed')
                writer.object_title = row.payload['content'].splitlines()[0][:80]
                service = recognition_service(reader, source_owner=writer)
                usage = UsageService(reader, source_owner=writer)
                replacement = {}
                if action == 'edit':
                    if reader.read('recognition_candidates', identity) is not None:
                        saved = service.edit_candidate(scope=scope, candidate_id=identity,
                            expected_revision=row.revision, content=body['text'], conditions=body['conditions'],
                            editor=actor.user_id)
                    else:
                        saved = service.revise(scope=scope, recognition_id=identity,
                            expected_revision=row.revision, content=body['text'], conditions=body['conditions'])
                        usage.record_usage('insight', saved.id, project, 1.0, reset=True)
                elif action == 'forget':
                    kind = 'recognition' if reader.read('recognitions', identity) is not None else 'candidate'
                    if recall_preference_revision(reader, row, kind=kind) != body['preference_revision']:
                        raise RecognitionConflict('shared_insight_preference_changed')
                    if kind == 'candidate':
                        if row.payload.get('state') != 'pending' or body['forgotten']:
                            raise SharedProjectError('shared_insight_not_active', status=409)
                        faded = reader.read('v2_candidate_fade', identity)
                        if faded is not None:
                            tx.delete('v2_candidate_fade', identity, expected_revision=faded.revision)
                    else:
                        set_preference(reader, scope, identity, recognition_revision=row.revision,
                            preference_revision=body['preference_revision'],
                            state='forgotten' if body['forgotten'] else 'normal',
                            _usage_writer=lambda: usage.record_usage('insight', identity, project, 1.0, reset=True))
                else:
                    candidate = reader.read('recognition_candidates', identity)
                    if candidate != row or not _pending_candidate(reader, scope, candidate, body['expected_revision']):
                        raise RecognitionConflict('shared_supersession_candidate_changed')
                    target = _replacement_target(reader, service, scope, candidate.object_id, body)
                    if target is None:
                        raise RecognitionConflict('shared_supersession_target_changed')
                    published = service.publish(scope=scope, candidate_id=identity,
                        expected_revision=row.revision, reviewer=actor.user_id)
                    if not published.authorized:
                        raise RecognitionConflict('shared_supersession_source_changed')
                    relations = RelationProposalService(reader)
                    proposed = relations.propose(scope, published.id, target.object_id,
                        'supersedes', _MANUAL_SUPERSESSION)
                    approved = relations.review(scope, proposed['id'], proposed['revision'], 'approved')
                    links = InsightLinks(reader, service, source_owner=writer)
                    edge = links._activate_reviewed(tx, project, approved, edge_id=_relation_id(approved['id']))
                    replacement = _replacement_facts(edge, target.revision)
                    identity = published.id
                    cap.validate()
                value = insight_view(reader, scope, identity, service=service, source_owner=writer)
                if value is None:
                    raise SharedProjectError('shared_insight_not_visible', status=409)
                value = {**value, 'project_id': project, 'namespace': source.namespace,
                    'usage_count': read_usage_count(reader, 'insight', value['id'], project)}
                result = {'insight': self.library._actors(reader, value), 'namespace': source.namespace,
                    'authority': tx.access.authority, 'can_write': True, **replacement}
                tx.commit()
                return result

    @contextmanager
    def _supersession_read(self, child, actor, project, identity, body):
        segment(project)
        segment(identity)
        revision(body['expected_revision'])
        with self.library._projection(child, actor, project) as (query, library, access):
            if not library.can_write:
                raise SharedProjectError('shared_write_forbidden', status=403)
            if query.source_namespace != body['namespace']:
                raise RecognitionConflict('shared_insight_owner_changed')
            envelope = {'namespace': query.source_namespace, 'authority': access.authority, 'can_write': True}
            yield query, WorkScope('local-user', project), query.records.read('recognition_candidates', identity), envelope

    def read_supersession(self, child, actor, project, identity, body):
        _target_fields(body)
        with self._supersession_read(child, actor, project, identity, body) as (query, scope, candidate, envelope):
            reader = query.records
            if _pending_candidate(reader, scope, candidate, body['expected_revision']):
                target = _replacement_target(reader, query.service, scope, candidate.object_id, body)
                return {**envelope, 'state': 'pending' if target is not None else 'changed'}
            edge = self._approved_supersession(reader, scope, candidate, body)
            if edge is None:
                return {**envelope, 'state': 'changed'}
            return {**envelope, 'state': 'applied', 'insight_id': edge.payload['from_id'],
                **_replacement_facts(edge, body['target_revision'])}

    def read_supersession_targets(self, child, actor, project, identity, body):
        with self._supersession_read(child, actor, project, identity, body) as (query, scope, candidate, envelope):
            reader = query.records
            if not _pending_candidate(reader, scope, candidate, body['expected_revision']):
                raise RecognitionConflict('shared_supersession_candidate_changed')
            rows = reader.list_matching('recognitions', state='active')
            return {**envelope, 'items': [{'id': row.object_id, 'text': row.payload['content'], 'revision': row.revision,
                'kind': 'recognition', 'state': 'active', 'project_id': project, 'namespace': query.source_namespace}
                for row in rows if row.object_id != identity and _eligible_replacement(reader, query.service, scope, row)]}

    @staticmethod
    def _approved_supersession(reader, scope, candidate, body):
        if not _scope_row(candidate, scope):
            return None
        published_id, target_id = candidate.payload.get('recognition_id'), body['target_id']
        if (candidate.revision != body['expected_revision'] + 1 or candidate.payload['state'] != 'published'
                or not isinstance(published_id, str) or published_id == target_id):
            return None
        published = reader.read('recognitions', published_id)
        target = reader.read('recognitions', target_id)
        birth = reader.read('recognition_versions', published_id + '~v1')
        if (not _scope_row(published, scope) or not _scope_row(target, scope) or birth is None or birth.revision != 1
                or birth.payload.get('action') != 'publish' or birth.payload.get('recognition_id') != published_id
                or birth.payload.get('version') != 1 or birth.payload.get('recognition_revision') != 1
                or birth.payload['snapshot'].get('scope') != candidate.payload['scope']
                or birth.payload['snapshot'].get('content') != candidate.payload['content']):
            return None
        proposals = [row for row in reader.list_matching('recognition_relation_proposals',
            project_id=scope.project_id, from_id=published_id, to_id=target_id)
            if _scope_row(row, scope) and row.revision == 2 and isinstance(row.payload.get('reviewed_at'), str)
            and all(row.payload.get(key) == value for key, value in {
                'source': 'manual', 'state': 'approved', 'relation': 'supersedes', 'evidence': _MANUAL_SUPERSESSION,
                'from_revision': 1, 'to_revision': body['target_revision'],
            }.items())]
        if len(proposals) != 1:
            return None
        edge = reader.read('recognition_relations', _relation_id(proposals[0].object_id))
        validity = reader.read('v2_insight_validity', target_id)
        if (not _scope_row(edge, scope) or edge.revision != 1 or edge.payload.get('id') != edge.object_id
                or edge.payload.get('from_id') != published_id or edge.payload.get('to_id') != target_id
                or edge.payload.get('relation') != 'supersedes' or validity is None
                or validity.payload['superseded_by'] != published_id
                or validity.payload['valid_until'] != edge.payload['created_at']):
            return None
        return edge


def install_shared_insight_maintenance(router, *, library, context, identity, execute):
    maintenance = SharedInsightMaintenance(library, context)

    async def mutate(project, insight, request, action):
        actor = identity(request)
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(400, 'shared_insight_request_invalid') from None
        async def run():
            try:
                return await maintenance.mutate(actor, project, insight, body, action=action)
            except QuotaError as error:
                raise SharedProjectError(str(error), status=429) from error
        return await execute(run)

    @router.patch('/{project_id}/library/insights/{insight_id}')
    async def edit(project_id: str, insight_id: str, request: Request):
        return await mutate(project_id, insight_id, request, 'edit')

    @router.post('/{project_id}/library/insights/{insight_id}/forget')
    async def forget(project_id: str, insight_id: str, request: Request):
        return await mutate(project_id, insight_id, request, 'forget')

    @router.post('/{project_id}/library/insights/{insight_id}/supersede')
    async def supersede(project_id: str, insight_id: str, request: Request):
        return await mutate(project_id, insight_id, request, 'supersede')

    async def read(project_id, insight_id, request, operation, body):
        actor = identity(request)
        async def run():
            access = authorize_user(context.users, actor, actor.user_id)
            with user_context(access):
                async with context.pool.lease(actor.user_id) as child:
                    return await retain_until_finished(asyncio.to_thread(
                        operation, child, actor, project_id, insight_id, body))
        return await execute(run)

    @router.get('/{project_id}/library/insights/{insight_id}/supersede')
    async def supersession(project_id: str, insight_id: str, request: Request,
            namespace: str, expected_revision: int, target_id: str, target_revision: int):
        return await read(project_id, insight_id, request, maintenance.read_supersession,
            {'namespace': namespace, 'expected_revision': expected_revision, 'target_id': target_id,
             'target_revision': target_revision})

    @router.get('/{project_id}/library/insights/{insight_id}/supersession-targets')
    async def supersession_targets(project_id: str, insight_id: str, request: Request,
            namespace: str, expected_revision: int):
        return await read(project_id, insight_id, request, maintenance.read_supersession_targets,
            {'namespace': namespace, 'expected_revision': expected_revision})
