"""Explicit review orchestration over existing signal and recall owners."""
from datetime import datetime, timedelta, timezone
import json
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from backend.recognition import WorkScope
from backend.shared.document_visibility import LegacyDocumentVisibility
from core.storage_provider import SQLiteUnitOfWorkConflict
from ..transaction_records import TransactionRecords
from ..workspace_contracts import _json, _project
from .policies import get, version
from .recall_preferences import set_preference
from .signals import SignalService, _fact_time

REVIEWS = 'v2_signal_reviews'
DECISIONS = 'v2_signal_decisions'


def _key(project, kind, identities):
    # Local identifiers only; no digest or question text becomes a fact.
    return json.dumps([project, kind, identities], ensure_ascii=True, separators=(',', ':'))


class SignalReviews:
    def __init__(self, records, *, service, documents, runtime_root=None,
                 now=lambda: datetime.now(timezone.utc)):
        self.records, self.service, self.documents = records, service, documents
        self.runtime_root, self.now = runtime_root, now

    def _groups(self, project):
        if self.runtime_root is None:
            return []
        from ..kernel.receipt_projection import kernel_call_groups
        return kernel_call_groups(self.runtime_root, project=project, remote_only=False,
                                  records=self.records, include_answer_input=True)

    def _object(self, project, layer, identity):
        scope = WorkScope('local-user', project)
        if layer == 'insight':
            value = self.service.get_recognition(scope=scope, recognition_id=identity)
            row = self.records.read('recognitions', identity)
            if value is None or not value.authorized or not value.evidence_eligible or value.state != 'active':
                return None
            collection, revision, title = 'recognitions', value.revision, value.content.splitlines()[0]
            pref_collection = 'recognition_recall_preferences'
        elif layer in {'note', 'summary'}:
            value = self.documents.read(identity)
            row = self.records.read('documents', identity)
            if (value is None or value.get('project_id') != project or value.get('status') == 'archived'
                    or not LegacyDocumentVisibility.from_repository(self.documents, project_id=project).allows(value)):
                return None
            collection, revision, title = 'documents', value['revision'], value['title']
            pref_collection = 'v2_document_recall'
        else:
            return None
        pref = self.records.read(pref_collection, identity)
        if pref and (pref.payload.get('state') != 'normal'
                     or collection == 'recognitions' and (pref.payload.get('project_id') != project
                         or pref.payload.get('user_id') != scope.user_id)):
            return None
        return {'kind':'insight' if collection == 'recognitions' else 'document',
                'id':identity, 'revision':revision}, title, [collection, identity, row.revision], [pref_collection, identity, pref.revision if pref else 0]

    def _candidates(self, project, clock):
        policy = get('review')
        groups = self._groups(project)
        by_id = {g['turn_id']:g for g in groups}
        frozen = lambda identity: by_id.get(identity, {}).get('answer_input')
        report = SignalService(self.records, now=self.now).report(kernel_groups=groups,
            read_model_input=frozen, since=clock-timedelta(days=policy.window_days), until=clock, project_id=project)
        if not report:
            return []
        turns = {row.object_id:row for row in self.records.list('v2_turns')
                 if row.payload.get('project_id') == project}
        candidates = []
        def add(kind, identities, strength, obj=None, guards=(), title=''):
            rows = [turns[i] for i in identities]
            candidates.append({'review_key':_key(project,kind, [obj['kind'],obj['id']] if obj else identities),
                'kind':kind, 'turn_ids':identities, 'object':obj, 'strength':strength,
                'event_at':max(_fact_time(r.payload['created_at']) for r in rows).isoformat(),
                'guards':[ ['v2_turns',r.object_id,r.revision] for r in rows]+list(guards)})
        for pair in report['reask']['pairs']:
            add('reask',pair['turn_ids'],1)
        # A missing input/citation is coverage uncertainty, never an unused zero.
        if report['unused']['unknown_turns'] == 0:
            seen = set()
            for unused in sorted(report['unused']['objects'],key=lambda item:(-item['sent'],item['layer'],item['object_id'])):
                if unused['sent'] < policy.minimum_sent or unused['cited'] != 0:
                    continue
                target = self._object(project, unused['layer'], unused['object_id'])
                if target is None or (target[0]['kind'], target[0]['id']) in seen:
                    continue
                obj, title, object_guard, pref_guard = target
                identities = []
                for identity, row in turns.items():
                    group = by_id.get(identity, {})
                    at = _fact_time(row.payload.get('created_at'))
                    if (at is None or not clock-timedelta(days=policy.window_days) <= at <= clock
                            or row.payload.get('by') == 'admin'
                            or not group.get('answer_input') or not any(c.get('status') == 'completed'
                                and c.get('model_call_purpose') == 'primary' and c.get('turn_id') == identity
                                for c in group.get('calls', []))):
                        continue
                    entries = row.payload.get('receipt',{}).get('ask',{}).get('context',{}).get('entries',[])
                    frozen_receipt = (group.get('answer') or {}).get('receipt', {}).get('ask', {})
                    receipt_id = frozen_receipt.get('egress_receipt_id')
                    receipt = self.records.read('workspace_ask_receipts', receipt_id) if isinstance(receipt_id, str) else None
                    chosen = (group.get('answer') or {}).get('chosen')
                    ordered = [entry for entry in entries if not entry.get('persona')]
                    if (receipt is None or receipt.payload.get('project_id') != project
                            or receipt.payload.get('status') != 'completed'
                            or frozen_receipt.get('context', {}).get('entries') != entries
                            or not isinstance(chosen, list) or len(chosen) != len(ordered)
                            or len(receipt.payload.get('sources', [])) < len(ordered)):
                        continue
                    for entry, candidate, source in zip(ordered, chosen, receipt.payload['sources']):
                        expected_kind = 'recognition' if obj['kind'] == 'insight' else 'document'
                        if (entry.get('layer') == unused['layer'] and entry.get('id') == obj['id']
                                and candidate.get('layer') == {'insight':'L3','summary':'L2','note':'L1'}[unused['layer']]
                                and candidate.get('entry', {}).get('id') == obj['id']
                                and candidate.get('project_id') == project
                                and source.get('kind') == expected_kind and source.get('id') == obj['id']
                                and source.get('revision') == obj['revision']):
                            identities.append(identity)
                            break
                if len(identities) >= policy.minimum_sent:
                    add('unused',sorted(identities),len(identities),obj,[object_guard,pref_guard],title)
                    seen.add((obj['kind'],obj['id']))
        for stopped in report['interruptions']['turns']:
            row = turns[stopped['turn_id']]
            if stopped['stop'] and row.payload.get('intent') == 'ask':
                add('stop',[row.object_id],stopped['stop'])
        return candidates

    def _render(self, item):
        rows = [self.records.read('v2_turns', identity) for identity in item['turn_ids']]
        if item['kind'] == 'unused':
            rows = sorted(rows,key=lambda row:(_fact_time(row.payload['created_at']),row.object_id))[-get('review').evidence_questions:]
        questions = [row.payload['user_text'] for row in rows]
        evidence = {'questions':questions}
        if item['kind'] == 'unused':
            evidence.update(sent=item['strength'], used=0)
        else:
            evidence['count'] = item['strength']
        if item['kind'] in {'reask', 'stop'}:
            evidence['answer'] = '\n'.join(rows[0].payload['receipt']['ask']['answer'].splitlines()[:2])
            title = questions[0]
        else:
            obj = item['object']
            target = self._object(rows[0].payload['project_id'], 'insight' if obj['kind']=='insight' else 'note',obj['id'])
            title = target[1] if target else obj['id']
            evidence['object'] = obj
        return {'id':item['id'],'kind':item['kind'],'title':title,'evidence':evidence,
                'effect':'cool' if item['kind']=='unused' else 'correction','revision':item['revision']}

    def current(self, project):
        project = _project(project)
        clock = self.now().astimezone(timezone.utc)
        initial, settings_revision = SignalService._settings(self.records)
        if not initial['enabled']:
            return {'items':[]}
        candidates = self._candidates(project, clock)
        prior = self.records.read(REVIEWS, project)
        old = {item['review_key']:item for item in prior.payload['items']} if prior else {}
        decisions = {row.payload['review_key'] for row in self.records.list(DECISIONS)}
        items = []
        for candidate in candidates:
            candidate['settings_revision'] = settings_revision
            candidate['policy'] = version('review')
            previous = old.get(candidate['review_key'])
            if previous:
                candidate['created_at'] = previous['created_at']
                candidate['id'] = previous['id']
                candidate['revision'] = previous['revision'] + int(any(previous.get(k)!=candidate[k] for k in ('guards','strength','object','settings_revision','policy')))
            else:
                candidate.update(id='review-'+uuid4().hex,revision=1,created_at=clock.isoformat())
            items.append(candidate)
        # Keep disappeared/expired identities so GET cannot refresh their lifetime.
        selected = get('review')(items,now=clock,dismissed=decisions,enabled=initial['enabled'])
        merged = {**old, **{item['review_key']:item for item in selected}}
        payload = {'items':list(merged.values()),'settings_revision':settings_revision}
        with self.records.begin() as tx:
            if SignalService._settings(tx) != (initial, settings_revision) or tx.read(REVIEWS,project) != prior:
                raise SQLiteUnitOfWorkConflict('signal_reviews_changed')
            if prior is None or prior.payload != payload:
                tx.put(REVIEWS,project,payload,expected_revision=prior.revision if prior else 0)
            tx.commit()
        return {'items':[self._render(item) for item in selected]}

    def decide(self, project, selections, *, by='user'):
        project = _project(project)
        if (by not in {'user','admin'} or not isinstance(selections,list) or not selections or len(selections)>5
                or any(not isinstance(item,dict) or set(item)!={'id','action','expected_revision'}
                    or not isinstance(item['id'],str) or not isinstance(item['action'],str)
                    or item['action'] not in {'confirm','dismiss'}
                    or type(item['expected_revision']) is not int or item['expected_revision']<1 for item in selections)
                or len({item['id'] for item in selections}) != len(selections)):
            raise HTTPException(400,'invalid_signal_decisions')
        for item in selections:
            _project(item['id'])
        current = self.current(project)
        visible = {item['id']:item for item in current['items']}
        if any(item['id'] not in visible or visible[item['id']]['revision']!=item['expected_revision'] for item in selections):
            raise HTTPException(409, detail={'code':'signal_reviews_changed','current':current})
        projection = self.records.read(REVIEWS,project)
        selected = {item['id']:item for item in projection.payload['items']}
        clock = self.now().astimezone(timezone.utc)
        with self.records.begin() as tx:
            settings, revision = SignalService._settings(tx)
            if not settings['enabled'] or revision != projection.payload['settings_revision'] or tx.read(REVIEWS,project)!=projection:
                raise SQLiteUnitOfWorkConflict('signal_reviews_changed')
            known = {row.payload['review_key'] for row in tx.list(DECISIONS)}
            results = []
            for request in selections:
                item = selected[request['id']]
                if item['review_key'] in known:
                    raise SQLiteUnitOfWorkConflict('signal_reviews_changed')
                for collection, identity, expected in item['guards']:
                    row = tx.read(collection,identity)
                    if (row.revision if row else 0) != expected:
                        raise SQLiteUnitOfWorkConflict('signal_reviews_changed')
                if request['action']=='confirm' and item['kind']=='unused':
                    obj = item['object']
                    if obj['kind']=='insight':
                        row = tx.read('recognitions',obj['id'])
                        if not self.service._qualified_recognition(tx,row).evidence_eligible:
                            raise SQLiteUnitOfWorkConflict('signal_reviews_changed')
                        pref = tx.read('recognition_recall_preferences',obj['id'])
                        set_preference(TransactionRecords(tx),WorkScope('local-user',project),obj['id'],
                            recognition_revision=obj['revision'],preference_revision=pref.revision if pref else 0,state='cooled')
                    else:
                        # Same SQLite writer lock prevents publication changes while
                        # the original read owner rechecks current visibility.
                        document = self.documents.read(obj['id'])
                        if (document is None or document.get('project_id') != project
                                or document.get('status') == 'archived' or document['revision'] != obj['revision']
                                or not LegacyDocumentVisibility.from_repository(self.documents,project_id=project).allows(document)):
                            raise SQLiteUnitOfWorkConflict('signal_reviews_changed')
                        pref = tx.read('v2_document_recall',obj['id'])
                        tx.put('v2_document_recall',obj['id'],{'state':'cooled','by':'user','changed_at':clock.isoformat()},
                               expected_revision=pref.revision if pref else 0)
                tx.put(DECISIONS,'decision-'+uuid4().hex,{'review_key':item['review_key'],'kind':item['kind'],
                    'action':request['action'],'turn_ids':item['turn_ids'],'object':item['object'],
                    'at':clock.isoformat(),'by':by},expected_revision=0)
                results.append({'id':item['id'],'state':'confirmed' if request['action']=='confirm' else 'dismissed'})
            tx.commit()
        return {'items':results}


def install_signal_review_routes(application, *, records, owner=None, service=None, documents=None, runtime_root=None):
    owner = owner or SignalReviews(records,service=service,documents=documents,runtime_root=runtime_root)
    application.state.signal_reviews = owner
    router = APIRouter(prefix='/api/v2/library/signal-reviews')
    @router.get('')
    def current(project_id: str):
        try:
            return owner.current(project_id)
        except SQLiteUnitOfWorkConflict:
            raise HTTPException(409,'signal_reviews_changed') from None
    @router.post('/decide')
    async def decide(request: Request):
        body = await _json(request)
        if set(body)!={'project_id','items'}:
            raise HTTPException(400,'invalid_signal_decisions')
        try:
            return owner.decide(body['project_id'],body['items'])
        except HTTPException as error:
            if error.status_code == 409 and isinstance(error.detail, dict):
                return JSONResponse(status_code=409,content={'detail':error.detail['code'],'current':error.detail['current']})
            raise
        except SQLiteUnitOfWorkConflict:
            return JSONResponse(status_code=409,content={'detail':'signal_reviews_changed','current':owner.current(body['project_id'])})
    application.include_router(router)
    return owner
