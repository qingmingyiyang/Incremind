from datetime import datetime, timedelta, timezone
import json
import logging
import sqlite3
from threading import Lock
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from backend.recognition import RecognitionConflict, RecognitionError, WorkScope
from core.storage_provider import SQLiteUnitOfWorkConflict
from core.storage_provider.connection_scope import with_connection_scope
from ..original_sources import document_roots
from ..source_egress import SourceEgressService
from ..transaction_records import TransactionRecords
from ..workspace_contracts import _json, _project
from .inspiration_extraction import (
    INPUTS, InspirationExtractionError, _basis, _completed_result, _database, _key,
    extract_inspirations,
)
from .insights import insight_view
from .outcomes import hidden_outcome_ids
from .policies import get
from .privacy import egress_allowed, is_private_project
from .projects import scene_of


SETTINGS_COLLECTION = 'v2_inspiration_settings'
SETTINGS_KEY = 'daily'
JOBS_COLLECTION = 'v2_automatic_inspiration_jobs'
SEEN_COLLECTION = 'v2_automatic_inspiration_seen'
POLICY = '@1'
_LOGGER = logging.getLogger(__name__)
_MESSAGES = {
    'no_eligible_materials': '暂无可提炼资料', 'no_insights': '本次未提炼出新观点',
    'generation_disabled': '生成未开启', 'automatic_disabled': '自动提炼已关闭',
    'sources_changed': '来源已变化', 'material_budget_insufficient': '材料超出当前模型容量',
    'generation_failed': '提炼未完成', 'generation_unknown': '发送状态尚未核验',
    'generation_evidence_unavailable': '生成证明尚未核验', 'automatic_interrupted': '自动提炼已中断',
    'generation_unavailable': '生成暂不可用',
}


def _now():
    return datetime.now(timezone.utc)


class AutomaticInspirations:
    def __init__(self, records, service, documents, models, library, *, now=_now):
        self.records, self.service, self.documents = records, service, documents
        self.models, self.library, self.now = models, library, now
        self.owner = uuid4().hex
        self._lock = Lock()
        self._active_job = None

    @property
    def policy(self):
        return get('inspiration_schedule', version=POLICY)

    def settings(self, reader=None):
        row = (reader or self.records).read(SETTINGS_COLLECTION, SETTINGS_KEY)
        return {**self.policy({'operation': 'settings', 'settings': row.payload if row else None}),
            'revision': row.revision if row else 0}

    def save_settings(self, body):
        if (not isinstance(body, dict) or set(body) != {'enabled', 'time', 'expected_revision'}
                or type(body['expected_revision']) is not int or body['expected_revision'] < 0):
            raise HTTPException(400, 'invalid_inspiration_schedule_fields')
        try:
            settings = self.policy({'operation': 'settings', 'settings': {
                'enabled': body['enabled'], 'time': body['time'], 'timezone': 'Asia/Shanghai'}})
        except (ValueError, TypeError):
            raise HTTPException(400, 'invalid_inspiration_schedule_fields') from None
        with self.records.begin() as tx:
            prior = tx.read(SETTINGS_COLLECTION, SETTINGS_KEY)
            if (prior.revision if prior else 0) != body['expected_revision']:
                raise HTTPException(409, 'inspiration_schedule_changed')
            tx.put(SETTINGS_COLLECTION, SETTINGS_KEY, {**settings, 'updated_at': self.now().isoformat()},
                expected_revision=body['expected_revision'])
            tx.commit()
        return self.settings()

    def _slot(self):
        row = self.records.read(SETTINGS_COLLECTION, SETTINGS_KEY)
        return self.policy({'operation': 'slot', 'settings': row.payload if row else None,
            'now': self.now().isoformat()})

    def _update(self, identity, **changes):
        with self.records.begin() as tx:
            row = tx.read(JOBS_COLLECTION, identity)
            if row is None or row.payload.get('owner_id') != self.owner or row.payload.get('status') != 'running':
                raise RecognitionConflict('automatic_inspiration_ownership_changed')
            row = tx.put(JOBS_COLLECTION, identity,
                {**row.payload, **changes, 'updated_at': self.now().isoformat()}, expected_revision=row.revision)
            tx.commit()
        return row

    def _guard(self, identity, reader=None):
        reader = reader or self.records
        if not self.settings(reader)['enabled']:
            raise RecognitionConflict('automatic_inspiration_disabled')
        row = reader.read(JOBS_COLLECTION, identity)
        if row is None or row.payload.get('owner_id') != self.owner or row.payload.get('status') != 'running':
            raise RecognitionConflict('automatic_inspiration_ownership_changed')
        project, scene = row.payload.get('project_id'), row.payload.get('scene')
        if project is None or is_private_project(reader, project):
            raise RecognitionConflict('automatic_inspiration_sources_changed')
        current = self.library.docs(project)
        hidden = hidden_outcome_ids(reader, project)
        authority = SourceEgressService(self.records, reader=None if reader is self.records else reader)
        scope = WorkScope('local-user', project)
        for selected in row.payload['documents']:
            document = current.get(selected['document_id'])
            assignment = scene_of(reader, 'document', selected['document_id'])
            actual_scene = assignment.get('scene') if assignment and assignment.get('project_id') == project else None
            recall = reader.read('v2_document_recall', selected['document_id'])
            if (document is None or document['revision'] != selected['expected_revision']
                    or selected['document_id'] in hidden or actual_scene != scene
                    or assignment is not None and assignment.get('project_id') != project
                    or recall is not None and recall.payload.get('state') == 'forgotten'):
                raise RecognitionConflict('automatic_inspiration_sources_changed')
            roots = document_roots(reader, scope, document.get('source_refs', []))
            snapshot = authority.snapshot(scope, [{'type': kind, 'id': owner, 'revision': revision}
                for kind, owner, revision in roots])
            authority.require(snapshot, 'generation')

    def _covered(self, document, markdown):
        revisions = set()
        for row in self.records.list(INPUTS):
            if row.payload.get('project_id') != document['project_id']:
                continue
            revisions.update(source['revision'] for source in row.payload.get('documents', [])
                if source.get('document_id') == document['id'] and type(source.get('revision')) is int)
        for row in self.records.list(JOBS_COLLECTION):
            if row.payload.get('project_id') != document['project_id'] or not self._entered(row):
                continue
            revisions.update(source['expected_revision'] for source in row.payload.get('documents', [])
                if source.get('document_id') == document['id'] and type(source.get('expected_revision')) is int)
        return self.policy({'operation': 'covered', 'revision': document['revision'], 'markdown': markdown,
            'previous': [{'revision': revision, 'markdown': self.documents.markdown(document['id'], revision=revision)}
                for revision in revisions]})

    def _select(self):
        candidates, authority = [], SourceEgressService(self.records)
        history = {}
        for job in self.records.list(JOBS_COLLECTION):
            value = job.payload
            if value.get('project_id') is not None and value.get('status') in {'done', 'empty'}:
                own = (value['project_id'], value.get('scene'))
                history[own] = max(history.get(own, ''), value.get('date', ''))
        projects = sorted({row.payload['project_id'] for row in self.records.list('documents')
            if isinstance(row.payload.get('project_id'), str)})
        for project in projects:
            if is_private_project(self.records, project):
                continue
            hidden = hidden_outcome_ids(self.records, project)
            for identity, document in self.library.docs(project).items():
                if identity in hidden:
                    continue
                recall = self.records.read('v2_document_recall', identity)
                if recall is not None and recall.payload.get('state') == 'forgotten':
                    continue
                markdown = self.documents.markdown(identity, revision=document['revision'])
                if not isinstance(markdown, str) or not markdown.strip() or self._covered(document, markdown):
                    continue
                scope = WorkScope('local-user', project)
                try:
                    roots = document_roots(self.records, scope, document.get('source_refs', []))
                    snapshot = authority.snapshot(scope, [{'type': kind, 'id': owner, 'revision': revision}
                        for kind, owner, revision in roots])
                    authority.require(snapshot, 'generation')
                except RecognitionError:
                    continue
                assignment = scene_of(self.records, 'document', identity)
                scene = assignment.get('scene') if assignment and assignment.get('project_id') == project else None
                candidates.append({'project_id': project, 'scene': scene, 'document_id': identity,
                    'expected_revision': document['revision'], 'created_at': document.get('created_at'),
                    'markdown': markdown, 'last_auto_at': history.get((project, scene), '')})
        return self.policy({'operation': 'select', 'candidates': candidates})

    def _configured(self):
        if self.models is None:
            return False
        configuration = self.models.public()['generation']
        local = urlsplit(str(configuration.get('base_url', ''))).hostname in {'localhost', '127.0.0.1', '::1'}
        return (configuration.get('configured') is True and configuration.get('enabled', True) is True
            and (local or configuration.get('allow_remote') is True))

    def _success(self, row):
        value = row.payload
        if not value.get('project_id') or not value.get('documents'):
            return None
        result = _completed_result(self.records, self.service, WorkScope('local-user', value['project_id']),
            value.get('scene'), value['documents'])
        if result is None:
            return None
        basis = _basis(self.records, WorkScope('local-user', value['project_id']), result['turn_id'])
        return {'turn_id': result['turn_id'], 'candidate_ids': basis['ids'],
            'status': 'done' if basis['ids'] else 'empty', 'reason': None if basis['ids'] else 'no_insights'}

    def _entered(self, row):
        value = row.payload
        if value.get('turn_id'):
            return True
        if not value.get('project_id') or not value.get('documents'):
            return False
        base = _key(value['project_id'], value.get('scene'), value['documents'])
        return any(index.payload.get('identity', {}).get('kind') == 'memory.propose_insights'
            and index.payload['identity'].get('project') == value['project_id']
            and isinstance(index.payload['identity'].get('key'), str)
            and (index.payload['identity']['key'] == base or index.payload['identity']['key'].startswith(base + ':retry-of:'))
            for index in self.records.list('v2_memory_turn_keys'))

    def _failure(self, row, error=None):
        code = error.detail.get('code') if isinstance(error, InspirationExtractionError) else None
        if code == 'automatic_inspiration_disabled' or isinstance(error, RecognitionConflict) and str(error) == 'automatic_inspiration_disabled':
            return {'status': 'skipped', 'reason': 'automatic_disabled'}
        if code == 'automatic_inspiration_retry_changed':
            return {'status': 'failed', 'reason': 'generation_unavailable'}
        if isinstance(error, RecognitionConflict) and str(error) == 'automatic_inspiration_sources_changed':
            return {'status': 'failed', 'reason': 'sources_changed'}
        if code == 'inspiration_material_budget_insufficient':
            return {'status': 'failed', 'reason': 'material_budget_insufficient'}
        if code in {'inspiration_sources_changed', 'inspiration_document_unavailable'}:
            return {'status': 'failed', 'reason': 'sources_changed'}
        turn_id = row.payload.get('turn_id') or (error.detail.get('turn_id') if isinstance(error, InspirationExtractionError) else None)
        if turn_id is not None and _database(self.records).is_file():
            try:
                from backend.recognition.external_turn_facts import _readonly
                with _readonly(_database(self.records)) as connection:
                    events = [json.loads(raw[0]) for raw in connection.execute(
                        'SELECT event_json FROM ai_turn_events WHERE turn_id=? ORDER BY sequence', (turn_id,))]
                if events and events[-1]['type'] not in {'turn.failed', 'turn.cancelled', 'turn.completed'}:
                    return {'status': 'unknown', 'reason': 'generation_unknown'}
            except (ValueError, OSError, sqlite3.Error):
                return {'status': 'unknown', 'reason': 'generation_unknown'}
        if code == 'inspiration_generation_unknown':
            return {'status': 'unknown', 'reason': 'generation_unknown'}
        return {'status': 'failed', 'reason': 'generation_failed' if error is not None else 'automatic_interrupted'}

    def _execute(self, row):
        identity = row.object_id
        if not self._configured():
            return self._update(identity, status='skipped', reason='generation_disabled')
        if not row.payload.get('documents'):
            selected = self._select()
            if not selected:
                return self._update(identity, status='skipped', reason='no_eligible_materials')
            project, scene = selected[0]['project_id'], selected[0]['scene']
            row = self._update(identity, project_id=project, scene=scene,
                documents=[{key: source[key] for key in ('document_id', 'expected_revision')} for source in selected])
        project, scene = row.payload['project_id'], row.payload.get('scene')
        if not egress_allowed(self.records, self.models, project, 'generation'):
            configuration = self.models.public()['generation']
            if urlsplit(str(configuration.get('base_url', ''))).hostname not in {'localhost', '127.0.0.1', '::1'}:
                return self._update(identity, status='skipped', reason='generation_disabled')
        self._guard(identity)
        result = extract_inspirations(self.models, self.service, self.documents, project=project, scene=scene,
            selected=row.payload['documents'], execution_guard=lambda reader=None: self._guard(identity, reader),
            on_turn=lambda turn_id: self._update(identity, turn_id=turn_id), retry_version='@3')
        basis = _basis(self.records, WorkScope('local-user', project), result['turn_id'])
        return self._update(identity, status='done' if basis['ids'] else 'empty', reason=None if basis['ids'] else 'no_insights',
            turn_id=result['turn_id'], candidate_ids=basis['ids'])

    @with_connection_scope
    def run_due(self):
        if not self._lock.acquire(blocking=False):
            return None
        try:
            slot = self._slot()
            if not slot['due']:
                return None
            identity = slot['key']
            self._active_job = identity
            with self.records.begin() as tx:
                row = tx.read(JOBS_COLLECTION, identity)
                if row is None:
                    now = self.now().isoformat()
                    row = tx.put(JOBS_COLLECTION, identity, {'date': slot['date'], 'scheduled_for': slot['scheduled_for'],
                        'created_at': now, 'updated_at': now, 'settings_revision': self.settings(tx)['revision'],
                        'status': 'running', 'reason': None, 'owner_id': self.owner, 'project_id': None, 'scene': None,
                        'documents': [], 'turn_id': None, 'candidate_ids': [],
                        'selection_policy': 'inspiration_schedule@1', 'retry_policy': 'retry@3'}, expected_revision=0)
                elif row.payload.get('status') != 'running':
                    return None
                else:
                    row = tx.put(JOBS_COLLECTION, identity, {**row.payload, 'owner_id': self.owner,
                        'updated_at': self.now().isoformat()}, expected_revision=row.revision)
                tx.commit()
            try:
                success = self._success(row)
                if success is not None:
                    return self._update(identity, **success)
                if self._entered(row):
                    return self._update(identity, **self._failure(row))
                return self._execute(row)
            except Exception as error:
                current = self.records.read(JOBS_COLLECTION, identity)
                if current is None or current.payload.get('owner_id') != self.owner:
                    raise
                _LOGGER.warning('automatic_inspiration_failed exception_type=%s', type(error).__name__)
                return self._update(identity, **self._failure(current, error))
        finally:
            self._active_job = None
            self._lock.release()

    def _observed(self, row):
        try:
            success = self._success(row)
            if success is not None:
                return success
            if row.payload.get('status') in {'done', 'empty'}:
                return {**row.payload, 'status': 'failed', 'reason': 'generation_evidence_unavailable', 'candidate_ids': []}
            if row.payload.get('status') == 'running' and self._active_job != row.object_id:
                return {**row.payload, **self._failure(row)}
            return row.payload
        except (RecognitionError, KeyError, TypeError, ValueError, OSError, sqlite3.Error):
            return {**row.payload, 'status': 'failed', 'reason': 'generation_evidence_unavailable', 'candidate_ids': []}

    def _items(self, row, observed):
        project = row.payload.get('project_id')
        if project is None:
            return []
        scope = WorkScope('local-user', project)
        return [view for identity in observed.get('candidate_ids', [])
            if (view := insight_view(self.records, scope, identity, service=self.service)) is not None]

    def _unread(self, row, observed, items):
        seen = self.records.read(SEEN_COLLECTION, row.object_id)
        saved = seen.payload if seen is not None else {}
        pending = [item for item in items if item['kind'] == 'candidate' and item['state'] == 'pending']
        unread = sum(item['id'] not in saved.get('candidate_ids', []) for item in pending)
        failure = observed['status'] in {'failed', 'unknown'} and saved.get('failure_revision') != row.revision
        return len(pending), unread, bool(unread or failure)

    @with_connection_scope
    def job_views(self, fallback_project='default'):
        result, now = [], self.now()
        for row in self.records.list(JOBS_COLLECTION):
            if row.payload.get('project_id') is None:
                continue
            observed = self._observed(row)
            items = self._items(row, observed)
            pending, unread, attention = self._unread(row, observed, items)
            updated = datetime.fromisoformat(row.payload['updated_at'].replace('Z', '+00:00'))
            if not pending and not attention and now - updated > timedelta(days=1):
                continue
            status = observed['status']
            state = ('processing' if status == 'running' else 'failed' if status in {'failed', 'unknown'}
                else 'pending' if pending else 'done')
            project = row.payload['project_id']
            result.append({'id': row.object_id, 'kind': 'inspiration', 'title': '新灵感', 'state': state,
                'progress': None, 'pending_count': pending, 'unread_count': unread, 'attention': attention,
                'error': observed.get('reason'), 'message': _MESSAGES.get(observed.get('reason'), ''),
                'target': {'type': 'inspiration', 'id': row.object_id, 'project_id': project,
                    'scene': row.payload.get('scene')}, 'updated_at': row.payload['updated_at']})
        return result

    def represented_candidates(self):
        result = set()
        for row in self.records.list(JOBS_COLLECTION):
            observed = self._observed(row)
            result.update(observed.get('candidate_ids', []))
        return result

    @with_connection_scope
    def detail(self, identity, project):
        row = self.records.read(JOBS_COLLECTION, identity)
        if row is None or row.payload.get('project_id') != project:
            raise HTTPException(404, 'automatic_inspiration_not_found')
        observed = self._observed(row)
        items = self._items(row, observed)
        _, unread, attention = self._unread(row, observed, items)
        return {'job_id': identity, 'project_id': row.payload['project_id'],
            'scene': row.payload.get('scene'), 'date': row.payload['date'], 'status': observed['status'],
            'reason': observed.get('reason'), 'message': _MESSAGES.get(observed.get('reason'), ''),
            'revision': row.revision, 'unread_count': unread, 'attention': attention, 'items': items}

    @with_connection_scope
    def mark_read(self, identity, body):
        if (not isinstance(body, dict) or set(body) != {'project_id', 'expected_revision'}
                or type(body['expected_revision']) is not int or body['expected_revision'] < 1):
            raise HTTPException(400, 'invalid_automatic_inspiration_read')
        project = _project(body['project_id'])
        detail = self.detail(identity, project)
        if detail['revision'] != body['expected_revision']:
            raise HTTPException(409, 'automatic_inspiration_changed')
        with self.records.begin() as tx:
            row = tx.read(JOBS_COLLECTION, identity)
            if row is None or row.revision != body['expected_revision'] or row.payload.get('project_id') != project:
                raise HTTPException(409, 'automatic_inspiration_changed')
            prior = tx.read(SEEN_COLLECTION, identity)
            candidate_ids = sorted({*(prior.payload.get('candidate_ids', []) if prior else []),
                *(item['id'] for item in detail['items'] if item['kind'] == 'candidate')})
            tx.put(SEEN_COLLECTION, identity, {'project_id': row.payload.get('project_id'),
                'candidate_ids': candidate_ids, 'failure_revision': row.revision,
                'read_at': self.now().isoformat()}, expected_revision=prior.revision if prior else 0)
            tx.commit()
        return {'read': True, 'unread_count': 0}


def install_automatic_inspiration_routes(application, *, automatic):
    settings = APIRouter(prefix='/api/v2/settings')
    library = APIRouter(prefix='/api/v2/library/inspirations/automatic')

    @settings.get('/inspirations')
    @with_connection_scope
    def read_settings():
        return automatic.settings()

    @settings.patch('/inspirations')
    async def save_settings(request: Request):
        body = await _json(request)
        try:
            return await run_in_threadpool(with_connection_scope(automatic.save_settings), body)
        except SQLiteUnitOfWorkConflict:
            raise HTTPException(409, 'inspiration_schedule_changed') from None

    @library.get('/{identity}')
    def read_job(identity: str, project_id: str):
        return automatic.detail(_project(identity), _project(project_id))

    @library.post('/{identity}/read')
    async def read_job_seen(identity: str, request: Request):
        body = await _json(request)
        try:
            return await run_in_threadpool(automatic.mark_read, _project(identity), body)
        except SQLiteUnitOfWorkConflict:
            raise HTTPException(409, 'automatic_inspiration_changed') from None

    application.include_router(settings)
    application.include_router(library)
