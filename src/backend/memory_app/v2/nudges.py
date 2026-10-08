"""从现有资料库重建提醒投影，原事实和问的外发终检仍归原领域服务。"""
import asyncio
from collections.abc import Mapping
from copy import deepcopy
from datetime import timezone
from functools import lru_cache
import gzip
import json
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from fastapi import APIRouter, HTTPException, Request
from core.storage_provider import SQLiteUnitOfWorkConflict
from core.document_engine import SQLiteDocumentRepository
from backend.recognition import RecognitionConflict, WorkScope
from ..recall_state import is_recall_excluded
from ..original_sources import document_roots, original
from ..workspace_contracts import _json, _project
from ..document_visibility import LegacyDocumentVisibility
from ..transaction_records import TransactionRecords
from .layers import facts_of
from .insights import insight_view
from .policies import get, version
from .policies.insight_time import instant
from .privacy import egress_allowed, is_private_project
from .projects import scene_of
from .usage import recall_weight


DATES = 'v2_dates'
NUDGES = 'v2_nudges'
SETTINGS = 'v2_nudge_settings'


def _identity(prefix, *parts):
    return prefix + uuid5(NAMESPACE_URL, json.dumps(parts, ensure_ascii=False)).hex


def _view(row):
    return {'id': row.object_id, **row.payload, 'revision': row.revision}


class _SourceReader(TransactionRecords):
    """沿已有 overview 接点，只在调用者的事务中读取可见性条件。"""
    def list_matching(self, collection, **fields):
        return tuple(row for row in self.list(collection)
                     if all(row.payload.get(key) == value for key, value in fields.items()))


@lru_cache(maxsize=1)
def _cities():
    path = Path(__file__).parent / 'resources' / 'geonames_cities500.json.gz'
    with gzip.open(path, 'rt', encoding='utf-8') as raw:
        data = json.load(raw)
    if data.get('schema') != 1 or not isinstance(data.get('cities'), list) or not data['cities']:
        raise ValueError('invalid_nudge_city_resource')
    by_name, lengths = {}, {}
    for city in data['cities']:
        if (set(city) != {'id', 'name', 'country', 'aliases'} or type(city['id']) is not int
                or not isinstance(city['name'], str) or not isinstance(city['aliases'], list)):
            raise ValueError('invalid_nudge_city_resource_entry')
        for alias in city['aliases']:
            normalized = alias.casefold()
            by_name.setdefault(normalized, {})[city['id']] = {'id': city['id'], 'name': city['name'], 'country': city['country']}
            lengths.setdefault(normalized[:2], set()).add(len(normalized))
    return {'names': by_name, 'lengths': lengths}


class NudgeService:
    """每个空间共享实际送达日额度，持久写入都由短事务和 CAS 负责。"""
    def __init__(self, records, *, library, query, models, signals, reminders,
                 now, local_timezone, policy_version=None):
        self.records, self.library, self.query, self.models = records, library, query, models
        self.signals, self.reminders = signals, reminders
        self.now, self.local_timezone, self.policy_version = now, local_timezone, policy_version

    def _policy(self):
        return get('nudge', version=self.policy_version)

    def run(self):
        """原每日调度器的同步回调，只消费当前空间已选择的策略。"""
        if self.policy_version is None:
            try:
                version('nudge')
            except ValueError as error:
                if str(error) != 'unknown_policy_interface':
                    raise
                return
        projects = {_project(row.object_id) for row in self.records.list('v2_projects')}
        # 用户提醒不建工作项或整理稿；旧事实与提醒事实都参与只读项目发现。
        for collection in ('workspace_items', 'documents', 'recognitions', 'v2_reminders'):
            for row in self.records.list(collection):
                project = row.payload.get('project_id')
                if project is None:
                    scope = row.payload.get('scope')
                    project = scope.get('project_id') if isinstance(scope, Mapping) else None
                if project is not None:
                    projects.add(_project(project))

        async def consume():
            for project in sorted(projects):
                await self.deliver(project)

        # DailyJobs 在线程执行同步回调；本轮沿同一个事件循环串行复用消费服务。
        return asyncio.run(consume())

    def settings(self, *, reader=None):
        row = (reader or self.records).read(SETTINGS, 'local')
        return {'limit': row.payload['limit'] if row else self._policy().default_limit,
                'revision': row.revision if row else 0}

    def set_limit(self, limit, *, expected_revision):
        policy = self._policy()
        if (type(limit) is not int or not 0 <= limit <= policy.maximum_limit
                or type(expected_revision) is not int or expected_revision < 0):
            raise HTTPException(400, 'invalid_nudge_settings')
        with self.records.begin() as tx:
            current = self.settings(reader=tx)
            if current['revision'] != expected_revision:
                raise HTTPException(409, 'nudge_settings_changed')
            saved = tx.put(SETTINGS, 'local', {'limit': limit}, expected_revision=expected_revision)
            tx.commit()
        return {'limit': limit, 'revision': saved.revision}

    def _date_inputs(self, reader, project, document):
        scope, captures, closure = WorkScope('local-user', project), {}, []
        try:
            for kind, identity, revision in document_roots(reader, scope, document['source_refs'], optional=True):
                source = original(reader, scope, kind, identity)
                if source.revision != revision:
                    raise HTTPException(409, 'nudge_source_changed')
                closure.append((kind, identity, revision, deepcopy(source.payload)))
                owner = identity if kind == 'original_item' else (
                    source.payload['workspace_item_id']
                    if source.payload.get('identity_method') == 'workspace_confirmation' else None)
                if owner is not None:
                    row = reader.read('workspace_items', owner)
                    if (row is None or row.payload.get('project_id') != project
                            or row.payload.get('status') != 'confirmed'
                            or row.payload.get('document_id') != document['id']):
                        raise HTTPException(409, 'nudge_source_changed')
                    captures[('original_item', owner)] = row.payload.get('created_at')
                else:
                    captures[(kind, identity)] = source.payload.get('created_at')
        except RecognitionConflict as error:
            raise HTTPException(409, 'nudge_source_changed') from error
        return {'captures': tuple(captures[key] for key in sorted(captures)), 'closure': tuple(closure)}

    def _sources(self, project):
        scope, result = WorkScope('local-user', project), []
        for view in self.library.insights(project):
            if (view['kind'] != 'recognition' or view['state'] != 'active'
                    or is_recall_excluded(self.records, scope, view['id'])):
                continue
            row = self.records.read('recognitions', view['id'])
            if row is None:
                raise HTTPException(409, 'nudge_source_changed')
            result.append({'source_kind': 'recognition', 'source_id': view['id'],
                'source_revision': view['revision'], 'project_id': project, 'scene': view['scene'],
                'title': view['text'], 'text': view['text'], 'source_at': row.payload.get('created_at'),
                '_guard': ('recognitions', row.object_id, row.revision, deepcopy(row.payload))})
        for identity, document in sorted(self.library.docs(project).items()):
            if recall_weight(self.records, 'document', identity) == 0:
                continue
            row = self.records.read('documents', identity)
            if row is None:
                raise HTTPException(409, 'nudge_source_changed')
            markdown = self.library.documents.markdown(identity, revision=document['revision'])
            if not isinstance(markdown, str):
                raise HTTPException(409, 'nudge_source_changed')
            date_inputs = self._date_inputs(self.records, project, document)
            policy = self._policy()
            try:
                source_at = policy.capture_reference(date_inputs['captures'])
            except ValueError as error:
                raise HTTPException(409, 'invalid_nudge_source_reference') from error
            result.append({'source_kind': 'document', 'source_id': identity,
                'source_revision': document['revision'], 'project_id': project,
                'scene': self.library.scene('document', identity, project), 'title': document['title'],
                'text': markdown, 'source_at': source_at, '_date_guard': date_inputs,
                '_guard': ('documents', row.object_id, row.revision, deepcopy(row.payload))})
        return result

    def _validate_sources(self, reader, sources):
        for source in sources:
            collection, identity, revision, payload = source['_guard']
            current = reader.read(collection, identity)
            if current is None or current.revision != revision or current.payload != payload:
                raise HTTPException(409, 'nudge_source_changed')
            if source['source_kind'] == 'recognition':
                scope = WorkScope('local-user', source['project_id'])
                if not hasattr(reader, '_connect'):
                    # 另连读模型不能代替当前事务的原领域资格终检。
                    qualified = self.library.service._qualified_recognition(reader, current)
                    if (qualified is None or qualified.id != identity or qualified.scope != scope
                            or qualified.revision != current.revision or not qualified.authorized):
                        raise HTTPException(409, 'nudge_source_changed')
                view = insight_view(reader, scope, identity, service=self.library.service)
                if view is None or view['state'] != 'active':
                    raise HTTPException(409, 'nudge_source_changed')
                scene = view['scene']
            else:
                # 与 overview 相同的薄 reader 接点复用原可见性业务，不再开写事务。
                enlisted = reader if hasattr(reader, '_connect') else _SourceReader(reader)
                documents = SQLiteDocumentRepository(enlisted, namespace_id=self.library.documents.namespace_id)
                document = documents.read(identity)
                if (document is None or document.get('id') != identity
                        or document.get('project_id') != source['project_id'] or document.get('status') == 'archived'
                        or document.get('revision') != source['source_revision']
                        or documents.markdown(identity, revision=source['source_revision']) != source['text']
                        or not LegacyDocumentVisibility.from_repository(documents,
                            project_id=source['project_id'], document_ids={identity}).allows(document)):
                    raise HTTPException(409, 'nudge_source_changed')
                if self._date_inputs(reader, source['project_id'], payload) != source['_date_guard']:
                    raise HTTPException(409, 'nudge_source_changed')
                if recall_weight(reader, 'document', identity) == 0:
                    raise HTTPException(409, 'nudge_source_changed')
                assignment = scene_of(reader, 'document', identity)
                scene = assignment['scene'] if assignment and assignment.get('project_id') == source['project_id'] else None
            if scene != source['scene']:
                raise HTTPException(409, 'nudge_scene_changed')

    def refresh(self, project_id):
        project = _project(project_id)
        if is_private_project(self.records, project):
            return []
        policy, now, sources = self._policy(), self.now(), self._sources(project)
        prepared = {}
        for source in sources:
            texts = [source['text']] if source['source_kind'] == 'recognition' else facts_of(source['text'])
            for text in texts:
                annual = policy.annual_source(text, source['source_kind'])
                for event in policy.dates(text, source_at=source['source_at'], now=now,
                                          local_timezone=self.local_timezone, annual=annual):
                    identity = _identity('date-', project, source['source_kind'], source['source_id'], event['span'])
                    prepared[identity] = {key: value for key, value in source.items() if key not in {'_guard', '_date_guard'}}
                    prepared[identity].update(event, text=text)
        with self.records.begin() as tx:
            if is_private_project(tx, project):
                raise HTTPException(409, 'nudge_project_changed')
            self._validate_sources(tx, sources)
            old = {row.object_id: row for row in tx.list(DATES) if row.payload['project_id'] == project}
            result = []
            for identity, payload in sorted(prepared.items()):
                row = old.pop(identity, None)
                if row is None or row.payload != payload:
                    row = tx.put(DATES, identity, payload, expected_revision=row.revision if row else 0)
                result.append(_view(row))
            # 日期索引是可重建投影；此处只移除已不再合格的推导，不删除原资料。
            for row in old.values():
                tx.delete(DATES, row.object_id, expected_revision=row.revision)
            tx.commit()
        return result

    def _turns(self):
        settings = self.signals.settings()
        cleared = instant(settings['cleared_at'])
        turns = []
        writer_for = getattr(self.records, 'writer_for', None)
        for row in self.records.list('v2_turns'):
            if cleared is not None and ((at := instant(row.payload.get('created_at'))) is None or at <= cleared):
                continue
            writer = writer_for('v2_turns', row.object_id, 1) if callable(writer_for) else None
            turns.append({**row.payload, 'id': row.object_id,
                          **({'by': writer['by']} if writer else {})})
        return turns, settings['enabled']

    def place_candidates(self, project_id):
        project = _project(project_id)
        if is_private_project(self.records, project):
            return []
        policy, resource, result = self._policy(), _cities(), []
        for source in self._sources(project):
            for city in policy.city_mentions(source['text'], source['scene'], resource):
                result.append({key: value for key, value in source.items() if key not in {'_guard', '_date_guard'}})
                result[-1].update(id=_identity('place-', project, source['source_kind'], source['source_id'], city['id']),
                                 city=city['display'], city_id=city['id'], kind='place')
        return result

    def _reserve(self, project, items, *, now):
        policy = self._policy()
        # 根绑定读模型在写事务外准备；选取与所有资格仍在下面同一事务复验。
        sources = self._sources(project) if any(item['kind'] != 'reminder' for item in items) else []
        with self.records.begin() as tx:
            if is_private_project(tx, project):
                raise HTTPException(409, 'nudge_project_changed')
            history = [{**row.payload, 'id': row.object_id} for row in tx.list(NUDGES)]
            chosen = policy(items, now=now, local_timezone=self.local_timezone,
                            limit=self.settings(reader=tx)['limit'], history=history)
            reserved = []
            for item in chosen:
                day = now.astimezone(self.local_timezone).date().isoformat()
                identity = _identity('nudge-', item['id']) if item['kind'] == 'reminder' else _identity('nudge-', item['id'], day)
                old = tx.read(NUDGES, identity)
                if old is not None and old.payload['state'] != 'failed' and (
                        item['kind'] != 'reminder' or old.payload.get('reminder_revision') == item['revision']):
                    continue
                source = None
                if item['kind'] != 'reminder':
                    source = next((source for source in sources
                        if source['source_kind'] == item['source_kind'] and source['source_id'] == item['source_id']), None)
                    if source is None or any(source[key] != item[key] for key in ('source_revision', 'scene', 'source_at')):
                        raise HTTPException(409, 'nudge_source_changed')
                    self._validate_sources(tx, [source])
                payload = {**item, 'event_id': item['id'], 'delivery_at': now.astimezone(timezone.utc).isoformat(),
                           'text': item['text'] if item['kind'] == 'reminder' else '',
                           'state': 'pending', 'action': None, 'action_at': None, 'evidence_ids': []}
                payload.pop('id', None)
                payload.pop('revision', None)
                if item['kind'] == 'reminder':
                    payload['reminder_revision'] = item['revision']
                saved = tx.put(NUDGES, identity, payload, expected_revision=old.revision if old else 0)
                # 原件闭包只随本轮执行保留，不写入投影或回执。
                reserved.append({**_view(saved), '_source_text': item['text'], '_source_input': source})
            tx.commit()
        return reserved

    async def _render(self, item, *, now):
        policy = self._policy()
        if item['kind'] == 'reminder':
            return {'text': item['text'], 'evidence_ids': [], 'model_used': False, 'egress_receipt_id': None}
        if not egress_allowed(self.records, self.models, item['project_id'], 'generation'):
            return self._template(item, now=now)
        current = next((source for source in self._sources(item['project_id'])
                        if source['source_kind'] == item['source_kind'] and source['source_id'] == item['source_id']), None)
        if (current is None or any(current[key] != item[key] for key in ('source_revision', 'scene', 'source_at'))
                or current.get('_date_guard') != item['_source_input'].get('_date_guard')):
            raise HTTPException(409, 'nudge_source_changed')
        self._validate_sources(self.records, [item['_source_input']])
        question = policy.situation(item, now=now, local_timezone=self.local_timezone)
        plan = self.query.prepare_ask(item['project_id'], question, scene=item['scene'],
            retrieval_question=item['_source_text'], situation=question,
            evidence_limit=policy.evidence_limit, profile_limit=policy.profile_limit)
        # 情境本身来自资料；触发条目必须属于同一次有预算且有外发终检的实际证据。
        trigger = next((candidate for candidate in plan['chosen']
                        if candidate['kind'] == item['source_kind'] and candidate['entry']['id'] == item['source_id']), None)
        if trigger is None:
            return self._template(item, now=now)
        if trigger['entry']['revision'] != item['source_revision']:
            raise HTTPException(409, 'nudge_source_changed')
        preview = self.query.store_ask_preview(plan)
        answer = await self.query.execute_ask(preview, item['project_id'], question, True)
        return {'text': policy.response_text(answer['answer']),
                'evidence_ids': [source['id'] for source in answer['sources']],
                'model_used': True, 'egress_receipt_id': preview}

    def _template(self, item, *, now):
        return {'text': self._policy().fallback(item, now=now, local_timezone=self.local_timezone),
                'evidence_ids': [item['source_id']], 'model_used': False, 'egress_receipt_id': None}

    def _finish(self, item, rendered):
        with self.records.begin() as tx:
            row = tx.read(NUDGES, item['id'])
            if row is None or row.revision != item['revision'] or row.payload['state'] != 'pending':
                raise HTTPException(409, 'nudge_changed')
            if is_private_project(tx, item['project_id']):
                raise HTTPException(409, 'nudge_project_changed')
            if item['kind'] == 'reminder':
                reminder = self.reminders.read(item['event_id'], project_id=item['project_id'], reader=tx)
                if (reminder['revision'] != item['reminder_revision'] or reminder['state'] != 'active'
                        or instant(reminder['at']) > self.now()):
                    raise HTTPException(409, 'nudge_reminder_changed')
            else:
                # 预约已冻结匹配来源；完成仍用当前事务核完整正文、闭包、资格与场景。
                self._validate_sources(tx, [item['_source_input']])
            saved = tx.put(NUDGES, item['id'], {**row.payload, **rendered, 'state': 'delivered'},
                           expected_revision=row.revision)
            tx.commit()
        return _view(saved)

    def _failed(self, identity):
        with self.records.begin() as tx:
            row = tx.read(NUDGES, identity)
            if row is not None and row.payload['state'] == 'pending':
                tx.put(NUDGES, identity, {**row.payload, 'state': 'failed'}, expected_revision=row.revision)
                tx.commit()

    def list(self, project_id):
        project = _project(project_id)
        if is_private_project(self.records, project):
            return []
        now = self.now()
        active = {row['id'] for row in self.reminders.due(project)}
        return [_view(row) for row in self.records.list(NUDGES)
                if row.payload['project_id'] == project and row.payload['state'] == 'delivered'
                and row.payload.get('action') != 'closed'
                and (row.payload['event_id'] in active if row.payload['kind'] == 'reminder' else
                     instant(row.payload['delivery_at']).astimezone(self.local_timezone).date() == now.astimezone(self.local_timezone).date())]

    async def deliver(self, project_id, *, city=None):
        project, now = _project(project_id), self.now()
        if is_private_project(self.records, project):
            return []
        policy = self._policy()
        turns, enabled = self._turns()
        scheduled = policy.delivery_at(turns, now=now, local_timezone=self.local_timezone, recording_enabled=enabled)
        candidates = [{**row, 'kind': 'date', 'delivery_at': scheduled} for row in self.refresh(project)] if scheduled else []
        if city is not None:
            resolved = policy.city_mentions('', city, _cities())
            admitted = {row['id'] for row in resolved}
            candidates.extend({**row, 'delivery_at': scheduled} for row in self.place_candidates(project)
                              if row['city_id'] in admitted and scheduled)
        candidates.extend({**row, 'kind': 'reminder'} for row in self.reminders.due(project))
        reserved = self._reserve(project, candidates, now=now)
        try:
            for item in reserved:
                rendered = await self._render(item, now=now)
                self._finish(item, rendered)
        except BaseException:
            # 未发送的预约一起释放额度；完成的投影和原失败事实都保留。
            for item in reserved:
                self._failed(item['id'])
            raise
        return self.list(project)

    def feedback(self, identity, *, project_id, action, expected_revision):
        project, identity = _project(project_id), _project(identity)
        if action not in {'opened', 'ignored', 'closed'} or type(expected_revision) is not int or expected_revision < 1:
            raise HTTPException(400, 'invalid_nudge_feedback')
        try:
            with self.records.begin() as tx:
                row = tx.read(NUDGES, identity)
                if row is None or row.payload['project_id'] != project:
                    raise HTTPException(404, 'nudge_not_found')
                if row.revision != expected_revision or row.payload['state'] != 'delivered':
                    raise HTTPException(409, 'nudge_changed')
                saved = tx.put(NUDGES, identity, {**row.payload, 'action': action,
                    'action_at': self.now().astimezone(timezone.utc).isoformat()}, expected_revision=row.revision)
                tx.commit()
        except SQLiteUnitOfWorkConflict:
            raise HTTPException(409, 'nudge_changed') from None
        return _view(saved)


def install_nudge_routes(application, *, service):
    """新 v2 接口只调用应用持有的同一个主人，不生成或投放提醒。"""
    router = APIRouter(prefix='/api/v2')

    def settings_result(operation, **fields):
        try:
            # 已保存数量不替代当前策略资格，也不强选尚未激活的候选。
            service._policy()
            return operation(**fields)
        except ValueError as error:
            if str(error) != 'unknown_policy_interface':
                raise
            raise HTTPException(409, 'nudge_policy_unavailable') from None

    @router.get('/nudges')
    async def list_nudges(project_id: str):
        return service.list(project_id)

    @router.patch('/nudges/{id}')
    async def patch_nudge(id: str, request: Request):
        body = await _json(request)
        if set(body) != {'project_id', 'action', 'expected_revision'} or not isinstance(body.get('action'), str):
            raise HTTPException(400, 'invalid_nudge_feedback_fields')
        return service.feedback(id, **body)

    @router.get('/settings/nudges')
    async def get_settings():
        return settings_result(service.settings)

    @router.patch('/settings/nudges')
    async def patch_settings(request: Request):
        body = await _json(request)
        if set(body) != {'limit', 'expected_revision'}:
            raise HTTPException(400, 'invalid_nudge_settings')
        return settings_result(service.set_limit, **body)

    application.include_router(router)
