"""Event-only usage signals, isolated from learning and original revisions."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
import json
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request, Response
from starlette.background import BackgroundTask
from core.storage_provider import SQLiteUnitOfWorkConflict
from backend.recognition import WorkScope
from ..workspace_contracts import _json, _project
from .insights import resolve_insight
from .projects import scene_of

EVENTS = 'v2_signals'
ROLLUPS = 'v2_signal_rollups'
SETTINGS = 'v2_signal_settings'
RETENTION_DAYS = 180
_LOGGER = logging.getLogger(__name__)


def _time(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError('signal_time_invalid')
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class PreparedSignal:
    client_id: str
    payload: dict
    enabled: bool
    cleared_at: str | None


class SignalService:
    """All signal reads/writes share one short SQLite transaction owner."""
    def __init__(self, records, *, now=lambda: datetime.now(timezone.utc)):
        self.records, self.now = records, now

    @staticmethod
    def _settings(reader):
        row = reader.read(SETTINGS, 'local')
        value = row.payload if row else {'enabled': True, 'cleared_at': None}
        if (set(value) != {'enabled', 'cleared_at'} or type(value['enabled']) is not bool
                or value['cleared_at'] is not None and not isinstance(value['cleared_at'], str)):
            raise ValueError('signal_settings_invalid')
        if value['cleared_at'] is not None:
            _time(value['cleared_at'])
        return dict(value), row.revision if row else 0

    @staticmethod
    def rollups(reader):
        """Return explicit project/month ownership; never infer it from keys."""
        return reader.list(ROLLUPS)

    def _view(self, reader):
        value, revision = self._settings(reader)
        count = sum(row.payload['by'] == 'user' for row in reader.list(EVENTS))
        count += sum(sum(row.payload['counts'].values()) for row in self.rollups(reader))
        return {**value, 'count': count, 'retention_days': RETENTION_DAYS, 'revision': revision}

    def report(self, *, kernel_groups=(), read_model_input=None, since=None, until=None, project_id=None):
        """Project existing facts into identifier-only offline metrics."""
        return _signal_report(self, kernel_groups=kernel_groups, read_model_input=read_model_input,
                              since=since, until=until, project_id=project_id)

    def settings(self):
        with self.records.begin() as tx:
            return self._view(tx)

    def set_enabled(self, enabled, *, expected_revision):
        if type(enabled) is not bool or type(expected_revision) is not int or expected_revision < 0:
            raise HTTPException(400, 'invalid_signal_settings')
        with self.records.begin() as tx:
            value, revision = self._settings(tx)
            if revision != expected_revision:
                raise SQLiteUnitOfWorkConflict('signal_settings_changed')
            if value['enabled'] != enabled:
                tx.put(SETTINGS, 'local', {**value, 'enabled': enabled}, expected_revision=revision)
            result = self._view(tx)
            tx.commit()
            return result

    def clear(self, *, expected_revision):
        if type(expected_revision) is not int or expected_revision < 0:
            raise HTTPException(400, 'invalid_signal_settings')
        with self.records.begin() as tx:
            value, revision = self._settings(tx)
            if revision != expected_revision:
                raise SQLiteUnitOfWorkConflict('signal_settings_changed')
            cleared = sum(row.payload['by'] == 'user' for row in tx.list(EVENTS))
            cleared += sum(sum(row.payload['counts'].values()) for row in self.rollups(tx))
            for collection in (EVENTS, ROLLUPS):
                for row in tx.list(collection):
                    tx.delete(collection, row.object_id, expected_revision=row.revision)
            tx.put(SETTINGS, 'local', {**value, 'cleared_at': self.now().astimezone(timezone.utc).isoformat()}, expected_revision=revision)
            tx.commit()
            return {'cleared': cleared}

    @staticmethod
    def _qualified(reader, payload):
        project, kind, turn_id = payload['project_id'], payload['kind'], payload['turn_id']
        # Project registration is lazy; the actual scoped Turn/object is authority.
        scene = None
        if turn_id is not None:
            turn = reader.read('v2_turns', turn_id)
            if turn is None or turn.payload.get('project_id') != project:
                raise HTTPException(404, 'signal_turn_not_found')
            if kind == 'copy' and (turn.payload.get('intent') != 'ask'
                    or not isinstance(turn.payload.get('receipt', {}).get('ask', {}).get('answer'), str)):
                raise HTTPException(404, 'signal_answer_not_found')
            scene = turn.payload.get('scene')
        if kind == 'view':
            obj = payload['object']
            row = resolve_insight(reader, WorkScope('local-user', project), obj['id'])
            if (row is None or row.payload.get('state') != 'pending'
                    or reader.read('v2_candidate_merges', row.object_id)
                    or reader.read('v2_candidate_fade', row.object_id)):
                raise HTTPException(404, 'signal_candidate_not_found')
            if row.revision != obj['revision']:
                raise HTTPException(409, 'signal_object_changed')
            assignment = scene_of(reader, 'candidate', row.object_id)
            if assignment and assignment.get('project_id') == project:
                scene = assignment['scene']
        return scene

    def prepare(self, body, *, by='user', server=False):
        required = {'project_id', 'kind', 'client_id'}
        if (not isinstance(body, dict) or not required <= set(body)
                or set(body) - required - {'turn_id', 'object'}
                or not isinstance(body['kind'], str)
                or body['kind'] not in ({'copy', 'view', 'stop'} if server else {'copy', 'view'})
                or by not in {'user', 'admin'}):
            raise HTTPException(400, 'invalid_signal_fields')
        project, client = _project(body['project_id']), _project(body['client_id'])
        kind = body['kind']
        turn = body.get('turn_id')
        if turn is not None:
            turn = _project(turn)
        obj = body.get('object')
        if obj is not None:
            if (not isinstance(obj, dict) or set(obj) != {'kind', 'id', 'revision'}
                    or obj['kind'] != 'insight' or type(obj['revision']) is not int or obj['revision'] < 1):
                raise HTTPException(400, 'invalid_signal_object')
            obj = {**obj, 'id': _project(obj['id'])}
        if (kind in {'copy', 'stop'} and (turn is None or obj is not None)
                or kind == 'view' and obj is None):
            raise HTTPException(400, 'invalid_signal_fields')
        payload = {'kind': kind, 'project_id': project, 'scene': None, 'turn_id': turn,
            'object': obj, 'at': self.now().astimezone(timezone.utc).isoformat(), 'by': by}
        with self.records.begin() as tx:
            value, _ = self._settings(tx)
            if value['enabled']:
                payload['scene'] = self._qualified(tx, payload)
                self._identity(tx, client, payload)
        return PreparedSignal(client, payload, value['enabled'], value['cleared_at'])

    @staticmethod
    def _identity(reader, identity, payload):
        previous = reader.read(EVENTS, identity)
        if previous and any(previous.payload[key] != payload[key] for key in ('kind', 'project_id', 'turn_id', 'object', 'by')):
            raise HTTPException(409, 'signal_client_conflict')
        return previous

    @staticmethod
    def _payload(value):
        """Validate and detach the exact fact at the final write boundary."""
        keys = {'kind', 'project_id', 'scene', 'turn_id', 'object', 'at', 'by'}
        try:
            if (not isinstance(value, dict) or set(value) != keys
                    or not isinstance(value['kind'], str) or value['kind'] not in {'copy', 'view', 'stop'}
                    or not isinstance(value['by'], str) or value['by'] not in {'user', 'admin'}
                    or value['scene'] is not None and (not isinstance(value['scene'], str) or not value['scene'].strip())
                    or not isinstance(value['at'], str)):
                raise ValueError('invalid_signal_payload')
            _project(value['project_id'])
            _time(value['at'])
            if value['turn_id'] is not None:
                _project(value['turn_id'])
            obj = value['object']
            if obj is not None:
                if (not isinstance(obj, dict) or set(obj) != {'kind', 'id', 'revision'}
                        or obj['kind'] != 'insight' or type(obj['revision']) is not int or obj['revision'] < 1):
                    raise ValueError('invalid_signal_payload')
                _project(obj['id'])
            if (value['kind'] in {'copy', 'stop'} and (value['turn_id'] is None or obj is not None)
                    or value['kind'] == 'view' and obj is None):
                raise ValueError('invalid_signal_payload')
            return {**value, 'object': dict(obj) if obj is not None else None}
        except (HTTPException, TypeError, ValueError):
            raise ValueError('invalid_signal_payload') from None

    def record(self, prepared):
        if not prepared.enabled:
            return None
        payload = self._payload(prepared.payload)
        with self.records.begin() as tx:
            value, _ = self._settings(tx)
            if (not value['enabled'] or value['cleared_at'] != prepared.cleared_at
                    or value['cleared_at'] is not None and _time(payload['at']) <= _time(value['cleared_at'])):
                return None
            self._qualified(tx, payload)
            if self._identity(tx, prepared.client_id, payload):
                return None
            at = _time(payload['at'])
            for row in tx.list(EVENTS):
                old = row.payload
                if old['kind'] != payload['kind'] or old['project_id'] != payload['project_id'] or old['by'] != payload['by']:
                    continue
                old_at = _time(old['at'])
                if payload['kind'] == 'copy' and old['turn_id'] == payload['turn_id'] and abs(at - old_at) < timedelta(minutes=10):
                    return None
                if payload['kind'] == 'view' and old['object']['id'] == payload['object']['id'] and at.date() == old_at.date():
                    return None
                if payload['kind'] == 'stop' and old['turn_id'] == payload['turn_id']:
                    return None
            row = tx.put(EVENTS, prepared.client_id, payload, expected_revision=0)
            tx.commit()
            return row

    def safe_record(self, prepared):
        try:
            self.record(prepared)
        except Exception as error:
            _LOGGER.warning('signal_write_failed exception_type=%s', type(error).__name__)

    def rollup(self):
        cutoff = self.now().astimezone(timezone.utc) - timedelta(days=RETENTION_DAYS)
        with self.records.begin() as tx:
            expired = [row for row in tx.list(EVENTS) if _time(row.payload['at']) < cutoff]
            summaries = {(row.payload['project_id'], row.payload['month']): row for row in self.rollups(tx)}
            for event in expired:
                value = event.payload
                if value['by'] == 'user':
                    key = value['project_id'], _time(value['at']).strftime('%Y-%m')
                    previous = summaries.get(key)
                    counts = dict(previous.payload['counts']) if previous else {}
                    counts[value['kind']] = counts.get(value['kind'], 0) + 1
                    row = tx.put(ROLLUPS, previous.object_id if previous else 'rollup-' + uuid4().hex,
                        {'project_id': key[0], 'month': key[1], 'counts': counts}, expected_revision=previous.revision if previous else 0)
                    summaries[key] = row
                tx.delete(EVENTS, event.object_id, expected_revision=event.revision)
            tx.commit()


def install_signal_routes(application, *, records, service=None):
    owner = service or SignalService(records)
    application.state.memory_signals = owner
    router = APIRouter(prefix='/api/v2')

    @router.post('/signals')
    async def post_signal(request: Request):
        prepared = owner.prepare(await _json(request))
        return Response(status_code=204, background=BackgroundTask(owner.safe_record, prepared))

    @router.get('/settings/signals')
    def get_settings():
        return owner.settings()

    @router.patch('/settings/signals')
    async def patch_settings(request: Request):
        body = await _json(request)
        if set(body) != {'enabled', 'expected_revision'}:
            raise HTTPException(400, 'invalid_signal_settings')
        try:
            return owner.set_enabled(**body)
        except SQLiteUnitOfWorkConflict:
            raise HTTPException(409, 'signal_settings_changed') from None

    @router.post('/settings/signals/clear')
    async def clear_settings(request: Request):
        body = await _json(request)
        if set(body) != {'expected_revision'}:
            raise HTTPException(400, 'invalid_signal_settings')
        try:
            return owner.clear(**body)
        except SQLiteUnitOfWorkConflict:
            raise HTTPException(409, 'signal_settings_changed') from None

    application.include_router(router)
    return owner


# The projection uses existing readers only; it never creates a store or writes.
def _fact_time(value):
    try:
        return _time(value)
    except (ValueError, TypeError):
        return None


def _signal_report(owner, *, kernel_groups=(), read_model_input=None, since=None, until=None, project_id=None):
    from collections import Counter, defaultdict
    from statistics import median
    from .policies import get, version as selected_policy_version
    reader = owner.records
    initial, revision = owner._settings(reader)
    if not initial['enabled']:
        return {}
    cutoff = _fact_time(initial['cleared_at'])
    def eligible(payload, key='at', *, owner_project=None):
        at = _fact_time(payload.get(key))
        return (payload.get('by') != 'admin' and at is not None and (cutoff is None or at > cutoff)
                and (since is None or at >= since) and (until is None or at <= until)
                and (project_id is None or payload.get('project_id', payload.get('scope', {}).get('project_id', owner_project)) == project_id))
    turns = [row for row in reader.list('v2_turns') if eligible(row.payload, 'created_at')]
    asks = [row for row in turns if isinstance(row.payload.get('receipt', {}).get('ask'), dict)]
    events = [row for row in reader.list(EVENTS) if eligible(row.payload)]
    groups = {g['turn_id']: g for g in kernel_groups if isinstance(g, dict) and isinstance(g.get('turn_id'), str)}
    def generation_turn(payload):
        generation = payload.get('generation') or {}
        identity = generation.get('turn_id')
        if identity is None and generation.get('id'):
            # MemoryTurn metadata uses the UUID spelling of its exact memory-ID.
            try:
                from uuid import UUID
                identity = 'memory-' + UUID(generation['id']).hex
            except (ValueError, TypeError, AttributeError):
                return None
        group = groups.get(identity)
        project = payload.get('scope', {}).get('project_id', payload.get('project_id'))
        return identity if group and group.get('project_id') == project else None

    def event_origin(payload):
        identity = payload.get('turn_id')
        if payload.get('type') == 'answer_miss':
            return identity if identity in groups and groups[identity].get('project_id') == payload.get('project_id') else None
        collection = 'recognitions' if payload.get('object_kind') == 'recognition' else 'recognition_candidates'
        row = reader.read(collection, payload.get('object_id', ''))
        scope = {'user_id': payload.get('user_id', 'local-user'), 'project_id':payload.get('project_id')}
        if row is None or row.payload.get('scope') != scope:
            return None
        if collection == 'recognition_candidates':
            return generation_turn(row.payload) if row.revision == payload.get('object_revision') else None
        version = next((v for v in reader.list('recognition_versions')
                        if v.payload.get('recognition_id') == row.object_id
                        and v.payload.get('recognition_revision') == payload.get('object_revision')
                        and v.payload.get('snapshot',{}).get('scope') == scope), None)
        if version is None:
            return None
        candidates = [c for c in reader.list('recognition_candidates')
                      if c.payload.get('scope') == scope and c.payload.get('state') == 'published'
                      and c.payload.get('recognition_id') == row.object_id]
        identities = {generation_turn(c.payload) for c in candidates}
        return next(iter(identities)) if len(identities) == 1 else None

    def output_models(identity):
        group = groups.get(identity, {})
        kind = group.get('kind')
        purpose = 'primary' if kind in {'project.answer', 'project.task'} else 'aux' if kind in {
            'memory.organize', 'memory.propose_insights', 'memory.consolidate'} else None
        models = {call['model_id'] for call in group.get('calls', [])
                  if purpose is not None and call.get('model_call_purpose') == purpose
                  and call.get('status') == 'completed' and call.get('turn_id') == identity
                  and isinstance(call.get('model_id'), str)}
        # Missing purpose and multiple successful producers are unresolved;
        # do not assign responsibility to every participating model.
        return models if len(models) == 1 else set()

    def policies(identity):
        group = groups.get(identity, {})
        return group.get('request', {}).get('policy_versions', {})
    def policy(identity, interface):
        selected = policies(identity).get(interface)
        return interface + selected if isinstance(selected, str) and selected.startswith('@') and selected[1:].isdigit() else None
    unused = Counter()
    unknown = 0
    layer_counts = defaultdict(lambda: {'sent':0, 'cited':0, 'unknown_citations':0})
    for row in asks:
        receipt = row.payload['receipt']['ask']
        entries = receipt.get('context', {}).get('entries')
        citations = receipt.get('citations')
        frozen = read_model_input(row.object_id) if read_model_input is not None else None
        messages = frozen.get('messages') if isinstance(frozen, dict) else None
        if not isinstance(entries, list) or not isinstance(messages, list):
            unknown += 1
            continue
        cited = {(c.get('layer'), c.get('id')) for c in citations if isinstance(c, dict)} if isinstance(citations, list) else None
        number = 0
        for entry in entries:
            if not entry.get('persona'):
                number += 1
            layer = 'persona' if entry.get('persona') else entry.get('layer')
            if layer not in {'insight','summary','note','source','persona','inspiration'} or not isinstance(entry.get('id'), str):
                continue
            texts = [m.get('content','') for m in messages if isinstance(m,dict) and isinstance(m.get('content'),str)]
            if layer == 'persona':
                from ..context_adapter import format_recognition_content
                snapshots = [v.payload.get('snapshot',{}) for v in reader.list('recognition_versions')
                             if v.payload.get('recognition_id') == entry['id']
                             and v.payload.get('snapshot',{}).get('scope',{}).get('project_id') == 'me']
                proven = any(format_recognition_content(snapshot).rsplit('\n\n',1)[0] in text
                             for snapshot in snapshots for text in texts)
            else:
                prefix = '[' + str(number) + '] ' + str(entry.get('title', '')) + '\n'
                proven = any(prefix in text for text in texts) and isinstance(entry.get('title'),str)
            if not proven:
                unknown += 1
                continue
            key = (row.payload['project_id'], layer, entry['id'])
            unused[(key, 'sent')] += 1
            layer_counts[layer]['sent'] += 1
            if cited is None or layer == 'persona':
                unused[(key, 'unknown')] += 1
                layer_counts[layer]['unknown_citations'] += 1
            else:
                count = int((entry.get('layer'), entry['id']) in cited)
                unused[(key, 'cited')] += count
                layer_counts[layer]['cited'] += count
    objects = [{'project_id':p,'layer':l,'object_id':i,'sent':unused[((p,l,i),'sent')],
                'cited':None if unused[((p,l,i),'unknown')] else unused[((p,l,i),'cited')]}
               for p,l,i in sorted({key for key, kind in unused if kind == 'sent'})]
    reasks = []
    adjacent_pairs = set()
    previous = {}
    for row in sorted(asks, key=lambda x: (_time(x.payload['created_at']), x.object_id)):
        payload = row.payload
        project = payload['project_id']
        old = previous.get(project)
        if old:
            first, second = old.payload.get('user_text'), payload.get('user_text')
            if isinstance(first, str) and isinstance(second, str):
                adjacent_pairs.add((project, old.object_id, row.object_id))
                compared = get('reask')(first, second,
                    elapsed_seconds=(_time(payload['created_at']) - _time(old.payload['created_at'])).total_seconds())
                if compared['kind'] == 'reask':
                    reasks.append({'project_id':project,'turn_ids':[old.object_id,row.object_id],'score':compared['score']})
        previous[project] = row
    counterexamples = []
    for row in reader.list('v2_signal_decisions'):
        payload = row.payload
        identities = payload.get('turn_ids')
        if (payload.get('kind') != 'reask' or payload.get('action') != 'dismiss'
                or not isinstance(identities, list) or len(identities) != 2
                or not all(isinstance(identity, str) for identity in identities)):
            continue
        try:
            key = json.loads(payload.get('review_key', ''))
        except (ValueError, TypeError):
            continue
        if not isinstance(key, list) or len(key) != 3 or not isinstance(key[0], str) or key[1:] != ['reask', identities]:
            continue
        project = key[0]
        if (project, *identities) in adjacent_pairs and eligible(payload, owner_project=project):
            counterexamples.append({'project_id':project,'turn_ids':identities,'decision_id':row.object_id})
    after = []
    for row in asks:
        at = _time(row.payload['created_at'])
        after.append({'project_id':row.payload['project_id'],'turn_id':row.object_id,
            'copy':sum(e.payload.get('kind')=='copy' and e.payload.get('turn_id')==row.object_id for e in events),
            'do':sum(t.payload.get('intent')=='do' and t.payload['project_id']==row.payload['project_id']
                     and 0 < (_time(t.payload['created_at'])-at).total_seconds() <= 600 for t in turns)})
    opens, older = 0, 0
    for collection in ('v2_usage_document','v2_usage_recognition','v2_usage_candidate','v2_usage_insight'):
        for row in reader.list(collection):
            # older_count has no timestamps; report coverage, never assign it to a Turn.
            if row.payload.get('by') == 'admin' or project_id is not None and row.payload.get('project_id') != project_id:
                continue
            older += row.payload.get('older_count', 0)
            opens += sum(e.get('kind')=='open' and eligible(e, owner_project=row.payload.get('project_id')) for e in row.payload.get('events', []))
    dwell, dwell_groups = [], defaultdict(list)
    for row in reader.list('recognition_candidates'):
        payload = row.payload
        if not eligible(payload, 'created_at'):
            continue
        project = payload.get('scope', {}).get('project_id')
        if not project:
            continue
        fade, merge = reader.read('v2_candidate_fade',row.object_id), reader.read('v2_candidate_merges',row.object_id)
        state = payload.get('state')
        terminal = payload.get('reviewed_at') if state in {'published', 'rejected'} else None
        if state == 'pending' and merge:
            target = reader.read('recognition_candidates', merge.payload.get('candidate_id',''))
            if merge.payload.get('project_id') == project and target and target.payload.get('scope') == payload.get('scope'):
                state = 'merged'
                proposal = reader.read('recognition_restructure_proposals', merge.payload.get('proposal_id',''))
                if (proposal and proposal.payload.get('scope') == payload.get('scope')
                        and proposal.payload.get('state') == 'approved' and proposal.payload.get('operation') == 'merge'
                        and proposal.payload.get('snapshot',{}).get('pending_output') is True
                        and row.object_id in proposal.payload.get('snapshot',{}).get('target_recognition_ids',[])
                        and any(c.get('id') == row.object_id and c.get('revision') == row.revision
                                for c in proposal.payload.get('snapshot',{}).get('candidates',[]))
                        and merge.payload['candidate_id'] in proposal.payload.get('result_candidate_ids',[])):
                    terminal = proposal.payload.get('reviewed_at')
            else:
                state = 'unknown'
        elif state == 'pending' and fade:
            state, terminal = 'faded', fade.payload.get('faded_at')
        if state not in {'pending', 'published', 'rejected', 'merged', 'faded'}:
            state = 'unknown'
        start, end = _fact_time(payload['created_at']), _fact_time(terminal)
        seconds = (end-start).total_seconds() if end and end >= start else None
        version = policy(generation_turn(payload), 'extract')
        def valid_view(event):
            value, obj = event.payload, event.payload.get('object') or {}
            at = _fact_time(value.get('at'))
            return (value.get('kind') == 'view' and value.get('project_id') == project
                    and obj.get('kind') == 'insight' and obj.get('id') == row.object_id
                    and type(obj.get('revision')) is int and 1 <= obj['revision'] <= row.revision
                    and at is not None and at >= start and (end is None or at <= end))
        views = sum(valid_view(e) for e in events)
        item = {'project_id':project,'object_id':row.object_id,'state':state,'seconds':seconds,'views':views,'policy':version}
        dwell.append(item)
        if version:
            dwell_groups[version].append(item)
    dwell_summary = [{'policy':version,'objects':len(items),'known_durations':sum(i['seconds'] is not None for i in items),
        'median_seconds':median([i['seconds'] for i in items if i['seconds'] is not None]) if any(i['seconds'] is not None for i in items) else None,
        'faded_unhandled_rate':sum(i['state']=='faded' for i in items)/len(items)} for version,items in sorted(dwell_groups.items())]
    def organize_origins(document):
        items = [row for row in reader.list('workspace_items')
                 if row.payload.get('document_id') == document.object_id
                 and row.payload.get('project_id') == document.payload.get('project_id')]
        identities = []
        for step in reader.list('workspace_organize_steps'):
            payload = step.payload
            group = groups.get(payload.get('turn_id'), {})
            if not payload.get('output') or payload.get('rejected') or group.get('kind') != 'memory.organize':
                continue
            refs = group.get('request',{}).get('privacy',{}).get('material_refs',[])
            if any(ref.get('type') == 'original_item' and ref.get('id') == item.object_id for ref in refs for item in items):
                if group.get('project_id') == document.payload.get('project_id'):
                    identities.append(payload['turn_id'])
        return sorted(set(identities))

    def organize_origin(document):
        versions = {policy(identity, 'organize') for identity in organize_origins(document)}
        return next(iter(versions)) if len(versions) == 1 else None

    corrections, unknown_events = Counter(), 0
    output_counts = Counter()
    outputs = [(row.object_id, row.payload['project_id'], ('ask', row.object_id)) for row in asks
               if isinstance(row.payload['receipt']['ask'].get('answer'), str)]
    for row in reader.list('recognition_candidates'):
        if eligible(row.payload, 'created_at'):
            identity = generation_turn(row.payload)
            if identity:
                outputs.append((identity, row.payload['scope']['project_id'], ('candidate', row.object_id)))
    for document in reader.list('documents'):
        initial_revision = reader.read('document_revisions', document.object_id+'~r1')
        if initial_revision and eligible(initial_revision.payload, 'created_at', owner_project=document.payload.get('project_id')):
            for identity in organize_origins(document):
                outputs.append((identity, document.payload.get('project_id'), ('document',document.object_id)))
    counted_outputs = set()
    unknown_model_outputs = set()
    for identity, project, output_id in outputs:
        group = groups.get(identity, {})
        if group.get('project_id') != project:
            continue
        for interface in policies(identity):
            selected = policy(identity, interface)
            if selected and (output_id, 'policy', selected) not in counted_outputs:
                counted_outputs.add((output_id, 'policy', selected))
                output_counts[('policy', selected)] += 1
        models = output_models(identity)
        if not models:
            unknown_model_outputs.add(output_id)
        for model in models:
            if (output_id, 'model', model) not in counted_outputs:
                counted_outputs.add((output_id, 'model', model))
                output_counts[('model', model)] += 1
    seen = set()
    unknown_model_events = 0
    for collection in ('v2_correction_events','v2_context_feedback','v2_place_examples','v2_signal_decisions'):
        for row in reader.list(collection):
            payload = row.payload
            if collection == 'v2_signal_decisions':
                identities = payload.get('turn_ids')
                if (payload.get('action') != 'confirm' or payload.get('kind') not in {'reask', 'stop'}
                        or not isinstance(identities, list) or not identities):
                    continue
                turn = next((turn for turn in turns if turn.object_id == identities[0]), None)
                if turn is None:
                    continue
                payload = {**payload, 'type':'answer_miss', 'turn_id':turn.object_id,
                           'project_id':turn.payload.get('project_id')}
            if not eligible(payload):
                continue
            key = (payload.get('turn_id'),payload.get('object_kind'),payload.get('object_id'),payload.get('object_revision'),payload.get('type',payload.get('action')))
            if key in seen:
                continue
            seen.add(key)
            identity = event_origin(payload)
            values = [('policy',policy(identity,i)) for i in policies(identity)]
            models = output_models(identity)
            if not models:
                unknown_model_events += 1
            values += [('model', model) for model in models]
            values = set((kind,value) for kind,value in values if value)
            if not values:
                unknown_events += 1
            for key in values:
                corrections[key] += 1
    correction_groups = [{'dimension':kind,'id':identity,'corrections':corrections[(kind,identity)],
        'outputs':output_counts[(kind,identity)] or None,
        'rate':corrections[(kind,identity)]/output_counts[(kind,identity)] if output_counts[(kind,identity)] else None}
        for kind,identity in sorted(set(corrections)|set(output_counts))]
    edits = []
    ai_blocks = defaultdict(dict)
    ai_documents = set()
    for row in sorted(reader.list('document_revisions'), key=lambda r:(r.payload.get('document_id',''),r.payload.get('revision',0))):
        payload = row.payload
        identity = payload.get('document_id')
        document = reader.read('documents',identity) if identity else None
        if document is None or project_id is not None and document.payload.get('project_id') != project_id:
            continue
        changes = payload.get('changed_blocks',[])
        if payload.get('author')=='system':
            if payload.get('conflict',{}).get('status') == 'detected':
                continue
            ai_documents.add(identity)
            for change in changes:
                block = change.get('block',{})
                if isinstance(block.get('content'),str):
                    ai_blocks[identity][change.get('block_id')] = block['content']
        elif payload.get('author')=='user' and eligible(payload,'created_at', owner_project=document.payload.get('project_id')):
            changed = {c.get('block_id') for c in changes if identity in ai_documents
                       and c.get('block',{}).get('block_type') == 'paragraph'
                       and c.get('block',{}).get('content') != ai_blocks[identity].get(c.get('block_id'))}
            if changed:
                edits.append({'project_id':document.payload.get('project_id'),'object_id':identity,
                    'revision':payload.get('revision'),'paragraphs':len(changed),'policy':organize_origin(document)})
        if payload.get('author')=='user':
            ai_blocks[identity] = {c.get('block_id'):c.get('block',{}).get('content') for c in changes if isinstance(c.get('block',{}).get('content'),str)}
    edit_groups = [{'policy':version,'revisions':sum(e['policy']==version for e in edits),
                    'paragraphs':sum(e['paragraphs'] for e in edits if e['policy']==version)}
                   for version in sorted({e['policy'] for e in edits if e['policy']})]
    interruptions = []
    for row in turns:
        stops = sum(e.payload.get('kind')=='stop' and e.payload.get('turn_id')==row.object_id for e in events)
        steers = row.payload.get('steers')
        if stops or isinstance(steers,list):
            interruptions.append({'project_id':row.payload['project_id'],'turn_id':row.object_id,'stop':stops,
                'steer':sum(eligible(e, owner_project=row.payload['project_id']) for e in steers) if isinstance(steers,list) else None})
    result = {'unused':{'layers':{key:{**counts,'cited':None if counts['unknown_citations'] else counts['cited']} for key,counts in layer_counts.items()},'objects':objects,'unknown_turns':unknown},
        'corrections':{'groups':correction_groups,'unknown_events':unknown_events,
                       'unknown_model_outputs':len(unknown_model_outputs),'unknown_model_events':unknown_model_events},'reask':{'policy':'reask'+selected_policy_version('reask'),'pairs':reasks},
        'after_answer':{'turns':after,'opens':opens,'unlocated_uses':None if cutoff else older},
        'dwell':{'objects':sorted(dwell,key=lambda x:x['object_id']),'groups':dwell_summary},
        'document_edits':{'objects':edits,'groups':edit_groups,'unknown_versions':sum(e['policy'] is None for e in edits)},'interruptions':{'turns':interruptions}}
    if counterexamples:
        result['reask']['counterexamples'] = counterexamples
    # Another instance may have cleared/disabled while this projection was read.
    final, final_revision = owner._settings(reader)
    return result if final == initial and final_revision == revision else {}
